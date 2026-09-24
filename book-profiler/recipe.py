"""
recipe.py
---------
Stage 4b of the book profiler: turn the sweep lengths and the hallucination
scan into a duration-scale recipe per chapter, then judge whether one
recipe can serve the whole book.

    uv run --project book-profiler python book-profiler/recipe.py \
        --book yojo-senki --speaker C:/Irodori-TTS/seiyuu/list/tanya.speaker.safetensors \
        [--chapter chapter_001 --chapter ...]

Without --chapter it uses every chapter that has a score.json. Writes

    <work_root>/<book>/<scope>/recipe/<seiyuu>/
        profile_book.json              the book-wide recipe
        profile_chapter_001.json ...   one per chapter
        recipe.md

## The formula (agreed 2026-09-16, window 2026-09-22)

For each length step of a chapter, over the scales that were rendered:

- a scale is CLEAN when none of its takes is flagged and none sits at the
  ceiling (takes are re-judged by score.flag_takes, the current rules);
- the WINDOW is the longest run of adjacent clean scales, ties to the
  slower run. Originally the run had to reach the top of the sweep, which
  assumed a seiyuu only fails by reading too fast; marinka-03-calm-shonen
  fails slow too (a tail after the sentence) and got no recipe at all;
- faster  = the window's lowest scale plus `safety_margin` (0.1) - three
  takes a side at the boundary is a small sample;
- slower  = the window's highest scale, no margin (the tail checks find
  that edge directly);
- default = midway between faster and slower, rounded to 0.1.

## Fewer speeds

A profile offers "faster" only when every band has it at least `speed_gap`
(0.1) below default, "slower" likewise above; a dropped speed is written
equal to default and `available_speeds` lists what remains (profile v3,
see dynamic_profile). A chapter with no clean scale on its first judged
step has no bands and no profile; no chapter at all exits NO_WINDOW_EXIT.

A step no longer than `word_span` characters is not judged at all (every
mismatch in it would count as a word slip) and its lengths use the next
step's recipe.

A sentence takes the recipe of the NEXT LONGER step - the cautious side.
One longer than every step takes the longest step's recipe and is counted
as beyond what was measured.

A step with no clean scale at all is where the seiyuu stops coping: the
comfortable length L is the longest step BELOW the first such step, and
longer sentences are cut to L by text_pipeline.split_for_length.

### Word slips

Measured on chapter_001: where Whisper and the seiyuu disagree over a
single rare word (`玉響`, read たまひょう / タマヒビ) or a spelling
(`もっとも` heard as `最も`), the flagged take's mismatch sits inside a few
characters. A real hallucination garbles 11-65 characters of the script.
So a flagged take whose whole mismatch fits in `word_span` characters is a
WORD SLIP: it does not move the faster boundary, and the word is listed in
the report for a person to look at. Judged per take - see word_slips().

## One recipe for the book?

The book recipe is the cautious envelope of the chapter recipes at every
sentence length: the highest `faster` any chapter needs and the lowest
`slower` any chapter allows, default midway. It is viable when faster never
exceeds slower. Each chapter is then compared with it - how much slower
the book recipe's default reads that chapter than its own would - so the
cost of using one recipe is visible, chapter by chapter.
"""

import argparse
import difflib
import json
import os
import statistics
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
for path in (GENERATOR_DIR, SCRIPT_DIR, os.path.join(GENERATOR_DIR, "chapter-repair")):
    if path not in sys.path:
        sys.path.insert(0, path)
import text_pipeline as tp  # noqa: E402
import dynamic_profile  # noqa: E402
import analyze  # noqa: E402
import sweep  # noqa: E402
import score  # noqa: E402
import repair  # noqa: E402

RECIPE_DEFAULTS = {
    "safety_margin": 0.1,
    # A speed is offered only this far from default in every band.
    "speed_gap": 0.1,
    "word_span": 6,
    # Silences dynamic mode renders with (user decisions 2026-09-16).
    "silence_section": 1.5,
    "silence_sentence": 1.0,
    "silence_comma": 0.7,
}
# The profile format is the generator's contract, so its version lives there.
PROFILE_VERSION = dynamic_profile.PROFILE_VERSION
# Exit code when no chapter has a clean window: nothing to write, and the
# same score.json files give the same answer - profiler_runner does not retry.
NO_WINDOW_EXIT = 3


def load_settings():
    settings = analyze.merged_settings(RECIPE_DEFAULTS)
    settings.update(sweep.load_settings())
    return settings


def r1(value):
    """Round to one decimal, halves up - 1.55 is 1.6, as a person would."""
    return int(value * 10 + 0.5 + 1e-9) / 10


# ------------------------------------------------------------ one step

def mismatch(script, heard):
    """How much of a transcript disagrees with its script, after the same
    normalisation similarity() uses:

        script_chars  script characters inside a non-matching block
        heard_chars   transcript characters inside one (catches ad-libs)
        truncated     the transcript stops short of the script's ending
        words         the script text of each non-matching block

    Counted, not spanned: chapter_005 has a take with two separate slips
    (議事進行 -> 疑似信仰, 教官 -> 教育員) whose outer span covers the whole
    sentence while only five characters are actually wrong."""
    a = repair.normalise_for_compare(script)
    b = repair.normalise_for_compare(heard)
    blocks = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes() if op[0] != "equal"]
    return {
        "script_chars": sum(i2 - i1 for _t, i1, i2, _j1, _j2 in blocks),
        "heard_chars": sum(j2 - j1 for _t, _i1, _i2, j1, j2 in blocks),
        "truncated": any(i2 == len(a) and i2 > i1 for _t, i1, i2, _j1, _j2 in blocks)
                     and len(b) < len(a),
        "words": [a[i1:i2] for _t, i1, i2, _j1, _j2 in blocks if i2 > i1],
    }


def word_slips(step_row, settings):
    """Mark each flagged take as a WORD SLIP (in place) when its whole
    mismatch fits inside `word_span` characters, and return what the slips
    were, grouped by the script word.

    Judged per take, not per step. First version required EVERY flag of a
    step to be the same small slip, and chapter_005 broke it: its step 2
    has real hallucinations at 1.0-1.1 AND a Whisper homophone (議事進行
    heard as 疑似信仰) at 1.3 and 1.5, and the all-or-nothing rule let
    that stray 1.5 flag push the step's faster boundary to 1.7."""
    words = {}
    for t in step_row["takes"]:
        t["word_slip"] = False
        if not t["flags"]:
            continue
        # spoken_text: readings applied (score.py, 2026-09-22); older rows
        # have none and were scored against the book text.
        m = mismatch(step_row.get("spoken_text", step_row["text"]), t["heard"])
        # A dropped ending is the abandoned-sentence failure, never a slip:
        # chapter_002's 聞き慣れてもなお我慢のならないじ loses 自分の声だ.
        # Nor is an ADDED one (score.py's ran_long): marinka's …葉を噛む、噛む。
        # is a two-character mismatch and was heard as a hallucination.
        if m["truncated"] or t.get("ran_long") or \
                max(m["script_chars"], m["heard_chars"]) > settings["word_span"]:
            continue
        t["word_slip"] = True
        entry = words.setdefault(" / ".join(m["words"]) or "(insertion)",
                                 {"takes": 0, "scales": set(), "heard": set()})
        entry["takes"] += 1
        entry["scales"].add(t["scale"])
        entry["heard"].add(t["heard"])
    return [{"word": w, "takes": e["takes"], "scales": sorted(e["scales"]),
             "heard": sorted(e["heard"])} for w, e in words.items()]


def step_recipe(step_row, settings):
    slips = word_slips(step_row, settings)
    scales = sorted({t["scale"] for t in step_row["takes"]})
    # A sentence no longer than a word slip cannot show a hallucination at
    # all - every mismatch in it is "a slip" - so it cannot set a recipe.
    # chapter_002's 応答願う」 came back as Whisper's stock
    # ご視聴ありがとうございました on a 0.88 s clip.
    if len(repair.normalise_for_compare(step_row["text"])) <= settings["word_span"]:
        return {"step": step_row["step"], "tts_len": step_row["tts_len"],
                "text": step_row["text"], "clean": {}, "word_slips": slips,
                "usable": True, "judged": False, "why_not": "too short",
                "faster": None, "default": None, "slower": None}
    # Whisper and the book spell this sentence differently at every scale
    # (score.flag_takes' low_agreement), so nothing it says about this step
    # is about the seiyuu. Not judged, exactly like a too-short step: its
    # lengths take the next longer step's recipe, and spelling noise cannot
    # set a band (user decision 2026-09-24).
    if any(t.get("low_agreement") for t in step_row["takes"]):
        return {"step": step_row["step"], "tts_len": step_row["tts_len"],
                "text": step_row["text"], "clean": {}, "word_slips": slips,
                "usable": True, "judged": False, "why_not": "script and transcript never agree",
                "faster": None, "default": None, "slower": None}
    clean = {}
    for scale in scales:
        takes = [t for t in step_row["takes"] if t["scale"] == scale]
        flagged = sum(1 for t in takes if t["flags"] and not t["word_slip"])
        capped = sum(1 for t in takes if t["at_ceiling"])
        clean[scale] = flagged == 0 and capped == 0
    base = {"step": step_row["step"], "tts_len": step_row["tts_len"], "text": step_row["text"],
            "clean": {f"{s:.1f}": clean[s] for s in scales}, "word_slips": slips}
    window = clean_window(scales, clean)
    if not window:
        return dict(base, usable=False, judged=True, window=None,
                    faster=None, default=None, slower=None)
    faster_base, slower = window
    faster = min(r1(faster_base + settings["safety_margin"]), slower)
    return dict(base, usable=True, judged=True, window=list(window), faster_base=faster_base,
                faster=faster, slower=slower, default=r1((faster + slower) / 2))


def clean_window(scales, clean):
    """(lowest, highest) of the longest run of adjacent clean scales, ties
    to the slower run - or None when no scale is clean.

    Until 2026-09-22 the run had to reach the top of the sweep, because
    tanya only ever failed by reading too FAST. marinka-03-calm-shonen also
    fails SLOW (reads the sentence, then ad-libs a tail), which left her no
    usable step at all. No margin below the top edge (decision 2026-09-22):
    score.py's tail checks find that edge directly."""
    runs, run = [], []
    for scale in scales:
        if clean[scale]:
            run.append(scale)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    if not runs:
        return None
    best = max(runs, key=lambda r: (len(r), r[-1]))
    return best[0], best[-1]


# ------------------------------------------------------------ one chapter

def chapter_recipe(score, settings):
    steps = [step_recipe(row, settings) for row in sorted(score["rows"], key=lambda r: r["tts_len"])]
    limit_index = next((i for i, s in enumerate(steps) if not s["usable"]), None)
    kept = steps if limit_index is None else steps[:limit_index]
    # Unjudged (too-short) steps set nothing: their lengths fall into the
    # next judged step's band, the cautious side as everywhere else.
    kept = [s for s in kept if s["judged"]]
    bands = []
    previous = 0
    for s in kept:
        bands.append({"from_len": previous + 1, "to_len": s["tts_len"],
                      "faster": s["faster"], "default": s["default"], "slower": s["slower"],
                      "step": s["step"]})
        previous = s["tts_len"]
    non_monotone = [(a["step"], b["step"]) for a, b in zip(kept, kept[1:])
                    if b["faster"] < a["faster"]]
    return {
        "chapter": score["chapter"],
        "steps": steps,
        "bands": bands,
        "comfortable_length": kept[-1]["tts_len"] if kept else None,
        "limited_by_step": None if limit_index is None else steps[limit_index]["step"],
        "non_monotone_faster": non_monotone,
    }


def lookup(bands, length):
    """The band for a sentence of `length` engine characters: the next
    longer step, or the longest one for anything beyond it."""
    for band in bands:
        if length <= band["to_len"]:
            return band, False
    return (bands[-1], True) if bands else (None, True)


# ------------------------------------------------------------ the book

def book_recipe(chapters, max_len):
    """Cautious envelope at every length from 1 to max_len."""
    rows = []
    for length in range(1, max_len + 1):
        picks = [lookup(c["bands"], length)[0] for c in chapters if c["bands"]]
        if not picks:
            continue
        faster = max(p["faster"] for p in picks)
        slower = min(p["slower"] for p in picks)
        rows.append({"len": length, "faster": faster, "slower": slower,
                     "viable": faster <= slower,
                     "default": r1((faster + slower) / 2) if faster <= slower else None})
    bands = []
    for row in rows:
        key = (row["faster"], row["default"], row["slower"])
        if bands and (bands[-1]["faster"], bands[-1]["default"], bands[-1]["slower"]) == key:
            bands[-1]["to_len"] = row["len"]
        else:
            bands.append({"from_len": row["len"], "to_len": row["len"], "faster": row["faster"],
                          "default": row["default"], "slower": row["slower"],
                          "viable": row["viable"]})
    return bands, all(r["viable"] for r in rows)


def speeds_for(bands, settings):
    """The speeds a profile of `bands` can offer, and the bands to write.

    Profile-wide (decision 2026-09-22): "faster" stays only when EVERY band
    has it at least `speed_gap` below default, "slower" likewise above. A
    dropped speed is written equal to default in every band, so a stale
    selection still renders at the safe speed."""
    gap = settings["speed_gap"] - 1e-9
    speeds = ["default"]
    if all(b["default"] - b["faster"] >= gap for b in bands):
        speeds.append("faster")
    if all(b["slower"] - b["default"] >= gap for b in bands):
        speeds.append("slower")
    speeds = [s for s in dynamic_profile.SPEEDS if s in speeds]
    written = [dict(b, **{s: b["default"] for s in ("faster", "slower") if s not in speeds})
               for b in bands]
    return speeds, written


def sentences_by_chapter(analysis):
    out = {}
    for s in analysis["sentences"]:
        out.setdefault(s["chapter"], []).append(s)
    return out


def application(bands, limit, sentences, engine):
    """What the recipe does to real sentences: cut counts, what stays over
    L, how many go beyond the measured lengths, and the style mix."""
    measure = lambda text: len(engine(text))
    pieces = cut_sentences = stuck = beyond = 0
    stuck_examples = []
    for s in sentences:
        parts = [s["text"]]
        if limit and s["tts_len"] > limit:
            cut = tp.split_for_length(s["text"], limit, measure)
            parts = [p for p, _k in cut]
            cut_sentences += 1
            worst = max(measure(p) for p in parts)
            if worst > limit:
                stuck += 1
                if len(stuck_examples) < 10:
                    stuck_examples.append({"chapter": s["chapter"], "index": s["index"],
                                           "tts_len": s["tts_len"], "worst_piece": worst,
                                           "text": s["text"]})
        for p in parts:
            pieces += 1
            _band, past = lookup(bands, measure(p))
            beyond += 1 if past else 0
    return {"sentences": len(sentences), "requests": pieces, "cut_sentences": cut_sentences,
            "still_over_limit": stuck, "beyond_measured": beyond, "stuck_examples": stuck_examples}


def chapter_cost(chapter, book_bands, sentences, engine):
    """Mean and worst extra scale the book recipe's default puts on this
    chapter's own sentences, compared with the chapter's own recipe."""
    diffs = []
    for s in sentences:
        own, _ = lookup(chapter["bands"], s["tts_len"])
        book, _ = lookup(book_bands, s["tts_len"])
        if own and book and book["default"] is not None:
            diffs.append(round(book["default"] - own["default"], 2))
    if not diffs:
        return {"mean_extra_scale": None, "max_extra_scale": None, "sentences_changed": 0}
    return {"mean_extra_scale": round(sum(diffs) / len(diffs), 3),
            "max_extra_scale": max(diffs),
            "sentences_slower": sum(1 for d in diffs if d > 0),
            "sentences_faster": sum(1 for d in diffs if d < 0),
            "sentences": len(diffs)}


# ------------------------------------------------------------ pace view

def pace_view(scores, chapters, settings):
    """The same takes seen by speaking speed instead of by scale.

    Found while the five-chapter run was going: flag rate climbs steadily
    with characters per second (under 1% at 5.5 ch/s or slower, 24% at
    7.0, 83% at 8.0 over chapters 1-3), and the pace a sentence gets at a
    given scale varies a lot, because Irodori predicts each sentence's own
    duration - chapter_003's 32-character step reads at 9.2 ch/s at 1.0.
    This does not change the recipe. It shows what the agreed recipe means
    in pace, so the two can be compared."""
    by_pace = {}
    per_step = []
    recipes = {c["chapter"]: {s["step"]: s for s in c["steps"]} for c in chapters}
    for score in scores:
        for row in score["rows"]:
            recipe = recipes[score["chapter"]][row["step"]]
            if not recipe["judged"]:
                continue
            word_slips(row, settings)
            unit = None
            for scale in sorted({t["scale"] for t in row["takes"]}):
                takes = [t for t in row["takes"] if t["scale"] == scale]
                seconds = takes[0]["seconds"]
                unit = unit or seconds / scale
                pace = row["tts_len"] / seconds
                key = round(pace * 2) / 2
                entry = by_pace.setdefault(key, [0, 0])
                entry[0] += sum(1 for t in takes if t["flags"] and not t["word_slip"])
                entry[1] += len(takes)
            if recipe["usable"] and unit:
                per_step.append({
                    "chapter": score["chapter"], "step": row["step"], "tts_len": row["tts_len"],
                    "pace_at_1": round(row["tts_len"] / unit, 2),
                    "faster": round(row["tts_len"] / (unit * recipe["faster"]), 2),
                    "default": round(row["tts_len"] / (unit * recipe["default"]), 2),
                    "slower": round(row["tts_len"] / (unit * recipe["slower"]), 2),
                })
    table = [{"pace": k, "flagged": v[0], "takes": v[1],
              "rate": round(v[0] / v[1], 3) if v[1] else 0} for k, v in sorted(by_pace.items())]
    return table, per_step


def render_pace_md(table, per_step):
    o = ["## The same takes by pace\n",
         "Not part of the recipe - a comparison. Flag rate (word slips excluded) by engine "
         "characters per second, all judged steps of all chapters:\n",
         "| pace ch/s | flagged / takes | rate |", "|---|---|---|"]
    for row in table:
        o.append(f"| {row['pace']:.1f} | {row['flagged']} / {row['takes']} | "
                 f"{100 * row['rate']:.1f}% |")
    o.append("")
    o.append("What the agreed recipe's scales mean in pace, per step:\n")
    o.append("| chapter | step | chars | pace at 1.0 | faster | default | slower |")
    o.append("|---|---|---|---|---|---|---|")
    for s in per_step:
        o.append(f"| {s['chapter']} | {s['step']} | {s['tts_len']} | {s['pace_at_1']} | "
                 f"{s['faster']} | {s['default']} | {s['slower']} |")
    if per_step:
        for style in ("faster", "default", "slower"):
            values = sorted(s[style] for s in per_step)
            o.append(f"\n{style}: {values[0]}–{values[-1]} ch/s across steps "
                     f"(median {values[len(values) // 2]})")
    o.append("")
    return "\n".join(o)


# ------------------------------------------------------------ output

def pace_targets(scores, bands, settings):
    """The Even pace recipe's targets: the MEDIAN pace this profile's own
    scale bands produce per style, over every judged step of `scores`.

    So both recipes of one profile average the same speed and differ in
    evenness only - which is what the 2026-09-17 listening test compared.
    A step's pace at a scale is exact, not estimated: length is text x
    scale (the sweep measured it), so one take's seconds / scale gives the
    unit."""
    paces = {style: [] for style in ("default", "slower", "faster")}
    for score in scores:
        for row in score["rows"]:
            if len(repair.normalise_for_compare(row["text"])) <= settings["word_span"]:
                continue
            take = row["takes"][0]
            unit = take["seconds"] / take["scale"]
            band, _past = lookup(bands, row["tts_len"])
            for style in paces:
                paces[style].append(row["tts_len"] / (unit * band[style]))
    return {style: round(statistics.median(values), 2) for style, values in paces.items()}


def profile(book, speaker, scope, chapters_in, bands, limit, settings, extra, targets, speeds):
    return dict({
        "version": PROFILE_VERSION,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book": book,
        "speaker_path": speaker,
        "speaker_stamp": sweep.speaker_stamp(speaker),
        "scope": scope,
        "chapters": chapters_in,
        "rules": "dynamic mode (text_pipeline.dynamic_sentences / split_for_length / "
                 "prepare_tts_text_dynamic)",
        "trim_tail": bool(settings["trim_tail"]),
        "silence": {"section": settings["silence_section"],
                    "sentence": settings["silence_sentence"],
                    "comma": settings["silence_comma"]},
        "comfortable_length": limit,
        # Both methods kept on every profile (decision 2026-09-17): scale_*
        # from the bands, pace_* from pace_targets. A narrow seiyuu offers
        # fewer speeds, for both alike (decision 2026-09-22).
        "available_speeds": speeds,
        "styles": [k for k in dynamic_profile.STYLE_KEYS
                   if dynamic_profile.split_style(k)[1] in speeds],
        "bands": bands,
        "pace_targets": targets,
    }, **extra)


def fmt(value):
    return "–" if value is None else f"{value:.1f}"


def render_md(book, speaker, chapters, book_bands, viable, book_limit, apply_book, costs,
              book_speeds, no_window):
    o = [f"# Recipe - {book} / {sweep.nickname_for(speaker)}\n",
         "Clean window = the longest run of adjacent clean scales. faster = its lowest scale "
         "+ 0.1; slower = its highest; default = midway. A sentence uses the next longer "
         "step. A speed is offered only when it is at least 0.1 from default in every band.\n"]
    if no_window:
        o.append("**No clean window** (no profile): " + ", ".join(
            f"{c['chapter']} (step {c['limited_by_step']})" for c in no_window) + "\n")
    o.append("## Book recipe\n")
    o.append(f"One recipe for the whole book: **{'viable' if viable else 'NOT viable'}**. "
             f"Comfortable length L = **{book_limit}** engine characters.\n")
    if not viable:
        o.append("The chapters' clean windows do not overlap at every length, so "
                 "**no profile_book.json was written** - use a chapter profile.\n")
    elif book_speeds and len(book_speeds) < len(dynamic_profile.SPEEDS):
        dropped = [s for s in dynamic_profile.SPEEDS if s not in book_speeds]
        o.append(f"**Narrow seiyuu: this profile offers {' and '.join(book_speeds)} only** "
                 f"({len(book_speeds) * 2} of 6 styles). No room for "
                 f"{' / '.join(dropped)} at least 0.1 from default in every band - the "
                 "profile writes them equal to default. The table shows what was measured.\n")
    o.append("| sentence length | faster | default | slower |")
    o.append("|---|---|---|---|")
    for b in book_bands:
        o.append(f"| {b['from_len']}–{b['to_len']} | {fmt(b['faster'])} | {fmt(b['default'])} | "
                 f"{fmt(b['slower'])}{'' if b['viable'] else ' **conflict**'} |")
    o.append("")
    o.append(f"Applied to the book's {apply_book['sentences']} sentences: "
             f"{apply_book['requests']} TTS requests, {apply_book['cut_sentences']} sentences cut "
             f"to L, **{apply_book['still_over_limit']} still over L after cutting**, "
             f"{apply_book['beyond_measured']} requests longer than any measured step.\n")
    for s in apply_book["stuck_examples"]:
        o.append(f"- {s['chapter']} #{s['index']} ({s['tts_len']} chars, longest piece "
                 f"{s['worst_piece']}): {s['text']}")
    o.append("")

    o.append("## What one recipe costs each chapter\n")
    o.append("Extra duration scale the book default puts on a chapter's sentences, compared "
             "with that chapter's own recipe.\n")
    o.append("| chapter | own L | limited by | mean extra | max extra | sentences slower | "
             "sentences faster |")
    o.append("|---|---|---|---|---|---|---|")
    for c in chapters:
        k = costs[c["chapter"]]
        o.append(f"| {c['chapter']} | {c['comfortable_length']} | "
                 f"{'step ' + str(c['limited_by_step']) if c['limited_by_step'] else 'nothing'} | "
                 f"{k.get('mean_extra_scale')} | {k.get('max_extra_scale')} | "
                 f"{k.get('sentences_slower', 0)} | {k.get('sentences_faster', 0)} |")
    o.append("")

    o.append("## Chapter recipes\n")
    for c in chapters:
        o.append(f"### {c['chapter']}\n")
        if len(c.get("speeds") or dynamic_profile.SPEEDS) < len(dynamic_profile.SPEEDS):
            o.append(f"Chapter profile offers {' and '.join(c['speeds'])} only.\n")
        if c["non_monotone_faster"]:
            o.append("Note: a longer step needs a LOWER faster scale than a shorter one at "
                     + ", ".join(f"steps {a}->{b}" for a, b in c["non_monotone_faster"])
                     + " - the next-longer-step rule follows the data as measured.\n")
        o.append("| step | chars | clean scales | faster | default | slower | note |")
        o.append("|---|---|---|---|---|---|---|")
        for s in c["steps"]:
            marks = " ".join(k if v else f"~~{k}~~" for k, v in s["clean"].items())
            note = "; ".join(
                f"word slip `{w['word']}` x{w['takes']} (heard {' / '.join(w['heard'][:2])})"
                for w in s["word_slips"])
            if not s["usable"]:
                note = "no clean scale - the seiyuu stops coping here"
            if not s["judged"]:
                note = (f"{s.get('why_not', 'too short')} - not judged, uses the next step's "
                        f"recipe" + (f"; {note}" if note else ""))
            o.append(f"| {s['step']} | {s['tts_len']} | {marks} | {fmt(s['faster'])} | "
                     f"{fmt(s['default'])} | {fmt(s['slower'])} | {note} |")
        o.append("")
    return "\n".join(o)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Book profiler, stage 4b: recipe")
    parser.add_argument("--book", required=True)
    parser.add_argument("--scope", default="book")
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--chapter", action="append")
    analyze.add_run_options(parser)
    args = parser.parse_args()

    settings = analyze.apply_run_options(load_settings(), args)
    speaker = os.path.abspath(args.speaker)
    base = os.path.join(settings["work_root"], args.book, args.scope)
    with open(os.path.join(base, "analysis", "analysis.json"), "r", encoding="utf-8") as f:
        analysis = json.load(f)
    names = args.chapter or [c["chapter"] for c in analysis["chapters"]]
    scores = []
    for name in names:
        path = os.path.join(sweep.chapter_dir(settings, args.book, args.scope, speaker, name),
                            "score.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                scores.append(json.load(f))
        elif args.chapter:
            raise SystemExit(f"no score.json for {name}: {path}")
    if not scores:
        raise SystemExit("no scored chapters")
    # Judge every take by the scorer's CURRENT rules from what score.json
    # holds - a cleaned chapter has no wavs to re-run score.py on, and its
    # profile must still follow a rule change (2026-09-22: tails).
    score_settings = score.load_settings()
    for s in scores:
        for row in s["rows"]:
            score.flag_takes(row.get("spoken_text", row["text"]), row["takes"], score_settings)

    normalize, _path = analyze.load_irodori_normalizer(settings["irodori_root"])
    engine = analyze.make_engine(normalize)
    every_chapter = [chapter_recipe(s, settings) for s in scores]
    # A chapter whose first judged step has no clean scale gives no bands at
    # all: nothing it could recommend. Said plainly, and left out.
    no_window = [c for c in every_chapter if not c["bands"]]
    chapters = [c for c in every_chapter if c["bands"]]
    for c in no_window:
        print(f"{c['chapter']}: no clean scale range at step {c['limited_by_step']} - "
              f"this seiyuu has no usable speed for the chapter's shortest sentences; "
              f"no profile for it")
    if not chapters:
        print(f"NO CLEAN WINDOW: not one scored chapter has a clean scale range for "
              f"{sweep.nickname_for(speaker)} - no profile written. A rerun cannot change "
              f"this; only different takes can.")
        sys.exit(NO_WINDOW_EXIT)
    by_chapter = sentences_by_chapter(analysis)
    in_scope = [c["chapter"] for c in chapters]
    book_sentences = [s for name in in_scope for s in by_chapter.get(name, [])]
    max_len = max(s["tts_len"] for s in book_sentences)

    book_bands, viable = book_recipe(chapters, max_len)
    # L for the book: the shortest length at which some chapter's seiyuu
    # actually stopped coping. A chapter that never stopped only says "fine
    # up to my longest step", which is a limit of what it had to test, not
    # of the seiyuu - taking the minimum of those cut 48 yojo-senki sentences
    # to chapter_001's 82 characters when 116 had been read cleanly.
    limited = [c["comfortable_length"] for c in chapters
               if c["limited_by_step"] and c["comfortable_length"]]
    measured = [c["comfortable_length"] for c in chapters if c["comfortable_length"]]
    book_limit = min(limited) if limited else (max(measured) if measured else None)
    # The book bands only need to reach L: nothing longer is ever requested.
    book_bands = [b for b in book_bands if book_limit is None or b["from_len"] <= book_limit]
    if book_bands and book_limit:
        book_bands[-1]["to_len"] = min(book_bands[-1]["to_len"], book_limit)
    apply_book = application(book_bands, book_limit, book_sentences, engine)
    costs = {c["chapter"]: chapter_cost(c, book_bands, by_chapter.get(c["chapter"], []), engine)
             for c in chapters}

    out_dir = os.path.join(base, "recipe", sweep.nickname_for(speaker))
    os.makedirs(out_dir, exist_ok=True)
    score_by_chapter = {s["chapter"]: s for s in scores}
    for c in chapters:
        extra = {"steps": c["steps"],
                 "application": application(c["bands"], c["comfortable_length"],
                                            by_chapter.get(c["chapter"], []), engine)}
        c["speeds"], written = speeds_for(c["bands"], settings)
        targets = pace_targets([score_by_chapter[c["chapter"]]], written, settings)
        with open(os.path.join(out_dir, f"profile_{c['chapter']}.json"), "w", encoding="utf-8") as f:
            json.dump(profile(args.book, speaker, "chapter", [c["chapter"]], written,
                              c["comfortable_length"], settings, extra, targets, c["speeds"]),
                      f, ensure_ascii=False, indent=2)
    book_path = os.path.join(out_dir, "profile_book.json")
    book_speeds = book_targets = None
    if viable and book_bands:
        book_speeds, written = speeds_for(book_bands, settings)
        book_targets = pace_targets(scores, written, settings)
        with open(book_path, "w", encoding="utf-8") as f:
            json.dump(profile(args.book, speaker, "book", in_scope, written, book_limit, settings,
                              {"viable": viable, "application": apply_book,
                               "chapter_costs": costs, "no_window": [c["chapter"] for c in no_window]},
                              book_targets, book_speeds),
                      f, ensure_ascii=False, indent=2)
    elif os.path.isfile(book_path):
        # A book profile from an earlier run would read as this run's answer.
        os.remove(book_path)
    with open(os.path.join(out_dir, "recipe.md"), "w", encoding="utf-8") as f:
        f.write(render_md(args.book, speaker, chapters, book_bands, viable, book_limit,
                          apply_book, costs, book_speeds, no_window))
        if book_targets:
            order = [s for s in ("faster", "default", "slower") if s in book_speeds]
            f.write("\nEven pace recipe (book): " + " · ".join(
                f"{s} {book_targets[s]}" for s in order) + " ch/s - the median pace the book "
                "bands give each style.\n")
        table, per_step = pace_view(scores, chapters, settings)
        f.write("\n" + render_pace_md(table, per_step))
    if not viable:
        print("book recipe NOT viable: the chapters' clean windows do not overlap at some "
              "length - no profile_book.json; the chapter profiles are written")
    print(f"recipe for {len(chapters)} chapter(s): book recipe "
          f"{'viable' if viable else 'NOT viable'}, L={book_limit}"
          + (f", speeds {'/'.join(book_speeds)}" if book_speeds else "")
          + (f", {len(no_window)} chapter(s) with no clean window" if no_window else "")
          + f" -> {out_dir}")


if __name__ == "__main__":
    main()
