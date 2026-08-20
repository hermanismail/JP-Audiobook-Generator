"""
JP Audiobook Generator - Progress Window
-------------------------------------------
Replaces the raw PowerShell console that currently pops up on "Save & Run"
with a proper in-app progress window. See Documentation/progress_window_spec.md
for the full design spec (mockup review, enhancements, module choices).

Build order (per the approved spec):
    1. THIS FILE, standalone, fed by mock data via the __main__ block below
       - confirms the visual design matches the mockup before anything
       - touches the real pipeline.  <-- we are here
    2. Wire real subprocess redirection (CREATE_NO_WINDOW + pipe + thread +
       queue) in gui_settings.py's on_save_and_run(), so the PowerShell
       popup disappears and raw log lines flow into this window's log
       panel - no accurate percentage yet at that stage.
    3. Add a `PROGRESS <done> <total> <label>` stdout line to
       run_audiobook.py (alongside its existing prose logging) so
       set_chapter_progress()/set_stats() below can be driven by real
       numbers instead of mock data.
    4. Hook a real subprocess into on_cancel/on_open_output.
    5. (Optional, later) win11toast/windows-toasts completion notification.

Only step 1 is done here. Every public method on ProgressWindow
(append_log, set_chapter_progress, set_stats, set_state) is written as
the exact API a queue-draining self.after() loop would call in step 2 -
so wiring it up later shouldn't require changing this class, only
whatever creates/feeds it.

Dependencies: customtkinter only (already in this project's GUI venv,
same as gui_settings.py - nothing new to install for this step).

Run standalone for a visual demo:
    uv run python progress_window.py
"""

import os
import time

import customtkinter as ctk

from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ACCENT, COLOR_ENTRY_BORDER, COLOR_BTN_NEUTRAL_BORDER,
    COLOR_BTN_NEUTRAL_TEXT, IconBadge,
)

# Computed locally (identical to gui_settings.py's own SCRIPT_DIR/ICON_PATH,
# since both files live in the same folder) rather than imported from
# gui_settings.py - importing from it here is what caused the circular
# import (gui_settings.py -> progress_window.py -> gui_settings.py) when
# Save & Run was wired up to launch ProgressWindow directly.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(SCRIPT_DIR, "audiobook_icon.ico")

# ---------------------------------------------------------------------------
# Design tokens specific to the progress window
# ---------------------------------------------------------------------------
# Everything else in this window stays on gui_settings.py's light theme
# (imported above) - only the log panel itself is dark, matching
# Documentation/progress_window.png.

COLOR_LOG_BG = "#1C1C22"
COLOR_LOG_BORDER = "#2A2A32"
COLOR_LOG_TIMESTAMP = "#6FD3E6"
COLOR_LOG_PROCESSING = "#B9A3F7"
COLOR_LOG_TEXT = "#C7C7D1"
COLOR_LOG_SUCCESS = "#5FD98A"
COLOR_LOG_ERROR = "#F1706E"

# Rendered lines kept in the on-screen log widget. Once a run passes this
# many lines, the oldest are trimmed from the widget (see append_log) -
# the full, untrimmed log keeps going to disk regardless, so nothing is
# actually lost, only what's rendered gets capped for performance.
MAX_LOG_LINES = 1500

SPINNER_FRAMES = ["\u25D0", "\u25D3", "\u25D1", "\u25D2"]  # ◐ ◓ ◑ ◒

# (progress_color, track/fg_color) per state - the bar now encodes run
# status the same way the header icon and status pill already do: blue
# while running, green once done. failed/cancelled reuse those states'
# own red/gray so the bar doesn't contradict the rest of the header.
BAR_COLORS = {
    "running": ("#4DA6FF", "#E6F1FB"),
    "completed": ("#2FB668", "#E6F8ED"),
    "failed": ("#D85A5A", "#FCEAEA"),
    "cancelled": ("#7A7A85", "#F0F0F3"),
}

# One entry per ProgressWindow state. `spin` states animate the header
# icon via SPINNER_FRAMES instead of using a static `glyph`.
STATE_STYLES = {
    "running": dict(
        icon_bg="#EDEBFC", icon_color=COLOR_ACCENT, spin=True, glyph=None,
        title="Generating Audiobook...",
        subtitle="Please wait while chapters are being processed.",
        pill_bg="#FFF3DD", pill_text="#C98A2E", pill_label="In Progress"),
    "completed": dict(
        icon_bg="#E6F8ED", icon_color="#2FB668", spin=False, glyph="\u2714",
        title="Completed!",
        subtitle="All chapters have been processed successfully.",
        pill_bg="#E6F8ED", pill_text="#2FB668", pill_label="Completed"),
    "failed": dict(
        icon_bg="#FCEAEA", icon_color="#D85A5A", spin=False, glyph="\u2715",
        title="Generation Failed",
        subtitle="Something went wrong - check the process log below.",
        pill_bg="#FCEAEA", pill_text="#D85A5A", pill_label="Failed"),
    "cancelled": dict(
        icon_bg="#F0F0F3", icon_color="#7A7A85", spin=False, glyph="\u25A0",
        title="Cancelled",
        subtitle="Generation was stopped before all chapters finished.",
        pill_bg="#F0F0F3", pill_text="#7A7A85", pill_label="Cancelled"),
}


def default_log_path(temp_dir):
    """Convenience for step 2: a fresh timestamped log file path under
    <temp_dir>/logs/, so each run's full log is kept separately rather
    than one file being appended to forever."""
    log_dir = os.path.join(temp_dir, "logs")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(log_dir, f"run_{stamp}.log")


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class ProgressWindow(ctk.CTkToplevel):
    """Progress window shown while run_audiobook.py generates an
    audiobook. Non-modal by design (no grab_set()) - the person can alt-tab
    away from it while a long run is in progress, per the approved spec.
    """

    def __init__(self, master, total_chapters=0, log_file_path=None,
                 on_cancel=None, on_open_output=None, **kwargs):
        super().__init__(master, fg_color=COLOR_BG, **kwargs)
        self.title("JP Audiobook Generator - Progress")
        self.resizable(True, True)
        self.minsize(560, 620)
        self.geometry("700x780")
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass  # non-fatal cosmetic failure, same pattern as gui_settings.py

        self._on_cancel = on_cancel
        self._on_open_output = on_open_output
        self._log_file_path = log_file_path
        self._log_line_count = 0
        self._spinner_label = None
        self._spinner_index = 0
        self._spinner_job = None
        self._timer_job = None
        self._start_time = None
        self._state = None

        if self._log_file_path:
            log_dir = os.path.dirname(self._log_file_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)

        # Clicking the OS window X while a run is active goes through the
        # same path as the Cancel button, rather than silently killing the
        # subprocess or leaving an orphaned process behind (see
        # _on_close_button below).
        self.protocol("WM_DELETE_WINDOW", self._on_close_button)

        self._build_ui()
        self.set_stats(total=total_chapters, completed=0,
                        in_progress=1 if total_chapters else 0)
        self.set_state("running")
        self._start_timer()

    # ---------- UI construction ----------
    def _build_ui(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        outer.grid_columnconfigure(0, weight=1)

        # Row weight (relative growth share) : minsize (floor, in px, sized
        # against this window's own minsize(560, 620) - see __init__) both
        # encode the same approved ratio - header : progress card : stat
        # chips : process log : footer = 1 : 1.5 : 2.5 : 3.5 : 0.5. Because
        # the minsize floors are themselves already in that ratio, weight-
        # based distribution of any extra space (e.g. at the default
        # 700x780 size) reproduces the same ratio rather than fighting it.
        for grid_row, (weight, minsize) in enumerate(
                [(2, 57), (3, 86), (5, 143), (7, 201), (1, 29)]):
            outer.grid_rowconfigure(grid_row, weight=weight, minsize=minsize)

        self._build_header(outer, 0)
        self._build_progress_card(outer, 1)
        self._build_stats_row(outer, 2)
        self._build_log_card(outer, 3)
        self._build_footer(outer, 4)

    def _build_header(self, parent, grid_row):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))

        # A separate inner frame, packed with expand=True (no fill on the
        # y-axis) so it's vertically centered within the taller cell above,
        # rather than stuck to the top - this is also what fixed stat-chip
        # icons touching their card's top border (same technique, see
        # _build_stats_row).
        content = ctk.CTkFrame(cell, fg_color="transparent")
        content.pack(fill="x", expand=True)

        self.state_icon = IconBadge(
            content, "\u25D0", "#EDEBFC", text_color=COLOR_ACCENT,
            size=48, font_size=20, corner_radius=12)
        self.state_icon.pack(side="left", padx=(0, 14))

        text_frame = ctk.CTkFrame(content, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)
        self.title_label = ctk.CTkLabel(
            text_frame, text="", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"), anchor="w")
        self.title_label.pack(fill="x", anchor="w")
        self.subtitle_label = ctk.CTkLabel(
            text_frame, text="", text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=14), anchor="w", justify="left")
        self.subtitle_label.pack(fill="x", anchor="w")

        self.status_pill = ctk.CTkLabel(
            content, text="", corner_radius=14, fg_color="#FFF3DD", text_color="#C98A2E",
            font=ctk.CTkFont(size=12, weight="bold"), width=104, height=30)
        self.status_pill.pack(side="right", anchor="n")

    def _build_progress_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)

        top_row = ctk.CTkFrame(inner, fg_color="transparent")
        top_row.pack(fill="x")
        self.chapter_label = ctk.CTkLabel(
            top_row, text="Chapter 0 of 0", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=17, weight="bold"))
        self.chapter_label.pack(side="left")
        self.current_item_label = ctk.CTkLabel(
            top_row, text="", text_color=COLOR_ACCENT, font=ctk.CTkFont(size=14))
        self.current_item_label.pack(side="left", padx=(14, 0))
        self.percent_label = ctk.CTkLabel(
            top_row, text="0%", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=17, weight="bold"))
        self.percent_label.pack(side="right")

        # A single bar only - an earlier revision also had a secondary
        # "overall chapters" bar, but it was dropped during design review:
        # the same "N of M chapters done" info is already on the stat
        # chips below (Total Chapters / Completed), so the second bar was
        # pure duplication rather than adding anything. Thickness is a
        # deliberate test value - the progress card now has more headroom
        # (1.5 of 9 ratio units) than it did at 8px, so trying 12px again.
        self.progress_bar = ctk.CTkProgressBar(
            inner, height=12, corner_radius=6, progress_color=BAR_COLORS["running"][0],
            fg_color=BAR_COLORS["running"][1])
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", pady=(10, 0))

    def _build_stats_row(self, parent, grid_row):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        row.grid_rowconfigure(0, weight=1)
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="stat")

        self.stat_chips = {}

        # key -> (glyph, icon_bg, caption). The "in_progress" chip's glyph
        # changes with run state (see set_state) - everything else here
        # is static. Icon + number sit in one row, centered as a unit,
        # rather than the icon sitting alone above a left-aligned number.
        chip_defs = [
            ("total", "\U0001F4D6", "#8272F4", "Total Chapters"),
            ("completed", "\u2728", "#4FCB8F", "Completed"),
            ("in_progress", "\u25B6", "#4DA6FF", "In Progress"),
        ]
        for i, (key, glyph, bg, caption) in enumerate(chip_defs):
            chip = self._make_chip(row, i)
            inner = ctk.CTkFrame(chip, fg_color="transparent")
            inner.pack(expand=True)

            icon_row = ctk.CTkFrame(inner, fg_color="transparent")
            icon_row.pack()
            icon = IconBadge(icon_row, glyph, bg, size=48, font_size=20, corner_radius=13)
            icon.pack(side="left")
            # IconBadge places its glyph at a mathematically-dead-center
            # rely=0.5, but the open-book emoji (Total Chapters) renders
            # with extra empty space above its visual "ink" in Segoe UI
            # Emoji, so dead-center placement reads as slightly low for
            # this glyph specifically - nudge it up a touch to compensate.
            # (Other glyphs here haven't shown the same issue.)
            if key == "total":
                icon.winfo_children()[0].place_configure(rely=0.46)
            value_label = ctk.CTkLabel(icon_row, text="0", text_color=COLOR_TITLE,
                                        font=ctk.CTkFont(size=26, weight="bold"))
            value_label.pack(side="left", padx=(9, 0))

            caption_label = ctk.CTkLabel(
                inner, text=caption, text_color=COLOR_SUBTITLE,
                font=ctk.CTkFont(size=14), wraplength=120, justify="center")
            caption_label.pack(pady=(8, 0))

            self.stat_chips[key] = {"value": value_label, "caption": caption_label, "icon": icon}

        # Time chip has no icon, by design: dropping it gives the HH:MM:SS
        # value the full card width to grow into past the one-hour mark
        # (e.g. "01:00:00") - though since _format_duration always zero-
        # pads to 2 digits per field, the string is a fixed 8 characters
        # regardless, so this is really just a look, not a functional fix.
        time_chip = self._make_chip(row, 3)
        time_inner = ctk.CTkFrame(time_chip, fg_color="transparent")
        time_inner.pack(expand=True)
        time_value = ctk.CTkLabel(time_inner, text="00:00:00", text_color=COLOR_TITLE,
                                   font=ctk.CTkFont(size=28, weight="bold"))
        time_value.pack()
        time_caption = ctk.CTkLabel(
            time_inner, text="Elapsed Time", text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=14), wraplength=120, justify="center")
        time_caption.pack(pady=(8, 0))
        self.stat_chips["time"] = {"value": time_value, "caption": time_caption, "icon": None}

    @staticmethod
    def _make_chip(parent, column):
        """A stat-chip CTkFrame. Height comes entirely from the outer
        grid's stat-row allotment (see _build_ui/_build_stats_row's
        grid_rowconfigure calls) via sticky="nsew" - not from anything
        chip-local - so it stays consistent with the approved 1:1:3:3.5:0.5
        section-height ratio instead of self-sizing to its own content."""
        chip = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=14,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        chip.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        return chip

    def _build_log_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        header_row = ctk.CTkFrame(inner, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header_row, text="\U0001F4C4  Process Log", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(
            header_row, text="\U0001F5D1  Clear Log", width=100, height=28, corner_radius=7,
            fg_color="transparent", hover_color="#F0F0F3", border_width=1,
            border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
            font=ctk.CTkFont(size=11), command=self.clear_log,
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            inner, fg_color=COLOR_LOG_BG, corner_radius=10, border_width=1,
            border_color=COLOR_LOG_BORDER, wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True)

        # CTkTextbox doesn't expose a public tagged-insert/see API, so the
        # colored per-line log formatting below reaches into the plain
        # tkinter Text widget it wraps internally (self.log_box._textbox) -
        # a standard workaround for CTk "console"-style widgets. Worth a
        # quick smoke test against the installed customtkinter version,
        # since this relies on that internal attribute name staying stable.
        text = self.log_box._textbox
        text.tag_config("timestamp", foreground=COLOR_LOG_TIMESTAMP)
        text.tag_config("text", foreground=COLOR_LOG_TEXT)
        text.tag_config("processing", foreground=COLOR_LOG_PROCESSING)
        text.tag_config("success", foreground=COLOR_LOG_SUCCESS)
        text.tag_config("error", foreground=COLOR_LOG_ERROR)
        self.log_box.configure(state="disabled")

    def _build_footer(self, parent, grid_row):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=grid_row, column=0, sticky="nsew")
        # Rebuilt per state in _render_footer() - the button set genuinely
        # differs (Cancel vs. Open Output Folder+Close vs. just Close)
        # rather than one fixed row of buttons being enabled/disabled.
        self.footer = ctk.CTkFrame(cell, fg_color="transparent")
        self.footer.pack(fill="x", expand=True)

    def _render_footer(self, state):
        for w in self.footer.winfo_children():
            w.destroy()

        if state == "running":
            # Enhancement #1 from the spec review: a real Cancel button
            # instead of a greyed-out, unusable Close.
            ctk.CTkButton(
                self.footer, text="\u2715  Cancel", width=110, height=38, corner_radius=8,
                fg_color="transparent", hover_color="#FCEAEA", border_width=1,
                border_color="#D85A5A", text_color="#D85A5A",
                command=self._handle_cancel,
            ).pack(side="right")
        elif state == "completed":
            ctk.CTkButton(
                self.footer, text="Close", width=100, height=38, corner_radius=8,
                fg_color="#D85A5A", hover_color="#C24A4A", text_color="white",
                command=self.destroy,
            ).pack(side="right")
            ctk.CTkButton(
                self.footer, text="\U0001F4C1  Open Output Folder", width=190, height=38,
                corner_radius=8, fg_color="white", hover_color="#F5F5F8", border_width=1,
                border_color=COLOR_ENTRY_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
                command=self._handle_open_output,
            ).pack(side="right", padx=(0, 10))
        else:  # failed / cancelled
            ctk.CTkButton(
                self.footer, text="Close", width=100, height=38, corner_radius=8,
                fg_color="transparent", hover_color="#F0F0F3", border_width=1,
                border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
                command=self.destroy,
            ).pack(side="right")

    # ---------- Footer / window-close handlers ----------
    def _handle_cancel(self):
        self.append_log("Cancel requested by user...", tag="error")
        if self._on_cancel:
            self._on_cancel()
        else:
            # Standalone/demo mode (step 1) with no real subprocess wired
            # up yet - just reflect the cancelled state in the UI so the
            # button is still demoable on its own.
            self.set_state("cancelled")

    def _handle_open_output(self):
        if self._on_open_output:
            self._on_open_output()

    def _on_close_button(self):
        if self._state == "running":
            self._handle_cancel()
        else:
            self.destroy()

    # ---------- Public API (this is what step 2's queue-drain loop calls) ----------
    def set_state(self, state, title=None, subtitle=None):
        """state: one of 'running', 'completed', 'failed', 'cancelled'."""
        style = STATE_STYLES[state]
        self._state = state

        self.title_label.configure(text=title or style["title"])
        self.subtitle_label.configure(text=subtitle or style["subtitle"])
        self.status_pill.configure(
            text=style["pill_label"], fg_color=style["pill_bg"], text_color=style["pill_text"])

        self.state_icon.configure(fg_color=style["icon_bg"])
        # IconBadge's glyph label is its only child - update it directly
        # rather than rebuilding the badge each time state changes.
        glyph_label = self.state_icon.winfo_children()[0]
        glyph_label.configure(text_color=style["icon_color"])

        if style["spin"]:
            self._start_spinner(glyph_label)
        else:
            self._stop_spinner()
            glyph_label.configure(text=style["glyph"])

        # Stat-chip cosmetic tweaks tied to state (mirrors the mockup): the
        # "In Progress" chip's icon switches from a play glyph to a
        # checkmark once the run finishes, and "Elapsed Time" becomes
        # "Total Time" once the timer is frozen below.
        self._set_chip_icon_glyph("in_progress", "\u25B6" if state == "running" else "\u2714")
        self.stat_chips["time"]["caption"].configure(
            text="Elapsed Time" if state == "running" else "Total Time")

        bar_color, bar_track = BAR_COLORS[state]
        self.progress_bar.configure(progress_color=bar_color, fg_color=bar_track)

        if state in ("completed", "failed", "cancelled"):
            self._stop_timer(freeze=True)

        self._render_footer(state)

    def append_log(self, message, tag="text"):
        """Appends one timestamped line to both the on-screen log (subject
        to MAX_LOG_LINES trimming) and the full on-disk log file, if one
        was configured. `tag` controls the line's color: 'text' (default),
        'processing', 'success', or 'error'."""
        timestamp = time.strftime("[%H:%M:%S] ")

        if self._log_file_path:
            try:
                with open(self._log_file_path, "a", encoding="utf-8") as f:
                    f.write(timestamp + message + "\n")
            except OSError:
                pass  # a disk write failure shouldn't break the on-screen log

        text = self.log_box._textbox
        text.configure(state="normal")
        text.insert("end", timestamp, ("timestamp",))
        text.insert("end", message + "\n", (tag,))
        self._log_line_count += 1
        if self._log_line_count > MAX_LOG_LINES:
            # Trim only what's rendered - the file above already has the
            # untrimmed full line, so nothing is actually lost.
            overflow = self._log_line_count - MAX_LOG_LINES
            text.delete("1.0", f"{overflow + 1}.0")
            self._log_line_count = MAX_LOG_LINES
        text.see("end")
        text.configure(state="disabled")

    def clear_log(self):
        """Clears the on-screen log only - the full record on disk (if a
        log file was configured) is left intact, since that's what you'd
        actually go back to when debugging a chunking issue after the
        fact."""
        text = self.log_box._textbox
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.configure(state="disabled")
        self._log_line_count = 0

    def set_chapter_progress(self, chapter_index, total_chapters, current_item, percent):
        """chapter_index/total_chapters: 1-based 'Chapter X of Y'.
        current_item: short label, e.g. 'Processing: chapter_055'.
        percent: 0-100, the CURRENT chapter's own chunk progress."""
        self.chapter_label.configure(text=f"Chapter {chapter_index} of {total_chapters}")
        self.current_item_label.configure(text=current_item)
        self.percent_label.configure(text=f"{int(percent)}%")
        self.progress_bar.set(max(0.0, min(1.0, percent / 100)))

    def set_stats(self, total=None, completed=None, in_progress=None):
        if total is not None:
            self.stat_chips["total"]["value"].configure(text=str(total))
        if completed is not None:
            self.stat_chips["completed"]["value"].configure(text=str(completed))
        if in_progress is not None:
            self.stat_chips["in_progress"]["value"].configure(text=str(in_progress))

    # ---------- Internal: spinner + elapsed timer ----------
    def _set_chip_icon_glyph(self, key, glyph):
        self.stat_chips[key]["icon"].winfo_children()[0].configure(text=glyph)

    def _start_spinner(self, glyph_label):
        self._spinner_label = glyph_label
        self._spin_tick()

    def _spin_tick(self):
        self._spinner_label.configure(text=SPINNER_FRAMES[self._spinner_index % len(SPINNER_FRAMES)])
        self._spinner_index += 1
        self._spinner_job = self.after(180, self._spin_tick)

    def _stop_spinner(self):
        if self._spinner_job is not None:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None

    def _start_timer(self):
        self._start_time = time.time()
        self._tick_timer()

    def _tick_timer(self):
        elapsed = time.time() - self._start_time
        self.stat_chips["time"]["value"].configure(text=self._format_duration(elapsed))
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self, freeze=True):
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        if freeze and self._start_time is not None:
            self.stat_chips["time"]["value"].configure(
                text=self._format_duration(time.time() - self._start_time))

    @staticmethod
    def _format_duration(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Standalone visual demo (build-order step 1)
# ---------------------------------------------------------------------------
# Feeds the window a scripted sequence of fake log lines/progress updates
# via .after() timers - no real subprocess involved yet. Compare this
# against Documentation/progress_window.png to sign off on step 1 before
# step 2 (real subprocess wiring) starts.

if __name__ == "__main__":
    ctk.set_appearance_mode("light")

    # On a Windows display with scaling above 100%, CTk auto-scales widget
    # sizes/fonts to match the monitor's DPI, but self.geometry() below is
    # always literal pixels and is NOT auto-scaled to match. The two units
    # disagreeing is what squished the whole window in the last test run -
    # not just the stat-chip squares, but everything, since the window
    # ended up smaller (literal pixels) than the content it had to hold
    # (DPI-scaled pixels). Pinning both to 1:1 keeps them in the same
    # units regardless of the display's scaling setting.
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)

    root = ctk.CTk()
    root.withdraw()  # the demo only needs the Toplevel below, not a real main window

    demo_log_path = os.path.join(SCRIPT_DIR, "_demo_logs", "progress_demo.log")

    win = ProgressWindow(root, total_chapters=2, log_file_path=demo_log_path)

    # Deliberately NOT overriding win's own WM_DELETE_WINDOW handler here -
    # doing so would bypass the exact "closing while running asks for
    # confirmation via Cancel first" behavior this demo exists to show off.
    # Instead, just end the demo once the ProgressWindow actually closes.
    def _end_demo_on_win_destroy(event):
        if event.widget is win:
            root.destroy()
    win.bind("<Destroy>", _end_demo_on_win_destroy)

    CH1_CHUNKS = 8
    CH2_CHUNKS = 5


    def schedule(events):
        """events: list of (delay_seconds_from_previous_event, callback)."""
        cumulative = 0.0
        for delay, callback in events:
            cumulative += delay
            win.after(int(cumulative * 1000), callback)


    def chunk_event(chapter_idx, total_chapters, name, chunk_i, total_chunks,
                     sec, par, chars, silence, tags):
        def _do():
            win.append_log(
                f" Generating chunk {chunk_i}/{total_chunks} (sec {sec:03d} par {par:03d}, "
                f"{chars} chars, {silence}x silence before [{tags}])...", tag="text")
            win.set_chapter_progress(
                chapter_idx, total_chapters, f"Processing: {name}",
                percent=chunk_i / total_chunks * 100)
        return _do


    def chapter_start_event(chapter_idx, total_chapters, name, total_chunks, sections, paragraphs):
        def _do():
            win.append_log(f">>> Processing: {name}", tag="processing")
            win.append_log(
                f"Found {sections} section(s), {paragraphs} paragraph(s), "
                f"{total_chunks} TTS input chunk(s).", tag="text")
            win.set_chapter_progress(chapter_idx, total_chapters, f"Processing: {name}", percent=0)
        return _do


    def chapter_done_event(chapter_idx, total_chapters, name):
        def _do():
            win.append_log(
                "Detected TTS output sample rate: 48000Hz - generating matching silence.wav...",
                tag="text")
            win.append_log(f"Stitching {name} into final MP3...", tag="text")
            win.append_log(f"Done! Saved to: F:\\AUDIOBOOK_OUTPUT\\{name}.mp3", tag="success")
            win.append_log("Cleaning up temporary files in F:\\AUDIOBOOK_TMP...", tag="text")
            win.set_stats(completed=chapter_idx,
                           in_progress=1 if chapter_idx < total_chapters else 0)
        return _do


    def all_done_event():
        def _do():
            win.append_log(
                "All chapters processed successfully. Output saved to F:\\AUDIOBOOK_OUTPUT",
                tag="success")
            win.set_state("completed")
        return _do


    demo_events = [(0.3, chapter_start_event(1, 2, "chapter_055", CH1_CHUNKS, 2, 6))]
    for i in range(1, CH1_CHUNKS + 1):
        demo_events.append((0.5, chunk_event(
            1, 2, "chapter_055", i, CH1_CHUNKS, 2, (i - 1) // 2 + 1, 30 + i * 4, 2,
            "paragraph,bracket_open")))
    demo_events.append((0.4, chapter_done_event(1, 2, "chapter_055")))

    demo_events.append((0.5, chapter_start_event(2, 2, "chapter_056", CH2_CHUNKS, 2, 3)))
    for i in range(1, CH2_CHUNKS + 1):
        demo_events.append((0.5, chunk_event(
            2, 2, "chapter_056", i, CH2_CHUNKS, 1, i, 20 + i * 5, 1, "sentence")))
    demo_events.append((0.4, chapter_done_event(2, 2, "chapter_056")))
    demo_events.append((0.5, all_done_event()))

    schedule(demo_events)
    root.mainloop()
