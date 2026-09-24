"""
dynrepair.py
------------
The engine behind the Dynamic Repair window (2026-09-18). No Tk in here.

Repairs chapters rendered in the generator's DYNAMIC PROFILE mode, where
every sync.json entry ("Part" on the reader) is one TTS request - a
sentence or a piece of one - and `<chapter>.render.json` records exactly
how each was made: the text the engine got, the request (a duration scale
or a length in seconds), the band, the seed, the silence before it, and
the profile, style and seiyuu.

That record is what makes this a different tool from `chapter-repair/`
(which stays as it is, for the pre-dynamic library):

- **Nothing to guess.** The seiyuu, the style and every band come from
  render.json, so a replacement is asked for exactly like its neighbours
  were - or deliberately differently, in any of the six styles or a typed
  scale.
- **The text the TTS reads can be edited** without touching what the
  reader shows: a name or a rare kanji is spelled out in kana for the
  engine, while sync.json, the .srt and the translations keep the book's
  wording. The request stays keyed to the ORIGINAL text (its band, and
  for an even-pace style its length), because the meaning - and so the
  speaking time - has not changed.
- **readings.json** (per book, in the folder) remembers such fixes. It
  does not exist until the first repair that uses one; after that it is
  found when the folder is parsed and appended to.

## The folder contract (user decisions 2026-09-18)

The user copies everything into ONE folder and points the tool at it. The
tool works IN PLACE and makes no backups: the folder is a copy, and the
originals are the undo. Per chapter:

    required   chapter_NNN.flac  .m4a  .sync.json  .render.json
    optional   chapter_NNN.srt  .translation.json   (re-timed when present)
    per book   readings.json                        (optional)

## Why splicing is safe here too

Exactly the chapter-repair argument, and its code (`repair.splice_many`,
`encode_m4a`, `copy_tags`, `build_time_map`, the Whisper scan): every
part sits between pure silences whose lengths the profile sets (0.7 / 1.0
/ 1.5 s), sync.json names every boundary, and a repair moves everything
after it by ONE number. The FLAC is the splice source - the generator's
own lossless master - so every repair is one AAC encode from lossless,
however many times a chapter is repaired.
"""

import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
REPAIR_DIR = os.path.join(GENERATOR_DIR, "chapter-repair")
for _path in (GENERATOR_DIR, REPAIR_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import dynamic_profile  # noqa: E402
import repair  # noqa: E402  - chapter-repair's engine, reused one-way

DEFAULT_SETTINGS = {
    "irodori_root": "C:\\Irodori-TTS",
    "batch_script": "",
    "whisper_exe": "C:\\Transcribe\\.venv\\Scripts\\whisper.exe",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ja",
    "device": "cuda",
    # The generator's default; a repaired chapter must match its neighbours.
    "aac_bitrate": "64k",
    # Takes, transcripts and scratch - never inside the user's folder.
    "work_root": "F:\\tmp\\dynamic-repair",
    "similarity_threshold": 0.72,
    # The profiler found a part this short cannot show a hallucination to
    # Whisper (one misheard character is already a 15% miss), so it is
    # never flagged.
    "min_judged_chars": 7,
    "last_folder": "",
}
READINGS_FILE = "readings.json"
CHAPTER_FILE_RE = re.compile(r"^(chapter_\d+)\.(flac|m4a|sync\.json|render\.json|srt|"
                             r"translation\.json)$", re.I)
REQUIRED = (".flac", ".m4a", ".sync.json", ".render.json")
OPTIONAL = (".srt", ".translation.json")
# A FLAC and its sync.json disagreeing by more than this means they are not
# the same render - splicing would cut in the wrong place.
DURATION_TOLERANCE = 0.05
CUSTOM_STYLE = "custom"


# ------------------------------------------------------------ settings

def merge_setting_defaults(data):
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (data or {}).items() if k in DEFAULT_SETTINGS})
    return merged


def load_settings():
    if not os.path.isfile(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return merge_setting_defaults(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merge_setting_defaults(data), f, ensure_ascii=False, indent=2)


def batch_script(settings):
    return (settings.get("batch_script") or "").strip() or \
        os.path.join(GENERATOR_DIR, "irodori_batch.py")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    """Written beside, then swapped in, so a crash mid-write never leaves a
    half file where a whole one was."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------ the folder

def tool_problems(settings):
    """What this machine lacks, independent of any folder."""
    problems = []
    if not os.path.isdir(settings["irodori_root"]):
        problems.append(f"Irodori-TTS not found at {settings['irodori_root']}")
    if not os.path.isfile(batch_script(settings)):
        problems.append(f"TTS worker not found: {batch_script(settings)}")
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            problems.append(f"{exe} is not on PATH")
    return problems


def _chapter_status(folder, base, settings):
    """{base, ok, missing, optional, problems, warnings} for one chapter."""
    status = {"base": base, "ok": False, "missing": [], "optional": [], "problems": [],
              "warnings": []}
    for ext in REQUIRED:
        if not os.path.isfile(os.path.join(folder, base + ext)):
            status["missing"].append(base + ext)
    for ext in OPTIONAL:
        if os.path.isfile(os.path.join(folder, base + ext)):
            status["optional"].append(base + ext)
    render_path = os.path.join(folder, base + ".render.json")
    if not os.path.isfile(render_path):
        if not status["missing"] or status["missing"] == [base + ".render.json"]:
            status["problems"].append("no render.json - not a dynamic chapter; use the old "
                                      "Chapter Repair tool")
        return status
    if status["missing"]:
        return status
    try:
        render = _read_json(render_path)
        sync = _read_json(os.path.join(folder, base + ".sync.json"))
    except (OSError, ValueError) as e:
        status["problems"].append(f"unreadable json: {e}")
        return status
    if render.get("mode") != "dynamic" or not render.get("pieces") or not render.get("profile"):
        status["problems"].append("render.json is not a dynamic render record")
        return status
    by_index = {p.get("sync_index") for p in render["pieces"] if p.get("sync_index") is not None}
    missing_parts = [c["index"] for c in sync.get("chunks", []) if c["index"] not in by_index]
    if missing_parts:
        status["problems"].append(f"render.json has no record for part(s) "
                                  f"{', '.join(str(i + 1) for i in missing_parts[:5])} - "
                                  f"it does not belong to this sync.json")
    speaker = render["profile"].get("speaker_path", "")
    if not os.path.isfile(speaker):
        status["problems"].append(f"the seiyuu file is missing: {speaker}")
    elif render["profile"].get("speaker_stamp_at_render") and \
            dynamic_profile.speaker_stamp(speaker) != render["profile"]["speaker_stamp_at_render"]:
        status["warnings"].append("the seiyuu file changed since this chapter was rendered - "
                                  "a repair may not sound like its neighbours")
    status["ok"] = not status["problems"]
    return status


def parse_folder(folder, settings):
    """Everything the window shows after a folder is chosen:

        {ok, message, book, chapters: [status], ready: [base],
         readings_path, readings: [...] or None, tool_problems}

    `ok` is True when at least one chapter is ready and the machine has
    what a repair needs. A chapter that is not ready is listed with what
    it lacks and cannot be opened."""
    out = {"ok": False, "message": "", "book": "", "chapters": [], "ready": [],
           "readings_path": "", "readings": None, "tool_problems": tool_problems(settings)}
    if not folder or not os.path.isdir(folder):
        out["message"] = "Choose the folder that holds the chapter files to repair."
        return out
    folder = os.path.abspath(folder)
    out["book"] = os.path.basename(folder)
    bases = sorted({m.group(1) for m in (CHAPTER_FILE_RE.match(n) for n in os.listdir(folder))
                    if m})
    if not bases:
        out["message"] = "No chapter files in this folder (chapter_NNN.flac, .m4a, " \
                         ".sync.json, .render.json)."
        return out
    out["chapters"] = [_chapter_status(folder, b, settings) for b in bases]
    out["ready"] = [c["base"] for c in out["chapters"] if c["ok"]]
    out["readings_path"] = os.path.join(folder, READINGS_FILE)
    if os.path.isfile(out["readings_path"]):
        try:
            out["readings"] = load_readings(out["readings_path"])
        except (OSError, ValueError) as e:
            out["tool_problems"].append(f"readings.json is unreadable: {e}")
    blocked = [c for c in out["chapters"] if not c["ok"]]
    if out["tool_problems"]:
        out["message"] = "Cannot repair on this machine: " + "; ".join(out["tool_problems"])
    elif not out["ready"]:
        out["message"] = f"No chapter here can be repaired - {len(blocked)} chapter(s) " \
                         f"have something missing (listed below)."
    else:
        out["ok"] = True
        out["message"] = f"All is there - {len(out['ready'])} chapter(s) ready" + \
            (f", {len(blocked)} not (listed below)" if blocked else "") + "."
    return out


# ------------------------------------------------------------ readings

def load_readings(path):
    """[{word, reading, added, chapter}] - longest word first, which is the
    order they must be applied in (a name inside a longer name). The same
    file the generator applies at render time (dynamic_profile)."""
    return dynamic_profile.load_readings(path)


def apply_readings(text, readings):
    return dynamic_profile.apply_readings(text, readings)[0]


def merge_readings(path, book, new_items, chapter):
    """Appends (or updates) entries and returns what changed. Creates the
    file on the first repair that uses a reading - never before."""
    data = {"version": 1, "book": book, "readings": []}
    if os.path.isfile(path):
        data = _read_json(path)
        data.setdefault("readings", [])
    by_word = {r["word"]: r for r in data["readings"]}
    changed = []
    for word, reading in new_items.items():
        now = time.strftime("%Y-%m-%d %H:%M")
        if word in by_word:
            if by_word[word]["reading"] != reading:
                by_word[word].update({"reading": reading, "updated": now, "chapter": chapter})
                changed.append((word, reading, "updated"))
        else:
            entry = {"word": word, "reading": reading, "added": now, "chapter": chapter}
            data["readings"].append(entry)
            by_word[word] = entry
            changed.append((word, reading, "added"))
    if changed:
        _write_json(path, data)
    return changed


# ------------------------------------------------------------ a chapter

class DynChapter:
    """One dynamic chapter in the user's folder.

    Duck-types what chapter-repair's scan functions read (`settings`,
    `work_dir`, `audio_source()`, `chunks()`, `base`, `name`), so its
    Whisper pass and scoring are reused rather than copied."""

    def __init__(self, settings, folder, base):
        self.settings = settings
        self.folder = os.path.abspath(folder)
        self.book = os.path.basename(self.folder)
        self.base = base
        self.flac = os.path.join(self.folder, base + ".flac")
        self.m4a = os.path.join(self.folder, base + ".m4a")
        self.sync_path = os.path.join(self.folder, base + ".sync.json")
        self.render_path = os.path.join(self.folder, base + ".render.json")
        self.srt_path = os.path.join(self.folder, base + ".srt")
        self.translation_path = os.path.join(self.folder, base + ".translation.json")
        self.readings_path = os.path.join(self.folder, READINGS_FILE)
        self.work_dir = os.path.join(settings["work_root"], self.book, base)
        self.reload()

    @property
    def name(self):
        return f"{self.book}/{self.base}"

    def reload(self):
        self.sync = _read_json(self.sync_path)
        self.render = _read_json(self.render_path)
        self.profile = self.render["profile"]
        self.style = self.render.get("style", dynamic_profile.DEFAULT_STYLE)
        self._pieces = {p["sync_index"]: p for p in self.render["pieces"]
                        if p.get("sync_index") is not None}

    def audio_source(self):
        return self.flac

    def chunks(self):
        return self.sync["chunks"]

    @property
    def total(self):
        return len(self.sync["chunks"])

    def part(self, index):
        """Everything about one part. `part` is what the reader shows
        (index + 1); `index` is sync.json's own."""
        chunk = self.sync["chunks"][index]
        piece = self._pieces[index]
        return {
            "index": index, "part": index + 1, "start": chunk["start"], "end": chunk["end"],
            "duration": round(chunk["end"] - chunk["start"], 3), "text": chunk.get("text", ""),
            "tts_text": piece["tts_text"], "engine_len": piece["engine_len"],
            "band": piece.get("band"), "request": piece["request"],
            "used_seed": piece.get("used_seed"), "gap_before": piece.get("gap_before"),
            "repairs": len(piece.get("repairs") or []),
            # Readings already inside tts_text: applied by the generator at
            # render time, or by an earlier repair ({} for older renders).
            "readings": dict(piece.get("readings") or {}),
        }

    def check_audio(self):
        """None, or why the FLAC cannot be trusted as this sync.json's audio."""
        flac = repair.audio_duration(self.flac)
        last = self.sync["chunks"][-1]["end"]
        if abs(flac - last) > DURATION_TOLERANCE:
            return (f"{self.base}.flac is {flac:.2f}s but sync.json ends at {last:.2f}s - they "
                    f"are not from the same render, so a splice would cut in the wrong place")
        return None

    # --- finding a part
    def find_part(self, number):
        """The reader's Part N -> index, or None."""
        return number - 1 if 1 <= number <= self.total else None

    def find_time(self, seconds):
        """The part playing at `seconds`; in a silence, the part after it."""
        for chunk in self.sync["chunks"]:
            if chunk["start"] <= seconds < chunk["end"]:
                return chunk["index"]
        later = [c for c in self.sync["chunks"] if c["start"] >= seconds]
        return (later[0] if later else self.sync["chunks"][-1])["index"]

    def parts_with_readings(self, readings):
        """[(index, [words])] - parts whose engine text still holds a word
        readings.json knows a reading for."""
        out = []
        for index in range(self.total):
            tts = self._pieces[index]["tts_text"]
            words = [r["word"] for r in readings or [] if r["word"] in tts]
            if words:
                out.append((index, words))
        return out

    # --- requests
    def request_for(self, index, style, custom_scale=None):
        """What to ask the engine for, from the piece's recorded
        `engine_len`. For a chapter rendered before 2026-09-24 that is the
        book's own wording; for one rendered after, it is the text as it
        was SPOKEN (furigana and readings applied), because the generator
        now measures that. Either way this tool reuses the number the
        render recorded, so the request matches what was rendered.

        Keyed to the recorded length: its band,
        and for an even-pace style its length (decision 2026-09-18 - an
        edited reading changes the characters, not the meaning or the
        speaking time)."""
        if style == CUSTOM_STYLE:
            return {"duration_scale": round(float(custom_scale), 2)}
        return dynamic_profile.request_for(self.profile, style,
                                           self._pieces[index]["engine_len"])


def parse_time(text):
    """`7:06`, `1:07:06` or `7:06.5` -> seconds; None if it is not one."""
    parts = (text or "").strip().split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if any(v < 0 for v in values) or values[-1] >= 60 or (len(values) == 3 and values[1] >= 60):
        return None
    seconds = 0.0
    for v in values:
        seconds = seconds * 60 + v
    return seconds


def clock(seconds):
    seconds = max(0.0, float(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m)}:{s:04.1f}"


# ------------------------------------------------------------ scanning

def _stamp(path):
    info = os.stat(path)
    return [info.st_size, int(info.st_mtime)]


def scan_cache_path(chapter):
    return os.path.join(chapter.work_dir, chapter.base + ".qa.json")


def load_scan(chapter):
    """The cached scan, only if it was taken of THIS audio - a repair
    changes the FLAC and every time after it."""
    try:
        data = _read_json(scan_cache_path(chapter))
    except (OSError, ValueError):
        return None
    return data if data.get("flac_stamp") == _stamp(chapter.flac) else None


def scan(chapter, log, on_proc=None):
    """Whisper over the whole chapter (chapter-repair's pass), scored per
    part, cached against the FLAC's size and mtime."""
    transcript = os.path.join(chapter.work_dir, chapter.base + ".json")
    cached = load_scan(chapter)
    if cached is None and os.path.isfile(transcript):
        os.remove(transcript)           # a transcript of audio that no longer exists
    segments = repair.transcribe_chapter(chapter, log, on_proc)
    rows = repair.score_chapter(chapter, segments)
    for row in rows:
        row["part"] = row["index"] + 1
    data = {"chapter": chapter.name, "scanned": time.strftime("%Y-%m-%d %H:%M"),
            "flac_stamp": _stamp(chapter.flac), "rows": rows}
    os.makedirs(chapter.work_dir, exist_ok=True)
    _write_json(scan_cache_path(chapter), data)
    return data


def flagged(rows, threshold, min_chars):
    """Worst first, as chapter-repair ranks them - minus the parts too short
    to judge. A shortlist, never a verdict: Whisper mishears too."""
    out = []
    for row in rows:
        if len(repair.normalise_for_compare(row["text"])) < min_chars:
            continue
        lr = row["length_ratio"]
        if row["similarity"] < threshold or lr is None or not (0.65 <= lr <= 1.45):
            out.append(row)
    return sorted(out, key=lambda r: r["similarity"])


def listen_clip(chapter, index, pad=1.0):
    part = chapter.part(index)
    out = os.path.join(chapter.work_dir, "_listen", f"part_{part['part']:04d}.wav")
    return repair.extract_segment(chapter.flac, part["start"], part["end"], out, pad=pad)


# ------------------------------------------------------------ takes

def take_dir(chapter, index):
    return os.path.join(chapter.work_dir, "takes", f"part_{index + 1:04d}")


def existing_takes(chapter, index):
    """Takes already rendered for this part, oldest first - kept across
    sessions so a take you liked yesterday is still there."""
    out = []
    for meta in sorted(glob.glob(os.path.join(take_dir(chapter, index), "take_*.json"))):
        try:
            data = _read_json(meta)
        except (OSError, ValueError):
            continue
        if os.path.isfile(data.get("wav", "")):
            out.append(data)
    return out


def generate_takes(chapter, index, tts_text, request, style, count, readings_used, log,
                   on_proc=None):
    """`count` fresh takes of one part from ONE worker (one model load),
    each on its own random seed - the way the chapter was rendered.

    Returns the takes' records. The worker's job file is the documented
    irodori_batch.py contract with per-job overrides, as the generator's
    dynamic mode writes it."""
    folder = take_dir(chapter, index)
    os.makedirs(folder, exist_ok=True)
    first = len(glob.glob(os.path.join(folder, "take_*.json"))) + 1
    jobs = []
    for n in range(count):
        wav = os.path.join(folder, f"take_{first + n:02d}.wav")
        job = {"index": n + 1, "text": tts_text, "output_wav": wav}
        job.update(request)
        jobs.append(job)
    jobs_path = os.path.join(folder, f"jobs-{time.strftime('%Y%m%d-%H%M%S')}.json")
    checkpoint = chapter.render.get("checkpoint") or "Aratako/Irodori-TTS-v4.1-Small"
    _write_json(jobs_path, {
        "uv_project_dir": chapter.settings["irodori_root"],
        "checkpoint": checkpoint,
        "checkpoint_is_hf": not checkpoint.lower().endswith(".safetensors"),
        "speaker_path": chapter.profile["speaker_path"],
        "duration_scale": 1.0,
        "trim_tail": bool(chapter.profile.get("trim_tail", True)),
        "seed": None,
        # As the chapter was rendered: a take without the watermark beside
        # neighbours with it would be the odd one out.
        "watermark": bool(chapter.render.get("watermark", True)),
        "jobs": jobs,
    })
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.Popen(["uv", "run", "--no-sync", "python", "-u", batch_script(chapter.settings),
                             "--jobs", jobs_path], cwd=chapter.settings["irodori_root"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if on_proc:
        on_proc(proc)
    seeds = {}
    # Read to the end whatever happens: an unread pipe fills and the worker
    # blocks forever (CLAUDE.md, "a reader that stops reading").
    for raw in proc.stdout:
        line = raw.rstrip()
        try:
            if line.startswith("MODEL_LOADED "):
                log(f"  model loaded in {line.split()[1]}s")
            elif line.startswith("CHUNK_DONE "):
                bits = line.split()
                seeds[int(bits[1])] = int(bits[2]) if len(bits) > 2 and bits[2] != "None" else None
                log(f"  take {int(bits[1])}/{count} done")
            elif line.startswith("CHUNK_FAIL "):
                log("  ! " + line.split(" ", 2)[-1])
        except Exception:
            pass
    code = proc.wait()
    takes = []
    for job in jobs:
        if not os.path.isfile(job["output_wav"]):
            continue
        record = {"wav": job["output_wav"], "index": index, "part": index + 1,
                  "tts_text": tts_text, "request": request, "style": style,
                  "used_seed": seeds.get(job["index"]),
                  "seconds": round(repair.audio_duration(job["output_wav"]), 3),
                  "readings": readings_used, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _write_json(job["output_wav"][:-4] + ".json", record)
        takes.append(record)
    if code != 0 or not takes:
        raise RuntimeError(f"the TTS worker exited with code {code}"
                           + ("" if takes else " and produced no audio"))
    return takes


# ------------------------------------------------------------ apply

def plan(chapter, selections):
    """The batch, worked out before anything is written: [edit], ascending.
    `selections` are take records (one per part)."""
    edits, seen = [], set()
    for take in sorted(selections, key=lambda t: t["index"]):
        if take["index"] in seen:
            raise ValueError(f"part {take['index'] + 1} is queued twice")
        seen.add(take["index"])
        chunk = chapter.sync["chunks"][take["index"]]
        new = repair.audio_duration(take["wav"])
        edits.append({"index": take["index"], "start": chunk["start"], "end": chunk["end"],
                      "wav": take["wav"], "old_duration": round(chunk["end"] - chunk["start"], 3),
                      "new_duration": round(new, 3), "take": take})
    if not edits:
        raise ValueError("nothing queued")
    delta = sum(e["new_duration"] - e["old_duration"] for e in edits)
    return {"edits": edits, "delta": round(delta, 3),
            "old_total": round(chapter.sync["chunks"][-1]["end"], 3),
            "new_total": round(chapter.sync["chunks"][-1]["end"] + delta, 3),
            "later_parts": sum(1 for c in chapter.sync["chunks"] if c["start"] >= edits[0]["end"])}


def apply(chapter, selections, log):
    """Replaces the queued parts' audio IN PLACE and moves every time after
    them. No backup - the folder is the user's copy (decision 2026-09-18).

    Order: splice the FLAC, encode the .m4a from it, verify faststart and
    carry the tags, swap both in; then sync.json, the .srt and
    translation.json (by TIME, text untouched), then render.json (the new
    requests, seeds and texts, plus a repair history), then readings.json.
    Audio goes first so that if anything raises afterwards, the files that
    describe it can be rebuilt from what did get written."""
    problem = chapter.check_audio()
    if problem:
        raise RuntimeError(problem)
    p = plan(chapter, selections)
    edits = p["edits"]
    mapped = repair.build_time_map(edits)
    work = os.path.join(chapter.work_dir, "_apply")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    new_flac = os.path.join(work, chapter.base + ".flac")
    repair.splice_many(chapter.flac, edits, new_flac, work)
    new_m4a = os.path.join(work, chapter.base + ".m4a")
    repair.encode_m4a(new_flac, new_m4a, chapter.settings["aac_bitrate"])
    atoms = repair.atom_order(new_m4a)
    if "moov" not in atoms or "mdat" not in atoms or atoms.index("moov") > atoms.index("mdat"):
        raise RuntimeError(f"faststart was lost - atom order {atoms}; nothing was changed")
    tags = repair.copy_tags(chapter.m4a, new_m4a)
    os.replace(new_flac, chapter.flac)
    os.replace(new_m4a, chapter.m4a)
    log(f"  audio: {len(edits)} part(s) spliced into the FLAC, .m4a re-encoded from it "
        f"(faststart intact, {len(tags)} tag(s) carried over), {p['delta']:+.3f}s overall")

    # sync.json - the same running-shift walk as chapter-repair.
    by_index = {e["index"]: e for e in edits}
    shift = 0.0
    for entry in chapter.sync["chunks"]:
        start = entry["start"] + shift
        edit = by_index.get(entry["index"])
        entry["start"] = round(start, 3)
        if edit:
            entry["end"] = round(start + edit["new_duration"], 3)
            shift += edit["new_duration"] - edit["old_duration"]
        else:
            entry["end"] = round(entry["end"] + shift, 3)
    _write_json(chapter.sync_path, chapter.sync)
    log(f"  sync.json: {p['later_parts']} later part(s) shifted, text untouched")

    if os.path.isfile(chapter.srt_path):
        cues = repair.read_srt(chapter.srt_path)
        for cue in cues:
            cue["start"] = round(mapped(cue["start"]), 3)
            cue["end"] = round(mapped(cue["end"]), 3)
        repair.write_srt(chapter.srt_path, cues)
        log(f"  .srt: {len(cues)} cue(s) re-timed, text untouched")
    if os.path.isfile(chapter.translation_path):
        tr = _read_json(chapter.translation_path)
        for entry in tr.get("chunks", []):
            entry["start"] = round(mapped(entry["start"]), 3)
            entry["end"] = round(mapped(entry["end"]), 3)
        _write_json(chapter.translation_path, tr)
        log(f"  translation.json: {len(tr.get('chunks', []))} entr(ies) re-timed, "
            f"English untouched")

    # render.json - it must keep describing the audio that is really there.
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for e in edits:
        piece = chapter._pieces[e["index"]]
        take = e["take"]
        history = piece.setdefault("repairs", [])
        history.append({"at": now,
                        "before": {k: piece.get(k) for k in ("tts_text", "request", "used_seed",
                                                             "seconds", "readings")},
                        "style": take["style"]})
        piece.update({"tts_text": take["tts_text"], "request": take["request"],
                      "used_seed": take["used_seed"], "seconds": e["new_duration"],
                      "readings": dict(take.get("readings") or {})})
    chapter.render.setdefault("repairs", []).append(
        {"at": now, "parts": [e["index"] + 1 for e in edits], "delta": p["delta"]})
    _write_json(chapter.render_path, chapter.render)
    log(f"  render.json: {len(edits)} part(s) updated with their new request, seed and "
        f"text, history kept")
    # The take itself says it is in the chapter now, so an old take is never
    # mistaken for a fresh one next session.
    for e in edits:
        meta = e["take"]["wav"][:-4] + ".json"
        if os.path.isfile(meta):
            record = _read_json(meta)
            record["applied"] = now
            _write_json(meta, record)

    new_readings = {}
    for e in edits:
        new_readings.update(e["take"].get("readings") or {})
    changed = merge_readings(chapter.readings_path, chapter.book, new_readings,
                             chapter.base) if new_readings else []
    for word, reading, what in changed:
        log(f"  readings.json: {what} {word} → {reading}")

    shutil.rmtree(work, ignore_errors=True)
    chapter.reload()
    return p
