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
    customtkinter, pillow, mutagen

Layout notes (2026-08 tab restructure):
    The window is split into a left sidebar (General / Metadata /
    Advanced) and a right content area. Only one page is visible at a
    time; switching pages just remaps a different frame into the grid -
    all three pages are built once at startup so field values aren't lost
    when you switch tabs.

    - General page  = path/model settings (previously the only page).
    - Advanced page = Sentence/Paragraph/Section Silence Duration
      + Max Chunk Length + Keep Temp Files.
    - Metadata page = Author / Book Title / Genre / auto chapter
      numbering / cover art, all persisted to settings.json. "Apply Tags
      to Output MP3s" (mp3_metadata.py, using mutagen) writes the actual
      ID3v2 tags onto the already-generated chapter MP3s in output_folder
      - it's a separate manual step from generation, since Save & Run
      launches run_audiobook.py in a detached console the GUI doesn't
      wait on.
"""

import os
import sys
import json
import glob
import queue
import re
import time
import threading
import subprocess

import customtkinter as ctk
from tkinter import filedialog, messagebox

from progress_window import ProgressWindow, default_log_path
from subtitle_window import SubtitleWindow
from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ENTRY_BORDER, COLOR_ENTRY_TEXT, COLOR_ACCENT, COLOR_ACCENT_HOVER,
    COLOR_TOGGLE_ON, COLOR_BTN_NEUTRAL_BORDER, COLOR_BTN_NEUTRAL_TEXT, IconBadge,
)

# mp3_metadata (mutagen-based tagging) is imported lazily inside
# on_apply_metadata_tags() rather than here, so a missing `mutagen`
# install (e.g. before running `uv sync` after this feature was added)
# doesn't crash the whole settings GUI on launch - only the Apply Tags
# button, with a clear error message telling you what to run.

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
    "speaker_path": r"C:\Irodori-TTS\seiyuu\ueshama.speaker.safetensors",
    "silence_duration_sentence": 1.0,
    "silence_duration_paragraph": 1.2,
    "silence_duration_section": 1.5,
    "clean_temp_after_run": True,
    "uv_project_dir": r"C:\Irodori-TTS",
    "author_name": "",
    "book_title": "",
    "genre": "Audiobook",
    "auto_number_chapters": True,
    "cover_art_path": "",
    "auto_tag_generated_files": False,
    "max_chunk_length": 100,
    # Per-speaker TTS tuning. These vary enough between trained speakers
    # that fixing them in code produced inconsistent results, so they are
    # settings rather than constants - see run_audiobook.py's matching
    # defaults dict, which must be kept in step with this one.
    "duration_scale": 1.2,
    "no_trim_tail": True,
    "seed_enabled": False,
    "seed_value": 20260906,
    # Output encoding.
    "mp3_mono": True,
    "mp3_bitrate": "96k",
    # Translation subtitles. See translate_pipeline.py.
    "auto_translate_after_run": False,
    "translation_backend": "vntl",
    "llama_server_url": "http://127.0.0.1:8080",
    # Used to start llama-server on demand when nothing is already listening,
    # so the GPU is only occupied while translation is actually running.
    "llama_server_exe": r"C:\llama.cpp\llama-server.exe",
    "llama_model_path": r"C:\llama.cpp\models\vntl-llama3-8b-v2-hf-q5_k_m.gguf",
}

# ---------------------------------------------------------------------------
# Design tokens (matches Documentation/mockup_GUI_20260817.png)
# ---------------------------------------------------------------------------
# The core palette (COLOR_BG, COLOR_CARD, COLOR_TITLE, etc.) and IconBadge
# now live in ui_common.py, shared with progress_window.py - see that
# module's docstring for why (avoiding a circular import). Only the tokens
# specific to this file's sidebar remain here.

# Sidebar nav specific tokens
COLOR_SIDEBAR = "#FFFFFF"
COLOR_SIDEBAR_BORDER = "#E7E7EC"
COLOR_NAV_SELECTED_BG = "#EDEBFC"
COLOR_NAV_TEXT = "#5A5A64"

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
ICON_KEEP_TEMP = ("\u2714", "#E6F8ED", "#2FB668")      # check
ICON_CHUNK_LENGTH = ("\U0001F4CF", "#FFF1E0", "#E08A2C")  # ruler
ICON_DURATION_SCALE = ("⏳", "#EDEBFC", "#6C5DD3")    # ⏳ hourglass (pacing)
ICON_TRIM_TAIL = ("✂", "#FCEAEA", "#D85A5A")         # ✂ scissors
ICON_SEED = ("\U0001F331", "#E6F8ED", "#2FB668")          # 🌱 seedling
ICON_CHANNELS = ("\U0001F3A7", "#E6F1FB", "#3378C9")      # 🎧 headphones
ICON_BITRATE = ("\U0001F4CA", "#FFF1E0", "#E08A2C")       # 📊 bar chart
ICON_TRANSLATE = ("\U0001F310", "#E6F1FB", "#3378C9")     # 🌐 globe
ICON_ENDPOINT = ("\U0001F517", "#EDEBFC", "#6C5DD3")      # 🔗 link

# (emoji, pastel_bg, icon_color) for the metadata cards
ICON_AUTHOR = ("\U0001F464", "#EDEBFC", "#6C5DD3")     # 👤 person
ICON_BOOK_TITLE = ("\U0001F4D5", "#FCEAEA", "#D85A5A")  # 📕 closed book
ICON_GENRE = ("\U0001F3F7", "#FFF3DD", "#C98A2E")       # 🏷 tag
ICON_TRACK_NUM = ("\U0001F522", "#E6F8ED", "#2FB668")   # 🔢 numbers
ICON_COVER_ART = ("\U0001F5BC", "#E6F1FB", "#3378C9")   # 🖼 picture
ICON_AUTO_TAG = ("\u26A1", "#FFF3DD", "#C98A2E")         # ⚡ lightning (automation)

# Sidebar nav icons
NAV_ICON_GENERAL = "\u2699"      # ⚙ gear
NAV_ICON_METADATA = "\U0001F3B5"  # 🎵 music note
NAV_ICON_ADVANCED = "\U0001F39A"  # 🎚 level slider


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
    # Migrate BEFORE merging in the defaults - see the matching comment in
    # run_audiobook.load_settings() for why the order matters.
    loaded = migrate_silence_settings(loaded)

    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded)
    return merged


def migrate_silence_settings(settings):
    """Backwards compatibility for settings.json files written before the
    per-kind silence durations existed. Kept deliberately identical to
    run_audiobook.migrate_silence_settings() - if one changes, change both.

    Older versions stored a single "silence_duration" (the 1x base unit)
    and produced longer gaps by repeating silence.wav 2x/3x in the concat
    list. The legacy value becomes the sentence duration, with paragraph
    and section derived as 2x and 3x it, so an upgraded config sounds
    exactly like it did before the person tunes anything."""
    legacy = settings.pop("silence_duration", None)
    if legacy is None:
        return settings

    try:
        legacy = float(legacy)
    except (TypeError, ValueError):
        return settings

    # round(): 0.8 * 3 is 2.4000000000000004 in binary floating point, and
    # that full value would end up rendered verbatim in the GUI's spinner.
    if "silence_duration_sentence" not in settings:
        settings["silence_duration_sentence"] = round(legacy, 3)
    if "silence_duration_paragraph" not in settings:
        settings["silence_duration_paragraph"] = round(legacy * 2, 3)
    if "silence_duration_section" not in settings:
        settings["silence_duration_section"] = round(legacy * 3, 3)
    return settings


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Live progress wiring for Save & Run (step 2 of progress_window_spec.md)
# ---------------------------------------------------------------------------

# Matches run_audiobook.py's own print() formatting exactly - see
# process_chapter() in run_audiobook.py. If that wording ever changes,
# these need updating too; a more robust alternative (a dedicated
# machine-parseable `PROGRESS n/total` line) is spec'd as step 3 but not
# implemented yet - these regexes are what step 2 has to work with in the
# meantime, and already give real per-chunk progress for free.
_PROCESSING_LINE_RE = re.compile(r">>> Processing: (\S+)")
_CHUNK_LINE_RE = re.compile(r"Generating chunk (\d+)/(\d+)")


def _read_process_output(process, log_queue):
    """Runs in a background thread (started from on_save_and_run): reads
    run_audiobook.py's stdout line by line and hands each one to the main
    thread via log_queue, since Tkinter widgets are only safe to touch
    from the main thread. Puts None as a sentinel once the pipe closes -
    the process has finished writing output, though it may not have
    fully exited yet (see SettingsApp._finish_run, which waits on the
    actual return code before deciding completed vs. failed)."""
    try:
        for line in process.stdout:
            log_queue.put(line.rstrip("\n"))
    except Exception as e:
        log_queue.put(f"[GUI] Lost connection to run_audiobook.py's output: {e}")
    finally:
        log_queue.put(None)


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------
# IconBadge moved to ui_common.py (shared with progress_window.py).


class NumberSpinner(ctk.CTkFrame):
    """A small numeric entry with up/down stepper buttons (mimics the
    mockup's silence-duration spinner; ttk/CTk have no native spinbox)."""

    def __init__(self, parent, textvariable, step=0.5, minval=0.0, integer=False, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.var = textvariable
        self.step = step
        self.minval = minval
        self.integer = integer

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

    def _fmt(self, value):
        if self.integer:
            return str(int(value))
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
        self.minsize(860, 480)
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
            "speaker_path": ctk.StringVar(value=self.settings["speaker_path"]),
            "silence_duration_sentence": ctk.StringVar(
                value=str(self.settings["silence_duration_sentence"])),
            "silence_duration_paragraph": ctk.StringVar(
                value=str(self.settings["silence_duration_paragraph"])),
            "silence_duration_section": ctk.StringVar(
                value=str(self.settings["silence_duration_section"])),
            "uv_project_dir": ctk.StringVar(value=self.settings["uv_project_dir"]),
            "max_chunk_length": ctk.StringVar(
                value=str(self.settings["max_chunk_length"])),
            "duration_scale": ctk.StringVar(
                value=str(self.settings["duration_scale"])),
            "seed_value": ctk.StringVar(
                value=str(self.settings["seed_value"])),
            "mp3_bitrate": ctk.StringVar(
                value=str(self.settings["mp3_bitrate"])),
            "llama_server_url": ctk.StringVar(
                value=str(self.settings["llama_server_url"])),
            "llama_server_exe": ctk.StringVar(
                value=str(self.settings["llama_server_exe"])),
            "llama_model_path": ctk.StringVar(
                value=str(self.settings["llama_model_path"])),
        }
        self.translation_backend_var = ctk.StringVar(
            value=str(self.settings["translation_backend"]))

        initial_keep = not bool(self.settings.get("clean_temp_after_run", True))
        self.keep_temp_var = ctk.IntVar(value=1 if initial_keep else 0)
        self.no_trim_tail_var = ctk.IntVar(
            value=1 if self.settings.get("no_trim_tail", True) else 0)
        self.seed_enabled_var = ctk.IntVar(
            value=1 if self.settings.get("seed_enabled", False) else 0)
        self.mp3_mono_var = ctk.IntVar(
            value=1 if self.settings.get("mp3_mono", True) else 0)
        self.auto_translate_var = ctk.IntVar(
            value=1 if self.settings.get("auto_translate_after_run", False) else 0)

        # The Subtitle Generation Tool window, if it is open. Kept so a
        # second click raises the existing one rather than opening a rival
        # window that would fight over the same GPU.
        self._subtitle_window = None

        self.metadata_vars = {
            "author_name": ctk.StringVar(value=self.settings.get("author_name", "")),
            "book_title": ctk.StringVar(value=self.settings.get("book_title", "")),
            "genre": ctk.StringVar(value=self.settings.get("genre", "Audiobook")),
            "cover_art_path": ctk.StringVar(value=self.settings.get("cover_art_path", "")),
        }
        self.auto_number_var = ctk.IntVar(
            value=1 if self.settings.get("auto_number_chapters", True) else 0)
        self.auto_tag_var = ctk.IntVar(
            value=1 if self.settings.get("auto_tag_generated_files", False) else 0)

        self.pages = {}
        self.nav_buttons = {}
        self.current_page = "general"

        # State for whatever Save & Run has in flight, if anything - see
        # on_save_and_run/_drain_run_queue/_finish_run/_cancel_run. All
        # reset fresh at the top of on_save_and_run for each new run.
        self._run_process = None
        self._run_queue = None
        self._run_window = None
        self._run_cancelled = False
        self._run_total_chapters = 0
        self._run_current_chapter = 0
        self._run_current_chapter_name = ""
        self._run_completed_chapters = 0

        self._build_ui()

    def _set_initial_geometry(self):
        """Size the window relative to the actual screen (CustomTkinter is
        DPI-aware, so these are already logical/scaled pixels - no manual
        DPI math needed) and cap it so it always fits, then center it."""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        desired_w, desired_h = 980, 820
        # Leave headroom for the taskbar and window chrome.
        max_h = max(480, screen_h - 120)
        max_w = max(860, screen_w - 120)

        w = min(desired_w, max_w)
        h = min(desired_h, max_h)
        x = (screen_w - w) // 2
        y = max(0, (screen_h - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- UI construction ----------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        # Content area: all three pages live in the same grid cell and are
        # switched with tkraise() so field values survive tab switches.
        content_container = ctk.CTkFrame(self, fg_color="transparent")
        content_container.grid(row=0, column=1, sticky="nsew", padx=(20, 28), pady=24)
        content_container.grid_rowconfigure(0, weight=1)
        content_container.grid_columnconfigure(0, weight=1)

        general_page = ctk.CTkScrollableFrame(
            content_container, fg_color="transparent",
            scrollbar_button_color=COLOR_BG, scrollbar_button_hover_color="#D8D8DE")
        general_page.grid(row=0, column=0, sticky="nsew")
        self._build_general_page(general_page)
        self.pages["general"] = general_page

        metadata_page = ctk.CTkScrollableFrame(
            content_container, fg_color="transparent",
            scrollbar_button_color=COLOR_BG, scrollbar_button_hover_color="#D8D8DE")
        metadata_page.grid(row=0, column=0, sticky="nsew")
        self._build_metadata_page(metadata_page)
        self.pages["metadata"] = metadata_page

        advanced_page = ctk.CTkScrollableFrame(
            content_container, fg_color="transparent",
            scrollbar_button_color=COLOR_BG, scrollbar_button_hover_color="#D8D8DE")
        advanced_page.grid(row=0, column=0, sticky="nsew")
        self._build_advanced_page(advanced_page)
        self.pages["advanced"] = advanced_page

        # Bottom action bar. Reset to Defaults now lives in the sidebar
        # itself (centered under the nav items) rather than here, so this
        # row only spans the content column and holds the right-aligned
        # Save/Run/Close actions.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=1, column=1, sticky="ew", padx=(20, 28), pady=(0, 20))

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

        # Packed right-to-left, so Export is placed before Import to leave
        # Import sitting to its left.
        ctk.CTkButton(
            btn_row, text="Export Settings", width=130, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F1F0FC", border_width=1,
            border_color=COLOR_ACCENT, text_color=COLOR_ACCENT,
            command=self.on_export_settings,
        ).pack(side="right", padx=(0, 10))

        ctk.CTkButton(
            btn_row, text="Import Settings", width=130, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F1F0FC", border_width=1,
            border_color=COLOR_ACCENT, text_color=COLOR_ACCENT,
            command=self.on_import_settings,
        ).pack(side="right", padx=(0, 10))

        self._show_page("general")

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(
            self, width=170, fg_color=COLOR_SIDEBAR, corner_radius=0,
            border_width=0, border_color=COLOR_SIDEBAR_BORDER)
        sidebar.grid(row=0, rowspan=2, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        nav_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav_frame.pack(fill="x", padx=12, pady=(24, 0))

        self._add_nav_button(nav_frame, "general", NAV_ICON_GENERAL, "General")
        self._add_nav_button(nav_frame, "metadata", NAV_ICON_METADATA, "Metadata")
        self._add_nav_button(nav_frame, "advanced", NAV_ICON_ADVANCED, "Advanced")

        # Reset to Defaults sits at the bottom of the sidebar, centered
        # within the sidebar's own width (not the whole window) since it's
        # visually grouped with navigation rather than the Save/Run/Close
        # actions on the right.
        ctk.CTkButton(
            sidebar, text="Reset to Defaults", width=146, height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F0F0F3", border_width=1,
            border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
            command=self.on_reset_defaults,
        ).pack(side="bottom", pady=(0, 20))

    def _add_nav_button(self, parent, key, icon, label):
        btn = ctk.CTkButton(
            parent, text=f"{icon}   {label}", anchor="w", height=38, corner_radius=8,
            fg_color="transparent", hover_color="#F5F4FC",
            text_color=COLOR_NAV_TEXT, font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda k=key: self._show_page(k))
        btn.pack(fill="x", pady=(0, 4))
        self.nav_buttons[key] = btn

    def _show_page(self, key):
        # NOTE: tkraise() alone proved unreliable across three stacked
        # CTkScrollableFrame widgets (the last-built page kept rendering on
        # top regardless of which one was raised). Explicitly mapping only
        # the selected page with grid() and unmapping the rest with
        # grid_remove() sidesteps z-order entirely - only one page is ever
        # actually placed in the grid at a time.
        for k, page in self.pages.items():
            if k == key:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()
        for k, btn in self.nav_buttons.items():
            selected = (k == key)
            btn.configure(
                fg_color=COLOR_NAV_SELECTED_BG if selected else "transparent",
                text_color=COLOR_ACCENT if selected else COLOR_NAV_TEXT)
        self.current_page = key

    def _page_header(self, parent, title, subtitle):
        ctk.CTkLabel(parent, text=title, text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=24, weight="bold"),
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(parent, text=subtitle,
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=13),
                     anchor="w").pack(fill="x", pady=(2, 18))

    # ---------- General page ----------
    def _build_general_page(self, parent):
        self._page_header(
            parent, "General Settings",
            "Configure paths and basic preferences for the audiobook generation process.")

        paths_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                                   border_width=1, border_color=COLOR_CARD_BORDER)
        paths_card.pack(fill="x")

        self._add_path_row(paths_card, *ICON_INPUT, "Input Folder",
                            "Folder containing input chapters", "input_folder", "folder")
        self._add_path_row(paths_card, *ICON_OUTPUT, "Output Folder",
                            "Folder to save generated MP3 files", "output_folder", "folder")
        self._add_path_row(paths_card, *ICON_TEMP, "Temp Folder",
                            "Folder for temporary files", "temp_dir", "folder")
        self._add_path_row(paths_card, *ICON_SPEAKER, "Speaker Path",
                            "Path to the speaker (.safetensors)", "speaker_path", "file",
                            filetypes=[("SafeTensors", "*.safetensors"), ("All files", "*.*")])
        self._add_uv_row(paths_card)

    # ---------- Advanced page ----------
    def _build_advanced_page(self, parent):
        self._page_header(
            parent, "Advanced Settings",
            "Fine-tune generation behavior.")

        prefs_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                                   border_width=1, border_color=COLOR_CARD_BORDER)
        prefs_card.pack(fill="x")

        self._add_silence_row(
            prefs_card, "silence_duration_sentence",
            "Sentence Silence (seconds)",
            "Gap between sentences within a paragraph")
        self._add_silence_row(
            prefs_card, "silence_duration_paragraph",
            "Paragraph Silence (seconds)",
            "Gap between paragraphs, and before the first line of a chapter")
        self._add_silence_row(
            prefs_card, "silence_duration_section",
            "Section Silence (seconds)",
            "Gap between sections (text separated by a blank line)")
        self._add_chunk_length_row(prefs_card)
        self._add_toggle_row(prefs_card)

        # TTS tuning. Every speaker embedding responds differently to these,
        # so they are per-preset rather than fixed in run_audiobook.py -
        # export a preset per speaker/book and import it before a run.
        ctk.CTkLabel(parent, text="TTS Tuning", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=15, weight="bold"),
                     anchor="w").pack(fill="x", pady=(20, 8))
        tts_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                                border_width=1, border_color=COLOR_CARD_BORDER)
        tts_card.pack(fill="x")

        self._add_duration_scale_row(tts_card)
        self._add_no_trim_tail_row(tts_card)
        self._add_seed_row(tts_card)

        ctk.CTkLabel(parent, text="Output Encoding", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=15, weight="bold"),
                     anchor="w").pack(fill="x", pady=(20, 8))
        mp3_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                                border_width=1, border_color=COLOR_CARD_BORDER)
        mp3_card.pack(fill="x")

        self._add_channels_row(mp3_card)
        self._add_bitrate_row(mp3_card)

        # Translation runs over the finished sync.json, so it belongs after
        # generation rather than inside it - see translate_pipeline.py.
        ctk.CTkLabel(parent, text="Translation Subtitles", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=15, weight="bold"),
                     anchor="w").pack(fill="x", pady=(20, 8))
        tr_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                               border_width=1, border_color=COLOR_CARD_BORDER)
        tr_card.pack(fill="x")

        self._add_auto_translate_row(tr_card)
        self._add_backend_row(tr_card)
        self._add_endpoint_row(tr_card)
        self._add_path_row(
            tr_card, *ICON_MODEL, "llama-server path",
            "Started automatically when nothing is listening, and stopped again "
            "when translation finishes",
            "llama_server_exe", "file",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        self._add_path_row(
            tr_card, *ICON_SPEAKER, "Translation model (GGUF)",
            "The VNTL model llama-server loads",
            "llama_model_path", "file",
            filetypes=[("GGUF", "*.gguf"), ("All files", "*.*")])

        generate_card = ctk.CTkFrame(parent, fg_color="transparent")
        generate_card.pack(fill="x", pady=(16, 0))

        self.translate_button = ctk.CTkButton(
            generate_card, text="\U0001F310  Generate Subtitle", height=40,
            corner_radius=8, fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            text_color="white", font=ctk.CTkFont(size=13, weight="bold"),
            command=self.on_generate_subtitles)
        self.translate_button.pack(side="left")
        self._sync_translate_button()

    def _add_auto_translate_row(self, parent):
        """Runs once at the end of a whole run, not per chapter like tagging.
        A local translation model and Irodori-TTS would otherwise contend for
        the same 8GB card, and the book-level glossary is better applied in
        one pass over a finished book."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_TRANSLATE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))

        self.auto_translate_switch = ctk.CTkSwitch(
            row, text=self._auto_translate_text(bool(self.auto_translate_var.get())),
            variable=self.auto_translate_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12), command=self._on_auto_translate_changed)
        self.auto_translate_switch.pack(side="right")

        text_frame = self._title_block(
            row, "Auto-generate after run",
            "Writes an .srt per chapter once the whole book has finished")
        text_frame.pack(side="left", fill="x", expand=True)

    def _auto_translate_text(self, enabled):
        return "ON (after every run)" if enabled else "OFF (manual only)"

    def _on_auto_translate_changed(self):
        self.auto_translate_switch.configure(
            text=self._auto_translate_text(bool(self.auto_translate_var.get())))
        self._sync_translate_button()

    def _sync_translate_button(self):
        """Auto-generate ON means translation already happens at the end of
        every run, so the manual button is greyed out - the same reasoning as
        Apply Tags under auto-tagging. Turning it OFF hands the button back."""
        button = getattr(self, "translate_button", None)
        if button is None:
            return      # called while the page is still being built
        if self.auto_translate_var.get():
            button.configure(state="disabled", fg_color="#D3D3D3",
                             hover_color="#D3D3D3", text_color=COLOR_SUBTITLE)
        else:
            button.configure(state="normal", fg_color=COLOR_ACCENT,
                             hover_color=COLOR_ACCENT_HOVER, text_color="white")

    def _add_backend_row(self, parent):
        """'identity' hands the Japanese back untranslated - the control case
        for checking the artifact and player round-trip without a model in
        the loop. 'vntl' is the real one and needs llama-server running."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_TRANSLATE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))

        ctk.CTkOptionMenu(
            row, variable=self.translation_backend_var,
            values=["vntl", "identity"], width=130, height=36, corner_radius=8,
            fg_color="white", button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_ENTRY_TEXT,
            font=ctk.CTkFont(size=12)).pack(side="right")

        text_frame = self._title_block(
            row, "Translation backend",
            "vntl = local VNTL-Llama3 via llama-server; "
            "identity = passthrough, for testing the pipeline")
        text_frame.pack(side="left", fill="x", expand=True)

    def _add_endpoint_row(self, parent):
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_ENDPOINT
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))

        ctk.CTkEntry(
            row, textvariable=self.vars["llama_server_url"], width=200, height=36,
            corner_radius=8, border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white").pack(side="right")

        text_frame = self._title_block(
            row, "llama-server URL",
            "Where the local model is listening. Only used by the vntl backend")
        text_frame.pack(side="left", fill="x", expand=True)

    # ---------- Metadata page ----------
    def _build_metadata_page(self, parent):
        self._page_header(
            parent, "Metadata Settings",
            "Tag chapters so Spotify groups them as one album.")

        meta_card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                                  border_width=1, border_color=COLOR_CARD_BORDER)
        meta_card.pack(fill="x")

        self._add_text_row(meta_card, *ICON_AUTHOR, "Author Name",
                            "Written to Artist / Album Artist tags on every chapter",
                            self.metadata_vars["author_name"], placeholder="e.g. Haruki Murakami")
        self._add_text_row(meta_card, *ICON_BOOK_TITLE, "Book Title",
                            "Written to the Album tag - identical across all chapters",
                            self.metadata_vars["book_title"], placeholder="e.g. Norwegian Wood")
        self._add_text_row(meta_card, *ICON_GENRE, "Genre",
                            "Written to the Genre tag",
                            self.metadata_vars["genre"], placeholder="Audiobook")
        self.auto_number_switch = self._add_switch_row(
            meta_card, *ICON_TRACK_NUM, "Auto-number chapters",
            "Sets the Track Number tag from the chapter's file name",
            self.auto_number_var,
            on_text="(Sets the track number based on chapter filename)",
            off_text="(set track number manually)")
        self.auto_tag_switch = self._add_switch_row(
            meta_card, *ICON_AUTO_TAG, "Auto-tag generated files",
            "Sets the metadata of output files automatically",
            self.auto_tag_var,
            on_text="(Tag the output mp3 files automatically upon generation)",
            off_text="(Tag the output mp3 files manually)",
            command=self._on_auto_tag_changed)
        self._add_cover_art_row(meta_card)

        apply_card = ctk.CTkFrame(parent, fg_color="transparent")
        apply_card.pack(fill="x", pady=(16, 0))

        self.apply_tags_button = ctk.CTkButton(
            apply_card, text="\U0001F3F7  Apply Tags to Output MP3s", height=40,
            corner_radius=8, fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            text_color="white", font=ctk.CTkFont(size=13, weight="bold"),
            command=self.on_apply_metadata_tags,
        )
        self.apply_tags_button.pack(side="left")

        self.metadata_status_label = ctk.CTkLabel(
            apply_card, text="", text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=12),
            anchor="w", justify="left")
        self.metadata_status_label.pack(side="left", padx=(14, 0))

        # Reflect the loaded "Auto-tag generated files" state on the Apply
        # button right away (disabled if auto-tagging is already ON, since
        # tagging then happens automatically at the end of generation).
        self._on_auto_tag_changed()

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
        # When a fixed width is given, wrap title/subtitle text to that width
        # instead of letting it overflow past the column - this is what
        # keeps every row's description text from spilling into the
        # input/switch column on the right.
        wrap_len = max(fixed_width - 6, 40) if fixed_width else 0
        ctk.CTkLabel(text_frame, text=title, text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
                     justify="left", wraplength=wrap_len).pack(anchor="w", fill="x")
        ctk.CTkLabel(text_frame, text=subtitle, text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=11), anchor="w", justify="left",
                     wraplength=wrap_len).pack(anchor="w", fill="x")
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
            row, "uv Project Folder", "Base folder for uv project",
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

    def _add_silence_row(self, parent, var_key, title, subtitle):
        """One spinner row on the Advanced page. There are three of these -
        sentence, paragraph and section - each feeding its own duration into
        settings.json. run_audiobook.py renders one silence wav per kind and
        picks between them via text_pipeline's silence_kind_for()."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_SILENCE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(row, title, subtitle)
        text_frame.pack(side="left", fill="x", expand=True)

        NumberSpinner(row, self.vars[var_key], step=0.5, minval=0.0).pack(side="right")

    def _add_chunk_length_row(self, parent):
        """Soft cap (characters) on how much text text_pipeline.merge_units()
        packs into one TTS chunk before starting a new one - see
        run_audiobook.py's MAX_CHUNK_LENGTH. The hard limit it only crosses
        to avoid cutting a sentence off mid-way is always this + 30
        characters, not separately configurable."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_CHUNK_LENGTH
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Max Chunk Length (characters)",
            "Sentences are merged into one TTS chunk up to this length "
            "(hard limit: this + 30 characters)")
        text_frame.pack(side="left", fill="x", expand=True)

        NumberSpinner(
            row, self.vars["max_chunk_length"], step=10, minval=20,
            integer=True).pack(side="right")

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

    # ---------- Advanced page: TTS tuning rows ----------
    def _add_duration_scale_row(self, parent):
        """infer.py --duration-scale. Multiplies the duration v4-Small
        predicts for each chunk: above 1.0 slows delivery, below speeds it
        up. Every speaker embedding lands differently, which is why this is
        a per-preset value rather than a constant."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_DURATION_SCALE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Duration Scale",
            "Scales the predicted length of every chunk "
            "(above 1.0 = slower delivery, below = faster)")
        text_frame.pack(side="left", fill="x", expand=True)

        NumberSpinner(row, self.vars["duration_scale"], step=0.1,
                      minval=0.1).pack(side="right")

    def _add_no_trim_tail_row(self, parent):
        """infer.py --no-trim-tail. ON passes the flag, keeping the trailing
        region infer.py's tail heuristic would otherwise cut. The heuristic
        was written for v2's fixed 30-second outputs; on short chunks it can
        clip the last syllable, but leaving it off adds a little dead air."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_TRIM_TAIL
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Disable tail trimming (--no-trim-tail)",
            "ON keeps the end of every chunk intact; OFF lets infer.py trim "
            "trailing near-silence")
        text_frame.pack(side="left", fill="x", expand=True)

        self.no_trim_tail_switch = ctk.CTkSwitch(
            row, text=self._no_trim_tail_text(bool(self.no_trim_tail_var.get())),
            variable=self.no_trim_tail_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12), command=self._on_no_trim_tail_changed)
        self.no_trim_tail_switch.pack(side="right")

    def _no_trim_tail_text(self, enabled):
        return "ON (flag passed)" if enabled else "OFF (infer.py trims)"

    def _on_no_trim_tail_changed(self):
        self.no_trim_tail_switch.configure(
            text=self._no_trim_tail_text(bool(self.no_trim_tail_var.get())))

    def _add_seed_row(self, parent):
        """infer.py --seed. OFF (the default) lets infer.py draw a fresh
        random seed per chunk. ON pins one seed for the whole run, which
        makes a run reproducible but has been observed to destabilise some
        speakers - one unlucky draw then affects every chunk instead of
        averaging out. The value box only appears while the switch is ON."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_SEED
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))

        # The switch and its value box share one container that is packed
        # BEFORE the title block. Tk hands out space in packing order, so a
        # title block packed first with expand=True eats the whole row and
        # leaves a right-packed control with nothing to draw into - which is
        # exactly what happened here when this row's subtitle got long
        # enough. _add_path_row uses the same ordering for the same reason.
        # Grouping both controls also keeps the entry's show/hide independent
        # of the row's packing order.
        controls = ctk.CTkFrame(row, fg_color="transparent")
        controls.pack(side="right")

        self.seed_switch = ctk.CTkSwitch(
            controls, text=self._seed_text(bool(self.seed_enabled_var.get())),
            variable=self.seed_enabled_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12), command=self._on_seed_toggled)
        self.seed_switch.pack(side="right")

        # Packed/unpacked by _on_seed_toggled, so it only takes up space
        # while the switch is ON. Sits to the left of the switch.
        self.seed_entry = ctk.CTkEntry(
            controls, textvariable=self.vars["seed_value"], width=110, height=36,
            corner_radius=8, border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")

        text_frame = self._title_block(
            row, "Fixed sampling seed",
            "OFF uses a random seed per chunk; ON pins one for the whole run")
        text_frame.pack(side="left", fill="x", expand=True)
        self._on_seed_toggled()

    def _seed_text(self, enabled):
        return "ON (fixed)" if enabled else "OFF (random)"

    def _on_seed_toggled(self):
        enabled = bool(self.seed_enabled_var.get())
        self.seed_switch.configure(text=self._seed_text(enabled))
        if enabled:
            # Re-fill an emptied box with the default rather than leaving the
            # user to guess what a valid seed looks like.
            if not self.vars["seed_value"].get().strip():
                self.vars["seed_value"].set(str(DEFAULT_SETTINGS["seed_value"]))
            self.seed_entry.pack(side="right", padx=(0, 12))
        else:
            self.seed_entry.pack_forget()

    # ---------- Advanced page: output encoding rows ----------
    def _add_channels_row(self, parent):
        """ffmpeg -ac. Irodori-TTS renders mono, so stereo duplicates the
        same signal into both channels and halves the bits available to the
        content - mono is the default for that reason."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_CHANNELS
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "Output channels",
            "Mono matches what the TTS actually renders. Stereo duplicates it "
            "into both channels")
        text_frame.pack(side="left", fill="x", expand=True)

        self.mp3_mono_switch = ctk.CTkSwitch(
            row, text=self._mono_text(bool(self.mp3_mono_var.get())),
            variable=self.mp3_mono_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12), command=self._on_mono_changed)
        self.mp3_mono_switch.pack(side="right")

    def _mono_text(self, mono):
        return "MONO" if mono else "STEREO"

    def _on_mono_changed(self):
        self.mp3_mono_switch.configure(
            text=self._mono_text(bool(self.mp3_mono_var.get())))

    def _add_bitrate_row(self, parent):
        """ffmpeg -b:a. Constant bitrate on purpose: build_sync_data() writes
        per-chunk offsets that a player seeks to, and VBR seeking leans on a
        100-entry table that is far coarser than one chunk."""
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_BITRATE
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(
            row, "MP3 Bitrate",
            "Constant bitrate for the stitched chapter, e.g. 96k. "
            "Speech needs far less than music")
        text_frame.pack(side="left", fill="x", expand=True)

        ctk.CTkEntry(
            row, textvariable=self.vars["mp3_bitrate"], width=110, height=36,
            corner_radius=8, border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white").pack(side="right")

    # ---------- Metadata page row helpers (UI-only) ----------
    def _add_text_row(self, parent, glyph, pastel_bg, icon_color, title, subtitle,
                       string_var, placeholder=""):
        row = self._row_shell(parent)
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        self._title_block(row, title, subtitle, fixed_width=220, fixed_height=56)

        entry = ctk.CTkEntry(
            row, textvariable=string_var, placeholder_text=placeholder, height=36,
            corner_radius=8, border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")
        entry.pack(side="left", fill="x", expand=True)

    def _add_checkbox_row(self, parent, glyph, pastel_bg, icon_color, title, subtitle,
                           int_var):
        # NOTE: kept for potential future use, but the Metadata tab's
        # "Auto-number chapters" switched from this checkbox style to
        # _add_switch_row() below, to stay visually consistent with the
        # toggle switches used elsewhere (e.g. Advanced tab).
        row = self._row_shell(parent)
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        text_frame = self._title_block(row, title, subtitle)
        text_frame.pack(side="left", fill="x", expand=True)

        ctk.CTkCheckBox(
            row, text="", variable=int_var, onvalue=1, offvalue=0,
            width=24, checkbox_width=22, checkbox_height=22, corner_radius=6,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            border_color=COLOR_ENTRY_BORDER).pack(side="right")

    def _add_switch_row(self, parent, glyph, pastel_bg, icon_color, title, subtitle,
                         int_var, on_text, off_text, command=None):
        """Same visual pattern as _add_toggle_row (Keep temp files, on the
        Advanced tab): an icon badge, a title/subtitle block, and a switch
        whose own label text changes between on_text/off_text depending on
        state. Generalized here so the Metadata tab's two switches
        (Auto-number chapters, Auto-tag generated files) share the exact
        same look. Uses the same fixed-width title column as the text rows
        above/below it so the switch itself starts at the same left edge as
        every input box on this tab, rather than floating at the far right
        of the card. `command`, if given, runs after the label updates -
        used by Auto-tag generated files to enable/disable the Apply button.
        """
        row = self._row_shell(parent)
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        self._title_block(row, title, subtitle, fixed_width=220, fixed_height=56)

        switch = ctk.CTkSwitch(
            row, text=on_text if int_var.get() else off_text,
            variable=int_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12),
            command=lambda: self._on_switch_toggled(switch, int_var, on_text, off_text, command))
        switch.pack(side="left")
        return switch

    def _on_switch_toggled(self, switch, int_var, on_text, off_text, extra_command):
        switch.configure(text=on_text if int_var.get() else off_text)
        if extra_command:
            extra_command()

    def _on_auto_tag_changed(self):
        """Auto-tag generated files ON -> tagging happens automatically
        after each chapter inside run_audiobook.py, so the manual Apply
        Tags button is greyed out and disabled (avoids double-tagging /
        confusion about which metadata actually landed). OFF -> button is
        enabled again."""
        if self.auto_tag_var.get():
            self.apply_tags_button.configure(
                state="disabled", fg_color="#D3D3D3", hover_color="#D3D3D3",
                text_color=COLOR_SUBTITLE)
        else:
            self.apply_tags_button.configure(
                state="normal", fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                text_color="white")

    def _add_cover_art_row(self, parent):
        row = self._row_shell(parent)
        glyph, pastel_bg, icon_color = ICON_COVER_ART
        IconBadge(row, glyph, pastel_bg, text_color=icon_color, font_size=16).pack(
            side="left", padx=(0, 14))
        self._title_block(row, "Cover Art", "Embedded as artwork in every chapter's MP3",
                           fixed_width=220, fixed_height=56)

        entry = ctk.CTkEntry(
            row, textvariable=self.metadata_vars["cover_art_path"],
            placeholder_text="No image selected", height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 14))

        browse_btn = ctk.CTkButton(
            row, text="\U0001F4C1  Browse", width=110, height=36, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT,
            command=self._browse_cover_art)
        browse_btn.pack(side="right")

    def _browse_cover_art(self):
        current = self.metadata_vars["cover_art_path"].get()
        initialdir = os.path.dirname(current) if os.path.isfile(current) else SCRIPT_DIR
        chosen = filedialog.askopenfilename(
            title="Select Cover Art", initialdir=initialdir,
            filetypes=[("Image files", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
        if chosen:
            self.metadata_vars["cover_art_path"].set(os.path.normpath(chosen))

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
                "until you click 'Export Settings' or 'Save & Run'."):
            return
        # _apply_settings_to_fields() fills every field from DEFAULT_SETTINGS
        # and re-syncs all the switch labels, so resetting is just an import
        # of the defaults.
        self._apply_settings_to_fields(dict(DEFAULT_SETTINGS))
        self.metadata_status_label.configure(text="")

    def _collect_and_validate(self):
        silences = {}
        for key, label in (("silence_duration_sentence", "Sentence Silence"),
                           ("silence_duration_paragraph", "Paragraph Silence"),
                           ("silence_duration_section", "Section Silence")):
            try:
                value = float(self.vars[key].get())
                if value < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid Value",
                    f"{label} must be a positive number (e.g. 1.0).")
                return None
            silences[key] = value

        try:
            max_chunk_length = int(float(self.vars["max_chunk_length"].get()))
            if max_chunk_length < 20:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid Value",
                "Max Chunk Length must be a whole number of at least 20 characters.")
            return None

        try:
            duration_scale = float(self.vars["duration_scale"].get())
            if duration_scale <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid Value",
                "Duration Scale must be a positive number (e.g. 1.2).")
            return None

        # Only validated when the switch is ON - an unused seed box should
        # never be able to block a save.
        seed_value = DEFAULT_SETTINGS["seed_value"]
        if self.seed_enabled_var.get():
            try:
                seed_value = int(float(self.vars["seed_value"].get()))
                if seed_value < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid Value",
                    "Sampling seed must be a non-negative whole number "
                    "(e.g. 20260906), or turn the seed switch off.")
                return None

        mp3_bitrate = self._normalize_bitrate(self.vars["mp3_bitrate"].get())
        if mp3_bitrate is None:
            messagebox.showerror(
                "Invalid Value",
                "MP3 Bitrate must be between 32k and 320k, written as "
                "'96k' or '96'.")
            return None

        data = {
            "input_folder": self.vars["input_folder"].get().strip(),
            "output_folder": self.vars["output_folder"].get().strip(),
            "temp_dir": self.vars["temp_dir"].get().strip(),
            "speaker_path": self.vars["speaker_path"].get().strip(),
            "silence_duration_sentence": silences["silence_duration_sentence"],
            "silence_duration_paragraph": silences["silence_duration_paragraph"],
            "silence_duration_section": silences["silence_duration_section"],
            "clean_temp_after_run": not bool(self.keep_temp_var.get()),
            "uv_project_dir": self.vars["uv_project_dir"].get().strip(),
            "author_name": self.metadata_vars["author_name"].get().strip(),
            "book_title": self.metadata_vars["book_title"].get().strip(),
            "genre": self.metadata_vars["genre"].get().strip(),
            "auto_number_chapters": bool(self.auto_number_var.get()),
            "cover_art_path": self.metadata_vars["cover_art_path"].get().strip(),
            "auto_tag_generated_files": bool(self.auto_tag_var.get()),
            "max_chunk_length": max_chunk_length,
            "duration_scale": duration_scale,
            "no_trim_tail": bool(self.no_trim_tail_var.get()),
            "seed_enabled": bool(self.seed_enabled_var.get()),
            "seed_value": seed_value,
            "mp3_mono": bool(self.mp3_mono_var.get()),
            "mp3_bitrate": mp3_bitrate,
            "auto_translate_after_run": bool(self.auto_translate_var.get()),
            "translation_backend": self.translation_backend_var.get(),
            "llama_server_url": self.vars["llama_server_url"].get().strip(),
            "llama_server_exe": self.vars["llama_server_exe"].get().strip(),
            "llama_model_path": self.vars["llama_model_path"].get().strip(),
        }

        for key in ("input_folder", "output_folder", "temp_dir", "speaker_path",
                    "uv_project_dir"):
            if not data[key]:
                messagebox.showerror("Missing Value", f"'{key}' cannot be empty.")
                return None

        return data

    @staticmethod
    def _normalize_bitrate(raw):
        """Accepts '96k', '96K' or '96' and returns a canonical '96k'.
        Returns None if it isn't a usable CBR value, which the caller turns
        into an error dialog."""
        text = (raw or "").strip().lower().rstrip("k").strip()
        try:
            kbps = int(float(text))
        except ValueError:
            return None
        if not 32 <= kbps <= 320:
            return None
        return f"{kbps}k"

    def _apply_settings_to_fields(self, data):
        """Pushes a settings dict into every widget variable. Used by Import
        Settings; missing keys fall back to DEFAULT_SETTINGS so an older or
        hand-trimmed preset still loads cleanly."""
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)

        for key in ("input_folder", "output_folder", "temp_dir",
                    "speaker_path", "uv_project_dir"):
            self.vars[key].set(merged[key])
        for key in ("silence_duration_sentence", "silence_duration_paragraph",
                    "silence_duration_section", "max_chunk_length",
                    "duration_scale", "seed_value", "mp3_bitrate",
                    "llama_server_url", "llama_server_exe", "llama_model_path"):
            self.vars[key].set(str(merged[key]))
        self.translation_backend_var.set(str(merged["translation_backend"]))

        self.keep_temp_var.set(0 if merged["clean_temp_after_run"] else 1)
        self.no_trim_tail_var.set(1 if merged["no_trim_tail"] else 0)
        self.seed_enabled_var.set(1 if merged["seed_enabled"] else 0)
        self.mp3_mono_var.set(1 if merged["mp3_mono"] else 0)
        self.auto_translate_var.set(1 if merged["auto_translate_after_run"] else 0)

        for key in ("author_name", "book_title", "genre", "cover_art_path"):
            self.metadata_vars[key].set(merged[key])
        self.auto_number_var.set(1 if merged["auto_number_chapters"] else 0)
        self.auto_tag_var.set(1 if merged["auto_tag_generated_files"] else 0)

        self._refresh_switch_labels()

    def _refresh_switch_labels(self):
        """Every switch carries its own state in its label text, so anything
        that changes a switch variable programmatically has to re-sync them."""
        self._on_toggle_changed()
        self._on_no_trim_tail_changed()
        self._on_seed_toggled()
        self._on_mono_changed()
        self._on_auto_translate_changed()
        for switch, var, on_text, off_text in (
            (self.auto_number_switch, self.auto_number_var,
             "(Sets the track number based on chapter filename)", "(set track number manually)"),
            (self.auto_tag_switch, self.auto_tag_var,
             "(Tag the output mp3 files automatically upon generation)",
             "(Tag the output mp3 files manually)"),
        ):
            switch.configure(text=on_text if var.get() else off_text)
        self._on_auto_tag_changed()

    def on_export_settings(self):
        """Writes the current form to a preset file of the user's choosing.
        Deliberately does NOT touch settings.json - presets are per book or
        per speaker, and settings.json is written by Save & Run, which is
        what the pipeline actually reads."""
        data = self._collect_and_validate()
        if data is None:
            return

        suggested = "settings"
        book = data.get("book_title", "").strip()
        if book:
            # Keep it filesystem-safe; book titles here are often Japanese.
            suggested = "".join(c for c in book if c not in '\\/:*?"<>|').strip() or "settings"

        path = filedialog.asksaveasfilename(
            title="Export Settings", defaultextension=".json",
            initialfile=f"{suggested}.json",
            filetypes=[("Settings preset", "*.json"), ("All files", "*.*")])
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            messagebox.showerror("Export Failed", f"Could not write:\n{path}\n\n{e}")
            return

        messagebox.showinfo(
            "Exported",
            f"Settings exported to:\n{path}\n\nThis does not change "
            "settings.json - use Save & Run to apply these values to a run.")

    def on_import_settings(self):
        """Loads a preset file into the form. Nothing is written anywhere
        until the user then clicks Export Settings or Save & Run."""
        path = filedialog.askopenfilename(
            title="Import Settings",
            filetypes=[("Settings preset", "*.json"), ("All files", "*.*")])
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Import Failed", f"Could not read:\n{path}\n\n{e}")
            return

        if not isinstance(data, dict):
            messagebox.showerror(
                "Import Failed",
                f"{path}\n\nThis file is valid JSON but is not a settings preset.")
            return

        # A preset written before the per-kind silence durations existed
        # still carries the single legacy key - run it through the same
        # migration settings.json goes through so old presets keep working.
        data = migrate_silence_settings(data)

        known = set(DEFAULT_SETTINGS) & set(data)
        if not known:
            messagebox.showerror(
                "Import Failed",
                f"{path}\n\nNo recognised settings found in this file.")
            return

        self._apply_settings_to_fields(data)
        self.metadata_status_label.configure(text="")
        missing = len(DEFAULT_SETTINGS) - len(known)
        note = f"\n\n{missing} setting(s) not in the file were left at defaults." if missing else ""
        messagebox.showinfo(
            "Imported",
            f"Loaded {len(known)} setting(s) from:\n{path}{note}\n\n"
            "Nothing is saved yet - use Save & Run to apply them.")

    def on_save_and_run(self):
        if self._run_window is not None and self._run_window.winfo_exists():
            messagebox.showinfo(
                "Already Running",
                "A generation run is already in progress. Close its progress "
                "window (or wait for it to finish) before starting another.")
            return

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

        # Count chapters up front the same way run_audiobook.py itself will
        # (chapter_*.txt in input_folder, sorted), so the progress window
        # can show "Chapter 1 of N" from its very first line rather than
        # waiting to see what the subprocess reports.
        chapter_files = sorted(glob.glob(os.path.join(data["input_folder"], "chapter_*.txt")))
        total_chapters = len(chapter_files)
        if total_chapters == 0:
            messagebox.showerror(
                "No Chapters Found",
                f"No 'chapter_*.txt' files found in:\n{data['input_folder']}\n\n"
                "Add chapter files there before running.")
            return

        try:
            process = subprocess.Popen(
                # "-u": unbuffered stdout/stderr. Without it, Python block-
                # buffers when writing to a pipe instead of a terminal, and
                # the log panel would only update in big, laggy chunks
                # rather than live, line by line.
                ["uv", "run", "--no-sync", "python", "-u", RUN_SCRIPT_PATH],
                cwd=uv_project_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            messagebox.showerror(
                "uv not found",
                "Could not launch via 'uv'. Make sure 'uv' is installed and on PATH,\n"
                "or run run_audiobook.py manually from your existing environment.")
            return
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start run_audiobook.py:\n{e}")
            return

        self._run_process = process
        self._run_queue = queue.Queue()
        self._run_cancelled = False
        self._run_total_chapters = total_chapters
        self._run_current_chapter = 0
        self._run_current_chapter_name = ""
        self._run_completed_chapters = 0

        log_path = default_log_path(data["temp_dir"])
        self._run_window = ProgressWindow(
            self, total_chapters=total_chapters, log_file_path=log_path,
            on_cancel=self._cancel_run,
            on_open_output=lambda: self._open_output_folder(data["output_folder"]),
        )

        threading.Thread(
            target=_read_process_output, args=(process, self._run_queue), daemon=True,
        ).start()
        self.after(100, self._drain_run_queue)

    def _drain_run_queue(self):
        """Polls self._run_queue on the main thread (the only thread
        allowed to touch Tk widgets) and feeds each line to the progress
        window. Reschedules itself via self.after() as long as the window
        is still open - this IS the "queue-draining self.after() loop"
        progress_window.py's docstring describes step 2 as needing."""
        if self._run_window is None or not self._run_window.winfo_exists():
            return
        try:
            while True:
                line = self._run_queue.get_nowait()
                if line is None:
                    self._finish_run()
                    return
                self._handle_run_log_line(line)
        except queue.Empty:
            pass
        self.after(100, self._drain_run_queue)

    def _handle_run_log_line(self, line):
        stripped = line.strip()
        if not stripped:
            # process_chapter() prints a leading "\n" before ">>> Processing:",
            # which arrives here as a separate blank line - skip it rather
            # than showing an empty line in the log.
            return

        lower = stripped.lower()
        if lower.startswith("error") or "failed" in lower:
            tag = "error"
        elif "completed successfully" in lower or stripped.startswith("Done! Saved to:"):
            tag = "success"
        elif stripped.startswith(">>> Processing:"):
            tag = "processing"
        else:
            tag = "text"
        self._run_window.append_log(stripped, tag=tag)

        processing_match = _PROCESSING_LINE_RE.match(stripped)
        if processing_match:
            self._run_current_chapter += 1
            self._run_current_chapter_name = processing_match.group(1)
            self._run_window.set_chapter_progress(
                self._run_current_chapter, self._run_total_chapters,
                f"Processing: {self._run_current_chapter_name}", percent=0)
            self._run_window.set_stats(in_progress=1)
            return

        chunk_match = _CHUNK_LINE_RE.search(stripped)
        if chunk_match and self._run_current_chapter:
            done, total = int(chunk_match.group(1)), int(chunk_match.group(2))
            percent = (done / total * 100) if total else 0
            self._run_window.set_chapter_progress(
                self._run_current_chapter, self._run_total_chapters,
                f"Processing: {self._run_current_chapter_name}", percent=percent)
            return

        if stripped.startswith("Done! Saved to:"):
            self._run_completed_chapters += 1
            self._run_window.set_stats(completed=self._run_completed_chapters)

    def _finish_run(self):
        """Called once run_audiobook.py's stdout has closed. Waits on the
        actual exit code rather than assuming success just because the
        process stopped printing - auto-tagging (mp3_metadata.py) runs
        after every chapter and could itself fail on any of those calls."""
        returncode = self._run_process.wait()
        if self._run_window is None or not self._run_window.winfo_exists():
            return
        if self._run_cancelled:
            return  # _cancel_run already set state to "cancelled" - leave it
        # in_progress was only ever set to 1 (each time a chapter started,
        # in _handle_run_log_line) - nothing else zeroed it back out once
        # the last chapter actually finished, so it stayed stuck at 1
        # after "Completed!".
        self._run_window.set_stats(in_progress=0)
        if returncode == 0:
            self._run_window.set_state("completed")
        else:
            self._run_window.append_log(
                f"run_audiobook.py exited with code {returncode}.", tag="error")
            self._run_window.set_state("failed")

    def _cancel_run(self):
        self._run_cancelled = True
        try:
            # terminate()/kill() on Windows only stops the immediate `uv`
            # process, not the infer.py/ffmpeg children it spawns
            # underneath - taskkill /T recursively kills the whole process
            # tree instead, so TTS inference or ffmpeg don't keep running
            # as orphans after Cancel is clicked.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._run_process.pid)],
                capture_output=True)
        except Exception as e:
            if self._run_window and self._run_window.winfo_exists():
                self._run_window.append_log(f"Failed to stop process: {e}", tag="error")
        if self._run_window and self._run_window.winfo_exists():
            self._run_window.set_stats(in_progress=0)  # same reset as _finish_run above
            self._run_window.set_state("cancelled")

    def _open_output_folder(self, output_folder):
        try:
            os.startfile(output_folder)
        except Exception as e:
            messagebox.showerror("Couldn't Open Folder", f"{output_folder}\n\n{e}")

    # ---------- Generate Subtitle ----------
    def on_generate_subtitles(self):
        """Opens the Subtitle Generation Tool.

        The heavy lifting - worker thread, pause/resume, llama-server
        lifecycle, progress and the log - all lives in SubtitleWindow, so
        this only has to validate settings and hand them over. The window
        gets a copy: its Output Folder is deliberately overridable, so a
        different, already-generated book can be subtitled without
        disturbing what the main settings point at."""
        if self._subtitle_window is not None and self._subtitle_window.winfo_exists():
            self._subtitle_window.lift()
            self._subtitle_window.focus_force()
            return

        data = self._collect_and_validate()
        if data is None:
            return
        save_settings(data)

        self._subtitle_window = SubtitleWindow(
            self, data, backend=data["translation_backend"])


    def on_apply_metadata_tags(self):
        data = self._collect_and_validate()
        if data is None:
            return

        if not data["author_name"] or not data["book_title"]:
            messagebox.showerror(
                "Missing Metadata",
                "Author Name and Book Title are required before applying tags - "
                "these are what Spotify uses (Album Artist + Album) to group all "
                "the chapters together as one album.")
            return

        cover_path = data["cover_art_path"]
        if cover_path and not os.path.isfile(cover_path):
            messagebox.showerror(
                "Cover Art Not Found", f"'{cover_path}' does not exist.\n"
                "Clear the field or browse to a valid .jpg/.jpeg/.png file.")
            return

        if not os.path.isdir(data["input_folder"]):
            messagebox.showerror(
                "Input Folder Not Found",
                f"'{data['input_folder']}' does not exist.\n"
                "Chapter order and track numbers are read from the chapter_*.txt "
                "files there, so this needs to be correct even though we're only "
                "tagging MP3s right now.")
            return
        if not os.path.isdir(data["output_folder"]):
            messagebox.showerror(
                "Output Folder Not Found",
                f"'{data['output_folder']}' does not exist yet. Run generation "
                "first (Save & Run), then come back and apply tags.")
            return

        # Persist settings first, so settings.json always reflects what was
        # actually just tagged (and so General/Advanced edits aren't lost
        # if the person only meant to click Apply Tags).
        save_settings(data)

        try:
            import mp3_metadata
        except ImportError:
            messagebox.showerror(
                "mutagen Not Installed",
                "The 'mutagen' package isn't installed in this project's venv yet.\n\n"
                "Run this in PowerShell from the project folder, then try again:\n"
                "    uv sync")
            return

        try:
            result = mp3_metadata.apply_metadata(data, data)
        except Exception as e:
            messagebox.showerror("Tagging Failed", f"Unexpected error while tagging:\n{e}")
            return

        summary_lines = [f"Tagged {result.tagged_count} MP3 file(s)."]
        if result.missing:
            summary_lines.append(
                f"{len(result.missing)} chapter(s) have no MP3 yet in the output "
                "folder (skipped): " + ", ".join(result.missing[:5]) +
                ("..." if len(result.missing) > 5 else ""))
        if result.errors:
            summary_lines.append(f"{len(result.errors)} file(s) failed:")
            summary_lines.extend(f"  - {name}: {err}" for name, err in result.errors[:5])

        summary_text = "\n".join(summary_lines)
        self.metadata_status_label.configure(
            text=(f"\u2714 Tagged {result.tagged_count} file(s)"
                  + (f", {len(result.missing)} missing" if result.missing else "")
                  + (f", {len(result.errors)} errors" if result.errors else "")))

        if result.errors:
            messagebox.showwarning("Tagging Finished With Errors", summary_text)
        else:
            messagebox.showinfo("Tagging Complete", summary_text)


if __name__ == "__main__":
    # Must happen before the first CTk window is created in this process
    # (SettingsApp's root, below) - see progress_window.py's __main__ for
    # why: window geometry is always literal pixels but CTk auto-scales
    # widget sizes to display DPI by default, and the two disagreeing is
    # what squished ProgressWindow's layout during testing. Pinning both
    # here too keeps ProgressWindow consistent once it's opened as a real
    # child Toplevel of this already-running app, not just in its own
    # standalone demo.
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)

    app = SettingsApp()
    app.mainloop()
