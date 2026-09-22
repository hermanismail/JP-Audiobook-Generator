# CLAUDE.md — JP Audiobook Generator

Context for Claude Code sessions in this repo. Rewritten 2026-09-07, after
the translation-subtitle feature was built and merged to `main`. An earlier
version of this file carried that feature as an unbuilt spec — it is built
now, and where the implementation diverged from the spec is recorded below,
because the divergences are the interesting part.

Everything in this file describes code that exists today.

## State of play (2026-09-18) - start here

Everything below is merged to `main`. One generator, five separate tools:

| folder | what | status |
|---|---|---|
| (root) | the generator: **normal** mode (chunks) and **dynamic profile** mode (sentence by sentence from a profile) | both in use; dynamic is how books are rendered now |
| `book-profiler/` | measures a seiyuu against a book -> `profile_*.json` for dynamic mode; window with Profile + Clean up tabs | built, proven on yojo-senki x tanya |
| `dynamic-repair/` | repairs Parts of dynamic chapters; editable TTS text + per-book `readings.json` | built, proven on copies; awaiting real use |
| `chapter-repair/` | repairs pre-dynamic chapters | **frozen** - kept, not developed |
| `seiyuu-audition/` | hear a seiyuu at given settings | stable |
| `seiyuu-onboarder/` | train a new seiyuu | stable |

The user is slowly re-rendering older books in dynamic mode.

**Open threads, roughly in the order they were raised:**
1. The **reader shows one sentence per `sync.json` entry** for dynamic
   chapters. Accepted for now; grouping sentences for display is open
   (player side, `F:\JPAudiobookPlayer`).
2. ~~Readings at render time~~ - built 2026-09-22, see "Readings" under
   dynamic profile mode.
3. The user's by-ear notes on tanya (unmeasured, 2026-09-17): sentences
   ending `！`/`？` sound over-excited and sometimes unfinished; some 19-20
   character sentences ending `。` at x1.5 cut short; a 9-character
   sentence at x1.7 sounded like a different voice. Each is a hypothesis
   until measured.
4. The existing tanya profiling data sits in `F:\tmp\book-profiler`
   (2 GB of takes); the user will decide where profiles live.
5. The older ones listed under "Text, chunking and hallucination" below.
6. **Scene illustrator** (planned 2026-09-18, a separate tool): zen-mode
   images per chapter, timed to `sync.json` through a
   `chapter_<N>.images.json` sidecar. Local test: ComfyUI at `F:\ComfyUI`
   with FLUX.2 klein 4B, ~10 s per image on the 4060.

**Player side (user decision 2026-09-18): the Android app is FROZEN.**
Development goes into the web client first, then the server. Player
changes this repo needs (e.g. reading the images sidecar) are handed over
as a prompt for a separate session in `F:\JPAudiobookPlayer`.

**How the user works** (from the sessions that built all this): digest a
request, summarise it back with the loose ends and decisions needed, and
WAIT before building. Claims about model behaviour need a measurement. Git:
feature branch, commit, then "merge to main and push", then "delete the
branch"; every git call in a script gets its own check; the three
`settings.json` files at root / `chapter-repair/` / `seiyuu-audition/` stay
uncommitted and must survive branch switches. Exact-string edits, never
`str.replace` patch scripts.

## What this project is

A Windows desktop tool (Python + CustomTkinter) that turns Japanese chapter
text files into a chaptered audiobook: cleaned/chunked text → Irodori-TTS →
per-chunk wavs → stitched mono AAC `.m4a` + `sync.json` timing data + MP4
tags, and
optionally an English `.srt` per chapter. Its output folder is the input to
the separate **player** project (`F:\JPAudiobookPlayer` — Android app + Node
server/web client; that repo has its own detailed CLAUDE.md).

**Two-venv architecture**: a lightweight GUI venv for this project, and a
separate heavy ML venv at `C:\Irodori-TTS` for the TTS engine.
`run_audiobook.py` runs inside the Irodori venv and is stdlib-only where it
can be; it shells out to `audio_metadata.py` with `uv run --project <this dir>`
so `mutagen` never needs installing in the ML venv. Keep that separation — it
is deliberate.

## Layout

- `run_audiobook.py` — the pipeline driver. `process_chapter()` is the spine:
  read raw text → `text_pipeline.build_chunks()` → write working files → one
  TTS call per chunk → ffmpeg concat with silence wavs → **step 5b** write
  `sync.json` → **step 6** optional auto-tag. `main()` then optionally runs
  translation once for the whole book.
  - `build_sync_data()` is the load-bearing piece for anything downstream: it
    walks the *same* concat ordering used to stitch the `.m4a`, summing
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

  It has a second consumer now: `seiyuu-audition/` imports it so its E2E
  mode chunks exactly the way a real chapter does. Changing
  `build_chunks()`'s signature or the keys of a chunk dict breaks that
  tool as well as the generator.

  A closing bracket (`」』）`) right after a terminator belongs to the
  sentence it closes: `…から？」` | `彼女は…`, never `…から？` | `」彼女は…`
  (`TERMINATOR_RE` takes it as part of the terminator; `merge_units()`'s
  `、` fallback does the same). Before 2026-09-11 the bracket opened the
  next chunk — 297 chunks across the chapter files at length 40 — and a
  lone `」` left as its own fragment produced empty TTS text and was
  **dropped from `sync.json` entirely** (4 files). Chapters rendered before
  then still carry that in their `sync.json` until regenerated.

  A run of `─` (the conventional `──`) is reduced to a single `─` in
  `split_paragraphs()`, so `sync.json` shows one dash and chunk lengths
  count one character; the TTS text still turns it into `、`.

  Both rules were back-applied to the published library on 2026-09-11:
  every `sync.json` in `F:\AUDIOBOOK-HOST-AAC` had its text repaired in
  place (text fields only — the audio already matched, since the TTS drops
  an edge bracket and reads a dash as `、` either way). Originals are in
  `F:\_backup\AUDIOBOOK-HOST-AAC-sync-20260911`. `F:\AUDIOBOOK-HOST` (the
  MP3 masters) and `F:\AUDIOBOOK_OUTPUT` were not touched. Two chunks in
  sputnik (ch.007 #34, ch.012 #193) were a lone `」` with their own 0.76 s
  audio slot — left by an older bracket-edge splitter at broken-off speech —
  and were merged into the chunk before (its `end` stretched over the slot,
  later chunks renumbered). Those two chapters therefore have one fewer
  chunk than their `.srt` has cues; the extra cue is time-matched, so it

  still displays correctly.

  The repaired files were published to R2 the same evening (objects
  written 2026-09-11 15:38 UTC, in a separate session), so the live site
  serves the corrected text. Verified 2026-09-14 with `rclone check`
  against `r2:shama-audiobooks`: all 71 `sync.json` match by hash, and
  none has a chunk opening on a closing bracket or containing `──`.
  rclone's `lsl` timestamps are UTC; local time here is +0800.

  ### Chunking overhaul, 2026-09-16

  Four changes in one pass, after `wall/chapter_013` #35 came back as a
  175-character chunk against a `max_chunk_length` of 40.

  **1. `merge_units()` lost track of the remainder.** When a sentence
  overflowed the hard limit it split at ONE break point and put the whole
  remainder into the buffer without ever re-measuring it. Worse, the next
  sentence's break search measures `buffer + text[:p]` - which an
  oversized buffer already exceeds - so it fell through to “accept the
  overflow” and glued another whole sentence on top. chapter_013's
  129-character unit (commas at 7, 16, 60, 90, 104 - five usable points)
  became a 17-char chunk plus a 112-char remainder, which then grew to
  175. Now the buffer closes and the rest is broken down by
  `_split_oversized()` until every piece fits.

  **2. `！` `!` `?` are break points, NOT terminators.** This distinction
  is the whole point and was settled by measurement, not intuition. Same
  carrier sentence with no grammatical break at the insertion point, two
  independent seeds agreeing:

  | symbol | gap seed A | gap seed B | |
  |---|---|---|---|
  | (nothing) | 0.81 s | 0.59 s | baseline |
  | `、` | 1.01 | 1.05 | pauses |
  | `。` | 1.14 | 1.25 | pauses |
  | `？` | 1.16 | 1.49 | pauses |
  | `……` | 2.03 | 2.17 | pauses hardest |
  | `＊` | 1.16 | 1.25 | pauses, but as TWO silences - it stumbles |
  | `！` | 0.36 | 0.36 | **no pause - SHORTER than baseline** |
  | `─` | 0.81 | 0.59 | **identical to no symbol - deleted** |

  `！` does not merely fail to cue a pause, it shortens the natural gap:
  an intonation cue, not a break. Promoting it to a terminator would
  split there and insert a full silence wav the model never wanted, so
  `戦争が始まりました！！` would be followed by a second of nothing. As a
  fallback break point it costs nothing: a normal exclamation stays whole
  and only an over-long run is cut there. The ascii `?` is included
  because only the full-width `？` was ever a terminator and the library
  has 18 of the ascii form.

  The `─` row independently confirms Irodori's `text_normalization.py`,
  which deletes the whole dash range outright - so our `─` -> `、` rule
  is doing real work, not duplicating the engine. Without it the beat
  would vanish silently.

  **3. Invisible characters are stripped** (`INVISIBLE_RE`). 20 of the 82
  chapter files start with a UTF-8 BOM, and `open(..., encoding="utf-8")`
  keeps it (only `utf-8-sig` strips it). `str.strip()` does not touch
  U+FEFF - it is a format character, not whitespace - and neither did
  Irodori's normalizer, so it was reaching the engine as the first
  character of those chapters' first chunk.

  **4. `CLOSING_BRACKETS` widened** from `」』）` to include `〉》】〟)`.
  A scan of all 82 files found `〉` x83, `】` x8, `》` x7, `〟` x6, none of
  them covered by the rule that keeps a closer with the sentence it
  closes. One published chunk already opens on one: yojo-senki/
  chapter_002 #185 starts with `》`. That one is text-only and therefore
  back-applicable like the 2026-09-11 fix, because Irodori deletes `《》`
  outright and the audio never spoke it.

  **Measured across all 69 source chapters, old vs new:**

  | | before | after |
  |---|---|---|
  | chunks over the hard limit | 474 | **14** |
  | chunks that would hit the 30 s ceiling | 52 | **0** |
  | longest chunk | 222 chars | 93 |
  | chunks opening on a closing bracket | 2 | 0 |
  | chapters whose first chunk carries a BOM | 15 | 0 |
  | total chunks | 7,754 | 8,202 (+5.8%) |

  **Not one character of text changed**: for all 82 chapters the
  concatenation of every `display_text` is identical before and after,
  apart from the removed BOMs. No empty chunks, none punctuation-only.
  The 14 still over the limit have 0 or 1 break points in them - genuinely
  unsplittable sentences, the documented fallback - and none of them
  reaches the ceiling.

  **None of this can be back-applied.** Chunk boundaries are fixed by
  rendered audio, so existing books keep their chunking until re-rendered;
  their over-long chunks stay a `chapter-repair` job.

  ### Dynamic profile mode functions, 2026-09-16

  `text_pipeline.py` also carries the text rules for the planned **dynamic
  profile mode** (see `book-profiler/` below). They are ADDITIVE:
  `build_chunks()` and `prepare_tts_text()` are untouched, proven by
  comparing them against the previous module over 103 chapter files at
  chunk lengths 40 and 100 - 20,581 chunk lists, 0 differences.

  - `dynamic_sentences(raw)` - a **line break ends a sentence** as well as
    `。？……`. Normal mode joins an author's lines, and because dialogue in
    these books closes with `」` and no `。`, 910 of yojo-senki's 7,020
    "sentences" were a dialogue line welded to the narration after it -
    including its longest (168 chars; 134 with line breaks).
  - `split_for_length(text, limit, measure)` - cuts a sentence longer than
    the seiyuu's comfortable length after `、」）)` or before `「（(` (0.7 s
    `comma` silence) or after `！!?` (1.0 s `sentence` silence). The whole
    set of cuts is chosen at once - fit the limit if possible, then fewest
    pieces, then most even - and no piece is shorter than
    `DYNAMIC_MIN_PIECE` (5). A greedy latest-cut-that-fits was tried first
    and stranded requests like `と。` and `だから、`.
  - `prepare_tts_text_dynamic(text)` - `prepare_tts_text()` plus: `×` is
    stripped; `（）()` are treated like `「」` (removed at an edge, else `、`);
    four-digit kanji years (`一九二三年` -> `1923年`) and every other
    positional kanji number holding a `〇` (`高度四三〇〇` -> `4300`) go to
    digits, because Irodori's normaliser turns `〇` into `○`. A run touching
    `十百千万` (`二〇十三年`) is left alone.

  All of it is a boundary rule for future renders only - nothing to
  back-apply.

- `translate_pipeline.py` — subtitle generation. Stdlib only, so it runs
  unchanged in either venv. See the Translation section below.
- `audio_metadata.py` — tagging (mutagen): MP4 atoms on `.m4a`, ID3 on a
  legacy `.mp3`. Was `mp3_metadata.py` until the switch to AAC. Has a
  `--chapter <base>` CLI and re-reads `settings.json` itself. The pattern
  `translate_pipeline.py` copies.
- `irodori_batch.py` — the TTS worker: one model load per CHAPTER instead
  of one per chunk. Runs in the Irodori venv, reads a job file written by
  `run_audiobook.py`, and speaks a line protocol back (`MODEL_LOADED`,
  `CHUNK_START/DONE/FAIL`, `BATCH_DONE`) which `run_batch_worker()`
  translates into the same `Generating chunk i/N` output as before. See
  the TTS batching section below.
- `gui_settings.py` — CustomTkinter settings GUI. Three pages (General,
  Metadata, Advanced) plus a bottom bar with Import / Export / Save & Run.
  A sidebar switch picks **normal** or **dynamic profile** mode - see the
  next section. `dynamic_mode_ui.py` holds the dynamic-only widgets.
- `dynamic_profile.py` — the profile contract shared by the generator, the
  profiler and dynamic-repair: loading/validating `profile_*.json` (v2),
  the six styles, `request_for()` (length -> duration scale or seconds),
  `plan_pieces()` / `plan_chapter()`.

## Dynamic profile mode — the generator's second mode (built 2026-09-17)

Normal mode is the generator as it always was (chunks, your silences,
chunk length, TTS tuning) - proven byte-identical after dynamic mode went
in (stub-worker run of HEAD vs new over 3 real chapters: 2,347 files
identical). Dynamic mode renders **sentence by sentence from a
`book-profiler` profile**: each sentence gets the request its length band
calls for with that seiyuu.

- **Settings**: `generation_mode` (`normal`/`dynamic`) and a `dynamic`
  block `{profile_path, style, assign: "all"|"custom", chapters: {base:
  {enabled, profile_path, style}}}` in `settings.json`, merged by
  `run_audiobook.merge_dynamic_defaults()`. The window opens in the last
  mode used (a launch pop-up was built and REMOVED 2026-09-18 as redundant
  with the switch - do not reintroduce it).
- **GUI in dynamic mode**: General groups Input Folder + Profile Path
  (Speaker Path becomes Profile Path); choosing the input folder parses it
  (green count / red error that blocks Save & Run); under the profile, its
  summary and the style selector. "Assign profile for all chapters" or
  "Customize" (a window: tick, chapter, profile, style per row; unticked
  rows skipped; a newly ticked row takes the last profile assigned).
  Advanced shows only Output Encoding and Translation Subtitles.
- **Six styles**: Natural (the profile's duration scales per band) and Even
  pace (a fixed length per sentence from the profile's pace targets), each
  default / slower / faster - fewer for a narrow seiyuu (profile v3
  `available_speeds`, see book-profiler below). By ear on yojo-senki: Natural default was the
  most natural and cleanest; Even pace too flat.
- **Text**: `text_pipeline.dynamic_sentences()` - a line break ends a
  sentence; over-long sentences are cut with `split_for_length()` (0.7 s
  after `、」）)`, 1.0 s after `！!?`); a sentence never opens on a
  punctuation mark (a leading `、` is dropped from display and TTS, other
  marks join the previous sentence). Silences: 1.5 s section (also before
  a chapter's first sentence), 1.0 s sentence, 0.7 s comma.
- **Readings** (built 2026-09-22, dynamic mode ONLY): `readings.json` in
  the **Output folder**, beside `glossary.json` (which the translation tool
  reads from `<output_folder>`, not the input folder). Same format
  dynamic-repair writes; hand-written works - only `word`/`reading` count.
  Loaded once per run (`dynamic_profile.load_readings`, longest word
  first), applied by `plan_pieces()` to the **TTS text only**, after
  cutting and measuring: cuts, `engine_len`, band and an Even-pace length
  stay keyed to the book's wording, exactly as in dynamic-repair.
  `sync.json`/`.srt`/translations keep the book's text. A plain replace -
  a word inside another word changes too (accepted; keep words specific).
  `render.json` records the file's readings and, per piece, `readings`
  used. Proof (34 checks, stubbed worker, scratch folders): with no file
  the plan is identical to `main` over 7 yojo-senki chapters x 2 styles;
  with one, 562 pieces change TTS text and nothing else.
- **Output**: `process_chapter_dynamic()` -> the same `finish_chapter()`
  tail as normal mode (stitch, FLAC master, `sync.json`, tags) plus
  `<chapter>.render.json` - per `sync.json` entry the display and engine
  text, request, band, used seed, duration and gap, and the profile block.
  `dynamic-repair` depends on it. The settings snapshot is named
  `<book>_<mode>_YYYYMMDD_HHMM.json` and carries `_run.dynamic_plan`;
  Import refuses a snapshot of the other mode.
- **Real render** (yojo-senki chapter_001 + chapter_007, 2026-09-17): the
  user judged it a leap over unprofiled chapters; hallucination much
  reduced, not gone - judged close to what this model and seiyuu allow.
  Test output in `F:\AUDIOBOOK_TEST\yojo-senki-dynamic` (chapter_001 there
  predates the leading-comma fix).
- `subtitle_window.py` — the Subtitle Generation Tool window. Owns the worker
  thread, pause/resume, the llama-server lifecycle and its own log.
- `progress_window.py`, `ui_common.py` — progress UI and shared design tokens.

## `seiyuu-onboarder/` — a separate tool in the same repo

**Not part of the generator.** It shares no code with the files above, has
its own venv, `pyproject.toml` and `settings.json`, and imports nothing from
its neighbours. It lives here for one history and one clone, nothing more —
treat the folder boundary as the separation.

What it does: onboards a new Irodori-TTS speaker. Wav samples in
`audio/<speaker>/<style>` -> Whisper (shelled out to the existing
`C:\Transcribe` venv, never installed twice) -> **a review gate where the
person fixes the text** -> `metadata.csv` -> `prepare_manifest.py` ->
`train.py` -> a hardlink at `seiyuu/list/<speaker>-<style>.speaker.safetensors`,
which is what the generator's Speaker Path field points at.

The pair `<speaker>/<style>` is the only input; every path derives from it.
The gate is not automatable: Whisper returns unpunctuated lines AND mishears
— the real `moeshi/calm-01` text opens with an `えへへ。` that is nowhere in
its output. Saved text always wins over a fresh suggestion, or re-opening a
speaker would discard hand corrections.

**Proven end to end (2026-09-14).** A new speaker was onboarded entirely
through this tool - wav samples in, review gate, manifest, training - and
the resulting `seiyuu/list/*.speaker.safetensors` was then used for real
inference with no issue. The manual command-line path is retired.

## `seiyuu-audition/` — the other separate tool in this repo

**Also not part of the generator**, and it answers the question that
comes after the onboarder's: this speaker exists, but what chunk length
and duration scale should a book use with it? Speaker embeddings behave
differently on identical parameters, which is why those two are
per-preset settings rather than constants.

One text, one or more SAMPLES (a seiyuu plus a parameter set), played
back to back in a Results window. Two modes, never mixed in one
audition: `simple` speaks the text verbatim, `e2e` runs the real
chunking and cleaning and stitches the chunks with silences.

Three things about it are load-bearing:

- **It imports `text_pipeline.py` from the folder above** — the one
  exception to the onboarder's shares-nothing rule, and a deliberate
  one. A copy would drift away from the generator the next time a
  bracket or dash rule is revised, and e2e mode would then be
  simulating something that no longer exists. `text_pipeline` is
  stdlib-only and reads no settings, so it imports cleanly into this
  tool's small venv. `run_audiobook.py` deliberately is NOT imported:
  it builds module-level globals out of the generator's live
  `settings.json`, so importing it would drag a half-edited book preset
  into an audition. Its silence and concat logic is reimplemented in
  `audition.py` instead — short, and not the part that has to stay in
  step.
- **The engine stops after the stitch and never encodes.** An audition
  is heard once and deleted: nothing is streamed, tagged or published,
  so AAC would only buy file size that is thrown away minutes later. It
  also keeps playback inside the GUI, because `winsound` plays WAV and
  nothing else. `aac_bitrate` and the watermark are therefore absent by
  design. Verified 2026-09-14: 0.5 + 4.48 + 0.8 + 4.56 wavs stitch to
  exactly 10.34 s, mono 48 kHz `pcm_s16le`.
- **A sample is generated once, and the fingerprint says when.**
  `sample_fingerprint()` captures everything that decides how a sample
  sounds — mode, text, speaker path AND that file's size/mtime (the
  onboarder republishes `seiyuu/list/*.speaker.safetensors` in place, so
  the path alone does not identify a voice), duration scale, trim tail,
  seed, and in e2e mode the chunk length and silences. It is written
  beside the wav as `sample_NNN.json` only AFTER the stitch succeeds, or
  a half-made sample would be reused forever. Parameters inert in the
  current mode are excluded, matching what the GUI greys out.

  The reason is not speed. Samples run on a **random seed**, so
  regenerating one yields a different take — the sample you already
  judged silently changes when you add another to the comparison. Reuse
  prevents that; `Regenerate all` is the way to ask for fresh draws.

  Added 2026-09-14 after a report that reuse “stopped working when a
  second seiyuu was introduced”. There was no reuse at all at that
  point — timestamps showed all six samples rendering back to back — and
  the impression came from the auto-scrolling log plus the worker line
  `model loaded in 14.9s - reused for all 5 chunk(s) of this sample`,
  where “reused” means across chunks, not across samples. The seiyuu was
  never the variable. Worth remembering twice over: the misleading word
  was in a log line inherited from the generator, and the fix for a
  “why did it do that” report came from file timestamps, not from
  reading the code harder.
- **The TTS is `irodori_batch.py`, run unmodified.** Its job file is a
  documented contract (see its docstring) and this tool is simply a
  second caller of it — one worker per SAMPLE, so one model load each.
  Samples run one at a time, never two workers at once; that is
  catastrophically slower on this card, as recorded below.

Cleanup deletes exactly what a session created and then walks up
through emptied folders, stopping at the configured temp root. A tool
that recursively deletes a user-supplied path on exit is one typo from
being a disaster, so `remove_paths()` never takes that shortcut.


## `chapter-repair/` — the third separate tool, and the only one that
writes to the published library

Post-publication, where the other two are pre-generation. A finished
chapter occasionally has a spot where the TTS hallucinated - a sentence
read half way, words not in the book, an ad-libbed noise - and you find
it by listening, which for a long book means after publishing. This
replaces that one chunk instead of re-rendering the chapter.

Built 2026-09-14. It imports `text_pipeline` (to recover a chunk's TTS
text from its reader-facing text) and two functions from
`seiyuu-audition/audition.py` (`write_job_file`, `run_worker`) so the
replacement comes out of the same engine as its neighbours. Separate
tool, not a mode of the audition tool, because they belong to different
phases of publication.

**Why splicing is safe at all** - measured on after-dark/chapter_001,
not assumed:

- the gaps between chunks are pure silence at the configured durations
  (112 gaps of exactly 1.000 s, 4 of 1.300 s), so a cut at a chunk
  boundary never lands mid-word;
- `sync.json` describes the timeline exactly - last `end` 2740.98
  against a file duration of 2740.980000;
- the `.srt` is a mirror of `sync.json` - 117 cues for 117 chunks with
  ZERO differing timestamps;
- **the text never changes.** A hallucination is wrong audio for correct
  text, so `sync.json`'s text, the translations and the English cues all
  stay valid. Everything after a repaired chunk shifts by ONE number.

Several chunks can be fixed in a single pass: takes are kept per chunk,
choosing one queues it, and Apply does one splice, one encode and one
backup for the whole batch. Beyond speed that buys two things - a chapter
is never left partly repaired between two applies, and the Whisper scan
survives the batch, where fixing one chunk at a time would mean
re-transcribing after every single fix.

**The `.srt` is edited in place, never re-emitted.** The published
folders carry no `.translation.json` (those stay in `AUDIOBOOK_OUTPUT`),
so there is nothing to re-emit from, and an existing `.srt` may hold
hand corrections. Times are mapped by TIME, not by cue index - which is
what keeps it correct on the two sputnik chapters whose cue count does
not match their chunk count.

**faststart is verified after every re-encode**, by reading the
top-level atom order back; a file whose `moov` landed after `mdat` is
refused rather than published. Tags are copied from the OLD FILE, not
rebuilt from the generator's `settings.json` - that now describes
whatever book was generated last, and a chapter with no title is a
regression.

### The 30-second ceiling (found 2026-09-14)

**Not every bad chunk is a hallucination.** `irodori_tts` clamps its
duration predictor to `SamplingRequest.max_seconds`, default **30.0**
(`inference_runtime.py:217`; the clamp is `latent_steps = max(min_frames,
min(max_frames, latent_steps))` around line 1314). `infer.py` does not
expose it and nothing in this repo ever set it, so **every chapter ever
rendered used a 30 s ceiling**.

Text needing longer is NOT truncated - it is crammed into 30 s and the
middle garbles. **68 chunks in the published library sit at exactly
30.00 s.** They are findable from `sync.json` alone: no Whisper, no GPU,
the whole library in about a second (`repair.duration_rows`).

**Hitting the ceiling is not itself damage.** A chunk can land on it
because the book's `duration_scale` asked for more than 30 s while still
reading at a normal rate. What garbles audio is speaking FASTER than the
book does, so ranking uses each capped chunk's characters-per-second
against ITS OWN CHAPTER's median (pace is per-book: wall ~4.0 ch/s,
yojo-senki ~5.1). Of the 68: **14 read 15%+ above their chapter's pace**,
47 sit at or below it and are probably fine.

Proven on yojo-senki/chapter_002 #97 (207 chars, published at 30.00 s and
6.90 ch/s against the chapter's 5.13), same seed, the book's real
settings (tanya, duration_scale 1.5):

| | duration | pace |
|---|---|---|
| engine default | 30.00 s (clamped) | 6.90 ch/s |
| max_seconds 50 | **41.16 s** | 5.03 ch/s |

No OOM at 41 s on the 8 GB card; 44 s to generate.

**But raising the ceiling is NOT the fix.** The 41.16 s render was judged
by ear on 2026-09-15 and was still gibberish - differently broken, not
better. The reason is in Irodori's own training configs: all ten set
`max_latent_steps: 750`, which is exactly the 30 s the inference default
allows. **The ceiling is the trained window**, not an arbitrary limit, so
a 41 s request is outside anything the model has ever seen. The
generator's chunking is not a convenience - it is what keeps every
request inside that window.

The fix is to SPLIT the chunk (`repair.split_for_repair`): cut only at
terminators - `。？……` plus `！` and `!` - with a zero-width cut, so the
pieces rejoin byte-identically. Piece length comes from the chapter's own
median chunk so they match their neighbours' texture. #97's 207
characters become five requests of 46/51/25/35/50, longest 10.3 s of
audio, rendered in ONE worker run and joined into one wav that is spliced
in like any other take.

**Nothing downstream changes**: `sync.json` keeps ONE entry for #97 with
its original 207-character text, the `.srt` keeps ONE cue, chunk and cue
counts do not move. Only audio and duration change (30.0 s -> 42.56 s
butt-joined, 43.56 s with 0.25 s gaps). The gap between pieces is the one
audible difference from a single perfect render and is an ear decision.

`max_seconds` stays as an escape hatch but is deliberately NOT pre-filled
for a capped chunk - reaching for it is the wrong instinct.

For the record, the measurement that led here (still valid, still useful
for understanding the clamp):
**`max_seconds` and `duration_scale` are a pair.** Raising the ceiling
alone does nothing if the scale is low - the predictor never asks for the
room. The first run of this test used scale 1.1 instead of the book's 1.5
and produced 30.16 s, which read as “the fix does not work”. Always
reproduce at the settings the chapter was actually rendered with; the
snapshot in the book's `AUDIOBOOK_OUTPUT` folder records them.

`irodori_batch.py` accepts an optional `max_seconds` in its job file.
**Absent, nothing changes** - the engine default applies, exactly as every
chapter so far. Only chapter-repair sets it.

Why these chunks got so long in the first place: the terminator set is
`。？……`. The yojo-senki passage uses half-width `!` six times and `！`
once, neither of which is a terminator, so 156 characters pass before the
first `。` and the run cannot be split at any chunk length. **Adding `！`
and `!` to `TERMINATOR_RE` would prevent most of these** - not yet done,
and unlike the bracket fix it CANNOT be back-applied, because chunk
boundaries are tied to rendered audio.

**Finding the bad chunk is the actual value.** Whisper transcribes the
chapter once and every chunk is scored against the text it should have
read, worst first, on two signals: a low similarity catches invented
words, and a length ratio far from 1 catches a sentence abandoned half
way or an ad-lib tacked on. It is a SHORTLIST, not a verdict - Whisper
mishears too (see the onboarder's `えへへ。`), so the window always shows
the script, the transcript and a play button together.

### FLAC masters - `F:\AUDIOBOOK-HOST-MASTER` (built 2026-09-14)

`make_masters.py` built one per published chapter: 71 of 71 verified by
comparing the decoded PCM MD5 of the `.m4a` and the FLAC, 1.1 GB of
`.m4a` to 4.83 GB of FLAC in 4.5 minutes, recorded in `_masters.json`.

**They are not lossless originals** and cannot be - the audio inside is
what the `.m4a` decodes to. What they buy is that damage stops
compounding: with a master every repair is exactly ONE encode generation
from today's audio however many repairs happen, and the replacement
chunk arrives pristine from the TTS. Repairing straight from an `.m4a`
stacks a generation every time.

A master runs 0.012 s longer than its `sync.json` total - the AAC
decoder's final-frame padding, inherited once when the master was made
from the `.m4a`. Measured across two consecutive repairs of one chapter
it was still exactly 0.012 s, because repairs splice master to master.
It does not compound and it is not drift.

`sandbox_test.py` repairs a COPY of a real chapter and checks 18 things
(earlier chunks untouched, later ones shifted by exactly the delta,
silence gaps preserved, text unchanged, `.srt` still mirroring
`sync.json`, faststart, tags, cover, backup). It needs no GPU. Run it
after touching `repair.py`.

Since 2026-09-16 `run_audiobook.py` keeps its own FLAC master beside the
`.m4a` (`keep_flac_master`, default on) - a genuinely lossless one, from
the same wavs the encoder sees. `dynamic-repair/` splices into it.

**chapter-repair is frozen (user decision 2026-09-18)** - kept for the
pre-dynamic library, not developed further. New repair work goes into
`dynamic-repair/`.

## `dynamic-repair/` — repair for dynamic-profile chapters

Built 2026-09-18. The user is re-rendering older books in dynamic mode;
those chapters are better but not flawless. Separate tool, own venv, own
`settings.json` (gitignored); it imports `chapter-repair/repair.py`
one-way for the splice, encode, tag copy, time map and Whisper scan, and
`dynamic_profile` for requests.

**What makes it simpler than chapter-repair**: in a dynamic chapter one
`sync.json` entry is ONE TTS request, and `<chapter>.render.json` records
for each (by `sync_index` - a piece that produced no audio has none) the
engine text, request (`duration_scale` or `seconds`), band, seed and gap,
plus the profile block (bands, pace targets, silences, trim tail), style
and seiyuu. So nothing is typed in: the speaker and every style's request
come from the record.

**User decisions (2026-09-18)**:
- **One folder the user fills with COPIES**; the tool parses it (green /
  red per chapter) and works IN PLACE with NO backups. Required per
  chapter: `.flac`, `.m4a`, `.sync.json`, `.render.json`; optional `.srt`
  and `.translation.json` (both re-timed); per book `readings.json`.
- **Find by Time (`7:06`, `1:07:06`) or Part (`68`)**, as the reader shows
  them. The reader's Part is `sync.json` position + 1 (`idx + 1` in the
  player's `updateChapterPill`); chapter-repair showed the 0-based index,
  one off from the reader.
- **The TTS text is editable** (dynamic only) so a name or rare kanji is
  read right; `sync.json`, `.srt` and translations keep the book's
  wording. The request stays keyed to the ORIGINAL text - band, and for
  an even-pace style the length (meaning and speaking time unchanged).
- **All six styles or a custom scale** per take.
- **`readings.json`, per book, in the folder**: never created by the
  generator; created by the first applied repair that uses a reading,
  appended after. Opening a Part pre-fills known readings; "Known
  readings" lists Parts still holding one.

**Readings rendered in** (2026-09-22): a Part's `tts_text` may already
hold readings the generator applied; `part()["readings"]` (from the
piece's `readings` in `render.json`) says which, the note marks them
"rendered with it", and "Known readings" lists only Parts whose text
still holds a word. An applied repair writes the take's readings back to
the piece. The user copies the book's `readings.json` from the output
folder into the repair folder; `load_readings`/`apply_readings` are
`dynamic_profile`'s, so both tools apply the file identically.

**`translation.json` carries its own `start`/`end` per chunk** (copied
from `sync.json`), so it must be re-timed with the `.srt` - otherwise a
later re-emit of the `.srt` would bring back the old times.

**Proven on copies (`F:\tmp\dynamic-repair-test\proof.py`, 42 checks,
two real takes and a real Whisper scan)**: audio before and after a
repaired Part sample-identical to the original, the take in the FLAC
sample-exact, every 0.7/1.0/1.5 s gap preserved, FLAC ends where
`sync.json` ends, `.srt` and `translation.json` mirror `sync.json` with
text untouched, `render.json` updated with a `repairs` history and every
other Part untouched, `readings.json` created on the first reading only,
faststart and tags kept, a second repair on top of the first. The scan
found `此方側へ` heard as `コナタ側は` - the readings use case - and one
false alarm (Whisper writes `二〇一三年` as digits).

A take's length is fixed by text x scale, so re-rendering a Part in the
style it already had gives the same length (+0.000 s) - a fresh seed
changes what is said, not how long.

## `book-profiler/` — the fourth separate tool: a recipe per book and seiyuu

Built 2026-09-16/18, merged to `main`. Pre-generation, like the
audition tool, but it answers a different question: instead of one
hand-picked `duration_scale` / chunk length for a whole book, what should
each sentence get with THIS seiyuu, and does one recipe hold for the whole
book? Its output is the profile the generator's **dynamic profile mode**
reads (see above). A window (`app.py`, see below) drives the same CLI
scripts.

Imports, all deliberate and one-way: `text_pipeline` (sentences, cut rules,
TTS text), `chapter-repair/repair.py` (`similarity()`,
`run_streaming()`), and the generator's `irodori_batch.py` as the TTS.
Own venv, own `settings.json` (gitignored). Output under a root - the
window's "profile folder", or `--work-root`, defaulting to `work_root` in
`settings.json` (`F:\tmp\book-profiler`) - as `<root>\<book>\<scope>\`,
with the profiles in `recipe\<seiyuu>\profile_*.json`.

| stage | script | GPU | what it does |
|---|---|---|---|
| 1 | `analyze.py` | no | book or chosen chapters -> sentence lengths in ENGINE characters (after `prepare_tts_text_dynamic` AND Irodori's own `normalize_text`, loaded by file path because `irodori_tts/__init__` imports torch), structure, cut table for candidate lengths, symbol inventory marked measured / judged by ear / unmeasured / removed, 7 length-step sentences per chapter |
| 2 | `irodori_batch.py` | - | per-job `duration_scale`, `seed`, `seconds` (see TTS batching) |
| 3 | `sweep.py` | yes | each step sentence at scales 1.0-1.8, seeded + random arms, 3 takes each; resumable by per-take marker; a fresh worker every 80 takes |
| 4a | `score.py` | yes | Whisper over every take (one call per step/arm folder), scored with `repair.similarity()` |
| 4b | `recipe.py` | no | per-chapter and book recipes -> `profile_*.json` + `recipe.md` |

**The recipe formula (agreed 2026-09-16).** Per length step: a scale is
clean when no take is flagged; faster = the lowest scale with only clean
scales above it, + 0.1; slower = the highest clean scale (1.8 is the top
tested); default = midway. A sentence uses the next LONGER step. A flagged
take with at most 6 wrong characters that keeps its sentence ending is a
**word slip** (`玉響` read たまひょう, `議事進行` heard `疑似信仰`) and moves
nothing; a step of 6 characters or fewer cannot show a hallucination and
is not judged. The book recipe is the cautious envelope of the chapter
recipes; book L is the shortest length where some chapter failed, or the
longest measured if none did.

**First result - yojo-senki x tanya, 6 chapters (chapter_007, the
afterword, skipped), 2,268 takes.** One book recipe is viable and no
length limit was found (L = 116):

| engine chars | faster | default | slower |
|---|---|---|---|
| 1–32 | 1.5 | 1.7 | 1.8 |
| 33–44 | 1.3 | 1.6 | 1.8 |
| 45–116 | 1.4 | 1.6 | 1.8 |

The 1–32 band is set by two short sentences with hard readings
(`玉響の安息`, `左腕一本で…`), not by short sentences in general.

**What the sweep established - measured, not assumed:**

- **A take's length is fixed by text x scale.** All 6 takes of a sentence
  at a scale came out the same length to the sample, seeded or random,
  and exactly probe x scale (63 of 63 cells in chapter_001, 0 off
  prediction). The seed changes what is spoken, not how long it lasts. So
  one probe per step predicts every scale, and the repeats exist only for
  hallucination, which IS random.
- **Hallucination comes from reading too FAST.** Every chapter's flags sat
  at 1.0-1.3; none anywhere at 1.4 or above.
- **Pace predicts it better than length.** Flag rate by engine characters
  per second over all judged takes: 0.3% at 5.0, 2.1% at 6.0, 9.1% at 6.5,
  20% at 7.0, 53% at 7.5, 83% at 8.0, 100% at 9.0. Each sentence has its
  own pace at 1.0 (6.3-9.2 ch/s): chapter_003's 32-character step reads at
  9.2 and needed 1.4, where an 85-character one was clean from 1.1. Under
  the scale recipe, "default" therefore spans 3.7-6.2 ch/s. A pace-targeted
  recipe through `SamplingRequest.seconds` is being compared by ear in the
  listening test; not decided.
- **Seeds make no difference to hallucination**: seeded 25 vs random 19
  flagged in chapter_001, mean similarity 0.906 vs 0.905. Render with fresh
  random seeds, as always.
- **The 30 s ceiling hardly matters once line breaks end sentences**: the
  longest take in the whole sweep was under 26 s at scale 1.8.

**Still open**: how the reader groups one-sentence `sync.json` entries for
display.

### A seiyuu can fail SLOW too - narrow windows (2026-09-22)

marinka-03-calm-shonen on wall crashed `recipe.py` (`'NoneType' object is
not subscriptable` in `pace_targets`). She reads the sentence right and
then **ad-libs a tail** when given too much time (`…理解できなかった。理解
できなかった。`, `…思う。ぐげてもく。`) - clean x1.0-1.2/1.3, tails from
x1.3-1.5. The recipe assumed tanya's shape (fails only FAST, window open at
the top), found no usable scale on her first step, wrote no bands, and
`lookup()` returned None. Confirmed by ear on 16 takes, then measured.
User decisions, all built:

- **score.py**: `length_ratio_high` 1.45 -> **1.15** (a clean read
  transcribes at 1.00; 1.45 let 36 of 84 x1.5 takes through), plus
  `overrun` - characters heard after the sentence's ending (catches
  `…葉を噛む、噛む。`, +4%). Both set `ran_long`, which recipe.py never
  excuses as a word slip. 0 new flags over tanya's 1,944 takes; moeshi 2,
  both a real `ご視聴ありがとうございました` tail.
- **recipe.py** re-judges every take with `score.flag_takes()` from what
  `score.json` holds (a cleaned chapter cannot be re-scored), and the
  window is the **longest run of adjacent clean scales** (ties slower).
  faster = low edge + 0.1, slower = high edge, no margin.
- **Fewer speeds, profile-wide**: a speed stays only if it is >= 0.1 from
  default in every band; Natural and Even pace alike. Profile **v3**
  `available_speeds`; dropped speeds written equal to default. The
  generator (panel, Save & Run, Customize, `resolve_dynamic_plan`),
  dynamic-repair and the listening test offer only what is available; v2
  profiles read as all six. `render.json`'s profile block records it.
- **No window at all**: exit code 3 (`NO_WINDOW_EXIT`), plain message, no
  profile; the window's runner does not retry it. Chapters' windows not
  overlapping -> no `profile_book.json` (was written with `None`s).
- **Sweep now starts at x0.8** (was 1.0): marinka is clean at 1.0 on every
  step, so her fast edge was unmeasured. Resuming a swept chapter renders
  only 0.8/0.9.

Proven on copies (old `main` recipe vs new, 36 checks): all 7 tanya
profiles byte-identical in bands/L/pace targets; marinka -> default only
(x1.1-1.3 per band, L 98); **moeshi's longest step changed** - one x1.8
tail used to void the whole step, so L 76 -> 98 with a new 77-98 band
x1.1/1.4/1.7, all her other bands identical.

### Readings in the profiler (2026-09-22)

`--readings <the book's readings.json>` on every stage, and an optional
"Readings file" row in the window (remembered in `gui_state.json`; the
runner passes it to every stage and to the window-only listening test,
which re-plans its samples and would otherwise look for the wrong takes).
Applied to what the takes are rendered from (`analyze.tts_text`) and to
what Whisper is compared with (`analyze.spoken_text`, stored per row as
`spoken_text` and used by recipe.py's re-judging). Lengths and steps stay
the book's own. A step holding a reading word gets a new take fingerprint
and is **re-rendered** (user decision); every other step keeps its takes.

### The window (`app.py` + `profiler_runner.py`, 2026-09-18)

For a user who queues a book and leaves. Asks ONLY for the book folder, the
seiyuu (one for all chapters, or Customize: tick chapters, pair each with a
seiyuu) and a profile folder; every measured parameter stays fixed in
`settings.json`. The window remembers its inputs in `gui_state.json`
(gitignored). Run = analyse the whole book, then per chapter sweep + score,
then `recipe.py` per seiyuu over every scored chapter under the root.

- **It runs the CLI scripts as child processes** with `--work-root`
  (and `--arms`/`--takes` for sweep and score) - one code path, and a
  window run can be resumed or inspected from a terminal.
- **Unattended rules** (user decisions): a failing stage is retried once,
  then the chapter is skipped and the queue carries on; the PC is held
  awake (`SetThreadExecutionState`, per thread - set on the runner
  thread); a busy GPU WARNS, never refuses; Stop is `taskkill /T /F` on
  the stage, which took the TTS worker with it and returned VRAM to the
  1.35 GB desktop baseline (tested mid-sweep).
- **Seed tick box**: unticked = random x 6 takes, ticked = 3 fixed + 3
  random (tanya's method). Six takes either way, because rebuilding
  tanya's recipe from its 3 random takes alone made 3 of 6 chapters LESS
  cautious (chapter_001's shortest band x1.5/1.7 -> x1.1/1.5). The seed
  is not the variable; the number of chances to catch a hallucination is.
- **The chapter set cannot change the measurements**: each chapter's
  length steps come from its own sentences (verified: yojo-senki analysed
  with and without chapter_007 gives identical steps for 001-006). So
  the window always analyses the whole book, scope `book`.
- **"Measured" means no take marker is newer than `score.json`** - not
  "score.json newer than sweep.json": a `--report-only` sweep rewrites
  sweep.json without rendering (tanya's chapter_001 is like that).
- **Listening test is optional**, one button per profile, all six samples;
  the Results window opens as its own `listen.py --window-only` process.

**Clean up tab** - per seiyuu per book, PERMANENT (no Recycle Bin, user
decision: gigabytes piling up there is worse than no undo). Deletes only
`.wav` files of chapters that are scored AND in that seiyuu's book profile,
plus its listening-test audio; keeps markers, transcripts, `score.json`,
recipe, profiles. `cleaned.json` goes down FIRST in each chapter, and
`sweep.py`/`score.py` refuse a cleaned chapter - resume reads a missing wav
as "not rendered", so without the marker a rerun would silently re-render
it with fresh draws. Re-profiling = a fresh folder. Tested on a copy: 19
checks, every non-wav file byte-identical, recipe rebuilt from the kept
`score.json` with identical bands.

A module-path trap: `profiler_runner` puts `seiyuu-audition/` on
`sys.path`, so `import app` from anywhere that imported it finds the
AUDITION tool's `app.py`. The window runs as `__main__` and is unaffected;
a test harness must load it by file path.

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
are already fixed by the rendered audio by then, so timing is correct by
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

# Output encoding — mono AAC `.m4a` (since 2026-09-10)

The stitch (`aac_stitch_command()`) writes `chapter_<N>.m4a`: ffmpeg's
native `aac`, `-ac 1`, `-b:a <aac_bitrate>` (default 64k),
`-movflags +faststart`. The format was settled on the player side ("Phase 0"
in `F:\JPAudiobookPlayer\CLAUDE.md`, where the whole library was re-encoded)
and moved here so new books no longer need `npm run publish -- --reencode`.
That script stays in the player repo as the path for older MP3 folders.

- **The contract with the player.** Filename `chapter_<N>.m4a`; the player
  matches `^(chapter_(\d+))\.(?:mp3|m4a)$` and prefers `.m4a`, so mixed
  folders are fine. Title/artist/album and cover come out of the file's MP4
  atoms (`©nam`/`©ART`/`©alb`/`covr`) via music-metadata. A chapter with no
  cover is handled; **one with no title is a regression**, so the title is
  always written. `sync.json`, `.srt` and `.translation.json` did not change.
- **faststart is load-bearing** — the player streams from R2 with Range
  requests. mutagen's tagging afterwards keeps `moov` ahead of `mdat`
  (it grows moov in place and shifts the chunk offsets); verified with a
  185 KB cover. Anything else that rewrites the file must be re-checked:
  top-level atoms must read `ftyp, moov, …, mdat`.
- **Timing is exact through AAC — verified, not assumed.** ffmpeg records
  the 1024-sample encoder priming in the MP4 edit list (`elst` media time
  1024) and the decoder skips it. Measured on a real render and on a
  68-minute chapter built from the same concat list: stated duration equals
  the summed wavs and the old MP3's duration to 0.0 ms, and
  cross-correlating the decoded audio against the source wavs gives a lag
  of **0 samples** at the first, middle and last chunk. The decoder emits
  ~830–900 samples of final-frame padding *after* the last chunk; that is
  harmless (it only extends the tail) and is not drift.
- **Always mono; there is no channels switch.** The TTS renders mono and
  every stereo file this produced measured as dual mono (L−R at −91 dB,
  re-checked on `wall/chapter_068`, the last file made by the old path).
  If a second voice or any stereo effect is ever added, re-measure before
  keeping the downmix.
- **`aac_bitrate` is a new key on purpose.** `mp3_bitrate` / `mp3_mono` are
  never read, so a 320k from an old preset cannot silently become a 320k
  AAC; `_collect_and_validate()` drops them on the next save. Range 32–128k.
  No CBR requirement any more: MP4 seeks through its sample table, not the
  coarse 100-entry TOC that made VBR MP3 seeking miss chunk offsets.
- **An existing `.mp3` counts as a finished chapter** (`CHAPTER_AUDIO_EXTS`,
  mirrored in the GUI's chapter count). Re-rendering hours of TTS to change
  a container would be absurd. Regenerating such a chapter prints a note
  that the leftover `.mp3` no longer matches the new `sync.json`; it is not
  deleted.
- **The stitch's exit code is checked now.** Before, a failed encode still
  printed "Done!" and wrote a `sync.json` for audio that did not exist.
- `audio_metadata.py` tags both containers (MP4 on `.m4a`, ID3 on a legacy
  `.mp3`), so "Apply Tags" still works on old books, and exits non-zero if
  any file failed so the auto-tag step reports it.

# TTS batching — one model load per chapter (since 2026-09-14)

`run_audiobook.py` used to run `infer.py` once per chunk, and `infer.py`
loads the DiT checkpoint, the DACVAE codec and SilentCipher before it can
speak a word. Measured: **19–21 s per chunk, of which ~2–6 s was generation**
— the rest was re-loading the same model, hundreds of times per book.

Now `process_chapter()` writes `batch_jobs.json` into the chapter's work dir
and calls `irodori_batch.py` once; the worker loads the model (~16 s) and
loops over the chunks.

- **One worker per chapter, not per book.** A crash then costs one chapter,
  and the model leaves the card before `translate_pipeline.py` wants it for
  VNTL (they would otherwise contend for the same 8 GB).
- **The worker mirrors `infer.py` exactly**, including `resolve_cfg_scales()`
  — the `SamplingRequest` defaults alone are NOT equivalent, because the
  resolution depends on which conditions the checkpoint uses. Proven before
  the switch: same seed, same text, **byte-identical wavs** (sha256) from
  both paths, and sentence 2 generated after sentence 1 in one process
  matched a fresh process, so nothing leaks between chunks.
- **A failing chunk must not take the chapter with it.** Each chunk is
  wrapped; a failure reports `CHUNK_FAIL` (with the exception), empties the
  CUDA cache and carries on, matching what separate processes gave for free.
- **Progress must stay live.** The worker flushes every protocol line and is
  launched with `-u`; `run_batch_worker()` reads the pipe line by line and
  re-prints `Generating chunk i/N ...`, which is what
  `gui_settings._CHUNK_LINE_RE` parses. Buffer it and the progress bar
  freezes for minutes.
- **`watermark_audio` turns SilentCipher off** (GUI: Advanced -> Output
  Encoding). The worker drops the already-loaded model rather than avoiding
  its construction, because `InferenceRuntime.__init__` always builds one;
  `watermark.py` treats a missing model as "unavailable" and passes the audio
  through. It saves 60-200 ms per chunk on the GPU (600-1100 ms if the codec
  runs on CPU) and, more interestingly, removes the **48k -> 44.1k -> 48k
  resample** SilentCipher performs around the embed - its model is 44.1k
  only, which is where that `Reducing the sampling rate` warning comes from.
  Measured with a fixed seed: watermarking leaves the audio ~11 dB poorer
  above 22.05 kHz (inaudible, but it is a real round trip) and differs from
  the clean signal by -48.7 dB. **Default ON**, the engine's own default; the
  marker identifies the audio as AI-generated, so turning it off is a
  deliberate choice rather than pure optimisation.
- **Library noise goes to `batch_jobs.json.log`** beside the job file —
  SilentCipher prints two lines per chunk. The path is printed when a chunk
  fails.
- **Per-job overrides** (2026-09-16/17): a job may carry its own
  `duration_scale`, `seed` (null = fresh draw) and `seconds` (a fixed
  length in place of predictor x scale). Absent, the file-level value
  applies, so existing callers are unchanged - proven sha256-identical.
- **GPU memory is released after every job** (2026-09-17): the result is
  deleted, `gc.collect()`, `torch.cuda.empty_cache()`. In the profiler's
  sweep a long-running worker slowed from ~2 s to 13-43 s per take, and
  takes over ~14 s of audio were slow even in a fresh worker - VRAM 7.7 of
  8.2 GB with utilisation pinned, the same WDDM spill as two workers. With
  the release, ~1,500 takes up to 26 s of audio never rendered slower than
  7 s, and the audio is byte-identical. A short 5-take test did NOT show
  the slowdown at all - it builds up over a long worker, so only a long
  run can prove a fix for it.

**Judged by ear and accepted (2026-09-14).** The same 10 chunks of wall
`chapter_066` were generated both ways at the book's real settings
(ueshama, scale 1.1, chunk length 40, no seed) and listened to: no
noticeable change in speaker behaviour or audio quality, so batching is
considered a pass. Samples kept at
`F:\AUDIOBOOK_TEST\batch-vs-perchunk-20260914`. Note both stitched to
exactly the same length (108.9 s) - chunk durations are predicted from
the text, so batching does not change pacing.

Speed at real settings (wall, chunk length 40, ~10 s of audio per chunk):
**20.6 s → 6.2 s per chunk**, so a 314-chunk chapter drops from ~108 min to
~33 min. Note this is ~3.3x, not the ~9x that short test sentences suggest:
generation time scales with audio length, so the win is the fixed ~17 s
load, not a constant multiple.

## Two workers at once — tried 2026-09-14, do not repeat

Running two chapters (or two halves of one) concurrently on this card is not
"no gain", it is **catastrophically slower**. Measured with the production
worker, six chunks either way:

| | GPU util | peak VRAM | result |
|---|---|---|---|
| one worker | 80% while generating, 98% peaks | 7790 MiB of 8188 | six chunks in ~20 s |
| two workers | pegged ~100% | 7920 MiB | ONE chunk in several minutes, killed |

- **One worker already fills the card.** The checkpoint is 3.06 GB in fp32;
  with codec, SilentCipher, CUDA context and activations a single worker
  peaks at 7.8 of 8.2 GB. A second copy does not fit.
- **Windows does not OOM, it spills.** Neither worker logged an error: WDDM
  pages GPU memory out to system RAM over PCIe and everything crawls.
  `nvidia-smi` still reads ~100% utilisation throughout — **utilisation is
  not evidence of useful work**.
- **The GPU was already well fed.** CFG concatenates the batch
  (`cfg_batch_mult` in `rf.py`), so each of the 40 diffusion steps runs at an
  effective batch of 2-4, not a tiny tensor. The ~20% headroom is mostly the
  gaps between chunks.
- **Threads are excluded by design**: `InferenceRuntime._infer_lock`
  (`inference_runtime.py:620`) is taken by `synthesize()` (line 1184), so two
  threads in one process serialise anyway.
- **Real batching of different sentences is not reachable through the API**:
  `synthesize()` builds the batch as `[normalized_text] * num_candidates`
  (line 1195) — candidates of ONE sentence. Different texts would need
  changes inside Irodori (padding to a common length, attention masks,
  per-item duration prediction) and more VRAM than this card has.

The only genuine paths to parallelism are a second GPU or a smaller/quantised
checkpoint that leaves room for two copies. Neither is a code change here.

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

**A child Python writing Japanese to a PIPE dies on cp1252.** Capture a
subprocess's output and its stdout is no longer a console, so Python falls
back to the Windows locale encoding and the first Japanese character raises
`UnicodeEncodeError`. Whisper catches that per file, prints `Skipping ...`,
and the run "succeeds" having transcribed nothing. It never reproduces when
the same command is typed into a terminal, because a console takes a
different write path. Any subprocess that might print Japanese needs
`PYTHONIOENCODING=utf-8` in its environment — see
`seiyuu-onboarder/pipeline.run_streaming()`.

**Windows will not composite the off-screen part of a window.** `PrintWindow`
captures of a window taller than the screen come back black below the screen
edge. Screenshots of the scrollable Advanced page need two captures at a
screen-safe height.

**A fixed sampling seed made output *worse*.** Pinning one seed for a whole
run was tried on the theory it would stop chunks drifting against each other;
it destabilised chapters instead, because one unlucky draw then affects every
chunk rather than averaging out. The setting exists and defaults to OFF. Do
not "fix" this again.

**A bash heredoc eats doubled backslashes.** A Python script written with
`cat <<'EOF'` from the Bash tool turned `"C:\\Irodori-TTS\\seiyuu\\list\\tanya..."`
into a path where `\t` became a TAB. Write scripts that contain Windows
paths with the file tool, or use forward slashes, and assert the path
exists before spending GPU time on it.

**Stopping a background shell does not stop its children.** Killing the
`bash run_five.sh` task left `uv`, the sweep and the TTS worker running
and holding 7.7 GB of VRAM. After stopping anything long-running, list
`python`/`uv` processes and check `nvidia-smi` before starting the next
GPU job.

**Each book-profiler script reads only its own keys.** Every script's
`load_settings()` merged over the analyser's, which drops unknown keys - so
a sweep key like `batch_script` written into `settings.json` was silently
ignored. `analyze.merged_settings(defaults)` is the fix; a new script must
use it.

**A reader that stops reading deadlocks the whole chain.** In the book
profiler's first real test, the log callback raised (a cp1252 stdout could
not print Whisper's `█` progress bar); the read loop died, its `finally`
called `wait()`, `score.py` then blocked writing to the unread pipe and
Whisper blocked behind it - 2.5 hours of a "running" test with the GPU in
P8 and 39 of 54 transcripts written in the first 14 seconds. Anything that
pumps a child's output must keep reading whatever the callback does, and
kill the tree if the loop exits early (`profiler_runner.Run._stage`).
"Running for hours" with an idle GPU is a hang, not slowness - check file
timestamps before believing a progress estimate.

**`chapter-repair/sandbox_test.py` needs `PYTHONUTF8=1` from a piped
shell.** It prints `→` and decodes ffprobe's Japanese tags with the locale
encoding; with the variable set it passes 33 of 33.

---

---

# Text, chunking and hallucination — one map for tuning sessions

Added 2026-09-16 as the entry point for further work on the text logic.
Nothing here is new information; it collects what is already scattered
through this file (the `chapter-repair` sections, the chunking overhaul,
`seiyuu-audition`, the TTS batching notes) into the order a tuning
session needs it, and says what is still open.

## The one path text takes

```
<chapter>.txt  (UTF-8, 20 of 82 files carry a BOM)
  -> split_sections()      blank-line split
  -> split_paragraphs()    strip invisibles, ── run -> ─, whitespace
  -> split_sentences()     TERMINATOR_RE:  。 ？ ……  + trailing closers
  -> merge_units()         fill to soft_limit, never past hard_limit,
                           _split_oversized() breaks what does not fit
  -> build_chunks()        per chunk: text (TTS) + display_text (reader)
                           + gap tags -> silence kind
  -> irodori_batch.py      one worker per chapter, 30 s ceiling per chunk
  -> stitch + build_sync_data()   boundaries frozen into audio forever
```

**After the stitch, chunk boundaries are permanent.** Everything the
reader, the `.srt`, the translations and `chapter-repair` do is keyed to
them. This is why text-logic changes divide cleanly into two kinds, and
why the distinction matters more than any other rule in this file:

- **text-only** (the wording inside a chunk, with the same boundaries) —
  back-applicable to the published library, as the bracket and dash fixes
  were on 2026-09-11;
- **boundary-changing** (anything touching terminators, limits, or the
  merge) — applies to future renders ONLY. Existing books keep their
  chunking until re-rendered, and their bad chunks stay a `chapter-repair`
  job. Do not offer to back-apply one of these.

## Where the knobs actually are

| Knob | Lives in | Notes |
|---|---|---|
| `max_chunk_length` | `settings.json`, GUI Advanced | = `soft_limit`; **hard limit is `soft + 30`**, hardcoded at `run_audiobook.py:144`. Not a setting. |
| `SOFT_LIMIT` / `HARD_LIMIT` = 100 / 130 | `text_pipeline.py:165` | Defaults only — the generator always passes its own. The audition tool passes `soft`, `soft + 30` too. |
| `TERMINATOR_RE` | `text_pipeline.py:137` | `。 ？ ……` plus trailing `CLOSING_BRACKETS`. Splits AND earns a silence wav. |
| `BREAK_CHARS` | `text_pipeline.py:162` | `、！!?` — fallback cut points inside an over-long sentence. No silence, no split unless forced. |
| `CLOSING_BRACKETS` | `text_pipeline.py:134` | `」』）〉》】〟)` |
| `INVISIBLE_RE` | `text_pipeline.py:114` | BOM + zero-widths |
| silence durations | settings, three tiers | `STRUCTURAL_WEIGHTS`; `paragraph` is currently unreachable by design |
| `max_seconds` | job file only | absent = engine default 30.0. Only `chapter-repair` sets it. |

`build_chunks(raw_text, soft_limit, hard_limit)` and the keys of a chunk
dict are a **three-consumer contract**: the generator, `seiyuu-audition`
(e2e mode) and `chapter-repair` (`tts_text_for`) all call in. Changing
the signature or a key breaks two tools silently.

`book-profiler` is a fourth consumer of the module, but not of
`build_chunks()`: it uses `split_sections`, `split_lines`,
`split_sentences`, `dynamic_sentences`, `split_for_length`,
`prepare_tts_text_dynamic` and the character-class constants. Dynamic-mode
knobs:

| Knob | Lives in | Notes |
|---|---|---|
| `DYNAMIC_COMMA_AFTER` / `DYNAMIC_COMMA_BEFORE` | `text_pipeline.py` | `、」）)` / `「（(` - cut points, 0.7 s |
| `DYNAMIC_SENTENCE_AFTER` | `text_pipeline.py` | `！!?` - cut points, 1.0 s |
| `DYNAMIC_MIN_PIECE` | `text_pipeline.py` | 5 engine chars |
| `DYNAMIC_STRIP_RE`, `KANJI_YEAR_RE`, `KANJI_ZERO_NUMBER_RE` | `text_pipeline.py` | `×`; kanji years and 〇-numbers to digits |
| silences | profile JSON | section 1.5 / sentence 1.0 / comma 0.7 |
| recipe bands, comfortable length L | profile JSON | from `book-profiler/recipe.py` |

## What is settled by measurement — do not re-litigate

- **30 s is the trained window, not a limit to raise.** All ten Irodori
  training configs set `max_latent_steps: 750`. A 41 s render was made and
  judged by ear: still gibberish. Splitting is the only fix.
- **`！` is an intonation cue, not a pause.** It SHORTENS the natural gap
  (0.36 s vs a 0.59-0.81 s baseline, two seeds). Never promote it to a
  terminator; as a break char it costs nothing. Full table above under
  "Chunking overhaul".
- **`─` is deleted by Irodori's own normalizer**, which is why the
  `─` -> `、` rule is load-bearing rather than cosmetic.
- **A fixed seed makes output worse.** Tried, reverted, defaults OFF.
- **Two concurrent workers are catastrophically slower.** Tried, measured.
- **Batching did not change pacing** — durations are predicted per chunk
  from the text.
- **A take's length is text x scale, independent of the seed** - 2,268
  sweep takes, see `book-profiler/`.
- **Hallucination rises with pace, not length**: 0.3% of takes flagged at
  5.0 ch/s, 20% at 7.0, 83% at 8.0; nothing flagged at scale 1.4 or above
  in six chapters of yojo-senki with tanya.

## What is still open

1. **14 chunks still exceed the hard limit** after the overhaul — each has
   0 or 1 break points, i.e. a genuinely unsplittable sentence. None
   reaches the 30 s ceiling, so this is currently cosmetic. A finer break
   set (particles? `て`/`が` clause ends?) is the obvious next experiment
   and is **unmeasured** — treat any such rule as a hypothesis until it
   has a pause probe behind it.
2. **`＊` censored names** (`Ｍ＊＊くん`, 58 lines in the library) make the
   model stumble — it renders TWO silences. What the text should say
   instead is a content decision, not a code one, so it was left alone.
3. **Hallucination prevention now exists as dynamic profile mode**:
   `book-profiler/` measures, per seiyuu, which scale (or pace) keeps each
   sentence length clean, and the generator's dynamic mode applies it at
   render time. Much reduced by ear, not eliminated - repair
   (`dynamic-repair/`) is still needed.
4. **Detection is a shortlist, never a verdict.** Whisper mishears (see the
   onboarder's `えへへ。`), so the UI always shows script, transcript and a
   play button together. Do not add an "auto-repair the worst N" button.
5. **The library is split in two eras.** 71 published chapters carry
   pre-overhaul chunking; anything rendered from 2026-09-16 chunks the new
   way. Comparisons across that line are not like for like.
6. **Per-piece re-roll** in `chapter-repair` — when a split candidate has
   one bad piece, the data to re-roll just that piece is already on disk;
   there is no UI for it.

## The two detectors, and what they cost

| Detector | Input | Cost | Finds |
|---|---|---|---|
| `duration_rows()` / `capped_rows()` | `sync.json` only | ~1 s for the whole library, no GPU | chunks pinned at 30.00 s, ranked by pace against their OWN chapter's median (`chapter_pace`) |
| `transcribe_chapter()` + `score_chapter()` | Whisper over the `.m4a` | one pass per chapter, GPU | invented words (low similarity) and abandoned/ad-libbed reads (length ratio far from 1) |

Pace is per-book (wall ~4.0 ch/s, yojo-senki ~5.1), which is why the
ranking is relative and not an absolute threshold. Of the 68 capped
chunks, 14 read 15%+ above their chapter's pace; the other 47 are
probably fine.

## How to prove a text change before shipping it

The overhaul's bar, and the one to hold to:

1. **Zero text drift.** Concatenate every `display_text` across all 82
   chapter files before and after. It must be identical (BOM removal
   aside). This is what makes a text change safe to reason about.
2. **Count what moved**: chunks over the hard limit, chunks that would hit
   the ceiling, longest chunk, chunks opening on a closer, total chunks.
3. **Measure, do not assume, what the model does with a symbol.** The
   probe that settled the pause table: one carrier sentence that is a FLAT
   NOUN LIST with no natural clause break at the insertion point, the
   symbol inserted mid-list, silence measured with ffmpeg
   `silencedetect`, and **two independent seeds** that must agree. v1 of
   this probe used a carrier with a real clause break and its 1.20 s
   baseline gap swamped the signal.
4. `chapter-repair/sandbox_test.py` after touching `repair.py` — 33 checks
   on a copy of a real chapter, no GPU.
5. Audition e2e mode is the fastest way to HEAR a chunking change at real
   settings without rendering a chapter.

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
| Output since 2026-09-10 | mono AAC `.m4a`, 64k target, ~57 kbps actual (native encoder, speech with silences) |
| 68-min chapter, same concat list | 30.1 MB `.m4a` (incl. 185 KB cover) vs 49.1 MB old 96k stereo MP3 |
| TTS per chunk, one infer.py each (old) | 19–21 s, of which ~2–6 s generation |
| TTS per chunk, batched worker | ~6.2 s at chunk length 40, plus ~16 s model load per chapter |
| TTS output | 48 kHz, mono, 16-bit PCM (fixed; not configurable) |
| TTS per take, memory released per job (2026-09-17) | ~2 s at 8 s of audio, ~4-5 s at 16-20 s, ~6.5 s at 25 s |
| Profiler sweep, one chapter | 378 takes, ~18 min render |
| Whisper `large-v3-turbo` scan, one chapter | 378 takes in 14 calls, ~6-10 min |

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
