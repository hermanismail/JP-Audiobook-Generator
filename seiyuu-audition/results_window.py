"""
results_window.py
-----------------
Where an audition is actually judged: the text on the left of your eyes,
the samples under your finger, and nothing else.

It is a separate window rather than another section of the main one on
purpose - the main window is where you SET UP an audition, and stacking
results underneath it would push the controls off-screen exactly when you
want to tweak them and go again.

What it shows, per sample: the parameters it was generated with, a play
button, and - in e2e mode - the chunk breakdown as a table of number,
length and text. That breakdown is half the point of the mode: a chunk
length is judged by where it puts the breaks, and by how evenly the
lengths come out, as much as by how the result sounds.

Nothing in here scrolls on its own. Every block of text is a label that
grows to fit, so the only scrollbar is the window's own - scrolling the
samples must never mean scrolling inside one first.
"""

import os
import subprocess

import customtkinter as ctk

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
FAIL = "#C4453C"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"


def play_wav(path):
    """winsound, because it is stdlib, plays asynchronously, and needs no
    player window. It plays WAV and only WAV - which is the reason the
    engine stops before the AAC encode (see audition.py)."""
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


# How much narrower the table's text column is than a plain block: the
# two fixed columns and the padding either side of them.
TABLE_INSET = 120


def text_block(parent, text, register, size=13):
    """A read-only block of text that is as tall as its content.

    A CTkLabel rather than a CTkTextbox on purpose: a textbox has to be
    given a height, and any height that is not exactly right either clips
    the text behind a scrollbar or leaves dead space. Nesting a scroll
    region inside the window's own scroll region is the specific friction
    this avoids - you should scroll the samples, never the text inside one.

    A label has no width of its own either, so `wraplength` is handed to
    `register` and the window re-sets it whenever it is resized."""
    frame = ctk.CTkFrame(parent, fg_color="#FCFCFD", border_width=1,
                         border_color=ENTRY_BORDER, corner_radius=8)
    label = ctk.CTkLabel(frame, text=text, anchor="w", justify="left",
                         text_color=ENTRY_TEXT, font=ctk.CTkFont(size=size),
                         wraplength=760)
    label.pack(fill="x", padx=12, pady=9)
    register(label, 0)
    return frame


def chunk_table(parent, chunks, register):
    """The chunk breakdown as a table: number, length, text.

    The length column is the tally you used to do by hand before writing
    an infer.py command line, so it belongs next to the text it measures -
    a chunk length is judged by where the breaks fall AND by how evenly
    the chunks come out."""
    table = ctk.CTkFrame(parent, fg_color="#FCFCFD", border_width=1,
                         border_color=ENTRY_BORDER, corner_radius=8)

    head = ctk.CTkFrame(table, fg_color="transparent")
    head.pack(fill="x", padx=12, pady=(9, 4))
    for text, width, anchor in (("#", 26, "e"), ("CHARS", 46, "e"),
                                ("TEXT", 0, "w")):
        label = ctk.CTkLabel(head, text=text, text_color=SUBTITLE, anchor=anchor,
                             font=ctk.CTkFont(size=10, weight="bold"))
        if width:
            label.configure(width=width)
            label.pack(side="left", padx=(0, 10))
        else:
            label.pack(side="left", fill="x", expand=True, padx=(2, 0))

    # Rows are separated by space, not by rules. A 1px CTkFrame renders as
    # nothing anyway, and with chunks that wrap to two or three lines the
    # gap reads more clearly than a grid would.
    for chunk in chunks:
        row = ctk.CTkFrame(table, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=5)
        # anchor="n" on the fixed columns, or a chunk that wraps to three
        # lines centres its number against the middle of them.
        ctk.CTkLabel(row, text=str(chunk["index"]), width=26, anchor="ne",
                     text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12)).pack(side="left", anchor="n",
                                                     padx=(0, 10))
        ctk.CTkLabel(row, text=str(len(chunk["display_text"])), width=46,
                     anchor="ne", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12)).pack(side="left", anchor="n",
                                                     padx=(0, 10))
        body = ctk.CTkLabel(row, text=chunk["display_text"], anchor="w",
                            justify="left", text_color=ENTRY_TEXT,
                            font=ctk.CTkFont(size=13), wraplength=640)
        body.pack(side="left", fill="x", expand=True, padx=(2, 0))
        register(body, TABLE_INSET)

    ctk.CTkFrame(table, fg_color="transparent", height=4).pack()
    return table


class SampleResult(ctk.CTkFrame):
    """One generated sample: what it was, how to hear it, what it read."""

    def __init__(self, parent, number, result, spec, mode, register):
        super().__init__(parent, fg_color=CARD, corner_radius=11,
                         border_width=1, border_color=BORDER)
        self.result = result

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 6))

        ok = result.get("ok")
        self.play_button = ctk.CTkButton(
            head, text="▶", width=34, height=34, corner_radius=17,
            fg_color=ACCENT if ok else PASTEL_GREY,
            hover_color=ACCENT_HOVER if ok else PASTEL_GREY,
            text_color="white" if ok else SUBTITLE,
            font=ctk.CTkFont(size=13),
            state="normal" if ok else "disabled",
            command=self.on_play)
        self.play_button.pack(side="left")

        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(titles, text=f"SAMPLE {number}  ·  {spec['nickname']}",
                     text_color=TITLE,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(titles, text=summarise(spec, mode), text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11)).pack(anchor="w")

        if ok:
            ctk.CTkButton(head, text="Folder", width=72, height=28,
                          corner_radius=8, fg_color=CARD, hover_color=BG,
                          text_color=ENTRY_TEXT, border_width=1,
                          border_color="#D8D8DE",
                          font=ctk.CTkFont(size=11),
                          command=self.on_folder).pack(side="right")
            # A reused sample says so rather than reporting a generation
            # time it did not spend. On a random seed it is also the same
            # take you listened to last round, which is the point of reuse.
            took = ("reused" if result.get("reused")
                    else f"{result.get('seconds', 0.0):.0f}s")
            ctk.CTkLabel(head, text=f"{len(result.get('chunks', []))} chunk(s) "
                                    f"· {took}",
                         text_color=DONE if result.get("reused") else SUBTITLE,
                         font=ctk.CTkFont(size=11)).pack(side="right", padx=(0, 10))
        else:
            ctk.CTkLabel(head, text=result.get("error") or "failed",
                         text_color=FAIL, font=ctk.CTkFont(size=11),
                         wraplength=320, justify="right").pack(side="right")

        # The chunking is only meaningful in e2e mode - in simple mode the
        # "chunk" is the line you typed, already shown at the top.
        if ok and mode == "e2e":
            chunk_table(self, result.get("chunks", []), register).pack(
                fill="x", padx=14, pady=(0, 12))
        else:
            ctk.CTkFrame(self, fg_color="transparent", height=4).pack()

    def on_play(self):
        stop_playback()
        play_wav(self.result["wav"])

    def on_folder(self):
        folder = self.result.get("dir")
        if folder and os.path.isdir(folder):
            subprocess.Popen(["explorer", os.path.normpath(folder)])


def summarise(spec, mode):
    """The parameter line under a sample's name. Only the parameters that
    did something in this mode - listing a silence duration under a simple
    sample would be a lie about what was generated."""
    parts = [f"scale {spec['duration_scale']}"]
    if mode == "e2e":
        parts.append(f"chunk {spec['max_chunk_length']}")
        parts.append("silence {}/{}/{}".format(
            spec["silence_duration_sentence"],
            spec["silence_duration_paragraph"],
            spec["silence_duration_section"]))
    parts.append("trim tail off" if spec["no_trim_tail"] else "trim tail on")
    parts.append(f"seed {spec['seed_value']}" if spec.get("seed_enabled")
                 else "random seed")
    return "  ·  ".join(parts)


class ResultsWindow(ctk.CTkToplevel):
    """Reused, not re-opened: generating again refills this window, so it
    keeps its position on screen instead of reappearing somewhere else
    every round."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Audition Results")
        self.geometry("880x760")
        self.minsize(640, 460)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Labels size themselves vertically but need to be told how wide
        # they may run. Re-set on every resize, so widening the window
        # reflows the text instead of leaving it at its original column.
        self._wrap_labels = []
        self._wrap_width = 0
        self.bind("<Configure>", self._on_configure)

        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "audition_icon.ico")
        if os.path.isfile(icon):
            # Tk applies a Toplevel icon only once the window exists, and a
            # bad icon must never take the window with it.
            try:
                self.after(220, lambda: self.iconbitmap(icon))
            except Exception:
                pass

        head = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=58)
        head.pack(fill="x")
        inner = ctk.CTkFrame(head, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=11)
        self.head_label = ctk.CTkLabel(inner, text="Results", text_color=TITLE,
                                       font=ctk.CTkFont(size=15, weight="bold"))
        self.head_label.pack(side="left")
        ctk.CTkButton(inner, text="Stop", width=70, height=32, corner_radius=8,
                      fg_color=CARD, hover_color=BG, text_color=ENTRY_TEXT,
                      border_width=1, border_color="#D8D8DE",
                      command=stop_playback).pack(side="right", padx=(8, 0))

        self.body = ctk.CTkScrollableFrame(self, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkButton(bar, text="Close", width=110, height=34, corner_radius=8,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.on_close).pack(side="right", padx=18, pady=11)

    def _register_wrap(self, label, inset=0):
        """`inset` is how much narrower this label is than the full text
        column - a table's text sits to the right of two fixed columns."""
        self._wrap_labels.append((label, inset))

    def _on_configure(self, event):
        # Fires for every descendant as well; only the window's own size
        # decides the text column.
        if event.widget is self:
            self._apply_wraplength()

    def _apply_wraplength(self, force=False):
        """Window width minus everything between it and the text: the
        body's padding, the scrollbar, the card's padding and the block's
        own. Guarded on the last value because <Configure> fires
        continuously while a window is being dragged.

        The division is not optional. `winfo_width()` is real screen
        pixels, but CustomTkinter multiplies a widget's `wraplength` by the
        display scaling itself - so passing raw pixels on a 150% display
        asks for a column half again wider than the window, and the text
        is CLIPPED at the edge instead of wrapping."""
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            scaling = 1.0
        width = max(320, int(self.winfo_width() / max(scaling, 0.1)) - 110)
        if not force and width == self._wrap_width:
            return
        self._wrap_width = width
        for label, inset in self._wrap_labels:
            label.configure(wraplength=max(200, width - inset))

    def show(self, text, mode, specs, results):
        for child in self.body.winfo_children():
            child.destroy()
        # The labels went with their parents.
        self._wrap_labels = []

        ok = sum(1 for r in results if r.get("ok"))
        self.head_label.configure(
            text=f"{ok} of {len(results)} sample(s) · "
                 f"{'E2E simulation' if mode == 'e2e' else 'Simple'}")

        card = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=11,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(card, text="INPUT TEXT", text_color=SUBTITLE,
                     font=ctk.CTkFont(size=11, weight="bold"),
                     anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        text_block(card, text, self._register_wrap).pack(
            fill="x", padx=14, pady=(0, 12))

        for i, (spec, result) in enumerate(zip(specs, results), start=1):
            SampleResult(self.body, i, result, spec, mode,
                         self._register_wrap).pack(fill="x", pady=(0, 12))

        self.deiconify()
        self.lift()
        self.focus_force()
        # After deiconify, not before: an unmapped window reports width 1,
        # and every block would lay out at the minimum column.
        self.update_idletasks()
        self._apply_wraplength(force=True)

    def on_close(self):
        stop_playback()
        self.withdraw()
