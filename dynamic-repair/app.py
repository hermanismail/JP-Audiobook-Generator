"""
app.py
------
The Dynamic Repair window (2026-09-18): fix a Part of a chapter rendered in
the generator's dynamic profile mode. Everything under it is dynrepair.py.

    1 folder       the user's copy of the files; parsed, green or red
    2 find         Time (7:06) or Part (68) as the reader shows them,
                   a Whisper scan, or every Part holding a known reading
    3 part         what it says, how it was made, play it in context
    4 regenerate   the text the TTS reads (editable), readings, style,
                   takes to compare
    5 apply        every queued Part in one pass - in place, no backup

Run it with:   uv run python app.py
"""

import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

import dynrepair as dr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BG = "#F7F7FA"
CARD = "#FFFFFF"
BORDER = "#E7E7EC"
TITLE = "#17171C"
SUBTITLE = "#8B8B94"
ENTRY_BORDER = "#E2E2E8"
ENTRY_TEXT = "#3A3A42"
ACCENT = "#6C5DD3"
ACCENT_HOVER = "#5B4FC0"
NEUTRAL_BORDER = "#D8D8DE"
DONE = "#1E8B4E"
WARN = "#B66A16"
FAIL = "#C4453C"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"

STYLE_LABELS = dict(dr.dynamic_profile.STYLE_LABELS)
STYLE_LABELS[dr.CUSTOM_STYLE] = "Custom scale"
STYLE_BY_LABEL = {v: k for k, v in STYLE_LABELS.items()}
STYLE_ORDER = list(dr.dynamic_profile.STYLE_KEYS) + [dr.CUSTOM_STYLE]


def play_wav(path):
    import winsound
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except RuntimeError:
        pass


def stop_playback():
    import winsound
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except RuntimeError:
        pass


def describe_request(request):
    if "duration_scale" in request:
        return f"x{request['duration_scale']}"
    return f"{request['seconds']}s"


def button(parent, text, command, width=96, primary=False, danger=False, height=34, **kw):
    if primary:
        return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                             fg_color=ACCENT, hover_color=ACCENT_HOVER, command=command,
                             font=ctk.CTkFont(size=13, weight="bold"), **kw)
    return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                         fg_color=CARD, hover_color=BG, border_width=1,
                         border_color=NEUTRAL_BORDER,
                         text_color=FAIL if danger else ENTRY_TEXT, command=command, **kw)


def entry(parent, variable, width=None, placeholder=""):
    widget = ctk.CTkEntry(parent, textvariable=variable, height=34, corner_radius=8,
                          border_width=1, border_color=ENTRY_BORDER, fg_color=CARD,
                          text_color=ENTRY_TEXT, placeholder_text=placeholder)
    if width:
        widget.configure(width=width)
    return widget


class ResultRow(ctk.CTkFrame):
    """One Part in the find list: from a scan, or holding a known reading."""

    def __init__(self, parent, part, total, head, lines, colour, on_select):
        super().__init__(parent, fg_color=CARD, corner_radius=8, border_width=1,
                         border_color=BORDER)
        self.index = part["index"]
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(7, 0))
        ctk.CTkLabel(top, text=f"Part {part['part']}/{total}", width=110, anchor="w",
                     text_color=TITLE, font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text=dr.clock(part["start"]), width=70, anchor="w",
                     text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkLabel(top, text=head, anchor="w", text_color=colour,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
        self.labels = []
        for text, colour_ in lines:
            label = ctk.CTkLabel(self, text=text, anchor="w", justify="left", text_color=colour_,
                                 font=ctk.CTkFont(size=12), wraplength=820)
            label.pack(fill="x", padx=10, pady=(2, 0))
            self.labels.append(label)
        ctk.CTkFrame(self, fg_color="transparent", height=6).pack()
        for widget in (self, top, *self.labels):
            widget.bind("<Button-1>", lambda _e: on_select(self.index))

    def set_selected(self, selected):
        self.configure(border_width=2 if selected else 1,
                       border_color=ACCENT if selected else BORDER,
                       fg_color=PASTEL_VIOLET if selected else CARD)


class RepairApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Dynamic Repair")
        self.geometry("1080x980")
        self.minsize(920, 680)
        self.configure(fg_color=BG)
        icon = os.path.join(SCRIPT_DIR, "dynrepair_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.settings = dr.load_settings()
        self.info = None
        self.chapter = None
        self.readings = []
        self.scan_rows = {}
        self.result_widgets = []
        self.selected = None
        self.part_readings = {}     # index -> {word: reading} used in its text
        self.part_text = {}         # index -> the edited TTS text, kept while switching
        self.queued = {}            # index -> take record
        self._queue = queue.Queue()
        self._busy = False
        self._proc = None
        self._cancel = threading.Event()

        self.folder_var = ctk.StringVar(value=self.settings["last_folder"])
        self.chapter_var = ctk.StringVar(value="")
        self.time_var = ctk.StringVar()
        self.part_var = ctk.StringVar()
        self.style_var = ctk.StringVar()
        self.scale_var = ctk.StringVar(value="1.6")
        self.count_var = ctk.StringVar(value="2")
        self.word_var = ctk.StringVar()
        self.reading_var = ctk.StringVar()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        if self.folder_var.get():
            self.after(200, self.parse_folder)

    # ------------------------------------------------------------ building
    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11, border_width=1,
                            border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _head(self, card, number, title, hint=""):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(13, 8))
        ctk.CTkLabel(row, text=str(number), width=26, height=26, corner_radius=8,
                     fg_color=PASTEL_GREY, text_color="#78767F",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(row, text="  " + title.upper(), text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        label = ctk.CTkLabel(row, text=hint, text_color=SUBTITLE, font=ctk.CTkFont(size=11))
        label.pack(side="right")
        return label

    def _small(self, parent, text):
        return ctk.CTkLabel(parent, text=text, text_color=SUBTITLE, font=ctk.CTkFont(size=11))

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        header.pack(fill="x")
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=13)
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Dynamic Repair", text_color=TITLE,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        self.head_sub = ctk.CTkLabel(titles, text="Fix a Part of a dynamic-profile chapter",
                                     text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.head_sub.pack(anchor="w")
        self.cancel_button = button(inner, "Cancel", self.on_cancel, width=100, danger=True,
                                    state="disabled")
        self.cancel_button.pack(side="right")

        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        button(bar, "Close", self.on_close, width=110).pack(side="right", padx=18, pady=11)
        wrap = ctk.CTkFrame(self, fg_color=LOG_BG, corner_radius=0, height=140)
        wrap.pack(fill="x", side="bottom")
        wrap.pack_propagate(False)
        self.log_box = ctk.CTkTextbox(wrap, fg_color=LOG_BG, text_color=LOG_FG,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      border_width=0, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

        body = ctk.CTkScrollableFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 0))
        self.body = body
        self._build_folder(body)
        self._build_find(body)
        self._build_part(body)
        self._build_regenerate(body)
        self._build_apply(body)

    def _build_folder(self, parent):
        card = self._card(parent)
        self._head(card, 1, "Folder", "your COPY of the chapter files - repaired in place")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        button(row, "Browse…", self.on_browse).pack(side="right")
        folder_entry = entry(row, self.folder_var, placeholder="folder holding chapter_NNN.flac "
                                                               ".m4a .sync.json .render.json")
        folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        folder_entry.bind("<Return>", lambda _e: self.parse_folder())
        self.folder_status = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                          font=ctk.CTkFont(size=12), wraplength=960)
        self.folder_status.pack(fill="x", padx=16)
        self.chapter_lines = ctk.CTkFrame(card, fg_color="transparent")
        self.chapter_lines.pack(fill="x", padx=16, pady=(2, 6))
        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 13))
        self._small(row2, "chapter").pack(side="left", padx=(0, 6))
        self.chapter_menu = ctk.CTkOptionMenu(
            row2, values=["(none)"], variable=self.chapter_var, width=200, height=32,
            corner_radius=8, fg_color=CARD, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            text_color=ENTRY_TEXT, dropdown_fg_color=CARD, command=lambda _v: self.load_chapter())
        self.chapter_menu.pack(side="left")
        self.chapter_facts = ctk.CTkLabel(row2, text="", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=11))
        self.chapter_facts.pack(side="left", padx=12)

    def _build_find(self, parent):
        card = self._card(parent)
        self.find_hint = self._head(card, 2, "Find", "as the reader shows it")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self._small(row, "Time").pack(side="left", padx=(0, 6))
        time_entry = entry(row, self.time_var, width=90, placeholder="7:06")
        time_entry.pack(side="left", padx=(0, 6))
        time_entry.bind("<Return>", lambda _e: self.on_find_time())
        button(row, "Go", self.on_find_time, width=50).pack(side="left", padx=(0, 18))
        self._small(row, "Part").pack(side="left", padx=(0, 6))
        part_entry = entry(row, self.part_var, width=70, placeholder="68")
        part_entry.pack(side="left", padx=(0, 6))
        part_entry.bind("<Return>", lambda _e: self.on_find_part())
        button(row, "Go", self.on_find_part, width=50).pack(side="left", padx=(0, 18))
        self.scan_button = button(row, "Scan chapter", self.on_scan, width=130, primary=True)
        self.scan_button.pack(side="left", padx=(0, 8))
        button(row, "Rescan", lambda: self.on_scan(force=True), width=80).pack(side="left",
                                                                              padx=(0, 8))
        button(row, "Known readings", self.on_known_readings, width=130).pack(side="left")
        self.find_summary = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                         font=ctk.CTkFont(size=12))
        self.find_summary.pack(fill="x", padx=16, pady=(0, 6))
        self.results_frame = ctk.CTkScrollableFrame(card, fg_color=BG, height=240,
                                                    corner_radius=8)
        self.results_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_part(self, parent):
        card = self._card(parent)
        self.part_hint = self._head(card, 3, "Part", "")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self.play_button = button(row, "▶  Play in context", self.on_play_part, width=160,
                                  primary=True, state="disabled")
        self.play_button.pack(side="left")
        button(row, "Stop", stop_playback, width=70).pack(side="left", padx=(8, 0))
        self.part_facts = ctk.CTkLabel(row, text="", text_color=SUBTITLE,
                                       font=ctk.CTkFont(size=11), justify="left", anchor="w")
        self.part_facts.pack(side="left", padx=14)
        self.part_script = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                        text_color=ENTRY_TEXT, font=ctk.CTkFont(size=14),
                                        wraplength=960)
        self.part_script.pack(fill="x", padx=16, pady=(0, 2))
        self.part_heard = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                       text_color=SUBTITLE, font=ctk.CTkFont(size=12),
                                       wraplength=960)
        self.part_heard.pack(fill="x", padx=16, pady=(0, 13))

    def _build_regenerate(self, parent):
        card = self._card(parent)
        self._head(card, 4, "Regenerate", "the reader keeps the book's wording; only the "
                                          "audio reads this")
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(0, 4))
        self._small(top, "Text sent to the TTS - spell a name or a hard kanji in kana").pack(
            side="left")
        button(top, "Reset to original", self.on_reset_text, width=140, height=28).pack(
            side="right")
        self.text_box = ctk.CTkTextbox(card, height=64, border_width=1,
                                       border_color=ENTRY_BORDER, fg_color="#FCFCFD",
                                       text_color=ENTRY_TEXT, corner_radius=8,
                                       font=ctk.CTkFont(size=15), wrap="word")
        self.text_box.pack(fill="x", padx=16, pady=(0, 8))
        self.text_box.bind("<KeyRelease>", lambda _e: self._remember_text())

        rd = ctk.CTkFrame(card, fg_color="transparent")
        rd.pack(fill="x", padx=16, pady=(0, 4))
        self._small(rd, "Reading:  word").pack(side="left", padx=(0, 6))
        entry(rd, self.word_var, width=150, placeholder="玉響").pack(side="left", padx=(0, 8))
        self._small(rd, "reads as").pack(side="left", padx=(0, 6))
        entry(rd, self.reading_var, width=170, placeholder="たまゆら").pack(side="left",
                                                                          padx=(0, 8))
        button(rd, "Replace & remember", self.on_add_reading, width=160).pack(side="left")
        self.readings_note = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                          justify="left", font=ctk.CTkFont(size=11),
                                          wraplength=960)
        self.readings_note.pack(fill="x", padx=16, pady=(0, 8))

        st = ctk.CTkFrame(card, fg_color="transparent")
        st.pack(fill="x", padx=16, pady=(0, 10))
        self._small(st, "style").pack(side="left", padx=(0, 6))
        self.style_menu = ctk.CTkOptionMenu(
            st, values=[STYLE_LABELS[k] for k in STYLE_ORDER], variable=self.style_var,
            width=210, height=32, corner_radius=8, fg_color=CARD, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT, dropdown_fg_color=CARD,
            command=lambda _v: self._update_request())
        self.style_menu.pack(side="left", padx=(0, 8))
        self.scale_entry = entry(st, self.scale_var, width=64)
        self.scale_entry.pack(side="left", padx=(0, 8))
        self.scale_var.trace_add("write", lambda *_: self._update_request())
        self.request_label = ctk.CTkLabel(st, text="", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=11))
        self.request_label.pack(side="left", padx=(0, 14))
        self.generate_button = button(st, "Generate", self.on_generate, width=120, primary=True,
                                      state="disabled")
        self.generate_button.pack(side="right")
        ctk.CTkOptionMenu(st, values=["1", "2", "3"], variable=self.count_var, width=60,
                          height=32, corner_radius=8, fg_color=CARD, button_color=ACCENT,
                          button_hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT,
                          dropdown_fg_color=CARD).pack(side="right", padx=(0, 8))
        self._small(st, "takes").pack(side="right", padx=(0, 6))
        self.takes_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.takes_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_apply(self, parent):
        card = self._card(parent)
        self._head(card, 5, "Apply", "every queued Part in one pass, written IN PLACE")
        self.queue_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.queue_frame.pack(fill="x", padx=16, pady=(0, 8))
        self.plan_label = ctk.CTkLabel(card, text="Nothing queued. Pick a take in step 4.",
                                       text_color=SUBTITLE, anchor="w", justify="left",
                                       font=ctk.CTkFont(size=12))
        self.plan_label.pack(fill="x", padx=16, pady=(0, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 13))
        self.apply_button = button(row, "Apply repair", self.on_apply, width=150, primary=True,
                                   state="disabled")
        self.apply_button.pack(side="right")

    # ------------------------------------------------------------ folder
    def on_browse(self):
        chosen = filedialog.askdirectory(title="Select the folder with the chapter files",
                                         initialdir=self.folder_var.get() or None)
        if chosen:
            self.folder_var.set(os.path.normpath(chosen))
            self.parse_folder()

    def parse_folder(self):
        if self._busy:
            return
        if self.queued and not messagebox.askyesno(
                "Queued repairs", "Changing folder drops the queued repairs. Continue?"):
            return
        folder = self.folder_var.get().strip()
        self.info = dr.parse_folder(folder, self.settings)
        ok = self.info["ok"]
        self.folder_status.configure(text=("✓  " if ok else "✕  ") + self.info["message"],
                                     text_color=DONE if ok else FAIL)
        for w in self.chapter_lines.winfo_children():
            w.destroy()
        lines = []
        for c in self.info["chapters"]:
            if c["ok"]:
                extra = ", ".join(n.split(".", 1)[1] for n in c["optional"])
                line = f"✓ {c['base']}" + (f"  · with {extra}" if extra else
                                           "  · no .srt / translation.json (nothing to re-time)")
                colour = WARN if c["warnings"] else SUBTITLE
            else:
                why = (["missing " + ", ".join(c["missing"])] if c["missing"] else []) + \
                    c["problems"]
                line = f"✕ {c['base']}  · " + "; ".join(why)
                colour = FAIL
            for w in c["warnings"]:
                line += f"  · ⚠ {w}"
            lines.append((line, colour))
        if self.info["chapters"]:
            n = len(self.info["readings"] or [])
            lines.append((f"readings.json: {n} reading(s) for this book" if self.info["readings"]
                          is not None else "readings.json: none yet - created by the first "
                                           "repair that uses a reading", SUBTITLE))
        for line, colour in lines:
            ctk.CTkLabel(self.chapter_lines, text=line, anchor="w", justify="left",
                         text_color=colour, font=ctk.CTkFont(size=11), wraplength=960,
                         height=16).pack(fill="x")
        self.readings = self.info["readings"] or []
        ready = self.info["ready"] if ok else []
        self.chapter_menu.configure(values=ready or ["(none)"])
        self.chapter_var.set(ready[0] if ready else "(none)")
        self.settings["last_folder"] = folder
        self._save_settings()
        self.load_chapter()

    def load_chapter(self):
        stop_playback()
        base = self.chapter_var.get()
        self.chapter = None
        self.scan_rows = {}
        self.selected = None
        self.part_readings, self.part_text, self.queued = {}, {}, {}
        if not self.info or base not in (self.info.get("ready") or []):
            self._render_results([], "")
            self._render_part()
            self._render_queue()
            self.chapter_facts.configure(text="")
            return
        try:
            self.chapter = dr.DynChapter(self.settings, self.folder_var.get().strip(), base)
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror("Chapter", f"Could not read {base}: {e}")
            return
        problem = self.chapter.check_audio()
        style = dr.dynamic_profile.STYLE_LABELS.get(self.chapter.style, self.chapter.style)
        self.chapter_facts.configure(
            text=(f"✕ {problem}" if problem else
                  f"{self.chapter.total} parts · {dr.clock(self.chapter.chunks()[-1]['end'])} · "
                  f"{dr.dynamic_profile.nickname(self.chapter.profile)} · {style}"),
            text_color=FAIL if problem else SUBTITLE)
        if problem:
            self.chapter = None
        cached = dr.load_scan(self.chapter) if self.chapter else None
        if cached:
            self.scan_rows = {r["index"]: r for r in cached["rows"]}
            self._show_flagged(cached, rescanned=False)
        else:
            self._render_results([], "Not scanned yet - find a Part by Time or Part, or scan.")
        self._render_part()
        self._render_queue()
        if self.chapter:
            self.log(f"opened {self.chapter.name}")

    # ------------------------------------------------------------ find
    def on_find_time(self):
        if not self.chapter:
            return
        seconds = dr.parse_time(self.time_var.get())
        if seconds is None:
            messagebox.showerror("Time", "Type the time as the reader shows it: 7:06 "
                                         "or 1:07:06.")
            return
        self.select(self.chapter.find_time(seconds))

    def on_find_part(self):
        if not self.chapter:
            return
        try:
            index = self.chapter.find_part(int(self.part_var.get().strip()))
        except ValueError:
            index = None
        if index is None:
            messagebox.showerror("Part", f"Type a Part number from 1 to {self.chapter.total}, "
                                         f"as the reader shows it.")
            return
        self.select(index)

    def on_scan(self, force=False):
        if self._busy or not self.chapter:
            return
        cached = None if force else dr.load_scan(self.chapter)
        if cached:
            self._show_flagged(cached, rescanned=False)
            return
        chapter = self.chapter

        def work():
            if force:
                for path in (dr.scan_cache_path(chapter),
                             os.path.join(chapter.work_dir, chapter.base + ".json")):
                    if os.path.isfile(path):
                        os.remove(path)
            self.log(f"scanning {chapter.name} with Whisper")
            data = dr.scan(chapter, self.log, self._register_proc)
            self.ui(lambda: self._scan_done(data))

        self._start(work)

    def _scan_done(self, data):
        self.scan_rows = {r["index"]: r for r in data["rows"]}
        self._show_flagged(data, rescanned=True)

    def _show_flagged(self, data, rescanned):
        rows = dr.flagged(data["rows"], self.settings["similarity_threshold"],
                          self.settings["min_judged_chars"])
        items = []
        for row in rows:
            colour = FAIL if row["similarity"] < 0.5 else (WARN if row["similarity"] < 0.75
                                                           else SUBTITLE)
            lr = row["length_ratio"]
            items.append((row["index"], f"match {row['similarity']:.0%} · "
                          + ("length n/a" if lr is None else f"length {lr:.2f}x"),
                          [("script  " + row["text"], ENTRY_TEXT),
                           ("heard   " + (row["heard"] or "(nothing)"), SUBTITLE)], colour))
        self._render_results(items, f"{len(rows)} of {len(data['rows'])} part(s) worth a listen · "
                                    f"{'scanned just now' if rescanned else 'scan of ' + data['scanned']}"
                                    f" · parts under {self.settings['min_judged_chars']} characters "
                                    f"are not judged")

    def on_known_readings(self):
        if not self.chapter:
            return
        if not self.readings:
            messagebox.showinfo("Known readings", "readings.json has no readings for this book "
                                                  "yet. It is created by the first repair that "
                                                  "uses one.")
            return
        hits = self.chapter.parts_with_readings(self.readings)
        by_word = {r["word"]: r["reading"] for r in self.readings}
        items = [(index, "holds " + ", ".join(f"{w} → {by_word[w]}" for w in words),
                  [("script  " + self.chapter.part(index)["text"], ENTRY_TEXT)], ACCENT)
                 for index, words in hits]
        self._render_results(items, f"{len(hits)} part(s) still read a word readings.json "
                                    f"knows · open one: its text is pre-filled with the reading")

    def _render_results(self, items, summary):
        for w in self.result_widgets:
            w.destroy()
        self.result_widgets = []
        self.find_summary.configure(text=summary)
        if not self.chapter:
            return
        for index, head, lines, colour in items:
            widget = ResultRow(self.results_frame, self.chapter.part(index), self.chapter.total,
                               head, lines, colour, self.select)
            widget.pack(fill="x", pady=(0, 6), padx=2)
            widget.set_selected(index == self.selected)
            self.result_widgets.append(widget)

    # ------------------------------------------------------------ a part
    def select(self, index):
        stop_playback()
        self.selected = index
        for w in self.result_widgets:
            w.set_selected(w.index == index)
        if index not in self.part_text:
            original = self.chapter.part(index)["tts_text"]
            text = dr.apply_readings(original, self.readings)
            self.part_text[index] = text
            self.part_readings[index] = {r["word"]: r["reading"] for r in self.readings
                                         if r["word"] in original}
        # Each Part opens on the style its chapter was rendered with.
        self.style_var.set(STYLE_LABELS.get(self.chapter.style, STYLE_LABELS["scale_default"]))
        self._render_part()

    def _render_part(self):
        part = self.chapter.part(self.selected) if self.chapter and self.selected is not None \
            else None
        state = "normal" if part else "disabled"
        self.play_button.configure(state=state)
        self.generate_button.configure(state=state)
        self.text_box.delete("1.0", "end")
        if not part:
            self.part_hint.configure(text="")
            self.part_facts.configure(text="")
            self.part_script.configure(text="Find a Part in step 2.")
            self.part_heard.configure(text="")
            self.readings_note.configure(text="")
            self.request_label.configure(text="")
            self._render_takes()
            return
        self.part_hint.configure(text=f"Part {part['part']}/{self.chapter.total}")
        self.part_facts.configure(
            text=f"{dr.clock(part['start'])} → {dr.clock(part['end'])} ({part['duration']:.2f}s)"
                 f" · {part['engine_len']} chars, band {part['band']} · asked "
                 f"{describe_request(part['request'])} · silence {part['gap_before']} before"
                 + (f" · repaired {part['repairs']}x" if part["repairs"] else ""))
        self.part_script.configure(text=part["text"])
        row = self.scan_rows.get(part["index"])
        self.part_heard.configure(text="heard   " + (row["heard"] if row else "(not scanned)"))
        self.text_box.insert("1.0", self.part_text[part["index"]])
        self._update_readings_note()
        self._update_request()
        self._render_takes()

    def _update_readings_note(self):
        used = self.part_readings.get(self.selected) or {}
        if not used:
            self.readings_note.configure(text="No reading applied to this Part.")
            return
        known = {r["word"] for r in self.readings}
        self.readings_note.configure(text="Readings in this text: " + ",  ".join(
            f"{w} → {r}" + ("" if w in known else " (new - saved to readings.json on Apply)")
            for w, r in used.items()))

    def _remember_text(self):
        if self.selected is not None:
            self.part_text[self.selected] = self.text_box.get("1.0", "end").strip()

    def on_reset_text(self):
        if self.selected is None:
            return
        self.part_text[self.selected] = self.chapter.part(self.selected)["tts_text"]
        self.part_readings[self.selected] = {}
        self._render_part()

    def on_add_reading(self):
        if self.selected is None:
            return
        word, reading = self.word_var.get().strip(), self.reading_var.get().strip()
        if not word or not reading:
            messagebox.showerror("Reading", "Type the word as the book writes it and how it "
                                            "should be read.")
            return
        original = self.chapter.part(self.selected)["text"]
        if word not in original and word not in self.chapter.part(self.selected)["tts_text"]:
            messagebox.showerror("Reading", f"“{word}” is not in this Part.")
            return
        self._remember_text()
        text = self.part_text[self.selected]
        if word not in text:
            messagebox.showinfo("Reading", f"“{word}” is no longer in the text - it may "
                                           f"already be replaced.")
        self.part_text[self.selected] = text.replace(word, reading)
        self.part_readings.setdefault(self.selected, {})[word] = reading
        self.word_var.set("")
        self.reading_var.set("")
        self._render_part()

    def _style(self):
        return STYLE_BY_LABEL.get(self.style_var.get(), self.chapter.style if self.chapter
                                  else dr.dynamic_profile.DEFAULT_STYLE)

    def _update_request(self):
        custom = self._style() == dr.CUSTOM_STYLE
        self.scale_entry.configure(state="normal" if custom else "disabled",
                                   text_color=ENTRY_TEXT if custom else "#C4C4CC")
        if not self.chapter or self.selected is None:
            return
        try:
            request = self._request()
        except ValueError:
            self.request_label.configure(text="type a scale, e.g. 1.6", text_color=FAIL)
            return
        rendered = self.chapter.part(self.selected)["request"]
        self.request_label.configure(
            text=f"asks {describe_request(request)}"
                 + ("  (as rendered)" if request == rendered else
                    f"  (rendered with {describe_request(rendered)})"),
            text_color=SUBTITLE)

    def _request(self):
        style = self._style()
        if style == dr.CUSTOM_STYLE:
            scale = float(self.scale_var.get())
            if not 0.5 <= scale <= 3.0:
                raise ValueError("scale out of range")
            return self.chapter.request_for(self.selected, style, scale)
        return self.chapter.request_for(self.selected, style)

    def on_play_part(self):
        if self._busy or not self.chapter or self.selected is None:
            return
        chapter, index = self.chapter, self.selected

        def work():
            clip = dr.listen_clip(chapter, index)
            stop_playback()
            play_wav(clip)
            self.log(f"playing Part {index + 1} with a second either side")

        self._start(work)

    # ------------------------------------------------------------ takes
    def on_generate(self):
        if self._busy or not self.chapter or self.selected is None:
            return
        self._remember_text()
        text = self.part_text[self.selected].strip()
        if not text:
            messagebox.showerror("Text", "The text sent to the TTS is empty.")
            return
        try:
            request = self._request()
        except ValueError:
            messagebox.showerror("Scale", "A custom scale must be a number from 0.5 to 3.0.")
            return
        used = {w: r for w, r in (self.part_readings.get(self.selected) or {}).items()
                if r in text}
        chapter, index, style = self.chapter, self.selected, self._style()
        count = int(self.count_var.get())

        def work():
            self.log(f"Part {index + 1}: {count} take(s), {STYLE_LABELS[style]}, "
                     f"{describe_request(request)}" + (" - edited text" if text !=
                                                       chapter.part(index)["tts_text"] else ""))
            dr.generate_takes(chapter, index, text, request, style, count, used, self.log,
                              self._register_proc)
            self.ui(self._render_takes)

        self._start(work)

    def _render_takes(self):
        for w in self.takes_frame.winfo_children():
            w.destroy()
        if not self.chapter or self.selected is None:
            return
        part = self.chapter.part(self.selected)
        takes = dr.existing_takes(self.chapter, self.selected)
        if not takes:
            ctk.CTkLabel(self.takes_frame, text="No takes yet.", text_color=SUBTITLE,
                         font=ctk.CTkFont(size=12)).pack(anchor="w")
            return
        queued = self.queued.get(self.selected)
        for number, take in enumerate(takes, start=1):
            is_queued = bool(queued and queued["wav"] == take["wav"])
            box = ctk.CTkFrame(self.takes_frame, fg_color=PASTEL_VIOLET if is_queued else CARD,
                               corner_radius=8, border_width=2 if is_queued else 1,
                               border_color=ACCENT if is_queued else BORDER)
            box.pack(fill="x", pady=(0, 6))
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(7, 0))
            ctk.CTkButton(row, text="▶", width=30, height=30, corner_radius=15,
                          fg_color=ACCENT, hover_color=ACCENT_HOVER,
                          command=lambda t=take: (stop_playback(), play_wav(t["wav"]))).pack(
                side="left")
            delta = take["seconds"] - part["duration"]
            ctk.CTkLabel(row, text=f"  TAKE {number}  ·  {STYLE_LABELS.get(take['style'], '')}  "
                                   f"{describe_request(take['request'])}  ·  {take['seconds']:.2f}s "
                                   f"({delta:+.2f}s)",
                         text_color=TITLE, font=ctk.CTkFont(size=12, weight="bold")).pack(
                side="left")
            if take.get("applied"):
                ctk.CTkLabel(row, text=f"  applied {take['applied'][:16].replace('T', ' ')}",
                             text_color=DONE, font=ctk.CTkFont(size=11)).pack(side="left")
            ctk.CTkButton(row, text="Queued" if is_queued else "Use this", width=90, height=30,
                          corner_radius=8, fg_color=ACCENT if is_queued else CARD,
                          hover_color=ACCENT_HOVER if is_queued else BG,
                          text_color="white" if is_queued else ENTRY_TEXT,
                          border_width=0 if is_queued else 1, border_color=NEUTRAL_BORDER,
                          command=lambda t=take: self.on_use(t)).pack(side="right")
            edited = take["tts_text"] != part["tts_text"]
            ctk.CTkLabel(box, text=("read as  " if edited else "same text  ") + take["tts_text"],
                         anchor="w", justify="left", wraplength=900,
                         text_color=ACCENT if edited else SUBTITLE,
                         font=ctk.CTkFont(size=12)).pack(fill="x", padx=10, pady=(2, 7))

    def on_use(self, take):
        if self.queued.get(take["index"], {}).get("wav") == take["wav"]:
            del self.queued[take["index"]]
        else:
            self.queued[take["index"]] = take
        self._render_takes()
        self._render_queue()

    # ------------------------------------------------------------ apply
    def _render_queue(self):
        for w in self.queue_frame.winfo_children():
            w.destroy()
        if not self.chapter or not self.queued:
            self.plan_label.configure(text="Nothing queued. Pick a take in step 4.")
            self.apply_button.configure(state="disabled")
            return
        try:
            p = dr.plan(self.chapter, list(self.queued.values()))
        except (ValueError, OSError) as e:
            self.plan_label.configure(text=f"Cannot plan: {e}", text_color=FAIL)
            self.apply_button.configure(state="disabled")
            return
        for e in p["edits"]:
            row = ctk.CTkFrame(self.queue_frame, fg_color="#FCFCFD", corner_radius=8,
                               border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=(0, 6))
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(fill="x", padx=10, pady=7)
            take = e["take"]
            known = {r["word"] for r in self.readings}
            new_words = [w for w in (take.get("readings") or {}) if w not in known]
            ctk.CTkLabel(inner, text=f"Part {e['index'] + 1}", width=80, anchor="w",
                         text_color=TITLE, font=ctk.CTkFont(size=12, weight="bold")).pack(
                side="left")
            ctk.CTkLabel(inner, text=f"{e['old_duration']}s → {e['new_duration']}s  ·  "
                                     f"{describe_request(take['request'])}"
                                     + (f"  ·  new reading: {', '.join(new_words)}"
                                        if new_words else ""),
                         text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(side="left")
            button(inner, "Remove", lambda i=e["index"]: self.on_unqueue(i), width=80, height=28,
                   danger=True).pack(side="right")
            button(inner, "Show", lambda i=e["index"]: self.select(i), width=70,
                   height=28).pack(side="right", padx=(0, 8))
        self.plan_label.configure(
            text=f"{len(p['edits'])} part(s) · chapter {dr.clock(p['old_total'])} → "
                 f"{dr.clock(p['new_total'])} ({p['delta']:+.2f}s) · {p['later_parts']} later "
                 f"part(s) move. Written in place - the folder is your copy, no backup is made.",
            text_color=SUBTITLE)
        self.apply_button.configure(state="normal" if not self._busy else "disabled")

    def on_unqueue(self, index):
        self.queued.pop(index, None)
        self._render_takes()
        self._render_queue()

    def on_apply(self):
        if self._busy or not self.chapter or not self.queued:
            return
        takes = list(self.queued.values())
        p = dr.plan(self.chapter, takes)
        if not messagebox.askyesno(
                "Apply repair",
                f"Replace {len(takes)} part(s) of {self.chapter.base} IN PLACE:\n\n"
                + ", ".join(f"Part {e['index'] + 1}" for e in p["edits"])
                + f"\n\nChapter {dr.clock(p['old_total'])} → {dr.clock(p['new_total'])}. The "
                  f".flac, .m4a, .sync.json, .render.json and any .srt / translation.json in "
                  f"the folder are rewritten. No backup is made - this folder should be your "
                  f"copy.\n\nApply?", icon="warning"):
            return
        chapter = self.chapter

        def work():
            self.log(f"applying {len(takes)} repair(s) to {chapter.name}")
            dr.apply(chapter, takes, self.log)
            self.ui(self._after_apply)

        self._start(work)

    def _after_apply(self):
        self.log("=== applied - the chapter in the folder is repaired")
        self.queued = {}
        self.part_text, self.part_readings = {}, {}
        if os.path.isfile(self.chapter.readings_path):
            self.readings = dr.load_readings(self.chapter.readings_path)
        self.scan_rows = {}
        self._render_results([], "Applied. Times after the repair moved - scan again to "
                                 "check the chapter.")
        self.selected = None
        self._render_part()
        self._render_queue()
        self.head_sub.configure(text=f"Repaired {self.chapter.base}")

    # ------------------------------------------------------------ plumbing
    def _save_settings(self):
        try:
            dr.save_settings(self.settings)
        except OSError as e:
            self.log(f"! could not save settings: {e}")

    def log(self, message):
        self._queue.put(("log", message))

    def ui(self, fn):
        self._queue.put(("call", fn))

    def _register_proc(self, proc):
        self._proc = proc

    def _start(self, target):
        self._busy = True
        self._cancel.clear()
        self.cancel_button.configure(state="normal")
        for b in (self.scan_button, self.generate_button, self.apply_button):
            b.configure(state="disabled")

        def run():
            try:
                target()
            except Exception as e:                  # never lose the reason
                message = f"{type(e).__name__}: {e}"
                self.log("! " + message)
                self.ui(lambda: messagebox.showerror("Failed", message))
            finally:
                self._proc = None
                self.ui(self._finish)

        threading.Thread(target=run, daemon=True).start()

    def _finish(self):
        self._busy = False
        self.cancel_button.configure(state="disabled")
        self.scan_button.configure(state="normal")
        has_part = self.chapter is not None and self.selected is not None
        self.generate_button.configure(state="normal" if has_part else "disabled")
        self.apply_button.configure(state="normal" if self.queued else "disabled")

    def on_cancel(self):
        proc = self._proc
        if proc and proc.poll() is None:
            import subprocess
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.log("cancelled - the worker and its children were stopped")

    def _drain(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("%H:%M:%S  ") + payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                else:
                    try:
                        payload()
                    except Exception as e:          # a UI error must say so
                        self.log(f"! {type(e).__name__}: {e}")
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def on_close(self):
        if self._busy and not messagebox.askyesno("Busy", "Something is still running. "
                                                          "Close anyway?"):
            return
        self.on_cancel()
        stop_playback()
        if self.queued and not messagebox.askyesno(
                "Queued repairs", "Repairs are queued but not applied. Close anyway? (The "
                                  "takes stay on disk and come back next time.)"):
            return
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    RepairApp().mainloop()
