"""
pipeline.py
-----------
Everything the Seiyuu Onboarding tool does, minus the window: path
derivation, transcript cleanup, metadata.csv, and the three commands it
shells out to (Whisper, prepare_manifest.py, train.py).

Stdlib only and free of Tk, so the rules below can be exercised without a
GPU or a display - which is the point, because the interesting part is the
text cleanup and the path arithmetic, not the widgets.

The whole tool rests on one idea: a speaker is identified by the pair
`<speaker>/<style>` (moeshi/calm-01, marinka/05-lolinka), and that pair
appears in every path along the way. Type it once, derive the rest:

    audio/<speaker>/<style>/                  wavs + metadata.csv   (input)
    data/<speaker>/<style>/<speaker>-<style>.jsonl  + latents/      (manifest)
    seiyuu/<speaker>/<style>/                 checkpoints           (training)
    seiyuu/list/<speaker>-<style>.speaker.safetensors               (the one
                                                                     you pick)

Nothing here imports from the audiobook generator in the folder above; the
two tools share a repository and nothing else.
"""

import csv
import os
import re
import shutil
import subprocess

DEFAULT_SETTINGS = {
    # Where Irodori-TTS lives. Every command runs with this as its working
    # directory, which is why the arguments below stay relative.
    "irodori_root": r"C:\Irodori-TTS",
    # Whisper is NOT installed here - the tool shells out to the existing
    # transcription venv rather than duplicating a large dependency.
    "whisper_exe": r"C:\Transcribe\.venv\Scripts\whisper.exe",
    "transcript_root": r"C:\Transcribe\output",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "Japanese",
    "device": "cuda",
    "train_config": "configs/train_v4_small_speaker_inversion.yaml",
    "init_checkpoint": "./model.safetensors",
}

FINAL_CHECKPOINT_NAME = "checkpoint_final.speaker.safetensors"


def merge_setting_defaults(loaded):
    """Fills in keys a settings file written before a feature existed does
    not have. Same habit as the generator's scripts next door."""
    merged = dict(DEFAULT_SETTINGS)
    merged.update(loaded or {})
    return merged


class SpeakerPaths:
    """Every path for one <speaker>/<style> pair.

    The `rel_*` values are what go on the command lines (relative to the
    Irodori root, matching how these commands are run by hand); the plain
    ones are absolute, for touching files directly."""

    def __init__(self, root, speaker, style):
        self.root = os.path.abspath(root)
        self.speaker = speaker.strip().strip("/\\")
        self.style = style.strip().strip("/\\")

    @property
    def name(self):
        """`moeshi-calm-01` - unique on its own, which is what makes the
        flat list in seiyuu/list/ possible."""
        return f"{self.speaker}-{self.style}"

    # --- relative, for command lines -------------------------------------
    @property
    def rel_dataset(self):
        return f"audio/{self.speaker}/{self.style}"

    @property
    def rel_manifest(self):
        return f"data/{self.speaker}/{self.style}/{self.name}.jsonl"

    @property
    def rel_latents(self):
        return f"data/{self.speaker}/{self.style}/latents"

    @property
    def rel_output_dir(self):
        return f"seiyuu/{self.speaker}/{self.style}"

    # --- absolute, for file operations -----------------------------------
    def _abs(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    @property
    def audio_dir(self):
        return self._abs(self.rel_dataset)

    @property
    def metadata_csv(self):
        return os.path.join(self.audio_dir, "metadata.csv")

    @property
    def manifest(self):
        return self._abs(self.rel_manifest)

    @property
    def latents_dir(self):
        return self._abs(self.rel_latents)

    @property
    def output_dir(self):
        return self._abs(self.rel_output_dir)

    @property
    def final_checkpoint(self):
        return os.path.join(self.output_dir, FINAL_CHECKPOINT_NAME)

    @property
    def list_dir(self):
        return os.path.join(self.root, "seiyuu", "list")

    @property
    def list_entry(self):
        return os.path.join(self.list_dir, f"{self.name}.speaker.safetensors")

    def transcript_dir(self, transcript_root):
        return os.path.join(transcript_root, self.name)

    def transcript_for(self, transcript_root, wav_name):
        stem = os.path.splitext(os.path.basename(wav_name))[0]
        return os.path.join(self.transcript_dir(transcript_root), stem + ".txt")


def find_wavs(audio_dir):
    """Sorted wav file names in the sample folder. Sorted so row order is
    stable between runs - metadata.csv rows and review rows line up."""
    if not os.path.isdir(audio_dir):
        return []
    return sorted(f for f in os.listdir(audio_dir) if f.lower().endswith(".wav"))


def list_speakers(root):
    """Every <speaker>/<style> pair that already has a sample folder, for
    re-opening one instead of typing the pair again."""
    audio_root = os.path.join(root, "audio")
    if not os.path.isdir(audio_root):
        return []
    pairs = []
    for speaker in sorted(os.listdir(audio_root)):
        speaker_dir = os.path.join(audio_root, speaker)
        if not os.path.isdir(speaker_dir):
            continue
        for style in sorted(os.listdir(speaker_dir)):
            if find_wavs(os.path.join(speaker_dir, style)):
                pairs.append((speaker, style))
    return pairs


# ---------------------------------------------------------------------------
# Transcript cleanup
# ---------------------------------------------------------------------------
# Whisper returns one line per utterance with no trailing punctuation, and
# the TTS needs the punctuation to phrase properly - without it the model
# runs sentences together or clips them. These rules produce the SUGGESTION
# shown in the review box; the person always gets the last word, because
# Whisper also mishears and omits (the "えへへ。" that opens moeshi-calm-02
# is nowhere in its output).

TERMINATORS = "。？！…"
PUNCTUATION = TERMINATORS + "、"
WHITESPACE_RE = re.compile(r"[\s\u3000]+")


def clean_transcript(raw_text):
    """Whisper's raw .txt -> a single line ready for metadata.csv.

    - every line becomes one sentence: a line that ends without punctuation
      gets a "。"
    - line breaks disappear (metadata.csv is one row per wav)
    - spaces disappear, including full-width ones, which Whisper sprinkles
      into Japanese
    - runs of the same mark collapse, so joining never leaves "。。"
    """
    lines = [WHITESPACE_RE.sub("", line) for line in (raw_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    parts = []
    for line in lines:
        parts.append(line)
        if not line.endswith(tuple(PUNCTUATION)):
            parts.append("。")
    text = "".join(parts)

    for mark in PUNCTUATION:
        text = re.sub(re.escape(mark) + "{2,}", mark, text)
    # A trailing "、" is a join artefact, never how a sample ends.
    if text.endswith("、"):
        text = text[:-1] + "。"
    if not text.endswith(tuple(TERMINATORS)):
        text = text + "。"
    return text


# ---------------------------------------------------------------------------
# metadata.csv
# ---------------------------------------------------------------------------
# Header `file_name,text`, one row per wav, CRLF - matching the files
# prepare_manifest.py already reads.

def read_metadata(metadata_csv):
    """{file_name: text} from an existing metadata.csv, or {}."""
    if not os.path.isfile(metadata_csv):
        return {}
    with open(metadata_csv, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return {r["file_name"]: r.get("text", "") for r in rows if r.get("file_name")}


def write_metadata(metadata_csv, rows):
    """rows: [(file_name, text), ...] in the order they should appear."""
    os.makedirs(os.path.dirname(metadata_csv), exist_ok=True)
    with open(metadata_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\r\n")
        writer.writerow(["file_name", "text"])
        for file_name, text in rows:
            writer.writerow([file_name, text])
    return metadata_csv


# ---------------------------------------------------------------------------
# The three commands
# ---------------------------------------------------------------------------

def whisper_command(settings, wav_path, out_dir):
    """The transcription call, pointed straight at the wav where it already
    sits - no copying into the Transcribe folder first."""
    return [
        settings["whisper_exe"],
        wav_path,
        "--model", settings["whisper_model"],
        "--output_dir", out_dir,
        "--output_format", "txt",
        "--task", "transcribe",
        "--language", settings["whisper_language"],
    ]


def manifest_command(settings, paths):
    return [
        "uv", "run", "--no-sync", "python", "prepare_manifest.py",
        "--dataset", paths.rel_dataset,
        "--audio-column", "audio",
        "--text-column", "text",
        "--output-manifest", paths.rel_manifest,
        "--latent-dir", paths.rel_latents,
        "--device", settings["device"],
    ]


def train_command(settings, paths):
    return [
        "uv", "run", "--no-sync", "python", "train.py",
        "--config", settings["train_config"],
        "--manifest", paths.rel_manifest,
        "--init-checkpoint", settings["init_checkpoint"],
        "--output-dir", paths.rel_output_dir,
    ]


def publish_to_list(paths):
    """Puts the trained speaker in seiyuu/list/<speaker>-<style>.speaker.safetensors.

    A hardlink, not a copy: it needs no administrator rights (a symlink
    does, unless Developer Mode is on), costs no space, and both paths are
    on the same volume. Falls back to a copy if the filesystem refuses.

    Returns (mode, path) where mode is "hardlink", "copy" or "exists"."""
    src = paths.final_checkpoint
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"{FINAL_CHECKPOINT_NAME} is not in {paths.output_dir} - training "
            f"has not finished successfully.")

    os.makedirs(paths.list_dir, exist_ok=True)
    dest = paths.list_entry

    if os.path.exists(dest):
        if os.path.samefile(src, dest):
            return "exists", dest
        os.remove(dest)          # a stale link from an earlier training run

    try:
        os.link(src, dest)
        return "hardlink", dest
    except OSError:
        shutil.copy2(src, dest)
        return "copy", dest


# ---------------------------------------------------------------------------
# Where a speaker has got to
# ---------------------------------------------------------------------------

def status(settings, paths):
    """What already exists on disk, so a half-finished speaker resumes
    instead of starting over - training is long and must never restart by
    accident."""
    wavs = find_wavs(paths.audio_dir)
    metadata = read_metadata(paths.metadata_csv)
    transcripts = [
        w for w in wavs
        if os.path.isfile(paths.transcript_for(settings["transcript_root"], w))
    ]
    return {
        "wavs": wavs,
        "transcribed": transcripts,
        "transcribed_all": bool(wavs) and len(transcripts) == len(wavs),
        "metadata_rows": metadata,
        "metadata_done": bool(wavs) and all(
            metadata.get(w, "").strip() for w in wavs),
        "manifest_done": os.path.isfile(paths.manifest),
        "trained": os.path.isfile(paths.final_checkpoint),
        "published": os.path.isfile(paths.list_entry),
    }


def run_streaming(cmd, cwd, on_line):
    """Runs a command, handing each output line to on_line as it arrives.

    Unbuffered and line by line on purpose: these commands run for minutes
    (Whisper) to an hour (training), and output that sits in a pipe buffer
    turns a live log into a frozen one. Returns the exit code.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # Without this, a child Python writing to a PIPE falls back to the Windows
    # locale encoding (cp1252 here) and dies on the first Japanese character.
    # Whisper catches that per file and skips the sample, so the run "succeeds"
    # having transcribed nothing:
    #   UnicodeEncodeError: 'charmap' codec can't encode characters ...
    #   Skipping ...\marinka-03-calm-shonen.wav due to UnicodeEncodeError
    # It never happens when the same command is typed into a console, because
    # a console uses a different write path - so this only breaks once the
    # output is captured, which is exactly what this tool does.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace", env=env)
    for line in proc.stdout:
        on_line(line.rstrip())
    return proc.wait()
