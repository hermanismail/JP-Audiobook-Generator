"""
furigana.py
-----------
The ePub extractor can keep furigana in the chapter text as
`踝(くるぶし)` - the kanji, then its reading in half-width parens. This
module is how that reaches the engine correctly and the reader cleanly.

## Why it cannot simply go into readings.json

`readings.json` is a plain global replace. Measured on wall (71 chapters,
332 furigana, 261 distinct words): **175 of those words also appear WITHOUT
furigana**, often hundreds of times - `下(もと)` is annotated once while a
bare `下` appears 264 times. A global rule would rewrite every one of them.
Two words even take two readings in the same book (`瞬` = またた / まばた).

So a furigana reading is applied **where it is written**, and only becomes
a whole-book rule when the person says so (user decision 2026-09-24):

    always        a word carrying furigana is read as written - no question
    'once'        that is all: other occurrences are left to the seiyuu
    'book'        the other occurrences get the same reading (readings.json)
    'rejected'    the parens are stripped and the seiyuu decides

## How it survives the text pipeline

Annotations cannot stay in the text while it is split: `(` is a cut point
(DYNAMIC_COMMA_BEFORE) and `prepare_tts_text_dynamic` turns `()` into `、`,
which is how `踝(くるぶし)` reached the engine as `踝、くるぶし、` before
this module existed.

So each applied occurrence is replaced by the word plus a one-character
MARKER from the Unicode private use area. A marker is not punctuation, not
a terminator and not a cut point, so sentence boundaries come out exactly
as they would from the stripped text; `display()` drops the markers and
`spoken()` swaps the marked word for its reading. Rejected annotations are
stripped outright and leave nothing behind.
"""

import re

# Half-width parens are what the extractor writes; full-width are accepted
# because a hand-edited chapter may use them.
FURIGANA_RE = re.compile(
    r"([一-鿿々〆ヶ々〆]+)"      # kanji run
    r"[(（]([ぁ-ゟ゠-ヿーー]+)[)）]"  # (reading) in kana
)

MARKER_START = 0xE000          # private use; MARKER_START + n is occurrence n
MARKER_LIMIT = 0xF8FF
MARKER_RE = re.compile(f"[-]")


# Not every "kanji(kana)" is furigana. wall writes asides the same way -
# 夢(のようなもの) is "a dream, of sorts", and reading it as のようなもの
# would replace the word. Two signs give those away: a phrase marker, and
# far more kana than a reading of that many kanji could need.
PHRASE_RE = re.compile(r"のよう|らしき|みたい|ぐらい|くらい|といった|とか|など|ばかり")
MAX_KANA_PER_KANJI = 4


def looks_like_reading(word, reading):
    """False for a parenthetical aside dressed as furigana."""
    if PHRASE_RE.search(reading or ""):
        return False
    return len(reading or "") <= MAX_KANA_PER_KANJI * max(1, len(word or ""))


def find(text):
    """[(word, reading, start, end)] for every annotation in `text`."""
    return [(m.group(1), m.group(2), m.start(), m.end())
            for m in FURIGANA_RE.finditer(text or "")]


def pairs(text):
    """{(word, reading): count} - what the review dialog lists."""
    found = {}
    for word, reading, _s, _e in find(text):
        found[(word, reading)] = found.get((word, reading), 0) + 1
    return found


def strip(text):
    """The book's own wording: annotations removed, kanji kept. This is
    what the reader sees (user decision 2026-09-24: no ruby for now)."""
    return FURIGANA_RE.sub(lambda m: m.group(1), text or "")


SENTENCE_BREAKS = "。？！?!\n"
SENTENCE_ENDS = "。？！?!"


def sentence_around(text, start, end, limit=70):
    """(sentence, start, end) - the sentence holding text[start:end], with
    the span of the annotation inside it.

    The review window shows this so a reading can be judged in context:
    `下(もと)` means nothing on its own. Long lines are cut at `limit`
    characters either side rather than shown whole."""
    text = text or ""
    left = start
    while left > 0 and text[left - 1] not in SENTENCE_BREAKS and start - left < limit:
        left -= 1
    right = end
    while right < len(text) and text[right] not in SENTENCE_BREAKS and right - end < limit:
        right += 1
    if right < len(text) and text[right] in SENTENCE_ENDS:
        right += 1
    raw = text[left:right]
    lead = len(raw) - len(raw.lstrip())
    sentence = raw.strip()
    return sentence, start - left - lead, end - left - lead


def statistics(texts):
    """Per (word, reading): how often it carries furigana, and how often
    that word appears in the book WITHOUT any - the number that decides
    whether a whole-book rule is safe.

    `texts` is an iterable of chapter texts."""
    counts, words, examples = {}, {}, {}
    stripped = []
    for text in texts:
        stripped.append(strip(text))
        for word, reading, start, end in find(text):
            key = (word, reading)
            counts[key] = counts.get(key, 0) + 1
            words[word] = words.get(word, 0) + 1
            # where it is FIRST written, for the review window's context
            examples.setdefault(key, sentence_around(text, start, end))
    total = {}
    for word in words:
        total[word] = sum(t.count(word) for t in stripped)
    out = {}
    for (word, reading), n in counts.items():
        out[(word, reading)] = {
            "word": word, "reading": reading,
            "with_furigana": n,
            # every occurrence of the word, minus the annotated ones
            "bare": max(0, total.get(word, 0) - words.get(word, 0)),
            "readings_in_book": sorted({r for (w, r) in counts if w == word}),
            # False = probably an aside, not a reading; never applied
            # without being asked, and unticked by default.
            "is_reading": looks_like_reading(word, reading),
            # (sentence, start, end) of the first place it is written
            "example": examples.get((word, reading), ("", 0, 0)),
        }
    return out


def mark(text, applied):
    """(marked text, [(word, reading)] by marker index).

    `applied` is the set of (word, reading) pairs to speak as written;
    every other annotation is stripped. The marked text is what goes into
    text_pipeline, because a marker cannot change a sentence boundary."""
    out, spans, index = [], [], 0
    last = 0
    for word, reading, start, end in find(text or ""):
        out.append(text[last:start])
        if (word, reading) in applied and index <= (MARKER_LIMIT - MARKER_START):
            out.append(word + chr(MARKER_START + index))
            spans.append((word, reading))
            index += 1
        else:
            out.append(word)
        last = end
    out.append((text or "")[last:])
    return "".join(out), spans


def display(text, _spans=None):
    """What the reader sees: the marks removed, the book's wording left."""
    return MARKER_RE.sub("", text or "")


def spoken(text, spans):
    """What the engine is sent: each marked word replaced by its reading."""
    if not text or not spans:
        return display(text)
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        code = ord(ch)
        if MARKER_START <= code <= MARKER_LIMIT:
            index = code - MARKER_START
            if index < len(spans):
                word, reading = spans[index]
                # the word sits immediately before its marker
                if out and "".join(out).endswith(word):
                    joined = "".join(out)
                    out = [joined[:len(joined) - len(word)], reading]
                else:                       # a cut split word and marker
                    out.append(reading)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def count_marks(text):
    return len(MARKER_RE.findall(text or ""))


def applied_in(text, spans):
    """{word: reading} for the markers inside `text` - what a piece records."""
    used = {}
    for match in MARKER_RE.finditer(text or ""):
        index = ord(match.group(0)) - MARKER_START
        if index < len(spans):
            word, reading = spans[index]
            used[word] = reading
    return used
