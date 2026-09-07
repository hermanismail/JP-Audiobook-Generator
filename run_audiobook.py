import os
import json
import subprocess
import glob
import shutil

import text_pipeline

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
    # Per-speaker TTS tuning, set on the GUI's Advanced page. These vary
    # enough between trained speakers that fixing them here produced
    # inconsistent results across voices. Keep in step with
    # gui_settings.DEFAULT_SETTINGS.
    "duration_scale": 1.2,
    "no_trim_tail": True,
    "seed_enabled": False,
    "seed_value": 20260906,
    # Output encoding.
    "mp3_mono": True,
    "mp3_bitrate": "96k",
    # Translation subtitles. See translate_pipeline.py.
    "auto_translate_after_run": False,
    "translation_backend": "vntl",
    "llama_server_url": "http://127.0.0.1:8080",
    # Used to start llama-server on demand when nothing is already listening,
    # so the GPU is only occupied while translation is actually running.
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llama_model_path": r"C:\llama.cpp\models\vntl-llama3-8b-v2-hf-q5_k_m.gguf",
}


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

# ffmpeg output encoding. Mono because that is what Irodori-TTS renders;
# stereo would duplicate the same signal into both channels.
MP3_CHANNELS = "1" if bool(SETTINGS["mp3_mono"]) else "2"
MP3_BITRATE = str(SETTINGS["mp3_bitrate"])

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
    stitched MP3 (matters most for the silence wavs, whose real duration
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
    the final stitched MP3 for free, with no separate alignment pass.
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
    input_paths = write_working_files(working_data, chunks, work_dir)

    # Step 4: Generate audio for each input chunk
    audio_files = []          # list of wav paths, in order
    silence_kind_before_wav = []  # which silence wav precedes each entry
    sync_chunk_texts = []     # chunk["display_text"] (original wording,
                               # not the TTS-normalized text), parallel to
                               # audio_files - only used to build sync.json
                               # (see build_sync_data)

    for i, chunk in enumerate(chunks, start=1):
        key = (chunk["section"], chunk["paragraph"], chunk["chunk"])
        txt_filename = input_paths[key]
        wav_filename = os.path.join(
            work_dir, text_pipeline.chunk_filename(chunk, ext="wav"))

        cmd = [
            "uv", "run", "--no-sync", "python", "infer.py",
            *CHECKPOINT_ARGS,
            "--ref-embed", SPEAKER_PATH,
            "--text", chunk["text"],
            "--output-wav", wav_filename,
            "--duration-scale", str(DURATION_SCALE),
        ]
        if NO_TRIM_TAIL:
            cmd.append("--no-trim-tail")
        if SEED_ENABLED:
            cmd += ["--seed", str(SEED_VALUE)]

        print(f" Generating chunk {i}/{len(chunks)} "
              f"(sec {chunk['section']:03d} par {chunk['paragraph']:03d}, "
              f"{len(chunk['text'])} chars, "
              f"silence_{chunk['silence_kind']} before "
              f"[{','.join(chunk['boundary_tags'])}])...")
        result = subprocess.run(cmd, capture_output=True)

        if os.path.exists(wav_filename):
            audio_files.append(wav_filename)
            silence_kind_before_wav.append(chunk["silence_kind"])
            sync_chunk_texts.append(chunk["display_text"])
        else:
            # Previously this branch just skipped the chunk in silence,
            # which made any systematic infer.py failure (a bad checkpoint
            # argument, a missing speaker file, an out-of-memory GPU) look
            # like "nothing happened" with nothing to debug from. Print what
            # infer.py actually said - the tail, since a traceback's last
            # lines are the informative part.
            stderr = result.stderr.decode("utf-8", "replace").strip()
            print(f"  ! chunk {i} produced no audio (infer.py exit code "
                  f"{result.returncode}):")
            print("    " + (stderr[-600:].replace("\n", "\n    ")
                            if stderr else "(no error output)"))

    # Step 5: Combine parts into final MP3, inserting exactly one silence
    # file before each chunk - silence_sentence.wav, silence_paragraph.wav
    # or silence_section.wav, chosen by chunk["silence_kind"]. Each has its
    # own duration from the GUI's Advanced page, so long gaps are a single
    # correctly-sized file rather than the same 1x file repeated 2x/3x as
    # in earlier versions.
    if not audio_files:
        print(f"Error: No audio parts generated for {chapter_name[0]}")
        return

    output_mp3 = os.path.join(OUTPUT_FOLDER, f"{chapter_name[0]}.mp3")

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

    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for idx, audio_file in enumerate(audio_files):
            kind = silence_kind_before_wav[idx]
            f.write(f"file '{silence_wavs[kind]}'\n")
            f.write(f"file '{os.path.abspath(audio_file)}'\n")

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-acodec", "libmp3lame",
        "-ac", MP3_CHANNELS,
        "-b:a", MP3_BITRATE,
        output_mp3
    ]

    print(f"Stitching {chapter_name[0]} into final MP3...")
    subprocess.run(ffmpeg_cmd, capture_output=True)
    print(f"Done! Saved to: {output_mp3}")

    # Step 5b: Build and write sync.json - chunk start/end offsets (in
    # seconds) into the just-stitched MP3, for the Android player app (see
    # android-player-phase0-spec.md). MUST run before Step 6's cleanup:
    # build_sync_data() needs ffprobe access to the individual per-chunk
    # wav files, which clean_temp_dir() deletes right afterwards.
    sync_data = build_sync_data(audio_files, sync_chunk_texts, silence_kind_before_wav, silence_wavs)
    sync_path = os.path.join(OUTPUT_FOLDER, f"{chapter_name[0]}.sync.json")
    with open(sync_path, "w", encoding="utf-8") as f:
        json.dump(sync_data, f, ensure_ascii=False, indent=2)
    print(f"Sync data saved to: {sync_path}")

    # Step 6: Auto-tag step - runs the mp3_metadata.py tagger from the GUI
    # project's OWN lightweight uv venv (via `--project`), not this heavy
    # Irodori-TTS venv, so mutagen never needs to be installed here. Runs
    # right away, per chapter, so the file is fully usable (correct
    # metadata/album art for Spotify/phone) the moment it lands in the
    # output folder - no need to wait for the rest of the book to finish
    # before copying chapters over. Only runs when "Auto-tag generated
    # files" is turned on in the Metadata settings tab - otherwise the
    # person applies tags manually afterwards via the GUI's "Apply Tags to
    # Output MP3s" button.
    if SETTINGS.get("auto_tag_generated_files", False):
        print(f"Auto-tagging {chapter_name[0]}.mp3...")
        try:
            tag_result = subprocess.run(
                ["uv", "run", "--project", SCRIPT_DIR, "--no-sync", "python",
                 os.path.join(SCRIPT_DIR, "mp3_metadata.py"),
                 "--chapter", chapter_name[0]],
                cwd=SCRIPT_DIR, capture_output=True, text=True,
            )
            if tag_result.stdout:
                print(tag_result.stdout.strip())
            if tag_result.returncode != 0:
                print(f"Auto-tagging failed (exit code {tag_result.returncode}):")
                print(tag_result.stderr.strip())
        except Exception as e:
            print(f"Auto-tagging failed to start: {e}")

    # Step 7: Cleanup temporary files for this chapter (unless disabled in
    # settings.json)
    if CLEAN_TEMP_AFTER_RUN:
        print(f"Cleaning up temporary files in {TEMP_DIR}...")
        clean_temp_dir()
    else:
        print(f"Skipping temp cleanup (clean_temp_after_run is disabled). Files remain in {TEMP_DIR}")


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

    print(f"Found {len(chapter_files)} chapters to process.")

    for chapter_file in chapter_files:
        process_chapter(chapter_file)

    print("\nAll chapters completed successfully!")

    # The shared silence wavs were kept alive across chapters by
    # clean_temp_dir()'s skip; now that the run is over they can go too.
    if CLEAN_TEMP_AFTER_RUN and os.path.isdir(SILENCE_DIR):
        shutil.rmtree(SILENCE_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
