"""
repair.py
---------
The engine behind the Chapter Repair window. No Tk in here.

## The problem

A finished chapter occasionally contains a spot where the TTS hallucinated
- a sentence read half way, words that are not in the book, an ad-libbed
noise. You find these by listening, which for a long book means finding
them after publication.

Re-rendering the chapter costs hours of GPU time to fix ten seconds of
audio. This replaces just those ten seconds.

## Why it is tractable

Four properties of what the generator already writes, all verified against
the published library rather than assumed:

1. A chapter is `silence, chunk, silence, chunk, ...`. The gaps between
   chunks are pure silence at the configured durations (measured on
   after-dark/chapter_001: 112 gaps of exactly 1.000 s, 4 of 1.300 s).
   So a cut at a chunk boundary never lands mid-word.
2. `sync.json` names every boundary to the millisecond, and the file ends
   exactly at the last chunk's `end` (2740.98 vs a duration of
   2740.980000). The timeline is fully described.
3. The `.srt` is a mirror of `sync.json` - 117 cues for 117 chunks, with
   ZERO cues whose timestamps differ. Retiming subtitles is the same
   arithmetic as retiming chunks.
4. **The text does not change.** A hallucination is wrong audio for
   correct text, so `sync.json`'s text, the translations and the English
   cues all stay valid. Only audio is replaced and numbers move.

Everything after a repaired chunk therefore shifts by ONE number: the new
audio's duration minus the old one's. Several chunks can be repaired in a
single pass, and a time then carries the running sum of every repair
before it - see `build_time_map()`. A batch is one splice, one encode and
one backup however many chunks it fixes, so a chapter is never left
partly repaired between two applies, and a scan taken before the batch
stays valid right through it.

## Two rules that are not negotiable

**The `.srt` is edited in place, never re-emitted.** The published folders
contain no `.translation.json` (those stay in `AUDIOBOOK_OUTPUT`), so
there is nothing to re-emit from; and an existing `.srt` may carry hand
corrections. Times are mapped, the text is untouched. Mapping by TIME
rather than by cue index also survives the two sputnik chapters where the
cue count and the chunk count disagree.

**Nothing is overwritten without a backup**, and the master is repaired
alongside the `.m4a` so the two never disagree.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# The generator, one folder up: `text_pipeline` recovers the TTS text from
# a chunk's reader-facing text, and it must be the SAME code the chapter
# was built with or a regenerated chunk would be spoken from different
# input than its neighbours.
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
if GENERATOR_DIR not in sys.path:
    sys.path.insert(0, GENERATOR_DIR)
import text_pipeline  # noqa: E402

# The audition tool, beside this one: its TTS worker plumbing is reused
# rather than copied. This is a one-way dependency on two functions
# (write_job_file / run_worker) and nothing else - the two tools stay
# separate because they belong to different phases, pre-generation and
# post-publication.
AUDITION_DIR = os.path.join(GENERATOR_DIR, "seiyuu-audition")
if AUDITION_DIR not in sys.path:
    sys.path.insert(0, AUDITION_DIR)
import audition  # noqa: E402

DEFAULT_SETTINGS = {
    "library_root": "F:\\AUDIOBOOK-HOST-AAC",
    "master_root": "F:\\AUDIOBOOK-HOST-MASTER",
    "backup_root": "F:\\_backup\\chapter-repair",
    "work_root": "F:\\tmp\\chapter-repair",
    "irodori_root": "C:\\Irodori-TTS",
    "batch_script": "",
    "whisper_exe": "C:\\Transcribe\\.venv\\Scripts\\whisper.exe",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ja",
    "device": "cuda",
    # Must match what the library was encoded at, or a repaired chapter
    # would not match its neighbours. The generator's default is 64k.
    "aac_bitrate": "64k",
    "speaker_path": "",
    "duration_scale": 1.2,
    "no_trim_tail": False,
    # A chunk is worth listening to when the transcript diverges this far
    # from the text it should have read.
    "similarity_threshold": 0.72,
}


def merge_setting_defaults(data):
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (data or {}).items() if k in DEFAULT_SETTINGS})
    return merged


def load_settings():
    if not os.path.isfile(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return merge_setting_defaults(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merge_setting_defaults(data), f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ library

def list_books(library_root):
    try:
        return sorted(name for name in os.listdir(library_root)
                      if os.path.isdir(os.path.join(library_root, name)))
    except OSError:
        return []


CHAPTER_RE = re.compile(r"^(chapter_\d+)\.m4a$", re.I)


def list_chapters(library_root, book):
    """Base names (`chapter_001`) of the chapters in a book, in order."""
    folder = os.path.join(library_root, book)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = [m.group(1) for m in (CHAPTER_RE.match(n) for n in names) if m]
    return sorted(found)


class Chapter:
    """Every path and file that makes up one published chapter."""

    def __init__(self, settings, book, base):
        self.settings = settings
        self.book = book
        self.base = base
        folder = os.path.join(settings["library_root"], book)
        self.folder = folder
        self.m4a = os.path.join(folder, base + ".m4a")
        self.sync_path = os.path.join(folder, base + ".sync.json")
        self.srt_path = os.path.join(folder, base + ".srt")
        self.master = os.path.join(settings["master_root"], book, base + ".flac")
        self.work_dir = os.path.join(settings["work_root"], book, base)

    @property
    def name(self):
        return f"{self.book}/{self.base}"

    def has_master(self):
        return os.path.isfile(self.master)

    def audio_source(self):
        """What to cut from and listen to: the FLAC master when there is
        one, otherwise the published .m4a. Preferring the master keeps a
        repair one encode generation from today's audio no matter how many
        times a chapter is repaired."""
        return self.master if self.has_master() else self.m4a

    def load_sync(self):
        with open(self.sync_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def chunks(self):
        return self.load_sync()["chunks"]


# --------------------------------------------------------------------- srt

CUE_RE = re.compile(
    r"(?P<number>\d+)\s*\r?\n"
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
    r"\s*\r?\n(?P<text>.*?)(?=\r?\n\r?\n|\Z)",
    re.S)


def srt_time_to_seconds(stamp):
    hours, minutes, rest = stamp.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def seconds_to_srt_time(seconds):
    """Identical to translate_pipeline.format_timestamp - the same bytes
    have to come out, since this rewrites files that tool wrote."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def read_srt(path):
    """[{number, start, end, text}] - text kept verbatim, including any
    hand corrections, because this tool only ever moves the times."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    cues = []
    for match in CUE_RE.finditer(raw):
        cues.append({
            "number": int(match.group("number")),
            "start": srt_time_to_seconds(match.group("start")),
            "end": srt_time_to_seconds(match.group("end")),
            "text": match.group("text").rstrip("\r\n"),
        })
    return cues


def write_srt(path, cues):
    """UTF-8 with no BOM and LF endings - the player's parser expects
    exactly that, and a BOM would be glued to the first cue number."""
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue['number']}\n"
            f"{seconds_to_srt_time(cue['start'])} --> "
            f"{seconds_to_srt_time(cue['end'])}\n"
            f"{cue['text']}\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(blocks))


def build_time_map(edits):
    """Maps any time on the old timeline onto the repaired one, for ANY
    number of repairs in one pass.

    `edits` must be ascending by start and non-overlapping - which they
    are by construction, since each names a distinct chunk and chunks do
    not overlap.

    A time before every edit does not move. A time after an edit carries
    that edit's delta, so a time past several edits carries the running
    sum. A time INSIDE an edit is scaled within it, which in practice only
    ever affects the repaired chunk's own cue end.

    Working in TIME rather than in cue indices is what makes this safe on
    the two sputnik chapters whose cue count does not match their chunk
    count."""
    def mapped(t):
        shift = 0.0
        for edit in edits:
            old_duration = edit["end"] - edit["start"]
            if t >= edit["end"]:
                shift += edit["new_duration"] - old_duration
            elif t > edit["start"]:
                if old_duration <= 0:
                    return edit["start"] + shift
                return (edit["start"] + shift
                        + (t - edit["start"]) * (edit["new_duration"] / old_duration))
            else:
                break
        return t + shift

    return mapped


def time_mapper(old_start, old_end, new_duration):
    """The single-repair form, kept because it reads better at one call
    site and because the sandbox test pins it."""
    edit = {"start": old_start, "end": old_end, "new_duration": new_duration}
    return build_time_map([edit]), new_duration - (old_end - old_start)


# ----------------------------------------------------------------- ffmpeg

def run(cmd):
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(" ".join(cmd[:3]) + " failed: "
                           + result.stderr.decode("utf-8", "replace").strip()[-400:])
    return result


def audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def audio_format(path):
    fmt = {"sample_rate": 48000, "channels": 1}
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels", "-of", "default=nw=1", path],
        capture_output=True, text=True)
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in fmt and value.strip().isdigit():
            fmt[key.strip()] = int(value.strip())
    return fmt


def extract_segment(source, start, end, out_wav, pad=0.0):
    """Cuts [start, end] out of a chapter into a wav, for listening or for
    splicing. `pad` widens the window on both sides, which is what you
    want when auditioning a suspect chunk - hearing it run into its
    neighbours is how you tell a hallucination from a hard cut."""
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    begin = max(0.0, start - pad)
    run(["ffmpeg", "-v", "error", "-y", "-i", source,
         "-ss", f"{begin:.6f}", "-to", f"{end + pad:.6f}",
         "-c:a", "pcm_s16le", out_wav])
    return out_wav


def render_silence(path, audio_format, seconds):
    """A silence wav matching the chapter's own format, for joining the
    pieces of a split chunk. Matched exactly because the concat demuxer
    resamples nothing - a mismatch here corrupts the join."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rate = audio_format["sample_rate"]
    channels = audio_format["channels"]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r={rate}:cl={layout}", "-t", f"{float(seconds):.6f}",
         "-ar", str(rate), "-ac", str(channels), "-c:a", "pcm_s16le", path])
    return path


def splice_many(source, edits, out_path, work_dir):
    """Replaces several chunks in ONE pass:

        [0, s1) + new1 + [e1, s2) + new2 + ... + [eN, end]

    One concat and one output whatever the number of repairs, so a session
    that fixes five chunks encodes the chapter once instead of five times
    and can never leave it half repaired.

    `edits` must be ascending by start and non-overlapping. `-ss`/`-to` are
    given AFTER `-i` so ffmpeg decodes and cuts on an exact sample rather
    than seeking to the nearest keyframe."""
    os.makedirs(work_dir, exist_ok=True)
    fmt = audio_format(source)
    parts = []
    cursor = 0.0

    for position, edit in enumerate(edits):
        # The stretch of original audio before this repair. It is skipped
        # only if two repairs somehow abut exactly - chunks are always at
        # least a silence apart, so in practice this always has content.
        if edit["start"] - cursor > 0.0005:
            keep = os.path.join(work_dir, f"_keep_{position:02d}.wav")
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", source]
            if cursor > 0:
                cmd += ["-ss", f"{cursor:.6f}"]
            cmd += ["-to", f"{edit['start']:.6f}", "-c:a", "pcm_s16le", keep]
            run(cmd)
            parts.append(keep)

        # Each replacement is normalised to the chapter's own rate and
        # channel count - the concat demuxer resamples nothing, so a
        # mismatch here is the classic way to corrupt a stitch.
        body = os.path.join(work_dir, f"_body_{position:02d}.wav")
        run(["ffmpeg", "-v", "error", "-y", "-i", edit["wav"],
             "-ar", str(fmt["sample_rate"]), "-ac", str(fmt["channels"]),
             "-c:a", "pcm_s16le", body])
        parts.append(body)
        cursor = edit["end"]

    tail = os.path.join(work_dir, "_tail.wav")
    run(["ffmpeg", "-v", "error", "-y", "-i", source, "-ss", f"{cursor:.6f}",
         "-c:a", "pcm_s16le", tail])
    parts.append(tail)

    listing = os.path.join(work_dir, "_splice.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for part in parts:
            f.write(f"file '{os.path.abspath(part)}'\n")

    codec = "flac" if out_path.lower().endswith(".flac") else "pcm_s16le"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", listing, "-c:a", codec]
    if codec == "flac":
        cmd += ["-sample_fmt", "s16", "-compression_level", "8"]
    run(cmd + [out_path])
    return out_path


def splice(source, start, end, replacement_wav, out_path, work_dir):
    """The one-chunk form."""
    return splice_many(source, [{"start": start, "end": end,
                                 "wav": replacement_wav}],
                       out_path, work_dir)


def encode_m4a(source, out_path, bitrate):
    """The generator's own stitch settings, including faststart - the
    player streams these from R2 with Range requests, and a repaired
    chapter that loses `moov` in front stops playing until it is fully
    downloaded."""
    run(["ffmpeg", "-v", "error", "-y", "-i", source,
         "-c:a", "aac", "-b:a", str(bitrate), "-ac", "1",
         "-movflags", "+faststart", out_path])
    return out_path


def atom_order(path):
    """Top-level MP4 atoms in order. `ftyp, moov, ..., mdat` means
    faststart survived; moov after mdat means it did not."""
    atoms = []
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                name = header[4:8].decode("ascii", "replace")
                atoms.append(name)
                if size == 1:                      # 64-bit extended size
                    size = int.from_bytes(f.read(8), "big")
                    f.seek(size - 16, 1)
                elif size == 0:
                    break
                else:
                    f.seek(size - 8, 1)
    except OSError:
        return []
    return atoms


def copy_tags(from_m4a, to_m4a):
    """Carries the chapter's existing MP4 tags - title, artist, album,
    cover - onto the repaired file.

    Copied from the OLD FILE rather than rebuilt from the generator's
    settings.json on purpose: those settings now describe whatever book
    was last generated, not this one. A chapter with no cover is handled
    by the player; one with no title is a regression."""
    from mutagen.mp4 import MP4
    source = MP4(from_m4a)
    target = MP4(to_m4a)
    copied = []
    for key, value in (source.tags or {}).items():
        target[key] = value
        copied.append(key)
    target.save()
    return copied


# ------------------------------------------------------------------- QA

PUNCT_RE = re.compile(r"[\s\u3000。、？！…・「」『』（）()\.,!?\-\u2500~〜:：;；\"']+")


def normalise_for_compare(text):
    """Punctuation and spacing are exactly what an ASR transcript will not
    agree with, so neither side keeps any."""
    return PUNCT_RE.sub("", text or "")


def similarity(expected, heard):
    a = normalise_for_compare(expected)
    b = normalise_for_compare(heard)
    if not a and not b:
        return 1.0, 1.0
    if not a:
        return 0.0, float("inf")
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio, len(b) / len(a)


def whisper_command(settings, audio_path, out_dir):
    """One transcription for the WHOLE chapter, with timestamps.

    Per chunk would mean reloading Whisper a hundred-odd times for one
    chapter; the json output carries segment start/end on the chapter's
    own timeline, which is all the alignment this needs."""
    return [
        settings["whisper_exe"], audio_path,
        "--model", settings["whisper_model"],
        "--output_dir", out_dir,
        "--output_format", "json",
        "--task", "transcribe",
        "--language", settings["whisper_language"],
        "--device", settings["device"],
    ]


def run_streaming(cmd, on_line, cwd=None, on_proc=None):
    """Unbuffered, line by line. A chapter takes minutes to transcribe and
    output sitting in a pipe buffer turns a live log into a frozen one.

    PYTHONIOENCODING is the documented trap: a child Python writing
    Japanese to a PIPE otherwise falls back to the Windows locale encoding
    and dies on the first character, and Whisper catches that per file and
    quietly transcribes nothing."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", env=env)
    if on_proc:
        on_proc(proc)
    for line in proc.stdout:
        on_line(line.rstrip())
    return proc.wait()


def transcribe_chapter(chapter, log, on_proc=None):
    """Returns Whisper's segments for the chapter, transcribing only if
    there is no usable transcript already - a QA pass over a whole book
    should not redo minutes of GPU work every time the window opens."""
    os.makedirs(chapter.work_dir, exist_ok=True)
    source = chapter.audio_source()
    produced = os.path.join(chapter.work_dir,
                            os.path.splitext(os.path.basename(source))[0] + ".json")
    if os.path.isfile(produced):
        log(f"  using the transcript already in {chapter.work_dir}")
    else:
        log(f"  transcribing {os.path.basename(source)} "
            f"({chapter.settings['whisper_model']} on "
            f"{chapter.settings['device']}) - minutes, not seconds")
        code = run_streaming(
            whisper_command(chapter.settings, source, chapter.work_dir),
            lambda line: log("   " + line) if line.strip() else None,
            on_proc=on_proc)
        if code != 0:
            raise RuntimeError(f"Whisper exited with code {code}")
    with open(produced, "r", encoding="utf-8") as f:
        return json.load(f).get("segments", [])


def score_chapter(chapter, segments):
    """Lines up the transcript against sync.json and scores every chunk.

    A segment is credited to the chunk its MIDPOINT falls in. Chunks are
    separated by a second or more of silence, which is where Whisper
    prefers to break anyway, so this lands cleanly in practice.

    The result is a RANKING, not a verdict. Whisper mishears - the
    onboarder's own notes record it inventing text that was never spoken -
    so a low score means "worth listening to", never "broken"."""
    chunks = chapter.chunks()
    heard = {index: [] for index in range(len(chunks))}
    for segment in segments:
        middle = (float(segment.get("start", 0)) + float(segment.get("end", 0))) / 2
        for index, chunk in enumerate(chunks):
            if chunk["start"] <= middle < chunk["end"]:
                heard[index].append((segment.get("text") or "").strip())
                break

    rows = []
    for index, chunk in enumerate(chunks):
        text = chunk.get("text", "")
        transcript = "".join(heard[index])
        ratio, length_ratio = similarity(text, transcript)
        rows.append({
            "index": index,
            "start": chunk["start"],
            "end": chunk["end"],
            "duration": round(chunk["end"] - chunk["start"], 3),
            "text": text,
            "heard": transcript,
            "similarity": round(ratio, 4),
            "length_ratio": round(length_ratio, 3)
                            if length_ratio != float("inf") else None,
        })
    return rows


def suspicious(rows, threshold):
    """Worst first. Two separate signals, because they catch different
    failures: a low similarity catches invented or wrong words, and a
    length ratio far from 1 catches a sentence abandoned half way (short)
    or an ad-lib tacked on (long)."""
    flagged = []
    for row in rows:
        length_ratio = row["length_ratio"]
        odd_length = length_ratio is None or not (0.65 <= length_ratio <= 1.45)
        if row["similarity"] < threshold or odd_length:
            flagged.append(row)
    return sorted(flagged, key=lambda r: r["similarity"])


# ------------------------------------------------- the 30-second ceiling

# irodori_tts clamps the duration predictor to SamplingRequest.max_seconds,
# which defaults to 30.0 (inference_runtime.py:217, clamp at ~1314). Our
# stack never set it, so every chapter ever rendered used 30 s.
#
# A chunk whose text needs longer is NOT truncated - it is crammed into the
# 30 s it was given, and the middle comes out garbled. 68 chunks in the
# published library sit at exactly 30.00 s.
#
# This is the cheapest defect detector there is: it needs sync.json and
# nothing else. No Whisper, no GPU, no decoding - a whole library in about
# a second.
DURATION_CAP = 30.0
CAP_TOLERANCE = 0.02

# Median across 8,460 published chunks, used to estimate how long a piece
# of text actually needs. Chunks that hit the ceiling ran at 6.0-6.9.
CHARS_PER_SECOND = 5.0


def needed_seconds(text, pace=CHARS_PER_SECOND):
    return len(text or "") / (pace or CHARS_PER_SECOND)


def suggested_max_seconds(text, pace=CHARS_PER_SECOND):
    """What to give a chunk that hit the ceiling: the time its own book
    would take over that text, plus a quarter. Floored at the engine
    default, and capped at 90 s so a runaway estimate cannot ask an 8 GB
    card for a canvas it has no room for."""
    return max(DURATION_CAP, min(90.0, round(needed_seconds(text, pace) * 1.25)))


def chapter_pace(rows):
    """The chapter's own characters-per-second, from its UNCAPPED chunks.

    Measuring per chapter rather than using one library constant matters:
    pace is a per-book setting. wall renders at duration_scale 1.7 and
    reads at about 3.5 ch/s, yojo-senki at nearer 5. A capped chunk has to
    be judged against its own book, not against the library."""
    paces = sorted(row["chars_per_sec"] for row in rows
                   if not row["capped"] and row["duration"] > 2
                   and len(row["text"]) > 20)
    if not paces:
        return CHARS_PER_SECOND
    middle = len(paces) // 2
    if len(paces) % 2:
        return paces[middle]
    return (paces[middle - 1] + paces[middle]) / 2


def duration_rows(chapter):
    """Every chunk, with the numbers that show whether it hit the ceiling
    AND whether hitting it did any harm.

    The same shape `score_chapter` returns, so the window can list either
    kind of result. `similarity` is None because nothing was transcribed -
    that is the point of this pass."""
    rows = []
    for chunk in chapter.chunks():
        text = chunk.get("text", "")
        duration = round(chunk["end"] - chunk["start"], 3)
        rows.append({
            "index": chunk["index"],
            "start": chunk["start"],
            "end": chunk["end"],
            "duration": duration,
            "text": text,
            "heard": "",
            "similarity": None,
            "length_ratio": None,
            "capped": duration >= DURATION_CAP - CAP_TOLERANCE,
            "chars_per_sec": round(len(text) / duration, 2) if duration else 0.0,
        })

    # How much faster than its own chapter each capped chunk had to speak.
    # Landing on the ceiling is not itself damage: a chunk can hit it
    # because the book's duration_scale asked for more than 30 s while
    # still reading at a normal rate. What garbles the audio is being
    # made to speak FASTER than the book does, so that is what ranks them.
    pace = chapter_pace(rows)
    for row in rows:
        row["pace"] = round(pace, 2)
        row["pace_ratio"] = round(row["chars_per_sec"] / pace, 2) if pace else 1.0
        row["needs"] = round(len(row["text"]) / pace, 1) if pace else 0.0
    return rows


def capped_rows(rows, min_pace_ratio=1.0):
    """Capped chunks, worst first - most over their chapter's own pace.

    `min_pace_ratio` of 1.0 keeps every capped chunk but puts the ones
    reading at or below the book's normal rate at the bottom, where they
    belong: those are fine to listen to and probably fine to leave."""
    hits = [row for row in rows if row.get("capped")]
    return sorted(hits, key=lambda row: -row.get("pace_ratio", 0))


def survey_library(settings, log):
    """Every capped chunk in every book, straight from sync.json.

    Answers 'how bad is this across the library' before a single second of
    GPU time is spent on any of it."""
    total = capped = 0
    per_chapter = []
    for book in list_books(settings["library_root"]):
        for base in list_chapters(settings["library_root"], book):
            chapter = Chapter(settings, book, base)
            try:
                rows = duration_rows(chapter)
            except (OSError, ValueError, KeyError):
                log(f"  ! {book}/{base}: no readable sync.json")
                continue
            total += len(rows)
            hits = capped_rows(rows)
            capped += len(hits)
            if hits:
                per_chapter.append((book, base, hits))
    rushed = 0
    for book, base, hits in per_chapter:
        worst = hits[0]
        over = [h for h in hits if h["pace_ratio"] >= 1.15]
        rushed += len(over)
        log(f"  {book}/{base}: {len(hits)} capped ({len(over)} rushed), "
            f"worst #{worst['index']} at {len(worst['text'])} chars, "
            f"{worst['chars_per_sec']} ch/s vs the chapter's "
            f"{worst['pace']} ({worst['pace_ratio']}x)")
    log(f"{capped} capped chunk(s) of {total} across "
        f"{len(per_chapter)} chapter(s); {rushed} of them read 15% or more "
        f"above their own chapter's pace and are the ones worth hearing")
    return per_chapter, total, capped, rushed


def qa_cache_path(chapter):
    return os.path.join(chapter.work_dir, chapter.base + ".qa.json")


def save_qa(chapter, rows):
    os.makedirs(chapter.work_dir, exist_ok=True)
    with open(qa_cache_path(chapter), "w", encoding="utf-8") as f:
        json.dump({"chapter": chapter.name, "scanned": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "rows": rows}, f, ensure_ascii=False, indent=2)


def load_qa(chapter):
    try:
        with open(qa_cache_path(chapter), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ regeneration

def tts_text_for(chunk_text):
    """What the engine was actually given for this chunk. `sync.json`
    holds the reader-facing wording; the TTS saw it with brackets
    converted and dash runs turned into a comma."""
    return text_pipeline.prepare_tts_text(chunk_text)


# Where a chunk may be cut when it is too long for one request. The
# generator's own terminators are `。？……`; `！` and `!` are added here
# because they are what makes these chunks unsplittable in the first place
# (the yojo-senki passage runs 156 characters before its first `。`).
# Nothing is inserted or removed - the cut is zero-width, so the pieces
# rejoin byte-identically.
SPLIT_AFTER = "！!。？…"
_SPLIT_RE = re.compile("(?<=[" + SPLIT_AFTER + "])(?![" + SPLIT_AFTER + "])")


def piece_limit_for(chapter, rows=None):
    """How long each piece should be: the chapter's own typical chunk.

    Taking it from the book rather than from a constant means the pieces
    have the same texture as their neighbours - which is the whole point,
    since the repair has to sound like the rest of the chapter."""
    rows = rows or duration_rows(chapter)
    lengths = sorted(len(row["text"]) for row in rows
                     if not row["capped"] and len(row["text"]) > 10)
    if not lengths:
        return 55
    median = lengths[len(lengths) // 2]
    return max(30, min(80, median))


def split_for_repair(text, limit=55):
    """Cuts an over-long chunk into pieces the model can actually read.

    Cuts only after a terminator, never mid-sentence, and merges fragments
    up to `limit` so the pieces resemble ordinary chunks. The join is
    zero-width: `"".join(split_for_repair(t)) == t` always.

    This is not a workaround. Irodori was trained with
    `max_latent_steps: 750`, which is exactly the 30 s its inference
    default allows, so a 40-second request is outside anything it has
    seen - raising the ceiling produces different gibberish, not better
    audio (measured 2026-09-14). Splitting puts every request back inside
    the window, which is what the generator does for every other chunk in
    the library."""
    text = text or ""
    fragments = [f for f in _SPLIT_RE.split(text) if f]
    pieces, current = [], ""
    for fragment in fragments:
        if current and len(current) + len(fragment) > limit:
            pieces.append(current)
            current = fragment
        else:
            current += fragment
    if current:
        pieces.append(current)
    return pieces or ([text] if text else [])


def generate_split_candidate(chapter, pieces, spec, out_wav, gap, log,
                             on_proc=None):
    """Renders each piece as its own request, then joins them into ONE wav.

    One worker run for the whole set, so it costs a single model load. The
    piece wavs are kept beside the result: each was an independent request,
    so a single bad piece can be re-rolled without disturbing the others.

    `gap` is the silence inserted between pieces. Zero butt-joins them,
    which can be right since each piece already carries its own trailing
    silence; a quarter-second reads as a breath. That is an ear decision."""
    work = os.path.splitext(out_wav)[0] + "_pieces"
    os.makedirs(work, exist_ok=True)

    jobs = []
    for index, piece in enumerate(pieces, start=1):
        jobs.append({"index": index, "text": tts_text_for(piece),
                     "output_wav": os.path.join(work, f"piece_{index:02d}.wav")})
    jobs_path = os.path.join(work, "jobs.json")
    # Deliberately no max_seconds: every piece is inside the trained
    # window already, which is the entire point of splitting.
    audition.write_job_file(
        jobs_path,
        {"speaker_path": spec["speaker_path"],
         "duration_scale": spec["duration_scale"],
         "no_trim_tail": spec["no_trim_tail"],
         "seed_enabled": spec.get("seed_enabled", False),
         "seed_value": spec.get("seed_value", 0)},
        jobs, chapter.settings["irodori_root"])

    total = len(jobs)

    def relay(line):
        if line.startswith("MODEL_LOADED "):
            log(f"  model loaded in {line.split()[1]}s - {total} piece(s) "
                f"from one load")
        elif line.startswith("CHUNK_START "):
            i = int(line.split()[1])
            log(f"  piece {i}/{total} ({len(pieces[i - 1])} chars)")
        elif line.startswith("CHUNK_FAIL "):
            log("  ! " + line.split(" ", 2)[-1])
        elif line and not line.startswith(("CHUNK_DONE", "BATCH_DONE",
                                           "WATERMARK", "MAX_SECONDS")):
            log("  " + line)

    code = audition.run_worker(jobs_path, chapter.settings, relay, on_proc)
    missing = [job["index"] for job in jobs
               if not os.path.isfile(job["output_wav"])]
    if code != 0 or missing:
        raise RuntimeError(f"the TTS worker exited with code {code}"
                           + (f"; piece(s) {missing} produced no audio"
                              if missing else ""))

    parts = [job["output_wav"] for job in jobs]
    fmt = audio_format(parts[0])
    joiner = None
    if gap and gap > 0:
        joiner = render_silence(os.path.join(work, "_gap.wav"), fmt, gap)

    listing = os.path.join(work, "join.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for position, part in enumerate(parts):
            if position and joiner:
                f.write(f"file '{os.path.abspath(joiner)}'\n")
            f.write(f"file '{os.path.abspath(part)}'\n")
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", listing, "-c:a", "pcm_s16le",
         "-ar", str(fmt["sample_rate"]), "-ac", str(fmt["channels"]), out_wav])

    spoken = sum(audio_duration(part) for part in parts)
    log(f"  joined {total} piece(s): {spoken:.2f}s of speech"
        + (f" + {gap}s x {total - 1} between" if joiner else " butt-joined")
        + f" = {audio_duration(out_wav):.2f}s")
    return out_wav


def generate_candidate(chapter, chunk_text, spec, out_wav, log, on_proc=None):
    """One fresh take of one chunk, through the audition tool's worker.

    Same engine, same checkpoint and the same `resolve_cfg_scales` path
    the chapter itself was rendered with - a replacement produced any
    other way would not sit alongside its neighbours."""
    work = os.path.dirname(out_wav)
    os.makedirs(work, exist_ok=True)
    jobs_path = os.path.join(work, "jobs.json")
    audition.write_job_file(
        jobs_path,
        {"speaker_path": spec["speaker_path"],
         "duration_scale": spec["duration_scale"],
         "no_trim_tail": spec["no_trim_tail"],
         "seed_enabled": spec.get("seed_enabled", False),
         "seed_value": spec.get("seed_value", 0),
         # Absent or 0 leaves the engine's 30 s ceiling alone.
         "max_seconds": spec.get("max_seconds")},
        [{"index": 1, "text": tts_text_for(chunk_text), "output_wav": out_wav}],
        chapter.settings["irodori_root"])

    def relay(line):
        if line.startswith("MODEL_LOADED "):
            log(f"  model loaded in {line.split()[1]}s")
        elif line.startswith("MAX_SECONDS "):
            log(f"  ceiling raised to {line.split()[1]}s "
                f"(engine default is {DURATION_CAP:.0f}s)")
        elif line.startswith("CHUNK_FAIL "):
            log("  ! " + line.split(" ", 2)[-1])
        elif line and not line.startswith(("CHUNK_START", "CHUNK_DONE",
                                           "BATCH_DONE", "WATERMARK")):
            log("  " + line)

    code = audition.run_worker(jobs_path, chapter.settings, relay, on_proc)
    if code != 0 or not os.path.isfile(out_wav):
        raise RuntimeError(f"the TTS worker exited with code {code}")
    return out_wav


# ------------------------------------------------------------------ repair

def backup_chapter(chapter):
    """Copies everything this repair can touch into a timestamped folder
    and returns it. Runs BEFORE anything is written."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(chapter.settings["backup_root"], chapter.book,
                          f"{chapter.base}-{stamp}")
    os.makedirs(folder, exist_ok=True)
    for path in (chapter.m4a, chapter.sync_path, chapter.srt_path,
                 chapter.master):
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
    return folder


def plan_repairs(chapter, selections):
    """What a batch of repairs would do, worked out before anything is
    written, so the window can show it and the person can decline.

    `selections` is [{"index": int, "wav": path}] in any order; the plan
    sorts them, which is also the order they must be spliced in."""
    chunks = chapter.chunks()
    seen = set()
    edits = []
    for selection in sorted(selections, key=lambda s: s["index"]):
        index = selection["index"]
        if index in seen:
            raise ValueError(f"chunk {index} is queued twice")
        seen.add(index)
        chunk = chunks[index]
        old_duration = chunk["end"] - chunk["start"]
        new_duration = audio_duration(selection["wav"])
        edits.append({
            "index": index,
            "start": chunk["start"],
            "end": chunk["end"],
            "wav": selection["wav"],
            "old_duration": round(old_duration, 3),
            "new_duration": round(new_duration, 3),
            "delta": round(new_duration - old_duration, 3),
        })
    if not edits:
        raise ValueError("nothing queued")

    total_delta = sum(edit["new_duration"] - edit["end"] + edit["start"]
                      for edit in edits)
    cues = read_srt(chapter.srt_path)
    first = edits[0]["end"]
    return {
        "edits": edits,
        "count": len(edits),
        "indices": [edit["index"] for edit in edits],
        "delta": round(total_delta, 3),
        # Every chunk after the FIRST repair moves, whether or not it is
        # itself repaired.
        "chunks_after": sum(1 for chunk in chunks if chunk["start"] >= first),
        "cues": len(cues),
        "cues_shifted": sum(1 for cue in cues if cue["start"] >= first),
        "source": chapter.audio_source(),
        "from_master": chapter.has_master(),
        "old_total": round(chunks[-1]["end"], 3),
        "new_total": round(chunks[-1]["end"] + total_delta, 3),
    }


def plan_repair(chapter, index, replacement_wav):
    """The one-chunk form, flattened so a caller showing a single repair
    does not have to reach into `edits`."""
    plan = plan_repairs(chapter, [{"index": index, "wav": replacement_wav}])
    edit = plan["edits"][0]
    plan.update({k: edit[k] for k in ("index", "start", "end", "old_duration",
                                      "new_duration")})
    return plan


def apply_repair(chapter, index, replacement_wav, log, plan=None):
    """The one-chunk form."""
    return apply_repairs(chapter, [{"index": index, "wav": replacement_wav}],
                         log, plan)


def apply_repairs(chapter, selections, log, plan=None):
    """Replaces one or more chunks' audio and moves every number after the
    first of them.

    Order matters: back up, splice audio, re-encode, verify faststart,
    carry the tags over, and only then rewrite sync.json and the .srt. If
    anything raises before the last step the chapter on disk is still
    internally consistent, and the backup covers the rest.

    A batch is ONE splice, ONE encode and ONE backup however many chunks
    it fixes, so the chapter is never left partly repaired between two
    applies."""
    plan = plan or plan_repairs(chapter, selections)
    edits = plan["edits"]
    mapped = build_time_map(edits)
    by_index = {edit["index"]: edit for edit in edits}

    folder = backup_chapter(chapter)
    log(f"  backed up to {folder}")

    work = os.path.join(chapter.work_dir, "_apply")
    os.makedirs(work, exist_ok=True)
    source = chapter.audio_source()

    # 1. Audio. The master is repaired too when there is one, so the two
    #    never drift apart, and the .m4a is encoded FROM the master.
    summary = ", ".join(f"#{edit['index']} {edit['old_duration']}s→"
                        f"{edit['new_duration']}s" for edit in edits)
    if chapter.has_master():
        new_master = os.path.join(work, "repaired.flac")
        splice_many(source, edits, new_master, work)
        log(f"  spliced the master: {summary}")
        encode_source = new_master
    else:
        new_wav = os.path.join(work, "repaired.wav")
        splice_many(source, edits, new_wav, work)
        log(f"  spliced (no master - this is a second AAC generation): "
            f"{summary}")
        encode_source = new_wav
        new_master = None

    new_m4a = os.path.join(work, "repaired.m4a")
    encode_m4a(encode_source, new_m4a, chapter.settings["aac_bitrate"])
    atoms = atom_order(new_m4a)
    if "moov" not in atoms or "mdat" not in atoms or \
            atoms.index("moov") > atoms.index("mdat"):
        raise RuntimeError(f"faststart was lost - atom order is {atoms}. "
                           f"Refusing to publish a file the player cannot "
                           f"stream.")
    copied = copy_tags(chapter.m4a, new_m4a)
    log(f"  re-encoded, faststart intact ({', '.join(atoms[:3])}), "
        f"{len(copied)} tag(s) carried over")

    # 2. Put the audio in place.
    shutil.move(new_m4a, chapter.m4a)
    if new_master:
        shutil.move(new_master, chapter.master)

    # 3. sync.json: a repaired chunk keeps its (shifted) start and takes
    #    the new duration; everything else carries the running shift of
    #    every repair before it. Walking once in order is the same
    #    arithmetic build_time_map() does, which is why the .srt below
    #    cannot disagree with this.
    data = chapter.load_sync()
    shift = 0.0
    for position, entry in enumerate(data["chunks"]):
        start = entry["start"] + shift
        edit = by_index.get(position)
        if edit:
            entry["start"] = round(start, 3)
            entry["end"] = round(start + edit["new_duration"], 3)
            shift += edit["new_duration"] - (edit["end"] - edit["start"])
        else:
            entry["start"] = round(start, 3)
            entry["end"] = round(entry["end"] + shift, 3)
    with open(chapter.sync_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  sync.json: {plan['count']} chunk(s) re-timed "
        f"({', '.join('#' + str(i) for i in plan['indices'])}), "
        f"{plan['chunks_after']} later chunk(s) shifted, "
        f"{plan['delta']:+.3f}s overall")

    # 4. The .srt, by time and never by index, text untouched.
    cues = read_srt(chapter.srt_path)
    if cues:
        for cue in cues:
            cue["start"] = round(mapped(cue["start"]), 3)
            cue["end"] = round(mapped(cue["end"]), 3)
        write_srt(chapter.srt_path, cues)
        log(f"  .srt: {len(cues)} cue(s) re-timed, text untouched")
    else:
        log("  no .srt beside this chapter - nothing to re-time")

    shutil.rmtree(work, ignore_errors=True)
    return {"backup": folder, "delta": plan["delta"],
            "count": plan["count"], "indices": plan["indices"],
            "new_total": plan["new_total"]}
