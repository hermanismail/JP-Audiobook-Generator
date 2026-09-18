# Book Profiler

Measures how a seiyuu copes with a book's sentences, and turns the
measurements into a **profile**: a duration-scale recipe per sentence
length (and an even-pace alternative), in six styles, for the generator's
**dynamic profile mode** to render with - instead of one hand-picked
setting for a whole book.

**It is a separate tool.** Like `seiyuu-audition` it imports
`text_pipeline.py` from the generator folder so its sentences are the
generator's sentences, and it runs the generator's `irodori_batch.py` for
TTS and the `C:\Transcribe` Whisper for scoring. It has its own venv, its
own `settings.json` and `gui_state.json` (neither committed).

## The window

```powershell
& "C:\JP-Audiobook-Generator\book-profiler\Run-Profiler.ps1"
```

Or once, for a desktop shortcut you can pin to the taskbar:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\book-profiler\Create-Shortcut.ps1"
```

### Profile tab

1. **Book** - the folder of chapter `.txt` files. It is parsed as soon as
   it is chosen: green when every file reads, red (and Run refuses) when
   not.
2. **Seiyuu** - one for every chapter, or **Customize**: tick the chapters
   to profile and pair each with its own seiyuu. Unticked chapters are
   skipped (an afterword, say). A newly ticked chapter takes the last
   seiyuu assigned.
3. **Profile folder** - the root everything lands under. The profiles end
   up in `<folder>\<book>\book\recipe\<seiyuu>\`. The measurement audio
   sits beside them, about 0.3 GB per chapter, until cleaned up.
   The one option: **Also use fixed seeds** (see below).
4. **Progress** - one row per chapter, read from what is on disk, so a
   reopened window shows where things stand.
5. **Profiles** - each seiyuu's book profile and chapter profiles with their
   summary, **Copy path** (paste it into the generator's Profile Path),
   **Open folder**, and **Generate / Open listening test**.

**Run** analyses the book, then per chapter renders the sweep and scores it
with Whisper, then builds each seiyuu's recipe. It is meant to be left
alone - roughly 15-30 minutes per chapter on the RTX 4060:

- a stage that fails is retried once, then that chapter is skipped and the
  rest carry on; the summary says which;
- the PC is kept awake while it runs (the screen may still turn off);
- a busy GPU at the start gets a warning, not a refusal;
- **Stop** ends the stage and its whole process tree (uv, Python, the TTS
  worker, Whisper), and **Run carries on from what is on disk** next time -
  finished takes are never rendered twice.

The listening test is optional: it renders the passage in all six styles
and opens the audition tool's Results window. Skipping it and going
straight to a real chapter in the generator's dynamic mode is just as good
a test.

### Clean up tab

Once a profile has proved good, the takes it was measured from are dead
weight. Pick a profile folder; each seiyuu of each book is one row, with a
**Delete audio** button and its size. It deletes, **permanently** (no
Recycle Bin):

- the sweep takes of every chapter that is measured AND in that seiyuu's
  book profile - a chapter still in progress or one that failed keeps its
  takes;
- that seiyuu's listening-test audio.

It keeps the profiles, `recipe.md`, `score.json`, every take's record and
Whisper transcript, so the recipe can still be rebuilt and read. Each
cleaned chapter gets a `cleaned.json`, and `sweep.py` / `score.py` refuse
to run on it: resume treats a missing wav as "not rendered yet", so without
the marker a rerun would quietly render the whole chapter again. To measure
a cleaned chapter again, profile into a fresh folder.

### What is fixed

The window asks only for book, seiyuu and folder. Everything else is what
was measured to work on yojo-senki x tanya (see the book-profiler section of
the repo's CLAUDE.md) and lives in `settings.json` for a developer: scales
1.0-1.8, 6 takes per cell, trim tail on, a fresh TTS worker every 80 takes,
Whisper `large-v3-turbo`, similarity 0.72, word slips up to 6 characters,
silences 1.5 / 1.0 / 0.7 s.

**Seeds.** Unticked, every take draws a random seed, 6 takes per cell.
Ticked, 3 fixed-seed + 3 random - the method tanya's recipe was built with,
kept for experiments with other seiyuu. Six takes either way: rebuilding
tanya's recipe from only its 3 random takes made 3 of 6 chapters LESS
cautious (chapter_001's shortest band went from x1.5/1.7 to x1.1/1.5).
Seeds made no difference; the number of chances a cell gets to show a
hallucination does.

## The command line

The window runs these same scripts; a developer can run, resume or re-score
any stage by hand. Every stage takes `--work-root` (the window's profile
folder; default `work_root` in `settings.json`); `sweep.py` and `score.py`
also take `--arms` (`random` or `seeded,random`) and `--takes`.

| stage | script | GPU | |
|---|---|---|---|
| 1 | `analyze.py --book <folder>` | no | sentence lengths in ENGINE characters, structure, symbols, 7 length-step sentences per chapter |
| 3 | `sweep.py --book --chapter --speaker` | yes | every step at scales 1.0-1.8, resumable per take |
| 4a | `score.py --book --chapter --speaker` | yes | Whisper over every take, scored against the script |
| 4b | `recipe.py --book --speaker` | no | `profile_*.json` + `recipe.md` from every scored chapter |
| 5 | `listen.py --book --speaker --profile book` | yes | the six-sample listening test |

```powershell
cd C:\JP-Audiobook-Generator\book-profiler
uv run python analyze.py --book "F:\AUDIOBOOK-FINAL\yojo-senki\text" --work-root F:\AUDIOBOOK-PROFILES
uv run python sweep.py --book yojo-senki --chapter chapter_001 --speaker C:\Irodori-TTS\seiyuu\list\tanya.speaker.safetensors --arms random --takes 6 --work-root F:\AUDIOBOOK-PROFILES
```

Which chapters the analysis covers does not change any chapter's length
steps (each chapter's are chosen from its own sentences - verified on
yojo-senki with and without chapter_007), so the window always analyses the
whole book.

## Settings (`settings.json`, developers only)

| key | default | |
|---|---|---|
| `irodori_root` | `C:\Irodori-TTS` | where `irodori_tts\text_normalization.py` is read from, and the TTS venv |
| `work_root` | `F:\tmp\book-profiler` | CLI default output root |
| `length_steps` | 7 | sentences per chapter to sweep |
| `min_spoken_chars` | 4 | kana/kanji a step sentence needs |
| `scales`, `takes`, `arms`, `fixed_seeds` | 1.0-1.8, 3, both, 1001/2002/3003 | the sweep (the window overrides arms and takes) |
| `whisper_exe`, `whisper_model` | `C:\Transcribe\...`, `large-v3-turbo` | scoring |
| `similarity_threshold`, `length_ratio_low/high`, `step_drop` | 0.72, 0.65/1.45, 0.15 | what flags a take |
| `word_span`, `safety_margin` | 6, 0.1 | the recipe formula |
| `silence_section/sentence/comma` | 1.5 / 1.0 / 0.7 | written into every profile |
