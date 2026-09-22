# Scene Illustrator

One zen-mode image per chapter, with **character sheets**. The book is read,
and images are drawn, by **Google** (Gemini on Agent Platform). The local
**Qwen-Image-2.1** engine stays for the scenes Google refuses. Every decision
and every measurement behind it: [DESIGN.md](DESIGN.md) (M11 for Google, M9 and
M10 for Qwen; v2 and v1 below them as history).

## Run

```
& "C:\JP-Audiobook-Generator\scene-illustrator\Run-SceneIllustrator.ps1"
```

or run `Create-Shortcut.ps1` once for a desktop / taskbar shortcut.

Needs, outside this folder (paths in `settings.json`, defaults in
`illustrator.py`):

| for | what |
|---|---|
| reading, Write prompt, Google images | a Google Cloud project with the **Agent Platform API** enabled (`aiplatform.googleapis.com`), its ID in the Read tab, and this PC logged in once with `gcloud auth application-default login`. No key is stored anywhere. |
| local images | `F:\ComfyUI` (2026-09-20 or later) with `qwen_image_2.1_Q4_K_M.gguf` (ComfyUI-GGUF node), `qwen3vl_8b_w4a8.safetensors`, `qwen_image_2.1_vae_bf16.safetensors` |

Google's models: `gemini-3.8-flash` reads, `gemini-3.1-flash-image` (Nano
Banana 2) draws, set in `settings.json`. It must be Agent Platform, not an AI
Studio key: trial credits from after 2 March 2026 cannot pay for AI Studio.

Every Google call is logged, with its tokens and an estimated cost, to
`F:\tmp\scene-illustrator\google_usage.jsonl`. The header shows the running
total. The estimate uses list prices; Google's bill is the real figure.

Work files go to `F:\tmp\scene-illustrator\<book>\`. `style_ink.png` beside
this README is the drawing style; it is attached to Google draws and to new
sheets, and to local draws only when no sheet is attached.

## 1 Read

Choose the chapter text folder (`chapter_*.txt`) and enter the Google
project. **Read book** sends every **whole chapter** to Gemini and returns, per
chapter:

- a **summary**;
- the **3 key moments**, each with the sentence it happens in, copied from
  the text;
- **who is in the best moment**, identified across the book (男 → Takahashi),
  using your cast's names where it can;
- an **image prompt** for it.

It takes about 20-60 s and about $0.01-0.02 a chapter. The first Google
reading keeps the old one as `read_local_backup.json`. A prompt you never
edited follows a new reading; one you edited, or one Write prompt wrote, is
kept.

**images by default** picks the engine for chapters and for new sheets.

## 2 Cast

The people who get a **character sheet**, a full-body picture that tells the
image model how they look. For each one:

- **name**: the name prompts use ("Mari").
- **aliases**: the roster names that mean this person (マリ, 女の子). Chapters
  use them to guess who is in the picture. The unclaimed roster names are
  listed under the field.
- **tag** (optional): what sets them apart at a glance ("in the baseball cap").
  It helps the model give each action to the right person when three are in
  the picture.
- **the sheet**: **Import v1 sheets** brings in the sheets chosen in the old
  workflow, copied into this book's `cast` folder. Or write a **description**
  and **Draw sheet** (2 takes, about 4 min each), then **Use as sheet**.
  **Edit sheet** changes one thing on it.

## 3 Chapters

1. **moment**: pick one of the reading's 3 moments. **Summary** shows the
   chapter summary, the moments with their sentences, and who is in the
   picture.
2. **cast**: tick who is in the picture, **3 at most**. The ticks start as a
   guess from the aliases ("guessed from aliases"); once you tick, your choice
   is kept.
3. **Write prompt**: Gemini writes the prompt for the chosen moment around the
   ticked names, reading the chapter again, so positions and objects come from
   the text. Anyone without a sheet is described instead of named. Edit it
   freely. **Reset prompt** goes back to the reading.
4. **Draw samples** on the chapter's engine (the menu beside it):
   - **Google**: about 15 s and $0.07 a take.
   - **Local**: about 4 min a take.

   Each ticked person's sheet is attached and named in the prompt ("Mari is
   the person in `<image2>`"). When Google refuses a picture (it does for the
   book's violent or sexual scenes), the window offers to switch that chapter
   to the local engine.
5. Pick one with **Use this**: it becomes the *working image*.

**Fine-tune.** Say what should **change** and who must **keep** as they are,
then **Apply edit**. **attach sheets** adds up to 2 sheets. The label beside
them shows their slot, so the change can say "he carries the case from
`<image2>`". Each round is listed; **Use** takes a round's image as the
working image, **Back to before** returns to what it started from.
**Promote** makes the working image the chapter image.

**Export image** writes `chapter_<N>_img_1.png` into the book's output folder.
**Clear history** deletes every sample and take of that chapter except the
final and working images.

Everything is saved as you go, to `chapters.json` and `cast.json`.

## CLI

The window runs these; they also work on their own:

```
uv run python illustrator.py read      --book after-dark --text F:\...\chapter-text
uv run python illustrator.py import-v1 --book after-dark
uv run python illustrator.py sheet     --book after-dark --member m002 --count 2
uv run python illustrator.py write     --book after-dark --chapter chapter_001 --cast m003 --cast m002
uv run python illustrator.py draw      --book after-dark --chapter chapter_001 --cast m003 --cast m002 --engine google
uv run python illustrator.py edit      --book after-dark --chapter chapter_001 \
      --base <png> --instruction-file <txt> --cast m003 --engine local
```

`--engine` defaults to the chapter's engine, then to `image_engine` in
`settings.json`. A Google refusal prints `REFUSED <reason>` and exits with
code 3.
