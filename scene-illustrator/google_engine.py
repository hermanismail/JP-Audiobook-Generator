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
    # not on the price page read on 2026-09-22; estimated at 3.8's rates
    "gemini-3.7-flash": {"in": 0.75, "out": 3.75},
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

    # A call that never answers must not hang the run: a read sat 10+ minutes
    # on chapter_010 with no reply, no retry and no error (user report
    # 2026-09-22). Normal calls take 8-60 s.
    TEXT_TIMEOUT_S = 240        # chapter_010 once took 119 s and succeeded
    IMAGE_TIMEOUT_S = 120
    # Pay-as-you-go Gemini draws on a pool shared with everyone (Google's
    # "dynamic shared quota"): no fixed calls-per-minute, but 429 / 503 / 504
    # when the pool is busy. Google's advice is exponential backoff with
    # jitter; retries 3 s and 6 s apart gave up inside a busy spell
    # (chapter_011, 2026-09-22). Waits before tries 2-5, plus up to 30% jitter:
    BACKOFF_S = (5, 15, 40, 90)

    def _make_client(self, timeout_s):
        from google import genai
        from google.genai import types
        if not self.s.get("google_project"):
            raise RuntimeError("no Google project set (google_project in settings.json)")
        return genai.Client(vertexai=True, project=self.s["google_project"],
                            location=self.s.get("google_location") or "global",
                            http_options=types.HttpOptions(timeout=timeout_s * 1000))

    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client(self.TEXT_TIMEOUT_S)
        return self._client

    @property
    def image_client(self):
        if getattr(self, "_image_client", None) is None:
            self._image_client = self._make_client(self.IMAGE_TIMEOUT_S)
        return self._image_client

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

    def _call(self, client, model, contents, config, what, fallback=""):
        """generate_content with patient retries on transport and capacity
        errors. After the first failure a `fallback` model takes over, if
        given: on 2026-09-22 gemini-3.8-flash answered chapter_011 in 92 s
        after three 500s while 3.7-flash took 11 s. Every failed try goes into
        the usage log with its error, so a bad run can be read back afterwards
        (the window log is not kept). Returns (response, model used)."""
        import random
        tries = len(self.BACKOFF_S) + 1
        last = None
        for attempt in range(tries):
            if attempt == 1 and fallback and fallback != model:
                self.log(f"SERVER {what}: {model} is busy - switching to {fallback}")
                model = fallback
            t0 = time.time()
            try:
                return client.models.generate_content(model=model, contents=contents,
                                                      config=config), model
            except Exception as e:  # noqa: BLE001 - network, 429, 5xx, timeout
                last = e
                code = getattr(e, "code", None) or type(e).__name__
                self._record_error(what, model, time.time() - t0, f"{code}: {str(e)[:160]}")
                # a request Google rejects as malformed will not improve by waiting
                # (499 is our own time limit cancelling the call - worth another try)
                if isinstance(code, int) and 400 <= code < 500 and code not in (408, 429, 499):
                    break
                if attempt + 1 < tries:
                    wait = self.BACKOFF_S[attempt] * (1 + random.random() * 0.3)
                    self.log(f"SERVER {what}: no answer after {time.time() - t0:.0f}s ({code}) - "
                             f"waiting {wait:.0f}s, try {attempt + 2} of {tries}")
                    time.sleep(wait)
                else:
                    self.log(f"SERVER {what}: no answer after {time.time() - t0:.0f}s ({code})")
        raise RuntimeError(f"{what}: Google did not answer after {tries} tries ({last})")

    def _record_error(self, what, model, secs, error):
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "error", "model": model,
               "what": what, "in": 0, "out": 0, "usd": 0, "secs": round(secs, 1), "error": error}
        path = usage_path(self.s)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

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
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0.3, response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        last = None
        for _ in range(2):          # a second go only for an answer that is not JSON
            t0 = time.time()
            resp, used = self._call(self.client, model, user, config, what,
                                    fallback=self.s.get("google_text_fallback", ""))
            why = self._why_empty(resp)
            row = self._record("text", used, resp, time.time() - t0, why)
            if why:
                raise Refused(f"{what}: {why}")
            try:
                return json.loads(resp.text), row
            except (ValueError, TypeError) as e:
                last = e
                self.log(f"SERVER {what}: the answer was not JSON - asking again")
        raise RuntimeError(f"{what}: Google's answer was not JSON ({last})")

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
            image_config=types.ImageConfig(aspect_ratio=self.s.get("google_aspect") or "2:3"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        t0 = time.time()
        resp, model = self._call(self.image_client, model, contents, config, what)
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
                if row.get("kind") == "error":      # a failed try: no answer, no charge
                    continue
                usd += row.get("usd", 0)
                calls += 1
                refused += 1 if row.get("refused") else 0
    return usd, calls, refused
