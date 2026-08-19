# JP-Audiobook-Generator

This script reads a raw `.txt` file and outputs an audiobook in MP3 format.

# 🎧 Automated Japanese Audiobook Generator

## 1. Project Overview

The Automated Japanese Audiobook Generator is a Python automation pipeline designed to transform Japanese text into high-fidelity audiobooks. The system utilizes the Irodori-TTS engine to facilitate a seamless transition from raw textual data to polished, human-like narration. By automating the end-to-end lifecycle — including sophisticated linguistic pre-processing, sentence-level segmentation, and hardware-accelerated synthesis — this project provides a robust solution for local audiobook production.

## 2. Core Functional Features

- **Text Pre-processing:** The pipeline parses raw Japanese text into a structured hierarchy of sections, paragraphs, and sentences, isolating dialogue (`「」`) and parenthetical asides (`（）`) so they always get their own audio chunk and their own silence gap. See [Section 7](#7-detailed-text-cleaning-logic) for the full logic.
- **Sentence-Level Chunking:** To respect model token limits and prevent prosodic degradation, the script merges sentences into ~100-character chunks (130-character hard limit) using `。`, `？`, `……`, `「」`, and `（）` as boundaries. This keeps intonation natural across long-form content while minimizing the number of TTS calls.
- **AI Speech Synthesis:** The system integrates the Irodori-TTS engine, which utilizes a Flow Matching architecture for better voice quality. Local GPU inference is managed via the `uv` package manager to ensure environment stability.
- **Automated Audio Stitching:** Using FFmpeg's concat demuxer, the script merges individual chunk waveforms into a final chapter file, inserting tiered silence gaps (1×/2×/3×, scaled by whether the gap is a sentence, paragraph, or section boundary) to simulate natural human pacing.

## 3. System Prerequisites

**System Requirements**

| Requirement | Details |
|---|---|
| Operating System | Windows 11 |
| GPU | NVIDIA GeForce RTX 4060 (8GB VRAM minimum) |
| Tools | FFmpeg (full-shared build — required for `libtorchcodec` DLL support), `uv` (modern Python package manager) |
| Engine | Irodori-TTS (cloned repository) |
| Model Weights | `model.safetensors` (v4-Small recommended) and a trained `.speaker.safetensors` (Semantic-DACVAE codec) |

**Speaker setup**

You need to prepare the training manifest and perform speaker inversion — refer to the Irodori-TTS documentation. Convert your sample WAV first via the training manifest step, then train your speaker using that output:

1. [Prepare the training manifest](https://github.com/Aratako/Irodori-TTS#1-prepare-the-training-manifest)
2. [Train v4-Small](https://github.com/Aratako/Irodori-TTS#2-train-v4-small)
3. [Speaker inversion](https://github.com/Aratako/Irodori-TTS#4-speaker-inversion)

## 4. Environment Setup and GPU Verification

Follow these steps to initialize the hardware-accelerated environment:

**Environment synchronization** — install dependencies with CUDA 12.8 support from the project root:
```powershell
uv sync --extra cu128
```

**Hardware verification** — confirm the RTX 4060 is correctly mapped to PyTorch:
```powershell
uv run python -c "import torch; print('GPU Available:', torch.cuda.is_available()); print('Device Name:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'None')"
```

## 5. Script Workflow Diagram

The following diagram visualizes the data path from ingestion to the final output:

<img src="JP-Audiobook-Generator-Flow-Diagram.png" alt="Script Workflow Diagram" width="600">

## 6. User Configuration Guide

**Settings GUI**

A GUI (`gui_settings.py`) is available so you no longer need to hand-edit `run_audiobook.py` for routine changes. Once set up, you can open it directly from the Windows taskbar:

1. Run `Create-Shortcut.ps1` once to create a desktop shortcut pointing to `Launch-Settings-Silent.vbs`.
2. Pin that shortcut to the taskbar (right-click it → *Pin to taskbar*).
3. Click the taskbar icon anytime to open the settings window, change values, and run the program without touching the terminal.

Below is a look at the settings interface:

<img src="GUI-General.png" alt="Settings GUI General" width="600">
<img src="GUI-Metadata.png" alt="Settings GUI Metadata" width="600">
<img src="GUI-Advanced.png" alt="Settings GUI Advanced" width="600">

## 7. Detailed Text Cleaning Logic

> **2026-08 rewrite note:** this section previously described a search-and-replace pass that converted `」` and `……` into commas before splitting on `。`/`？`. That approach was replaced with the structured section/paragraph/sentence pipeline below (`text_pipeline.py`), which treats dialogue and parenthetical asides as first-class boundaries instead of substituting them away. This keeps dialogue and asides intact in the output text and gives them a guaranteed silence gap, rather than flattening them into a generic pause.

The text pipeline (`text_pipeline.py`) runs in four stages: it defines sentence/paragraph/section boundaries, splits the chapter into working files along those boundaries, merges sentences into TTS-sized input chunks, and finally drives silence insertion when the chunk audio is stitched back together.

### 7.1 Definitions

**Sentence** — text found in any of these spans:
1. Text ending at `。`, `？`, or `……` (ellipsis)
2. Text between `「` and `」`
3. Text between `（` and `）`

**Dialogue/aside priority rule:** `「」` and `（）` spans both take priority over the plain `。`/`？`/`……` rule. Whenever either is found, it is cut out as its own sentence, regardless of where it sits relative to a `。`-terminated sentence around it — if a `。`-sentence contains a `「...」` quote or a `（...）` aside inside it, that span is split off as its own separate sentence unit, and the surrounding narration text (before/after) becomes its own separate sentence(s) too. This also means a `「」` or `（）` sentence is never merged with neighboring sentences when building TTS input chunks — each always becomes its own input chunk, so each always gets a silence gap around it.

**Paragraph** — a run of sentences that ends with a single CRLF.

**Section** — a run of sentences that ends with more than one CRLF in a row (i.e. a blank line).

**Ordering note:** since paragraph/section boundaries are defined by CRLF patterns, boundary detection happens *before* the CRLF characters are removed — the parser reads the raw file once to mark section/paragraph/sentence boundaries, then strips whitespace/CRLF/IDSP when writing each working file's content.

**IDSP** — the ideographic space character (U+3000, full-width space).

### 7.2 Stage 1 — Parse & split into working files

Working from the original chapter `.txt` file, three passes:

1. **Section split** — find section boundaries (blank-line-separated runs), write each to `sec001.txt`, `sec002.txt`, … up to the last section.
2. **Paragraph split** — within each section, split on single-CRLF paragraph boundaries, write `sec001par001.txt`, `sec001par002.txt`, … (paragraph numbering resets to `001` at the start of each new section).
3. **Sentence split** — within each paragraph, split on sentence boundaries, write `sec001par001sen001.txt`, `sec001par001sen002.txt`, … (sentence numbering resets to `001` at the start of each new paragraph).

All spaces, CRLFs, and IDSP are stripped from the content of every working file produced in this stage.

### 7.3 Stage 2 — Merge sentences into TTS input chunks

Goal: instead of one audio generation call per sentence, combine sentences so each TTS input lands close to **100 characters**, never exceeding a **130-character hard limit** where avoidable.

Per paragraph, walk its sentence files in order and maintain a running buffer:

0. **Dialogue/aside check first** — if the next sentence is a `「」` or `（）` span, it does not enter the merge buffer at all; it's written out immediately as its own standalone input chunk (whatever's currently in the buffer, if anything, is closed out first as its own chunk too). This guarantees every dialogue line and parenthetical aside gets its own silence gap on both sides.
1. Otherwise, add the next sentence to the buffer; sum its character count into the running total.
2. If the running total is **≤ 100 chars**: keep going — pull in the next sentence (repeating the dialogue check each time) and repeat step 1.
3. If the running total lands **between 100 and 130 chars**: stop here, close the chunk, save it, start a new empty buffer for the next chunk.
4. If the running total **exceeds 130 chars** (hard limit): look inside the sentence that just pushed it over 130 for a `、` (comma) split point — "whichever sentence just caused the overflow," not necessarily literally the second sentence in the buffer.
   - If a `、` is found: recalculate the total using only the portion of that sentence up to the `、`. Close the chunk with that partial sentence included. The remainder (after the `、`) becomes the start of the next chunk's buffer.
   - If no usable `、` is found (or the comma-split portion is still over 130 chars): let it exceed the 130-character hard limit and close the chunk as-is. The TTS module will still complete the sentence — it just renders the speech slightly faster than normal. This is preferred over cutting a sentence off mid-way.

Repeat until every sentence in every paragraph has been consumed.

**Output naming** (chunk numbering resets to `001` at the start of each new paragraph):
```
sec001par001input001.txt
sec001par001input002.txt
...
sec001par001input00x.txt   (last chunk of paragraph 1)
sec001par002input001.txt
...
sec00Xpar00Ninput00x.txt   (last chunk overall)
```

### 7.4 Stage 3 — TTS generation

Each `...input00x.txt` goes through the TTS pipeline and produces a matching `...input00x.wav`. Each input file represents a merged group of sentences rather than a single sentence.

### 7.5 Stage 4 — Concatenation & silence insertion

When stitching the `.wav` files back together with FFmpeg, silence is inserted based on the boundary type between consecutive chunks:

| Boundary between chunks | Silence inserted |
|---|---|
| Chunk boundary within the same paragraph | 1× silence unit |
| Between paragraphs | 2× silence units |
| Between sections | 3× silence units |

Silence is only inserted at chunk boundaries, not between individual sentences that got merged inside the same chunk (those are spoken as one continuous TTS render, with pacing left to the TTS module). So in practice: between chunk *N* and chunk *N+1*, use 2× if that boundary is also a paragraph boundary, 3× if it's also a section boundary, otherwise 1×. Since every `「」` and `（）` sentence is always its own standalone chunk, this also means dialogue lines and parenthetical asides automatically get a silence gap before and after them.

## 8. Execution and Deployment

**Running from CLI**

You have the option to run via a PowerShell terminal, and can change settings manually by editing `settings.json`. Execute the automation script via the terminal using the execution guard:

```powershell
uv run --no-sync python run_audiobook.py
```
