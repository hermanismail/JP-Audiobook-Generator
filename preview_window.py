"""
preview_window.py
-----------------
Preview Chapters: what every chapter will send to the TTS and show in the
reader, before a single second of GPU time - and the place to correct
either one.

Laid out like the generator's own settings window: chapters down the left,
the chosen chapter on the right. Its summary sits on top (profile, seiyuu,
style, counts) and is NOT editable; below it one box per SOURCE SECTION,
split into two columns, one line per TTS request.

Both columns are editable. Save writes a PLAN into the generator's temp
folder (never the chapter's .txt) and records every changed box in the
suite library; the next render of that chapter uses the plan and then
deletes it. See preview.py.
"""

import os

import customtkinter as ctk
from tkinter import messagebox

import dynamic_profile
import preview
import suite_link
from ui_common import (
    COLOR_BG, COLOR_CARD, COLOR_CARD_BORDER, COLOR_TITLE, COLOR_SUBTITLE,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ENTRY_BORDER, COLOR_ENTRY_TEXT,
    mark_tag,
)

COLOR_OK = "#1E8B4E"
COLOR_WARN = "#B7791F"
COLOR_ERROR = "#C4453C"
COLOR_EDITED = "#6C5DD3"
COLOR_MARK = "#FFF3A3"          # where a reading or furigana changed the text

# Section 1 always shows (it holds the chapter header and the seiyuu
# credit, which is what a run is double-checked on); every later section is
# a tag, and only the one clicked is open. 34 sections of two text boxes
# meant scrolling to find anything (user, 2026-09-24).
TAG_IDLE = "#EDEDF2"
TAG_OPEN = COLOR_MARK
TAG_DIRTY = "#E3DEFA"           # edited, not saved yet
TAG_SAVED = "#D8F0E0"           # in the plan on disk


class PreviewWindow(ctk.CTkToplevel):
    """`chapters` is [(base, path, assignment)] - assignment as
    resolve_dynamic_plan() builds it: {"profile": profile, "style": key}."""

    def __init__(self, parent, chapters, settings, book, engine, readings=None,
                 furigana_applied=None, temp_dir=None, on_close=None, per_chapter=None):
        super().__init__(parent)
        self.title("Preview Chapters")
        self.geometry("1440x900")
        self.configure(fg_color=COLOR_BG)
        self.chapters = chapters
        self.settings = settings
        self.book = book
        self.engine = engine
        self.readings = readings or []
        self.furigana_applied = furigana_applied or set()
        # `per_chapter(base)` -> (readings, furigana pairs) for chapters
        # whose decisions were scoped (schema v2). Without it the same two
        # apply everywhere, as before.
        self.per_chapter = per_chapter
        self.temp_dir = temp_dir or settings.get("temp_dir")
        self.on_close_cb = on_close
        self.current = None
        self.boxes = {}                 # (section index, side) -> textbox
        self.built = {}                 # chapter -> sections
        self.held = {}                  # (section, side) -> (typed, built) when closed
        self.held_by_chapter = {}       # the same, per chapter left behind
        self.section_tags = {}
        self.open_section = None
        self.rest = []
        self.dirty = set()              # chapters with unsaved edits
        self.saved = set()              # chapters whose plan is on disk

        head = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=0)
        head.pack(fill="x")
        row = ctk.CTkFrame(head, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(14, 10))
        ctk.CTkLabel(row, text="Preview Chapters", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        ctk.CTkButton(row, text="Close", width=110, height=34, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=COLOR_ACCENT,
                      text_color=COLOR_ACCENT, hover_color="#F1F0FC",
                      command=self._close).pack(side="right")
        self.save_button = ctk.CTkButton(
            row, text="Save update", width=140, height=34, corner_radius=8,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.save)
        self.save_button.pack(side="right", padx=(8, 10))
        self.status = ctk.CTkLabel(row, text="", text_color=COLOR_SUBTITLE,
                                   font=ctk.CTkFont(size=12))
        self.status.pack(side="right", padx=(0, 14))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        side = ctk.CTkFrame(body, fg_color=COLOR_CARD, corner_radius=0, width=230)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ctk.CTkLabel(side, text="CHAPTERS", text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=18,
                                                                    pady=(16, 6))
        self.tabs = ctk.CTkScrollableFrame(side, fg_color="transparent")
        self.tabs.pack(fill="both", expand=True, padx=6, pady=(0, 10))
        self.tab_buttons = {}
        for base, _path, _assignment in chapters:
            button = ctk.CTkButton(
                self.tabs, text=base.replace("chapter_", "Chapter "), height=34,
                corner_radius=8, anchor="w", fg_color="transparent",
                hover_color="#F1F0FC", text_color=COLOR_ENTRY_TEXT,
                font=ctk.CTkFont(size=13), command=lambda b=base: self.show(b))
            button.pack(fill="x", padx=8, pady=2)
            self.tab_buttons[base] = button

        self.pane = ctk.CTkFrame(body, fg_color="transparent")
        self.pane.pack(side="left", fill="both", expand=True, padx=(12, 12), pady=12)
        self.summary = ctk.CTkLabel(self.pane, text="", justify="left", anchor="w",
                                    text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=12),
                                    wraplength=1100)
        self.summary.pack(fill="x", pady=(0, 8))
        self.sections_frame = ctk.CTkScrollableFrame(self.pane, fg_color=COLOR_BG)
        self.sections_frame.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._close)
        if chapters:
            self.after(50, lambda: self.show(chapters[0][0]))
        self.after(80, lambda: (self.lift(), self.focus_force()))

    # ------------------------------------------------------------ building
    def _assignment(self, base):
        return next(a for b, _p, a in self.chapters if b == base)

    def _path(self, base):
        return next(p for b, p, _a in self.chapters if b == base)

    def _raw(self, base):
        with open(self._path(base), "r", encoding="utf-8") as f:
            return f.read()

    def _intro(self, assignment):
        suite = suite_link.open_suite(self.settings)
        if suite is None:
            return (None, None)
        try:
            if not suite_link.uses_new_pipeline(self.book):
                return (None, None)
            return suite_link.intro_line(
                suite_link.seiyuu_for_profile(suite, assignment["profile"]))
        finally:
            suite.close()

    def build(self, base):
        if base in self.built:
            return self.built[base]
        assignment = self._assignment(base)
        readings, furigana_ok = self.readings, self.furigana_applied
        if self.per_chapter:
            readings, furigana_ok = self.per_chapter(base)
        sections, skipped, _pieces = preview.build(
            self._raw(base), assignment["profile"], assignment["style"], self.engine,
            readings, furigana_ok, self._intro(assignment))
        self.built[base] = sections
        self.skipped = skipped
        return sections

    # ------------------------------------------------------------ display
    def show(self, base):
        if self.current == base:
            return
        if self.current is not None:
            # Every box of the chapter being left, open or closed, so its
            # edits are still there when it is come back to.
            for index in list(self.section_tags) + [s["index"] for s in self.built
                                                    .get(self.current, [])[:1]]:
                self._remember(index)
            self.held_by_chapter[self.current] = dict(self.held)
        self.current = base
        self.held = dict(self.held_by_chapter.get(base, {}))
        for name, button in self.tab_buttons.items():
            edited = " ●" if name in self.dirty else (" ✔" if name in self.saved else "")
            button.configure(
                text=name.replace("chapter_", "Chapter ") + edited,
                fg_color=COLOR_ACCENT if name == base else "transparent",
                text_color="white" if name == base else COLOR_ENTRY_TEXT)
        sections = self.build(base)
        assignment = self._assignment(base)
        profile = assignment["profile"]
        lines = sum(len(s["lines"]) for s in sections)
        seconds = sum(1 for s in sections for line in s["lines"] if line["request"])
        intro = self._intro(assignment)[0]
        self.summary.configure(
            text=(f"{base}  ·  {len(sections)} section(s), {lines} TTS request(s)"
                  f"  ·  profile {os.path.basename(profile['_path'])} "
                  f"({profile.get('book')})  ·  seiyuu {dynamic_profile.nickname(profile)}"
                  f"  ·  {dynamic_profile.STYLE_LABELS[assignment['style']]}"
                  f"  ·  comfortable length {profile['comfortable_length']}\n"
                  + (f"introduction: {intro}" if intro
                     else "no introduction line (this seiyuu has no name in the library)")
                  + f"  ·  {seconds} request(s) priced  ·  edits are saved to the run's temp "
                    f"folder, never to the chapter file"))
        for widget in self.sections_frame.winfo_children():
            widget.destroy()
        self.boxes = {}
        self.section_tags = {}
        self.open_section = None
        first = [s for s in sections if s["index"] <= 1] or sections[:1]
        self.rest = [s for s in sections if s not in first]
        for section in first:
            self._add_section(section, self.sections_frame)
        if self.rest:
            self._add_tag_strip()
            self.detail = ctk.CTkFrame(self.sections_frame, fg_color="transparent")
            self.detail.pack(fill="both", expand=True)
            self.open_section_at(self.rest[0]["index"])

    def _add_tag_strip(self):
        """One tag per remaining section, in a rounded outlined box - the
        same shape as the chapters sidebar, so the two read alike."""
        strip = ctk.CTkFrame(self.sections_frame, fg_color=COLOR_CARD, corner_radius=10,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        strip.pack(fill="x", pady=(6, 8))
        ctk.CTkLabel(strip, text="SECTIONS", text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=12,
                                                                    pady=(8, 0))
        holder = ctk.CTkFrame(strip, fg_color="transparent")
        holder.pack(fill="x", padx=8, pady=8)
        for number, section in enumerate(self.rest):
            tag = ctk.CTkButton(
                holder, text=str(section["index"]), width=48, height=34, corner_radius=10,
                fg_color=TAG_IDLE, hover_color="#E2E1F4", text_color=COLOR_ENTRY_TEXT,
                border_width=1, border_color=COLOR_ENTRY_BORDER,
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda i=section["index"]: self.open_section_at(i))
            tag.grid(row=number // 18, column=number % 18, padx=3, pady=3)
            self.section_tags[section["index"]] = tag

    def open_section_at(self, index):
        """Show one section's two columns; its tag turns yellow. The boxes
        of the section being left are remembered, so an edit survives
        moving between tags exactly as it survives moving between
        chapters."""
        if self.open_section == index:
            return
        if self.open_section is not None:
            self._remember(self.open_section)
        self.open_section = index
        for widget in self.detail.winfo_children():
            widget.destroy()
        section = next(s for s in self.rest if s["index"] == index)
        self._add_section(section, self.detail)
        for number, tag in self.section_tags.items():
            tag.configure(fg_color=self._tag_fill(number))

    def _tag_fill(self, index):
        if index == self.open_section:
            return TAG_OPEN
        if any(key[0] == index for key in self._changed_keys()):
            return TAG_DIRTY
        if self.current in self.saved:
            return TAG_SAVED
        return TAG_IDLE

    def _changed_keys(self):
        return set(self._collect(self.current))

    def _remember(self, index):
        """Keep a section's text when its tag is closed, so `_collect`
        still sees the edit after the widgets are gone."""
        for (number, side), (box, original) in list(self.boxes.items()):
            if number != index:
                continue
            try:
                now = box.get("1.0", "end-1c")
            except Exception:              # the widget is already gone
                continue
            self.held[(number, side)] = (now, original)
            del self.boxes[(number, side)]

    def _add_section(self, section, parent):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=10,
                            border_width=1, border_color=COLOR_CARD_BORDER)
        card.pack(fill="x", pady=6)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(8, 2))
        requests = ", ".join(
            (f"x{line['request']['duration_scale']}" if line["request"]
             and "duration_scale" in line["request"]
             else f"{line['request']['seconds']}s" if line["request"] else "?")
            for line in section["lines"][:6])
        ctk.CTkLabel(header, text=f"Section {section['index']}", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text=f"   {len(section['lines'])} request(s) · silence "
                                  f"{section['gap']} before · {requests}"
                                  + (" …" if len(section["lines"]) > 6 else ""),
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=11)).pack(side="left")

        columns = ctk.CTkFrame(card, fg_color="transparent")
        columns.pack(fill="x", padx=12, pady=(0, 10))
        for side, title in (("tts", "Text to TTS"), ("reader", "Text to Reader")):
            column = ctk.CTkFrame(columns, fg_color="transparent")
            column.pack(side="left", fill="both", expand=True,
                        padx=(0, 8) if side == "tts" else (8, 0))
            ctk.CTkLabel(column, text=title, text_color=COLOR_SUBTITLE, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x")
            # A section reopened after an edit shows what was typed, not
            # what it was built from.
            text = preview.as_text(section, side)
            held = self.held.get((section["index"], side))
            shown = held[0] if held else text
            height = max(70, 24 * len(section["lines"]) + 16)
            box = ctk.CTkTextbox(column, height=min(height, 420), corner_radius=8,
                                 border_width=1, border_color=COLOR_ENTRY_BORDER,
                                 fg_color="white", text_color=COLOR_ENTRY_TEXT,
                                 font=ctk.CTkFont(size=13), wrap="word")
            box.pack(fill="both", expand=True)
            box.insert("1.0", shown)
            self._mark_readings(box, section, side)
            box.bind("<KeyRelease>", lambda _e, b=self.current: self._touched(b))
            self.boxes[(section["index"], side)] = (box, text)

    def _mark_readings(self, box, section, side):
        """Highlight every stretch a reading or a furigana pair decided:
        the kana in the TTS column, the word it stands for in the reader
        column (user, 2026-09-24 - it is what the review is about).

        A piece records what it used in `readings` ({word: reading}), so
        nothing has to be guessed from the text."""
        mark_tag(box, "reading", COLOR_MARK)
        for number, line in enumerate(section["lines"], start=1):
            text = line["display" if side == "reader" else "tts"]
            for word, reading in (line.get("readings") or {}).items():
                needle = word if side == "reader" else reading
                if not needle:
                    continue
                at = text.find(needle)
                while at >= 0:
                    box.tag_add("reading", f"{number}.{at}",
                                f"{number}.{at + len(needle)}")
                    at = text.find(needle, at + len(needle))

    def _touched(self, base):
        if base and base not in self.dirty:
            self.dirty.add(base)
            self.tab_buttons[base].configure(
                text=base.replace("chapter_", "Chapter ") + " ●")
            self.status.configure(text="unsaved edits", text_color=COLOR_WARN)

    def _collect(self, base):
        """{(section, side): text} for every box whose text changed -
        the sections whose tag is closed included."""
        changed = {}
        for (index, side), (now, original) in self.held.items():
            if now != original:
                changed[(index, side)] = now
        for (index, side), (box, original) in self.boxes.items():
            try:
                now = box.get("1.0", "end-1c")
            except Exception:
                continue
            if now != original:
                changed[(index, side)] = now
            else:
                changed.pop((index, side), None)
        return changed

    def _original_of(self, key):
        """The text the preview built for a box, open or closed - what an
        edit is recorded against."""
        if key in self.boxes:
            return self.boxes[key][1]
        held = self.held.get(key)
        return held[1] if held else ""

    # ------------------------------------------------------------ saving
    def save(self):
        base = self.current
        if not base:
            return
        changed = self._collect(base)
        if not changed:
            self.status.configure(text="nothing to save for this chapter",
                                  text_color=COLOR_SUBTITLE)
            return
        assignment = self._assignment(base)
        sections, problems = preview.apply_edits(self.built[base], changed)
        if problems:
            messagebox.showerror("Line counts differ", "\n\n".join(problems))
            return
        preview.remeasure(sections, assignment["profile"], assignment["style"], self.engine)

        suite = suite_link.open_suite(self.settings)
        edit_ids = []
        if suite is not None and self.book:
            try:
                for (index, side), text in changed.items():
                    original = self._original_of((index, side))
                    edit_id = suite.save_edit(self.book["id"], base, index, side, original, text)
                    if edit_id:
                        edit_ids.append(edit_id)
            finally:
                suite.close()

        path = preview.write_plan(
            self.temp_dir, (self.book or {}).get("slug") or "book", base, sections,
            self._raw(base), assignment["profile"]["_path"], assignment["style"],
            intro_line=self._intro(assignment)[0], edit_ids=edit_ids)
        self.built[base] = sections
        self.dirty.discard(base)
        self.saved.add(base)
        self.current = None                      # force a redraw from the new text
        self.show(base)
        self.status.configure(
            text=f"saved {len(changed)} box(es) · plan written · {len(edit_ids)} recorded",
            text_color=COLOR_OK)
        self._offer_readings(base, changed)
        print(f"preview plan: {path}")

    def _offer_readings(self, base, changed):
        """A TTS-only edit is usually a reading. Offer to remember it for
        the whole book (user decision 2026-09-24: check and offer)."""
        suite = suite_link.open_suite(self.settings)
        if suite is None or not self.book:
            return
        try:
            pairs = []
            for (index, side), text in changed.items():
                if side != "tts":
                    continue
                before = [line["display"] for line in self.built[base][index - 1]["lines"]] \
                    if 0 < index <= len(self.built[base]) else []
                for old, new in zip(before, [t for t in text.splitlines() if t.strip()]):
                    pairs.extend(_word_changes(old, new))
            pairs = [p for p in pairs if suite.reading_get(self.book["id"], p[0]) is None]
            if not pairs:
                return
            listed = "\n".join(f"    {word}  →  {reading}" for word, reading in pairs[:8])
            if messagebox.askyesno(
                    "Remember these readings?",
                    f"These look like readings rather than one-off edits:\n\n{listed}\n\n"
                    f"Add them to this book's readings, so every chapter reads them the same "
                    f"way from now on?"):
                for word, reading in pairs:
                    suite.reading_upsert(self.book["id"], word, reading, scope="book",
                                         origin="manual", chapter=base,
                                         on_conflict="overwrite")
                self.status.configure(text=f"{len(pairs)} reading(s) added to the book",
                                      text_color=COLOR_OK)
        finally:
            suite.close()

    def _close(self):
        if self.dirty and not messagebox.askyesno(
                "Unsaved edits",
                f"{len(self.dirty)} chapter(s) have edits you have not saved.\n\n"
                f"Close and lose them?"):
            return
        if self.on_close_cb:
            self.on_close_cb(sorted(self.saved))
        self.destroy()


def _word_changes(old, new, span=12):
    """[(old word, new word)] for the differing stretches of two lines, when
    they are small enough to be a reading rather than a rewrite."""
    import difflib
    if not old or not new or old == new:
        return []
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "equal":
            continue
        before, after = old[i1:i2], new[j1:j2]
        if before and after and len(before) <= span and len(after) <= span:
            out.append((before, after))
    return out
