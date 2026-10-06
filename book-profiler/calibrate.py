"""
Voice calibration: ONE setting per voice, for any chapter of any book.

    uv run python calibrate.py --speaker <...speaker.safetensors> --out <folder>

The per-book recipe (recipe.py) measured each chapter's sentences with each
voice and put "default" in the middle of the clean window. What decides a
hallucination turned out not to be the scale but the PACE it produces
(machi chapters 1-15, 2,366 Parts, 2026-10-05):

    engine chars / s    library Parts   hallucinated   profiler takes ran long
    under 3.0                  82           16%              74%
    3.0 - 3.5                 170           10.6%            42%
    3.5 - 4.0                 437            2.1%            19%
    4.0 - 4.5                 621            0.5%             6%
    4.5 - 6.0                 857            0                0

All 43 hallucinations were ad-libbed tails, all were repaired by a SHORTER
take, and the same scale means a different pace for each voice (tomita
reads 4.0 ch/s at x1.0, moeshi 6.0). Rendering whole chapters at one flat
scale per voice chosen for about 5 ch/s took the count from 28 to 1 over
four voices; too fast has its own fault (mikku swallowed half a sentence
at 7.3 ch/s). The user chose 5.0 ch/s by ear (2026-10-06).

## What it does

1. PROBE - every template sentence once at x1.0. A take's length is text x
   scale whatever the seed (2,268 sweep takes, CLAUDE.md), so one take
   gives the natural pace exactly.
2. SET   - scale = natural pace / target, on the sentences of
   `short_chars` + 1 and longer (their natural pace is flat across
   lengths; shorter ones read slower per character for every voice).
3. CHECK - the whole template at that scale, `takes` draws each, Whisper,
   the profiler's own tail and similarity rules.
4. ADJUST - the formula alone was wrong for one voice in five (chanyui: 4
   hallucinations at the formula's x1.10, on short slow sentences). Two or
   more tails among the long sentences -> the scale steps down and they are
   checked again; among the short ones -> the SHORT sentences get their own
   lower scale; two or more swallowed long sentences -> it steps up. At
   most `max_rounds` checks; a voice that will not settle is reported, and
   still gets the best setting found.

## What it writes (in --out)

    profile_voice.json   a dynamic profile (dynamic_profile.py, version 3):
                         scope "voice", one band - two when short sentences
                         need their own scale - and ONE speed. The
                         generator, the suite's render job and
                         dynamic-repair read it like any other profile.
    calibration.json     every take: scale, seconds, transcript, verdict
    calibration.md       the same for a person

Takes live under <work_root>/calibration/<voice>/ and can be deleted once
the profile is written.
"""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import wave

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import score  # noqa: E402  - Whisper pass, kana folding, overrun
import sweep  # noqa: E402  - MODEL_REF, batch script, nickname

analyze = sweep.analyze
repair = score.repair
dynamic_profile = analyze.dynamic_profile

TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "calibration_template.txt")
PROFILE_NAME = "profile_voice.json"
# Exit code for "calibrated, but the check never came back clean": the
# profile is written all the same, and the caller decides what to do.
NOT_SETTLED_EXIT = 4

CALIBRATE_DEFAULTS = {
    # The user's choice by ear, 2026-10-06 (tomita and maeda at 4.5 and 5.0).
    "target_pace": 5.0,
    "scale_step": 0.05,
    "scale_min": 0.6,
    "scale_max": 1.6,
    # Sentences up to this many engine characters are "short": slower per
    # character for every voice, and where chanyui's tails were.
    "short_chars": 12,
    "check_takes": 4,
    "max_rounds": 3,
    # This many faulty takes in a class move its scale. One is tolerated:
    # a single bad draw in ~100 would otherwise push every voice faster.
    "faults_to_adjust": 2,
    # A sentence no longer than this cannot show a hallucination (recipe.py).
    "word_span": 6,
}


def load_settings():
    settings = score.load_settings()
    settings.update(analyze.merged_settings(CALIBRATE_DEFAULTS))
    return settings


# ------------------------------------------------------------ the template

def load_template(path, engine):
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    version = "?"
    sentences = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#"):
            if "template - version" in line:
                version = line.split("version", 1)[1].split()[0]
            continue
        if not line:
            continue
        sentences.append({"index": len(sentences) + 1, "text": line,
                          "tts_text": analyze.tts_text(line, {}),
                          "tts_len": len(engine(line))})
    digest = hashlib.sha256("\n".join(s["text"] for s in sentences).encode("utf-8")).hexdigest()[:12]
    return {"version": version, "sha": digest, "sentences": sentences}


# ------------------------------------------------------------ rendering

def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def render(jobs, speaker, settings, folder, log):
    """jobs: [{sentence, scale, take, wav}] - one worker, one model load."""
    os.makedirs(folder, exist_ok=True)
    todo = [j for j in jobs if not os.path.isfile(j["wav"])]
    if not todo:
        return
    jobs_path = os.path.join(folder, "batch_jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump({
            "uv_project_dir": settings["irodori_root"],
            "checkpoint": sweep.MODEL_REF,
            "checkpoint_is_hf": not sweep.MODEL_REF.lower().endswith(".safetensors"),
            "speaker_path": speaker,
            "duration_scale": 1.0,
            "trim_tail": bool(settings["trim_tail"]),
            "seed": None,
            # Judged on clean audio, like the profiler's sweep.
            "watermark": False,
            "jobs": [{"index": n, "text": j["sentence"]["tts_text"], "output_wav": j["wav"],
                      "duration_scale": j["scale"]} for n, j in enumerate(todo, start=1)],
        }, f, ensure_ascii=False, indent=2)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen(["uv", "run", "--no-sync", "python", "-u", sweep.batch_script(settings),
                             "--jobs", jobs_path],
                            cwd=settings["irodori_root"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", env=env)
    done = 0
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if line.startswith("CHUNK_DONE "):
                done += 1
                if done % 20 == 0 or done == len(todo):
                    log(f"  rendered {done}/{len(todo)}")
            elif line.startswith("CHUNK_FAIL "):
                log("  ! " + line)
            elif line.startswith("MODEL_LOADED "):
                log(f"  model loaded in {line.split()[1]}s - {len(todo)} take(s)")
    finally:
        if proc.poll() is None:
            proc.terminate()
    code = proc.wait()
    missing = [j for j in todo if not os.path.isfile(j["wav"])]
    if code != 0 or missing:
        raise RuntimeError(f"the TTS worker exited {code} with {len(missing)} take(s) missing "
                           f"- see {jobs_path}.log")


def take_rows(jobs, settings, log):
    """Transcribe the jobs' wavs and return one row per take."""
    folders = {}
    for j in jobs:
        folders.setdefault(os.path.dirname(j["wav"]), []).append(j["wav"])
    # Whisper's own output (a progress bar per clip) is indented by
    # score.transcribe; it would bury the few lines that matter here.
    score.transcribe(folders, settings,
                     lambda message: None if message.startswith("   ") else log(message))
    rows = []
    for j in jobs:
        s = j["sentence"]
        heard = score.read_transcript(j["wav"])["heard"]
        similarity, ratio = score.compare(s["text"], heard)
        seconds = wav_seconds(j["wav"])
        rows.append({"sentence": s["index"], "tts_len": s["tts_len"], "scale": j["scale"],
                     "take": j["take"], "round": j["round"], "seconds": round(seconds, 3),
                     "pace": round(s["tts_len"] / seconds, 3) if seconds else None,
                     "heard": heard, "similarity": round(similarity, 4),
                     "length_ratio": None if ratio in (None, float("inf")) else round(ratio, 3),
                     "overrun": score.overrun(s["text"], heard)})
    return rows


def judge(rows, template, settings):
    """Set `fault` on every row: "tail", "swallowed" or None.

    The profiler's rules (score.flag_takes / recipe.step_recipe): a tail is
    characters heard after the sentence's own ending, or a transcript over
    `length_ratio_high`; a swallowed sentence is low similarity or a short
    transcript. A sentence Whisper spells differently at every take
    (best similarity under `step_agreement`) is judged on `overrun` alone -
    spelling cannot fake characters after an ending it did not find."""
    by_sentence = {}
    for r in rows:
        by_sentence.setdefault(r["sentence"], []).append(r)
    for index, mine in by_sentence.items():
        text = template["sentences"][index - 1]["text"]
        judged = len(repair.normalise_for_compare(text)) > settings["word_span"]
        spelling = max(r["similarity"] for r in mine) < settings["step_agreement"]
        for r in mine:
            r["fault"] = None
            r["judged"] = judged
            if not judged:
                continue
            ratio = r["length_ratio"]
            if r["overrun"] >= settings["overrun_chars"] or \
                    (not spelling and ratio is not None and ratio > settings["length_ratio_high"]):
                r["fault"] = "tail"
            elif not spelling and (r["similarity"] < settings["similarity_threshold"]
                                   or ratio is None or ratio < settings["length_ratio_low"]):
                r["fault"] = "swallowed"


def snap(value, settings):
    step = settings["scale_step"]
    value = round(round(value / step) * step, 2)
    return min(settings["scale_max"], max(settings["scale_min"], value))


# ------------------------------------------------------------ the loop

def calibrate(speaker, settings, template, root, log):
    sentences = template["sentences"]
    short_max = settings["short_chars"]
    is_short = lambda s: s["tts_len"] <= short_max  # noqa: E731
    rows = []

    def run(round_no, plan):
        """plan: [(sentence, scale, takes)]. Render, transcribe, judge."""
        folder = os.path.join(root, f"round{round_no}")
        jobs = [{"sentence": s, "scale": scale, "take": t, "round": round_no,
                 "wav": os.path.join(folder, f"s{s['index']:02d}_x{scale:.2f}_t{t}.wav")}
                for s, scale, takes in plan for t in range(1, takes + 1)]
        render(jobs, speaker, settings, folder, log)
        new = take_rows(jobs, settings, log)
        rows.extend(new)
        judge(rows, template, settings)
        return new

    log(f"probe: {len(sentences)} sentences at x1.0")
    probe = run(0, [(s, 1.0, 1) for s in sentences])
    long_paces = [r["pace"] for r in probe if r["tts_len"] > short_max]
    short_paces = [r["pace"] for r in probe if r["tts_len"] <= short_max]
    natural = statistics.median(long_paces)
    natural_short = statistics.median(short_paces) if short_paces else None
    scale = snap(natural / settings["target_pace"], settings)
    short_scale = scale
    log(f"natural pace {natural:.2f} ch/s (short sentences {natural_short:.2f}) "
        f"-> x{scale:.2f} for {settings['target_pace']} ch/s")

    rounds, settled, recheck = [], False, list(sentences)
    need = settings["faults_to_adjust"]
    for round_no in range(1, settings["max_rounds"] + 1):
        log(f"check {round_no}: {len(recheck)} sentence(s), long x{scale:.2f}"
            + (f", short x{short_scale:.2f}" if short_scale != scale else ""))
        new = run(round_no, [(s, short_scale if is_short(s) else scale, settings["check_takes"])
                             for s in recheck])
        count = lambda short, fault: sum(  # noqa: E731
            1 for r in new if (r["tts_len"] <= short_max) == short and r["fault"] == fault)
        found = {"long_tails": count(False, "tail"), "long_swallowed": count(False, "swallowed"),
                 "short_tails": count(True, "tail"), "short_swallowed": count(True, "swallowed")}
        rounds.append(dict(found, round=round_no, scale=scale, short_scale=short_scale,
                           takes=len(new)))
        log("  " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in found.items()))
        slower_wanted = found["long_swallowed"] >= need
        faster_wanted = found["long_tails"] >= need
        short_faster = found["short_tails"] >= need
        if not (slower_wanted or faster_wanted or short_faster):
            settled = True
            break
        if round_no == settings["max_rounds"]:
            break
        if slower_wanted and faster_wanted:
            # Tails and swallowed sentences at one scale: no scale fixes both.
            log("  tails AND swallowed sentences at this scale - no better scale to try")
            break
        recheck = []
        if faster_wanted or slower_wanted:
            moved = snap(scale + (settings["scale_step"] if slower_wanted
                                  else -settings["scale_step"]), settings)
            if moved == scale:
                break
            if short_scale == scale:
                short_scale = moved
            scale = moved
            recheck = list(sentences)
        if short_faster and not recheck:
            moved = snap(short_scale - 2 * settings["scale_step"], settings)
            if moved == short_scale:
                break
            short_scale = moved
            recheck = [s for s in sentences if is_short(s)]
        elif short_faster:
            short_scale = snap(min(short_scale, scale) - settings["scale_step"], settings)

    final = [r for r in rows if r["round"] == rounds[-1]["round"]]
    reached = statistics.median(r["pace"] for r in final if r["tts_len"] > short_max) \
        if any(r["tts_len"] > short_max for r in final) else natural / scale
    return {"natural_pace": round(natural, 3),
            "natural_pace_short": round(natural_short, 3) if natural_short else None,
            "scale": scale, "short_scale": short_scale, "settled": settled,
            "pace": round(reached, 3), "rounds": rounds, "takes": rows}


# ------------------------------------------------------------ output

def build_profile(speaker, settings, template, result):
    longest = max(s["tts_len"] for s in template["sentences"])
    short_max = settings["short_chars"]
    one = lambda a, b, scale: {"from_len": a, "to_len": b, "faster": scale,  # noqa: E731
                               "default": scale, "slower": scale}
    if result["short_scale"] != result["scale"]:
        bands = [one(1, short_max, result["short_scale"]), one(short_max + 1, longest, result["scale"])]
    else:
        bands = [one(1, longest, result["scale"])]
    pace = result["pace"]
    return {
        "version": dynamic_profile.PROFILE_VERSION,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book": None, "scope": "voice", "chapters": [],
        "speaker_path": speaker, "speaker_stamp": dynamic_profile.speaker_stamp(speaker),
        "rules": "voice calibration (book-profiler/calibrate.py)",
        "trim_tail": bool(settings["trim_tail"]),
        "silence": {"section": 1.5, "sentence": 1.0, "comma": 0.7},
        "comfortable_length": longest,
        "available_speeds": ["default"],
        "styles": ["scale_default"],
        "bands": bands,
        "pace_targets": {"default": pace, "slower": pace, "faster": pace},
        "calibration": {
            "template_version": template["version"], "template_sha": template["sha"],
            "target_pace": settings["target_pace"],
            "natural_pace": result["natural_pace"],
            "natural_pace_short": result["natural_pace_short"],
            "scale": result["scale"], "short_scale": result["short_scale"],
            "pace": pace, "settled": result["settled"], "rounds": result["rounds"],
        },
    }


def render_md(speaker, settings, template, result):
    name = sweep.nickname_for(speaker)
    o = [f"# Voice calibration - {name}\n",
         f"Template version {template['version']} ({len(template['sentences'])} sentences), "
         f"target {settings['target_pace']} ch/s.\n",
         f"- natural pace at x1.0: **{result['natural_pace']:.2f} ch/s** "
         f"(short sentences {result['natural_pace_short']})",
         f"- setting: **x{result['scale']:.2f}**"
         + (f", short sentences (up to {settings['short_chars']} chars) **x{result['short_scale']:.2f}**"
            if result["short_scale"] != result["scale"] else ""),
         f"- pace reached: {result['pace']:.2f} ch/s",
         f"- {'settled' if result['settled'] else '**NOT settled** - the last check still found faults'}\n",
         "| check | long scale | short scale | takes | long tails | long swallowed | short tails | short swallowed |",
         "|---|---|---|---|---|---|---|---|"]
    for r in result["rounds"]:
        o.append(f"| {r['round']} | x{r['scale']:.2f} | x{r['short_scale']:.2f} | {r['takes']} | "
                 f"{r['long_tails']} | {r['long_swallowed']} | {r['short_tails']} | {r['short_swallowed']} |")
    bad = [t for t in result["takes"] if t.get("fault")]
    if bad:
        o.append("\n## Faulty takes\n")
        for t in bad:
            text = template["sentences"][t["sentence"] - 1]["text"]
            o.append(f"- check {t['round']}, sentence {t['sentence']} ({t['tts_len']} chars) "
                     f"x{t['scale']:.2f}, {t['pace']} ch/s - **{t['fault']}**  \n  script: {text}  \n"
                     f"  heard: {t['heard'] or '(nothing)'}")
    return "\n".join(o) + "\n"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Calibrate one voice against the template text")
    parser.add_argument("--speaker", required=True, help="the .speaker.safetensors file")
    parser.add_argument("--out", required=True, help="folder for profile_voice.json and the report")
    parser.add_argument("--work-root", help="where the takes go (default: settings work_root)")
    parser.add_argument("--target", type=float, help="target pace in engine chars per second")
    parser.add_argument("--template", default=TEMPLATE_PATH)
    parser.add_argument("--list", action="store_true", help="print the template's lengths and stop")
    args = parser.parse_args()

    settings = load_settings()
    if args.work_root:
        settings["work_root"] = args.work_root
    if args.target:
        settings["target_pace"] = args.target
    normalize, norm_path = analyze.load_irodori_normalizer(settings["irodori_root"])
    if normalize is None:
        raise SystemExit(f"Irodori's normaliser not found: {norm_path}")
    template = load_template(args.template, analyze.make_engine(normalize))
    if args.list:
        for s in template["sentences"]:
            print(f"{s['index']:>3} {s['tts_len']:>3}  {s['text']}")
        return 0

    speaker = os.path.abspath(args.speaker)
    if not os.path.isfile(speaker):
        raise SystemExit(f"speaker file not found: {speaker}")
    name = sweep.nickname_for(speaker)
    root = os.path.join(settings["work_root"], "calibration", name)
    os.makedirs(root, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    def log(message):
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    started = time.time()
    log(f"calibrating {name} - template v{template['version']}, "
        f"target {settings['target_pace']} ch/s, takes in {root}")
    result = calibrate(speaker, settings, template, root, log)
    profile = build_profile(speaker, settings, template, result)
    with open(os.path.join(args.out, PROFILE_NAME), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "calibration.json"), "w", encoding="utf-8") as f:
        json.dump({"speaker": speaker, "template": {k: template[k] for k in ("version", "sha")},
                   "settings": {k: settings[k] for k in CALIBRATE_DEFAULTS}, **result},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "calibration.md"), "w", encoding="utf-8") as f:
        f.write(render_md(speaker, settings, template, result))
    dynamic_profile.load_profile(os.path.join(args.out, PROFILE_NAME))   # the contract holds
    log(f"{name}: natural {result['natural_pace']:.2f} ch/s -> x{result['scale']:.2f}"
        + (f" (short x{result['short_scale']:.2f})" if result["short_scale"] != result["scale"] else "")
        + f", {'settled' if result['settled'] else 'NOT settled'}, "
        f"{len(result['takes'])} takes in {time.time() - started:.0f}s")
    return 0 if result["settled"] else NOT_SETTLED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
