# Translation Aligner

Fits a **published English translation** (an epub) onto a finished
audiobook chapter and writes `<chapter>.en.srt` beside it, for the reader's
English mode (text style D). The VNTL `.srt` is never touched.

```
uv sync
uv run python app.py
```

1. **Book folder**: any folder with `chapter_NNN.sync.json` - a published
   folder (`F:\AUDIOBOOK-HOST-AAC\<book>`) or an output folder. Legacy and
   dynamic books both work: only `sync.json`'s text and times are used.
2. **English epub**, then **Read**.
3. **Suggest pairing** compares each chapter's opening with each epub
   chapter's and proposes first/last epub chapter per chapter. Check it;
   change it with the dropdowns. Files that split one chapter
   (`chapter003`, `003b`, `003d`) are already joined. Front matter,
   footnotes and newsletter pages are simply left unpaired.
4. **Align ticked** - about 20 s for a short chapter, ~6 min for a
   2,000-sentence one, on CPU.
5. **Review**: one row per group (`JP-EN` = how many chunks / sentences).
   Yellow rows are the ones to look at: Japanese or English with no
   partner, or a pair put back together afterwards. Red is low similarity
   and is often still right. Move boundaries with the buttons, then
   **Save + write .en.srt**.

## The `.en.srt` file

Same contract as `.srt` (UTF-8, no BOM, LF, cues found by time), except:

- a cue may span **several chunks** (start of the first, end of the last),
  because one English sentence often covers two or three Japanese ones;
- a chunk with no English has **no cue** (no U+200B placeholder);
- English with no Japanese joins the cue before it;
- the seiyuu credit is translated from `glossary.json` (`Narrated by
  <en>`), or, with no glossary entry, taken from the chapter's `.srt`.

## Where things live

- `settings.json` (optional, gitignored): `work_root`
  (`F:\tmp\translation-aligner`), `model`, `skip_cost`, `low_sim`,
  `credit_template`.
- Review state: `<work_root>\<book>\<chapter>.align.json` and
  `pairing.json` - never in the book folder.
- Model: `sentence-transformers/LaBSE`, in the HF cache on F:.
