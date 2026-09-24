"""
preview.py
----------
What Preview Chapters shows, and what it leaves behind for the render.
No Tk in here (preview_window.py is the window).

## What the preview is

Exactly what dynamic mode would send and display, built with the same
`dynamic_profile.plan_chapter()` the generator renders with - furigana
applied, the book's readings applied, the seiyuu credit injected. Two
columns per SOURCE SECTION (blank-line separated), one line per TTS
request:

    text to TTS                     text to Reader
    ぼくはくたびれた…               ぼくはくたびれた…
    きみは歩き疲れたように…         きみは歩き疲れたように…

## What Save leaves behind (user decisions 2026-09-24)

The chapter's .txt is NEVER touched. Instead the preview writes a PLAN
into the generator's temp folder, and the generator renders that plan
instead of planning the chapter again:

    <temp>/preview/<book>/<chapter>.plan.json

The plan is bound to the source text, the profile and the style by
`source_hash`. If any of them changes the plan is stale and ignored, so an
edited preview can never be applied to different text. It is deleted once
the chapter has rendered; a run that died leaves it behind, and
`salvage()` decides whether it can still be used.

Every changed box is also recorded in the suite library
(`chapter_edits`), with the hash of the section it was made against - that
is the lasting record, and what lets a later render offer the same edits
again.
"""

import hashlib
import json
import os
import re

import dynamic_profile
import suite_link
import text_pipeline as tp

PLAN_VERSION = 1


def source_hash(raw_text, profile_path, style):
    """Binds a plan to the text, the profile and the style it was made for."""
    digest = hashlib.sha1()
    for part in (raw_text or "", os.path.abspath(profile_path or ""), style or ""):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def section_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------ building

def build(raw_text, profile, style, engine, readings=None, furigana_applied=None,
          intro=None, undecided=None):
    """[{index, gap, lines: [{display, tts, gap, engine_len, band, request}]}]

    `intro` is (display line, tts line) - injected exactly as
    run_audiobook.process_chapter_dynamic does, so the preview shows the
    credit the render will produce."""
    intro_display, intro_tts = intro if intro else (None, None)
    if intro_display:
        raw_text = insert_intro(raw_text, intro_display)
        readings = sorted((readings or []) + suite_link.intro_readings(intro),
                          key=lambda r: -len(r["word"]))
    pieces, skipped = dynamic_profile.plan_chapter(raw_text, profile, style, engine, readings,
                                                   furigana_applied)
    sections = []
    for piece in pieces:
        number = piece.get("section", 0)
        if not sections or sections[-1]["index"] != number:
            sections.append({"index": number, "gap": piece["gap"], "lines": []})
        sections[-1]["lines"].append({
            "display": piece["display_text"],
            "tts": piece["tts_text"],
            "gap": piece["gap"],
            "engine_len": piece["engine_len"],
            "band": piece["band"],
            "request": piece["request"],
            "readings": piece["readings"],
            # Furigana nobody has ruled on yet. It is STRIPPED from this
            # text - the seiyuu decides - so without marking it the preview
            # cannot be used to judge the review (user, 2026-09-24).
            "candidates": candidates_in(piece["display_text"], undecided),
        })
    return sections, skipped, pieces


def candidates_in(text, undecided):
    """{word: reading} for the undecided pairs whose word is in `text`.

    EVERY occurrence counts, not only the annotated one, because that is
    exactly the question the review asks: the annotation is read as
    written either way, and what is being decided is the bare occurrences
    elsewhere."""
    found = {}
    for word, reading in (undecided or ()):
        if word and word in (text or ""):
            found[word] = reading
    return found


def insert_intro(raw_text, line):
    """run_audiobook.insert_intro_line, kept here so the preview does not
    import the pipeline driver (which builds globals from settings.json)."""
    lines = raw_text.splitlines()
    if not lines or not _is_header(lines[0]):
        return "\n".join([line, ""] + lines) + "\n"
    at = 2 if len(lines) > 1 and lines[1].strip() and _is_header(lines[1]) else 1
    return "\n".join(lines[:at] + [line] + lines[at:]) + "\n"


def _is_header(line):
    text = (line or "").strip()
    return bool(text) and len(text) <= 40 and not any(m in text for m in ("。", "？", "！", "……"))


def as_text(section, side):
    """One box's contents: a line per TTS request."""
    return "\n".join(line["display" if side == "reader" else "tts"] for line in section["lines"])


# ------------------------------------------------------------ editing

def apply_edits(sections, edits):
    """`edits` is {(section index, side): text}; returns new sections with
    those boxes replaced, and the list of problems.

    The two columns must keep the same number of lines: a request is one
    line on each side, so a reader line without its TTS line (or the other
    way round) has no meaning."""
    out, problems = [], []
    for section in sections:
        reader = edits.get((section["index"], "reader"))
        tts = edits.get((section["index"], "tts"))
        if reader is None and tts is None:
            out.append(section)
            continue
        reader_lines = _lines(reader) if reader is not None else \
            [line["display"] for line in section["lines"]]
        tts_lines = _lines(tts) if tts is not None else [line["tts"] for line in section["lines"]]
        if len(reader_lines) != len(tts_lines):
            problems.append(f"section {section['index']}: {len(tts_lines)} line(s) to the TTS "
                            f"but {len(reader_lines)} to the reader - each request is one line "
                            f"on both sides")
            out.append(section)
            continue
        lines = []
        for i, (shown, spoken) in enumerate(zip(reader_lines, tts_lines)):
            old = section["lines"][i] if i < len(section["lines"]) else None
            lines.append({
                "display": shown,
                "tts": spoken,
                # A line the person added takes the ordinary sentence gap.
                "gap": old["gap"] if old else "sentence",
                "engine_len": None, "band": None, "request": None,
                "readings": old["readings"] if old else {},
                "candidates": old.get("candidates", {}) if old else {},
                # Only a line that really changed counts as edited - the
                # rest of the box is the text the preview built.
                "edited": old is None or shown != old["display"] or spoken != old["tts"]
                or bool(old.get("edited")),
            })
        out.append(dict(section, lines=lines))
    return out, problems


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def remeasure(sections, profile, style, engine):
    """Re-price every line after an edit: an edited line is measured as it
    will be SPOKEN, like everything else since 2026-09-24, so its band and
    request follow the text actually there."""
    for section in sections:
        for line in section["lines"]:
            length = len(engine(line["tts"]))
            band, beyond = dynamic_profile.band_for(profile, length)
            line["engine_len"] = length
            line["band"] = f"{band['from_len']}-{band['to_len']}"
            line["beyond"] = beyond
            line["request"] = dynamic_profile.request_for(profile, style, length)
    return sections


# ------------------------------------------------------------ the plan file

def plan_dir(temp_dir, book_slug):
    return os.path.join(temp_dir, "preview", re.sub(r"[^\w.-]+", "_", book_slug or "book"))


def plan_path(temp_dir, book_slug, chapter):
    return os.path.join(plan_dir(temp_dir, book_slug), f"{chapter}.plan.json")


def write_plan(temp_dir, book_slug, chapter, sections, raw_text, profile_path, style,
               intro_line=None, edit_ids=None):
    path = plan_path(temp_dir, book_slug, chapter)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": PLAN_VERSION,
        "book": book_slug,
        "chapter": chapter,
        "profile_path": os.path.abspath(profile_path),
        "style": style,
        "intro_line": intro_line,
        "source_hash": source_hash(raw_text, profile_path, style),
        "edit_ids": list(edit_ids or []),
        "sections": [{"index": s["index"],
                      "lines": [{"display": line["display"], "tts": line["tts"],
                                 "gap": line["gap"], "edited": bool(line.get("edited"))}
                                for line in s["lines"]]}
                     for s in sections],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def read_plan(path):
    """The plan, or None when it is missing or unreadable. A half-written
    or corrupt file is treated as no plan at all (user decision
    2026-09-24: salvage what can be salvaged, otherwise start over)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        return None
    if plan.get("version") != PLAN_VERSION or not plan.get("sections"):
        return None
    for section in plan["sections"]:
        if not isinstance(section.get("lines"), list):
            return None
        for line in section["lines"]:
            if "display" not in line or "tts" not in line:
                return None
    return plan


def usable_plan(temp_dir, book_slug, chapter, raw_text, profile_path, style):
    """(plan, why) - `plan` is None when there is nothing to use, and `why`
    says what happened, for the log."""
    path = plan_path(temp_dir, book_slug, chapter)
    if not os.path.isfile(path):
        return None, None
    plan = read_plan(path)
    if plan is None:
        return None, f"a preview plan for {chapter} was unreadable - ignored and removed"
    if plan.get("source_hash") != source_hash(raw_text, profile_path, style):
        return None, (f"the preview plan for {chapter} was made for different text, profile or "
                      f"style - ignored and removed")
    edits = sum(1 for s in plan["sections"] for line in s["lines"] if line.get("edited"))
    return plan, (f"using the preview plan for {chapter}: {len(plan['sections'])} section(s), "
                  f"{edits} edited line(s)")


def discard_plan(temp_dir, book_slug, chapter):
    path = plan_path(temp_dir, book_slug, chapter)
    try:
        os.remove(path)
    except OSError:
        return False
    return True


def salvage(temp_dir, book_slug, chapters):
    """What is left in the preview folder after a failed or stopped run.
    Returns (kept, removed) chapter names; anything that is not a plan for
    one of `chapters` is removed, because it can never be used again."""
    folder = plan_dir(temp_dir, book_slug)
    kept, removed = [], []
    if not os.path.isdir(folder):
        return kept, removed
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".plan.json"):
            continue
        chapter = name[:-len(".plan.json")]
        path = os.path.join(folder, name)
        if chapter in chapters and read_plan(path) is not None:
            kept.append(chapter)
        else:
            try:
                os.remove(path)
                removed.append(chapter)
            except OSError:
                pass
    return kept, removed


# ------------------------------------------------------- plan -> pieces

def pieces_from_plan(plan, profile, style, engine, first_gap="section"):
    """The render list, priced from the plan's text. Same shape
    plan_pieces() returns, so the generator does not care which it got."""
    pieces = []
    for section in plan["sections"]:
        for number, line in enumerate(section["lines"]):
            length = len(engine(line["tts"]))
            band, beyond = dynamic_profile.band_for(profile, length)
            gap = line.get("gap") or "sentence"
            if not pieces:
                gap = first_gap
            pieces.append({
                "section": section["index"],
                "sentence": len(pieces) + 1,
                "piece": 1,
                "display_text": line["display"],
                "tts_text": line["tts"],
                "readings": {},
                "engine_len": length,
                "gap": gap,
                "band": f"{band['from_len']}-{band['to_len']}",
                "beyond": beyond,
                "request": dynamic_profile.request_for(profile, style, length),
                "removed_before": "",
                "removed_after": "",
                "edited": bool(line.get("edited")),
            })
    return pieces
