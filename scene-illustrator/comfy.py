"""
comfy.py
--------
The image engines, both in the separate ComfyUI install at F:\\ComfyUI and
driven over its HTTP API. v2 uses two, because each is clearly better at one
job (measured 2026-09-20, DESIGN.md v2):

  DRAW  FLUX.2 klein 9B (GGUF)  - draws a chapter's samples from a prompt,
        with the style sample attached. 4 steps, ~30 s a take.
  EDIT  FLUX.1 Kontext dev (GGUF) - changes one thing in an existing image and
        leaves the rest alone. 20 steps, ~165 s a take. It ignores a style
        image: it inherits the style of the image it edits.

Only one is on the card at a time; ComfyUI unloads the other by itself.

Also measured and relied on here:
  - up to 5 reference images fit in 8 GB; time grows with the count, VRAM
    does not;
  - a prompt cannot keep colour out, so every reference and every output is
    converted to greyscale (v2 decision v5).
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import websocket
from PIL import Image

MAX_REFS = 5
WIDTH, HEIGHT = 832, 1216
DRAW_STEPS = 4
EDIT_STEPS = 20
EDIT_GUIDANCE = 2.5


class ManagedComfy:
    """Starts ComfyUI only if nothing listens on the port, and only ever stops
    a server it started. Killed as a tree so VRAM comes back."""

    def __init__(self, settings, log):
        self.s = settings
        self.log = log
        self.proc = None
        self.url = f"http://127.0.0.1:{settings['comfy_port']}"

    def _listening(self):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", int(self.s["comfy_port"]))) == 0

    def __enter__(self):
        if self._listening():
            self.log(f"SERVER ComfyUI already listening on port {self.s['comfy_port']} - using it as-is")
            return self
        python = os.path.join(self.s["comfy_root"], ".venv", "Scripts", "python.exe")
        main = os.path.join(self.s["comfy_root"], "main.py")
        for path in (python, main):
            if not os.path.isfile(path):
                raise RuntimeError(f"ComfyUI not found: {path}")
        self.log("SERVER starting ComfyUI")
        self.proc = subprocess.Popen(
            [python, "-u", main, "--listen", "127.0.0.1", "--port", str(self.s["comfy_port"])],
            cwd=self.s["comfy_root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        t0 = time.time()
        while time.time() - t0 < 300:
            if self.proc.poll() is not None:
                raise RuntimeError(f"ComfyUI exited with code {self.proc.returncode}")
            try:
                urllib.request.urlopen(self.url + "/system_stats", timeout=2).read()
                self.log(f"SERVER ComfyUI ready in {time.time() - t0:.0f}s")
                return self
            except (urllib.error.URLError, OSError):
                time.sleep(2)
        self.__exit__(None, None, None)
        raise RuntimeError("ComfyUI did not become ready in 300 s")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
            self.log("SERVER ComfyUI stopped")
        self.proc = None
        return False

    # ------------------------------------------------------------ helpers
    def input_dir(self):
        return os.path.join(self.s["comfy_root"], "input")

    def put_reference(self, path, name):
        """Copy a reference into ComfyUI/input as greyscale; return its name."""
        to_greyscale(path, os.path.join(self.input_dir(), name))
        return name

    def check_models(self, mode):
        """Say plainly which file is missing rather than failing inside a graph."""
        root = self.s["comfy_root"]
        needed = ([("unet", self.s["draw_unet"]), ("text_encoders", self.s["draw_clip"]),
                   ("vae", self.s["draw_vae"])] if mode == "draw" else
                  [("unet", self.s["edit_unet"]), ("text_encoders", self.s["edit_clip_l"]),
                   ("text_encoders", self.s["edit_clip_t5"]), ("vae", self.s["edit_vae"])])
        missing = [n for sub, n in needed if not os.path.isfile(os.path.join(root, "models", sub, n))]
        if missing:
            raise RuntimeError(f"{mode} model files missing from {root}\\models: " + ", ".join(missing))

    # ------------------------------------------------------------- graphs
    def draw_graph(self, prompt, refs, seed, prefix, width, height):
        """FLUX.2 klein 9B: reference latents, 4 steps, cfg 1."""
        g = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.s["draw_unet"]}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": self.s["draw_clip"], "type": "flux2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.s["draw_vae"]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
            "8": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
        }
        pos, neg = ["7", 0], ["8", 0]
        for i, ref in enumerate(refs[:MAX_REFS]):
            b = 100 + 10 * i
            g[str(b)] = {"class_type": "LoadImage", "inputs": {"image": ref}}
            g[str(b + 1)] = {"class_type": "ImageScaleToTotalPixels",
                             "inputs": {"image": [str(b), 0], "upscale_method": "nearest-exact",
                                        "megapixels": 1.0, "resolution_steps": 1}}
            g[str(b + 2)] = {"class_type": "VAEEncode", "inputs": {"pixels": [str(b + 1), 0], "vae": ["3", 0]}}
            g[str(b + 3)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [str(b + 2), 0]}}
            g[str(b + 4)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": [str(b + 2), 0]}}
            pos, neg = [str(b + 3), 0], [str(b + 4), 0]
        g.update({
            "11": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": pos, "negative": neg, "cfg": 1.0}},
            "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "13": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "14": {"class_type": "Flux2Scheduler", "inputs": {"steps": DRAW_STEPS, "width": width, "height": height}},
            "15": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "16": {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["12", 0], "guider": ["11", 0], "sampler": ["13", 0],
                              "sigmas": ["14", 0], "latent_image": ["15", 0]}},
            "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["3", 0]}},
            "18": {"class_type": "SaveImage", "inputs": {"images": ["17", 0], "filename_prefix": prefix}},
        })
        return g

    def edit_graph(self, prompt, refs, seed, prefix, width, height):
        """FLUX.1 Kontext dev: the image(s) to work from, 20 steps, guidance 2.5."""
        g = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.s["edit_unet"]}},
            "2": {"class_type": "DualCLIPLoader",
                  "inputs": {"clip_name1": self.s["edit_clip_l"], "clip_name2": self.s["edit_clip_t5"],
                             "type": "flux", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.s["edit_vae"]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
            "9": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["7", 0], "guidance": EDIT_GUIDANCE}},
            "8": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
        }
        pos = ["9", 0]
        for i, ref in enumerate(refs[:MAX_REFS]):
            b = 110 + 10 * i
            g[str(b)] = {"class_type": "LoadImage", "inputs": {"image": ref}}
            g[str(b + 1)] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [str(b), 0]}}
            g[str(b + 2)] = {"class_type": "VAEEncode", "inputs": {"pixels": [str(b + 1), 0], "vae": ["3", 0]}}
            g[str(b + 3)] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [str(b + 2), 0]}}
            pos = [str(b + 3), 0]
        g.update({
            "15": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "16": {"class_type": "KSampler",
                   "inputs": {"model": ["1", 0], "positive": pos, "negative": ["8", 0], "latent_image": ["15", 0],
                              "seed": seed, "steps": EDIT_STEPS, "cfg": 1.0, "sampler_name": "euler",
                              "scheduler": "simple", "denoise": 1.0}},
            "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["3", 0]}},
            "18": {"class_type": "SaveImage", "inputs": {"images": ["17", 0], "filename_prefix": prefix}},
        })
        return g

    # ------------------------------------------------------------ drawing
    def run(self, mode, prompt, refs, seed, out_path, on_progress=None,
            width=WIDTH, height=HEIGHT):
        """One image through `mode` ("draw" or "edit"), saved greyscale at
        out_path. Returns seconds taken. Progress comes from ComfyUI's own
        WebSocket: on_progress(value, max) per sampler step, and (0, 0) when a
        node starts, since loading a model reports nothing."""
        t0 = time.time()
        graph = self.draw_graph if mode == "draw" else self.edit_graph
        prefix = "scene-illustrator/tmp"
        client_id = uuid.uuid4().hex
        ws = None
        if on_progress:
            try:
                ws = websocket.WebSocket()
                ws.connect(self.url.replace("http://", "ws://") + f"/ws?clientId={client_id}", timeout=10)
                ws.settimeout(1.0)
            except (OSError, websocket.WebSocketException):
                ws = None
        body = json.dumps({"prompt": graph(prompt, refs, seed, prefix, width, height),
                           "client_id": client_id}).encode()
        req = urllib.request.Request(self.url + "/prompt", body, {"Content-Type": "application/json"})
        try:
            pid = json.load(urllib.request.urlopen(req))["prompt_id"]
        except urllib.error.HTTPError as e:
            if ws:
                ws.close()
            raise RuntimeError("ComfyUI refused the job: " + e.read().decode()[:400])
        node = None
        while True:
            if ws:
                try:
                    message = ws.recv()
                    if isinstance(message, str):
                        data = json.loads(message)
                        payload = data.get("data") or {}
                        if payload.get("prompt_id") not in (None, pid):
                            continue
                        if data.get("type") == "progress":
                            on_progress(payload.get("value", 0), payload.get("max", 1))
                        elif data.get("type") == "executing" and payload.get("node") not in (None, node):
                            node = payload["node"]
                            on_progress(0, 0)
                except websocket.WebSocketTimeoutException:
                    pass
                except (OSError, websocket.WebSocketException, ValueError):
                    ws.close()
                    ws = None
            history = json.load(urllib.request.urlopen(f"{self.url}/history/{pid}"))
            if pid in history:
                break
            if not ws:
                time.sleep(1)
        if ws:
            ws.close()
        record = history[pid]
        status = record["status"].get("status_str")
        if status != "success":
            raise RuntimeError(f"ComfyUI job {status}: {json.dumps(record['status'])[:400]}")
        images = [im for node_out in record.get("outputs", {}).values() for im in node_out.get("images", [])]
        if not images:
            raise RuntimeError("ComfyUI produced no image")
        src = os.path.join(self.s["comfy_root"], "output", images[0]["subfolder"], images[0]["filename"])
        to_greyscale(src, out_path)
        os.remove(src)
        return round(time.time() - t0, 1)


def to_greyscale(src, dst):
    """Every book is monochrome: a prompt cannot keep colour out, so it is
    removed here, on the way in and on the way out."""
    folder = os.path.dirname(dst)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with Image.open(src) as im:
        im.convert("L").save(dst, "PNG")


def copy_style_image(path, dst):
    if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        to_greyscale(path, dst)
    else:
        shutil.copy(path, dst)
    return dst
