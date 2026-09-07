"""
translate_pipeline.py
---------------------
Generates an English subtitle track for each generated chapter, so the
player can show it alongside the Japanese reading text.

The approach, and why it is this way round: translation runs over the
*finished* `<chapter>.sync.json`, never over the raw text. By the time
sync.json exists the chunk boundaries are already fixed by the rendered
MP3, so the translation cannot desync - its timing is correct by
construction rather than by any alignment pass. English is emitted as a
separate sidecar and never written back into sync.json, which keeps the
change additive for everything downstream.

    read  <base>.sync.json          (never modified)
      -> translate chunk by chunk
      -> write <base>.translation.json    the artifact / source of truth
      -> write <base>.srt                 cheap re-emission for the player

**The invariant that makes this work: a chunk's index is sacred.** Text is
only ever filled in per existing index - chunks are never merged, split or
re-timed. Everything here is built so that stays true structurally: the
translation loop walks sync.json's own chunk list and zips results back
positionally, so a backend cannot renumber anything even if it wanted to.

Deliberately stdlib-only, so it runs unchanged in either venv - the
lightweight GUI one or the heavy Irodori-TTS one. The eventual local-LLM
backend talks HTTP to localhost, which `urllib` already covers, so that
stays true.

Invocation:
    - Standalone, over everything generated so far:
        uv run --project <this folder> python translate_pipeline.py --all
    - One chapter:
        uv run --project <this folder> python translate_pipeline.py --chapter chapter_002
    - Re-emit the .srt from an existing .translation.json without
      re-translating (e.g. after fixing a line by hand):
        uv run --project <this folder> python translate_pipeline.py --all --srt-only

Reads settings.json itself for `output_folder`, the same way
mp3_metadata.py does, so no arguments beyond the above are needed.
"""

import os
import re
import glob
import contextlib
import json
import time
import threading
import datetime
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# Bumped only when the on-disk shape of a .translation.json changes in a way
# a reader would have to care about.
TRANSLATION_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# The backend seam
# ---------------------------------------------------------------------------
# Every backend is a function taking the chapter's Japanese strings in
# order and returning the same number of English strings, in the same
# order. That signature is the entire contract, and it is what keeps
# local-now / API-later a one-line change.
#
# It also quietly enforces the index invariant: the caller zips the result
# back onto sync.json's own chunk list positionally, so a backend has no
# way to renumber, merge or drop a chunk. A backend that returns the wrong
# count is rejected outright rather than silently shifting every subtitle
# after the mistake.


def translate_identity(japanese_texts, *, control=None, on_chunk=None,
                       base_name=None, **_kwargs):
    """Passthrough 'translation' - hands the Japanese straight back.

    This is not a placeholder to be deleted. It is how the artifact
    format, the SRT emitter and the player round-trip get validated
    without a model in the loop at all, and it stays useful afterwards as
    the control case when a real backend starts producing something odd."""
    for i, _ in enumerate(japanese_texts, start=1):
        if control is not None:
            control.checkpoint()
        if on_chunk is not None:
            on_chunk(base_name, i, len(japanese_texts))
    return list(japanese_texts)


# --- VNTL-Llama3-8B-v2, served locally by llama.cpp's llama-server ---------
#
# VNTL is a completion model, not an instruction-following one: you do not
# ask it to translate, you write the beginning of a Japanese/English
# transcript and let it finish the English side. That has three consequences
# that shape everything below.
#
# 1. It cannot take a batch of indexed lines and hand back indexed lines, so
#    it is driven one chunk at a time. That is not a compromise - it makes
#    misalignment impossible, because the model never sees an index and the
#    pairing comes from this loop rather than from parsing a response.
# 2. Its prompt uses the LLaMA 3 token scheme but with custom header roles -
#    Metadata, Japanese, English - rather than system/user/assistant. No
#    chat-completions API can express that; it would apply its own template
#    and corrupt the prompt. Hence /completion with a raw string.
# 3. Its metadata block IS the glossary and its alternating history IS the
#    rolling context window. Both are native to the format rather than
#    bolted on.

LLAMA_SERVER_URL = "http://127.0.0.1:8080"

# How many previous chunk pairs to carry as context. The spec's guidance is
# 10-20; each pair is roughly 100 JA chars plus its English, so 12 sits
# comfortably inside an 8k context alongside the glossary.
VNTL_CONTEXT_PAIRS = 12

# Generous, because a long chunk can produce a long sentence, but bounded so
# a runaway generation cannot stall a 314-chunk chapter indefinitely.
VNTL_MAX_TOKENS = 400
VNTL_TIMEOUT_SECONDS = 180

_BOT = "<|begin_of_text|>"
_SH, _EH, _EOT = "<|start_header_id|>", "<|end_header_id|>", "<|eot_id|>"


def _section(role, body):
    return f"{_SH}{role}{_EH}\n\n{body}{_EOT}"


def load_glossary(output_folder):
    """Book-level glossary, hand-editable, living beside the chapters.

    Persisted precisely so chapter 15 does not rename a character
    introduced in chapter 2 - and so a human can correct a reading the
    model keeps getting wrong, once, in one place. Absent file is fine and
    means an empty metadata block."""
    path = os.path.join(output_folder, "glossary.json")
    if not os.path.exists(path):
        return {"characters": [], "notes": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("characters", [])
    data.setdefault("notes", [])
    return data


def build_metadata_block(glossary):
    """Renders the glossary into VNTL's native metadata section.

    Gender is a trained-in slot rather than decoration: Japanese drops
    pronouns constantly, and without it the model guesses "he" for a
    character it has been told nothing about."""
    lines = []
    for ch in glossary.get("characters", []):
        parts = [f"[character] Name: {ch.get('en') or ch.get('name', '')}"]
        if ch.get("name") and ch.get("en"):
            parts[0] = f"[character] Name: {ch['en']} ({ch['name']})"
        if ch.get("gender"):
            parts.append(f"Gender: {ch['gender']}")
        if ch.get("aliases"):
            parts.append(f"Aliases: {ch['aliases']}")
        lines.append(" | ".join(parts))
    lines.extend(glossary.get("notes", []))
    return "\n".join(lines)


def build_vntl_prompt(japanese, history, glossary):
    """One prompt: metadata, then the previous pairs as completed
    Japanese/English sections, then the current Japanese line with an empty
    English header for the model to continue from."""
    parts = [_BOT, _section("Metadata", build_metadata_block(glossary))]
    for prev_ja, prev_en in history:
        parts.append(_section("Japanese", prev_ja))
        parts.append(_section("English", prev_en))
    parts.append(_section("Japanese", japanese))
    # Deliberately unterminated - this is where generation begins.
    parts.append(f"{_SH}English{_EH}\n\n")
    return "".join(parts)


def _llama_completion(prompt, url=LLAMA_SERVER_URL):
    """Raw completion against llama-server. Temperature 0 and no repetition
    penalty are the model author's explicit recommendation - a repetition
    penalty is actively harmful for translation, since it punishes the
    legitimately recurring tokens (character names, particles) and pushes
    the model into renaming or omitting them."""
    import urllib.request

    payload = json.dumps({
        "prompt": prompt,
        "temperature": 0.0,
        "repeat_penalty": 1.0,
        "n_predict": VNTL_MAX_TOKENS,
        "stop": [_EOT, _SH],
        "cache_prompt": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/completion", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=VNTL_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))["content"].strip()


def translate_vntl(japanese_texts, *, settings=None, base_name=None,
                   progress=True, control=None, on_chunk=None, **_kwargs):
    import urllib.error

    settings = settings or {}
    # Endpoint is a setting so the server can live on another box (or port)
    # without editing code - useful once the 8GB card is the bottleneck.
    url = settings.get("llama_server_url") or LLAMA_SERVER_URL
    glossary = (load_glossary(settings["output_folder"])
                if settings.get("output_folder") else {"characters": [], "notes": []})
    history = []
    english = []

    for i, japanese in enumerate(japanese_texts, start=1):
        if control is not None:
            control.checkpoint()
        prompt = build_vntl_prompt(japanese, history, glossary)
        try:
            line = _llama_completion(prompt, url=url)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise BackendUnavailable(
                f"cannot reach llama-server at {url} ({e}). Start it with the "
                f"VNTL model loaded, or switch the backend to 'identity'.") from e
        english.append(line)
        history.append((japanese, line))
        if len(history) > VNTL_CONTEXT_PAIRS:
            history = history[-VNTL_CONTEXT_PAIRS:]
        if on_chunk is not None:
            on_chunk(base_name, i, len(japanese_texts))
        if progress and (i % 10 == 0 or i == len(japanese_texts)):
            print(f"    {base_name or ''} {i}/{len(japanese_texts)} chunk(s) translated")

    return english


BACKENDS = {
    "identity": translate_identity,
    "vntl": translate_vntl,
}


# ---------------------------------------------------------------------------
# llama-server lifecycle
# ---------------------------------------------------------------------------
# The 8GB card holds either the TTS model or the translation model, not both,
# so llama-server should only be resident while translation is actually
# running. Managing it here rather than in the GUI means both callers get it:
# the "Generate Subtitles Now" button and run_audiobook.py's end-of-run pass.
#
# The rule that matters: only ever stop a server we started ourselves. If one
# is already listening - because the person is using it for something else -
# it gets used as-is and left alone.

# Fallbacks for keys a settings.json written before this feature existed will
# not contain. run_audiobook.load_settings() merges its own defaults the same
# way; this module reads settings.json directly (like mp3_metadata.py), so it
# has to do its own merge or an older file silently yields empty paths.
SETTING_DEFAULTS = {
    "llama_server_url": "http://127.0.0.1:8080",
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llama_model_path": r"C:\llama.cpp\models\vntl-llama3-8b-v2-hf-q5_k_m.gguf",
    "translation_backend": "vntl",
}


def merge_setting_defaults(settings):
    merged = dict(SETTING_DEFAULTS)
    merged.update({k: v for k, v in (settings or {}).items() if v not in (None, "")})
    return merged


LLAMA_SERVER_ARGS = ["-ngl", "99", "-c", "8192"]
LLAMA_STARTUP_TIMEOUT = 240   # loading ~6GB onto the card is not instant
LLAMA_HEALTH_POLL = 2.0


def llama_server_reachable(url, timeout=2.0):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout):
            return True
    except Exception:
        return False


class ManagedLlamaServer:
    """Context manager that guarantees the server is stopped again.

    A `finally`-backed shutdown is the whole point: a crash, a failed
    chapter or a Ctrl-C midway through a book must not leave 6GB of VRAM
    locked up, because the next thing the person does is usually start a TTS
    run."""

    def __init__(self, settings, log=print):
        settings = merge_setting_defaults(settings)
        self.url = settings["llama_server_url"]
        self.exe = settings["llama_server_exe"]
        self.model = settings["llama_model_path"]
        self.log = log
        self.process = None          # only set when WE started it
        self.server_log_path = None

    def __enter__(self):
        if llama_server_reachable(self.url):
            self.log(f"  llama-server already running at {self.url} - using it "
                     f"(it will be left running).")
            return self

        if not self.exe or not os.path.isfile(self.exe):
            raise BackendUnavailable(
                f"nothing is listening at {self.url} and llama-server was not "
                f"found at {self.exe!r}. Set 'llama-server path' in Advanced "
                f"settings, or start the server yourself.")
        if not self.model or not os.path.isfile(self.model):
            raise BackendUnavailable(
                f"llama-server model not found at {self.model!r}. Set 'Model "
                f"(GGUF) path' in Advanced settings.")

        host, port = self._split_url(self.url)
        # The server is chatty on startup and its output is what tells you why
        # a model failed to load, so keep it, but in its own file rather than
        # drowning the translation progress.
        self.server_log_path = os.path.join(
            os.path.dirname(self.model) or ".",
            f"llama-server_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
        cmd = [self.exe, "-m", self.model, *LLAMA_SERVER_ARGS,
               "--host", host, "--port", str(port)]

        self.log(f"  starting llama-server ({os.path.basename(self.model)})...")
        try:
            handle = open(self.server_log_path, "w", encoding="utf-8")
        except OSError:
            handle = subprocess.DEVNULL
        self._server_log_handle = handle
        self.process = subprocess.Popen(cmd, stdout=handle,
                                        stderr=subprocess.STDOUT)

        deadline = time.time() + LLAMA_STARTUP_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise BackendUnavailable(
                    f"llama-server exited immediately (code "
                    f"{self.process.returncode}). See {self.server_log_path}")
            if llama_server_reachable(self.url):
                self.log(f"  llama-server ready at {self.url}")
                return self
            time.sleep(LLAMA_HEALTH_POLL)

        self.stop()
        raise BackendUnavailable(
            f"llama-server did not become ready within {LLAMA_STARTUP_TIMEOUT}s. "
            f"See {self.server_log_path}")

    def __exit__(self, *_exc):
        self.stop()
        return False    # never swallow the original exception

    def stop(self):
        if self.process is None:
            return      # not ours - leave it alone
        self.log("  stopping llama-server (freeing VRAM)...")
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        except Exception as e:
            self.log(f"  warning: could not stop llama-server cleanly: {e}")
        finally:
            self.process = None
            handle = getattr(self, "_server_log_handle", None)
            if handle not in (None, subprocess.DEVNULL):
                try:
                    handle.close()
                except Exception:
                    pass

    @staticmethod
    def _split_url(url):
        from urllib.parse import urlparse
        parsed = urlparse(url if "//" in url else f"http://{url}")
        return parsed.hostname or "127.0.0.1", parsed.port or 8080


class TranslationCancelled(RuntimeError):
    """Raised out of the chunk loop when the caller asks to stop."""


class TranslationControl:
    """Pause / cancel handle for a translation running in a background
    thread, checked between chunks.

    Pausing deliberately leaves llama-server up: the point is to hand the
    desktop back for a while without paying the model-load cost again on
    resume. Cancelling unwinds through ManagedLlamaServer's context
    manager, which is what actually frees the VRAM."""

    def __init__(self):
        self._resume = threading.Event()
        self._resume.set()                # not paused
        self._cancelled = threading.Event()

    def pause(self):
        self._resume.clear()

    def resume(self):
        self._resume.set()

    def cancel(self):
        self._cancelled.set()
        self._resume.set()                # unblock a paused worker so it can exit

    @property
    def paused(self):
        return not self._resume.is_set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def checkpoint(self):
        """Blocks while paused; raises if cancelled. Called between chunks,
        so the longest a stop can take is one chunk (a couple of seconds)."""
        if self._cancelled.is_set():
            raise TranslationCancelled("stopped by user")
        self._resume.wait()
        if self._cancelled.is_set():
            raise TranslationCancelled("stopped by user")


class BackendUnavailable(RuntimeError):
    """The backend itself cannot be reached, so every remaining chapter would
    fail the same way. Raised instead of a bare connection error so
    generate_subtitles() can stop rather than grinding through the whole book
    producing the identical failure once per chapter."""


class TranslationResult:
    """What generate_subtitles() did, for the caller to report on."""

    def __init__(self):
        self.written = []       # base names that got a fresh translation
        self.srt_only = []      # base names re-emitted from existing json
        self.missing = []       # base names with no .sync.json
        self.skipped = []       # base names whose .srt already existed
        self.errors = []        # (base_name, message)
        self.cancelled = False

    @property
    def total(self):
        return len(self.written) + len(self.srt_only)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_chapter_bases(output_folder):
    """Every chapter in the folder, whether or not it can actually be
    translated yet.

    Deliberately the union of the MP3s and the sync.json files rather than
    just the latter: a chapter whose sync.json is missing is a chapter with
    a *problem*, and it should be reported as skipped rather than quietly
    vanishing from the run because discovery never saw it.

    Sorted so chapter_002 follows chapter_001, which matters once rolling
    context between chapters is added - a later chapter should be able to
    see what came before it."""
    bases = set()
    for pattern, suffix in ((os.path.join(output_folder, "*.sync.json"), ".sync.json"),
                            (os.path.join(output_folder, "*.mp3"), ".mp3")):
        for path in glob.glob(pattern):
            bases.add(os.path.basename(path)[: -len(suffix)])
    return sorted(bases)


def sync_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.sync.json")


def translation_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.translation.json")


def srt_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.srt")


# ---------------------------------------------------------------------------
# SRT emission
# ---------------------------------------------------------------------------

def format_timestamp(seconds):
    """SRT wants HH:MM:SS,mmm - comma before the milliseconds, not a dot,
    and hours always present even for a short chapter. Chapter 2 of the
    reference book runs past two hours, so the hour field genuinely gets
    used rather than being decorative."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(doc):
    """One cue per chunk, which is what the player already handles - its
    splitCueBySentence() breaks a multi-sentence cue up on its own and
    shares the cue window out by length, so nothing here needs to.

    Cue numbers are 1-based per the SRT convention while chunk indices are
    0-based; the chunk's own index travels in the .translation.json, so
    nothing downstream has to infer one from the other."""
    blocks = []
    for cue_number, chunk in enumerate(doc["chunks"], start=1):
        start = float(chunk["start"])
        end = float(chunk["end"])
        # A zero-or-negative-length cue is silently dropped by some players
        # rather than reported, so give it a floor instead of emitting
        # something that would just vanish.
        if end <= start:
            end = start + 0.001
        text = (chunk.get("en") or "").strip()
        if not text:
            # Keep the cue rather than renumbering everything after it -
            # an empty subtitle is a visible gap, a missing one is a
            # silent desync. Blank cues are why this uses a placeholder.
            text = "​"  # zero-width space: renders as nothing
        blocks.append(
            f"{cue_number}\n"
            f"{format_timestamp(start)} --> {format_timestamp(end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def write_srt(path, doc):
    """UTF-8 with no BOM and LF line endings, written deterministically.

    newline="" stops Windows turning every \\n into \\r\\n behind our back,
    so the bytes on disk are the same wherever this runs. Python's "utf-8"
    codec writes no BOM (that would be "utf-8-sig"), which is what the
    player's parser expects - a BOM would end up glued to the first cue
    number and break it."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(build_srt(doc))


# ---------------------------------------------------------------------------
# The translation artifact
# ---------------------------------------------------------------------------

def build_translation_doc(base_name, sync_data, english_texts, backend_name,
                          model_name=None):
    """Pairs each sync.json chunk with its English text.

    Timings are copied in rather than referenced so the file is
    self-contained: --srt-only can re-emit a subtitle from this alone,
    without sync.json needing to still be around or still agree. The
    trade-off is that these timings go stale if the chapter is ever
    re-rendered, which is why `source_sync` records what they came from and
    a full (non --srt-only) run always re-reads sync.json."""
    chunks = sync_data["chunks"]
    if len(english_texts) != len(chunks):
        raise ValueError(
            f"backend returned {len(english_texts)} line(s) for "
            f"{len(chunks)} chunk(s) - refusing to write a translation "
            f"that would shift every subtitle after the mismatch")

    return {
        "version": TRANSLATION_FORMAT_VERSION,
        "chapter": base_name,
        "source_sync": f"{base_name}.sync.json",
        "source_version": sync_data.get("version"),
        "backend": backend_name,
        "model": model_name,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "chunks": [
            {
                "index": chunk["index"],
                "start": chunk["start"],
                "end": chunk["end"],
                "ja": chunk["text"],
                "en": english,
            }
            for chunk, english in zip(chunks, english_texts)
        ],
    }


def write_translation(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def read_translation(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_subtitles(settings, base_names=None, backend="identity",
                       srt_only=False, model_name=None, verbose=True,
                       limit=None, control=None, log=None, on_chunk=None,
                       on_chapter_done=None, skip_existing=True):
    """Translates the named chapters (or every rendered one) and writes a
    .translation.json plus a .srt for each, into output_folder alongside
    the MP3 and sync.json.

    sync.json is only ever read. Nothing in this module opens it for
    writing, and the English text never goes near its `text` field."""
    output_folder = settings["output_folder"]
    result = TranslationResult()

    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r} - available: {', '.join(sorted(BACKENDS))}")
    translate = BACKENDS[backend]

    if base_names is None:
        base_names = find_chapter_bases(output_folder)

    # Only the vntl backend needs a model server, and only when actually
    # translating - a --srt-only re-emission touches no model at all.
    needs_server = backend == "vntl" and not srt_only
    server_log = (lambda m: log(m.strip(), "processing")) if log else print
    server_ctx = (ManagedLlamaServer(settings, log=server_log) if needs_server
                  else contextlib.nullcontext())

    try:
        with server_ctx:
            _translate_chapters(base_names, output_folder, translate, settings,
                                backend, model_name, srt_only, limit, verbose,
                                result, control, log, on_chunk, on_chapter_done,
                                skip_existing)
    except TranslationCancelled:
        result.cancelled = True
        if log:
            log("Stopped by user.", "error")
    except BackendUnavailable as e:
        # Raised while bringing the server up, before any chapter was
        # attempted. Recorded like any other failure rather than thrown, so
        # the CLI prints a readable message instead of a traceback.
        result.errors.append(("(llama-server startup)", str(e)))
        if verbose:
            print(f"  ! {e}")
    return result


def _translate_chapters(base_names, output_folder, translate, settings, backend,
                        model_name, srt_only, limit, verbose, result,
                        control=None, log=None, on_chunk=None,
                        on_chapter_done=None, skip_existing=False):
    def say(message, tag="text"):
        if log:
            log(message, tag)
        elif verbose:
            print(message)

    for base_name in base_names:
        if control is not None:
            control.checkpoint()
        try:
            if skip_existing and not srt_only and os.path.exists(
                    srt_path_for(output_folder, base_name)):
                # Deliberately additive: an existing subtitle may have been
                # corrected by hand, and silently regenerating over it would
                # throw that work away.
                say(f"{base_name}: .srt already exists - skipping.", "text")
                result.skipped.append(base_name)
                if on_chapter_done:
                    on_chapter_done(base_name, "skipped")
                continue
            if srt_only:
                json_path = translation_path_for(output_folder, base_name)
                if not os.path.exists(json_path):
                    result.missing.append(base_name)
                    continue
                doc = read_translation(json_path)
                write_srt(srt_path_for(output_folder, base_name), doc)
                result.srt_only.append(base_name)
                if verbose:
                    print(f"  {base_name}: re-emitted {len(doc['chunks'])} cue(s) from "
                          f"existing translation")
                continue

            sync_file = sync_path_for(output_folder, base_name)
            if not os.path.exists(sync_file):
                say(f"{base_name}: no .sync.json found - skipping.", "error")
                result.missing.append(base_name)
                if on_chapter_done:
                    on_chapter_done(base_name, "missing")
                continue

            with open(sync_file, "r", encoding="utf-8") as f:
                sync_data = json.load(f)

            chunks = sync_data.get("chunks", [])
            if not chunks:
                result.errors.append((base_name, "sync.json contains no chunks"))
                continue

            japanese = [c["text"] for c in chunks]
            if limit is not None and limit < len(japanese):
                # Sampling run: translate the first N chunks and leave the
                # rest blank rather than truncating the chapter. Every chunk
                # keeps its index and timing, so the .srt still lines up with
                # the audio - it just goes quiet past the sample. Being able
                # to hear 20 chunks without waiting for 314 is what makes
                # prompt and glossary iteration practical.
                english = translate(japanese[:limit], base_name=base_name,
                                    settings=settings, control=control,
                                    on_chunk=on_chunk) + [""] * (len(japanese) - limit)
            else:
                english = translate(japanese, base_name=base_name,
                                    settings=settings, control=control,
                                    on_chunk=on_chunk)

            doc = build_translation_doc(base_name, sync_data, english, backend,
                                        model_name=model_name)
            write_translation(translation_path_for(output_folder, base_name), doc)
            write_srt(srt_path_for(output_folder, base_name), doc)
            result.written.append(base_name)
            say(f"{base_name}: {len(chunks)} chunk(s) -> {base_name}.translation.json "
                f"+ {base_name}.srt", "success")
            if on_chapter_done:
                on_chapter_done(base_name, "written")

        except TranslationCancelled:
            raise
        except BackendUnavailable as e:
            # No point attempting the remaining chapters - they would all
            # fail identically and bury the real cause under repetition.
            result.errors.append((base_name, str(e)))
            if verbose:
                print(f"  ! {base_name}: {e}")
                print("  Stopping: the backend is unavailable, so the remaining "
                      "chapters would fail the same way.")
            break
        except Exception as e:  # one bad chapter shouldn't abandon the rest
            result.errors.append((base_name, str(e)))
            if verbose:
                print(f"  ! {base_name}: {e}")


if __name__ == "__main__":
    # Mirrors mp3_metadata.py's entry point: re-reads settings.json itself
    # so the GUI (or run_audiobook.py at the end of a run) can shell out to
    # `uv run --project <here> python translate_pipeline.py --all` without
    # having to pass any configuration through.
    import argparse

    _parser = argparse.ArgumentParser(
        description="Generate English subtitle tracks from generated chapters.")
    _group = _parser.add_mutually_exclusive_group(required=True)
    _group.add_argument("--chapter", default=None,
                        help="Chapter base name (e.g. chapter_002) to translate.")
    _group.add_argument("--all", action="store_true",
                        help="Translate every chapter that has a .sync.json.")
    _parser.add_argument("--backend", default="identity",
                         choices=sorted(BACKENDS),
                         help="Translation backend (default: identity, which "
                              "passes the Japanese through unchanged).")
    _parser.add_argument("--limit", type=int, default=None,
                         help="Translate only the first N chunks of each "
                              "chapter, leaving the rest blank. For sampling "
                              "quality without waiting for a whole chapter.")
    _parser.add_argument("--regenerate", action="store_true",
                         help="Overwrite chapters that already have an .srt. "
                              "Off by default, so an existing subtitle - which "
                              "may have been corrected by hand - is left alone.")
    _parser.add_argument("--srt-only", action="store_true",
                         help="Re-emit each .srt from its existing "
                              ".translation.json without translating again.")
    _args = _parser.parse_args()

    with open(SETTINGS_PATH, "r", encoding="utf-8") as _f:
        _settings = merge_setting_defaults(json.load(_f))

    _bases = [_args.chapter] if _args.chapter else None

    print(f"Output folder: {_settings['output_folder']}")
    print(f"Backend: {_args.backend}"
          + ("  (--srt-only)" if _args.srt_only else "")
          + ("  (--regenerate: existing .srt files will be overwritten)"
             if _args.regenerate else ""))

    _result = generate_subtitles(_settings, base_names=_bases,
                                 backend=_args.backend, srt_only=_args.srt_only,
                                 limit=_args.limit,
                                 skip_existing=not _args.regenerate)

    print(f"Wrote subtitles for {_result.total} chapter(s).")
    if _result.missing:
        _what = "translation" if _args.srt_only else "sync.json"
        print(f"{len(_result.missing)} chapter(s) had no {_what} yet: "
              + ", ".join(_result.missing))
    if _result.errors:
        print(f"{len(_result.errors)} chapter(s) failed:")
        for _name, _err in _result.errors:
            print(f"  - {_name}: {_err}")
