# Scene Illustrator — design (draft for review, 2026-09-19)

A separate tool that makes the zen-mode images for a book: it reads the
book, builds a cast-and-places reference sheet with you, proposes four
scenes per chapter, draws them in the book's art style with you choosing
and tweaking, then names the images and times each one to the moment its
scene is read.

**Not built yet.** Everything here is either decided (marked **D**, with
the date) or a proposal waiting for your answer (listed under "Decisions
needed" at the end).

---

## 1. What is already settled

| # | Decision | |
|---|---|---|
| 1 | Its own tool, own folder (`scene-illustrator/`), own venv. Only link to the audiobook is `sync.json` for timing. | **D** 09-18 |
| 2 | Semi-automatic like `chapter-repair`: the tool prepares, you look, tweak prompts, re-roll, approve. Only approved images are renamed and exported. | **D** 09-18 |
| 3 | Reference sheet is semi-auto too: tool shortlists cast and places, draws **3 samples** each, you pick one or re-prompt. | **D** 09-18 |
| 4 | Timing lives in a sidecar per chapter, `chapter_<N>.images.json`. | **D** 09-18 |
| 5 | An image appears at the **start of the sentence** its scene comes from; adjustable afterwards. | **D** 09-18 |
| 6 | 0:00 only in the **first** chapter. In later chapters the previous chapter's last image trails until the first scene starts. | **D** 09-18 |
| 7 | Player changes (web client first, Android frozen) come after this tool, via a handover prompt to a session in `F:\JPAudiobookPlayer`. | **D** 09-18 |
| 8 | No automatic physical-logic check for now; you judge each image. A checklist library may come later. | **D** 09-18 |
| 9 | Work files under `F:\tmp\scene-illustrator\<book>\`. | **D** 09-18 |

### Measured (2026-09-18), not assumed

- **Image model: FLUX.2 [klein] 4B fp8** in ComfyUI (`F:\ComfyUI`, own
  venv). ~10 s per 832x1216 image on the 4060 once warm, ~30-45 s for the
  first one (text encoder swap). One reference image of Mari held her
  face, hair, glasses, clothes and the ink style across three scenes.
  Characters without a reference came out generic (Kaoru as a slim pretty
  woman). Faults seen: stray colour in a monochrome style, garbled
  logos/signs, an object swapped (trombone case -> guitar case), a
  person standing through a table.
- **Reading model: Qwen3.5-9B Q4_K_M** (`F:\models\llm`, llama.cpp).
  Beat Qwen3-8B on After Dark. ~6-7 min per book for per-chapter
  extraction. It finds nearly every character and place, with the text's
  own descriptions. **It still misattributes ~1-2 details per book**
  (Takahashi's scar given to Mari) and **cannot merge** the chapter lists
  into one cast: single-pass merge loops or breaks JSON; split merge
  either merges wrongly (Mari + Eri) or barely at all. Prompt rule "only
  what is physically present in the scene" cut place names 75 -> 57 and
  must stay.
- **VRAM**: the two models never share the card. Qwen3.5 uses ~6.8 GB,
  FLUX peaks near the 8 GB limit. Same rule as TTS vs VNTL.

---

## 2. The shape

```
book text  --(A) read-->  raw cast & places  --you merge/accept-->  bible.json
bible.json --(B) draw 3 each--> you pick/re-prompt --> reference sheet (refs/)
chapter    --(C) propose 4 scenes--> you edit --> draw takes with refs
           --> you pick/tweak/re-roll --> approved images
approved   --(D) match anchor sentence to sync.json--> you adjust
           --> export: chapter_<N>_img_<i>.png + chapter_<N>.images.json
```

Each stage writes its result to disk and can be reopened later. Closing
the window never loses a decision. Text and choices you saved **always win
over a fresh model suggestion** (the onboarder's rule), so re-running a
stage never overwrites your edits.

---

## 3. Stage A — Read the book (LLM)

**Input**: the book's chapter text folder (e.g.
`F:\AUDIOBOOK-FINAL\after-dark\chapter-text`).

**Engine**: llama-server with Qwen3.5-9B, started and stopped by the tool
(only stops a server it started — the `ManagedLlamaServer` rule).

**Per chapter**, in pieces of <= 7,000 characters split at line breaks:
list characters and places **physically present**, each with name as
written, aliases, role, and every appearance detail **with its source
sentence quoted**. The quote is what makes review possible.

Robustness learned the hard way (all needed on this llama.cpp build):
`--reasoning off --reasoning-format none`; strip the empty `<think>`
block; cut from first `{` to last `}` (code fences); lenient JSON
(raw line breaks in strings); on a server 500 or broken JSON retry with a
new seed; if a piece still fails, retry it without quotes and mark those
details "unverified".

### Review screen: "Cast & places"

Two tabs, Characters and Places. Left: the raw entries, each showing name,
chapters it appears in, and entry count. Right: the selected entry's
details, **each detail a row with its quote**, tick to keep / untick to
drop.

- **Merge**: select several entries (`若い男`, `タカハシ`, `高橋`) ->
  Merge -> choose the canonical name. Aliases and details are pooled.
- **Suggest merges**: optional button; the LLM proposes groups, shown as
  suggestions you accept one by one. Never applied automatically.
- **Delete** an entry (noise: someone only talked about).
- **Edit** the canonical name, add a note, add a detail by hand.
- **Priority**: mark an entry as *main* (gets a reference image) or
  *minor* (described in prompts only, no reference). Default: main =
  appears in 3+ chapters.

**Output**: `bible.json` — the merged, accepted cast and places. Nothing
downstream reads the raw lists.

Expected effort for After Dark: ~42 raw character entries -> ~14 real
ones, ~57 place entries -> ~15 real ones; 10-15 minutes of review.

---

## 4. Stage B — Reference sheet (image model)

**Inputs**: `bible.json`, and **your style image(s)** for the book (the
Mari drawing you supplied is the model for this).

For each *main* character and each key place:

1. The tool writes an English image prompt from the accepted details
   (LLM, then shown to you editable). Characters: full body, neutral
   standing pose, plain background, facing slightly left. Places: an
   empty establishing shot, no people.
2. FLUX draws **3 samples** with the style image attached.
3. You pick one, or edit the prompt and draw 3 more. Picking an image
   locks it.

A character can hold **more than one reference** (e.g. Eri awake / asleep
in pyjamas, Kaoru with and without her knit cap). Optional; proposed
because clothes change within books.

A **book style note** is part of every prompt, set once per book: e.g.
"pure black-and-white manga ink, hatching, no colour". This targets the
stray-colour fault.

**Output**: `refs/characters/<id>.png`, `refs/places/<id>.png`,
`refs.json` (which image, which prompt, which seed).

---

## 5. Stage C — Scenes (LLM proposes, image model draws)

### C1. Proposals (LLM)

Per chapter, the LLM receives the chapter text plus the list of bible
names and proposes **4 scenes spread across the chapter** (roughly one
per quarter, never two in the same passage). Each proposal:

- **anchor**: the exact sentence where the scene starts, copied from the
  text (checked by code to exist verbatim; if not, the tool finds the
  closest sentence and flags it);
- **what is seen**: one or two sentences of description;
- **cast** and **place**: bible IDs;
- **prompt**: English, built from the description + the cast's and
  place's accepted details + the book style note.

Short chapters may get fewer (ch.8 of After Dark is 1,700 characters and
has one scene). Chapters where nothing is drawable (dialogue only) are
allowed fewer.

**Proposal review** (before any drawing, because drawing is where time
goes): per chapter a list of 4 cards. You can edit the text, change the
anchor sentence (pick from the chapter text), add/remove cast, replace a
scene with your own idea, or delete one.

### C2. Drawing

Each approved proposal is drawn as **N takes** (default 3) with its
references attached: style image + each cast member's reference + the
place's reference.

**Scene gallery**: per chapter, one row per scene, its takes side by
side, the prompt editable under them. Per take: Pick, or re-roll.
Per scene: edit prompt -> draw N more. Picked = approved. Nothing is
exported until you approve.

Speed: 18 chapters x 4 scenes x 3 takes = ~216 images, ~40 min of GPU.

---

## 6. Stage D — Timing, naming, export

**Input**: the chapter's `sync.json` in the book's output folder.

1. **Match** each approved scene's anchor sentence to a `sync.json`
   entry by text. Dynamic chapters: one sentence per entry, a direct
   match. Normal-mode chapters: an entry holds several sentences, so the
   anchor is found *inside* an entry and its `start` is used. Unmatched
   anchors are flagged red and must be placed by hand.
2. **Adjust**: per image, show the matched Part (reader numbering,
   `sync.json` index + 1) and time, with the sentence text. Change by
   typing a Part (`68`) or a time (`7:06`), as in `dynamic-repair`.
3. **Rules**: in the first chapter, image 1 is forced to 0:00. Elsewhere
   an image starts at its anchor; before it, the previous chapter's last
   image keeps showing (player side). Starts must increase and stay
   inside the chapter's duration.
4. **Export** into the output folder, per chapter:
   - `chapter_<N>_img_<i>.png`, i = 1..4 in time order — the name the
     player already reads;
   - `chapter_<N>.images.json`:

```json
{
  "version": 1,
  "images": [
    {"file": "chapter_003_img_1.png", "start": 12.34},
    {"file": "chapter_003_img_2.png", "start": 402.10}
  ]
}
```

   Existing images for a chapter are **never overwritten silently**:
   export shows what is there and asks (skip-existing is the default,
   as everywhere in this repo).

The player today splits a chapter's images evenly over its playback
time. With the sidecar it should switch at `start`; without it, it keeps
the even split, so old books keep working. That change is the handover.

---

## 7. Files and folders

```
C:\JP-Audiobook-Generator\scene-illustrator\     the tool (code, own venv)
    app.py                window (stages as tabs)
    reader.py             stage A (llama-server client, extraction)
    comfy.py              ComfyUI client: graph builder, submit, poll
    scenes.py             stage C proposals
    timing.py             stage D matching + sidecar
    settings.json         gitignored: paths below

F:\tmp\scene-illustrator\<book>\                 work root, per book
    book.json             inputs + stage status
    read\pass1\<chapter>_<n>.json    raw extraction (with quotes)
    bible.json            your merged cast & places
    refs\characters\<id>\sample_*.png, chosen.png
    refs\places\<id>\...
    refs.json
    scenes\<chapter>\proposals.json
    scenes\<chapter>\<scene>\take_*.png, take_*.json (prompt, seed)
    scenes\<chapter>\approved.json
    export.json           what was exported where, when

<book output folder>                              written only at export
    chapter_<N>_img_<i>.png
    chapter_<N>.images.json
```

Engines (installed outside the repo, per the F: rule):
`F:\ComfyUI` (ComfyUI + FLUX.2 klein 4B fp8, `qwen_3_4b` encoder,
`flux2-vae`), `C:\llama.cpp\llama-server.exe` (existing) with
`F:\models\llm\Qwen3.5-9B-Q4_K_M.gguf`.

## 8. Engine handling

- **One engine on the GPU at a time.** Entering a stage that needs the
  other engine stops the current one first. The tool starts ComfyUI
  itself (`--listen 127.0.0.1`) and llama-server itself, and only ever
  stops what it started.
- Stop = kill the process **tree** and confirm VRAM returned near the
  desktop baseline (~1.1-1.3 GB) — a lesson from the profiler.
- A busy GPU at start **warns**, never refuses (profiler rule).
- Every child process is launched with `PYTHONIOENCODING=utf-8` and its
  output is read continuously (the deadlock lesson).

---

## 9. Still to measure before or while building

| # | Question | Why it matters |
|---|---|---|
| M1 | How many reference images fit in one FLUX klein request on 8 GB, and does quality hold with 3-4? | A scene with Mari + Kaoru + Komugi + a place is 5 images incl. style. |
| M2 | Japanese vs English prompts to FLUX (its text encoder is Qwen3-4B) | Could skip a translation step, keep names exact. |
| M3 | Qwen3.5's 4-scene proposals: are anchors verbatim, spread across the chapter, drawable? | Stage C1 quality; the same kind of test as the cast list. |
| M4 | Anchor -> `sync.json` match rate on a real published chapter (normal and dynamic mode) | Stage D automation level. |
| M5 | Does a "pure monochrome" style note stop the colour leaks? | Style consistency. |

---

## 10. Decisions needed from you

1. **Image format and size.** PNG ~1.8 MB each at 832x1216 (portrait).
   JPEG q90 would be ~300-500 KB — 72 images per book on R2 either way.
   Portrait 832x1216 suits zen mode's side column; confirm, or give the
   shape you use today.
2. **Takes per scene**: default 3? (Each take ~10 s.)
3. **Multiple references per character** (outfits/states): wanted now,
   or later?
4. **Who writes prompts' language**: keep English (proposed) unless M2
   shows Japanese works better?
5. **Style image**: one per book (proposed), or allow one per character?
6. **Minor characters**: no reference, described in prompt only
   (proposed) — OK?
7. **Chapters with fewer than 4 drawable scenes**: allow 1-3 (proposed),
   or always 4?
8. **Build order**, proposed:
   (a) M1-M3 measurements;
   (b) Stage A + cast review screen;
   (c) Stage B reference sheet;
   (d) Stage C proposals + gallery;
   (e) Stage D timing + export;
   (f) handover prompt for the player's web client.
   Each on its own feature branch, merged when you have used it.
