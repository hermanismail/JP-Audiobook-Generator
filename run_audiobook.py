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
    "model_path": r"C:\Irodori-TTS\model.safetensors",
    "speaker_path": r"C:\Irodori-TTS\seiyuu\ueshama.speaker.safetensors",
    "silence_duration": 1.0,
    "clean_temp_after_run": True,
    "uv_project_dir": r"C:\Irodori-TTS",  # not used by this script directly, kept for the GUI launcher
    "auto_tag_generated_files": False,
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

    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded)
    return merged


SETTINGS = load_settings()

INPUT_FOLDER = SETTINGS["input_folder"]
OUTPUT_FOLDER = SETTINGS["output_folder"]
TEMP_DIR = SETTINGS["temp_dir"]
MODEL_PATH = SETTINGS["model_path"]
SPEAKER_PATH = SETTINGS["speaker_path"]
SILENCE_DURATION = float(SETTINGS["silence_duration"])  # base "1x" unit, in seconds
CLEAN_TEMP_AFTER_RUN = bool(SETTINGS["clean_temp_after_run"])

# Silence-unit count per chunk gap is now computed directly by
# text_pipeline.py (chunk["silence_units"]), combining structural
# boundaries (chapter_start=5/section=3/paragraph=2/sentence=1) with any
# forced-break content tags (bracket edges, "──") via the MAX-based rule
# in text_pipeline.silence_units_for(). See text-cleaning-logic-spec.md
# section 5. No separate lookup table needed here anymore.

# Ensure folders exist
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


def clean_temp_dir():
    """Clears all files in the temporary directory."""
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
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
        for sen_idx, unit in enumerate(units, start=1):
            fname = f"sec{sec_idx:03d}par{par_idx:03d}sen{sen_idx:03d}.txt"
            with open(os.path.join(work_dir, fname), "w", encoding="utf-8") as f:
                f.write(unit["text"])

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


def probe_sample_rate(wav_path, default=48000):
    """Reads the actual sample rate of a generated TTS wav via ffprobe, so
    silence.wav can be generated to match it exactly. A mismatch here (the
    ffmpeg concat demuxer expects every segment to share the same sample
    rate/channels/format) is what caused the 2026-08 "weird sound" bug -
    silence.wav was hardcoded to 24kHz while Irodori-TTS actually outputs
    48kHz, so the concat demuxer misread the timing across the join and
    produced pitch/speed-distorted audio. Falls back to `default` if
    ffprobe fails or the wav can't be read for any reason."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", wav_path],
            capture_output=True, text=True,
        )
        return int(result.stdout.strip())
    except (ValueError, OSError):
        return default


def get_silence_wav(work_dir, sample_rate):
    """Generates (once) a single base silence.wav of SILENCE_DURATION
    seconds, at the given sample_rate - this MUST match the sample rate of
    the actual generated TTS wavs (see probe_sample_rate()) or the ffmpeg
    concat demuxer will distort the stitched audio. Longer gaps (2x/3x/5x)
    are produced by repeating this same file multiple times in the ffmpeg
    concat list, rather than rendering separate longer silence files."""
    silence_wav_path = os.path.join(work_dir, "silence.wav")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", str(SILENCE_DURATION), silence_wav_path
    ], capture_output=True)
    return silence_wav_path


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
    chunks, working_data = text_pipeline.build_chunks(raw_text)

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
    audio_files = []            # list of wav paths, in order
    silence_units_before_wav = []  # silence-unit count preceding each wav

    for i, chunk in enumerate(chunks, start=1):
        key = (chunk["section"], chunk["paragraph"], chunk["chunk"])
        txt_filename = input_paths[key]
        wav_filename = os.path.join(
            work_dir, text_pipeline.chunk_filename(chunk, ext="wav"))

        cmd = [
            "uv", "run", "--no-sync", "python", "infer.py",
            "--checkpoint", MODEL_PATH,
            "--ref-embed", SPEAKER_PATH,
            "--text", chunk["text"],
            "--output-wav", wav_filename
        ]

        print(f" Generating chunk {i}/{len(chunks)} "
              f"(sec {chunk['section']:03d} par {chunk['paragraph']:03d}, "
              f"{len(chunk['text'])} chars, "
              f"{chunk['silence_units']}x silence before "
              f"[{','.join(chunk['boundary_tags'])}])...")
        subprocess.run(cmd, capture_output=True)

        if os.path.exists(wav_filename):
            audio_files.append(wav_filename)
            silence_units_before_wav.append(chunk["silence_units"])

    # Step 5: Combine parts into final MP3, inserting silence sized by
    # chunk["silence_units"] before each chunk (1x sentence, 2x paragraph
    # or chapter start, 3x section, plus content tags for bracket edges/
    # "──" - see text-cleaning-logic-spec.md section 5).
    if not audio_files:
        print(f"Error: No audio parts generated for {chapter_name[0]}")
        return

    output_mp3 = os.path.join(OUTPUT_FOLDER, f"{chapter_name[0]}.mp3")
    tts_sample_rate = probe_sample_rate(audio_files[0])
    print(f"Detected TTS output sample rate: {tts_sample_rate}Hz - generating matching silence.wav...")
    silence_wav_path = get_silence_wav(work_dir, tts_sample_rate)

    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for idx, audio_file in enumerate(audio_files):
            units_count = silence_units_before_wav[idx]
            for _ in range(units_count):
                f.write(f"file '{os.path.abspath(silence_wav_path)}'\n")
            f.write(f"file '{os.path.abspath(audio_file)}'\n")

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-acodec", "libmp3lame",
        "-ac", "2",
        "-b:a", "320k",
        output_mp3
    ]

    print(f"Stitching {chapter_name[0]} into final MP3...")
    subprocess.run(ffmpeg_cmd, capture_output=True)
    print(f"Done! Saved to: {output_mp3}")

    # Step 6: Cleanup temporary files for this chapter (unless disabled in
    # settings.json)
    if CLEAN_TEMP_AFTER_RUN:
        print(f"Cleaning up temporary files in {TEMP_DIR}...")
        clean_temp_dir()
    else:
        print(f"Skipping temp cleanup (clean_temp_after_run is disabled). Files remain in {TEMP_DIR}")


def main():
    # Find all chapter_*.txt files in E:\AUDIOBOOK\chapter
    chapter_files = sorted(glob.glob(os.path.join(INPUT_FOLDER, "chapter_*.txt")))

    if not chapter_files:
        print(f"No files found in {INPUT_FOLDER} matching 'chapter_*.txt'")
        return

    print(f"Found {len(chapter_files)} chapters to process.")

    for chapter_file in chapter_files:
        process_chapter(chapter_file)

    print("\nAll chapters completed successfully!")

    # Auto-tag step: runs the mp3_metadata.py tagger from the GUI project's
    # OWN lightweight uv venv (via `--project`), not this heavy Irodori-TTS
    # venv, so mutagen never needs to be installed here. Only runs when
    # "Auto-tag generated files" is turned on in the Metadata settings tab -
    # otherwise the person applies tags manually afterwards via the GUI's
    # "Apply Tags to Output MP3s" button.
    if SETTINGS.get("auto_tag_generated_files", False):
        print("\nAuto-tag generated files is ON - tagging output MP3s...")
        try:
            tag_result = subprocess.run(
                ["uv", "run", "--project", SCRIPT_DIR, "--no-sync", "python",
                 os.path.join(SCRIPT_DIR, "mp3_metadata.py")],
                cwd=SCRIPT_DIR, capture_output=True, text=True,
            )
            if tag_result.stdout:
                print(tag_result.stdout.strip())
            if tag_result.returncode != 0:
                print(f"Auto-tagging failed (exit code {tag_result.returncode}):")
                print(tag_result.stderr.strip())
        except Exception as e:
            print(f"Auto-tagging failed to start: {e}")


if __name__ == "__main__":
    main()
