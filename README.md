# JP-Audiobook-Generator

This script reads a raw `.txt` file and outputs an audiobook in MP3 format.

# 🎧 Automated Japanese Audiobook Generator

## 1. Project Overview

The Automated Japanese Audiobook Generator is a Python automation pipeline designed to transform Japanese text into high-fidelity audiobooks. The system utilizes the Irodori-TTS engine to facilitate a seamless transition from raw textual data to polished, human-like narration. By automating the end-to-end lifecycle — including sophisticated linguistic pre-processing, sentence-level segmentation, and hardware-accelerated synthesis — this project provides a robust solution for local audiobook production.

## 2. Core Functional Features

- **Text Pre-processing:** The pipeline parses raw Japanese text into a structured hierarchy of sections, paragraphs, and sentences. Dialogue (`「」`) and parenthetical asides (`（）`) are no longer isolated as whole, unsplittable spans - instead each bracket edge is a guaranteed silence point, while the content between/around them is chunked by the same character-count rules as ordinary narration. A mid-sentence `──` is also a forced silence point. See [Section 7](#7-detailed-text-cleaning-logic) for the full logic.
- **Sentence-Level Chunking:** To respect model token limits and prevent prosodic degradation, the script merges sentences into ~100-character chunks (130-character hard limit) using `。`, `？`, `……`, and the forced break points above (`「`, `（`, `」`, `）`, `──`) as boundaries. This keeps intonation natural across long-form content (including long dialogue) while minimizing the number of TTS calls.
- **AI Speech Synthesis:** The system integrates the Irodori-TTS engine, which utilizes a Flow Matching architecture for better voice quality. Local GPU inference is managed via the `uv` package manager to ensure environment stability.
- **Automated Audio Stitching:** Using FFmpeg's concat demuxer, the script merges individual chunk waveforms into a final chapter file, inserting tiered silence gaps (1×/2×/3×, scaled by boundary type - sentence, paragraph/chapter start, or section - plus extra gaps around dialogue/asides and `──` pauses) to simulate natural human pacing.

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

> **2026-08 rewrite note:** this section previously described a search-and-replace pass that converted `」` and `……` into commas before splitting on `。`/`？`. That approach was replaced with the structured section/paragraph/sentence pipeline below (`text_pipeline.py`), which treats dialogue and parenthetical asides as first-class boundaries instead of substituting them away.
>
> **2026-08 v2 update:** the original rewrite isolated every `「」`/`（）` span as one indivisible chunk, regardless of length. In practice some dialogue lines ran to 200+ characters (this author's writing style leans long on dialogue), producing single TTS calls far past the 130-character hard limit. v2 replaces whole-span isolation with **forced break points**: each bracket edge, and each mid-sentence `──`, guarantees a silence gap at that exact point, but the text on either side is chunked by the normal 100/130-character rules just like narration. This fixes the long-dialogue problem while keeping the original guarantee that dialogue/asides always get a silence gap around them.

The text pipeline (`text_pipeline.py`) runs in four stages: it defines sentence/paragraph/section boundaries and forced break points, splits the chapter into working files along those boundaries, merges sentences into TTS-sized input chunks, and finally drives silence insertion when the chunk audio is stitched back together.

### 7.1 Definitions

**Sentence** — text ending at `。`, `？`, or `……` (ellipsis), same as before.

**Forced break points** — four situations where the text is always cut, regardless of the 100/130-character merge rules, and a silence gap is guaranteed at that exact point:

| Trigger | Cut position | Gap tag | Silence weight |
|---|---|---|---|
| `「` or `（` | Immediately **before** the bracket | `bracket_open` | 1× |
| `」` or `）` | Immediately **after** the bracket | `bracket_close` | 1× |
| `──` | At the dash itself (dash is dropped from the TTS text — the model ignores it anyway, so there's no point sending it) | `dash` | 2× |

Unlike the old priority rule, brackets no longer isolate their *entire* contents as one chunk — only the two edges are forced cut points. Everything between an opening and closing bracket (and everything outside brackets) is chunked by the same character-count merge logic described in 7.3, so a long line of dialogue now gets split into several ~100-character chunks internally, just like narration would.

**Paragraph** — a run of sentences that ends with a single CRLF.

**Section** — a run of sentences that ends with more than one CRLF in a row (i.e. a blank line).

**Chapter start** — the very first chunk of every chapter gets its own guaranteed lead-in silence (2×), so consecutive chapters don't run into each other when played back-to-back on a playlist.

**Ordering note:** since paragraph/section boundaries are defined by CRLF patterns, boundary detection happens *before* the CRLF characters are removed — the parser reads the raw file once to mark section/paragraph/sentence boundaries, then strips whitespace/CRLF/IDSP when writing each working file's content.

**IDSP** — the ideographic space character (U+3000, full-width space).

### 7.2 Stage 1 — Parse & split into working files

Working from the original chapter `.txt` file, three passes:

1. **Section split** — find section boundaries (blank-line-separated runs), write each to `sec001.txt`, `sec002.txt`, … up to the last section.
2. **Paragraph split** — within each section, split on single-CRLF paragraph boundaries, write `sec001par001.txt`, `sec001par002.txt`, … (paragraph numbering resets to `001` at the start of each new section).
3. **Sentence split** — within each paragraph, split on sentence boundaries and forced break points, write `sec001par001sen001.txt`, `sec001par001sen002.txt`, … (sentence numbering resets to `001` at the start of each new paragraph).

All spaces, CRLFs, and IDSP are stripped from the content of every working file produced in this stage.

### 7.3 Stage 2 — Merge sentences into TTS input chunks

Goal: instead of one audio generation call per sentence, combine sentences so each TTS input lands close to **100 characters**, never exceeding a **130-character hard limit** where avoidable.

Per paragraph, walk its sentence units in order and maintain a running buffer:

0. **Forced break check first** — if the next unit immediately follows a forced break point (7.1), it never merges backward into whatever's currently buffered; the current buffer (if any) is closed out as its own chunk first, and this unit starts a brand-new buffer. That new buffer still goes through the normal merge rules below for anything added to it afterward — the forced break only guarantees the cut *before* it, not that the resulting chunk stays short.
1. Otherwise, add the next sentence to the buffer; sum its character count into the running total.
2. If the running total is **≤ 100 chars**: keep going — pull in the next sentence (repeating the forced-break check each time) and repeat step 1.
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

When stitching the `.wav` files back together with FFmpeg, silence is inserted before each chunk based on the tags describing the gap immediately before it (7.1). Every gap always has exactly one **structural** tag (mutually exclusive, highest-scoped one wins) and may additionally carry **content** tags from forced break points (additive with each other, but not with the structural tag):

| Structural tag | Weight | | Content tag | Weight |
|---|---|---|---|---|
| `section` (new section) | 3× | | `bracket_open` | 1× |
| `paragraph` (new paragraph) | 2× | | `bracket_close` | 1× |
| `chapter_start` (first chunk of the chapter) | 2× | | `dash` | 2× |
| `sentence` (default, plain within-paragraph gap) | 1× | | | |

**Combination rule:** `silence units = MAX(structural weight, sum of content weights present at that gap)`. In practice this means a forced break point never gets *less* silence than the structural boundary it happens to coincide with, but content tags don't stack on top of an already-larger structural gap either. Worked examples:

| Situation | Result |
|---|---|
| Plain sentence gap, no bracket/dash | 1× |
| Before a `「` mid-paragraph | max(1, 1) = **1×** |
| `」` immediately followed by `「` (no text between) | max(1, 1+1) = **2×** |
| A new paragraph that happens to open with `「` | max(2, 1) = **2×** |
| A `──` cut mid-paragraph | max(1, 2) = **2×** |
| First chunk of the chapter | max(2, 0) = **2×** |

Silence is only inserted at chunk boundaries, not between individual sentences that got merged inside the same chunk (those are spoken as one continuous TTS render, with pacing left to the TTS module).

## 8. Execution and Deployment

**Running from CLI**

You have the option to run via a PowerShell terminal, and can change settings manually by editing `settings.json`. Execute the automation script via the terminal using the execution guard:

```powershell
uv run --no-sync python run_audiobook.py
```
