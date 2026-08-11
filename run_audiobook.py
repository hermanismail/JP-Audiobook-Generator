import os
import re
import subprocess
import glob
import shutil

# --- Configuration ---
# Paths for your environment
INPUT_FOLDER = r"E:\AUDIOBOOK\chapter"
OUTPUT_FOLDER = r"E:\AUDIOBOOK\output"
TEMP_DIR = r"D:\AUDIOBOOK_TMP"

# AI Model Configuration (Using your C: drive local path)
MODEL_PATH = r"C:\Irodori-TTS\model.safetensors"
# Replace with your actual trained speaker path
SPEAKER_PATH = r"C:\Irodori-TTS\seiyuu\ueshama.speaker.safetensors"

# Narration settings
SILENCE_DURATION = 1.0  # Pause in seconds between sentences

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

def process_chapter(chapter_path):
    chapter_name = os.path.splitext(os.path.basename(chapter_path))
    print(f"\n>>> Processing: {chapter_name}")
    
    # Step 1: Read and Clean Text
    with open(chapter_path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    # Retain only words and Japanese punctuation (、 and 。)
    cleaned_text = re.sub(r"[^\w\u3040-\u30ff\u4e03-\u9faf、。\s]", "", raw_text)
    cleaned_text = re.sub(r"\s+", "", cleaned_text)
    
    # Step 2: Split into sentences (keeping the period)
    raw_sentences = cleaned_text.split("。")
    sentences = [s + "。" for s in raw_sentences if s.strip()]
    print(f"Found {len(sentences)} sentences.")

    audio_files = []

    # Step 3: Generate Audio for each sentence
    for i, sentence in enumerate(sentences, start=1):
        txt_filename = os.path.join(TEMP_DIR, f"input_{i:04d}.txt")
        wav_filename = os.path.join(TEMP_DIR, f"parts_{i:04d}.wav")
        
        with open(txt_filename, "w", encoding="utf-8") as out_f:
            out_f.write(sentence)
        
        # Run Irodori-TTS Inference via uv
        # Note: We use --no-sync to keep the RTX 4060 active
        cmd = [
            "uv", "run", "--no-sync", "python", "infer.py",
            "--checkpoint", MODEL_PATH,
            "--ref-embed", SPEAKER_PATH,
            "--text", sentence,
            "--output-wav", wav_filename
        ]
        
        print(f" Generating part {i}/{len(sentences)}...")
        subprocess.run(cmd, capture_output=True)
        
        if os.path.exists(wav_filename):
            audio_files.append(wav_filename)

    # Step 5: Combine parts into final MP3 with silence gaps
    if not audio_files:
        print(f"Error: No audio parts generated for {chapter_name}")
        return

    output_mp3 = os.path.join(OUTPUT_FOLDER, f"{chapter_name[0]}.mp3")
    
    # Build FFmpeg filter complex for silence gaps
    # [0:a]apad=pad_dur=0.4[a0]; [1:a]apad=pad_dur=0.4[a1]... concat=n=X:v=0:a=1
    filter_complex = ""
    for idx in range(len(audio_files)):
        filter_complex += f"[{idx}:a]apad=pad_dur={SILENCE_DURATION}[a{idx}];"
    
    for idx in range(len(audio_files)):
        filter_complex += f"[a{idx}]"
    
    filter_complex += f"concat=n={len(audio_files)}:v=0:a=1[outa]"

    ffmpeg_cmd = ["ffmpeg", "-y"]
    for f in audio_files:
        ffmpeg_cmd.extend(["-i", f])
    
    ffmpeg_cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[outa]",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_mp3
    ])

    print(f"Stitching {chapter_name} into final MP3...")
    subprocess.run(ffmpeg_cmd, capture_output=True)
    print(f"Done! Saved to: {output_mp3}")

    # Step 5: Cleanup temporary files for this chapter
    print(f"Cleaning up temporary files in {TEMP_DIR}...")
    clean_temp_dir()

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

if __name__ == "__main__":
    main()