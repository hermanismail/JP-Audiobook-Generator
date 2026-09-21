"""
app.py
------
The Book Profiler window (2026-09-18): pick a book, a seiyuu and a folder,
press Run, walk away. Everything under the window is `profiler_runner.py`,
and everything under that is the same CLI scripts a developer runs.

Two tabs:

    Profile     1 book folder        parsed on choice, green or red
                2 seiyuu             one for every chapter, or Customize:
                                     tick chapters and pair each with a seiyuu
                3 profile folder     the root everything lands under
                4 progress           one row per ticked chapter, from disk
                5 results            each seiyuu's profiles - listening test,
                                     copy path, open folder
    Clean up    delete the measurement audio of a finished seiyuu, keeping
                every number the recipe was built from

Only the essentials are asked (decision 2026-09-18). The measured parameters
are fixed; a developer changes them in settings.json and runs the CLI.

Run it with:   uv run python app.py
"""

import os
import queue
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

import profiler_runner as pr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The generator's palette, as the other tools use it.
BG = "#F7F7FA"
CARD = "#FFFFFF"
BORDER = "#E7E7EC"
TITLE = "#17171C"
SUBTITLE = "#8B8B94"
ENTRY_BORDER = "#E2E2E8"
ENTRY_TEXT = "#3A3A42"
ACCENT = "#6C5DD3"
ACCENT_HOVER = "#5B4FC0"
DONE = "#1E8B4E"
WARN = "#B7791F"
FAIL = "#C4453C"
DISABLED = "#B8B8C0"
NEUTRAL_BORDER = "#D8D8DE"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"

STATE_COLORS = {"none": SUBTITLE, "partial": WARN, "swept": WARN, "running": ACCENT,
                "scored": DONE, "cleaned": DONE, "failed": FAIL}
ASSIGN_LABELS = {"all": "One seiyuu for all chapters", "custom": "Customize"}
ASSIGN_KEYS = {v: k for k, v in ASSIGN_LABELS.items()}
TAB_LABELS = ("Profile", "Clean up")


def button(parent, text, command, width=96, primary=False, danger=False, **kw):
    if primary:
        return ctk.CTkButton(parent, text=text, width=width, height=34, corner_radius=8,
                             fg_color=ACCENT, hover_color=ACCENT_HOVER, command=command,
                             font=ctk.CTkFont(size=13, weight="bold"), **kw)
    return ctk.CTkButton(parent, text=text, width=width, height=34, corner_radius=8,
                         fg_color=CARD, hover_color=BG, border_width=1,
                         border_color=NEUTRAL_BORDER,
                         text_color=FAIL if danger else ENTRY_TEXT, command=command, **kw)


def entry(parent, variable, placeholder=""):
    return ctk.CTkEntry(parent, textvariable=variable, height=34, corner_radius=8,
                        border_width=1, border_color=ENTRY_BORDER, fg_color=CARD,
                        text_color=ENTRY_TEXT, placeholder_text=placeholder)


def human_bytes(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


# ------------------------------------------------------------ seiyuu picker

class SeiyuuPicker(ctk.CTkFrame):
    """Dropdown over seiyuu/list plus Browse for anything else. `on_change`
    gets the chosen speaker path."""

    def __init__(self, parent, speakers, path, on_change, compact=False):
        super().__init__(parent, fg_color="transparent")
        self.speakers = speakers
        self.on_change = on_change
        self.path = path or ""
        self.choice = ctk.StringVar(value="")
        names = [n for n, _p in speakers] or ["(none found)"]
        self.menu = ctk.CTkOptionMenu(
            self, values=names, variable=self.choice, width=220 if compact else 250,
            height=32 if compact else 34, corner_radius=8, fg_color=CARD, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT, dropdown_fg_color=CARD,
            command=self._chosen)
        self.menu.pack(side="left", padx=(0, 8))
        self.browse = button(self, "Browse…", self._browse, width=90)
        self.browse.pack(side="left")
        self.path_label = ctk.CTkLabel(self, text="", text_color=SUBTITLE, anchor="w",
                                       font=ctk.CTkFont(size=11))
        self.path_label.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self._show()

    def _show(self):
        match = next((n for n, p in self.speakers
                      if os.path.normcase(p) == os.path.normcase(self.path)), None)
        self.choice.set(match or (pr.nickname(self.path) if self.path else "choose a seiyuu"))
        if self.path and not os.path.isfile(self.path):
            self.path_label.configure(text="file not found: " + self.path, text_color=FAIL)
        else:
            self.path_label.configure(text="" if match or not self.path else self.path,
                                      text_color=SUBTITLE)

    def set_path(self, path):
        self.path = path or ""
        self._show()

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.menu.configure(state=state)
        self.browse.configure(state=state, text_color=ENTRY_TEXT if enabled else DISABLED)

    def _chosen(self, name):
        path = next((p for n, p in self.speakers if n == name), None)
        if path:
            self.path = path
            self._show()
            self.on_change(path)

    def _browse(self):
        start = os.path.dirname(self.path) if self.path else None
        chosen = filedialog.askopenfilename(
            title="Pick a speaker file", initialdir=start if start and os.path.isdir(start) else None,
            filetypes=[("Speaker", "*.safetensors"), ("All files", "*.*")])
        if chosen:
            self.path = os.path.normpath(chosen)
            self._show()
            self.on_change(self.path)


# ------------------------------------------------------------ customize

class CustomizeWindow(ctk.CTkToplevel):
    """One row per chapter: tick it to profile it, pair it with a seiyuu.
    Unticked rows are greyed and skipped. A newly ticked row with no seiyuu
    takes the last one assigned - the main picker's, until a row is given
    one of its own (the same rule as the generator's Customize window)."""

    def __init__(self, master, chapters, plan, speakers, default_path, on_change):
        super().__init__(master)
        self.title("Customize Chapters")
        self.configure(fg_color=BG)
        self.geometry("820x640")
        self.minsize(640, 400)
        self.plan, self.on_change = plan, on_change
        self.last_path = default_path
        self.rows = {}

        head = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        head.pack(fill="x")
        ctk.CTkLabel(head, text="Customize chapters", text_color=TITLE,
                     font=ctk.CTkFont(size=17, weight="bold")).pack(side="left", padx=18, pady=12)
        for text, value in (("Untick all", False), ("Tick all", True)):
            button(head, text, lambda v=value: self._tick_all(v), width=90).pack(
                side="right", padx=(0, 10), pady=12)
        self.count_label = ctk.CTkLabel(head, text="", text_color=SUBTITLE,
                                        font=ctk.CTkFont(size=12))
        self.count_label.pack(side="right", padx=14)

        body = ctk.CTkScrollableFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        for base in chapters:
            self._add_row(body, base, speakers)

        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        button(bar, "Done", self.destroy, width=110, primary=True).pack(side="right", padx=18,
                                                                         pady=11)
        ctk.CTkLabel(bar, text="Unticked chapters are skipped. Each ticked chapter is measured "
                               "with its own seiyuu.",
                     text_color=SUBTITLE, font=ctk.CTkFont(size=11)).pack(side="left", padx=18)
        self._update_count()
        self.after(60, lambda: (self.lift(), self.focus_force()))
        # CTkToplevel sets its own icon ~200 ms after creation; go after it.
        icon = os.path.join(SCRIPT_DIR, "profiler_icon.ico")
        if os.path.isfile(icon):
            self.after(300, lambda: self.iconbitmap(icon))

    def _add_row(self, parent, base, speakers):
        item = self.plan.setdefault(base, {"enabled": False, "speaker_path": ""})
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, border_width=1,
                            border_color=BORDER)
        card.pack(fill="x", pady=4)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)
        var = ctk.IntVar(value=1 if item.get("enabled") else 0)
        ctk.CTkCheckBox(inner, text="", variable=var, width=24, fg_color=ACCENT,
                        hover_color=ACCENT_HOVER,
                        command=lambda b=base: self._toggled(b)).pack(side="left")
        label = ctk.CTkLabel(inner, text=base.replace("chapter_", "Chapter "), width=110,
                             anchor="w", font=ctk.CTkFont(size=13, weight="bold"))
        label.pack(side="left", padx=(4, 10))
        picker = SeiyuuPicker(inner, speakers, item.get("speaker_path", ""),
                              lambda path, b=base: self._assigned(b, path), compact=True)
        picker.pack(side="left", fill="x", expand=True)
        self.rows[base] = {"var": var, "label": label, "picker": picker}
        self._apply_enabled(base)

    def _apply_enabled(self, base):
        row = self.rows[base]
        enabled = bool(row["var"].get())
        row["label"].configure(text_color=TITLE if enabled else DISABLED)
        row["picker"].set_enabled(enabled)

    def _toggled(self, base):
        row = self.rows[base]
        enabled = bool(row["var"].get())
        self.plan[base]["enabled"] = enabled
        if enabled and not self.plan[base].get("speaker_path") and self.last_path:
            self.plan[base]["speaker_path"] = self.last_path
            row["picker"].set_path(self.last_path)
        self._apply_enabled(base)
        self._update_count()

    def _assigned(self, base, path):
        self.plan[base]["speaker_path"] = path
        self.last_path = path
        self._update_count()

    def _tick_all(self, value):
        for base, row in self.rows.items():
            if bool(row["var"].get()) != value:
                row["var"].set(1 if value else 0)
                self._toggled(base)

    def _update_count(self):
        ticked = [b for b in self.rows if self.plan[b].get("enabled")]
        paired = [b for b in ticked if os.path.isfile(self.plan[b].get("speaker_path", ""))]
        self.count_label.configure(text=f"{len(ticked)} of {len(self.rows)} ticked · "
                                        f"{len(paired)} with a seiyuu")
        self.on_change()


# ------------------------------------------------------------ the window

class ProfilerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Book Profiler")
        self.geometry("1040x960")
        self.minsize(900, 660)
        self.configure(fg_color=BG)
        icon = os.path.join(SCRIPT_DIR, "profiler_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass        # a missing or odd icon must never stop the tool

        self.gui = pr.load_gui_state()
        self.settings = pr.settings()
        self.speakers = pr.list_speakers(self.settings["irodori_root"])
        self.chapters = []
        self.folder_ok = False
        self.job = None             # the Run or ListenRun in progress
        self.live_states = {}       # chapter -> (key, text) reported by the run
        self._queue = queue.Queue()
        self._parse_pending = None
        self._customize = None

        self.folder_var = ctk.StringVar(value=self.gui["book_folder"])
        self.root_var = ctk.StringVar(value=self.gui["profile_root"])
        self.assign_var = ctk.StringVar(value=ASSIGN_LABELS[self.gui["assign"]])
        self.seeded_var = ctk.IntVar(value=1 if self.gui["seeded_arm"] else 0)
        self.clean_root_var = ctk.StringVar(value=self.gui["clean_root"]
                                            or self.gui["profile_root"])
        self.tab_var = ctk.StringVar(value=TAB_LABELS[0])

        self._build()
        self.folder_var.trace_add("write", lambda *_: self._schedule_parse())
        self.root_var.trace_add("write", lambda *_: self._schedule_refresh())
        self._parse_now()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain)

    # --------------------------------------------------------- building
    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11, border_width=1,
                            border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _section_head(self, card, number, title, hint=""):
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

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        header.pack(fill="x")
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=13)
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Book Profiler", text_color=TITLE,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        self.head_sub = ctk.CTkLabel(titles, text="A recipe per book and seiyuu, measured",
                                     text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.head_sub.pack(anchor="w")
        # Unselected segments are a mid grey, not the default light fill:
        # white text on #F1F1F4 was unreadable in the generator's GUI.
        ctk.CTkSegmentedButton(
            inner, values=list(TAB_LABELS), variable=self.tab_var, height=34, corner_radius=8,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, unselected_color="#A3A3AD",
            unselected_hover_color="#8E8E99", text_color="#FFFFFF",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._show_tab).pack(side="left", padx=(40, 0))

        self.run_button = button(inner, "▶  Run", self.on_run, width=130, primary=True)
        self.run_button.pack(side="right")
        self.stop_button = button(inner, "Stop", self.on_stop, width=100, danger=True,
                                  state="disabled")
        self.stop_button.pack(side="right", padx=(0, 10))

        self._build_log()

        self.profile_page = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.clean_page = ctk.CTkScrollableFrame(self, fg_color=BG)
        self._build_book(self.profile_page)
        self._build_seiyuu(self.profile_page)
        self._build_root(self.profile_page)
        self._build_progress(self.profile_page)
        self._build_results(self.profile_page)
        self._build_clean(self.clean_page)
        self._show_tab(TAB_LABELS[0])

    def _build_log(self):
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        button(bar, "Close", self.on_close, width=110).pack(side="right", padx=18, pady=11)
        self.status_label = ctk.CTkLabel(bar, text="", text_color=SUBTITLE,
                                         font=ctk.CTkFont(size=12))
        self.status_label.pack(side="left", padx=18)
        wrap = ctk.CTkFrame(self, fg_color=LOG_BG, corner_radius=0, height=190)
        wrap.pack(fill="x", side="bottom")
        wrap.pack_propagate(False)
        self.log_box = ctk.CTkTextbox(wrap, fg_color=LOG_BG, text_color=LOG_FG,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      border_width=0, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    def _build_book(self, parent):
        card = self._card(parent)
        self._section_head(card, 1, "Book", "the folder of chapter .txt files")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        button(row, "Browse…", self._browse_folder).pack(side="right")
        entry(row, self.folder_var, "e.g. F:\\AUDIOBOOK-FINAL\\<book>\\text").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        self.folder_status = ctk.CTkLabel(card, text="", anchor="w", justify="left",
                                          font=ctk.CTkFont(size=12), wraplength=900)
        self.folder_status.pack(fill="x", padx=16, pady=(0, 13))

    def _build_seiyuu(self, parent):
        card = self._card(parent)
        self._section_head(card, 2, "Seiyuu", "from " + os.path.join(
            self.settings["irodori_root"], "seiyuu", "list"))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(0, 10))
        self.assign_switch = ctk.CTkSegmentedButton(
            top, values=list(ASSIGN_LABELS.values()), variable=self.assign_var, height=32,
            corner_radius=8, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color="#A3A3AD", unselected_hover_color="#8E8E99",
            text_color="#FFFFFF", font=ctk.CTkFont(size=12, weight="bold"),
            command=self._assign_changed)
        self.assign_switch.pack(side="left")
        self.all_row = ctk.CTkFrame(card, fg_color="transparent")
        self.picker = SeiyuuPicker(self.all_row, self.speakers, self.gui["speaker_path"],
                                   self._main_speaker_changed)
        self.picker.pack(fill="x")
        self.custom_row = ctk.CTkFrame(card, fg_color="transparent")
        button(self.custom_row, "Customize chapters…", self._open_customize, width=170).pack(
            side="left")
        self.custom_status = ctk.CTkLabel(self.custom_row, text="", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=12))
        self.custom_status.pack(side="left", padx=12)
        self._assign_changed(self.assign_var.get(), persist=False)

    def _build_root(self, parent):
        card = self._card(parent)
        self._section_head(card, 3, "Profile folder", "where the profiles land")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        button(row, "Browse…", self._browse_root).pack(side="right")
        entry(row, self.root_var, "a folder of its own, e.g. F:\\AUDIOBOOK-PROFILES").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        self.root_hint = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                      justify="left", font=ctk.CTkFont(size=11))
        self.root_hint.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkCheckBox(card, text="Also use fixed seeds  (3 fixed-seed + 3 random takes per "
                                   "cell instead of 6 random - for seed experiments)",
                        variable=self.seeded_var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=ENTRY_TEXT, font=ctk.CTkFont(size=12),
                        command=self._persist).pack(anchor="w", padx=16, pady=(0, 13))

    def _build_progress(self, parent):
        card = self._card(parent)
        self.progress_hint = self._section_head(card, 4, "Progress", "")
        self.progress_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.progress_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_results(self, parent):
        card = self._card(parent)
        self._section_head(card, 5, "Profiles",
                           "use one in the generator's dynamic mode, or hear it here first")
        self.results_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.results_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_clean(self, parent):
        card = self._card(parent)
        self._section_head(card, 1, "Clean up measurement audio")
        ctk.CTkLabel(
            card, anchor="w", justify="left", text_color=ENTRY_TEXT, font=ctk.CTkFont(size=12),
            wraplength=900,
            text="Once a profile has proved good - by its listening test, or a real chapter in "
                 "the generator's dynamic mode - the takes it was measured from are no longer "
                 "needed. This deletes them for one seiyuu of one book, together with that "
                 "seiyuu's listening-test audio.\n\nKept: the profiles, recipe.md, score.json, "
                 "every take's record and Whisper transcript - the recipe can still be rebuilt "
                 "and read. Deletion is PERMANENT (no Recycle Bin), and a cleaned chapter "
                 "cannot be measured again in this folder: profile into a fresh folder instead."
        ).pack(fill="x", padx=16, pady=(0, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 13))
        button(row, "↻  Scan", self._refresh_clean, width=90).pack(side="right")
        button(row, "Browse…", self._browse_clean_root).pack(side="right", padx=(0, 8))
        entry(row, self.clean_root_var, "a profile folder").pack(side="left", fill="x",
                                                                 expand=True, padx=(0, 8))
        self.clean_card = self._card(parent)
        self.clean_hint = self._section_head(self.clean_card, 2, "Seiyuu in this folder", "")
        self.clean_frame = ctk.CTkFrame(self.clean_card, fg_color="transparent")
        self.clean_frame.pack(fill="x", padx=16, pady=(0, 13))

    # --------------------------------------------------------- tabs
    def _show_tab(self, label):
        if label == TAB_LABELS[0]:
            self.clean_page.pack_forget()
            self.profile_page.pack(fill="both", expand=True, padx=16, pady=(14, 0))
            self.run_button.pack(side="right")
            self.stop_button.pack(side="right", padx=(0, 10))
            self.refresh_all()
        else:
            self.profile_page.pack_forget()
            self.clean_page.pack(fill="both", expand=True, padx=16, pady=(14, 0))
            self.run_button.pack_forget()
            if not self.job:
                self.stop_button.pack_forget()
            self._refresh_clean()

    # --------------------------------------------------------- inputs
    def _browse_folder(self):
        chosen = filedialog.askdirectory(title="Select the book's chapter folder",
                                         initialdir=self.folder_var.get() or None)
        if chosen:
            self.folder_var.set(os.path.normpath(chosen))

    def _browse_root(self):
        chosen = filedialog.askdirectory(title="Select the profile folder",
                                         initialdir=self.root_var.get() or None)
        if chosen:
            self.root_var.set(os.path.normpath(chosen))
            if not self.clean_root_var.get().strip():
                self.clean_root_var.set(os.path.normpath(chosen))

    def _browse_clean_root(self):
        chosen = filedialog.askdirectory(title="Select a profile folder",
                                         initialdir=self.clean_root_var.get() or None)
        if chosen:
            self.clean_root_var.set(os.path.normpath(chosen))
            self._refresh_clean()

    def _schedule_parse(self):
        if self._parse_pending:
            self.after_cancel(self._parse_pending)
        self._parse_pending = self.after(400, self._parse_now)

    def _schedule_refresh(self):
        if self._parse_pending:
            self.after_cancel(self._parse_pending)
        self._parse_pending = self.after(400, self.refresh_all)

    def _parse_now(self):
        self._parse_pending = None
        folder = self.folder_var.get().strip()
        ok, message, bases = pr.parse_book_folder(folder)
        self.folder_ok, self.chapters = ok, bases if ok else []
        self.folder_status.configure(text=("✓  " if ok else "✕  ") + message,
                                     text_color=DONE if ok else FAIL)
        if self._customize is not None and self._customize.winfo_exists():
            self._customize.destroy()
        self.refresh_all()

    def _assign_changed(self, label, persist=True):
        if ASSIGN_KEYS[label] == "all":
            self.custom_row.pack_forget()
            self.all_row.pack(fill="x", padx=16, pady=(0, 13))
        else:
            self.all_row.pack_forget()
            self.custom_row.pack(fill="x", padx=16, pady=(0, 13))
        if persist:
            self.refresh_all()

    def _main_speaker_changed(self, _path):
        self.refresh_all()

    def _open_customize(self):
        if not self.folder_ok:
            messagebox.showerror("Book folder", "Choose a book folder that parses first.")
            return
        if self._customize is not None and self._customize.winfo_exists():
            self._customize.lift()
            return
        self._customize = CustomizeWindow(self, self.chapters, self.gui["chapters"],
                                          self.speakers, self.picker.path, self.refresh_all)

    # --------------------------------------------------------- the plan
    def plan(self):
        """[(chapter, speaker_path)] for the chosen chapters, in order."""
        if not self.folder_ok:
            return []
        if ASSIGN_KEYS[self.assign_var.get()] == "all":
            return [(c, self.picker.path) for c in self.chapters] if self.picker.path else []
        out = []
        for c in self.chapters:
            item = self.gui["chapters"].get(c) or {}
            if item.get("enabled"):
                out.append((c, item.get("speaker_path", "")))
        return out

    def book(self):
        return pr.analyze.book_name(self.folder_var.get().strip()) if self.folder_ok else ""

    def _persist(self):
        self.gui.update({
            "book_folder": self.folder_var.get().strip(),
            "profile_root": self.root_var.get().strip(),
            "speaker_path": self.picker.path,
            "assign": ASSIGN_KEYS[self.assign_var.get()],
            "seeded_arm": bool(self.seeded_var.get()),
            "clean_root": self.clean_root_var.get().strip(),
        })
        try:
            pr.save_gui_state(self.gui)
        except OSError as e:
            self.log(f"! could not save the window's state: {e}")

    # --------------------------------------------------------- refresh
    def refresh_all(self):
        self._persist()
        plan = self.plan()
        root = self.root_var.get().strip()
        book = self.book()
        if ASSIGN_KEYS[self.assign_var.get()] == "custom":
            ticked = sum(1 for c in self.chapters
                         if (self.gui["chapters"].get(c) or {}).get("enabled"))
            self.custom_status.configure(
                text=f"{ticked} of {len(self.chapters)} chapter(s) ticked" if self.folder_ok
                else "choose a book folder first")
        self.root_hint.configure(
            text=(f"Profiles will land in {os.path.join(root, book or '<book>', pr.SCOPE, 'recipe', '<seiyuu>')}"
                  f" - the measurement audio beside them, about 0.3 GB per chapter until "
                  f"cleaned up." if root else "Choose a folder for this book's profiles."))
        self._refresh_progress(plan, root, book)
        self._refresh_results(plan, root, book)
        self._refresh_buttons()

    def _refresh_progress(self, plan, root, book):
        for w in self.progress_frame.winfo_children():
            w.destroy()
        if not plan:
            self.progress_hint.configure(text="")
            ctk.CTkLabel(self.progress_frame, text="Choose a book and a seiyuu - the chapters "
                                                   "to profile are listed here.",
                         text_color=SUBTITLE, font=ctk.CTkFont(size=12)).pack(anchor="w")
            return
        done = 0
        for chapter, speaker in plan:
            key, text = self.live_states.get(chapter) or (
                pr.chapter_state(root, book, speaker, chapter) if root
                else ("none", "not started"))
            done += key in ("scored", "cleaned")
            row = ctk.CTkFrame(self.progress_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text=chapter, width=130, anchor="w", text_color=ENTRY_TEXT,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            ctk.CTkLabel(row, text=pr.nickname(speaker) or "(no seiyuu)", width=200, anchor="w",
                         text_color=ENTRY_TEXT if speaker else FAIL,
                         font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkLabel(row, text=("●  " if key != "none" else "○  ") + text, anchor="w",
                         text_color=STATE_COLORS.get(key, SUBTITLE),
                         font=ctk.CTkFont(size=12)).pack(side="left")
        todo = len(plan) - done
        lo, hi = (todo * m / 60 for m in pr.MINUTES_PER_CHAPTER)
        self.progress_hint.configure(
            text=f"{done} of {len(plan)} measured" +
                 (f" · about {lo:.1f}–{hi:.1f} h of GPU time to go" if todo else ""))

    def _refresh_results(self, plan, root, book):
        for w in self.results_frame.winfo_children():
            w.destroy()
        self._listen_buttons = []
        speakers = []
        for _c, s in plan:
            if s and s not in speakers:
                speakers.append(s)
        shown = 0
        for speaker in speakers:
            found = pr.profiles(root, book, speaker) if root and book else []
            if not found:
                continue
            shown += 1
            ctk.CTkLabel(self.results_frame, text=pr.nickname(speaker), anchor="w",
                         text_color=TITLE, font=ctk.CTkFont(size=14, weight="bold")).pack(
                fill="x", pady=(6, 2))
            for label, path in found:
                self._profile_row(root, book, speaker, label, path)
        if not shown:
            ctk.CTkLabel(self.results_frame, text="No profiles yet - they appear here when a "
                                                  "run finishes.",
                         text_color=SUBTITLE, font=ctk.CTkFont(size=12)).pack(anchor="w")

    def _profile_row(self, root, book, speaker, label, path):
        box = ctk.CTkFrame(self.results_frame, fg_color=BG, corner_radius=10)
        box.pack(fill="x", pady=4)
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(head, text="Book profile" if label == "book" else
                     label.replace("chapter_", "Chapter ") + " profile",
                     text_color=TITLE, font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ready = pr.listening_ready(root, book, speaker, label)
        listen = button(head, "▶  Open listening test" if ready else "Generate listening test",
                        lambda: self.on_listen(speaker, label, ready), width=190)
        listen.pack(side="right")
        self._listen_buttons.append(listen)
        button(head, "Open folder", lambda: os.startfile(os.path.dirname(path)),
               width=100).pack(side="right", padx=(0, 8))
        button(head, "Copy path", lambda: self._copy(path), width=90).pack(side="right",
                                                                         padx=(0, 8))
        try:
            lines = pr.dynamic_profile.summary_lines(pr.dynamic_profile.load_profile(path))
            color = SUBTITLE
        except pr.dynamic_profile.ProfileError as e:
            lines, color = [f"Not usable: {e}"], FAIL
        ctk.CTkLabel(box, text="\n".join(lines), anchor="w", justify="left", text_color=color,
                     font=ctk.CTkFont(size=11), wraplength=880).pack(fill="x", padx=12,
                                                                     pady=(4, 10))

    def _refresh_buttons(self):
        busy = self.job is not None
        self.run_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        for b in getattr(self, "_listen_buttons", []):
            try:
                b.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(f"copied {text}")

    # --------------------------------------------------------- run
    def _check_inputs(self):
        if not self.folder_ok:
            return "The book folder did not parse - choose the folder with the chapter .txt files."
        root = self.root_var.get().strip()
        if not root:
            return "Choose a profile folder."
        plan = self.plan()
        if not plan:
            return "No chapters to profile - choose a seiyuu, or tick chapters in Customize."
        missing = [c for c, s in plan if not s or not os.path.isfile(s)]
        if missing:
            return "These chapters have no usable seiyuu file: " + ", ".join(missing[:6]) + \
                (" …" if len(missing) > 6 else "")
        return None

    def on_run(self):
        if self.job:
            return
        problem = self._check_inputs()
        if problem:
            messagebox.showerror("Cannot run yet", problem)
            return
        root = self.root_var.get().strip()
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Profile folder", f"Could not create {root}:\n{e}")
            return
        plan = self.plan()
        book = self.book()
        todo = [c for c, s in plan
                if pr.chapter_state(root, book, s, c)[0] not in ("scored", "cleaned")]
        warning = pr.gpu_warning()
        if warning and not messagebox.askyesno("The GPU is busy", warning + "\n\nRun anyway?",
                                               icon="warning"):
            return
        lo, hi = (len(todo) * m / 60 for m in pr.MINUTES_PER_CHAPTER)
        if not messagebox.askyesno(
                "Start profiling",
                f"{book}: {len(plan)} chapter(s), {len(todo)} still to measure - roughly "
                f"{lo:.1f}–{hi:.1f} hours on the GPU.\n\nThe PC is kept awake until the run "
                f"ends. A chapter that fails is retried once and then skipped, so the rest "
                f"carry on.\n\nStart?"):
            return
        self._persist()
        self.live_states = {}
        self.job = pr.Run(self.folder_var.get().strip(), root, plan, bool(self.seeded_var.get()),
                          on_log=self.log,
                          on_state=lambda c, k, t: self.ui(lambda: self._live(c, k, t)),
                          on_done=lambda s: self.ui(lambda: self._run_done(s)))
        self.head_sub.configure(text=f"Profiling {book}…")
        self.status_label.configure(text="running - the PC is kept awake", text_color=ACCENT)
        self._refresh_buttons()
        self.job.start()

    def _live(self, chapter, key, text):
        self.live_states[chapter] = (key, text)
        self.refresh_all()

    def _run_done(self, s):
        self.job = None
        self.live_states = {c: v for c, v in self.live_states.items() if v[0] == "failed"}
        parts = []
        if s.get("analysis_failed"):
            parts.append("the analysis failed - nothing was measured")
        if s["measured"]:
            parts.append(f"{len(s['measured'])} chapter(s) measured")
        if s["failed"]:
            parts.append(f"{len(s['failed'])} failed: " + ", ".join(s["failed"]))
        if s["skipped_cleaned"]:
            parts.append(f"{len(s['skipped_cleaned'])} already cleaned up")
        if s["recipes"]:
            parts.append("recipe for " + ", ".join(s["recipes"]))
        if s["recipe_failed"]:
            parts.append("recipe FAILED for " + ", ".join(s["recipe_failed"]))
        if s.get("error"):
            parts.append("error: " + s["error"])
        verdict = ("Stopped" if s["stopped"] else "Finished") + f" after {s['minutes']} min"
        text = verdict + (" - " + "; ".join(parts) if parts else "")
        self.log("=== " + text)
        bad = s["failed"] or s["recipe_failed"] or s.get("analysis_failed") or s.get("error")
        self.head_sub.configure(text=verdict)
        self.status_label.configure(text=text, text_color=FAIL if bad else
                                    (WARN if s["stopped"] else DONE))
        self.refresh_all()

    def on_stop(self):
        if not self.job:
            return
        if not messagebox.askyesno("Stop", "Stop now? What is finished stays on disk, and Run "
                                           "carries on from there next time."):
            return
        self.log("stopping - ending the stage and its TTS / Whisper process…")
        self.stop_button.configure(state="disabled")
        self.job.stop()

    def on_listen(self, speaker, label, ready):
        if self.job:
            return
        root, book = self.root_var.get().strip(), self.book()
        if ready:
            pr.open_listening_window(book, root, speaker, label)
            self.log(f"opened the listening test: {pr.nickname(speaker)} / {label}")
            return
        warning = pr.gpu_warning()
        if warning and not messagebox.askyesno("The GPU is busy", warning + "\n\nRun anyway?",
                                               icon="warning"):
            return
        self.job = pr.ListenRun(book, root, speaker, label, on_log=self.log,
                                on_done=lambda s: self.ui(lambda: self._listen_done(s)))
        self.head_sub.configure(text=f"Listening test: {pr.nickname(speaker)} / {label}…")
        self.status_label.configure(text="rendering samples", text_color=ACCENT)
        self._refresh_buttons()
        self.job.start()

    def _listen_done(self, s):
        self.job = None
        text = "listening test ready - the Results window opens by itself" if s["ok"] else \
            ("listening test stopped" if s["stopped"] else "listening test FAILED - see the log")
        self.log("=== " + text)
        self.head_sub.configure(text="A recipe per book and seiyuu, measured")
        self.status_label.configure(text=text, text_color=DONE if s["ok"] else FAIL)
        self.refresh_all()

    # --------------------------------------------------------- clean up
    def _refresh_clean(self):
        self._persist()
        for w in self.clean_frame.winfo_children():
            w.destroy()
        root = self.clean_root_var.get().strip()
        rows = pr.cleanup_rows(root)
        total = sum(r["bytes"] for r in rows)
        self.clean_hint.configure(text=f"{human_bytes(total)} of measurement audio" if rows
                                  else "")
        if not rows:
            ctk.CTkLabel(self.clean_frame, text="Nothing measured in this folder." if root
                         else "Choose a profile folder.", text_color=SUBTITLE,
                         font=ctk.CTkFont(size=12)).pack(anchor="w")
            return
        for row in rows:
            box = ctk.CTkFrame(self.clean_frame, fg_color=BG, corner_radius=10)
            box.pack(fill="x", pady=4)
            head = ctk.CTkFrame(box, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(8, 0))
            name = f"{row['book']}  ·  {row['seiyuu']}" + \
                ("" if row["scope"] == pr.SCOPE else f"  ·  scope {row['scope']}")
            ctk.CTkLabel(head, text=name, text_color=TITLE,
                         font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
            can = bool(row["covered"]) and row["has_recipe"] and self.job is None
            b = button(head, f"Delete audio ({human_bytes(row['bytes'])})" if row["covered"]
                       else "Nothing to delete", lambda r=row: self.on_clean(r), width=190,
                       danger=True, state="normal" if can else "disabled")
            b.pack(side="right")
            notes = []
            if row["covered"]:
                notes.append(f"measured and in the book profile: {len(row['covered'])} "
                             f"chapter(s) - {', '.join(row['covered'])}")
            if row["cleaned"]:
                notes.append(f"already cleaned: {', '.join(row['cleaned'])}")
            if row["uncovered"]:
                notes.append(f"kept (not measured, or not in the book profile yet): "
                             f"{', '.join(row['uncovered'])}")
            if not row["has_recipe"]:
                notes.append("no recipe yet - finish the run first")
            if self.job is not None:
                notes.append("a run is in progress - clean up when it has finished")
            ctk.CTkLabel(box, text="\n".join(notes), anchor="w", justify="left",
                         text_color=SUBTITLE, font=ctk.CTkFont(size=11), wraplength=880).pack(
                fill="x", padx=12, pady=(4, 10))

    def on_clean(self, row):
        if self.job:
            return
        if not messagebox.askyesno(
                "Delete measurement audio",
                f"{row['book']} · {row['seiyuu']}\n\nPermanently delete {len(row['wavs'])} audio "
                f"file(s), {human_bytes(row['bytes'])}, from {len(row['covered'])} chapter(s) "
                f"and the listening tests?\n\nThis cannot be undone - nothing goes to the "
                f"Recycle Bin. The profiles and every measurement are kept.", icon="warning"):
            return
        try:
            files, freed = pr.clean_up(row, self.log)
        except OSError as e:
            self.log(f"! clean-up stopped: {e}")
            messagebox.showerror("Clean up", f"Stopped part way:\n{e}\n\nThe chapters already "
                                             f"marked are protected; press Scan and try again.")
        else:
            self.log(f"=== cleaned up {row['book']} / {row['seiyuu']}: {files} file(s), "
                     f"{human_bytes(freed)} freed")
        self._refresh_clean()

    # --------------------------------------------------------- plumbing
    def log(self, message):
        self._queue.put(("log", message))

    def ui(self, fn):
        """Runs fn on the Tk thread - the run's thread must never touch widgets."""
        self._queue.put(("call", fn))

    def _drain(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("%H:%M:%S  ") + payload + "\n")
                    # A night's run writes thousands of lines; keep the last few.
                    lines = int(self.log_box.index("end-1c").split(".")[0])
                    if lines > 4000:
                        self.log_box.delete("1.0", f"{lines - 3000}.0")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                else:
                    payload()
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def on_close(self):
        if self.job and not messagebox.askyesno(
                "Still running", "A run is in progress. Closing stops it - what is finished "
                                 "stays on disk and Run resumes it next time.\n\nClose anyway?"):
            return
        if self.job:
            self.job.stop()
        self._persist()
        self.destroy()


if __name__ == "__main__":
    ProfilerApp().mainloop()
