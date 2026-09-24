"""
analyze.py
----------
Stage 1 of the book profiler: read a book's chapter text and describe it
the way DYNAMIC PROFILE MODE will see it, with no GPU and no TTS.

    uv run --project book-profiler python book-profiler/analyze.py --book <folder> [--exclude chapter_007]
    uv run --project book-profiler python book-profiler/analyze.py --chapter <file> [--chapter <file> ...]

The scope is chosen up front - a whole book, or chosen chapters - because
one book can legitimately use different seiyuu for different chapters.

Output, into <work_root>/<book>/<scope>/analysis/:

    analysis.json   everything, for the later stages to read
    analysis.md     the same, for a person to read

## Dynamic mode's rules, not a copy of them

Sentences, cut points and the TTS text all come from `text_pipeline.py` in
the folder above (`dynamic_sentences`, `split_for_length`,
`prepare_tts_text_dynamic`) - the same functions the generator's dynamic
mode will call. A copy here would drift the first time a rule changes, and
the profile would describe sentences the generator never sends. Normal
mode (`build_chunks`) is reported once, as a comparison row.

In short: a line break ends a sentence as well as 。？……; a sentence longer
than the seiyuu's comfortable length is cut after 、 」 ） (0.7 s), before
「 （ (0.7 s) or after ！ ! ? (1.0 s).

## Length is what the ENGINE receives

`tts_len` is a sentence through `prepare_tts_text_dynamic()` and then
Irodori's own `normalize_text()`, which deletes and rewrites more (see the
symbol table). That is what the seiyuu has to read inside the 30 s window.
Irodori's normaliser is loaded by FILE PATH from `irodori_root`, because
`irodori_tts/__init__.py` pulls in torch while `text_normalization.py`
itself is stdlib only.

## Nothing here is a claim about how the model sounds

`measured` means a pause probe recorded in CLAUDE.md. `judged by ear` means
a decision the user made by listening. Everything else that reaches the
engine is `unmeasured`.
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

GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
if GENERATOR_DIR not in sys.path:
    sys.path.insert(0, GENERATOR_DIR)
import text_pipeline as tp  # noqa: E402
import dynamic_profile  # noqa: E402
import furigana  # noqa: E402
import suite_link  # noqa: E402

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
    # Comfortable lengths the split table is computed for. The real one is
    # only known after the sweep; these show what each would mean.
    "report_limits": [30, 40, 50, 60, 80, 100],
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


def merged_settings(defaults):
    """`defaults` overlaid with any of THOSE keys found in settings.json.

    The profiler's scripts share one settings.json but each owns its own
    keys. load_settings() above keeps only the analyser's, so without this
    a sweep or score key written into the file would be silently dropped -
    the same trap CLAUDE.md records for the generator's
    _collect_and_validate()."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    merged = dict(defaults)
    merged.update({k: v for k, v in data.items() if k in defaults})
    return merged


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merge_setting_defaults(data), f, ensure_ascii=False, indent=2)


def add_run_options(parser, takes=False):
    """Per-run overrides the GUI passes (2026-09-18). Absent, settings.json
    applies exactly as before, so a CLI user sees no change.

    --work-root is the GUI's "profile root": everything a run writes lands
    under it, so it has to reach every stage or they would disagree about
    where the analysis, the takes and the recipe are."""
    parser.add_argument("--work-root", help="output root (default: work_root in settings.json)")
    # The book's readings.json (it lives in the generator's OUTPUT folder,
    # which the profiler does not know). TTS text and Whisper comparison
    # only - lengths stay the book's own, as in the generator.
    parser.add_argument("--readings", help="the book's readings.json (optional)")
    # The furigana decisions live in the suite library, keyed by book slug.
    # Without this the annotations are stripped before measuring, which is
    # what an undecided book renders as anyway.
    parser.add_argument("--furigana-book", help="book slug in the suite library (optional)")
    if takes:
        parser.add_argument("--arms", help="comma list, e.g. 'random' or 'seeded,random'")
        parser.add_argument("--takes", type=int, help="takes per arm per scale")


def apply_run_options(settings, args):
    if getattr(args, "work_root", None):
        settings["work_root"] = os.path.abspath(args.work_root)
    settings["readings"] = []
    if getattr(args, "readings", None):
        if not os.path.isfile(args.readings):
            raise SystemExit(f"--readings: no such file {args.readings}")
        try:
            settings["readings"] = dynamic_profile.load_readings(args.readings)
        except dynamic_profile.ProfileError as e:
            raise SystemExit(f"--readings: {e}")
    settings["furigana"] = set()
    if getattr(args, "furigana_book", None):
        suite = suite_link.open_suite(settings)
        if suite is None:
            raise SystemExit(f"--furigana-book: the suite library is not reachable "
                             f"({suite_link.load_error()})")
        try:
            book = suite.book_by_slug(args.furigana_book)
            if book is None:
                raise SystemExit(f"--furigana-book: no book '{args.furigana_book}' in the library")
            settings["furigana"] = suite_link.furigana_applied(suite, book)
        finally:
            suite.close()
    if getattr(args, "arms", None):
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
        unknown = [a for a in arms if a not in ("seeded", "random")]
        if unknown or not arms:
            raise SystemExit(f"--arms: unknown arm(s) {unknown or args.arms}")
        settings["arms"] = arms
    if getattr(args, "takes", None):
        settings["takes"] = int(args.takes)
    # The seeded arm has one fixed seed per take; more takes than seeds
    # would index past the list mid-sweep.
    if "seeded" in settings.get("arms", ()) and \
            settings.get("takes", 0) > len(settings.get("fixed_seeds", ())):
        raise SystemExit(f"the seeded arm has {len(settings['fixed_seeds'])} fixed seeds - "
                         f"takes cannot exceed that")
    return settings


def tts_text(text, settings):
    """What the engine is sent for `text`: the generator's dynamic rule,
    then the book's readings - the same order plan_pieces() uses."""
    return dynamic_profile.apply_readings(tp.prepare_tts_text_dynamic(text),
                                          settings.get("readings"))[0]


def spoken_text(text, settings):
    """`text` as it is meant to be HEARD - readings applied - which is what
    Whisper's transcript is compared with (decision 2026-09-22)."""
    return dynamic_profile.apply_readings(text, settings.get("readings"))[0]


# A chapter whose sweep audio was deleted by the GUI's Clean-up tab. Resume
# treats a missing wav as "not rendered yet", so without this a rerun would
# quietly re-render the whole chapter with fresh random draws.
CLEANED_MARKER = "cleaned.json"


def cleaned_note(chapter_root):
    """None, or why this chapter's takes must not be touched again."""
    path = os.path.join(chapter_root, CLEANED_MARKER)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            when = json.load(f).get("cleaned", "?")
    except (OSError, ValueError):
        when = "?"
    return (f"its audio was cleaned up on {when} - the measurements are kept, but the "
            f"takes are gone. To measure again, profile into a fresh folder.")


# ------------------------------------------------------------ the engine side

def load_irodori_normalizer(irodori_root):
    path = os.path.join(irodori_root, "irodori_tts", "text_normalization.py")
    if not os.path.isfile(path):
        return None, path
    spec = importlib.util.spec_from_file_location("irodori_text_normalization", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_text, path


def make_engine(normalize):
    """text -> what the engine receives when that text is sent on its own."""
    def engine(text):
        piped = tp.prepare_tts_text_dynamic(text)
        return normalize(piped).strip() if normalize else piped
    return engine


# ------------------------------------------------------------ symbols

# Ordinary Japanese text: kana, kanji, and the marks that behave as letters.
# The katakana middle dot (U+30FB) is punctuation and is reported.
ORDINARY_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30fa\u30fc-\u30ff"
                         r"\u3400-\u4dbf\u4e00-\u9fff\u3005\u3006\u30f6]")
ALNUM_RUN_RE = re.compile(r"[0-9A-Za-z\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]+")

# Pause probe of 2026-09-16 (CLAUDE.md): flat noun-list carrier, symbol
# mid-list, silencedetect, two seeds. No symbol = 0.81 / 0.59 s.
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
JUDGED = {
    "・": "judged by ear 2026-09-16: names are read without a pause - no harm",
}


def symbol_effects(ch, engine):
    """What the TTS text and then the engine make of one character in the
    middle of a sentence. Kana on both sides keeps the edge-only rules out
    of it. Whitespace and invisibles never get that far - split_lines()
    strips them first."""
    if tp.INLINE_WHITESPACE_RE.fullmatch(ch) or tp.INVISIBLE_RE.fullmatch(ch):
        return ""
    result = engine("あ" + ch + "あ")
    if result.startswith("あ") and result.endswith("あ"):
        return result[1:-1]
    return result


def dynamic_role(ch):
    if tp.INLINE_WHITESPACE_RE.fullmatch(ch):
        return "whitespace - stripped"
    if tp.INVISIBLE_RE.fullmatch(ch):
        return "invisible - stripped"
    if ch in "。？…":
        return "ends a sentence"
    if tp.DYNAMIC_STRIP_RE.fullmatch(ch):
        return "stripped for TTS"
    roles = []
    if ch in tp.DYNAMIC_COMMA_BEFORE:
        roles.append("cut before it (0.7 s)")
    if ch in tp.DYNAMIC_COMMA_AFTER:
        roles.append("cut after it (0.7 s)")
    if ch in tp.DYNAMIC_SENTENCE_AFTER:
        roles.append("cut after it (1.0 s)")
    if ch in tp.DYNAMIC_BRACKETS:
        roles.append("removed or 、 for TTS")
    if ch == tp.DASH:
        roles.append("becomes 、 for TTS")
    return "; ".join(roles)


def symbol_status(ch, engine_form, engine):
    if ch in MEASURED:
        return "measured", MEASURED[ch]
    if ch in JUDGED:
        return "judged by ear", JUDGED[ch]
    if engine_form == "":
        return "removed", "never reaches the engine"
    for known, note in MEASURED.items():
        if symbol_effects(known, engine) == engine_form:
            return "measured", f"reaches the engine as {known} does: {note}"
    return "unmeasured", ""


def context_of(text, start, end, width=10):
    return (text[max(0, start - width):start] + "[" + text[start:end] + "]"
            + text[end:end + width]).replace("\n", "⏎")


# ------------------------------------------------------------ one chapter

HEADING_END_OK = "。？…！!?、" + tp.CLOSING_BRACKETS
TERMINATOR_END_RE = re.compile(r"(?:。|？|……)[" + re.escape(tp.CLOSING_BRACKETS) + r"]*$")


def analyse_chapter(path, engine, settings=None):
    """`settings` carries the book's readings and furigana decisions; with
    none, the text is measured as the author wrote it."""
    settings = settings or {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    name = os.path.splitext(os.path.basename(path))[0]

    # Furigana is applied (or stripped) before anything is measured - the
    # generator sends the reading, so the profiler must measure the
    # reading (user decision 2026-09-22/24).
    raw, spans = furigana.mark(raw, settings.get("furigana") or set())

    sections = tp.split_sections(raw)
    lines_per_section = [tp.split_lines(s) for s in sections]
    cleaned = "".join(line for lines in lines_per_section for line in lines)

    units = tp.dynamic_sentences(raw)
    # Nothing added, nothing lost but the 、 dynamic_sentences drops on
    # purpose (and records): every sentence rejoins to every line.
    drift = 0 if tp.dynamic_source_text(units) == cleaned else 1

    sentences, skipped = [], []
    for unit in units:
        marked = unit["text"]
        text = furigana.display(marked)              # the book's wording
        # Measured as it will be SPOKEN: furigana applied, then the book's
        # readings. Lengths pick the band, so they must match the request.
        engine_text = engine(spoken_text(furigana.spoken(marked, spans), settings))
        if not engine_text or tp.PUNCT_ONLY_RE.fullmatch(engine_text):
            skipped.append(text)
            continue
        sentences.append({
            "chapter": name,
            "index": len(sentences) + 1,
            "section": unit["section"],
            "line": unit["line"],
            "gap_before": unit["gap_before"],
            "text": text,
            "spoken_text": furigana.spoken(marked, spans),
            "engine_text": engine_text,
            "display_len": len(text),
            "tts_len": len(engine_text),
            "spoken_chars": len(ORDINARY_RE.findall(text)),
            "terminated": bool(TERMINATOR_END_RE.search(text)),
            "heading_like": not text or text[-1] not in HEADING_END_OK,
        })

    # Normal mode, for the comparison row only.
    normal_lengths = []
    for section in sections:
        for paragraph in tp.split_paragraphs(section):
            for unit in tp.split_sentences(paragraph):
                if tp.PUNCT_ONLY_RE.fullmatch(unit):
                    continue
                piped = tp.prepare_tts_text(unit)
                if piped:
                    normal_lengths.append(len(piped))

    return {
        "chapter": name,
        "path": path,
        "chars": len(raw),
        "bom": raw.startswith("\ufeff"),
        "sections": len(sections),
        "lines_per_section": [len(lines) for lines in lines_per_section],
        "sentences": sentences,
        "skipped": skipped,
        "drift": drift,
        "normal_lengths": normal_lengths,
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
        "count": len(values), "min": values[0], "max": values[-1],
        "mean": round(statistics.fmean(values), 1),
        "median": percentile(values, 50), "p10": percentile(values, 10),
        "p25": percentile(values, 25), "p75": percentile(values, 75),
        "p90": percentile(values, 90), "p95": percentile(values, 95),
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


# ------------------------------------------------------------ splitting

def split_table(sentences, limits, engine, settings=None):
    """For each candidate comfortable length L: how many sentences exceed
    it, what cutting them does, and which ones cannot be brought under it.

    Every cut is checked to rejoin byte-identically; `split_drift` must be 0.
    Cuts are measured as the sentence will be SPOKEN, like the bands."""
    measure = lambda text: len(engine(spoken_text(text, settings or {})))
    rows = []
    for limit in limits:
        over = [s for s in sentences if s["tts_len"] > limit]
        pieces_total = comma_cuts = sentence_cuts = split_drift = 0
        stuck = []
        longest_piece = 0
        for s in over:
            pieces = tp.split_for_length(s["text"], limit, measure)
            if "".join(p for p, _k in pieces) != s["text"]:
                split_drift += 1
            pieces_total += len(pieces)
            comma_cuts += sum(1 for _p, k in pieces if k == "comma")
            sentence_cuts += sum(1 for _p, k in pieces if k == "sentence")
            lengths = [measure(p) for p, _k in pieces]
            longest_piece = max([longest_piece] + lengths)
            worst = max(lengths)
            if worst > limit:
                stuck.append({
                    "chapter": s["chapter"], "index": s["index"],
                    "tts_len": s["tts_len"], "worst_piece": worst,
                    "pieces": [p for p, _k in pieces], "text": s["text"],
                })
        stuck.sort(key=lambda r: -(r["worst_piece"] - limit))
        rows.append({
            "limit": limit,
            "sentences_over": len(over),
            "pieces_after_split": pieces_total,
            "comma_cuts": comma_cuts,
            "sentence_cuts": sentence_cuts,
            "still_over": len(stuck),
            "longest_after_split": longest_piece if over else 0,
            "split_drift": split_drift,
            "stuck": stuck,
        })
    return rows


# ------------------------------------------------------------ symbols, whole scope

def symbol_inventory(chapters, engine):
    found, runs = {}, {}
    for chapter in chapters:
        raw = chapter["raw"]
        spans = []
        for match in ALNUM_RUN_RE.finditer(raw):
            spans.append((match.start(), match.end()))
            entry = runs.setdefault(match.group(), {"run": match.group(), "count": 0,
                                                    "examples": []})
            entry["count"] += 1
            if len(entry["examples"]) < 2:
                entry["examples"].append(
                    f"{chapter['chapter']}: {context_of(raw, match.start(), match.end())}")
        in_run = set(i for a, b in spans for i in range(a, b))
        for i, ch in enumerate(raw):
            if i in in_run or ch in "\r\n" or ORDINARY_RE.match(ch):
                continue
            entry = found.setdefault(ch, {"char": ch, "codepoint": f"U+{ord(ch):04X}",
                                          "count": 0, "examples": []})
            entry["count"] += 1
            if len(entry["examples"]) < 3:
                entry["examples"].append(f"{chapter['chapter']}: {context_of(raw, i, i + 1)}")

    symbols = []
    for ch, entry in found.items():
        engine_form = symbol_effects(ch, engine)
        status, note = symbol_status(ch, engine_form, engine)
        entry.update({
            "role": dynamic_role(ch),
            "engine_receives": engine_form,
            "engine_effect": ("unchanged" if engine_form == ch else
                              "deleted" if engine_form == "" else f"-> {engine_form}"),
            "status": status,
            "note": note,
        })
        symbols.append(entry)
    order = {"unmeasured": 0, "judged by ear": 1, "measured": 2, "removed": 3}
    symbols.sort(key=lambda e: (order[e["status"]], -e["count"]))
    return symbols, sorted(runs.values(), key=lambda e: -e["count"])


def kanji_years(chapters):
    """What the kanji number rules convert, and every 〇 they leave behind.

    Run on each LINE, as the TTS text rules are, so a match can never
    reach across a line break the sentence splitter would cut at."""
    converted, leftover = {}, []
    for chapter in chapters:
        for line in chapter["raw"].split("\n"):
            spans = []
            for kind, regex, convert in (
                    ("year", tp.KANJI_YEAR_RE, tp._kanji_year_to_arabic),
                    ("number", tp.KANJI_ZERO_NUMBER_RE, tp._kanji_zero_number_to_arabic)):
                for match in regex.finditer(line):
                    start, end = match.start(1), match.end(1)
                    if any(a < end and start < b for a, b in spans):
                        continue
                    result = convert(match)
                    if result == match.group(1):
                        continue
                    spans.append((start, end))
                    suffix = "年" if kind == "year" else ""
                    key = match.group(1) + suffix
                    entry = converted.setdefault(key, {
                        "kind": kind, "from": key, "to": result + suffix, "count": 0,
                        "example": f"{chapter['chapter']}: {context_of(line, start, end)}"})
                    entry["count"] += 1
            for i, ch in enumerate(line):
                if ch == "〇" and not any(a <= i < b for a, b in spans):
                    leftover.append(f"{chapter['chapter']}: {context_of(line, i, i + 1)}")
    return sorted(converted.values(), key=lambda e: (e["kind"], -e["count"])), leftover


# ------------------------------------------------------------ length steps

def exclusion_reasons(sentence, unmeasured, min_spoken):
    reasons = []
    if sentence["spoken_chars"] < min_spoken:
        reasons.append(f"only {sentence['spoken_chars']} spoken characters")
    if sentence["heading_like"]:
        reasons.append("heading-like line (no ending punctuation)")
    bad = sorted({ch for ch in sentence["text"] if ch in unmeasured})
    if bad:
        reasons.append("unmeasured symbol " + "".join(bad))
    # Checked on the ENGINE text: a converted year arrives as digits too.
    if ALNUM_RUN_RE.search(sentence["engine_text"]):
        reasons.append("latin letters or digits reach the engine")
    return reasons


def choose_length_steps(sentences, unmeasured, steps, min_spoken):
    """`steps` real sentences spread EVENLY in length between the shortest
    and longest usable one.

    Evenly, not by percentile: the distribution is skewed short, so
    percentiles would crowd the steps among short sentences - and the long
    end is where the 30 s question lives. Each step's percentile is
    reported so the skew stays visible.

    Usable = nothing but LENGTH could explain a bad take (exclusion_reasons).
    The two ends are chosen first: filling the middle first let a sparse
    long tail hand the longest sentence to the step below it."""
    usable = [s for s in sentences if not exclusion_reasons(s, unmeasured, min_spoken)]
    if not usable:
        return [], usable
    lo = min(s["tts_len"] for s in usable)
    hi = max(s["tts_len"] for s in usable)
    all_lengths = sorted(s["tts_len"] for s in sentences)
    targets = [lo if steps == 1 else lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
    order = [0, steps - 1] + list(range(1, steps - 1)) if steps > 1 else [0]

    chosen, used = [], set()
    for step in order:
        target = targets[step]
        ranked = sorted(usable, key=lambda s: (abs(s["tts_len"] - target), s["index"]))
        pick = next((s for s in ranked if s["index"] not in used), None)
        if pick is None:
            break
        used.add(pick["index"])
        chosen.append({
            "target_len": round(target, 1),
            "tts_len": pick["tts_len"],
            "display_len": pick["display_len"],
            "percentile": round(100 * sum(1 for v in all_lengths if v <= pick["tts_len"])
                                / len(all_lengths), 1),
            "chapter": pick["chapter"],
            "sentence_index": pick["index"],
            "text": pick["text"],
            # As it will be SPOKEN (furigana applied); the sweep renders
            # this and the scorer compares Whisper against it, so a step
            # measures the same text the generator would send.
            "spoken_text": pick["spoken_text"],
            "engine_text": pick["engine_text"],
        })
    chosen.sort(key=lambda c: c["tts_len"])
    for number, entry in enumerate(chosen, start=1):
        entry["step"] = number
    return chosen, usable


# ------------------------------------------------------------ scope

def scope_for(args):
    """(book name, scope id, [chapter paths], [excluded names])."""
    excluded = []
    if args.book:
        folder = os.path.abspath(args.book)
        skip = {os.path.splitext(e)[0] for e in (args.exclude or [])}
        paths = []
        for path in chapter_files(folder):
            base = os.path.splitext(os.path.basename(path))[0]
            if base in skip:
                excluded.append(base)
                continue
            paths.append(path)
        book_dir, scope = folder, "book"
    else:
        paths = [os.path.abspath(p) for p in args.chapter]
        book_dir = os.path.dirname(paths[0])
        scope = "+".join(os.path.splitext(os.path.basename(p))[0] for p in paths)
    return args.name or book_name(book_dir), scope, paths, excluded


def book_name(book_dir):
    """The folder name, or its parent's when it is called `text`
    (F:\\AUDIOBOOK-FINAL\\yojo-senki\\text -> yojo-senki)."""
    book_dir = os.path.abspath(book_dir)
    name = os.path.basename(book_dir)
    if name.lower() == "text":
        name = os.path.basename(os.path.dirname(book_dir))
    return name


def chapter_files(book_dir):
    """What --book profiles: every .txt in the folder, sorted."""
    return [os.path.join(book_dir, n) for n in sorted(os.listdir(book_dir))
            if n.lower().endswith(".txt")]


def run(args, settings):
    normalize, normalizer_path = load_irodori_normalizer(settings["irodori_root"])
    engine = make_engine(normalize)
    book, scope, paths, excluded = scope_for(args)
    if not paths:
        raise SystemExit("No .txt chapter files found.")

    chapters = [analyse_chapter(p, engine, settings) for p in paths]
    symbols, alnum = symbol_inventory(chapters, engine)
    unmeasured = {s["char"] for s in symbols if s["status"] == "unmeasured"}
    years, leftover_zero = kanji_years(chapters)
    limits = [int(v) for v in settings["report_limits"]]
    min_spoken = int(settings["min_spoken_chars"])

    all_sentences = [s for c in chapters for s in c["sentences"]]
    per_chapter = []
    for chapter in chapters:
        sents = chapter["sentences"]
        lengths = [s["tts_len"] for s in sents]
        steps, usable = choose_length_steps(sents, unmeasured,
                                            int(settings["length_steps"]), min_spoken)
        longest = max(sents, key=lambda s: s["tts_len"], default=None)
        lps = chapter["lines_per_section"]
        per_chapter.append({
            "chapter": chapter["chapter"],
            "path": chapter["path"],
            "chars": chapter["chars"],
            "bom": chapter["bom"],
            "sections": chapter["sections"],
            "lines": sum(lps),
            "lines_per_section": length_stats(lps),
            "heading_like_lines": [s["text"] for s in sents if s["heading_like"]],
            "skipped_units": chapter["skipped"],
            "drift": chapter["drift"],
            "sentence_tts_len": length_stats(lengths),
            "normal_mode_tts_len": length_stats(chapter["normal_lengths"]),
            "histogram": histogram(lengths),
            "split": [{k: v for k, v in row.items() if k != "stuck"}
                      for row in split_table(sents, limits, engine, settings)],
            "usable_sentences": len(usable),
            "longest_overall": None if not longest else {
                "tts_len": longest["tts_len"], "index": longest["index"],
                "text": longest["text"],
                "excluded_because": exclusion_reasons(longest, unmeasured, min_spoken)},
            "length_steps": steps,
        })

    lengths = [s["tts_len"] for s in all_sentences]
    report = {
        "version": 2,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book": book,
        "scope": scope,
        "chapters_in_scope": [c["chapter"] for c in chapters],
        "chapters_excluded": excluded,
        "rules": "dynamic mode (text_pipeline.dynamic_sentences / split_for_length / "
                 "prepare_tts_text_dynamic)",
        "irodori_normalizer": normalizer_path if normalize else None,
        "overall": {
            "sentences": len(all_sentences),
            "sentence_tts_len": length_stats(lengths),
            "normal_mode_tts_len": length_stats(
                [v for c in chapters for v in c["normal_lengths"]]),
            "histogram": histogram(lengths),
            "drift": sum(c["drift"] for c in chapters),
            "split": split_table(all_sentences, limits, engine, settings),
        },
        "chapters": per_chapter,
        "symbols": symbols,
        "kanji_years": years,
        "zero_not_in_a_year": leftover_zero,
        "latin_digit_runs": alnum,
        "sentences": all_sentences,
    }

    out_dir = os.path.join(settings["work_root"], book, scope, "analysis")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "analysis.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    return report, out_dir


# ------------------------------------------------------------ markdown

STATS_HEAD = ("| | sentences | min | p25 | median | mean | p75 | p90 | p99 | max |\n"
              "|---|---|---|---|---|---|---|---|---|---|")


def stats_row(label, st):
    if not st.get("count"):
        return f"| {label} | 0 | | | | | | | | |"
    return (f"| {label} | {st['count']} | {st['min']} | {st['p25']} | {st['median']} | "
            f"{st['mean']} | {st['p75']} | {st['p90']} | {st['p99']} | {st['max']} |")


def bar_chart(hist, width=40):
    if not hist:
        return ""
    top = max(h["count"] for h in hist) or 1
    lines = [f"{h['from']:>4}-{h['to']:<4} {h['count']:>5}  "
             + "█" * max(1 if h["count"] else 0, round(width * h["count"] / top))
             for h in hist]
    return "```\n" + "\n".join(lines) + "\n```"


def esc(text):
    return (text or "").replace("|", "\\|")


def render_markdown(r):
    o = []
    add = o.append
    ov = r["overall"]
    add(f"# Text analysis - {r['book']} ({r['scope']})\n")
    add(f"Created {r['created']}. Chapters: {', '.join(r['chapters_in_scope'])}."
        + (f" Excluded: {', '.join(r['chapters_excluded'])}." if r["chapters_excluded"] else "")
        + "\n")
    add(f"Rules: **{r['rules']}**. Length = characters the ENGINE receives, after "
        f"Irodori's normaliser (`{r['irodori_normalizer'] or 'NOT FOUND'}`).\n")
    split_drift = sum(row["split_drift"] for row in ov["split"])
    add(f"Checks: **drift = {ov['drift']}** (sentences rejoin to the lines), "
        f"**split drift = {split_drift}** (cut pieces rejoin to their sentence). Both must be 0.\n")

    add("## Sentence length\n")
    add("A line break ends a sentence, as do 。？……\n")
    add(STATS_HEAD)
    add(stats_row("**dynamic mode, all**", ov["sentence_tts_len"]))
    for c in r["chapters"]:
        add(stats_row(c["chapter"], c["sentence_tts_len"]))
    add(stats_row("*normal mode today, all*", ov["normal_mode_tts_len"]))
    add("")
    add(bar_chart(ov["histogram"]))
    add("")

    add("## Long sentences after cutting\n")
    add("For each candidate comfortable length L (the real one comes from the sweep): "
        "sentences longer than L are cut after 、」）, before 「（ (0.7 s) or after "
        "！!? (1.0 s). The set of cuts is chosen to fit L if at all possible, then with "
        f"the fewest pieces, then the most even; no piece shorter than "
        f"{tp.DYNAMIC_MIN_PIECE} engine characters.\n")
    add("| L | sentences over L | pieces after cutting | 0.7 s cuts | 1.0 s cuts | "
        "**still over L** | longest piece |")
    add("|---|---|---|---|---|---|---|")
    for row in ov["split"]:
        add(f"| {row['limit']} | {row['sentences_over']} | {row['pieces_after_split']} | "
            f"{row['comma_cuts']} | {row['sentence_cuts']} | **{row['still_over']}** | "
            f"{row['longest_after_split']} |")
    add("")
    add("Still over L, per chapter:\n")
    add("| chapter | " + " | ".join(f"L={row['limit']}" for row in ov["split"]) + " |")
    add("|---|" + "---|" * len(ov["split"]))
    for c in r["chapters"]:
        add(f"| {c['chapter']} | " + " | ".join(
            f"{row['still_over']} / {row['sentences_over']}" for row in c["split"]) + " |")
    add("\n(still over / sentences over)\n")
    # Longest L first, and each list shows only sentences not already shown
    # at a longer L - otherwise every list opens with the same worst ten.
    shown_before = set()
    for row in sorted(ov["split"], key=lambda row: -row["limit"]):
        fresh = [s for s in row["stuck"] if (s["chapter"], s["index"]) not in shown_before]
        shown_before.update((s["chapter"], s["index"]) for s in row["stuck"])
        if not row["stuck"]:
            continue
        add(f"### Cannot be brought under L={row['limit']} - {row['still_over']} "
            f"(new at this L: {len(fresh)}, worst shown)\n")
        for s in fresh[:8]:
            shown = " ‖ ".join(esc(p) for p in s["pieces"])
            add(f"- {s['chapter']} #{s['index']} - {s['tts_len']} chars, longest piece "
                f"{s['worst_piece']}: {shown}")
        if len(fresh) > 8:
            add(f"- … {len(fresh) - 8} more in analysis.json")
        add("")

    add("## Structure\n")
    add("| chapter | chars | BOM | sections | lines | lines/section median (max) | "
        "heading-like lines | skipped (punctuation or stripped only) |")
    add("|---|---|---|---|---|---|---|---|")
    for c in r["chapters"]:
        lps = c["lines_per_section"]
        add(f"| {c['chapter']} | {c['chars']} | {'yes' if c['bom'] else ''} | {c['sections']} | "
            f"{c['lines']} | {lps.get('median', 0)} ({lps.get('max', 0)}) | "
            f"{len(c['heading_like_lines'])} | {len(c['skipped_units'])} |")
    add("")
    add("A **section** is text between blank lines (1.5 s before it). A **line** is one "
        "author line; in dynamic mode it ends a sentence (1.0 s). **Heading-like** = a "
        "line ending without punctuation - titles, locations, dates - now its own "
        "sentence instead of being glued to the next one.\n")
    for c in r["chapters"]:
        if c["heading_like_lines"]:
            add(f"- {c['chapter']}: " + " / ".join(
                f"`{esc(h)}`" for h in c["heading_like_lines"][:15])
                + (f" … +{len(c['heading_like_lines']) - 15}"
                   if len(c["heading_like_lines"]) > 15 else ""))
    add("")

    add("## Length steps per chapter (candidate audition sentences)\n")
    add("Evenly spread in length between the shortest and longest USABLE sentence. "
        "Usable = at least the minimum spoken characters, not heading-like, no "
        "unmeasured symbol, no latin letters or digits reaching the engine.\n")
    for c in r["chapters"]:
        add(f"### {c['chapter']} - {c['usable_sentences']} of "
            f"{c['sentence_tts_len'].get('count', 0)} sentences usable\n")
        lo = c["longest_overall"]
        if lo and lo["excluded_because"]:
            add(f"Longest overall is {lo['tts_len']} chars (#{lo['index']}), not usable: "
                f"{'; '.join(lo['excluded_because'])}.\n")
        add("| step | target | engine chars | percentile | # | sentence |")
        add("|---|---|---|---|---|---|")
        for s in c["length_steps"]:
            add(f"| {s['step']} | {s['target_len']} | {s['tts_len']} | {s['percentile']}% | "
                f"{s['sentence_index']} | {esc(s['text'])} |")
        add("")

    add("## Symbols\n")
    add("`engine` = what reaches Irodori's model mid-sentence, after the dynamic TTS "
        "text rules AND Irodori's normaliser.\n")
    add("| char | code | count | dynamic mode | engine | status | note / examples |")
    add("|---|---|---|---|---|---|---|")
    for s in r["symbols"]:
        shown = {" ": "(space)", "\u3000": "(IDSP)"}.get(s["char"], s["char"])
        detail = s["note"] if s["status"] in ("measured", "judged by ear") else \
            " · ".join(esc(e) for e in s["examples"])
        add(f"| {esc(shown)} | {s['codepoint']} | {s['count']} | {esc(s['role'])} | "
            f"{esc(s['engine_effect'])} | {s['status']} | {detail} |")
    add("")

    add("## Kanji numbers\n")
    add("Four-digit years before 年, and every other positional number holding a 〇, "
        "are sent as arabic digits.\n")
    add("| kind | source | sent as | count | example |")
    add("|---|---|---|---|---|")
    for y in r["kanji_years"]:
        add(f"| {y['kind']} | {y['from']} | {y['to']} | {y['count']} | {esc(y['example'])} |")
    add("")
    left = r["zero_not_in_a_year"]
    add(f"**{len(left)}** 〇 left unconverted - these still reach the engine as ○:\n")
    for e in left[:20]:
        add(f"- {esc(e)}")
    if len(left) > 20:
        add(f"- … {len(left) - 20} more in analysis.json")
    add("")

    add("## Latin letters and digit runs\n")
    add("Irodori's NFKC step turns full-width letters and digits into ascii. How a "
        "seiyuu READS them is unmeasured.\n")
    add("| run | count | example |")
    add("|---|---|---|")
    for e in r["latin_digit_runs"][:30]:
        add(f"| {esc(e['run'])} | {e['count']} | {esc(e['examples'][0])} |")
    if len(r["latin_digit_runs"]) > 30:
        add(f"\n… {len(r['latin_digit_runs']) - 30} more distinct runs in analysis.json")
    add("")
    return "\n".join(o)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Book profiler, stage 1: text analysis")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--book", help="folder of chapter .txt files - profile the whole book")
    group.add_argument("--chapter", action="append",
                       help="one chapter .txt; repeat for several - profile only these")
    parser.add_argument("--exclude", action="append",
                        help="with --book: a chapter to leave out (e.g. chapter_007); repeatable")
    parser.add_argument("--name", help="book name for the output folder "
                                       "(default: the folder name, or its parent when it is 'text')")
    add_run_options(parser)
    args = parser.parse_args()
    report, out_dir = run(args, apply_run_options(load_settings(), args))
    ov = report["overall"]
    st = ov["sentence_tts_len"]
    print(f"{report['book']} ({report['scope']}): {len(report['chapters'])} chapter(s), "
          f"{ov['sentences']} sentences, engine length min {st.get('min')} / "
          f"median {st.get('median')} / max {st.get('max')}, drift {ov['drift']}, "
          f"split drift {sum(row['split_drift'] for row in ov['split'])}")
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
