"""
text_pipeline.py
-----------------
Rewritten text cleaning / chunking flow for JP Audiobook Generator, per
text-cleaning-logic-spec.md (2026-08, revised 2026-08 v2). Runs inside the
Irodori-TTS uv venv (same as run_audiobook.py, which imports this module) -
stdlib only, no third-party dependencies, so it never needs anything
installed beyond what Irodori-TTS's venv already has.

Pipeline stages implemented here (see the spec doc for full rationale):
  1. split_sections()    - split raw chapter text on blank lines (2+ CRLF)
  2. split_paragraphs()  - split a section on single CRLF
  3. split_sentences()   - split a paragraph into sentence units. "「」"
                            and "（）" no longer isolate their whole span
                            (that priority rule was removed in v2 - it
                            produced unusably long dialogue chunks). Instead
                            each bracket edge, and each "──" occurrence, is
                            a *forced break point*: text is always cut
                            there, and the resulting gap carries a
                            "gap_tags" list (e.g. ["bracket_open"],
                            ["dash"]) describing why. Everything else about
                            sentence splitting (。/？/…… terminators) is
                            unchanged and applies normally on both sides of
                            a forced break.
  4. merge_units()       - merge sentence units into ~100-char (130 hard
                            limit) TTS input chunks, same soft/hard-limit +
                            "、" fallback-split logic as before. A unit
                            carrying gap_tags always starts a new chunk
                            (never merges backward across a forced break);
                            units without gap_tags merge normally.
  5. build_chunks()      - runs the full pipeline end-to-end and returns
                            an ordered, flat list of chunk dicts, each
                            annotated with:
                              - section/paragraph/chunk indices (for
                                working-file naming)
                              - "boundary_tags": the combined list of tags
                                describing the gap *before* this chunk -
                                one structural tag ("chapter_start" /
                                "section" / "paragraph" / "sentence") plus
                                any content tags ("bracket_open" /
                                "bracket_close" / "dash") from forced
                                breaks at that same gap
                              - "silence_units": the resolved integer
                                silence-unit count for that gap, per the
                                combination rule in silence_units_for()
                                below - this is exactly what
                                run_audiobook.py uses to size the silence
                                inserted before each chunk (including x2
                                before the very first chunk of a chapter).

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

DASH = "──"  # mid-sentence pause marker the TTS model ignores - forces a
             # cut + 2x silence gap, and is stripped from the TTS text
OPEN_BRACKETS = "「（"
CLOSE_BRACKETS = "」）"

TERMINATOR_RE = re.compile(r"(。|？|……)")
COMMA = "、"

SOFT_LIMIT = 100
HARD_LIMIT = 130

# --- Silence combination rule -------------------------------------------
# Structural tags are mutually exclusive (only the single highest-scoped
# one applies to a given gap - same as before). Content tags come from
# forced breaks (bracket edges / dash) and are additive with each other,
# but not with the structural baseline. The final unit count for a gap is
# MAX(structural weight, sum of content weights) - see
# text-cleaning-logic-spec.md section 5 for the worked examples this is
# based on (e.g. "」" immediately followed by "「" = 2x, not 3x; a
# paragraph that opens with "「" stays at 2x, not 3x; a "──" cut mid
# paragraph = 2x).
STRUCTURAL_WEIGHTS = {"chapter_start": 2, "section": 3, "paragraph": 2, "sentence": 1}
CONTENT_WEIGHTS = {"bracket_open": 1, "bracket_close": 1, "dash": 2}


def silence_units_for(gap_tags):
    """Resolve a gap's tag list (one structural tag + zero or more content
    tags) into the final integer silence-unit count for that gap."""
    structural = STRUCTURAL_WEIGHTS["sentence"]  # default baseline: 1
    content_sum = 0
    for tag in gap_tags:
        if tag in STRUCTURAL_WEIGHTS:
            structural = max(structural, STRUCTURAL_WEIGHTS[tag])
        elif tag in CONTENT_WEIGHTS:
            content_sum += CONTENT_WEIGHTS[tag]
    return max(structural, content_sum)


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
    """Split plain text (no forced-break characters in it) into sentence
    strings on 。/？/…… terminators, keeping the terminator attached. Falls
    back to treating any un-terminated trailing text as its own sentence."""
    parts = TERMINATOR_RE.split(text)
    sentences = []
    buf = ""
    for part in parts:
        if not part:
            continue
        buf += part
        if TERMINATOR_RE.fullmatch(part):
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)
    return sentences


def _tokenize_forced_segments(paragraph_text):
    """Split paragraph text into segments at forced-break trigger points:
    "──" (dash), and the four bracket-edge characters 「（」）. Returns an
    ordered list of {"text": str, "gap_tags": [str, ...]} where gap_tags
    describes the forced-break tag(s) for the gap immediately BEFORE this
    segment ([] for the very first segment - that gap is a plain
    paragraph-internal one, resolved by the paragraph/section boundary
    logic in build_chunks instead).

    Bracket placement: the opening bracket char stays attached to the
    segment that STARTS with it (gap goes before it); the closing bracket
    char stays attached to the segment that ENDS with it (gap goes after
    it). "──" itself is dropped entirely from both segments it separates.

    Consecutive trigger points with no text between them (e.g. "」「" with
    nothing in between) collapse into a single combined gap carrying both
    tags, rather than producing an empty in-between segment - this is what
    gives "」" immediately followed by "「" its 2x total (bracket_close +
    bracket_open) instead of losing one of the two tags."""
    n = len(paragraph_text)
    raw_segments = []  # [{"text": str, "gap_tags": [str,...]}, ...]
    pending_tags = []
    buf_start = 0
    i = 0
    while i < n:
        if paragraph_text[i:i + 2] == DASH:
            raw_segments.append({"text": paragraph_text[buf_start:i], "gap_tags": pending_tags})
            pending_tags = ["dash"]
            i += 2
            buf_start = i
            continue

        ch = paragraph_text[i]
        if ch in OPEN_BRACKETS:
            raw_segments.append({"text": paragraph_text[buf_start:i], "gap_tags": pending_tags})
            pending_tags = ["bracket_open"]
            buf_start = i  # bracket char stays in the NEW segment
            i += 1
            continue
        if ch in CLOSE_BRACKETS:
            raw_segments.append({"text": paragraph_text[buf_start:i + 1], "gap_tags": pending_tags})
            pending_tags = ["bracket_close"]
            buf_start = i + 1  # bracket char stays in the segment that just closed
            i += 1
            continue
        i += 1

    raw_segments.append({"text": paragraph_text[buf_start:], "gap_tags": pending_tags})

    # Collapse zero-length segments (back-to-back trigger points), folding
    # their gap_tags forward onto the next non-empty segment.
    segments = []
    accumulated_tags = []
    for seg in raw_segments:
        if seg["text"] == "":
            accumulated_tags.extend(seg["gap_tags"])
            continue
        segments.append({"text": seg["text"], "gap_tags": accumulated_tags + seg["gap_tags"]})
        accumulated_tags = []
    # A trailing empty segment at the very end of the paragraph (e.g. it
    # ends right on a closing bracket) has nowhere to attach its tags -
    # nothing follows, so there's no gap left to insert silence into.
    return segments


def split_sentences(paragraph_text):
    """Split a paragraph into an ordered list of sentence units:
    {"text": str, "gap_tags": [str, ...]}. gap_tags is non-empty only for
    the first sentence unit immediately after a forced break (bracket edge
    or "──"); ordinary terminator-based sentence splits within the same
    segment carry gap_tags=[] (a plain default "sentence"-level gap)."""
    units = []
    for seg in _tokenize_forced_segments(paragraph_text):
        seg_sentences = _split_narration(seg["text"])
        if not seg_sentences:
            continue
        for idx, sentence_text in enumerate(seg_sentences):
            units.append({"text": sentence_text, "gap_tags": seg["gap_tags"] if idx == 0 else []})
    return units


PUNCT_ONLY_RE = re.compile(r"[。？……、]+")


def merge_units(units, soft_limit=SOFT_LIMIT, hard_limit=HARD_LIMIT):
    """Stage 2 merge logic. Takes the sentence units for a single paragraph
    (in order, as produced by split_sentences) and returns a list of
    {"text": str, "gap_tags": [str,...]} chunks ready to become TTS input
    files. gap_tags on the OUTPUT chunk describes the gap immediately
    BEFORE that chunk.

    Rules (see spec doc section 3/5 for the full explanation):
      0. A unit carrying gap_tags (i.e. immediately after a forced break -
         bracket edge or "──") always starts a brand-new chunk; whatever
         was buffered before it is flushed first. The new chunk carries
         those gap_tags forward to whenever it eventually flushes. A unit
         that's nothing but leftover terminator punctuation AND carries no
         gap_tags of its own is folded onto the immediately preceding
         chunk instead of becoming its own 1-character chunk.
      1-3. Otherwise keep adding sentences to the running buffer while the
         total stays <= 100 chars; once adding a sentence pushes the total
         into 100-130, close the chunk there.
      4. If adding a sentence pushes the total past the 130 hard limit,
         look for a 、 inside *that* sentence to split on - preferring a
         split point that keeps the chunk <= 100 chars, falling back to
         any split point <= 130 chars. The remainder after the 、 starts
         the next chunk's buffer (with no gap_tags of its own - this is
         just a plain char-limit split, not a forced break). If no usable
         、 exists, let the chunk exceed 130 rather than cut the sentence
         off mid-way (confirmed fallback - TTS just renders it slightly
         faster, never unfinished).
    """
    chunks = []
    buffer_text = ""
    buffer_gap_tags = []

    def flush():
        nonlocal buffer_text, buffer_gap_tags
        if buffer_text.strip():
            chunks.append({"text": buffer_text, "gap_tags": buffer_gap_tags})
        buffer_text = ""
        buffer_gap_tags = []

    for unit in units:
        text = unit["text"]
        gap_tags = unit["gap_tags"]

        if gap_tags:
            # Forced break before this unit - close out whatever's
            # buffered first. buffer_gap_tags now holds these gap_tags,
            # pending whatever chunk ends up carrying them forward (either
            # this unit itself, or - if it turns out to be a pure
            # leftover terminator - whatever comes after it, per the fold
            # case just below).
            flush()
            buffer_gap_tags = gap_tags

        if buffer_text == "" and chunks and PUNCT_ONLY_RE.fullmatch(text):
            # Trailing terminator-only text (e.g. the lone "。" left over
            # right after a bracket span that itself ended the sentence) -
            # fold its character(s) onto the previous chunk instead of
            # becoming a pointless 1-character chunk of its own.
            # buffer_gap_tags (possibly just set above) stays pending and
            # gets forwarded onto whichever chunk starts next, so the
            # silence guarantee isn't lost - it just moves past this
            # meaningless leftover punctuation.
            chunks[-1]["text"] += text
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
            remainder = text[best + 1:]
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
          "text": str,               # final, whitespace-stripped TTS input
          "boundary_tags": [str,...],# combined tags for the gap *before*
                                      # this chunk - one structural tag
                                      # ("chapter_start"/"section"/
                                      # "paragraph"/"sentence") plus any
                                      # content tags ("bracket_open"/
                                      # "bracket_close"/"dash")
          "silence_units": int,      # resolved silence-unit count for that
                                      # gap (see silence_units_for()) -
                                      # always >= 1, including x2 for the
                                      # very first chunk of the chapter
        }
    plus the raw section/paragraph/sentence structure (needed by
    run_audiobook.py to also write out the sec/par/sen working files for
    inspection), as a second return value:
        {
          "sections": [str, ...],                # sec001.txt content, etc.
          "paragraphs": {sec_idx: [str, ...]},    # sec001par001.txt, etc.
          "sentences": {(sec_idx, par_idx): [{"text":, "gap_tags":}, ...]},
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

            for chunk_idx, merged_chunk in enumerate(merged, start=1):
                clean_text = strip_whitespace(merged_chunk["text"])
                if not clean_text:
                    continue

                if prev_section is None:
                    structural_tag = "chapter_start"
                elif sec_idx != prev_section:
                    structural_tag = "section"
                elif par_idx != prev_paragraph:
                    structural_tag = "paragraph"
                else:
                    structural_tag = "sentence"

                boundary_tags = [structural_tag] + merged_chunk["gap_tags"]

                chunks.append({
                    "section": sec_idx,
                    "paragraph": par_idx,
                    "chunk": chunk_idx,
                    "text": clean_text,
                    "boundary_tags": boundary_tags,
                    "silence_units": silence_units_for(boundary_tags),
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
        "翌朝、空は晴れていた──いや、本当は曇っていたのかもしれない。\n\n"
        "第二章はここから始まる。「そうか」「わかった」"
    )
    chunks, _ = build_chunks(sample)
    for c in chunks:
        print(f"{chunk_filename(c)}  {c['silence_units']}x [{','.join(c['boundary_tags'])}]  "
              f"({len(c['text'])} chars)  {c['text']}")
