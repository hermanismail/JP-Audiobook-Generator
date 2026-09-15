"""
app.py
------
The Chapter Repair window: fix one bad chunk in a published chapter
without re-rendering the chapter.

    1  chapter     pick a book and chapter from the published library
    2  inspect     Whisper reads it back; chunks that do not match the
                   text they should have read are ranked worst-first
    3  chunk       the suspect one - its text, what was heard, and a play
                   button that includes a second either side
    4  regenerate  fresh takes of that chunk at new parameters, played
                   against the original
    5  apply       splice, re-encode, re-time sync.json and the .srt

Steps 2 and 3 are the point. Finding a hallucination by ear means
listening to a whole book; this narrows a 45-minute chapter to the
handful of chunks whose transcript disagrees with the script, which is a
few minutes of listening instead of a few hours.

The ranking is a shortlist, never a verdict - Whisper mishears too. It
tells you where to listen, and your ears decide.

Run it with:   uv run python app.py
"""

import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

import repair

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The generator's palette, so the four tools look related.
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
NEUTRAL_BORDER = "#D8D8DE"
DONE = "#1E8B4E"
WARN = "#B66A16"
FAIL = "#C4453C"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"


def play_wav(path):
    import winsound
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except RuntimeError:
        pass


def stop_playback():
    import winsound
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except RuntimeError:
        pass


def clock(seconds):
    """mm:ss.s - how you would read a position off a player."""
    minutes, rest = divmod(max(0.0, float(seconds)), 60)
    return f"{int(minutes):d}:{rest:04.1f}"


def parse_clock(text):
    """Accepts `14:32`, `14:32.5` or plain seconds, because you will be
    reading the number off whatever the player happens to show."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if ":" in text:
            minutes, seconds = text.rsplit(":", 1)
            return int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return None


class ChunkRow(ctk.CTkFrame):
    """One flagged chunk in the inspect list."""

    def __init__(self, parent, row, on_select):
        super().__init__(parent, fg_color=CARD, corner_radius=8,
                         border_width=1, border_color=BORDER)
        self.row = row
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(7, 0))

        ctk.CTkLabel(head, text=f"#{row['index']}", width=48, anchor="w",
                     text_color=TITLE,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text=clock(row["start"]), width=64, anchor="w",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")

        score = row.get("similarity")
        if score is None:
            # A capped-chunk row: nothing was transcribed, and it does not
            # need to be. Hitting the ceiling IS the finding.
            # Landing on the ceiling is not itself damage - what matters is
            # whether it had to speak faster than the book does.
            ratio = row.get("pace_ratio", 1.0)
            colour = FAIL if ratio >= 1.25 else (WARN if ratio >= 1.15
                                                 else SUBTITLE)
            ctk.CTkLabel(head, text=f"capped · {ratio:.2f}x pace",
                         width=120, anchor="w", text_color=colour,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(head,
                         text=f"{len(row['text'])} chars at "
                              f"{row['chars_per_sec']} ch/s vs the chapter's "
                              f"{row.get('pace')}  ·  needs ~{row['needs']}s",
                         anchor="w", text_color=SUBTITLE,
                         font=ctk.CTkFont(size=11)).pack(side="left")
        else:
            colour = FAIL if score < 0.5 else (WARN if score < 0.75 else SUBTITLE)
            ctk.CTkLabel(head, text=f"match {score:.0%}", width=84, anchor="w",
                         text_color=colour,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            length = row["length_ratio"]
            ctk.CTkLabel(head, text=("length n/a" if length is None
                                     else f"length {length:.2f}x"),
                         anchor="w", text_color=SUBTITLE,
                         font=ctk.CTkFont(size=11)).pack(side="left")

        self.expected = ctk.CTkLabel(
            self, text="script  " + row["text"], anchor="w", justify="left",
            text_color=ENTRY_TEXT, font=ctk.CTkFont(size=12), wraplength=820)
        self.expected.pack(fill="x", padx=10, pady=(4, 0))
        self.heard = ctk.CTkLabel(
            self, text="heard   " + (row["heard"] or "(nothing)"), anchor="w",
            justify="left", text_color=SUBTITLE, font=ctk.CTkFont(size=12),
            wraplength=820)
        self.heard.pack(fill="x", padx=10, pady=(1, 8))

        for widget in (self, head, self.expected, self.heard):
            widget.bind("<Button-1>", lambda _e: on_select(row["index"]))

    def set_selected(self, selected):
        self.configure(border_width=2 if selected else 1,
                       border_color=ACCENT if selected else BORDER,
                       fg_color=PASTEL_VIOLET if selected else CARD)

    def set_wrap(self, width):
        self.expected.configure(wraplength=width)
        self.heard.configure(wraplength=width)


class CandidateRow(ctk.CTkFrame):
    """One regenerated take of the selected chunk."""

    def __init__(self, parent, number, candidate, on_play, on_use,
                 queued=False):
        super().__init__(parent, fg_color=PASTEL_VIOLET if queued else CARD,
                         corner_radius=8, border_width=2 if queued else 1,
                         border_color=ACCENT if queued else BORDER)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=8)
        ctk.CTkButton(row, text="▶", width=30, height=30, corner_radius=15,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(size=12),
                      command=lambda: on_play(candidate)).pack(side="left")
        made = (f"{candidate['pieces']} pieces, {candidate['gap']}s gaps"
                if candidate.get("pieces", 1) > 1 else "one request")
        label = (f"  TAKE {number}  ·  {made}  ·  "
                 f"scale {candidate['scale']}  ·  {candidate['seed_label']}")
        ctk.CTkLabel(row, text=label, text_color=TITLE, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        delta = candidate["duration"] - candidate["old_duration"]
        ctk.CTkLabel(row, text=f"{candidate['duration']:.2f}s "
                               f"({delta:+.2f}s vs original)",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(12, 0))
        ctk.CTkButton(row, text="Queued" if queued else "Use this",
                      width=90, height=30, corner_radius=8,
                      fg_color=ACCENT if queued else CARD,
                      hover_color=ACCENT_HOVER if queued else BG,
                      text_color="white" if queued else ENTRY_TEXT,
                      border_width=0 if queued else 1,
                      border_color=NEUTRAL_BORDER,
                      font=ctk.CTkFont(size=12),
                      command=lambda: on_use(candidate)).pack(side="right")


class QueueRow(ctk.CTkFrame):
    """One chunk waiting in the batch that Apply will run."""

    def __init__(self, parent, edit, on_remove, on_show):
        super().__init__(parent, fg_color="#FCFCFD", corner_radius=8,
                         border_width=1, border_color=BORDER)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=7)
        ctk.CTkLabel(row, text=f"#{edit['index']}", width=52, anchor="w",
                     text_color=TITLE,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(row, text=clock(edit["start"]), width=64, anchor="w",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkLabel(row,
                     text=f"{edit['old_duration']}s → "
                          f"{edit['new_duration']}s  ({edit['delta']:+.2f}s)",
                     anchor="w", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkButton(row, text="Remove", width=80, height=28, corner_radius=8,
                      fg_color=CARD, hover_color=BG, text_color=FAIL,
                      border_width=1, border_color=NEUTRAL_BORDER,
                      font=ctk.CTkFont(size=11),
                      command=lambda: on_remove(edit["index"])).pack(side="right")
        ctk.CTkButton(row, text="Show", width=70, height=28, corner_radius=8,
                      fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
                      border_width=1, border_color=NEUTRAL_BORDER,
                      font=ctk.CTkFont(size=11),
                      command=lambda: on_show(edit["index"])).pack(
            side="right", padx=(0, 8))


class RepairApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Chapter Repair")
        self.geometry("1080x980")
        self.minsize(940, 680)
        self.configure(fg_color=BG)

        icon = os.path.join(SCRIPT_DIR, "repair_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.settings = repair.load_settings()
        self.chapter = None
        self.rows = []              # every scored chunk
        self.flagged = []           # the shortlist actually listed
        self.row_widgets = []
        self.selected_index = None
        # Takes are kept PER CHUNK. Browsing the shortlist used to throw
        # away everything generated for the chunk you were leaving, which
        # made comparing two suspect chunks cost that GPU time twice.
        self.takes = {}            # chunk index -> [take]
        self.queued = {}           # chunk index -> the take chosen for it
        self.candidate_widgets = []
        self.queue_widgets = []
        self.plan = None

        self._queue = queue.Queue()
        self._busy = False
        self._cancel = threading.Event()
        self._proc = None
        self._wrap = 0

        self.book_var = ctk.StringVar()
        self.chapter_var = ctk.StringVar()
        self.find_var = ctk.StringVar()
        self.threshold_var = ctk.StringVar(
            value=f"{float(self.settings['similarity_threshold']):.2f}")
        self.speaker_var = ctk.StringVar(value=self.settings["speaker_path"])
        self.scale_var = ctk.StringVar(
            value=f"{float(self.settings['duration_scale']):.1f}")
        self.seed_var = ctk.StringVar(value="")
        # Empty leaves the engine's 30 s ceiling alone, which is where it
        # should stay. Deliberately NOT pre-filled for a capped chunk:
        # raising it was tried on 2026-09-14 and produced 41 s of
        # differently-broken audio, because 30 s is the trained window and
        # not an arbitrary limit. It survives as an escape hatch only.
        self.max_seconds_var = ctk.StringVar(value="")
        self.split_var = ctk.IntVar(value=0)
        self.gap_var = ctk.StringVar(value="0.25")
        self.pieces = []
        self.trim_var = ctk.IntVar(value=1 if self.settings["no_trim_tail"] else 0)

        self._build()
        self.refresh_books()
        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain)

    # --------------------------------------------------------------- build
    def _card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _head(self, card, number, title, hint=""):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(13, 8))
        ctk.CTkLabel(row, text=str(number), width=26, height=26,
                     corner_radius=8, fg_color=PASTEL_GREY, text_color="#78767F",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(row, text="  " + title.upper(), text_color=TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        label = ctk.CTkLabel(row, text=hint, text_color=SUBTITLE,
                             font=ctk.CTkFont(size=11))
        label.pack(side="right")
        return label

    def _entry(self, parent, variable, width=None, **kwargs):
        entry = ctk.CTkEntry(parent, textvariable=variable, height=34,
                             corner_radius=8, border_width=1,
                             border_color=ENTRY_BORDER, fg_color=CARD,
                             text_color=ENTRY_TEXT, **kwargs)
        if width:
            entry.configure(width=width)
        return entry

    def _ghost(self, parent, text, command, width=110):
        return ctk.CTkButton(parent, text=text, width=width, height=34,
                             corner_radius=8, fg_color=CARD, hover_color=BG,
                             text_color=ENTRY_TEXT, border_width=1,
                             border_color=NEUTRAL_BORDER, command=command)

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        header.pack(fill="x")
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=13)
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Chapter Repair", text_color=TITLE,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        self.head_sub = ctk.CTkLabel(
            titles, text="Replace one bad chunk without re-rendering the chapter",
            text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.head_sub.pack(anchor="w")
        self.cancel_button = ctk.CTkButton(
            inner, text="Cancel", height=38, width=110, corner_radius=8,
            fg_color=CARD, hover_color=BG, text_color=FAIL, border_width=1,
            border_color=NEUTRAL_BORDER, state="disabled",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_cancel)
        self.cancel_button.pack(side="right")

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        self._build_chapter(self.body)
        self._build_inspect(self.body)
        self._build_chunk(self.body)
        self._build_regenerate(self.body)
        self._build_apply(self.body)
        self._build_log()

    def _build_chapter(self, parent):
        card = self._card(parent)
        self.chapter_hint = self._head(card, 1, "Chapter",
                                       self.settings["library_root"])
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 10))
        self.book_menu = ctk.CTkOptionMenu(
            row, values=["(none)"], variable=self.book_var, width=190, height=34,
            corner_radius=8, fg_color=CARD, button_color=CARD,
            button_hover_color=BG, text_color=ENTRY_TEXT, dropdown_fg_color=CARD,
            command=lambda _v: self.refresh_chapters())
        self.book_menu.pack(side="left", padx=(0, 8))
        self.chapter_menu = ctk.CTkOptionMenu(
            row, values=["(none)"], variable=self.chapter_var, width=190,
            height=34, corner_radius=8, fg_color=CARD, button_color=CARD,
            button_hover_color=BG, text_color=ENTRY_TEXT, dropdown_fg_color=CARD,
            command=lambda _v: self.load_chapter())
        self.chapter_menu.pack(side="left", padx=(0, 8))
        self._ghost(row, "↻", self.refresh_books, width=40).pack(side="left")

        self.chapter_facts = ctk.CTkLabel(
            card, text="", text_color=SUBTITLE, anchor="w", justify="left",
            font=ctk.CTkFont(size=12))
        self.chapter_facts.pack(fill="x", padx=16, pady=(0, 13))

    def _build_inspect(self, parent):
        card = self._card(parent)
        self.inspect_hint = self._head(
            card, 2, "Inspect", "Whisper reads it back and ranks the mismatches")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(row, text="flag below", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        self._entry(row, self.threshold_var, width=64).pack(side="left",
                                                            padx=(0, 8))
        self.scan_button = ctk.CTkButton(
            row, text="Scan chapter", width=140, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_scan)
        self.scan_button.pack(side="left")
        self._ghost(row, "Rescan", lambda: self.on_scan(force=True),
                    width=90).pack(side="left", padx=(8, 0))
        # Free, instant, and finds a whole class of defect the transcript
        # pass would spend minutes to reach - see repair.DURATION_CAP.
        self._ghost(row, "Capped only", self.on_find_capped,
                    width=110).pack(side="left", padx=(8, 0))
        self._ghost(row, "Survey library", self.on_survey,
                    width=120).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(row, text="jump to", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(18, 6))
        find = self._entry(row, self.find_var, width=150,
                           placeholder_text="14:32 or text")
        find.pack(side="left", padx=(0, 8))
        find.bind("<Return>", lambda _e: self.on_find())
        self._ghost(row, "Find", self.on_find, width=80).pack(side="left")

        self.inspect_summary = ctk.CTkLabel(
            card, text="Nothing scanned yet.", text_color=SUBTITLE, anchor="w",
            font=ctk.CTkFont(size=12))
        self.inspect_summary.pack(fill="x", padx=16, pady=(0, 6))

        # Capped: a chapter with thirty flagged chunks must not push the
        # rest of the window off the screen.
        self.rows_frame = ctk.CTkScrollableFrame(card, fg_color=BG, height=300,
                                                 corner_radius=8)
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_chunk(self, parent):
        card = self._card(parent)
        self.chunk_hint = self._head(card, 3, "Chunk", "")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self.play_original = ctk.CTkButton(
            row, text="▶  Play in context", width=160, height=34,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), state="disabled",
            command=self.on_play_original)
        self.play_original.pack(side="left")
        self._ghost(row, "Stop", stop_playback, width=80).pack(side="left",
                                                               padx=(8, 0))
        self.chunk_facts = ctk.CTkLabel(row, text="", text_color=SUBTITLE,
                                        font=ctk.CTkFont(size=11))
        self.chunk_facts.pack(side="left", padx=(14, 0))

        self.chunk_script = ctk.CTkLabel(
            card, text="", anchor="w", justify="left", text_color=ENTRY_TEXT,
            font=ctk.CTkFont(size=13), wraplength=900)
        self.chunk_script.pack(fill="x", padx=16, pady=(0, 4))
        self.chunk_heard = ctk.CTkLabel(
            card, text="", anchor="w", justify="left", text_color=SUBTITLE,
            font=ctk.CTkFont(size=12), wraplength=900)
        self.chunk_heard.pack(fill="x", padx=16, pady=(0, 13))

    def _build_regenerate(self, parent):
        card = self._card(parent)
        self._head(card, 4, "Regenerate", "same engine, new parameters")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self._entry(row, self.speaker_var).pack(side="left", fill="x",
                                                expand=True, padx=(0, 8))
        self._ghost(row, "Browse…", self.on_browse_speaker,
                    width=96).pack(side="left")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(row2, text="duration scale", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        self._entry(row2, self.scale_var, width=70).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(row2, text="seed", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        self._entry(row2, self.seed_var, width=110,
                    placeholder_text="random").pack(side="left", padx=(0, 14))
        ctk.CTkSwitch(row2, text="trim tail off", variable=self.trim_var,
                      onvalue=1, offvalue=0, switch_width=42, switch_height=22,
                      progress_color=TOGGLE_ON, text_color=SUBTITLE,
                      font=ctk.CTkFont(size=12)).pack(side="left")
        self.generate_button = ctk.CTkButton(
            row2, text="Generate a take", width=150, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, state="disabled",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_generate)
        self.generate_button.pack(side="right")

        # Splitting is the answer for a chunk that is too long for one
        # request. Irodori was trained at max_latent_steps 750, which IS
        # the 30 s its inference ceiling allows, so asking for 40 s in one
        # go is outside anything it has seen - raising the ceiling gives
        # different gibberish, not better audio. Splitting puts every
        # request back inside the window, exactly as the generator does
        # for every other chunk in the book.
        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=16, pady=(0, 10))
        self.split_switch = ctk.CTkSwitch(
            row3, text="split into pieces", variable=self.split_var,
            onvalue=1, offvalue=0, switch_width=42, switch_height=22,
            progress_color=TOGGLE_ON, text_color=ENTRY_TEXT,
            font=ctk.CTkFont(size=12), command=self._update_split_hint)
        self.split_switch.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(row3, text="gap", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        gap = self._entry(row3, self.gap_var, width=64)
        gap.pack(side="left", padx=(0, 14))
        self.gap_var.trace_add("write", lambda *_a: self._update_split_hint())
        ctk.CTkLabel(row3, text="max seconds", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        self._entry(row3, self.max_seconds_var, width=70,
                    placeholder_text="30").pack(side="left", padx=(0, 14))
        self.split_hint = ctk.CTkLabel(row3, text="", text_color=SUBTITLE,
                                       font=ctk.CTkFont(size=11))
        self.split_hint.pack(side="left")

        self.candidates_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.candidates_frame.pack(fill="x", padx=16, pady=(0, 13))

    def _build_apply(self, parent):
        card = self._card(parent)
        self._head(card, 5, "Apply",
                   "everything queued, in one pass - backs up first, always")
        self.queue_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.queue_frame.pack(fill="x", padx=16, pady=(0, 8))
        self.plan_label = ctk.CTkLabel(
            card, text="Nothing queued. Pick a take in step 4 to add a chunk.",
            text_color=SUBTITLE, anchor="w", justify="left",
            font=ctk.CTkFont(size=12))
        self.plan_label.pack(fill="x", padx=16, pady=(0, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 13))
        self.apply_button = ctk.CTkButton(
            row, text="Apply repair", width=150, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, state="disabled",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_apply)
        self.apply_button.pack(side="right")

    def _build_log(self):
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._ghost(bar, "Close", self.on_close).pack(side="right", padx=18,
                                                      pady=11)
        wrap = ctk.CTkFrame(self, fg_color=LOG_BG, corner_radius=0, height=150)
        wrap.pack(fill="x", side="bottom")
        wrap.pack_propagate(False)
        self.log_box = ctk.CTkTextbox(wrap, fg_color=LOG_BG, text_color=LOG_FG,
                                      font=ctk.CTkFont(family="Consolas", size=11),
                                      border_width=0, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------- library
    def refresh_books(self):
        books = repair.list_books(self.settings["library_root"])
        self.book_menu.configure(values=books or ["(none)"])
        if books and self.book_var.get() not in books:
            self.book_var.set(books[0])
        self.refresh_chapters()

    def refresh_chapters(self):
        book = self.book_var.get()
        chapters = repair.list_chapters(self.settings["library_root"], book)
        self.chapter_menu.configure(values=chapters or ["(none)"])
        if chapters and self.chapter_var.get() not in chapters:
            self.chapter_var.set(chapters[0])
        self.load_chapter()

    def load_chapter(self):
        book, base = self.book_var.get(), self.chapter_var.get()
        if not book or base in ("", "(none)"):
            return
        self.chapter = repair.Chapter(self.settings, book, base)
        self.rows = []
        self.flagged = []
        self.selected_index = None
        self.takes = {}
        self.queued = {}
        self.plan = None
        self._render_rows()
        self._render_candidates()
        self._render_chunk()
        self._render_queue()

        try:
            chunks = self.chapter.chunks()
        except OSError as e:
            self.chapter_facts.configure(text=f"cannot read sync.json: {e}")
            return
        cues = repair.read_srt(self.chapter.srt_path)
        master = ("FLAC master present" if self.chapter.has_master()
                  else "NO master - a repair would cost a second AAC generation")
        self.chapter_facts.configure(
            text=f"{len(chunks)} chunks · {clock(chunks[-1]['end'])} long "
                 f"· {len(cues)} srt cue(s) · {master}")

        cached = repair.load_qa(self.chapter)
        if cached:
            self.rows = cached["rows"]
            self.inspect_summary.configure(
                text=f"Scanned {cached['scanned']} · press Scan to use it, "
                     f"Rescan to transcribe again.")
        else:
            self.inspect_summary.configure(text="Nothing scanned yet.")
        self.log(f"loaded {self.chapter.name}")

    # ------------------------------------------------------------- inspect
    def on_scan(self, force=False):
        if self._busy or not self.chapter:
            return
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            messagebox.showerror("Threshold", "The flag threshold must be a "
                                              "number between 0 and 1.")
            return
        if self.rows and not force:
            self._finish_scan(self.rows, threshold, rescanned=False)
            return
        self._start(lambda: self._scan_worker(threshold, force))

    def _scan_worker(self, threshold, force):
        chapter = self.chapter
        if force:
            cache = repair.qa_cache_path(chapter)
            if os.path.isfile(cache):
                os.remove(cache)
            # Whisper reuses its own json too, so drop that as well.
            source = chapter.audio_source()
            produced = os.path.join(
                chapter.work_dir,
                os.path.splitext(os.path.basename(source))[0] + ".json")
            if os.path.isfile(produced):
                os.remove(produced)
        self.log(f"scanning {chapter.name}")
        segments = repair.transcribe_chapter(chapter, self.log,
                                             self._register_proc)
        if self._cancel.is_set():
            self.log("cancelled")
            return
        rows = repair.score_chapter(chapter, segments)
        repair.save_qa(chapter, rows)
        self.ui(lambda: self._finish_scan(rows, threshold, rescanned=True))

    def _finish_scan(self, rows, threshold, rescanned):
        self.rows = rows
        self.flagged = repair.suspicious(rows, threshold)
        worst = min((r["similarity"] for r in rows), default=1.0)
        self.inspect_summary.configure(
            text=f"{len(self.flagged)} of {len(rows)} chunk(s) worth a listen "
                 f"· worst match {worst:.0%} · "
                 f"{'transcribed just now' if rescanned else 'from the cached scan'}")
        self._render_rows()
        self.log(f"  {len(self.flagged)} flagged of {len(rows)}")

    def on_find_capped(self):
        """The 30-second ceiling pass. No Whisper, no GPU - sync.json says
        everything, so this is instant and costs nothing."""
        if self._busy or not self.chapter:
            return
        rows = repair.duration_rows(self.chapter)
        self.rows = rows
        self.flagged = repair.capped_rows(rows)
        if self.flagged:
            worst = self.flagged[0]
            rushed = sum(1 for row in self.flagged
                         if row.get("pace_ratio", 1.0) >= 1.15)
            self.inspect_summary.configure(
                text=f"{len(self.flagged)} of {len(rows)} chunk(s) hit the "
                     f"{repair.DURATION_CAP:.0f}s ceiling, {rushed} of them "
                     f"reading 15%+ above this chapter's "
                     f"{worst.get('pace')} ch/s · worst is #{worst['index']} "
                     f"at {worst.get('pace_ratio')}x, needing ~{worst['needs']}s")
        else:
            self.inspect_summary.configure(
                text=f"No chunk in this chapter hits the "
                     f"{repair.DURATION_CAP:.0f}s ceiling.")
        self._render_rows()
        self.log(f"capped pass: {len(self.flagged)} of {len(rows)} chunk(s)")

    def on_survey(self):
        """Every capped chunk in the library, from sync.json alone."""
        if self._busy:
            return
        self.log("surveying the library for capped chunks…")
        per_chapter, total, capped, rushed = repair.survey_library(
            self.settings, self.log)
        messagebox.showinfo(
            "Library survey",
            f"{capped} chunk(s) of {total} hit the "
            f"{repair.DURATION_CAP:.0f}s ceiling, across "
            f"{len(per_chapter)} chapter(s).\n\n"
            f"{rushed} of those read 15% or more above their own chapter's "
            f"pace - those are the ones worth hearing. The rest landed on "
            f"the ceiling because the book's duration scale asked for more "
            f"than 30s while still reading at a normal rate, and are "
            f"probably fine.\n\n"
            f"Full breakdown is in the log. These are crammed, not "
            f"truncated: the text is all there, spoken too fast.")

    def _render_rows(self):
        for widget in self.row_widgets:
            widget.destroy()
        self.row_widgets = []
        for row in self.flagged:
            widget = ChunkRow(self.rows_frame, row, self.on_select_chunk)
            widget.pack(fill="x", pady=(0, 6), padx=2)
            widget.set_selected(row["index"] == self.selected_index)
            self.row_widgets.append(widget)
        self._apply_wrap(force=True)

    def on_find(self):
        """A timestamp read off the player, or a scrap of the text."""
        if not self.chapter:
            return
        raw = self.find_var.get().strip()
        if not raw:
            return
        chunks = self.chapter.chunks()
        seconds = parse_clock(raw)
        found = None
        if seconds is not None:
            for chunk in chunks:
                if chunk["start"] <= seconds < chunk["end"]:
                    found = chunk["index"]
                    break
            if found is None and chunks:
                # Between two chunks - the silence belongs to the one after.
                later = [c for c in chunks if c["start"] >= seconds]
                found = (later[0] if later else chunks[-1])["index"]
        else:
            for chunk in chunks:
                if raw in chunk.get("text", ""):
                    found = chunk["index"]
                    break
        if found is None:
            messagebox.showinfo("Not found",
                                f"Nothing matches “{raw}” in this chapter.")
            return
        self.on_select_chunk(found)
        self.log(f"jumped to chunk {found}")

    def on_select_chunk(self, index):
        """Switching chunks keeps everything: this chunk's takes come back,
        and anything already queued stays queued."""
        self.selected_index = index
        for widget in self.row_widgets:
            widget.set_selected(widget.row["index"] == index)
        self._render_candidates()
        self._render_chunk()

    def _chunk_row(self):
        """The scored row for the selection, or a bare one built from
        sync.json when the chunk was found without a scan."""
        if self.selected_index is None:
            return None
        for row in self.rows:
            if row["index"] == self.selected_index:
                return row
        for chunk in self.chapter.chunks():
            if chunk["index"] == self.selected_index:
                return {"index": chunk["index"], "start": chunk["start"],
                        "end": chunk["end"], "text": chunk.get("text", ""),
                        "heard": "", "similarity": 1.0, "length_ratio": None,
                        "duration": round(chunk["end"] - chunk["start"], 3)}
        return None

    def _render_chunk(self):
        row = self._chunk_row()
        if not row:
            self.chunk_hint.configure(text="")
            self.chunk_facts.configure(text="")
            self.chunk_script.configure(text="")
            self.chunk_heard.configure(text="")
            self.play_original.configure(state="disabled")
            self.generate_button.configure(state="disabled")
            return
        self.chunk_hint.configure(text=f"#{row['index']}")
        duration = row["end"] - row["start"]
        facts = (f"{clock(row['start'])} → {clock(row['end'])} "
                 f"({duration:.2f}s) · {len(row['text'])} chars")
        # A chunk sitting exactly on the ceiling was crammed, not truncated,
        # and no amount of re-rolling the seed will fix that - it needs room.
        if duration >= repair.DURATION_CAP - repair.CAP_TOLERANCE:
            pace = row.get("pace") or repair.CHARS_PER_SECOND
            suggested = repair.suggested_max_seconds(row["text"], pace)
            facts += (f"  ·  HIT THE {repair.DURATION_CAP:.0f}s CEILING, "
                      f"needs ~{repair.needed_seconds(row['text'], pace):.0f}s "
                      f"at this chapter's pace — split it")
            self.chunk_facts.configure(text=facts, text_color=FAIL)
            # Splitting, not a raised ceiling: 30 s is the trained window.
            self.split_var.set(1)
        else:
            self.chunk_facts.configure(text=facts, text_color=SUBTITLE)
            self.split_var.set(0)
        self._update_split_hint()
        self.chunk_script.configure(text="script  " + row["text"])
        self.chunk_heard.configure(
            text="heard   " + (row["heard"] or "(not scanned)"))
        self.play_original.configure(state="normal")
        self.generate_button.configure(state="normal")
        self._apply_wrap(force=True)

    def _update_split_hint(self):
        """Shows what splitting would do before anything is generated -
        piece count, the longest piece, and what the chunk would become."""
        row = self._chunk_row()
        if not row or not self.split_var.get():
            self.pieces = []
            self.split_hint.configure(
                text="one request, as the chapter was rendered"
                if row else "", text_color=SUBTITLE)
            return
        limit = repair.piece_limit_for(self.chapter, self.rows or None)
        self.pieces = repair.split_for_repair(row["text"], limit)
        pace = row.get("pace") or repair.CHARS_PER_SECOND
        try:
            gap = max(0.0, float(self.gap_var.get()))
        except ValueError:
            gap = 0.0
        longest = max((len(p) for p in self.pieces), default=0) / pace
        spoken = len(row["text"]) / pace
        self.split_hint.configure(
            text=f"{len(self.pieces)} piece(s) at ≤{limit} chars · longest "
                 f"~{longest:.1f}s · chunk becomes ~"
                 f"{spoken + gap * max(0, len(self.pieces) - 1):.1f}s "
                 f"(now {row['end'] - row['start']:.1f}s)",
            text_color=DONE if longest < 20 else WARN)

    def on_play_original(self):
        row = self._chunk_row()
        if not row or self._busy:
            return

        def work():
            out = os.path.join(self.chapter.work_dir, "_listen",
                               f"chunk_{row['index']:04d}.wav")
            # A second either side: a hallucination is often only obvious
            # against the words it runs into.
            repair.extract_segment(self.chapter.audio_source(), row["start"],
                                   row["end"], out, pad=1.0)
            stop_playback()
            play_wav(out)
            self.log(f"playing chunk {row['index']} with 1s either side")

        self._start(work)

    # ---------------------------------------------------------- regenerate
    def on_browse_speaker(self):
        start = audition_list_dir(self.settings)
        chosen = filedialog.askopenfilename(
            title="Pick the speaker this chapter was rendered with",
            initialdir=start if os.path.isdir(start) else None,
            filetypes=[("Speaker", "*.safetensors"), ("All files", "*.*")])
        if chosen:
            self.speaker_var.set(os.path.normpath(chosen))

    def on_generate(self):
        row = self._chunk_row()
        if self._busy or not row:
            return
        speaker = self.speaker_var.get().strip()
        if not os.path.isfile(speaker):
            messagebox.showerror("Speaker", "Point at the .speaker.safetensors "
                                            "this chapter was rendered with.\n\n"
                                            "A different voice mid-chapter is "
                                            "worse than the hallucination.")
            return
        try:
            scale = float(self.scale_var.get())
        except ValueError:
            messagebox.showerror("Duration scale", "That is not a number.")
            return
        seed_raw = self.seed_var.get().strip()
        seed = None
        if seed_raw:
            try:
                seed = int(seed_raw)
            except ValueError:
                messagebox.showerror("Seed", "A seed must be a whole number, "
                                             "or empty for a random one.")
                return

        max_raw = self.max_seconds_var.get().strip()
        max_seconds = None
        if max_raw:
            try:
                max_seconds = float(max_raw)
            except ValueError:
                messagebox.showerror("Max seconds",
                                     "That is not a number. Leave it empty to "
                                     "use the engine default of "
                                     f"{repair.DURATION_CAP:.0f}s.")
                return
            if max_seconds < repair.DURATION_CAP:
                messagebox.showerror(
                    "Max seconds",
                    f"Lowering the ceiling below {repair.DURATION_CAP:.0f}s "
                    f"would crush the audio further, not help it.\n\n"
                    f"Leave it empty for the default, or raise it.")
                return

        gap = 0.0
        pieces = None
        if self.split_var.get():
            try:
                gap = max(0.0, float(self.gap_var.get()))
            except ValueError:
                messagebox.showerror("Gap", "The gap between pieces must be a "
                                            "number of seconds.")
                return
            limit = repair.piece_limit_for(self.chapter, self.rows or None)
            pieces = repair.split_for_repair(row["text"], limit)
            if len(pieces) < 2:
                messagebox.showinfo(
                    "Nothing to split",
                    "This chunk has no terminator to cut at, so it would go "
                    "to the engine whole anyway.\n\nIt will be generated as "
                    "one request.")
                pieces = None

        spec = {"speaker_path": speaker, "duration_scale": scale,
                "no_trim_tail": bool(self.trim_var.get()),
                "seed_enabled": seed is not None, "seed_value": seed or 0,
                "max_seconds": max_seconds,
                "pieces": pieces, "gap": gap}
        number = len(self.takes.get(row["index"], [])) + 1
        self._start(lambda: self._generate_worker(row, spec, number))

    def _generate_worker(self, row, spec, number):
        out = os.path.join(self.chapter.work_dir, "_takes",
                           f"chunk_{row['index']:04d}_take_{number:02d}.wav")
        pieces = spec.pop("pieces", None)
        gap = spec.pop("gap", 0.0)
        if pieces and len(pieces) > 1:
            self.log(f"generating take {number} for chunk {row['index']} as "
                     f"{len(pieces)} piece(s), scale {spec['duration_scale']}, "
                     f"{gap}s gaps")
            repair.generate_split_candidate(self.chapter, pieces, spec, out,
                                            gap, self.log, self._register_proc)
        else:
            self.log(f"generating take {number} for chunk {row['index']} "
                     f"(scale {spec['duration_scale']})")
            repair.generate_candidate(self.chapter, row["text"], spec, out,
                                      self.log, self._register_proc)
        candidate = {
            "path": out, "number": number, "scale": spec["duration_scale"],
            "seed_label": (f"seed {spec['seed_value']}" if spec["seed_enabled"]
                           else "random seed"),
            "duration": repair.audio_duration(out),
            "old_duration": row["end"] - row["start"],
            "index": row["index"],
            "pieces": len(pieces) if pieces else 1,
            "gap": gap,
        }
        self.takes.setdefault(row["index"], []).append(candidate)
        self.log(f"  take {number}: {candidate['duration']:.2f}s "
                 f"(original {candidate['old_duration']:.2f}s)")
        self.ui(self._render_candidates)

    def _render_candidates(self):
        for widget in self.candidate_widgets:
            widget.destroy()
        self.candidate_widgets = []
        chosen = self.queued.get(self.selected_index)
        for candidate in self.takes.get(self.selected_index, []):
            widget = CandidateRow(self.candidates_frame, candidate["number"],
                                  candidate, self.on_play_candidate,
                                  self.on_use_candidate,
                                  queued=(chosen is candidate))
            widget.pack(fill="x", pady=(0, 6))
            self.candidate_widgets.append(widget)

    def on_play_candidate(self, candidate):
        stop_playback()
        play_wav(candidate["path"])

    def on_use_candidate(self, candidate):
        """Queues this take for its chunk. One take per chunk - choosing
        another replaces it rather than stacking two repairs of the same
        audio, which the plan would refuse anyway."""
        if self._busy:
            return
        self.queued[candidate["index"]] = candidate
        self.log(f"queued take {candidate['number']} for chunk "
                 f"{candidate['index']} ({len(self.queued)} queued)")
        self._render_candidates()
        self._render_queue()

    def on_unqueue(self, index):
        if self._busy:
            return
        self.queued.pop(index, None)
        self.log(f"removed chunk {index} from the queue")
        self._render_candidates()
        self._render_queue()

    def _render_queue(self):
        """The pending batch, and what applying it would do. Re-planned
        from scratch on every change, so the numbers on screen are always
        the numbers Apply will use."""
        for widget in self.queue_widgets:
            widget.destroy()
        self.queue_widgets = []
        self.plan = None

        if not self.queued:
            self.plan_label.configure(
                text="Nothing queued. Pick a take in step 4 to add a chunk.")
            self.apply_button.configure(state="disabled")
            return

        selections = [{"index": index, "wav": take["path"]}
                      for index, take in self.queued.items()]
        try:
            plan = repair.plan_repairs(self.chapter, selections)
        except (ValueError, OSError) as e:
            self.plan_label.configure(text=f"cannot plan this batch: {e}")
            self.apply_button.configure(state="disabled")
            return
        self.plan = plan

        for edit in plan["edits"]:
            widget = QueueRow(self.queue_frame, edit, self.on_unqueue,
                              self.on_select_chunk)
            widget.pack(fill="x", pady=(0, 6))
            self.queue_widgets.append(widget)

        self.plan_label.configure(
            text=(f"{plan['count']} chunk(s) queued · "
                  f"{plan['delta']:+.3f}s overall\n"
                  f"{plan['chunks_after']} chunk(s) and "
                  f"{plan['cues_shifted']} of {plan['cues']} srt cue(s) move; "
                  f"chapter {plan['old_total']}s → {plan['new_total']}s\n"
                  f"one splice and one encode, cut from "
                  f"{'the FLAC master' if plan['from_master'] else 'the .m4a (a second AAC generation)'}"))
        self.apply_button.configure(state="normal")

    # --------------------------------------------------------------- apply
    def on_apply(self):
        if self._busy or not self.plan:
            return
        plan = self.plan
        listed = ", ".join("#" + str(i) for i in plan["indices"])
        if not messagebox.askyesno(
                "Apply repair",
                f"Replace {plan['count']} chunk(s) ({listed}) of "
                f"{self.chapter.name}?\n\n"
                f"The .m4a, sync.json"
                f"{', the .srt' if plan['cues'] else ''} and the master will "
                f"be rewritten in one pass. Everything is backed up first.\n\n"
                f"Afterwards the chapter still has to be re-uploaded to R2."):
            return
        self._start(self._apply_worker)

    def _apply_worker(self):
        plan = self.plan
        selections = [{"index": edit["index"], "wav": edit["wav"]}
                      for edit in plan["edits"]]
        self.log(f"repairing {self.chapter.name}: "
                 f"{', '.join('#' + str(i) for i in plan['indices'])}")
        result = repair.apply_repairs(self.chapter, selections, self.log, plan)
        self.log(f"done - chapter is now {result['new_total']}s")
        self.log("REMEMBER: re-upload this chapter's .m4a, .sync.json and "
                 ".srt to R2.")
        self.ui(self._after_apply)

    def _after_apply(self):
        messagebox.showinfo(
            "Repaired",
            "The chapter has been repaired and backed up.\n\n"
            "It still has to be re-uploaded to R2 before the site serves the "
            "fixed version.")
        self.takes = {}
        self.queued = {}
        self.plan = None
        self._render_candidates()
        self._render_queue()
        # The scan describes audio that has just changed.
        cache = repair.qa_cache_path(self.chapter)
        if os.path.isfile(cache):
            os.remove(cache)
        self.rows = []
        self.flagged = []
        self._render_rows()
        self.load_chapter()

    # ------------------------------------------------------------ plumbing
    def log(self, message):
        self._queue.put(("log", message))

    def ui(self, fn):
        self._queue.put(("call", fn))

    def _register_proc(self, proc):
        self._proc = proc

    def _start(self, target):
        if self._busy:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        self._busy = True
        self._cancel.clear()
        self.cancel_button.configure(state="normal")
        self.scan_button.configure(state="disabled")
        self.generate_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")

        def wrapper():
            try:
                target()
            except Exception as e:                     # never lose the reason
                self.log(f"! {type(e).__name__}: {e}")
                self.ui(lambda: messagebox.showerror("Failed", str(e)))
            finally:
                self._busy = False
                self._proc = None
                self.ui(self._finish)

        threading.Thread(target=wrapper, daemon=True).start()

    def _finish(self):
        self.cancel_button.configure(state="disabled")
        self.scan_button.configure(state="normal")
        if self.selected_index is not None:
            self.generate_button.configure(state="normal")
        if self.queued:
            self.apply_button.configure(state="normal")

    def on_cancel(self):
        if not self._busy:
            return
        self._cancel.set()
        self.log("cancelling…")
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.cancel_button.configure(state="disabled")

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

    def _on_configure(self, event):
        if event.widget is self:
            self._apply_wrap()

    def _apply_wrap(self, force=False):
        """Text columns are labels, so they size themselves vertically but
        have to be told how wide to run. Divided by the display scaling
        because CustomTkinter scales `wraplength` itself - passing raw
        pixels on a 150% display clips the text instead of wrapping it."""
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            scaling = 1.0
        width = max(320, int(self.winfo_width() / max(scaling, 0.1)) - 130)
        if not force and width == self._wrap:
            return
        self._wrap = width
        for widget in self.row_widgets:
            widget.set_wrap(width - 40)
        self.chunk_script.configure(wraplength=width)
        self.chunk_heard.configure(wraplength=width)

    def on_close(self):
        if self._busy and not messagebox.askyesno(
                "Still running", "Something is still running. Close anyway?"):
            return
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        stop_playback()
        data = dict(self.settings)
        data["speaker_path"] = self.speaker_var.get().strip()
        try:
            data["duration_scale"] = float(self.scale_var.get())
        except ValueError:
            pass
        try:
            data["similarity_threshold"] = float(self.threshold_var.get())
        except ValueError:
            pass
        data["no_trim_tail"] = bool(self.trim_var.get())
        try:
            repair.save_settings(data)
        except OSError:
            pass
        self.destroy()


def audition_list_dir(settings):
    return os.path.join(settings["irodori_root"], "seiyuu", "list")


if __name__ == "__main__":
    RepairApp().mainloop()
