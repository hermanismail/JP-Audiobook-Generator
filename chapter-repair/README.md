# Chapter Repair

Find the spot where the TTS hallucinated, regenerate just that chunk, and
splice it into the published chapter — without re-rendering hours of audio.

A finished chapter occasionally contains a sentence read half way, words
that are not in the book, or an ad-libbed noise. Auditioning a speaker
cannot predict these, and QA-ing a long book by ear costs more than the
render did. This tool narrows a 45-minute chapter to the handful of chunks
worth listening to, then fixes one in place.

**It rewrites published files.** Every repair backs up the `.m4a`,
`.sync.json`, `.srt` and the FLAC master first.

## Why this is safe to do at all

Four properties of what the generator writes, measured on
`after-dark/chapter_001` rather than assumed:

| | |
|---|---|
| The gaps between chunks are pure silence | 112 gaps of exactly 1.000 s, 4 of 1.300 s — nothing else |
| `sync.json` describes the timeline exactly | last `end` 2740.98, file duration 2740.980000 |
| The `.srt` mirrors `sync.json` | 117 cues for 117 chunks, **zero** differing timestamps |
| The text never changes | a hallucination is wrong audio for correct text |

So a cut at a chunk boundary never lands mid-word, and everything after a
repaired chunk shifts by exactly one number: the new audio's duration
minus the old one's. Repair several chunks at once and a time simply
carries the running sum of every repair before it.

## The five steps

1. **Chapter** — pick a book and chapter from the published library. It
   tells you the chunk count, the length, the cue count, and whether a
   FLAC master exists.
2. **Inspect** — Whisper transcribes the chapter once (minutes, cached
   afterwards) and every chunk is scored against the text it should have
   read. Chunks are ranked worst-first on two signals: a low **match**
   catches invented or wrong words, and a **length ratio** far from 1.00
   catches a sentence abandoned half way or an ad-lib tacked on.
3. **Chunk** — the suspect one: its script, what Whisper heard, and
   **Play in context**, which includes a second either side because a
   hallucination is often only obvious against the words it runs into.
4. **Regenerate** — fresh takes through the same engine, at a duration
   scale or seed of your choosing. Each take shows its length against the
   original. Play them, pick one; picking **queues** it.
5. **Apply** — everything queued, in one pass. It shows you each chunk's
   delta, how many chunks and cues move, and the chapter's new length
   before you commit.

You can also skip the scan entirely: **jump to** takes `14:32` — a
timestamp read straight off the player — or a scrap of the chunk's text.

### Fix several chunks in one go

Takes are kept **per chunk**, so working through the shortlist never
throws away what you generated for the chunk you just left. Choosing a
take queues it; the queue is listed in step 5 with each chunk's delta, and
anything can be removed before you commit. One take per chunk — picking
another replaces it.

Apply then does the lot in **one pass**: one splice (`[0,s1) + new1 +
[e1,s2) + new2 + ... + [eN,end]`), one re-encode, one backup. That matters
for two reasons beyond speed — the chapter is never left partly repaired
between two applies, and **your scan survives**, which is the real win:
rescanning after every single fix would mean minutes of Whisper each time.

### The 30-second ceiling — check this first, it is free

Before spending minutes on Whisper, press **Capped only**. It needs
`sync.json` and nothing else, so it is instant.

`irodori_tts` clamps its duration predictor to
`SamplingRequest.max_seconds`, which defaults to **30.0** — and nothing in
this stack ever set it, so every chapter ever rendered used 30 s. A chunk
whose text needs longer is **not truncated**; it is crammed into the 30 s
it was given, and the middle comes out garbled. **68 chunks in the
published library sit at exactly 30.00 s.**

Landing on the ceiling is not by itself damage. A chunk can hit it because
the book's `duration_scale` asked for more than 30 s while still reading at
a perfectly normal rate. What garbles audio is being made to speak *faster
than the book does*, so that is what the ranking uses — each capped chunk's
characters-per-second against **its own chapter's** median, measured from
that chapter's uncapped chunks. Pace is a per-book setting: wall reads at
about 4.0 ch/s, yojo-senki at 5.1.

On the library as it stands: 68 capped, of which **14 read 15% or more
above their own chapter's pace** and 47 sit at or below it. **Survey
library** prints that breakdown for every book.

Measured on `yojo-senki/chapter_002` #97 — 207 characters, published at
30.00 s and 6.90 ch/s against the chapter's 5.13 — regenerated at the
book's real settings with the same seed:

| | duration | pace |
|---|---|---|
| engine default | 30.00 s (clamped) | 6.90 ch/s |
| `max seconds` = 50 | 41.16 s | 5.03 ch/s |

**`max seconds` and `duration scale` work together.** Raising the ceiling
alone does nothing if the scale is low — the predictor never asks for the
extra room. The first attempt at this test used scale 1.1 instead of the
book's 1.5 and produced 30.16 s, which looked like the fix had failed. Set
the scale the chapter was rendered with; the settings snapshot in the
book's `AUDIOBOOK_OUTPUT` folder records it.

**Raising the ceiling is not the fix.** Tried on 2026-09-14: chunk #97 at
`max seconds` 50 rendered 41.16 s at the chapter's own pace - and still
came out as gibberish, differently broken. The reason is in the model's
own configs, where all ten set `max_latent_steps: 750`, which is exactly
the 30 s the inference default allows. **The ceiling is the trained
window.** Asking for 41 s asks for something the model has never seen.

`max seconds` survives as an escape hatch and is deliberately **not**
pre-filled, because reaching for it is the wrong instinct. Split instead.

### Splitting an over-long chunk

**Split into pieces** cuts the chunk only at terminators - `。？……` plus
`！` and `!`, which the generator does not currently treat as one and which
is precisely why these chunks form. The cut is zero-width, so the pieces
rejoin **byte-identically**: nothing is added, removed or reworded. Piece
length comes from the chapter's own median chunk, so the pieces have the
same texture as their neighbours.

Chunk #97, 207 characters, becomes five requests of 46/51/25/35/50
characters - the longest 10.3 s of audio, a third of the trained window.
All five render in **one worker run** (one model load), then join into a
single wav which is spliced in exactly as any other take.

**What downstream sees: nothing.** `sync.json` keeps one entry for #97
with its original 207-character text, the `.srt` keeps one cue with its
original English, and the chunk and cue counts do not move. Only the audio
and the duration change - 30.0 s to about 42.6 s, which is the time that
text actually needs.

The **gap** between pieces is the one audible difference from a
hypothetical perfect single render. 0 butt-joins them, which can be right
since each piece already carries its own trailing silence; 0.25 s reads as
a breath. Measured: 42.56 s butt-joined, 43.56 s at 0.25 s. That is an ear
decision, so listen to both.

Because each piece was an independent request, its wav is kept beside the
result - a single bad piece can be re-rolled without disturbing the other
four. The one-request path gives you no such handle.

### The ranking is a shortlist, not a verdict

Whisper mishears. The onboarder's notes record it inventing text that was
never spoken. A low score means *worth listening to*, never *broken* —
your ears decide, which is why every row has the script and the transcript
side by side and every chunk has a play button.

## Two rules in the code that are not negotiable

**The `.srt` is edited in place, never re-emitted.** The published folders
contain no `.translation.json` (those stay in `AUDIOBOOK_OUTPUT`), so
there is nothing to re-emit from — and an existing `.srt` may carry hand
corrections. Only the times move, and they move by **time** rather than by
cue index, which is what makes it correct on the two sputnik chapters
whose cue count does not match their chunk count.

**faststart is verified, not assumed.** After re-encoding, the top-level
atom order is read back and a file whose `moov` landed after `mdat` is
refused rather than published — the player streams these from R2 with
Range requests and would have to download the whole file first. The old
file's title, artist, album and cover are carried across the re-encode
from the **old file**, not rebuilt from the generator's `settings.json`,
which by now describes whatever book was generated last.

## Masters

`make_masters.py` builds `F:\AUDIOBOOK-HOST-MASTER\<book>\chapter_NNN.flac`
from every published `.m4a`, verifying each one by comparing the decoded
PCM MD5 of both. 71 chapters took 4.5 minutes; the record is in
`_masters.json` at the destination root.

These are **not** lossless originals — the audio inside is what the `.m4a`
decodes to, and what AAC discarded at 64k is gone. What they buy is that
damage stops compounding: with a master, every repair is exactly one
encode generation from today's audio however many times a chapter is
repaired, and the new chunk arrives pristine from the TTS. Repairing
straight from an `.m4a` stacks a generation each time.

Measured across two consecutive repairs of the same chapter: the master
runs 0.012 s longer than `sync.json`'s total, and stayed at 0.012 s after
the second repair — that is the AAC decoder's final-frame padding,
inherited once when the master was made from the `.m4a`. It does not
compound, because repairs splice master to master.

## Running it

```powershell
& "C:\JP-Audiobook-Generator\chapter-repair\Run-Repair.ps1"
```

Or once, for a desktop shortcut to pin (the icon is teal — a waveform with
one bar replaced):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\chapter-repair\Create-Shortcut.ps1"
```

## After a repair

The chapter has to be **re-uploaded to R2** before the site serves the
fixed version — the `.m4a`, the `.sync.json` and the `.srt`. The tool says
so and does not do it for you.

## sandbox_test.py

An end-to-end test on a *copy* of a real chapter — 33 checks, no GPU
needed (the "replacement" is a segment cut from elsewhere in the chapter).

It repairs one chunk and checks 18 things: earlier chunks untouched, later
ones all shifted by exactly the delta, silence gaps preserved, text
unchanged, the `.srt` still mirroring `sync.json`, faststart intact, the
title/artist/album/cover carried over, the backup complete.

Then it repairs **three chunks at once on a fresh copy**, queued
deliberately out of order, and checks 15 more — that the batch sorts
itself, that every chunk carries the running sum of the deltas before it,
that each repaired chunk takes its own new duration, that the total
matches the plan, and that queueing the same chunk twice is refused. A
batch is where this arithmetic goes wrong, so it gets its own pass.

Run it after touching anything in `repair.py`:

```
uv run python sandbox_test.py
```

## Requirements

- `ffmpeg` and `ffprobe` on PATH
- Whisper at `whisper_exe` (`C:\Transcribe\.venv\Scripts\whisper.exe`)
- `uv`, and the Irodori-TTS project — the TTS runs in *its* venv through
  the audition tool's worker
- a speaker file: point step 4 at **the same speaker the chapter was
  rendered with**. A different voice mid-chapter is worse than the
  hallucination.
