# Seiyuu Onboarder

Turns a folder of wav samples into a trained Irodori-TTS speaker you can pick
in the audiobook generator's **Speaker Path** field.

**This is a separate tool.** It lives in this repository for convenience —
one clone, one history — but shares no code with the generator in the folder
above. It has its own venv, its own `settings.json`, and imports nothing from
its neighbours.

```powershell
cd seiyuu-onboarder
uv sync
uv run python app.py
```

Or without a terminal, the same three launchers the generator has:

| File | What it is |
|---|---|
| `Run-Onboarder.ps1` | starts the GUI from PowerShell |
| `Launch-Onboarder-Silent.vbs` | the same, with no console flash - what the shortcut points at |
| `Create-Shortcut.ps1` | run **once** to put a "Seiyuu Onboarder" shortcut on the Desktop, ready to pin to the taskbar |

The shortcut uses `seiyuu_icon.ico` (a violet 声), deliberately different from the
generator's icon so the two are distinguishable on the taskbar.

## What it automates

A speaker is identified by one pair — `<speaker>/<style>`, e.g.
`moeshi/calm-01` — and that pair names every folder along the way. You type it
once; the tool derives the rest and shows them all before anything runs.

| Step | What happens | Result |
|---|---|---|
| 1 Source | You point at `audio/<speaker>/<style>` | the wavs you put there |
| 2 Transcribe | Whisper, run from `C:\Transcribe`'s venv, reading the wavs where they sit | one `.txt` per wav |
| 3 **Review** | cleaned text shown in editable boxes — **you approve it** | `metadata.csv` |
| 4 Manifest | `prepare_manifest.py` | `.jsonl` + `latents/` |
| 5 Train | `train.py` (speaker inversion) | `checkpoint_final.speaker.safetensors` |
| ✔ | hardlinked into a flat list | `seiyuu/list/<speaker>-<style>.speaker.safetensors` |

**Run remaining steps** walks 2 → 5 in order and parks at step 3. Press it
again after saving and it carries on.

## Why step 3 is not automatic

Whisper returns lines with no trailing punctuation, and the TTS needs that
punctuation or it runs sentences together. The cleanup handles that — join the
lines, add `。` where a line ends without one, drop the spaces Whisper
sprinkles into Japanese, collapse doubled marks.

What it cannot handle is what Whisper *missed*. In the real `moeshi/calm-01`
sample, the text begins `えへへ。` — a sound Whisper never transcribed. So the
tool suggests, you correct, and nothing downstream runs until you save.

Text you have already saved always wins over a fresh suggestion: re-opening a
speaker shows what is in `metadata.csv`, never a re-clean that would discard
your edits.

## Resuming

Every step checks what is already on disk and shows a green check instead of
repeating itself. Re-transcribing is a deliberate checkbox; retraining means
deleting `checkpoint_final.speaker.safetensors` yourself. Training is long and
must never restart by accident.

## The flat list

`seiyuu/list/<speaker>-<style>.speaker.safetensors` is a **hardlink**, not a
copy: no administrator rights (a symlink needs them unless Developer Mode is
on), no extra space, and both paths sit on the same volume. It falls back to a
copy if the filesystem refuses. The trained folder stays exactly where Irodori
puts it.

## Settings

`settings.json`, written beside `app.py` on first run:

| Key | Default | Notes |
|---|---|---|
| `irodori_root` | `C:\Irodori-TTS` | every command runs with this as its working directory |
| `whisper_exe` | `C:\Transcribe\.venv\Scripts\whisper.exe` | the existing install; nothing is duplicated |
| `transcript_root` | `C:\Transcribe\output` | one subfolder per `<speaker>-<style>` |
| `whisper_model` | `large-v3-turbo` | also on the window |
| `whisper_language` | `Japanese` | |
| `device` | `cuda` | training will use the whole card — don't generate a book at the same time |
| `train_config` | `configs/train_v4_small_speaker_inversion.yaml` | |
| `init_checkpoint` | `./model.safetensors` | |

## Layout

- `pipeline.py` — paths, cleanup rules, `metadata.csv`, the three commands.
  Stdlib only and Tk-free, so the rules can be exercised without a GPU.
- `app.py` — the window. Threads do the work; only the Tk thread touches
  widgets.
