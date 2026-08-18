# JP-Audiobook-Generator
This script reads a raw txt file and output as an audio file in mp3 format.
<h1>🎧 Automated Japanese Audiobook Generator</h1>
<h2>1. Project Overview</h2>
The Automated Japanese Audiobook Generator is a Python automation pipeline designed to transform Japanese text into high-fidelity audiobooks. The system utilizes the Irodori-TTS engine to facilitate a seamless transition from raw textual data to polished, human-like narration. By automating the end-to-end lifecycle—including sophisticated linguistic pre-processing, sentence-level segmentation, and hardware-accelerated synthesis—this project provides a robust solution for local audiobook production.<br><br>
<h2>2. Core Functional Features</h2>
<b>Text Pre-processing: </b>The pipeline employs a sequential search-and-replace logic tailored for Japanese typography. It converts dialogue brackets (」) and ellipses (……) into conversational pauses (、) while preserving interrogative markers (？) to ensure the AI maintains correct tonal inflection.<br><br>
<b>Sentence-Level Chunking:</b> To respect model token limits and prevent prosodic degradation, the script segments text into manageable chunks using 。 and ？ as hard delimiters. This ensures high-quality intonation across long-form content.<br><br>
<b>AI Speech Synthesis:</b> The system integrates the Irodori-TTS engine, which utilizes a Flow Matching architecture for better voice quality. Local GPU inference is managed via the uv package manager to ensure environment stability.<br><br>
<b>Automated Audio Stitching:</b> Using FFmpeg’s filter_complex and the apad filter, the script merges individual sentence waveforms into a final chapter file. This process injects natural, configurable silence gaps to simulate human breathing and pacing.<br><br>
<h2>3. System Prerequisites</h2><br>
<b>System Requirements</b><br>
Operating System : Windows 11<br>
GPU : NVIDIA GeForce RTX 4060 (8GB VRAM minimum)<br>
Tools : FFmpeg (Full-shared version): Required for libtorchcodec DLL support.<br>
 uv: Modern Python package manager.<br>
Engine : Irodori-TTS (Cloned repository)<br>
Model Weights : model.safetensors (v4-Small recommended) and trained .speaker.safetensors (Semantic-DACVAE codec).<br><br>
YOU NEED TO PREPARE THE TRAINING MANIFEST AND PERFORM SPEAKER INVERSION <br>
Refer documentation on Irodori page.<br>
You need to convert your sample wav first via Training Manifest step:<br>
https://github.com/Aratako/Irodori-TTS#1-prepare-the-training-manifest<br>
Then you can proceed with Training your speaker using the output of the Training Manifest:<br>
https://github.com/Aratako/Irodori-TTS#2-train-v4-small<br>
https://github.com/Aratako/Irodori-TTS#4-speaker-inversion<br><br>

<h2>4. Environment Setup and GPU Verification</h2>
Follow these steps to initialize the hardware-accelerated environment:<br>
<b>Environment Synchronization: </b>Execute the following command in the project root to install dependencies with CUDA 12.8 support:<br>
uv sync --extra cu128<br><br>
<b>Hardware Verification: </b>Run the following command to confirm the RTX 4060 is correctly mapped to PyTorch:<br>
uv run python -c "import torch; print('GPU Available:', torch.cuda.is_available()); print('Device Name:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'None')"<br><br>


<h2>5. Script Workflow Diagram</h2>
The following diagram visualizes the data path from ingestion to the final output:<br>
<img src="JP-Audiobook-Generator-Flow-Diagram.png" alt="Script Workflow Diagram" width="600">

<h2>6. User Configuration Guide</h2>
<b>Settings GUI (Recommended)</b><br>
A GUI (gui_settings.py) is now available so you no longer need to hand-edit run_audiobook.py for routine changes. Once set up, you can open it directly from the Windows taskbar:<br>
1. Run Create-Shortcut.ps1 once to create a desktop shortcut pointing to Launch-Settings-Silent.vbs.<br>
2. Pin that shortcut to the taskbar (right-click it → Pin to taskbar).<br>
3. Click the taskbar icon anytime to open the settings window, change values, and run the program without touching the terminal.<br><br>
Below is a look at the settings interface:<br>
<img src="GUI-General.png" alt="Settings GUI General" width="600"><br>
<img src="GUI-Metadata.png" alt="Settings GUI Metadata" width="600"><br>
<img src="GUI-Advanced.png" alt="Settings GUI Advanced" width="600"><br>
<br>
Users must configure the absolute paths in the run_audiobook.py script.<br>
Note on Raw Strings: When defining Windows paths, you must use the r prefix (e.g., r"C:\Path"). This creates a "Raw String," preventing Python from interpreting backslashes as escape characters, a common cause of execution failure on Windows.<br>
--- Configuration ---<br>
Absolute paths for your environment (Modify for your local drive: C, D, or E)<br>
INPUT_FOLDER = r"E:\AUDIOBOOK\chapter"          # Source for chapter_*.txt<br>
OUTPUT_FOLDER = r"E:\AUDIOBOOK\output"        # Destination for final MP3s<br>
TEMP_DIR = r"D:\AUDIOBOOK_TMP"                # Working directory for audio chunks<br><br>

<b>AI Model Configuration</b><br>
MODEL_PATH = r"C:\Irodori-TTS\model.safetensors"<br>
**Please use your SPEAKER INVERSION result<br>
SPEAKER_PATH = r"outputs/speaker_inversion/name/checkpoint_final.speaker.safetensors"<br>

<b>Narration settings</b><br>
SILENCE_DURATION = 1.0  # Breath/pause (seconds) between sentences<br><br>

<h2>7. Detailed Text Cleaning Logic</h2>
To optimize text for the TTS engine, the script applies six sequential rules before final regex stripping. These rules must be executed in order to handle overlapping punctuation patterns:<br>
Ellipsis Conversion: Replace …… with a Japanese comma (、).<br>
Dialogue Bracket Conversion: Replace 」 with a Japanese comma (、).<br>
Double Comma Fix: Replace any resulting 、、 with a single 、 (occurs when a bracket followed an ellipsis).<br>
Interrogative Preservation: Explicitly retain ？ to guide the model's pitch-accent predictor.<br>
Trailing Bracket Cleanup: Use regex (？\s*、 -> ？) to remove redundant commas following question marks.<br>
Character Filtering: Strip all symbols except Japanese alphanumeric characters, 、, 。, and ？.<br>
Following these steps, the script uses re.split(r"([。？])", text) to ensure 。 and ？ act as hard boundaries for audio chunking.<br><br>
<h2>8. Execution and Deployment</h2>
<b>Running the Pipeline</b><br>
Place your chapter files (e.g., chapter_01.txt) into the INPUT_FOLDER.<br>
Execute the automation script via the terminal using the execution guard:<br>
uv run --no-sync python run_audiobook.py


