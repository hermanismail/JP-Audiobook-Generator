# Handover: English mode should prefer `<base>.en.srt` (JP Audiobook Generator → JP Audiobook Player)

Read this repo's CLAUDE.md first ("English reading mode - text style D").

## What changed on the generator side
A new tool (`translation-aligner/` in `C:\JP-Audiobook-Generator`) fits the
**published English translation** of a book onto its chapters and writes a
NEW sidecar beside each chapter: `chapter_NNN.en.srt`. The existing
`chapter_NNN.srt` (VNTL) is unchanged and still produced.

## The `.en.srt` format
Same as `.srt` - UTF-8, no BOM, LF, `HH:MM:SS,mmm`, 1-based cue numbers -
with three differences:
1. **A cue may span several `sync.json` chunks** (its start is the first
   chunk's start, its end the last chunk's end): one English sentence often
   covers two or three Japanese ones. Most cues are still one chunk.
2. **A chunk with no English has no cue** - there are gaps, and no U+200B
   placeholders. A gap can last a few chunks (e.g. footnotes read aloud at
   the end of a chapter).
3. A cue usually holds **exactly one English sentence**, so cue edges are
   real sentence edges and `splitCueBySentence()` mostly has nothing to
   estimate.

## What the reader should do
- In English mode (style D), fetch `<base>.en.srt`; if it is missing (404),
  fall back to `<base>.srt` exactly as today. Nothing else uses it: the
  bottom caption and everything outside style D keep reading `.srt`.
- Cues are already found by timestamp, so multi-chunk cues should need no
  change - please confirm `englishCueAt` and the up-front paging behave
  when a cue covers several chunks and when there is no cue at the current
  time (it should keep the current page, with the last spoken sentence
  shown as spoken).
- Publishing: `npm run publish` must upload `*.en.srt` alongside `*.srt`
  (check its file filter), and the server must serve it with the same
  content type.

## Test data
`yojo-senki` and `machi` chapter_001 have been aligned and reviewed; the
user will place their `.en.srt` files in the published folders.
