# Book Profiler

Measures how a seiyuu copes with a book's sentences, and turns the
measurements into a parameter recipe (duration scale per sentence length,
with default / slower / faster styles) instead of one hand-picked setting
for a whole book.

**It is a separate tool.** Like `seiyuu-audition` it imports
`text_pipeline.py` from the generator folder so its sentences are the
generator's sentences, and it will run the generator's `irodori_batch.py`
for TTS. It has its own venv and its own `settings.json` (not committed).

## Status

Stage 1 of 6 - the text analyser. No GPU, no TTS.

## Stage 1: analyse the text

```powershell
cd C:\JP-Audiobook-Generator
uv run --project book-profiler python book-profiler/analyze.py --book "F:\AUDIOBOOK-FINAL\yojo-senki\text"
uv run --project book-profiler python book-profiler/analyze.py --chapter "F:\AUDIOBOOK-FINAL\yojo-senki\text\chapter_003.txt"
```

Profile a whole book (`--book`) or chosen chapters (`--chapter`, repeatable)
- a book can use different seiyuu for different chapters. Output goes to
`<work_root>\<book>\<scope>\analysis\` as `analysis.json` (for the later
stages) and `analysis.md` (for reading).

The report covers:

- **Sentence length** per chapter and overall, split at the generator's
  terminators and measured in characters the ENGINE receives (after
  `prepare_tts_text` and Irodori's own `normalize_text`, which is loaded by
  file path from `irodori_root`).
- **Sentences that run across author lines** - dialogue closed by `」`
  without `。` is joined to the next line - and the lengths you would get
  if a line break also ended a sentence.
- **Structure** - sections, author lines, heading-like sections, headings
  glued onto the next sentence.
- **Length steps** - 7 real sentences per chapter, evenly spread in length,
  chosen only from sentences where nothing but length could explain a bad
  take.
- **Symbols** - every non-kana/kanji character with its count, what the
  pipeline and the engine do to it, and `measured` / `unmeasured` /
  `removed`. `measured` means a pause probe recorded in CLAUDE.md, nothing
  weaker.

`drift` in the report must be 0: every paragraph's sentences rejoin to the
paragraph exactly.

## Settings

| key | default | |
|---|---|---|
| `irodori_root` | `C:\Irodori-TTS` | where `irodori_tts\text_normalization.py` is read from |
| `work_root` | `F:\tmp\book-profiler` | all output |
| `length_steps` | 7 | sentences per chapter to audition |
| `min_spoken_chars` | 4 | kana/kanji a step sentence needs |
