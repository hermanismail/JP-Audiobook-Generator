"""
illustrator.py
--------------
Core of the Scene Illustrator. One image per chapter (v2, 2026-09-20), with
character sheets (2026-09-21, M9/M10), read and drawn by Google (2026-09-22,
M11), with the local Qwen-Image engine kept for what Google refuses:

    read    each WHOLE chapter -> a summary, the 3 key moments with their
            sentences, who is in the best one (identified across the book),
            and an image prompt that names them (Gemini)
    cast    the people who get a character sheet - a list the user keeps;
            sheets are imported from v1 or drawn here
    write   rewrite a chapter's prompt around the ticked cast, for the moment
            the user picked (Gemini)
    draw    samples for a chapter, with up to 3 cast sheets attached, each
            named by its slot ("Mari is the person in <image2>")
    edit    change one thing in an image and leave the rest alone, optionally
            with sheets attached (e.g. "the case from <image2>")

Images go to the chapter's engine: "google" (Nano Banana 2, ~15 s a take) or
"local" (Qwen-Image-2.1 in ComfyUI, ~4 min). Google refuses the book's
violent and sexual scenes (M11); the CLI then says REFUSED and the window
offers the local engine.

The reasons behind every rule here are in DESIGN.md; the ones that cost real
time to find are commented where they apply.

CLI (the window runs these as child processes, one code path):

    python illustrator.py read   --book <name> --text <chapter folder> [--chapter chapter_003]
    python illustrator.py write  --book <name> --chapter chapter_003 [--cast c002 --cast c003]
    python illustrator.py draw   --book <name> --chapter chapter_003 [--count 2] [--cast c002 ...]
                                 [--engine google|local]
    python illustrator.py sheet  --book <name> --member c002 [--count 2] [--engine ...]
    python illustrator.py edit   --book <name> (--chapter chapter_003 | --member c002)
                                 --base <png> --instruction-file <txt> [--count 2] [--cast c003]
                                 [--engine ...]
    python illustrator.py import-v1 --book <name>

Protocol lines on stdout (the window parses them):

    SERVER <message>
    CHAPTER <i>/<n> <base> ok|failed <secs>
    TAKE <i>/<n> start        PROGRESS <value> <max>
    SAMPLE <i>/<n> <path> <secs>s
    PROMPT <path>             (write: the new prompt, in a text file)
    REFUSED <reason>          (Google declined; exit code 3)
    DONE <summary>
"""

import argparse
import difflib
import glob
import json
import os
import re
import shutil
import sys
import time

import google_engine as ge

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
STYLE_IMAGE = os.path.join(SCRIPT_DIR, "style_ink.png")   # shipped with the tool (decision v4)

DEFAULT_SETTINGS = {
    "work_root": r"F:\tmp\scene-illustrator",
    # Google (M11): reading and prompts always; images unless a chapter is
    # switched to the local engine
    "google_project": "",          # the user's own; lives in the uncommitted settings.json
    "google_location": "global",
    "google_text_model": "gemini-3.8-flash",
    "google_image_model": "gemini-3.1-flash-image",
    "google_aspect": "2:3",
    "image_engine": "google",      # default for chapters and sheets: google | local
    "comfy_root": r"F:\ComfyUI",
    "comfy_port": 8188,
    # Qwen-Image-2.1 draws and edits (M9)
    "qwen_unet": "qwen_image_2.1_Q4_K_M.gguf",
    "qwen_clip": "qwen3vl_8b_w4a8.safetensors",
    "qwen_vae": "qwen_image_2.1_vae_bf16.safetensors",
    # new key names on purpose: a saved 3 from the klein days would otherwise
    # survive, and at ~4 min a take the default is 2 (user decision 2026-09-21)
    "draw_samples": 2,
    "edit_count": 2,
    "last_book": "",
    "last_text_folder": "",
    "last_output_folder": "",   # save_settings() keeps only these keys - it was dropped before
}

# The prompt rules that made the difference (DESIGN.md v2, M8): the references
# supply the drawing style, and the background is kept out of the way.
STYLE_NOTE = ("Black and white manga illustration, detailed ink linework and cross-hatching, "
              "in the same art style as the reference images.")
OLD_STYLE_NOTES = ("Black and white manga illustration, detailed ink linework and cross-hatching, "
                   "same art style as the reference image.",)     # prompts saved before 2026-09-21
MINIMAL_NOTE = ("Only the people named below and what they hold or sit at are drawn. No room, no "
                "windows, no other people, no scenery - the rest of the picture is empty white "
                "paper. No text, letters or signs anywhere in the picture.")
SHEET_NOTE = ("Full body character reference of one person, standing, neutral pose, facing "
              "slightly left, plain white background, nothing else in the picture.")


# ------------------------------------------------------------------ settings
def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings.update(json.load(f))
    return settings


def save_settings(settings):
    write_json(SETTINGS_PATH, {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()})


def write_json(path, data):
    """Atomic: a crash mid-write never leaves half a file behind."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def book_dir(settings, book):
    return os.path.join(settings["work_root"], book)


def book_name_for(text_folder):
    """after-dark\\chapter-text -> after-dark; otherwise the folder's own name."""
    folder = os.path.normpath(text_folder)
    name = os.path.basename(folder)
    if name.lower() in ("chapter-text", "text", "chapters"):
        name = os.path.basename(os.path.dirname(folder))
    return name


# ------------------------------------------------------------------ the book
def chapter_files(text_folder):
    return sorted(glob.glob(os.path.join(text_folder, "chapter_*.txt")))


def chapter_bases(text_folder):
    return [os.path.splitext(os.path.basename(p))[0] for p in chapter_files(text_folder)]


def read_text(path):
    with open(path, encoding="utf-8-sig") as f:   # utf-8-sig: 20 of 82 files carry a BOM
        return f.read()


_SENTENCE_RE = re.compile(r"(?<=[。！？!?])|\n")
_NORM_RE = re.compile(r"[\s「」『』（）()〈〉《》【】]")


def norm(text):
    return _NORM_RE.sub("", text or "")


def sentences(text):
    return [s.strip() for s in _SENTENCE_RE.split(text) if s and s.strip()]


def find_quote(quote, text):
    """The sentence a moment came from, looked up in the text by code - the
    model's quote is only the search key (it trims and glues sentences)."""
    key = norm(quote)
    if not key:
        return "", "none"
    sents = sentences(text)
    for s in sents:
        if key in norm(s):
            return s, "exact"
    best, score = "", 0.0
    for s in sents:
        r = difflib.SequenceMatcher(None, key, norm(s)).ratio()
        if r > score:
            best, score = s, r
    return (best, "fuzzy") if score >= 0.5 else ("", "none")


class ModelFailed(Exception):
    pass


def _google(settings, log):
    return ge.GoogleEngine(settings, log)


# ------------------------------------------------------------- Stage: read
# The whole chapter goes to Gemini (M11): Qwen3.5 read 25% of one, and could
# not tell that 男 in chapter 1 is Takahashi or that the girl is Mari.
READ = """You are choosing ONE illustration for a chapter of a Japanese novel, and keeping track of
who appears in the book.
{known}
Return JSON only:
{{"summary":"","moments":[{{"moment":"","quote":""}}],"best":0,
  "people":[{{"as_written":"","who":"","appearance":""}}],"prompt":""}}
- "summary": 4-6 English sentences on what happens in the chapter.
- "moments": the 3 most important visual moments, in story order. "moment" is one English sentence
  on what the picture shows; "quote" is ONE sentence copied exactly from the text where it happens.
- "best": the index of the moment that best represents the chapter.
- "people": everyone physically present in the BEST moment. "as_written" is how the text refers to
  them there (Japanese, e.g. 男, 女の子). "who" is who they are, using the book's knowledge beyond
  this chapter if you have it - and EXACTLY the name from the known list above when it is one of
  them. "appearance" is a short English description of how they look, from the text.
- "prompt": an English image prompt for the best moment, under 70 words. Name each person by their
  "who" name; say what each does, where each is relative to the others (left, right, seated,
  standing, across a table), and what they hold or sit at. Do NOT describe a room, windows,
  weather or scenery, and do not describe faces, hair or clothes."""

KNOWN_BLOCK = """People already known in this book - use these exact names for them:
{lines}"""


def known_lines(roster, cast):
    """Cast names first (they are what prompts must use), then the rest of the
    roster, each with the names the text has used for them."""
    lines, seen = [], set()
    for member in (cast or {}).get("members", {}).values():
        names = [member["name"]] + member["aliases"]
        lines.append(f"- {member['name']} (also written: {', '.join(member['aliases']) or '-'})")
        seen.update(names)
    for name, rec in roster.items():
        if name not in seen:
            aka = ", ".join(rec.get("aka") or []) or "-"
            lines.append(f"- {name} (also written: {aka}) - {rec.get('appearance', '')[:80]}")
    return "\n".join(lines)


def read_path(root):
    return os.path.join(root, "read.json")


def load_read(root):
    return read_json(read_path(root)) or {"version": 2, "chapters": {}, "roster": {}}


def build_prompt(subject):
    """What actually goes to the image model: the style note, the minimal
    background rule, then the subject. Assembled in code so the rules cannot
    drift (DESIGN.md v2, M8)."""
    return f"{STYLE_NOTE} {MINIMAL_NOTE} {subject.strip()}"


def run_read(settings, book, text_folder, only_chapter, log):
    root = book_dir(settings, book)
    write_json(os.path.join(root, "book.json"),
               {"book": book, "text_folder": os.path.abspath(text_folder)})
    bases = chapter_bases(text_folder)
    if not bases:
        raise RuntimeError(f"no chapter_*.txt in {text_folder}")
    # the Qwen-era reading is kept once, the first time Google reads this
    # book (user decision 2026-09-22: Google's reading replaces it)
    backup = os.path.join(root, "read_local_backup.json")
    data = load_read(root)
    if data.get("reader") != "google":
        if os.path.isfile(read_path(root)) and not os.path.isfile(backup):
            shutil.copy2(read_path(root), backup)
            log(f"SERVER kept the previous reading as {os.path.basename(backup)}")
        data = {"version": 3, "reader": "google", "chapters": {}, "roster": {}}
    cast = load_cast(root)
    engine = _google(settings, log)
    todo = [b for b in bases if not only_chapter or b == only_chapter]
    ok = failed = 0
    log(f"SERVER reading with {settings['google_text_model']}")
    for i, base in enumerate(todo, 1):
        text = read_text(os.path.join(text_folder, base + ".txt"))
        t0 = time.time()
        lines = known_lines(data["roster"], cast)
        system = READ.format(known=KNOWN_BLOCK.format(lines=lines) if lines else "")
        try:
            raw, row = engine.json_call(system, text, what=base)
        except (ge.Refused, RuntimeError) as e:
            failed += 1
            log(f"CHAPTER {i}/{len(todo)} {base} failed {time.time() - t0:.1f}s {e}")
            continue
        moments = []
        for m in raw.get("moments") or []:
            quote, match = find_quote(str(m.get("quote") or ""), text)   # code looks it up (v1)
            moments.append({"moment": str(m.get("moment") or ""), "quote": quote,
                            "quote_match": match})
        try:
            best = max(0, min(int(raw.get("best") or 0), len(moments) - 1))
        except (TypeError, ValueError):
            best = 0
        people = []
        chapter_no = int(base.split("_")[1])
        for p in raw.get("people") or []:
            who = str(p.get("who") or p.get("as_written") or "").strip()
            if not who:
                continue
            rec = data["roster"].setdefault(who, {"appearance": "", "chapters": [], "aka": []})
            if not rec["appearance"]:
                rec["appearance"] = str(p.get("appearance") or "")
            if chapter_no not in rec["chapters"]:
                rec["chapters"].append(chapter_no)
                rec["chapters"].sort()
            written = str(p.get("as_written") or "").strip()
            if written and written != who and written not in rec["aka"]:
                rec["aka"].append(written)
            if who not in people:
                people.append(who)
        pick = moments[best] if moments else {"moment": "", "quote": "", "quote_match": "none"}
        data["chapters"][base] = {
            "summary": str(raw.get("summary") or ""),
            "moments": moments, "best": best,
            "moment": pick["moment"], "quote": pick["quote"], "quote_match": pick["quote_match"],
            "subject": str(raw.get("prompt") or ""),
            "characters": people,
            "secs": round(time.time() - t0, 1), "usd": row["usd"],
        }
        write_json(read_path(root), data)
        ok += 1
        log(f"CHAPTER {i}/{len(todo)} {base} ok {time.time() - t0:.1f}s "
            f"{len(people)} character(s) ${row['usd']:.3f}")
    log(f"DONE read {ok} chapter(s), {failed} failed, {len(data['roster'])} characters known")
    return failed == 0


# --------------------------------------------------------- Stage: chapters
def chapters_path(root):
    return os.path.join(root, "chapters.json")


def load_chapters(root):
    """The saved state, seeded from the reading for any chapter not started.
    Saved choices always win: a re-read never overwrites a prompt the user
    edited, a sample, an edit chain or a final image."""
    data = read_json(chapters_path(root)) or {"version": 2, "chapters": {}}
    read = load_read(root)
    old = unedited_prompts(root)
    for base, record in read["chapters"].items():
        entry = data["chapters"].setdefault(base, new_chapter())
        seeded = build_prompt(record["subject"])
        # a prompt the user never touched follows a new reading; an edited one
        # (or one written by Write prompt) is theirs and stays
        if not entry["prompt"] or entry["prompt"] in old.get(base, ()) or \
                entry["prompt"] == entry.get("seeded_from"):
            entry["prompt"] = seeded
        if entry["prompt"] == seeded:
            entry["seeded_from"] = seeded
        entry["summary"] = record.get("summary", "")
        entry["moments"] = record.get("moments") or []
        pick = entry.get("moment_pick")
        if pick is None or not (0 <= pick < len(entry["moments"])):
            pick = record.get("best", 0)
        if entry["moments"]:
            entry["moment"] = entry["moments"][pick]["moment"]
            entry["quote"] = entry["moments"][pick]["quote"]
        else:
            entry["moment"] = record.get("moment", "")
            entry["quote"] = record.get("quote", "")
        entry["characters"] = record.get("characters", [])
    for base in list(data["chapters"]):
        adopt_loose_files(root, base, data["chapters"][base])
    return data


def unedited_prompts(root):
    """Per chapter, the prompts the previous (Qwen) reading would have seeded,
    in both style-note spellings - a saved prompt equal to one of them was
    never edited, so it may follow the new reading."""
    backup = read_json(os.path.join(root, "read_local_backup.json")) or {}
    out = {}
    for base, record in backup.get("chapters", {}).items():
        subject = (record.get("subject") or "").strip()
        out[base] = {f"{note} {MINIMAL_NOTE} {subject}" for note in (STYLE_NOTE,) + OLD_STYLE_NOTES}
    return out


def adopt_loose_files(root, base, entry):
    """List any take that is on disk but not in the file - a draw started from
    the command line, or a run that died after writing an image. Samples join
    the sample list; edits join the last round, or a round of their own."""
    listed = set(entry["samples"]) | {p for s in entry["chain"] for p in s["takes"]}
    for kind in ("samples", "edits"):
        folder = os.path.join(chapter_dir(root, base), kind)
        found = sorted(glob.glob(os.path.join(folder, "*.png")))
        loose = [p for p in found if p not in listed]
        if not loose:
            continue
        if kind == "samples":
            entry["samples"] += loose
        elif entry["chain"]:
            entry["chain"][-1]["takes"] += loose
        else:
            entry["chain"].append({"change": "(found on disk)", "keep": "", "instruction": "",
                                   "parent": entry["chosen"], "takes": loose, "chosen": ""})


def new_chapter():
    return {"prompt": "", "moment": "", "quote": "", "characters": [],
            "summary": "", "moments": [],
            "moment_pick": None, # which of the reading's moments; None = the reading's best
            "engine": None,      # google | local; None = the default in settings
            "cast": None,        # cast member ids attached; None = not set, derive from aliases
            "samples": [], "chosen": "",   # the drawn samples; chosen = working image
            "chain": [],         # [{instruction, parent, takes:[...], chosen, secs}]
            "final": ""}         # promoted chapter image


def save_chapters(root, data):
    write_json(chapters_path(root), data)


def chapter_dir(root, base):
    return os.path.join(root, "chapters", base)


def next_path(root, base, kind, owner_dir=None):
    """samples: sample_001.png; edits: edit_001.png - one flat folder per
    chapter, or per cast member when `owner_dir` is given."""
    folder = os.path.join(owner_dir or chapter_dir(root, base), kind)
    os.makedirs(folder, exist_ok=True)
    n = 1
    stem = "sample" if kind == "samples" else "edit"
    while os.path.exists(os.path.join(folder, f"{stem}_{n:03d}.png")):
        n += 1
    return os.path.join(folder, f"{stem}_{n:03d}.png")


MAX_SHEETS = 3             # M10: a 4th sheet lost a character and hit the VRAM ceiling
MAX_EDIT_SHEETS = 2        # an edit's base image takes one of the slots


# --------------------------------------------------------------- the cast
# A list the user keeps. The roster from `read` is only a hint for it: in
# after-dark it recorded chapter 1's Takahashi and Mari as 男 / 女の子, merged
# 男 with 白川 and filed マリ under 浅井エリ (M10), so it cannot decide whose
# sheet goes into a picture.
def cast_path(root):
    return os.path.join(root, "cast.json")


def new_member(name=""):
    return {"name": name,        # the name prompts use ("Mari")
            "aliases": [],       # roster names that mean this person (マリ, 女の子)
            "tag": "",           # optional: "in the baseball cap" - helps who-does-what at 3 (M10)
            "description": "",   # what a new sheet is drawn from
            "samples": [],       # sheet takes and edits
            "sheet": ""}         # the chosen one


def load_cast(root):
    data = read_json(cast_path(root)) or {"version": 1, "members": {}}
    for member in data["members"].values():
        for key, value in new_member().items():
            member.setdefault(key, value)
    return data


def save_cast(root, data):
    write_json(cast_path(root), data)


def member_dir(root, member_id):
    return os.path.join(root, "cast", member_id)


def new_member_id(cast):
    n = 1
    while f"m{n:03d}" in cast["members"]:
        n += 1
    return f"m{n:03d}"


def import_v1_sheets(root):
    """v1's chosen character sheets (refs.json + bible.json in the same book
    folder) become cast members, one each. The image is COPIED into the cast
    folder, so deleting v1's work never takes a sheet with it. Names come as
    v1 had them ("Mari (Protagonist)" -> "Mari"); aliases are the user's to
    add. A member already imported is skipped. Returns the ids added."""
    import shutil

    refs = read_json(os.path.join(root, "refs.json")) or {}
    bible = read_json(os.path.join(root, "bible.json")) or {}
    people = bible.get("characters") or []
    if isinstance(people, dict):
        people = [dict(v, id=k) for k, v in people.items()]
    names = {c.get("id"): c.get("name", "") for c in people}
    cast = load_cast(root)
    taken = {m.get("v1_id") for m in cast["members"].values()}
    added = []
    for v1_id, rec in (refs.get("characters") or {}).items():
        chosen = next((v["chosen"] for v in rec.get("variants", []) if v.get("chosen")), "")
        if not chosen or not os.path.isfile(chosen) or v1_id in taken:
            continue
        name = re.sub(r"\s*\(.*?\)\s*$", "", names.get(v1_id) or v1_id).strip() or v1_id
        member_id = new_member_id(cast)
        member = new_member(name)
        member["v1_id"] = v1_id
        dst = os.path.join(member_dir(root, member_id), "samples", "sample_001.png")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(chosen, dst)
        member["samples"] = [dst]
        member["sheet"] = dst
        cast["members"][member_id] = member
        added.append(member_id)
    if added:
        save_cast(root, cast)
    return added


def derived_cast(cast, characters):
    """The members a chapter's reading points at: its roster names matched
    against each member's name and aliases. Only a first guess - the user's
    ticks replace it the moment they touch one."""
    out = []
    wanted = set(characters or [])
    for member_id, member in cast["members"].items():
        if wanted & ({member["name"]} | set(member["aliases"])) and member["sheet"]:
            out.append(member_id)
    return out[:MAX_SHEETS]


def chapter_cast(cast, record):
    ids = record.get("cast")
    if ids is None:
        ids = derived_cast(cast, record.get("characters"))
    return [i for i in ids if i in cast["members"] and cast["members"][i]["sheet"]][:MAX_SHEETS]


def spoken_name(member):
    return f"{member['name']} ({member['tag'].strip()})" if member.get("tag", "").strip() else member["name"]


def cast_line(members, first_slot=1):
    """'Mari is the person in <image1>. ...' - names alone hold the faces
    (M10, test N); a tag rides along when the user gave one."""
    return " ".join(f"{spoken_name(m)} is the person in <image{i}>."
                    for i, m in enumerate(members, first_slot))


def with_cast(prompt, line):
    """Put the cast line right after the style sentence, where the measured
    prompts had it; before everything else if the prompt was edited away
    from that shape."""
    if not line:
        return prompt
    for note in (STYLE_NOTE,) + OLD_STYLE_NOTES:
        if prompt.startswith(note):
            return f"{STYLE_NOTE} {line} {prompt[len(note):].strip()}"
    return f"{line} {prompt}"


# ------------------------------------------------------------ Stage: write
WRITE = """Write the illustration prompt for one picture from a chapter of a Japanese novel (the
chapter text follows). Return JSON only: {{"subject":""}}
- The picture shows this moment: {moment}
  It happens at this sentence: {quote}
- "subject": English, under 70 words. These people have a reference picture that shows how they
  look, so name them EXACTLY like this and do NOT describe their faces, hair or clothes:
{names}
  Anyone else who is in the moment gets a short description of how they look instead of a name.
  Say what each person is doing, where each is relative to the others (left, right, seated,
  standing, across a table), and what they hold or sit at, true to the text. Do NOT describe a
  room, windows, weather or scenery."""


def run_write(settings, book, base, member_ids, log):
    """Gemini writes the chapter's prompt around the attached cast, for the
    moment the user picked, reading the whole chapter again so positions and
    objects come from the text. The window reads the result from
    PROMPT <path>, so the child never writes chapters.json under a window
    that holds it open."""
    root = book_dir(settings, book)
    entry = load_chapters(root)["chapters"].get(base)
    if not entry or not entry.get("moment"):
        raise RuntimeError(f"{base} has not been read yet")
    book_info = read_json(os.path.join(root, "book.json")) or {}
    text_path = os.path.join(book_info.get("text_folder", ""), base + ".txt")
    if not os.path.isfile(text_path):
        raise RuntimeError(f"chapter text not found: {text_path}")
    cast = load_cast(root)
    members = [cast["members"][i] for i in member_ids if i in cast["members"]]
    if not members:
        raise RuntimeError("tick at least one cast member first")
    names = "\n".join(f"  - {m['name']}" for m in members)
    t0 = time.time()
    log(f"SERVER writing with {settings['google_text_model']}")
    raw, row = _google(settings, log).json_call(
        WRITE.format(moment=entry["moment"], quote=entry.get("quote", ""), names=names),
        read_text(text_path), what=f"write {base}")
    subject = str(raw.get("subject") or "").strip()
    if not subject:
        raise RuntimeError("the model returned no prompt")
    path = os.path.join(chapter_dir(root, base), "written_prompt.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_prompt(subject))
    log(f"PROMPT {path}")
    log(f"DONE wrote a prompt in {time.time() - t0:.0f}s (${row['usd']:.3f})")


# ------------------------------------------------------- Stage: draw, edit
def _engine(settings, log):
    import comfy
    engine = comfy.ManagedComfy(settings, log)
    engine.check_models()
    return engine


class Declined(Exception):
    """Google refused the picture; the CLI exits 3 so the window can offer
    the local engine."""


def engine_for(settings, record_or_member):
    name = (record_or_member or {}).get("engine") or settings.get("image_engine") or "google"
    return name if name in ("google", "local") else "google"


def _takes(settings, engine_name, log, count, prompt, refs, out_for, edit=False):
    """`count` takes on the chosen engine. `refs` are file paths, in slot
    order: <image1> is refs[0] for both engines."""
    import random
    if engine_name == "local":
        engine = _engine(settings, log)
        with engine:
            names = [engine.put_reference(p, f"si_ref{i}.png") for i, p in enumerate(refs, 1)]
            for i in range(count):
                out = out_for()
                log(f"TAKE {i + 1}/{count} start")
                secs = engine.run(prompt, names, random.randrange(2 ** 48), out, edit=edit,
                                  on_progress=lambda v, m: log(f"PROGRESS {v} {m}"))
                log(f"SAMPLE {i + 1}/{count} {out} {secs}s")
        return
    google = _google(settings, log)
    # each image is labelled with its slot, so "<image2>" in the prompt means
    # the same picture it means to the local engine
    parts = []
    for i, path in enumerate(refs, 1):
        parts += [f"<image{i}>:", path]
    parts.append(prompt)
    log(f"SERVER {settings['google_image_model']} with {len(refs)} image(s)")
    for i in range(count):
        out = out_for()
        log(f"TAKE {i + 1}/{count} start")
        try:
            secs = google.image(parts, out, what=f"take {i + 1}")
        except ge.Refused as e:
            log(f"REFUSED {e}")
            raise Declined(str(e))
        log(f"SAMPLE {i + 1}/{count} {out} {secs}s")


def sheet_members(root, member_ids, limit):
    cast = load_cast(root)
    members = []
    for member_id in member_ids or []:
        member = cast["members"].get(member_id)
        if not member:
            raise RuntimeError(f"no cast member {member_id}")
        if not member["sheet"] or not os.path.isfile(member["sheet"]):
            raise RuntimeError(f"{member['name']} has no sheet yet")
        members.append((member_id, member))
    if len(members) > limit:
        raise RuntimeError(f"at most {limit} sheets here - {len(members)} were given (M10)")
    return members


def run_draw(settings, book, base, count, member_ids, engine_name, log):
    """`count` samples for one chapter. Up to 3 cast sheets, each named by its
    slot. The style sample: locally only when no sheet is attached (decision
    q4 - the sheets carry the style); on Google always, last, because that is
    how the M11 trial held the ink style."""
    root = book_dir(settings, book)
    entry = load_chapters(root)["chapters"].get(base) or new_chapter()
    engine_name = engine_name or engine_for(settings, entry)
    prompt = entry["prompt"].strip()
    if not prompt:
        raise RuntimeError(f"{base} has no prompt yet - read the book first")
    members = sheet_members(root, member_ids, MAX_SHEETS)
    if not os.path.isfile(STYLE_IMAGE):
        raise RuntimeError(f"style image missing: {STYLE_IMAGE}")
    refs = [m["sheet"] for _, m in members]
    if members:
        prompt = with_cast(prompt, cast_line([m for _, m in members]))
        log(f"SERVER drawing on {engine_name} with the sheets of "
            + ", ".join(m["name"] for _, m in members))
    else:
        log(f"SERVER drawing on {engine_name} with the style sample (no cast attached)")
    if engine_name == "google" or not members:
        refs.append(STYLE_IMAGE)
        if members:
            prompt = with_style_slot(prompt, len(refs))
    _takes(settings, engine_name, log, count, prompt, refs, lambda: next_path(root, base, "samples"))
    log(f"DONE drew {count}")


def with_style_slot(prompt, slot):
    return f"{prompt} <image{slot}> is only the drawing style to follow - nobody from it is drawn."


def run_sheet(settings, book, member_id, count, engine_name, log):
    """New character sheets: the member's description, full body on white,
    with the style sample attached."""
    root = book_dir(settings, book)
    member = load_cast(root)["members"].get(member_id)
    if not member:
        raise RuntimeError(f"no cast member {member_id}")
    if not member["description"].strip():
        raise RuntimeError(f"{member['name']} has no description to draw from")
    if not os.path.isfile(STYLE_IMAGE):
        raise RuntimeError(f"style image missing: {STYLE_IMAGE}")
    engine_name = engine_name or engine_for(settings, None)
    prompt = f"{STYLE_NOTE} {SHEET_NOTE} {member['description'].strip()}"
    log(f"SERVER drawing a sheet for {member['name']} on {engine_name}")
    _takes(settings, engine_name, log, count, prompt, [STYLE_IMAGE],
           lambda: next_path(root, member_id, "samples", member_dir(root, member_id)))
    log(f"DONE drew {count}")


EDIT_TEMPLATE = ("Keep <image1> exactly as it is - the same people, the same faces, poses, clothes "
                 "and lines. {cast}Change ONLY this: {change}{keep}")


def edit_prompt(change, keep, members=()):
    """The phrasing that works. Measured (DESIGN.md v2 M6, M9 test C): an
    instruction must say what changes AND restate that everything else stays,
    or the change lands on the wrong person. Attached sheets follow the image
    being edited, from <image2>. The user writes the change; the tool writes
    the rest."""
    keep_part = f" {keep.strip()}" if keep and keep.strip() else ""
    if keep_part and not keep_part.rstrip().endswith("."):
        keep_part += "."
    change = change.strip()
    if not change.endswith("."):
        change += "."
    line = cast_line(list(members), first_slot=2)
    return (EDIT_TEMPLATE.format(cast=f"{line} " if line else "", change=change, keep=keep_part)
            + " Everything else identical.")


def run_edit(settings, book, base, member, count, base_image, instruction, member_ids,
             engine_name, log):
    """`count` takes of one change against `base_image` - a chapter's image
    (`base`) or a cast member's sheet (`member`). The instruction already
    names the attached sheets (edit_prompt); here they are only loaded, in
    the same order."""
    root = book_dir(settings, book)
    if not os.path.isfile(base_image):
        raise RuntimeError(f"image to edit not found: {base_image}")
    members = sheet_members(root, member_ids, MAX_EDIT_SHEETS)
    if member:
        out_for = lambda: next_path(root, member, "edits", member_dir(root, member))   # noqa: E731
        engine_name = engine_name or engine_for(settings, None)
    else:
        out_for = lambda: next_path(root, base, "edits")   # noqa: E731
        engine_name = engine_name or engine_for(
            settings, load_chapters(root)["chapters"].get(base))
    refs = [base_image] + [m["sheet"] for _, m in members]
    log(f"SERVER editing on {engine_name} with {len(refs)} image(s)")
    _takes(settings, engine_name, log, count, instruction, refs, out_for, edit=True)
    log(f"DONE edited {count}")


def export_final(root, base, final_path, output_folder):
    """The player reads chapter_<N>_img_<i>.png next to the chapter audio; v2
    writes exactly one per chapter."""
    number = base.split("_")[1]
    dst = os.path.join(output_folder, f"chapter_{number}_img_1.png")
    import comfy
    comfy.to_greyscale(final_path, dst)
    return dst


def clear_history(root, base, keep_paths):
    """Delete every take of a chapter except the ones named - the final image
    and whatever the user still wants (decision v14: history must be
    deletable, it is gigabytes)."""
    keep = {os.path.normpath(p) for p in keep_paths if p}
    removed = 0
    for kind in ("samples", "edits"):
        folder = os.path.join(chapter_dir(root, base), kind)
        for path in glob.glob(os.path.join(folder, "*.png")):
            if os.path.normpath(path) not in keep:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    return removed


# --------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("--book", required=True)
    r.add_argument("--text", required=True)
    r.add_argument("--chapter", default="")
    w = sub.add_parser("write")
    w.add_argument("--book", required=True)
    w.add_argument("--chapter", required=True)
    w.add_argument("--cast", action="append", default=[])
    d = sub.add_parser("draw")
    d.add_argument("--book", required=True)
    d.add_argument("--chapter", required=True)
    d.add_argument("--count", type=int, default=2)
    d.add_argument("--cast", action="append", default=[])
    d.add_argument("--engine", choices=["google", "local"])
    s = sub.add_parser("sheet")
    s.add_argument("--book", required=True)
    s.add_argument("--member", required=True)
    s.add_argument("--count", type=int, default=2)
    s.add_argument("--engine", choices=["google", "local"])
    e = sub.add_parser("edit")
    e.add_argument("--book", required=True)
    target = e.add_mutually_exclusive_group(required=True)
    target.add_argument("--chapter")
    target.add_argument("--member")
    e.add_argument("--base", required=True)
    e.add_argument("--instruction-file", required=True)
    e.add_argument("--count", type=int, default=2)
    e.add_argument("--cast", action="append", default=[])
    e.add_argument("--engine", choices=["google", "local"])
    i = sub.add_parser("import-v1")
    i.add_argument("--book", required=True)
    args = ap.parse_args()

    def log(line):
        print(line, flush=True)

    settings = load_settings()
    try:
        if args.cmd == "read":
            sys.exit(0 if run_read(settings, args.book, args.text, args.chapter, log) else 2)
        if args.cmd == "write":
            run_write(settings, args.book, args.chapter, args.cast, log)
            return
        if args.cmd == "draw":
            run_draw(settings, args.book, args.chapter, args.count, args.cast, args.engine, log)
            return
        if args.cmd == "sheet":
            run_sheet(settings, args.book, args.member, args.count, args.engine, log)
            return
        if args.cmd == "import-v1":
            added = import_v1_sheets(book_dir(settings, args.book))
            log(f"DONE imported {len(added)} sheet(s)")
            return
        # utf-8-sig: a BOM from the writer would otherwise reach the model as
        # the first character of the instruction
        with open(args.instruction_file, encoding="utf-8-sig") as f:
            instruction = f.read().strip()
        run_edit(settings, args.book, args.chapter, args.member, args.count, args.base,
                 instruction, args.cast, args.engine, log)
    except (Declined, ge.Refused) as e:
        if isinstance(e, ge.Refused):        # a Declined take already said so
            log(f"REFUSED {e}")
        log("DONE refused by Google - the local engine can draw this")
        sys.exit(3)
    except (RuntimeError, ModelFailed) as e:
        log(f"DONE error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
