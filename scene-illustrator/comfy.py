"""
comfy.py
--------
The image engine: FLUX.2 [klein] 4B in the separate ComfyUI install at
F:\\ComfyUI, driven over its HTTP API.

Measured 2026-09-19 on the 4060 (see DESIGN.md §9):
  - up to 5 reference images fit; VRAM stays ~7.0-7.4 GB, TIME grows
    (1-3 refs ~17-26 s, 5 refs ~60 s), so MAX_REFS is 5;
  - the first image of a NEW prompt costs ~26 s (the text encoder is
    swapped back in), further takes of the same prompt ~10 s - draw all
    takes of one prompt in one run;
  - a prompt cannot keep colour out, so every reference and every output
    is converted to greyscale here (decision 22).
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image

MAX_REFS = 5
WIDTH, HEIGHT = 832, 1216
STEPS = 4


class ManagedComfy:
    """Starts ComfyUI only if nothing listens on the port, and only ever
    stops a server it started. Killed as a tree so VRAM comes back."""

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

    # ------------------------------------------------------------ drawing
    def input_dir(self):
        return os.path.join(self.s["comfy_root"], "input")

    def put_reference(self, path, name):
        """Copy a reference into ComfyUI/input as greyscale; return its name."""
        dst = os.path.join(self.input_dir(), name)
        os.makedirs(self.input_dir(), exist_ok=True)
        to_greyscale(path, dst)
        return name

    def graph(self, prompt, refs, seed, prefix):
        g = {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": self.s["flux_unet"], "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": self.s["flux_clip"], "type": "flux2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.s["flux_vae"]}},
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
            "14": {"class_type": "Flux2Scheduler", "inputs": {"steps": STEPS, "width": WIDTH, "height": HEIGHT}},
            "15": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
            "16": {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["12", 0], "guider": ["11", 0], "sampler": ["13", 0],
                              "sigmas": ["14", 0], "latent_image": ["15", 0]}},
            "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["3", 0]}},
            "18": {"class_type": "SaveImage", "inputs": {"images": ["17", 0], "filename_prefix": prefix}},
        })
        return g

    def draw(self, prompt, refs, seed, out_path):
        """One image, saved greyscale at out_path. Returns seconds taken."""
        t0 = time.time()
        prefix = "scene-illustrator/tmp"
        body = json.dumps({"prompt": self.graph(prompt, refs, seed, prefix)}).encode()
        req = urllib.request.Request(self.url + "/prompt", body, {"Content-Type": "application/json"})
        try:
            pid = json.load(urllib.request.urlopen(req))["prompt_id"]
        except urllib.error.HTTPError as e:
            raise RuntimeError("ComfyUI refused the job: " + e.read().decode()[:400])
        while True:
            history = json.load(urllib.request.urlopen(f"{self.url}/history/{pid}"))
            if pid in history:
                break
            time.sleep(1)
        record = history[pid]
        status = record["status"].get("status_str")
        if status != "success":
            raise RuntimeError(f"ComfyUI job {status}: {json.dumps(record['status'])[:400]}")
        images = [im for node in record.get("outputs", {}).values() for im in node.get("images", [])]
        if not images:
            raise RuntimeError("ComfyUI produced no image")
        src = os.path.join(self.s["comfy_root"], "output", images[0]["subfolder"], images[0]["filename"])
        to_greyscale(src, out_path)
        os.remove(src)
        return round(time.time() - t0, 1)


def to_greyscale(src, dst):
    """Every book is monochrome (decision 22): a prompt cannot keep colour
    out, so it is removed here, on the way in and on the way out."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with Image.open(src) as im:
        im.convert("L").save(dst, "PNG")


def copy_style_image(path, dst):
    if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        to_greyscale(path, dst)
    else:
        shutil.copy(path, dst)
    return dst
