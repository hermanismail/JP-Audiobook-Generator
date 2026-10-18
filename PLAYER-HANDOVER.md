# Player handover — state of the reader, and what an English audiobook needs from the generator

Written 2026-10-02 from the player repo (`F:\JPAudiobookPlayer`,
hermanismail/JP-Audiobook-Interactive-Reader). The player's own
`CLAUDE.md` is the full record; this file is the part the generator needs.

**Purpose.** The next feature is: **while the player is in English reading
mode (text style D), switch the AUDIO from the Japanese audiobook to an
English audiobook.** The generator builds the English audio first; the
player learns to play it afterwards. So this file describes what the player
does today, which contracts must not break, and a proposed contract for the
new files, with the open decisions marked.

---

## 1. What the player is today

The live player is the **web client**: a Cloudflare Worker at
`audiobook.shamareader.online`, behind Cloudflare Access. Books live in R2;
bookmarks, resume points and preferences live in D1. The Android app is no
longer developed. There is a local Express twin that reads
`F:\AUDIOBOOK-HOST-AAC` directly, for development.

### Files the player reads from a book folder

| File | Used for |
| --- | --- |
| `chapter_NNN.m4a` | the audio (64 kbps mono AAC, `+faststart`; `.mp3` still accepted) |
| `chapter_NNN.sync.json` | Japanese chunks `{index, start, end, text}`, timed to that audio; drives vertical text, karaoke highlight and the "Part" counter |
| `chapter_NNN.srt` | VNTL AI translation, one cue per chunk, U+200B for empty; the **AI subtitle** (caption, C) |
| `chapter_NNN.en.srt` | the **published** English translation from `translation-aligner/`, timed to the **Japanese** audio; cues may span chunks, gaps have no cue |
| `chapter_NNN_img_NNN.(png\|jpg)` | zen-mode side images |
| `book.json` | `{title, author, producer}`; `producer: "riery"` enables mark-for-repair |
| `library.json` (library root) | the book list and its order |

`scripts/publish.mjs` (in the player repo, run by the suite as
`node scripts/publish.mjs --book <slug> --force`, cwd `server/`) writes
`<book>/manifest.json`. **The Worker serves only files the manifest
names.** So a new file type is invisible until `publish.mjs` learns to
record it. That is player-side work and will be done when we come back
here.

Manifest chapter entry today:
`{base, number, audio, audioType, bytes, duration, title, artist, album,
cover, coverType, sync, subtitle, subtitleEn, images}`.

### What English text does in the player now

- **Style D (English reading mode)**: the reading area shows English
  sentences as pages, built for the whole chapter up front. Each sentence
  is lit when its time comes. The source is `.en.srt` when the chapter has
  one, otherwise the AI `.srt`. A sentence followed by 2 s of silence dims
  to "spoken". Timing is per sentence, because the English is a
  translation, not a transcript.
- **T, "text from the book"**: in Japanese styles A–C, the book's English
  (`.en.srt`) sits in a box above the vertical Japanese: the sentences that
  overlap the current Part. 20% of the reading area in portrait, 30% in
  landscape. Long text shrinks to 13 px, then scrolls.
- **C, the AI caption**: the `.srt` under the reading area.
- **Subtitle source menu**: on a book with `.en.srt`, C offers the AI
  subtitle or text from the book, and the choice can be remembered.
- **In style D, tapping C leaves English** for the last Japanese style and
  turns the chosen subtitle on.
- **Remote** (`remote.html`, a phone driving a player elsewhere): it can
  show either text on its own screen.

**Everything in the player is keyed to the JAPANESE audio's timeline**:
resume position, bookmarks, the Part counter, mark-for-repair (Part
numbers are Japanese chunks), the remote's seek bar, and the `.en.srt`
cue times.

---

## 2. Contracts the generator must keep

- **Filenames**: `chapter_NNN.<ext>` with the same base for every sidecar.
  The player's chapter regex is `^(chapter_(\d+))\.(mp3|m4a)$`.
- **`.srt` / `.en.srt`**: UTF-8, **no BOM**, LF. Timed to the Japanese
  audio. Do not retime `.en.srt` to an English recording: T, style D and
  the remote all depend on it matching the Japanese.
- **`book.json`** `producer: "riery"` on every publish, `library.json`
  appended by `publish.mjs`, and the suite's rclone filter
  (`+ /*/chapter_*.m4a`, `+ /*/chapter_*.sync.json`, `+ /*/chapter_*.srt`, …).
- **Repair marks** resolve against the Japanese Parts; a repair must not
  add or remove Parts.

---

## 3. Proposed contract for the English audiobook (decisions open)

Recommendations are marked **(rec)**. Decide them in the generator
session, write the result into a reader handover the way
`translation-aligner/reader-handover.md` did, and the player will be built
to match.

### 3a. Audio file

- **`chapter_NNN.en.m4a`** (rec). This name is safe and already
  admitted:
  - it does NOT match the chapter regex above, so it cannot turn into a
    phantom chapter "chapter_001.en";
  - the existing upload filter `+ /*/chapter_*.m4a` already uploads it,
    and so does the suite's one-level-down rewrite of that rule.
- Same encoding as the Japanese: **64 kbps mono AAC, ffmpeg native,
  `-movflags +faststart`**, the same ID3/MP4 tags and cover. Leftover
  `.mp3` masters are kept out of the bucket by the filter. An English `.mp3`
  would need a new rule; emit `.m4a`.

### 3b. Its own timeline — the key design question

The English recording has its own durations, so the `.en.srt` (Japanese
time) cannot drive it. The player needs:

1. **English sentence timing on the English audio**, to light the
   sentence being spoken in style D.
2. **A mapping between the two timelines**, so that switching language
   mid-chapter lands on the same place, and resume, bookmarks, the Part
   counter and the remote keep working. They all store JAPANESE time and
   should keep doing so (rec).

Proposal (rec): **`chapter_NNN.en.sync.json`**, shaped like `sync.json`,
one entry per English sentence, timed to `chapter_NNN.en.m4a`, and each
entry carrying its anchor in the Japanese timeline:

```json
{
  "version": 1,
  "source": "chapter_001.en.srt",
  "chunks": [
    { "index": 0, "start": 0.50, "end": 3.10,
      "text": "Reader, Tanya von Degurechaff",
      "enSrtCue": 1, "jaStart": 7.34, "jaEnd": 10.90 }
  ]
}
```

- `enSrtCue` is the 1-based block number in `.en.srt`; `jaStart`/`jaEnd`
  are that cue's times. Both are given so the player never has to re-derive
  the link.
- Sentence granularity should match how the player splits `.en.srt`
  cues: on `.` (plus closing quotes or brackets after it). About 1 cue in 20
  holds two sentences. If the TTS reads per cue rather than per sentence,
  say so; the player can split by proportional time as it already does.
- The upload filter `+ /*/chapter_*.sync.json` already admits this name.
  `publish.mjs` looks up `${base}.sync.json` exactly, so `.en.sync.json`
  cannot be mistaken for the Japanese sync file.

### 3c. Questions to settle

- **Gaps.** Japanese stretches with no English (no `.en.srt` cue): what
  does the English audio do there — skip them (rec, the mapping handles
  it), or insert silence to keep the timelines parallel (simpler mapping,
  dead air)?
- **Chapter headers.** Should the English audio speak the title, author
  and reader lines that the Japanese chunks 0–2 carry? `.en.srt` has
  "Reader, Tanya von Degurechaff" as its first cue.
- **Voice.** Which English TTS, and one voice per book or per character?
  This is generator-only; the player only needs the file.
- **Completeness.** All chapters of a book or none (as with `.en.srt`),
  or per chapter? Per chapter is easy for the player (fall back to
  Japanese audio with a toast) but worth deciding.
- **Repair.** Do English-audio problems need their own marks? Today a mark
  is a Japanese Part. If yes, define a key (`enSyncIndex`?), and the D1
  table would need a column, which changes the suite contract.
- **Regeneration.** If the `.en.srt` is re-aligned, the English audio and
  `.en.sync.json` must be regenerated together. Record the `.en.srt`'s
  MD5 in `.en.sync.json` (rec, e.g. `"sourceMd5"`) so the player or
  publish can detect a stale pair.
- **Footnote markers (existing bug).** The aligner passes markers like
  "Londinium.6" through into `.en.srt`, which creates a stray sentence
  "6 If…". Strip them before generating English audio, or the TTS will
  read the numbers.

---

## 4. What the player will do afterwards (for context, not for the generator to build)

- `publish.mjs`: record `audioEn` and `syncEn` in the manifest; the Worker
  and Express serve them through new routes, and `/api/books` reports
  whether a book has them.
- Style D gains an audio switch (design to be agreed, mockup first): it
  swaps the `<audio>` source, seeks through the mapping, and lights
  sentences from `.en.sync.json`. Leaving D, or tapping C in D, returns
  to the Japanese audio at the mapped position.
- Resume, bookmarks and repair stay in Japanese time, converted on the way
  in and out.
