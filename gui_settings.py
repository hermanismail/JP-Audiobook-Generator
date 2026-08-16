"""
JP Audiobook Generator - Settings GUI
---------------------------------------
A small Tkinter GUI for editing settings.json used by run_audiobook.py.

Usage (from PowerShell):
    py gui_settings.py
or via the provided Run-Settings.ps1 wrapper.

Requires only the Python standard library (tkinter, json, os, subprocess).
"""

import os
import sys
import json
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
RUN_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "run_audiobook.py")

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


class ToggleSwitch(tk.Canvas):
    """A simple clickable toggle switch widget (since ttk has no native one)."""

    def __init__(self, parent, initial=False, width=52, height=26,
                 on_color="#4cd964", off_color="#c7c7cc", command=None, **kwargs):
        super().__init__(parent, width=width, height=height,
                          highlightthickness=0, bd=0, **kwargs)
        self._on = initial
        # NOTE: do not name these self._w / self._h - tk.Widget already uses
        # self._w internally to store the widget's Tk pathname, and clobbering
        # it causes errors like: _tkinter.TclError: invalid command name "52"
        self._switch_width = width
        self._switch_height = height
        self._on_color = on_color
        self._off_color = off_color
        self._command = command
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def _draw(self):
        self.delete("all")
        pad = 2
        color = self._on_color if self._on else self._off_color
        w = self._switch_width
        h = self._switch_height
        r = h / 2
        # Rounded pill background
        self.create_oval(0, 0, h, h, fill=color, outline=color)
        self.create_oval(w - h, 0, w, h, fill=color, outline=color)
        self.create_rectangle(r, 0, w - r, h, fill=color, outline=color)
        # Knob
        knob_x = w - h + pad if self._on else pad
        self.create_oval(knob_x, pad, knob_x + h - 2 * pad,
                          h - pad, fill="white", outline="#dddddd")

    def _toggle(self, _event=None):
        self.set(not self._on)

    def set(self, value):
        self._on = bool(value)
        self._draw()
        if self._command:
            self._command(self._on)

    def get(self):
        return self._on


class SettingsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("JP Audiobook Generator - Settings")
        self.resizable(False, False)
        self.settings = load_settings()

        self.vars = {
            "input_folder": tk.StringVar(value=self.settings["input_folder"]),
            "output_folder": tk.StringVar(value=self.settings["output_folder"]),
            "temp_dir": tk.StringVar(value=self.settings["temp_dir"]),
            "model_path": tk.StringVar(value=self.settings["model_path"]),
            "speaker_path": tk.StringVar(value=self.settings["speaker_path"]),
            "silence_duration": tk.StringVar(value=str(self.settings["silence_duration"])),
            "uv_project_dir": tk.StringVar(value=self.settings["uv_project_dir"]),
        }

        self._build_ui()

    # ---------- UI construction ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        container = ttk.Frame(self, padding=16)
        container.grid(row=0, column=0, sticky="nsew")

        row = 0
        row = self._add_folder_row(container, row, "Input Folder (chapters):", "input_folder")
        row = self._add_folder_row(container, row, "Output Folder (MP3s):", "output_folder")
        row = self._add_folder_row(container, row, "Temp Folder:", "temp_dir")
        row = self._add_file_row(container, row, "Model Path (.safetensors):", "model_path",
                                  [("SafeTensors", "*.safetensors"), ("All files", "*.*")])
        row = self._add_file_row(container, row, "Speaker Path (.safetensors):", "speaker_path",
                                  [("SafeTensors", "*.safetensors"), ("All files", "*.*")])
        row = self._add_folder_row(container, row, "uv Project Folder (where 'uv' runs from):",
                                    "uv_project_dir")

        # Silence duration
        ttk.Label(container, text="Silence Duration (seconds):").grid(
            row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(container, textvariable=self.vars["silence_duration"], width=10)
        entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        # Toggle: keep temp files
        ttk.Label(container, text="Keep temp files after run:").grid(
            row=row, column=0, sticky="w", **pad)
        toggle_frame = ttk.Frame(container)
        toggle_frame.grid(row=row, column=1, sticky="w", **pad)
        initial_keep = not bool(self.settings.get("clean_temp_after_run", True))
        self.toggle = ToggleSwitch(toggle_frame, initial=initial_keep)
        self.toggle.pack(side="left")
        self.toggle_status_label = ttk.Label(toggle_frame, text=self._toggle_text(initial_keep))
        self.toggle_status_label.pack(side="left", padx=(8, 0))
        self.toggle._command = self._on_toggle_changed
        row += 1

        # Buttons
        btn_frame = ttk.Frame(container)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=(16, 0), sticky="e")

        ttk.Button(btn_frame, text="Save Settings", command=self.on_save).pack(
            side="left", padx=6)
        ttk.Button(btn_frame, text="Save & Run", command=self.on_save_and_run).pack(
            side="left", padx=6)
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(
            side="left", padx=6)

    def _toggle_text(self, keep_temp):
        return "ON (temp files kept)" if keep_temp else "OFF (temp files cleared)"

    def _on_toggle_changed(self, is_on):
        self.toggle_status_label.config(text=self._toggle_text(is_on))

    def _add_folder_row(self, container, row, label, key):
        pad = {"padx": 10, "pady": 6}
        ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(container, textvariable=self.vars[key], width=55)
        entry.grid(row=row, column=1, sticky="w", **pad)
        ttk.Button(container, text="Browse...",
                   command=lambda k=key: self._browse_folder(k)).grid(
            row=row, column=2, sticky="w", **pad)
        return row + 1

    def _add_file_row(self, container, row, label, key, filetypes):
        pad = {"padx": 10, "pady": 6}
        ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(container, textvariable=self.vars[key], width=55)
        entry.grid(row=row, column=1, sticky="w", **pad)
        ttk.Button(container, text="Browse...",
                   command=lambda k=key, ft=filetypes: self._browse_file(k, ft)).grid(
            row=row, column=2, sticky="w", **pad)
        return row + 1

    # ---------- Actions ----------
    def _browse_folder(self, key):
        current = self.vars[key].get()
        initialdir = current if os.path.isdir(current) else SCRIPT_DIR
        chosen = filedialog.askdirectory(title="Select Folder", initialdir=initialdir)
        if chosen:
            self.vars[key].set(os.path.normpath(chosen))

    def _browse_file(self, key, filetypes):
        current = self.vars[key].get()
        initialdir = os.path.dirname(current) if os.path.isfile(current) else SCRIPT_DIR
        chosen = filedialog.askopenfilename(title="Select File", initialdir=initialdir,
                                             filetypes=filetypes)
        if chosen:
            self.vars[key].set(os.path.normpath(chosen))

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
            "clean_temp_after_run": not self.toggle.get(),
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
