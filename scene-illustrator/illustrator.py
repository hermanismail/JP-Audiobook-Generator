"""
illustrator.py
--------------
Core of the Scene Illustrator (Stage A, 2026-09-20): read a book with a
local LLM, then keep the cast-and-places "bible" the user curates.

CLI (the window runs these as child processes, one code path):

    python illustrator.py read    --book <name> --text <chapter folder>
    python illustrator.py suggest --book <name>

Protocol lines on stdout (the window parses them):

    SERVER <message>
    PIECE <i>/<n> <piece id> ok|failed <secs> [detail]
    SUGGEST <n> groups
    DONE <summary>

Everything a run produces lives under <work_root>/<book>/ and a run can
be stopped and re-run: finished pieces are kept, failed ones are retried.
See DESIGN.md for the decisions behind each rule.
"""

import argparse
import difflib
import glob
import hashlib
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

DEFAULT_SETTINGS = {
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llm_model": r"F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf",
    "llm_port": 8080,
    "llm_ctx": 24576,
    "work_root": r"F:\tmp\scene-illustrator",
    "piece_chars": 7000,
    "comfy_root": r"F:\ComfyUI",
    "comfy_port": 8188,
    "flux_unet": "flux-2-klein-4b-fp8.safetensors",
    "flux_clip": "qwen_3_4b.safetensors",
    "flux_vae": "flux2-vae.safetensors",
    "takes": 3,
    "last_book": "",
    "last_text_folder": "",
}


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
    """Atomic: a crash mid-write never leaves half a bible behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
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


def read_text(path):
    with open(path, encoding="utf-8-sig") as f:   # utf-8-sig: 20 of 82 files carry a BOM
        return f.read()


def pieces(text, limit):
    """Split at line breaks into pieces of at most `limit` characters."""
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > limit:
            out.append(buf)
            buf = ""
        buf += line
    if buf.strip():
        out.append(buf)
    return out


def all_pieces(text_folder, limit):
    """[(piece_id, chapter_base, text)] in reading order."""
    result = []
    for path in chapter_files(text_folder):
        base = os.path.splitext(os.path.basename(path))[0]
        for i, p in enumerate(pieces(read_text(path), limit), 1):
            result.append((f"{base}_{i}", base, p))
    return result


# ---------------------------------------------------------- evidence lookup
_SENTENCE_RE = re.compile(r"(?<=[。！？!?])|\n")
_NORM_RE = re.compile(r"[\s「」『』（）()〈〉《》【】]")


def norm(text):
    return _NORM_RE.sub("", text or "")


def sentences(text):
    return [s.strip() for s in _SENTENCE_RE.split(text) if s and s.strip()]


def find_source(quote, piece_text):
    """The sentence of the piece a detail comes from. The model's quote is
    only the search key (decision 2026-09-20): it may be trimmed or glued.
    Returns (sentence, "exact"|"fuzzy"|"none")."""
    key = norm(quote)
    if not key:
        return "", "none"
    sents = sentences(piece_text)
    for s in sents:
        if key in norm(s):
            return s, "exact"
    # a quote spanning several sentences: find each of its own sentences
    parts = [norm(p) for p in sentences(quote) if norm(p)]
    if len(parts) > 1:
        found = []
        for p in parts:
            hit = next((s for s in sents if p in norm(s)), None)
            if hit is None:
                break
            if hit not in found:
                found.append(hit)
        else:
            return "".join(found), "exact"
    best, score = "", 0.0
    for s in sents:
        r = difflib.SequenceMatcher(None, key, norm(s)).ratio()
        if r > score:
            best, score = s, r
    return (best, "fuzzy") if score >= 0.5 else ("", "none")


# -------------------------------------------------------------- llama-server
class ManagedLlamaServer:
    """Starts llama-server only if nothing listens on the port, and only ever
    stops a server it started (the translate_pipeline rule). The whole
    process tree is killed so VRAM really comes back."""

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
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True)
            self.log("SERVER stopped")
        self.proc = None
        return False


class ModelFailed(Exception):
    pass


def chat(url, system, user, max_tokens, attempts=(None, 1, 2, 3, 4)):
    """One JSON reply. The model occasionally emits a broken UTF-8 sequence
    (server 500) or malformed JSON; a different seed samples a different
    path, so retry with fresh seeds before giving up."""
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


# ----------------------------------------------------------------- Stage A
EXTRACT = """You are reading part of a Japanese novel to prepare illustrations.
List every CHARACTER and every PLACE that appears in this passage.
Rules:
- ONLY people and places PHYSICALLY PRESENT in a scene of this passage. Skip anyone or anywhere
  that is only talked about, remembered, dreamed of or planned (family in a story, cities named
  in conversation, a prison someone was in years ago, a place they will travel to).
- Include minor characters who act or speak.
- Only facts the text actually states. Never invent appearance details.
- Assign each detail ONLY to the person the sentence describes. If a sentence describes person A
  while person B is also in the scene, the detail belongs to A alone. When unsure, leave it out.
- Character: "name" exactly as written in the text (Japanese), "aliases" (other ways the text
  refers to them), "role" (a short English phrase).
- "details": how they LOOK, in ENGLISH only, one short fact each (hair, face, build, age, clothes,
  carried items for people; layout, furniture, light for places). NOT actions, gestures, speech or
  feelings ("raises a finger", "brings a menu", "looks tired" are not details). A person with no
  stated appearance gets an empty list. Each with "quote": a SHORT exact
  Japanese quote (max 40 characters) from the passage that states it.
Return JSON only:
{"characters":[{"name":"","aliases":[],"role":"","details":[{"detail":"","quote":""}]}],
 "places":[{"name":"","details":[{"detail":"","quote":""}]}]}"""

EXTRACT_NO_QUOTE = EXTRACT.replace(
    'Each with "quote": a SHORT exact\n  Japanese quote (max 40 characters) from the passage that states it.',
    'Leave "quote" as "" (empty).')
assert EXTRACT_NO_QUOTE != EXTRACT


def piece_path(root, piece_id, failed=False):
    return os.path.join(root, "read", "pass1", piece_id + (".failed.json" if failed else ".json"))


def clean_piece(raw, piece_text):
    """Normalise the model's reply and attach the source sentence to every
    detail by code lookup."""
    out = {"characters": [], "places": []}
    for kind in ("characters", "places"):
        for item in raw.get(kind) or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            details = []
            for d in item.get("details") or []:
                if isinstance(d, str):
                    d = {"detail": d, "quote": ""}
                text = str(d.get("detail") or "").strip()
                if not text:
                    continue
                source, match = find_source(d.get("quote", ""), piece_text)
                details.append({"text": text, "quote": d.get("quote", ""),
                                "source": source, "match": match})
            entry = {"name": name, "details": details}
            if kind == "characters":
                entry["aliases"] = [a for a in item.get("aliases") or [] if a and a != name]
                entry["role"] = str(item.get("role") or "")
            out[kind].append(entry)
    return out


def run_read(settings, book, text_folder, log):
    root = book_dir(settings, book)
    write_json(os.path.join(root, "book.json"),
               {"book": book, "text_folder": os.path.abspath(text_folder)})
    todo = all_pieces(text_folder, int(settings["piece_chars"]))
    if not todo:
        raise RuntimeError(f"no chapter_*.txt in {text_folder}")
    pending = [p for p in todo if not os.path.isfile(piece_path(root, p[0]))]
    log(f"SERVER {len(todo)} pieces, {len(todo) - len(pending)} already read")
    ok = failed = 0
    if pending:
        with ManagedLlamaServer(settings, log) as server:
            for i, (pid, _base, text) in enumerate(todo, 1):
                if os.path.isfile(piece_path(root, pid)):
                    continue
                t0 = time.time()
                note = ""
                try:
                    try:
                        raw = chat(server.url, EXTRACT, text, 4000)
                    except ModelFailed:
                        # seen on after-dark ch.16: every seed aborted while copying a quote
                        raw = chat(server.url, EXTRACT_NO_QUOTE, text, 4000)
                        note = "without quotes"
                    data = clean_piece(raw, text)
                    data["_meta"] = {"secs": round(time.time() - t0, 1), "note": note}
                    write_json(piece_path(root, pid), data)
                    if os.path.isfile(piece_path(root, pid, failed=True)):
                        os.remove(piece_path(root, pid, failed=True))
                    ok += 1
                    log(f"PIECE {i}/{len(todo)} {pid} ok {time.time() - t0:.1f}s {note}".rstrip())
                except ModelFailed as e:
                    write_json(piece_path(root, pid, failed=True), {"error": str(e)})
                    failed += 1
                    log(f"PIECE {i}/{len(todo)} {pid} failed {time.time() - t0:.1f}s {e}")
    log(f"DONE read {ok} new, {failed} failed, {len(todo) - len(pending)} kept")
    return failed == 0


# ------------------------------------------------------------- the bible
KINDS = ("characters", "places")


def raw_entries(root):
    """Every piece's names merged by EXACT name: {kind: {name: {...}}}."""
    raw = {k: {} for k in KINDS}
    for path in sorted(glob.glob(os.path.join(root, "read", "pass1", "*.json"))):
        if path.endswith(".failed.json"):
            continue
        pid = os.path.basename(path)[:-5]
        chapter = int(pid.split("_")[1])
        data = read_json(path, {})
        for kind in KINDS:
            for item in data.get(kind, []):
                e = raw[kind].setdefault(item["name"], {"aliases": [], "roles": [], "chapters": [],
                                                         "details": []})
                if chapter not in e["chapters"]:
                    e["chapters"].append(chapter)
                for a in item.get("aliases", []):
                    if a not in e["aliases"]:
                        e["aliases"].append(a)
                if item.get("role") and item["role"] not in e["roles"]:
                    e["roles"].append(item["role"])
                for d in item.get("details", []):
                    e["details"].append({**d, "chapter": chapter, "piece": pid,
                                         "key": detail_key(pid, item["name"], d["text"])})
    return raw


def detail_key(pid, name, text):
    return hashlib.sha1(f"{pid}|{name}|{text}".encode("utf-8")).hexdigest()[:12]


def bible_path(root):
    return os.path.join(root, "bible.json")


def load_bible(root):
    """The saved bible, extended with anything new the reader found. Saved
    choices always win (the onboarder's rule): an existing entry, its name,
    its kept/dropped details and deletions are never touched; only raw
    names nobody has placed yet become new entries, and details new to an
    entry's members are appended."""
    bible = read_json(bible_path(root)) or {"version": 1, "characters": [], "places": [],
                                            "deleted": {"characters": [], "places": []}}
    raw = raw_entries(root)
    for kind in KINDS:
        placed = {m for e in bible[kind] for m in e["members"]} | set(bible["deleted"][kind])
        for e in bible[kind]:
            known = {d["key"] for d in e["details"]}
            for m in e["members"]:
                for d in raw[kind].get(m, {}).get("details", []):
                    if d["key"] not in known:
                        e["details"].append({**d, "keep": True, "new": True})
                        known.add(d["key"])
                r = raw[kind].get(m)
                if r:
                    e["chapters"] = sorted(set(e["chapters"]) | set(r["chapters"]))
        for name, r in raw[kind].items():
            if name in placed:
                continue
            entry = {"id": new_id(bible, kind), "name": name, "members": [name],
                     "aliases": list(r["aliases"]), "roles": list(r["roles"]),
                     "chapters": sorted(r["chapters"]), "main": False, "note": "",
                     "details": [{**d, "keep": True} for d in r["details"]]}
            if kind == "places":
                entry.pop("aliases"); entry.pop("roles")
            bible[kind].append(entry)
    return bible


def new_id(bible, kind):
    prefix = "c" if kind == "characters" else "p"
    used = {e["id"] for e in bible[kind]}
    n = 1
    while f"{prefix}{n:03d}" in used:
        n += 1
    return f"{prefix}{n:03d}"


def save_bible(root, bible):
    write_json(bible_path(root), bible)


def find(bible, kind, entry_id):
    return next(e for e in bible[kind] if e["id"] == entry_id)


def merge(bible, kind, ids, name):
    """Pool several entries into the first; the others disappear."""
    entries = [find(bible, kind, i) for i in ids]
    target = entries[0]
    for e in entries[1:]:
        target["members"] += [m for m in e["members"] if m not in target["members"]]
        known = {d["key"] for d in target["details"]}
        target["details"] += [d for d in e["details"] if d["key"] not in known]
        target["chapters"] = sorted(set(target["chapters"]) | set(e["chapters"]))
        target["main"] = target["main"] or e["main"]
        target["note"] = "\n".join(x for x in (target["note"], e["note"]) if x)
        if kind == "characters":
            for field in ("aliases", "roles"):
                target[field] += [a for a in e[field] if a not in target[field]]
        bible[kind].remove(e)
    target["name"] = name
    if kind == "characters":
        target["aliases"] = [a for a in dict.fromkeys(target["aliases"] + target["members"]) if a != name]
    return target


def delete(bible, kind, entry_id):
    e = find(bible, kind, entry_id)
    bible["deleted"][kind] += [m for m in e["members"] if m not in bible["deleted"][kind]]
    bible[kind].remove(e)


def add_detail(entry, text):
    key = hashlib.sha1(f"user|{entry['id']}|{text}|{time.time()}".encode()).hexdigest()[:12]
    entry["details"].append({"text": text, "quote": "", "source": "", "match": "user",
                             "chapter": 0, "piece": "", "key": key, "keep": True})


# --------------------------------------------------- Stage B: references
DEFAULT_STYLE_NOTE = ("Black and white manga illustration, detailed ink linework and "
                      "cross-hatching, same art style as the reference image.")
CHARACTER_FRAME = ("Full body character reference, standing, neutral pose, facing slightly left, "
                   "plain white background.")
PLACE_FRAME = "Establishing shot of the place, no people in the picture."


def refs_path(root):
    return os.path.join(root, "refs.json")


def load_refs(root):
    refs = read_json(refs_path(root)) or {"version": 1, "style_image": "", "style_note": DEFAULT_STYLE_NOTE,
                                          "characters": {}, "places": {}}
    if rehome_samples(refs):
        save_refs(root, refs)
    return refs


def rehome_samples(refs):
    """A take belongs to the entry whose folder it was written into. Moves
    any sample listed under the wrong entry back where it belongs - the
    window used to append a finished job's takes to whatever was selected
    at the time (fixed 2026-09-20), and this repairs files written then."""
    moved = 0
    for kind in KINDS:
        for entry_id, record in list(refs[kind].items()):
            for index, variant in enumerate(record["variants"]):
                for path in list(variant["samples"]):
                    parts = os.path.normpath(path).split(os.sep)
                    if len(parts) < 3 or parts[-3] == entry_id:
                        continue
                    owner, folder = parts[-3], parts[-2]
                    target_index = int(folder[1:]) - 1 if folder[1:].isdigit() else 0
                    variant["samples"].remove(path)
                    if variant["chosen"] == path:
                        variant["chosen"] = ""
                    variants = refs[kind].setdefault(owner, {"variants": []})["variants"]
                    while len(variants) <= target_index:
                        variants.append({"label": "", "prompt": "", "samples": [], "chosen": ""})
                    if path not in variants[target_index]["samples"]:
                        variants[target_index]["samples"].append(path)
                    moved += 1
    return moved


def save_refs(root, refs):
    write_json(refs_path(root), refs)


def style_image_path(root):
    return os.path.join(root, "refs", "style.png")


def build_prompt(entry, kind, style_note, variant_label=""):
    """The first prompt for a reference sheet, from the details the user kept.
    Assembled in code (not by the LLM): predictable, instant, and the user
    edits it anyway."""
    facts = [d["text"].strip().rstrip(".") for d in entry["details"] if d["keep"]]
    frame = CHARACTER_FRAME if kind == "characters" else PLACE_FRAME
    who = ""
    if kind == "characters":
        roles = [r for r in entry.get("roles", []) if r]
        who = f"{roles[0]}. " if roles else ""
    bits = [style_note, frame, who + (", ".join(facts) + "." if facts else "")]
    if variant_label:
        bits.append(variant_label.strip().rstrip(".") + ".")
    return " ".join(b for b in bits if b.strip())


def variants_of(refs, kind, entry_id):
    return refs[kind].setdefault(entry_id, {"variants": []})["variants"]


def add_variant(refs, kind, entry, label, style_note):
    v = {"label": label, "prompt": build_prompt(entry, kind, style_note, label),
         "samples": [], "chosen": ""}
    variants_of(refs, kind, entry["id"]).append(v)
    return v


def sample_dir(root, kind, entry_id, variant_index):
    return os.path.join(root, "refs", kind, entry_id, f"v{variant_index + 1}")


def next_sample_path(root, kind, entry_id, variant_index):
    folder = sample_dir(root, kind, entry_id, variant_index)
    os.makedirs(folder, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(folder, f"sample_{n:03d}.png")):
        n += 1
    return os.path.join(folder, f"sample_{n:03d}.png")


def run_draw(settings, book, kind, entry_id, variant_index, count, log):
    """Draw `count` samples for one reference variant. The GUI owns refs.json,
    so this prints SAMPLE lines and never writes it."""
    import random
    import comfy

    root = book_dir(settings, book)
    refs = load_refs(root)
    variant = variants_of(refs, kind, entry_id)[variant_index]
    style = style_image_path(root)
    with comfy.ManagedComfy(settings, log) as engine:
        ref_names = []
        if os.path.isfile(style):
            ref_names.append(engine.put_reference(style, f"si_{book}_style.png"))
        else:
            log("SERVER no style image set - drawing without one")
        for i in range(count):
            out = next_sample_path(root, kind, entry_id, variant_index)
            secs = engine.draw(variant["prompt"], ref_names, random.randrange(2 ** 48), out)
            log(f"SAMPLE {i + 1}/{count} {out} {secs}s")
    log("DONE drew " + str(count))


# ------------------------------------------------------- suggest merges
SUGGEST = """Below is a numbered list of {kind} from one Japanese novel, each with the chapters it
appears in and a short note. Some entries may be the SAME {kind_one} under different names (full
name vs surname, a description like "the big woman", a job title). Propose groups ONLY for entries
you are confident are the same. Leave everything else out.
Return JSON only: {{"groups":[{{"name":"best name","members":[1,2],"why":"short reason"}}]}}"""


def run_suggest(settings, book, log):
    root = book_dir(settings, book)
    bible = load_bible(root)
    result = {}
    with ManagedLlamaServer(settings, log) as server:
        for kind in KINDS:
            entries = bible[kind]
            lines = []
            for i, e in enumerate(entries, 1):
                facts = "; ".join(d["text"] for d in e["details"] if d["keep"])[:100]
                extra = ", ".join(e.get("aliases", []) + e.get("roles", []))
                lines.append(f"{i}. {e['name']} | ch {e['chapters']} | {extra} | {facts}")
            if len(entries) < 2:
                result[kind] = []
                continue
            raw = chat(server.url, SUGGEST.format(kind=kind, kind_one=kind[:-1]), "\n".join(lines), 3000)
            groups = []
            for g in raw.get("groups", []):
                ids = [entries[m - 1]["id"] for m in g.get("members", [])
                       if isinstance(m, int) and 0 < m <= len(entries)]
                ids = list(dict.fromkeys(ids))
                if len(ids) >= 2:
                    groups.append({"name": g.get("name") or find(bible, kind, ids[0])["name"],
                                   "ids": ids, "why": g.get("why", "")})
            result[kind] = groups
            log(f"SUGGEST {len(groups)} {kind} groups")
    write_json(os.path.join(root, "suggestions.json"), result)
    log("DONE suggestions written")


# --------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("--book", required=True)
    r.add_argument("--text", required=True)
    s = sub.add_parser("suggest")
    s.add_argument("--book", required=True)
    d = sub.add_parser("draw")
    d.add_argument("--book", required=True)
    d.add_argument("--kind", required=True, choices=KINDS)
    d.add_argument("--id", required=True)
    d.add_argument("--variant", type=int, default=0)
    d.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    def log(line):
        print(line, flush=True)

    settings = load_settings()
    try:
        if args.cmd == "read":
            sys.exit(0 if run_read(settings, args.book, args.text, log) else 2)
        if args.cmd == "draw":
            run_draw(settings, args.book, args.kind, args.id, args.variant, args.count, log)
            return
        run_suggest(settings, args.book, log)
    except (RuntimeError, ModelFailed) as e:
        log(f"DONE error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
