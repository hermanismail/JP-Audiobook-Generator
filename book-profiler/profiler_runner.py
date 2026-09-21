"""
profiler_runner.py
------------------
Everything the Book Profiler window does that is not drawing widgets:
reading a book folder, the unattended run queue, the state of what is
already on disk, and the clean-up. `app.py` is the window over it.

The run is the CLI, not a copy of it. Every stage is the same script a
developer runs by hand (`analyze.py`, `sweep.py`, `score.py`, `recipe.py`,
`listen.py`), started as a child process of this venv's Python with the
GUI's choices passed as flags. So there is one code path, and a run the
window started can be finished, inspected or re-scored from a terminal.

## What a Run does (decisions 2026-09-18)

    analyse the whole book            once, CPU, seconds
    per ticked chapter:  sweep, score  GPU, retried once, then skipped
    per seiyuu:          recipe       every scored chapter under the root

The user queues it and leaves, so a failure never stops the queue: a stage
that fails is tried once more (the sweep resumes from its markers, so a
retry costs only what was lost), and a chapter that fails twice is marked
and skipped. The PC is held awake for the run's duration.

## Fixed parameters

A window user picks only the book, the seiyuu and the profile root.
Everything else is what was measured to work (see CLAUDE.md, book-profiler)
and stays in settings.json for developers. The one choice exposed is the
seed tick box:

    unticked   random seeds, 6 takes per cell
    ticked     3 fixed-seed + 3 random - the method the tanya recipe used

Six takes either way. Rebuilding tanya's recipe from its 3 random takes
alone made 3 of 6 chapters less cautious (chapter_001's shortest band went
from x1.5/1.7 to x1.1/1.5): what matters is how many chances a cell has to
show a hallucination, not whether the seeds are fixed.
"""

import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
for _path in (GENERATOR_DIR, SCRIPT_DIR, os.path.join(GENERATOR_DIR, "seiyuu-audition")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import text_pipeline as tp  # noqa: E402
import dynamic_profile  # noqa: E402
import analyze  # noqa: E402
import sweep  # noqa: E402

SCOPE = "book"      # the window always analyses the whole book
GUI_STATE_PATH = os.path.join(SCRIPT_DIR, "gui_state.json")
GUI_DEFAULTS = {
    "book_folder": "",
    "profile_root": "",
    "speaker_path": "",
    "assign": "all",            # "all" | "custom"
    "chapters": {},             # {base: {"enabled": bool, "speaker_path": str}}
    "seeded_arm": False,
    "clean_root": "",
}
ARM_OPTIONS = {False: ("random", 6), True: ("seeded,random", 3)}
ATTEMPTS = 2                    # retry once, then skip
NO_WINDOW_EXIT = 3              # recipe.NO_WINDOW_EXIT: no clean window, never retried
# Measured per chapter on this card (2026-09-17, memory released per job):
# sweep 7-18 min, Whisper 6-10 min.
MINUTES_PER_CHAPTER = (15, 30)
PROGRESS_BAR_RE = re.compile(r"\d+%\|.*\|\s*\d+/\d+ \[")
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ------------------------------------------------------------ state file

def load_gui_state():
    try:
        with open(GUI_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    state = dict(GUI_DEFAULTS)
    state.update({k: v for k, v in data.items() if k in GUI_DEFAULTS})
    return state


def save_gui_state(state):
    with open(GUI_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({k: state.get(k, v) for k, v in GUI_DEFAULTS.items()}, f,
                  ensure_ascii=False, indent=2)


def settings():
    """The developer settings the stages run with, for the paths only."""
    return sweep.load_settings()


# ------------------------------------------------------------ inputs

def parse_book_folder(folder):
    """(ok, message, [chapter bases]) - the same files analyze.py --book
    takes (every .txt), each of which must decode and hold a sentence."""
    if not folder or not folder.strip():
        return False, "Choose the folder that holds the chapter .txt files.", []
    if not os.path.isdir(folder):
        return False, "This folder does not exist - choose the folder that holds " \
                      "the chapter .txt files.", []
    files = analyze.chapter_files(folder)
    if not files:
        return False, "No .txt files in this folder - choose the folder that holds " \
                      "the chapters.", []
    bases, problems = [], []
    for path in files:
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except UnicodeDecodeError:
            problems.append(f"{base} is not UTF-8")
            continue
        except OSError as e:
            problems.append(f"{base} could not be read ({e.strerror})")
            continue
        if not tp.dynamic_sentences(raw):
            problems.append(f"{base} has no text")
            continue
        bases.append(base)
    if problems:
        return False, "Parsing failed: " + "; ".join(problems[:4]) + \
            (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "") + \
            ". Fix the file(s) or choose the correct folder.", bases
    book = analyze.book_name(folder)
    span = f"{bases[0]} … {bases[-1]}" if len(bases) > 1 else bases[0]
    return True, f"Parsing successful - book \"{book}\", {len(bases)} chapter file(s) " \
                 f"({span})", bases


def list_speakers(irodori_root):
    import audition
    return audition.list_speakers(irodori_root)


def nickname(speaker_path):
    return sweep.nickname_for(speaker_path) if speaker_path else ""


# ------------------------------------------------------------ what is on disk

def book_base(root, book):
    return os.path.join(root, book, SCOPE)


def chapter_root(root, book, speaker, chapter):
    return sweep.chapter_dir({"work_root": root}, book, SCOPE, speaker, chapter)


def chapter_state(root, book, speaker, chapter):
    """(key, text) for the status grid. Read from the files the stages
    leave behind, so the window and a terminal run agree."""
    if not root or not speaker:
        return "none", ""
    folder = chapter_root(root, book, speaker, chapter)
    if os.path.isfile(os.path.join(folder, analyze.CLEANED_MARKER)):
        return "cleaned", "measured · audio cleaned up"
    sweep_json = os.path.join(folder, "sweep.json")
    score_json = os.path.join(folder, "score.json")
    # Scored = no take was rendered after the score. Not "score.json is
    # newer than sweep.json": a report-only sweep rewrites sweep.json
    # without rendering anything (tanya's chapter_001 is like that).
    markers = glob.glob(os.path.join(folder, "step*", "*", "*.json"))
    if os.path.isfile(score_json) and markers and \
            max(os.path.getmtime(m) for m in markers) <= os.path.getmtime(score_json):
        return "scored", "measured"
    if os.path.isfile(sweep_json):
        try:
            with open(sweep_json, "r", encoding="utf-8") as f:
                rows = json.load(f).get("rows", [])
        except (OSError, ValueError):
            rows = []
        if rows and all(not r["status"].startswith(("in progress", "not started"))
                        for r in rows):
            return "swept", "rendered · not scored yet"
        return "partial", "partly rendered - Run resumes it"
    if glob.glob(os.path.join(folder, "step*", "*", "*.json")):
        return "partial", "partly rendered - Run resumes it"
    return "none", "not started"


def recipe_dir(root, book, speaker):
    return os.path.join(book_base(root, book), "recipe", nickname(speaker))


def profiles(root, book, speaker):
    """[(label, path)] - the book profile first, then the chapters."""
    folder = recipe_dir(root, book, speaker)
    found = sorted(glob.glob(os.path.join(folder, "profile_*.json")))
    book_first = sorted(found, key=lambda p: (not p.endswith("profile_book.json"), p))
    return [(os.path.basename(p)[len("profile_"):-len(".json")], p) for p in book_first]


def listen_dir(root, book, speaker, profile_label):
    return os.path.join(book_base(root, book), "listen", nickname(speaker), profile_label)


def listening_ready(root, book, speaker, profile_label):
    folder = listen_dir(root, book, speaker, profile_label)
    return len(glob.glob(os.path.join(folder, "sample_*.wav"))) == 6


# ------------------------------------------------------------ the machine

def gpu_warning():
    """A reason to warn before a run, or None. The card fits ONE TTS worker
    (CLAUDE.md: two workers are catastrophically slower, not an error), so
    anything already holding VRAM means the run will crawl. Warn, never
    refuse - the user may queue it and leave the card to it (2026-09-18)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        used, total = [int(v) for v in out.stdout.strip().splitlines()[0].split(",")]
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    heavy = sorted({os.path.basename(line.strip()) for line in apps.splitlines()
                    if any(k in line.lower() for k in ("python", "llama", "whisper", "uv.exe"))})
    # The desktop alone holds ~1.4 GB here; a TTS worker or VNTL holds 5-7.
    if used < 3000 and not heavy:
        return None
    parts = [f"The GPU already has {used / 1024:.1f} of {total / 1024:.1f} GB in use"]
    if heavy:
        parts.append(" by " + ", ".join(heavy))
    return "".join(parts) + (". If the generator, the audition tool or llama-server is "
                             "running, the profiler will share the card with it and run "
                             "far slower until it finishes.")


class KeepAwake:
    """Holds the PC out of sleep (the screen may still turn off) while a run
    is active. SetThreadExecutionState is per THREAD, so it is set and
    cleared on the runner thread itself."""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        except (AttributeError, OSError):
            pass
        return self

    def __exit__(self, *_exc):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        except (AttributeError, OSError):
            pass


def _never_raises(callback):
    """A UI callback that fails must never stop an unattended run - neither
    the queue's own messages nor the reading of a stage's pipe (an unread
    pipe fills and the stage blocks forever; see _stage)."""
    def call(*args):
        try:
            callback(*args)
        except Exception:
            pass
    return call


def kill_tree(proc):
    """The whole tree: stopping the Python child alone left uv and the TTS
    worker holding 7.7 GB of VRAM once (CLAUDE.md)."""
    if proc is None or proc.poll() is not None:
        return
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


# ------------------------------------------------------------ the run

class Run:
    """One queued run on its own thread.

    plan      [(chapter, speaker_path)] in chapter order
    on_log    (message) - every line of every stage
    on_state  (chapter, key, text) - a status-grid change
    on_done   (summary dict)
    """

    def __init__(self, book_folder, root, plan, seeded_arm, on_log, on_state, on_done):
        self.book_folder = os.path.abspath(book_folder)
        self.book = analyze.book_name(book_folder)
        self.root = os.path.abspath(root)
        self.plan = plan
        self.arms, self.takes = ARM_OPTIONS[bool(seeded_arm)]
        self.on_log, self.on_state = _never_raises(on_log), _never_raises(on_state)
        self.on_done = _never_raises(on_done)
        self.stopped = threading.Event()
        self.proc = None
        self.thread = threading.Thread(target=self._main, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        kill_tree(self.proc)

    # --- plumbing
    def _stage(self, script, *args):
        """Runs one CLI stage; returns its exit code (None if stopped)."""
        cmd = [sys.executable, "-u", os.path.join(SCRIPT_DIR, script), *args,
               "--work-root", self.root]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # The stage runs on this venv's own interpreter; an inherited
        # VIRTUAL_ENV only makes the TTS worker's `uv run` warn that it
        # does not match the Irodori project.
        env.pop("VIRTUAL_ENV", None)
        self.proc = subprocess.Popen(cmd, cwd=SCRIPT_DIR, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     encoding="utf-8", errors="replace", env=env,
                                     creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        read_all = False
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                # Whisper's per-file progress bars: hundreds of lines a
                # chapter, all noise (score.log still has them).
                if not line or PROGRESS_BAR_RE.search(line):
                    continue
                # on_log never raises (_never_raises): if it could, the
                # reading would stop, the unread pipe would fill, the stage
                # would block writing to it and wait() below would never
                # return. Found 2026-09-18 - a cp1252 console could not
                # print Whisper's bar and a run hung for hours, GPU idle.
                self.on_log(line)
            read_all = True
        finally:
            if not read_all:
                kill_tree(self.proc)
            code = self.proc.wait()
            self.proc = None
        return None if self.stopped.is_set() else code

    def _attempts(self, label, script, *args, final=()):
        """True / False / None (stopped). An exit code in `final` is an
        answer, not a failure: the same inputs would give it again, so it
        is not retried."""
        for attempt in range(1, ATTEMPTS + 1):
            if self.stopped.is_set():
                return None
            if attempt > 1:
                self.on_log(f"--- {label}: retrying (attempt {attempt} of {ATTEMPTS})")
            code = self._stage(script, *args)
            if code is None:
                return None
            if code == 0:
                return True
            self.on_log(f"! {label} exited with code {code}")
            if code in final:
                return False
        return False

    # --- the queue
    def _main(self):
        began = time.time()
        summary = {"measured": [], "failed": [], "skipped_cleaned": [], "recipes": [],
                   "recipe_failed": [], "stopped": False, "analysis_failed": False}
        with KeepAwake():
            try:
                self._queue(summary)
            except Exception as e:                       # never lose the reason
                self.on_log(f"! {type(e).__name__}: {e}")
                summary["error"] = str(e)
        summary["stopped"] = self.stopped.is_set()
        summary["minutes"] = round((time.time() - began) / 60, 1)
        self.on_done(summary)

    def _queue(self, summary):
        self.on_log(f"=== {self.book}: {len(self.plan)} chapter(s), seeds: "
                    f"{self.arms} x{self.takes} takes -> {self.root}")
        self.on_log("=== analyse the book")
        ok = self._attempts("analysis", "analyze.py", "--book", self.book_folder,
                            "--name", self.book)
        if not ok:
            summary["analysis_failed"] = ok is False
            return
        for chapter, speaker in self.plan:
            if self.stopped.is_set():
                return
            key, _text = chapter_state(self.root, self.book, speaker, chapter)
            if key == "cleaned":
                self.on_log(f"=== {chapter}: already measured and cleaned up - skipped")
                summary["skipped_cleaned"].append(chapter)
                continue
            common = ["--book", self.book, "--scope", SCOPE, "--chapter", chapter,
                      "--speaker", speaker, "--arms", self.arms, "--takes", str(self.takes)]
            self.on_log(f"=== {chapter} with {nickname(speaker)}: sweep")
            self.on_state(chapter, "running", "rendering takes…")
            ok = self._attempts(f"{chapter} sweep", "sweep.py", *common)
            if ok:
                self.on_log(f"=== {chapter}: score (Whisper)")
                self.on_state(chapter, "running", "transcribing with Whisper…")
                ok = self._attempts(f"{chapter} score", "score.py", *common)
            if ok is None:
                self.on_state(chapter, *chapter_state(self.root, self.book, speaker, chapter))
                return
            if ok:
                summary["measured"].append(chapter)
                self.on_state(chapter, "scored", "measured")
            else:
                summary["failed"].append(chapter)
                self.on_state(chapter, "failed", "failed twice - skipped (see log)")
        for speaker in sorted({s for _c, s in self.plan}):
            if self.stopped.is_set():
                return
            scored = [c for c in self._all_chapters()
                      if chapter_state(self.root, self.book, speaker, c)[0]
                      in ("scored", "cleaned")]
            if not scored:
                self.on_log(f"=== {nickname(speaker)}: no measured chapter - no recipe")
                continue
            self.on_log(f"=== recipe for {nickname(speaker)} over {len(scored)} chapter(s)")
            ok = self._attempts(f"{nickname(speaker)} recipe", "recipe.py", "--book", self.book,
                                "--scope", SCOPE, "--speaker", speaker,
                                final=(NO_WINDOW_EXIT,))
            (summary["recipes"] if ok else summary["recipe_failed"]).append(nickname(speaker))

    def _all_chapters(self):
        return [os.path.splitext(os.path.basename(p))[0]
                for p in analyze.chapter_files(self.book_folder)]


class ListenRun(Run):
    """The optional listening test for one profile: every style it offers
    rendered, then the audition tool's Results window opened in its own
    process (listen.py --window-only), so closing it never touches this
    window."""

    def __init__(self, book, root, speaker, profile_label, on_log, on_done):
        self.book, self.root = book, os.path.abspath(root)
        self.speaker, self.profile_label = speaker, profile_label
        self.on_log, self.on_done = _never_raises(on_log), _never_raises(on_done)
        self.stopped = threading.Event()
        self.proc = None
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self):
        args = ["--book", self.book, "--scope", SCOPE, "--speaker", self.speaker,
                "--profile", self.profile_label]
        with KeepAwake():
            self.on_log(f"=== listening test: {nickname(self.speaker)} / {self.profile_label}")
            ok = self._attempts("listening test", "listen.py", *args, "--no-window")
        if ok:
            open_listening_window(self.book, self.root, self.speaker, self.profile_label)
        self.on_done({"ok": bool(ok), "stopped": self.stopped.is_set()})


def open_listening_window(book, root, speaker, profile_label):
    subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, "listen.py"), "--book", book,
                      "--scope", SCOPE, "--speaker", speaker, "--profile", profile_label,
                      "--window-only", "--work-root", os.path.abspath(root)],
                     cwd=SCRIPT_DIR, creationflags=CREATE_NO_WINDOW)


# ------------------------------------------------------------ clean-up

def _size(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def cleanup_rows(root):
    """One row per (book, scope, seiyuu) that has sweep audio or a recipe
    under `root`:

        {book, scope, seiyuu, sweep_dir, listen_dir, recipe_dir, has_recipe,
         covered: [chapter], uncovered: [chapter], cleaned: [chapter],
         wavs: [path], bytes}

    `covered` chapters are the ones clean-up may touch: scored AND inside
    the seiyuu's book profile. A chapter still being measured, or one that
    failed, keeps its takes - deleting them would only cost a re-render."""
    rows = []
    if not root or not os.path.isdir(root):
        return rows
    for sweep_dir in sorted(glob.glob(os.path.join(root, "*", "*", "sweep", "*"))):
        if not os.path.isdir(sweep_dir):
            continue
        base = os.path.dirname(os.path.dirname(sweep_dir))
        seiyuu = os.path.basename(sweep_dir)
        book, scope = os.path.basename(os.path.dirname(base)), os.path.basename(base)
        rdir = os.path.join(base, "recipe", seiyuu)
        ldir = os.path.join(base, "listen", seiyuu)
        profiled = set()
        book_profile = os.path.join(rdir, "profile_book.json")
        if os.path.isfile(book_profile):
            try:
                profiled = set(dynamic_profile.load_profile(book_profile).get("chapters") or [])
            except dynamic_profile.ProfileError:
                profiled = set()
        covered, uncovered, cleaned, wavs = [], [], [], []
        for chapter_dir in sorted(glob.glob(os.path.join(sweep_dir, "*"))):
            if not os.path.isdir(chapter_dir):
                continue
            chapter = os.path.basename(chapter_dir)
            if os.path.isfile(os.path.join(chapter_dir, analyze.CLEANED_MARKER)):
                cleaned.append(chapter)
                continue
            if chapter in profiled and os.path.isfile(os.path.join(chapter_dir, "score.json")):
                covered.append(chapter)
                wavs += glob.glob(os.path.join(chapter_dir, "step*", "*", "*.wav"))
            else:
                uncovered.append(chapter)
        if covered:
            wavs += [p for p in glob.glob(os.path.join(ldir, "**", "*.wav"), recursive=True)]
        rows.append({"book": book, "scope": scope, "seiyuu": seiyuu, "sweep_dir": sweep_dir,
                     "listen_dir": ldir, "recipe_dir": rdir, "has_recipe": bool(profiled),
                     "covered": covered, "uncovered": uncovered, "cleaned": cleaned,
                     "wavs": wavs, "bytes": _size(wavs)})
    return rows


def _inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return os.path.commonpath([path, folder]) == folder and path != folder


def clean_up(row, log):
    """Permanently deletes the row's audio (decision 2026-09-18: no Recycle
    Bin - gigabytes piling up there is worse than no undo) and marks every
    covered chapter cleaned. Only .wav files are deleted, each checked to
    lie inside the row's own sweep or listen folder; the markers, Whisper
    transcripts, score.json, recipe and profiles all stay, so the recipe
    can still be rebuilt and inspected. Returns (files, bytes)."""
    removed, freed = 0, 0
    when = time.strftime("%Y-%m-%d %H:%M")
    for chapter in row["covered"]:
        folder = os.path.join(row["sweep_dir"], chapter)
        mine = [p for p in glob.glob(os.path.join(folder, "step*", "*", "*.wav"))
                if _inside(p, row["sweep_dir"])]
        size = _size(mine)
        # The marker goes down FIRST: if this is interrupted half way, the
        # chapter is already protected from a silent re-render.
        with open(os.path.join(folder, analyze.CLEANED_MARKER), "w", encoding="utf-8") as f:
            json.dump({"cleaned": when, "files": len(mine), "bytes": size,
                       "kept": "take markers, Whisper transcripts, sweep.json, score.json"},
                      f, ensure_ascii=False, indent=2)
        for p in mine:
            os.remove(p)
        removed += len(mine)
        freed += size
        log(f"{row['book']} / {row['seiyuu']} / {chapter}: {len(mine)} take(s), "
            f"{size / 1e6:.0f} MB")
    if row["covered"] and os.path.isdir(row["listen_dir"]):
        listen_wavs = [p for p in glob.glob(os.path.join(row["listen_dir"], "**", "*.wav"),
                                            recursive=True) if _inside(p, row["listen_dir"])]
        size = _size(listen_wavs)
        for p in listen_wavs:
            os.remove(p)
        # The stitch folders hold nothing but silences and a concat list.
        for work in glob.glob(os.path.join(row["listen_dir"], "*", "*_work")):
            if _inside(work, row["listen_dir"]):
                shutil.rmtree(work, ignore_errors=True)
        removed += len(listen_wavs)
        freed += size
        if listen_wavs:
            log(f"{row['book']} / {row['seiyuu']} / listening tests: {len(listen_wavs)} file(s), "
                f"{size / 1e6:.0f} MB")
    return removed, freed
