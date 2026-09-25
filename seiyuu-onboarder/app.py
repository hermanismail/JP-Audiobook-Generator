"""
app.py
------
The Seiyuu Onboarding window: wav samples in, a trained speaker file out.

Five steps, in order, all derived from one <speaker>/<style> pair:

    1 source      point at audio/<speaker>/<style>
    2 identity    who this voice is: name, kana, translation
    3 transcribe  Whisper, from the existing C:\\Transcribe venv
    4 review      YOU fix the text - the pipeline parks here
    5 manifest    prepare_manifest.py
    6 train       train.py, then hardlink the result into seiyuu/list/

Step 2 is REQUIRED (user decision 2026-09-25): the three names go into
the suite library when training publishes the speaker file, so everything
downstream - the generator's credit line, the profiler, dynamic-repair -
can read them instead of asking again. The names are prepopulated from
any other style of the same speaker, because the parent folder is the
identity.

"Run remaining steps" walks 2 -> 5 and stops at step 3, because Whisper
mishears and omits, and only a person can tell a good transcript from a
plausible one.

Run it with:   uv run python app.py
"""

import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

import library
import pipeline

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# The generator's palette (ui_common.py), so the two tools look related
# without sharing a line of code.
BG = "#F7F7FA"
CARD = "#FFFFFF"
BORDER = "#E7E7EC"
TITLE = "#17171C"
SUBTITLE = "#8B8B94"
ENTRY_BORDER = "#E2E2E8"
ENTRY_TEXT = "#3A3A42"
ACCENT = "#6C5DD3"
ACCENT_HOVER = "#5B4FC0"
DONE = "#34C773"
WAIT = "#E08A2C"
PASTEL_GREEN = "#E6F8ED"
PASTEL_AMBER = "#FFF1E0"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"

STEP_PILLS = {
    "done": (PASTEL_GREEN, "#1E8B4E"),
    "wait": (PASTEL_AMBER, "#B66A16"),
    "queued": (PASTEL_GREY, "#78767F"),
    "running": (PASTEL_VIOLET, ACCENT),
}


def load_settings():
    import json
    if not os.path.isfile(SETTINGS_PATH):
        save_settings(pipeline.DEFAULT_SETTINGS)
        return dict(pipeline.DEFAULT_SETTINGS)
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return pipeline.merge_setting_defaults(json.load(f))


def save_settings(data):
    import json
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class ReviewRow(ctk.CTkFrame):
    """One wav: its raw transcript, an editable suggestion, and a play
    button - because the text sometimes needs a word Whisper never heard."""

    def __init__(self, parent, wav_name, wav_path, on_play):
        super().__init__(parent, fg_color="#FCFCFD", border_width=1,
                         border_color=BORDER, corner_radius=10)
        self.wav_name = wav_name
        self.wav_path = wav_path
        self.raw_text = ""

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkButton(head, text="\u25B6", width=26, height=26, corner_radius=13,
                      fg_color=CARD, hover_color=PASTEL_VIOLET, text_color=ACCENT,
                      border_width=1, border_color=ENTRY_BORDER,
                      font=ctk.CTkFont(size=11),
                      command=lambda: on_play(wav_path)).pack(side="left")
        ctk.CTkLabel(head, text="  " + wav_name, text_color=ENTRY_TEXT,
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.status_label = ctk.CTkLabel(head, text="", text_color=SUBTITLE,
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(side="right")

        self.raw_label = ctk.CTkLabel(
            self, text="whisper: (not transcribed yet)", text_color=SUBTITLE,
            font=ctk.CTkFont(size=11), anchor="w", justify="left", wraplength=760)
        self.raw_label.pack(fill="x", padx=12, pady=(0, 6))

        self.textbox = ctk.CTkTextbox(
            self, height=62, border_width=1, border_color=ENTRY_BORDER,
            fg_color=CARD, text_color=ENTRY_TEXT, corner_radius=8,
            font=ctk.CTkFont(size=14), wrap="word")
        self.textbox.pack(fill="x", padx=12, pady=(0, 11))

    def set_raw(self, raw):
        self.raw_text = raw or ""
        shown = self.raw_text.replace("\n", " \u23CE ").strip()
        self.raw_label.configure(
            text="whisper: " + (shown if shown else "(not transcribed yet)"))

    def set_text(self, text):
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", text or "")

    def get_text(self):
        return self.textbox.get("1.0", "end").strip()

    def mark(self, message, colour=SUBTITLE):
        self.status_label.configure(text=message, text_color=colour)


class OnboarderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Seiyuu Onboarding")
        self.geometry("1020x900")
        self.minsize(880, 640)
        self.configure(fg_color=BG)

        icon = os.path.join(SCRIPT_DIR, "seiyuu_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass        # a missing or odd icon must never stop the tool

        self.settings = load_settings()
        self.paths = None
        self.display_var = ctk.StringVar()
        self.kana_var = ctk.StringVar()
        self.translation_var = ctk.StringVar()
        self.rows = []
        self._queue = queue.Queue()
        self._busy = False
        self._cancel = threading.Event()
        self._started_at = None

        self.speaker_var = ctk.StringVar()
        self.style_var = ctk.StringVar()
        self.model_var = ctk.StringVar(value=self.settings["whisper_model"])
        self.device_var = ctk.StringVar(value=self.settings["device"])
        self.retranscribe_var = ctk.IntVar(value=0)

        self._build()
        self.after(100, self._drain)

    # ---------------------------------------------------------------- build
    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _section_head(self, card, number, title):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(13, 8))
        badge = ctk.CTkLabel(row, text=str(number), width=26, height=26,
                             corner_radius=8, fg_color=PASTEL_GREY,
                             text_color="#78767F",
                             font=ctk.CTkFont(size=12, weight="bold"))
        badge.pack(side="left")
        ctk.CTkLabel(row, text="  " + title.upper(), text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        pill = ctk.CTkLabel(row, text="queued", corner_radius=999,
                            fg_color=PASTEL_GREY, text_color="#78767F",
                            font=ctk.CTkFont(size=11, weight="bold"),
                            padx=10, pady=3)
        pill.pack(side="right")
        return badge, pill

    def _set_state(self, badge, pill, state, text, number=None):
        bg, fg = STEP_PILLS[state]
        pill.configure(text=text, fg_color=bg, text_color=fg)
        badge.configure(fg_color=bg, text_color=fg,
                        text="\u2713" if state == "done" else str(number))

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        header.pack(fill="x")
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=13)
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left")
        self.head_title = ctk.CTkLabel(titles, text="No speaker selected",
                                       text_color=TITLE,
                                       font=ctk.CTkFont(size=16, weight="bold"))
        self.head_title.pack(anchor="w")
        self.head_sub = ctk.CTkLabel(titles, text="Enter a speaker and style to begin",
                                     text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.head_sub.pack(anchor="w")

        self.run_button = ctk.CTkButton(
            inner, text="\u25B6  Run remaining steps", height=38, width=190,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_run_remaining)
        self.run_button.pack(side="right")

        body = ctk.CTkScrollableFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        self._build_source(body)
        self._build_identity(body)
        self._build_transcribe(body)
        self._build_review(body)
        self._build_manifest(body)
        self._build_train(body)
        self._build_result(body)
        self._build_log()

    def _build_source(self, parent):
        card = self._card(parent)
        self.source_badge, self.source_pill = self._section_head(card, 1, "Source")

        fields = ctk.CTkFrame(card, fg_color="transparent")
        fields.pack(fill="x", padx=16, pady=(0, 8))
        for label, var in (("Speaker", self.speaker_var), ("Style", self.style_var)):
            box = ctk.CTkFrame(fields, fg_color="transparent")
            box.pack(side="left", padx=(0, 10))
            ctk.CTkLabel(box, text=label, text_color=SUBTITLE,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
            entry = ctk.CTkEntry(box, textvariable=var, width=170, height=34,
                                 corner_radius=8, border_width=1,
                                 border_color=ENTRY_BORDER, fg_color=CARD,
                                 text_color=ENTRY_TEXT)
            entry.pack()
            entry.bind("<Return>", lambda _e: self.on_load())
        ctk.CTkButton(fields, text="Browse\u2026", width=96, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1, border_color="#D8D8DE",
                      command=self.on_browse).pack(side="left", padx=(0, 8), pady=(16, 0))
        ctk.CTkButton(fields, text="Load", width=86, height=34, corner_radius=8,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.on_load).pack(side="left", pady=(16, 0))

        self.wavs_label = ctk.CTkLabel(card, text="", text_color=SUBTITLE,
                                       font=ctk.CTkFont(size=12), anchor="w",
                                       justify="left")
        self.wavs_label.pack(fill="x", padx=16, pady=(0, 6))

        self.paths_label = ctk.CTkLabel(
            card, text="", text_color=ENTRY_TEXT, anchor="w", justify="left",
            font=ctk.CTkFont(family="Consolas", size=11))
        self.paths_label.pack(fill="x", padx=16, pady=(0, 13))

    def _build_identity(self, parent):
        """Who this voice is. Required before training, because the row it
        writes is what every other tool reads a voice's name from."""
        card = self._card(parent)
        self.id_badge, self.id_pill = self._section_head(card, 2, "Identity")
        ctk.CTkLabel(card, anchor="w", justify="left", wraplength=820,
                     text_color=SUBTITLE, font=ctk.CTkFont(size=12),
                     text=("The name the reader sees, how the engine should say it, and "
                           "how it is written in English. Saved to the library when "
                           "training publishes the speaker file.")).pack(
            fill="x", padx=16, pady=(0, 8))

        fields = ctk.CTkFrame(card, fg_color="transparent")
        fields.pack(fill="x", padx=16, pady=(0, 6))
        for label, var, width, hint in (
                ("Name", self.display_var, 190, "高野麻里佳"),
                ("Reading (kana)", self.kana_var, 190, "こうのまりか"),
                ("Translation", self.translation_var, 190, "Kouno Marika")):
            box = ctk.CTkFrame(fields, fg_color="transparent")
            box.pack(side="left", padx=(0, 10))
            ctk.CTkLabel(box, text=label, text_color=SUBTITLE,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
            ctk.CTkEntry(box, textvariable=var, width=width, height=34, corner_radius=8,
                         border_width=1, border_color=ENTRY_BORDER, fg_color=CARD,
                         text_color=ENTRY_TEXT, placeholder_text=hint).pack()

        self.identity_note = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                          justify="left", wraplength=820,
                                          font=ctk.CTkFont(size=12))
        self.identity_note.pack(fill="x", padx=16, pady=(0, 13))

    def _build_transcribe(self, parent):
        card = self._card(parent)
        self.tr_badge, self.tr_pill = self._section_head(card, 3, "Transcribe")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkOptionMenu(row, values=["large-v3-turbo", "large-v3", "medium"],
                          variable=self.model_var, width=160, height=34,
                          corner_radius=8, fg_color=CARD, button_color=CARD,
                          button_hover_color=BG, text_color=ENTRY_TEXT,
                          dropdown_fg_color=CARD).pack(side="left", padx=(0, 8))
        ctk.CTkOptionMenu(row, values=["cuda", "cpu"], variable=self.device_var,
                          width=96, height=34, corner_radius=8, fg_color=CARD,
                          button_color=CARD, button_hover_color=BG,
                          text_color=ENTRY_TEXT,
                          dropdown_fg_color=CARD).pack(side="left", padx=(0, 8))
        ctk.CTkCheckBox(row, text="re-transcribe existing",
                        variable=self.retranscribe_var, checkbox_width=18,
                        checkbox_height=18, text_color=SUBTITLE,
                        font=ctk.CTkFont(size=12),
                        fg_color=ACCENT).pack(side="left", padx=(4, 0))
        self.tr_button = ctk.CTkButton(row, text="Transcribe", width=120, height=34,
                                       corner_radius=8, fg_color=ACCENT,
                                       hover_color=ACCENT_HOVER,
                                       font=ctk.CTkFont(size=13, weight="bold"),
                                       command=self.on_transcribe)
        self.tr_button.pack(side="right")

    def _build_review(self, parent):
        card = self._card(parent)
        self.rev_badge, self.rev_pill = self._section_head(card, 4, "Review & clean")

        ctk.CTkLabel(card, text="Whisper's text is a suggestion - fix it here before it "
                               "becomes training data.", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12), anchor="w").pack(fill="x", padx=16,
                                                                 pady=(0, 8))
        # Capped: a ten-sample speaker must not push the steps below off-screen.
        self.rows_frame = ctk.CTkScrollableFrame(card, fg_color=BG, height=306,
                                                 corner_radius=8)
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 10))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 13))
        ctk.CTkButton(actions, text="Re-apply cleanup", width=140, height=32,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1, border_color="#D8D8DE",
                      command=self.on_reapply).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Revert to raw", width=120, height=32,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1, border_color="#D8D8DE",
                      command=self.on_revert).pack(side="left")
        self.save_button = ctk.CTkButton(
            actions, text="Save metadata.csv & continue", width=220, height=32,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_save_metadata)
        self.save_button.pack(side="right")

    def _build_manifest(self, parent):
        card = self._card(parent)
        self.man_badge, self.man_pill = self._section_head(card, 5, "Manifest")
        self.man_cmd = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                    justify="left",
                                    font=ctk.CTkFont(family="Consolas", size=11))
        self.man_cmd.pack(fill="x", padx=16, pady=(0, 8))
        self.man_button = ctk.CTkButton(card, text="Build manifest", width=140,
                                        height=32, corner_radius=8, fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER,
                                        font=ctk.CTkFont(size=13, weight="bold"),
                                        command=self.on_manifest)
        self.man_button.pack(anchor="e", padx=16, pady=(0, 13))

    def _build_train(self, parent):
        card = self._card(parent)
        self.train_badge, self.train_pill = self._section_head(card, 6, "Train speaker")
        self.train_cmd = ctk.CTkLabel(card, text="", text_color=SUBTITLE, anchor="w",
                                      justify="left",
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.train_cmd.pack(fill="x", padx=16, pady=(0, 8))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 13))
        self.train_status = ctk.CTkLabel(row, text="", text_color=ENTRY_TEXT,
                                         font=ctk.CTkFont(size=12))
        self.train_status.pack(side="left")
        self.cancel_button = ctk.CTkButton(
            row, text="Cancel", width=90, height=32, corner_radius=8, fg_color=CARD,
            hover_color=BG, text_color=ENTRY_TEXT, border_width=1,
            border_color="#D8D8DE", command=self.on_cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.train_button = ctk.CTkButton(
            row, text="Train speaker", width=140, height=32, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_train)
        self.train_button.pack(side="right")

    def _build_result(self, parent):
        self.result_card = ctk.CTkFrame(parent, fg_color=PASTEL_GREY,
                                        corner_radius=11, border_width=1,
                                        border_color=BORDER)
        self.result_card.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(self.result_card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=12)
        self.result_label = ctk.CTkLabel(
            row, text="seiyuu/list/ entry appears here once training finishes",
            text_color=SUBTITLE, font=ctk.CTkFont(family="Consolas", size=11))
        self.result_label.pack(side="left")
        self.open_button = ctk.CTkButton(
            row, text="Open folder", width=110, height=30, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT, border_width=1,
            border_color="#D8D8DE", command=self.on_open_folder, state="disabled")
        self.open_button.pack(side="right", padx=(8, 0))
        self.copy_button = ctk.CTkButton(
            row, text="Copy path", width=100, height=30, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT, border_width=1,
            border_color="#D8D8DE", command=self.on_copy_path, state="disabled")
        self.copy_button.pack(side="right")

    def _build_log(self):
        wrap = ctk.CTkFrame(self, fg_color=LOG_BG, corner_radius=0, height=170)
        wrap.pack(fill="x", side="bottom")
        wrap.pack_propagate(False)
        self.log_box = ctk.CTkTextbox(wrap, fg_color=LOG_BG, text_color=LOG_FG,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      border_width=0, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------ plumbing
    def log(self, message):
        self._queue.put(("log", message))

    def _drain(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("%H:%M:%S  ") + payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        if self._busy and self._started_at:
            elapsed = int(time.time() - self._started_at)
            self.train_status.configure(text=f"running \u00b7 {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.after(120, self._drain)

    def mark_running(self, badge, pill, number, text):
        """Shows a step as running right now. refresh() only knows what is
        on disk, so without this a step in progress still reads 'queued'
        until it finishes."""
        self.ui(lambda: self._set_state(badge, pill, "running", text, number))

    def ui(self, fn):
        """Runs fn on the Tk thread - worker threads must never touch widgets."""
        self._queue.put(("call", fn))

    def _start(self, target):
        if self._busy:
            messagebox.showinfo("Busy", "A step is already running. Wait for it to "
                                        "finish, or cancel it first.")
            return
        if not self.paths:
            messagebox.showerror("No speaker", "Load a speaker and style first.")
            return
        self._busy = True
        self._cancel.clear()
        self._started_at = time.time()
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")

        def wrapper():
            try:
                target()
            except Exception as e:                      # never lose the reason
                self.log(f"! {type(e).__name__}: {e}")
                self.ui(lambda: messagebox.showerror("Failed", str(e)))
            finally:
                self._busy = False
                self._started_at = None
                self.ui(self._finish_run)

        threading.Thread(target=wrapper, daemon=True).start()

    def _finish_run(self):
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.train_status.configure(text="")
        self.refresh()

    # -------------------------------------------------------------- actions
    def on_browse(self):
        start = os.path.join(self.settings["irodori_root"], "audio")
        chosen = filedialog.askdirectory(title="Pick a sample folder "
                                               "(audio/<speaker>/<style>)",
                                         initialdir=start)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        style = os.path.basename(chosen)
        speaker = os.path.basename(os.path.dirname(chosen))
        self.speaker_var.set(speaker)
        self.style_var.set(style)
        self.on_load()

    def on_load(self):
        speaker = self.speaker_var.get().strip()
        style = self.style_var.get().strip()
        if not speaker or not style:
            messagebox.showerror("Missing", "Both Speaker and Style are needed - "
                                            "they name every folder this tool touches.")
            return
        self.paths = pipeline.SpeakerPaths(self.settings["irodori_root"], speaker, style)
        if not os.path.isdir(self.paths.audio_dir):
            messagebox.showerror(
                "Not found",
                f"{self.paths.audio_dir} does not exist.\n\nPut the wav samples "
                f"there first - this tool never creates that folder.")
            self.paths = None
            return
        self.log(f"loaded {self.paths.name} from {self.paths.audio_dir}")
        self._load_identity(speaker, style)
        self._build_rows()
        self.refresh()

    def _load_identity(self, speaker, style):
        """Fill the Identity fields in from the library: this exact voice
        if it is already there, otherwise any other STYLE of the same
        speaker - the parent folder is the identity (user, 2026-09-25)."""
        known = library.known_voice(self.settings, self.paths.list_entry)
        if known and known.get("display_name"):
            self.display_var.set(known.get("display_name") or "")
            self.kana_var.set(known.get("reading_kana") or "")
            self.translation_var.set(known.get("translation_name") or "")
            self._identity_note(f"The library already knows {self.paths.name}.", DONE)
            return
        names, from_style = library.names_for_speaker(self.settings, speaker)
        if names:
            self.display_var.set(names["display_name"])
            self.kana_var.set(names["reading_kana"])
            self.translation_var.set(names["translation_name"])
            self._identity_note(f"Taken from {speaker}/{from_style}, already onboarded - "
                                f"change them if this style is someone else.", DONE)
            return
        if library.open_library(self.settings) is None:
            self._identity_note(f"Library not reachable ({library.error()}). Training is "
                                f"blocked until it is, so no voice is trained that "
                                f"nothing can name.", "#C4453C")
            return
        self._identity_note("New voice - fill these in before training.", SUBTITLE)

    def _identity_note(self, text, colour):
        if hasattr(self, "identity_note"):
            self.identity_note.configure(text=text, text_color=colour)

    def identity(self):
        return {"display_name": self.display_var.get().strip(),
                "reading_kana": self.kana_var.get().strip(),
                "translation_name": self.translation_var.get().strip()}

    def identity_problem(self):
        """Why training cannot start, or None.

        The names are required so that everything downstream can read a
        voice from the library instead of asking for it again. A library
        that is not there blocks too - with an escape, because an hour of
        GPU should not be lost to an unplugged drive."""
        names = self.identity()
        missing = [label for label, key in (("Name", "display_name"),
                                            ("Reading (kana)", "reading_kana"),
                                            ("Translation", "translation_name"))
                   if not names[key]]
        if missing:
            return ("Fill in " + ", ".join(missing) + " in step 2 first.\n\n"
                    "They are what the generator credits this voice with and what the "
                    "engine is told to say, and they are stored once, here.")
        if library.open_library(self.settings) is None:
            return (f"The library is not reachable ({library.error()}).\n\n"
                    f"Training would produce a voice that nothing downstream can name. "
                    f"Reconnect it, or train anyway and add the names later in the "
                    f"generator's Verify seiyuu name box.")
        return None

    def _build_rows(self):
        for row in self.rows:
            row.destroy()
        self.rows = []
        for wav in pipeline.find_wavs(self.paths.audio_dir):
            row = ReviewRow(self.rows_frame, wav,
                            os.path.join(self.paths.audio_dir, wav), self.on_play)
            row.pack(fill="x", pady=(0, 8), padx=2)
            self.rows.append(row)
        self._fill_rows()

    def _fill_rows(self):
        """Existing metadata wins over a fresh cleanup - it may be text you
        corrected by hand, and losing that would be the worst thing this
        tool could do."""
        saved = pipeline.read_metadata(self.paths.metadata_csv)
        for row in self.rows:
            raw_path = self.paths.transcript_for(self.settings["transcript_root"],
                                                 row.wav_name)
            raw = ""
            if os.path.isfile(raw_path):
                with open(raw_path, "r", encoding="utf-8") as f:
                    raw = f.read()
            row.set_raw(raw)
            if saved.get(row.wav_name, "").strip():
                row.set_text(saved[row.wav_name])
                row.mark("saved", DONE)
            elif raw:
                row.set_text(pipeline.clean_transcript(raw))
                row.mark("needs review", WAIT)
            else:
                row.set_text("")
                row.mark("no transcript", SUBTITLE)

    def on_play(self, wav_path):
        try:
            import winsound
            winsound.PlaySound(wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self.log(f"! cannot play {os.path.basename(wav_path)}: {e}")

    def on_reapply(self):
        for row in self.rows:
            if row.raw_text:
                row.set_text(pipeline.clean_transcript(row.raw_text))
        self.log("cleanup re-applied to the suggestions")

    def on_revert(self):
        for row in self.rows:
            if row.raw_text:
                row.set_text(row.raw_text.strip())
        self.log("boxes reverted to Whisper's raw text")

    def on_transcribe(self):
        self._start(self._do_transcribe)

    def _do_transcribe(self):
        out_dir = self.paths.transcript_dir(self.settings["transcript_root"])
        os.makedirs(out_dir, exist_ok=True)
        redo = bool(self.retranscribe_var.get())
        wavs = pipeline.find_wavs(self.paths.audio_dir)

        self.mark_running(self.tr_badge, self.tr_pill, 2, "transcribing")
        for index, wav in enumerate(wavs, start=1):
            if self._cancel.is_set():
                self.log("cancelled")
                return
            target = self.paths.transcript_for(self.settings["transcript_root"], wav)
            if os.path.isfile(target) and not redo:
                self.log(f"[{index}/{len(wavs)}] {wav} - transcript exists, skipped")
                continue
            self.log(f"[{index}/{len(wavs)}] whisper {wav} "
                     f"({self.model_var.get()}, {self.device_var.get()})")
            settings = dict(self.settings)
            settings["whisper_model"] = self.model_var.get()
            cmd = pipeline.whisper_command(
                settings, os.path.join(self.paths.audio_dir, wav), out_dir)
            code = pipeline.run_streaming(cmd, self.paths.audio_dir, self.log)
            if code != 0:
                raise RuntimeError(f"whisper exited with code {code} on {wav} - "
                                   f"see the log above.")
            self.ui(self.refresh)          # the count climbs as files land
        self.ui(self._fill_rows)
        self.log("transcription done - review the text, then save")

    def on_save_metadata(self):
        if not self.paths:
            return
        rows = [(row.wav_name, row.get_text()) for row in self.rows]
        empty = [name for name, text in rows if not text]
        if empty:
            messagebox.showerror("Empty text",
                                 "These samples have no text yet:\n  "
                                 + "\n  ".join(empty)
                                 + "\n\nTranscribe them, or type the text yourself.")
            return
        pipeline.write_metadata(self.paths.metadata_csv, rows)
        self.log(f"wrote {self.paths.metadata_csv} ({len(rows)} row(s))")
        self._fill_rows()
        self.refresh()

    def on_manifest(self):
        self._start(self._do_manifest)

    def _do_manifest(self):
        cmd = pipeline.manifest_command(self.settings, self.paths)
        self.mark_running(self.man_badge, self.man_pill, 4, "building")
        self.log("prepare_manifest.py " + self.paths.rel_dataset)
        code = pipeline.run_streaming(cmd, self.settings["irodori_root"], self.log)
        if code != 0:
            raise RuntimeError(f"prepare_manifest.py exited with code {code}.")
        if not os.path.isfile(self.paths.manifest):
            raise RuntimeError(f"no manifest at {self.paths.manifest} despite a "
                               f"clean exit - check the log.")
        self.log(f"manifest ready: {self.paths.rel_manifest}")
        self.ui(self.refresh)

    def on_train(self):
        if not self._identity_ready():
            return
        self._start(self._do_train)

    def _identity_ready(self):
        """True when training may start. A missing name simply stops it; a
        missing library asks, because an hour of GPU should not be lost to
        an unplugged drive (see identity_problem())."""
        problem = self.identity_problem()
        if problem is None:
            return True
        if "not reachable" in problem:
            return messagebox.askyesno("Library not reachable", problem + "\n\nTrain anyway?",
                                       icon="warning")
        messagebox.showerror("Identity", problem)
        return False

    def _do_train(self):
        cmd = pipeline.train_command(self.settings, self.paths)
        self.mark_running(self.train_badge, self.train_pill, 5, "training")
        self.log(f"train.py -> {self.paths.rel_output_dir} (this takes a while)")
        # train.py prints "  105/3000 [01:20<23:46, 2.03step/s, epoch=100 ...".
        # Match the fraction, not the word "step" - "epoch_step=1/1" and
        # "2.03step/s" both sit on the same line and would win otherwise.
        step_re = re.compile(r"(\d+)/(\d+)\s*\[")

        def on_line(line):
            self.log(line)
            found = step_re.search(line)
            if found:
                done, total = found.group(1), found.group(2)
                self.ui(lambda: self.train_status.configure(
                    text=f"step {done} / {total}"))

        code = pipeline.run_streaming(cmd, self.settings["irodori_root"], on_line)
        if code != 0:
            raise RuntimeError(f"train.py exited with code {code}.")
        mode, dest = pipeline.publish_to_list(self.paths)
        self.log(f"{mode}: {dest}")
        self._record_voice(dest)
        self.ui(self.refresh)

    def _record_voice(self, speaker_path):
        """Write the voice into the library, now that the speaker file
        exists. `onboarded_at` is stamped here rather than when the form
        was filled, because this is the moment the voice is real."""
        row, what = library.record_voice(
            self.settings, speaker_path, self.identity(),
            speaker_key=self.paths.speaker, style=self.paths.style,
            samples_folder=self.paths.audio_dir)
        if row is None:
            self.log(f"! the library was not updated ({what}) - add the names in the "
                     f"generator's Verify seiyuu name box")
            return
        names = self.identity()
        self.log(f"library: {what} {names['display_name']} ({names['reading_kana']}) "
                 f"= {names['translation_name']}")
        self.ui(lambda: self._identity_note(
            f"Saved to the library: {names['display_name']} - the generator will credit "
            f"chapters to this name.", DONE))

    def on_cancel(self):
        self._cancel.set()
        self.log("cancel requested - it takes effect between samples")

    def on_copy_path(self):
        if self.paths:
            self.clipboard_clear()
            self.clipboard_append(self.paths.list_entry)
            self.log("path copied to clipboard")

    def on_open_folder(self):
        if self.paths and os.path.isdir(self.paths.list_dir):
            subprocess.Popen(["explorer", os.path.normpath(self.paths.list_dir)])

    def on_run_remaining(self):
        if not self._identity_ready():
            return
        self._start(self._do_run_remaining)

    def _do_run_remaining(self):
        state = pipeline.status(self.settings, self.paths)
        if not state["transcribed_all"] or self.retranscribe_var.get():
            self._do_transcribe()
            state = pipeline.status(self.settings, self.paths)

        if not state["metadata_done"]:
            self.log("parked at step 3 - review the text and press "
                     "'Save metadata.csv & continue', then run again")
            self.ui(lambda: self.head_sub.configure(
                text="waiting for your review", text_color=WAIT))
            return

        if not state["manifest_done"]:
            self._do_manifest()
        else:
            self.log("manifest exists, skipped")

        if not state["trained"]:
            self._do_train()
        else:
            self.log("checkpoint_final exists, skipped - delete it to retrain")
            mode, dest = pipeline.publish_to_list(self.paths)
            self.log(f"{mode}: {dest}")

    # ------------------------------------------------------------- refresh
    def refresh(self):
        if not self.paths:
            return
        state = pipeline.status(self.settings, self.paths)
        wavs = state["wavs"]

        self.head_title.configure(text=f"{self.paths.speaker} \u00b7 {self.paths.style}")
        self.wavs_label.configure(
            text=f"{len(wavs)} wav: " + ", ".join(wavs) if wavs else "no wav files here")
        self.paths_label.configure(text="\n".join([
            "will create",
            f"  {self.paths.rel_dataset}/metadata.csv",
            f"  {self.paths.rel_manifest}   + latents/",
            f"  {self.paths.rel_output_dir}/{pipeline.FINAL_CHECKPOINT_NAME}",
            f"  seiyuu/list/{self.paths.name}.speaker.safetensors",
        ]))
        self.man_cmd.configure(text=" ".join(
            pipeline.manifest_command(self.settings, self.paths)))
        self.train_cmd.configure(text=" ".join(
            pipeline.train_command(self.settings, self.paths)))

        self._set_state(self.source_badge, self.source_pill,
                        "done" if wavs else "wait",
                        f"{len(wavs)} wav found" if wavs else "no wav", 1)

        done_n = len(state["transcribed"])
        self._set_state(self.tr_badge, self.tr_pill,
                        "done" if state["transcribed_all"] else "wait",
                        f"{done_n} of {len(wavs)}" if wavs else "queued", 2)

        self._set_state(self.rev_badge, self.rev_pill,
                        "done" if state["metadata_done"] else "wait",
                        "saved" if state["metadata_done"] else "needs you", 3)

        self._set_state(self.man_badge, self.man_pill,
                        "done" if state["manifest_done"] else "queued",
                        "built" if state["manifest_done"] else "queued", 4)

        self._set_state(self.train_badge, self.train_pill,
                        "done" if state["trained"] else "queued",
                        "trained" if state["trained"] else "queued", 5)

        if state["published"]:
            self.result_card.configure(fg_color=PASTEL_GREEN, border_color="#CDEBDA")
            self.result_label.configure(
                text=f"seiyuu/list/{self.paths.name}.speaker.safetensors",
                text_color="#1E6B42")
            self.copy_button.configure(state="normal")
            self.open_button.configure(state="normal")
        else:
            self.result_card.configure(fg_color=PASTEL_GREY, border_color=BORDER)
            self.result_label.configure(
                text="seiyuu/list/ entry appears here once training finishes",
                text_color=SUBTITLE)
            self.copy_button.configure(state="disabled")
            self.open_button.configure(state="disabled")

        step = ("6 of 6 \u00b7 done" if state["published"] else
                "6 of 6 \u00b7 train" if state["metadata_done"] and state["manifest_done"] else
                "5 of 6 \u00b7 manifest" if state["metadata_done"] else
                "4 of 6 \u00b7 waiting for your review" if state["transcribed_all"] else
                "3 of 6 \u00b7 transcribe")
        self.head_sub.configure(
            text=f"{len(wavs)} sample(s) \u00b7 step {step}",
            text_color=WAIT if "review" in step else SUBTITLE)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    OnboarderApp().mainloop()
