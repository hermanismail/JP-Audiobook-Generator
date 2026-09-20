"""
app.py
------
The Scene Illustrator window. Stage A (2026-09-20):

    Read            the book's chapter text -> local LLM -> raw cast & places
                    (runs `illustrator.py read` as a child process)
    Cast & places   you merge, rename, delete, pre-mark main, and keep or
                    drop each detail against the sentence it came from

Every change is saved to <work_root>/<book>/bible.json at once.

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

import comfy
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

PIECE_RE = re.compile(r"^PIECE (\d+)/(\d+) (\S+) (ok|failed)")
SAMPLE_RE = re.compile(r"^SAMPLE (\d+)/(\d+) (.+?) ([\d.]+)s$")
TAKE_RE = re.compile(r"^TAKE (\d+)/(\d+) start$")
PROGRESS_RE = re.compile(r"^PROGRESS (\d+) (\d+)$")
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


def chapters_text(chapters):
    return "ch " + ", ".join(str(c) for c in chapters) if chapters else "added by hand"


class Gallery:
    """The takes of one thing (a reference variant, or a scene): thumbnails,
    Choose, Delete, tick boxes with Select all / none and a bulk delete.
    Both tabs use it; `container` is any dict with "samples" and "chosen"."""

    def __init__(self, app, parent, save, refresh, height=290):
        self.app = app
        self.save = save
        self.refresh = refresh
        self.container = None
        self.picked = set()
        self._thumbs = []

        picks = ctk.CTkFrame(parent, fg_color="transparent")
        picks.pack(fill="x", padx=14, pady=(2, 0))
        button(picks, "Select all", self.select_all, width=90, height=28).pack(side="left")
        button(picks, "Select none", self.select_none, width=96, height=28).pack(side="left", padx=6)
        self.delete_btn = button(picks, "Delete selected", self.delete_picked, width=130, height=28, danger=True)
        self.delete_btn.pack(side="left")
        self.count_label = ctk.CTkLabel(picks, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.count_label.pack(side="left", padx=10)
        self.frame = ctk.CTkScrollableFrame(parent, fg_color=CARD, orientation="horizontal", height=height)
        self.frame.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def show(self, container):
        self.container = container
        for w in self.frame.winfo_children():
            w.destroy()
        self._thumbs = []
        if container is None:
            self.picked = set()
            self._update_count()
            return
        self.picked &= set(container["samples"])
        for path in container["samples"]:
            self._card(path)
        self._update_count()

    def _card(self, path):
        if not os.path.isfile(path):
            return
        chosen = self.container["chosen"] == path
        card = ctk.CTkFrame(self.frame, fg_color=PASTEL_VIOLET if chosen else CARD, corner_radius=8,
                            border_width=2 if chosen else 1, border_color=ACCENT if chosen else BORDER)
        card.pack(side="left", padx=6, pady=6)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(4, 0))
        picked = ctk.BooleanVar(value=path in self.picked)
        ctk.CTkCheckBox(top, text="", width=24, variable=picked, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=lambda: self.pick(path, picked.get())).pack(side="left")
        ctk.CTkLabel(top, text=os.path.basename(path).split("_")[-1][:-4], text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        with Image.open(path) as im:
            image = ctk.CTkImage(light_image=im.copy(), size=THUMB)
        self._thumbs.append(image)
        thumb = ctk.CTkLabel(card, image=image, text="", cursor="hand2")
        thumb.pack(padx=6, pady=(2, 2))
        thumb.bind("<Button-1>", lambda _e, p=path: ImageViewer(self.app, p))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 6))
        if chosen:
            ctk.CTkLabel(row, text="chosen", text_color=DONE,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
        else:
            button(row, "Choose", lambda: self.choose(path), width=74, height=26).pack(side="left")
        button(row, "Delete", lambda: self.delete(path), width=68, height=26, danger=True).pack(side="right")

    def pick(self, path, picked):
        (self.picked.add if picked else self.picked.discard)(path)
        self._update_count()

    def _update_count(self):
        n = len(self.picked)
        self.count_label.configure(text=f"{n} selected" if n else "")
        self.delete_btn.configure(state="normal" if n else "disabled")

    def select_all(self):
        if self.container:
            self.picked = {p for p in self.container["samples"] if os.path.isfile(p)}
            self.refresh()

    def select_none(self):
        self.picked = set()
        self.refresh()

    def choose(self, path):
        self.container["chosen"] = path
        self.save()
        self.refresh()

    def delete(self, path, refresh=True):
        self.container["samples"] = [p for p in self.container["samples"] if p != path]
        if self.container["chosen"] == path:
            self.container["chosen"] = ""
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
        paths = [p for p in self.container["samples"] if p in self.picked]
        warn = "\n\nOne of them is the chosen image." if self.container["chosen"] in paths else ""
        if not messagebox.askyesno("Delete takes", f"Delete {len(paths)} take(s)?{warn}"):
            return
        for path in paths:
            self.delete(path, refresh=False)
        self.picked = set()
        self.refresh()

    def add(self, paths):
        self.container["samples"] += paths
        self.save()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Scene Illustrator")
        self.geometry("1240x900")
        self.minsize(1000, 680)
        self.configure(fg_color=BG)
        icon = os.path.join(SCRIPT_DIR, "illustrator_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.settings = il.load_settings()
        self.bible = None
        self.refs = None
        self.ref_kind = "characters"    # References tab
        self.ref_entry = None           # selected entry id there
        self.ref_variant = 0
        self._drawn = []                # SAMPLE paths of the running job
        self._draw_target = None        # (kind, entry id, variant) that job is for
        self._take = None               # (i, n) of the take being drawn
        self._take_started = 0.0
        self._step = (0, 0)             # sampler step from ComfyUI's WebSocket
        self._last_secs = 0.0
        self._take_done = False
        self._starting = 0.0            # clicked Draw, engine not drawing yet
        self._draw_where = "refs"       # which tab owns the status line
        self.scenes = None
        self.scene_chapter = ""
        self.scene_index = 0
        self.kind = "characters"
        self.current = None            # selected entry id
        self.selected = set()          # ids ticked for merge / delete
        self._proc = None
        self._queue = queue.Queue()

        self.text_var = ctk.StringVar(value=self.settings["last_text_folder"])
        self.book_var = ctk.StringVar(value=self.settings["last_book"])
        self.status_var = ctk.StringVar(value="")
        self.name_var = ctk.StringVar()
        self.main_var = ctk.BooleanVar()
        self.new_detail_var = ctk.StringVar()
        self.style_var = ctk.StringVar()
        self.note_var = ctk.StringVar(value=il.DEFAULT_STYLE_NOTE)
        self.takes_var = ctk.StringVar(value=str(self.settings["takes"]))

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
        self._build_cast(self.tabs.add("2  Cast & places"))
        self._build_refs(self.tabs.add("3  References"))
        self._build_scenes(self.tabs.add("4  Scenes"))

    def _card(self, parent, **pack):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 10), **pack)
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
        self.progress = ctk.CTkProgressBar(card, progress_color=ACCENT, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(0, 14))

        self.log = ctk.CTkTextbox(tab, fg_color=LOG_BG, text_color=LOG_FG, corner_radius=11,
                                  font=ctk.CTkFont(family="Consolas", size=12))
        self.log.pack(fill="both", expand=True)
        self.book_var.trace_add("write", lambda *_: self._update_work_label())
        self._update_work_label()

    def _build_cast(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        self.kind_switch = ctk.CTkSegmentedButton(bar, values=["Characters", "Places"],
                                                  command=self.switch_kind,
                                                  selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self.kind_switch.set("Characters")
        self.kind_switch.pack(side="left")
        button(bar, "Merge ticked", self.merge_selected, width=120).pack(side="left", padx=(16, 6))
        button(bar, "Delete ticked", self.delete_selected, width=120, danger=True).pack(side="left")
        self.suggest_btn = button(bar, "Suggest merges", self.start_suggest, width=140)
        self.suggest_btn.pack(side="left", padx=6)
        self.cast_info = ctk.CTkLabel(bar, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.cast_info.pack(side="right")

        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.list_frame = ctk.CTkScrollableFrame(body, width=330, fg_color=CARD, corner_radius=11,
                                                 border_width=1, border_color=BORDER)
        self.list_frame.pack(side="left", fill="y")
        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        top = ctk.CTkFrame(right, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(top, text="Name", width=50, anchor="w", text_color=TITLE).pack(side="left")
        name_entry = entry(top, self.name_var, width=260)
        name_entry.pack(side="left")
        name_entry.bind("<Return>", lambda _e: self.rename())
        name_entry.bind("<FocusOut>", lambda _e: self.rename())
        self.main_box = ctk.CTkCheckBox(top, text="Pre-mark as main (gets a reference)",
                                        variable=self.main_var, command=self.toggle_main,
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT)
        self.main_box.pack(side="left", padx=16)
        self.members_label = ctk.CTkLabel(right, text="", anchor="w", justify="left", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=11), wraplength=780)
        self.members_label.pack(fill="x", padx=14)
        note_row = ctk.CTkFrame(right, fg_color="transparent")
        note_row.pack(fill="x", padx=14, pady=(4, 4))
        ctk.CTkLabel(note_row, text="Note", width=50, anchor="w", text_color=TITLE).pack(side="left", anchor="n")
        self.note_box = ctk.CTkTextbox(note_row, height=46, fg_color=CARD, border_width=1,
                                       border_color=ENTRY_BORDER, text_color=ENTRY_TEXT)
        self.note_box.pack(side="left", fill="x", expand=True)
        self.note_box.bind("<FocusOut>", lambda _e: self.save_note())

        self.detail_frame = ctk.CTkScrollableFrame(right, fg_color=CARD)
        self.detail_frame.pack(fill="both", expand=True, padx=6, pady=4)
        add = ctk.CTkFrame(right, fg_color="transparent")
        add.pack(fill="x", padx=14, pady=(0, 12))
        add_entry = entry(add, self.new_detail_var, placeholder="Add a detail by hand (English)")
        add_entry.pack(side="left", fill="x", expand=True)
        add_entry.bind("<Return>", lambda _e: self.add_detail())
        button(add, "Add", self.add_detail, width=70).pack(side="left", padx=(8, 0))

    def _build_refs(self, tab):
        card = self._card(tab)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(row, text="Style image", width=90, anchor="w", text_color=TITLE).pack(side="left")
        button(row, "Browse", self.browse_style, width=80).pack(side="right")
        entry(row, self.style_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkLabel(row, text="Style note", width=90, anchor="w", text_color=TITLE).pack(side="left")
        note = entry(row, self.note_var)
        note.pack(side="left", fill="x", expand=True, padx=(0, 8))
        note.bind("<FocusOut>", lambda _e: self.save_style_note())
        ctk.CTkLabel(row, text="drawn in greyscale, always", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")

        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", fill="y")
        self.ref_kind_switch = ctk.CTkSegmentedButton(left, values=["Characters", "Places"],
                                                      command=self.switch_ref_kind, selected_color=ACCENT,
                                                      selected_hover_color=ACCENT_HOVER)
        self.ref_kind_switch.set("Characters")
        self.ref_kind_switch.pack(fill="x", pady=(0, 6))
        self.ref_list = ctk.CTkScrollableFrame(left, width=300, fg_color=CARD, corner_radius=11,
                                               border_width=1, border_color=BORDER)
        self.ref_list.pack(fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        self.ref_title = ctk.CTkLabel(head, text="Select a character or place", anchor="w",
                                      text_color=TITLE, font=ctk.CTkFont(size=14, weight="bold"))
        self.ref_title.pack(side="left")
        button(head, "Add variant", self.add_variant, width=110).pack(side="right")
        self.variant_bar = ctk.CTkFrame(right, fg_color="transparent")
        self.variant_bar.pack(fill="x", padx=14)
        self.prompt_box = ctk.CTkTextbox(right, height=104, fg_color=CARD, border_width=1,
                                         border_color=ENTRY_BORDER, text_color=ENTRY_TEXT,
                                         font=ctk.CTkFont(size=12))
        self.prompt_box.pack(fill="x", padx=14, pady=(6, 4))
        self.prompt_box.bind("<FocusOut>", lambda _e: self.save_prompt())
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 6))
        self.draw_btn = button(row, "Draw", self.start_draw, width=90, primary=True)
        self.draw_btn.pack(side="left")
        entry(row, self.takes_var, width=48).pack(side="left", padx=(8, 4))
        ctk.CTkLabel(row, text="takes", text_color=SUBTITLE).pack(side="left")
        button(row, "Rebuild prompt", self.rebuild_prompt, width=130).pack(side="left", padx=10)
        self.ref_info = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.ref_info.pack(side="left", padx=6)
        status = ctk.CTkFrame(right, fg_color="transparent")
        status.pack(fill="x", padx=14, pady=(0, 6))
        self.draw_status = ctk.CTkLabel(status, text="", anchor="w", text_color=ENTRY_TEXT,
                                        font=ctk.CTkFont(size=12))
        self.draw_status.pack(fill="x")
        self.draw_bar = ctk.CTkProgressBar(status, progress_color=ACCENT, height=8)
        self.draw_bar.set(0)
        self.draw_bar.pack(fill="x", pady=(4, 0))
        self.ref_gallery = Gallery(self, right, save=lambda: il.save_refs(self.root(), self.refs),
                                   refresh=self.render_ref_entry)

    def _build_scenes(self, tab):
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", fill="y")
        bar = ctk.CTkFrame(left, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 6))
        self.propose_btn = button(bar, "Propose scenes", self.start_propose, width=140, primary=True)
        self.propose_btn.pack(side="left")
        self.propose_all_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="whole book", variable=self.propose_all_var, fg_color=ACCENT,
                        hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT).pack(side="left", padx=8)
        self.chapter_list = ctk.CTkScrollableFrame(left, width=250, fg_color=CARD, corner_radius=11,
                                                   border_width=1, border_color=BORDER)
        self.chapter_list.pack(fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 2))
        self.scene_title = ctk.CTkLabel(head, text="Choose a chapter", anchor="w", text_color=TITLE,
                                        font=ctk.CTkFont(size=14, weight="bold"))
        self.scene_title.pack(side="left")
        button(head, "Cast & place", self.edit_scene_cast, width=110).pack(side="right")
        button(head, "Anchor", self.edit_scene_anchor, width=90).pack(side="right", padx=6)
        self.scene_bar = ctk.CTkFrame(right, fg_color="transparent")
        self.scene_bar.pack(fill="x", padx=14)
        self.scene_anchor = ctk.CTkLabel(right, text="", anchor="w", justify="left", wraplength=800,
                                         text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.scene_anchor.pack(fill="x", padx=14, pady=(4, 0))
        self.scene_cast = ctk.CTkLabel(right, text="", anchor="w", justify="left", wraplength=800,
                                       text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.scene_cast.pack(fill="x", padx=14)
        self.scene_prompt = ctk.CTkTextbox(right, height=92, fg_color=CARD, border_width=1,
                                           border_color=ENTRY_BORDER, text_color=ENTRY_TEXT,
                                           font=ctk.CTkFont(size=12))
        self.scene_prompt.pack(fill="x", padx=14, pady=(6, 4))
        self.scene_prompt.bind("<FocusOut>", lambda _e: self.save_scene_prompt())
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 4))
        self.scene_draw_btn = button(row, "Draw", self.start_draw_scene, width=90, primary=True)
        self.scene_draw_btn.pack(side="left")
        self.scene_takes_var = ctk.StringVar(value=str(self.settings["takes"]))
        entry(row, self.scene_takes_var, width=48).pack(side="left", padx=(8, 4))
        ctk.CTkLabel(row, text="takes", text_color=SUBTITLE).pack(side="left")
        self.scene_info = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.scene_info.pack(side="left", padx=10)
        status = ctk.CTkFrame(right, fg_color="transparent")
        status.pack(fill="x", padx=14, pady=(0, 4))
        self.scene_status = ctk.CTkLabel(status, text="", anchor="w", text_color=ENTRY_TEXT,
                                         font=ctk.CTkFont(size=12))
        self.scene_status.pack(fill="x")
        self.scene_bar_progress = ctk.CTkProgressBar(status, progress_color=ACCENT, height=8)
        self.scene_bar_progress.set(0)
        self.scene_bar_progress.pack(fill="x", pady=(4, 0))
        self.scene_gallery = Gallery(self, right, save=lambda: il.save_scenes(self.root(), self.scenes),
                                     refresh=self.render_scene, height=260)

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

    def remember(self):
        self.settings["last_text_folder"] = self.text_var.get().strip()
        self.settings["last_book"] = self.book_var.get().strip()
        il.save_settings(self.settings)

    def open_book(self):
        if not self.book_var.get().strip():
            return
        self.refresh_read_info()
        self.bible = il.load_bible(self.root())
        self.refs = il.load_refs(self.root())
        self.style_var.set(self.refs["style_image"])
        self.note_var.set(self.refs["style_note"])
        self.current = None
        self.ref_entry = None
        self.selected.clear()
        self.scenes = il.load_scenes(self.root())
        self.scene_chapter = ""
        self.scene_index = 0
        self.render_list()
        self.render_entry()
        self.render_ref_list()
        self.render_ref_entry()
        self.render_chapter_list()
        self.render_scene()

    def refresh_read_info(self):
        text = self.text_var.get().strip()
        if not os.path.isdir(text):
            self.read_info.configure(text="choose the chapter text folder", text_color=FAIL)
            return
        todo = il.all_pieces(text, int(self.settings["piece_chars"]))
        done = sum(os.path.isfile(il.piece_path(self.root(), p[0])) for p in todo)
        failed = sum(os.path.isfile(il.piece_path(self.root(), p[0], failed=True)) for p in todo)
        chapters = len(il.chapter_files(text))
        msg = f"{chapters} chapters, {len(todo)} pieces - {done} read"
        if failed:
            msg += f", {failed} failed (Read again retries them)"
        self.read_info.configure(text=msg, text_color=FAIL if failed else (DONE if done == len(todo) else SUBTITLE))
        self.progress.set(done / len(todo) if todo else 0)

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
        for b in (self.read_btn, self.suggest_btn, self.draw_btn, self.propose_btn, self.scene_draw_btn):
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

    def _drain(self):
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == "line":
                    self.log_line(value)
                    m = PIECE_RE.match(value)
                    s = SAMPLE_RE.match(value)
                    t = TAKE_RE.match(value)
                    p = PROGRESS_RE.match(value)
                    if m:
                        self.progress.set(int(m.group(1)) / int(m.group(2)))
                        self.status_var.set(f"reading {m.group(3)}")
                    elif t:
                        if self._starting:
                            self._starting = 0.0
                            self.draw_bar.stop()
                            self.draw_bar.configure(mode="determinate")
                        self._take = (int(t.group(1)), int(t.group(2)))
                        self._take_started = time.time()
                        self._step = (0, 0)
                        self._take_done = False
                        self.update_draw_status()
                    elif p:
                        self._step = (int(p.group(1)), int(p.group(2)))
                        self.update_draw_status()
                    elif s:
                        self.progress.set(int(s.group(1)) / int(s.group(2)))
                        self.status_var.set(f"drawing {s.group(1)}/{s.group(2)}")
                        self._drawn.append(s.group(3))
                        self._last_secs = float(s.group(4))
                        self._step = (0, 0)
                        self._take_done = True
                        self.update_draw_status(done=True)
                    elif value.startswith("SERVER"):
                        self.status_var.set(value.split(" ", 1)[1])
                        # only while starting: never overwrite "take N/N done"
                        if self._starting:
                            self.draw_widgets()[0].configure(text=value.split(" ", 1)[1], text_color=ENTRY_TEXT)
                    elif value.startswith("SUGGEST"):
                        self.status_var.set(value.split(" ", 1)[1])
                else:
                    code, on_done = value
                    self._proc = None
                    for b in (self.read_btn, self.suggest_btn, self.draw_btn, self.propose_btn,
                              self.scene_draw_btn):
                        b.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set("" if code == 0 else f"stopped (exit {code})")
                    on_done(code)
        except queue.Empty:
            pass
        if self._starting or (self._take and not self._take_done):
            self.update_draw_status()   # keep the elapsed seconds moving
        self.after(120, self._drain)

    def stop(self):
        if self._proc:
            # the tree, so llama-server goes too and VRAM comes back
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self._proc.pid)], capture_output=True)
            self.log_line("-- stopped by you")

    def start_read(self):
        text = self.text_var.get().strip()
        book = self.book_var.get().strip()
        if not os.path.isdir(text) or not il.chapter_files(text):
            messagebox.showerror("Scene Illustrator", "Choose a folder holding chapter_*.txt files.")
            return
        if not book:
            messagebox.showerror("Scene Illustrator", "Give the book a name.")
            return
        self.log_line(f"-- reading {book}")
        self._run_child(["read", "--book", book, "--text", text], self._read_done)

    def _read_done(self, _code):
        self.open_book()

    def start_suggest(self):
        if not self.bible:
            return
        self.tabs.set("1  Read")
        self.log_line("-- asking the model for merge suggestions")
        self._run_child(["suggest", "--book", self.book_var.get().strip()], self._suggest_done)

    def _suggest_done(self, code):
        path = os.path.join(self.root(), "suggestions.json")
        if code == 0 and os.path.isfile(path):
            self.tabs.set("2  Cast & places")
            SuggestionWindow(self, il.read_json(path))

    # ------------------------------------------------------------ the bible
    def save(self):
        il.save_bible(self.root(), self.bible)

    def switch_kind(self, value):
        self.kind = "characters" if value == "Characters" else "places"
        self.current = None
        self.selected.clear()
        self.render_list()
        self.render_entry()

    def render_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.bible:
            return
        entries = sorted(self.bible[self.kind], key=lambda e: (-len(e["chapters"]), min(e["chapters"] or [99])))
        for e in entries:
            row = ctk.CTkFrame(self.list_frame, fg_color=PASTEL_VIOLET if e["id"] == self.current else CARD,
                               corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            var = ctk.BooleanVar(value=e["id"] in self.selected)
            ctk.CTkCheckBox(row, text="", width=24, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            command=lambda i=e["id"], v=var: self.tick(i, v)).pack(side="left", padx=(6, 0))
            kept = sum(d["keep"] for d in e["details"])
            head = e["name"] + ("  ★" if e["main"] else "")
            label = ctk.CTkLabel(row, text=f"{head}\n{chapters_text(e['chapters'])} · {kept}/{len(e['details'])} details"
                                 + ("  · NEW" if any(d.get("new") for d in e["details"]) else ""),
                                 anchor="w", justify="left", text_color=TITLE, font=ctk.CTkFont(size=12))
            label.pack(side="left", fill="x", expand=True, pady=4)
            for w in (row, label):
                w.bind("<Button-1>", lambda _ev, i=e["id"]: self.select(i))
        chars, places = len(self.bible["characters"]), len(self.bible["places"])
        self.cast_info.configure(text=f"{chars} characters, {places} places · saved to bible.json")

    def tick(self, entry_id, var):
        (self.selected.add if var.get() else self.selected.discard)(entry_id)

    def select(self, entry_id):
        self.save_note()
        self.current = entry_id
        self.render_list()
        self.render_entry()

    def entry(self):
        if not self.bible or not self.current:
            return None
        return next((e for e in self.bible[self.kind] if e["id"] == self.current), None)

    def render_entry(self):
        for w in self.detail_frame.winfo_children():
            w.destroy()
        e = self.entry()
        self.note_box.delete("1.0", "end")
        if not e:
            self.name_var.set("")
            self.members_label.configure(text="Select an entry on the left.")
            return
        self.name_var.set(e["name"])
        self.main_var.set(e["main"])
        bits = [chapters_text(e["chapters"]), "from: " + " / ".join(e["members"])]
        if e.get("aliases"):
            bits.append("aliases: " + ", ".join(e["aliases"]))
        if e.get("roles"):
            more = f" (+{len(e['roles']) - 3} more)" if len(e["roles"]) > 3 else ""
            bits.append("role: " + "; ".join(e["roles"][:3]) + more)
        self.members_label.configure(text="   ·   ".join(bits))
        self.note_box.insert("1.0", e.get("note", ""))
        for d in sorted(e["details"], key=lambda d: (d["chapter"], d["piece"])):
            self._detail_row(e, d)

    def _detail_row(self, e, d):
        row = ctk.CTkFrame(self.detail_frame, fg_color=CARD if d["keep"] else PASTEL_GREY,
                           corner_radius=8, border_width=1, border_color=BORDER)
        row.pack(fill="x", pady=2, padx=2)
        var = ctk.BooleanVar(value=d["keep"])
        ctk.CTkCheckBox(row, text="", width=24, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=lambda: self.keep(d, var.get())).pack(side="left", padx=(8, 4), anchor="n", pady=8)
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True, pady=4)
        ctk.CTkLabel(col, text=d["text"] + ("   NEW" if d.get("new") else ""), anchor="w", justify="left",
                     wraplength=720, text_color=TITLE if d["keep"] else SUBTITLE,
                     font=ctk.CTkFont(size=13)).pack(fill="x")
        if d["match"] == "user":
            src, colour = "added by hand", SUBTITLE
        elif d["match"] == "none":
            src, colour = f"ch {d['chapter']} · no source sentence found" + (f" (model quoted: {d['quote']})" if d["quote"] else ""), FAIL
        else:
            src = f"ch {d['chapter']} · {d['source']}"
            colour = WARN if d["match"] == "fuzzy" else SUBTITLE
            if d["match"] == "fuzzy":
                src += "   (closest sentence - check it)"
        ctk.CTkLabel(col, text=src, anchor="w", justify="left", wraplength=720, text_color=colour,
                     font=ctk.CTkFont(size=12)).pack(fill="x")

    def keep(self, d, value):
        d["keep"] = value
        d.pop("new", None)
        self.save()
        self.render_list()
        self.render_entry()

    def rename(self):
        e = self.entry()
        name = self.name_var.get().strip()
        if e and name and name != e["name"]:
            e["name"] = name
            self.save()
            self.render_list()

    def toggle_main(self):
        e = self.entry()
        if e:
            e["main"] = self.main_var.get()
            self.save()
            self.render_list()

    def save_note(self):
        e = self.entry()
        if e:
            note = self.note_box.get("1.0", "end").strip()
            if note != e.get("note", ""):
                e["note"] = note
                self.save()

    def add_detail(self):
        e = self.entry()
        text = self.new_detail_var.get().strip()
        if e and text:
            il.add_detail(e, text)
            self.new_detail_var.set("")
            self.save()
            self.render_list()
            self.render_entry()

    def merge_selected(self, ids=None, name=None):
        ids = list(ids or self.selected)
        if len(ids) < 2:
            messagebox.showinfo("Merge", "Tick two or more entries to merge.")
            return
        entries = [il.find(self.bible, self.kind, i) for i in ids]
        if name is None:
            name = MergeDialog(self, [e["name"] for e in entries]).result
            if not name:
                return
        target = il.merge(self.bible, self.kind, ids, name)
        self.save()
        self.selected.clear()
        self.current = target["id"]
        self.render_list()
        self.render_entry()

    def delete_selected(self):
        ids = list(self.selected)
        if not ids:
            return
        names = ", ".join(il.find(self.bible, self.kind, i)["name"] for i in ids)
        if not messagebox.askyesno("Delete", f"Delete {names}?\n\nA re-read will not bring them back."):
            return
        for i in ids:
            il.delete(self.bible, self.kind, i)
        self.save()
        self.selected.clear()
        if self.current in ids:
            self.current = None
        self.render_list()
        self.render_entry()

    # ------------------------------------------------------------- scenes
    def entry_names(self):
        return {e["id"]: e["name"] for kind in il.KINDS for e in (self.bible[kind] if self.bible else [])}

    def render_chapter_list(self):
        for w in self.chapter_list.winfo_children():
            w.destroy()
        if not self.scenes or not os.path.isdir(self.text_var.get().strip()):
            return
        plan, _median = il.scene_plan(self.text_var.get().strip())
        for base in sorted(plan):
            chars, want = plan[base]
            record = self.scenes["chapters"].get(base)
            got = len(record["scenes"]) if record else 0
            chosen = sum(1 for s in record["scenes"] if s["chosen"]) if record else 0
            row = ctk.CTkFrame(self.chapter_list, fg_color=PASTEL_VIOLET if base == self.scene_chapter else CARD,
                               corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            if not record:
                state, colour = f"{want} scenes to propose", SUBTITLE
            elif chosen == got:
                state, colour = f"{got} scenes · all chosen", DONE
            else:
                state, colour = f"{got} scenes · {chosen} chosen", ENTRY_TEXT
            label = ctk.CTkLabel(row, text=f"{base.replace('chapter_', 'Chapter ')}   {chars:,} chars\n{state}",
                                 anchor="w", justify="left", text_color=colour, font=ctk.CTkFont(size=12))
            label.pack(side="left", fill="x", expand=True, padx=8, pady=5)
            for w in (row, label):
                w.bind("<Button-1>", lambda _e, b=base: self.select_chapter(b))

    def select_chapter(self, base):
        self.save_scene_prompt()
        self.scene_chapter = base
        self.scene_index = 0
        self.scene_gallery.picked = set()
        self.render_chapter_list()
        self.render_scene()

    def chapter_scenes(self):
        record = self.scenes["chapters"].get(self.scene_chapter) if self.scenes else None
        return record["scenes"] if record else []

    def current_scene(self):
        scenes = self.chapter_scenes()
        if not scenes:
            return None
        self.scene_index = min(self.scene_index, len(scenes) - 1)
        return scenes[self.scene_index]

    def render_scene(self):
        for w in self.scene_bar.winfo_children():
            w.destroy()
        self.scene_prompt.delete("1.0", "end")
        scene = self.current_scene()
        if not scene:
            self.scene_title.configure(text="Choose a chapter" if not self.scene_chapter
                                       else f"{self.scene_chapter}: no scenes yet - Propose scenes")
            self.scene_anchor.configure(text="")
            self.scene_cast.configure(text="")
            self.scene_info.configure(text="")
            self.scene_gallery.show(None)
            return
        scenes = self.chapter_scenes()
        self.scene_title.configure(text=f"{self.scene_chapter.replace('chapter_', 'Chapter ')}   "
                                        f"scene {self.scene_index + 1} of {len(scenes)}")
        for i, s in enumerate(scenes):
            button(self.scene_bar, f"#{i + 1}" + (" ✓" if s["chosen"] else ""),
                   lambda idx=i: self.select_scene(idx), width=0, height=28,
                   primary=(i == self.scene_index)).pack(side="left", padx=(0, 6), pady=4)
        where = f"{scene['position'] * 100:.0f}% into the chapter"
        note = "" if scene["anchor_match"] == "exact" else "   (closest sentence - check it)"
        self.scene_anchor.configure(text=f"starts at {where}:  {scene['anchor']}{note}",
                                    text_color=SUBTITLE if scene["anchor_match"] == "exact" else WARN)
        names = self.entry_names()
        cast = ", ".join(names.get(c, c) for c in scene["cast"]) or "nobody"
        place = names.get(scene["place"], scene["place"]) or "no place"
        refs = il.scene_references(self.refs, scene) if self.refs else []
        have = len(refs) + (1 if os.path.isfile(il.style_image_path(self.root())) else 0)
        self.scene_cast.configure(text=f"cast: {cast}   ·   place: {place}   ·   {have} reference(s) attached"
                                       + ("  - over the limit of 5, extras are dropped" if have > 5 else ""),
                                  text_color=WARN if have > 5 else SUBTITLE)
        self.scene_prompt.insert("1.0", scene["prompt"])
        self.scene_info.configure(text=scene["seen"][:110])
        self.scene_gallery.show(scene)

    def select_scene(self, index):
        self.save_scene_prompt()
        self.scene_index = index
        self.scene_gallery.picked = set()
        self.render_scene()

    def save_scene_prompt(self):
        scene = self.current_scene() if self.scenes and self.scene_chapter else None
        if scene:
            text = self.scene_prompt.get("1.0", "end").strip()
            if text and text != scene["prompt"]:
                scene["prompt"] = text
                il.save_scenes(self.root(), self.scenes)

    def edit_scene_cast(self):
        scene = self.current_scene()
        if not scene or not self.bible:
            return
        dialog = CastDialog(self, self.bible, self.refs, scene)
        if dialog.result is not None:
            scene["cast"], scene["place"] = dialog.result
            il.save_scenes(self.root(), self.scenes)
            self.render_scene()

    def edit_scene_anchor(self):
        scene = self.current_scene()
        if not scene:
            return
        path = os.path.join(self.text_var.get().strip(), self.scene_chapter + ".txt")
        if not os.path.isfile(path):
            return
        text = il.read_text(path)
        picked = AnchorDialog(self, il.sentences(text), scene["anchor"]).result
        if picked:
            scene["anchor"] = picked
            scene["anchor_match"] = "exact"
            scene["position"] = il.position_of(picked, text)
            self.chapter_scenes().sort(key=lambda s: s["position"])
            self.scene_index = self.chapter_scenes().index(scene)
            il.save_scenes(self.root(), self.scenes)
            self.render_scene()

    def start_propose(self):
        text = self.text_var.get().strip()
        if not self.bible or not os.path.isdir(text):
            return
        self.save_scene_prompt()
        whole = self.propose_all_var.get() or not self.scene_chapter
        target = "" if whole else self.scene_chapter
        if self.scenes and (self.scenes["chapters"] if whole else self.scene_chapter in self.scenes["chapters"]):
            what = "every chapter" if whole else self.scene_chapter
            if not messagebox.askyesno("Propose scenes",
                                       f"Replace the scenes of {what}?\n\nPrompts you edited and takes you "
                                       "drew for them are kept on disk but no longer listed."):
                return
        self.log_line(f"-- proposing scenes for {'the whole book' if whole else self.scene_chapter}")
        args = ["propose", "--book", self.book_var.get().strip(), "--text", text]
        if target:
            args += ["--chapter", target]
        self._run_child(args, self._propose_done)

    def _propose_done(self, _code):
        self.scenes = il.load_scenes(self.root())
        self.render_chapter_list()
        self.render_scene()

    def start_draw_scene(self):
        scene = self.current_scene()
        if not scene:
            return
        self.save_scene_prompt()
        if not scene["prompt"].strip():
            messagebox.showerror("Draw", "This scene has no prompt.")
            return
        try:
            count = max(1, min(8, int(self.scene_takes_var.get())))
        except ValueError:
            count = int(self.settings["takes"])
        self.scene_takes_var.set(str(count))
        self._drawn = []
        self._draw_target = ("scene", self.scene_chapter, self.scene_index)
        self._draw_where = "scene"
        self._take = None
        self._take_done = False
        self._step = (0, 0)
        self._starting = time.time()
        self.scene_status.configure(text=f"starting to draw {count} take(s)…", text_color=ENTRY_TEXT)
        self.scene_bar_progress.configure(mode="indeterminate")
        self.scene_bar_progress.set(0)
        self.scene_bar_progress.start()
        self.update_idletasks()
        self.log_line(f"-- drawing {count} for {self.scene_chapter} scene {self.scene_index + 1}")
        self._run_child(["drawscene", "--book", self.book_var.get().strip(), "--chapter", self.scene_chapter,
                         "--scene", str(self.scene_index), "--count", str(count)], self._draw_scene_done)

    def _draw_scene_done(self, code):
        if self._drawn and self._draw_target and self._draw_target[0] == "scene":
            _, base, index = self._draw_target
            scenes = self.scenes["chapters"].get(base, {}).get("scenes", [])
            if index < len(scenes):
                scenes[index]["samples"] += self._drawn
                il.save_scenes(self.root(), self.scenes)
        self._drawn = []
        self._draw_target = None
        self._take = None
        self._starting = 0.0
        self.scene_bar_progress.stop()
        self.scene_bar_progress.configure(mode="determinate")
        if code != 0:
            self.scene_status.configure(text="stopped", text_color=WARN)
            self.scene_bar_progress.set(0)
        self.render_chapter_list()
        self.render_scene()

    # -------------------------------------------------------- references
    def browse_style(self):
        path = filedialog.askopenfilename(title="Style image for this book",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        dst = il.style_image_path(self.root())
        comfy.copy_style_image(path, dst)
        self.refs["style_image"] = os.path.abspath(path)
        il.save_refs(self.root(), self.refs)
        self.style_var.set(self.refs["style_image"])
        self.render_ref_entry()

    def save_style_note(self):
        if self.refs and self.note_var.get().strip() and self.note_var.get() != self.refs["style_note"]:
            self.refs["style_note"] = self.note_var.get().strip()
            il.save_refs(self.root(), self.refs)

    def switch_ref_kind(self, value):
        self.ref_kind = "characters" if value == "Characters" else "places"
        self.ref_entry = None
        self.ref_variant = 0
        self.render_ref_list()
        self.render_ref_entry()

    def ref_entries(self):
        if not self.bible:
            return []
        entries = self.bible[self.ref_kind]
        return sorted(entries, key=lambda e: (not e["main"], -len(e["chapters"]), min(e["chapters"] or [99])))

    def render_ref_list(self):
        for w in self.ref_list.winfo_children():
            w.destroy()
        if not self.refs:
            return
        for e in self.ref_entries():
            variants = il.variants_of(self.refs, self.ref_kind, e["id"])
            chosen = sum(1 for v in variants if v["chosen"])
            row = ctk.CTkFrame(self.ref_list, fg_color=PASTEL_VIOLET if e["id"] == self.ref_entry else CARD,
                               corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            state = f"{chosen} chosen" if chosen else (f"{len(variants)} variant(s)" if variants else "no reference")
            label = ctk.CTkLabel(row, text=f"{e['name']}{'  ★' if e['main'] else ''}\n{chapters_text(e['chapters'])} · {state}",
                                 anchor="w", justify="left", text_color=TITLE if chosen else ENTRY_TEXT,
                                 font=ctk.CTkFont(size=12))
            label.pack(side="left", fill="x", expand=True, padx=8, pady=5)
            if chosen:
                ctk.CTkLabel(row, text="✓", text_color=DONE, width=20,
                             font=ctk.CTkFont(size=14, weight="bold")).pack(side="right", padx=6)
            for w in (row, label):
                w.bind("<Button-1>", lambda _ev, i=e["id"]: self.select_ref(i))

    def select_ref(self, entry_id):
        self.save_prompt()
        self.ref_entry = entry_id
        self.ref_variant = 0
        self.ref_gallery.picked = set()
        self.render_ref_list()
        self.render_ref_entry()

    def ref_entry_obj(self):
        if not self.bible or not self.ref_entry:
            return None
        return next((e for e in self.bible[self.ref_kind] if e["id"] == self.ref_entry), None)

    def current_variant(self):
        e = self.ref_entry_obj()
        if not e:
            return None
        variants = il.variants_of(self.refs, self.ref_kind, e["id"])
        if not variants:
            il.add_variant(self.refs, self.ref_kind, e, "", self.refs["style_note"])
            il.save_refs(self.root(), self.refs)
            variants = il.variants_of(self.refs, self.ref_kind, e["id"])
        self.ref_variant = min(self.ref_variant, len(variants) - 1)
        return variants[self.ref_variant]

    def render_ref_entry(self):
        for w in self.variant_bar.winfo_children():
            w.destroy()
        self.prompt_box.delete("1.0", "end")
        e = self.ref_entry_obj()
        if not e:
            self.ref_title.configure(text="Select a character or place")
            self.ref_info.configure(text="")
            self.ref_gallery.show(None)
            return
        kept = sum(d["keep"] for d in e["details"])
        self.ref_title.configure(text=f"{e['name']}   ({kept} details kept)")
        variants = il.variants_of(self.refs, self.ref_kind, e["id"])
        v = self.current_variant()
        for i, var in enumerate(variants):
            label = var["label"] or "default"
            b = button(self.variant_bar, label + (" ✓" if var["chosen"] else ""),
                       lambda idx=i: self.select_variant(idx), width=0,
                       primary=(i == self.ref_variant), height=28)
            b.pack(side="left", padx=(0, 6), pady=4)
        self.prompt_box.insert("1.0", v["prompt"])
        style_set = os.path.isfile(il.style_image_path(self.root()))
        self.ref_info.configure(text="" if style_set else "no style image set",
                                text_color=SUBTITLE if style_set else WARN)
        self.ref_gallery.show(v)

    # the References gallery, under the names the buttons and tests use
    def pick_sample(self, path, picked):
        self.ref_gallery.pick(path, picked)

    def select_all_samples(self):
        self.ref_gallery.select_all()

    def select_no_samples(self):
        self.ref_gallery.select_none()

    def delete_picked_samples(self):
        self.ref_gallery.delete_picked()

    def choose_sample(self, _variant, path):
        self.ref_gallery.choose(path)

    def delete_sample(self, _variant, path, refresh=True):
        self.ref_gallery.delete(path, refresh=refresh)

    def select_variant(self, index):
        self.save_prompt()
        self.ref_variant = index
        self.ref_gallery.picked = set()
        self.render_ref_entry()

    def add_variant(self):
        e = self.ref_entry_obj()
        if not e:
            return
        label = VariantDialog(self).result
        if not label:
            return
        self.save_prompt()
        il.add_variant(self.refs, self.ref_kind, e, label, self.refs["style_note"])
        il.save_refs(self.root(), self.refs)
        self.ref_variant = len(il.variants_of(self.refs, self.ref_kind, e["id"])) - 1
        self.render_ref_list()
        self.render_ref_entry()

    def save_prompt(self):
        if not self.refs or not self.ref_entry:
            return
        variants = il.variants_of(self.refs, self.ref_kind, self.ref_entry)
        if self.ref_variant < len(variants):
            text = self.prompt_box.get("1.0", "end").strip()
            if text and text != variants[self.ref_variant]["prompt"]:
                variants[self.ref_variant]["prompt"] = text
                il.save_refs(self.root(), self.refs)

    def rebuild_prompt(self):
        e = self.ref_entry_obj()
        v = self.current_variant()
        if not e or not v:
            return
        v["prompt"] = il.build_prompt(e, self.ref_kind, self.refs["style_note"], v["label"])
        il.save_refs(self.root(), self.refs)
        self.render_ref_entry()

    def start_draw(self):
        e = self.ref_entry_obj()
        v = self.current_variant()
        if not e or not v:
            return
        self.save_prompt()
        try:
            count = max(1, min(8, int(self.takes_var.get())))
        except ValueError:
            count = int(self.settings["takes"])
        self.takes_var.set(str(count))
        self.settings["takes"] = count
        self._drawn = []
        # remember WHICH variant this job is for: the selection may change
        # while it draws, and the takes belong to the entry that asked
        self._draw_target = (self.ref_kind, e["id"], self.ref_variant)
        self._draw_where = "refs"
        # answer the click NOW: the engine may take half a minute to start,
        # and a dead bar reads as "did my click land?" (user report 09-20)
        self._take = None
        self._take_done = False
        self._step = (0, 0)
        self._starting = time.time()
        self.draw_status.configure(text=f"starting to draw {count} take(s)…", text_color=ENTRY_TEXT)
        self.draw_bar.configure(mode="indeterminate")
        self.draw_bar.set(0)
        self.draw_bar.start()
        self.update_idletasks()
        self.log_line(f"-- drawing {count} for {e['name']} ({v['label'] or 'default'})")
        self._run_child(["draw", "--book", self.book_var.get().strip(), "--kind", self.ref_kind,
                         "--id", e["id"], "--variant", str(self.ref_variant), "--count", str(count)],
                        self._draw_done)

    def draw_widgets(self):
        """Whichever tab asked for this draw owns the status line and bar."""
        if self._draw_where == "scene":
            return self.scene_status, self.scene_bar_progress
        return self.draw_status, self.draw_bar

    def update_draw_status(self, done=False):
        """Takes done + the sampler's own step, so the bar moves inside a take.
        Loading the text encoder reports nothing, so that stretch shows as
        'preparing' with the elapsed seconds instead of a fake percentage."""
        status, bar = self.draw_widgets()
        if not self._take:
            if self._starting:      # engine starting: keep the bar alive
                status.configure(text=f"starting to draw…   ·   {time.time() - self._starting:.0f}s",
                                 text_color=ENTRY_TEXT)
            else:
                status.configure(text="")
                bar.set(0)
            return
        i, n = self._take
        step, steps = self._step
        elapsed = time.time() - self._take_started
        share = (step / steps) if steps else 0.0
        fraction = ((i - 1) + (1.0 if done else share)) / n
        bar.set(fraction)
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
        status.configure(text=text, text_color=DONE if done and i == n else ENTRY_TEXT)

    def _draw_done(self, _code):
        if self._drawn and self._draw_target:
            kind, entry_id, index = self._draw_target
            variants = il.variants_of(self.refs, kind, entry_id)
            if index < len(variants):
                variants[index]["samples"] += self._drawn
                il.save_refs(self.root(), self.refs)
        self._drawn = []
        self._draw_target = None
        self._take = None
        self._starting = 0.0
        self.draw_bar.stop()
        self.draw_bar.configure(mode="determinate")
        if _code != 0:
            self.draw_status.configure(text="stopped", text_color=WARN)
            self.draw_bar.set(0)
        self.render_ref_list()
        self.render_ref_entry()

    def on_close(self):
        self.save_note()
        self.save_scene_prompt()
        if self._proc:
            if not messagebox.askyesno("Scene Illustrator", "A run is in progress. Stop it and close?"):
                return
            self.stop()
        self.remember()
        self.destroy()


class MergeDialog(ctk.CTkToplevel):
    """Pick (or type) the name the merged entry keeps."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.title("Merge")
        self.configure(fg_color=BG)
        self.result = None
        self.var = ctk.StringVar(value=names[0])
        ctk.CTkLabel(self, text="Name for the merged entry:", text_color=TITLE).pack(padx=20, pady=(16, 6), anchor="w")
        for n in names:
            ctk.CTkRadioButton(self, text=n, variable=self.var, value=n, fg_color=ACCENT).pack(padx=24, pady=2, anchor="w")
        entry(self, self.var, width=320).pack(padx=20, pady=8)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(4, 16))
        button(row, "Merge", self.ok, primary=True).pack(side="left", padx=6)
        button(row, "Cancel", self.destroy).pack(side="left")
        bring_to_front(self)
        self.grab_set()
        self.wait_window()

    def ok(self):
        self.result = self.var.get().strip()
        self.destroy()


def bring_to_front(window):
    """CustomTkinter finishes placing a Toplevel a moment after it is made,
    which leaves it BEHIND the main window (user report 2026-09-20). Lift it
    again once that is done."""
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


class ImageViewer(ctk.CTkToplevel):
    """One take, big: Fit / 1:1 / zoom buttons, the mouse wheel to zoom and
    drag to pan, so a face or a hand can actually be judged."""

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
        """Next zoom strictly above / below the current one. Comparing by
        index broke after 1:1, where the list held 1.0 twice and 'next'
        landed on the same value (user report 2026-09-20)."""
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
        """The image is drawn INTO the canvas, not placed as a widget on top
        of it: a widget swallowed the mouse, so drag-to-pan and wheel zoom
        never reached the canvas (user report 2026-09-20)."""
        z = self.current_zoom()
        w, h = max(int(self.image.width * z), 1), max(int(self.image.height * z), 1)
        resample = Image.LANCZOS if z <= 1 else Image.NEAREST
        self._photo = ImageTk.PhotoImage(self.image.resize((w, h), resample))
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_image(max((cw - w) // 2, 0), max((ch - h) // 2, 0), anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, max(w, cw), max(h, ch)))
        self.canvas.configure(cursor="fleur" if (w > cw or h > ch) else "")
        self.zoom_label.configure(text=f"{z * 100:.0f}%" + ("  (fit)" if self.zoom is None else ""))


class CastDialog(ctk.CTkToplevel):
    """Who is in this picture, and where. Entries with a chosen reference are
    listed first and marked, because those are the ones that stay consistent."""

    def __init__(self, parent, bible, refs, scene):
        super().__init__(parent)
        self.title("Cast & place")
        self.geometry("720x700")
        self.configure(fg_color=BG)
        self.result = None
        self.vars = {}
        self.place_var = ctk.StringVar(value=scene.get("place") or "")

        ctk.CTkLabel(self, text="In the picture", text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=16, pady=(14, 2), anchor="w")
        frame = ctk.CTkScrollableFrame(self, fg_color=CARD, height=300)
        frame.pack(fill="both", expand=True, padx=16)
        for e in self.order(bible["characters"], refs, "characters"):
            has = self.has_reference(refs, "characters", e["id"])
            var = ctk.BooleanVar(value=e["id"] in (scene.get("cast") or []))
            self.vars[e["id"]] = var
            ctk.CTkCheckBox(frame, text=f"{e['name']}   {'✓ reference' if has else '· no reference'}",
                            variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            text_color=TITLE if has else SUBTITLE).pack(anchor="w", padx=10, pady=3)

        ctk.CTkLabel(self, text="Place", text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=16, pady=(12, 2), anchor="w")
        places = ctk.CTkScrollableFrame(self, fg_color=CARD, height=200)
        places.pack(fill="both", expand=True, padx=16)
        ctk.CTkRadioButton(places, text="no place", variable=self.place_var, value="",
                           fg_color=ACCENT, text_color=SUBTITLE).pack(anchor="w", padx=10, pady=3)
        for e in self.order(bible["places"], refs, "places"):
            has = self.has_reference(refs, "places", e["id"])
            ctk.CTkRadioButton(places, text=f"{e['name']}   {'✓ reference' if has else '· no reference'}",
                               variable=self.place_var, value=e["id"], fg_color=ACCENT,
                               text_color=TITLE if has else SUBTITLE).pack(anchor="w", padx=10, pady=3)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=12)
        button(row, "Save", self.ok, primary=True).pack(side="left", padx=6)
        button(row, "Cancel", self.destroy).pack(side="left")
        bring_to_front(self)
        self.grab_set()
        self.wait_window()

    @staticmethod
    def has_reference(refs, kind, entry_id):
        if not refs:
            return False
        return any(v.get("chosen") for v in refs.get(kind, {}).get(entry_id, {}).get("variants", []))

    def order(self, entries, refs, kind):
        return sorted(entries, key=lambda e: (not self.has_reference(refs, kind, e["id"]),
                                              -len(e["chapters"])))

    def ok(self):
        self.result = ([i for i, v in self.vars.items() if v.get()], self.place_var.get())
        self.destroy()


class AnchorDialog(ctk.CTkToplevel):
    """Move a scene to another sentence of the chapter. The image will appear
    when that sentence is read (Stage D)."""

    def __init__(self, parent, sentences, current):
        super().__init__(parent)
        self.title("Anchor sentence")
        self.geometry("900x700")
        self.configure(fg_color=BG)
        self.result = None
        self.sentences = sentences
        self.filter_var = ctk.StringVar()
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(row, text="Filter", width=50, anchor="w", text_color=TITLE).pack(side="left")
        field = entry(row, self.filter_var)
        field.pack(side="left", fill="x", expand=True)
        self.filter_var.trace_add("write", lambda *_: self.render())
        self.frame = ctk.CTkScrollableFrame(self, fg_color=CARD)
        self.frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.current = current
        self.render()
        bring_to_front(self)
        self.grab_set()
        self.wait_window()

    def render(self):
        for w in self.frame.winfo_children():
            w.destroy()
        needle = self.filter_var.get().strip()
        shown = 0
        for i, s in enumerate(self.sentences, 1):
            if needle and needle not in s:
                continue
            shown += 1
            if shown > 400:
                ctk.CTkLabel(self.frame, text="…narrow the filter to see more", text_color=SUBTITLE).pack(anchor="w")
                break
            row = ctk.CTkFrame(self.frame, fg_color=PASTEL_VIOLET if s == self.current else CARD,
                               corner_radius=6)
            row.pack(fill="x", pady=1)
            label = ctk.CTkLabel(row, text=f"{i:>4}  {s}", anchor="w", justify="left", wraplength=800,
                                 text_color=TITLE, font=ctk.CTkFont(size=12), cursor="hand2")
            label.pack(fill="x", padx=8, pady=3)
            for w in (row, label):
                w.bind("<Button-1>", lambda _e, text=s: self.pick(text))

    def pick(self, text):
        self.result = text
        self.destroy()


class VariantDialog(ctk.CTkToplevel):
    """A second reference for the same character: a label such as
    'asleep in blue pyjamas' or 'wearing the knit cap'."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Add variant")
        self.configure(fg_color=BG)
        self.result = None
        self.var = ctk.StringVar()
        ctk.CTkLabel(self, text="What is different in this variant?", text_color=TITLE).pack(
            padx=20, pady=(16, 4), anchor="w")
        ctk.CTkLabel(self, text="e.g. asleep in blue pyjamas · in a green tracksuit, bruised face",
                     text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(padx=20, anchor="w")
        field = entry(self, self.var, width=420)
        field.pack(padx=20, pady=10)
        field.bind("<Return>", lambda _e: self.ok())
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(0, 16))
        button(row, "Add", self.ok, primary=True).pack(side="left", padx=6)
        button(row, "Cancel", self.destroy).pack(side="left")
        bring_to_front(self)
        self.grab_set()
        field.focus_set()
        self.wait_window()

    def ok(self):
        self.result = self.var.get().strip()
        self.destroy()


class SuggestionWindow(ctk.CTkToplevel):
    """The model's merge ideas, accepted or dismissed one by one. Never
    applied automatically (measured 2026-09-18: it merged Mari with Eri)."""

    def __init__(self, app, suggestions):
        super().__init__(app)
        self.app = app
        self.title("Suggested merges")
        self.geometry("760x560")
        self.configure(fg_color=BG)
        frame = ctk.CTkScrollableFrame(self, fg_color=BG)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        any_ = False
        for kind in il.KINDS:
            for g in suggestions.get(kind, []):
                live = [i for i in g["ids"] if any(e["id"] == i for e in app.bible[kind])]
                if len(live) < 2:
                    continue
                any_ = True
                names = " + ".join(il.find(app.bible, kind, i)["name"] for i in live)
                card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=8, border_width=1, border_color=BORDER)
                card.pack(fill="x", pady=4)
                ctk.CTkLabel(card, text=f"{kind[:-1]}: {names}  ->  {g['name']}", anchor="w", wraplength=680,
                             justify="left", text_color=TITLE, font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=10, pady=(8, 0))
                if g.get("why"):
                    ctk.CTkLabel(card, text=g["why"], anchor="w", wraplength=680, justify="left",
                                 text_color=SUBTITLE).pack(fill="x", padx=10)
                row = ctk.CTkFrame(card, fg_color="transparent")
                row.pack(fill="x", padx=10, pady=8)
                button(row, "Accept", lambda k=kind, ids=live, n=g["name"], c=card: self.accept(k, ids, n, c),
                       primary=True, width=90).pack(side="left")
                button(row, "Dismiss", card.destroy, width=90).pack(side="left", padx=6)
        if not any_:
            ctk.CTkLabel(frame, text="No suggestions.", text_color=SUBTITLE).pack(pady=20)
        bring_to_front(self)

    def accept(self, kind, ids, name, card):
        # an earlier accept may already have merged some of these away
        ids = [i for i in ids if any(e["id"] == i for e in self.app.bible[kind])]
        if len(ids) < 2:
            card.destroy()
            return
        if self.app.kind != kind:
            self.app.kind_switch.set("Characters" if kind == "characters" else "Places")
            self.app.switch_kind(self.app.kind_switch.get())
        self.app.merge_selected(ids=ids, name=name)
        card.destroy()


if __name__ == "__main__":
    App().mainloop()
