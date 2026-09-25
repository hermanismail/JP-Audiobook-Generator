"""
suite_link.py
-------------
The generator's door to the Audiobook Creation Suite library
(`F:\\AUDIOBOOK-CREATION-SUITE`, its own repo and its own SQLite file).

Everything here is best-effort: if the library or its database is missing,
every call returns None and the generator behaves exactly as it did before.
A missing library must never stop a render.

## What "legacy" means (user decision 2026-09-24)

A book the database does not know - or one whose row says `legacy` - keeps
the pre-2026-09-24 behaviour everywhere: no seiyuu intro line, no furigana,
no database writes, `readings.json` read straight from the output folder as
before. That is what keeps every already-published book repairable with
today's `dynamic-repair`, which has not been taught the database.

## Readings while both sides exist

`sync_readings()` imports whatever the book's `readings.json` holds and the
database does not, then writes the database back over that file. So the
file stays the thing the render (and dynamic-repair) reads, and the
database is the library across books.

Stdlib only - this is imported from the Irodori venv.
"""

import os
import re
import sys

DEFAULT_SUITE_ROOT = r"F:\AUDIOBOOK-CREATION-SUITE"
_MODULE = None
_LOAD_ERROR = None


def suite_root(settings=None):
    value = (settings or {}).get("suite_root") or os.environ.get("CREATOR_SUITE_ROOT")
    return os.path.abspath(value or DEFAULT_SUITE_ROOT)


def load_client(settings=None):
    """The creator_suite.client module, or None. Imported BY PATH: the
    suite is a separate repo, not a package installed in this venv."""
    global _MODULE, _LOAD_ERROR
    if _MODULE is not None:
        return _MODULE
    root = suite_root(settings)
    if not os.path.isfile(os.path.join(root, "creator_suite", "client.py")):
        _LOAD_ERROR = f"no creator_suite package at {root}"
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from creator_suite import client  # noqa: WPS433 - deliberate late import
    except Exception as e:                # any import problem is "no library"
        _LOAD_ERROR = f"{type(e).__name__}: {e}"
        return None
    _MODULE = client
    return _MODULE


def load_error():
    return _LOAD_ERROR


def open_suite(settings=None):
    """A Suite, or None when the library or database is unavailable."""
    client = load_client(settings)
    if client is None:
        return None
    try:
        return client.Suite(os.path.join(suite_root(settings), "creator.db"))
    except Exception as e:
        global _LOAD_ERROR
        _LOAD_ERROR = f"{type(e).__name__}: {e}"
        return None


def book_for_output(suite, output_folder):
    """The book row for this output folder, or None (which means legacy)."""
    if suite is None or not output_folder:
        return None
    try:
        return suite.book_by_output_folder(output_folder)
    except Exception:
        return None


def uses_new_pipeline(book):
    """True only for a book the database knows AND marks `db`."""
    return bool(book) and str(book.get("pipeline")) == "db"


def describe(suite, book, settings=None):
    """One line for a log or a status label."""
    if suite is None:
        return f"library not connected ({load_error() or 'no database'}) - legacy behaviour"
    if book is None:
        return (f"library {suite.path} - this book is not in it, so it runs the legacy way "
                f"(no intro line, no furigana, no records)")
    return (f"library {suite.path} - book #{book['id']} {book['slug']} "
            f"({'new pipeline' if uses_new_pipeline(book) else 'legacy'})")


def seiyuu_for_profile(suite, profile):
    """The seiyuu row for a loaded profile's speaker file, or None."""
    if suite is None or not profile:
        return None
    try:
        return suite.seiyuu_by_path(profile["speaker_path"])
    except Exception:
        return None


def intro_line(seiyuu, template="朗読者：{name}"):
    """(display line, tts line) for a seiyuu row, or (None, None) when the
    row has no names yet. The reader gets the written name, the engine the
    kana - measured 2026-09-24: `：` is spoken as a short pause, not a
    word, so the same template serves both."""
    if not seiyuu:
        return None, None
    display, kana = seiyuu.get("display_name"), seiyuu.get("reading_kana")
    if not display or not kana:
        return None, None
    return template.format(name=display), template.format(name=kana)


def furigana_applied(suite, book, chapter=None):
    """{(word, reading)} the person has approved - 'book' and 'once' alike,
    because both are spoken where they are written. Rejected pairs are
    absent, so their parens are simply stripped.

    With `chapter`, a decision made while only other chapters were selected
    is left out: it was taken on evidence from those chapters (schema v2).
    An older library without the column answers the same as before."""
    if not uses_new_pipeline(book):
        return set()
    try:
        return suite.furigana_applied(book["id"], chapter)
    except AttributeError:          # a library older than schema v2
        pass
    except Exception:
        return set()
    try:
        rows = suite.conn.execute(
            "SELECT word, reading FROM furigana_decisions WHERE book_id = ?"
            " AND decision IN ('book', 'once')", (book["id"],))
        return {(r["word"], r["reading"]) for r in rows}
    except Exception:
        return set()


def readings_for(suite, book, chapter=None):
    """The book's readings as dicts, narrowed to `chapter` - a reading
    decided from two chapters does not apply to a third."""
    if suite is None or not uses_new_pipeline(book):
        return []
    try:
        return [dict(r) for r in suite.readings_for_book(book["id"], chapter=chapter)]
    except Exception:
        return []


def intro_readings(intro):
    """[{word, reading}] that turns the written credit into kana.

    Both the form written into the text AND its whitespace-collapsed form,
    because `split_paragraphs()` collapses runs of whitespace: a display
    name with a space in it (`早見 沙織`) reaches `plan_pieces` as
    `朗読者：早見沙織`, and a pair keyed to the spaced form never matched -
    the engine was sent the kanji. Found 2026-09-24 in the preview, on
    hayamin."""
    display, tts = intro if intro else (None, None)
    if not display or not tts:
        return []
    forms = {display, re.sub(r"\s+", "", display)}
    return [{"word": word, "reading": tts}
            for word in sorted(forms, key=len, reverse=True)]


def profile_used(suite, profile_path):
    """Stamp a profile as used by a render. Best-effort, like everything
    here: it is a statistic, not a step of the pipeline."""
    if suite is None or not profile_path:
        return False
    try:
        suite.profile_used(profile_path)
        return True
    except Exception:
        return False


def sync_readings(suite, book, output_folder):
    """Import anything only the file has, then write the DB back over it.
    Returns the import result, or None when there is nothing to sync."""
    if not uses_new_pipeline(book):
        return None
    path = os.path.join(output_folder, "readings.json")
    try:
        return suite.sync_readings_file(book["id"], path, book_name=book["slug"])
    except Exception as e:
        print(f"  ! could not sync readings with the library ({type(e).__name__}: {e}) - "
              f"the file is used as it is")
        return None


def merge_glossary_entry(output_folder, seiyuu):
    """Puts the seiyuu's name in the book's glossary.json so the intro line
    translates with the spelling you chose (user decision 2026-09-24).
    Hand edits are kept: an existing entry for that name is left alone."""
    import json
    if not seiyuu or not seiyuu.get("display_name") or not seiyuu.get("translation_name"):
        return False
    path = os.path.join(output_folder, "glossary.json")
    data = {"characters": [], "notes": []}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
    characters = data.setdefault("characters", [])
    if any(c.get("name") == seiyuu["display_name"] for c in characters):
        return False
    characters.append({"name": seiyuu["display_name"], "en": seiyuu["translation_name"],
                       "gender": "", "aliases": "", "note": "seiyuu (added by the generator)"})
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True
