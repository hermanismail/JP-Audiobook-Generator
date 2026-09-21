# Scene Illustrator

One zen-mode image per chapter, drawn and edited by **Qwen-Image-2.1** with
**character sheets**. Every decision and every measurement behind it:
[DESIGN.md](DESIGN.md) (M9 and M10 for the Qwen switch; v2 and v1 below them
as history).

## Run

```
& "C:\JP-Audiobook-Generator\scene-illustrator\Run-SceneIllustrator.ps1"
```

or run `Create-Shortcut.ps1` once for a desktop / taskbar shortcut.

Needs, outside this folder (paths in `settings.json`, defaults in
`illustrator.py`):

| for | what |
|---|---|
| reading, Write prompt | `C:\llama.cpp\llama-server.exe` + `F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf` |
| drawing and editing | `F:\ComfyUI` (2026-09-20 or later) with `qwen_image_2.1_Q4_K_M.gguf` (ComfyUI-GGUF node), `qwen3vl_8b_w4a8.safetensors`, `qwen_image_2.1_vae_bf16.safetensors` |

Only one engine is on the card at a time. Work files go to
`F:\tmp\scene-illustrator\<book>\`; `style_ink.png` beside this README is the
drawing style, used when no sheet is attached and for new sheets.

## 1 Read

Choose the chapter text folder (`chapter_*.txt`). **Read book** reads the
**first 25% of each chapter** and returns, per chapter, a drawable moment, the
sentence it happens in and an image prompt, plus a **roster** of the people
the model saw. About 7 s a chapter.

The roster is a hint, not a decision: it records people the text has not
named yet as 男 or 女の子, and it merges people it should not. The Cast tab is
where you decide.

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

1. **cast**: tick who is in the picture, **3 at most**. The ticks start as a
   guess from the aliases ("guessed from aliases"); once you tick, your choice
   is kept.
2. **Write prompt**: Qwen3.5 rewrites the chapter's reading into a prompt that
   names the ticked people. Edit it freely. **Reset prompt** goes back to the
   reading.
3. **Draw samples**: 2 takes by default. Each ticked person's sheet is attached
   and named in the prompt ("Mari is the person in `<image2>`"). With nobody
   ticked, the style sample is attached instead. About 4 min a take with two
   people, 5-6 with three. With three people, faces hold but who-does-what
   sometimes swaps: expect to redraw.
4. Pick one with **Use this**: it becomes the *working image*.

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
uv run python illustrator.py draw      --book after-dark --chapter chapter_001 --cast m003 --cast m002
uv run python illustrator.py edit      --book after-dark --chapter chapter_001 \
      --base <png> --instruction-file <txt> --cast m003
```
