"""
google_engine.py
----------------
Gemini on Google's Agent Platform (formerly Vertex AI), for reading, prompt
writing, drawing and editing (DESIGN.md M11, user decision 2026-09-22).

  text   gemini-3.8-flash      reads a WHOLE chapter in ~20 s
  image  gemini-3.1-flash-image (Nano Banana 2) - 12-21 s a take, sheets as
         reference images; refuses the book's violent/sexual scenes, which is
         why the local engine stays as the fallback

Login is the machine's Application Default Credentials
(`gcloud auth application-default login`) - no key in any file. It has to be
Agent Platform, not an AI Studio key: the user's trial began after
2026-03-02, and such trial credits cannot pay for AI Studio.

Every call is appended to <work_root>/google_usage.jsonl with its tokens and
an estimated cost, refused or not, so the window can show a running total.
The rates are the published list prices; the bill is the truth.
"""

import io
import json
import os
import time

from PIL import Image

# USD per 1M tokens (list prices, 2026-09-22). Thinking tokens bill as output.
RATES = {
    "gemini-3.8-flash": {"in": 0.75, "out": 3.75},
    "gemini-3.1-flash-image": {"in": 0.50, "out": 3.00, "image_out": 60.0},
}


class Refused(Exception):
    """Google returned no answer for a content reason - not worth retrying
    with the same prompt."""


class GoogleEngine:
    def __init__(self, settings, log):
        self.s = settings
        self.log = log
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google import genai
            if not self.s.get("google_project"):
                raise RuntimeError("no Google project set (google_project in settings.json)")
            self._client = genai.Client(vertexai=True, project=self.s["google_project"],
                                        location=self.s.get("google_location") or "global")
        return self._client

    # ------------------------------------------------------------ records
    def _record(self, kind, model, resp, secs, refused=""):
        u = getattr(resp, "usage_metadata", None)
        tin = getattr(u, "prompt_token_count", 0) or 0
        tout = (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        rate = RATES.get(model, {})
        if kind == "image" and not refused:
            # an image's output tokens are billed at the image rate
            cost = tin * rate.get("in", 0) / 1e6 + tout * rate.get("image_out", rate.get("out", 0)) / 1e6
        else:
            cost = tin * rate.get("in", 0) / 1e6 + tout * rate.get("out", 0) / 1e6
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "model": model,
               "in": tin, "out": tout, "usd": round(cost, 5), "secs": round(secs, 1),
               "refused": refused}
        path = usage_path(self.s)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    @staticmethod
    def _why_empty(resp):
        fb = getattr(resp, "prompt_feedback", None)
        if fb and getattr(fb, "block_reason", None):
            return f"prompt blocked ({fb.block_reason})"
        if not resp.candidates:
            return "no answer"
        reason = str(getattr(resp.candidates[0], "finish_reason", "") or "")
        return "" if reason.endswith("STOP") else reason.replace("FinishReason.", "")

    # --------------------------------------------------------------- text
    def json_call(self, system, user, what="text"):
        """One JSON answer. A transport error is retried twice; a refusal is not."""
        from google.genai import types
        model = self.s["google_text_model"]
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.3,
                                             response_mime_type="application/json")
        last = None
        for attempt in range(3):
            t0 = time.time()
            try:
                resp = self.client.models.generate_content(model=model, contents=user, config=config)
            except Exception as e:  # noqa: BLE001 - network, quota, 5xx
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            why = self._why_empty(resp)
            row = self._record("text", model, resp, time.time() - t0, why)
            if why:
                raise Refused(f"{what}: {why}")
            try:
                return json.loads(resp.text), row
            except (ValueError, TypeError) as e:
                last = e
        raise RuntimeError(f"{what}: Google did not answer ({last})")

    # -------------------------------------------------------------- image
    def image(self, parts, out_path, what="image"):
        """One image from `parts` (text strings and image paths, in order),
        saved greyscale at out_path - every book is monochrome (v2 decision
        v5), so the colour original is not kept. Returns seconds taken."""
        from google.genai import types
        model = self.s["google_image_model"]
        contents = []
        for p in parts:
            if isinstance(p, str) and os.path.isfile(p):
                with open(p, "rb") as f:
                    contents.append(types.Part.from_bytes(data=f.read(), mime_type=_mime(p)))
            else:
                contents.append(p)
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(aspect_ratio=self.s.get("google_aspect") or "2:3"))
        t0 = time.time()
        last = None
        for attempt in range(3):
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
                break
            except Exception as e:  # noqa: BLE001 - network, quota, 5xx
                last = e
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"{what}: Google did not answer ({last})")
        data = [p.inline_data.data for c in (resp.candidates or []) for p in ((c.content and c.content.parts) or [])
                if getattr(p, "inline_data", None) and p.inline_data.data]
        if not data:
            why = self._why_empty(resp) or "no image returned"
            self._record("image", model, resp, time.time() - t0, why)
            raise Refused(f"{what}: {why}")
        self._record("image", model, resp, time.time() - t0)
        folder = os.path.dirname(out_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with Image.open(io.BytesIO(data[0])) as im:
            im.convert("L").save(out_path, "PNG")
        return round(time.time() - t0, 1)


def _mime(path):
    ext = os.path.splitext(path)[1].lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")


def usage_path(settings):
    return os.path.join(settings["work_root"], "google_usage.jsonl")


def spent(settings):
    """(USD, calls, refused) over the whole usage log - the window's running total."""
    usd = calls = refused = 0
    path = usage_path(settings)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                usd += row.get("usd", 0)
                calls += 1
                refused += 1 if row.get("refused") else 0
    return usd, calls, refused
