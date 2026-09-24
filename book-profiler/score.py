"""
score.py
--------
Stage 4a of the book profiler: transcribe every sweep take with Whisper and
score it against the sentence it should have read.

    uv run --project book-profiler python book-profiler/score.py \
        --book yojo-senki --chapter chapter_001 \
        --speaker C:/Irodori-TTS/seiyuu/list/tanya.speaker.safetensors

Writes beside the sweep:

    <sweep chapter dir>/step03_31c/seeded/whisper/s1.20_t2.json   transcript
    <sweep chapter dir>/score.json / score.md

## The method is chapter-repair's, imported

`repair.similarity()` - punctuation and spacing stripped from both sides,
difflib ratio, plus the transcript/script length ratio - and the two
thresholds `repair.suspicious()` applies (similarity below
`similarity_threshold`, or a length ratio outside 0.65-1.45 - here tightened
to 0.65-1.15, plus an ending-repeat check; see SCORE_DEFAULTS). Imported, not
copied, for the reason text_pipeline is imported: the profiler must judge
takes exactly the way a published chapter is judged.

## Whisper once per folder, not once per take

whisper.exe takes many files and loads the model once (verified in its
source, transcribe.py `nargs="+"`). It names each transcript after the
audio file, and every step/arm folder reuses the same names
(`s1.00_t1.wav`...), so there is one call per folder with that folder's own
`whisper/` as output - 14 calls for a 7-step chapter instead of 378.

A transcript newer than its wav is reused, so scoring can be re-run freely
and a re-rendered take is transcribed again.

## A shortlist, never a verdict

Whisper mishears (CLAUDE.md: the onboarder's えへへ。), and it spells
differently from the book - a kanji the author wrote in kana is a
"mismatch" at every scale. So besides the absolute thresholds each take is
also compared with its own STEP: the same sentence at another scale carries
the same spelling noise, and a take far below its step's median is
suspicious whatever the absolute number says. Very short clips are the
weakest case - Whisper is known to invent text on a second of audio.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
REPAIR_DIR = os.path.join(GENERATOR_DIR, "chapter-repair")
for path in (GENERATOR_DIR, SCRIPT_DIR, REPAIR_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)
import sweep  # noqa: E402
import repair  # noqa: E402

SCORE_DEFAULTS = {
    "whisper_exe": "C:\\Transcribe\\.venv\\Scripts\\whisper.exe",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ja",
    "device": "cuda",
    # repair.DEFAULT_SETTINGS' value; the low length ratio is repair.suspicious()'s.
    "similarity_threshold": 0.72,
    "length_ratio_low": 0.65,
    # Was repair's 1.45 until 2026-09-22. A clean read transcribes at 1.00;
    # 1.45 let a seiyuu read the whole sentence and then ad-lib a tail
    # (marinka-03-calm-shonen: 36 of 84 takes at x1.5 ran >15% long, none
    # flagged). At 1.15: 0 new flags over tanya's 1,944 takes and moeshi's.
    "length_ratio_high": 1.15,
    # Transcript characters after the sentence's ending was already heard -
    # catches a tail too short for any ratio (…葉を噛む、噛む。, +4%).
    "overrun_chars": 2,
    # A take this far below its step's median similarity is flagged too.
    "step_drop": 0.15,
    # When the best take of a step scores below this, Whisper and the book
    # simply spell the sentence differently (林檎 vs リンゴ): similarity and
    # a long length ratio stop meaning anything for that step. See
    # flag_takes(). Chosen from the data: 54 of 56 steps measured on tanya
    # and hayamin have a best take at 0.88 or above.
    "step_agreement": 0.85,
}


def load_settings():
    settings = sweep.analyze.merged_settings(SCORE_DEFAULTS)
    settings.update(sweep.load_settings())
    return settings


# ------------------------------------------------------------ transcription

def folder_takes(root):
    """{folder: [wav, ...]} for every take with a valid marker on disk."""
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        if os.path.basename(dirpath) == "whisper":
            continue
        for name in sorted(files):
            if name.endswith(".wav") and os.path.isfile(os.path.join(dirpath, name[:-4] + ".json")):
                found.setdefault(dirpath, []).append(os.path.join(dirpath, name))
    return found


def transcript_path(wav):
    folder, name = os.path.split(wav)
    return os.path.join(folder, "whisper", os.path.splitext(name)[0] + ".json")


def needs_transcript(wav):
    out = transcript_path(wav)
    return not os.path.isfile(out) or os.path.getmtime(out) < os.path.getmtime(wav)


def transcribe(folders, settings, log):
    todo = {folder: [w for w in wavs if needs_transcript(w)] for folder, wavs in folders.items()}
    todo = {folder: wavs for folder, wavs in todo.items() if wavs}
    if not todo:
        log("all transcripts already on disk")
        return
    for number, (folder, wavs) in enumerate(sorted(todo.items()), start=1):
        out_dir = os.path.join(folder, "whisper")
        os.makedirs(out_dir, exist_ok=True)
        rel = os.path.relpath(folder, os.path.dirname(os.path.dirname(folder)))
        log(f"whisper {number}/{len(todo)}: {rel} - {len(wavs)} take(s)")
        cmd = [settings["whisper_exe"], *wavs,
               "--model", settings["whisper_model"],
               "--output_dir", out_dir,
               "--output_format", "json",
               "--task", "transcribe",
               "--language", settings["whisper_language"],
               "--device", settings["device"],
               "--verbose", "False"]
        started = time.time()
        # repair.run_streaming sets PYTHONIOENCODING - without it Whisper
        # dies on the first Japanese character written to a pipe and
        # "succeeds" having transcribed nothing.
        code = repair.run_streaming(cmd, lambda line: log("   " + line) if line.strip() else None)
        missing = [w for w in wavs if needs_transcript(w)]
        log(f"  {time.time() - started:.1f}s, exit {code}"
            + (f", {len(missing)} transcript(s) missing" if missing else ""))
        if code != 0 or missing:
            raise RuntimeError(f"Whisper failed in {folder}")


# ------------------------------------------------------------ scoring

def read_transcript(wav):
    with open(transcript_path(wav), "r", encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments", [])
    return {
        "heard": (data.get("text") or "").strip(),
        "segments": len(segments),
        "max_compression_ratio": max((s.get("compression_ratio", 0) for s in segments), default=0),
        "min_avg_logprob": min((s.get("avg_logprob", 0) for s in segments), default=0),
        "max_no_speech_prob": max((s.get("no_speech_prob", 0) for s in segments), default=0),
    }


def fold_kana(text):
    """Katakana folded to hiragana, so a transcript that writes リンゴ for a
    script's りんご is not counted as a mismatch (user decision 2026-09-24).
    Whisper chooses a script of its own; only the sounds are comparable."""
    out = []
    for ch in text or "":
        code = ord(ch)
        # Full-width katakana ァ..ヶ -> hiragana; ー and ヽ are left alone.
        out.append(chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else ch)
    return "".join(out)


def compare(script, heard):
    """similarity() on kana-folded text."""
    return repair.similarity(fold_kana(script), fold_kana(heard))


def overrun(script, heard, k=3):
    """How many transcript characters follow the point where the script's
    last `k` characters were heard (punctuation stripped from both). 0 when
    the ending was never heard - that is a misread or a truncation, which
    similarity and the low length ratio already cover.

    The ending is matched at its own occurrence count, so a sentence that
    says its last word twice on purpose is not a repeat."""
    a = fold_kana(repair.normalise_for_compare(script))
    b = fold_kana(repair.normalise_for_compare(heard))
    if len(a) < k:
        return 0
    ending = a[-k:]
    pos = -1
    for _ in range(a.count(ending)):
        pos = b.find(ending, pos + 1)
        if pos < 0:
            return 0
    return len(b) - (pos + k)


def score_chapter(root, steps, speaker, settings):
    rows = []
    for step in steps:
        state = sweep.step_state(root, step, speaker, settings)
        sweep.classify(step, state, settings)
        # Judged against what was meant to be HEARD: the readings applied.
        spoken = sweep.analyze.spoken_text(step.get("spoken_text") or step["text"], settings)
        takes = []
        for scale in sorted(state):
            for marker in state[scale]:
                fp = marker["fingerprint"]
                wav, _m = sweep.take_paths(root, step, fp["arm"], scale, fp["take"])
                t = read_transcript(wav)
                ratio, length_ratio = compare(spoken, t["heard"])
                takes.append(dict(t, scale=scale, arm=fp["arm"], take=fp["take"],
                                  wav=wav, seconds=marker["seconds"],
                                  used_seed=marker["used_seed"],
                                  at_ceiling=marker["at_ceiling"],
                                  similarity=round(ratio, 4),
                                  length_ratio=None if length_ratio == float("inf")
                                  else round(length_ratio, 3)))
        median = flag_takes(spoken, takes, settings)
        rows.append({"step": step["step"], "tts_len": step["tts_len"], "text": step["text"],
                     "spoken_text": spoken,
                     "median_similarity": round(median, 4), "takes": takes})
    return rows


def flag_takes(text, takes, settings):
    """Set each take's `flags` (reasons, empty = clean), `overrun` and
    `ran_long` in place; return the step's median similarity. Needs only
    what a score.json take already holds, so it can re-judge old scores."""
    median = statistics.median(t["similarity"] for t in takes) if takes else 0
    # A step where even the BEST take disagrees with its script is one
    # Whisper spells differently, not one the seiyuu read badly: wall's
    # 林檎をもいだり…, transcribed リンゴ / りんご, never scores above 0.84
    # although the reads are correct. Every signal is then noise -
    # similarity, the length ratio (kana is longer than kanji) and the
    # step's own median alike - so recipe.py does not judge such a step at
    # all; it is recorded here and the flags below stay as a shortlist for
    # a person (user decision 2026-09-24).
    low_agreement = bool(takes) and max(t["similarity"] for t in takes) \
        < settings.get("step_agreement", 0.85)
    for t in takes:
        reasons = []
        if t["similarity"] < settings["similarity_threshold"]:
            reasons.append(f"similarity {t['similarity']:.2f}")
        lr = t["length_ratio"]
        if lr is None or not (settings["length_ratio_low"] <= lr <= settings["length_ratio_high"]):
            reasons.append(f"length ratio {lr}")
        if median - t["similarity"] > settings["step_drop"]:
            reasons.append(f"{median - t['similarity']:.2f} below the step median")
        t["overrun"] = overrun(text, t["heard"])
        if t["overrun"] >= settings["overrun_chars"]:
            reasons.append(f"runs {t['overrun']} chars past the ending")
        # Something was added: never excused as a word slip (recipe.py).
        t["ran_long"] = (lr is not None and lr > settings["length_ratio_high"]) or \
            t["overrun"] >= settings["overrun_chars"]
        t["step_median"] = round(median, 4)
        t["low_agreement"] = low_agreement
        t["flags"] = reasons
    return median


def arm_summary(takes, arms):
    out = {}
    for arm in arms:
        mine = [t for t in takes if t["arm"] == arm]
        if not mine:
            continue
        out[arm] = {"takes": len(mine),
                    "mean_similarity": round(statistics.fmean(t["similarity"] for t in mine), 4),
                    "flagged": sum(1 for t in mine if t["flags"])}
    return out


def render_md(book, chapter, speaker, settings, rows):
    arms = settings["arms"]
    scales = sorted({t["scale"] for r in rows for t in r["takes"]})
    every = [t for r in rows for t in r["takes"]]
    o = [f"# Hallucination scan - {book} / {chapter} / {sweep.nickname_for(speaker)}\n",
         f"Whisper `{settings['whisper_model']}`, scored with chapter-repair's "
         f"`similarity()`. Flagged when similarity < {settings['similarity_threshold']}, "
         f"length ratio outside {settings['length_ratio_low']}-{settings['length_ratio_high']}, "
         f"{settings['overrun_chars']}+ characters heard after the sentence's ending, "
         f"or more than {settings['step_drop']} below the step's own median. Kana is folded "
         f"before comparing, and a step whose best take scores below "
         f"{settings['step_agreement']} is judged without the similarity and long-ratio "
         f"tests - Whisper spells it differently. A shortlist, "
         "not a verdict.\n",
         f"**{len(every)} takes, {sum(1 for t in every if t['flags'])} flagged.**\n"]

    o.append("## Seeded vs random\n")
    o.append("| arm | takes | mean similarity | flagged |")
    o.append("|---|---|---|---|")
    for arm, s in arm_summary(every, arms).items():
        o.append(f"| {arm} | {s['takes']} | {s['mean_similarity']:.3f} | {s['flagged']} |")
    o.append("")

    o.append("## Flagged takes per step and scale\n")
    o.append("Each cell: flagged / takes, then the lowest similarity.\n")
    o.append("| step | chars | median sim | " + " | ".join(f"{s:.1f}" for s in scales) + " |")
    o.append("|---|---|---|" + "---|" * len(scales))
    for r in rows:
        cells = []
        for s in scales:
            mine = [t for t in r["takes"] if t["scale"] == s]
            if not mine:
                cells.append("")
                continue
            bad = sum(1 for t in mine if t["flags"])
            low = min(t["similarity"] for t in mine)
            cells.append(f"{'**' if bad else ''}{bad}/{len(mine)}{'**' if bad else ''} · {low:.2f}")
        o.append(f"| {r['step']} | {r['tts_len']} | {r['median_similarity']:.2f} | "
                 + " | ".join(cells) + " |")
    o.append("")

    o.append("## Mean similarity per scale, all steps\n")
    o.append("| scale | " + " | ".join(arms) + " | all | flagged |")
    o.append("|---|" + "---|" * (len(arms) + 2))
    for s in scales:
        mine = [t for t in every if t["scale"] == s]
        per_arm = arm_summary(mine, arms)
        o.append(f"| {s:.1f} | " + " | ".join(
            f"{per_arm[a]['mean_similarity']:.3f}" if a in per_arm else "" for a in arms)
            + f" | {statistics.fmean(t['similarity'] for t in mine):.3f} | "
            f"{sum(1 for t in mine if t['flags'])}/{len(mine)} |")
    o.append("")

    o.append("## Flagged takes\n")
    for r in rows:
        bad = [t for t in r["takes"] if t["flags"]]
        if not bad:
            continue
        o.append(f"### Step {r['step']} - {r['tts_len']} chars\n")
        o.append(f"Script: {r['text']}\n")
        for t in bad:
            o.append(f"- x{t['scale']:.1f} {t['arm']} t{t['take']} ({t['seconds']:.2f}s) - "
                     f"{'; '.join(t['flags'])}  \n  heard: {t['heard'] or '(nothing)'}  \n"
                     f"  `{t['wav']}`")
        o.append("")

    o.append("## Unflagged sample transcripts (lowest similarity per step)\n")
    o.append("What spelling noise looks like when nothing is wrong.\n")
    for r in rows:
        good = sorted((t for t in r["takes"] if not t["flags"]), key=lambda t: t["similarity"])
        if good:
            t = good[0]
            o.append(f"- step {r['step']} x{t['scale']:.1f} {t['arm']} t{t['take']} "
                     f"sim {t['similarity']:.2f}: {t['heard']}")
    o.append("")
    return "\n".join(o)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Book profiler, stage 4a: hallucination scan")
    parser.add_argument("--book", required=True)
    parser.add_argument("--scope", default="book")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--speaker", required=True)
    sweep.analyze.add_run_options(parser, takes=True)
    args = parser.parse_args()

    settings = sweep.analyze.apply_run_options(load_settings(), args)
    speaker = os.path.abspath(args.speaker)
    # Scoring reads the wavs; a cleaned chapter's score.json is final.
    cleaned = sweep.analyze.cleaned_note(sweep.chapter_dir(settings, args.book, args.scope,
                                                           speaker, args.chapter))
    if cleaned:
        raise SystemExit(f"{args.chapter}: {cleaned}")
    analysis_path = os.path.join(settings["work_root"], args.book, args.scope,
                                 "analysis", "analysis.json")
    with open(analysis_path, "r", encoding="utf-8") as f:
        analysis = json.load(f)
    chapter = next(c for c in analysis["chapters"] if c["chapter"] == args.chapter)
    steps = sorted(chapter["length_steps"], key=lambda s: s["tts_len"])
    root = sweep.chapter_dir(settings, args.book, args.scope, speaker, args.chapter)

    log_path = os.path.join(root, "score.log")

    def log(message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    folders = folder_takes(root)
    log(f"score {args.book}/{args.chapter} with {sweep.nickname_for(speaker)} - "
        f"{sum(len(v) for v in folders.values())} takes in {len(folders)} folder(s)")
    transcribe(folders, settings, log)
    rows = score_chapter(root, steps, speaker, settings)
    with open(os.path.join(root, "score.json"), "w", encoding="utf-8") as f:
        json.dump({"book": args.book, "chapter": args.chapter, "speaker": speaker,
                   "settings": {k: settings[k] for k in SCORE_DEFAULTS}, "rows": rows},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(root, "score.md"), "w", encoding="utf-8") as f:
        f.write(render_md(args.book, args.chapter, speaker, settings, rows))
    every = [t for r in rows for t in r["takes"]]
    log(f"done: {len(every)} takes scored, {sum(1 for t in every if t['flags'])} flagged -> {root}")


if __name__ == "__main__":
    main()
