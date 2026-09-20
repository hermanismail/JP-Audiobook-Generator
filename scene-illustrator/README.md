# Scene Illustrator

One zen-mode image per chapter. Every decision and every measurement behind
it: [DESIGN.md](DESIGN.md) (v2 at the top; v1 is kept below as history).

## Run

```
& "C:\JP-Audiobook-Generator\scene-illustrator\Run-SceneIllustrator.ps1"
```

or run `Create-Shortcut.ps1` once for a desktop / taskbar shortcut.

Needs, outside this folder (paths in `settings.json`, defaults in
`illustrator.py`):

| for | what |
|---|---|
| reading | `C:\llama.cpp\llama-server.exe` + `F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf` |
| drawing | `F:\ComfyUI` with `flux-2-klein-9b-Q4_K_M.gguf`, `qwen_3_8b_fp8mixed.safetensors`, `flux2-vae.safetensors` |
| editing | the same ComfyUI with `flux1-kontext-dev-Q4_K_M.gguf`, `clip_l.safetensors`, `t5xxl_fp8_e4m3fn_scaled.safetensors`, `ae.safetensors` |

Both GGUF models load through the **ComfyUI-GGUF** custom node. Only one
engine is on the card at a time. Work files go to
`F:\tmp\scene-illustrator\<book>\`; `style_ink.png` beside this README is the
book's drawing style and is always attached when drawing.

## 1 Read

Choose the chapter text folder (`chapter_*.txt`). **Read book** reads the
**first 25% of each chapter** and returns, per chapter, a drawable moment, the
sentence it happens in, and an image prompt - plus a **character roster** that
records where each character reappears, because a character met in chapter 1
is recognised again in chapter 9 (and a second pass re-checks every chapter
against the finished roster). About 7 s a chapter.

The roster is listed on the left; the log on the right. Stop kills the whole
tree, so VRAM comes back.

## 2 Chapters

One image per chapter, in two stages.

**Samples.** The prompt is assembled in code - the style note, the
minimal-background rule, then the subject the model wrote - and is yours to
edit; **Reset prompt** starts again from the reading. **anchor** is either
"draw fresh" or another chapter's finished image, attached so a character
carries over. **Draw samples** makes 3 takes with klein 9B (~30 s each).
Pick one with **Use this**: it becomes the *working image*.

**Fine-tune.** Say what should **change** and who must **keep** as they are,
and **Apply edit** runs Kontext (3 takes, ~3 min each). It changes that one
thing and leaves the rest of the drawing alone. Both fields matter: an
instruction that does not restate what stays put the cap on the wrong person
in testing (DESIGN.md v2, M6). Each round is listed; **Use** takes a round's
image as the new working image, **Back to before** returns to what it started
from. **Promote** makes the working image the chapter image.

**Export image** writes `chapter_<N>_img_1.png` into the book's output folder,
which is what the player reads. **Clear history** deletes every sample and
take of that chapter except the final and working images.

Everything is saved as you go, to `chapters.json`; takes live in
`chapters\<chapter>\samples\` and `...\edits\`. Takes drawn outside the window
(the CLI) are picked up when the book is opened.

## CLI

The window runs these; they also work on their own:

```
uv run python illustrator.py read  --book after-dark --text F:\...\chapter-text
uv run python illustrator.py draw  --book after-dark --chapter chapter_001 --count 3
uv run python illustrator.py edit  --book after-dark --chapter chapter_001 \
      --base <png> --instruction-file <txt> --count 3
```
