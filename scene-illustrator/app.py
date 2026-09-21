"""
app.py
------
The Scene Illustrator window. One image per chapter, drawn and edited by
Qwen-Image-2.1 with character sheets (2026-09-21).

    1 Read       the first 25% of every chapter -> a drawable moment, an image
                 prompt and a character roster (local LLM)
    2 Cast       the people who get a character sheet: imported from v1 or
                 drawn here, with the roster names that mean them
    3 Chapters   per chapter: tick the cast (up to 3) -> Write prompt ->
                 samples -> choose one -> promote it, or fine-tune it by
                 instruction, round after round, then export
                 chapter_<N>_img_1.png

Everything is saved as you go, in <work_root>/<book>/chapters.json and
cast.json.

Run it with:   uv run python app.py
"""

import os
import queue
import re
import subprocess
import sys
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import illustrator as il

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

CHAPTER_RE = re.compile(r"^CHAPTER (\d+)/(\d+) (\S+) (ok|failed)")
TAKE_RE = re.compile(r"^TAKE (\d+)/(\d+) start$")
PROGRESS_RE = re.compile(r"^PROGRESS (\d+) (\d+)$")
SAMPLE_RE = re.compile(r"^SAMPLE (\d+)/(\d+) (.+?) ([\d.]+)s$")
THUMB = (168, 246)


def button(parent, text, command, width=96, primary=False, danger=False, height=32, **kw):
    if primary:
        return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                             fg_color=ACCENT, hover_color=ACCENT_HOVER, command=command,
                             font=ctk.CTkFont(size=13, weight="bold"), **kw)
    return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                         fg_color=CARD, hover_color=BG, border_width=1, border_color=NEUTRAL_BORDER,
                         text_color=FAIL if danger else ENTRY_TEXT, command=command, **kw)


def entry(parent, variable, width=None, placeholder=""):
    widget = ctk.CTkEntry(parent, textvariable=variable, height=32, corner_radius=8, border_width=1,
                          border_color=ENTRY_BORDER, fg_color=CARD, text_color=ENTRY_TEXT,
                          placeholder_text=placeholder)
    if width:
        widget.configure(width=width)
    return widget


def bring_to_front(window):
    """CustomTkinter finishes placing a Toplevel a moment after it is made,
    which leaves it behind the main window. Lift it again once that is done."""
    def lift():
        try:
            window.lift()
            window.focus_force()
            window.attributes("-topmost", True)
            window.after(400, lambda: window.attributes("-topmost", False))
        except Exception:
            pass
    window.after(60, lift)
    window.after(300, lift)


class Gallery:
    """The takes of one thing (a chapter's samples, one edit round, a cast
    member's sheet takes): thumbnails, Use this, Delete, tick boxes with
    Select all / none and a bulk delete. `container` is any dict with a list
    under `key`; `working`, `choose` and `forget` say which take is the chosen
    one, choose one, and drop a deleted one from wherever it was chosen."""

    def __init__(self, app, parent, save, refresh, key="samples", height=286, choose_text="Use this",
                 thumb=THUMB, working=None, choose=None, forget=None, chosen_text="working image"):
        self.app = app
        self.save = save
        self.refresh = refresh
        self.key = key
        self.choose_text = choose_text
        self.chosen_text = chosen_text
        self.thumb = thumb
        self.working = working or app.working_image
        self.choose = choose or app.set_working
        self.forget = forget or app.forget_image
        self.container = None
        self.picked = set()
        self._thumbs = []

        picks = ctk.CTkFrame(parent, fg_color="transparent")
        picks.pack(fill="x", padx=10, pady=(2, 0))
        button(picks, "Select all", self.select_all, width=90, height=28).pack(side="left")
        button(picks, "Select none", self.select_none, width=96, height=28).pack(side="left", padx=6)
        self.delete_btn = button(picks, "Delete selected", self.delete_picked, width=130, height=28,
                                 danger=True)
        self.delete_btn.pack(side="left")
        self.count_label = ctk.CTkLabel(picks, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.count_label.pack(side="left", padx=10)
        self.frame = ctk.CTkScrollableFrame(parent, fg_color=CARD, orientation="horizontal", height=height)
        self.frame.pack(fill="both", expand=True, padx=6, pady=(0, 8))

    def paths(self):
        return self.container.get(self.key, []) if self.container else []

    def show(self, container):
        self.container = container
        for w in self.frame.winfo_children():
            w.destroy()
        self._thumbs = []
        if container is None:
            self.picked = set()
            self._update_count()
            return
        self.picked &= set(self.paths())
        for path in self.paths():
            self._card(path)
        self._update_count()

    def _card(self, path):
        if not os.path.isfile(path):
            return
        working = self.working() == path
        card = ctk.CTkFrame(self.frame, fg_color=PASTEL_VIOLET if working else CARD, corner_radius=8,
                            border_width=2 if working else 1, border_color=ACCENT if working else BORDER)
        card.pack(side="left", padx=6, pady=6)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(4, 0))
        picked = ctk.BooleanVar(value=path in self.picked)
        ctk.CTkCheckBox(top, text="", width=24, variable=picked, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=lambda: self.pick(path, picked.get())).pack(side="left")
        ctk.CTkLabel(top, text=os.path.basename(path)[:-4].split("_")[-1], text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        with Image.open(path) as im:
            image = ctk.CTkImage(light_image=im.copy(), size=self.thumb)
        self._thumbs.append(image)
        thumb = ctk.CTkLabel(card, image=image, text="", cursor="hand2")
        thumb.pack(padx=6, pady=(2, 2))
        thumb.bind("<Button-1>", lambda _e, p=path: ImageViewer(self.app, p))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 6))
        if working:
            ctk.CTkLabel(row, text=self.chosen_text, text_color=DONE,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
        else:
            button(row, self.choose_text, lambda: self.choose(path), width=84,
                   height=26).pack(side="left")
        button(row, "Delete", lambda: self.delete(path), width=62, height=26, danger=True).pack(side="right")

    def pick(self, path, picked):
        (self.picked.add if picked else self.picked.discard)(path)
        self._update_count()

    def _update_count(self):
        n = len(self.picked)
        self.count_label.configure(text=f"{n} selected" if n else "")
        self.delete_btn.configure(state="normal" if n else "disabled")

    def select_all(self):
        if self.container:
            self.picked = {p for p in self.paths() if os.path.isfile(p)}
            self.refresh()

    def select_none(self):
        self.picked = set()
        self.refresh()

    def delete(self, path, refresh=True):
        self.container[self.key] = [p for p in self.paths() if p != path]
        self.forget(path)
        self.picked.discard(path)
        self.save()
        try:
            os.remove(path)
        except OSError:
            pass
        if refresh:
            self.refresh()

    def delete_picked(self):
        if not self.container or not self.picked:
            return
        paths = [p for p in self.paths() if p in self.picked]
        warn = f"\n\nOne of them is the {self.chosen_text}." if self.working() in paths else ""
        if not messagebox.askyesno("Delete takes", f"Delete {len(paths)} take(s)?{warn}"):
            return
        for path in paths:
            self.delete(path, refresh=False)
        self.picked = set()
        self.refresh()


class ImageViewer(ctk.CTkToplevel):
    """One take, big: Fit / 1:1 / zoom buttons, the wheel to zoom, drag to pan."""

    ZOOMS = [0.25, 0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]

    def __init__(self, parent, path):
        super().__init__(parent)
        self.title(os.path.basename(path))
        self.geometry("980x900")
        self.configure(fg_color=BG)
        self.image = Image.open(path)
        self.zoom = None                # None = fit to the window
        self._photo = None

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(10, 4))
        button(bar, "−", lambda: self.step(-1), width=44).pack(side="left")
        button(bar, "+", lambda: self.step(1), width=44).pack(side="left", padx=6)
        button(bar, "Fit", self.fit, width=64).pack(side="left")
        button(bar, "1:1", lambda: self.set_zoom(1.0), width=64).pack(side="left", padx=6)
        self.zoom_label = ctk.CTkLabel(bar, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.zoom_label.pack(side="left", padx=10)
        ctk.CTkLabel(bar, text=f"{self.image.width}×{self.image.height}", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12)).pack(side="right")

        self.canvas = ctk.CTkCanvas(self, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<MouseWheel>", lambda e: self.step(1 if e.delta > 0 else -1))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(60, self.render)
        bring_to_front(self)

    def current_zoom(self):
        if self.zoom:
            return self.zoom
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        return min(cw / self.image.width, ch / self.image.height)

    def step(self, direction):
        z = self.current_zoom()
        if direction > 0:
            nxt = [v for v in self.ZOOMS if v > z * 1.001]
            self.set_zoom(nxt[0] if nxt else self.ZOOMS[-1])
        else:
            prv = [v for v in self.ZOOMS if v < z * 0.999]
            self.set_zoom(prv[-1] if prv else self.ZOOMS[0])

    def set_zoom(self, value):
        self.zoom = value
        self.render()

    def fit(self):
        self.zoom = None
        self.render()

    def render(self):
        z = self.current_zoom()
        w, h = max(int(self.image.width * z), 1), max(int(self.image.height * z), 1)
        resample = Image.LANCZOS if z <= 1 else Image.NEAREST
        self._photo = ImageTk.PhotoImage(self.image.resize((w, h), resample))
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_image(max((cw - w) // 2, 0), max((ch - h) // 2, 0), anchor="nw",
                                 image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, max(w, cw), max(h, ch)))
        self.canvas.configure(cursor="fleur" if (w > cw or h > ch) else "")
        self.zoom_label.configure(text=f"{z * 100:.0f}%" + ("  (fit)" if self.zoom is None else ""))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Scene Illustrator")
        self.geometry("1320x950")
        self.minsize(1060, 700)
        self.configure(fg_color=BG)
        icon = os.path.join(SCRIPT_DIR, "illustrator_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.settings = il.load_settings()
        self.read = None
        self.chapters = None
        self.cast = None
        self.chapter = ""              # selected chapter base
        self.member = ""               # selected cast member id
        self.edit_sheets = []          # member ids attached to the next chapter edit
        self._proc = None
        self._queue = queue.Queue()
        self._drawn = []               # SAMPLE paths of the running job
        self._written = ""             # PROMPT path of a running write
        # ("samples", chapter) | ("edit", chapter, chain index) | ("write", chapter)
        # | ("sheet", member)
        self._target = None
        self._take = None
        self._take_started = 0.0
        self._step = (0, 0)
        self._last_secs = 0.0
        self._take_done = False
        self._starting = 0.0
        self._working_thumb = None

        self.text_var = ctk.StringVar(value=self.settings["last_text_folder"])
        self.book_var = ctk.StringVar(value=self.settings["last_book"])
        self.status_var = ctk.StringVar(value="")
        self.samples_var = ctk.StringVar(value=str(self.settings["draw_samples"]))
        self.takes_var = ctk.StringVar(value=str(self.settings["edit_count"]))
        self.change_var = ctk.StringVar()
        self.keep_var = ctk.StringVar()
        self.m_name = ctk.StringVar()
        self.m_aliases = ctk.StringVar()
        self.m_tag = ctk.StringVar()
        self.m_count = ctk.StringVar(value=str(self.settings["draw_samples"]))
        self.m_change = ctk.StringVar()
        self.output_var = ctk.StringVar(value=self.settings.get("last_output_folder", ""))

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        if self.book_var.get():
            self.after(200, self.open_book)

    # ------------------------------------------------------------ building
    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=58)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="Scene Illustrator", text_color=TITLE,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=20, pady=14)
        ctk.CTkLabel(header, textvariable=self.status_var, text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12)).pack(side="right", padx=20)
        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color=ACCENT_HOVER)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self._build_read(self.tabs.add("1  Read"))
        self._build_cast(self.tabs.add("2  Cast"))
        self._build_chapters(self.tabs.add("3  Chapters"))
        self.draw_status, self.draw_bar = self.chapter_status, self.chapter_bar

    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 10))
        return card

    def _build_read(self, tab):
        card = self._card(tab)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(row, text="Chapter text", width=110, anchor="w", text_color=TITLE).pack(side="left")
        button(row, "Browse", self.browse_text, width=80).pack(side="right")
        entry(row, self.text_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(row, text="Book", width=110, anchor="w", text_color=TITLE).pack(side="left")
        entry(row, self.book_var, width=220).pack(side="left")
        self.work_label = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=11))
        self.work_label.pack(side="left", padx=12)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 14))
        self.read_btn = button(row, "Read book", self.start_read, width=120, primary=True)
        self.read_btn.pack(side="left")
        self.stop_btn = button(row, "Stop", self.stop, width=80, danger=True, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.read_info = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.read_info.pack(side="left", padx=10)
        ctk.CTkLabel(row, text="the first 25% of each chapter", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="right")
        self.read_bar = ctk.CTkProgressBar(card, progress_color=ACCENT, height=8)
        self.read_bar.set(0)
        self.read_bar.pack(fill="x", padx=16, pady=(0, 14))

        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.roster_frame = ctk.CTkScrollableFrame(body, width=380, fg_color=CARD, corner_radius=11,
                                                   border_width=1, border_color=BORDER)
        self.roster_frame.pack(side="left", fill="y")
        self.log = ctk.CTkTextbox(body, fg_color=LOG_BG, text_color=LOG_FG, corner_radius=11,
                                  font=ctk.CTkFont(family="Consolas", size=12))
        self.log.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.book_var.trace_add("write", lambda *_: self._update_work_label())
        self._update_work_label()

    def _build_cast(self, tab):
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        left = ctk.CTkFrame(body, fg_color="transparent", width=260)
        left.pack(side="left", fill="y")
        tools = ctk.CTkFrame(left, fg_color="transparent")
        tools.pack(fill="x", pady=(0, 6))
        self.add_member_btn = button(tools, "Add character", self.add_member, width=120)
        self.add_member_btn.pack(side="left")
        self.import_btn = button(tools, "Import v1 sheets", self.import_v1, width=130)
        self.import_btn.pack(side="left", padx=6)
        self.member_list = ctk.CTkScrollableFrame(left, width=240, fg_color=CARD, corner_radius=11,
                                                  border_width=1, border_color=BORDER)
        self.member_list.pack(fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.member_pane = right
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.member_title = ctk.CTkLabel(head, text="Choose or add a character", anchor="w",
                                         text_color=TITLE, font=ctk.CTkFont(size=15, weight="bold"))
        self.member_title.pack(side="left")
        self.delete_member_btn = button(head, "Remove character", self.delete_member, width=140,
                                        danger=True)
        self.delete_member_btn.pack(side="right")

        def field(label, var, placeholder):
            row = ctk.CTkFrame(right, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(row, text=label, width=90, anchor="w", text_color=TITLE).pack(side="left")
            widget = entry(row, var, placeholder=placeholder)
            widget.pack(side="left", fill="x", expand=True)
            widget.bind("<FocusOut>", lambda _e: self.save_member())
            return row
        field("name", self.m_name, "the name prompts use, e.g. Mari")
        field("aliases", self.m_aliases, "roster names that mean this person, comma separated")
        self.roster_hint = ctk.CTkLabel(right, text="", anchor="w", justify="left", wraplength=880,
                                        text_color=SUBTITLE, font=ctk.CTkFont(size=11))
        self.roster_hint.pack(fill="x", padx=(104, 12))
        field("tag", self.m_tag, "optional: what sets them apart at a glance, e.g. in the baseball cap")
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=2)
        ctk.CTkLabel(row, text="description", width=90, anchor="nw", text_color=TITLE).pack(
            side="left", anchor="n")
        self.m_desc = ctk.CTkTextbox(row, height=60, fg_color=CARD, border_width=1,
                                     border_color=ENTRY_BORDER, text_color=ENTRY_TEXT,
                                     font=ctk.CTkFont(size=12))
        self.m_desc.pack(side="left", fill="x", expand=True)
        self.m_desc.bind("<FocusOut>", lambda _e: self.save_member())

        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(6, 2))
        self.sheet_btn = button(row, "Draw sheet", self.start_sheet, width=120, primary=True)
        self.sheet_btn.pack(side="left")
        entry(row, self.m_count, width=44).pack(side="left", padx=(8, 4))
        ctk.CTkLabel(row, text="takes, from the description", text_color=SUBTITLE).pack(side="left")
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=2)
        ctk.CTkLabel(row, text="change", width=90, anchor="w", text_color=TITLE).pack(side="left")
        change = entry(row, self.m_change, placeholder="edit the sheet, e.g. he carries a tote bag")
        change.pack(side="left", fill="x", expand=True)
        change.bind("<Return>", lambda _e: self.start_sheet_edit())
        self.sheet_edit_btn = button(row, "Edit sheet", self.start_sheet_edit, width=110, primary=True)
        self.sheet_edit_btn.pack(side="left", padx=(8, 0))
        status = ctk.CTkFrame(right, fg_color="transparent")
        status.pack(fill="x", padx=12, pady=(2, 4))
        self.cast_status = ctk.CTkLabel(status, text="", anchor="w", text_color=ENTRY_TEXT,
                                        font=ctk.CTkFont(size=12))
        self.cast_status.pack(fill="x")
        self.cast_bar = ctk.CTkProgressBar(status, progress_color=ACCENT, height=8)
        self.cast_bar.set(0)
        self.cast_bar.pack(fill="x", pady=(4, 0))
        self.sheet_gallery = Gallery(self, right, save=self.save_cast_file, refresh=self.render_member,
                                     key="samples", choose_text="Use as sheet", chosen_text="sheet",
                                     working=self.member_sheet, choose=self.set_sheet,
                                     forget=self.forget_sheet)

    def _build_chapters(self, tab):
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.chapter_list = ctk.CTkScrollableFrame(body, width=240, fg_color=CARD, corner_radius=11,
                                                   border_width=1, border_color=BORDER)
        self.chapter_list.pack(side="left", fill="y")
        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 2))
        self.chapter_title = ctk.CTkLabel(head, text="Choose a chapter", anchor="w", text_color=TITLE,
                                          font=ctk.CTkFont(size=15, weight="bold"))
        self.chapter_title.pack(side="left")
        self.final_label = ctk.CTkLabel(head, text="", text_color=DONE,
                                        font=ctk.CTkFont(size=12, weight="bold"))
        self.final_label.pack(side="right")
        self.moment_label = ctk.CTkLabel(right, text="", anchor="w", justify="left", wraplength=880,
                                         text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.moment_label.pack(fill="x", padx=12)
        self.prompt_box = ctk.CTkTextbox(right, height=84, fg_color=CARD, border_width=1,
                                         border_color=ENTRY_BORDER, text_color=ENTRY_TEXT,
                                         font=ctk.CTkFont(size=12))
        # the cast row sits ABOVE the prompt: who is in the picture decides what
        # Write prompt writes
        self.cast_row = ctk.CTkFrame(right, fg_color="transparent")
        self.cast_row.pack(fill="x", padx=12, pady=(6, 0))
        self.prompt_box.pack(fill="x", padx=12, pady=(4, 4))
        self.prompt_box.bind("<FocusOut>", lambda _e: self.save_prompt())
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.draw_btn = button(row, "Draw samples", self.start_draw, width=130, primary=True)
        self.draw_btn.pack(side="left")
        entry(row, self.samples_var, width=44).pack(side="left", padx=(8, 4))
        ctk.CTkLabel(row, text="samples", text_color=SUBTITLE).pack(side="left")
        button(row, "Reset prompt", self.reset_prompt, width=110).pack(side="right")
        self.write_btn = button(row, "Write prompt", self.start_write, width=120)
        self.write_btn.pack(side="right", padx=6)
        status = ctk.CTkFrame(right, fg_color="transparent")
        status.pack(fill="x", padx=12, pady=(0, 4))
        self.chapter_status = ctk.CTkLabel(status, text="", anchor="w", text_color=ENTRY_TEXT,
                                           font=ctk.CTkFont(size=12))
        self.chapter_status.pack(fill="x")
        self.chapter_bar = ctk.CTkProgressBar(status, progress_color=ACCENT, height=8)
        self.chapter_bar.set(0)
        self.chapter_bar.pack(fill="x", pady=(4, 0))

        self.stage = ctk.CTkSegmentedButton(right, values=["Samples", "Fine-tune"],
                                            command=lambda _v: self.render_chapter(),
                                            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self.stage.set("Samples")
        self.stage.pack(padx=12, pady=(4, 2), anchor="w")

        self.samples_pane = ctk.CTkFrame(right, fg_color="transparent")
        self.sample_gallery = Gallery(self, self.samples_pane, save=self.save,
                                      refresh=self.render_chapter, key="samples")

        self.tune_pane = ctk.CTkFrame(right, fg_color="transparent")
        top = ctk.CTkFrame(self.tune_pane, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(2, 2))
        self.working_label = ctk.CTkLabel(top, text="", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=12), compound="top")
        self.working_label.pack(side="left")
        # a plain frame, not a scrollable one: a nested scroll area either ate the
        # gallery's height or hid its own rows
        self.chain_frame = ctk.CTkFrame(top, fg_color=PASTEL_GREY, corner_radius=8)
        self.chain_frame.pack(side="left", fill="both", expand=True, padx=8)
        button(top, "Promote", self.promote, width=110, primary=True).pack(side="right", anchor="n")
        form = ctk.CTkFrame(self.tune_pane, fg_color="transparent")
        form.pack(fill="x", padx=10)
        ctk.CTkLabel(form, text="change", width=56, anchor="w", text_color=TITLE).pack(side="left")
        change = entry(form, self.change_var,
                       placeholder="e.g. the seated girl wears a navy baseball cap")
        change.pack(side="left", fill="x", expand=True)
        change.bind("<Return>", lambda _e: self.start_edit())
        form2 = ctk.CTkFrame(self.tune_pane, fg_color="transparent")
        form2.pack(fill="x", padx=10, pady=(4, 4))
        ctk.CTkLabel(form2, text="keep", width=56, anchor="w", text_color=TITLE).pack(side="left")
        entry(form2, self.keep_var,
              placeholder="who must NOT change, e.g. the standing man stays bare-headed").pack(
            side="left", fill="x", expand=True)
        self.edit_btn = button(form2, "Apply edit", self.start_edit, width=110, primary=True)
        self.edit_btn.pack(side="left", padx=(8, 4))
        entry(form2, self.takes_var, width=44).pack(side="left")
        ctk.CTkLabel(form2, text="takes", text_color=SUBTITLE).pack(side="left", padx=(4, 0))
        # sheets to pull something from, e.g. "the case from <image2>" (M9 test C)
        self.edit_sheet_row = ctk.CTkFrame(self.tune_pane, fg_color="transparent")
        self.edit_sheet_row.pack(fill="x", padx=10, pady=(0, 2))
        # smaller cards here: this pane also carries the working image, the round
        # list, the two instruction fields and the sheet ticks
        self.edit_gallery = Gallery(self, self.tune_pane, save=self.save, refresh=self.render_chapter,
                                    key="takes", height=200, thumb=(74, 108))

        # side="bottom": the footer stays under the galleries whatever order the
        # panes are packed in later
        foot = ctk.CTkFrame(right, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(foot, text="output folder", text_color=SUBTITLE).pack(side="left")
        entry(foot, self.output_var).pack(side="left", fill="x", expand=True, padx=8)
        button(foot, "Browse", self.browse_output, width=80).pack(side="left")
        button(foot, "Export image", self.export, width=120).pack(side="left", padx=6)
        button(foot, "Clear history", self.clear_history, width=110, danger=True).pack(side="left")

    # ------------------------------------------------------------- helpers
    def _update_work_label(self):
        book = self.book_var.get().strip()
        self.work_label.configure(text=il.book_dir(self.settings, book) if book else "")

    def log_line(self, line):
        self.log.insert("end", line + "\n")
        self.log.see("end")

    def root(self):
        return il.book_dir(self.settings, self.book_var.get().strip())

    def browse_text(self):
        folder = filedialog.askdirectory(initialdir=self.text_var.get() or "F:\\")
        if folder:
            self.text_var.set(os.path.normpath(folder))
            self.book_var.set(il.book_name_for(folder))
            self.open_book()

    def browse_output(self):
        folder = filedialog.askdirectory(initialdir=self.output_var.get() or "F:\\")
        if folder:
            self.output_var.set(os.path.normpath(folder))
            self.remember()

    def remember(self):
        self.settings["last_text_folder"] = self.text_var.get().strip()
        self.settings["last_book"] = self.book_var.get().strip()
        self.settings["last_output_folder"] = self.output_var.get().strip()
        il.save_settings(self.settings)

    def open_book(self):
        if not self.book_var.get().strip():
            return
        self.read = il.load_read(self.root())
        self.chapters = il.load_chapters(self.root())
        self.cast = il.load_cast(self.root())
        self.chapter = ""
        self.member = ""
        self.refresh_read_info()
        self.render_roster()
        self.render_member_list()
        self.render_member()
        self.render_chapter_list()
        self.render_chapter()

    def save(self):
        il.save_chapters(self.root(), self.chapters)

    def refresh_read_info(self):
        text = self.text_var.get().strip()
        if not os.path.isdir(text):
            self.read_info.configure(text="choose the chapter text folder", text_color=FAIL)
            return
        bases = il.chapter_bases(text)
        done = sum(1 for b in bases if b in (self.read or {}).get("chapters", {}))
        self.read_info.configure(text=f"{len(bases)} chapters, {done} read",
                                 text_color=DONE if bases and done == len(bases) else SUBTITLE)
        self.read_bar.set(done / len(bases) if bases else 0)

    def render_roster(self):
        for w in self.roster_frame.winfo_children():
            w.destroy()
        roster = (self.read or {}).get("roster", {})
        ctk.CTkLabel(self.roster_frame, text=f"{len(roster)} characters found", anchor="w",
                     text_color=TITLE, font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", padx=8, pady=(6, 2))
        for name, rec in sorted(roster.items(), key=lambda kv: -len(kv[1]["chapters"])):
            card = ctk.CTkFrame(self.roster_frame, fg_color=CARD, corner_radius=8, border_width=1,
                                border_color=BORDER)
            card.pack(fill="x", padx=6, pady=2)
            chapters = ", ".join(str(c) for c in rec["chapters"])
            aka = ("  (also: " + ", ".join(rec["aka"]) + ")") if rec.get("aka") else ""
            ctk.CTkLabel(card, text=f"{name}{aka}\nch {chapters}", anchor="w", justify="left",
                         text_color=TITLE, font=ctk.CTkFont(size=12)).pack(fill="x", padx=8, pady=(4, 0))
            ctk.CTkLabel(card, text=rec["appearance"], anchor="w", justify="left", wraplength=330,
                         text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(fill="x", padx=8, pady=(0, 5))

    # --------------------------------------------------------------- cast
    def save_cast_file(self):
        il.save_cast(self.root(), self.cast)

    def member_rec(self):
        if not self.cast or not self.member:
            return None
        return self.cast["members"].get(self.member)

    def member_sheet(self):
        member = self.member_rec()
        return member["sheet"] if member else ""

    def set_sheet(self, path):
        member = self.member_rec()
        if member:
            member["sheet"] = path
            self.save_cast_file()
            self.render_member_list()
            self.render_member()
            self.render_chapter()

    def forget_sheet(self, path):
        member = self.member_rec()
        if member and member["sheet"] == path:
            member["sheet"] = ""

    def render_member_list(self):
        for w in self.member_list.winfo_children():
            w.destroy()
        if not self.cast:
            return
        if not self.cast["members"]:
            ctk.CTkLabel(self.member_list, text="No characters yet.\nImport v1 sheets, or add one.",
                         justify="left", text_color=SUBTITLE).pack(padx=8, pady=8, anchor="w")
        for member_id, member in self.cast["members"].items():
            row = ctk.CTkFrame(self.member_list, fg_color=PASTEL_VIOLET if member_id == self.member
                               else CARD, corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            aliases = ", ".join(member["aliases"]) or "no aliases"
            state = "sheet ✓" if member["sheet"] else "no sheet yet"
            label = ctk.CTkLabel(row, text=f"{member['name']}   ·   {state}\n{aliases}", anchor="w",
                                 justify="left", font=ctk.CTkFont(size=12),
                                 text_color=ENTRY_TEXT if member["sheet"] else WARN)
            label.pack(side="left", fill="x", expand=True, padx=8, pady=5)
            for w in (row, label):
                w.bind("<Button-1>", lambda _e, i=member_id: self.select_member(i))

    def select_member(self, member_id):
        self.save_member()
        self.member = member_id
        self.sheet_gallery.picked = set()
        self.render_member_list()
        self.render_member()

    def render_member(self):
        member = self.member_rec()
        self.m_desc.delete("1.0", "end")
        if not member:
            self.member_title.configure(text="Choose or add a character")
            for var in (self.m_name, self.m_aliases, self.m_tag):
                var.set("")
            self.roster_hint.configure(text="")
            self.sheet_gallery.show(None)
            return
        self.member_title.configure(text=member["name"] or "(no name)")
        self.m_name.set(member["name"])
        self.m_aliases.set(", ".join(member["aliases"]))
        self.m_tag.set(member["tag"])
        self.m_desc.insert("1.0", member["description"])
        # the roster names no member claims yet - what aliases are for
        claimed = {a for m in self.cast["members"].values() for a in m["aliases"] + [m["name"]]}
        free = [n for n in (self.read or {}).get("roster", {}) if n not in claimed]
        self.roster_hint.configure(text=("roster names not claimed yet: " + ", ".join(free))
                                   if free else "every roster name is claimed")
        self.sheet_gallery.show(member)

    def save_member(self):
        member = self.member_rec()
        if not member:
            return
        name = self.m_name.get().strip()
        aliases = [a.strip() for a in re.split(r"[,、，]", self.m_aliases.get()) if a.strip()]
        new = {"name": name or member["name"], "aliases": aliases, "tag": self.m_tag.get().strip(),
               "description": self.m_desc.get("1.0", "end").strip()}
        if any(member[k] != v for k, v in new.items()):
            member.update(new)
            self.save_cast_file()
            self.member_title.configure(text=member["name"])
            self.render_member_list()
            self.render_chapter()

    def add_member(self):
        if not self.cast:
            messagebox.showinfo("Cast", "Open a book first (1 Read).")
            return
        self.save_member()
        member_id = il.new_member_id(self.cast)
        self.cast["members"][member_id] = il.new_member("new character")
        self.save_cast_file()
        self.select_member(member_id)

    def delete_member(self):
        member = self.member_rec()
        if not member:
            return
        if not messagebox.askyesno("Remove character",
                                   f"Remove {member['name']} and delete every sheet take of theirs?"
                                   "\n\nThis cannot be undone."):
            return
        import shutil
        folder = il.member_dir(self.root(), self.member)
        # only ever the member's own folder under this book's cast folder
        if os.path.normpath(os.path.dirname(folder)) == os.path.normpath(
                os.path.join(self.root(), "cast")) and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        del self.cast["members"][self.member]
        for record in self.chapters["chapters"].values():
            if record.get("cast"):
                record["cast"] = [i for i in record["cast"] if i != self.member]
        self.save()
        self.save_cast_file()
        self.member = ""
        self.render_member_list()
        self.render_member()
        self.render_chapter()

    def import_v1(self):
        if not self.cast:
            messagebox.showinfo("Cast", "Open a book first (1 Read).")
            return
        added = il.import_v1_sheets(self.root())
        self.cast = il.load_cast(self.root())
        self.log_line(f"-- imported {len(added)} v1 sheet(s)")
        self.render_member_list()
        self.render_chapter()
        messagebox.showinfo("Import v1 sheets",
                            f"{len(added)} sheet(s) imported." + (
                                "\n\nGive each one its aliases - the roster names that mean them - "
                                "so chapters can guess their cast." if added else
                                "\n\nNothing new: no v1 sheets in this book folder, or all are "
                                "imported already."))

    def _member_count(self):
        try:
            count = max(1, min(8, int(self.m_count.get())))
        except ValueError:
            count = int(self.settings["draw_samples"])
        self.m_count.set(str(count))
        return count

    def start_sheet(self):
        member = self.member_rec()
        if not member:
            return
        self.save_member()
        if not member["description"]:
            messagebox.showinfo("Draw sheet", "Write a description to draw from first.")
            return
        count = self._member_count()
        self._drawn = []
        self._target = ("sheet", self.member)
        self._answer_click(f"starting {count} sheet take(s)…", cast_tab=True)
        self.log_line(f"-- drawing {count} sheet take(s) for {member['name']}")
        self._run_child(["sheet", "--book", self.book_var.get().strip(), "--member", self.member,
                         "--count", str(count)], self._sheet_done)

    def start_sheet_edit(self):
        member = self.member_rec()
        if not member:
            return
        self.save_member()
        change = self.m_change.get().strip()
        if not member["sheet"]:
            messagebox.showinfo("Edit sheet", "Choose a sheet first (Use as sheet).")
            return
        if not change:
            messagebox.showinfo("Edit sheet", "Say what should change.")
            return
        count = self._member_count()
        path = os.path.join(il.member_dir(self.root(), self.member), "instruction.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(il.edit_prompt(change, ""))
        self._drawn = []
        self._target = ("sheet", self.member)
        self._answer_click(f"starting {count} sheet edit take(s)…", cast_tab=True)
        self.log_line(f"-- editing {member['name']}'s sheet: {change}")
        self._run_child(["edit", "--book", self.book_var.get().strip(), "--member", self.member,
                         "--base", member["sheet"], "--instruction-file", path,
                         "--count", str(count)], self._sheet_done)

    def _sheet_done(self, _code):
        member_id = self._target[1] if self._target else ""
        member = self.cast["members"].get(member_id) if member_id else None
        if member is not None and self._drawn:
            member["samples"] += self._drawn
            self.save_cast_file()
            self.log_line(f"-- {len(self._drawn)} take(s) added to {member['name']}")
            self.m_change.set("")
        self._drawn = []
        self._target = None
        if member_id and member_id != self.member:
            self.member = member_id
            self.log_line("-- showing that character again")
        self.render_member_list()
        self.render_member()

    # ------------------------------------------------------- child process
    def _run_child(self, args, on_done):
        if self._proc:
            return
        self.remember()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-u", os.path.join(SCRIPT_DIR, "illustrator.py")] + args
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                      encoding="utf-8", errors="replace",
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        for b in self._job_buttons():
            b.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        proc = self._proc

        def pump():
            # keep reading whatever happens, or the child blocks on a full pipe
            for line in proc.stdout:
                self._queue.put(("line", line.rstrip()))
            proc.wait()
            self._queue.put(("exit", (proc.returncode, on_done)))
        threading.Thread(target=pump, daemon=True).start()

    def _job_buttons(self):
        """Everything that starts a child: one engine on the card at a time."""
        return (self.read_btn, self.draw_btn, self.edit_btn, self.write_btn, self.sheet_btn,
                self.sheet_edit_btn, self.import_btn)

    def _drain(self):
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == "line":
                    self.log_line(value)
                    self._parse(value)
                else:
                    code, on_done = value
                    self._proc = None
                    for b in self._job_buttons():
                        b.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set("" if code == 0 else f"stopped (exit {code})")
                    self._take = None
                    self._starting = 0.0
                    self.draw_bar.stop()
                    self.draw_bar.configure(mode="determinate")
                    if code != 0:
                        self.draw_status.configure(text="stopped", text_color=WARN)
                    on_done(code)
        except queue.Empty:
            pass
        if self._starting or (self._take and not self._take_done):
            self.update_status()
        self.after(120, self._drain)

    def _parse(self, line):
        chapter = CHAPTER_RE.match(line)
        take = TAKE_RE.match(line)
        progress = PROGRESS_RE.match(line)
        sample = SAMPLE_RE.match(line)
        if chapter:
            self.read_bar.set(int(chapter.group(1)) / int(chapter.group(2)))
            self.status_var.set(f"reading {chapter.group(3)}")
        elif take:
            if self._starting:
                self._starting = 0.0
                self.draw_bar.stop()
                self.draw_bar.configure(mode="determinate")
            self._take = (int(take.group(1)), int(take.group(2)))
            self._take_started = time.time()
            self._step = (0, 0)
            self._take_done = False
            self.update_status()
        elif progress:
            self._step = (int(progress.group(1)), int(progress.group(2)))
            self.update_status()
        elif sample:
            self._drawn.append(sample.group(3))
            self._last_secs = float(sample.group(4))
            self._step = (0, 0)
            self._take_done = True
            self.update_status(done=True)
        elif line.startswith("PROMPT "):
            self._written = line.split(" ", 1)[1].strip()
        elif line.startswith("SERVER"):
            self.status_var.set(line.split(" ", 1)[1])
            if self._starting:
                self.draw_status.configure(text=line.split(" ", 1)[1], text_color=ENTRY_TEXT)

    def update_status(self, done=False):
        """Takes done + the sampler's own step. Loading a model reports
        nothing, so that stretch says "preparing" with elapsed seconds instead
        of a made-up percentage."""
        if not self._take:
            if self._starting:
                self.draw_status.configure(
                    text=f"starting…   ·   {time.time() - self._starting:.0f}s", text_color=ENTRY_TEXT)
            return
        i, n = self._take
        step, steps = self._step
        elapsed = time.time() - self._take_started
        share = (step / steps) if steps else 0.0
        self.draw_bar.set(((i - 1) + (1.0 if done else share)) / n)
        if done:
            text = f"take {i}/{n} done in {self._last_secs:.0f}s"
            if i < n:
                text += "   ·   next take starting"
        elif steps:
            text = f"take {i}/{n}   ·   step {step}/{steps}   ·   {elapsed:.0f}s"
        else:
            text = f"take {i}/{n}   ·   preparing   ·   {elapsed:.0f}s"
            if self._last_secs:
                text += f"   (last take {self._last_secs:.0f}s)"
        self.draw_status.configure(text=text, text_color=DONE if done and i == n else ENTRY_TEXT)

    def _answer_click(self, what, cast_tab=False):
        """Answer the click at once: an engine can take half a minute to start,
        and a dead bar reads as "did my click land?". Progress goes to the tab
        the job was started from."""
        if cast_tab:
            self.draw_status, self.draw_bar = self.cast_status, self.cast_bar
        else:
            self.draw_status, self.draw_bar = self.chapter_status, self.chapter_bar
        self._take = None
        self._take_done = False
        self._step = (0, 0)
        self._starting = time.time()
        self.draw_status.configure(text=what, text_color=ENTRY_TEXT)
        self.draw_bar.configure(mode="indeterminate")
        self.draw_bar.set(0)
        self.draw_bar.start()
        self.update_idletasks()

    def stop(self):
        if self._proc:
            # the tree, so the engine goes too and VRAM comes back
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self._proc.pid)], capture_output=True)
            self.log_line("-- stopped by you")

    # --------------------------------------------------------------- read
    def start_read(self):
        text = self.text_var.get().strip()
        book = self.book_var.get().strip()
        if not os.path.isdir(text) or not il.chapter_files(text):
            messagebox.showerror("Scene Illustrator", "Choose a folder holding chapter_*.txt files.")
            return
        if not book:
            messagebox.showerror("Scene Illustrator", "Give the book a name.")
            return
        if self.read and self.read["chapters"] and not messagebox.askyesno(
                "Read book", "Read again?\n\nMoments and the roster are rewritten. Prompts you "
                             "edited, samples, edits and final images are kept."):
            return
        self.log_line(f"-- reading the first 25% of every chapter of {book}")
        self._run_child(["read", "--book", book, "--text", text], self._read_done)

    def _read_done(self, _code):
        self.open_book()

    # ----------------------------------------------------------- chapters
    def render_chapter_list(self):
        for w in self.chapter_list.winfo_children():
            w.destroy()
        text = self.text_var.get().strip()
        if not self.chapters or not os.path.isdir(text):
            return
        for base in il.chapter_bases(text):
            record = self.chapters["chapters"].get(base)
            row = ctk.CTkFrame(self.chapter_list,
                               fg_color=PASTEL_VIOLET if base == self.chapter else CARD,
                               corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            if not record or not record["prompt"]:
                state, colour = "not read yet", SUBTITLE
            elif record["final"]:
                state, colour = "final image ✓", DONE
            elif record["chosen"]:
                rounds = len(record["chain"])
                state = "working image" + (f" · {rounds} edit round(s)" if rounds else "")
                colour = ENTRY_TEXT
            elif record["samples"]:
                state, colour = f"{len(record['samples'])} samples", ENTRY_TEXT
            else:
                state, colour = "ready to draw", ENTRY_TEXT
            label = ctk.CTkLabel(row, text=f"{base.replace('chapter_', 'Chapter ')}\n{state}",
                                 anchor="w", justify="left", text_color=colour,
                                 font=ctk.CTkFont(size=12))
            label.pack(side="left", fill="x", expand=True, padx=8, pady=5)
            for w in (row, label):
                w.bind("<Button-1>", lambda _e, b=base: self.select_chapter(b))

    def select_chapter(self, base):
        self.save_prompt()
        self.chapter = base
        self.sample_gallery.picked = set()
        self.edit_gallery.picked = set()
        self.render_chapter_list()
        self.render_chapter()

    def entry(self):
        if not self.chapters or not self.chapter:
            return None
        return self.chapters["chapters"].get(self.chapter)

    def working_image(self):
        record = self.entry()
        return record["chosen"] if record else ""

    def forget_image(self, path):
        """A deleted file must not stay as the working or final image."""
        record = self.entry()
        if not record:
            return
        if record["chosen"] == path:
            record["chosen"] = ""
        if record["final"] == path:
            record["final"] = ""
        for step in record["chain"]:
            if step.get("chosen") == path:
                step["chosen"] = ""

    def sheet_members(self):
        """Cast members that have a sheet, in the Cast tab's order."""
        return [(i, m) for i, m in (self.cast or {}).get("members", {}).items() if m["sheet"]]

    def chapter_cast(self):
        record = self.entry()
        return il.chapter_cast(self.cast, record) if record and self.cast else []

    def _tick_row(self, frame, label, chosen, limit, on_change, empty):
        """A label and one tick box per cast member with a sheet; at most
        `limit` ticked (M10)."""
        for w in frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(frame, text=label, text_color=TITLE).pack(side="left", padx=(0, 6))
        members = self.sheet_members()
        if not members:
            ctk.CTkLabel(frame, text=empty, text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(
                side="left")
            return

        def toggle(member_id, var):
            ticked = [i for i in chosen if i != member_id] + ([member_id] if var.get() else [])
            if len(ticked) > limit:
                var.set(False)
                messagebox.showinfo("Cast", f"At most {limit} here - a 4th sheet lost a character "
                                            "in testing (DESIGN.md M10).")
                return
            on_change(ticked)
        for member_id, member in members:
            var = ctk.BooleanVar(value=member_id in chosen)
            # width=24: a box sized to its name, or seven names overflow the row
            ctk.CTkCheckBox(frame, text=member["name"], variable=var, width=24, fg_color=ACCENT,
                            hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT,
                            command=lambda i=member_id, v=var: toggle(i, v)).pack(side="left", padx=(4, 8))

    def set_chapter_cast(self, ticked):
        record = self.entry()
        if record is not None:
            record["cast"] = ticked      # an explicit choice now: no longer derived
            self.save()
            self.render_chapter()

    def set_edit_sheets(self, ticked):
        self.edit_sheets = ticked
        self.render_chapter()

    def render_chapter(self):
        self.samples_pane.pack_forget()
        self.tune_pane.pack_forget()
        for w in self.chain_frame.winfo_children():
            w.destroy()
        self.prompt_box.delete("1.0", "end")
        record = self.entry()
        if not record:
            self.chapter_title.configure(text="Choose a chapter")
            self.moment_label.configure(text="")
            self.final_label.configure(text="")
            self.sample_gallery.show(None)
            self.edit_gallery.show(None)
            return
        self.chapter_title.configure(text=self.chapter.replace("chapter_", "Chapter "))
        read_as = ", ".join(record.get("characters") or []) or "nobody named"
        self.moment_label.configure(text=f"{record.get('moment', '')}   ·   the reading saw: {read_as}")
        self.final_label.configure(text="final image set" if record["final"] else "")
        self.prompt_box.insert("1.0", record["prompt"])
        guessed = record.get("cast") is None and self.chapter_cast()
        self._tick_row(self.cast_row, "cast:" + ("  (guessed from aliases)" if guessed else ""),
                       self.chapter_cast(), il.MAX_SHEETS, self.set_chapter_cast,
                       "no sheets yet - the style sample is used (see the Cast tab)")
        self.edit_sheets = [i for i in self.edit_sheets if i in dict(self.sheet_members())]
        self._tick_row(self.edit_sheet_row, "attach sheets:", self.edit_sheets, il.MAX_EDIT_SHEETS,
                       self.set_edit_sheets, "none - add characters in the Cast tab")
        # which slot each sheet is, for the change text ("the case from <image2>");
        # at the end of the tick row - a row of its own squeezed the takes gallery
        if self.edit_sheets:
            slots = ", ".join(f"{self.cast['members'][i]['name']} = <image{n}>"
                              for n, i in enumerate(self.edit_sheets, 2))
            ctk.CTkLabel(self.edit_sheet_row, text=f"→ {slots}", text_color=ACCENT,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(6, 0))

        if self.stage.get() == "Samples":
            self.samples_pane.pack(fill="both", expand=True)
            self.sample_gallery.show(record)
            self.edit_gallery.show(None)
        else:
            self.tune_pane.pack(fill="both", expand=True)
            self._render_working(record)
            self._render_chain(record)
            self.edit_gallery.show(record["chain"][-1] if record["chain"] else None)
            self.sample_gallery.show(None)

    def _render_working(self, record):
        path = record["chosen"]
        if path and os.path.isfile(path):
            with Image.open(path) as im:
                self._working_thumb = ctk.CTkImage(light_image=im.copy(), size=(74, 108))
            self.working_label.configure(image=self._working_thumb,
                                         text="  working image: " + os.path.basename(path))
        else:
            self._working_thumb = None
            self.working_label.configure(image=None,
                                         text="No working image yet - pick one under Samples.")

    def _render_chain(self, record):
        if not record["chain"]:
            ctk.CTkLabel(self.chain_frame, text="No edits yet. Say what should change, and who must "
                                                "stay as they are.", anchor="w", text_color=SUBTITLE,
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=6, pady=4)
            return
        steps = list(enumerate(record["chain"], 1))
        # only the latest two rounds (the rest stay in the file): with the sheet
        # ticks, a longer list pushed the takes' buttons off the bottom
        if len(steps) > 2:
            ctk.CTkLabel(self.chain_frame, text=f"… {len(steps) - 2} earlier round(s)", anchor="w",
                         text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(fill="x", padx=8, pady=(4, 0))
            steps = steps[-2:]
        for i, step in steps:
            row = ctk.CTkFrame(self.chain_frame, fg_color=CARD, corner_radius=6)
            row.pack(fill="x", padx=4, pady=2)
            if step.get("chosen"):
                mark = "  ✓"
            elif not step.get("takes"):
                mark = "   (no takes kept)"
            else:
                mark = ""
            ctk.CTkLabel(row, text=f"{i}. {step['change']}{mark}", anchor="w", justify="left",
                         wraplength=560, text_color=TITLE, font=ctk.CTkFont(size=11)).pack(
                side="left", fill="x", expand=True, padx=6, pady=3)
            if step.get("chosen"):
                button(row, "Use", lambda p=step["chosen"]: self.set_working(p), width=50,
                       height=24).pack(side="right", padx=4)
            button(row, "Back to before", lambda p=step["parent"]: self.set_working(p), width=112,
                   height=24).pack(side="right")

    def save_prompt(self):
        record = self.entry()
        if record:
            text = self.prompt_box.get("1.0", "end").strip()
            if text and text != record["prompt"]:
                record["prompt"] = text
                self.save()

    def reset_prompt(self):
        record = self.entry()
        source = (self.read or {}).get("chapters", {}).get(self.chapter, {})
        if record and source.get("subject"):
            record["prompt"] = il.build_prompt(source["subject"])
            self.save()
            self.render_chapter()

    def start_write(self):
        record = self.entry()
        if not record:
            return
        cast = self.chapter_cast()
        if not cast:
            messagebox.showinfo("Write prompt", "Tick who is in the picture first - the prompt is "
                                                "written around their names.")
            return
        self.save_prompt()
        if record["prompt"].strip() and not messagebox.askyesno(
                "Write prompt", "Replace the current prompt with a new one written around "
                                "the ticked cast?"):
            return
        self._written = ""
        self._target = ("write", self.chapter)
        self._answer_click("writing a prompt…")
        self.log_line(f"-- writing a prompt for {self.chapter}")
        args = ["write", "--book", self.book_var.get().strip(), "--chapter", self.chapter]
        for member_id in cast:
            args += ["--cast", member_id]
        self._run_child(args, self._write_done)

    def _write_done(self, code):
        base = self._target[1] if self._target else ""
        record = self.chapters["chapters"].get(base) if base else None
        if code == 0 and record is not None and self._written and os.path.isfile(self._written):
            with open(self._written, encoding="utf-8") as f:
                record["prompt"] = f.read().strip()
            self.save()
            self.draw_status.configure(text="new prompt written - edit it freely", text_color=DONE)
        self._written = ""
        self._target = None
        self._show_result(base, self.stage.get())

    def set_working(self, path):
        record = self.entry()
        if not record or not path:
            return
        record["chosen"] = path
        for step in record["chain"]:
            if path in step.get("takes", []):
                step["chosen"] = path
        self.save()
        self.render_chapter_list()
        self.render_chapter()

    def promote(self):
        record = self.entry()
        if not record or not record["chosen"]:
            messagebox.showinfo("Promote", "Pick a working image first.")
            return
        record["final"] = record["chosen"]
        self.save()
        self.render_chapter_list()
        self.render_chapter()

    def start_draw(self):
        record = self.entry()
        if not record:
            return
        self.save_prompt()
        if not record["prompt"].strip():
            messagebox.showerror("Draw", "This chapter has no prompt - read the book first.")
            return
        try:
            count = max(1, min(8, int(self.samples_var.get())))
        except ValueError:
            count = int(self.settings["draw_samples"])
        self.samples_var.set(str(count))
        self.settings["draw_samples"] = count
        cast = self.chapter_cast()
        names = [self.cast["members"][i]["name"] for i in cast]
        missing = [n for n in names if n not in record["prompt"]]
        if missing and not messagebox.askyesno(
                "Draw", f"The prompt never names {', '.join(missing)}, so the model cannot tell "
                        "who does what.\n\nWrite prompt fixes that. Draw anyway?"):
            return
        args = ["draw", "--book", self.book_var.get().strip(), "--chapter", self.chapter,
                "--count", str(count)]
        for member_id in cast:
            args += ["--cast", member_id]
        self._drawn = []
        self._target = ("samples", self.chapter, -1)
        self._answer_click(f"starting to draw {count} sample(s)…")
        self.stage.set("Samples")
        self.log_line(f"-- drawing {count} sample(s) for {self.chapter}"
                      + (f" with {', '.join(names)}" if names else " with the style sample"))
        self._run_child(args, self._draw_done)

    def _draw_done(self, _code):
        base = self._target[1] if self._target else ""
        if self._drawn and self._target:
            record = self.chapters["chapters"].setdefault(base, il.new_chapter())
            record["samples"] += self._drawn
            self.save()
            self.log_line(f"-- {len(self._drawn)} sample(s) added to {base}")
        self._drawn = []
        self._target = None
        self._show_result(base, "Samples")

    def start_edit(self):
        record = self.entry()
        if not record:
            return
        if not record["chosen"]:
            messagebox.showinfo("Apply edit", "Pick a working image first, under Samples.")
            return
        change = self.change_var.get().strip()
        if not change:
            messagebox.showinfo("Apply edit", "Say what should change.")
            return
        try:
            count = max(1, min(8, int(self.takes_var.get())))
        except ValueError:
            count = int(self.settings["edit_count"])
        self.takes_var.set(str(count))
        self.settings["edit_count"] = count
        sheets = [self.cast["members"][i] for i in self.edit_sheets]
        instruction = il.edit_prompt(change, self.keep_var.get(), sheets)
        path = os.path.join(il.chapter_dir(self.root(), self.chapter), "instruction.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(instruction)
        record["chain"].append({"change": change, "keep": self.keep_var.get().strip(),
                                "instruction": instruction, "parent": record["chosen"],
                                "takes": [], "chosen": ""})
        self.save()
        self._drawn = []
        self._target = ("edit", self.chapter, len(record["chain"]) - 1)
        self._answer_click(f"starting {count} edit take(s)…")
        self.stage.set("Fine-tune")
        self.log_line(f"-- editing {self.chapter}: {change}")
        args = ["edit", "--book", self.book_var.get().strip(), "--chapter", self.chapter,
                "--base", record["chosen"], "--instruction-file", path, "--count", str(count)]
        for member_id in self.edit_sheets:
            args += ["--cast", member_id]
        self._run_child(args, self._edit_done)

    def _edit_done(self, _code):
        base = self._target[1] if self._target else ""
        if self._target and self._target[0] == "edit":
            _, base, index = self._target
            record = self.chapters["chapters"].get(base)
            if record and index < len(record["chain"]):
                if self._drawn:
                    record["chain"][index]["takes"] += self._drawn
                    self.log_line(f"-- {len(self._drawn)} take(s) added to {base}")
                elif not record["chain"][index]["takes"]:
                    record["chain"].pop(index)      # nothing drawn: drop the empty round
                self.save()
        self._drawn = []
        self._target = None
        self.change_var.set("")
        self._show_result(base, "Fine-tune")

    def _show_result(self, base, stage):
        """Put the finished chapter back on screen. A draw or an edit takes
        minutes, and whatever was selected when it ended used to be what got
        redrawn - so results looked like they had vanished (user report
        2026-09-21)."""
        if base and base != self.chapter:
            self.chapter = base
            self.log_line(f"-- showing {base} again")
        if base:
            self.stage.set(stage)
        self.render_chapter_list()
        self.render_chapter()

    def export(self):
        record = self.entry()
        folder = self.output_var.get().strip()
        if not record or not record["final"]:
            messagebox.showinfo("Export", "Promote a working image to the chapter image first.")
            return
        if not os.path.isdir(folder):
            messagebox.showerror("Export", "Choose the book's output folder (where the chapter "
                                           "audio is).")
            return
        number = self.chapter.split("_")[1]
        target = os.path.join(folder, f"chapter_{number}_img_1.png")
        if os.path.exists(target) and not messagebox.askyesno(
                "Export", f"{os.path.basename(target)} already exists.\n\nReplace it?"):
            return
        dst = il.export_final(self.root(), self.chapter, record["final"], folder)
        self.log_line(f"-- exported {dst}")
        messagebox.showinfo("Export", f"Written:\n{dst}")

    def clear_history(self):
        record = self.entry()
        if not record:
            return
        if not messagebox.askyesno(
                "Clear history",
                "Delete every sample and edit take of this chapter except the final image and the "
                "working image?\n\nThis cannot be undone."):
            return
        removed = il.clear_history(self.root(), self.chapter, [record["final"], record["chosen"]])
        record["samples"] = [p for p in record["samples"] if os.path.isfile(p)]
        for step in record["chain"]:
            step["takes"] = [p for p in step["takes"] if os.path.isfile(p)]
            if step.get("chosen") and not os.path.isfile(step["chosen"]):
                step["chosen"] = ""
        record["chain"] = [s for s in record["chain"] if s["takes"] or s.get("chosen")]
        self.save()
        self.log_line(f"-- cleared {removed} file(s) for {self.chapter}")
        self.render_chapter_list()
        self.render_chapter()

    def on_close(self):
        self.save_prompt()
        if self._proc:
            if not messagebox.askyesno("Scene Illustrator", "A run is in progress. Stop it and close?"):
                return
            self.stop()
        self.remember()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
