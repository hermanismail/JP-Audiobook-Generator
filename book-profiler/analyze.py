"""
analyze.py
----------
Stage 1 of the book profiler: read a book's chapter text and describe it,
with no GPU and no TTS.

    uv run --project book-profiler python book-profiler/analyze.py --book  <folder>
    uv run --project book-profiler python book-profiler/analyze.py --chapter <file> [--chapter <file> ...]

The scope is chosen up front - a whole book, or chosen chapters - because
one book can legitimately use different seiyuu for different chapters.

What it produces, into <work_root>/<book>/<scope>/analysis/:

    analysis.json   everything, for the later stages to read
    analysis.md     the same, for a person to read

## Sentences are the generator's sentences

Nothing here re-implements splitting. `split_sections`, `split_paragraphs`
and `split_sentences` are imported from `text_pipeline.py` in the folder
above - the same rule seiyuu-audition follows, for the same reason. A copy
would drift the next time a bracket or dash rule changes, and the length
steps chosen here would then be sentences the generator never produces.
The analyser checks this itself: every paragraph's sentences must rejoin
to exactly that paragraph (`drift` in the report must be 0).

## Two lengths per sentence

- `display_len` - the reader-facing wording, as `sync.json` would hold it.
- `tts_len`     - what the ENGINE actually receives: the sentence through
  `text_pipeline.prepare_tts_text()` and then through Irodori's own
  `normalize_text()`, which deletes and rewrites more characters (see the
  symbol table). This is the primary measure - it is what the seiyuu has
  to read inside the 30 s window.

Irodori's normaliser is loaded by FILE PATH from `irodori_root`, not
copied and not imported as a package: `irodori_tts/__init__.py` pulls in
torch, while `text_normalization.py` itself is stdlib only. If the file is
missing the report says so and `tts_len` falls back to the pipeline text.

## Nothing here is a claim about how the model sounds

A symbol is marked `measured` only where CLAUDE.md records a pause probe
for it (two seeds, silencedetect). Everything else is `unmeasured`, with
its count, what the pipeline and the normaliser do to it, and examples in
context - the decision about it belongs to a probe or to a person.
"""

import argparse
import importlib.util
import json
import os
import re
import statistics
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# The generator, one folder up. Only text_pipeline is imported from it.
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
if GENERATOR_DIR not in sys.path:
    sys.path.insert(0, GENERATOR_DIR)
import text_pipeline  # noqa: E402

DEFAULT_SETTINGS = {
    "irodori_root": "C:\\Irodori-TTS",
    # F: by the disk-layout rule in CLAUDE.md - sweeps will put thousands
    # of wavs under here.
    "work_root": "F:\\tmp\\book-profiler",
    # shortest, longest, and this many minus two in between
    "length_steps": 7,
    # A step sentence needs this many kana/kanji to be worth a take:
    # "「……" and "と。" are sentences to the splitter, but there is nothing
    # in them for Whisper to check or for pace to be measured on.
    "min_spoken_chars": 4,
}


def merge_setting_defaults(data):
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (data or {}).items() if k in DEFAULT_SETTINGS})
    return merged


def load_settings():
    if not os.path.isfile(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return merge_setting_defaults(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merge_setting_defaults(data), f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------ the engine side

def load_irodori_normalizer(irodori_root):
    """Irodori's normalize_text, or None. See the module docstring for why
    this goes by file path."""
    path = os.path.join(irodori_root, "irodori_tts", "text_normalization.py")
    if not os.path.isfile(path):
        return None, path
    spec = importlib.util.spec_from_file_location("irodori_text_normalization", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_text, path


def engine_text(text, normalize):
    """What the engine receives for one sentence sent on its own."""
    piped = text_pipeline.prepare_tts_text(text)
    return normalize(piped).strip() if normalize else piped


# ------------------------------------------------------------ symbols

# Ordinary Japanese text: kana, kanji, and the few marks that behave as
# letters. Everything else is reported. The katakana middle dot (U+30FB)
# sits inside the katakana block but is punctuation, so it is excluded.
ORDINARY_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30fa\u30fc-\u30ff"
                         r"\u3400-\u4dbf\u4e00-\u9fff\u3005\u3006\u30f6]")
# Letters and digits, half- and full-width, reported as RUNS - "ＷＴＮ" is
# one reading problem, not three.
ALNUM_RUN_RE = re.compile(r"[0-9A-Za-z\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]+")
LINE_BREAKS = "\r\n"

# What CLAUDE.md records as measured. The pause probe of 2026-09-16: flat
# noun-list carrier, symbol inserted mid-list, silencedetect, two seeds
# (gap seed A / seed B; no symbol = 0.81 / 0.59 s).
MEASURED = {
    "、": "pauses - 1.01 / 1.05 s",
    "。": "pauses - 1.14 / 1.25 s",
    "？": "pauses - 1.16 / 1.49 s",
    "…": "pauses hardest - 2.03 / 2.17 s (measured as ……)",
    "＊": "pauses as TWO silences, stumbles - 1.16 / 1.25 s",
    "！": "NO pause, shortens the gap - 0.36 / 0.36 s",
    "─": "raw ─ is deleted by Irodori (0.81 / 0.59 s, same as no symbol); "
         "the pipeline turns it into 、 first",
}


def symbol_effects(ch, normalize):
    """What the pipeline and then the engine make of one character in the
    middle of a sentence. A carrier of kana on both sides keeps the
    edge-only rules (bracket dropping, strip_outer_brackets) out of it."""
    carrier = "あ" + ch + "あ"
    piped = text_pipeline.prepare_tts_text(carrier)
    # split_paragraphs strips inline whitespace and invisibles before any
    # of that, so those never even reach prepare_tts_text.
    if text_pipeline.INLINE_WHITESPACE_RE.fullmatch(ch) or \
            text_pipeline.INVISIBLE_RE.fullmatch(ch):
        piped = "ああ"
    engine = normalize(piped) if normalize else piped

    def middle(s):
        return s[1:-1] if s.startswith("あ") and s.endswith("あ") else s

    return middle(piped), middle(engine)


def pipeline_role(ch):
    if text_pipeline.INLINE_WHITESPACE_RE.fullmatch(ch):
        return "whitespace - stripped"
    if text_pipeline.INVISIBLE_RE.fullmatch(ch):
        return "invisible - stripped"
    if ch in "。？…":
        return "terminator"
    if ch in text_pipeline.BREAK_CHARS:
        return "break point"
    if ch in text_pipeline.CLOSING_BRACKETS:
        return "closing bracket - kept with its sentence"
    if ch in text_pipeline.BRACKETS:
        return "bracket - removed for TTS"
    if ch == text_pipeline.DASH:
        return "dash - becomes 、 for TTS"
    return ""


def measured_status(ch, engine_form, normalize):
    """`measured` if CLAUDE.md has a probe for this character, or for a
    character the engine cannot tell apart from it - Irodori folds ！ to !
    and ？ to ?, so the ascii forms reach the model as the same symbol."""
    if ch in MEASURED:
        return "measured", MEASURED[ch]
    if engine_form == "":
        return "removed", "never reaches the engine"
    if engine_form:
        for known, note in MEASURED.items():
            _p, known_engine = symbol_effects(known, normalize)
            if known_engine and known_engine == engine_form:
                return "measured", f"reaches the engine as {known} does: {note}"
    return "unmeasured", ""


def context_of(text, start, end, width=10):
    left = text[max(0, start - width):start]
    right = text[end:end + width]
    return (left + "[" + text[start:end] + "]" + right).replace("\n", "⏎")


# ------------------------------------------------------------ one chapter

TERMINATOR_END_RE = re.compile(
    r"(?:。|？|……)[" + re.escape(text_pipeline.CLOSING_BRACKETS) + r"]*$")
# A line ending in any of these reads on naturally into the next line, so
# joining it is harmless. A line ending in anything else - a location
# heading, a chapter title, a date - is glued onto the next sentence by
# split_paragraphs(), which is what inflates sentence lengths.
LINE_END_OK = "。？…！!?、" + text_pipeline.CLOSING_BRACKETS


def clean_line(line):
    """split_paragraphs()'s cleaning, applied to one line so line offsets
    can be mapped onto the joined paragraph."""
    line = text_pipeline.INLINE_WHITESPACE_RE.sub("", line)
    line = text_pipeline.INVISIBLE_RE.sub("", line)
    # Without this every section holding a "──" is one character shorter
    # as a paragraph than as lines, and its offsets cannot be trusted.
    line = text_pipeline.DASH_RUN_RE.sub(text_pipeline.DASH, line)
    return line


def analyse_chapter(path, normalize):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    name = os.path.splitext(os.path.basename(path))[0]

    sentences = []
    skipped_punct_only = 0
    drift = 0
    sections_out = []
    glued_lines = []
    line_aware_lengths = []

    for sec_idx, section in enumerate(text_pipeline.split_sections(raw), start=1):
        lines = [clean_line(l) for l in section.split("\n")]
        lines = [l for l in lines if l]
        paragraphs = text_pipeline.split_paragraphs(section)
        section_sentences = 0

        # Where each line ENDS inside the joined paragraph, for the lines
        # that do not end on a natural break.
        glued_offsets = []
        line_ends = []
        offset = 0
        for line_idx, line in enumerate(lines):
            offset += len(line)
            is_last = line_idx == len(lines) - 1
            if not is_last:
                line_ends.append(offset)
                if line[-1] not in LINE_END_OK:
                    glued_offsets.append((offset, line))

        # The alternative the report puts beside the generator's rule: what
        # the sentences would be if every author line also ended one. Not a
        # pipeline change - a measurement, so the choice can be made on it.
        for line in lines:
            for unit in text_pipeline.split_sentences(
                    text_pipeline.DASH_RUN_RE.sub(text_pipeline.DASH, line)):
                if text_pipeline.PUNCT_ONLY_RE.fullmatch(unit):
                    continue
                engine = engine_text(unit, normalize)
                if engine:
                    line_aware_lengths.append(len(engine))

        for paragraph in paragraphs:
            units = text_pipeline.split_sentences(paragraph)
            if "".join(units) != paragraph:
                drift += 1
            # The dash-run rule can shorten a paragraph relative to its
            # lines; offsets are only trusted when nothing was collapsed.
            trust_offsets = sum(len(l) for l in lines) == len(paragraph)

            position = 0
            for unit in units:
                start, end = position, position + len(unit)
                position = end
                if text_pipeline.PUNCT_ONLY_RE.fullmatch(unit):
                    skipped_punct_only += 1
                    continue
                engine = engine_text(unit, normalize)
                if not engine:
                    skipped_punct_only += 1
                    continue
                joined = [line for off, line in glued_offsets
                          if trust_offsets and start < off < end]
                for line in joined:
                    glued_lines.append({"chapter": name, "section": sec_idx,
                                        "line": line, "sentence": unit})
                sentences.append({
                    "chapter": name,
                    "section": sec_idx,
                    "index": len(sentences) + 1,
                    "text": unit,
                    "engine_text": engine,
                    "display_len": len(unit),
                    "tts_len": len(engine),
                    "terminated": bool(TERMINATOR_END_RE.search(unit)),
                    "joined_lines": joined,
                    # How many of the author's lines this sentence runs
                    # across. >1 is typically dialogue closed by 」 with no
                    # 。 before it, welded to the narration that follows.
                    "lines_spanned": (1 + sum(1 for off in line_ends if start < off < end))
                                     if trust_offsets else None,
                    "spoken_chars": len(ORDINARY_RE.findall(unit)),
                })
                section_sentences += 1

        sections_out.append({
            "section": sec_idx,
            "lines": len(lines),
            "chars": sum(len(l) for l in lines),
            "sentences": section_sentences,
            "first_line": lines[0][:40] if lines else "",
            "heading_like": bool(lines) and lines[0][-1] not in LINE_END_OK,
        })

    return {
        "chapter": name,
        "path": path,
        "chars": len(raw),
        "bom": raw.startswith("\ufeff"),
        "sections": sections_out,
        "sentences": sentences,
        "skipped_punct_only": skipped_punct_only,
        "drift": drift,
        "glued_lines": glued_lines,
        "line_aware_lengths": line_aware_lengths,
        "raw": raw,
    }


# ------------------------------------------------------------ numbers

def percentile(sorted_values, p):
    if not sorted_values:
        return 0
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo), 1)


def length_stats(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": values[0],
        "max": values[-1],
        "mean": round(statistics.fmean(values), 1),
        "median": percentile(values, 50),
        "p10": percentile(values, 10),
        "p25": percentile(values, 25),
        "p75": percentile(values, 75),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def histogram(values, width=10):
    bins = {}
    for v in values:
        b = (v - 1) // width if v > 0 else 0
        bins[b] = bins.get(b, 0) + 1
    if not bins:
        return []
    return [{"from": b * width + 1, "to": (b + 1) * width, "count": bins.get(b, 0)}
            for b in range(0, max(bins) + 1)]


THRESHOLDS = (40, 60, 80, 100, 150, 200)


def over_counts(values):
    return {str(t): sum(1 for v in values if v > t) for t in THRESHOLDS}


# ------------------------------------------------------------ symbols, whole scope

def symbol_inventory(chapters, normalize):
    found = {}
    runs = {}
    for chapter in chapters:
        raw = chapter["raw"]
        for match in ALNUM_RUN_RE.finditer(raw):
            key = match.group()
            entry = runs.setdefault(key, {"run": key, "count": 0, "examples": []})
            entry["count"] += 1
            if len(entry["examples"]) < 2:
                entry["examples"].append(
                    f"{chapter['chapter']}: {context_of(raw, match.start(), match.end())}")
        alnum_spans = [(m.start(), m.end()) for m in ALNUM_RUN_RE.finditer(raw)]
        span_iter = iter(alnum_spans)
        current = next(span_iter, None)
        for i, ch in enumerate(raw):
            while current and i >= current[1]:
                current = next(span_iter, None)
            if current and current[0] <= i < current[1]:
                continue
            if ch in LINE_BREAKS or ORDINARY_RE.match(ch):
                continue
            entry = found.setdefault(ch, {"char": ch, "codepoint": f"U+{ord(ch):04X}",
                                          "count": 0, "chapters": {}, "examples": []})
            entry["count"] += 1
            entry["chapters"][chapter["chapter"]] = entry["chapters"].get(chapter["chapter"], 0) + 1
            if len(entry["examples"]) < 3:
                entry["examples"].append(
                    f"{chapter['chapter']}: {context_of(raw, i, i + 1)}")

    symbols = []
    for ch, entry in found.items():
        piped, engine = symbol_effects(ch, normalize)
        status, note = measured_status(ch, engine, normalize)
        if engine == ch:
            engine_effect = "unchanged"
        elif engine == "":
            engine_effect = "deleted"
        else:
            engine_effect = f"-> {engine}"
        entry.update({
            "pipeline_role": pipeline_role(ch),
            "pipeline_gives": piped,
            "engine_receives": engine,
            "engine_effect": engine_effect,
            "status": status,
            "note": note,
        })
        symbols.append(entry)
    symbols.sort(key=lambda e: (e["status"] != "unmeasured", -e["count"]))
    alnum = sorted(runs.values(), key=lambda e: -e["count"])
    return symbols, alnum


def unmeasured_chars(symbols):
    """Characters that still reach the engine and have no probe behind
    them. Characters the pipeline or the engine delete are harmless to a
    length measurement and are not counted here."""
    return {s["char"] for s in symbols
            if s["status"] == "unmeasured" and s["engine_receives"] != ""}


# ------------------------------------------------------------ length steps

def exclusion_reasons(sentence, unmeasured, min_spoken=0):
    reasons = []
    if sentence["spoken_chars"] < min_spoken:
        reasons.append(f"only {sentence['spoken_chars']} spoken characters")
    if sentence["joined_lines"]:
        reasons.append("a heading/unterminated line is glued into it")
    if sentence["lines_spanned"] is None:
        reasons.append("author-line structure could not be mapped")
    elif sentence["lines_spanned"] > 1:
        reasons.append(f"runs across {sentence['lines_spanned']} author lines")
    if not sentence["terminated"]:
        reasons.append("no terminator")
    bad = sorted({ch for ch in sentence["text"] if ch in unmeasured})
    if bad:
        reasons.append("unmeasured symbol " + "".join(bad))
    if ALNUM_RUN_RE.search(sentence["text"]):
        reasons.append("latin letters or digits")
    return reasons


def choose_length_steps(sentences, unmeasured, steps, min_spoken=0):
    """`steps` real sentences whose lengths are spread EVENLY between the
    chapter's shortest and longest usable sentence.

    Evenly in length, not by percentile: the distribution is heavily
    skewed short, so percentiles would put five of seven steps among short
    sentences - and the long end is exactly where the 30 s question lives.
    The percentile of each chosen step is reported so the skew is visible.

    A sentence is usable when nothing but its LENGTH could explain a bad
    take: no glued heading, a real terminator, no unmeasured symbol and no
    latin letters or digits, enough spoken characters, and one author line
    only (see exclusion_reasons). The longest sentence overall is reported
    separately when it is not usable."""
    usable = [s for s in sentences if not exclusion_reasons(s, unmeasured, min_spoken)]
    if not usable:
        return [], usable
    lo = min(s["tts_len"] for s in usable)
    hi = max(s["tts_len"] for s in usable)
    all_lengths = sorted(s["tts_len"] for s in sentences)

    chosen, used = [], set()
    targets = [lo if steps == 1 else lo + (hi - lo) * step / (steps - 1)
               for step in range(steps)]
    # The two ends first: they are exact by construction, and filling the
    # middle first let a sparse long tail hand the longest sentence to the
    # step BELOW it, pushing the last step back down the distribution.
    order = [0, steps - 1] + list(range(1, steps - 1)) if steps > 1 else [0]
    for step in order:
        target = targets[step]
        ranked = sorted(usable, key=lambda s: (abs(s["tts_len"] - target), s["index"]))
        pick = next((s for s in ranked if (s["chapter"], s["index"]) not in used), None)
        if pick is None:
            break
        used.add((pick["chapter"], pick["index"]))
        below = sum(1 for v in all_lengths if v <= pick["tts_len"])
        chosen.append({
            "step": step + 1,
            "target_len": round(target, 1),
            "tts_len": pick["tts_len"],
            "display_len": pick["display_len"],
            "percentile": round(100 * below / len(all_lengths), 1),
            "chapter": pick["chapter"],
            "sentence_index": pick["index"],
            "text": pick["text"],
            "engine_text": pick["engine_text"],
        })
    chosen.sort(key=lambda c: c["tts_len"])
    for number, entry in enumerate(chosen, start=1):
        entry["step"] = number
    return chosen, usable


# ------------------------------------------------------------ scope

def scope_for(args):
    """(book name, scope id, [chapter paths])."""
    if args.book:
        folder = os.path.abspath(args.book)
        paths = sorted(os.path.join(folder, n) for n in os.listdir(folder)
                       if n.lower().endswith(".txt"))
        book_dir = folder
        scope = "book"
    else:
        paths = [os.path.abspath(p) for p in args.chapter]
        book_dir = os.path.dirname(paths[0])
        scope = "+".join(os.path.splitext(os.path.basename(p))[0] for p in paths)
    # F:\AUDIOBOOK-FINAL\yojo-senki\text -> yojo-senki
    name = os.path.basename(book_dir)
    if name.lower() == "text":
        name = os.path.basename(os.path.dirname(book_dir))
    return args.name or name, scope, paths


def run(args, settings):
    normalize, normalizer_path = load_irodori_normalizer(settings["irodori_root"])
    book, scope, paths = scope_for(args)
    if not paths:
        raise SystemExit("No .txt chapter files found.")

    chapters = [analyse_chapter(p, normalize) for p in paths]
    symbols, alnum = symbol_inventory(chapters, normalize)
    unmeasured = unmeasured_chars(symbols)

    all_sentences = [s for c in chapters for s in c["sentences"]]
    per_chapter = []
    for chapter in chapters:
        lengths = [s["tts_len"] for s in chapter["sentences"]]
        min_spoken = int(settings["min_spoken_chars"])
        steps, usable = choose_length_steps(chapter["sentences"], unmeasured,
                                            int(settings["length_steps"]), min_spoken)
        multi_line = [s for s in chapter["sentences"] if (s["lines_spanned"] or 1) > 1]
        longest = max(chapter["sentences"], key=lambda s: s["tts_len"], default=None)
        longest_info = None
        if longest:
            longest_info = {
                "tts_len": longest["tts_len"], "display_len": longest["display_len"],
                "index": longest["index"], "text": longest["text"],
                "excluded_because": exclusion_reasons(longest, unmeasured, min_spoken),
            }
        per_chapter.append({
            "chapter": chapter["chapter"],
            "path": chapter["path"],
            "chars": chapter["chars"],
            "bom": chapter["bom"],
            "sections": len(chapter["sections"]),
            "lines": sum(s["lines"] for s in chapter["sections"]),
            "heading_like_sections": [s["first_line"] for s in chapter["sections"]
                                      if s["heading_like"]],
            "lines_per_section": length_stats([s["lines"] for s in chapter["sections"]]),
            "sentences_per_section": length_stats([s["sentences"] for s in chapter["sections"]]),
            "sentence_tts_len": length_stats(lengths),
            "sentence_display_len": length_stats([s["display_len"] for s in chapter["sentences"]]),
            "over": over_counts(lengths),
            "histogram": histogram(lengths),
            "unterminated_sentences": sum(1 for s in chapter["sentences"] if not s["terminated"]),
            "sentences_with_glued_lines": sum(1 for s in chapter["sentences"] if s["joined_lines"]),
            "multi_line_sentences": len(multi_line),
            "multi_line_tts_len": length_stats([s["tts_len"] for s in multi_line]),
            "line_aware_tts_len": length_stats(chapter["line_aware_lengths"]),
            "line_aware_over": over_counts(chapter["line_aware_lengths"]),
            "glued_lines": chapter["glued_lines"],
            "skipped_punct_only": chapter["skipped_punct_only"],
            "drift": chapter["drift"],
            "usable_sentences": len(usable),
            "longest_overall": longest_info,
            "length_steps": steps,
        })

    lengths = [s["tts_len"] for s in all_sentences]
    report = {
        "version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book": book,
        "scope": scope,
        "chapters_in_scope": [c["chapter"] for c in chapters],
        "irodori_normalizer": normalizer_path if normalize else None,
        "length_measure": "tts_len = characters the engine receives "
                          "(prepare_tts_text, then Irodori normalize_text)",
        "overall": {
            "sentences": len(all_sentences),
            "sentence_tts_len": length_stats(lengths),
            "sentence_display_len": length_stats([s["display_len"] for s in all_sentences]),
            "over": over_counts(lengths),
            "histogram": histogram(lengths),
            "drift": sum(c["drift"] for c in chapters),
            "multi_line_sentences": sum(1 for s in all_sentences
                                        if (s["lines_spanned"] or 1) > 1),
            "line_aware_tts_len": length_stats(
                [v for c in chapters for v in c["line_aware_lengths"]]),
            "line_aware_over": over_counts(
                [v for c in chapters for v in c["line_aware_lengths"]]),
        },
        "chapters": per_chapter,
        "symbols": [{k: v for k, v in s.items()} for s in symbols],
        "latin_digit_runs": alnum,
        "sentences": [{k: v for k, v in s.items()} for s in all_sentences],
    }

    out_dir = os.path.join(settings["work_root"], book, scope, "analysis")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "analysis.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    return report, out_dir


# ------------------------------------------------------------ markdown

def stats_row(label, st):
    if not st.get("count"):
        return f"| {label} | 0 | | | | | | | |"
    return (f"| {label} | {st['count']} | {st['min']} | {st['p25']} | {st['median']} | "
            f"{st['mean']} | {st['p75']} | {st['p90']} | {st['max']} |")


STATS_HEAD = ("| | sentences | min | p25 | median | mean | p75 | p90 | max |\n"
              "|---|---|---|---|---|---|---|---|---|")


def bar_chart(hist, width=40):
    if not hist:
        return ""
    top = max(h["count"] for h in hist) or 1
    lines = []
    for h in hist:
        bar = "█" * max(1 if h["count"] else 0, round(width * h["count"] / top))
        lines.append(f"{h['from']:>4}-{h['to']:<4} {h['count']:>5}  {bar}")
    return "```\n" + "\n".join(lines) + "\n```"


def md_escape(text):
    return (text or "").replace("|", "\\|")


def render_markdown(r):
    o = []
    add = o.append
    add(f"# Text analysis - {r['book']} ({r['scope']})\n")
    add(f"Created {r['created']}. Chapters: {', '.join(r['chapters_in_scope'])}.\n")
    add(f"Length measure: **{r['length_measure']}**. "
        f"Irodori normaliser: `{r['irodori_normalizer'] or 'NOT FOUND - pipeline text only'}`.\n")
    ov = r["overall"]
    add(f"Split check: **drift = {ov['drift']}** (paragraphs whose sentences do not "
        f"rejoin to the paragraph; must be 0).\n")

    add("## Sentence length, engine characters\n")
    add(STATS_HEAD)
    add(stats_row("**all in scope**", ov["sentence_tts_len"]))
    for c in r["chapters"]:
        add(stats_row(c["chapter"], c["sentence_tts_len"]))
    add("")
    add("Reader-facing (display) length, for comparison:\n")
    add(STATS_HEAD)
    add(stats_row("**all in scope**", ov["sentence_display_len"]))
    add("")
    add("Sentences longer than N engine characters:\n")
    add("| | " + " | ".join(f">{t}" for t in THRESHOLDS) + " |")
    add("|---|" + "---|" * len(THRESHOLDS))
    add("| **all** | " + " | ".join(str(ov["over"][str(t)]) for t in THRESHOLDS) + " |")
    for c in r["chapters"]:
        add(f"| {c['chapter']} | " + " | ".join(str(c["over"][str(t)]) for t in THRESHOLDS) + " |")
    add("")
    add("### Sentences that run across author lines\n")
    add(f"**{ov['multi_line_sentences']} of {ov['sentences']}** sentences span more "
        "than one of the author's lines. `split_paragraphs` joins lines and only "
        "`。？……` end a sentence, so dialogue closed with `」` (or `！」`) and no "
        "`。` is welded to the narration line after it. If a line break ALSO ended "
        "a sentence, the lengths would be:\n")
    add(STATS_HEAD)
    add(stats_row("generator rule (today)", ov["sentence_tts_len"]))
    add(stats_row("line break also ends", ov["line_aware_tts_len"]))
    add("")
    add("| | " + " | ".join(f">{t}" for t in THRESHOLDS) + " |")
    add("|---|" + "---|" * len(THRESHOLDS))
    add("| generator rule | " + " | ".join(str(ov["over"][str(t)]) for t in THRESHOLDS) + " |")
    add("| line break also ends | " + " | ".join(
        str(ov["line_aware_over"][str(t)]) for t in THRESHOLDS) + " |")
    add("")
    add("| chapter | multi-line sentences | their median length | their max |")
    add("|---|---|---|---|")
    for c in r["chapters"]:
        st = c["multi_line_tts_len"]
        add(f"| {c['chapter']} | {c['multi_line_sentences']} | {st.get('median', '')} | "
            f"{st.get('max', '')} |")
    add("")
    add("Distribution, all in scope (10-character bins):\n")
    add(bar_chart(ov["histogram"]))
    add("")

    add("## Structure\n")
    add("| chapter | chars | BOM | sections | lines | lines/section median (max) | "
        "sentences/section median (max) | heading-like sections | glued lines | unterminated | punct-only skipped |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for c in r["chapters"]:
        lps, sps = c["lines_per_section"], c["sentences_per_section"]
        add(f"| {c['chapter']} | {c['chars']} | {'yes' if c['bom'] else ''} | {c['sections']} | "
            f"{c['lines']} | {lps.get('median', 0)} ({lps.get('max', 0)}) | "
            f"{sps.get('median', 0)} ({sps.get('max', 0)}) | {len(c['heading_like_sections'])} | "
            f"{len(c['glued_lines'])} | {c['unterminated_sentences']} | {c['skipped_punct_only']} |")
    add("")
    add("- **section** = text between blank lines (what `split_sections` splits on).\n"
        "- **line** = a single-newline line inside a section. `split_paragraphs` joins "
        "them, so they get no silence of their own today.\n"
        "- **heading-like** = a section whose first line does not end on a terminator, "
        "break point or closing bracket - chapter titles, locations, dates.\n"
        "- **glued line** = such a line that is NOT the last in its section, so the "
        "join welds it onto the next sentence and that sentence is longer than what "
        "the author wrote.\n")
    for c in r["chapters"]:
        if c["heading_like_sections"] or c["glued_lines"]:
            add(f"### {c['chapter']}\n")
            if c["heading_like_sections"]:
                add("Heading-like sections: " + " / ".join(
                    f"`{md_escape(h)}`" for h in c["heading_like_sections"]) + "\n")
            for g in c["glued_lines"][:12]:
                add(f"- glued `{md_escape(g['line'])}` -> sentence of "
                    f"{len(g['sentence'])} chars: {md_escape(g['sentence'][:70])}…")
            if len(c["glued_lines"]) > 12:
                add(f"- … {len(c['glued_lines']) - 12} more in analysis.json")
            add("")

    add("## Length steps per chapter (candidate audition sentences)\n")
    add("Spread evenly in length between the shortest and longest USABLE sentence. "
        "Usable = enough spoken characters, one author line, no glued heading, has a "
        "terminator, no unmeasured symbol, no latin letters/digits - so a bad take "
        "can only be blamed on length.\n")
    for c in r["chapters"]:
        add(f"### {c['chapter']} - {c['usable_sentences']} of "
            f"{c['sentence_tts_len'].get('count', 0)} sentences usable\n")
        lo = c["longest_overall"]
        if lo and lo["excluded_because"]:
            add(f"Longest overall is {lo['tts_len']} chars (#{lo['index']}) but not usable: "
                f"{'; '.join(lo['excluded_because'])}.\n")
        add("| step | target | engine chars | percentile | # | sentence |")
        add("|---|---|---|---|---|---|")
        for s in c["length_steps"]:
            add(f"| {s['step']} | {s['target_len']} | {s['tts_len']} | {s['percentile']}% | "
                f"{s['sentence_index']} | {md_escape(s['text'])} |")
        add("")

    add("## Symbols\n")
    add("`measured` only where CLAUDE.md records a pause probe. `engine` is what "
        "Irodori's normaliser makes of the character in mid-sentence, AFTER the "
        "pipeline. A deleted character is never heard, whatever it looks like.\n")
    add("| char | code | count | pipeline | engine | status | note / examples |")
    add("|---|---|---|---|---|---|---|")
    for s in r["symbols"]:
        shown = {" ": "(space)", "\u3000": "(IDSP)"}.get(s["char"], s["char"])
        detail = s["note"] if s["status"] == "measured" else " · ".join(
            md_escape(e) for e in s["examples"])
        add(f"| {md_escape(shown)} | {s['codepoint']} | {s['count']} | "
            f"{md_escape(s['pipeline_role'])} | {md_escape(s['engine_effect'])} | "
            f"{s['status']} | {detail} |")
    add("")
    add("## Latin letters and digit runs\n")
    add("Irodori's NFKC step turns full-width letters and digits into ascii. How a "
        "seiyuu READS them (ＷＴＮ, 1923, Ｘ) is unmeasured.\n")
    add("| run | count | example |")
    add("|---|---|---|")
    for run_entry in r["latin_digit_runs"][:40]:
        add(f"| {md_escape(run_entry['run'])} | {run_entry['count']} | "
            f"{md_escape(run_entry['examples'][0])} |")
    if len(r["latin_digit_runs"]) > 40:
        add(f"\n… {len(r['latin_digit_runs']) - 40} more distinct runs in analysis.json")
    add("")
    return "\n".join(o)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--book", help="folder of chapter .txt files - profile the whole book")
    group.add_argument("--chapter", action="append",
                       help="one chapter .txt; repeat for several - profile only these")
    parser.add_argument("--name", help="book name for the output folder "
                                       "(default: the folder name, or its parent when it is 'text')")
    args = parser.parse_args()
    settings = load_settings()
    report, out_dir = run(args, settings)
    ov = report["overall"]
    st = ov["sentence_tts_len"]
    print(f"{report['book']} ({report['scope']}): {len(report['chapters'])} chapter(s), "
          f"{ov['sentences']} sentences, engine length min {st.get('min')} / "
          f"median {st.get('median')} / max {st.get('max')}, drift {ov['drift']}")
    print(f"unmeasured symbols reaching the engine: "
          f"{sum(1 for s in report['symbols'] if s['status'] == 'unmeasured' and s['engine_receives'])}")
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
