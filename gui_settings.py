"""
JP Audiobook Generator - Settings GUI
---------------------------------------
A CustomTkinter GUI for editing settings.json used by run_audiobook.py.

Usage (from PowerShell):
    uv run python gui_settings.py
or via the provided Run-Settings.ps1 wrapper (recommended - handles the
correct venv automatically).

Dependencies (installed in THIS project's own uv-managed venv, separate
from the Irodori-TTS venv used to actually run the audiobook pipeline):
    customtkinter, pillow
"""

import os
import sys
import json
import subprocess

import customtkinter as ctk
from tkinter import filedialog, messagebox

# ---------------------------------------------------------------------------
# Paths / defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
RUN_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "run_audiobook.py")
ICON_PATH = os.path.join(SCRIPT_DIR, "audiobook_icon.ico")

DEFAULT_SETTINGS = {
    "input_folder": r"E:\AUDIOBOOK\chapter",
    "output_folder": r"E:\AUDIOBOOK\output",
    "temp_dir": r"D:\AUDIOBOOK_TMP",
    "model_path": r"C:\Irodori-TTS\model.safetensors",
    "speaker_path": r"C:\Irodori-TTS\seiyuu\ueshama.speaker.safetensors",
    "silence_duration": 1.0,
    "clean_temp_after_run": True,
    "uv_project_dir": r"C:\Irodori-TTS",
}

# ---------------------------------------------------------------------------
# Design tokens (matches Documentation/mockup_GUI_20260817.png)
# ---------------------------------------------------------------------------

COLOR_BG = "#F7F7FA"
COLOR_CARD = "#FFFFFF"
COLOR_CARD_BORDER = "#E7E7EC"
COLOR_TITLE = "#17171C"
COLOR_SUBTITLE = "#8B8B94"
COLOR_ENTRY_BORDER = "#E2E2E8"
COLOR_ENTRY_TEXT = "#3A3A42"
COLOR_ACCENT = "#6C5DD3"
COLOR_ACCENT_HOVER = "#5B4FC0"
COLOR_TOGGLE_ON = "#34C773"
COLOR_BTN_NEUTRAL_BORDER = "#D8D8DE"
COLOR_BTN_NEUTRAL_TEXT = "#3A3A42"

# (emoji, vivid_bg_color) for the top "path" cards
ICON_INPUT = ("\U0001F4D6", "#8272F4")      # 📖 open book
ICON_OUTPUT = ("\U0001F3B5", "#4FCB8F")     # 🎵 music note
ICON_TEMP = ("\U0001F5C4", "#F5A83C")       # 🗄 file cabinet (storage)
ICON_MODEL = ("\U0001F4E6", "#4DA6FF")      # 📦 package/cube
ICON_SPEAKER = ("\U0001F50A", "#9C6DEE")    # 🔊 speaker
# uv icon is plain "UV" text rather than emoji, drawn separately.
ICON_UV_BG = "#2BC0BA"

# (emoji, pastel_bg, icon_color) for the lower "preference" cards
ICON_SILENCE = ("\U0001F550", "#EDEBFC", "#6C5DD3")   # 🕐 clock
ICON_KEEP_TEMP = ("\u2714", "#E6F8ED", "#2FB668")      # ✔ check


def load_settings():
    """Load settings.json, filling in any missing keys with defaults.
    Creates the file with defaults if it doesn't exist yet."""
    if not os.path.exists(SETTINGS_PATH):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded)
    return merged


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------

class IconBadge(ctk.CTkFrame):
    """Rounded colored square with a centered glyph/emoji or short text."""

    def __init__(self, parent, glyph, bg_color, text_color="white",
                 size=44, font_size=18, corner_radius=12, **kwargs):
        super().__init__(parent, width=size, height=size, corner_radius=corner_radius,
                          fg_color=bg_color, **kwargs)
        self.pack_propagate(False)
        ctk.CTkLabel(self, text=glyph, text_color=text_color,
                     font=ctk.CTkFont(size=font_size, weight="bold")).place(
            relx=0.5, rely=0.5, anchor="center")


class NumberSpinner(ctk.CTkFrame):
    """A small numeric entry with up/down stepper buttons (mimics the
    mockup's silence-duration spinner; ttk/CTk have no native spinbox)."""

    def __init__(self, parent, textvariable, step=0.5, minval=0.0, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.var = textvariable
        self.step = step
        self.minval = minval

        self.entry = ctk.CTkEntry(
            self, textvariable=self.var, width=70, height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")
        self.entry.pack(side="left")

        stepper = ctk.CTkFrame(self, fg_color="transparent", width=26, height=36)
        stepper.pack(side="left", padx=(4, 0))
        stepper.pack_propagate(False)

        ctk.CTkButton(
            stepper, text="\u25B2", width=26, height=17, corner_radius=6,
            fg_color="#F3F3F6", hover_color="#E5E5EA", text_color="#666666",
            font=ctk.CTkFont(size=9), command=self._increment,
        ).pack(side="top")
        ctk.CTkButton(
            stepper, text="\u25BC", width=26, height=17, corner_radius=6,
            fg_color="#F3F3F6", hover_color="#E5E5EA", text_color="#666666",
            font=ctk.CTkFont(size=9), command=self._decrement,
        ).pack(side="top", pady=(2, 0))

    def _current(self):
        try:
            return float(self.var.get())
        except ValueError:
            return 0.0

    def _increment(self):
        self.var.set(self._fmt(round(self._current() + self.step, 2)))

    def _decrement(self):
        self.var.set(self._fmt(round(max(self.minval, self._current() - self.step), 2)))

    @staticmethod
    def _fmt(value):
        # Keep whole numbers looking like "1.0" rather than "1" for clarity.
        return f"{value:g}" if value != int(value) else f"{value:.1f}"


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class SettingsApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")

        self.title("JP Audiobook Generator - Settings")
        self.configure(fg_color=COLOR_BG)
        # Resizable (not fixed) + scrollable content, so the window can never
        # again render taller than the screen with no way to reach the rest.
        self.resizable(True, True)
        self.minsize(720, 480)
        self._set_initial_geometry()
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass  # non-fatal cosmetic failure

        self.settings = load_settings()

        self.vars = {
            "input_folder": ctk.StringVar(value=self.settings["input_folder"]),
            "output_folder": ctk.StringVar(value=self.settings["output_folder"]),
            "temp_dir": ctk.StringVar(value=self.settings["temp_dir"]),
            "model_path": ctk.StringVar(value=self.settings["model_path"]),
            "speaker_path": ctk.StringVar(value=self.settings["speaker_path"]),
            "silence_duration": ctk.StringVar(value=str(self.settings["silence_duration"])),
            "uv_project_dir": ctk.StringVar(value=self.settings["uv_project_dir"]),
        }

        initial_keep = not bool(self.settings.get("clean_temp_after_run", True))
        self.keep_temp_var = ctk.IntVar(value=1 if initial_keep else 0)

        self._build_ui()

    def _set_initial_geometry(self):
        """Size the window relative to the actual screen (CustomTkinter is
        DPI-aware, so these are already logical/scaled pixels - no manual
        DPI math needed) and cap it so it always fits, then center it."""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        desired_w, desired_h = 880, 820
        # Leave headroom for the taskbar and window chrome.
        max_h = max(480, screen_h - 120)
        max_w = max(720, screen_w - 120)

        w = min(desired_w, max_w)
        h = min(desired_h, max_h)
        x = (screen_w - w) // 2
        y = max(0, (screen_h - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- UI construction ----------
    def _build_ui(self):
        # scrollbar colors match the window background so the bar is
        # invisible when everything already fits (the normal case) but
        # still works if content ever needs to scroll on a smaller screen.
        outer = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLOR_BG, scrollbar_button_hover_color="#D8D8DE")
        outer.pack(fill="both", expand=True, padx=28, pady=24)

        # Header
        ctk.CTkLabel(outer, text="General Settings", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=24, weight="bold"),
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(outer, text="Configure paths and basic preferences for the audiobook generation process.",
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=13),
                     anchor="w").pack(fill="x", pady=(2, 18))

        # Card 1: paths
        paths_card = ctk.CTkFrame(outer, fg_color=COLOR_CARD, corner_radius=16,
                                   border_width=1, border_color=COLOR_CARD_BORDER)
        paths_card.pack(fill="x")

        self._add_path_row(paths_card, *ICON_INPUT, "Input Folder (chapters)",
                            "Folder containing input chapters", "input_folder", "folder")
        self._add_path_row(paths_card, *ICON_OUTPUT, "Output Folder (MP3s)",
                            "Folder to save generated MP3 files", "output_folder", "folder")
        self._add_path_row(paths_card, *ICON_TEMP, "Temp Folder",
                            "Folder for temporary files", "temp_dir", "folder")
        self._add_path_row(paths_card, *ICON_MODEL, "Model Path (.safetensors)",
                            "Path to the model (.safetensors)", "model_path", "file",
                            filetypes=[("SafeTensors", "*.safetensors"), ("All files", "*.*")])
        self._add_path_row(paths_card, *ICON_SPEAKER, "Speaker Path (.safetensors)",
                            "Path to the speaker (.safetensors)", "speaker_path", "file",
                            filetypes=[("SafeTensors", "*.safetensors"), ("All files", "*.*")])
        self._add_uv_row(paths_card)

        # Card 2: preferences
        prefs_card = ctk.CTkFrame(outer, fg_color=COLOR_CARD, corner_radius=16,
                                   border_width=1, border_color=COLOR_CARD_BORDER)
        prefs_card.pack(fill="x", pady=(18, 0))

        self._add_silence_row(prefs_card)
        self._add_toggle_row(prefs_card)

        # Action buttons
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", pady=(20, 0))

        ctk.CTkButton(
            btn_row, text="Reset to Defaults", width=150, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F0F0F3", border_width=1,
            border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
            command=self.on_reset_defaults,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text="Close", width=100, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F0F0F3", border_width=1,
            border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
            command=self.destroy,
        ).pack(side="right")

        ctk.CTkButton(
            btn_row, text="Save & Run", width=130, height=38, corner_radius=8,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color="white",
            command=self.on_save_and_run,
        ).pack(side="right", padx=(0, 10))

        ctk.CTkButton(
            btn_row, text="Save Settings", width=130, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F1F0FC", border_width=1,
            border_color=COLOR_ACCENT, text_color=COLOR_ACCENT,
            command=self.on_save,
        ).pack(side="right", padx=(0, 10))

    def _row_shell(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=12)
        return row

    def _title_block(self, row, title, subtitle, fixed_width=None, fixed_height=None):
        text_frame = ctk.CTkFrame(row, fg_color="transparent")
        if fixed_width or fixed_height:
            # IMPORTANT: CTkFrame defaults to height=200 if not given explicitly.
            # pack_propagate(False) LOCKS that in, so we must always pass an
            # explicit height here too, or every row silently becomes 200px tall.
            text_frame.configure(width=fixed_width or 1, height=fixed_height or 44)
            text_frame.pack_propagate(False)
        text_frame.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(text_frame, text=title, text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(text_frame, text=subtitle, text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=11), anchor="w").pack(anchor="w")
        return text_frame

    def _add_path_row(self, parent, glyph, bg_color, title, subtitle, key,
                       browse_kind, filetypes=None):
        row = self._row_shell(parent)
        IconBadge(row, glyph, bg_color).pack(side="left", padx=(0, 14))
        self._title_block(row, title, subtitle, fixed_width=210, fixed_height=44)

        browse_btn = ctk.CTkButton(
            row, text="\U0001F4C1  Browse", width=110, height=36, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT,
            command=lambda: self._browse(key, browse_kind, filetypes))
        browse_btn.pack(side="right")

        entry = ctk.CTkEntry(
            row, textvariable=self.vars[key], height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 14))

    def _add_uv_row(self, parent):
        row = self._row_shell(parent)
        IconBadge(row, "UV", ICON_UV_BG, font_size=13).pack(side="left", padx=(0, 14))
        self._title_block(
            row, "uv Project Folder (where 'uv' runs from)", "Base folder for uv project",
            fixed_width=210, fixed_height=44)

        browse_btn = ctk.CTkButton(
            row, text="\U0001F4C1  Browse", width=110, height=36, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT,
            command=lambda: self._browse("uv_project_dir", "folder", None))
        browse_btn.pack(side="right")

        entry = ctk.CTkEntry(
            row, textvariable=self.vars["uv_project_dir"], height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 14))

    def _add_silence_row(self, parent):
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_SILENCE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Silence Duration (seconds)", "Duration of silence between sentences")
        text_frame.pack(side="left", fill="x", expand=True)

        NumberSpinner(row, self.vars["silence_duration"], step=0.5, minval=0.0).pack(side="right")

    def _add_toggle_row(self, parent):
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_KEEP_TEMP
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Keep temp files after run", "Keep temporary files after generation")
        text_frame.pack(side="left", fill="x", expand=True)

        self.toggle = ctk.CTkSwitch(
            row, text=self._toggle_text(bool(self.keep_temp_var.get())),
            variable=self.keep_temp_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12), command=self._on_toggle_changed)
        self.toggle.pack(side="right")

    def _toggle_text(self, keep_temp):
        return "ON (temp files kept)" if keep_temp else "OFF (temp files cleared)"

    def _on_toggle_changed(self):
        self.toggle.configure(text=self._toggle_text(bool(self.keep_temp_var.get())))

    # ---------- Actions ----------
    def _browse(self, key, kind, filetypes):
        current = self.vars[key].get()
        if kind == "folder":
            initialdir = current if os.path.isdir(current) else SCRIPT_DIR
            chosen = filedialog.askdirectory(title="Select Folder", initialdir=initialdir)
        else:
            initialdir = os.path.dirname(current) if os.path.isfile(current) else SCRIPT_DIR
            chosen = filedialog.askopenfilename(
                title="Select File", initialdir=initialdir, filetypes=filetypes)
        if chosen:
            self.vars[key].set(os.path.normpath(chosen))

    def on_reset_defaults(self):
        if not messagebox.askyesno(
                "Reset to Defaults",
                "Reset all fields to their default values? This does not save "
                "until you click 'Save Settings' or 'Save & Run'."):
            return
        for key in ("input_folder", "output_folder", "temp_dir", "model_path",
                    "speaker_path", "uv_project_dir"):
            self.vars[key].set(DEFAULT_SETTINGS[key])
        self.vars["silence_duration"].set(str(DEFAULT_SETTINGS["silence_duration"]))
        keep_temp = not DEFAULT_SETTINGS["clean_temp_after_run"]
        self.keep_temp_var.set(1 if keep_temp else 0)
        self._on_toggle_changed()

    def _collect_and_validate(self):
        try:
            silence = float(self.vars["silence_duration"].get())
            if silence < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid Value",
                "Silence Duration must be a positive number (e.g. 1.0).")
            return None

        data = {
            "input_folder": self.vars["input_folder"].get().strip(),
            "output_folder": self.vars["output_folder"].get().strip(),
            "temp_dir": self.vars["temp_dir"].get().strip(),
            "model_path": self.vars["model_path"].get().strip(),
            "speaker_path": self.vars["speaker_path"].get().strip(),
            "silence_duration": silence,
            "clean_temp_after_run": not bool(self.keep_temp_var.get()),
            "uv_project_dir": self.vars["uv_project_dir"].get().strip(),
        }

        for key in ("input_folder", "output_folder", "temp_dir", "model_path", "speaker_path",
                    "uv_project_dir"):
            if not data[key]:
                messagebox.showerror("Missing Value", f"'{key}' cannot be empty.")
                return None

        return data

    def on_save(self):
        data = self._collect_and_validate()
        if data is None:
            return
        save_settings(data)
        messagebox.showinfo("Saved", "Settings saved to settings.json")

    def on_save_and_run(self):
        data = self._collect_and_validate()
        if data is None:
            return
        save_settings(data)

        if not os.path.exists(RUN_SCRIPT_PATH):
            messagebox.showerror("Not Found", f"Could not find:\n{RUN_SCRIPT_PATH}")
            return

        uv_project_dir = data["uv_project_dir"]
        if not os.path.isdir(uv_project_dir):
            messagebox.showerror(
                "uv Project Folder Not Found",
                f"'{uv_project_dir}' does not exist.\n"
                "Set the correct 'uv Project Folder' (where you normally run 'uv run ...' from).")
            return

        try:
            # uv (and the venv it manages) lives in uv_project_dir, so we must run
            # `uv` from there, passing the absolute path of run_audiobook.py as the
            # script to execute. This mirrors:
            #   cd <uv_project_dir>
            #   uv run --no-sync python "C:\JP-Audiobook-Generator\run_audiobook.py"
            subprocess.Popen(
                ["uv", "run", "--no-sync", "python", RUN_SCRIPT_PATH],
                cwd=uv_project_dir,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            messagebox.showinfo(
                "Started", "run_audiobook.py has been started in a new console window.")
        except FileNotFoundError:
            messagebox.showerror(
                "uv not found",
                "Could not launch via 'uv'. Make sure 'uv' is installed and on PATH,\n"
                "or run run_audiobook.py manually from your existing environment.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start run_audiobook.py:\n{e}")


if __name__ == "__main__":
    app = SettingsApp()
    app.mainloop()
