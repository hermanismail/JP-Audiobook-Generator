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
    print(f"\n>>> Processing: {chapter_name[0]}")
    
    # Step 1: Read Raw Text
    with open(chapter_path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    # --- Custom Symbol Cleaning Logic ---
    # 1. Replace …… with Japanese comma (、)
    cleaned_text = raw_text.replace("……", "、")
    
    # 2. Replace 」 with Japanese comma (、)
    cleaned_text = cleaned_text.replace("」", "、")
    
    # 3. Handle specific overlapping patterns where a quote follows an ellipsis or question mark:
    # Rule 4: If character after …… is 」, remove the redundant 」 (handled above by replacement, but we can clean up double commas if needed)
    # Rule 5: When character after ？ 」? (or ？ followed by unnecessary closing bracket), clean up trailing brackets.
    # Let's target specific trailing garbage patterns like "、」" or "？、" resulting from replacements:
    cleaned_text = cleaned_text.replace("、、", "、")
    cleaned_text = re.sub(r"？\s*、", "？", cleaned_text)

    # Retain only words, Japanese characters, spaces, and allowed punctuation (、, 。, ？)
    cleaned_text = re.sub(r"[^\w\u3040-\u30ff\u4e03-\u9faf、。？\s]", "", cleaned_text)
    cleaned_text = re.sub(r"\s+", "", cleaned_text)
    
    # Step 2: Split into sentences (keeping the period or question mark as sentence boundaries)
    # Splitting by 。 or ？ while retaining them
    raw_sentences = re.split(r"([。？])", cleaned_text)
    sentences = []
    for i in range(0, len(raw_sentences) - 1, 2):
        sentence = raw_sentences[i] + raw_sentences[i+1]
        if sentence.strip():
            sentences.append(sentence)
            
    # Fallback if text doesn't end with standard punctuation
    if len(raw_sentences) % 2 != 0 and raw_sentences[-1].strip():
        sentences.append(raw_sentences[-1])

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

    # Step 5: Combine parts into final MP3 with silence gaps using text list demuxer
    if not audio_files:
        print(f"Error: No audio parts generated for {chapter_name[0]}")
        return

    output_mp3 = os.path.join(OUTPUT_FOLDER, f"{chapter_name[0]}.mp3")
    
    # Generate temporary silence file matching SILENCE_DURATION
    silence_wav_path = os.path.join(TEMP_DIR, "silence.wav")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", str(SILENCE_DURATION), silence_wav_path
    ], capture_output=True)

    # Write concat list file to bypass Windows command-line character length limits (WinError 206)
    concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for idx, audio_file in enumerate(audio_files):
            f.write(f"file '{os.path.abspath(audio_file)}'\n")
            if idx < len(audio_files) - 1:
                f.write(f"file '{os.path.abspath(silence_wav_path)}'\n")

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