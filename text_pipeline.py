"""
text_pipeline.py
-----------------
Text cleaning / chunking flow for JP Audiobook Generator. Runs inside the
Irodori-TTS uv venv (same as run_audiobook.py, which imports this module) -
stdlib only, no third-party dependencies, so it never needs anything
installed beyond what Irodori-TTS's venv already has.

v3 (2026-08) rewrite - motivation: v2 isolated "「」"/"（）" spans and
"──" as forced break points, always starting a new TTS chunk (and a
sentence/paragraph-level silence) at those edges. In practice this produced
way too many short chunks with silence between them, especially for short
lines of dialogue - the narration kept getting interrupted mid-flow. v3
removes forced breaks entirely: brackets and "──" are just ordinary
characters now, sentences merge across them exactly like any other
sentence boundary, and a paragraph is free to become one long compact
chunk as long as it fits the soft/hard char limits. See build_chunks()'s
docstring and prepare_tts_text() for what replaced the old mechanism.

Pipeline stages implemented here:
  1. split_sections()    - split raw chapter text on blank lines (2+ CRLF)
  2. split_paragraphs()  - a paragraph is now exactly one section's text
                            (2+ CRLF is the paragraph boundary - see
                            build_chunks() docstring for why the old
                            single-CRLF paragraph tier was dropped),
                            merged into one line and stripped of inline
                            whitespace (spaces/tabs/IDSP) ready for
                            sentence splitting.
  3. split_sentences()   - split a paragraph into sentence strings on
                            。/？/…… terminators. No more forced breaks at
                            bracket edges or "──" - see prepare_tts_text().
  4. merge_units()       - merge sentence strings into ~100-char (130 hard
                            limit) TTS input chunks: keep adding sentences
                            to a chunk while it fits, otherwise flush and
                            start a new one; if a single sentence alone
                            blows the hard limit, fall back to splitting it
                            on a "、" inside it. This is what lets several
                            short sentences collapse into one compact
                            chunk instead of each getting its own gap.
  5. prepare_tts_text()  - per-chunk text transform applied ONLY to the
                            text actually sent to the TTS engine (the
                            reader-facing sync.json text keeps the
                            original wording, brackets included, as-is):
                              - "─"/"──" (any run) -> single "、" - a
                                mid-sentence pause marker the TTS model
                                doesn't understand, but a comma reads
                                naturally in its place.
                              - every "「"/"」" is removed (2026-09 v4 -
                                Irodori-TTS turned out not to reliably
                                pause around a kept bracket even with a
                                "、" placed next to it). A bracket next to
                                an existing terminator/comma, or at the
                                very edge of the chunk, is just dropped;
                                anywhere else it becomes a single "、" -
                                see _convert_brackets().
                              - a run of 2+ terminator/comma marks
                                (。？……、) - e.g. one left stacked up by the
                                dash/bracket substitutions above, or
                                already present like "……、" - collapses to
                                just the last mark in that run. This is
                                also what reduces a chain like "」「" (now
                                two adjacent "、"s) down to one, and a
                                chain like "？」「" down to a single "、"
                                too (the "？" and the new "、" end up
                                adjacent once the "」" between them drops).
  6. build_chunks()      - runs the full pipeline end-to-end and returns
                            an ordered, flat list of chunk dicts, each
                            annotated with:
                              - section/paragraph/chunk indices (for
                                working-file naming)
                              - "text": the TTS-ready text (post
                                prepare_tts_text())
                              - "display_text": the original wording for
                                that chunk (spacing/newlines still
                                cleaned up, but dashes/punctuation
                                untouched) - this is what goes into
                                sync.json for the reader app
                              - "boundary_tags": the tag describing the
                                gap *before* this chunk - "chapter_start" /
                                "section" / "paragraph" / "sentence"
                              - "silence_units": the resolved integer
                                silence-unit count for that gap - what
                                run_audiobook.py uses to size the silence
                                inserted before each chunk
                              - "silence_kind": that count bucketed into
                                "sentence"/"paragraph"/"section", naming
                                which pre-rendered silence wav goes into
                                the concat list for that gap

Working-file naming convention (written by run_audiobook.py, not this
module - this module only computes text + structure):
    sec001.txt                 <- raw section text, untouched (for inspection)
    sec001par001.txt           <- cleaned/merged paragraph text
    sec001par001sen001.txt     <- one sentence unit
    sec001par001input001.txt   <- what actually gets sent to TTS
"""

import re

IDSP = "\u3000"  # ideographic space (full-width space)
INLINE_WHITESPACE_RE = re.compile(r"[ \t" + IDSP + r"]")

TERMINATOR_RE = re.compile(r"(。|？|……)")
COMMA = "、"

SOFT_LIMIT = 100
HARD_LIMIT = 130

# --- Silence rule ---------------------------------------------------------
# Every gap now carries exactly one structural tag (the old bracket/dash
# "forced break" content tags are gone - see module docstring). "paragraph"
# is currently unreachable (see split_paragraphs()/build_chunks() - a
# section always yields exactly one paragraph now) and is kept only so the
# old single-CRLF paragraph tier can be reinstated without redoing this
# table, in case listening tests call for it.
STRUCTURAL_WEIGHTS = {"chapter_start": 2, "section": 3, "paragraph": 2, "sentence": 1}


def silence_units_for(gap_tags):
    """Resolve a gap's tag list into the final integer silence-unit count
    for that gap."""
    return max(STRUCTURAL_WEIGHTS.get(tag, STRUCTURAL_WEIGHTS["sentence"]) for tag in gap_tags)


# --- Silence kinds -------------------------------------------------------
# run_audiobook.py renders three distinct silence files up front -
# silence_sentence.wav, silence_paragraph.wav and silence_section.wav -
# each with its own user-configurable duration set on the GUI's Advanced
# page, and the concat list references the right one by name exactly once
# per gap. This buckets the resolved unit count into one of those names:
#     1  -> "sentence"
#     2  -> "paragraph"
#     3+ -> "section"
SILENCE_KINDS = ("sentence", "paragraph", "section")


def silence_kind_for_units(units):
    """Bucket a resolved silence-unit count into one of SILENCE_KINDS."""
    if units <= 1:
        return "sentence"
    if units == 2:
        return "paragraph"
    return "section"


def silence_kind_for(gap_tags):
    """Convenience: tag list -> silence kind name, in one step."""
    return silence_kind_for_units(silence_units_for(gap_tags))


def strip_whitespace(text):
    """Remove all spaces, tabs, IDSP (U+3000), and any leftover CRLF. Kept
    as a final safety net on chunk text - split_paragraphs() already
    removes all of this earlier in the pipeline, so by the time text
    reaches here it's normally a no-op."""
    return re.sub(r"[ \t" + IDSP + r"\r\n]", "", text)


def split_sections(raw_text):
    """Section = text separated by 2+ consecutive newlines (a blank line).
    Python's text-mode file reading already normalizes \\r\\n / \\r to \\n
    (universal newlines), so run_audiobook.py's plain open(..., "r") read
    is sufficient before calling this."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    sections = re.split(r"\n{2,}", text)
    return [s.strip("\n") for s in sections if s.strip()]


def split_paragraphs(section_text):
    """A paragraph is now exactly one section's text (2+ CRLF is the
    paragraph boundary - the old single-CRLF paragraph tier is gone).
    Single CRLFs inside the section are just the author's line-wraps, not
    real paragraph breaks - joining them back together lets sentences on
    either side merge into the same TTS chunk instead of always being cut
    apart. Inline whitespace (half-width space, tab, IDSP - e.g. the
    space in "衝突？　衝突って") is stripped here too, for both the TTS and
    the reader-facing text.

    Returns a single-element list (or [] if the section is blank) so the
    section/paragraph working-file structure and indices are unchanged."""
    cleaned = INLINE_WHITESPACE_RE.sub("", section_text.replace("\n", ""))
    return [cleaned] if cleaned.strip() else []


def split_sentences(paragraph_text):
    """Split a cleaned paragraph into sentence strings on 。/？/……
    terminators, keeping the terminator attached. Falls back to treating
    any un-terminated trailing text as its own sentence. No forced breaks
    at bracket edges or "──" any more - see prepare_tts_text()."""
    parts = TERMINATOR_RE.split(paragraph_text)
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


def merge_units(units, soft_limit=SOFT_LIMIT, hard_limit=HARD_LIMIT):
    """Merges sentence strings (in order, as produced by split_sentences)
    into TTS-input-sized chunks:
      1-3. Keep adding sentences to the running buffer while the total
         stays <= 100 chars; once adding a sentence pushes the total into
         100-130, close the chunk there.
      4. If adding a sentence pushes the total past the 130 hard limit,
         look for a 、 inside *that* sentence to split on - preferring a
         split point that keeps the chunk <= 100 chars, falling back to
         any split point <= 130 chars. The remainder after the 、 starts
         the next chunk's buffer. If no usable 、 exists, let the chunk
         exceed 130 rather than cut the sentence off mid-way (confirmed
         fallback - TTS just renders it slightly faster, never
         unfinished).
    Returns a list of chunk text strings."""
    chunks = []
    buffer_text = ""

    def flush():
        nonlocal buffer_text
        if buffer_text.strip():
            chunks.append(buffer_text)
        buffer_text = ""

    for text in units:
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


# --- TTS-only text normalization ------------------------------------------
DASH_RUN_RE = re.compile("─+")
PUNCT_TOKEN_RE = re.compile(r"。|？|……|、")
PUNCT_ONLY_RE = re.compile(r"[。？……、]+")
BRACKETS = "「」"
TERMINATOR_OR_COMMA_CHARS = "。？…、"  # single-char membership check - each
                                        # "…" in "……" matches individually


def _append_punct_only(base_text, addition):
    """Appends a punctuation-only fragment onto base_text (see the fold-in
    of a standalone leftover-punctuation paragraph in build_chunks),
    skipping it if base_text already ends with that exact fragment - e.g.
    a lone "。" folded onto text already ending in "。" would otherwise
    leave a stray "。。" behind. Deliberately just an exact-suffix check
    (not the TTS-only redundant-punctuation collapse) so a genuinely
    different mark, like an ellipsis followed by this fold's "。", is kept
    intact rather than collapsed away - this also runs on the reader-facing
    display text, which should stay as close to the original wording as
    possible."""
    if addition and base_text.endswith(addition):
        return base_text
    return base_text + addition


def _collapse_redundant_punctuation(text):
    """Collapses a back-to-back run of 2+ terminator/comma tokens
    (。？……、) into just the LAST token in that run - e.g. "……、" -> "、".
    A lone "……" is a single atomic token (it doesn't match twice) and is
    left untouched."""
    out = []
    i, n = 0, len(text)
    while i < n:
        m = PUNCT_TOKEN_RE.match(text, i)
        if not m:
            out.append(text[i])
            i += 1
            continue
        last = m.group(0)
        i = m.end()
        while True:
            m2 = PUNCT_TOKEN_RE.match(text, i)
            if not m2:
                break
            last = m2.group(0)
            i = m2.end()
        out.append(last)
    return "".join(out)


def _convert_brackets(text):
    """Removes every "「"/"」" for TTS (2026-09 v4 - Irodori-TTS turned out
    not to insert a natural pause reliably around a kept bracket, even with
    a "、" placed next to it - see prepare_tts_text()'s docstring). A
    bracket touching an existing terminator/comma (。？……、) - on EITHER
    side - already has a pause cue right there and contributes nothing
    extra, so it's just dropped; same for a bracket sitting at the very
    edge of the chunk (nothing on that side to pause against - the real
    inter-chunk silence is handled separately, by run_audiobook.py's
    silence wavs). Anywhere else a bracket is replaced by a single "、",
    standing in for the pause the bracket used to visually mark. This
    naturally collapses a run like "」「" into two adjacent "、"s, which
    _collapse_redundant_punctuation() (run right after this) then reduces
    to just one - and the same goes for a chain like "？」「", where the
    dropped "」" leaves "？" and the new "、" adjacent, so that collapse
    reduces the whole run to a single "、" too."""
    n = len(text)
    out = []
    for i, ch in enumerate(text):
        if ch not in BRACKETS:
            out.append(ch)
            continue
        prev_ch = text[i - 1] if i > 0 else None
        next_ch = text[i + 1] if i + 1 < n else None
        if prev_ch is None or next_ch is None:
            continue
        if prev_ch in TERMINATOR_OR_COMMA_CHARS or next_ch in TERMINATOR_OR_COMMA_CHARS:
            continue
        out.append(COMMA)
    return "".join(out)


def prepare_tts_text(text):
    """Derives the text actually sent to the TTS engine from a chunk's
    display text. See module docstring stage 5 and _convert_brackets() for
    the rationale behind each step. The reader-facing sync.json text is NOT
    run through this - it keeps the original wording, brackets included,
    as-is."""
    text = DASH_RUN_RE.sub(COMMA, text)
    text = _convert_brackets(text)
    text = _collapse_redundant_punctuation(text)
    return text


def build_chunks(raw_text, soft_limit=SOFT_LIMIT, hard_limit=HARD_LIMIT):
    """Run the full pipeline on one chapter's raw text. soft_limit/
    hard_limit are forwarded straight to merge_units() - see run_audiobook.py,
    which derives them from the GUI's "Max Chunk Length" advanced setting
    (hard_limit = soft_limit + 30) instead of always using this module's
    100/130 defaults. Returns a flat, ordered list of dicts:
        {
          "section": int, "paragraph": int, "chunk": int,   # 1-indexed,
                                                              # chunk resets
                                                              # per paragraph
          "text": str,               # final TTS input (prepare_tts_text()
                                      # applied - dashes/redundant
                                      # punctuation normalized)
          "display_text": str,       # original wording for this chunk,
                                      # inline whitespace stripped but
                                      # otherwise untouched - for sync.json
          "boundary_tags": [str],    # the tag for the gap *before* this
                                      # chunk: "chapter_start"/"section"/
                                      # "paragraph"/"sentence"
          "silence_units": int,      # resolved silence-unit count for that
                                      # gap (see silence_units_for()) -
                                      # always >= 1, including x2 for the
                                      # very first chunk of the chapter
          "silence_kind": str,       # "sentence" / "paragraph" / "section"
                                      # - which of the three pre-rendered
                                      # silence wavs run_audiobook.py drops
                                      # into the concat list for this gap
                                      # (see silence_kind_for_units())
        }
    plus the raw section/paragraph/sentence structure (needed by
    run_audiobook.py to also write out the sec/par/sen working files for
    inspection), as a second return value:
        {
          "sections": [str, ...],                # sec001.txt content, etc.
          "paragraphs": {sec_idx: [str, ...]},    # sec001par001.txt, etc.
          "sentences": {(sec_idx, par_idx): [str, ...]},
        }

    Note on why a section always yields exactly one paragraph now: a
    paragraph's boundary (2+ CRLF) is the same rule that already defines a
    section, so splitting a section into paragraphs is a no-op by
    construction (see split_paragraphs()) - the old "section" tier is kept
    around anyway (both in code and in STRUCTURAL_WEIGHTS) since it's the
    conservative option while this chunking approach is still being
    listened to; if it holds up, the redundant paragraph tier can be
    dropped for good.
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

            merged = merge_units(units, soft_limit=soft_limit, hard_limit=hard_limit)

            for chunk_idx, display_text in enumerate(merged, start=1):
                if not display_text.strip():
                    continue

                tts_text = prepare_tts_text(display_text)
                if not tts_text.strip():
                    continue

                if chunks and PUNCT_ONLY_RE.fullmatch(display_text):
                    # A whole paragraph/section that's nothing but leftover
                    # terminator punctuation (e.g. a lone "。" used as its
                    # own one-line rhetorical beat) isn't real content on
                    # its own - fold it onto the immediately preceding
                    # chunk instead of giving it a pointless chunk (and a
                    # full section/paragraph-level silence) of its own.
                    # Left alone, this also shows up as a stray doubled
                    # "。。" once the reader app runs it into its neighbor.
                    # prev_section/prev_paragraph are deliberately NOT
                    # updated here, so whatever real content comes next
                    # still gets the structural boundary (and silence) it
                    # actually deserves.
                    chunks[-1]["text"] = _append_punct_only(chunks[-1]["text"], tts_text)
                    chunks[-1]["display_text"] = _append_punct_only(
                        chunks[-1]["display_text"], display_text)
                    continue

                if prev_section is None:
                    structural_tag = "chapter_start"
                elif sec_idx != prev_section:
                    structural_tag = "section"
                elif par_idx != prev_paragraph:
                    structural_tag = "paragraph"
                else:
                    structural_tag = "sentence"

                boundary_tags = [structural_tag]

                chunks.append({
                    "section": sec_idx,
                    "paragraph": par_idx,
                    "chunk": chunk_idx,
                    "text": tts_text,
                    "display_text": display_text,
                    "boundary_tags": boundary_tags,
                    "silence_units": silence_units_for(boundary_tags),
                    "silence_kind": silence_kind_for(boundary_tags),
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
        print(f"{chunk_filename(c)}  {c['silence_units']}x -> silence_{c['silence_kind']}.wav "
              f"[{','.join(c['boundary_tags'])}]  "
              f"({len(c['text'])} chars)  {c['text']}")
