"""
dynamic_profile.py
------------------
Everything dynamic profile mode needs to turn a book-profiler profile into
TTS requests - shared, so the generator, its GUI and book-profiler can never
disagree about it. Stdlib only: it is imported from the Irodori venv
(run_audiobook.py), the GUI venv (gui_settings.py) and book-profiler's venv.

## A profile (version 2)

Written by book-profiler/recipe.py, one per chapter and one for the book:

    {
      "version": 2, "book": ..., "scope": "book" | "chapter", "chapters": [...],
      "speaker_path": ..., "speaker_stamp": [size, mtime],
      "trim_tail": true,
      "silence": {"section": 1.5, "sentence": 1.0, "comma": 0.7},
      "comfortable_length": 116,           # engine characters
      "bands": [{"from_len", "to_len", "faster", "default", "slower"}, ...],
      "pace_targets": {"default": 5.01, "slower": 4.46, "faster": 5.72},
      ...
    }

## Six styles

    scale_default / scale_slower / scale_faster   "Natural"    duration_scale from the band
    pace_default  / pace_slower  / pace_faster    "Even pace"  seconds = engine chars / target

Both kept because a listening test (yojo-senki x tanya, 2026-09-17) found
the scale recipe more natural and the pace recipe too even - on one book
and one seiyuu. Other pairings may differ, so every profile carries both.

## Lengths are ENGINE lengths

A band is chosen by how many characters the engine actually receives:
text_pipeline.prepare_tts_text_dynamic() and then Irodori's own
normalize_text(). That is what book-profiler measured the bands in, so
measuring anything else here would put sentences in the wrong band.
Irodori's normaliser is loaded by FILE PATH because irodori_tts/__init__.py
imports torch, and the GUI venv has no torch.
"""

import importlib.util
import json
import os

import text_pipeline as tp

PROFILE_VERSION = 2

STYLE_KEYS = ("scale_default", "scale_slower", "scale_faster",
              "pace_default", "pace_slower", "pace_faster")
STYLE_LABELS = {
    "scale_default": "Natural — default",
    "scale_slower": "Natural — slower",
    "scale_faster": "Natural — faster",
    "pace_default": "Even pace — default",
    "pace_slower": "Even pace — slower",
    "pace_faster": "Even pace — faster",
}
DEFAULT_STYLE = "scale_default"

SILENCE_KINDS = ("section", "sentence", "comma")


class ProfileError(Exception):
    """A profile that cannot be used. The message is written for a person."""


def split_style(style_key):
    method, _, style = style_key.partition("_")
    if style_key not in STYLE_KEYS:
        raise ProfileError(f"unknown style '{style_key}'")
    return method, style


# ------------------------------------------------------------ engine length

def load_irodori_normalizer(irodori_root):
    """(normalize_text, path) - or (None, path) when the file is missing."""
    path = os.path.join(irodori_root, "irodori_tts", "text_normalization.py")
    if not os.path.isfile(path):
        return None, path
    spec = importlib.util.spec_from_file_location("irodori_text_normalization", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_text, path


def make_engine(normalize):
    """text -> the string the engine receives when that text is sent alone."""
    def engine(text):
        piped = tp.prepare_tts_text_dynamic(text)
        return normalize(piped).strip() if normalize else piped
    return engine


# ------------------------------------------------------------ profiles

def speaker_stamp(path):
    try:
        info = os.stat(path)
        return [info.st_size, int(info.st_mtime)]
    except OSError:
        return None


def load_profile(path):
    """The profile at `path`, checked. Raises ProfileError."""
    if not path or not os.path.isfile(path):
        raise ProfileError(f"profile not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, ValueError) as e:
        raise ProfileError(f"not a readable profile ({e})")
    version = profile.get("version")
    if version != PROFILE_VERSION:
        raise ProfileError(f"profile version {version} - this generator needs version "
                           f"{PROFILE_VERSION}. Re-run book-profiler's recipe.py to rewrite it.")
    for key in ("speaker_path", "silence", "comfortable_length", "bands", "pace_targets"):
        if key not in profile:
            raise ProfileError(f"profile is missing '{key}'")
    if not profile["bands"]:
        raise ProfileError("profile has no length bands")
    for kind in SILENCE_KINDS:
        if kind not in profile["silence"]:
            raise ProfileError(f"profile silence is missing '{kind}'")
    for style in ("default", "slower", "faster"):
        if not profile["pace_targets"].get(style):
            raise ProfileError(f"profile has no pace target for '{style}'")
        for band in profile["bands"]:
            if band.get(style) is None:
                raise ProfileError(f"band {band.get('from_len')}-{band.get('to_len')} "
                                   f"has no '{style}' scale")
    profile["_path"] = os.path.abspath(path)
    return profile


def speaker_status(profile):
    """(ok, message). ok is False only when the speaker file is missing -
    a changed file is a warning: the voice may differ from what was
    measured, which is the person's call."""
    path = profile["speaker_path"]
    stamp = speaker_stamp(path)
    if stamp is None:
        return False, f"speaker file not found: {path}"
    if profile.get("speaker_stamp") and stamp != profile["speaker_stamp"]:
        return True, ("speaker file changed since profiling (retrained?) - "
                      "the recipe may not fit it any more")
    return True, "speaker file unchanged since profiling"


def nickname(profile):
    name = os.path.basename(profile["speaker_path"])
    suffix = ".speaker.safetensors"
    return name[:-len(suffix)] if name.endswith(suffix) else os.path.splitext(name)[0]


def band_for(profile, length):
    """(band, beyond) for a request of `length` engine characters: the next
    longer measured band, or the longest one - beyond=True - past them all."""
    for band in profile["bands"]:
        if length <= band["to_len"]:
            return band, False
    return profile["bands"][-1], True


def request_for(profile, style_key, length, ceiling=29.5):
    """The TTS parameters for one request: {"duration_scale": x} for a
    Natural style, {"seconds": s} for an Even pace style."""
    method, style = split_style(style_key)
    if method == "scale":
        band, _beyond = band_for(profile, length)
        return {"duration_scale": band[style]}
    seconds = length / float(profile["pace_targets"][style])
    return {"seconds": round(min(ceiling, max(0.6, seconds)), 2)}


def summary_lines(profile, style_key=None):
    """A short, human description of a profile - what the GUI shows under
    the Profile Path to confirm the recipe is usable."""
    ok, speaker_note = speaker_status(profile)
    chapters = profile.get("chapters") or []
    lines = [
        f"{profile.get('book', '?')} · seiyuu {nickname(profile)} · "
        f"{profile.get('scope', '?')} profile of {len(chapters)} chapter(s)",
        f"comfortable length {profile['comfortable_length']} chars · silences "
        f"{profile['silence']['section']}/{profile['silence']['sentence']}/"
        f"{profile['silence']['comma']} s · trim tail "
        f"{'on' if profile.get('trim_tail') else 'off'}",
        "Natural: " + "  ".join(
            f"{b['from_len']}-{b['to_len']} x{b['faster']}/{b['default']}/{b['slower']}"
            for b in profile["bands"]) + "  (faster/default/slower)",
        f"Even pace: {profile['pace_targets']['faster']} / {profile['pace_targets']['default']} / "
        f"{profile['pace_targets']['slower']} ch/s  (faster/default/slower)",
        ("OK · " if ok else "NOT USABLE · ") + speaker_note,
    ]
    if style_key:
        lines.append(f"style: {STYLE_LABELS.get(style_key, style_key)}")
    return lines


# ------------------------------------------------------------ a chapter

def plan_pieces(sentences, profile, style_key, engine, first_gap="section"):
    """The requests for a run of sentences.

    `sentences` is [(text, gap_before)] - gap_before being
    text_pipeline.dynamic_sentences()'s "chapter_start" / "section" /
    "sentence". A sentence longer than the profile's comfortable length is
    cut with text_pipeline.split_for_length(), each cut earning its own
    "comma" or "sentence" silence.

    Returns (pieces, skipped):

        pieces   [{"sentence", "piece", "display_text", "tts_text",
                   "engine_len", "gap", "band", "beyond", "request"}], gap
                 being the silence kind BEFORE the piece - one of
                 SILENCE_KINDS. The first piece gets `first_gap` (section,
                 by decision 2026-09-17).
        skipped  display texts that leave nothing for the engine - a line
                 of "×××" once × is stripped. Normal mode drops these too
                 (build_chunks skips empty TTS text); here they are
                 returned so the caller can record them."""
    measure = lambda text: len(engine(text))
    limit = profile["comfortable_length"]
    out, skipped = [], []
    for number, item in enumerate(sentences, start=1):
        # (text, gap_before) or (text, gap_before, removed_before, removed_after)
        text, gap_before = item[0], item[1]
        removed_before = item[2] if len(item) > 2 else ""
        removed_after = item[3] if len(item) > 3 else ""
        if not engine(text) or tp.PUNCT_ONLY_RE.fullmatch(engine(text)):
            skipped.append(text)
            continue
        if gap_before in (None, "chapter_start"):
            gap = first_gap if not out else "sentence"
        else:
            gap = "section" if gap_before == "section" else "sentence"
        length = measure(text)
        cuts = tp.split_for_length(text, limit, measure) if length > limit else [(text, None)]
        for piece_number, (piece, cut_kind) in enumerate(cuts, start=1):
            piece_len = measure(piece)
            band, beyond = band_for(profile, piece_len)
            out.append({
                "sentence": number,
                "piece": piece_number,
                "display_text": piece,
                "tts_text": tp.prepare_tts_text_dynamic(piece),
                "engine_len": piece_len,
                "gap": gap if piece_number == 1 else cut_kind,
                "band": f"{band['from_len']}-{band['to_len']}",
                "beyond": beyond,
                "request": request_for(profile, style_key, piece_len),
                # The 、 text_pipeline dropped at this sentence's edges (see
                # LEADING_MARKS_RE) - recorded, because the reader text no
                # longer matches the book there.
                "removed_before": removed_before if piece_number == 1 else "",
                "removed_after": removed_after if piece_number == len(cuts) else "",
            })
    return out, skipped


def plan_chapter(raw_text, profile, style_key, engine):
    """plan_pieces() over a whole chapter's text. Returns (pieces, skipped)."""
    units = tp.dynamic_sentences(raw_text)
    return plan_pieces([(u["text"], u["gap_before"], u["removed_before"], u["removed_after"])
                        for u in units], profile, style_key, engine)
