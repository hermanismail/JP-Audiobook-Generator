"""
app.py
------
The Translation Aligner window (2026-10-01). Everything under it is
aligner.py.

    1 book       the book folder (chapter_*.sync.json) and the English epub
    2 pairing    each chapter -> the epub chapter(s) it is; proposed from the
                 opening text, confirmed or corrected here
    3 align      ticked chapters, one after another (CPU, ~1 min each)
    4 review     per chapter: every group, yellow where something was
                 skipped or re-paired; boundaries can be moved
    5 write      <chapter>.en.srt beside the chapter - .srt is never touched

Review state lives under work_root/<book>/, never in the book folder, so a
published folder only ever gains the .en.srt files.

Run it with:   uv run python app.py
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

import aligner as al

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_STATE = os.path.join(SCRIPT_DIR, "gui_state.json")

BG = "#F7F7FA"
CARD = "#FFFFFF"
TITLE = "#17171C"
SUBTITLE = "#8B8B94"
ACCENT = "#6C5DD3"
ACCENT_HOVER = "#5B4FC0"
DONE = "#1E8B4E"
WARN = "#B66A16"
FAIL = "#C4453C"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"
YELLOW = "#FFF3C4"
RED = "#FFE0E0"
NONE_LABEL = "(none)"


def fmt_time(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def load_gui_state():
    try:
        with open(GUI_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_gui_state(d):
    try:
        with open(GUI_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Translation Aligner")
        self.geometry("1100x780")
        self.configure(fg_color=BG)
        self.settings = al.load_settings()
        self.q = queue.Queue()
        self.busy = False
        self.chapters = []      # [(base, chunks)]
        self.units = []
        self.rows = {}          # base -> widgets
        self._build()
        st = load_gui_state()
        self.book_var.set(st.get("book", ""))
        self.epub_var.set(st.get("epub", ""))
        self.after(100, self._poll)

    # ------------------------------------------------------------------ ui
    def _build(self):
        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        top.pack(fill="x", padx=16, pady=(16, 8))
        self.book_var = tk.StringVar()
        self.epub_var = tk.StringVar()
        for r, (label, var, cmd) in enumerate((
                ("Book folder", self.book_var, self._browse_book),
                ("English epub", self.epub_var, self._browse_epub))):
            ctk.CTkLabel(top, text=label, text_color=TITLE, width=110, anchor="w").grid(
                row=r, column=0, padx=(14, 6), pady=6, sticky="w")
            ctk.CTkEntry(top, textvariable=var).grid(row=r, column=1, sticky="ew", pady=6)
            ctk.CTkButton(top, text="Browse", width=80, command=cmd, fg_color="#EDEBFC",
                          text_color=ACCENT, hover_color="#E0DCFA").grid(row=r, column=2, padx=8)
        top.grid_columnconfigure(1, weight=1)
        self.read_btn = ctk.CTkButton(top, text="Read book + epub", command=self._read,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.read_btn.grid(row=0, column=3, rowspan=2, padx=(4, 14))
        ctk.CTkLabel(top, text="Writes <chapter>.en.srt beside each chapter. The .srt and "
                     "sync.json are never touched.", text_color=SUBTITLE).grid(
            row=2, column=0, columnspan=4, padx=14, pady=(0, 8), sticky="w")

        mid = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        mid.pack(fill="both", expand=True, padx=16, pady=8)
        bar = ctk.CTkFrame(mid, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(bar, text="Chapters", text_color=TITLE,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.align_btn = ctk.CTkButton(bar, text="Align ticked", command=self._align,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER, width=120)
        self.align_btn.pack(side="right")
        self.suggest_btn = ctk.CTkButton(bar, text="Suggest pairing", command=self._suggest,
                                         width=130, fg_color="#EDEBFC", text_color=ACCENT,
                                         hover_color="#E0DCFA")
        self.suggest_btn.pack(side="right", padx=8)
        ctk.CTkLabel(mid, text="Each chapter is matched to its first and last epub chapter "
                     "(a chapter split over several files is joined automatically).",
                     text_color=SUBTITLE).pack(anchor="w", padx=12)
        self.table = ctk.CTkScrollableFrame(mid, fg_color="transparent")
        self.table.pack(fill="both", expand=True, padx=6, pady=6)

        self.log = tk.Text(self, height=8, bg=LOG_BG, fg=LOG_FG, relief="flat",
                           font=("Consolas", 9), wrap="word")
        self.log.pack(fill="x", padx=16, pady=(0, 16))

    def _browse_book(self):
        d = filedialog.askdirectory(parent=self, title="Book folder (chapter_*.sync.json)")
        if d:
            self.book_var.set(d)
        self.lift()

    def _browse_epub(self):
        f = filedialog.askopenfilename(parent=self, title="English epub",
                                       filetypes=[("epub", "*.epub")])
        if f:
            self.epub_var.set(f)
        self.lift()

    def say(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    # ------------------------------------------------------------- worker
    def _run(self, fn, *args):
        if self.busy:
            return
        self.busy = True
        for b in (self.read_btn, self.suggest_btn, self.align_btn):
            b.configure(state="disabled")

        def go():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - reported, never swallowed
                self.q.put(("log", f"ERROR: {type(e).__name__}: {e}"))
            self.q.put(("idle", None))
        threading.Thread(target=go, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.say(val)
                elif kind == "idle":
                    self.busy = False
                    for b in (self.read_btn, self.suggest_btn, self.align_btn):
                        b.configure(state="normal")
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def ui(self, fn):
        self.q.put(("call", fn))

    def log_(self, msg):
        self.q.put(("log", msg))

    # --------------------------------------------------------------- read
    def _book_dir(self):
        return self.book_var.get().strip()

    def _pairing_path(self):
        book = os.path.basename(os.path.normpath(self._book_dir()))
        return os.path.join(self.settings["work_root"], book, "pairing.json")

    def _read(self):
        book, epub = self._book_dir(), self.epub_var.get().strip()
        if not os.path.isdir(book) or not os.path.isfile(epub):
            messagebox.showerror("Translation Aligner", "Choose a book folder and an epub first.",
                                 parent=self)
            return
        save_gui_state({"book": book, "epub": epub})

        def work():
            chs = al.book_chapters(book)
            units, title = al.epub_units(epub)
            self.log_(f"{len(chs)} chapters in the book, {len(units)} chapters in '{title}'")
            saved = {}
            try:
                with open(self._pairing_path(), encoding="utf-8") as f:
                    p = json.load(f)
                if p.get("epub") == epub:
                    saved = p.get("pairs", {})
            except (OSError, ValueError):
                pass
            def show():
                self.chapters, self.units = chs, units
                self._fill_table(saved)
                if not saved and chs:
                    self.say("No saved pairing - press Suggest pairing, or set it by hand.")
            self.ui(show)
        self._run(work)

    def _unit_label(self, k):
        u = self.units[k]
        return f"{k + 1:>3}  {u['label'][:28]}  -  {u['preview'][:50]}"

    def _fill_table(self, saved):
        for w in self.table.winfo_children():
            w.destroy()
        self.rows = {}
        values = [NONE_LABEL] + [self._unit_label(k) for k in range(len(self.units))]
        hdr = ctk.CTkFrame(self.table, fg_color="transparent")
        hdr.pack(fill="x")
        for text, w in (("", 30), ("chapter", 110), ("first epub chapter", 330),
                        ("last epub chapter", 330), ("status", 170)):
            ctk.CTkLabel(hdr, text=text, width=w, anchor="w", text_color=SUBTITLE).pack(side="left", padx=2)
        for base, chunks in self.chapters:
            row = ctk.CTkFrame(self.table, fg_color="transparent")
            row.pack(fill="x", pady=1)
            tick = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(row, text="", variable=tick, width=30).pack(side="left", padx=2)
            ctk.CTkLabel(row, text=f"{base}  ({len(chunks)})", width=110, anchor="w",
                         text_color=TITLE).pack(side="left", padx=2)
            first, last = tk.StringVar(value=NONE_LABEL), tk.StringVar(value=NONE_LABEL)
            pr = saved.get(base)
            if pr:
                first.set(values[pr[0] + 1])
                last.set(values[pr[1] + 1])
            for var in (first, last):
                ctk.CTkOptionMenu(row, values=values, variable=var, width=330, fg_color="#F1F1F4",
                                  text_color=TITLE, button_color="#E2E2E8",
                                  command=lambda _v, b=base: self._pair_changed(b)).pack(side="left", padx=2)
            status = ctk.CTkLabel(row, text="", width=170, anchor="w")
            status.pack(side="left", padx=2)
            ctk.CTkButton(row, text="Review", width=70, fg_color="#EDEBFC", text_color=ACCENT,
                          hover_color="#E0DCFA", command=lambda b=base: self._review(b)).pack(side="left", padx=4)
            self.rows[base] = {"tick": tick, "first": first, "last": last, "status": status}
            self._refresh_status(base)

    def _range(self, base):
        r = self.rows[base]
        def idx(v):
            return None if v == NONE_LABEL else int(v.split()[0]) - 1
        a, b = idx(r["first"].get()), idx(r["last"].get())
        if a is None:
            return None
        if b is None or b < a:
            b = a
        return [a, b]

    def _pair_changed(self, base):
        r = self.rows[base]
        if r["last"].get() == NONE_LABEL or (self._range(base) and
                                             int(r["last"].get().split()[0]) < int(r["first"].get().split()[0])):
            r["last"].set(r["first"].get())
        self._save_pairing()
        self._refresh_status(base)

    def _save_pairing(self):
        pairs = {b: self._range(b) for b in self.rows if self._range(b)}
        al.save_state(self._pairing_path(), {"epub": self.epub_var.get().strip(), "pairs": pairs})

    def _state_path(self, base):
        return al.state_path(self.settings["work_root"], self._book_dir(), base)

    def _refresh_status(self, base):
        r = self.rows[base]
        st = al.load_state(self._state_path(base))
        rng = self._range(base)
        if rng is None:
            r["status"].configure(text="not paired", text_color=SUBTITLE)
        elif not st or st.get("unit_range") != rng or st.get("epub") != self.epub_var.get().strip():
            r["status"].configure(text="not aligned", text_color=SUBTITLE)
        else:
            look = sum(al.needs_look(g, st["jp"]) for g in st["groups"])
            written = os.path.exists(al.en_srt_path(self._book_dir(), base))
            if st.get("reviewed") and written:
                r["status"].configure(text=".en.srt written", text_color=DONE)
            else:
                r["status"].configure(text=f"aligned - {look} to look at",
                                      text_color=WARN if look else DONE)

    # ------------------------------------------------------ suggest/align
    def _suggest(self):
        if not self.chapters:
            return

        def work():
            self.log_("Loading the embedding model and comparing chapter openings...")
            pick = al.suggest_pairing(self.chapters, self.units, self.settings["model"])
            def show():
                values = [NONE_LABEL] + [self._unit_label(k) for k in range(len(self.units))]
                for (base, _), p in zip(self.chapters, pick):
                    r = self.rows[base]
                    r["first"].set(values[p[0] + 1] if p else NONE_LABEL)
                    r["last"].set(values[p[1] + 1] if p else NONE_LABEL)
                    self._refresh_status(base)
                self._save_pairing()
                missing = [b for (b, _), p in zip(self.chapters, pick) if not p]
                self.say("Pairing suggested - check it. " +
                         (f"Not matched: {', '.join(missing)}" if missing else "Every chapter matched."))
            self.ui(show)
        self._run(work)

    def _align(self):
        todo = [(b, c) for b, c in self.chapters if self.rows[b]["tick"].get() and self._range(b)]
        if not todo:
            self.say("Nothing ticked and paired.")
            return
        epub, book = self.epub_var.get().strip(), self._book_dir()
        ranges = {b: self._range(b) for b, _ in todo}

        def work():
            for base, chunks in todo:
                self.log_(f"{base}: aligning ...")
                st = al.align_chapter(book, base, chunks, epub, self.units, ranges[base], self.settings)
                al.save_state(self._state_path(base), st)
                look = sum(al.needs_look(g, st["jp"]) for g in st["groups"])
                self.log_(f"{base}: {len(st['jp'])} chunks x {len(st['en'])} sentences, "
                          f"{len(st['groups'])} groups, {look} to look at")
                self.ui(lambda b=base: self._refresh_status(b))
            self.log_("Done. Review each chapter, then write its .en.srt.")
        self._run(work)

    def _review(self, base):
        st = al.load_state(self._state_path(base))
        if not st or st.get("unit_range") != self._range(base):
            messagebox.showinfo("Translation Aligner", f"{base} is not aligned with this pairing yet.",
                                parent=self)
            return
        chunks = dict(self.chapters)[base]
        ReviewWindow(self, base, st, chunks)


class ReviewWindow(ctk.CTkToplevel):
    def __init__(self, app, base, state, chunks):
        super().__init__(app)
        self.app, self.base, self.st, self.chunks = app, base, state, chunks
        self.title(f"Review - {base}")
        self.geometry("1150x760")
        self.configure(fg_color=BG)
        self.dirty = False
        self._build()
        self._fill()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self.lift)

    def _build(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        self.summary = ctk.CTkLabel(bar, text="", text_color=TITLE)
        self.summary.pack(side="left")
        ctk.CTkButton(bar, text="Save + write .en.srt", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._write).pack(side="right")
        ctk.CTkButton(bar, text="Next to look at", width=120, fg_color="#EDEBFC", text_color=ACCENT,
                      hover_color="#E0DCFA", command=self._next_flag).pack(side="right", padx=8)

        ctk.CTkLabel(self, text="Yellow: Japanese or English with no partner, or a pair put back "
                     "together after the fact. Red: low similarity (often still right). "
                     "Times are when the Japanese is spoken.", text_color=SUBTITLE,
                     wraplength=1100, justify="left").pack(anchor="w", padx=12)

        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True, padx=12, pady=6)
        style = ttk.Style(self)
        style.configure("Al.Treeview", rowheight=24, font=("Segoe UI", 10))
        cols = ("n", "time", "kind", "sim", "jp", "en")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", style="Al.Treeview",
                                 selectmode="browse")
        for c, w, t in (("n", 50, "#"), ("time", 70, "time"), ("kind", 55, "JP-EN"),
                        ("sim", 50, "sim"), ("jp", 430, "Japanese"), ("en", 480, "English")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c in ("jp", "en"), anchor="w")
        self.tree.tag_configure("look", background=YELLOW)
        self.tree.tag_configure("low", background=RED)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show())

        detail = ctk.CTkFrame(self, fg_color=CARD, corner_radius=10)
        detail.pack(fill="x", padx=12, pady=(0, 6))
        self.jp_box = ctk.CTkTextbox(detail, height=110, wrap="word", font=ctk.CTkFont(size=14))
        self.en_box = ctk.CTkTextbox(detail, height=110, wrap="word", font=ctk.CTkFont(size=14))
        self.jp_box.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)
        self.en_box.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        ops = ctk.CTkFrame(self, fg_color="transparent")
        ops.pack(fill="x", padx=12, pady=(0, 12))
        for text, fn in (("JP: first to row above", lambda k: al.move(self._g(), k, "jp", -1)),
                         ("JP: last to row below", lambda k: al.move(self._g(), k, "jp", 1)),
                         ("EN: first to row above", lambda k: al.move(self._g(), k, "en", -1)),
                         ("EN: last to row below", lambda k: al.move(self._g(), k, "en", 1)),
                         ("Merge with row below", lambda k: al.merge_next(self._g(), k)),
                         ("Split off last", lambda k: al.split_tail(self._g(), k))):
            ctk.CTkButton(ops, text=text, width=150, fg_color="#F1F1F4", text_color=TITLE,
                          hover_color="#E2E2E8", command=lambda f=fn: self._edit(f)).pack(side="left", padx=3)

    def _g(self):
        return self.st["groups"]

    def _fill(self, select=None):
        self.tree.delete(*self.tree.get_children())
        jp, en, low = self.st["jp"], self.st["en"], self.app.settings["low_sim"]
        for k, g in enumerate(self._g()):
            js, jn = g["jp"]
            es, enn = g["en"]
            t = self.chunks[js]["start"] if jn else None
            sim = g.get("sim")
            tag = ("look",) if al.needs_look(g, jp) else (("low",) if sim is not None and sim < low else ())
            self.tree.insert("", "end", iid=str(k), tags=tag, values=(
                k + 1, fmt_time(t) if t is not None else "", f"{jn}-{enn}" + ("*" if g.get("edited") else ""),
                f"{sim:.2f}" if sim is not None else "", "".join(jp[js:js + jn]), " ".join(en[es:es + enn])))
        look = sum(al.needs_look(g, self.st["jp"]) for g in self._g())
        self.summary.configure(text=f"{len(self.st['jp'])} chunks, {len(self.st['en'])} sentences, "
                                    f"{len(self._g())} groups, {look} to look at"
                                    + ("   (unsaved)" if self.dirty else ""))
        if select is not None and self._g():
            k = str(min(select, len(self._g()) - 1))
            self.tree.selection_set(k)
            self.tree.see(k)

    def _sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def _show(self):
        k = self._sel()
        if k is None:
            return
        g = self._g()[k]
        js, jn = g["jp"]
        es, enn = g["en"]
        for box, text in ((self.jp_box, "\n".join(self.st["jp"][js:js + jn])),
                          (self.en_box, "\n".join(self.st["en"][es:es + enn]))):
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", text)
            box.configure(state="disabled")

    def _edit(self, fn):
        k = self._sel()
        if k is None:
            return
        self.st["groups"] = fn(k)
        self.dirty = True
        self._fill(select=k)

    def _next_flag(self):
        k = self._sel()
        start = -1 if k is None else k
        g = self._g()
        for i in list(range(start + 1, len(g))) + list(range(0, start + 1)):
            if al.needs_look(g[i], self.st["jp"]):
                self.tree.selection_set(str(i))
                self.tree.see(str(i))
                return

    def _write(self):
        self.st["reviewed"] = True
        al.save_state(self.app._state_path(self.base), self.st)
        book = self.app._book_dir()
        path, n = al.write_en_srt(book, self.st, self.chunks, al.load_glossary(book),
                                  self.app.settings["credit_template"])
        self.dirty = False
        self._fill(select=self._sel())
        self.app.say(f"{self.base}: wrote {os.path.basename(path)} ({n} cues)")
        self.app._refresh_status(self.base)

    def _close(self):
        if self.dirty and not messagebox.askyesno(
                "Review", "Discard the unsaved changes to this chapter?", parent=self):
            return
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
