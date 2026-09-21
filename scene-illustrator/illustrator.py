"""
illustrator.py
--------------
Core of the Scene Illustrator. One image per chapter (v2, 2026-09-20), drawn
and edited by Qwen-Image-2.1 with character sheets (2026-09-21, DESIGN.md
M9/M10):

    read    the first 25% of each chapter -> a drawable moment, an image
            prompt, and a character roster (local LLM through llama-server)
    cast    the people who get a character sheet - a list the user keeps,
            because the roster is not reliable enough to decide (M10);
            sheets are imported from v1 or drawn here
    write   rewrite a chapter's prompt so it names the cast (local LLM)
    draw    samples for a chapter, with up to 3 cast sheets attached, each
            named by its slot ("Mari is the person in <image2>"); the style
            sample instead when nobody is attached
    edit    change one thing in an image and leave the rest alone, optionally
            with sheets attached (e.g. "the case from <image2>")

The reasons behind every rule here are in DESIGN.md; the ones that cost real
time to find are commented where they apply.

CLI (the window runs these as child processes, one code path):

    python illustrator.py read   --book <name> --text <chapter folder> [--chapter chapter_003]
    python illustrator.py write  --book <name> --chapter chapter_003 [--cast c002 --cast c003]
    python illustrator.py draw   --book <name> --chapter chapter_003 [--count 2] [--cast c002 ...]
    python illustrator.py sheet  --book <name> --member c002 [--count 2]
    python illustrator.py edit   --book <name> (--chapter chapter_003 | --member c002)
                                 --base <png> --instruction-file <txt> [--count 2] [--cast c003]
    python illustrator.py import-v1 --book <name>

Protocol lines on stdout (the window parses them):

    SERVER <message>
    CHAPTER <i>/<n> <base> ok|failed <secs>
    TAKE <i>/<n> start        PROGRESS <value> <max>
    SAMPLE <i>/<n> <path> <secs>s
    PROMPT <path>             (write: the new prompt, in a text file)
    DONE <summary>
"""

import argparse
import difflib
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
STYLE_IMAGE = os.path.join(SCRIPT_DIR, "style_ink.png")   # shipped with the tool (decision v4)

DEFAULT_SETTINGS = {
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llm_model": r"F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf",
    "llm_port": 8080,
    "llm_ctx": 24576,
    "work_root": r"F:\tmp\scene-illustrator",
    "read_fraction": 0.25,
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


def opening(text, fraction):
    """The chapter's first `fraction`, cut at a line break so no sentence is
    halved (decision v6: the image comes from this part of the chapter)."""
    target = max(int(len(text) * fraction), 400)
    if len(text) <= target:
        return text
    cut = text.rfind("\n", 0, target)
    return text[:cut if cut > target // 2 else target]


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


# -------------------------------------------------------------- llama-server
class ManagedLlamaServer:
    """Starts llama-server only if nothing listens on the port, and only ever
    stops a server it started. The whole tree is killed so VRAM comes back."""

    def __init__(self, settings, log):
        self.s = settings
        self.log = log
        self.proc = None
        self.url = f"http://127.0.0.1:{settings['llm_port']}"

    def _listening(self):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", int(self.s["llm_port"]))) == 0

    def __enter__(self):
        if self._listening():
            self.log(f"SERVER already listening on port {self.s['llm_port']} - using it as-is")
            return self
        for key in ("llama_server_exe", "llm_model"):
            if not os.path.isfile(self.s[key]):
                raise RuntimeError(f"{key} not found: {self.s[key]}")
        cmd = [self.s["llama_server_exe"], "-m", self.s["llm_model"], "-c", str(self.s["llm_ctx"]),
               "-ngl", "99", "-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
               # this llama.cpp build's output parser aborts valid replies (500)
               # unless reasoning is off and left unparsed (measured 2026-09-18)
               "--reasoning", "off", "--reasoning-format", "none",
               "--host", "127.0.0.1", "--port", str(self.s["llm_port"])]
        self.log("SERVER starting " + os.path.basename(self.s["llm_model"]))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        t0 = time.time()
        while time.time() - t0 < 180:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self.proc.returncode}")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        self.log(f"SERVER ready in {time.time() - t0:.0f}s")
                        return self
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(1)
        self.__exit__(None, None, None)
        raise RuntimeError("llama-server did not become ready in 180 s")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
            self.log("SERVER stopped")
        self.proc = None
        return False


class ModelFailed(Exception):
    pass


def chat(url, system, user, max_tokens, attempts=(None, 1, 2, 3, 4)):
    """One JSON reply. The model occasionally emits a broken UTF-8 sequence
    (server 500) or malformed JSON; a different seed samples a different path,
    so retry with fresh seeds before giving up."""
    body = {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    last = ""
    for seed in attempts:
        if seed is not None:
            body["seed"] = seed
        try:
            req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as r:
                reply = json.load(r)
            content = reply["choices"][0]["message"]["content"].split("</think>")[-1]
            content = content[content.index("{"):content.rindex("}") + 1]   # drop ```json fences
            return json.loads(content, strict=False)                        # raw newlines in strings
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except ValueError as e:
            last = f"bad JSON ({e})"
    raise ModelFailed(last)


# ------------------------------------------------------------- Stage: read
READ = """You are choosing ONE illustration for this part of a Japanese novel chapter, and keeping
track of who appears in the book.
{roster_block}
Return JSON only:
{{"moment":"","quote":"","subject":"","characters":[{{"name":"","known_as":"","appearance":""}}]}}
- "moment": one English sentence saying what is happening in the picture.
- "quote": ONE sentence copied EXACTLY from the passage below, where that moment happens.
- "subject": an English image prompt for it: who is in the picture, what they are doing, how they
  are positioned, and what they hold or sit at. Describe each person by how they LOOK (hair, face,
  build, clothes). Do NOT describe a room, furniture beyond what they touch, weather, or other
  people. Under 70 words. Never name a character.
- "characters": the people in the picture. "name" as the text writes it (Japanese). "known_as" is
  the matching name from the roster above if this is the same person, else "". "appearance" is a
  short English description from the text.
Only people physically present in this passage."""

ROSTER_BLOCK = """Characters already known in this book (name - description - chapters):
{lines}
If a person below is one of them, put that name in "known_as"."""


def read_path(root):
    return os.path.join(root, "read.json")


def load_read(root):
    return read_json(read_path(root)) or {"version": 2, "chapters": {}, "roster": {}}


def roster_lines(roster):
    return "\n".join(f"- {name} - {rec['appearance'][:90]} - ch {rec['chapters']}"
                     for name, rec in roster.items()) or "- (none yet)"


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
    data = load_read(root)
    todo = [b for b in bases if not only_chapter or b == only_chapter]
    fraction = float(settings["read_fraction"])
    ok = failed = 0
    with ManagedLlamaServer(settings, log) as server:
        for i, base in enumerate(todo, 1):
            text = read_text(os.path.join(text_folder, base + ".txt"))
            part = opening(text, fraction)
            t0 = time.time()
            block = ROSTER_BLOCK.format(lines=roster_lines(data["roster"])) if data["roster"] else ""
            try:
                raw = chat(server.url, READ.format(roster_block=block), part, 1200)
            except ModelFailed as e:
                failed += 1
                log(f"CHAPTER {i}/{len(todo)} {base} failed {time.time() - t0:.1f}s {e}")
                continue
            quote, match = find_quote(raw.get("quote", ""), text)
            people = []
            for c in raw.get("characters") or []:
                name = str(c.get("name") or "").strip()
                if not name:
                    continue
                known = str(c.get("known_as") or "").strip()
                canonical = known if known in data["roster"] else name
                rec = data["roster"].setdefault(canonical, {"appearance": "", "chapters": [], "aka": []})
                if not rec["appearance"]:
                    rec["appearance"] = str(c.get("appearance") or "")
                chapter_no = int(base.split("_")[1])
                if chapter_no not in rec["chapters"]:
                    rec["chapters"].append(chapter_no)
                    rec["chapters"].sort()
                if name != canonical and name not in rec["aka"]:
                    rec["aka"].append(name)
                people.append(canonical)
            data["chapters"][base] = {
                "moment": str(raw.get("moment") or ""),
                "quote": quote, "quote_match": match,
                "subject": str(raw.get("subject") or ""),
                "characters": people,
                "secs": round(time.time() - t0, 1),
            }
            write_json(read_path(root), data)
            ok += 1
            log(f"CHAPTER {i}/{len(todo)} {base} ok {time.time() - t0:.1f}s "
                f"{len(people)} character(s)")
        # second pass (decision v6): every chapter re-checked against the FULL
        # roster, so a character first met in chapter 9 is also recognised in
        # chapter 2, which the sequential pass could not know.
        if not only_chapter and data["roster"]:
            log("SERVER second pass: cross-checking characters against the whole book")
            for base, record in data["chapters"].items():
                names = record["characters"]
                if not names:
                    continue
                try:
                    res = chat(server.url,
                               "Match each name to the roster. Return JSON only: "
                               '{"pairs":[{"name":"","roster":""}]} - "roster" is the roster name '
                               'for the same person, or "" if none matches.',
                               "Roster:\n" + roster_lines(data["roster"]) + "\n\nNames:\n"
                               + "\n".join(f"- {n}" for n in names), 800)
                except ModelFailed:
                    continue
                mapping = {p.get("name"): p.get("roster") for p in res.get("pairs") or []}
                merged = []
                for n in names:
                    target = mapping.get(n) or n
                    if target not in data["roster"]:
                        target = n
                    if target not in merged:
                        merged.append(target)
                    rec = data["roster"].get(target)
                    chapter_no = int(base.split("_")[1])
                    if rec and chapter_no not in rec["chapters"]:
                        rec["chapters"].append(chapter_no)
                        rec["chapters"].sort()
                record["characters"] = merged
            write_json(read_path(root), data)
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
    for base, record in read["chapters"].items():
        entry = data["chapters"].setdefault(base, new_chapter())
        if not entry["prompt"]:
            entry["prompt"] = build_prompt(record["subject"])
        entry["moment"] = record.get("moment", "")
        entry["quote"] = record.get("quote", "")
        entry["characters"] = record.get("characters", [])
    for base in list(data["chapters"]):
        adopt_loose_files(root, base, data["chapters"][base])
    return data


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
WRITE = """Rewrite an illustration prompt for one picture from a novel so that it names the people
in it. Each named person already has a reference picture that shows how they look, so do NOT
describe faces, hair or clothes. Return JSON only: {{"subject":""}}
- "subject": English, under 70 words. Say who is in the picture using EXACTLY these names:
{names}
  what each of them is doing, where each is relative to the others (left, right, seated, standing,
  across a table), and what they hold or sit at. Only these people - no one else. Do NOT describe a
  room, windows, weather or scenery."""


def run_write(settings, book, base, member_ids, log):
    """Qwen3.5 rewrites the chapter's reading into a prompt that names the
    attached cast. The window reads the result from PROMPT <path>, so the
    child never writes chapters.json under a window that holds it open."""
    root = book_dir(settings, book)
    record = load_read(root)["chapters"].get(base)
    if not record:
        raise RuntimeError(f"{base} has not been read yet")
    cast = load_cast(root)
    members = [cast["members"][i] for i in member_ids if i in cast["members"]]
    if not members:
        raise RuntimeError("tick at least one cast member first")
    names = "\n".join(f"  - {m['name']}" for m in members)
    source = (f"What happens: {record.get('moment', '')}\n"
              f"The sentence: {record.get('quote', '')}\n"
              f"The current prompt: {record.get('subject', '')}")
    with ManagedLlamaServer(settings, log) as server:
        t0 = time.time()
        raw = chat(server.url, WRITE.format(names=names), source, 600)
    subject = str(raw.get("subject") or "").strip()
    if not subject:
        raise RuntimeError("the model returned no prompt")
    path = os.path.join(chapter_dir(root, base), "written_prompt.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_prompt(subject))
    log(f"PROMPT {path}")
    log(f"DONE wrote a prompt in {time.time() - t0:.0f}s")


# ------------------------------------------------------- Stage: draw, edit
def _engine(settings, log):
    import comfy
    engine = comfy.ManagedComfy(settings, log)
    engine.check_models()
    return engine


def _takes(engine, log, count, prompt, refs, out_for, edit=False):
    import random
    for i in range(count):
        out = out_for()
        log(f"TAKE {i + 1}/{count} start")
        secs = engine.run(prompt, refs, random.randrange(2 ** 48), out, edit=edit,
                          on_progress=lambda v, m: log(f"PROGRESS {v} {m}"))
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


def run_draw(settings, book, base, count, member_ids, log):
    """`count` samples for one chapter. Up to 3 cast sheets, each named by its
    slot; the style sample only when no sheet is attached (user decision
    2026-09-21: the sheets carry the style themselves)."""
    root = book_dir(settings, book)
    entry = load_chapters(root)["chapters"].get(base) or new_chapter()
    prompt = entry["prompt"].strip()
    if not prompt:
        raise RuntimeError(f"{base} has no prompt yet - read the book first")
    members = sheet_members(root, member_ids, MAX_SHEETS)
    if not members and not os.path.isfile(STYLE_IMAGE):
        raise RuntimeError(f"style image missing: {STYLE_IMAGE}")
    engine = _engine(settings, log)
    with engine:
        if members:
            refs = [engine.put_reference(m["sheet"], f"si_{book}_sheet_{i}.png")
                    for i, (_, m) in enumerate(members, 1)]
            prompt = with_cast(prompt, cast_line([m for _, m in members]))
            log("SERVER drawing with the sheets of " + ", ".join(m["name"] for _, m in members))
        else:
            refs = [engine.put_reference(STYLE_IMAGE, f"si_{book}_style.png")]
            log("SERVER drawing with the style sample (no cast attached)")
        _takes(engine, log, count, prompt, refs, lambda: next_path(root, base, "samples"))
    log(f"DONE drew {count}")


def run_sheet(settings, book, member_id, count, log):
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
    prompt = f"{STYLE_NOTE} {SHEET_NOTE} {member['description'].strip()}"
    engine = _engine(settings, log)
    with engine:
        refs = [engine.put_reference(STYLE_IMAGE, f"si_{book}_style.png")]
        log(f"SERVER drawing a sheet for {member['name']}")
        _takes(engine, log, count, prompt, refs,
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


def run_edit(settings, book, base, member, count, base_image, instruction, member_ids, log):
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
    else:
        out_for = lambda: next_path(root, base, "edits")   # noqa: E731
    engine = _engine(settings, log)
    with engine:
        refs = [engine.put_reference(base_image, f"si_{book}_edit_base.png")]
        refs += [engine.put_reference(m["sheet"], f"si_{book}_edit_sheet{i}.png")
                 for i, (_, m) in enumerate(members, 1)]
        log(f"SERVER editing with {len(refs)} image(s)")
        _takes(engine, log, count, instruction, refs, out_for, edit=True)
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
    s = sub.add_parser("sheet")
    s.add_argument("--book", required=True)
    s.add_argument("--member", required=True)
    s.add_argument("--count", type=int, default=2)
    e = sub.add_parser("edit")
    e.add_argument("--book", required=True)
    target = e.add_mutually_exclusive_group(required=True)
    target.add_argument("--chapter")
    target.add_argument("--member")
    e.add_argument("--base", required=True)
    e.add_argument("--instruction-file", required=True)
    e.add_argument("--count", type=int, default=2)
    e.add_argument("--cast", action="append", default=[])
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
            run_draw(settings, args.book, args.chapter, args.count, args.cast, log)
            return
        if args.cmd == "sheet":
            run_sheet(settings, args.book, args.member, args.count, log)
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
                 instruction, args.cast, log)
    except (RuntimeError, ModelFailed) as e:
        log(f"DONE error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
