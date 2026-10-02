"""
aligner.py
----------
Fits a PUBLISHED English translation (an epub) onto a finished chapter, so
the reader's English mode (text style D) can show the real book instead of
the VNTL subtitles. Built 2026-10-01.

    epub  -> units (one per chapter; chapter003 + 003a..003d joined by stem)
          -> English sentences
    sync.json chunks + English sentences
          -> monotonic alignment (LaBSE embeddings, 1-1 / 1-2 / 2-1 / 1-3 /
             3-1 / skips) -> groups
          -> human review (app.py)
          -> <chapter>.en.srt

The sidecar is NEW and additive: `.srt` is never touched. Same format as the
`.srt` (UTF-8, no BOM, LF, 1-based cues found by time) with one difference
the reader was told about: a cue may span SEVERAL chunks, because an
English sentence often covers two or three Japanese ones. A chunk with no
English simply has no cue.

Measured before building (scratchpad, 2026-10-01): yojo-senki ch.1 x vol. 1
prologue, 432 chunks x 431 sentences, 349 one-to-one groups; machi ch.1,
121 x 115, 107 one-to-one; no wrong pairing found by eye in either.
Similarity runs LOW on correct pairs (0.32 seen), so a low score alone is
not a flag - a skip is.
"""

import html
import json
import os
import re
import zipfile
import posixpath
from urllib.parse import unquote

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

DEFAULTS = {
    "work_root": r"F:\tmp\translation-aligner",
    "model": "sentence-transformers/LaBSE",
    "skip_cost": 0.30,      # a group must beat this similarity to be taken
    "low_sim": 0.45,        # shown red in review; NOT a flag on its own
    "credit_template": "Narrated by {name}",
}

BEADS = [(1, 1), (1, 2), (2, 1), (1, 3), (3, 1), (1, 0), (0, 1)]
MIN_UNIT_CHARS = 200        # an epub doc shorter than this is a title page
BAND_MIN = 120              # alignment search band around the diagonal

CHUNK_RE = re.compile(r"^(chapter_\d+)\.sync\.json$")
CREDIT_RE = re.compile(r"^朗読者[：、:]")
ABBREV = ("Mr.", "Mrs.", "Ms.", "Dr.", "St.", "Lt.", "Col.", "Gen.", "Capt.",
          "Maj.", "Sgt.", "Prof.", "No.", "vs.", "etc.", "e.g.", "i.e.")


def load_settings():
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    return s


# ---------------------------------------------------------------------------
# the book: sync.json chapters
# ---------------------------------------------------------------------------

def book_chapters(folder):
    """Every chapter with a sync.json, in order: [(base, chunks)]."""
    out = []
    for name in sorted(os.listdir(folder)):
        m = CHUNK_RE.match(name)
        if m:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                out.append((m.group(1), json.load(f)["chunks"]))
    return out


def load_glossary(folder):
    path = os.path.join(folder, "glossary.json")
    if not os.path.exists(path):
        return {"characters": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def credit_text(jp, glossary, template):
    """The seiyuu credit in English, from the glossary (user decision). The
    generator adds the seiyuu to glossary.json when it renders, so the
    spelling is the one chosen there. None if no entry matches."""
    if not CREDIT_RE.match(jp):
        return None
    name = re.sub(r"\s+", "", CREDIT_RE.sub("", jp))
    for ch in glossary.get("characters", []):
        jn = re.sub(r"\s+", "", ch.get("name", ""))
        if jn and jn == name and ch.get("en"):
            return template.format(name=ch["en"])
    return None


# ---------------------------------------------------------------------------
# the epub: units and sentences
# ---------------------------------------------------------------------------

def _spine(z):
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    opf = z.read(opf_path).decode("utf-8", "ignore")
    base = posixpath.dirname(opf_path)
    items = {}
    for tag in re.findall(r"<item\b[^>]*>", opf):
        i = re.search(r'\bid="([^"]+)"', tag)
        h = re.search(r'\bhref="([^"]+)"', tag)
        if i and h:
            # hrefs are URLs: a Calibre epub writes spaces as %20
            href = unquote(html.unescape(h.group(1)).split("#")[0])
            items[i.group(1)] = posixpath.normpath(posixpath.join(base, href))
    title = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    order = [items[r] for r in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', opf) if r in items]
    return order, html.unescape(title.group(1)).strip() if title else ""


def _paragraphs(raw, title):
    out = []
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", raw, re.S):
        # footnote references (`Londinium.<a href="footnote.xhtml#..">
        # <sup>6</sup></a>`) would become text: "Londinium.6", then a
        # stray sentence "6 If..." (found by the player, 2026-10-02).
        # Only a NUMBER (or * / †) in superscript goes; `1<sup>st</sup>` stays.
        p = re.sub(r"<a\b[^>]*>\s*<sup\b[^>]*>\s*[\d*†‡]+\s*</sup>\s*</a>", "", p, flags=re.S)
        p = re.sub(r"<sup\b[^>]*>\s*(?:<a\b[^>]*>)?\s*[\d*†‡]+\s*(?:</a>)?\s*</sup>", "", p, flags=re.S)
        t = html.unescape(re.sub(r"<[^>]+>", "", p))
        t = re.sub(r"\s+", " ", t).strip()
        # scene breaks (`*`, `* * *`, `◇`): nothing to read, and as a 0-1
        # group they would be glued onto the previous cue
        if not t or not re.search(r"\w", t):
            continue
        # running heads: the book's title, and "Chapter 34, <title>"
        if title and (t == title or t.startswith(title + ",")):
            continue
        if title and re.match(r"^Chapter \w+, " + re.escape(title), t):
            continue
        out.append(t)
    return out


def split_sentences(text):
    """English sentences: ends at . ! ? … (plus closing quotes) followed by
    a space and something that can open a sentence. Titles like Mr. do not
    end one. Only the ALIGNMENT uses this; the reader splits again itself."""
    out, start = [], 0
    for m in re.finditer(r"[.!?…]+[”’\"')\]]*\s+(?=[“‘\"'(\[A-Z0-9—…])", text):
        piece = text[start:m.end()].strip()
        if piece.endswith(ABBREV) or piece.split()[-1] in ABBREV:
            continue
        out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def epub_units(path):
    """[{key, docs, label, sentences}] in spine order. Docs sharing a stem
    (chapter003, chapter003a, chapter003b) are one unit; a doc with almost
    no text (title pages, image pages) is dropped."""
    z = zipfile.ZipFile(path)
    order, title = _spine(z)
    units = []
    for doc in order:
        if not doc.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        raw = z.read(doc).decode("utf-8", "ignore")
        paras = _paragraphs(raw, title)
        if sum(len(p) for p in paras) < MIN_UNIT_CHARS:
            continue
        stem = re.sub(r"(_r\d+)?\.x?html?$", "", posixpath.basename(doc).lower())
        stem = re.sub(r"(\d+)[a-z]$", r"\1", stem)
        h = re.search(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", raw, re.S)
        label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h.group(1)))).strip() if h else ""
        if not label:
            alt = re.search(r'<img\b[^>]*\balt="(Chapter[^"]*)"', raw)
            label = html.unescape(alt.group(1)) if alt else stem[-12:]
        sents = [s for p in paras for s in split_sentences(p)]
        if units and units[-1]["key"] == stem:
            units[-1]["docs"].append(doc)
            units[-1]["sentences"] += sents
        else:
            units.append({"key": stem, "docs": [doc], "label": label,
                          "sentences": sents})
    for u in units:
        u["preview"] = " ".join(u["sentences"][:2])[:120]
    return units, title


# ---------------------------------------------------------------------------
# embeddings and alignment
# ---------------------------------------------------------------------------

_MODEL = {}


def model(name):
    if name not in _MODEL:
        from sentence_transformers import SentenceTransformer
        _MODEL[name] = SentenceTransformer(name, device="cpu")
    return _MODEL[name]


def _embed(m, texts):
    return m.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False)


def suggest_pairing(chapters, units, model_name):
    """For each JP chapter, the (first, last) unit index it most likely
    covers, or None. Opening and closing text are compared and the chapters
    are kept in order (a unit is never used twice). Only a proposal: the
    window shows it for confirmation."""
    if not chapters or not units:
        return [None] * len(chapters)
    m = model(model_name)
    def body(chunks):
        return [c["text"] for c in chunks if not CREDIT_RE.match(c["text"])]
    jo = _embed(m, ["".join(body(ch)[1:12]) for _, ch in chapters])
    uo = _embed(m, [" ".join(u["sentences"][:10]) for u in units])
    sim = jo @ uo.T
    N, M = sim.shape
    # monotonic: chapter i takes unit j, units may be skipped (front matter)
    best = np.full((N + 1, M + 1), -1e9)
    best[0, :] = 0
    back = {}
    for i in range(1, N + 1):
        for j in range(1, M + 1):
            cand = [(best[i, j - 1], ("skipu",)), (best[i - 1, j - 1] + sim[i - 1, j - 1], ("take",)),
                    (best[i - 1, j], ("skipc",))]
            v, how = max(cand, key=lambda c: c[0])
            best[i, j] = v
            back[(i, j)] = how[0]
    pick = [None] * N
    i, j = N, int(np.argmax(best[N]))
    while i > 0 and j > 0:
        how = back[(i, j)]
        if how == "take":
            pick[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif how == "skipu":
            j -= 1
        else:
            i -= 1
    # a chapter whose opening matches badly is left for the person
    out = []
    for i, j in enumerate(pick):
        out.append((j, j) if j is not None and sim[i, j] >= 0.3 else None)
    return out


def align(jp_texts, en_sents, model_name, skip_cost):
    """Monotonic alignment -> groups [{jp:[start,n], en:[start,n], sim}].
    Each group's score is its similarity minus skip_cost, so a skip (worth 0)
    is taken only when nothing pairs better. Scores are NOT weighted by
    group size: weighted, 140 of 260 yojo groups came out 2-2 and the
    sentence-level timing was thrown away."""
    m = model(model_name)
    N, M = len(jp_texts), len(en_sents)
    def spans(xs, sep):
        keys, texts = [], []
        for k in (1, 2, 3):
            for i in range(len(xs) - k + 1):
                keys.append((i, k))
                texts.append(sep.join(xs[i:i + k]))
        return keys, texts
    jk, jt = spans(jp_texts, "")
    ek, et = spans(en_sents, " ")
    jv = dict(zip(jk, _embed(m, jt))) if jt else {}
    ev = dict(zip(ek, _embed(m, et))) if et else {}
    band = max(BAND_MIN, int(0.15 * max(N, M)))
    best = {(0, 0): 0.0}
    back = {}
    for i in range(N + 1):
        centre = i * M / N if N else 0
        lo, hi = max(0, int(centre - band)), min(M, int(centre + band))
        for j in range(lo, hi + 1):
            cur = best.get((i, j))
            if cur is None:
                continue
            for a, b in BEADS:
                ni, nj = i + a, j + b
                if ni > N or nj > M:
                    continue
                if a and b:
                    s = float(jv[(i, a)] @ ev[(j, b)])
                    gain = s - skip_cost
                else:
                    s, gain = None, 0.0
                if cur + gain > best.get((ni, nj), -1e18):
                    best[(ni, nj)] = cur + gain
                    back[(ni, nj)] = (i, j, a, b, s)
    if (N, M) not in back and (N, M) != (0, 0):
        raise RuntimeError("alignment did not reach the end of both texts - "
                           "are these the same chapter?")
    groups, cur = [], (N, M)
    while cur != (0, 0):
        i, j, a, b, s = back[cur]
        groups.append({"jp": [i, a], "en": [j, b], "sim": s})
        cur = (i, j)
    groups.reverse()
    return repair_skips(groups, jv, ev)


def repair_skips(groups, jv, ev):
    """A 0-1 next to a 1-0 is almost always ONE short pair the scorer
    under-rated (`証明終了。` = "I rest my case.", 0.2x): yojo had 6. Pair
    them up and mark it, so review still looks at them."""
    out, k = [], 0
    while k < len(groups):
        g = groups[k]
        if k + 1 < len(groups):
            h = groups[k + 1]
            kinds = {(g["jp"][1], g["en"][1]), (h["jp"][1], h["en"][1])}
            if kinds == {(1, 0), (0, 1)}:
                jp = g["jp"] if g["jp"][1] else h["jp"]
                en = g["en"] if g["en"][1] else h["en"]
                s = float(jv[(jp[0], 1)] @ ev[(en[0], 1)])
                out.append({"jp": [jp[0], 1], "en": [en[0], 1], "sim": s, "repaired": True})
                k += 2
                continue
        out.append(g)
        k += 1
    return out


def needs_look(g, jp=None):
    """What the review window marks yellow: a skip either way, or a pair
    the post-pass put back together. The seiyuu credit is not one - the
    glossary translates it (pass the chapter's jp list to know)."""
    if jp is not None and g["jp"][1] == 1 and g["en"][1] == 0 and CREDIT_RE.match(jp[g["jp"][0]]):
        return False
    return g["jp"][1] == 0 or g["en"][1] == 0 or g.get("repaired", False)


# ---------------------------------------------------------------------------
# review edits - groups are contiguous ranges over both texts, so every edit
# is a boundary moving between two neighbours
# ---------------------------------------------------------------------------

def _normalise(groups):
    keep = [g for g in groups if g["jp"][1] or g["en"][1]]
    for g in keep:
        if g.get("edited"):
            g.pop("repaired", None)
    return keep


def move(groups, k, side, direction):
    """side 'jp'/'en'; direction -1 gives group k's FIRST item to k-1,
    +1 gives its LAST item to k+1. Returns the new list (or the same one if
    the move is impossible)."""
    t = k + direction
    if not (0 <= t < len(groups)) or groups[k][side][1] == 0:
        return groups
    g, h = groups[k], groups[t]
    if direction < 0:
        if h[side][1] == 0:
            h[side][0] = g[side][0]
        h[side][1] += 1
        g[side][0] += 1
        g[side][1] -= 1
    else:
        g[side][1] -= 1
        if h[side][1] == 0:
            h[side][0] = g[side][0] + g[side][1]
        else:
            h[side][0] -= 1
        h[side][1] += 1
    for x in (g, h):
        x["edited"], x["sim"] = True, None
    return _normalise(groups)


def merge_next(groups, k):
    if k + 1 >= len(groups):
        return groups
    g, h = groups[k], groups[k + 1]
    for side in ("jp", "en"):
        if g[side][1] == 0:
            g[side] = list(h[side])
        elif h[side][1]:
            g[side][1] += h[side][1]
    g["edited"], g["sim"] = True, None
    del groups[k + 1]
    return _normalise(groups)


def split_tail(groups, k):
    """Split off group k's last chunk and last sentence (each only if it has
    more than one) as a new group after it."""
    g = groups[k]
    if g["jp"][1] < 2 and g["en"][1] < 2:
        return groups
    new = {"jp": [g["jp"][0] + g["jp"][1], 0], "en": [g["en"][0] + g["en"][1], 0],
           "sim": None, "edited": True}
    for side in ("jp", "en"):
        if g[side][1] >= 2:
            g[side][1] -= 1
            new[side] = [g[side][0] + g[side][1], 1]
    g["edited"], g["sim"] = True, None
    groups.insert(k + 1, new)
    return _normalise(groups)


# ---------------------------------------------------------------------------
# state and output
# ---------------------------------------------------------------------------

def state_path(work_root, book_folder, base):
    book = os.path.basename(os.path.normpath(book_folder))
    return os.path.join(work_root, book, f"{base}.align.json")


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def align_chapter(book_folder, base, chunks, epub_path, units, unit_range, settings):
    """Aligns one chapter against units[first..last] and returns the state
    to save (it carries everything the review and the writer need)."""
    first, last = unit_range
    sents = [s for u in units[first:last + 1] for s in u["sentences"]]
    jp = [c["text"] for c in chunks]
    groups = align(jp, sents, settings["model"], settings["skip_cost"])
    return {"version": 1, "base": base, "book_folder": book_folder,
            "epub": epub_path, "units": [u["key"] for u in units[first:last + 1]],
            "unit_range": [first, last], "jp": jp, "en": sents,
            "groups": groups, "reviewed": False}


def format_timestamp(seconds):
    # same as translate_pipeline.format_timestamp
    total_ms = int(round(max(seconds, 0.0) * 1000))
    h, r = divmod(total_ms, 3_600_000)
    mi, r = divmod(r, 60_000)
    s, ms = divmod(r, 1000)
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def read_srt(path):
    """[(start, end, text)] from an .srt; [] if absent."""
    if not os.path.exists(path):
        return []
    def secs(t):
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    out = []
    with open(path, encoding="utf-8") as f:
        for block in f.read().split("\n\n"):
            lines = block.strip().split("\n")
            if len(lines) >= 3 and "-->" in lines[1]:
                a, b = (x.strip() for x in lines[1].split("-->"))
                out.append((secs(a), secs(b), "\n".join(lines[2:]).strip()))
    return out


def srt_text_at(cues, t):
    for s, e, text in cues:
        if s - 0.01 <= t <= e:
            return text.replace("​", "").strip() or None
    return None


def build_cues(state, chunks, glossary, template, vntl_cues=()):
    """[(start, end, text)]. A group with both sides is a cue over its
    chunks' time. English with no Japanese joins the cue before it (or the
    next one, at the very start). Japanese with no English has no cue -
    except the seiyuu credit, which the glossary translates. With no
    glossary entry (an output folder not to hand), the credit's cue from the
    book's VNTL .srt is used - that one was translated WITH the glossary."""
    jp, en = state["jp"], state["en"]
    cues, pending = [], []
    for g in state["groups"]:
        js, jn = g["jp"]
        es, enn = g["en"]
        text = " ".join(en[es:es + enn]).strip()
        if jn == 0:
            if cues:
                s, e, t = cues[-1]
                cues[-1] = (s, e, (t + " " + text).strip())
            else:
                pending.append(text)
            continue
        start, end = chunks[js]["start"], chunks[js + jn - 1]["end"]
        if enn == 0:
            credit = None
            if jn == 1 and CREDIT_RE.match(jp[js]):
                credit = (credit_text(jp[js], glossary, template)
                          or srt_text_at(vntl_cues, float(chunks[js]["start"]) + 0.05))
            if not credit:
                continue
            text = credit
        if pending:
            text = " ".join(pending + [text])
            pending = []
        cues.append((float(start), float(max(end, start + 0.001)), text))
    return cues


def en_srt_path(book_folder, base):
    return os.path.join(book_folder, f"{base}.en.srt")


def write_en_srt(book_folder, state, chunks, glossary, template):
    """UTF-8, no BOM, LF - the .srt contract, cues may span chunks."""
    vntl = read_srt(os.path.join(book_folder, f"{state['base']}.srt"))
    cues = build_cues(state, chunks, glossary, template, vntl)
    blocks = [f"{n}\n{format_timestamp(s)} --> {format_timestamp(e)}\n{t}\n"
              for n, (s, e, t) in enumerate(cues, start=1)]
    path = en_srt_path(book_folder, state["base"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(blocks))
    return path, len(cues)
