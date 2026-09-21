"""
dynamic_mode_ui.py
------------------
The pieces of the settings GUI that exist only for dynamic profile mode
(2026-09-17). Kept out of gui_settings.py so normal mode's code stays as it
was; gui_settings.py wires these in.

    parse_input_folder  what "choose an Input Folder" checks in dynamic mode
    ProfilePanel        Profile Path + summary + style selector
    CustomizeWindow     one row per chapter: tick, profile, style

All profile logic comes from dynamic_profile.py - the same code the
generator renders with - so what the GUI calls usable is what the run can
actually use.
"""

import glob
import os

import customtkinter as ctk
from tkinter import filedialog

import dynamic_profile
import text_pipeline
from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ENTRY_BORDER, COLOR_ENTRY_TEXT, COLOR_ACCENT, COLOR_ACCENT_HOVER,
    COLOR_BTN_NEUTRAL_BORDER, COLOR_BTN_NEUTRAL_TEXT,
)

COLOR_OK = "#1E8B4E"
COLOR_ERROR = "#C4453C"
COLOR_WARN = "#B7791F"
COLOR_DISABLED = "#B8B8C0"
STYLE_BY_LABEL = {label: key for key, label in dynamic_profile.STYLE_LABELS.items()}


# ------------------------------------------------------------ input folder

def parse_input_folder(folder):
    """(ok, message, [chapter bases]).

    Dynamic mode renders sentence by sentence, so "parsed" means more than
    "files exist": every chapter_*.txt must decode as UTF-8 and yield at
    least one sentence. Any failure blocks generation."""
    if not folder or not folder.strip():
        return False, "Choose the folder that holds the chapter_*.txt files.", []
    if not os.path.isdir(folder):
        return False, "This folder does not exist - choose the folder that holds " \
                      "the chapter_*.txt files.", []
    files = sorted(glob.glob(os.path.join(folder, "chapter_*.txt")))
    if not files:
        return False, "No chapter_*.txt files in this folder - choose the folder " \
                      "that holds them.", []
    bases, problems = [], []
    for path in files:
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except UnicodeDecodeError:
            problems.append(f"{base} is not UTF-8")
            continue
        except OSError as e:
            problems.append(f"{base} could not be read ({e.strerror})")
            continue
        if not text_pipeline.dynamic_sentences(raw):
            problems.append(f"{base} has no text")
            continue
        bases.append(base)
    if problems:
        return False, "Parsing failed: " + "; ".join(problems[:4]) + \
            (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "") + \
            ". Fix the file(s) or choose the correct folder.", bases
    return True, f"Parsing successful - {len(bases)} chapter file(s) found " \
                 f"({bases[0]} … {bases[-1]})" if len(bases) > 1 else \
                 f"Parsing successful - 1 chapter file found ({bases[0]})", bases


def check_profile(path, style=None):
    """(profile or None, [summary lines], ok). ok is False for anything the
    run would refuse: unreadable, wrong version, missing speaker file, or -
    when `style` is given - a style the profile does not offer."""
    if not path or not path.strip():
        return None, ["No profile chosen."], False
    try:
        profile = dynamic_profile.load_profile(path.strip())
        if style:
            dynamic_profile.check_style(profile, style)
    except dynamic_profile.ProfileError as e:
        return None, [f"Not usable: {e}"], False
    ok, _note = dynamic_profile.speaker_status(profile)
    return profile, dynamic_profile.summary_lines(profile), ok


# ------------------------------------------------------------ profile panel

class ProfilePanel(ctk.CTkFrame):
    """Profile Path entry + Browse, then - once a profile is chosen - its
    summary and the style selector. Used on the General page (the profile
    for all chapters / the default) and in every Customize row.

    `on_change(path, style)` fires whenever either changes."""

    def __init__(self, parent, path="", style=dynamic_profile.DEFAULT_STYLE, on_change=None,
                 compact=False, entry_width=None):
        super().__init__(parent, fg_color="transparent")
        self.on_change = on_change
        self.path_var = ctk.StringVar(value=path or "")
        self.style_var = ctk.StringVar(value=dynamic_profile.STYLE_LABELS.get(
            style, dynamic_profile.STYLE_LABELS[dynamic_profile.DEFAULT_STYLE]))
        self.ok = False
        self._enabled = True

        line = ctk.CTkFrame(self, fg_color="transparent")
        line.pack(fill="x")
        self.browse = ctk.CTkButton(
            line, text="\U0001F4C1  Browse", width=110, height=34, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT, command=self._browse)
        self.browse.pack(side="right")
        self.entry = ctk.CTkEntry(line, textvariable=self.path_var, height=34, corner_radius=8,
                                  border_width=1, border_color=COLOR_ENTRY_BORDER,
                                  text_color=COLOR_ENTRY_TEXT, fg_color="white",
                                  placeholder_text="profile_*.json from book-profiler")
        if entry_width:
            self.entry.configure(width=entry_width)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.details = ctk.CTkFrame(self, fg_color="transparent")
        self.summary = ctk.CTkLabel(self.details, text="", justify="left", anchor="w",
                                    text_color=COLOR_SUBTITLE,
                                    font=ctk.CTkFont(size=11 if compact else 12),
                                    wraplength=420)
        self.summary.pack(fill="x", pady=(6, 4))
        style_line = ctk.CTkFrame(self.details, fg_color="transparent")
        style_line.pack(fill="x")
        ctk.CTkLabel(style_line, text="Style", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 10))
        self.style_menu = ctk.CTkOptionMenu(
            style_line, variable=self.style_var,
            values=[dynamic_profile.STYLE_LABELS[k] for k in dynamic_profile.STYLE_KEYS],
            width=210, height=30, fg_color="white", button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_ENTRY_TEXT,
            dropdown_fg_color="white", command=lambda _v: self._changed())
        self.style_menu.pack(side="left")

        self._trace = self.path_var.trace_add("write", lambda *_: self._schedule())
        self._pending = None
        self.refresh()

    # --- state
    @property
    def path(self):
        return self.path_var.get().strip()

    @property
    def style(self):
        return STYLE_BY_LABEL.get(self.style_var.get(), dynamic_profile.DEFAULT_STYLE)

    def set_value(self, path, style):
        self.path_var.set(path or "")
        self.style_var.set(dynamic_profile.STYLE_LABELS.get(
            style, dynamic_profile.STYLE_LABELS[dynamic_profile.DEFAULT_STYLE]))
        self.refresh()

    def set_enabled(self, enabled):
        self._enabled = enabled
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state, text_color=COLOR_ENTRY_TEXT if enabled
                             else COLOR_DISABLED)
        self.browse.configure(state=state, text_color=COLOR_ENTRY_TEXT if enabled
                              else COLOR_DISABLED)
        self.style_menu.configure(state=state)
        self.refresh()

    # --- behaviour
    def _browse(self):
        current = self.path
        initial = os.path.dirname(current) if os.path.isfile(current) else os.getcwd()
        chosen = filedialog.askopenfilename(title="Select Profile", initialdir=initial,
                                            filetypes=[("Profile", "profile_*.json"),
                                                       ("JSON", "*.json"), ("All files", "*.*")])
        if chosen:
            self.path_var.set(os.path.normpath(chosen))

    def _schedule(self):
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(350, self._changed)

    def _changed(self):
        self._pending = None
        self.refresh()
        if self.on_change:
            self.on_change(self.path, self.style)

    def refresh(self):
        if not self.path:
            self.ok = False
            self.details.pack_forget()
            return
        profile, lines, ok = check_profile(self.path)
        self.ok = ok
        color = COLOR_SUBTITLE
        if not ok:
            color = COLOR_ERROR
        elif profile is not None and profile.get("speaker_stamp") and \
                dynamic_profile.speaker_stamp(profile["speaker_path"]) != profile["speaker_stamp"]:
            # Tested on the stamps, not the wording: "unchanged since
            # profiling" contains "changed since profiling".
            color = COLOR_WARN
        if not self._enabled:
            color = COLOR_DISABLED
        self.summary.configure(text="\n".join(lines), text_color=color)
        self.details.pack(fill="x")
        if profile is not None:
            # A narrow seiyuu's profile offers fewer styles (v3): offer only
            # those, and move a saved style it does not have to default.
            offered = [dynamic_profile.STYLE_LABELS[k]
                       for k in dynamic_profile.available_styles(profile)]
            self.style_menu.configure(values=offered)
            if self.style_var.get() not in offered:
                self.style_var.set(dynamic_profile.STYLE_LABELS[dynamic_profile.DEFAULT_STYLE])
                if self.on_change:
                    self.after_idle(lambda: self.on_change(self.path, self.style))
        if ok:
            self.style_menu.master.pack(fill="x")
        else:
            self.style_menu.master.pack_forget()


# ------------------------------------------------------------ customize

class CustomizeWindow(ctk.CTkToplevel):
    """One row per chapter found in the input folder: tick it to render it,
    then give it a profile and a style.

    A newly ticked row with no profile is pre-filled with the last profile
    and style assigned anywhere - the General page's, until a row is given
    one of its own (decision 2026-09-17). Unticked rows are greyed out and
    skipped by the run.

    Edits go straight into `plan` ({base: {"enabled", "profile_path",
    "style"}}); `on_change()` fires after each so the General page can
    update its count."""

    def __init__(self, master, chapters, plan, default_path, default_style, on_change=None):
        super().__init__(master)
        self.title("Customize Chapters")
        self.configure(fg_color=COLOR_BG)
        self.geometry("860x680")
        self.minsize(700, 420)
        self.plan = plan
        self.on_change = on_change
        self.last_path = default_path
        self.last_style = default_style
        self.rows = {}

        head = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=0)
        head.pack(fill="x")
        ctk.CTkLabel(head, text="Customize chapters", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=17, weight="bold")).pack(side="left", padx=18, pady=12)
        for text, value in (("Untick all", False), ("Tick all", True)):
            ctk.CTkButton(head, text=text, width=90, height=30, corner_radius=8,
                          fg_color="transparent", hover_color="#F0F0F3", border_width=1,
                          border_color=COLOR_BTN_NEUTRAL_BORDER,
                          text_color=COLOR_BTN_NEUTRAL_TEXT,
                          command=lambda v=value: self._tick_all(v)).pack(side="right", padx=(0, 10),
                                                                          pady=12)
        self.count_label = ctk.CTkLabel(head, text="", text_color=COLOR_SUBTITLE,
                                        font=ctk.CTkFont(size=12))
        self.count_label.pack(side="right", padx=14)

        body = ctk.CTkScrollableFrame(self, fg_color=COLOR_BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        for base in chapters:
            self._add_row(body, base)

        bar = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkButton(bar, text="Done", width=110, height=34, corner_radius=8,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                      command=self.destroy).pack(side="right", padx=18, pady=11)
        ctk.CTkLabel(bar, text="Unticked chapters are skipped. A ticked chapter needs a "
                               "usable profile before Save & Run will start.",
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=11)).pack(
            side="left", padx=18)
        self._update_count()
        self.after(60, lambda: (self.lift(), self.focus_force()))

    def _add_row(self, parent, base):
        entry = self.plan.setdefault(base, {"enabled": False, "profile_path": "",
                                            "style": dynamic_profile.DEFAULT_STYLE})
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=10, border_width=1,
                            border_color=COLOR_CARD_BORDER)
        card.pack(fill="x", pady=5)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        var = ctk.IntVar(value=1 if entry.get("enabled") else 0)
        check = ctk.CTkCheckBox(inner, text="", variable=var, width=24,
                                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                                command=lambda b=base: self._toggled(b))
        check.pack(side="left", anchor="n", pady=(6, 0))
        label = ctk.CTkLabel(inner, text=base.replace("chapter_", "Chapter "), width=100,
                             anchor="w", font=ctk.CTkFont(size=13, weight="bold"))
        label.pack(side="left", anchor="n", padx=(4, 10), pady=(4, 0))
        panel = ProfilePanel(inner, entry.get("profile_path", ""), entry.get("style"),
                             on_change=lambda path, style, b=base: self._assigned(b, path, style),
                             compact=True)
        panel.pack(side="left", fill="x", expand=True)
        self.rows[base] = {"var": var, "label": label, "panel": panel}
        self._apply_enabled(base)

    def _apply_enabled(self, base):
        row = self.rows[base]
        enabled = bool(row["var"].get())
        row["label"].configure(text_color=COLOR_TITLE if enabled else COLOR_DISABLED)
        row["panel"].set_enabled(enabled)

    def _toggled(self, base):
        row = self.rows[base]
        enabled = bool(row["var"].get())
        self.plan[base]["enabled"] = enabled
        if enabled and not row["panel"].path and self.last_path:
            row["panel"].set_value(self.last_path, self.last_style)
            self.plan[base]["profile_path"] = self.last_path
            self.plan[base]["style"] = self.last_style
        self._apply_enabled(base)
        self._update_count()

    def _assigned(self, base, path, style):
        self.plan[base]["profile_path"] = path
        self.plan[base]["style"] = style
        if path:
            self.last_path, self.last_style = path, style
        self._update_count()

    def _tick_all(self, value):
        for base, row in self.rows.items():
            if bool(row["var"].get()) != value:
                row["var"].set(1 if value else 0)
                self._toggled(base)

    def _update_count(self):
        ticked = [b for b in self.rows if self.plan[b].get("enabled")]
        ready = [b for b in ticked if self.rows[b]["panel"].ok]
        self.count_label.configure(
            text=f"{len(ticked)} of {len(self.rows)} ticked · {len(ready)} ready")
        if self.on_change:
            self.on_change()


def plan_status(chapters, plan):
    """(ticked, ready, [problems]) for a Customize plan over `chapters`."""
    ticked, ready, problems = [], [], []
    for base in chapters:
        entry = plan.get(base) or {}
        if not entry.get("enabled"):
            continue
        ticked.append(base)
        _p, lines, ok = check_profile(entry.get("profile_path", ""),
                                      entry.get("style") or dynamic_profile.DEFAULT_STYLE)
        if ok:
            ready.append(base)
        else:
            problems.append(f"{base}: {lines[0] if lines else 'no profile'}")
    return ticked, ready, problems
