import os
import sys
import json
import datetime
import subprocess
import glob
import shutil

import text_pipeline
import dynamic_profile
import furigana
import preview
import suite_link

# This script prints Japanese - chapter text, readings, the seiyuu credit -
# and the GUI runs it with its stdout on a PIPE, where Python falls back to
# the Windows locale encoding (cp1252) and the first Japanese character
# raises UnicodeEncodeError (CLAUDE.md: "A child Python writing Japanese to
# a PIPE dies on cp1252"). Fixed here rather than in the caller's
# environment, so running this script by hand behaves the same way.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):     # already wrapped, or not a stream
        pass

# --- Configuration ---
# Settings are now stored in settings.json (same folder as this script)
# instead of being hardcoded here. If settings.json is missing, it will be
# created automatically using the defaults below.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "input_folder": r"E:\AUDIOBOOK\chapter",
    "output_folder": r"E:\AUDIOBOOK\output",
    "temp_dir": r"D:\AUDIOBOOK_TMP",
    "speaker_path": r"C:\Irodori-TTS\seiyuu\ueshama.speaker.safetensors",
    "silence_duration_sentence": 1.0,
    "silence_duration_paragraph": 1.2,
    "silence_duration_section": 1.5,
    "clean_temp_after_run": True,
    "uv_project_dir": r"C:\Irodori-TTS",  # not used by this script directly, kept for the GUI launcher
    "auto_tag_generated_files": False,
    "max_chunk_length": 100,
    "regenerate_existing_chapters": False,
    # Per-speaker TTS tuning, set on the GUI's Advanced page. These vary
    # enough between trained speakers that fixing them here produced
    # inconsistent results across voices. Keep in step with
    # gui_settings.DEFAULT_SETTINGS.
    "duration_scale": 1.2,
    "no_trim_tail": True,
    "seed_enabled": False,
    "seed_value": 20260906,
    # SilentCipher embeds an inaudible "this is AI-generated" marker.
    # Leaving it ON is the engine default; OFF skips the embed and the
    # 48k->44.1k->48k resample it does on the way (see irodori_batch.py).
    "watermark_audio": True,
    # Output encoding. The chapter is always mono AAC in an .m4a; only the
    # bitrate is a setting. See AAC_BITRATE below.
    "aac_bitrate": "64k",
    "keep_flac_master": True,
    # The Audiobook Creation Suite library (its own repo and SQLite file).
    # Absent or unreachable = every book behaves the legacy way; see
    # suite_link.py.
    "suite_root": r"F:\AUDIOBOOK-CREATION-SUITE",
    # Translation subtitles. See translate_pipeline.py.
    "auto_translate_after_run": False,
    # Listen to the finished book with Whisper and say which Parts are
    # worth a human ear, once at the end of the run (user decision
    # 2026-09-25). Dynamic mode only - it is keyed to sync.json Parts.
    "scan_after_run": True,
    "whisper_exe": "C:\\Transcribe\\.venv\\Scripts\\whisper.exe",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ja",
    "translation_backend": "vntl",
    "llama_server_url": "http://127.0.0.1:8080",
    # Used to start llama-server on demand when nothing is already listening,
    # so the GPU is only occupied while translation is actually running.
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llama_model_path": r"C:\llama.cpp\models\vntl-llama3-8b-v2-hf-q5_k_m.gguf",
    # Generation mode (2026-09-17). "normal" renders from the settings above.
    # "dynamic" renders sentence by sentence from book-profiler profiles -
    # see dynamic_profile.py and process_chapter_dynamic(). Keep in step
    # with gui_settings.DEFAULT_SETTINGS.
    "generation_mode": "normal",
    "dynamic": {
        # The profile and style every chapter gets ("assign": "all"), or
        # the default for chapters without their own entry ("custom").
        "profile_path": "",
        "style": "scale_default",
        "assign": "all",
        # "custom" only: {"chapter_001": {"enabled", "profile_path", "style"}}.
        # A chapter absent from this dict, or with enabled false, is skipped.
        "chapters": {},
    },
}


def merge_dynamic_defaults(loaded):
    """The "dynamic" block is a nested dict, so a plain update() would let an
    older or hand-trimmed file drop its default keys wholesale."""
    block = dict(DEFAULT_SETTINGS["dynamic"])
    block["chapters"] = {}
    block.update(loaded.get("dynamic") or {})
    return block


def load_settings():
    """Loads settings.json, creating it with defaults if it doesn't exist.
    Any keys missing from an existing file are filled in with defaults,
    so older settings.json files stay compatible with new options."""
    if not os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
        return dict(DEFAULT_SETTINGS)

    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    # Migrate BEFORE merging in the defaults: migrate_silence_settings()
    # decides what to do based on which keys the file actually contained,
    # and DEFAULT_SETTINGS already supplies all three new ones - merging
    # first would hide the legacy case entirely.
    loaded = migrate_silence_settings(loaded)

    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded)
    merged["dynamic"] = merge_dynamic_defaults(loaded)
    return merged


def migrate_silence_settings(settings):
    """Backwards compatibility for settings.json files written before the
    per-kind silence durations existed.

    Older versions stored a single "silence_duration" (the 1x base unit)
    and produced longer gaps by repeating silence.wav 2x/3x in the concat
    list. The new scheme renders three separate files with independent
    durations. When only the legacy key is present we carry it straight
    into the sentence duration and derive paragraph/section as 2x and 3x
    that value, which reproduces the old output exactly - so upgrading
    changes nothing audible until the person actually tunes the numbers on
    the Advanced page."""
    legacy = settings.pop("silence_duration", None)
    if legacy is None:
        return settings

    try:
        legacy = float(legacy)
    except (TypeError, ValueError):
        return settings

    # round(): 0.8 * 3 is 2.4000000000000004 in binary floating point, and
    # that full value would end up rendered verbatim in the GUI's spinner.
    if "silence_duration_sentence" not in settings:
        settings["silence_duration_sentence"] = round(legacy, 3)
    if "silence_duration_paragraph" not in settings:
        settings["silence_duration_paragraph"] = round(legacy * 2, 3)
    if "silence_duration_section" not in settings:
        settings["silence_duration_section"] = round(legacy * 3, 3)
    return settings


SETTINGS = load_settings()

INPUT_FOLDER = SETTINGS["input_folder"]
OUTPUT_FOLDER = SETTINGS["output_folder"]
TEMP_DIR = SETTINGS["temp_dir"]
# The model is no longer a setting - it was dropped from the GUI once the
# pipeline moved to the published Hugging Face checkpoint, which needs no
# per-book or per-speaker choice. Change this one line to point somewhere
# else; checkpoint_args() below accepts either form, so a local
# ".safetensors" path still works, as does a "repo/subfolder" id such as
# Aratako/Irodori-TTS-v4.1-Small-Quantized/int8-weight-only.
MODEL_REF = "Aratako/Irodori-TTS-v4.1-Small"
SPEAKER_PATH = SETTINGS["speaker_path"]

# The per-chapter TTS worker (see irodori_batch.py). It is launched with
# cwd set to the Irodori-TTS project so `uv run --no-sync` resolves that
# venv, the same one this script is itself running in.
BATCH_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "irodori_batch.py")
UV_PROJECT_DIR = SETTINGS["uv_project_dir"]
# Independent gap durations, in seconds - one per silence kind produced by
# text_pipeline.silence_kind_for(). Each is rendered to its own wav once
# per run (see get_silence_wavs) and referenced by name in concat_list.txt.
SILENCE_DURATIONS = {
    "sentence": float(SETTINGS["silence_duration_sentence"]),
    "paragraph": float(SETTINGS["silence_duration_paragraph"]),
    "section": float(SETTINGS["silence_duration_section"]),
}
# Soft limit on TTS chunk length (characters), user-configurable on the
# GUI's Advanced page. The hard limit (text_pipeline.merge_units() only
# crosses it to avoid cutting a sentence off mid-way - see its docstring)
# is always exactly 30 characters above it, not separately configurable.
MAX_CHUNK_LENGTH = int(SETTINGS["max_chunk_length"])
MAX_CHUNK_LENGTH_HARD = MAX_CHUNK_LENGTH + 30
CLEAN_TEMP_AFTER_RUN = bool(SETTINGS["clean_temp_after_run"])
REGENERATE_EXISTING_CHAPTERS = bool(SETTINGS["regenerate_existing_chapters"])


def checkpoint_args(model_ref):
    """Builds the checkpoint argument pair for infer.py.

    infer.py takes the model EITHER as a local file (--checkpoint) OR as a
    Hugging Face repo id (--hf-checkpoint), and those two live in a
    mutually exclusive, required argparse group. Passing both makes it exit
    with code 2 before generating anything, so exactly one has to be
    chosen here.

    Which one is decided from the value itself, so the GUI's single "Model
    Path" field can hold either form:
        C:\\Irodori-TTS\\model.safetensors  ->  --checkpoint
        Aratako/Irodori-TTS-v4.1-Small     ->  --hf-checkpoint

    A repo id is "org/name", so the giveaways for a local path are a drive
    letter or leading slash, a backslash, or a weights file extension.
    Anything else is treated as a repo id."""
    ref = (model_ref or "").strip()
    looks_local = (
        os.path.isabs(ref)
        or "\\" in ref
        or ref.lower().endswith((".safetensors", ".pt"))
    )
    return ["--checkpoint" if looks_local else "--hf-checkpoint", ref]


CHECKPOINT_ARGS = checkpoint_args(MODEL_REF)

# infer.py tuning, all set per preset on the GUI's Advanced page.
DURATION_SCALE = float(SETTINGS["duration_scale"])
NO_TRIM_TAIL = bool(SETTINGS["no_trim_tail"])
SEED_ENABLED = bool(SETTINGS["seed_enabled"])
SEED_VALUE = int(SETTINGS["seed_value"])
WATERMARK_AUDIO = bool(SETTINGS["watermark_audio"])

# ffmpeg output encoding: ffmpeg's native AAC encoder into an MP4 container
# (.m4a), always mono. Irodori-TTS renders mono, and every stereo MP3 this
# script used to write measured as dual mono (L-R at -91 dB, i.e. digital
# silence), so a second channel is an exact duplicate that only halves the
# bits available per channel. 64k is what the player library was re-encoded
# to after an A/B by ear - see the player repo's CLAUDE.md, "Phase 0".
#
# The old mp3_bitrate / mp3_mono keys are deliberately NOT read: a value
# chosen for stereo MP3 (320k, in older presets) is meaningless for AAC and
# would silently produce files five times larger than they need to be.
AAC_BITRATE = str(SETTINGS["aac_bitrate"])

# Keep a lossless FLAC of the stitched chapter beside the .m4a.
#
# Why it earns its disk: chapter-repair splices a replacement chunk into a
# chapter and re-encodes. Done against the .m4a that stacks a fresh
# generation of AAC loss every repair; done against a master it is always
# exactly one generation from the master, however many repairs happen. The
# 71 masters built retroactively in F:\AUDIOBOOK-HOST-MASTER are lossless
# CONTAINERS around already-decoded 64k audio - the best that could be done
# after the fact. One written here is a genuine lossless original, because
# it comes from the same wavs the encoder sees.
#
# Default ON: a master you did not keep cannot be recovered without
# re-rendering the chapter, while one you did not want is a single delete.
# It lands in the output folder and is NOT something the player wants - move
# or delete it before publishing.
KEEP_FLAC_MASTER = bool(SETTINGS["keep_flac_master"])

# Extensions a finished chapter can have, preferred first. .m4a is what this
# script writes now; .mp3 is what it wrote before. A chapter that already has
# an .mp3 is still finished - re-rendering it would spend hours of GPU time
# just to change the container, which the player repo's
# `npm run publish -- --reencode` does in seconds. The player accepts either
# and prefers .m4a when both are present.
CHAPTER_AUDIO_EXTS = (".m4a", ".mp3")


def existing_chapter_audio(base_name):
    """Paths of this chapter's finished audio in OUTPUT_FOLDER, .m4a first.
    Empty when the chapter has not been generated yet."""
    candidates = (os.path.join(OUTPUT_FOLDER, base_name + ext)
                  for ext in CHAPTER_AUDIO_EXTS)
    return [path for path in candidates if os.path.exists(path)]

# Shared, run-scoped folder holding the three rendered silence wavs. They
# are generated once (on the first chapter, once a real TTS wav exists to
# probe) and reused by every chapter afterwards.
SILENCE_DIR = os.path.join(TEMP_DIR, "_silence")

# Cache of {kind: wav path} for the current run, populated by
# get_silence_wavs() on first use and reused from then on.
_SILENCE_WAVS = {}

# Which silence goes into each chunk gap is decided entirely by
# text_pipeline.py (chunk["silence_kind"]), which combines structural
# boundaries (chapter_start/section/paragraph/sentence) with any
# forced-break content tags (bracket edges, "──") via the MAX-based rule
# in silence_units_for(), then buckets the result into one of
# "sentence"/"paragraph"/"section" via silence_kind_for_units(). See
# text-cleaning-logic-spec.md section 5. This module just looks up the
# matching pre-rendered wav - no lookup table or repetition needed.

# Ensure folders exist
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


def clean_temp_dir(include_silence=False):
    """Clears the temporary directory.

    The shared _silence folder is skipped by default: per-chapter cleanup
    runs after every chapter, and the three silence wavs in there are
    rendered once and reused by all remaining chapters. main() calls this
    once more with include_silence=True at the end of the run, when they
    are genuinely no longer needed."""
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        if not include_silence and os.path.abspath(file_path) == os.path.abspath(SILENCE_DIR):
            continue
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}. Reason: {e}")


def write_working_files(working_data, chunks, work_dir):
    """Writes out the sec/par/sen/input working files described in
    text-cleaning-logic-spec.md, for inspection/debugging. Returns the
    absolute path of each chunk's input .txt file, keyed by
    (section, paragraph, chunk)."""
    os.makedirs(work_dir, exist_ok=True)

    # sec001.txt
    for sec_idx, section_text in enumerate(working_data["sections"], start=1):
        with open(os.path.join(work_dir, f"sec{sec_idx:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(section_text)

    # sec001par001.txt
    for sec_idx, paragraphs in working_data["paragraphs"].items():
        for par_idx, para_text in enumerate(paragraphs, start=1):
            fname = f"sec{sec_idx:03d}par{par_idx:03d}.txt"
            with open(os.path.join(work_dir, fname), "w", encoding="utf-8") as f:
                f.write(para_text)

    # sec001par001sen001.txt
    for (sec_idx, par_idx), units in working_data["sentences"].items():
        for sen_idx, unit_text in enumerate(units, start=1):
            fname = f"sec{sec_idx:03d}par{par_idx:03d}sen{sen_idx:03d}.txt"
            with open(os.path.join(work_dir, fname), "w", encoding="utf-8") as f:
                f.write(unit_text)

    # sec001par001input001.txt  <- what actually gets sent to TTS
    input_paths = {}
    for chunk in chunks:
        fname = text_pipeline.chunk_filename(chunk)
        path = os.path.join(work_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(chunk["text"])
        key = (chunk["section"], chunk["paragraph"], chunk["chunk"])
        input_paths[key] = path

    return input_paths


def probe_audio_format(wav_path):
    """Reads the actual audio format of a generated TTS wav via ffprobe, so
    the silence wavs can be generated to match it exactly.

    A mismatch here (the ffmpeg concat demuxer expects every segment to
    share the same sample rate, channel count and sample format) is what
    caused the 2026-08 "weird sound" bug - silence.wav was hardcoded to
    24kHz while Irodori-TTS actually outputs 48kHz, so the concat demuxer
    misread the timing across the join and produced pitch/speed-distorted
    audio.

    Returns a dict with "sample_rate", "channels", "sample_fmt" and
    "codec_name". Any field ffprobe can't supply falls back to the
    conservative defaults below."""
    fmt = {
        "sample_rate": 48000,
        "channels": 1,
        "sample_fmt": "s16",
        "codec_name": "pcm_s16le",
    }
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels,sample_fmt,codec_name",
             "-of", "default=noprint_wrappers=1", wav_path],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
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
    """ffmpeg's anullsrc wants a layout name, not a raw channel count."""
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")


def get_audio_duration(path):
    """Returns the duration of an audio file in seconds via ffprobe, or 0.0
    if it can't be determined (missing file, ffprobe failure, etc).

    Used only to build each chapter's sync.json (chunk start/end offsets
    for the Android player - see android-player-phase0-spec.md). Reads the
    duration of the actual rendered file rather than trusting the
    requested/configured value, so timestamps match what's really in the
    stitched chapter (matters most for the silence wavs, whose real duration
    can differ very slightly from the `-t` value passed to ffmpeg)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except (ValueError, OSError):
        return 0.0


def get_silence_wavs(audio_format):
    """Renders the three named silence wavs - silence_sentence.wav,
    silence_paragraph.wav and silence_section.wav - into SILENCE_DIR, one
    per entry in SILENCE_DURATIONS.

    They are written ONCE per run and reused by every chapter: the result
    is cached in _SILENCE_WAVS, so the second and later chapters reuse the
    same files rather than re-rendering identical audio. Each file is
    encoded to match the TTS output's sample rate, channel count and
    sample format exactly (see probe_audio_format) - the concat demuxer
    does no resampling, so any drift here corrupts the stitched audio.

    Returns {kind: absolute wav path}."""
    if _SILENCE_WAVS:
        return _SILENCE_WAVS

    os.makedirs(SILENCE_DIR, exist_ok=True)

    sample_rate = audio_format["sample_rate"]
    channels = audio_format["channels"]
    layout = channel_layout_for(channels)
    codec = audio_format["codec_name"]
    sample_fmt = audio_format["sample_fmt"]

    for kind, duration in SILENCE_DURATIONS.items():
        path = os.path.abspath(os.path.join(SILENCE_DIR, f"silence_{kind}.wav"))
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r={sample_rate}:cl={layout}",
            "-t", str(duration),
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-acodec", codec,
            "-sample_fmt", sample_fmt,
            path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if not os.path.exists(path):
            # Some pcm codec/sample_fmt pairs ffprobe reports back aren't
            # accepted verbatim on the encode side; retry letting ffmpeg
            # pick the codec itself, still pinned to the probed rate and
            # channel count (which are the two that actually break concat).
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r={sample_rate}:cl={layout}",
                "-t", str(duration),
                "-ar", str(sample_rate),
                "-ac", str(channels),
                path,
            ], capture_output=True)
        if not os.path.exists(path):
            raise RuntimeError(
                f"Failed to render {path}. ffmpeg said: "
                f"{result.stderr.decode('utf-8', 'replace').strip()}")

        _SILENCE_WAVS[kind] = path
        print(f"  silence_{kind}.wav  ({duration}s)")

    return _SILENCE_WAVS


def build_sync_data(audio_files, sync_chunk_texts, silence_kind_before_wav, silence_wavs):
    """Builds the chunk timing list for a chapter's sync.json (consumed by
    the Android player app - see android-player-phase0-spec.md section B).

    Mirrors concat_list.txt's exact ordering: one silence file then one
    chunk audio file, per chunk. Walking that same sequence and summing
    ffprobe'd durations as we go gives each chunk's [start, end) window in
    the final stitched .m4a for free, with no separate alignment pass.

    This stays exact through the AAC encode. AAC adds ~1024 samples of
    encoder priming at the start and pads the last frame; ffmpeg records
    both in the MP4 edit list, and players (browsers included) trim them,
    so the presented timeline is sample-for-sample the concatenated wavs.
    Measured: 0.0 ms drift on all 48 chapters of the re-encoded library.

    Silence-wav durations are probed once per kind and reused (the wavs
    themselves are already shared/cached the same way by get_silence_wavs)."""
    cumulative = 0.0
    silence_durations = {}
    entries = []

    for idx, audio_file in enumerate(audio_files):
        kind = silence_kind_before_wav[idx]
        if kind not in silence_durations:
            silence_durations[kind] = get_audio_duration(silence_wavs[kind])
        cumulative += silence_durations[kind]

        chunk_duration = get_audio_duration(audio_file)
        start = cumulative
        end = cumulative + chunk_duration

        entries.append({
            "index": idx,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": sync_chunk_texts[idx],
        })

        cumulative = end

    return {"version": 1, "chunks": entries}


def aac_stitch_command(concat_list_path, output_path):
    """The ffmpeg command that turns a chapter's concat list into its final
    .m4a. A function of its own so the encode can be exercised against a
    kept concat list without re-running TTS."""
    return [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c:a", "aac",
        "-b:a", AAC_BITRATE,
        "-ac", "1",
        # moov atom before mdat. Load-bearing: the player streams these from
        # R2 with HTTP Range requests, and without faststart it must fetch
        # the index from the END of the file before it can play anything.
        # Tagging afterwards keeps it in front (mutagen rewrites moov in
        # place and shifts mdat's chunk offsets).
        "-movflags", "+faststart",
        output_path,
    ]


def flac_master_command(concat_list_path, output_path):
    """The lossless master, built from the SAME concat list the .m4a is.

    Deliberately a second pass over that list rather than encoding the
    .m4a from the FLAC: the concat -> AAC path is the one whose timing was
    verified sample-exact, and it stays untouched. FLAC is lossless, so
    both encoders receive byte-identical PCM and the master is a faithful
    record of what was encoded.

    No `-ac`: the master preserves the TTS output as it is. The .m4a
    downmixes to mono because that is a delivery decision, and a master
    should not bake a delivery decision in."""
    return [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c:a", "flac",
        "-sample_fmt", "s16",
        "-compression_level", "8",
        output_path,
    ]


def run_batch_worker(jobs_path, chunks):
    """Runs irodori_batch.py once for the whole chapter and relays its
    progress, translating the worker's protocol lines into the same
    "Generating chunk i/N ..." output this script has always printed - the
    GUI progress window parses exactly that (see gui_settings._CHUNK_LINE_RE),
    so the batching stays invisible to it.

    Reading the pipe line by line (with "-u" on the worker) is what keeps the
    progress live; buffering it would freeze the progress bar for minutes and
    then jump."""
    cmd = ["uv", "run", "--no-sync", "python", "-u", BATCH_SCRIPT_PATH,
           "--jobs", jobs_path]
    try:
        proc = subprocess.Popen(
            cmd, cwd=UV_PROJECT_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Error: could not start the TTS worker ({e}).")
        return

    total = len(chunks)
    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        if line.startswith("MODEL_LOADED "):
            print(f"Model loaded in {line.split()[1]}s - reused for all "
                  f"{total} chunk(s) of this chapter.")
        elif line.startswith("CHUNK_START "):
            i = int(line.split()[1])
            chunk = chunks[i - 1]
            print(f" Generating chunk {i}/{total} "
                  f"(sec {chunk['section']:03d} par {chunk['paragraph']:03d}, "
                  f"{len(chunk['text'])} chars, "
                  f"silence_{chunk['silence_kind']} before "
                  f"[{','.join(chunk['boundary_tags'])}])...")
        elif line.startswith("CHUNK_FAIL "):
            parts = line.split(" ", 2)
            print(f"  ! chunk {parts[1]} failed: "
                  f"{parts[2] if len(parts) > 2 else '(no message)'}")
        elif line == "WATERMARK off":
            print("SilentCipher watermarking is OFF for this run.")
        elif line.startswith("BATCH_LOG "):
            print(f"  Worker log: {line.split(' ', 1)[1]}")
        elif line.startswith(("CHUNK_DONE ", "BATCH_DONE ")):
            pass  # accounted for by the wav check below
        elif line:
            print(line)  # anything unexpected is worth seeing

    code = proc.wait()
    if code != 0:
        print(f"Error: the TTS worker exited with code {code} - no audio was "
              f"produced for this chapter.")


def finish_chapter(chapter_base, work_dir, audio_files, silence_kind_before_wav,
                   sync_chunk_texts, silence_wavs, after_sync=None):
    """Steps 5-6 of a chapter, shared by normal and dynamic mode: concat
    list -> stitched .m4a -> FLAC master -> sync.json -> auto-tag.

    `silence_kind_before_wav[i]` names the key of `silence_wavs` to put in
    front of `audio_files[i]` - sentence/paragraph/section in normal mode,
    section/sentence/comma in dynamic mode. `after_sync(sync_data)` runs once
    sync.json is written and before tagging, while the per-chunk wavs still
    exist - dynamic mode writes render.json there.

    Returns False if the stitch failed (nothing after it was written)."""
    output_audio = os.path.join(OUTPUT_FOLDER, f"{chapter_base}.m4a")

    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for idx, audio_file in enumerate(audio_files):
            kind = silence_kind_before_wav[idx]
            f.write(f"file '{silence_wavs[kind]}'\n")
            f.write(f"file '{os.path.abspath(audio_file)}'\n")

    print(f"Stitching {chapter_base} into final .m4a...")
    stitch = subprocess.run(aac_stitch_command(concat_list_path, output_audio),
                            capture_output=True)
    if stitch.returncode != 0 or not os.path.exists(output_audio):
        # Without this a failed encode printed "Done!" anyway and went on to
        # write a sync.json for audio that does not exist.
        stderr = stitch.stderr.decode("utf-8", "replace").strip()
        print(f"Error: ffmpeg failed to stitch {chapter_base} "
              f"(exit code {stitch.returncode}):")
        print("    " + (stderr[-600:].replace("\n", "\n    ")
                        if stderr else "(no error output)"))
        return False
    print(f"Done! Saved to: {output_audio}")

    # Step 5a: the lossless master, from the same concat list. A failure
    # here must not cost the chapter - the .m4a, sync.json and the
    # subtitles are all still correct without it, so it warns and carries
    # on rather than returning.
    if KEEP_FLAC_MASTER:
        master_path = os.path.join(OUTPUT_FOLDER, f"{chapter_base}.flac")
        master = subprocess.run(flac_master_command(concat_list_path, master_path),
                                capture_output=True)
        if master.returncode != 0 or not os.path.exists(master_path):
            stderr = master.stderr.decode("utf-8", "replace").strip()
            print(f"Warning: could not write the FLAC master (exit code "
                  f"{master.returncode}). The chapter itself is fine.")
            print("    " + (stderr[-400:].replace("\n", "\n    ")
                            if stderr else "(no error output)"))
        else:
            size = os.path.getsize(master_path) / (1024 * 1024)
            print(f"Master saved to: {master_path} ({size:.1f} MB)")
            print("    Move it out of the output folder before publishing - "
                  "the player has no use for it.")

    # A regenerated chapter can leave an .mp3 from before the switch to AAC.
    # The player prefers the .m4a so it is harmless there, but that .mp3 is
    # no longer timed against the sync.json about to be written. Deleting
    # finished work is left to the person, so just say so.
    stale_mp3 = os.path.join(OUTPUT_FOLDER, f"{chapter_base}.mp3")
    if os.path.exists(stale_mp3):
        print(f"Note: {os.path.basename(stale_mp3)} from an earlier run is "
              f"still in the output folder and no longer matches the new "
              f"sync.json. The player uses the .m4a; delete the .mp3 before "
              f"using it anywhere else.")

    # Step 5b: Build and write sync.json - chunk start/end offsets (in
    # seconds) into the just-stitched .m4a, for the Android player app (see
    # android-player-phase0-spec.md). MUST run before Step 6's cleanup:
    # build_sync_data() needs ffprobe access to the individual per-chunk
    # wav files, which clean_temp_dir() deletes right afterwards.
    sync_data = build_sync_data(audio_files, sync_chunk_texts, silence_kind_before_wav, silence_wavs)
    sync_path = os.path.join(OUTPUT_FOLDER, f"{chapter_base}.sync.json")
    with open(sync_path, "w", encoding="utf-8") as f:
        json.dump(sync_data, f, ensure_ascii=False, indent=2)
    print(f"Sync data saved to: {sync_path}")

    if after_sync:
        after_sync(sync_data)

    # Step 6: Auto-tag step - runs the audio_metadata.py tagger from the GUI
    # project's OWN lightweight uv venv (via `--project`), not this heavy
    # Irodori-TTS venv, so mutagen never needs to be installed here. Runs
    # right away, per chapter, so the file is fully usable (correct
    # metadata/album art for Spotify/phone) the moment it lands in the
    # output folder - no need to wait for the rest of the book to finish
    # before copying chapters over. Only runs when "Auto-tag generated
    # files" is turned on in the Metadata settings tab - otherwise the
    # person applies tags manually afterwards via the GUI's "Apply Tags to
    # Output Files" button.
    if SETTINGS.get("auto_tag_generated_files", False):
        print(f"Auto-tagging {os.path.basename(output_audio)}...")
        try:
            tag_result = subprocess.run(
                ["uv", "run", "--project", SCRIPT_DIR, "--no-sync", "python",
                 os.path.join(SCRIPT_DIR, "audio_metadata.py"),
                 "--chapter", chapter_base],
                cwd=SCRIPT_DIR, capture_output=True, text=True,
            )
            if tag_result.stdout:
                print(tag_result.stdout.strip())
            if tag_result.returncode != 0:
                print(f"Auto-tagging failed (exit code {tag_result.returncode}):")
                print(tag_result.stderr.strip())
        except Exception as e:
            print(f"Auto-tagging failed to start: {e}")
    return True


def process_chapter(chapter_path):
    chapter_name = os.path.splitext(os.path.basename(chapter_path))
    print(f"\n>>> Processing: {chapter_name[0]}")

    # Step 1: Read raw text
    with open(chapter_path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    # Step 2: Run the text pipeline (section -> paragraph -> sentence ->
    # merged TTS input chunks, with 「」/（） edges and "──" as forced
    # break points rather than whole-span isolation). See text_pipeline.py
    # / text-cleaning-logic-spec.md.
    chunks, working_data = text_pipeline.build_chunks(
        raw_text, soft_limit=MAX_CHUNK_LENGTH, hard_limit=MAX_CHUNK_LENGTH_HARD)

    if not chunks:
        print(f"Error: No text chunks produced for {chapter_name[0]} - is the file empty?")
        return

    print(f"Found {len(working_data['sections'])} section(s), "
          f"{sum(len(p) for p in working_data['paragraphs'].values())} paragraph(s), "
          f"{len(chunks)} TTS input chunk(s).")

    # Step 3: Write out the sec/par/sen/input working files for this
    # chapter so they can be inspected if something looks off.
    work_dir = os.path.join(TEMP_DIR, chapter_name[0])
    write_working_files(working_data, chunks, work_dir)

    # Step 4: Generate audio for every chunk, in ONE worker process that
    # loads the model once (see irodori_batch.py and run_batch_worker).
    audio_files = []          # list of wav paths, in order
    silence_kind_before_wav = []  # which silence wav precedes each entry
    sync_chunk_texts = []     # chunk["display_text"] (original wording,
                               # not the TTS-normalized text), parallel to
                               # audio_files - only used to build sync.json
                               # (see build_sync_data)

    wav_by_index = {}
    jobs = []
    for i, chunk in enumerate(chunks, start=1):
        wav_filename = os.path.join(
            work_dir, text_pipeline.chunk_filename(chunk, ext="wav"))
        wav_by_index[i] = wav_filename
        jobs.append({"index": i, "text": chunk["text"],
                     "output_wav": wav_filename})

    # One worker for the whole chapter instead of one infer.py per chunk.
    # The model load dominated everything else: 19.1 s per chunk of which
    # only ~1.9 s was generation, measured 2026-09-14. Same engine, same
    # request parameters - verified byte-identical output on a fixed seed
    # before this replaced the per-chunk path.
    jobs_path = os.path.join(work_dir, "batch_jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump({
            "uv_project_dir": UV_PROJECT_DIR,
            "checkpoint": CHECKPOINT_ARGS[1],
            "checkpoint_is_hf": CHECKPOINT_ARGS[0] == "--hf-checkpoint",
            "speaker_path": SPEAKER_PATH,
            "duration_scale": DURATION_SCALE,
            "trim_tail": not NO_TRIM_TAIL,
            "seed": SEED_VALUE if SEED_ENABLED else None,
            "watermark": WATERMARK_AUDIO,
            "jobs": jobs,
        }, f, ensure_ascii=False, indent=2)

    run_batch_worker(jobs_path, chunks)

    for i, chunk in enumerate(chunks, start=1):
        wav_filename = wav_by_index[i]
        if os.path.exists(wav_filename):
            audio_files.append(wav_filename)
            silence_kind_before_wav.append(chunk["silence_kind"])
            sync_chunk_texts.append(chunk["display_text"])
        else:
            # The worker already said why on its CHUNK_FAIL line; this keeps
            # the old habit of naming every chunk that produced no audio, so
            # a systematic failure is obvious in the log rather than silent.
            print(f"  ! chunk {i} produced no audio.")

    # Step 5: Combine parts into the final .m4a, inserting exactly one silence
    # file before each chunk - silence_sentence.wav, silence_paragraph.wav
    # or silence_section.wav, chosen by chunk["silence_kind"]. Each has its
    # own duration from the GUI's Advanced page, so long gaps are a single
    # correctly-sized file rather than the same 1x file repeated 2x/3x as
    # in earlier versions.
    if not audio_files:
        print(f"Error: No audio parts generated for {chapter_name[0]}")
        return

    # The silence wavs are rendered once for the whole run, from the first
    # chapter's first TTS wav; later chapters hit the _SILENCE_WAVS cache
    # and skip both the probe and the render.
    if _SILENCE_WAVS:
        silence_wavs = _SILENCE_WAVS
    else:
        audio_format = probe_audio_format(audio_files[0])
        print(f"Detected TTS output: {audio_format['sample_rate']}Hz, "
              f"{audio_format['channels']}ch, {audio_format['sample_fmt']} "
              f"({audio_format['codec_name']}) - rendering matching silence "
              f"files into {SILENCE_DIR}...")
        silence_wavs = get_silence_wavs(audio_format)

    # Steps 5-6 (stitch, FLAC master, sync.json, auto-tag) are shared with
    # dynamic mode - see finish_chapter().
    if not finish_chapter(chapter_name[0], work_dir, audio_files,
                          silence_kind_before_wav, sync_chunk_texts, silence_wavs):
        return

    # Step 7: Cleanup temporary files for this chapter (unless disabled in
    # settings.json)
    if CLEAN_TEMP_AFTER_RUN:
        print(f"Cleaning up temporary files in {TEMP_DIR}...")
        clean_temp_dir()
    else:
        print(f"Skipping temp cleanup (clean_temp_after_run is disabled). Files remain in {TEMP_DIR}")


# ---------------------------------------------------------------------------
# Dynamic profile mode (2026-09-17)
# ---------------------------------------------------------------------------
#
# A chapter is rendered sentence by sentence from a book-profiler profile
# instead of from the silence / chunk length / duration scale settings. All
# of the text and recipe logic lives in dynamic_profile.py, shared with the
# GUI and book-profiler; this section only plans the run, drives the same
# irodori_batch.py worker and hands the result to finish_chapter().

GENERATION_MODE = "dynamic" if SETTINGS.get("generation_mode") == "dynamic" else "normal"


def resolve_dynamic_plan(chapter_files):
    """{chapter_base: assignment or None} for every chapter file, where an
    assignment is {"profile": loaded profile, "style": key}. None = skipped
    (unticked in Customize). Raises dynamic_profile.ProfileError for any
    profile that cannot be used - before a single second of GPU time."""
    block = SETTINGS["dynamic"]
    loaded = {}

    def profile_at(path):
        key = os.path.abspath(path) if path else path
        if key not in loaded:
            loaded[key] = dynamic_profile.load_profile(path)
            ok, note = dynamic_profile.speaker_status(loaded[key])
            if not ok:
                raise dynamic_profile.ProfileError(f"{path}: {note}")
        return loaded[key]

    plan = {}
    for chapter_file in chapter_files:
        base = os.path.splitext(os.path.basename(chapter_file))[0]
        if block.get("assign") == "custom":
            entry = (block.get("chapters") or {}).get(base)
            if not entry or not entry.get("enabled"):
                plan[base] = None
                continue
            path, style = entry.get("profile_path"), entry.get("style")
        else:
            path, style = block.get("profile_path"), block.get("style")
        style = style or dynamic_profile.DEFAULT_STYLE
        dynamic_profile.split_style(style)
        profile = profile_at(path)
        # A narrow seiyuu's profile offers fewer styles: refuse up front.
        try:
            dynamic_profile.check_style(profile, style)
        except dynamic_profile.ProfileError as e:
            raise dynamic_profile.ProfileError(f"{base}: {e}")
        plan[base] = {"profile": profile, "style": style}
    return plan


def render_silence_set(audio_format, durations, folder):
    """{kind: wav} for `durations` ({kind: seconds}), matched to the TTS
    output's exact format like get_silence_wavs() - the concat demuxer does
    no resampling. Per chapter in dynamic mode, since two chapters may use
    profiles with different silences."""
    os.makedirs(folder, exist_ok=True)
    rate, channels = audio_format["sample_rate"], audio_format["channels"]
    layout = channel_layout_for(channels)
    out = {}
    for kind, duration in durations.items():
        path = os.path.abspath(os.path.join(folder, f"silence_{kind}.wav"))
        base = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}",
                "-t", str(duration), "-ar", str(rate), "-ac", str(channels)]
        result = subprocess.run(base + ["-acodec", audio_format["codec_name"],
                                        "-sample_fmt", audio_format["sample_fmt"], path],
                                capture_output=True)
        if not os.path.exists(path):
            subprocess.run(base + [path], capture_output=True)
        if not os.path.exists(path):
            raise RuntimeError(f"Failed to render {path}. ffmpeg said: "
                               f"{result.stderr.decode('utf-8', 'replace').strip()}")
        out[kind] = path
    return out


def run_batch_worker_dynamic(jobs_path, pieces):
    """run_batch_worker() for dynamic mode: the same "Generating chunk i/N"
    lines the GUI progress window parses, describing a sentence and its
    request instead of a section/paragraph chunk. Returns {index: used seed}
    for render.json."""
    cmd = ["uv", "run", "--no-sync", "python", "-u", BATCH_SCRIPT_PATH, "--jobs", jobs_path]
    try:
        proc = subprocess.Popen(cmd, cwd=UV_PROJECT_DIR, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Error: could not start the TTS worker ({e}).")
        return {}

    total = len(pieces)
    seeds = {}
    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        if line.startswith("MODEL_LOADED "):
            print(f"Model loaded in {line.split()[1]}s - reused for all "
                  f"{total} sentence(s) of this chapter.")
        elif line.startswith("CHUNK_START "):
            i = int(line.split()[1])
            p = pieces[i - 1]
            request = p["request"]
            asked = (f"x{request['duration_scale']}" if "duration_scale" in request
                     else f"{request['seconds']}s")
            print(f" Generating chunk {i}/{total} (sentence {p['sentence']}"
                  f"{'.' + str(p['piece']) if p['piece'] > 1 else ''}, "
                  f"{p['engine_len']} chars, {asked}, silence_{p['gap']} before)...")
        elif line.startswith("CHUNK_DONE "):
            parts = line.split()
            if len(parts) > 2 and parts[2] != "None":
                seeds[int(parts[1])] = int(parts[2])
        elif line.startswith("CHUNK_FAIL "):
            parts = line.split(" ", 2)
            print(f"  ! chunk {parts[1]} failed: "
                  f"{parts[2] if len(parts) > 2 else '(no message)'}")
        elif line == "WATERMARK off":
            print("SilentCipher watermarking is OFF for this run.")
        elif line.startswith("BATCH_LOG "):
            print(f"  Worker log: {line.split(' ', 1)[1]}")
        elif line.startswith("BATCH_DONE "):
            pass
        elif line:
            print(line)
    code = proc.wait()
    if code != 0:
        print(f"Error: the TTS worker exited with code {code} - no audio was "
              f"produced for this chapter.")
    return seeds


def wav_duration(path):
    try:
        import wave
        with wave.open(path, "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:
        return get_audio_duration(path)


CHAPTER_HEADER_MAX = 40


def looks_like_header(line):
    """A chapter number or title line, not prose: short and with no
    sentence terminator. The extractor writes one for every chapter now;
    a file without one is still handled (see insert_intro_line)."""
    text = (line or "").strip()
    if not text or len(text) > CHAPTER_HEADER_MAX:
        return False
    return not any(mark in text for mark in ("。", "？", "！", "……"))


def insert_intro_line(raw_text, line):
    """`朗読者：…` after the chapter number and its title, as its own line -
    so text_pipeline gives it a 1.0 s sentence gap and the prose after it
    keeps the 1.5 s section gap (user decision 2026-09-24).

    With no header at all the name goes first, then a blank line, then the
    prose."""
    lines = raw_text.splitlines()
    if not lines or not looks_like_header(lines[0]):
        return "\n".join([line, ""] + lines) + "\n"
    at = 1
    if len(lines) > 1 and lines[1].strip() and looks_like_header(lines[1]):
        at = 2                      # the chapter has a title as well
    return "\n".join(lines[:at] + [line] + lines[at:]) + "\n"


def process_chapter_dynamic(chapter_path, assignment, engine, readings=None, intro=None,
                            furigana_applied=None, book_slug=None):
    """One chapter in dynamic profile mode. `assignment` comes from
    resolve_dynamic_plan(); `engine` is dynamic_profile.make_engine();
    `readings` is dynamic_profile.load_readings() - TTS text only.

    `intro` is (display line, tts line) for the seiyuu credit, or None for a
    legacy book. Returns the chapter's record for the suite library, or None
    when nothing was produced."""
    base = os.path.splitext(os.path.basename(chapter_path))[0]
    profile, style = assignment["profile"], assignment["style"]
    print(f"\n>>> Processing: {base}")
    print(f"Profile: {profile['_path']}")
    print(f"  {profile.get('book')} / {dynamic_profile.nickname(profile)} / "
          f"{dynamic_profile.STYLE_LABELS[style]}"
          + ("" if base in (profile.get("chapters") or []) else
             f"  - {base} was not among the profile's measured chapters"))

    with open(chapter_path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    # A preview plan is bound to the FILE's text, before the credit is put
    # in front of it - the plan already contains that line.
    plan, note = (preview.usable_plan(TEMP_DIR, book_slug, base, raw_text, profile["_path"],
                                      style) if book_slug else (None, None))
    if note:
        print(f"Preview: {note}")
    if plan is None and book_slug:
        preview.discard_plan(TEMP_DIR, book_slug, base)

    intro_display, intro_tts = intro if intro else (None, None)
    if plan is not None:
        intro_display = plan.get("intro_line")
    elif intro_display:
        raw_text = insert_intro_line(raw_text, intro_display)
        # The reader shows the written name, the engine is sent the kana.
        # A reading does that without a special case in plan_pieces; it is
        # the longest word in play, so it is applied first.
        readings = sorted((readings or []) + suite_link.intro_readings(intro),
                          key=lambda r: -len(r["word"]))
        print(f"Intro line: {intro_display}  (spoken: {intro_tts})")

    # Furigana: approved annotations are spoken as written, the rest are
    # stripped so the engine never sees the parens (which it would read as
    # a pause and then the reading - see furigana.py).
    found = furigana.pairs(raw_text)
    if found and plan is None:
        approved = {p: n for p, n in found.items() if p in (furigana_applied or set())}
        print(f"Furigana: {sum(found.values())} annotation(s), "
              f"{sum(approved.values())} applied, "
              f"{sum(found.values()) - sum(approved.values())} stripped "
              f"(the seiyuu decides those)")
    # The plan already holds the text as the preview showed it - furigana,
    # readings and the credit included - so it is rendered as it stands.
    if plan is not None:
        pieces = preview.pieces_from_plan(plan, profile, style, engine)
        skipped = []
    else:
        pieces, skipped = dynamic_profile.plan_chapter(raw_text, profile, style, engine, readings,
                                                       furigana_applied)
    if not pieces:
        print(f"Error: No sentences produced for {base} - is the file empty?")
        return
    cut = sum(1 for p in pieces if p["piece"] > 1)
    beyond = sum(1 for p in pieces if p["beyond"])
    with_readings = sum(1 for p in pieces if p["readings"])
    print(f"Found {len(pieces)} request(s) from {len({p['sentence'] for p in pieces})} "
          f"sentence(s); {cut} cut piece(s) past L={profile['comfortable_length']}, "
          f"{beyond} longer than any measured band, {len(skipped)} skipped (nothing to read)"
          + (f", {with_readings} with a reading." if readings else "."))

    work_dir = os.path.join(TEMP_DIR, base)
    os.makedirs(work_dir, exist_ok=True)
    jobs = []
    for i, p in enumerate(pieces, start=1):
        p["wav"] = os.path.join(work_dir, f"s{p['sentence']:04d}p{p['piece']:02d}.wav")
        with open(p["wav"][:-4] + ".txt", "w", encoding="utf-8") as f:
            f.write(p["tts_text"])
        job = {"index": i, "text": p["tts_text"], "output_wav": p["wav"]}
        job.update(p["request"])
        jobs.append(job)

    jobs_path = os.path.join(work_dir, "batch_jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump({
            "uv_project_dir": UV_PROJECT_DIR,
            "checkpoint": CHECKPOINT_ARGS[1],
            "checkpoint_is_hf": CHECKPOINT_ARGS[0] == "--hf-checkpoint",
            "speaker_path": profile["speaker_path"],
            "duration_scale": 1.0,
            "trim_tail": bool(profile.get("trim_tail", True)),
            # A fresh draw for every sentence: a fixed seed made chapters
            # worse (CLAUDE.md), and the profiler measured no benefit.
            "seed": None,
            "watermark": WATERMARK_AUDIO,
            "jobs": jobs,
        }, f, ensure_ascii=False, indent=2)

    seeds = run_batch_worker_dynamic(jobs_path, pieces)

    rendered = [p for p in pieces if os.path.exists(p["wav"])]
    for i, p in enumerate(pieces, start=1):
        p["used_seed"] = seeds.get(i)
        if not os.path.exists(p["wav"]):
            print(f"  ! chunk {i} produced no audio.")
    if not rendered:
        print(f"Error: No audio parts generated for {base}")
        return

    silence_wavs = render_silence_set(
        probe_audio_format(rendered[0]["wav"]),
        {kind: float(profile["silence"][kind]) for kind in dynamic_profile.SILENCE_KINDS},
        os.path.join(work_dir, "_silence"))

    def write_render_json(sync_data):
        for index, p in enumerate(rendered):
            p["sync_index"] = index
        record = {
            "version": 1,
            "mode": "dynamic",
            "chapter": base,
            # The book's slug in the suite library, so a tool working on a
            # COPY of this folder (dynamic-repair) can still find the book;
            # its own output folder no longer matches. Null for a book the
            # library does not track.
            "book_slug": book_slug,
            "rendered_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "checkpoint": MODEL_REF,
            "watermark": WATERMARK_AUDIO,
            "profile": {
                "path": profile["_path"],
                "version": profile.get("version"),
                "book": profile.get("book"),
                "scope": profile.get("scope"),
                "chapters": profile.get("chapters"),
                "created": profile.get("created"),
                "speaker_path": profile["speaker_path"],
                "speaker_stamp_at_profiling": profile.get("speaker_stamp"),
                "speaker_stamp_at_render": dynamic_profile.speaker_stamp(profile["speaker_path"]),
                "comfortable_length": profile["comfortable_length"],
                "silence": profile["silence"],
                "trim_tail": profile.get("trim_tail"),
                "bands": profile["bands"],
                "pace_targets": profile["pace_targets"],
                # dynamic-repair offers only these (v3; load_profile fills
                # all three for a v2 profile).
                "available_speeds": profile["available_speeds"],
            },
            "style": style,
            "style_label": dynamic_profile.STYLE_LABELS[style],
            "chapter_was_profiled": base in (profile.get("chapters") or []),
            # The seiyuu credit this render put in front of the chapter, or
            # null for a legacy book (see suite_link.py).
            "intro_line": intro_display,
            # The book's readings as they were at render time; each piece
            # lists the ones its TTS text used.
            "readings": [{"word": r["word"], "reading": r["reading"]} for r in readings or []],
            "sync_entries": len(sync_data["chunks"]),
            "skipped_texts": skipped,
            "pieces": [{
                "sync_index": p.get("sync_index"),
                # The source section (blank-line separated) this came from,
                # and whether a person edited it in Preview Chapters - a
                # repair must not "correct" a deliberate edit back.
                "section": p.get("section"),
                "edited": bool(p.get("edited")),
                "sentence": p["sentence"],
                "piece": p["piece"],
                "display_text": p["display_text"],
                "tts_text": p["tts_text"],
                "readings": p["readings"],
                "engine_len": p["engine_len"],
                "band": p["band"],
                "beyond_measured": p["beyond"],
                "request": p["request"],
                "used_seed": p["used_seed"],
                "seconds": wav_duration(p["wav"]) if os.path.exists(p["wav"]) else None,
                "gap_before": p["gap"],
                "removed_before": p["removed_before"],
                "removed_after": p["removed_after"],
            } for p in pieces],
        }
        path = os.path.join(OUTPUT_FOLDER, f"{base}.render.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"Render record saved to: {path}")

    if not finish_chapter(base, work_dir, [p["wav"] for p in rendered],
                          [p["gap"] for p in rendered],
                          [p["display_text"] for p in rendered],
                          silence_wavs, after_sync=write_render_json):
        return None

    # What the suite library records; the caller writes it, because only
    # main() knows whether this book uses the database at all. Measured
    # before the cleanup, while the per-piece wavs still exist.
    summary = {
        "chapter": base,
        "profile_path": profile["_path"],
        "style": style,
        "speaker_path": profile["speaker_path"],
        "sync_entries": len(rendered),
        "intro_line": intro_display,
        "readings_applied": {w: r for p in pieces for w, r in (p["readings"] or {}).items()},
        "furigana_applied": sum(n for pair, n in furigana.pairs(raw_text).items()
                                if pair in (furigana_applied or set())),
        "edits_applied": sum(1 for p in pieces if p.get("edited")),
        "edit_ids": (plan or {}).get("edit_ids") or [],
        "seconds": round(sum(wav_duration(p["wav"]) for p in rendered
                             if os.path.exists(p["wav"])), 2),
        "render_json_path": os.path.join(OUTPUT_FOLDER, f"{base}.render.json"),
    }

    # The plan has been rendered, so it goes (user decision 2026-09-24:
    # the temp file lives only until the chapter is generated). What was
    # changed stays in the library's chapter_edits.
    if plan is not None and book_slug:
        preview.discard_plan(TEMP_DIR, book_slug, base)
        print(f"Preview: plan for {base} used and removed "
              f"({summary['edits_applied']} edited line(s))")

    if CLEAN_TEMP_AFTER_RUN:
        print(f"Cleaning up temporary files in {TEMP_DIR}...")
        clean_temp_dir()
    else:
        print(f"Skipping temp cleanup (clean_temp_after_run is disabled). Files remain in {TEMP_DIR}")
    return summary


def record_chapter(suite, book, seiyuu, summary):
    """One rendered chapter into the suite library: the record, and the
    seiyuu's usage count (re-rendering the same chapter with the same voice
    increments it - user decision 2026-09-24). Never fatal: a library
    problem must not lose a chapter that rendered fine."""
    if suite is None or not book:
        return
    try:
        record_id = suite.add_chapter_record(
            book["id"], summary["chapter"],
            seiyuu_id=(seiyuu or {}).get("id"),
            profile_path=summary["profile_path"], style=summary["style"],
            sync_entries=summary["sync_entries"], intro_line=summary["intro_line"],
            readings_applied=summary["readings_applied"], seconds=summary["seconds"],
            furigana_applied=summary.get("furigana_applied"),
            edits_applied=summary.get("edits_applied"),
            render_json_path=summary["render_json_path"])
        # The preview edits that went into this render now point at it.
        if summary.get("edit_ids"):
            suite.mark_edits_applied(summary["edit_ids"], record_id)
        if seiyuu:
            suite.record_usage(seiyuu["id"], book["id"], summary["chapter"],
                               summary["profile_path"], summary["style"])
        # The profile was MEASURED when book-profiler wrote it; this is
        # when it was USED, which is the other half of the question
        # "when did this book get made" (2026-09-25).
        suite_link.profile_used(suite, summary["profile_path"])
        print(f"  library: recorded {summary['chapter']}"
              + (f" for {seiyuu['nickname']}" if seiyuu else ""))
    except Exception as e:
        print(f"  ! could not record {summary['chapter']} in the library "
              f"({type(e).__name__}: {e}) - the chapter itself is fine")


def snapshot_filename(when=None):
    """`<bookname>_<mode>_YYYYMMDD_HHMM.json` - mode is `normal` or `dynamic`.

    The two modes' settings are no longer interchangeable (dynamic mode
    carries profiles and a chapter plan, normal mode silences and a scale),
    so the mode is in the name to make a folder of snapshots readable at a
    glance. The GUI's Import refuses a file of the other mode.

    The book name is the output folder's own name - "wall", "sputnik" - which
    is already how the library is organised, one folder per book, and keeps
    the filename ASCII and sortable. The Book Title setting is deliberately
    not used: it is usually Japanese, which is legal on NTFS but awkward to
    type, to script against, and to read in a sorted listing."""
    name = os.path.basename(os.path.normpath(OUTPUT_FOLDER))
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    stamp = (when or datetime.datetime.now()).strftime("%Y%m%d_%H%M")
    mode = "dynamic" if SETTINGS.get("generation_mode") == "dynamic" else "normal"
    return f"{safe or 'settings'}_{mode}_{stamp}.json"


def write_settings_snapshot(chapter_count, dynamic_plan=None):
    """Drops a copy of the settings this run is using into the output folder.

    The point is traceability after the fact: with this many tunables, the
    only reliable way to know why a book six months old sounds the way it
    does is to have the parameters sitting next to it. Written *before* the
    first chapter rather than after the last, so a run that crashes halfway
    still leaves a record of what it was attempting.

    Run metadata lives under "_run" so the rest of the file stays a faithful
    copy of settings.json - which means a snapshot can be fed straight back
    through the GUI's Import Settings to rebuild a preset.

    Failure here is logged, never fatal: nobody should lose a night of
    generation because a bookkeeping file could not be written."""
    path = os.path.join(OUTPUT_FOLDER, snapshot_filename())
    snapshot = dict(SETTINGS)
    snapshot["_run"] = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "chapters_found": chapter_count,
        "note": "Automatic snapshot of the settings used for this run. "
                "Importable via the GUI's Import Settings button.",
    }
    if dynamic_plan is not None:
        # What each chapter was actually given, resolved - so the record
        # survives the profile files being moved or rewritten later.
        snapshot["_run"]["dynamic_plan"] = {
            base: None if a is None else {
                "profile_path": a["profile"]["_path"],
                "book": a["profile"].get("book"),
                "seiyuu": dynamic_profile.nickname(a["profile"]),
                "style": a["style"],
                "style_label": dynamic_profile.STYLE_LABELS[a["style"]],
                "chapter_was_profiled": base in (a["profile"].get("chapters") or []),
            } for base, a in dynamic_plan.items()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"Settings snapshot saved to: {path}")
    except OSError as e:
        print(f"Could not write the settings snapshot ({e}) - continuing anyway.")


def main():
    # Fail fast on a local checkpoint that isn't there, rather than letting
    # every chunk in the book fail the same way one at a time.
    if CHECKPOINT_ARGS[0] == "--checkpoint" and not os.path.isfile(CHECKPOINT_ARGS[1]):
        print(f"Error: Model Path is a local file that doesn't exist:\n  {CHECKPOINT_ARGS[1]}")
        print("Point it at an existing .safetensors file, or at a Hugging Face "
              "repo id such as Aratako/Irodori-TTS-v4.1-Small.")
        return

    source = "local file" if CHECKPOINT_ARGS[0] == "--checkpoint" else "Hugging Face repo"
    print(f"Model: {CHECKPOINT_ARGS[1]}  ({source})")

    # Find all chapter_*.txt files in E:\AUDIOBOOK\chapter
    chapter_files = sorted(glob.glob(os.path.join(INPUT_FOLDER, "chapter_*.txt")))

    if not chapter_files:
        print(f"No files found in {INPUT_FOLDER} matching 'chapter_*.txt'")
        return

    # Dynamic profile mode: resolve and check every chapter's profile before
    # any GPU time is spent, and drop the chapters left unticked in Customize.
    dynamic_plan, engine, readings = None, None, []
    suite, book, new_pipeline, furigana_ok = None, None, False, set()
    if GENERATION_MODE == "dynamic":
        print("Mode: dynamic profile")
        normalize, normalizer_path = dynamic_profile.load_irodori_normalizer(UV_PROJECT_DIR)
        if normalize is None:
            print(f"Error: Irodori's text normaliser was not found at {normalizer_path}. "
                  f"Dynamic mode measures sentences in the characters the engine "
                  f"receives and cannot run without it.")
            return
        engine = dynamic_profile.make_engine(normalize)
        try:
            dynamic_plan = resolve_dynamic_plan(chapter_files)
            # The book's readings, beside glossary.json in the OUTPUT folder
            # (decision 2026-09-22). No file = nothing changes.
            readings_path = os.path.join(OUTPUT_FOLDER, dynamic_profile.READINGS_FILE)
        except dynamic_profile.ProfileError as e:
            print(f"Error: {e}")
            return

        # The suite library. A book it does not know - or one marked
        # legacy - runs exactly as before: no intro line, no records.
        suite = suite_link.open_suite(SETTINGS)
        book = suite_link.book_for_output(suite, OUTPUT_FOLDER)
        new_pipeline = suite_link.uses_new_pipeline(book)
        print("Library: " + suite_link.describe(suite, book, SETTINGS))
        if new_pipeline:
            synced = suite_link.sync_readings(suite, book, OUTPUT_FOLDER)
            if synced:
                if synced["added"]:
                    print(f"  readings.json had {len(synced['added'])} reading(s) the library "
                          f"did not: " + ", ".join(f"{a['word']}→{a['reading']}"
                                                   for a in synced["added"][:6])
                          + " - imported")
                for clash in synced["conflicts"]:
                    print(f"  ! {clash['word']}: the file says {clash['file']}, the library "
                          f"says {clash['db']} - the library's reading is used")
                print(f"  {synced['exported']} reading(s) written back to readings.json")

        furigana_ok = suite_link.furigana_applied(suite, book)
        if new_pipeline:
            print(f"Furigana decisions: {len(furigana_ok)} approved for this book"
                  if furigana_ok else
                  "Furigana decisions: none yet - any annotations are stripped and the "
                  "seiyuu decides (review them in the generator window)")

        try:
            readings = dynamic_profile.load_readings(readings_path)
        except dynamic_profile.ProfileError as e:
            print(f"Error: {e}")
            return
        print(f"Readings: {len(readings)} from {readings_path}" if readings
              else f"Readings: none ({readings_path} not found)")
        unticked = [b for b, a in dynamic_plan.items() if a is None]
        if unticked:
            print(f"Skipping {len(unticked)} chapter(s) not ticked in Customize: "
                  + ", ".join(unticked))
        chapter_files = [f for f in chapter_files
                         if dynamic_plan[os.path.splitext(os.path.basename(f))[0]]]
        if not chapter_files:
            print("Nothing to generate - no chapter is ticked.")
            return
        for f in chapter_files:
            base = os.path.splitext(os.path.basename(f))[0]
            a = dynamic_plan[base]
            print(f"  {base}: {os.path.basename(a['profile']['_path'])} "
                  f"({a['profile'].get('book')} / {dynamic_profile.nickname(a['profile'])}) "
                  f"- {dynamic_profile.STYLE_LABELS[a['style']]}")

    # Chapters whose audio already exists (.m4a, or an .mp3 from before the
    # switch to AAC - see CHAPTER_AUDIO_EXTS) are skipped unless the person
    # has explicitly asked for a rebuild. This replaces the old workflow of
    # moving .txt files in and out of the input folder by hand to avoid
    # clobbering work - which is easy to get wrong, and expensive when you
    # do, since a chapter is hours of GPU time.
    pending, already_done = [], []
    for chapter_file in chapter_files:
        base = os.path.splitext(os.path.basename(chapter_file))[0]
        existing = existing_chapter_audio(base)
        if not REGENERATE_EXISTING_CHAPTERS and existing:
            already_done.append(os.path.basename(existing[0]))
        else:
            pending.append(chapter_file)

    if already_done:
        print(f"Skipping {len(already_done)} chapter(s) that already have audio "
              f"(turn on 'Regenerate existing chapters' to rebuild them):")
        for name in already_done:
            print(f"  - {name}")

    if not pending:
        print("Nothing to generate - every chapter already has audio.")
        return

    print(f"Found {len(chapter_files)} chapters, {len(pending)} to process.")

    # Preview plans left behind by a run that failed or was stopped
    # (user decision 2026-09-24: salvage what is usable, clean up the
    # rest). A plan for a chapter this run is not rendering, or one that
    # cannot be read, can never be used again.
    if new_pipeline and book:
        kept, removed = preview.salvage(
            TEMP_DIR, book["slug"],
            {os.path.splitext(os.path.basename(f))[0] for f in pending})
        if kept:
            print(f"Preview plans found from an earlier run: {', '.join(kept)} - they will be "
                  f"used unless the text, profile or style has changed since")
        if removed:
            print(f"Preview plans that can no longer be used were removed: {', '.join(removed)}")

    write_settings_snapshot(len(pending), dynamic_plan)

    for chapter_file in pending:
        if dynamic_plan is not None:
            base = os.path.splitext(os.path.basename(chapter_file))[0]
            assignment = dynamic_plan[base]
            seiyuu = (suite_link.seiyuu_for_profile(suite, assignment["profile"])
                      if new_pipeline else None)
            intro = suite_link.intro_line(seiyuu) if new_pipeline else (None, None)
            if new_pipeline and not intro[0]:
                nickname = dynamic_profile.nickname(assignment["profile"])
                print(f"  ! no name for {nickname} in the library - {base} gets no intro line. "
                      f"Add it in the generator window (Verify seiyuu name).")
            elif new_pipeline and suite_link.merge_glossary_entry(OUTPUT_FOLDER, seiyuu):
                # So the credit translates with the spelling you chose.
                print(f"  glossary.json: added {seiyuu['display_name']} "
                      f"= {seiyuu['translation_name']}")
            # A furigana decision or a reading may have been made while
            # only certain chapters were selected; it applies to those and
            # no further (schema v2, decision 2026-09-24). So each chapter
            # asks the library what reaches IT - the file's own readings
            # are shared by all of them, as before.
            chapter_readings, chapter_furigana = readings, furigana_ok
            if new_pipeline and suite is not None:
                scoped = suite_link.readings_for(suite, book, base)
                extra = [r for r in scoped
                         if not any(r["word"] == f["word"] for f in readings)]
                chapter_readings = sorted(readings + extra, key=lambda r: -len(r["word"]))
                chapter_furigana = suite_link.furigana_applied(suite, book, base)
                dropped = len(furigana_ok) - len(chapter_furigana)
                if dropped > 0:
                    print(f"  {dropped} furigana decision(s) were made for other chapters "
                          f"and are not applied to {base}")
            summary = process_chapter_dynamic(chapter_file, assignment, engine,
                                              chapter_readings, intro, chapter_furigana,
                                              (book or {}).get("slug"))
            if summary and new_pipeline:
                record_chapter(suite, book, seiyuu, summary)
        else:
            process_chapter(chapter_file)

    print("\nAll chapters completed successfully!")
    rendered_bases = [os.path.splitext(os.path.basename(f))[0] for f in pending]

    # The shared silence wavs were kept alive across chapters by
    # clean_temp_dir()'s skip; now that the run is over they can go too.
    if CLEAN_TEMP_AFTER_RUN and os.path.isdir(SILENCE_DIR):
        shutil.rmtree(SILENCE_DIR, ignore_errors=True)

    # Translation runs once here, at the end of the whole book, rather than
    # per chapter the way tagging does. Two independent reasons: a local
    # translation model and Irodori-TTS would contend for the same 8GB card
    # if this ran between chapters, and the book-level glossary is better
    # applied in one pass over a finished book. The automatic path is just
    # the standalone path with --all, which is also what makes back-filling
    # an already-generated book free.
    #
    # Runs in the GUI-side venv via `uv run --project`, matching
    # audio_metadata.py. Two deliberate choices about its output:
    #   - not captured, so it inherits this process's stdout and flows
    #     straight into the GUI progress window's log as it happens;
    #   - "-u", so Python doesn't block-buffer that pipe. Without it the
    #     per-chapter progress would sit in an 8KB buffer and arrive in one
    #     lump hours later, which defeats the point (gui_settings.py passes
    #     -u when launching this script for exactly the same reason).
    # The QA scan, before translation: it answers "is any of this worth
    # re-rendering", which is a question to settle before spending an hour
    # on subtitles for it. Dynamic mode only, because it reads Parts from
    # sync.json and render.json. Its own process, in the GUI venv (Whisper
    # is shelled out from there), unbuffered so the report arrives in the
    # progress window as it happens - the same shape as translation below.
    if GENERATION_MODE == "dynamic" and SETTINGS.get("scan_after_run", True) and rendered_bases:
        print(f"\nScanning {len(rendered_bases)} chapter(s) for parts worth a listen - "
              f"Whisper, a few minutes")
        settings_path = os.path.join(TEMP_DIR, "qa_settings.json")
        try:
            os.makedirs(TEMP_DIR, exist_ok=True)
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({k: SETTINGS.get(k) for k in
                           ("whisper_exe", "whisper_model", "whisper_language", "device")
                           if SETTINGS.get(k)}, f)
            command = ["uv", "run", "--project", SCRIPT_DIR, "--no-sync", "python", "-u",
                       os.path.join(SCRIPT_DIR, "qa_scan.py"),
                       "--folder", OUTPUT_FOLDER,
                       "--work-root", os.path.join(TEMP_DIR, "qa"),
                       "--settings", settings_path,
                       "--chapters", *rendered_bases]
            code = subprocess.run(command).returncode
            if code != 0:
                print(f"  ! the scan exited with code {code} - the render is unaffected")
        except Exception as e:
            # A finished, correct render must never be reported as failed
            # because the QA pass could not run.
            print(f"  ! the scan could not run ({type(e).__name__}: {e}) - the render is "
                  f"unaffected; open dynamic-repair to scan there")

    if SETTINGS.get("auto_translate_after_run", False):
        backend = SETTINGS.get("translation_backend", "vntl")
        # The chapter decision drives the subtitle decision - one answer to
        # "should existing work be redone", applied to both halves.
        #
        # This is not only about consistency. Regenerating a chapter rewrites
        # its sync.json, and an .srt is timed against that file; keeping the
        # old subtitle would leave cues pointing at audio that has moved.
        # Conversely, skipping a chapter means its audio and its subtitle are
        # both still valid, so re-translating would burn GPU hours to arrive
        # back where it started - and would silently discard any correction
        # made to that .srt by hand.
        cmd = ["uv", "run", "--project", SCRIPT_DIR, "--no-sync", "python", "-u",
               os.path.join(SCRIPT_DIR, "translate_pipeline.py"),
               "--all", "--backend", backend]
        if REGENERATE_EXISTING_CHAPTERS:
            cmd.append("--regenerate")

        print(f"\nGenerating translation subtitles (backend: {backend}"
              + (", regenerating existing" if REGENERATE_EXISTING_CHAPTERS
                 else ", skipping chapters that already have an .srt") + ")...")
        try:
            translate_result = subprocess.run(cmd, cwd=SCRIPT_DIR)
            if translate_result.returncode != 0:
                print(f"Translation failed (exit code {translate_result.returncode}). "
                      f"The audiobook itself is unaffected - subtitles can be "
                      f"regenerated later from the Subtitle Generation Tool.")
        except Exception as e:
            print(f"Translation failed to start: {e}")


if __name__ == "__main__":
    main()
