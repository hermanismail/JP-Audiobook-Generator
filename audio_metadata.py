"""
audio_metadata.py
-----------------
Writes tags to the generated chapter files so the player - and Spotify
(desktop and, importantly for this project, the Android app's Local Files
view) - groups them together as a single album, in chapter order.

Two containers, one set of tags:
    - .m4a, which run_audiobook.py writes now: MP4 atoms. The player reads
      these with music-metadata, which returns the MP4 title/artist/album
      atoms and the `covr` cover exactly as it returned ID3, so nothing on
      the player side had to change.
    - .mp3, from books generated before the switch to AAC: ID3v2, as before,
      so "Apply Tags" still works on an old folder.
A chapter that has both (a regenerated chapter can leave its old .mp3
behind) gets both tagged.

This deliberately runs inside the lightweight GUI-side uv venv
(C:\\JP-Audiobook-Generator), NOT the heavy Irodori-TTS venv used for actual
TTS generation - mutagen has zero external dependencies, so it's a cheap
addition here and keeps the two environments' concerns separated, matching
the rest of this project's setup.

Invocation:
    - From the GUI (gui_settings.py), via the "Apply Tags to Output Files"
      button - calls apply_metadata() directly, in-process.
    - Automatically after each chapter finishes rendering, when "Auto-tag
      generated files" is ON: run_audiobook.py (running in the *Irodori-TTS*
      venv) shells out to `uv run --project <this folder> python
      audio_metadata.py --chapter <base_name>`, which re-reads settings.json
      itself and runs the same apply_metadata() logic against just that one
      chapter - keeping mutagen out of the Irodori-TTS venv entirely, and
      making each chapter fully tagged (and usable on a phone) the moment it
      lands in the output folder, without waiting for the rest of the book.
      See the __main__ block at the bottom.

Chapter <-> file matching mirrors run_audiobook.py exactly: chapter text
files are looked up as `chapter_*.txt` under input_folder, sorted
alphabetically, and each one's audio is expected at
output_folder/<same base name>.m4a (or .mp3).

Tags written per chapter (only when the corresponding field is non-empty,
except the title, which is always written - the player lists chapters by
it, so a chapter without one is a regression, while a missing cover is
handled):
                          MP4     ID3
    Artist                ©ART    TPE1   <- author_name
    Album Artist          aART    TPE2   <- author_name   (the important one
                                                           for reliable
                                                           Spotify grouping)
    Album                 ©alb    TALB   <- book_title    (identical across
                                                           every chapter)
    Genre                 ©gen    TCON   <- genre
    Title                 ©nam    TIT2   <- humanized chapter filename, e.g.
                                            "Chapter 001"
    Track number          trkn    TRCK   <- parsed from the chapter's own
                                            file name (the last run of digits
                                            in it - e.g. chapter_045 -> track
                                            45), only if auto_number_chapters
                                            is enabled. Files with no digits
                                            in the name are left without one
                                            rather than guessed at.
    Cover art             covr    APIC   <- cover_art_path, embedded on every
                                            chapter

Tagging an .m4a keeps it faststart: mutagen grows the metadata inside the
moov atom in place and shifts mdat's chunk offsets to match, so moov stays
in front of mdat and the player can still stream it with Range requests.
"""

import os
import re
import glob
import json

from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TPE2, TALB, TCON, TRCK, TIT2, APIC
from mutagen.mp4 import MP4, MP4Cover

# Containers a chapter can be in, preferred first - matches
# run_audiobook.CHAPTER_AUDIO_EXTS.
AUDIO_EXTS = (".m4a", ".mp3")

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

_MP4_COVER_FORMAT = {
    "image/jpeg": MP4Cover.FORMAT_JPEG,
    "image/png": MP4Cover.FORMAT_PNG,
}


class MetadataApplyResult:
    """Simple result bag returned by apply_metadata(), summarized by the
    GUI in a single message box after the run."""

    def __init__(self):
        self.tagged = []    # filenames successfully tagged
        self.missing = []   # chapter base names with no audio file yet
        self.errors = []    # (filename, error message) pairs

    @property
    def tagged_count(self):
        return len(self.tagged)


def _load_cover_art_bytes(cover_art_path):
    """Returns (bytes, mime) or (None, None) if no usable cover art path
    was given. Unsupported extensions are silently skipped rather than
    raising, since cover art is optional."""
    if not cover_art_path or not os.path.isfile(cover_art_path):
        return None, None
    ext = os.path.splitext(cover_art_path)[1].lower()
    mime = _MIME_BY_EXT.get(ext)
    if mime is None:
        return None, None
    with open(cover_art_path, "rb") as f:
        return f.read(), mime


def _humanize_title(base_name):
    """'chapter_001' -> 'Chapter 001'. Used only for the title tag - doesn't
    affect file names or chapter matching."""
    return base_name.replace("_", " ").strip().title()


def _track_number_from_filename(base_name):
    """Extracts the track number FROM the chapter's own file name, e.g.
    'chapter_045' -> 45, 'Chapter_12_final' -> 12 (last run of digits wins).
    Returns None if the name has no digits at all, in which case the
    caller leaves the track number unset rather than guessing."""
    matches = re.findall(r"\d+", base_name)
    if not matches:
        return None
    return int(matches[-1])


def find_chapter_base_names(input_folder):
    """Returns the sorted list of base names (no extension) for every
    chapter_*.txt in input_folder - independent of whether the matching
    audio exists yet, so callers can report missing ones."""
    chapter_files = sorted(glob.glob(os.path.join(input_folder, "chapter_*.txt")))
    return [os.path.splitext(os.path.basename(f))[0] for f in chapter_files]


def _tag_mp4(path, fields):
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    if fields["author"]:
        tags["\xa9ART"] = [fields["author"]]
        tags["aART"] = [fields["author"]]
    if fields["album"]:
        tags["\xa9alb"] = [fields["album"]]
    if fields["genre"]:
        tags["\xa9gen"] = [fields["genre"]]
    tags["\xa9nam"] = [fields["title"]]
    if fields["track"] is not None:
        # (track, total). The total is left 0 = unknown: the chapter count
        # isn't final until the whole book is rendered, and auto-tagging runs
        # per chapter as each one lands.
        tags["trkn"] = [(fields["track"], 0)]
    if fields["cover_bytes"]:
        tags["covr"] = [MP4Cover(
            fields["cover_bytes"],
            imageformat=_MP4_COVER_FORMAT[fields["cover_mime"]])]

    audio.save()


def _tag_id3(path, fields):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    if fields["author"]:
        tags.setall("TPE1", [TPE1(encoding=3, text=fields["author"])])
        tags.setall("TPE2", [TPE2(encoding=3, text=fields["author"])])
    if fields["album"]:
        tags.setall("TALB", [TALB(encoding=3, text=fields["album"])])
    if fields["genre"]:
        tags.setall("TCON", [TCON(encoding=3, text=fields["genre"])])
    tags.setall("TIT2", [TIT2(encoding=3, text=fields["title"])])
    if fields["track"] is not None:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(fields["track"]))])
    if fields["cover_bytes"]:
        tags.setall("APIC", [APIC(
            encoding=3, mime=fields["cover_mime"], type=3, desc="Cover",
            data=fields["cover_bytes"])])

    tags.save(path)


_TAGGER_BY_EXT = {".m4a": _tag_mp4, ".mp3": _tag_id3}


def apply_metadata(settings, metadata, base_names=None):
    """
    settings: the loaded settings.json dict (needs input_folder, output_folder)
    metadata: dict with keys author_name, book_title, genre,
              auto_number_chapters, cover_art_path
    base_names: chapter base names to tag (e.g. ["chapter_003"]). Defaults to
        every chapter_*.txt under input_folder - pass a subset to tag just
        one freshly-rendered chapter instead of rescanning the whole book
        (see run_audiobook.py's per-chapter auto-tag call).

    Returns a MetadataApplyResult. Never raises for per-file problems -
    those are collected in result.errors so one bad file doesn't stop the
    rest of the book from being tagged.
    """
    input_folder = settings["input_folder"]
    output_folder = settings["output_folder"]

    author_name = (metadata.get("author_name") or "").strip()
    book_title = (metadata.get("book_title") or "").strip()
    genre = (metadata.get("genre") or "").strip()
    auto_number = bool(metadata.get("auto_number_chapters", True))
    cover_art_path = (metadata.get("cover_art_path") or "").strip()

    cover_bytes, cover_mime = _load_cover_art_bytes(cover_art_path)

    result = MetadataApplyResult()

    if base_names is None:
        base_names = find_chapter_base_names(input_folder)

    for base_name in base_names:
        paths = [os.path.join(output_folder, base_name + ext) for ext in AUDIO_EXTS]
        paths = [path for path in paths if os.path.exists(path)]

        if not paths:
            result.missing.append(base_name)
            continue

        fields = {
            "author": author_name,
            "album": book_title,
            "genre": genre,
            "title": _humanize_title(base_name),
            "track": _track_number_from_filename(base_name) if auto_number else None,
            "cover_bytes": cover_bytes,
            "cover_mime": cover_mime,
        }

        for path in paths:
            try:
                _TAGGER_BY_EXT[os.path.splitext(path)[1]](path, fields)
                result.tagged.append(os.path.basename(path))
            except Exception as e:
                result.errors.append((os.path.basename(path), str(e)))

    return result


if __name__ == "__main__":
    # CLI entrypoint used by run_audiobook.py's auto-tag step (see module
    # docstring). Reads settings.json itself so no arguments are needed -
    # run_audiobook.py calls `uv run --project <here> python
    # audio_metadata.py [--chapter <base_name>]` right after each chapter is
    # stitched, passing --chapter so only that chapter gets tagged instead of
    # rescanning/re-tagging the whole book every time.
    import argparse

    _parser = argparse.ArgumentParser()
    _parser.add_argument("--chapter", default=None,
                          help="Chapter base name (e.g. chapter_003) to tag "
                               "instead of every chapter in input_folder.")
    _args = _parser.parse_args()

    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _settings_path = os.path.join(_script_dir, "settings.json")

    with open(_settings_path, "r", encoding="utf-8") as _f:
        _settings = json.load(_f)

    _base_names = [_args.chapter] if _args.chapter else None
    _result = apply_metadata(_settings, _settings, base_names=_base_names)

    print(f"Tagged {_result.tagged_count} file(s): " + ", ".join(_result.tagged)
          if _result.tagged else "Tagged 0 file(s).")
    if _result.missing:
        print(f"{len(_result.missing)} chapter(s) had no audio in the output folder yet: "
              + ", ".join(_result.missing))
    if _result.errors:
        print(f"{len(_result.errors)} file(s) failed to tag:")
        for _name, _err in _result.errors:
            print(f"  - {_name}: {_err}")
        # Non-zero so run_audiobook.py's auto-tag step reports the failure
        # instead of treating a half-tagged chapter as done.
        raise SystemExit(1)
