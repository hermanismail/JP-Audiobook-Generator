"""
app.py
------
The Scene Illustrator window. Stage A (2026-09-20):

    Read            the book's chapter text -> local LLM -> raw cast & places
                    (runs `illustrator.py read` as a child process)
    Cast & places   you merge, rename, delete, pre-mark main, and keep or
                    drop each detail against the sentence it came from

Every change is saved to <work_root>/<book>/bible.json at once.

Run it with:   uv run python app.py
"""

import os
import queue
import re
import subprocess
import sys
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

import illustrator as il

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BG = "#F7F7FA"
CARD = "#FFFFFF"
BORDER = "#E7E7EC"
TITLE = "#17171C"
SUBTITLE = "#8B8B94"
ENTRY_BORDER = "#E2E2E8"
ENTRY_TEXT = "#3A3A42"
ACCENT = "#6C5DD3"
ACCENT_HOVER = "#5B4FC0"
NEUTRAL_BORDER = "#D8D8DE"
DONE = "#1E8B4E"
WARN = "#B66A16"
FAIL = "#C4453C"
PASTEL_VIOLET = "#EDEBFC"
PASTEL_GREY = "#F1F1F4"
LOG_BG = "#1E1D26"
LOG_FG = "#C9C6D6"

PIECE_RE = re.compile(r"^PIECE (\d+)/(\d+) (\S+) (ok|failed)")


def button(parent, text, command, width=96, primary=False, danger=False, height=32, **kw):
    if primary:
        return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                             fg_color=ACCENT, hover_color=ACCENT_HOVER, command=command,
                             font=ctk.CTkFont(size=13, weight="bold"), **kw)
    return ctk.CTkButton(parent, text=text, width=width, height=height, corner_radius=8,
                         fg_color=CARD, hover_color=BG, border_width=1, border_color=NEUTRAL_BORDER,
                         text_color=FAIL if danger else ENTRY_TEXT, command=command, **kw)


def entry(parent, variable, width=None, placeholder=""):
    widget = ctk.CTkEntry(parent, textvariable=variable, height=32, corner_radius=8, border_width=1,
                          border_color=ENTRY_BORDER, fg_color=CARD, text_color=ENTRY_TEXT,
                          placeholder_text=placeholder)
    if width:
        widget.configure(width=width)
    return widget


def chapters_text(chapters):
    return "ch " + ", ".join(str(c) for c in chapters) if chapters else "added by hand"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Scene Illustrator")
        self.geometry("1240x900")
        self.minsize(1000, 680)
        self.configure(fg_color=BG)
        icon = os.path.join(SCRIPT_DIR, "illustrator_icon.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.settings = il.load_settings()
        self.bible = None
        self.kind = "characters"
        self.current = None            # selected entry id
        self.selected = set()          # ids ticked for merge / delete
        self._proc = None
        self._queue = queue.Queue()

        self.text_var = ctk.StringVar(value=self.settings["last_text_folder"])
        self.book_var = ctk.StringVar(value=self.settings["last_book"])
        self.status_var = ctk.StringVar(value="")
        self.name_var = ctk.StringVar()
        self.main_var = ctk.BooleanVar()
        self.new_detail_var = ctk.StringVar()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        if self.book_var.get():
            self.after(200, self.open_book)

    # ------------------------------------------------------------ building
    def _build(self):
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=58)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="Scene Illustrator", text_color=TITLE,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=20, pady=14)
        ctk.CTkLabel(header, textvariable=self.status_var, text_color=SUBTITLE,
                     font=ctk.CTkFont(size=12)).pack(side="right", padx=20)

        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color=ACCENT_HOVER)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self._build_read(self.tabs.add("1  Read"))
        self._build_cast(self.tabs.add("2  Cast & places"))

    def _card(self, parent, **pack):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 10), **pack)
        return card

    def _build_read(self, tab):
        card = self._card(tab)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(row, text="Chapter text", width=110, anchor="w", text_color=TITLE).pack(side="left")
        button(row, "Browse", self.browse_text, width=80).pack(side="right")
        entry(row, self.text_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(row, text="Book", width=110, anchor="w", text_color=TITLE).pack(side="left")
        entry(row, self.book_var, width=220).pack(side="left")
        self.work_label = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=11))
        self.work_label.pack(side="left", padx=12)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 14))
        self.read_btn = button(row, "Read book", self.start_read, width=120, primary=True)
        self.read_btn.pack(side="left")
        self.stop_btn = button(row, "Stop", self.stop, width=80, danger=True, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.read_info = ctk.CTkLabel(row, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.read_info.pack(side="left", padx=10)
        self.progress = ctk.CTkProgressBar(card, progress_color=ACCENT, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(0, 14))

        self.log = ctk.CTkTextbox(tab, fg_color=LOG_BG, text_color=LOG_FG, corner_radius=11,
                                  font=ctk.CTkFont(family="Consolas", size=12))
        self.log.pack(fill="both", expand=True)
        self.book_var.trace_add("write", lambda *_: self._update_work_label())
        self._update_work_label()

    def _build_cast(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        self.kind_switch = ctk.CTkSegmentedButton(bar, values=["Characters", "Places"],
                                                  command=self.switch_kind,
                                                  selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self.kind_switch.set("Characters")
        self.kind_switch.pack(side="left")
        button(bar, "Merge ticked", self.merge_selected, width=120).pack(side="left", padx=(16, 6))
        button(bar, "Delete ticked", self.delete_selected, width=120, danger=True).pack(side="left")
        self.suggest_btn = button(bar, "Suggest merges", self.start_suggest, width=140)
        self.suggest_btn.pack(side="left", padx=6)
        self.cast_info = ctk.CTkLabel(bar, text="", text_color=SUBTITLE, font=ctk.CTkFont(size=12))
        self.cast_info.pack(side="right")

        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.list_frame = ctk.CTkScrollableFrame(body, width=330, fg_color=CARD, corner_radius=11,
                                                 border_width=1, border_color=BORDER)
        self.list_frame.pack(side="left", fill="y")
        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=11, border_width=1, border_color=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        top = ctk.CTkFrame(right, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(top, text="Name", width=50, anchor="w", text_color=TITLE).pack(side="left")
        name_entry = entry(top, self.name_var, width=260)
        name_entry.pack(side="left")
        name_entry.bind("<Return>", lambda _e: self.rename())
        name_entry.bind("<FocusOut>", lambda _e: self.rename())
        self.main_box = ctk.CTkCheckBox(top, text="Pre-mark as main (gets a reference)",
                                        variable=self.main_var, command=self.toggle_main,
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ENTRY_TEXT)
        self.main_box.pack(side="left", padx=16)
        self.members_label = ctk.CTkLabel(right, text="", anchor="w", justify="left", text_color=SUBTITLE,
                                          font=ctk.CTkFont(size=11), wraplength=780)
        self.members_label.pack(fill="x", padx=14)
        note_row = ctk.CTkFrame(right, fg_color="transparent")
        note_row.pack(fill="x", padx=14, pady=(4, 4))
        ctk.CTkLabel(note_row, text="Note", width=50, anchor="w", text_color=TITLE).pack(side="left", anchor="n")
        self.note_box = ctk.CTkTextbox(note_row, height=46, fg_color=CARD, border_width=1,
                                       border_color=ENTRY_BORDER, text_color=ENTRY_TEXT)
        self.note_box.pack(side="left", fill="x", expand=True)
        self.note_box.bind("<FocusOut>", lambda _e: self.save_note())

        self.detail_frame = ctk.CTkScrollableFrame(right, fg_color=CARD)
        self.detail_frame.pack(fill="both", expand=True, padx=6, pady=4)
        add = ctk.CTkFrame(right, fg_color="transparent")
        add.pack(fill="x", padx=14, pady=(0, 12))
        add_entry = entry(add, self.new_detail_var, placeholder="Add a detail by hand (English)")
        add_entry.pack(side="left", fill="x", expand=True)
        add_entry.bind("<Return>", lambda _e: self.add_detail())
        button(add, "Add", self.add_detail, width=70).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------- helpers
    def _update_work_label(self):
        book = self.book_var.get().strip()
        self.work_label.configure(text=il.book_dir(self.settings, book) if book else "")

    def log_line(self, line):
        self.log.insert("end", line + "\n")
        self.log.see("end")

    def root(self):
        return il.book_dir(self.settings, self.book_var.get().strip())

    def browse_text(self):
        folder = filedialog.askdirectory(initialdir=self.text_var.get() or "F:\\")
        if folder:
            self.text_var.set(os.path.normpath(folder))
            self.book_var.set(il.book_name_for(folder))
            self.open_book()

    def remember(self):
        self.settings["last_text_folder"] = self.text_var.get().strip()
        self.settings["last_book"] = self.book_var.get().strip()
        il.save_settings(self.settings)

    def open_book(self):
        if not self.book_var.get().strip():
            return
        self.refresh_read_info()
        self.bible = il.load_bible(self.root())
        self.current = None
        self.selected.clear()
        self.render_list()
        self.render_entry()

    def refresh_read_info(self):
        text = self.text_var.get().strip()
        if not os.path.isdir(text):
            self.read_info.configure(text="choose the chapter text folder", text_color=FAIL)
            return
        todo = il.all_pieces(text, int(self.settings["piece_chars"]))
        done = sum(os.path.isfile(il.piece_path(self.root(), p[0])) for p in todo)
        failed = sum(os.path.isfile(il.piece_path(self.root(), p[0], failed=True)) for p in todo)
        chapters = len(il.chapter_files(text))
        msg = f"{chapters} chapters, {len(todo)} pieces - {done} read"
        if failed:
            msg += f", {failed} failed (Read again retries them)"
        self.read_info.configure(text=msg, text_color=FAIL if failed else (DONE if done == len(todo) else SUBTITLE))
        self.progress.set(done / len(todo) if todo else 0)

    # ------------------------------------------------------- child process
    def _run_child(self, args, on_done):
        if self._proc:
            return
        self.remember()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-u", os.path.join(SCRIPT_DIR, "illustrator.py")] + args
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                      encoding="utf-8", errors="replace",
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        self.read_btn.configure(state="disabled")
        self.suggest_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        proc = self._proc

        def pump():
            # keep reading whatever happens, or the child blocks on a full pipe
            for line in proc.stdout:
                self._queue.put(("line", line.rstrip()))
            proc.wait()
            self._queue.put(("exit", (proc.returncode, on_done)))
        threading.Thread(target=pump, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == "line":
                    self.log_line(value)
                    m = PIECE_RE.match(value)
                    if m:
                        self.progress.set(int(m.group(1)) / int(m.group(2)))
                        self.status_var.set(f"reading {m.group(3)}")
                    elif value.startswith(("SERVER", "SUGGEST")):
                        self.status_var.set(value.split(" ", 1)[1])
                else:
                    code, on_done = value
                    self._proc = None
                    self.read_btn.configure(state="normal")
                    self.suggest_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set("" if code == 0 else f"stopped (exit {code})")
                    on_done(code)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def stop(self):
        if self._proc:
            # the tree, so llama-server goes too and VRAM comes back
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self._proc.pid)], capture_output=True)
            self.log_line("-- stopped by you")

    def start_read(self):
        text = self.text_var.get().strip()
        book = self.book_var.get().strip()
        if not os.path.isdir(text) or not il.chapter_files(text):
            messagebox.showerror("Scene Illustrator", "Choose a folder holding chapter_*.txt files.")
            return
        if not book:
            messagebox.showerror("Scene Illustrator", "Give the book a name.")
            return
        self.log_line(f"-- reading {book}")
        self._run_child(["read", "--book", book, "--text", text], self._read_done)

    def _read_done(self, _code):
        self.open_book()

    def start_suggest(self):
        if not self.bible:
            return
        self.tabs.set("1  Read")
        self.log_line("-- asking the model for merge suggestions")
        self._run_child(["suggest", "--book", self.book_var.get().strip()], self._suggest_done)

    def _suggest_done(self, code):
        path = os.path.join(self.root(), "suggestions.json")
        if code == 0 and os.path.isfile(path):
            self.tabs.set("2  Cast & places")
            SuggestionWindow(self, il.read_json(path))

    # ------------------------------------------------------------ the bible
    def save(self):
        il.save_bible(self.root(), self.bible)

    def switch_kind(self, value):
        self.kind = "characters" if value == "Characters" else "places"
        self.current = None
        self.selected.clear()
        self.render_list()
        self.render_entry()

    def render_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.bible:
            return
        entries = sorted(self.bible[self.kind], key=lambda e: (-len(e["chapters"]), min(e["chapters"] or [99])))
        for e in entries:
            row = ctk.CTkFrame(self.list_frame, fg_color=PASTEL_VIOLET if e["id"] == self.current else CARD,
                               corner_radius=8, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=2, padx=2)
            var = ctk.BooleanVar(value=e["id"] in self.selected)
            ctk.CTkCheckBox(row, text="", width=24, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            command=lambda i=e["id"], v=var: self.tick(i, v)).pack(side="left", padx=(6, 0))
            kept = sum(d["keep"] for d in e["details"])
            head = e["name"] + ("  ★" if e["main"] else "")
            label = ctk.CTkLabel(row, text=f"{head}\n{chapters_text(e['chapters'])} · {kept}/{len(e['details'])} details"
                                 + ("  · NEW" if any(d.get("new") for d in e["details"]) else ""),
                                 anchor="w", justify="left", text_color=TITLE, font=ctk.CTkFont(size=12))
            label.pack(side="left", fill="x", expand=True, pady=4)
            for w in (row, label):
                w.bind("<Button-1>", lambda _ev, i=e["id"]: self.select(i))
        chars, places = len(self.bible["characters"]), len(self.bible["places"])
        self.cast_info.configure(text=f"{chars} characters, {places} places · saved to bible.json")

    def tick(self, entry_id, var):
        (self.selected.add if var.get() else self.selected.discard)(entry_id)

    def select(self, entry_id):
        self.save_note()
        self.current = entry_id
        self.render_list()
        self.render_entry()

    def entry(self):
        if not self.bible or not self.current:
            return None
        return next((e for e in self.bible[self.kind] if e["id"] == self.current), None)

    def render_entry(self):
        for w in self.detail_frame.winfo_children():
            w.destroy()
        e = self.entry()
        self.note_box.delete("1.0", "end")
        if not e:
            self.name_var.set("")
            self.members_label.configure(text="Select an entry on the left.")
            return
        self.name_var.set(e["name"])
        self.main_var.set(e["main"])
        bits = [chapters_text(e["chapters"]), "from: " + " / ".join(e["members"])]
        if e.get("aliases"):
            bits.append("aliases: " + ", ".join(e["aliases"]))
        if e.get("roles"):
            more = f" (+{len(e['roles']) - 3} more)" if len(e["roles"]) > 3 else ""
            bits.append("role: " + "; ".join(e["roles"][:3]) + more)
        self.members_label.configure(text="   ·   ".join(bits))
        self.note_box.insert("1.0", e.get("note", ""))
        for d in sorted(e["details"], key=lambda d: (d["chapter"], d["piece"])):
            self._detail_row(e, d)

    def _detail_row(self, e, d):
        row = ctk.CTkFrame(self.detail_frame, fg_color=CARD if d["keep"] else PASTEL_GREY,
                           corner_radius=8, border_width=1, border_color=BORDER)
        row.pack(fill="x", pady=2, padx=2)
        var = ctk.BooleanVar(value=d["keep"])
        ctk.CTkCheckBox(row, text="", width=24, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=lambda: self.keep(d, var.get())).pack(side="left", padx=(8, 4), anchor="n", pady=8)
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True, pady=4)
        ctk.CTkLabel(col, text=d["text"] + ("   NEW" if d.get("new") else ""), anchor="w", justify="left",
                     wraplength=720, text_color=TITLE if d["keep"] else SUBTITLE,
                     font=ctk.CTkFont(size=13)).pack(fill="x")
        if d["match"] == "user":
            src, colour = "added by hand", SUBTITLE
        elif d["match"] == "none":
            src, colour = f"ch {d['chapter']} · no source sentence found" + (f" (model quoted: {d['quote']})" if d["quote"] else ""), FAIL
        else:
            src = f"ch {d['chapter']} · {d['source']}"
            colour = WARN if d["match"] == "fuzzy" else SUBTITLE
            if d["match"] == "fuzzy":
                src += "   (closest sentence - check it)"
        ctk.CTkLabel(col, text=src, anchor="w", justify="left", wraplength=720, text_color=colour,
                     font=ctk.CTkFont(size=12)).pack(fill="x")

    def keep(self, d, value):
        d["keep"] = value
        d.pop("new", None)
        self.save()
        self.render_list()
        self.render_entry()

    def rename(self):
        e = self.entry()
        name = self.name_var.get().strip()
        if e and name and name != e["name"]:
            e["name"] = name
            self.save()
            self.render_list()

    def toggle_main(self):
        e = self.entry()
        if e:
            e["main"] = self.main_var.get()
            self.save()
            self.render_list()

    def save_note(self):
        e = self.entry()
        if e:
            note = self.note_box.get("1.0", "end").strip()
            if note != e.get("note", ""):
                e["note"] = note
                self.save()

    def add_detail(self):
        e = self.entry()
        text = self.new_detail_var.get().strip()
        if e and text:
            il.add_detail(e, text)
            self.new_detail_var.set("")
            self.save()
            self.render_list()
            self.render_entry()

    def merge_selected(self, ids=None, name=None):
        ids = list(ids or self.selected)
        if len(ids) < 2:
            messagebox.showinfo("Merge", "Tick two or more entries to merge.")
            return
        entries = [il.find(self.bible, self.kind, i) for i in ids]
        if name is None:
            name = MergeDialog(self, [e["name"] for e in entries]).result
            if not name:
                return
        target = il.merge(self.bible, self.kind, ids, name)
        self.save()
        self.selected.clear()
        self.current = target["id"]
        self.render_list()
        self.render_entry()

    def delete_selected(self):
        ids = list(self.selected)
        if not ids:
            return
        names = ", ".join(il.find(self.bible, self.kind, i)["name"] for i in ids)
        if not messagebox.askyesno("Delete", f"Delete {names}?\n\nA re-read will not bring them back."):
            return
        for i in ids:
            il.delete(self.bible, self.kind, i)
        self.save()
        self.selected.clear()
        if self.current in ids:
            self.current = None
        self.render_list()
        self.render_entry()

    def on_close(self):
        self.save_note()
        if self._proc:
            if not messagebox.askyesno("Scene Illustrator", "A run is in progress. Stop it and close?"):
                return
            self.stop()
        self.remember()
        self.destroy()


class MergeDialog(ctk.CTkToplevel):
    """Pick (or type) the name the merged entry keeps."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.title("Merge")
        self.configure(fg_color=BG)
        self.result = None
        self.var = ctk.StringVar(value=names[0])
        ctk.CTkLabel(self, text="Name for the merged entry:", text_color=TITLE).pack(padx=20, pady=(16, 6), anchor="w")
        for n in names:
            ctk.CTkRadioButton(self, text=n, variable=self.var, value=n, fg_color=ACCENT).pack(padx=24, pady=2, anchor="w")
        entry(self, self.var, width=320).pack(padx=20, pady=8)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(4, 16))
        button(row, "Merge", self.ok, primary=True).pack(side="left", padx=6)
        button(row, "Cancel", self.destroy).pack(side="left")
        self.grab_set()
        self.wait_window()

    def ok(self):
        self.result = self.var.get().strip()
        self.destroy()


class SuggestionWindow(ctk.CTkToplevel):
    """The model's merge ideas, accepted or dismissed one by one. Never
    applied automatically (measured 2026-09-18: it merged Mari with Eri)."""

    def __init__(self, app, suggestions):
        super().__init__(app)
        self.app = app
        self.title("Suggested merges")
        self.geometry("760x560")
        self.configure(fg_color=BG)
        frame = ctk.CTkScrollableFrame(self, fg_color=BG)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        any_ = False
        for kind in il.KINDS:
            for g in suggestions.get(kind, []):
                live = [i for i in g["ids"] if any(e["id"] == i for e in app.bible[kind])]
                if len(live) < 2:
                    continue
                any_ = True
                names = " + ".join(il.find(app.bible, kind, i)["name"] for i in live)
                card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=8, border_width=1, border_color=BORDER)
                card.pack(fill="x", pady=4)
                ctk.CTkLabel(card, text=f"{kind[:-1]}: {names}  ->  {g['name']}", anchor="w", wraplength=680,
                             justify="left", text_color=TITLE, font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=10, pady=(8, 0))
                if g.get("why"):
                    ctk.CTkLabel(card, text=g["why"], anchor="w", wraplength=680, justify="left",
                                 text_color=SUBTITLE).pack(fill="x", padx=10)
                row = ctk.CTkFrame(card, fg_color="transparent")
                row.pack(fill="x", padx=10, pady=8)
                button(row, "Accept", lambda k=kind, ids=live, n=g["name"], c=card: self.accept(k, ids, n, c),
                       primary=True, width=90).pack(side="left")
                button(row, "Dismiss", card.destroy, width=90).pack(side="left", padx=6)
        if not any_:
            ctk.CTkLabel(frame, text="No suggestions.", text_color=SUBTITLE).pack(pady=20)

    def accept(self, kind, ids, name, card):
        # an earlier accept may already have merged some of these away
        ids = [i for i in ids if any(e["id"] == i for e in self.app.bible[kind])]
        if len(ids) < 2:
            card.destroy()
            return
        if self.app.kind != kind:
            self.app.kind_switch.set("Characters" if kind == "characters" else "Places")
            self.app.switch_kind(self.app.kind_switch.get())
        self.app.merge_selected(ids=ids, name=name)
        card.destroy()


if __name__ == "__main__":
    App().mainloop()
