"""
mp3_metadata.py
----------------
Writes ID3v2 tags to the generated chapter MP3s so Spotify (desktop and,
importantly for this project, the Android app's Local Files view) groups
them together as a single album, in chapter order.

This deliberately runs inside the lightweight GUI-side uv venv
(C:\\JP-Audiobook-Generator), NOT the heavy Irodori-TTS venv used for actual
TTS generation - mutagen has zero external dependencies, so it's a cheap
addition here and keeps the two environments' concerns separated, matching
the rest of this project's setup.

Invocation:
    - From the GUI (gui_settings.py), via the "Apply Tags to Output MP3s"
      button - calls apply_metadata() directly, in-process.
    - Automatically at the end of the generation pipeline, when "Auto-tag
      generated files" is ON: run_audiobook.py (running in the *Irodori-TTS*
      venv) shells out to `uv run --project <this folder> python
      mp3_metadata.py`, which re-reads settings.json itself and runs the
      same apply_metadata() logic - keeping mutagen out of the Irodori-TTS
      venv entirely. See the __main__ block at the bottom.

Chapter <-> MP3 matching mirrors run_audiobook.py exactly: chapter text
files are looked up as `chapter_*.txt` under input_folder, sorted
alphabetically, and each one's MP3 is expected at
output_folder/<same base name>.mp3.

Tags written per chapter MP3 (only when the corresponding field is
non-empty):
    TPE1 (Artist)         <- author_name
    TPE2 (Album Artist)   <- author_name   (this is the important one for
                                             reliable Spotify album grouping)
    TALB (Album)          <- book_title    (identical across every chapter)
    TCON (Genre)          <- genre
    TIT2 (Title)          <- humanized chapter filename, e.g. "Chapter 001"
    TRCK (Track number)   <- parsed from the chapter's own file name (the
                              last run of digits in it - e.g. chapter_045
                              -> track 45), only if auto_number_chapters is
                              enabled. Files with no digits in the name are
                              left without a TRCK tag rather than guessed at.
    APIC (Cover art)      <- cover_art_path, embedded on every chapter
"""

import os
import re
import glob
import json

from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TPE2, TALB, TCON, TRCK, TIT2, APIC

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class MetadataApplyResult:
    """Simple result bag returned by apply_metadata(), summarized by the
    GUI in a single message box after the run."""

    def __init__(self):
        self.tagged = []    # filenames successfully tagged
        self.missing = []   # chapter base names with no matching mp3 yet
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
    """'chapter_001' -> 'Chapter 001'. Used only for the TIT2 (track title)
    tag - doesn't affect file names or chapter matching."""
    return base_name.replace("_", " ").strip().title()


def _track_number_from_filename(base_name):
    """Extracts the track number FROM the chapter's own file name, e.g.
    'chapter_045' -> 45, 'Chapter_12_final' -> 12 (last run of digits wins).
    Returns None if the name has no digits at all, in which case the
    caller leaves TRCK unset rather than guessing."""
    matches = re.findall(r"\d+", base_name)
    if not matches:
        return None
    return int(matches[-1])


def find_chapter_base_names(input_folder):
    """Returns the sorted list of base names (no extension) for every
    chapter_*.txt in input_folder - independent of whether the matching
    MP3 exists yet, so callers can report missing ones."""
    chapter_files = sorted(glob.glob(os.path.join(input_folder, "chapter_*.txt")))
    return [os.path.splitext(os.path.basename(f))[0] for f in chapter_files]


def apply_metadata(settings, metadata):
    """
    settings: the loaded settings.json dict (needs input_folder, output_folder)
    metadata: dict with keys author_name, book_title, genre,
              auto_number_chapters, cover_art_path

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

    for base_name in find_chapter_base_names(input_folder):
        mp3_path = os.path.join(output_folder, f"{base_name}.mp3")

        if not os.path.exists(mp3_path):
            result.missing.append(base_name)
            continue

        try:
            try:
                tags = ID3(mp3_path)
            except ID3NoHeaderError:
                tags = ID3()

            if author_name:
                tags.setall("TPE1", [TPE1(encoding=3, text=author_name)])
                tags.setall("TPE2", [TPE2(encoding=3, text=author_name)])
            if book_title:
                tags.setall("TALB", [TALB(encoding=3, text=book_title)])
            if genre:
                tags.setall("TCON", [TCON(encoding=3, text=genre)])
            tags.setall("TIT2", [TIT2(encoding=3, text=_humanize_title(base_name))])
            if auto_number:
                track_number = _track_number_from_filename(base_name)
                if track_number is not None:
                    tags.setall("TRCK", [TRCK(encoding=3, text=str(track_number))])
            if cover_bytes:
                tags.setall("APIC", [APIC(
                    encoding=3, mime=cover_mime, type=3, desc="Cover",
                    data=cover_bytes)])

            tags.save(mp3_path)
            result.tagged.append(os.path.basename(mp3_path))
        except Exception as e:
            result.errors.append((os.path.basename(mp3_path), str(e)))

    return result


if __name__ == "__main__":
    # CLI entrypoint used by run_audiobook.py's auto-tag step (see module
    # docstring). Reads settings.json itself so no arguments are needed -
    # run_audiobook.py just calls `uv run --project <here> python
    # mp3_metadata.py` after generation finishes.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _settings_path = os.path.join(_script_dir, "settings.json")

    with open(_settings_path, "r", encoding="utf-8") as _f:
        _settings = json.load(_f)

    _result = apply_metadata(_settings, _settings)

    print(f"Tagged {_result.tagged_count} MP3 file(s).")
    if _result.missing:
        print(f"{len(_result.missing)} chapter(s) had no MP3 in the output folder yet: "
              + ", ".join(_result.missing))
    if _result.errors:
        print(f"{len(_result.errors)} file(s) failed to tag:")
        for _name, _err in _result.errors:
            print(f"  - {_name}: {_err}")
