"""
translate_pipeline.py
---------------------
Generates an English subtitle track for each generated chapter, so the
player can show it alongside the Japanese reading text.

The approach, and why it is this way round: translation runs over the
*finished* `<chapter>.sync.json`, never over the raw text. By the time
sync.json exists the chunk boundaries are already fixed by the rendered
MP3, so the translation cannot desync - its timing is correct by
construction rather than by any alignment pass. English is emitted as a
separate sidecar and never written back into sync.json, which keeps the
change additive for everything downstream.

    read  <base>.sync.json          (never modified)
      -> translate chunk by chunk
      -> write <base>.translation.json    the artifact / source of truth
      -> write <base>.srt                 cheap re-emission for the player

**The invariant that makes this work: a chunk's index is sacred.** Text is
only ever filled in per existing index - chunks are never merged, split or
re-timed. Everything here is built so that stays true structurally: the
translation loop walks sync.json's own chunk list and zips results back
positionally, so a backend cannot renumber anything even if it wanted to.

Deliberately stdlib-only, so it runs unchanged in either venv - the
lightweight GUI one or the heavy Irodori-TTS one. The eventual local-LLM
backend talks HTTP to localhost, which `urllib` already covers, so that
stays true.

Invocation:
    - Standalone, over everything generated so far:
        uv run --project <this folder> python translate_pipeline.py --all
    - One chapter:
        uv run --project <this folder> python translate_pipeline.py --chapter chapter_002
    - Re-emit the .srt from an existing .translation.json without
      re-translating (e.g. after fixing a line by hand):
        uv run --project <this folder> python translate_pipeline.py --all --srt-only

Reads settings.json itself for `output_folder`, the same way
mp3_metadata.py does, so no arguments beyond the above are needed.
"""

import os
import re
import glob
import json
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")

# Bumped only when the on-disk shape of a .translation.json changes in a way
# a reader would have to care about.
TRANSLATION_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# The backend seam
# ---------------------------------------------------------------------------
# Every backend is a function taking the chapter's Japanese strings in
# order and returning the same number of English strings, in the same
# order. That signature is the entire contract, and it is what keeps
# local-now / API-later a one-line change.
#
# It also quietly enforces the index invariant: the caller zips the result
# back onto sync.json's own chunk list positionally, so a backend has no
# way to renumber, merge or drop a chunk. A backend that returns the wrong
# count is rejected outright rather than silently shifting every subtitle
# after the mistake.


def translate_identity(japanese_texts, **_kwargs):
    """Passthrough 'translation' - hands the Japanese straight back.

    This is not a placeholder to be deleted. It is how the artifact
    format, the SRT emitter and the player round-trip get validated
    without a model in the loop at all, and it stays useful afterwards as
    the control case when a real backend starts producing something odd."""
    return list(japanese_texts)


BACKENDS = {
    "identity": translate_identity,
}


class TranslationResult:
    """What generate_subtitles() did, for the caller to report on."""

    def __init__(self):
        self.written = []       # base names that got a fresh translation
        self.srt_only = []      # base names re-emitted from existing json
        self.missing = []       # base names with no .sync.json
        self.errors = []        # (base_name, message)

    @property
    def total(self):
        return len(self.written) + len(self.srt_only)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_chapter_bases(output_folder):
    """Every chapter that has been rendered far enough to translate, i.e.
    has a sync.json next to its MP3. Sorted so chapter_002 follows
    chapter_001, which matters once rolling context between chapters is
    added - a later chapter should be able to see what came before it."""
    pattern = os.path.join(output_folder, "*.sync.json")
    bases = [os.path.basename(p)[: -len(".sync.json")] for p in glob.glob(pattern)]
    return sorted(bases)


def sync_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.sync.json")


def translation_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.translation.json")


def srt_path_for(output_folder, base_name):
    return os.path.join(output_folder, f"{base_name}.srt")


# ---------------------------------------------------------------------------
# SRT emission
# ---------------------------------------------------------------------------

def format_timestamp(seconds):
    """SRT wants HH:MM:SS,mmm - comma before the milliseconds, not a dot,
    and hours always present even for a short chapter. Chapter 2 of the
    reference book runs past two hours, so the hour field genuinely gets
    used rather than being decorative."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(doc):
    """One cue per chunk, which is what the player already handles - its
    splitCueBySentence() breaks a multi-sentence cue up on its own and
    shares the cue window out by length, so nothing here needs to.

    Cue numbers are 1-based per the SRT convention while chunk indices are
    0-based; the chunk's own index travels in the .translation.json, so
    nothing downstream has to infer one from the other."""
    blocks = []
    for cue_number, chunk in enumerate(doc["chunks"], start=1):
        start = float(chunk["start"])
        end = float(chunk["end"])
        # A zero-or-negative-length cue is silently dropped by some players
        # rather than reported, so give it a floor instead of emitting
        # something that would just vanish.
        if end <= start:
            end = start + 0.001
        text = (chunk.get("en") or "").strip()
        if not text:
            # Keep the cue rather than renumbering everything after it -
            # an empty subtitle is a visible gap, a missing one is a
            # silent desync. Blank cues are why this uses a placeholder.
            text = "​"  # zero-width space: renders as nothing
        blocks.append(
            f"{cue_number}\n"
            f"{format_timestamp(start)} --> {format_timestamp(end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def write_srt(path, doc):
    """UTF-8 with no BOM and LF line endings, written deterministically.

    newline="" stops Windows turning every \\n into \\r\\n behind our back,
    so the bytes on disk are the same wherever this runs. Python's "utf-8"
    codec writes no BOM (that would be "utf-8-sig"), which is what the
    player's parser expects - a BOM would end up glued to the first cue
    number and break it."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(build_srt(doc))


# ---------------------------------------------------------------------------
# The translation artifact
# ---------------------------------------------------------------------------

def build_translation_doc(base_name, sync_data, english_texts, backend_name,
                          model_name=None):
    """Pairs each sync.json chunk with its English text.

    Timings are copied in rather than referenced so the file is
    self-contained: --srt-only can re-emit a subtitle from this alone,
    without sync.json needing to still be around or still agree. The
    trade-off is that these timings go stale if the chapter is ever
    re-rendered, which is why `source_sync` records what they came from and
    a full (non --srt-only) run always re-reads sync.json."""
    chunks = sync_data["chunks"]
    if len(english_texts) != len(chunks):
        raise ValueError(
            f"backend returned {len(english_texts)} line(s) for "
            f"{len(chunks)} chunk(s) - refusing to write a translation "
            f"that would shift every subtitle after the mismatch")

    return {
        "version": TRANSLATION_FORMAT_VERSION,
        "chapter": base_name,
        "source_sync": f"{base_name}.sync.json",
        "source_version": sync_data.get("version"),
        "backend": backend_name,
        "model": model_name,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "chunks": [
            {
                "index": chunk["index"],
                "start": chunk["start"],
                "end": chunk["end"],
                "ja": chunk["text"],
                "en": english,
            }
            for chunk, english in zip(chunks, english_texts)
        ],
    }


def write_translation(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def read_translation(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_subtitles(settings, base_names=None, backend="identity",
                       srt_only=False, model_name=None, verbose=True):
    """Translates the named chapters (or every rendered one) and writes a
    .translation.json plus a .srt for each, into output_folder alongside
    the MP3 and sync.json.

    sync.json is only ever read. Nothing in this module opens it for
    writing, and the English text never goes near its `text` field."""
    output_folder = settings["output_folder"]
    result = TranslationResult()

    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r} - available: {', '.join(sorted(BACKENDS))}")
    translate = BACKENDS[backend]

    if base_names is None:
        base_names = find_chapter_bases(output_folder)

    for base_name in base_names:
        try:
            if srt_only:
                json_path = translation_path_for(output_folder, base_name)
                if not os.path.exists(json_path):
                    result.missing.append(base_name)
                    continue
                doc = read_translation(json_path)
                write_srt(srt_path_for(output_folder, base_name), doc)
                result.srt_only.append(base_name)
                if verbose:
                    print(f"  {base_name}: re-emitted {len(doc['chunks'])} cue(s) from "
                          f"existing translation")
                continue

            sync_file = sync_path_for(output_folder, base_name)
            if not os.path.exists(sync_file):
                result.missing.append(base_name)
                continue

            with open(sync_file, "r", encoding="utf-8") as f:
                sync_data = json.load(f)

            chunks = sync_data.get("chunks", [])
            if not chunks:
                result.errors.append((base_name, "sync.json contains no chunks"))
                continue

            japanese = [c["text"] for c in chunks]
            english = translate(japanese, base_name=base_name, settings=settings)

            doc = build_translation_doc(base_name, sync_data, english, backend,
                                        model_name=model_name)
            write_translation(translation_path_for(output_folder, base_name), doc)
            write_srt(srt_path_for(output_folder, base_name), doc)
            result.written.append(base_name)
            if verbose:
                print(f"  {base_name}: {len(chunks)} chunk(s) -> "
                      f"{base_name}.translation.json + {base_name}.srt")

        except Exception as e:  # one bad chapter shouldn't abandon the rest
            result.errors.append((base_name, str(e)))

    return result


if __name__ == "__main__":
    # Mirrors mp3_metadata.py's entry point: re-reads settings.json itself
    # so the GUI (or run_audiobook.py at the end of a run) can shell out to
    # `uv run --project <here> python translate_pipeline.py --all` without
    # having to pass any configuration through.
    import argparse

    _parser = argparse.ArgumentParser(
        description="Generate English subtitle tracks from generated chapters.")
    _group = _parser.add_mutually_exclusive_group(required=True)
    _group.add_argument("--chapter", default=None,
                        help="Chapter base name (e.g. chapter_002) to translate.")
    _group.add_argument("--all", action="store_true",
                        help="Translate every chapter that has a .sync.json.")
    _parser.add_argument("--backend", default="identity",
                         choices=sorted(BACKENDS),
                         help="Translation backend (default: identity, which "
                              "passes the Japanese through unchanged).")
    _parser.add_argument("--srt-only", action="store_true",
                         help="Re-emit each .srt from its existing "
                              ".translation.json without translating again.")
    _args = _parser.parse_args()

    with open(SETTINGS_PATH, "r", encoding="utf-8") as _f:
        _settings = json.load(_f)

    _bases = [_args.chapter] if _args.chapter else None

    print(f"Output folder: {_settings['output_folder']}")
    print(f"Backend: {_args.backend}" + ("  (--srt-only)" if _args.srt_only else ""))

    _result = generate_subtitles(_settings, base_names=_bases,
                                 backend=_args.backend, srt_only=_args.srt_only)

    print(f"Wrote subtitles for {_result.total} chapter(s).")
    if _result.missing:
        _what = "translation" if _args.srt_only else "sync.json"
        print(f"{len(_result.missing)} chapter(s) had no {_what} yet: "
              + ", ".join(_result.missing))
    if _result.errors:
        print(f"{len(_result.errors)} chapter(s) failed:")
        for _name, _err in _result.errors:
            print(f"  - {_name}: {_err}")
