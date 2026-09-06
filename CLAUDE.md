# CLAUDE.md — JP Audiobook Generator

Context for Claude Code sessions in this repo. Written 2026-09-06 from a
session in the **player** repo (`F:\JPAudiobookPlayer`) that studied this
pipeline in order to design a translation-subtitle feature. The
"Translation feature" section below is a **spec that has not been built
yet** — everything above it describes code that exists today.

## What this project is

A Windows desktop tool (Python + CustomTkinter) that turns Japanese
chapter text files into a chaptered audiobook: cleaned/chunked text →
Irodori-TTS → per-chunk wavs → stitched MP3 + `sync.json` timing data +
ID3 tags. Its output folder is the input to the separate **player**
project (`F:\JPAudiobookPlayer` — Android app + Node server/web client;
that repo has its own detailed CLAUDE.md).

**Two-venv architecture**: a lightweight GUI venv for this project, and a
separate heavy ML venv at `C:\Irodori-TTS` for the TTS engine.
`run_audiobook.py` runs inside the Irodori venv and is stdlib-only where
it can be; it shells out to `mp3_metadata.py` with `uv run --project
<this dir>` so `mutagen` never needs installing in the ML venv. Keep that
separation — it is deliberate.

## Layout

- `run_audiobook.py` — the pipeline driver. `process_chapter()` is the
  spine: read raw text → `text_pipeline.build_chunks()` → write working
  files → one TTS call per chunk → ffmpeg concat with silence wavs →
  **step 5b** write `sync.json` → **step 6** optional auto-tag.
  - `build_sync_data()` is the load-bearing piece for anything
    downstream: it walks the *same* concat ordering used to stitch the
    MP3, summing `ffprobe`'d durations, so each chunk's `start`/`end` in
    the final file falls out with no separate alignment pass. Output is
    `{version, chunks:[{index, start, end, text}]}` written to
    `<chapter>.sync.json`. It must run **before** the temp-dir cleanup —
    it needs the per-chunk wavs still on disk.
  - `get_silence_wavs()` renders three silence wavs once per run, matched
    to the TTS output's exact sample rate / channels / sample format —
    the concat demuxer does no resampling, so drift here corrupts audio.
- `text_pipeline.py` — text cleaning and chunking, stdlib only. Sections
  (blank-line split) → paragraphs → sentences (。？……) → merged chunks at
  a soft/hard char limit. Each chunk carries **two** texts:
  - `text` — TTS-normalized (brackets stripped, ── → 、, punctuation runs
    collapsed). What goes to the TTS engine.
  - `display_text` — original wording. **This is what lands in
    `sync.json` and what the reader app displays.** Do not conflate them.
- `mp3_metadata.py` — ID3 tagging (mutagen). Has a `--chapter <base>` CLI
  and re-reads `settings.json` itself. **This is the pattern to copy for
  any new per-chapter post-processing step** (see Translation below).
- `gui_settings.py` — CustomTkinter settings GUI; `DEFAULT_SETTINGS` near
  the top mirrors `run_audiobook.py`'s own defaults dict — both must be
  updated together when adding a setting. Also hosts the standalone
  "Apply Tags to Output MP3s" button.
- `progress_window.py`, `ui_common.py` — progress UI and shared widgets.
  *(Not read in detail during the session that wrote this file.)*

## Conventions worth not breaking

- Hand-rolled `json` building; no serialization library.
- `json.dump(..., ensure_ascii=False)` — the data is Japanese.
- A new setting means three edits: `DEFAULT_SETTINGS` in
  `gui_settings.py`, the defaults dict in `run_audiobook.py`, and the
  save handler that writes `settings.json`.
- Post-processing tools are separate scripts with their own `--chapter`
  CLI that re-read `settings.json`, invoked via `uv run --project`, and
  gated behind a boolean setting for the automatic path. Both the
  automatic and manual paths call the same code.

---

# Translation feature — SPEC, NOT YET BUILT

Full rationale, cost math, research citations and the alternatives that
were rejected live in `F:\JPAudiobookPlayer\translation-pipeline-notes-rev2.pdf`.
This is the buildable summary. **Build on a separate branch; the user
controls branching and commits.**

## Goal

Generate an English translation subtitle for each chapter, so the player
can show it alongside the Japanese reading text.

## The decision, and why

The player already reads an optional `<chapter_base>.srt` sitting next to
the audio (`GET /api/books/:id/chapters/:base/subtitle`; missing file =
404 = no-op). Today those are made by hand outside the pipeline.

**Approach: translate the finished `sync.json` chunk-by-chunk, emit a
separate `.srt`.** Do *not* merge English into `sync.json`.

- Translating *after* audio exists means chunk boundaries are already
  fixed by the rendered MP3 — the translation can never desync. Timing is
  correct by construction, not by alignment.
- Emitting a sidecar `.srt` keeps the change 100% additive: the karaoke
  engine, the Kotlin JS bridge, and the two separate copies of the player
  HTML all keep their current data contract.
- Rejected: translating first and chunking both languages together
  (assumes a 1:1 JA→EN segment mapping that Japanese word order makes
  fictional), and anything using Whisper (it is ASR — it would discard
  the exact timings this pipeline already computes).

**The invariant that makes it all work: the model only ever fills in text
per existing chunk index. It never merges, splits, or re-times.** Every
mature open-source subtitle translator converges on this rule.

## Shape

A new `translate_pipeline.py`, mirroring `mp3_metadata.py`:

| | Auto-tagging (exists) | Translation (build this) |
|---|---|---|
| Standalone | GUI "Apply Tags to Output MP3s" → `mp3_metadata.py --chapter <base>` | GUI "Generate Translation Subtitles" → `translate_pipeline.py --chapter <base>` \| `--all` |
| Automatic | `auto_tag_generated_files` setting; runs **per chapter** inside `process_chapter()` | `auto_translate_after_run` setting; runs **once at the very end of the whole run** |

**The end-of-run placement is deliberate, for two independent reasons:**
1. VRAM — Irodori-TTS and a local translation model would contend for the
   same 8GB card if translation ran between chapters.
2. The book-level glossary is better built in one pass over a finished
   book than accumulated chapter by chapter.

The automatic path is just the standalone path invoked with `--all`. That
also makes back-filling already-generated books free, with no TTS re-run.

## Per chapter

```
read chapter_NNN.sync.json
  → batch its chunks (with rolling context + book glossary)
  → LLM
  → validate: same count, same indices, or retry
  → write chapter_NNN.translation.json   (the artifact / source of truth)
  → write chapter_NNN.srt                (cheap re-emission for the player)
```

Rules:
- **Indices are sacred.** Validate length and index alignment on every
  response; reject and retry on mismatch. This is what keeps timing right.
- **Context, not isolation.** Line-by-line translation is the single
  biggest quality killer; always send neighbouring chunks as read-only
  context.
- **Book-level glossary**, persisted to disk, so chapter 15 doesn't
  rename a character introduced in chapter 2. Hand-editable — that's the
  point of persisting it.
- **Never touch `sync.json`.** English does not go near its `text` field.
- SRT: UTF-8, no BOM, `HH:MM:SS,mmm`.
- A chunk can be a sentence fragment (the hard char limit may split on
  「、」), so context matters for translating those sensibly.

## Model — local first

The user is **not spending on API translation for now**. Machine: RTX
4060, **8GB VRAM**, 16GB RAM, no local LLM runtime installed yet.

Put the LLM call behind **one adapter function** so local-now /
API-later is a one-line swap. The seam must be wide enough for two
genuinely different call shapes:

- **Qwen3 8B/14B (Q4) — the default.** General instruction-following
  model, strong at Japanese. Can honour the JSON-batch + index contract.
- **VNTL-Llama3-8B-v2 — a per-book option.** A QLoRA fine-tune of Llama 3
  Youko trained only on JA→EN visual-novel translation. You do not
  instruct it; you complete its format: a metadata block (character name,
  aliases, **gender** — a trained-in slot that addresses Japanese's
  dropped pronouns) then alternating `[Japanese]`/`[English]` sections.
  Its metadata block is a native glossary mechanism and its alternating
  history is a native rolling context window — both match this design.
  **But it does no JSON and no instruction-following**, so it must be
  driven one chunk at a time with alignment taken from the loop. Good fit
  for 幼女戦記; off-distribution for the Murakami titles.
- **Skip `nllb-jaen-1.3B-lightnovels`** despite the promising name — a
  seq2seq NMT model with a 128-token cap and no glossary/context ability.

Tuning notes:
- **Never use a repetition penalty for translation** (any model). It
  penalizes legitimately recurring tokens — character names, particles —
  and pushes the model into renaming or omitting them. VNTL specifically
  wants temp 0, no repetition penalty.
- Local context is ~8-16k usable at 8B/Q4, so the whole chapter does
  **not** fit. Use a rolling window of ~10-20 previous chunks plus the
  glossary (~2-3k tokens), not the full chapter.
- **Batch small (5-10 chunks), not 50.** An 8B drifts on long indexed
  lists. If it still drifts, go one chunk at a time — this is an offline
  batch job, so slow is affordable (TTS already takes hours).
- `llama-server`/Ollama expose an OpenAI-compatible HTTP endpoint, so the
  adapter is just an HTTP call to localhost.

If an API model is ever used instead: `claude-opus-5` (1M context, so the
whole chapter fits and can be prompt-cached; ~$0.50-1.30/chapter,
~$4-5 for an 18-chapter book).

## Measured reference data (real output folders, 2026-09-06)

| Book | Chapters | Chunks/ch | JA chars |
|---|---|---|---|
| after-dark | 18 | 25-117 | 115,224 total |
| yojo-senki ch2 | — | **314** (largest seen) | 33,785 |
| sputnik ch15 | — | 164 | 17,447 |

Chunks average ~110 JA chars; `max_chunk_length` is user-configurable
(currently 50 in `settings.json`, default 100).

## Player side — no changes needed

Already implemented and working: SRT fetch per chapter, a minimal parser,
and `splitCueBySentence()` which splits a multi-sentence cue on periods
and gives each a share of the cue window proportional to its length. So
emitting **one cue per chunk** displays correctly today. The WebSocket
carries only the current cue *string*, never the file — subtitle format
has zero bearing on sync traffic.
