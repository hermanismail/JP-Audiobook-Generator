"""
text_pipeline.py
-----------------
Rewritten text cleaning / chunking flow for JP Audiobook Generator, per
text-cleaning-logic-spec.md (2026-08). Runs inside the Irodori-TTS uv venv
(same as run_audiobook.py, which imports this module) - stdlib only, no
third-party dependencies, so it never needs anything installed beyond what
Irodori-TTS's venv already has.

Pipeline stages implemented here (see the spec doc for full rationale):
  1. split_sections()    - split raw chapter text on blank lines (2+ CRLF)
  2. split_paragraphs()  - split a section on single CRLF
  3. split_sentences()   - split a paragraph into sentence units, with
                            "「」" and "（）" spans always isolated as their
                            own unit (dialogue / aside priority rule)
  4. merge_units()       - merge narration sentence units into ~100-char
                            (130 hard limit) TTS input chunks; isolated
                            units (dialogue/aside) are never merged
  5. build_chunks()      - runs the full pipeline end-to-end and returns
                            an ordered, flat list of chunk dicts annotated
                            with the section/paragraph/chunk indices used
                            for working-file naming, plus a "boundary"
                            field describing the gap *before* each chunk
                            ("section" / "paragraph" / "sentence" / None
                            for the very first chunk) - this is what
                            run_audiobook.py uses to decide how much
                            silence (1x/2x/3x) to insert before it.

Working-file naming convention (written by run_audiobook.py, not this
module - this module only computes text + structure):
    sec001.txt
    sec001par001.txt
    sec001par001sen001.txt
    sec001par001input001.txt   <- what actually gets sent to TTS
"""

import re

IDSP = "\u3000"  # ideographic space (full-width space)
WHITESPACE_RE = re.compile(r"[ \t" + IDSP + r"\r\n]")

BRACKET_RE = re.compile(r"「[^」]*」|（[^）]*）")
TERMINATOR_RE = re.compile(r"(。|？|……)")
COMMA = "、"

SOFT_LIMIT = 100
HARD_LIMIT = 130


def strip_whitespace(text):
    """Remove all spaces, tabs, IDSP (U+3000), and any leftover CRLF."""
    return WHITESPACE_RE.sub("", text)


def split_sections(raw_text):
    """Section = text separated by 2+ consecutive newlines (a blank line).
    Python's text-mode file reading already normalizes \\r\\n / \\r to \\n
    (universal newlines), so run_audiobook.py's plain open(..., "r") read
    is sufficient before calling this."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    sections = re.split(r"\n{2,}", text)
    return [s.strip("\n") for s in sections if s.strip()]


def split_paragraphs(section_text):
    """Paragraph = a single-newline-separated chunk within a section."""
    paragraphs = re.split(r"\n", section_text)
    return [p for p in paragraphs if p.strip()]


def _split_narration(text):
    """Split plain narration text (no 「」/（） in it) into sentence units
    on 。/？/…… terminators, keeping the terminator attached. Falls back to
    treating any un-terminated trailing text as its own sentence."""
    parts = TERMINATOR_RE.split(text)
    sentences = []
    buf = ""
    for part in parts:
        if not part:
            continue
        buf += part
        if TERMINATOR_RE.fullmatch(part):
            sentences.append((buf, False))
            buf = ""
    if buf.strip():
        sentences.append((buf, False))
    return sentences


def split_sentences(paragraph_text):
    """Split a paragraph into an ordered list of (text, isolated) units.
    isolated=True for 「」/（） spans (dialogue / aside priority rule -
    these never get merged with neighbors downstream). isolated=False for
    ordinary narration sentences."""
    units = []
    pos = 0
    for m in BRACKET_RE.finditer(paragraph_text):
        before = paragraph_text[pos:m.start()]
        if before.strip():
            units.extend(_split_narration(before))
        units.append((m.group(0), True))
        pos = m.end()
    tail = paragraph_text[pos:]
    if tail.strip():
        units.extend(_split_narration(tail))
    return units


PUNCT_ONLY_RE = re.compile(r"[。？……、]+")


def merge_units(units, soft_limit=SOFT_LIMIT, hard_limit=HARD_LIMIT):
    """Stage 2 merge logic. Takes the (text, isolated) units for a single
    paragraph (in order) and returns a list of (text, isolated) chunks
    ready to become TTS input files.

    Rules (see spec doc section 3 for the full explanation):
      0. An isolated (「」/（）) unit is never merged - it flushes whatever
         is currently buffered, then becomes its own standalone chunk. A
         unit that's nothing but leftover terminator punctuation (e.g. the
         stray "。" right after a 「」/（） span that itself ended the
         sentence) is folded onto the immediately preceding chunk instead
         of becoming its own 1-character chunk with its own silence gap.
      1-3. Otherwise keep adding sentences to the running buffer while the
         total stays <= 100 chars; once adding a sentence pushes the total
         into 100-130, close the chunk there.
      4. If adding a sentence pushes the total past the 130 hard limit,
         look for a 、 inside *that* sentence to split on - preferring a
         split point that keeps the chunk <= 100 chars, falling back to
         any split point <= 130 chars. The remainder after the 、 starts
         the next chunk's buffer. If no usable 、 exists, let the chunk
         exceed 130 rather than cut the sentence off mid-way (confirmed
         fallback - TTS just renders it slightly faster, never unfinished).
    """
    chunks = []
    buffer_text = ""

    def flush():
        nonlocal buffer_text
        if buffer_text.strip():
            chunks.append((buffer_text, False))
        buffer_text = ""

    for text, isolated in units:
        if isolated:
            flush()
            chunks.append((text, True))
            continue

        if buffer_text == "" and chunks and PUNCT_ONLY_RE.fullmatch(text):
            prev_text, prev_isolated = chunks[-1]
            chunks[-1] = (prev_text + text, prev_isolated)
            continue

        candidate = buffer_text + text

        if len(candidate) <= soft_limit:
            buffer_text = candidate
            continue

        if len(candidate) <= hard_limit:
            buffer_text = candidate
            flush()
            continue

        # candidate exceeds the hard limit - look for a 、 split point
        # inside the sentence that just caused the overflow.
        comma_positions = [m.start() for m in re.finditer(COMMA, text)]

        best = None
        for p in comma_positions:
            if len(buffer_text + text[: p + 1]) <= soft_limit:
                best = p  # keep the latest (largest) position under the soft limit
        if best is None:
            for p in comma_positions:
                if len(buffer_text + text[: p + 1]) <= hard_limit:
                    best = p

        if best is not None:
            buffer_text = buffer_text + text[: best + 1]
            flush()
            remainder = text[best + 1 :]
            if remainder.strip():
                buffer_text = remainder
        else:
            # No usable comma - accept the overflow rather than cut the
            # sentence off unfinished.
            buffer_text = candidate
            flush()

    flush()
    return chunks


def build_chunks(raw_text):
    """Run the full pipeline on one chapter's raw text. Returns a flat,
    ordered list of dicts:
        {
          "section": int, "paragraph": int, "chunk": int,   # 1-indexed,
                                                              # chunk resets
                                                              # per paragraph
          "text": str,        # final, whitespace-stripped TTS input text
          "isolated": bool,   # True for a 「」/（） chunk
          "boundary": str | None,  # gap *before* this chunk:
                                    # "section" / "paragraph" / "sentence" /
                                    # None for the very first chunk overall
        }
    plus the raw section/paragraph/sentence structure (needed by
    run_audiobook.py to also write out the sec/par/sen working files for
    inspection), as a second return value:
        {
          "sections": [str, ...],                # sec001.txt content, etc.
          "paragraphs": {sec_idx: [str, ...]},    # sec001par001.txt, etc.
          "sentences": {(sec_idx, par_idx): [(text, isolated), ...]},
        }
    """
    sections = split_sections(raw_text)
    paragraphs_by_section = {}
    sentences_by_paragraph = {}

    chunks = []
    prev_section = None
    prev_paragraph = None

    for sec_idx, section_text in enumerate(sections, start=1):
        paragraphs = split_paragraphs(section_text)
        paragraphs_by_section[sec_idx] = paragraphs

        for par_idx, para_text in enumerate(paragraphs, start=1):
            units = split_sentences(para_text)
            sentences_by_paragraph[(sec_idx, par_idx)] = units

            merged = merge_units(units)

            for chunk_idx, (chunk_text, isolated) in enumerate(merged, start=1):
                clean_text = strip_whitespace(chunk_text)
                if not clean_text:
                    continue

                if prev_section is None:
                    boundary = None
                elif sec_idx != prev_section:
                    boundary = "section"
                elif par_idx != prev_paragraph:
                    boundary = "paragraph"
                else:
                    boundary = "sentence"

                chunks.append({
                    "section": sec_idx,
                    "paragraph": par_idx,
                    "chunk": chunk_idx,
                    "text": clean_text,
                    "isolated": isolated,
                    "boundary": boundary,
                })

                prev_section = sec_idx
                prev_paragraph = par_idx

    working_data = {
        "sections": sections,
        "paragraphs": paragraphs_by_section,
        "sentences": sentences_by_paragraph,
    }
    return chunks, working_data


def chunk_filename(chunk, ext="txt"):
    """Naming convention: sec001par001input001.txt"""
    return (
        f"sec{chunk['section']:03d}par{chunk['paragraph']:03d}"
        f"input{chunk['chunk']:03d}.{ext}"
    )


if __name__ == "__main__":
    # Quick smoke test - run `uv run --no-sync python text_pipeline.py`
    # from C:\Irodori-TTS to sanity-check the logic against a small sample
    # without touching any real chapter files.
    sample = (
        "彼は静かに窓の外を見た。「もう、行かないと」と彼女は言った。"
        "それから二人は黙って歩き続けた、長い坂道を上りながら、"
        "何も言わずに、ただ前だけを見つめていた（この時、彼は本当は"
        "何かを言いたかったのだが、言葉が見つからなかった）。\n"
        "翌朝、空は晴れていた。\n\n"
        "第二章はここから始まる。"
    )
    chunks, _ = build_chunks(sample)
    for c in chunks:
        print(f"{chunk_filename(c)}  [{c['boundary']}]  ({len(c['text'])} chars)  {c['text']}")
