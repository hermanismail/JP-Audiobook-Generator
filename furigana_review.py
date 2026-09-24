"""
furigana_review.py
------------------
The window that decides what happens to the furigana in a book's chapters.

The rule that needs no decision (user, 2026-09-24): **a word carrying
furigana is always read as written**. What the person decides is what
happens to the SAME word where the author did not annotate it - which
matters because a reading is a plain global replace and `下(もと)` is
annotated once in wall while a bare `下` appears 264 times.

So a pair with no bare occurrences is applied automatically and only
listed; a pair with bare occurrences gets a row:

    [x] Apply   ( ) only where it is written   ( ) everywhere in the book
        [ ] and in other books

Unticked means rejected: the parens are stripped and the seiyuu decides.
A word the book gives TWO readings (wall: 瞬 = またた / まばた) cannot be
a whole-book rule, so that choice is disabled for it.

Decisions are stored per book, so the window only ever shows pairs that
have not been decided yet.
"""

import os

import customtkinter as ctk

import furigana
import suite_link
from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ENTRY_TEXT,
)

COLOR_OK = "#1E8B4E"
COLOR_WARN = "#B7791F"
COLOR_ERROR = "#C4453C"


def scan_folder(folder):
    """{(word, reading): stats} for every chapter_*.txt in `folder`."""
    import glob
    texts = []
    for path in sorted(glob.glob(os.path.join(folder or "", "chapter_*.txt"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                texts.append(f.read())
        except (OSError, UnicodeDecodeError):
            continue
    return furigana.statistics(texts)


def pending(stats, suite, book):
    """The pairs with no decision yet, and the ones that need no question
    (no bare occurrences) - (undecided, automatic)."""
    if suite is None or not suite_link.uses_new_pipeline(book):
        return [], []
    undecided, automatic = [], []
    for (word, reading), row in sorted(stats.items()):
        if suite.furigana_decision(book["id"], word, reading) is not None:
            continue
        # Only a real reading with nowhere else to apply can skip the
        # question; anything that looks like an aside is always asked.
        (automatic if row["bare"] == 0 and row.get("is_reading", True)
         else undecided).append(row)
    return undecided, automatic


class FuriganaReview(ctk.CTkToplevel):
    """One row per reading that needs a decision. `on_done(saved)` fires
    after Save or Close."""

    def __init__(self, parent, book, stats, undecided, automatic, settings, on_done=None):
        super().__init__(parent)
        self.title("Furigana readings")
        self.geometry("1020x720")
        self.configure(fg_color=COLOR_BG)
        self.book = book
        self.settings = settings
        self.on_done = on_done
        self.rows = {}
        self.automatic = automatic

        head = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=0)
        head.pack(fill="x")
        ctk.CTkLabel(head, text="Furigana readings", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(
            head, anchor="w", justify="left", wraplength=960, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12),
            text=("A word written with furigana is always read that way - that needs no "
                  "decision. The question below is the same word where the author did NOT "
                  "annotate it: should it be read the same way there too?\n"
                  "Leave a reading unticked and its parens are simply removed - then the "
                  "seiyuu decides how to read it.")).pack(anchor="w", padx=20, pady=(0, 12))

        if automatic:
            note = ", ".join(f"{r['word']}({r['reading']}) ×{r['with_furigana']}"
                             for r in automatic[:6])
            ctk.CTkLabel(
                head, anchor="w", justify="left", wraplength=960, text_color=COLOR_OK,
                font=ctk.CTkFont(size=12),
                text=(f"{len(automatic)} reading(s) appear only with furigana, so they are "
                      f"applied without a question: {note}"
                      + (" …" if len(automatic) > 6 else ""))).pack(
                anchor="w", padx=20, pady=(0, 12))

        self.body = ctk.CTkScrollableFrame(self, fg_color=COLOR_BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=12)
        for row in undecided:
            self._add_row(row)
        if not undecided:
            ctk.CTkLabel(self.body, text="Nothing to decide - every reading in these chapters "
                                         "is already settled.",
                         text_color=COLOR_SUBTITLE).pack(anchor="w", pady=20)

        bar = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=0, height=64)
        bar.pack(fill="x")
        ctk.CTkButton(bar, text="Save decisions", width=150, height=36, corner_radius=8,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.save).pack(side="right", padx=(8, 18), pady=14)
        ctk.CTkButton(bar, text="Close", width=110, height=36, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=COLOR_ACCENT,
                      text_color=COLOR_ACCENT, hover_color="#F1F0FC",
                      command=self._close).pack(side="right", pady=14)
        self.status = ctk.CTkLabel(bar, text=f"{len(undecided)} reading(s) to decide",
                                   text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=12))
        self.status.pack(side="left", padx=18)
        self.after(60, lambda: (self.lift(), self.focus_force()))

    def _add_row(self, row):
        many = len(row["readings_in_book"]) > 1
        aside = not row.get("is_reading", True)
        card = ctk.CTkFrame(self.body, fg_color=COLOR_CARD, corner_radius=10, border_width=1,
                            border_color=COLOR_CARD_BORDER)
        card.pack(fill="x", pady=5)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        apply_var = ctk.IntVar(value=0 if aside else 1)
        scope_var = ctk.StringVar(value="once")
        global_var = ctk.IntVar(value=0)

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkCheckBox(top, text="", variable=apply_var, width=24, fg_color=COLOR_ACCENT,
                        hover_color=COLOR_ACCENT_HOVER,
                        command=lambda w=row["word"], r=row["reading"]: self._toggle(w, r)).pack(
            side="left")
        ctk.CTkLabel(top, text=f"{row['word']}（{row['reading']}）", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=(4, 14))
        ctk.CTkLabel(top, text=f"with furigana ×{row['with_furigana']}  ·  "
                               f"the same word without furigana ×{row['bare']}",
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=12)).pack(side="left")

        choices = ctk.CTkFrame(inner, fg_color="transparent")
        choices.pack(fill="x", padx=(28, 0), pady=(6, 0))
        once = ctk.CTkRadioButton(choices, text="only where it is written", variable=scope_var,
                                  value="once", fg_color=COLOR_ACCENT,
                                  hover_color=COLOR_ACCENT_HOVER,
                                  font=ctk.CTkFont(size=12), text_color=COLOR_ENTRY_TEXT)
        once.pack(side="left", padx=(0, 16))
        every = ctk.CTkRadioButton(choices,
                                   text=f"everywhere in this book (all {row['bare']} of them)",
                                   variable=scope_var, value="book", fg_color=COLOR_ACCENT,
                                   hover_color=COLOR_ACCENT_HOVER,
                                   font=ctk.CTkFont(size=12), text_color=COLOR_ENTRY_TEXT)
        every.pack(side="left", padx=(0, 16))
        other = ctk.CTkCheckBox(choices, text="and in other books", variable=global_var,
                                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                                font=ctk.CTkFont(size=12), text_color=COLOR_ENTRY_TEXT)
        other.pack(side="left")
        if many:
            every.configure(state="disabled")
            other.configure(state="disabled")
            ctk.CTkLabel(inner, text=f"This book reads {row['word']} two ways "
                                     f"({' / '.join(row['readings_in_book'])}), so it can only "
                                     f"be applied where it is written.",
                         text_color=COLOR_WARN, font=ctk.CTkFont(size=11),
                         anchor="w").pack(fill="x", padx=(28, 0), pady=(4, 0))
        if aside:
            ctk.CTkLabel(inner, text=f"This looks like an aside rather than a reading "
                                     f"(“{row['word']}（{row['reading']}）”). Applying it would "
                                     f"replace the word itself, so it starts unticked - the "
                                     f"parentheses are removed and the seiyuu reads "
                                     f"{row['word']}.",
                         text_color=COLOR_ERROR, font=ctk.CTkFont(size=11), justify="left",
                         wraplength=900, anchor="w").pack(fill="x", padx=(28, 0), pady=(4, 0))
        if aside or not apply_var.get():
            self.after(10, lambda w=row["word"], r=row["reading"]: self._toggle(w, r))

        self.rows[(row["word"], row["reading"])] = {
            "apply": apply_var, "scope": scope_var, "global": global_var,
            "widgets": (once, every, other), "many": many, "row": row}

    def _toggle(self, word, reading):
        state = self.rows[(word, reading)]
        on = bool(state["apply"].get())
        once, every, other = state["widgets"]
        once.configure(state="normal" if on else "disabled")
        every.configure(state="normal" if on and not state["many"] else "disabled")
        other.configure(state="normal" if on and not state["many"] else "disabled")

    # --- saving
    def save(self):
        suite = suite_link.open_suite(self.settings)
        if suite is None:
            self.status.configure(text=f"Library not reachable ({suite_link.load_error()})",
                                  text_color=COLOR_ERROR)
            return
        applied = rejected = whole = 0
        try:
            # No question asked: these are only ever written with furigana.
            for row in self.automatic:
                suite.furigana_decide(self.book["id"], row["word"], row["reading"], "book")
                suite.reading_upsert(self.book["id"], row["word"], row["reading"],
                                     scope="book", origin="furigana",
                                     occurrences=row["with_furigana"],
                                     bare_occurrences=row["bare"], on_conflict="overwrite")
                applied += 1
            for (word, reading), state in self.rows.items():
                row = state["row"]
                if not state["apply"].get():
                    suite.furigana_decide(self.book["id"], word, reading, "rejected")
                    rejected += 1
                    continue
                scope = "once" if state["many"] else state["scope"].get()
                is_global = bool(state["global"].get()) and scope == "book"
                suite.furigana_decide(self.book["id"], word, reading, scope,
                                      global_ok=is_global)
                # Only a whole-book decision becomes a reading; a 'once' one
                # is applied where it is written and stays out of
                # readings.json, which cannot express it.
                if scope == "book":
                    suite.reading_upsert(self.book["id"], word, reading, scope="book",
                                         origin="furigana", global_ok=is_global,
                                         occurrences=row["with_furigana"],
                                         bare_occurrences=row["bare"],
                                         on_conflict="overwrite")
                    whole += 1
                applied += 1
        finally:
            suite.close()
        self.status.configure(
            text=f"Saved: {applied} applied ({whole} whole-book), {rejected} left to the seiyuu",
            text_color=COLOR_OK)
        if self.on_done:
            self.on_done(True)
        self.after(700, self.destroy)

    def _close(self):
        if self.on_done:
            self.on_done(False)
        self.destroy()
