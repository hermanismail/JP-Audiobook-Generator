# Seiyuu Audition

Hear a speaker read a book's text — at the settings you would actually
render it with — before committing a chapter of GPU time to the choice.

Different speaker embeddings behave differently on the same parameters.
The two that matter most are **max chunk length** and **duration scale**,
which is why both are on the generator's Advanced page in the first place.
Finding a speaker's values used to mean counting characters by hand,
writing an `infer.py` command line, inventing a filename that encoded the
parameters, then hunting the wav in Explorer with the source text open in
Notepad beside a media player. This window is all of that except the
listening.

**It is a separate tool.** It shares one file with the audiobook generator
— `text_pipeline.py`, imported so the simulation stays a simulation — and
runs the generator's `irodori_batch.py` as a subprocess. Nothing else. It
has its own venv, its own `settings.json`, and it never touches a book.

## Running it

```powershell
& "C:\JP-Audiobook-Generator\seiyuu-audition\Run-Audition.ps1"
```

Or once, for a desktop shortcut you can pin to the taskbar:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\seiyuu-audition\Create-Shortcut.ps1"
```

The icon is amber, so the generator (violet), the onboarder and this are
three distinguishable things on a taskbar.

## The two modes

| | Simple | E2E Simulation |
|---|---|---|
| What is spoken | your text, verbatim | the text after the generator's real cleaning |
| Chunking | none — one utterance | `build_chunks()` at this sample's chunk length |
| Brackets, `──`, punctuation runs | untouched | normalised exactly as a chapter would be |
| Silences | none | rendered and stitched between chunks |
| Output | one wav | the chunks concatenated into one wav |
| Use it for | "how does this voice say this line" | "how would this voice read this book" |

**One audition is one mode.** A simple sample and an e2e sample are not
comparable, so switching mode clears the samples (it asks first).

## How a comparison is set up

The **text is shared** by every sample — a voice cannot be judged against
another voice on different words. Everything in sections 2 and 3 belongs
to the **selected** sample, and those sections are its editor; the
selected card is the one with the violet border.

So the normal loop is: set up SAMPLE 1, press **Add Sample** (which clones
it), change the one parameter you are asking about, press **Generate
sample(s)**. Two cards, one difference, two play buttons.

In e2e mode the header of section 1 answers the character-counting
question live, before anything is generated:

    248 characters → 7 chunk(s) at length 40, longest 44

## A sample is generated once

Generate renders only the samples it has to. Each finished wav gets a
`sample_001.json` beside it holding a fingerprint of everything that
decided how it sounds — mode, text, speaker (including that file's size
and timestamp, so a retrained speaker invalidates), duration scale, trim
tail, seed, and in e2e mode the chunk length and the three silences.
Press Generate again and any sample whose fingerprint still matches is
reused; the others render.

Parameters that do nothing in the current mode are left out of the
fingerprint, exactly as the GUI greys them out — nudging a silence in
simple mode cannot throw away a sample it could not have affected.

This is not only about speed. Samples normally run on a **random seed**,
so regenerating one hands you a *different take* of it. Reuse is what
keeps the sample you already listened to and formed an opinion about from
changing underneath you when you add a seventh sample to the comparison.

Each card says which side it falls on before you spend anything:

    SAMPLE 3 · marinka-03-calm-shonen          on disk · will be reused
    SAMPLE 4 · moeshi-calm-01                  will generate

**Regenerate all** (beside Add Sample) ignores all of that and renders
everything — which on a random seed means genuinely fresh takes of the
same parameters.

## Results

Results open in their own window: the input text at the top, then one row
per sample with its parameters, a play button, and — in e2e mode — the
chunk breakdown as a table of number, length and text:

     #   CHARS   TEXT
     1      15   第十一章、朗読者、こうのまりか
     2      62   私は「官舎地区」と呼ばれる区域に、小さな住居を…

Where a chunk length puts its breaks, and how evenly the lengths come
out, is as much of the judgement as how it sounds — and the CHARS column
is the tally you used to do by hand.

**Nothing inside the window scrolls on its own.** Every block of text is
sized to its content, so the only scrollbar is the window's: scrolling
between samples never means scrolling inside one first.

Playback is `winsound`: it starts immediately, it has no pause, and
playing another sample stops the current one. **Stop** in the header
silences it.

## Files, and their deletion

Everything lands under the temp folder:

    <temp root>\YYYYMMDD\<seiyuu>\sample_001.wav        <- what plays
    <temp root>\YYYYMMDD\<seiyuu>\sample_001.json       <- its fingerprint
    <temp root>\YYYYMMDD\<seiyuu>\sample_001_work\      <- chunks, silences,
                                                           the job file, the
                                                           worker's log

Naming is automatic on purpose — the manual version of this had you
encoding parameters into filenames, which is what the sample cards are
for now. The work folder is visible rather than hidden so there is
something to look at when a sample sounds wrong.

**Deleting on exit is the default**, and it happens silently. It removes
exactly the files this session created, then any folder left empty — never
the temp root itself, which is a path you configured and could point
anywhere. Turn cleanup off and closing the window reminds you that the
files are still there.

## Requirements

- `uv` on PATH, and the Irodori-TTS project at `irodori_root`
  (`C:\Irodori-TTS` by default) — the TTS runs in *its* venv, not this one
- `ffmpeg` and `ffprobe` on PATH, for the silences and the stitch
- at least one speaker in `<irodori_root>\seiyuu\list`, which is what the
  Seiyuu Onboarder publishes. Anything else can be reached with **Browse**.

## Settings

`settings.json` here is written when you press Generate or Close, and
restores the editor next time — the text most of all, since auditioning a
new speaker means reading the *same* passage again.

`aac_bitrate` and the SilentCipher watermark are deliberately absent.
An audition is listened to once and deleted, so the engine stops after the
stitch and never encodes: nothing here is streamed, tagged or published,
and stopping early is also what keeps playback inside the window.
