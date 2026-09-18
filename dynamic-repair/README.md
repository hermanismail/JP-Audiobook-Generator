# Dynamic Repair

Fixes a Part of a chapter rendered in the generator's **dynamic profile
mode** - a hallucinated sentence, a misread name, a rare kanji the seiyuu
gets wrong - by regenerating just that Part and splicing it in.

Chapters from before dynamic mode are the old **Chapter Repair** tool's
job (`chapter-repair/`), which is kept as it is.

## Running it

```powershell
& "C:\JP-Audiobook-Generator\dynamic-repair\Run-DynamicRepair.ps1"
```

Or once, for a desktop shortcut you can pin to the taskbar:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\dynamic-repair\Create-Shortcut.ps1"
```

## The folder

Put **copies** of the files into one folder and point the tool at it. It
repairs IN PLACE and makes no backups - the originals are your undo.

| file | |
|---|---|
| `chapter_NNN.flac` | required - the lossless master the generator writes; the repair is spliced into it |
| `chapter_NNN.m4a` | required - rebuilt from the repaired FLAC, tags carried over, faststart checked |
| `chapter_NNN.sync.json` | required - the Parts and their times; re-timed, text untouched |
| `chapter_NNN.render.json` | required - how each Part was made (the TTS text, request, seed, silence) and the profile, style and seiyuu |
| `chapter_NNN.srt` | optional - re-timed when present, text untouched |
| `chapter_NNN.translation.json` | optional - re-timed when present, English untouched |
| `readings.json` | optional, one per book - see below |

Several chapters of one book can share the folder. Parsing shows green when
all is there, and red per chapter for anything missing - a chapter without
a `render.json` is not a dynamic chapter. The seiyuu named in `render.json`
must exist on this PC; if it changed since the chapter was rendered you get
a warning, because a retrained voice would sound different mid-chapter.
The FLAC must end where `sync.json` ends, or the tool refuses the chapter:
they would not be from the same render.

## Finding the Part

- **Time** - as the reader shows it: `7:06` or `1:07:06`.
- **Part** - the number from the reader's "Part 68/432": `68`.
- **Scan chapter** - Whisper reads the chapter back and lists the Parts
  that do not match, worst first. It is a shortlist, never a verdict:
  Whisper mishears too, so always listen. Parts under 7 characters are not
  judged.
- **Known readings** - every Part that still has a word `readings.json`
  knows a reading for.

## Regenerating

- **The text sent to the TTS is editable.** Spell a name or a hard kanji
  in kana (`玉響` → `たまゆら`). The reader keeps the book's wording:
  `sync.json`, the `.srt` and the translations do not change, only the
  audio reads the new spelling.
- **Reading: word / reads as → Replace & remember** replaces the word in
  the text and remembers it. When a repair using it is applied, it is
  written to `readings.json`.
- **Style** - any of the six (Natural or Even pace; default, slower,
  faster) or a custom scale. It opens on the style the chapter was rendered
  with. The request is always keyed to the ORIGINAL text: its band, and for
  an even-pace style its length, since a kana spelling changes the
  characters, not the meaning or the speaking time.
- **Takes** - 1 to 3 per click, one model load, each on a fresh random
  seed, as the chapter was rendered. Takes stay on disk (in the tool's
  `work_root`, never in your folder) and come back next time; an applied
  one is marked so.

## readings.json

One per book, in the folder. It does not exist until the first repair
that uses a reading, and it is appended to after that - keep it with the
book and copy it in again next time. Opening a Part pre-fills its text with
every known reading, and **Known readings** lists the Parts that still need
one. Nothing is regenerated or applied without you pressing the buttons.

## Apply

Everything queued goes in one pass: the FLAC is spliced, the `.m4a` is
encoded from it (so every repair is one encode from lossless), and every
time after the first repaired Part moves by the same amount. `render.json`
is updated with each repaired Part's new text, request and seed, and keeps
what they were before under `repairs`.

## Settings (`settings.json`, not committed)

| key | default | |
|---|---|---|
| `irodori_root` | `C:\Irodori-TTS` | the TTS venv |
| `whisper_exe`, `whisper_model` | `C:\Transcribe\...`, `large-v3-turbo` | scanning |
| `aac_bitrate` | `64k` | must match the generator's |
| `work_root` | `F:\tmp\dynamic-repair` | takes, transcripts, scratch |
| `similarity_threshold` | 0.72 | what a scan flags |
| `min_judged_chars` | 7 | Parts shorter than this are not judged |
