"""
app.py
------
The Seiyuu Audition window: does this voice suit this book at these
settings?

The manual version of this took a hand-counted character total, a
hand-written infer.py command line, a filename invented to encode the
parameters, then Explorer plus a media player plus Notepad to hear the
result against the source. Everything in that sentence except "hear the
result" is what this window removes.

Shape of it:

    1  mode + the text            one text, so samples are comparable
    2  seiyuu                     dropdown over seiyuu/list, or Browse
    3  tuning                     the generator's Advanced parameters
    4  temp folder                where working files go, and whether they stay
    5  samples                    one card per (seiyuu + parameters), then Run
    6  log
       close

Mode and text are audition-wide; everything in sections 2 and 3 belongs to
the SELECTED sample, and those sections are its editor. Adding a sample
clones the current one, so "same voice, one setting different" - the
comparison this tool exists for - is two clicks.

Run it with:   uv run python app.py
"""

import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

import audition
import results_window

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The generator's palette (ui_common.py), so the three tools look related
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
TOGGLE_ON = "#34C773"
DONE = "#1E8B4E"
NEUTRAL_BORDER = "#D8D8DE"
FAIL = "#C4453C"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"

MODE_LABELS = {"simple": "Simple", "e2e": "E2E Simulation"}
MODE_KEYS = {v: k for k, v in MODE_LABELS.items()}

# Everything that makes one sample different from another. Mode and text
# are deliberately absent - they are audition-wide.
SAMPLE_KEYS = (
    "speaker_path", "duration_scale", "no_trim_tail", "seed_enabled",
    "seed_value", "max_chunk_length", "silence_duration_sentence",
    "silence_duration_paragraph", "silence_duration_section",
)


class Spinner(ctk.CTkFrame):
    """Entry with - and + either side. The duration scale is nudged a
    tenth at a time all day long in this tool, and making that a click
    rather than a select-and-retype is most of why the window exists."""

    def __init__(self, parent, variable, step, minval, maxval, decimals=1,
                 width=74):
        super().__init__(parent, fg_color="transparent")
        self.var = variable
        self.step = step
        self.minval = minval
        self.maxval = maxval
        self.decimals = decimals
        self.buttons = []
        self.minus = self._button("−", -1)
        self.entry = ctk.CTkEntry(self, textvariable=variable, width=width,
                                  height=32, corner_radius=8, border_width=1,
                                  border_color=ENTRY_BORDER, fg_color=CARD,
                                  text_color=ENTRY_TEXT, justify="center")
        self.entry.pack(side="left", padx=4)
        self.plus = self._button("+", 1)

    def _button(self, glyph, direction):
        button = ctk.CTkButton(
            self, text=glyph, width=30, height=32, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
            border_width=1, border_color=NEUTRAL_BORDER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.nudge(direction))
        button.pack(side="left")
        self.buttons.append(button)
        return button

    def nudge(self, direction):
        try:
            value = float(self.var.get())
        except ValueError:
            value = self.minval
        value = max(self.minval, min(self.maxval, value + direction * self.step))
        self.var.set(f"{value:.{self.decimals}f}" if self.decimals
                     else str(int(round(value))))

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state,
                             text_color=ENTRY_TEXT if enabled else SUBTITLE)
        for button in self.buttons:
            button.configure(state=state)


class SampleCard(ctk.CTkFrame):
    """One (seiyuu + parameters) pairing. The selected one is what
    sections 2 and 3 are editing - shown by the border, because a summary
    that silently belongs to a different card is the one way this design
    can mislead."""

    def __init__(self, parent, number, on_select):
        super().__init__(parent, fg_color=CARD, corner_radius=10,
                         border_width=1, border_color=BORDER)
        self.number = number

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 0))
        self.name_label = ctk.CTkLabel(
            head, text=f"SAMPLE {number}", text_color=TITLE, anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"))
        self.name_label.pack(side="left")
        # Says, before any GPU time is spent, whether Generate would have
        # to render this one or already has it on disk.
        self.status_label = ctk.CTkLabel(head, text="", text_color=SUBTITLE,
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(side="right")

        self.summary_label = ctk.CTkLabel(
            self, text="", text_color=SUBTITLE, anchor="w", justify="left",
            font=ctk.CTkFont(size=11))
        self.summary_label.pack(fill="x", padx=14, pady=(2, 11))

        for widget in (self, head, self.name_label, self.status_label,
                       self.summary_label):
            widget.bind("<Button-1>", lambda _e: on_select(number))

    def update_view(self, nickname, summary, selected, status=("", SUBTITLE)):
        self.name_label.configure(text=f"SAMPLE {self.number}  ·  {nickname}")
        self.summary_label.configure(text=summary)
        self.status_label.configure(text=status[0], text_color=status[1])
        self.configure(border_width=2 if selected else 1,
                       border_color=ACCENT if selected else BORDER,
                       fg_color=PASTEL_VIOLET if selected else CARD)


class AuditionApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Seiyuu Audition")
        self.geometry("1040x960")
        self.minsize(900, 660)
        self.configure(fg_color=BG)

        icon = os.path.join(SCRIPT_DIR, "audition_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass        # a missing or odd icon must never stop the tool

        self.settings = audition.load_settings()
        self.mode = self.settings["mode"]
        self.samples = [self._sample_from_settings()]
        self.selected = 1
        self.cards = []
        self.results_window = None

        self._queue = queue.Queue()
        self._busy = False
        self._cancel = threading.Event()
        self._proc = None
        self._loading = False
        self._preview_job = None
        # False until _build() has run, so the card status does not go
        # looking for widgets that do not exist yet.
        self._built = False
        # Exactly what this session wrote under the temp folder. Cleanup
        # deletes this list and nothing else - see audition.remove_paths.
        self._created = []

        self.mode_var = ctk.StringVar(value=MODE_LABELS[self.mode])
        self.speaker_choice_var = ctk.StringVar(value="")
        self.vars = {
            "speaker_path": ctk.StringVar(value=self.settings["speaker_path"]),
            "duration_scale": ctk.StringVar(
                value=f"{float(self.settings['duration_scale']):.1f}"),
            "max_chunk_length": ctk.StringVar(
                value=str(int(self.settings["max_chunk_length"]))),
            "seed_value": ctk.StringVar(value=str(self.settings["seed_value"])),
            "silence_duration_sentence": ctk.StringVar(
                value=f"{float(self.settings['silence_duration_sentence']):.1f}"),
            "silence_duration_paragraph": ctk.StringVar(
                value=f"{float(self.settings['silence_duration_paragraph']):.1f}"),
            "silence_duration_section": ctk.StringVar(
                value=f"{float(self.settings['silence_duration_section']):.1f}"),
        }
        self.no_trim_tail_var = ctk.IntVar(
            value=1 if self.settings["no_trim_tail"] else 0)
        self.seed_enabled_var = ctk.IntVar(
            value=1 if self.settings["seed_enabled"] else 0)
        self.temp_root_var = ctk.StringVar(value=self.settings["temp_root"])
        self.clean_var = ctk.IntVar(
            value=1 if self.settings["clean_temp_on_exit"] else 0)

        self._build()
        self._built = True
        self._wire_traces()
        self.refresh_speakers()
        self.apply_mode()
        self.refresh_cards()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain)

    # ------------------------------------------------------------- samples
    def _sample_from_settings(self):
        sample = {key: self.settings[key] for key in SAMPLE_KEYS}
        sample["duration_scale"] = f"{float(sample['duration_scale']):.1f}"
        sample["max_chunk_length"] = str(int(sample["max_chunk_length"]))
        sample["seed_value"] = str(sample["seed_value"])
        for key in ("silence_duration_sentence", "silence_duration_paragraph",
                    "silence_duration_section"):
            sample[key] = f"{float(sample[key]):.1f}"
        return sample

    def current(self):
        return self.samples[self.selected - 1]

    # --------------------------------------------------------------- build
    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _section_head(self, card, number, title, hint=""):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(13, 8))
        ctk.CTkLabel(row, text=str(number), width=26, height=26,
                     corner_radius=8, fg_color=PASTEL_GREY,
                     text_color="#78767F",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(row, text="  " + title.upper(), text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        label = ctk.CTkLabel(row, text=hint, text_color=SUBTITLE,
                             font=ctk.CTkFont(size=11))
        label.pack(side="right")
        return label

    def _field(self, parent, label, widget_builder, width=None):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(side="left", padx=(0, 16))
        caption = ctk.CTkLabel(box, text=label, text_color=SUBTITLE, anchor="w",
                               font=ctk.CTkFont(size=11, weight="bold"))
        caption.pack(anchor="w", pady=(0, 3))
        widget = widget_builder(box)
        return caption, widget

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        header.pack(fill="x")
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=13)
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Seiyuu Audition", text_color=TITLE,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        self.head_sub = ctk.CTkLabel(
            titles, text="One text, one or more voices - judged by ear",
            text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.head_sub.pack(anchor="w")

        # Generate lives up here, not down in section 5 with Add and Reset,
        # for one reason: the five sections do not fit on a screen, and the
        # primary action of a window must never be the thing you have to
        # scroll to find. Add/Reset stay beside the sample cards, where
        # what they act on is visible.
        self.run_button = ctk.CTkButton(
            inner, text="▶  Generate sample(s)", height=38, width=190,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_generate)
        self.run_button.pack(side="right")
        self.cancel_button = ctk.CTkButton(
            inner, text="Cancel", height=38, width=110, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=FAIL, border_width=1,
            border_color=NEUTRAL_BORDER, state="disabled",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_cancel)
        self.cancel_button.pack(side="right", padx=(0, 10))

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(14, 0))
        body = self.body

        self._build_mode(body)
        self._build_seiyuu(body)
        self._build_tuning(body)
        self._build_temp(body)
        self._build_samples(body)
        self._build_log()

    def _build_mode(self, parent):
        card = self._card(parent)
        self.chunk_hint = self._section_head(card, 1, "Mode & text")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 10))
        self.mode_switch = ctk.CTkSegmentedButton(
            row, values=[MODE_LABELS["simple"], MODE_LABELS["e2e"]],
            variable=self.mode_var, height=34, corner_radius=8,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD, unselected_hover_color=BG,
            text_color=ENTRY_TEXT, font=ctk.CTkFont(size=12, weight="bold"),
            command=self.on_mode_clicked)
        self.mode_switch.pack(side="left")
        ctk.CTkButton(row, text="Load from .txt…", width=130, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=self.on_load_text).pack(side="right")

        self.mode_note = ctk.CTkLabel(
            card, text="", text_color=SUBTITLE, anchor="w", justify="left",
            font=ctk.CTkFont(size=11))
        self.mode_note.pack(fill="x", padx=16, pady=(0, 6))

        self.text_box = ctk.CTkTextbox(
            card, height=150, border_width=1, border_color=ENTRY_BORDER,
            fg_color="#FCFCFD", text_color=ENTRY_TEXT, corner_radius=8,
            font=ctk.CTkFont(size=14), wrap="word")
        self.text_box.pack(fill="x", padx=16, pady=(0, 13))
        self.text_box.insert("1.0", self.settings["text"])
        self.text_box.bind("<KeyRelease>", lambda _e: self.schedule_preview())

    def _build_seiyuu(self, parent):
        card = self._card(parent)
        self._section_head(card, 2, "Seiyuu",
                           "from " + audition.speaker_list_dir(
                               self.settings["irodori_root"]))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 13))

        self.speaker_menu = ctk.CTkOptionMenu(
            row, values=["(none found)"], variable=self.speaker_choice_var,
            width=250, height=34, corner_radius=8, fg_color=CARD,
            button_color=CARD, button_hover_color=BG, text_color=ENTRY_TEXT,
            dropdown_fg_color=CARD, command=self.on_speaker_chosen)
        self.speaker_menu.pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="↻", width=34, height=34, corner_radius=8,
                      fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
                      border_width=1, border_color=NEUTRAL_BORDER,
                      command=self.refresh_speakers).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Browse…", width=96, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=self.on_browse_speaker).pack(side="right")
        ctk.CTkEntry(row, textvariable=self.vars["speaker_path"], height=34,
                     corner_radius=8, border_width=1, border_color=ENTRY_BORDER,
                     fg_color=CARD, text_color=ENTRY_TEXT).pack(
            side="left", fill="x", expand=True, padx=(0, 8))

    def _build_tuning(self, parent):
        card = self._card(parent)
        self._section_head(card, 3, "Tuning parameters",
                           "the generator's Advanced page")

        # The e2e-only controls, greyed out in simple mode. A chunk length
        # that does nothing is worse than one that is not there: it looks
        # like it was applied.
        self.e2e_widgets = []

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(0, 10))

        self._field(top, "Duration scale", lambda box: self._spin(
            box, "duration_scale", 0.1, 0.5, 3.0, 1))

        caption, spinner = self._field(top, "Max chunk length", lambda box: self._spin(
            box, "max_chunk_length", 10, 20, 200, 0))
        self.e2e_widgets.append((caption, spinner))

        trim_box = ctk.CTkFrame(top, fg_color="transparent")
        trim_box.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(trim_box, text="Trim tail", text_color=SUBTITLE, anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
            anchor="w", pady=(0, 3))
        self.trim_switch = ctk.CTkSwitch(
            trim_box, text="", variable=self.no_trim_tail_var, onvalue=0,
            offvalue=1, width=46, switch_width=42, switch_height=22,
            progress_color=TOGGLE_ON, command=self.write_back)
        self.trim_switch.pack(anchor="w", pady=(4, 0))

        seed_box = ctk.CTkFrame(top, fg_color="transparent")
        seed_box.pack(side="left")
        ctk.CTkLabel(seed_box, text="Fixed seed", text_color=SUBTITLE, anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
            anchor="w", pady=(0, 3))
        seed_row = ctk.CTkFrame(seed_box, fg_color="transparent")
        seed_row.pack(anchor="w")
        ctk.CTkSwitch(seed_row, text="", variable=self.seed_enabled_var,
                      onvalue=1, offvalue=0, width=46, switch_width=42,
                      switch_height=22, progress_color=TOGGLE_ON,
                      command=self.on_seed_toggled).pack(side="left", pady=(4, 0))
        self.seed_entry = ctk.CTkEntry(
            seed_row, textvariable=self.vars["seed_value"], width=110, height=32,
            corner_radius=8, border_width=1, border_color=ENTRY_BORDER,
            fg_color=CARD, text_color=ENTRY_TEXT)

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(0, 13))
        for key, label in (("silence_duration_sentence", "Sentence silence"),
                           ("silence_duration_paragraph", "Paragraph silence"),
                           ("silence_duration_section", "Section silence")):
            caption, spinner = self._field(bottom, label, lambda box, k=key:
                                           self._spin(box, k, 0.1, 0.0, 5.0, 1))
            self.e2e_widgets.append((caption, spinner))

        self.on_seed_toggled()

    def _spin(self, parent, key, step, minval, maxval, decimals):
        spinner = Spinner(parent, self.vars[key], step, minval, maxval, decimals)
        spinner.pack(anchor="w")
        return spinner

    def _build_temp(self, parent):
        card = self._card(parent)
        self._section_head(card, 4, "Temp folder",
                           "working files, so you can look when it sounds wrong")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkEntry(row, textvariable=self.temp_root_var, height=34,
                     corner_radius=8, border_width=1, border_color=ENTRY_BORDER,
                     fg_color=CARD, text_color=ENTRY_TEXT).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Browse…", width=96, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=self.on_browse_temp).pack(side="right")

        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 13))
        ctk.CTkLabel(foot, text="Samples land in "
                               "YYYYMMDD\\<seiyuu>\\sample_001.wav",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.clean_switch = ctk.CTkSwitch(
            foot, text="delete this session's files on exit",
            variable=self.clean_var, onvalue=1, offvalue=0,
            switch_width=42, switch_height=22, progress_color=TOGGLE_ON,
            text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.clean_switch.pack(side="right")

    def _build_samples(self, parent):
        card = self._card(parent)
        self.samples_hint = self._section_head(card, 5, "Samples", "")

        self.cards_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.cards_frame.pack(fill="x", padx=16, pady=(0, 10))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 13))
        ctk.CTkButton(actions, text="Add Sample", width=120, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=self.on_add_sample).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Reset all Samples", width=150, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=self.on_reset_samples).pack(side="left",
                                                          padx=(0, 8))
        # Generate reuses any sample that is already on disk unchanged.
        # This is the way to ask for fresh draws anyway - which, on a random
        # seed, means genuinely different takes of the same parameters.
        ctk.CTkButton(actions, text="Regenerate all", width=130, height=34,
                      corner_radius=8, fg_color=CARD, hover_color=BG,
                      text_color=ENTRY_TEXT, border_width=1,
                      border_color=NEUTRAL_BORDER,
                      command=lambda: self.on_generate(force=True)).pack(
            side="left")
        ctk.CTkLabel(actions, text="Generate is in the title bar, above",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="right")

    def _build_log(self):
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkButton(bar, text="Close", width=110, height=34, corner_radius=8,
                      fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
                      border_width=1, border_color=NEUTRAL_BORDER,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.on_close).pack(side="right", padx=18, pady=11)
        self.results_button = ctk.CTkButton(
            bar, text="Show results", width=130, height=34, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
            border_width=1, border_color=NEUTRAL_BORDER, state="disabled",
            command=self.on_show_results)
        self.results_button.pack(side="right", pady=11)

        wrap = ctk.CTkFrame(self, fg_color=LOG_BG, corner_radius=0, height=160)
        wrap.pack(fill="x", side="bottom")
        wrap.pack_propagate(False)
        self.log_box = ctk.CTkTextbox(wrap, fg_color=LOG_BG, text_color=LOG_FG,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      border_width=0, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    # -------------------------------------------------------------- traces
    def _wire_traces(self):
        """Every editor control writes straight into the selected sample.
        `_loading` blocks that while the editor is being filled FROM a
        sample, or selecting a card would immediately overwrite it with
        the values still on screen."""
        for key, var in self.vars.items():
            var.trace_add("write", lambda *_a: self.write_back())
        self.seed_enabled_var.trace_add("write", lambda *_a: self.write_back())
        self.no_trim_tail_var.trace_add("write", lambda *_a: self.write_back())

    def write_back(self):
        if self._loading:
            return
        sample = self.current()
        for key, var in self.vars.items():
            sample[key] = var.get()
        sample["seed_enabled"] = bool(self.seed_enabled_var.get())
        sample["no_trim_tail"] = bool(self.no_trim_tail_var.get())
        self.refresh_cards()
        self.schedule_preview()

    def load_into_editor(self, sample):
        self._loading = True
        try:
            for key, var in self.vars.items():
                var.set(str(sample[key]))
            self.seed_enabled_var.set(1 if sample["seed_enabled"] else 0)
            self.no_trim_tail_var.set(1 if sample["no_trim_tail"] else 0)
        finally:
            self._loading = False
        self.on_seed_toggled()
        self.refresh_cards()

    # ------------------------------------------------------------ refresh
    def refresh_speakers(self):
        self.speakers = audition.list_speakers(self.settings["irodori_root"])
        names = [nickname for nickname, _path in self.speakers]
        self.speaker_menu.configure(values=names or ["(none found)"])
        current = self.vars["speaker_path"].get()
        match = next((n for n, p in self.speakers
                      if os.path.normcase(p) == os.path.normcase(current)), None)
        self.speaker_choice_var.set(match or (names[0] if names and not current
                                              else ""))
        if not current and names:
            self.vars["speaker_path"].set(self.speakers[0][1])

    def refresh_cards(self):
        while len(self.cards) < len(self.samples):
            card = SampleCard(self.cards_frame, len(self.cards) + 1,
                              self.on_select_sample)
            card.pack(fill="x", pady=(0, 8))
            self.cards.append(card)
        while len(self.cards) > len(self.samples):
            self.cards.pop().destroy()
        for i, (card, sample) in enumerate(zip(self.cards, self.samples), start=1):
            card.update_view(audition.nickname_for(sample["speaker_path"]),
                             results_window.summarise(
                                 self._display_spec(sample), self.mode),
                             i == self.selected,
                             self._card_status(sample, i))
        self.samples_hint.configure(
            text=f"{len(self.samples)} sample(s) · editing SAMPLE "
                 f"{self.selected}")

    def _card_status(self, sample, number):
        """"on disk" or "will generate", worked out from the same
        fingerprint the generator uses, so what the card promises and what
        Generate does cannot disagree.

        Everything here is deliberately quiet on failure: this runs on
        every keystroke, and a half-typed number is normal, not an error
        worth shouting about."""
        text = self.text_box.get("1.0", "end").strip() if self._built else ""
        temp_root = self.temp_root_var.get().strip()
        if not text or not temp_root:
            return ("", SUBTITLE)
        try:
            spec = self.parse_sample(sample, number)
        except ValueError:
            return ("check parameters", FAIL)
        spec["text"] = text
        settings = dict(self.settings)
        settings["temp_root"] = temp_root
        try:
            ready = audition.existing_sample(spec, settings, number)
        except OSError:
            return ("", SUBTITLE)
        return ("on disk · will be reused", DONE) if ready \
            else ("will generate", SUBTITLE)

    def _display_spec(self, sample):
        """A sample as results_window.summarise() wants it - values as
        typed, no parsing, because a half-typed number must not throw while
        you are still typing it."""
        spec = dict(sample)
        spec["nickname"] = audition.nickname_for(sample["speaker_path"])
        return spec

    def apply_mode(self):
        simple = self.mode == "simple"
        for caption, widget in self.e2e_widgets:
            caption.configure(text_color=SUBTITLE if not simple else "#C4C4CC")
            widget.set_enabled(not simple)
        self.mode_note.configure(text=(
            "Simple: the text below is spoken verbatim - no chunking, no "
            "bracket or dash rules, one wav."
            if simple else
            "E2E: the text runs through the generator's real chunking and "
            "cleaning, then the chunks are stitched with silences."))
        self.update_preview()
        self.refresh_cards()

    def schedule_preview(self):
        """Debounced: build_chunks() is fast, but running it on every
        keystroke of a long excerpt is still wasted work."""
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(250, self.update_preview)

    def update_preview(self):
        self._preview_job = None
        text = self.text_box.get("1.0", "end").strip()
        if self.mode == "simple":
            self.chunk_hint.configure(text=f"{len(text)} characters")
            return
        try:
            length = int(float(self.vars["max_chunk_length"].get()))
        except ValueError:
            self.chunk_hint.configure(text="chunk length is not a number")
            return
        try:
            chunks = audition.chunk_preview(text, length)
        except Exception as e:
            self.chunk_hint.configure(text=f"cannot chunk: {e}")
            return
        if not chunks:
            self.chunk_hint.configure(text="no text yet")
            return
        longest = max(len(c["display_text"]) for c in chunks)
        self.chunk_hint.configure(
            text=f"{len(text)} characters → {len(chunks)} chunk(s) at "
                 f"length {length}, longest {longest}")

    # ------------------------------------------------------------- actions
    def on_speaker_chosen(self, nickname):
        path = next((p for n, p in self.speakers if n == nickname), None)
        if path:
            self.vars["speaker_path"].set(path)

    def on_browse_speaker(self):
        start = audition.speaker_list_dir(self.settings["irodori_root"])
        chosen = filedialog.askopenfilename(
            title="Pick a speaker file",
            initialdir=start if os.path.isdir(start) else None,
            filetypes=[("Speaker", "*.safetensors"), ("All files", "*.*")])
        if chosen:
            self.vars["speaker_path"].set(os.path.normpath(chosen))
            self.refresh_speakers()

    def on_browse_temp(self):
        chosen = filedialog.askdirectory(title="Pick a temp folder")
        if chosen:
            self.temp_root_var.set(os.path.normpath(chosen))

    def on_load_text(self):
        chosen = filedialog.askopenfilename(
            title="Load audition text",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not chosen:
            return
        try:
            with open(chosen, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("Could not read", str(e))
            return
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self.log(f"loaded {len(text)} characters from {os.path.basename(chosen)}")
        self.update_preview()

    def on_mode_clicked(self, label):
        chosen = MODE_KEYS[label]
        if chosen == self.mode:
            return
        # One audition is one mode: a simple sample and an e2e sample are
        # not comparable, so the samples cannot survive the switch.
        if len(self.samples) > 1 and not self.confirm_clear(
                "Switching mode will clear your samples"):
            self.mode_var.set(MODE_LABELS[self.mode])
            return
        self.mode = chosen
        self.reset_samples()
        self.apply_mode()
        self.log(f"mode: {MODE_LABELS[chosen]}")

    def confirm_clear(self, what):
        return messagebox.askyesno(
            "Clear samples?",
            f"{what}.\n\nYou have {len(self.samples)} samples set up. "
            f"Continue?")

    def reset_samples(self):
        self.samples = [self.current()]
        self.selected = 1
        self.refresh_cards()

    def on_reset_samples(self):
        if len(self.samples) > 1 and not self.confirm_clear(
                "This will clear your samples"):
            return
        self.reset_samples()
        self.log("samples reset to one")

    def on_add_sample(self):
        if self._busy:
            return
        clone = dict(self.current())
        self.samples.append(clone)
        self.selected = len(self.samples)
        self.load_into_editor(clone)
        self.log(f"added SAMPLE {self.selected} - change a parameter to make "
                 f"it differ")

    def on_select_sample(self, number):
        if self._busy or number == self.selected:
            return
        self.selected = number
        self.load_into_editor(self.current())

    def on_seed_toggled(self):
        if self.seed_enabled_var.get():
            self.seed_entry.pack(side="left", padx=(8, 0), pady=(4, 0))
        else:
            self.seed_entry.pack_forget()

    def on_show_results(self):
        if self.results_window:
            self.results_window.deiconify()
            self.results_window.lift()

    # ---------------------------------------------------------- generation
    def parse_sample(self, sample, index):
        """Turns a sample's typed values into what the engine wants, or
        raises ValueError naming the sample - a bad number must say WHICH
        card it is on, or you hunt for it."""
        spec = {"mode": self.mode,
                "nickname": audition.nickname_for(sample["speaker_path"]),
                "speaker_path": sample["speaker_path"],
                "no_trim_tail": bool(sample["no_trim_tail"]),
                "seed_enabled": bool(sample["seed_enabled"])}
        numbers = {"duration_scale": float, "seed_value": int,
                   "max_chunk_length": int,
                   "silence_duration_sentence": float,
                   "silence_duration_paragraph": float,
                   "silence_duration_section": float}
        for key, caster in numbers.items():
            try:
                spec[key] = caster(float(sample[key]))
            except (TypeError, ValueError):
                raise ValueError(f"SAMPLE {index}: \"{sample[key]}\" is not a "
                                 f"valid {key.replace('_', ' ')}.")
        if spec["duration_scale"] <= 0:
            raise ValueError(f"SAMPLE {index}: duration scale must be above 0.")
        if spec["max_chunk_length"] < 20:
            raise ValueError(f"SAMPLE {index}: max chunk length must be at "
                             f"least 20.")
        if not spec["speaker_path"] or not os.path.isfile(spec["speaker_path"]):
            raise ValueError(f"SAMPLE {index}: the speaker file is missing.\n\n"
                             f"{spec['speaker_path'] or '(nothing chosen)'}")
        return spec

    def on_generate(self, force=False):
        if self._busy:
            return
        text = self.text_box.get("1.0", "end").strip()
        if not text:
            messagebox.showerror("No text", "Type or load the text the seiyuu "
                                            "should read.")
            return
        temp_root = self.temp_root_var.get().strip()
        if not temp_root:
            messagebox.showerror("No temp folder",
                                 "Pick a folder for the working files.")
            return
        try:
            specs = [self.parse_sample(s, i)
                     for i, s in enumerate(self.samples, start=1)]
        except ValueError as e:
            messagebox.showerror("Check the parameters", str(e))
            return
        for spec in specs:
            spec["text"] = text

        try:
            os.makedirs(temp_root, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Temp folder", f"Could not create {temp_root}:\n{e}")
            return

        # A wav being played is a wav Windows will not let ffmpeg overwrite.
        results_window.stop_playback()
        self.persist()

        settings = dict(self.settings)
        settings["temp_root"] = temp_root

        self._busy = True
        self._cancel.clear()
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.head_sub.configure(text="Generating…")
        threading.Thread(target=self._worker, args=(specs, settings, force),
                         daemon=True).start()

    def _worker(self, specs, settings, force=False):
        results = []
        reused = 0
        started = time.time()
        try:
            for i, spec in enumerate(specs, start=1):
                if self._cancel.is_set():
                    self.log("cancelled - remaining samples were not generated")
                    break
                self.log(f"SAMPLE {i}/{len(specs)} · {spec['nickname']} "
                         f"· scale {spec['duration_scale']}"
                         + (f" · chunk {spec['max_chunk_length']}"
                            if self.mode == "e2e" else ""))
                result = audition.generate_sample(
                    spec, settings, i, self.log, self._cancel.is_set,
                    self._register_proc, force=force)
                # Recorded whether or not it worked: a failed sample still
                # leaves working files, and cleanup has to know about them.
                # The fingerprint too, or cleanup would leave a stale one
                # pointing at a wav it had just deleted.
                paths = audition.sample_paths(settings["temp_root"],
                                              spec["nickname"], i)
                self._created.extend(p for p in (paths["work_dir"],
                                                 paths["wav"],
                                                 paths["fingerprint"]) if p)
                results.append(result)
                if result.get("reused"):
                    reused += 1
                    self.log(f"  reused {os.path.basename(result['wav'])} "
                             f"- nothing about it changed")
                elif result["ok"]:
                    self.log(f"  done in {result['seconds']:.0f}s → "
                             f"{os.path.basename(result['wav'])}")
                else:
                    self.log(f"  ! {result['error']}")
        except Exception as e:                          # never lose the reason
            self.log(f"! {type(e).__name__}: {e}")
            self.ui(lambda: messagebox.showerror("Failed", str(e)))
        finally:
            self._busy = False
            self._proc = None
            elapsed = time.time() - started
            if reused:
                self.log(f"{reused} sample(s) were already on disk unchanged "
                         f"- use Regenerate all to force fresh takes")
            self.ui(lambda: self._finish(specs, results, elapsed))

    def _register_proc(self, proc):
        self._proc = proc

    def _finish(self, specs, results, elapsed):
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        ok = sum(1 for r in results if r.get("ok"))
        reused = sum(1 for r in results if r.get("reused"))
        summary = f"{ok} of {len(specs)} sample(s) in {elapsed:.0f}s"
        if reused:
            summary += f" ({reused} reused)"
        self.head_sub.configure(
            text=summary if results
            else "One text, one or more voices - judged by ear")
        if not results:
            return
        self.results_button.configure(state="normal")
        if self.results_window is None:
            self.results_window = results_window.ResultsWindow(self)
        self.results_window.show(specs[0]["text"], self.mode, specs, results)

    def on_cancel(self):
        if not self._busy:
            return
        self._cancel.set()
        self.log("cancelling - stopping the TTS worker…")
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.cancel_button.configure(state="disabled")

    # ------------------------------------------------------------ plumbing
    def log(self, message):
        self._queue.put(("log", message))

    def ui(self, fn):
        """Runs fn on the Tk thread - worker threads must never touch
        widgets."""
        self._queue.put(("call", fn))

    def _drain(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("%H:%M:%S  ")
                                        + payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # --------------------------------------------------------------- close
    def persist(self):
        """Stores the editor's state, so re-opening the tool resumes where
        the last audition left off - the text most of all, since judging a
        new seiyuu means reading the SAME passage again."""
        data = dict(self.settings)
        sample = self.current()
        for key in SAMPLE_KEYS:
            data[key] = sample[key]
        data["mode"] = self.mode
        data["text"] = self.text_box.get("1.0", "end").strip()
        data["temp_root"] = self.temp_root_var.get().strip()
        data["clean_temp_on_exit"] = bool(self.clean_var.get())
        try:
            audition.save_settings(data)
            self.settings = audition.load_settings()
        except OSError as e:
            self.log(f"! could not save settings: {e}")

    def on_close(self):
        if self._busy and not messagebox.askyesno(
                "Still generating",
                "A sample is still being generated. Close anyway?"):
            return
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        results_window.stop_playback()
        self.persist()

        if self.clean_var.get():
            # The default, and therefore silent: being asked about deleting
            # scratch files every single time is the kind of prompt people
            # learn to click through.
            audition.remove_paths(self._created,
                                  stop_at=self.temp_root_var.get().strip())
        elif self._created:
            messagebox.showinfo(
                "Files kept",
                "Cleanup is turned off, so this session's samples are still "
                "on disk:\n\n"
                + self.temp_root_var.get().strip()
                + "\n\nDelete them yourself when you are done listening.")
        self.destroy()


if __name__ == "__main__":
    AuditionApp().mainloop()
