"""
comfy.py
--------
The image engine: Qwen-Image-2.1 in the separate ComfyUI install at
F:\\ComfyUI, driven over its HTTP API. One model draws AND edits (user
decision 2026-09-21, after DESIGN.md M9/M10):

  - the prompt names each reference by its slot, <image1>, <image2>, ... -
    which is what lets a character sheet stand for one named person;
  - a draw samples on our portrait canvas; an edit samples on its base
    image's own grid (the encoder's latent), since any other size shifts the
    edit;
  - 25 steps, cfg 1, ~4 min a take with two references, ~5-6 with three.

Measured and relied on here:
  - 3 character sheets is the most that holds (M10): a 4th lost a character
    and peaked at 7.84 of 8 GB;
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

MAX_SHEETS = 3          # character sheets in one draw (M10)
MAX_REFS = MAX_SHEETS + 1   # an edit's base image + the sheets
WIDTH, HEIGHT = 832, 1216
STEPS = 25


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

    def check_models(self):
        """Say plainly which file is missing rather than failing inside a graph."""
        root = self.s["comfy_root"]
        # ComfyUI reads a diffusion model from either folder
        needed = [(("diffusion_models", "unet"), self.s["qwen_unet"]),
                  (("text_encoders", "clip"), self.s["qwen_clip"]), (("vae",), self.s["qwen_vae"])]
        missing = [n for subs, n in needed
                   if not any(os.path.isfile(os.path.join(root, "models", sub, n)) for sub in subs)]
        if missing:
            raise RuntimeError(f"model files missing from {root}\\models: " + ", ".join(missing))

    # -------------------------------------------------------------- graph
    def graph(self, prompt, refs, seed, prefix, width, height, edit):
        """Qwen-Image-2.1: the references go to the text encoder in slot order
        (<image1> is refs[0]); 25 steps, cfg 1, euler/simple - the shipped
        template's settings (M9)."""
        g = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.s["qwen_unet"]}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": self.s["qwen_clip"], "type": "qwen_image", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.s["qwen_vae"]}},
        }
        enc = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "negative_prompt": "",
               "resolution": 1024}
        for i, ref in enumerate(refs[:MAX_REFS], 1):
            g[str(100 + i)] = {"class_type": "LoadImage", "inputs": {"image": ref}}
            enc[f"images.image_{i}"] = [str(100 + i), 0]     # the node's growable inputs, API form
        g["7"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
        g["15"] = {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height,
                                                                "batch_size": 1}}
        g["16"] = {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["7", 0], "negative": ["7", 1],
            # an edit samples on its base image's own grid - any other size shifts it
            "latent_image": ["7", 2] if edit else ["15", 0],
            "seed": seed, "steps": STEPS, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "denoise": 1.0}}
        g["17"] = {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["3", 0]}}
        g["18"] = {"class_type": "SaveImage", "inputs": {"images": ["17", 0], "filename_prefix": prefix}}
        return g

    # ------------------------------------------------------------ drawing
    def run(self, prompt, refs, seed, out_path, on_progress=None, edit=False,
            width=WIDTH, height=HEIGHT):
        """One image, saved greyscale at out_path. `edit`: refs[0] is the image
        being changed and the output keeps its size. Returns seconds taken.
        Progress comes from ComfyUI's own WebSocket: on_progress(value, max)
        per sampler step, and (0, 0) when a node starts, since loading a model
        reports nothing."""
        t0 = time.time()
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
        body = json.dumps({"prompt": self.graph(prompt, refs, seed, prefix, width, height, edit),
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
