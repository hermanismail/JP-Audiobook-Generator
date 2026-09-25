"""
library.py
----------
The onboarder's door to the Audiobook Creation Suite library
(`F:\\AUDIOBOOK-CREATION-SUITE`, its own repo and SQLite file).

The folder rule for this tool is that it shares no code with its
neighbours - own venv, own settings, imports nothing from the generator -
so that it can be lifted out whole. The library does not break that rule:
`creator_suite.client` is stdlib-only and is the common door every tool
goes through, so this loads it BY PATH the way the generator's
`suite_link.py` does, rather than importing anything of the generator's.

Everything here is best-effort and returns None when the library is
absent - except where the window deliberately blocks (see app.py): a
voice trained without a row is a voice nothing downstream can name, and
naming it here is the whole point (user decision 2026-09-25).
"""

import importlib.util
import os
import sys

DEFAULT_ROOT = r"F:\AUDIOBOOK-CREATION-SUITE"
_CLIENT = None
_ERROR = ""


def root(settings=None):
    return (settings or {}).get("suite_root") or DEFAULT_ROOT


def load_client(settings=None):
    """`creator_suite.client`, loaded from the library's own checkout."""
    global _CLIENT, _ERROR
    if _CLIENT is not None:
        return _CLIENT
    folder = root(settings)
    init = os.path.join(folder, "creator_suite", "__init__.py")
    if not os.path.isfile(init):
        _ERROR = f"no creator_suite package under {folder}"
        return None
    try:
        if folder not in sys.path:
            sys.path.insert(0, folder)
        spec = importlib.util.spec_from_file_location(
            "creator_suite.client", os.path.join(folder, "creator_suite", "client.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLIENT = module
        return module
    except Exception as e:
        _ERROR = f"{type(e).__name__}: {e}"
        return None


def error():
    return _ERROR or "not connected"


def open_library(settings=None):
    """A Suite, or None. The caller decides what to do about None."""
    client = load_client(settings)
    if client is None:
        return None
    try:
        return client.Suite(os.path.join(root(settings), "creator.db"))
    except Exception as e:
        global _ERROR
        _ERROR = f"{type(e).__name__}: {e}"
        return None


def known_voice(settings, speaker_path):
    """The row for this exact speaker file, or None."""
    suite = open_library(settings)
    if suite is None:
        return None
    try:
        return suite.seiyuu_by_path(speaker_path)
    except Exception:
        return None
    finally:
        suite.close()


def names_for_speaker(settings, speaker_key):
    """The names already given to ANY style of this speaker - what fills
    the form in when a second style is onboarded (user decision
    2026-09-25: the parent folder is the identity).

    Returns (names, style it came from) or (None, None)."""
    suite = open_library(settings)
    if suite is None:
        return None, None
    try:
        for row in suite.seiyuu_by_key(speaker_key):
            if row.get("display_name") and row.get("reading_kana"):
                return ({"display_name": row["display_name"],
                         "reading_kana": row["reading_kana"],
                         "translation_name": row.get("translation_name") or ""},
                        row.get("style") or row.get("nickname"))
    except Exception:
        return None, None
    finally:
        suite.close()
    return None, None


def record_voice(settings, speaker_path, names, speaker_key=None, style=None,
                 samples_folder=None, trained=True):
    """Write the voice into the library after training published it.

    `onboarded_at` is stamped when the speaker file reaches
    `seiyuu/list/` - not when the form was filled - because that is when
    the voice exists. Returns (row, what) or (None, why)."""
    suite = open_library(settings)
    if suite is None:
        return None, error()
    try:
        client = load_client(settings)
        now = client._db.now() if hasattr(client, "_db") else None
        row, what = suite.seiyuu_upsert(
            speaker_path,
            display_name=names.get("display_name"),
            reading_kana=names.get("reading_kana"),
            translation_name=names.get("translation_name"),
            speaker_key=speaker_key, style=style, samples_folder=samples_folder,
            onboarded_at=now, trained_at=now if trained else None)
        return row, what
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        suite.close()
