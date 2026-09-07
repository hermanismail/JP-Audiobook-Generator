# CLAUDE.md — JP Audiobook Generator

Context for Claude Code sessions in this repo. Rewritten 2026-09-07, after
the translation-subtitle feature was built and merged to `main`. An earlier
version of this file carried that feature as an unbuilt spec — it is built
now, and where the implementation diverged from the spec is recorded below,
because the divergences are the interesting part.

Everything in this file describes code that exists today.

## What this project is

A Windows desktop tool (Python + CustomTkinter) that turns Japanese chapter
text files into a chaptered audiobook: cleaned/chunked text → Irodori-TTS →
per-chunk wavs → stitched MP3 + `sync.json` timing data + ID3 tags, and
optionally an English `.srt` per chapter. Its output folder is the input to
the separate **player** project (`F:\JPAudiobookPlayer` — Android app + Node
server/web client; that repo has its own detailed CLAUDE.md).

**Two-venv architecture**: a lightweight GUI venv for this project, and a
separate heavy ML venv at `C:\Irodori-TTS` for the TTS engine.
`run_audiobook.py` runs inside the Irodori venv and is stdlib-only where it
can be; it shells out to `mp3_metadata.py` with `uv run --project <this dir>`
so `mutagen` never needs installing in the ML venv. Keep that separation — it
is deliberate.

## Layout

- `run_audiobook.py` — the pipeline driver. `process_chapter()` is the spine:
  read raw text → `text_pipeline.build_chunks()` → write working files → one
  TTS call per chunk → ffmpeg concat with silence wavs → **step 5b** write
  `sync.json` → **step 6** optional auto-tag. `main()` then optionally runs
  translation once for the whole book.
  - `build_sync_data()` is the load-bearing piece for anything downstream: it
    walks the *same* concat ordering used to stitch the MP3, summing
    `ffprobe`'d durations, so each chunk's `start`/`end` in the final file
    falls out with no separate alignment pass. Output is
    `{version, chunks:[{index, start, end, text}]}` written to
    `<chapter>.sync.json`. It must run **before** the temp-dir cleanup — it
    needs the per-chunk wavs still on disk.
  - `get_silence_wavs()` renders three silence wavs once per run, matched to
    the TTS output's exact sample rate / channels / sample format — the concat
    demuxer does no resampling, so drift here corrupts audio.
  - `MODEL_REF` is a **constant, not a setting**. The pipeline uses the
    published `Aratako/Irodori-TTS-v4.1-Small` checkpoint, so there is no
    Model Path field in the GUI. `checkpoint_args()` picks `--checkpoint` vs
    `--hf-checkpoint` from the shape of the value, because those two are a
    *mutually exclusive, required* argparse group in `infer.py` and passing
    both makes it exit 2 before generating anything.
  - `write_settings_snapshot()` drops `<foldername>_YYYYMMDD_HHMM.json` into
    the output folder before the first chapter, so a run that dies halfway
    still leaves a record. Everything outside its `_run` block is a faithful
    copy of `settings.json`, which makes a snapshot a valid preset for the
    GUI's Import.
- `text_pipeline.py` — text cleaning and chunking, stdlib only. Sections
  (blank-line split) → paragraphs → sentences (。？……) → merged chunks at a
  soft/hard char limit. Each chunk carries **two** texts:
  - `text` — TTS-normalized (brackets stripped, ── → 、, punctuation runs
    collapsed). What goes to the TTS engine.
  - `display_text` — original wording. **This is what lands in `sync.json`
    and what the reader app displays.** Do not conflate them.
- `translate_pipeline.py` — subtitle generation. Stdlib only, so it runs
  unchanged in either venv. See the Translation section below.
- `mp3_metadata.py` — ID3 tagging (mutagen). Has a `--chapter <base>` CLI and
  re-reads `settings.json` itself. The pattern `translate_pipeline.py`
  copies.
- `gui_settings.py` — CustomTkinter settings GUI. Three pages (General,
  Metadata, Advanced) plus a bottom bar with Import / Export / Save & Run.
- `subtitle_window.py` — the Subtitle Generation Tool window. Owns the worker
  thread, pause/resume, the llama-server lifecycle and its own log.
- `progress_window.py`, `ui_common.py` — progress UI and shared design tokens.

## Conventions worth not breaking

- Hand-rolled `json` building; no serialization library.
- `json.dump(..., ensure_ascii=False)` — the data is Japanese.
- **A new setting means four edits**: `DEFAULT_SETTINGS` in `gui_settings.py`,
  the defaults dict in `run_audiobook.py`, `_collect_and_validate()` (which
  writes `settings.json`), and a widget. Import and Reset need no work —
  `_apply_settings_to_fields()` merges over `DEFAULT_SETTINGS` generically.
- **`_collect_and_validate()` rebuilds `settings.json` from a fixed key list.**
  Any key it does not know about is silently dropped on the next save. This is
  why the llama paths and TTS tuning values had to be added there and not just
  to the defaults dicts, and why "just add it to settings.json by hand" does
  not survive.
- Post-processing tools are separate scripts with their own `--chapter` CLI
  that re-read `settings.json`, invoked via `uv run --project`, and gated
  behind a boolean setting for the automatic path. Both the automatic and
  manual paths call the same code.
- Scripts reading `settings.json` directly must merge their own defaults
  (`translate_pipeline.merge_setting_defaults`). A settings file written
  before a feature existed otherwise yields empty strings, not defaults.

---

# Translation subtitles — BUILT

Original rationale, cost math and rejected alternatives live in
`F:\JPAudiobookPlayer\translation-pipeline-notes-rev2.pdf`. What follows is
what actually shipped.

## The shape that survived

Translation reads the finished `<chapter>.sync.json`, never the raw text, and
emits a sidecar `.srt` plus a `.translation.json` artifact. Chunk boundaries
are already fixed by the rendered MP3 by then, so timing is correct by
construction. `sync.json` is only ever read; English never goes near its
`text` field.

```
read  <base>.sync.json          (never modified)
  -> translate chunk by chunk
  -> write <base>.translation.json    source of truth
  -> write <base>.srt                 cheap re-emission
```

**The index invariant is enforced structurally, not by validation.** The spec
called for asking the model for indexed batches and rejecting mismatches. That
is not what happened, because VNTL made it unnecessary: the driver zips
backend output positionally onto `sync.json`'s own chunk list, and a backend
returning the wrong count is rejected before anything is written. The model
never sees an index, so it cannot renumber one. There is no retry loop.

## Where the implementation diverged from the spec

| Spec said | What shipped | Why |
|---|---|---|
| Batch 5–10 chunks as indexed JSON, validate and retry | One chunk at a time, alignment from the loop | VNTL is a completion model with no JSON and no instruction-following. This turned out **safer**, not weaker — misalignment became impossible rather than merely detected. |
| Glossary as a separate persisted phase | Glossary *is* VNTL's native `Metadata` block | The format has a trained-in slot for name/gender/aliases. Bolting a second mechanism on top would have fought it. |
| Rolling context window as bespoke machinery | The prompt's own alternating Japanese/English history | Same reason. 12 pairs. |
| VNTL uses a custom alternating prompt format | v2 uses the **standard LLaMA 3 token scheme** with custom header roles (`Metadata` / `Japanese` / `English`) | The spec described v1. v2 changed it. No chat-completions API can express those roles, so the adapter must use raw `/completion`. |

## Key files and objects

- `BACKENDS` — `{"identity": ..., "vntl": ...}`. Adding a backend is one
  function plus one dict entry. `identity` passes the Japanese through
  untranslated and is **not** a placeholder: it validates the artifact format,
  the SRT emitter and the player round-trip with no model in the loop.
- `TranslationControl` — pause/cancel, checked between chunks. Pausing leaves
  llama-server loaded so resuming is instant; only cancelling unwinds through
  the context manager that frees VRAM. **Cancel must also release a paused
  worker**, or the emergency stop hangs forever on a paused run.
- `ManagedLlamaServer` — context manager. Starts the server on demand, stops
  it afterwards. **Only ever stops a server it started**; one already
  listening is used as-is and left running. `finally`-backed, so a crash or an
  interrupt still frees the card.
- `BackendUnavailable` — stops the whole run at the first chapter rather than
  failing identically once per chapter and burying the cause.
- `glossary.json` — per book, beside the chapters, reloaded at the start of
  every chapter. `name` must match the text exactly; `gender` fixes pronouns
  Japanese leaves implicit.

## Tuning that is not negotiable

- **Temperature 0, no repetition penalty.** The model author's explicit
  recommendation. A repetition penalty punishes the legitimately recurring
  tokens translation depends on — character names, particles — and pushes the
  model into renaming or omitting them.
- **Skip existing is the default everywhere.** `generate_subtitles` defaults
  to `skip_existing=True`; overwriting is always explicit (`--regenerate`, or
  the tool's toggle). An existing `.srt` may have been corrected by hand.
- **The chapter decision drives the subtitle decision on the automatic path.**
  `Regenerate existing chapters` OFF means existing `.srt` files are skipped
  too. This is correctness, not tidiness: regenerating a chapter rewrites its
  `sync.json`, and an `.srt` is timed against that file.

---

# Disk layout — C: is tight, new things go on F:

Recorded 2026-09-07, after the cache migration described below.

**Default rule for anything new: install to F:.** New models, new
checkpoints, new engines, new heavy venvs, new caches — F: is a 932 GB
internal SATA SSD (`KINGSTON SUV500S37/960G`, NTFS, fixed, stable letter,
same storage class as C:, so no meaningful load-time penalty). C: is a
223 GB SATA SSD that ran down to **20.8 GB free**. Do not add gigabytes to
C: without a reason that has to be on C:.

## What was actually moved (Phase 0, done)

Only the two caches moved. They were the best value-per-risk on the list:
bigger than the three project folders combined, and relocatable by
environment variable with **zero code changes**.

| Cache | Now at | Env var | Reclaimed |
|---|---|---|---|
| HuggingFace | `F:\caches\huggingface` | `HF_HOME` | 11.76 GB |
| uv | `F:\caches\uv` | `UV_CACHE_DIR` | 0.97 GB |

C: went 20.84 → **33.57 GB** free. Both env vars are **user-scoped**, so
they apply to newly launched processes only.

**Why uv reclaimed 0.97 GB and not its nominal 6.08 GB**: the uv cache
**hardlinks** into the venvs, so a recursive size measurement counts those
bytes twice. Deleting the cache frees only the unshared portion; the rest
is pinned by `C:\Irodori-TTS\.venv` and comes back only when that venv
goes. Expect the same illusion anywhere else hardlinks are in play — a
`du`/`Get-ChildItem` total is an upper bound on what a delete will free.

Side effect of the split: uv's cache is now on F: while the venvs are on
C:, so uv **copies instead of hardlinking** on install. Costs real disk,
harms nothing. Co-locating a venv with the cache on F: restores linking.

## Still on C: — deliberately, not forgotten

`C:\Irodori-TTS` (8.1 GB), `C:\llama.cpp` (6.0 GB) and this repo (33 MB)
were assessed and left alone; 33.57 GB was judged enough buffer. The
settings keys pointing at them are correct as written. If that changes,
the cheapest next step is llama.cpp: loose exes/dlls/gguf with no embedded
paths, so it is a file move plus `llama_server_exe` / `llama_model_path`
in `settings.json` (and the matching defaults in `gui_settings.py`,
`run_audiobook.py`, `translate_pipeline.py` — a stale default silently
points at a dead path, because `merge_setting_defaults` fills from it).

## Never copy a venv between drives

Both `.venv` dirs hardcode absolute paths — `pyvenv.cfg`'s `home`,
`Scripts\activate.bat`, every `Scripts\*.exe` launcher stub, and the
editable install's `direct_url.json` (`file:///C:/JP-Audiobook-Generator`).
A copied venv looks fine until something invokes it, then fails obscurely.
**Delete it and `uv sync --locked` at the destination.** For Irodori that
rebuild needs network *and* git — `silentcipher` comes from a GitHub URL —
so it is not an offline operation.

`%APPDATA%\Roaming\uv\python` holds the interpreters both `pyvenv.cfg`
files point at. Leave it on C:; it is 0.1 GB.

## The HF cache has 14 phantom entries

Five repos list files in the cache metadata whose blobs were never stored
locally (tokenizers, configs, silentcipher's `.ckpt` set). `snapshot_download`
with `local_files_only=True` therefore raises `IncompleteSnapshotError`, and
those 14 `hf_hub_download` calls raise `LocalEntryNotFoundError`. **This
predates the move** — the same scan against the C: original returned an
identical 6-resolved / 14-failed split. Not migration damage. Do not "fix"
it by re-downloading in the belief that the copy was corrupt.

Corollary worth keeping: when validating any copy, run the identical check
against the original as a control before concluding the copy is broken.

# Things that bit us, and will again

Recorded because each cost real time to find.

**Subprocess output is silently discarded by default.** The TTS call used
`subprocess.run(cmd, capture_output=True)` and then only checked whether the
wav appeared. An `infer.py` argparse error therefore looked like "nothing
happened", with no message anywhere. It now prints stderr and the exit code on
failure. Any new subprocess call should assume it will fail one day and needs
to say why.

**`-u` matters for anything whose output you want to watch.** Python
block-buffers stdout when it is a pipe. Without `-u` the per-chunk progress
sits in an 8KB buffer and arrives hours later, in one lump. `gui_settings.py`
passes it when launching `run_audiobook.py`; `run_audiobook.py` passes it when
launching `translate_pipeline.py`.

**Tk packing order decides who gets space.** `_title_block` packs its frame
immediately, so a title packed before a right-hand control with
`expand=True` claims the whole row and the control renders at zero width. This
made the seed switch invisible. Pack the right-hand control **first** —
`_add_path_row` has always done this. Symptom: a widget that exists, reports
`winfo_manager()` truthy, and cannot be seen.

**`_add_path_row`'s title block is a fixed 210x44.** A description longer than
about one line is clipped, not wrapped, and collides with the entry beside it.
Long explanations belong in the README.

**Do not drive real GUI handlers in tests.** `on_generate_subtitles()` and
`_collect_and_validate()` write `settings.json` and `output_folder`. Running
them in a smoke test wrote 18 identity-passthrough `.srt` files into a live
serving folder, and separately overwrote a chosen backend setting. Test the
library functions with an explicit throwaway settings dict instead.

**Patch scripts must assert.** A `str.replace()` that does not match fails
silently and the script still prints success. One such failure left the
end-of-run translation hook out of a commit whose message claimed it shipped.
Every replacement gets an `assert old in s`.

**Windows will not composite the off-screen part of a window.** `PrintWindow`
captures of a window taller than the screen come back black below the screen
edge. Screenshots of the scrollable Advanced page need two captures at a
screen-safe height.

**A fixed sampling seed made output *worse*.** Pinning one seed for a whole
run was tried on the theory it would stop chunks drifting against each other;
it destabilised chapters instead, because one unlucky draw then affects every
chunk rather than averaging out. The setting exists and defaults to OFF. Do
not "fix" this again.

---

# Measured reference data

Real numbers from this machine (RTX 4060, 8GB), 2026-09-06/07.

| Thing | Value |
|---|---|
| Translation throughput | **2.40–2.41 s/chunk**, flat with chapter length (measured over 422 chunks across two chapters) |
| VNTL model | `vntl-llama3-8b-v2-hf-q5_k_m.gguf`, 5.73 GB |
| VRAM with model loaded, 8k context | ~7.1–7.3 GB of 8188 MiB |
| llama-server cold start | ~18 s |
| A 314-chunk chapter | ~13 min |
| An 18-chapter book (~1040 chunks) | ~42 min |
| MP3 at 96k mono vs old 320k stereo | ~30% of the size |
| TTS output | 48 kHz, mono, 16-bit PCM (fixed; not configurable) |

Chunk counts vary with `max_chunk_length`, which is user-configurable — the
older reference figures of ~110 JA chars per chunk were taken at a higher
setting than is currently in use.

# Player side — no changes needed

Already implemented and working there: SRT fetch per chapter, a minimal
parser, and `splitCueBySentence()` which splits a multi-sentence cue on
periods and gives each a share of the cue window proportional to its length.
Emitting **one cue per chunk** displays correctly today. The WebSocket carries
only the current cue *string*, never the file.

# Open threads

- `chapter_001.translation.json` in `F:\AUDIOBOOK_OUTPUT\yojo-senki` is
  truncated to a few chunks from a `--limit` test. Regenerate if that folder
  matters; the complete version was copied to `AUDIOBOOK-HOST\yojo-senki`.
- Progress-window screenshots in the README predate translation logging; they
  are still accurate for the generation run itself.
