# Scene Illustrator

Zen-mode images per chapter. The full design, every decision and every
measurement behind it: [DESIGN.md](DESIGN.md).

**Built so far: Stage A** — read the book, curate the cast and places.

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
