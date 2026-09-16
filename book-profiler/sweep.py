"""
sweep.py
--------
Stage 3 of the book profiler: render every length-step sentence of a
chapter at every duration scale, in two arms, several takes each, and
record how long each take came out.

    uv run --project book-profiler python book-profiler/sweep.py \
        --book yojo-senki --chapter chapter_001 \
        --speaker C:/Irodori-TTS/seiyuu/list/tanya.speaker.safetensors

It reads the length steps stage 1 wrote (`analysis.json`) and writes into

    <work_root>/<book>/<scope>/sweep/<seiyuu>/<chapter>/
        step03_31c/seeded/s1.20_t2.wav     the take
        step03_31c/seeded/s1.20_t2.json    its MARKER - see below
        sweep.json / sweep.md              everything so far, re-written each round
        sweep.log

## What is swept

- **steps** - shortest to longest, as stage 1 chose them. Every sentence is
  sent whole: the sweep is what FINDS the comfortable length, so it cannot
  cut to one.
- **scales** - 1.0 upward in 0.1 steps (`scales` setting) until the take
  reaches the 30 s ceiling. The first scale that reaches it is still
  rendered and recorded - "it hit the ceiling at 1.5" is a result - and
  nothing above it is.
- **arms** - `seeded` uses the same fixed seeds at every scale, so a
  difference between 1.0 and 1.2 comes from the scale and not from luck;
  `random` draws fresh ones. The seed actually used is recorded either way.
- **takes** - per arm, per scale.

If a step reaches the ceiling at the LOWEST scale, every longer step is
skipped: that step is where the seiyuu stops coping, and the last step
before it is the longest usable length.

## Length is predicted, repeats are for hallucination

Measured in the chapter_001 pilot (2026-09-16, tanya, trim on): every take
of a sentence at a given scale came out the SAME length to the sample -
seeded or random, all 42 takes of round 1. Irodori predicts duration from
the text and multiplies it by `duration_scale`; the seed changes what is
spoken, not how long it lasts. So:

- ONE probe take per step at the lowest scale is enough to predict the
  length at every scale: `probe_seconds * scale / lowest`.
- The repeats (arms x takes) exist for stage 4 - hallucination is random,
  length is not.
- Every other take is still measured, and one whose length misses its
  prediction by more than `prediction_tolerance` is reported. That is also
  how the clamp shows itself: a take that should have run past the ceiling
  comes back shorter than predicted.

Two rounds, one model load each: round 1 renders the probes, round 2 every
take predicted to stay under the ceiling plus the first scale predicted to
reach it (a ceiling hit is a result, so it is confirmed, not assumed).
Should a take disagree with its prediction, a further round catches up.

## Reaching the ceiling, with trimming on

Trimming shaves trailing silence AFTER the engine has clamped a take to
30 s, so a squeezed take can land a little under 30.00. A take counts as at
the ceiling when it is within `ceiling_margin` of `ceiling_seconds`, or
when its prediction reached that mark and the take came back short.

## Resume

A take is DONE only when its marker exists and describes exactly this
take (sentence, engine text, speaker file size and mtime, scale, arm, seed
rule, trim, checkpoint). The marker is written after the worker reports
CHUNK_DONE and the wav can be read - never before - so a take interrupted
mid-render is simply rendered again. Starting the command again carries on
from whatever is on disk; delete the chapter folder to start over.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import wave

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
if GENERATOR_DIR not in sys.path:
    sys.path.insert(0, GENERATOR_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import text_pipeline as tp  # noqa: E402
import analyze  # noqa: E402

# Same pin as run_audiobook.py and seiyuu-audition.
MODEL_REF = "Aratako/Irodori-TTS-v4.1-Small"
SPEAKER_SUFFIX = ".speaker.safetensors"
MARKER_VERSION = 1

SWEEP_DEFAULTS = {
    "batch_script": "",
    "scales": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8],
    "takes": 3,
    "fixed_seeds": [1001, 2002, 3003],
    "arms": ["seeded", "random"],
    "trim_tail": True,
    "ceiling_seconds": 30.0,
    "ceiling_margin": 0.5,
    # A take further than this from probe * scale is reported.
    "prediction_tolerance": 0.05,
    # A fresh worker every this many takes. In the chapter_001 pilot one
    # worker slowed from ~2 s to 13-36 s per take after ~155 takes, with
    # VRAM at 7.7 of 8.2 GB and utilisation pinned at 100% - the WDDM spill
    # CLAUDE.md records - and a restarted one was fast again. A model load
    # is ~16 s; a spilled take costs more than that.
    "max_takes_per_worker": 80,
    "max_rounds": 8,
}


def load_settings():
    settings = analyze.merged_settings(SWEEP_DEFAULTS)
    settings.update(analyze.load_settings())
    return settings


# ------------------------------------------------------------ paths

def nickname_for(speaker_path):
    name = os.path.basename(speaker_path)
    return name[:-len(SPEAKER_SUFFIX)] if name.endswith(SPEAKER_SUFFIX) \
        else os.path.splitext(name)[0]


def speaker_stamp(path):
    info = os.stat(path)
    return [info.st_size, int(info.st_mtime)]


def chapter_dir(settings, book, scope, speaker, chapter):
    return os.path.join(settings["work_root"], book, scope, "sweep",
                        nickname_for(speaker), chapter)


def step_dir_name(step):
    return f"step{step['step']:02d}_{step['tts_len']}c"


def take_paths(root, step, arm, scale, take):
    folder = os.path.join(root, step_dir_name(step), arm)
    stem = f"s{scale:.2f}_t{take}"
    return os.path.join(folder, stem + ".wav"), os.path.join(folder, stem + ".json")


# ------------------------------------------------------------ takes

def fingerprint(step, arm, scale, take, speaker, settings):
    """Everything that decides what this take sounds like."""
    return {
        "version": MARKER_VERSION,
        "checkpoint": MODEL_REF,
        "speaker_path": os.path.abspath(speaker),
        "speaker_stamp": speaker_stamp(speaker),
        "text": step["text"],
        "tts_text": tp.prepare_tts_text_dynamic(step["text"]),
        "scale": round(scale, 2),
        "arm": arm,
        "take": take,
        "seed_rule": settings["fixed_seeds"][take - 1] if arm == "seeded" else "random",
        "trim_tail": bool(settings["trim_tail"]),
    }


def read_marker(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def done_take(root, step, arm, scale, take, speaker, settings):
    """The marker of a finished take, or None."""
    wav, marker_path = take_paths(root, step, arm, scale, take)
    marker = read_marker(marker_path)
    if not marker or not os.path.isfile(wav):
        return None
    if marker.get("fingerprint") != fingerprint(step, arm, scale, take, speaker, settings):
        return None
    return marker


def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ------------------------------------------------------------ planning

def step_state(root, step, speaker, settings):
    """{scale: [markers]} for this step, finished takes only."""
    state = {}
    for scale in settings["scales"]:
        for arm in settings["arms"]:
            for take in range(1, settings["takes"] + 1):
                marker = done_take(root, step, arm, scale, take, speaker, settings)
                if marker:
                    state.setdefault(scale, []).append(marker)
    return state


def probe_seconds(state, settings):
    """Length at the lowest scale, from the probe take (or any take there)."""
    lowest = settings["scales"][0]
    takes = state.get(lowest) or []
    return takes[0]["seconds"] if takes else None


def classify(step, state, settings):
    """Annotate every finished take in place - predicted length, miss, at
    ceiling - and return the lowest scale at which any take reached the
    ceiling (or None)."""
    probe = probe_seconds(state, settings)
    lowest = settings["scales"][0]
    mark = settings["ceiling_seconds"] - settings["ceiling_margin"]
    first_cap = None
    for scale in sorted(state):
        for m in state[scale]:
            predicted = probe * scale / lowest if probe is not None else None
            miss = round(m["seconds"] - predicted, 3) if predicted is not None else None
            m["predicted_seconds"] = round(predicted, 3) if predicted is not None else None
            m["prediction_miss"] = miss
            m["off_prediction"] = miss is not None and abs(miss) > settings["prediction_tolerance"]
            clamped = predicted is not None and predicted >= mark and m["off_prediction"] \
                and miss < 0
            m["at_ceiling"] = m["seconds"] >= mark or clamped
        if first_cap is None and any(m["at_ceiling"] for m in state[scale]):
            first_cap = scale
    return first_cap


def plan_round(root, steps, speaker, settings):
    """The takes to render next, and a per-step status for the report."""
    jobs, status = [], {}
    lowest = settings["scales"][0]
    gated = False
    for step in steps:
        key = step_dir_name(step)
        if gated:
            status[key] = "skipped - a shorter step reached the ceiling at the lowest scale"
            continue
        state = step_state(root, step, speaker, settings)
        first_cap = classify(step, state, settings)

        probe = probe_seconds(state, settings)
        wanted = []
        if probe is None:
            # Round 1: one probe take - the first seeded take - and nothing
            # else until its length is known.
            arm, take = settings["arms"][0], 1
            wav, marker = take_paths(root, step, arm, lowest, take)
            jobs.append({"step": step, "arm": arm, "scale": lowest, "take": take,
                         "wav": wav, "marker": marker})
        elif first_cap != lowest:
            highest_done = max(state)
            mark = settings["ceiling_seconds"] - settings["ceiling_margin"]
            for scale in settings["scales"]:
                if first_cap is not None and scale > first_cap:
                    break
                wanted.append(scale)
                # Never stop at or below what is already rendered: a
                # prediction that said "reaches it at 1.7" and was wrong
                # must still move on to 1.8, or the step would silently end.
                if first_cap is None and scale > highest_done and \
                        probe * scale / lowest >= mark:
                    break
        else:
            # Reached the ceiling at the lowest scale: finish that scale's
            # repeats (stage 4 still scores them) and go no higher.
            wanted = [lowest]

        for scale in wanted:
            for arm in settings["arms"]:
                for take in range(1, settings["takes"] + 1):
                    if done_take(root, step, arm, scale, take, speaker, settings):
                        continue
                    wav, marker = take_paths(root, step, arm, scale, take)
                    jobs.append({"step": step, "arm": arm, "scale": scale, "take": take,
                                 "wav": wav, "marker": marker})

        pending = any(job["step"] is step for job in jobs)
        if first_cap == lowest:
            status[key] = f"reached the ceiling at {lowest} - longer steps skipped"
            gated = True
        elif pending:
            status[key] = "in progress"
        elif first_cap is not None:
            status[key] = f"complete - reached the ceiling at {first_cap}"
        else:
            status[key] = "complete - never reached the ceiling"
    return jobs, status


# ------------------------------------------------------------ rendering

def batch_script(settings):
    return (settings.get("batch_script") or "").strip() or \
        os.path.join(GENERATOR_DIR, "irodori_batch.py")


def render_round(jobs, speaker, settings, root, log):
    """One worker, one model load, every job of the round."""
    os.makedirs(root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jobs_path = os.path.join(root, f"round-{stamp}.jobs.json")
    payload_jobs = []
    for index, job in enumerate(jobs, start=1):
        os.makedirs(os.path.dirname(job["wav"]), exist_ok=True)
        if os.path.exists(job["marker"]):
            os.remove(job["marker"])
        seed = settings["fixed_seeds"][job["take"] - 1] if job["arm"] == "seeded" else None
        payload_jobs.append({"index": index,
                             "text": tp.prepare_tts_text_dynamic(job["step"]["text"]),
                             "output_wav": job["wav"],
                             "duration_scale": job["scale"],
                             "seed": seed})
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump({
            "uv_project_dir": settings["irodori_root"],
            "checkpoint": MODEL_REF,
            "checkpoint_is_hf": not MODEL_REF.lower().endswith(".safetensors"),
            "speaker_path": os.path.abspath(speaker),
            "duration_scale": settings["scales"][0],
            "trim_tail": bool(settings["trim_tail"]),
            "seed": None,
            # The profiler judges clean audio; the watermark is a
            # publishing decision (see seiyuu-audition's write_job_file).
            "watermark": False,
            "jobs": payload_jobs,
        }, f, ensure_ascii=False, indent=2)

    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    cmd = ["uv", "run", "--no-sync", "python", "-u", batch_script(settings),
           "--jobs", jobs_path]
    started = {}
    total = len(jobs)
    proc = subprocess.Popen(cmd, cwd=settings["irodori_root"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", env=env)
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if line.startswith("MODEL_LOADED "):
                log(f"  model loaded in {line.split()[1]}s - {total} take(s) this round")
            elif line.startswith("CHUNK_START "):
                started[int(line.split()[1])] = time.time()
            elif line.startswith("CHUNK_DONE "):
                parts = line.split()
                index, used_seed = int(parts[1]), parts[2] if len(parts) > 2 else None
                job = jobs[index - 1]
                if not os.path.isfile(job["wav"]):
                    log(f"  ! take {index} reported done but has no wav")
                    continue
                seconds = wav_seconds(job["wav"])
                marker = {
                    "fingerprint": fingerprint(job["step"], job["arm"], job["scale"],
                                               job["take"], speaker, settings),
                    "seconds": round(seconds, 3),
                    "used_seed": int(used_seed) if used_seed not in (None, "None") else None,
                    "render_seconds": round(time.time() - started.get(index, time.time()), 2),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(job["marker"], "w", encoding="utf-8") as f:
                    json.dump(marker, f, ensure_ascii=False, indent=2)
                log(f"  [{index}/{total}] {step_dir_name(job['step'])} {job['arm']:<6} "
                    f"x{job['scale']:.1f} t{job['take']}: {seconds:6.2f}s "
                    f"(render {marker['render_seconds']:.1f}s)")
            elif line.startswith("CHUNK_FAIL "):
                log("  ! " + line)
            elif line.startswith(("BATCH_DONE", "WATERMARK", "BATCH_LOG")):
                log("  " + line)
            elif line:
                log("  worker: " + line)
    finally:
        if proc.poll() is None:
            proc.terminate()
    return proc.wait()


# ------------------------------------------------------------ report

def summarise(root, steps, speaker, settings, status, rounds, wall_seconds):
    rows = []
    for step in steps:
        state = step_state(root, step, speaker, settings)
        first_cap = classify(step, state, settings)
        cells = []
        for scale in sorted(state):
            takes = state[scale]
            secs = [m["seconds"] for m in takes]
            by_arm = {arm: sum(1 for m in takes if m["fingerprint"]["arm"] == arm)
                      for arm in settings["arms"]}
            cells.append({
                "scale": scale,
                "takes": len(takes),
                "takes_by_arm": by_arm,
                "seconds_min": min(secs),
                "seconds_max": max(secs),
                "identical": max(secs) - min(secs) <= 0.001,
                "predicted_seconds": takes[0]["predicted_seconds"],
                "off_prediction": sum(1 for m in takes if m["off_prediction"]),
                "pace_chars_per_second": round(step["tts_len"] / (sum(secs) / len(secs)), 2),
                "at_ceiling": sum(1 for m in takes if m["at_ceiling"]),
                "render_seconds": round(sum(m["render_seconds"] for m in takes), 1),
                "takes_detail": [{"arm": m["fingerprint"]["arm"], "take": m["fingerprint"]["take"],
                                  "seconds": m["seconds"], "used_seed": m["used_seed"]}
                                 for m in takes],
            })
        rows.append({"step": step["step"], "tts_len": step["tts_len"], "text": step["text"],
                     "status": status.get(step_dir_name(step), "not started"),
                     "first_ceiling_scale": first_cap, "cells": cells})
    cells = [c for r in rows for c in r["cells"]]
    return {"rows": rows, "rounds": rounds,
            "takes": sum(c["takes"] for c in cells),
            "cells": len(cells),
            "cells_identical": sum(1 for c in cells if c["identical"]),
            "takes_off_prediction": sum(c["off_prediction"] for c in cells),
            "render_seconds": round(sum(c["render_seconds"] for c in cells), 1),
            "audio_seconds": round(sum(d["seconds"] for c in cells for d in c["takes_detail"]), 1),
            "wall_seconds": round(wall_seconds, 1)}


def render_md(book, chapter, speaker, settings, summary):
    arms = " + ".join(f"{a}" for a in settings["arms"])
    o = [f"# Sweep - {book} / {chapter} / {nickname_for(speaker)}\n",
         f"Arms: {arms}, {settings['takes']} takes each per scale (seeded arm seeds "
         f"{settings['fixed_seeds']}); trim tail {'on' if settings['trim_tail'] else 'off'}; "
         f"ceiling {settings['ceiling_seconds']} s, margin {settings['ceiling_margin']} s.\n",
         f"**{summary['takes']} takes**, {summary['audio_seconds']} s of audio, render time "
         f"{summary['render_seconds']} s (sum of CHUNK_START->DONE), wall time this run "
         f"{summary['wall_seconds']} s over {summary['rounds']} round(s).\n",
         f"**Length identical across every take:** {summary['cells_identical']} of "
         f"{summary['cells']} scale cells. **Takes off their predicted length** "
         f"(probe x scale, tolerance {settings['prediction_tolerance']} s): "
         f"{summary['takes_off_prediction']}.\n",
         "Length and pace are therefore one number per scale; the repeats are for stage 4's "
         "hallucination scoring.\n"]
    for row in summary["rows"]:
        o.append(f"## Step {row['step']} - {row['tts_len']} engine chars\n")
        o.append(f"{row['text']}\n")
        o.append(f"Status: **{row['status']}**\n")
        if not row["cells"]:
            continue
        o.append("| scale | length s | predicted s | pace ch/s | takes | off prediction | "
                 "at ceiling | render s |")
        o.append("|---|---|---|---|---|---|---|---|")
        for c in row["cells"]:
            length = f"{c['seconds_min']:.2f}" if c["identical"] else \
                f"**{c['seconds_min']:.2f}–{c['seconds_max']:.2f} VARIES**"
            takes = " + ".join(str(c["takes_by_arm"][a]) for a in settings["arms"])
            predicted = "" if c["predicted_seconds"] is None else f"{c['predicted_seconds']:.2f}"
            o.append(f"| {c['scale']:.1f} | {length} | {predicted} | "
                     f"{c['pace_chars_per_second']} | {takes} | {c['off_prediction'] or ''} | "
                     f"{c['at_ceiling'] or ''} | {c['render_seconds']} |")
        o.append("")
    return "\n".join(o)


# ------------------------------------------------------------ main

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Book profiler, stage 3: duration-scale sweep")
    parser.add_argument("--book", required=True)
    parser.add_argument("--scope", default="book")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--report-only", action="store_true",
                        help="render nothing - rebuild sweep.json/sweep.md from the takes on disk")
    args = parser.parse_args()

    settings = load_settings()
    speaker = os.path.abspath(args.speaker)
    if not os.path.isfile(speaker):
        raise SystemExit(f"speaker not found: {speaker}")
    analysis_path = os.path.join(settings["work_root"], args.book, args.scope,
                                 "analysis", "analysis.json")
    with open(analysis_path, "r", encoding="utf-8") as f:
        analysis = json.load(f)
    chapter = next((c for c in analysis["chapters"] if c["chapter"] == args.chapter), None)
    if chapter is None:
        raise SystemExit(f"{args.chapter} is not in {analysis_path}")
    steps = sorted(chapter["length_steps"], key=lambda s: s["tts_len"])

    root = chapter_dir(settings, args.book, args.scope, speaker, args.chapter)
    os.makedirs(root, exist_ok=True)
    log_path = os.path.join(root, "sweep.log")

    def log(message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"sweep {args.book}/{args.chapter} with {nickname_for(speaker)} - "
        f"{len(steps)} steps, {len(settings['scales'])} scales, arms {settings['arms']}, "
        f"{settings['takes']} takes")
    began = time.time()
    rounds = 0
    status = {}
    while rounds < settings["max_rounds"]:
        jobs, status = plan_round(root, steps, speaker, settings)
        for key, value in status.items():
            log(f"  {key}: {value}")
        if not jobs or args.report_only:
            if jobs:
                log(f"report only - {len(jobs)} take(s) would still be rendered")
            break
        rounds += 1
        size = max(1, int(settings["max_takes_per_worker"]))
        batches = [jobs[i:i + size] for i in range(0, len(jobs), size)]
        log(f"round {rounds}: {len(jobs)} take(s) in {len(batches)} worker(s)")
        failed = False
        for number, batch in enumerate(batches, start=1):
            if len(batches) > 1:
                log(f" worker {number}/{len(batches)}: {len(batch)} take(s)")
            code = render_round(batch, speaker, settings, root, log)
            if code != 0:
                log(f"! worker exited with code {code} - stopping; run again to resume")
                failed = True
                break
        if failed:
            break
    else:
        log(f"! stopped after max_rounds={settings['max_rounds']}")

    summary = summarise(root, steps, speaker, settings, status, rounds, time.time() - began)
    with open(os.path.join(root, "sweep.json"), "w", encoding="utf-8") as f:
        json.dump(dict(summary, book=args.book, chapter=args.chapter,
                       speaker=speaker, settings={k: settings[k] for k in SWEEP_DEFAULTS}),
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(root, "sweep.md"), "w", encoding="utf-8") as f:
        f.write(render_md(args.book, args.chapter, speaker, settings, summary))
    log(f"done: {summary['takes']} takes, render {summary['render_seconds']} s, "
        f"wall {summary['wall_seconds']} s -> {root}")

    # Exit non-zero whenever takes are still owed, so a script chaining
    # sweep -> score never scores a half-rendered chapter as if it were whole.
    remaining, _status = plan_round(root, steps, speaker, settings)
    if remaining and not args.report_only:
        log(f"! INCOMPLETE - {len(remaining)} take(s) still to render; run again to resume")
        sys.exit(1)


if __name__ == "__main__":
    main()
