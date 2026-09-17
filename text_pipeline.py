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
                            merged into one line, stripped of inline
                            whitespace (spaces/tabs/IDSP), and with any
                            "──" (run of "─") reduced to a single "─",
                            ready for sentence splitting.
  3. split_sentences()   - split a paragraph into sentence strings on
                            。/？/…… terminators. A closing bracket right
                            after a terminator stays with the sentence it
                            closes ("…？」" | "彼女は…"), never opens the
                            next one. No more forced breaks at bracket
                            edges or "──" - see prepare_tts_text().
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

# Invisible characters that must never reach the TTS. U+FEFF is the UTF-8
# BOM: 20 of the 82 chapter files in the library start with one, and
# open(..., encoding="utf-8") keeps it (only "utf-8-sig" strips it), so it
# was travelling into the first chunk of those chapters as the first
# character the engine saw. Neither our cleaning nor Irodori's normalizer
# removed it - str.strip() does not touch U+FEFF, which is a format
# character, not whitespace.
INVISIBLE_RE = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")

# A run of box-drawing dashes ("\u2500\u2500", the conventional Japanese double dash)
# is reduced to a single "\u2500" at paragraph-cleaning time, so both the reader
# text in sync.json and the chunk-length accounting see one character. The
# TTS text then turns that "\u2500" into a "\u3001" - see prepare_tts_text().
DASH = "\u2500"
DASH_RUN_RE = re.compile(DASH + "+")

# Closing brackets belong to the sentence they close. Without this, a split
# right after the terminator in "…から？」彼女は…" left the "」" at the start
# of the NEXT sentence - and so at the start of the next chunk and its
# sync.json text, where the reader app showed a stray "」" opening a line.
# Widened 2026-09-16 after scanning all 82 chapter files: the library also
# uses 〉(83), 】(8), 》(7) and 〟(6), none of which were listed, so the
# rule that keeps a closer with the sentence it closes did not apply to
# them. One published chunk already opens on one - yojo-senki/chapter_002
# #185 starts with "》". (Its audio is unaffected: Irodori's normalizer
# deletes 《》 outright, so that one is a text-only repair, exactly like
# the 」 fix of 2026-09-11.)
CLOSING_BRACKETS = "」』）〉》】〟)"

# A terminator plus any closing brackets straight after it, as one token.
TERMINATOR_RE = re.compile(r"((?:。|？|……)[" + re.escape(CLOSING_BRACKETS) + r"]*)")
COMMA = "、"

# Where an over-long sentence may be cut when it has no terminator.
#
# 、 has always been here. ！ ! ? were added 2026-09-16, and deliberately
# NOT as terminators, because of what the engine actually does with them.
# Measured on a carrier sentence with no grammatical break at the
# insertion point, two independent seeds agreeing:
#
#   (nothing)  0.81 / 0.59 s gap      ！  0.36 / 0.36 s  - NO pause
#   、          1.01 / 1.05 s  pauses  ？  1.16 / 1.49 s  - pauses
#   。          1.14 / 1.25 s  pauses  ……  2.03 / 2.17 s  - pauses hardest
#
# So ！ does not merely fail to cue a pause, it SHORTENS the natural gap -
# it is an intonation cue, not a break. Promoting it to a terminator would
# split there and insert a full silence wav the model never wanted, and
# "戦争が始まりました！！" would be followed by a second of nothing.
#
# As a fallback split point it costs nothing: a normal-length exclamation
# is left whole, and a 173-character run gets cut at one only because the
# alternative is the engine's 30 s ceiling and the garbling that follows.
# The ascii ? is here for the same reason it should have been a terminator
# all along - only the full-width ？ was ever listed, and the library has
# 18 of the ascii form.
BREAK_CHARS = COMMA + "！!?"
BREAK_RUN_RE = re.compile("[" + re.escape(BREAK_CHARS) + "]+")

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
    the reader-facing text. A "──" (any run of "─") becomes a single "─"
    here as well, for the same two consumers.

    Returns a single-element list (or [] if the section is blank) so the
    section/paragraph working-file structure and indices are unchanged."""
    cleaned = INLINE_WHITESPACE_RE.sub("", section_text.replace("\n", ""))
    cleaned = INVISIBLE_RE.sub("", cleaned)
    cleaned = DASH_RUN_RE.sub(DASH, cleaned)
    return [cleaned] if cleaned.strip() else []


def split_sentences(paragraph_text):
    """Split a cleaned paragraph into sentence strings on 。/？/……
    terminators, keeping the terminator attached - together with any
    closing brackets (」』）) right after it, so "…から？」彼女は…" splits
    as "…から？」" + "彼女は…", never "…から？" + "」彼女は…". Falls back
    to treating any un-terminated trailing text as its own sentence. No
    forced breaks at bracket edges or "──" any more - see
    prepare_tts_text()."""
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


def _best_break(text, soft_room, hard_room):
    """Index just past the best break point in `text`, or None.

    Prefers the LATEST point that still fits `soft_room`, falling back to
    the latest that fits `hard_room` - the same preference the comma
    search has always had, so a chunk is filled rather than cut early.

    Any closing brackets straight after the break travel with it, for the
    reason split_sentences() does the same: otherwise the next chunk opens
    on a stray "」". That can take the piece a character or two past the
    room it was measured against, which is accepted.

    A break at the very end of `text` is not a break - splitting there
    would produce an empty remainder."""
    def end_of(match):
        end = match.end()
        while end < len(text) and text[end] in CLOSING_BRACKETS:
            end += 1
        return end

    for room in (soft_room, hard_room):
        best = None
        for match in BREAK_RUN_RE.finditer(text):
            end = end_of(match)
            if end >= len(text):
                continue
            if end <= room:
                best = end
        if best is not None:
            return best
    return None


def _split_oversized(text, soft_limit, hard_limit):
    """Breaks one over-long sentence into pieces that fit, repeatedly.

    Returns at least one piece. A piece still longer than the hard limit
    means the sentence genuinely has no break point left in it - the
    documented fallback, where letting the chunk run long beats cutting a
    sentence off unfinished."""
    pieces = []
    rest = text
    while len(rest) > hard_limit:
        best = _best_break(rest, soft_limit, hard_limit)
        if best is None:
            break
        pieces.append(rest[:best])
        rest = rest[best:]
    if rest:
        pieces.append(rest)
    return pieces


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

        # The incoming sentence takes us past the hard limit.
        #
        # Before 2026-09-16 this split `text` at ONE break point and put
        # the whole remainder into the buffer without ever re-measuring
        # it, and then - because the next sentence's break search measures
        # `buffer + text[:p]`, which an oversized buffer already exceeds -
        # fell through to "accept the overflow" and glued another whole
        # sentence on top. wall/chapter_013's 129-character unit (commas
        # at 7, 16, 60, 90, 104 - five usable points) became a 17-char
        # chunk plus a 112-char remainder, which then grew to 175: the
        # worst capped chunk in the published library.
        #
        # Now the buffer closes, and whatever is left is broken down on
        # its own until every piece fits.
        if buffer_text:
            head = _best_break(text, soft_limit - len(buffer_text),
                               hard_limit - len(buffer_text))
            if head is not None:
                buffer_text += text[:head]
                rest = text[head:]
            else:
                rest = text
            flush()
        else:
            rest = text

        pieces = _split_oversized(rest, soft_limit, hard_limit)
        for piece in pieces[:-1]:
            buffer_text = piece
            flush()
        buffer_text = pieces[-1] if pieces else ""

    flush()
    return chunks


# --- TTS-only text normalization ------------------------------------------
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


def _convert_brackets(text, brackets=BRACKETS):
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
        if ch not in brackets:
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


# --- Dynamic profile mode -------------------------------------------------
#
# Everything below is ADDITIVE. build_chunks() and prepare_tts_text() - normal
# mode, and what chapter-repair uses to recover a published chunk's TTS text -
# are untouched by it. Dynamic mode (book-profiler, 2026-09-16) sends the TTS
# one sentence at a time, each with parameters picked for its length, so it
# needs a different notion of a sentence and of where a long one may be cut:
#
#   - a LINE BREAK ends a sentence. Normal mode joins an author's lines, and
#     because dialogue in these books closes with 」 and no 。, a line of
#     dialogue was welded to the narration after it: 910 of yojo-senki's
#     7,020 sentences ran across lines, including its longest.
#   - a sentence longer than the seiyuu's comfortable length is cut at a
#     break point. Where the cut lands decides the silence inserted there:
#       after 、 or a closing 」 ） )        -> "comma"    (0.7 s)
#       before an opening 「 （ (           -> "comma"    (0.7 s)
#       after ！ ! ?                       -> "sentence" (1.0 s)
#     ！ stays a break point and NOT a terminator - see BREAK_CHARS for the
#     measurement. When one cut is both ("！」"), the longer silence wins.

DYNAMIC_COMMA_AFTER = COMMA + "」）)"
DYNAMIC_SENTENCE_AFTER = "！!?"
DYNAMIC_COMMA_BEFORE = "「（("
DYNAMIC_SILENCE_KINDS = ("comma", "sentence", "section")

# Stripped before TTS, never spoken (user decision 2026-09-16: yojo-senki's
# "×××××××××××" is a redaction, not something to read).
DYNAMIC_STRIP_RE = re.compile("[×]")

# A year written in positional kanji digits - 二〇一三年, 一九二三年 - becomes
# arabic digits for the TTS (user decision 2026-09-16). Irodori's normaliser
# otherwise turns 〇 into the circle ○. Four digits only: 二三年 is "two or
# three years", not the year 23. Mixed forms with 十 (二〇十三年) are left.
KANJI_DIGITS = "〇一二三四五六七八九"
KANJI_YEAR_RE = re.compile(
    "(?<![" + KANJI_DIGITS + "十百千万])([" + KANJI_DIGITS + "]{4})(?=年)")
# Any other number written positionally with a 〇 - 高度四三〇〇, 第二〇五,
# 一二〇ミリ - goes to digits as well (user decision 2026-09-16), for the same
# reason: the engine would otherwise receive ○. Only runs CONTAINING 〇 are
# touched, so 一二三 or 九五式 keep their kanji reading, and a run touching
# 十百千万 (二〇十三年) is left, because it is not positional.
KANJI_ZERO_NUMBER_RE = re.compile(
    "(?<![" + KANJI_DIGITS + "十百千万])"
    "([" + KANJI_DIGITS + "]*〇[" + KANJI_DIGITS + "]*)"
    "(?![" + KANJI_DIGITS + "十百千万])")
DYNAMIC_BRACKETS = "「」（）()"


def _kanji_year_to_arabic(match):
    return "".join(str(KANJI_DIGITS.index(ch)) for ch in match.group(1))


def _kanji_zero_number_to_arabic(match):
    run = match.group(1)
    if len(run) < 2:
        return run
    return "".join(str(KANJI_DIGITS.index(ch)) for ch in run)


def convert_kanji_numbers(text):
    """Years first (they need no 〇), then every other positional number
    that holds a 〇. Returns the converted text."""
    text = KANJI_YEAR_RE.sub(_kanji_year_to_arabic, text)
    return KANJI_ZERO_NUMBER_RE.sub(_kanji_zero_number_to_arabic, text)


def prepare_tts_text_dynamic(text):
    """Dynamic mode's version of prepare_tts_text(). Same steps, plus:
    kanji years and 〇-numbers to arabic digits, × stripped, and parentheses treated like
    「」 - removed at an edge or next to punctuation, otherwise a 、, so the
    seiyuu voices the pause itself. The reader-facing text keeps all of it."""
    text = convert_kanji_numbers(text)
    text = DYNAMIC_STRIP_RE.sub("", text)
    text = DASH_RUN_RE.sub(COMMA, text)
    text = _convert_brackets(text, DYNAMIC_BRACKETS)
    text = _collapse_redundant_punctuation(text)
    return text


def split_lines(section_text):
    """A section's author lines, cleaned exactly as split_paragraphs()
    cleans a paragraph - inline whitespace, invisibles, ── -> ─ - but NOT
    joined. Empty lines are dropped."""
    lines = []
    for line in section_text.split("\n"):
        line = INLINE_WHITESPACE_RE.sub("", line)
        line = INVISIBLE_RE.sub("", line)
        line = DASH_RUN_RE.sub(DASH, line)
        if line.strip():
            lines.append(line)
    return lines


# Marks that may NOT open a dynamic-mode sentence. Splitting on terminators
# alone left 30 sentences across the 103 chapter files starting with one
# (2026-09-17): "しかし……、なぜ？" became "しかし……" + "、なぜ？", and the
# stray 、 made an audible glitch before なぜ; "スプートニク……？」" became
# "…" + "？」"; a long "…………" run split mid-way. A leading 、 is DROPPED
# (user decision: a sentence never opens on 、, in the reader text or the
# TTS text); any other leading mark moves to the end of the sentence before
# it on the same line, so nothing else is lost.
LEADING_MARKS_RE = re.compile(
    "^([、，,…？！?!]+[" + re.escape(CLOSING_BRACKETS) + "]*)")
DROPPED_LEADING = "、，,"


def dynamic_sentences(raw_text):
    """The chapter as dynamic mode sees it: a flat list of
        {"section", "line", "sentence", "text", "gap_before", "removed_before"}
    where a sentence ends at a terminator (TERMINATOR_RE) or at the end of
    a line, and gap_before is "chapter_start" / "section" / "sentence".

    No sentence opens on a punctuation mark - see LEADING_MARKS_RE. A leading
    、 is dropped and recorded in "removed_before" (or "removed_after" of the
    sentence before, when nothing followed it); other leading marks join the
    previous sentence on the same line.

    A unit that is nothing but punctuation is folded onto the one before
    it, as build_chunks() does, so it never becomes a silent request of its
    own. dynamic_source_text() of the result gives back every cleaned line
    in order - nothing is added, and nothing lost but the dropped 、."""
    out = []
    for sec_idx, section in enumerate(split_sections(raw_text), start=1):
        for line_idx, line in enumerate(split_lines(section), start=1):
            for sen_idx, unit in enumerate(split_sentences(line), start=1):
                removed = ""
                lead = LEADING_MARKS_RE.match(unit)
                if lead:
                    head, rest = lead.group(1), unit[len(lead.group(1)):]
                    kept = "".join(ch for ch in head if ch not in DROPPED_LEADING)
                    removed = "".join(ch for ch in head if ch in DROPPED_LEADING)
                    same_line = bool(out) and out[-1]["section"] == sec_idx \
                        and out[-1]["line"] == line_idx
                    if same_line:
                        # The marks close the sentence before; the 、 goes.
                        out[-1]["text"] += kept
                        if not rest:
                            out[-1]["removed_after"] += removed
                            continue
                        unit = rest
                    else:
                        # Nothing on this line to attach to: keep the marks,
                        # still drop the 、.
                        unit = kept + rest
                        if not unit:
                            if out:
                                out[-1]["removed_after"] += removed
                            continue
                if out and PUNCT_ONLY_RE.fullmatch(unit):
                    out[-1]["text"] += unit
                    out[-1]["removed_after"] += removed
                    continue
                if not out:
                    gap = "chapter_start"
                elif line_idx == 1 and sen_idx == 1:
                    gap = "section"
                else:
                    gap = "sentence"
                out.append({"section": sec_idx, "line": line_idx,
                            "sentence": sen_idx, "text": unit, "gap_before": gap,
                            "removed_before": removed, "removed_after": ""})
    return out


def dynamic_source_text(units):
    """What `units` were made from: every cleaned line, in order. The check
    that dynamic_sentences() lost nothing but the 、 it dropped on purpose -
    see LEADING_MARKS_RE for the one ordering assumption (a dropped 、 comes
    after any mark it shared the sentence start with, as in "……、")."""
    return "".join(u["removed_before"] + u["text"] + u["removed_after"] for u in units)


def dynamic_cut_points(text):
    """{index: silence kind} for every place `text` may be cut. An index is
    where the NEXT piece starts. Cuts at 0 or len(text) are not cuts."""
    cuts = {}

    def add(index, kind):
        if 0 < index < len(text):
            if cuts.get(index) != "sentence":
                cuts[index] = kind

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in DYNAMIC_COMMA_BEFORE:
            add(i, "comma")
        if ch in DYNAMIC_COMMA_AFTER or ch in DYNAMIC_SENTENCE_AFTER:
            # Take the whole run of marks and closers as one token, so the
            # cut never lands between "！" and its "」".
            j, strongest = i, "comma"
            while j < n and (text[j] in DYNAMIC_COMMA_AFTER or
                             text[j] in DYNAMIC_SENTENCE_AFTER or
                             text[j] in CLOSING_BRACKETS):
                if text[j] in DYNAMIC_SENTENCE_AFTER:
                    strongest = "sentence"
                j += 1
            add(j, strongest)
            i = j
            continue
        i += 1
    return cuts


DYNAMIC_MIN_PIECE = 5


def split_for_length(text, limit, measure=len, min_piece=DYNAMIC_MIN_PIECE):
    """Cuts one sentence into pieces no longer than `limit`, measured with
    `measure` (the profiler passes the length the ENGINE receives).

    Returns [(piece, gap_kind)], gap_kind being the silence BEFORE the
    piece: None for the first (it keeps the sentence's own gap), then
    "comma" or "sentence" per dynamic_cut_points().

    Chooses the whole set of cuts at once, ranked by:
      1. how far the longest piece is OVER the limit (0 when it all fits),
      2. fewest pieces - no cut, and no silence, that is not needed,
      3. the shortest longest piece - even pieces, not 40 + a stray "と。".
    A greedy "latest cut that fits" was tried first and failed exactly
    there: where nothing fitted it shaved off "だから、" and left a 93-char
    remainder, and elsewhere it stranded a lone "と。" as a request of its
    own. A piece still over the limit here means the text genuinely has no
    better set of cuts - the caller reports it.

    No piece may be shorter than `min_piece`: even the ranking above would
    otherwise cut "だから、" off a 97-char sentence to bring its overflow
    from 17 to 13 - a silence and a four-character request, for nothing."""
    if measure(text) <= limit:
        return [(text, None)]
    cuts = dynamic_cut_points(text)
    if not cuts:
        return [(text, None)]
    points = [0] + sorted(cuts) + [len(text)]
    size = {}

    def piece_len(i, j):
        if (i, j) not in size:
            size[(i, j)] = measure(text[points[i]:points[j]])
        return size[(i, j)]

    last = len(points) - 1
    # best[j][p]: smallest possible longest piece covering text[:points[j]]
    # in exactly p pieces, with the choice that achieved it.
    best = [dict() for _ in points]
    best[0][0] = (0, None)
    for j in range(1, last + 1):
        for i in range(j):
            if piece_len(i, j) < min_piece:
                continue
            for p, (longest, _prev) in best[i].items():
                candidate = max(longest, piece_len(i, j))
                current = best[j].get(p + 1)
                if current is None or candidate < current[0]:
                    best[j][p + 1] = (candidate, i)

    def rank(p):
        longest = best[last][p][0]
        return (max(0, longest - limit), p, longest)

    if not best[last]:
        return [(text, None)]
    count = min(best[last], key=rank)
    bounds, j, p = [], last, count
    while j:
        i = best[j][p][1]
        bounds.append((i, j))
        j, p = i, p - 1
    bounds.reverse()
    return [(text[points[i]:points[j]], None if i == 0 else cuts[points[i]])
            for i, j in bounds]


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
