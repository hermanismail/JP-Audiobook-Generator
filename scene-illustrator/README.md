# Scene Illustrator

Zen-mode images per chapter. The full design, every decision and every
measurement behind it: [DESIGN.md](DESIGN.md).

**Built so far: Stage A** (read the book, curate the cast and places),
**Stage B** (reference sheets) and **Stage C** (scenes).

## Run

```
& "C:\JP-Audiobook-Generator\scene-illustrator\Run-SceneIllustrator.ps1"
```

or run `Create-Shortcut.ps1` once for a desktop / taskbar shortcut.

Needs, outside this folder: `C:\llama.cpp\llama-server.exe` and
`F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf` (paths in `settings.json`, created
on first save; defaults in `illustrator.py`). Work files go to
`F:\tmp\scene-illustrator\<book>\`.

## 1 Read

Choose the book's chapter text folder (`chapter_*.txt`); the book name is
taken from the folder. **Read book** starts llama-server, reads every
chapter in pieces of up to 7,000 characters, and stops the server again —
about 12 minutes for After Dark. Stop kills the whole tree, so VRAM comes
back. Finished pieces are kept: Read again only does what is missing or
failed.

## 2 Cast & places

Every name the model found, one entry per exact name. For each entry:

- **every detail with the sentence it came from**, found in the text by
  code (the model's own quote is only the search key). Untick a detail
  the sentence does not support — about 1 in 5 on After Dark (e.g.
  "wearing red socks" from レッドソックスの帽子). Orange = closest
  sentence, red = no sentence found;
- **Merge ticked** pools entries that are one person or place
  (若い男 + タカハシ + 高橋 -> 高橋テツヤ); **Delete ticked** removes noise
  (上田麗奈 from the narrator credit); rename, note, add a detail by hand;
- **Pre-mark as main** forces a reference image. Otherwise main/minor is
  decided from the scene proposals (Stage C): drawn more than once = main;
- **Suggest merges** asks the model; each suggestion is accepted or
  dismissed by you, never applied on its own.

Every change is saved to `bible.json` at once. Your choices always win:
a re-read adds new names and new details (marked NEW) but never undoes a
merge, a deletion, a rename or an unticked detail.

## 3 References

One drawing per character and place, so they look the same in every scene.

- **Style image**: one per book, the drawing whose style the book follows.
  It is copied to `refs\style.png` in greyscale and attached to every
  request. **Style note** is the wording added to every prompt.
- The **prompt** is assembled in code from the details you kept, and is
  yours to edit. **Rebuild prompt** starts again from the current details.
- **Draw** makes N takes (3 by default) through ComfyUI, which the tool
  starts and stops; ~40 s for the first take of a prompt, ~12 s after
  that. **Choose** locks one as the reference; Delete throws a take away.
- **Add variant** gives the same character a second reference for a
  different look (asleep in pyjamas, in a green tracksuit), each with its
  own label, prompt and chosen image.
- ★ marks entries pre-marked main in tab 2. Everything is greyscale, in
  and out: a prompt cannot keep colour away (DESIGN.md §9, M5).

Saved in `refs.json`. Images live in `refs\<kind>\<id>\v<n>\`.

## 4 Scenes

**Propose scenes** asks the model for the scenes of the selected chapter,
or of the whole book with the tick box. How many a chapter gets comes from
its length against the book's median chapter: under 0.35x = 1, under
0.75x = 2, up to 1.5x = 3, above = 4 (After Dark: 52 scenes). The chapter
is cut into that many slices and one scene is taken from each, which is
what spreads them out - asked for four scenes at once, the model bunches
them at the start and end of the chapter (DESIGN.md §9, M3).

Per scene: the **anchor** sentence (where the image will appear in Stage
D) with its position in the chapter, the **cast and place**, and the
**prompt**, editable like everywhere else.

- **Anchor** opens every sentence of the chapter, filterable, to move the
  scene somewhere else; scenes re-sort by position.
- **Cast & place** ticks who is in the picture; entries with a chosen
  reference come first and are marked, since those are the ones that stay
  consistent.
- **Draw** attaches the style image plus the chosen reference of every
  cast member and the place - 5 references is the measured ceiling, and
  the line above says how many are attached and warns when extras would
  be dropped. Choose one take per scene.

Saved in `scenes.json`; images in `scenes\<chapter>\s<n>\`.
