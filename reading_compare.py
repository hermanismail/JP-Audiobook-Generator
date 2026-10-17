"""
reading_compare.py
------------------
Does a transcript SOUND like what the seiyuu was sent? (2026-10-17)

Whisper writes what it hears in its own characters: 智香 read correctly as
ちか comes back 地下 or チカ, 林檎 as リンゴ. Compared character by
character that is a fault; compared by READING it is not. Both sides are
turned into hiragana with a morphological analyser (fugashi + unidic-lite)
and must match exactly - one kana is a real fault (肯いた read うなぬいた).

* the expected side is the TTS TEXT, what the engine was actually sent
  (readings and furigana already in kana) - an analyser guesses 建原 as
  けんはら, the TTS text says たてはら;
* the chapter's own readings are applied to the TRANSCRIPT too, because
  Whisper writes a name in kanji its own way (子易 -> こやす);
* numbers are spelled out: Whisper writes 16歳, the text 十六歳.

Measured on every QA flag with a person's verdict (166 Parts, machi and
maho-shojo-ikusei, 2026-10-17): of 87 false alarms 60 read the same and
are dropped; of 79 genuine faults 77 still differ - the 2 that do not were
heard by Whisper exactly as written (judged by ear) and no text rule can
catch them. SudachiPy did slightly worse (36 left) and is larger.

Used only to DROP a flag the character rule raised, never to add one. With
fugashi missing, `available()` is False and callers keep today's rule.
"""

import re

try:
    import fugashi
    _TAGGER = fugashi.Tagger()
except Exception:                      # not installed, or no dictionary
    _TAGGER = None

_DIGITS = "〇一二三四五六七八九"
_STRIP = re.compile(r"[\s、。，．・…！？!?「」『』（）()〈〉《》【】〟〝―─ー\"'.,：:~〜]")


def available():
    return _TAGGER is not None


def kanji_number(n):
    """1923 -> 千九百二十三 (what the analyser reads as せんきゅうひゃく…)."""
    if n == 0:
        return "〇"
    out = ""
    for unit, name in ((10 ** 8, "億"), (10 ** 4, "万")):
        if n >= unit:
            out += kanji_number(n // unit) + name
            n %= unit
    for unit, name in ((1000, "千"), (100, "百"), (10, "十")):
        if n >= unit:
            q = n // unit
            out += ("" if q == 1 else _DIGITS[q]) + name
            n %= unit
    return out + (_DIGITS[n] if n else "")


def _numbers(text):
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return re.sub(r"\d{1,8}", lambda m: kanji_number(int(m.group())), text)


def _hiragana(text):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def reading(text, readings=None):
    """Hiragana reading of `text`, punctuation and long-vowel marks dropped.
    `readings` {word: kana} is applied first, longest word first."""
    text = _numbers(text or "")
    for word in sorted(readings or {}, key=len, reverse=True):
        text = text.replace(word, readings[word])
    out = []
    for w in _TAGGER(text):
        kana = getattr(w.feature, "kana", None)
        out.append(kana if kana and kana != "*" else w.surface)
    return _STRIP.sub("", _hiragana("".join(out)))


def same_reading(tts_text, heard, readings=None):
    """(match, expected kana, heard kana) - or (None, None, None) when no
    analyser is installed."""
    if not available() or not tts_text or heard is None:
        return None, None, None
    expected = reading(tts_text, readings)
    got = reading(heard, readings)
    return bool(expected) and expected == got, expected, got
