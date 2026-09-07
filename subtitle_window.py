"""
JP Audiobook Generator - Subtitle Generation Tool window
--------------------------------------------------------
A dedicated window for generating English `.srt` subtitle tracks from
already-rendered chapters, opened from the Advanced page's "Generate
Subtitle" button.

Why a window rather than a button that just runs:
- Translation is minutes per chapter and hours per book, so it needs real
  progress reporting rather than one truncated status line.
- It has to be pausable. The job saturates the GPU, and being able to hand
  the desktop back for a while - without paying the model-load cost again
  on resume - is the difference between "run it overnight" and "run it when
  I'm not using the machine".
- Its parameters (especially Output Folder) want overriding per run, so a
  different, already-generated book can be subtitled without disturbing the
  main settings.

Design language follows the SHAMA Server launcher (header badge + status
pill, white cards at radius 16, dark Consolas log panel) and the generator's
own Progress window (the four stat chips, the log tag colours), so the three
windows read as one family.

Threading model: the translation runs on a worker thread and posts events
into a queue; the window drains that queue on a Tk `after` timer. Tk widgets
are only ever touched from the main thread.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

import translate_pipeline
from ui_common import (COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE,
                        COLOR_SUBTITLE, COLOR_ENTRY_BORDER, COLOR_ENTRY_TEXT,
                        COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BTN_NEUTRAL_BORDER,
                        COLOR_BTN_NEUTRAL_TEXT, IconBadge)

# Status palette, mirroring the launcher's pill colours.
COLOR_IDLE, COLOR_IDLE_SOFT = "#7A7A85", "#F0F0F3"
COLOR_WAIT, COLOR_WAIT_SOFT = "#C98A2E", "#FFF3DD"
COLOR_OK, COLOR_OK_SOFT = "#2FB668", "#E6F8ED"
COLOR_FAIL, COLOR_FAIL_SOFT = "#D85A5A", "#FCEAEA"
COLOR_ACCENT_SOFT = "#EFECFB"

# Log panel, matching progress_window.py exactly.
COLOR_LOG_BG, COLOR_LOG_BORDER = "#1C1C22", "#2A2A32"
COLOR_LOG_TIMESTAMP = "#6FD3E6"
COLOR_LOG_TEXT = "#C7C7D1"
COLOR_LOG_PROCESSING = "#B9A3F7"
COLOR_LOG_SUCCESS = "#5FD98A"
COLOR_LOG_ERROR = "#F1706E"

MAX_LOG_LINES = 800

# State -> (window title, description, pill text, pill colours, button label).
# The title and its description are both dynamic, per the spec.
STATES = {
    "ready": ("Subtitle Generation Tool",
              "Generate English subtitle tracks from chapters that have already been rendered.",
              "Ready", COLOR_IDLE, COLOR_IDLE_SOFT, "▶  Start"),
    "running": ("Generating Subtitle...",
                "Translating chapter by chapter. This can take a while - pausing frees the "
                "GPU without losing progress.",
                "In Progress", COLOR_WAIT, COLOR_WAIT_SOFT, "⏸  Pause"),
    "paused": ("Generating Subtitle...",
               "Paused. The model is still loaded, so resuming costs nothing but the wait.",
               "In Progress", COLOR_WAIT, COLOR_WAIT_SOFT, "▶  Resume..."),
    "completed": ("Completed",
                  "Every chapter has been processed. The .srt files sit beside the audio.",
                  "Completed", COLOR_OK, COLOR_OK_SOFT, "✕  Exit"),
    "stopped": ("Stopped",
                "The run was stopped before finishing. Chapters already written are kept.",
                "Ready", COLOR_IDLE, COLOR_IDLE_SOFT, "✕  Exit"),
    "error": ("Completed",
              "The run finished with errors - see the log below for what failed.",
              "Error", COLOR_FAIL, COLOR_FAIL_SOFT, "✕  Exit"),
}

PARAM_FIELDS = [
    ("output_folder", "\U0001F3B5", "#4FCB8F", "Output Folder",
     "Folder holding the generated chapters to subtitle", "folder", None),
    ("llama_server_url", "\U0001F517", "#6C5DD3", "llama-server URL",
     "Where the local model is listening", None, None),
    ("llama_server_exe", "\U0001F4E6", "#4DA6FF", "llama-server path",
     "Started automatically if nothing is listening, and stopped afterwards", "file",
     [("Executable", "*.exe"), ("All files", "*.*")]),
    ("llama_model_path", "\U0001F50A", "#9C6DEE", "Translation model (GGUF)",
     "The VNTL model llama-server loads", "file",
     [("GGUF", "*.gguf"), ("All files", "*.*")]),
]


class SubtitleWindow(ctk.CTkToplevel):
    def __init__(self, master, settings, backend="vntl"):
        super().__init__(master)
        self.title("Subtitle Generation Tool")
        self.configure(fg_color=COLOR_BG)
        self.geometry("760x900")
        self.minsize(700, 700)

        self.settings = dict(settings)
        self.backend = backend
        self.state_name = "ready"

        self.vars = {key: ctk.StringVar(value=str(self.settings.get(key, "")))
                     for key, *_ in PARAM_FIELDS}
        self.param_entries = {}
        self.param_browse = {}
        self.param_readonly = {}

        self._control = None
        self._worker = None
        self._events = queue.Queue()
        self._start_time = None
        self._timer_job = None
        self._totals = {"total": 0, "completed": 0, "in_progress": 0}

        self._build_ui()
        self._apply_state("ready")

        # Closing mid-run must not orphan a loaded model, so the X button is
        # routed through the same confirmation as the emergency stop.
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.after(200, self._drain_events)
        self.after(80, self._raise_window)

    def _raise_window(self):
        self.lift()
        self.focus_force()

    # ---------- construction ----------
    def _build_ui(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=24, pady=22)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)      # log card takes the slack

        self._build_header(root, 0)
        self._build_params_card(root, 1)
        self._build_action_card(root, 2)
        self._build_stats_row(root, 3)
        self._build_log_card(root, 4)
        self._build_footer(root, 5)

    def _build_header(self, parent, grid_row):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=grid_row, column=0, sticky="ew", pady=(0, 16))
        header.grid_columnconfigure(1, weight=1)

        badge = ctk.CTkFrame(header, width=46, height=46, corner_radius=12,
                             fg_color=COLOR_ACCENT_SOFT)
        badge.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 14))
        badge.grid_propagate(False)
        ctk.CTkLabel(badge, text="\U0001F310", font=ctk.CTkFont(size=22),
                     text_color=COLOR_ACCENT).place(relx=0.5, rely=0.5, anchor="center")

        self.title_label = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=22, weight="bold"),
            text_color=COLOR_TITLE, anchor="w")
        self.title_label.grid(row=0, column=1, sticky="ew")

        # wraplength is set from the real width in _on_header_resize rather
        # than guessed, so a long description never gets clipped.
        self.subtitle_label = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=13), text_color=COLOR_SUBTITLE,
            anchor="w", justify="left", wraplength=440)
        self.subtitle_label.grid(row=1, column=1, sticky="ew", pady=(2, 0))

        self.pill = ctk.CTkLabel(
            header, text="Ready", font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=14, fg_color=COLOR_IDLE_SOFT, text_color=COLOR_IDLE,
            width=118, height=30)
        self.pill.grid(row=0, column=2, rowspan=2, sticky="e", padx=(14, 0))

        header.bind("<Configure>", self._on_header_resize)

    def _on_header_resize(self, event):
        # 46 badge + 14 gap + 118 pill + 14 gap, with a little slack.
        available = max(240, event.width - 210)
        self.subtitle_label.configure(wraplength=available)

    def _build_params_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                            border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)

        for i, (key, glyph, bg, title, subtitle, browse, filetypes) in enumerate(PARAM_FIELDS):
            self._param_row(card, i, key, glyph, bg, title, subtitle, browse, filetypes)

    def _param_row(self, parent, row, key, glyph, bg, title, subtitle, browse, filetypes):
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.grid(row=row, column=0, sticky="ew", padx=20,
                  pady=(16 if row == 0 else 8, 16 if row == len(PARAM_FIELDS) - 1 else 8))
        line.grid_columnconfigure(1, weight=1)

        IconBadge(line, glyph, bg, size=40, font_size=16, corner_radius=11).grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))

        ctk.CTkLabel(line, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=COLOR_TITLE, anchor="w").grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(line, text=subtitle, font=ctk.CTkFont(size=11),
                     text_color=COLOR_SUBTITLE, anchor="w", justify="left",
                     wraplength=520).grid(row=1, column=1, sticky="w")

        field = ctk.CTkFrame(line, fg_color="transparent")
        field.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        field.grid_columnconfigure(0, weight=1)

        entry = ctk.CTkEntry(field, textvariable=self.vars[key], height=34,
                             corner_radius=8, border_width=1,
                             border_color=COLOR_ENTRY_BORDER,
                             text_color=COLOR_ENTRY_TEXT, fg_color="white")
        entry.grid(row=0, column=0, sticky="ew")
        self.param_entries[key] = entry

        # Shown in place of the entry once a run starts: same text, no editing.
        readonly = ctk.CTkLabel(field, textvariable=self.vars[key], height=34,
                                anchor="w", font=ctk.CTkFont(size=12),
                                text_color=COLOR_SUBTITLE)
        self.param_readonly[key] = readonly

        if browse:
            btn = ctk.CTkButton(
                field, text="\U0001F4C1  Browse", width=104, height=34,
                corner_radius=8, fg_color="white", hover_color="#F5F5F8",
                border_width=1, border_color=COLOR_ENTRY_BORDER,
                text_color=COLOR_ENTRY_TEXT,
                command=lambda k=key, b=browse, f=filetypes: self._browse(k, b, f))
            btn.grid(row=0, column=1, padx=(10, 0))
            self.param_browse[key] = btn

    def _browse(self, key, kind, filetypes):
        current = self.vars[key].get().strip()
        initial = current if os.path.isdir(current) else os.path.dirname(current)
        if kind == "folder":
            chosen = filedialog.askdirectory(parent=self, initialdir=initial or None)
        else:
            chosen = filedialog.askopenfilename(parent=self, initialdir=initial or None,
                                                filetypes=filetypes or [])
        if chosen:
            self.vars[key].set(os.path.normpath(chosen))

    def _build_action_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                            border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)

        self.main_btn = ctk.CTkButton(
            card, text="▶  Start", command=self._on_main_button, height=48,
            corner_radius=8, font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color="#FFFFFF")
        self.main_btn.grid(row=0, column=0, sticky="ew", padx=20, pady=18)

    def _build_stats_row(self, parent, grid_row):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=grid_row, column=0, sticky="ew", pady=(0, 14))
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="stat")

        self.chips = {}
        for i, (key, glyph, bg, caption) in enumerate([
                ("total", "\U0001F4D6", "#8272F4", "Total Chapters"),
                ("completed", "✨", "#4FCB8F", "Completed"),
                ("in_progress", "▶", "#4DA6FF", "In Progress")]):
            card = self._chip_card(row, i)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(expand=True)
            icon_row = ctk.CTkFrame(inner, fg_color="transparent")
            icon_row.pack()
            IconBadge(icon_row, glyph, bg, size=44, font_size=18,
                      corner_radius=12).pack(side="left")
            value = ctk.CTkLabel(icon_row, text="0", text_color=COLOR_TITLE,
                                 font=ctk.CTkFont(size=24, weight="bold"))
            value.pack(side="left", padx=(9, 0))
            ctk.CTkLabel(inner, text=caption, text_color=COLOR_SUBTITLE,
                         font=ctk.CTkFont(size=13), wraplength=130,
                         justify="center").pack(pady=(7, 0))
            self.chips[key] = value

        time_card = self._chip_card(row, 3)
        time_inner = ctk.CTkFrame(time_card, fg_color="transparent")
        time_inner.pack(expand=True)
        self.chips["time"] = ctk.CTkLabel(
            time_inner, text="00:00:00", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=26, weight="bold"))
        self.chips["time"].pack(pady=(16, 0))
        ctk.CTkLabel(time_inner, text="Total Time", text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=13)).pack(pady=(7, 16))

    @staticmethod
    def _chip_card(parent, column):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=14,
                            border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 6, 0 if column == 3 else 6))
        return card

    def _build_log_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                            border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 10))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="\U0001F4C4  Process Log",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=COLOR_TITLE, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(head, text="\U0001F5D1  Clear Log", command=self.clear_log,
                      width=100, height=30, corner_radius=8,
                      font=ctk.CTkFont(size=12), fg_color=COLOR_CARD,
                      hover_color=COLOR_BG, text_color=COLOR_BTN_NEUTRAL_TEXT,
                      border_width=1, border_color=COLOR_BTN_NEUTRAL_BORDER,
                      ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            card, corner_radius=10, fg_color=COLOR_LOG_BG, border_width=1,
            border_color=COLOR_LOG_BORDER, text_color=COLOR_LOG_TEXT,
            font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 18))
        inner = self.log_box._textbox
        inner.tag_config("timestamp", foreground=COLOR_LOG_TIMESTAMP)
        inner.tag_config("text", foreground=COLOR_LOG_TEXT)
        inner.tag_config("processing", foreground=COLOR_LOG_PROCESSING)
        inner.tag_config("success", foreground=COLOR_LOG_SUCCESS)
        inner.tag_config("error", foreground=COLOR_LOG_ERROR)
        self.log_box.configure(state="disabled")

    def _build_footer(self, parent, grid_row):
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.grid(row=grid_row, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self.stop_btn = ctk.CTkButton(
            footer, text="⛔  Stop Process and Exit", command=self._on_emergency_stop,
            height=42, corner_radius=8, font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=COLOR_FAIL, hover_color="#C24B4B", text_color="#FFFFFF")
        self.stop_btn.grid(row=0, column=0, sticky="e")

    # ---------- log ----------
    def append_log(self, message, tag="text"):
        stamp = time.strftime("[%H:%M:%S] ")
        self.log_box.configure(state="normal")
        self.log_box._textbox.insert("end", stamp, "timestamp")
        self.log_box._textbox.insert("end", message + "\n", tag)
        line_count = int(self.log_box._textbox.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_box._textbox.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box._textbox.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ---------- state ----------
    def _apply_state(self, name):
        self.state_name = name
        title, desc, pill, pill_fg, pill_bg, btn = STATES[name]
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=desc)
        self.pill.configure(text=pill, text_color=pill_fg, fg_color=pill_bg)
        self.main_btn.configure(text=btn)
        self._set_params_editable(name == "ready")

    def _set_params_editable(self, editable):
        for key in self.vars:
            entry, readonly = self.param_entries[key], self.param_readonly[key]
            browse = self.param_browse.get(key)
            if editable:
                readonly.grid_forget()
                entry.grid(row=0, column=0, sticky="ew")
                if browse:
                    browse.grid(row=0, column=1, padx=(10, 0))
            else:
                entry.grid_forget()
                if browse:
                    browse.grid_forget()
                readonly.grid(row=0, column=0, columnspan=2, sticky="ew")

    # ---------- main button ----------
    def _on_main_button(self):
        if self.state_name == "ready":
            self._start()
        elif self.state_name == "running":
            self._pause()
        elif self.state_name == "paused":
            self._resume()
        else:
            self.destroy()

    def _start(self):
        settings = dict(self.settings)
        for key in self.vars:
            settings[key] = self.vars[key].get().strip()

        output_folder = settings["output_folder"]
        if not os.path.isdir(output_folder):
            messagebox.showerror("Output Folder Not Found",
                                 f"'{output_folder}' does not exist.", parent=self)
            return

        bases = translate_pipeline.find_chapter_bases(output_folder)
        if not bases:
            messagebox.showerror(
                "Nothing to Translate",
                f"No .sync.json files in:\n{output_folder}\n\nSubtitles are built "
                "from the chunk timings generation writes beside each MP3.",
                parent=self)
            return

        if self.backend == "vntl" and not translate_pipeline.llama_server_reachable(
                settings["llama_server_url"]):
            for key, label in (("llama_server_exe", "llama-server path"),
                               ("llama_model_path", "Translation model (GGUF)")):
                if not os.path.isfile(settings[key]):
                    messagebox.showerror(
                        "Cannot Start llama-server",
                        f"Nothing is listening at {settings['llama_server_url']}, so "
                        f"the server needs starting - but '{label}' does not point "
                        f"at an existing file:\n\n{settings[key]}", parent=self)
                    return

        self._totals = {"total": len(bases), "completed": 0, "in_progress": 0}
        self._render_stats()
        self.append_log(f"Output folder: {output_folder}", "processing")
        self.append_log(f"Backend: {self.backend}  |  {len(bases)} chapter(s) found",
                        "processing")

        self._control = translate_pipeline.TranslationControl()
        self._apply_state("running")
        self._start_time = time.time()
        self._tick_timer()

        self._worker = threading.Thread(
            target=self._run_translation, args=(settings, bases), daemon=True)
        self._worker.start()

    def _pause(self):
        if self._control:
            self._control.pause()
        self._apply_state("paused")
        self.append_log("Paused. llama-server is left running so resuming is instant.",
                        "text")

    def _resume(self):
        if self._control:
            self._control.resume()
        self._apply_state("running")
        self.append_log("Resumed.", "text")

    # ---------- worker ----------
    def _run_translation(self, settings, bases):
        """Runs on the worker thread. Everything it reports goes through the
        queue - no Tk call happens here."""
        def log(message, tag="text"):
            self._events.put(("log", message, tag))

        def on_chunk(base, done, total):
            self._events.put(("chunk", base, done, total))

        def on_chapter_done(base, kind):
            self._events.put(("chapter_done", base, kind))

        try:
            result = translate_pipeline.generate_subtitles(
                settings, base_names=bases, backend=self.backend,
                control=self._control, log=log, on_chunk=on_chunk,
                on_chapter_done=on_chapter_done, skip_existing=True, verbose=False)
            self._events.put(("done", result))
        except Exception as e:                      # never lose a worker crash
            self._events.put(("log", f"Unexpected failure: {e}", "error"))
            self._events.put(("done", None))

    def _drain_events(self):
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append_log(event[1], event[2])
                elif kind == "chunk":
                    base, done, total = event[1], event[2], event[3]
                    self._totals["in_progress"] = 1
                    if done == 1:
                        self.append_log(f">>> {base}: {total} chunk(s) to translate",
                                        "processing")
                    elif done % 25 == 0 or done == total:
                        self.append_log(f"    {base}: {done}/{total}")
                    self._render_stats()
                elif kind == "chapter_done":
                    if event[2] == "written":
                        self._totals["completed"] += 1
                    self._totals["in_progress"] = 0
                    self._render_stats()
                elif kind == "done":
                    self._finish(event[1])
        except queue.Empty:
            pass
        self.after(200, self._drain_events)

    def _finish(self, result):
        self._totals["in_progress"] = 0
        self._render_stats()
        self._stop_timer()

        if result is None:
            self._apply_state("error")
        elif getattr(result, "cancelled", False):
            self._apply_state("stopped")
        else:
            self.append_log(
                f"Wrote {len(result.written)} chapter(s); skipped {len(result.skipped)}; "
                f"{len(result.missing)} without sync.json; {len(result.errors)} failed.",
                "success" if not result.errors else "error")
            for name, err in result.errors:
                self.append_log(f"  - {name}: {err}", "error")
            self._apply_state("error" if result.errors else "completed")
        self._control = None

    # ---------- stats / timer ----------
    def _render_stats(self):
        self.chips["total"].configure(text=str(self._totals["total"]))
        self.chips["completed"].configure(text=str(self._totals["completed"]))
        self.chips["in_progress"].configure(text=str(self._totals["in_progress"]))

    def _tick_timer(self):
        if self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self.chips["time"].configure(text=f"{h:02d}:{m:02d}:{s:02d}")
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
            self._timer_job = None

    # ---------- stopping ----------
    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def _on_window_close(self):
        """The X button. While a run is in flight it behaves exactly like the
        emergency stop - closing the window without releasing the model would
        leave ~6GB of VRAM held by a process with no UI attached to it."""
        if self._busy():
            self._on_emergency_stop()
        else:
            self.destroy()

    def _on_emergency_stop(self):
        if not self._busy():
            self.destroy()
            return

        if not messagebox.askyesno(
                "Stop Everything?",
                "This stops translation and shuts down llama-server to release "
                "the VRAM, then closes this window.\n\nChapters already written "
                "are kept; the one in progress is discarded.\n\nStop now?",
                icon="warning", parent=self):
            return      # accidental click - carry on exactly as before

        self.append_log("Stopping... waiting for the current chunk to finish.", "error")
        self.stop_btn.configure(state="disabled", text="Stopping...")
        self.main_btn.configure(state="disabled")
        if self._control:
            self._control.cancel()      # also unblocks a paused worker
        self.after(200, self._await_shutdown)

    def _await_shutdown(self):
        """The worker unwinds through ManagedLlamaServer's context manager, so
        by the time the thread is gone the server is down and the VRAM freed.
        Only then is it safe to close the window."""
        if self._busy():
            self.after(200, self._await_shutdown)
            return
        self._stop_timer()
        self.destroy()
