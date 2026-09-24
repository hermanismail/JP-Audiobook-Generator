"""
qa_scan.py
----------
Listen to a rendered chapter with Whisper and say which Parts are worth a
human ear - at the END OF A RUN, before anything is published (user
decision 2026-09-25), and in dynamic-repair, from the same code.

## Why a second pass exists

The scan transcribes the WHOLE chapter in one go, and `score_chapter()`
credits a segment to the Part its midpoint falls in. Whisper segments the
file its own way, so a segment that starts late or swallows a silence
leaves a Part with truncated or empty text - and the Part is then flagged
although the audio is perfect.

Measured on wall/chapter_008 (2026-09-25), the five Parts the scan
flagged, all judged correct by ear:

    Part   in the chapter pass        alone as a clip
    43     (nothing heard)   0.00     ドゥルーブラック        0.88  ok
    75     多分生まれつき     0.45     何かの加減で、たぶん…   0.90  ok
    162    その誰かが…       0.67     その誰かが、誰だったか   1.00  ok
    189    そして…          0.46     そして涙を流している     1.00  ok
    190    …紙巣の鍋を       0.61     広く静かで紙巣の鍋を     0.46  FLAG

Four of five were artefacts of the chapter-wide pass. A bigger model does
not fix it (large-v3 made 75 worse, at 25x the time); transcribing the
Part ON ITS OWN does. So every flagged Part is re-transcribed as a clip -
all of them in ONE whisper call, 6 clips in 13.6 s - and dropped from the
shortlist if it comes back clean. Both transcripts are kept, so a dropped
Part can still be looked at.

The scoring is book-profiler's: kana folded before comparing, and
`overrun` for a tail spoken after the sentence ended (part 37's `客`,
which nothing else caught).

## What it writes

`<chapter>.qa.json` beside the audio, in dynamic-repair's own cache
format, so opening the chapter there shows the scan that already ran
instead of spending the GPU again:

    {chapter, scanned, flac_stamp, rows: [...], verified: [...],
     flagged: [part], dropped: [part]}
"""

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.join(SCRIPT_DIR, "chapter-repair"),
              os.path.join(SCRIPT_DIR, "book-profiler"),
              os.path.join(SCRIPT_DIR, "seiyuu-audition")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import repair  # noqa: E402  - chapter-repair's Whisper pass and scoring
import score as profiler_score  # noqa: E402  - kana folding and overrun

DEFAULTS = {
    "whisper_exe": r"C:\Transcribe\.venv\Scripts\whisper.exe",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ja",
    "device": "cuda",
    "min_judged_chars": 7,
}


def judge(script, heard):
    """{similarity, length_ratio, overrun, flagged} for one Part, scored
    the profiler's way."""
    settings = profiler_score.SCORE_DEFAULTS
    similarity, ratio = profiler_score.compare(script or "", heard or "")
    over = profiler_score.overrun(script or "", heard or "")
    flagged = (similarity < settings["similarity_threshold"]
               or ratio is None
               or not (settings["length_ratio_low"] <= ratio <= settings["length_ratio_high"])
               or over >= settings["overrun_chars"])
    return {"similarity": round(similarity, 3),
            "length_ratio": round(ratio, 3) if ratio is not None else None,
            "overrun": over, "flagged": bool(flagged)}


class Chapter:
    """The little that repair.py's scan needs: it duck-types
    dynamic-repair's DynChapter, so both tools scan identically."""

    def __init__(self, folder, base, settings, work_dir=None):
        self.settings = dict(DEFAULTS, **(settings or {}))
        self.folder = os.path.abspath(folder)
        self.base = base
        self.name = base
        self.flac = os.path.join(self.folder, base + ".flac")
        self.m4a = os.path.join(self.folder, base + ".m4a")
        self.sync_path = os.path.join(self.folder, base + ".sync.json")
        self.work_dir = work_dir or os.path.join(self.folder, "_qa", base)
        with open(self.sync_path, "r", encoding="utf-8") as f:
            self.sync = json.load(f)

    def audio_source(self):
        # The lossless master when the run kept one, else the published file.
        return self.flac if os.path.isfile(self.flac) else self.m4a

    def chunks(self):
        return self.sync["chunks"]


def stamp(path):
    info = os.stat(path)
    return [info.st_size, int(info.st_mtime)]


def qa_path(folder, base):
    return os.path.join(folder, base + ".qa.json")


def verify(chapter, rows, indexes, log):
    """Re-transcribe the flagged Parts one clip at a time (one whisper
    call for all of them) and score them again.

    Returns {index: {heard_alone, ...judgement}}. Nothing is deleted: a
    Part that comes back clean is recorded as dropped, with both
    transcripts, because the clip pass has no context either and the point
    is to show a person why."""
    if not indexes:
        return {}
    clips_dir = os.path.join(chapter.work_dir, "_clips")
    os.makedirs(clips_dir, exist_ok=True)
    clips = {}
    for index in indexes:
        chunk = chapter.chunks()[index]
        out = os.path.join(clips_dir, f"part_{index + 1:04d}.wav")
        if not os.path.isfile(out):
            repair.extract_segment(chapter.audio_source(), chunk["start"], chunk["end"],
                                   out, pad=0.0)
        clips[index] = out
    out_dir = os.path.join(chapter.work_dir, "_verify")
    os.makedirs(out_dir, exist_ok=True)
    settings = chapter.settings
    log(f"  verifying {len(clips)} flagged part(s), each on its own")
    command = [settings["whisper_exe"], *clips.values(),
               "--model", settings["whisper_model"],
               "--output_dir", out_dir, "--output_format", "json",
               "--task", "transcribe", "--language", settings["whisper_language"],
               "--device", settings["device"]]
    if settings.get("whisper_model_dir"):
        command += ["--model_dir", settings["whisper_model_dir"]]
    subprocess.run(command, capture_output=True)
    out = {}
    for index, wav in clips.items():
        produced = os.path.join(out_dir, os.path.basename(wav)[:-4] + ".json")
        heard = ""
        if os.path.isfile(produced):
            try:
                with open(produced, "r", encoding="utf-8") as f:
                    heard = "".join(s.get("text", "")
                                    for s in json.load(f).get("segments", [])).strip()
            except (OSError, ValueError):
                heard = ""
        row = next(r for r in rows if r["index"] == index)
        out[index] = dict(judge(row["text"], heard), heard_alone=heard)
    return out


def scan_chapter(folder, base, settings=None, log=print, work_dir=None):
    """Transcribe, score, verify - and write <chapter>.qa.json."""
    chapter = Chapter(folder, base, settings, work_dir)
    os.makedirs(chapter.work_dir, exist_ok=True)
    started = time.time()
    segments = repair.transcribe_chapter(chapter, log, None)
    rows = repair.score_chapter(chapter, segments)
    minimum = chapter.settings["min_judged_chars"]
    judged = []
    for row in rows:
        row["part"] = row["index"] + 1
        if len(repair.normalise_for_compare(row["text"])) < minimum:
            row["judged"] = False
            continue
        row["judged"] = True
        row.update(judge(row["text"], row["heard"]))
        if row["flagged"]:
            judged.append(row["index"])
    verified = verify(chapter, rows, judged, log)
    flagged, dropped = [], []
    for index, result in verified.items():
        row = next(r for r in rows if r["index"] == index)
        row["verified"] = result
        (flagged if result["flagged"] else dropped).append(index + 1)
    data = {
        "chapter": base,
        "scanned": time.strftime("%Y-%m-%d %H:%M"),
        "flac_stamp": stamp(chapter.audio_source()),
        "seconds": round(time.time() - started, 1),
        "rows": rows,
        "flagged": sorted(flagged),
        "dropped": sorted(dropped),
    }
    path = qa_path(folder, base)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return data


def report(results):
    """What the run prints when every chapter has been scanned."""
    lines = ["", "=" * 62, "QA scan - Parts worth listening to before publishing"]
    total = sum(len(r["flagged"]) for r in results)
    for data in results:
        rows = {r["index"]: r for r in data["rows"]}
        if not data["flagged"]:
            lines.append(f"  {data['chapter']}: nothing flagged"
                         + (f" ({len(data['dropped'])} dropped by the second pass)"
                            if data["dropped"] else ""))
            continue
        lines.append(f"  {data['chapter']}: {len(data['flagged'])} part(s) to check"
                     + (f", {len(data['dropped'])} dropped by the second pass"
                        if data["dropped"] else ""))
        for part in data["flagged"]:
            row = rows[part - 1]
            result = row.get("verified") or row
            lines.append(f"      Part {part}  similarity {result['similarity']}"
                         f"  ratio {result['length_ratio']}"
                         + (f"  runs {result['overrun']} past the ending"
                            if result.get("overrun") else ""))
            lines.append(f"        script  {row['text']}")
            lines.append(f"        heard   {result.get('heard_alone') or row['heard']}")
    lines.append(f"  {total} part(s) flagged in {len(results)} chapter(s). Open "
                 f"dynamic-repair on this folder - the scan is already there.")
    lines.append("=" * 62)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, help="the output folder")
    parser.add_argument("--chapters", nargs="*", help="bases; default every chapter there")
    parser.add_argument("--settings", help="JSON file with whisper_exe / whisper_model / …")
    parser.add_argument("--work-root", help="where clips and transcripts go")
    args = parser.parse_args()

    settings = {}
    if args.settings and os.path.isfile(args.settings):
        with open(args.settings, "r", encoding="utf-8") as f:
            raw = json.load(f)
        settings = {k: v for k, v in raw.items() if k in DEFAULTS or k == "whisper_model_dir"}

    bases = args.chapters or sorted(
        os.path.splitext(os.path.basename(p))[0].replace(".sync", "")
        for p in os.listdir(args.folder) if p.endswith(".sync.json"))
    results = []
    for base in bases:
        print(f"Scanning {base} …", flush=True)
        work_dir = os.path.join(args.work_root, base) if args.work_root else None
        try:
            results.append(scan_chapter(args.folder, base, settings, print, work_dir))
        except Exception as e:                      # never fail a finished run
            print(f"  ! {base} could not be scanned ({type(e).__name__}: {e})", flush=True)
    if results:
        print(report(results), flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
