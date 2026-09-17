"""
listen.py
---------
Stage 5 of the book profiler: the listening test. Renders one real passage
of the book the way dynamic profile mode would - sentence by sentence, each
with its own parameters, joined with the dynamic silences - in six
versions, and opens them in the audition tool's Results window.

    uv run --project book-profiler python book-profiler/listen.py \
        --book yojo-senki --speaker C:/Irodori-TTS/seiyuu/list/tanya.speaker.safetensors

    --window-only   open the window on what is already rendered
    --no-window     render, write listen.md, do not open the window

## The six samples

                default   slower   faster
    scale recipe    1        2        3      duration_scale from the profile's bands
    pace recipe     4        5        6      a fixed length per sentence

The SCALE recipe is the one stage 4 built: each sentence gets the
`duration_scale` of its length band, and Irodori's own duration predictor
decides how long that is.

The PACE recipe asks for a length directly (`seconds` in the job, i.e.
SamplingRequest.seconds): engine characters / target pace. The target for
each style is the MEDIAN pace the scale recipe itself produces for that
style across the book's measured steps - so both recipes average the same
speed, and what the ear compares is evenness: the scale recipe keeps
Irodori's per-sentence sense of timing (and its 3.7-6.2 ch/s spread at
"default"), the pace recipe flattens it. Why pace is on the table at all:
over 2,268 sweep takes, flag rate rose with pace, not with length (see
CLAUDE.md, book-profiler).

## Fair comparison

Every sentence gets its OWN fixed seed, and that seed is shared by all six
samples, so the recipe is the only thing that differs between them. This
is not the "one seed for a whole run" CLAUDE.md warns against - no two
sentences share a seed.

## The passage

Chosen automatically from the chapters the profile covers: consecutive
sentences, ~`target_chars` engine characters, that contain short, medium
and long sentences, dialogue as well as narration, and - if the book allows
- a section break, so all three silences are heard. Sentences are never
cut unless they exceed the profile's comfortable length L.

Rendering reuses a take whose wav and job fingerprint are already on disk,
so reopening or re-stitching costs nothing; delete the folder to redraw.
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
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
AUDITION_DIR = os.path.join(GENERATOR_DIR, "seiyuu-audition")
for path in (GENERATOR_DIR, SCRIPT_DIR, AUDITION_DIR,
             os.path.join(GENERATOR_DIR, "chapter-repair")):
    if path not in sys.path:
        sys.path.insert(0, path)
import text_pipeline as tp  # noqa: E402
import dynamic_profile  # noqa: E402
import analyze  # noqa: E402
import sweep  # noqa: E402
import recipe  # noqa: E402
import audition  # noqa: E402

LISTEN_DEFAULTS = {
    "target_chars": 450,
    "listen_min_long": 70,
    "seed_base": 5000,
}
STYLES = ("default", "slower", "faster")
METHODS = ("scale", "pace")


def load_settings():
    settings = analyze.merged_settings(LISTEN_DEFAULTS)
    settings.update(recipe.load_settings())
    return settings


# ------------------------------------------------------------ passage

def choose_passage(analysis, chapters, bands, settings):
    """Consecutive sentences of one chapter, scored for coverage.

    Must: at least one sentence in every recipe band, one of at least
    `listen_min_long` characters, some dialogue (「) and some narration.
    Then prefer a section break inside the window, then the total nearest
    `target_chars`. Sentences with an unmeasured symbol or latin letters
    are avoided, so nothing but the recipe is being judged."""
    unmeasured = {s["char"] for s in analysis["symbols"] if s["status"] == "unmeasured"}
    target = settings["target_chars"]
    best = None
    for name in chapters:
        sents = [s for s in analysis["sentences"] if s["chapter"] == name]
        for start in range(len(sents)):
            total = 0
            for end in range(start, len(sents)):
                s = sents[end]
                # Headings stay in: they are read aloud in a real chapter,
                # and they usually sit right after the section break the
                # passage should contain. Only what would confound the
                # judgement ends a window.
                if [r for r in analyze.exclusion_reasons(s, unmeasured, 0)
                        if r.startswith(("unmeasured", "latin"))]:
                    break
                total += s["tts_len"]
                if total > target * 1.3:
                    break
                window = sents[start:end + 1]
                if total < target * 0.7:
                    continue
                banded = {recipe.lookup(bands, w["tts_len"])[0]["from_len"] for w in window}
                if len(banded) < len(bands):
                    continue
                if max(w["tts_len"] for w in window) < settings["listen_min_long"]:
                    continue
                # A spoken line OPENS with 「 - "「技術」" inside narration
                # is a quotation, not dialogue.
                dialogue = sum(1 for w in window if w["text"].startswith("「")) >= 2
                narration = sum(1 for w in window if not w["text"].startswith("「")) >= 2
                if not (dialogue and narration):
                    continue
                section_break = any(w["gap_before"] == "section" for w in window[1:])
                key = (section_break, -abs(total - target), -start)
                if best is None or key > best[0]:
                    best = (key, name, window)
    if best is None:
        raise SystemExit("no passage in these chapters covers every band - "
                         "lower target_chars or listen_min_long")
    return best[1], best[2]


# ------------------------------------------------------------ recipes

def sample_jobs(window, profile, engine, settings):
    """{(method, style): [job params per piece]}.

    The requests come from dynamic_profile.plan_pieces() - the SAME code the
    generator's dynamic mode renders a chapter with - so what is heard here
    is what a chapter would get. The passage's first piece has no silence
    before it: a sample starts on speech."""
    sentences = [(s["text"], "chapter_start" if i == 0 else s["gap_before"])
                 for i, s in enumerate(window)]
    out = {}
    for method in METHODS:
        for style in STYLES:
            pieces, _skipped = dynamic_profile.plan_pieces(
                sentences, profile, f"{method}_{style}", engine, first_gap=None)
            rows = []
            for index, piece in enumerate(pieces):
                row = {"piece": index + 1, "text": piece["display_text"], "gap": piece["gap"],
                       "chars": piece["engine_len"], "seed": settings["seed_base"] + index + 1}
                row.update(piece["request"])
                rows.append(row)
            out[(method, style)] = rows
    return out


# ------------------------------------------------------------ rendering

def job_fingerprint(row, speaker, settings):
    return {"text": tp.prepare_tts_text_dynamic(row["text"]),
            "speaker": os.path.abspath(speaker), "speaker_stamp": sweep.speaker_stamp(speaker),
            "seed": row["seed"], "duration_scale": row.get("duration_scale"),
            "seconds": row.get("seconds"), "trim_tail": bool(settings["trim_tail"]),
            "checkpoint": sweep.MODEL_REF}


def take_path(root, row, speaker, settings):
    digest = hashlib.sha1(json.dumps(job_fingerprint(row, speaker, settings),
                                     ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return os.path.join(root, "takes", digest.hexdigest()[:16] + ".wav")


def render_missing(samples, speaker, settings, root, log):
    """Every distinct take any sample needs and does not have, through the
    same worker the generator uses, `max_takes_per_worker` at a time."""
    needed = {}
    for rows in samples.values():
        for row in rows:
            wav = take_path(root, row, speaker, settings)
            if not os.path.isfile(wav):
                needed[wav] = row
    if not needed:
        log("all takes already on disk")
        return
    os.makedirs(os.path.join(root, "takes"), exist_ok=True)
    items = list(needed.items())
    size = int(settings["max_takes_per_worker"])
    for batch_no, first in enumerate(range(0, len(items), size), start=1):
        batch = items[first:first + size]
        jobs = []
        for index, (wav, row) in enumerate(batch, start=1):
            job = {"index": index, "text": tp.prepare_tts_text_dynamic(row["text"]),
                   "output_wav": wav + ".part.wav", "seed": row["seed"]}
            if row.get("duration_scale") is not None:
                job["duration_scale"] = row["duration_scale"]
            if row.get("seconds") is not None:
                job["seconds"] = row["seconds"]
            jobs.append(job)
        jobs_path = os.path.join(root, f"render-{time.strftime('%Y%m%d-%H%M%S')}.jobs.json")
        with open(jobs_path, "w", encoding="utf-8") as f:
            json.dump({"uv_project_dir": settings["irodori_root"], "checkpoint": sweep.MODEL_REF,
                       "checkpoint_is_hf": True, "speaker_path": os.path.abspath(speaker),
                       "duration_scale": 1.0, "trim_tail": bool(settings["trim_tail"]),
                       "seed": None, "watermark": False, "jobs": jobs},
                      f, ensure_ascii=False, indent=2)
        log(f"worker {batch_no}: {len(jobs)} take(s)")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.Popen(["uv", "run", "--no-sync", "python", "-u",
                                 sweep.batch_script(settings), "--jobs", jobs_path],
                                cwd=settings["irodori_root"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env)
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if line.startswith("CHUNK_DONE "):
                    index = int(line.split()[1])
                    wav, _row = batch[index - 1]
                    # Renamed only once the worker says it is done, so an
                    # interrupted take is never mistaken for a finished one.
                    os.replace(wav + ".part.wav", wav)
                    if index % 10 == 0 or index == len(batch):
                        log(f"  {index}/{len(batch)}")
                elif line.startswith(("CHUNK_FAIL", "MODEL_LOADED", "BATCH_DONE")):
                    log("  " + line)
        finally:
            if proc.poll() is None:
                proc.terminate()
        if proc.wait() != 0:
            raise SystemExit("the TTS worker failed - run again to resume")


def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def stitch(samples, speaker, settings, root, log):
    silences = {"section": settings["silence_section"], "sentence": settings["silence_sentence"],
                "comma": settings["silence_comma"]}
    results = {}
    for (method, style), rows in samples.items():
        wavs = [take_path(root, row, speaker, settings) for row in rows]
        missing = [w for w in wavs if not os.path.isfile(w)]
        if missing:
            results[(method, style)] = {"ok": False, "error": f"{len(missing)} take(s) missing"}
            continue
        fmt = audition.probe_audio_format(wavs[0])
        work = os.path.join(root, f"{method}_{style}_work")
        silence_wavs = audition.render_silence_wavs(work, fmt, silences)
        listing = os.path.join(work, "concat.txt")
        with open(listing, "w", encoding="utf-8") as f:
            for row, wav in zip(rows, wavs):
                if row["gap"]:
                    kind = "section" if row["gap"] == "section" else \
                        "comma" if row["gap"] == "comma" else "sentence"
                    f.write(f"file '{silence_wavs[kind]}'\n")
                f.write(f"file '{os.path.abspath(wav)}'\n")
        out = os.path.join(root, f"sample_{method}_{style}.wav")
        ok, error = audition.stitch_wavs(listing, out, fmt)
        for row, wav in zip(rows, wavs):
            row["take_seconds"] = round(wav_seconds(wav), 2)
            row["pace"] = round(row["chars"] / row["take_seconds"], 2) if row["take_seconds"] else 0
        results[(method, style)] = {"ok": ok, "error": error, "wav": out if ok else None,
                                    "dir": root,
                                    "seconds": round(wav_seconds(out), 1) if ok else 0}
        if ok:
            log(f"sample {method}/{style}: {results[(method, style)]['seconds']} s")
    return results


# ------------------------------------------------------------ report

def render_md(book, speaker, chapter, window, samples, results, targets):
    o = [f"# Listening test - {book} / {sweep.nickname_for(speaker)}\n",
         f"Passage: {chapter}, sentences #{window[0]['index']}–#{window[-1]['index']} "
         f"({len(window)} sentences, {sum(s['tts_len'] for s in window)} engine chars).\n",
         f"Pace targets (median pace of the scale recipe per style): "
         + ", ".join(f"{k} {v} ch/s" for k, v in targets.items()) + ".\n",
         "| sample | length | pace range ch/s |", "|---|---|---|"]
    for (method, style), result in results.items():
        paces = [r["pace"] for r in samples[(method, style)] if r.get("pace")]
        o.append(f"| {method} {style} | {result.get('seconds', 0)} s | "
                 f"{min(paces) if paces else '-'}–{max(paces) if paces else '-'} |")
    o.append("\n| # | gap | chars | " + " | ".join(f"{m} {s}" for m in METHODS for s in STYLES)
             + " | text |")
    o.append("|---|---|---|" + "---|" * 6 + "---|")
    for i, row in enumerate(samples[("scale", "default")]):
        cells = []
        for m in METHODS:
            for s in STYLES:
                r = samples[(m, s)][i]
                what = f"x{r['duration_scale']}" if m == "scale" else f"{r['seconds']}s"
                cells.append(f"{what} → {r.get('pace', '')}")
        o.append(f"| {row['piece']} | {row['gap'] or ''} | {row['chars']} | "
                 + " | ".join(cells) + f" | {row['text']} |")
    o.append("\nEach cell: what was requested → the pace (engine ch/s) that came out.")
    return "\n".join(o)


# ------------------------------------------------------------ window

def open_window(book, speaker, chapter, window, samples, results, targets, settings):
    import customtkinter as ctk
    import results_window as rw

    def summarise(spec, _mode):
        return spec["summary"]

    def chunk_table(parent, chunks, register):
        """The audition table, with the recipe's request and the pace that
        came out in the length column's place."""
        table = ctk.CTkFrame(parent, fg_color="#FCFCFD", border_width=1,
                             border_color=rw.ENTRY_BORDER, corner_radius=8)
        head = ctk.CTkFrame(table, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(9, 4))
        for text, width in (("#", 26), ("GAP", 60), ("CHARS", 46), ("REQUEST", 70),
                            ("CH/S", 46), ("TEXT", 0)):
            label = ctk.CTkLabel(head, text=text, text_color=rw.SUBTITLE,
                                 font=ctk.CTkFont(size=10, weight="bold"),
                                 anchor="e" if width else "w")
            if width:
                label.configure(width=width)
                label.pack(side="left", padx=(0, 10))
            else:
                label.pack(side="left", fill="x", expand=True, padx=(2, 0))
        for chunk in chunks:
            row = ctk.CTkFrame(table, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=5)
            for value, width in ((chunk["index"], 26), (chunk["gap"], 60), (chunk["chars"], 46),
                                 (chunk["request"], 70), (chunk["pace"], 46)):
                ctk.CTkLabel(row, text=str(value), width=width, anchor="ne",
                             text_color=rw.SUBTITLE, font=ctk.CTkFont(size=12)).pack(
                    side="left", anchor="n", padx=(0, 10))
            body = ctk.CTkLabel(row, text=chunk["display_text"], anchor="w", justify="left",
                                text_color=rw.ENTRY_TEXT, font=ctk.CTkFont(size=13),
                                wraplength=560)
            body.pack(side="left", fill="x", expand=True, padx=(2, 0))
            register(body, rw.TABLE_INSET + 190)
        ctk.CTkFrame(table, fg_color="transparent", height=4).pack()
        return table

    # The window's module-level helpers are swapped for recipe-shaped ones;
    # everything else - cards, play, stop, folder, layout - is the audition
    # tool's own.
    rw.summarise = summarise
    rw.chunk_table = chunk_table

    specs, shown = [], []
    silence = (f"silence {settings['silence_section']}/{settings['silence_sentence']}/"
               f"{settings['silence_comma']}")
    for method in METHODS:
        for style in STYLES:
            rows = samples[(method, style)]
            if method == "scale":
                scales = sorted({r["duration_scale"] for r in rows})
                what = f"scale recipe · {style} · x{scales[0]}–{scales[-1]}"
            else:
                what = f"pace recipe · {style} · {targets[style]} ch/s"
            specs.append({"nickname": sweep.nickname_for(speaker),
                          "summary": f"{what}  ·  {silence}  ·  trim tail on  ·  "
                                     f"fixed seed per sentence"})
            result = dict(results[(method, style)])
            result["chunks"] = [{"index": r["piece"], "gap": r["gap"] or "-", "chars": r["chars"],
                                 "request": f"x{r['duration_scale']}" if method == "scale"
                                 else f"{r['seconds']}s",
                                 "pace": r.get("pace", ""), "display_text": r["text"]}
                                for r in rows]
            result["seconds"] = result.get("seconds", 0)
            shown.append(result)

    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    root.withdraw()
    window_ = rw.ResultsWindow(root)
    window_.title("Listening Test")
    window_.on_close = lambda: (rw.stop_playback(), root.destroy())
    window_.protocol("WM_DELETE_WINDOW", window_.on_close)
    text = "".join(s["text"] for s in window)
    window_.show(f"{book} · {chapter} #{window[0]['index']}–#{window[-1]['index']}\n\n{text}",
                 "e2e", specs, shown)
    root.mainloop()


# ------------------------------------------------------------ main

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Book profiler, stage 5: listening test")
    parser.add_argument("--book", required=True)
    parser.add_argument("--scope", default="book")
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--profile", default="book",
                        help="'book' or a chapter name - which profile_*.json to listen to")
    parser.add_argument("--window-only", action="store_true")
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    speaker = os.path.abspath(args.speaker)
    base = os.path.join(settings["work_root"], args.book, args.scope)
    recipe_dir = os.path.join(base, "recipe", sweep.nickname_for(speaker))
    try:
        profile = dynamic_profile.load_profile(
            os.path.join(recipe_dir, f"profile_{args.profile}.json"))
    except dynamic_profile.ProfileError as e:
        raise SystemExit(f"profile: {e}")
    with open(os.path.join(base, "analysis", "analysis.json"), encoding="utf-8") as f:
        analysis = json.load(f)
    settings.update({"silence_section": profile["silence"]["section"],
                     "silence_sentence": profile["silence"]["sentence"],
                     "silence_comma": profile["silence"]["comma"],
                     "trim_tail": profile["trim_tail"]})

    targets = profile["pace_targets"]

    normalize, _p = dynamic_profile.load_irodori_normalizer(settings["irodori_root"])
    engine = dynamic_profile.make_engine(normalize)
    chapter, window = choose_passage(analysis, profile["chapters"], profile["bands"], settings)
    samples = sample_jobs(window, profile, engine, settings)
    pieces = samples[("scale", "default")]

    root = os.path.join(base, "listen", sweep.nickname_for(speaker), args.profile)
    os.makedirs(root, exist_ok=True)

    def log(message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(os.path.join(root, "listen.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"passage {chapter} #{window[0]['index']}-#{window[-1]['index']}: {len(pieces)} piece(s); "
        f"pace targets {targets}")
    if not args.window_only:
        render_missing(samples, speaker, settings, root, log)
    results = stitch(samples, speaker, settings, root, log)
    with open(os.path.join(root, "listen.json"), "w", encoding="utf-8") as f:
        json.dump({"book": args.book, "profile": args.profile, "chapter": chapter,
                   "sentences": [s["index"] for s in window], "pace_targets": targets,
                   "samples": {f"{m}_{s}": {"result": results[(m, s)], "pieces": samples[(m, s)]}
                               for (m, s) in samples}},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(root, "listen.md"), "w", encoding="utf-8") as f:
        f.write(render_md(args.book, speaker, chapter, window, samples, results, targets))
    log(f"written to {root}")
    if not args.no_window:
        open_window(args.book, speaker, chapter, window, samples, results, targets, settings)


if __name__ == "__main__":
    main()
