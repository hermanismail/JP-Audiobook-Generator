"""
audition.py
-----------
The engine behind the Seiyuu Audition window. No Tk in here, so every
piece of it can be exercised from a plain Python prompt.

What an audition is: ONE piece of text, read by one or more
(seiyuu + parameters) combinations called SAMPLES, so they can be played
back to back and judged by ear. Two modes:

    simple  the text goes to the TTS verbatim - no chunking, no cleaning,
            no stitching. One utterance, one wav. The fast loop for
            "how does this voice say this line".

    e2e     the text goes through the REAL generator pipeline:
            text_pipeline.build_chunks() with this sample's chunk length,
            one TTS call per chunk, silence wavs between them, ffmpeg
            concat. What the book would actually sound like.

## Two deliberate choices

**The chunking rules are imported, not copied.** `text_pipeline.py` in the
folder above is stdlib-only and reads no settings, so it imports cleanly
into this tool's small venv. Copying it would mean e2e mode slowly stops
simulating the generator every time a bracket or dash rule is revised -
which would make the mode worthless. Nothing else is shared: everything in
`run_audiobook.py` is built from module-level globals read out of the
generator's live `settings.json`, so importing it would drag a half-edited
book preset into this tool. Its silence/concat logic is reimplemented below
instead - it is short, and it is not the part that has to stay in step.

**The output is a WAV, and stops before the AAC encode.** An audition is
listened to once and deleted; there is nothing to stream and nothing to
tag, so the encode would only buy file size this tool immediately throws
away. It also keeps playback inside the GUI - `winsound` plays WAV and
nothing else. `aac_bitrate` and the SilentCipher watermark therefore have
no meaning here and are absent by design, not by omission.

The TTS itself is `irodori_batch.py` from the generator folder, run
unmodified through `uv` in the Irodori venv. It already loads the model
once and walks a queue of text->wav jobs, which is exactly one sample.
"""

import json
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# The audiobook generator, one folder up. Two things come from there: the
# text pipeline (imported) and the TTS worker (run as a subprocess).
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
if GENERATOR_DIR not in sys.path:
    sys.path.insert(0, GENERATOR_DIR)

import text_pipeline  # noqa: E402  - needs the sys.path line above

# A constant in run_audiobook.py too, for the same reason: the pipeline is
# pinned to the published checkpoint, so there is no model field anywhere
# in either GUI. Change it here and there together if that ever moves.
MODEL_REF = "Aratako/Irodori-TTS-v4.1-Small"

SPEAKER_SUFFIX = ".speaker.safetensors"

DEFAULT_SETTINGS = {
    "irodori_root": "C:\\Irodori-TTS",
    # Empty means "<generator folder>\irodori_batch.py" - resolved at use
    # time so a moved checkout still works without editing settings.
    "batch_script": "",
    "temp_root": "F:\\tmp\\audition-tmp",
    "clean_temp_on_exit": True,
    # The editor's last state, restored on open and used as SAMPLE 1.
    "mode": "simple",
    "text": "",
    "speaker_path": "",
    "max_chunk_length": 40,
    "duration_scale": 1.2,
    "no_trim_tail": False,
    "seed_enabled": False,
    "seed_value": 20260906,
    "silence_duration_sentence": 0.5,
    "silence_duration_paragraph": 0.5,
    "silence_duration_section": 0.8,
}

# Which parameters mean anything in which mode. Simple mode never chunks
# and never stitches, so the chunk length and the three silences have
# nothing to act on - the GUI greys them out using this list rather than
# leaving dead controls that appear to do something.
E2E_ONLY_KEYS = (
    "max_chunk_length",
    "silence_duration_sentence",
    "silence_duration_paragraph",
    "silence_duration_section",
)


def merge_setting_defaults(data):
    """A settings file written before a key existed must still yield the
    default for it, not an empty string. Same rule as the generator's
    translate_pipeline.merge_setting_defaults."""
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


def batch_script_path(settings):
    configured = (settings.get("batch_script") or "").strip()
    return configured or os.path.join(GENERATOR_DIR, "irodori_batch.py")


# --------------------------------------------------------------- speakers

def speaker_list_dir(irodori_root):
    """Where the onboarder publishes finished speakers. The same folder the
    generator's Speaker Path field points into."""
    return os.path.join(irodori_root, "seiyuu", "list")


def nickname_for(speaker_path):
    """`marinka-03-calm-shonen.speaker.safetensors` -> `marinka-03-calm-shonen`.
    A speaker picked with Browse can be any .safetensors, so fall back to
    the plain stem rather than showing the whole filename."""
    name = os.path.basename(speaker_path or "")
    if name.endswith(SPEAKER_SUFFIX):
        return name[: -len(SPEAKER_SUFFIX)]
    return os.path.splitext(name)[0] or "speaker"


def list_speakers(irodori_root):
    """[(nickname, absolute path)] for everything in seiyuu/list, sorted.
    Empty when the folder is missing - the tool still works, you just have
    to Browse to a .safetensors yourself."""
    folder = speaker_list_dir(irodori_root)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = [n for n in names if n.endswith(SPEAKER_SUFFIX)]
    return [(nickname_for(n), os.path.join(folder, n)) for n in sorted(found)]


# ------------------------------------------------------------------ text

def chunk_preview(text, max_chunk_length):
    """The chunks e2e mode would generate, without generating anything.

    This is the character counting the tool exists to abolish: the same
    build_chunks() the generator runs, answered instantly while you type,
    so the effect of a chunk length is visible before any GPU time is
    spent on it. Returns [] for empty text."""
    if not (text or "").strip():
        return []
    soft = int(max_chunk_length)
    chunks, _working = text_pipeline.build_chunks(
        text, soft_limit=soft, hard_limit=soft + 30)
    return chunks


# ------------------------------------------------------------------ paths

def sample_paths(temp_root, nickname, number, when=None):
    """Where SAMPLE <number> lives:

        <temp_root>\\YYYYMMDD\\<nickname>\\sample_001.wav        <- played
        <temp_root>\\YYYYMMDD\\<nickname>\\sample_001_work\\     <- the rest

    The number is the sample's position in the audition, so two samples on
    the same seiyuu share a folder without colliding. Working files sit
    beside the result rather than under a hidden temp name, because the
    reason this tool has a visible temp folder at all is to be able to look
    when something sounds wrong."""
    stamp = time.strftime("%Y%m%d", when or time.localtime())
    folder = os.path.join(temp_root, stamp, nickname)
    base = f"sample_{number:03d}"
    return {
        "dir": folder,
        "work_dir": os.path.join(folder, base + "_work"),
        "wav": os.path.join(folder, base + ".wav"),
        # What the wav beside it was generated from - see sample_fingerprint.
        # Kept next to the wav rather than inside the work dir, because it
        # has to outlive a regeneration of the work dir.
        "fingerprint": os.path.join(folder, base + ".json"),
        "date_dir": os.path.join(temp_root, stamp),
    }


# ------------------------------------------------------------ fingerprints

# Bump when a change here alters the AUDIO a given spec produces (the
# stitching order, the silence rendering, what reaches the TTS). Old
# fingerprints then stop matching and everything regenerates once, which
# is the safe direction.
FINGERPRINT_VERSION = 1


def speaker_stamp(path):
    """Size and mtime of the speaker file. Part of the fingerprint because
    the onboarder republishes `seiyuu/list/<name>.speaker.safetensors` in
    place when a speaker is retrained: same path, different voice. Without
    this, a retrained speaker would keep playing back the old one's
    samples."""
    try:
        info = os.stat(path)
        return [info.st_size, int(info.st_mtime)]
    except OSError:
        return None


def sample_fingerprint(spec):
    """Everything that decides what a sample SOUNDS like, and nothing else.

    Two rules keep it honest:

    - Parameters that do nothing in this mode are left out, exactly as the
      GUI greys them out. Nudging a silence duration in simple mode must
      not throw away a sample it could not have affected.
    - `seed_value` counts only when the seed is on. With the seed off the
      value on screen is inert.

    A random-seed sample is not reproducible, which is the argument FOR
    reuse rather than against it: regenerating it would hand back a
    different take of a sample you have already listened to and formed an
    opinion about."""
    fingerprint = {
        "version": FINGERPRINT_VERSION,
        "mode": spec["mode"],
        "text": spec["text"],
        "speaker_path": spec["speaker_path"],
        "speaker_stamp": speaker_stamp(spec["speaker_path"]),
        "duration_scale": float(spec["duration_scale"]),
        "no_trim_tail": bool(spec["no_trim_tail"]),
        "seed": int(spec["seed_value"]) if spec.get("seed_enabled") else None,
    }
    if spec["mode"] != "simple":
        fingerprint["max_chunk_length"] = int(spec["max_chunk_length"])
        for key in ("silence_duration_sentence", "silence_duration_paragraph",
                    "silence_duration_section"):
            fingerprint[key] = float(spec[key])
    return fingerprint


def read_fingerprint(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_fingerprint(path, fingerprint):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fingerprint, f, ensure_ascii=False, indent=2)


def existing_sample(spec, settings, number):
    """The wav already on disk for this exact spec, or None.

    Both the generator and the GUI's sample cards ask this - the cards so
    you can see, before spending any GPU time, which samples a Generate
    would actually have to render."""
    nickname = spec.get("nickname") or nickname_for(spec["speaker_path"])
    paths = sample_paths(settings["temp_root"], nickname, number)
    if not os.path.exists(paths["wav"]):
        return None
    if read_fingerprint(paths["fingerprint"]) != sample_fingerprint(spec):
        return None
    return paths["wav"]


# ----------------------------------------------------------------- ffmpeg

def probe_audio_format(wav_path):
    """The TTS wav's real sample rate / channels / sample format.

    The concat demuxer resamples nothing, so the silence wavs have to match
    the generated audio exactly - this is the same guard run_audiobook.py
    grew after silence.wav was hardcoded to 24kHz against 48kHz TTS output
    and the joins came out pitch-shifted."""
    fmt = {"sample_rate": 48000, "channels": 1,
           "sample_fmt": "s16", "codec_name": "pcm_s16le"}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels,sample_fmt,codec_name",
             "-of", "default=noprint_wrappers=1", wav_path],
            capture_output=True, text=True)
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not value or value == "N/A":
                continue
            if key in ("sample_rate", "channels"):
                fmt[key] = int(value)
            elif key in ("sample_fmt", "codec_name"):
                fmt[key] = value
    except (ValueError, OSError):
        pass
    return fmt


def channel_layout_for(channels):
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")


def render_silence_wavs(work_dir, audio_format, durations):
    """One silence wav per kind, in this sample's own work dir.

    Per sample, not per run: the three durations are sample parameters
    here, so two samples in one audition can legitimately want different
    silence. Three sub-second ffmpeg calls is not worth caching around.

    Returns {kind: path}."""
    os.makedirs(work_dir, exist_ok=True)
    rate = audio_format["sample_rate"]
    channels = audio_format["channels"]
    layout = channel_layout_for(channels)
    out = {}
    for kind, seconds in durations.items():
        path = os.path.abspath(os.path.join(work_dir, f"silence_{kind}.wav"))
        base = ["ffmpeg", "-y", "-f", "lavfi",
                "-i", f"anullsrc=r={rate}:cl={layout}",
                "-t", str(float(seconds)),
                "-ar", str(rate), "-ac", str(channels)]
        result = subprocess.run(
            base + ["-acodec", audio_format["codec_name"],
                    "-sample_fmt", audio_format["sample_fmt"], path],
            capture_output=True)
        if not os.path.exists(path):
            # Some pcm codec/sample_fmt pairs ffprobe reports are not
            # accepted verbatim on the encode side. Retry letting ffmpeg
            # choose, still pinned to the rate and channel count - the two
            # that actually break the concat.
            subprocess.run(base + [path], capture_output=True)
        if not os.path.exists(path):
            raise RuntimeError(
                "Could not render " + path + ". ffmpeg said: " +
                result.stderr.decode("utf-8", "replace").strip()[-400:])
        out[kind] = path
    return out


def stitch_wavs(concat_list_path, output_wav, audio_format):
    """Concatenates the per-chunk wavs and their silences into one wav.

    The generator encodes to AAC at this point; an audition does not - see
    the module docstring. Decoding stays lossless and the result is
    playable by winsound, which is the whole reason the encode is skipped.

    Returns (ok, ffmpeg stderr tail)."""
    codec = audio_format["codec_name"]
    if not codec.startswith("pcm_"):
        codec = "pcm_s16le"
    result = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c:a", codec,
        "-ar", str(audio_format["sample_rate"]),
        "-ac", str(audio_format["channels"]),
        output_wav,
    ], capture_output=True)
    if result.returncode != 0 or not os.path.exists(output_wav):
        tail = result.stderr.decode("utf-8", "replace").strip()[-600:]
        return False, tail or "(no error output)"
    return True, ""


# -------------------------------------------------------------------- TTS

def write_job_file(path, spec, jobs, irodori_root):
    """The job file irodori_batch.py reads. Its schema is documented in
    that script's docstring; this tool is a second caller of it, not a
    fork of it."""
    payload = {
        "uv_project_dir": irodori_root,
        "checkpoint": MODEL_REF,
        # MODEL_REF is a Hugging Face id, not a local file. A path ending in
        # .safetensors would have to go through --checkpoint instead; the
        # two are a mutually exclusive required pair in infer.py.
        "checkpoint_is_hf": not MODEL_REF.lower().endswith(".safetensors"),
        "speaker_path": spec["speaker_path"],
        "duration_scale": float(spec["duration_scale"]),
        "trim_tail": not bool(spec["no_trim_tail"]),
        "seed": int(spec["seed_value"]) if spec.get("seed_enabled") else None,
        # Auditions are judged by ear on clean audio. The watermark is the
        # generator's decision to make on files that get published; it has
        # no bearing on whether a voice suits a book.
        "watermark": False,
        "jobs": jobs,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def run_worker(jobs_path, settings, on_line, on_proc=None):
    """Runs irodori_batch.py once and hands every protocol line to on_line.

    `on_proc` receives the Popen object so a Cancel button has something to
    terminate. Line by line and unbuffered on purpose: a sample is tens of
    seconds to minutes, and buffered output makes a live log a frozen one.

    Returns the exit code, or -1 if the worker could not be started."""
    cmd = ["uv", "run", "--no-sync", "python", "-u",
           batch_script_path(settings), "--jobs", jobs_path]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # A child Python writing Japanese to a PIPE otherwise falls back to the
    # Windows locale encoding and dies on the first character - see the
    # same three lines in seiyuu-onboarder/pipeline.run_streaming().
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.Popen(
            cmd, cwd=settings["irodori_root"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace", env=env)
    except OSError as e:
        on_line(f"! could not start the TTS worker: {e}")
        return -1
    if on_proc:
        on_proc(proc)
    for raw in proc.stdout:
        on_line(raw.rstrip())
    return proc.wait()


# ------------------------------------------------------------- generation

def generate_sample(spec, settings, number, log, cancelled, on_proc=None,
                    force=False):
    """Generates one sample end to end and returns a result dict:

        {"ok", "wav", "chunks", "error", "seconds"}

    `chunks` is [{"index", "text", "display_text"}] - what the Results
    window shows beside the play button. In simple mode that is the one
    line you typed; in e2e mode it is the real chunking, which is the
    thing being judged as much as the voice is.

    `cancelled` is a callable returning True once Cancel has been pressed.
    """
    started = time.time()
    nickname = spec.get("nickname") or nickname_for(spec["speaker_path"])
    paths = sample_paths(settings["temp_root"], nickname, number)
    os.makedirs(paths["work_dir"], exist_ok=True)

    result = {"ok": False, "wav": None, "chunks": [], "error": "",
              "seconds": 0.0, "reused": False,
              "dir": paths["dir"], "work_dir": paths["work_dir"]}

    # 1. What to speak.
    if spec["mode"] == "simple":
        # Verbatim. Not running the cleaning rules IS the mode: it is how
        # you hear the raw line, and how you hear what the rules change.
        text = spec["text"].strip()
        if not text:
            result["error"] = "No text to speak."
            return result
        units = [{"text": text, "display_text": text, "silence_kind": None}]
    else:
        chunks = chunk_preview(spec["text"], spec["max_chunk_length"])
        if not chunks:
            result["error"] = "The text produced no chunks - is it empty?"
            return result
        units = [{"text": c["text"], "display_text": c["display_text"],
                  "silence_kind": c["silence_kind"]} for c in chunks]

    result["chunks"] = [{"index": i, "text": u["text"],
                         "display_text": u["display_text"]}
                        for i, u in enumerate(units, start=1)]

    # 2. Is this exact sample already on disk? Worked out AFTER the units,
    #    because the Results window still shows the chunk breakdown of a
    #    reused sample - only the ~30 s of GPU time is skipped, nothing
    #    about what is displayed.
    fingerprint = sample_fingerprint(spec)
    if not force:
        ready = existing_sample(spec, settings, number)
        if ready:
            log("  unchanged since it was last generated - reusing it")
            result.update(ok=True, wav=ready, reused=True)
            return result

    # 3. Generate every unit in one worker - one model load per sample.
    jobs = []
    for i, unit in enumerate(units, start=1):
        jobs.append({"index": i, "text": unit["text"],
                     "output_wav": os.path.join(paths["work_dir"],
                                                f"chunk_{i:03d}.wav")})
    jobs_path = os.path.join(paths["work_dir"], "batch_jobs.json")
    write_job_file(jobs_path, spec, jobs, settings["irodori_root"])

    total = len(jobs)

    def relay(line):
        if line.startswith("MODEL_LOADED "):
            log(f"model loaded in {line.split()[1]}s - reused for all "
                f"{total} chunk(s) of this sample")
        elif line.startswith("CHUNK_START "):
            i = int(line.split()[1])
            log(f"  generating {i}/{total} "
                f"({len(units[i - 1]['text'])} chars)")
        elif line.startswith("CHUNK_FAIL "):
            parts = line.split(" ", 2)
            log(f"  ! chunk {parts[1]} failed: "
                f"{parts[2] if len(parts) > 2 else '(no message)'}")
        elif line.startswith(("CHUNK_DONE ", "BATCH_DONE ", "WATERMARK ")):
            pass
        elif line.startswith("BATCH_LOG "):
            log("  worker log: " + line.split(" ", 1)[1])
        elif line:
            log("  " + line)

    code = run_worker(jobs_path, settings, relay, on_proc)
    if cancelled():
        result["error"] = "Cancelled."
        return result
    if code != 0:
        result["error"] = f"The TTS worker exited with code {code}."
        return result

    produced = [j["output_wav"] for j in jobs if os.path.exists(j["output_wav"])]
    if not produced:
        result["error"] = "The worker produced no audio at all."
        return result
    if len(produced) != total:
        log(f"  ! only {len(produced)} of {total} chunk(s) produced audio - "
            f"the sample is incomplete")

    # 4. Simple mode is already finished: one wav, nothing to stitch.
    if spec["mode"] == "simple":
        shutil.copyfile(produced[0], paths["wav"])
        write_fingerprint(paths["fingerprint"], fingerprint)
        result.update(ok=True, wav=paths["wav"], seconds=time.time() - started)
        return result

    # 5. e2e: silence between chunks, then concat. Exactly the ordering
    #    run_audiobook.py writes - one silence file, then one chunk wav,
    #    per chunk - so the pacing is the book's pacing.
    audio_format = probe_audio_format(produced[0])
    log(f"  stitching {len(produced)} chunk(s) at "
        f"{audio_format['sample_rate']}Hz {audio_format['channels']}ch")
    silence = render_silence_wavs(paths["work_dir"], audio_format, {
        "sentence": spec["silence_duration_sentence"],
        "paragraph": spec["silence_duration_paragraph"],
        "section": spec["silence_duration_section"],
    })

    concat_list = os.path.join(paths["work_dir"], "concat_list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for i, job in enumerate(jobs):
            wav = job["output_wav"]
            if not os.path.exists(wav):
                continue
            kind = units[i]["silence_kind"] or "sentence"
            f.write(f"file '{silence[kind]}'\n")
            f.write(f"file '{os.path.abspath(wav)}'\n")

    ok, error = stitch_wavs(concat_list, paths["wav"], audio_format)
    if not ok:
        result["error"] = "ffmpeg could not stitch the sample: " + error
        return result

    # Written only now, after the stitch succeeded: a fingerprint beside a
    # wav that was never finished would reuse a broken sample forever.
    write_fingerprint(paths["fingerprint"], fingerprint)
    result.update(ok=True, wav=paths["wav"], seconds=time.time() - started)
    return result


# -------------------------------------------------------------- cleanup

def remove_paths(paths, stop_at=None):
    """Deletes exactly the files and folders handed in, then any parent
    left empty, and returns what it removed.

    Deliberately NOT "delete temp_root": that folder is a setting, it can
    be pointed anywhere, and a tool that recursively deletes a
    user-supplied path on exit is one typo away from being a disaster. The
    GUI passes only what this session created, and `stop_at` (the temp
    root) is where the empty-parent walk stops - the folder the person
    configured is theirs, even when this tool leaves it empty."""
    removed = []
    stop = os.path.normcase(os.path.abspath(stop_at)) if stop_at else None
    parents = set()
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed.append(path)
            parents.add(os.path.dirname(path))
        except OSError:
            pass
    # Walk upward while folders are empty, so a finished session leaves no
    # bare YYYYMMDD\nickname shells behind.
    for parent in sorted(parents, key=len, reverse=True):
        while parent and os.path.isdir(parent):
            if stop and os.path.normcase(os.path.abspath(parent)) == stop:
                break
            try:
                if os.listdir(parent):
                    break
                os.rmdir(parent)
                removed.append(parent)
                parent = os.path.dirname(parent)
            except OSError:
                break
    return removed
