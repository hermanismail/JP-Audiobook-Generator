"""
furigana_review.py
------------------
The window that decides what happens to the furigana in a book's chapters.

The rule that needs no decision (user, 2026-09-24): **a word carrying
furigana is always read as written**. What the person decides is what
happens to the SAME word where the author did not annotate it - which
matters because a reading is a plain replace and `下(もと)` is annotated
once in wall while a bare `下` appears 264 times.

So a pair with no bare occurrences is applied automatically and only
listed; a pair with bare occurrences gets a card:

    [x] Apply   ( ) only where it is written   ( ) everywhere in scope
        [ ] and in other books

Unticked means rejected: the parens are stripped and the seiyuu decides.
A word the book gives TWO readings (wall: 瞬 = またた / まばた) cannot be
a whole-book rule, so that choice is disabled for it.

## Scope (user decision, 2026-09-24)

The review covers **the chapters selected for the run**, not the book.
With "assign profile for all chapters" that is the whole book, exactly as
before. With Customize and two chapters ticked, only those two are
scanned: a pair that appears nowhere in them is never asked about, the
bare counts are broken down per chapter (`1st written in chapter_009 ·
without furigana ×2 in chapter_009, ×3 in chapter_010`), and the decision
is stored against those two chapters only. The same pair comes back for
review the next time a chapter it appears in is selected - reviewing 177
readings to render two chapters was the problem this solves.

## Layout

177 cards meant scrolling to find out what was left. Instead every pair
is a TAG at the top, colour-coded by what it will do, and clicking one
opens its card below. So the state of the whole review is one glance, and
only one card is ever on screen.
"""

import os

import customtkinter as ctk

import furigana
import suite_link
from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ENTRY_TEXT, COLOR_ENTRY_BORDER,
    mark_tag,
)

COLOR_OK = "#1E8B4E"
COLOR_WARN = "#B7791F"
COLOR_ERROR = "#C4453C"
COLOR_MARK = "#FFF3A3"          # the stretch being judged, and the open tag

# What a tag's fill says about the decision, at a glance.
TAG_TODO = "#EDEDF2"            # not decided yet
TAG_KEEP = "#D8F0E0"            # ticked - read as written
TAG_DROP = "#FBE0DD"            # unticked - parens stripped, seiyuu decides
TAG_OPEN = COLOR_MARK           # the card showing below
COLOR_ASIDE_BORDER = "#D98B36"  # 夢（のようなもの） - never a reading


def scan_folder(folder, chapters=None):
    """{(word, reading): stats} for the chapter files in `folder`.

    `chapters` narrows it to the chapters selected for the run (a list of
    bases, e.g. ["chapter_009"]); None means every chapter, as before.
    Counts come back per chapter, so the window can say where the bare
    occurrences are."""
    import glob
    texts = {}
    for path in sorted(glob.glob(os.path.join(folder or "", "chapter_*.txt"))):
        base = os.path.splitext(os.path.basename(path))[0]
        if chapters is not None and base not in set(chapters):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                texts[base] = f.read()
        except (OSError, UnicodeDecodeError):
            continue
    return furigana.statistics(texts)


def chapters_of(row):
    """Every chapter the pair touches - annotated or bare. A decision has
    to reach all of them before the pair counts as settled."""
    return set(row.get("with_by_chapter") or {}) | set(row.get("bare_by_chapter") or {})


def pending(stats, suite, book, chapters=None):
    """The pairs with no decision covering the chapters they appear in,
    and the ones that need no question (no bare occurrences) -
    (undecided, automatic)."""
    if suite is None or not suite_link.uses_new_pipeline(book):
        return [], []
    undecided, automatic = [], []
    for (word, reading), row in sorted(stats.items()):
        where = chapters_of(row) or {None}
        if all(suite.furigana_decision(book["id"], word, reading, chapter) is not None
               for chapter in where):
            continue
        # Only a real reading with nowhere else to apply can skip the
        # question; anything that looks like an aside is always asked.
        (automatic if row["bare"] == 0 and row.get("is_reading", True)
         else undecided).append(row)
    return undecided, automatic


def where_phrase(row, limit=4):
    """`1st written in chapter_009 · without furigana ×2 in chapter_009,
    ×3 in chapter_010` - the evidence the decision is made on."""
    parts = []
    if row.get("first_chapter"):
        parts.append(f"1st written in {row['first_chapter']}")
    bare = sorted((row.get("bare_by_chapter") or {}).items())
    if bare:
        listed = ", ".join(f"×{n} in {chapter}" for chapter, n in bare[:limit])
        if len(bare) > limit:
            listed += f", … {len(bare) - limit} more chapter(s)"
        parts.append("without furigana " + listed)
    else:
        parts.append("no bare occurrences in these chapters")
    return "  ·  ".join(parts)


class FuriganaReview(ctk.CTkToplevel):
    """A tag per reading that needs a decision; the open one's card sits
    below. `on_done(saved)` fires after Save or Close."""

    def __init__(self, parent, book, stats, undecided, automatic, settings, on_done=None,
                 chapters=None):
        super().__init__(parent)
        self.title("Furigana readings")
        self.geometry("1020x760")
        self.configure(fg_color=COLOR_BG)
        self.book = book
        self.settings = settings
        self.on_done = on_done
        self.chapters = list(chapters) if chapters else None
        self.rows = {}
        self.tags = {}
        self.automatic = automatic
        self.open_key = None

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
                  "seiyuu decides how to read it.")).pack(anchor="w", padx=20, pady=(0, 8))
        ctk.CTkLabel(
            head, anchor="w", justify="left", wraplength=960, text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=12, weight="bold"), text=self._scope_line()).pack(
            anchor="w", padx=20, pady=(0, 10))

        if automatic:
            note = ", ".join(f"{r['word']}({r['reading']}) ×{r['with_furigana']}"
                             for r in automatic[:6])
            ctk.CTkLabel(
                head, anchor="w", justify="left", wraplength=960, text_color=COLOR_OK,
                font=ctk.CTkFont(size=12),
                text=(f"{len(automatic)} reading(s) appear only with furigana here, so they "
                      f"are applied without a question: {note}"
                      + (" …" if len(automatic) > 6 else ""))).pack(
                anchor="w", padx=20, pady=(0, 12))

        # --- the tag strip
        strip = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=10, border_width=1,
                             border_color=COLOR_CARD_BORDER)
        strip.pack(fill="x", padx=16, pady=(12, 8))
        self.strip = ctk.CTkScrollableFrame(strip, fg_color="transparent", height=150)
        self.strip.pack(fill="both", expand=True, padx=8, pady=8)
        self.strip.bind("<Configure>", lambda _e: self._reflow())
        self.order = [(r["word"], r["reading"]) for r in undecided]
        self.stats = {(r["word"], r["reading"]): r for r in undecided}
        for row in undecided:
            self._state(row)            # its default answer, so the tag has a colour
            self._add_tag(row)
            self._paint((row["word"], row["reading"]))
        self._reflow()

        # --- the open card
        self.detail = ctk.CTkFrame(self, fg_color="transparent")
        self.detail.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.empty = ctk.CTkLabel(
            self.detail, text_color=COLOR_SUBTITLE, justify="left",
            text=("Nothing to decide - every reading in these chapters is already settled."
                  if not undecided else "Click a reading above to decide it."))
        self.empty.pack(anchor="w", pady=20)

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
        self.status = ctk.CTkLabel(bar, text="", text_color=COLOR_SUBTITLE,
                                   font=ctk.CTkFont(size=12))
        self.status.pack(side="left", padx=18)
        self._tally()
        if self.order:
            self.after(30, lambda: self.open(self.order[0]))
        self.after(60, lambda: (self.lift(), self.focus_force()))

    def _scope_line(self):
        if not self.chapters:
            return "Scope: every chapter in the input folder"
        listed = ", ".join(self.chapters[:4]) + (" …" if len(self.chapters) > 4 else "")
        return (f"Scope: {len(self.chapters)} chapter(s) selected for this run - {listed}. "
                f"Decisions apply to these chapters only.")

    # ------------------------------------------------------------- tags
    def _add_tag(self, row):
        key = (row["word"], row["reading"])
        many = len(row["readings_in_book"]) > 1
        aside = not row.get("is_reading", True)
        # Two readings of one word cannot be told apart by the kanji alone.
        label = f"{row['word']}\n{row['reading']}" if many else row["word"]
        tag = ctk.CTkButton(
            self.strip, text=label, width=58, height=44 if many else 38, corner_radius=10,
            fg_color=TAG_TODO, hover_color="#E2E1F4", text_color=COLOR_ENTRY_TEXT,
            border_width=2 if aside else 1,
            border_color=COLOR_ASIDE_BORDER if aside else COLOR_ENTRY_BORDER,
            font=ctk.CTkFont(size=12 if many else 15, weight="bold"),
            command=lambda k=key: self.open(k))
        self.tags[key] = tag

    def _reflow(self, _event=None):
        """Tags wrap to the width of the strip - a plain grid, recomputed
        when the window is resized."""
        width = max(self.strip.winfo_width(), 400)
        per_row = max(1, (width - 16) // 68)
        for number, key in enumerate(self.order):
            self.tags[key].grid(row=number // per_row, column=number % per_row,
                                padx=4, pady=4, sticky="w")

    def _paint(self, key):
        state = self.rows.get(key)
        if state is None:
            fill = TAG_TODO
        else:
            fill = TAG_KEEP if state["apply"].get() else TAG_DROP
        if key == self.open_key:
            fill = TAG_OPEN
        self.tags[key].configure(fg_color=fill)

    def _tally(self):
        keep = sum(1 for s in self.rows.values() if s["apply"].get())
        drop = len(self.rows) - keep
        looked = sum(1 for s in self.rows.values() if s["widgets"])
        self.status.configure(
            text=(f"{len(self.order)} reading(s)  ·  {keep} read as written  ·  "
                  f"{drop} left to the seiyuu  ·  {looked} opened"),
            text_color=COLOR_SUBTITLE)

    # ------------------------------------------------------------- card
    def open(self, key):
        if self.open_key == key:
            return
        previous, self.open_key = self.open_key, key
        if previous is not None:
            self._paint(previous)
        for widget in self.detail.winfo_children():
            widget.destroy()
        self._add_card(self.stats[key])
        self._paint(key)
        self._tally()

    def _state(self, row):
        """Every pair gets its default answer before anything is clicked -
        ticked, applied where it is written; an aside unticked. So a tag is
        green or red from the start and Save means "these answers", not
        "only the ones I opened"."""
        key = (row["word"], row["reading"])
        if key not in self.rows:
            aside = not row.get("is_reading", True)
            self.rows[key] = {
                "apply": ctk.IntVar(value=0 if aside else 1),
                "scope": ctk.StringVar(value="once"),
                "global": ctk.IntVar(value=0),
                "widgets": None,
                "many": len(row["readings_in_book"]) > 1,
                "row": row,
            }
        return self.rows[key]

    def _add_card(self, row):
        key = (row["word"], row["reading"])
        many = len(row["readings_in_book"]) > 1
        aside = not row.get("is_reading", True)
        remembered = self._state(row)
        card = ctk.CTkFrame(self.detail, fg_color=COLOR_CARD, corner_radius=10, border_width=1,
                            border_color=COLOR_CARD_BORDER)
        card.pack(fill="both", expand=True)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=10)

        # The vars live in self.rows, so reopening a tag shows the answer
        # it already has and Save sees it whether it was opened or not.
        apply_var = remembered["apply"]
        scope_var = remembered["scope"]
        global_var = remembered["global"]

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkCheckBox(top, text="", variable=apply_var, width=24, fg_color=COLOR_ACCENT,
                        hover_color=COLOR_ACCENT_HOVER,
                        command=lambda k=key: self._toggle(k)).pack(side="left")
        ctk.CTkLabel(top, text=f"{row['word']}（{row['reading']}）", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left", padx=(4, 14))
        ctk.CTkLabel(top, text=f"with furigana ×{row['with_furigana']}  ·  "
                               f"the same word without furigana ×{row['bare']} here",
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkLabel(inner, text=where_phrase(row), text_color=COLOR_SUBTITLE, anchor="w",
                     justify="left", wraplength=900,
                     font=ctk.CTkFont(size=12)).pack(fill="x", padx=(28, 0), pady=(4, 0))

        self._add_example(inner, row)

        choices = ctk.CTkFrame(inner, fg_color="transparent")
        choices.pack(fill="x", padx=(28, 0), pady=(8, 0))
        once = ctk.CTkRadioButton(choices, text="only where it is written", variable=scope_var,
                                  value="once", fg_color=COLOR_ACCENT,
                                  hover_color=COLOR_ACCENT_HOVER,
                                  command=lambda k=key: self._paint(k),
                                  font=ctk.CTkFont(size=12), text_color=COLOR_ENTRY_TEXT)
        once.pack(side="left", padx=(0, 16))
        every = ctk.CTkRadioButton(choices, text=self._everywhere_label(row),
                                   variable=scope_var, value="book", fg_color=COLOR_ACCENT,
                                   hover_color=COLOR_ACCENT_HOVER,
                                   command=lambda k=key: self._paint(k),
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

        remembered["widgets"] = (once, every, other)
        self._toggle(key)

    def _everywhere_label(self, row):
        """The claim has to match the evidence: with two chapters selected
        this is not "everywhere in this book"."""
        if not self.chapters:
            return f"everywhere in this book (all {row['bare']} of them)"
        return (f"everywhere in the {len(self.chapters)} chapter(s) selected "
                f"(all {row['bare']} of them)")

    def _add_example(self, parent, row):
        """The sentence where the reading is first written, with the
        annotation itself marked - a reading cannot be judged alone
        (user, 2026-09-24)."""
        sentence, start, end = row.get("example") or ("", 0, 0)
        if not sentence:
            return
        box = ctk.CTkTextbox(parent, height=62, corner_radius=8, border_width=1,
                             border_color=COLOR_CARD_BORDER, fg_color="white",
                             text_color=COLOR_ENTRY_TEXT, wrap="word",
                             font=ctk.CTkFont(size=13), activate_scrollbars=False)
        box.pack(fill="x", padx=(28, 0), pady=(8, 0))
        box.insert("1.0", sentence)
        mark_tag(box, "mark", COLOR_MARK)
        if 0 <= start < end <= len(sentence):
            box.tag_add("mark", f"1.{start}", f"1.{end}")
        box.configure(state="disabled")
        self.example_box = box

    def _toggle(self, key):
        state = self.rows[key]
        on = bool(state["apply"].get())
        if not state["widgets"]:            # its card has never been opened
            self._paint(key)
            self._tally()
            return
        once, every, other = state["widgets"]
        once.configure(state="normal" if on else "disabled")
        every.configure(state="normal" if on and not state["many"] else "disabled")
        other.configure(state="normal" if on and not state["many"] else "disabled")
        self._paint(key)
        self._tally()

    # ------------------------------------------------------------ saving
    def save(self):
        suite = suite_link.open_suite(self.settings)
        if suite is None:
            self.status.configure(text=f"Library not reachable ({suite_link.load_error()})",
                                  text_color=COLOR_ERROR)
            return
        scope_chapters = self.chapters          # None = the whole book
        applied = rejected = whole = 0
        try:
            # No question asked: these are only ever written with furigana.
            for row in self.automatic:
                suite.furigana_decide(self.book["id"], row["word"], row["reading"], "book",
                                      chapters=scope_chapters)
                suite.reading_upsert(self.book["id"], row["word"], row["reading"],
                                     scope="book", origin="furigana",
                                     occurrences=row["with_furigana"],
                                     bare_occurrences=row["bare"], chapters=scope_chapters,
                                     on_conflict="overwrite")
                applied += 1
            for (word, reading), state in self.rows.items():
                row = state["row"]
                if not state["apply"].get():
                    suite.furigana_decide(self.book["id"], word, reading, "rejected",
                                          chapters=scope_chapters)
                    rejected += 1
                    continue
                scope = "once" if state["many"] else state["scope"].get()
                is_global = bool(state["global"].get()) and scope == "book"
                suite.furigana_decide(self.book["id"], word, reading, scope,
                                      global_ok=is_global, chapters=scope_chapters)
                # Only a whole-book decision becomes a reading; a 'once' one
                # is applied where it is written and stays out of
                # readings.json, which cannot express it.
                if scope == "book":
                    suite.reading_upsert(self.book["id"], word, reading, scope="book",
                                         origin="furigana", global_ok=is_global,
                                         occurrences=row["with_furigana"],
                                         bare_occurrences=row["bare"],
                                         chapters=scope_chapters,
                                         on_conflict="overwrite")
                    whole += 1
                applied += 1
        finally:
            suite.close()
        self.status.configure(
            text=(f"Saved: {applied} applied ({whole} whole-book), {rejected} left to the "
                  f"seiyuu"
                  + (f" · for {len(scope_chapters)} chapter(s)" if scope_chapters else "")),
            text_color=COLOR_OK)
        if self.on_done:
            self.on_done(True)
        self.after(700, self.destroy)

    def _close(self):
        if self.on_done:
            self.on_done(False)
        self.destroy()
