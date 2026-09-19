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
| 10 | Images are **PNG, 832x1216 portrait**. | **D** 09-19 |
| 11 | **3 takes** per scene by default. | **D** 09-19 |
| 12 | **Several references per character** (outfits / states) — in the first build. | **D** 09-19 |
| 13 | Image prompts in **English**. | **D** 09-19 |
| 14 | **One style image per book.** | **D** 09-19 |
| 15 | A character drawn in **more than one** scene is main and must have a reference; otherwise minor (see §4a). | **D** 09-19 |
| 16 | Scenes per chapter scale with chapter length against the book's **median** chapter: median = 3 scenes, shorter = fewer, longer = more (see §5a). | **D** 09-19 |
| 17 | Build order: measurements, then A, B, C, D, then the player handover. | **D** 09-19 |
| 18 | A character or place drawn **only once in the whole book** is drawn without a reference. | **D** 09-19 |
| 19 | **At most 4 scenes per chapter.** Image 4 trails until the next chapter's first image. | **D** 09-19 |
| 20 | Scene count by **bands** of chapter length / median, not rounding. | **D** 09-19 |
| 21 | **Places follow the same rule** as characters: drawn once = no reference. | **D** 09-19 |

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
- **Main / minor** is not decided here — it depends on how often a
  character is DRAWN, which is only known after scene proposals (§4a).
  You may pre-mark someone main here to force a reference.

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
in pyjamas, Kaoru with and without her knit cap). Each variant has a
short label; a scene names the variant it uses, the proposal picks one
and you can change it.

### 4a. Main and minor characters (decided 09-19)

A character is **main** when it is drawn in **more than one** scene of
the book; a main character must have an approved reference before any of
its scenes are drawn. **Minor** = drawn in exactly one scene of the whole
book, and drawn there **without a reference**, from the prompt alone —
consistency cannot matter for someone seen once. **Places follow the
same rule.** The count comes from the approved scene proposals across the
whole book (§5), so:

- Stage B first draws references for anyone pre-marked main or already
  in 2+ proposals;
- if editing proposals later puts a minor character into a second scene,
  it is **promoted**: the tool flags it and its scenes wait until its
  reference is approved in Stage B.

A **book style note** is part of every prompt, set once per book: e.g.
"pure black-and-white manga ink, hatching, no colour". This targets the
stray-colour fault.

**Output**: `refs/characters/<id>.png`, `refs/places/<id>.png`,
`refs.json` (which image, which prompt, which seed).

---

## 5. Stage C — Scenes (LLM proposes, image model draws)

### C1. Proposals (LLM)

Per chapter, the LLM receives the chapter text plus the list of bible
names and proposes **its scene count (§5a) spread across the chapter**
(one per equal slice of the text, never two in the same passage). Each
proposal:

- **anchor**: the exact sentence where the scene starts, copied from the
  text (checked by code to exist verbatim; if not, the tool finds the
  closest sentence and flags it);
- **what is seen**: one or two sentences of description;
- **cast** and **place**: bible IDs;
- **prompt**: English, built from the description + the cast's and
  place's accepted details + the book style note.

### 5a. How many scenes per chapter (decided 09-19)

No fixed threshold: the baseline is the **median chapter length of the
book**, measured over all its chapters. Bands of `chapter / median`
(edges confirmed 09-19), **capped at 4**:

| chapter / median | scenes |
|---|---|
| under 0.35 | 1 |
| 0.35 - 0.75 | 2 |
| 0.75 - 1.5 | 3 (the median chapter) |
| over 1.5 | 4 (cap) |

After Dark (median 6,083 chars): ch.8 (1,698, 0.28) 1; ch.2, 4, 7, 14
(0.45-0.70) 2; ch.5, 6, 10-13, 16-18 (0.81-1.47) 3; ch.1, 3, 9, 15
(1.53-2.23) 4 — **52 images** for the book. In a chapter with more than
4 drawable moments, image 4 simply trails until the next chapter's first
image. You can change a chapter's count in the proposal review; the LLM
may also propose fewer when a chapter has nothing drawable.

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

Speed: After Dark's 52 scenes x 3 takes = 156 images, ~26 min of GPU.

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
   - `chapter_<N>_img_<i>.png`, i = 1..n in time order — the name the
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

### Results of step (a), 2026-09-19

Files: `F:\tmp\scene-illustrator\after-dark\measure\`.

**M1 — references per request: 5 fit.** Peak VRAM ~7.0-7.4 GB whatever
the count; time grows instead: 1-2 refs ~17-26 s, 3 refs ~26 s, 5 refs
~60 s. Identity: with its own reference Kaoru matched her sheet in both
seeds (without one she was a different woman); a place reference carried
the bar's layout (record shelves, counter) into the scene. The 5-ref
crowded room kept all four women recognisable but composed as a small,
cluttered wide shot. Rule for the tool: **cap at 5 references** (style +
up to 4), warn above 3.

**Time per image depends on the prompt, not the take**: the first image
of a NEW prompt costs ~26 s (the text encoder is swapped back onto the
full card), further takes of the same prompt ~10 s. So draw all takes of
one scene together; a scene of 3 takes ~45 s (2 refs) to ~2 min (5 refs).

**M2 — Japanese prompt: works as well as English** (same identities,
same scene), no gain, ~2x slower to encode. Decision 13 (English) stands.

**M3 — scene proposals (Qwen3.5-9B, all 18 chapters, 4.4 min):**
- Every chapter got exactly its band count (52).
- **Choices are good**: the visual key moments (Takahashi at Mari's
  table, Kaoru bursting in, room 404, the bar, the kitten, Korogi's
  brand, the swings, the station goodbye, Mari into Eri's bed).
- **6 of 52 anchors not verbatim** (the model glued sentences together)
  -> code must snap each anchor to the closest real sentence and flag it.
- **Poor spread**: scenes cluster at chapter starts and ends (ch.13:
  0.00 / 0.89 / 0.99). -> ask ONE scene per slice of the chapter
  (N calls, each given only its slice), which forces the spread.
- Cast/place slips ~1 in 8: e.g. room 404 exit scene missing Mari and
  Kaoru, the taxi driver left out, a truck scene given Mari + Takahashi,
  invented extras ("waving goodbye at dawn"). Caught in proposal review.

**M5 — "pure monochrome, no colour" does NOT stop colour** when the
description names one (orange trousers, red hair, pink shirt came out in
colour), and a coloured reference image leaks its colour into scenes.
Fix is deterministic, not a prompt: a per-book **monochrome switch** that
converts reference images to greyscale before use and every output to
greyscale on save.

**M4 — anchor -> `sync.json`: 52 of 52 matched** against the published
(normal-mode) After Dark, by searching the anchor's FIRST sentence inside
the concatenated entries — including the 6 non-verbatim anchors, whose
first sentence was real. In normal mode an entry holds several sentences,
so the image switches at the start of the entry containing the anchor,
sometimes one sentence early; in dynamic chapters an entry is one
sentence, so it is exact. Stage D needs no fuzzy matching in the common
case; keep it only as the fallback.

**Also seen**: body build is weakly followed (Kaoru "big, 175 cm,
ex-wrestler" drawn slim in every sheet); "windowless" rooms got windows.
Reference sheets need the re-prompt loop Stage B already has; negative
details ("no windows") are unreliable.

---

## 10. Decisions needed from you

All decisions so far are recorded in §1 and §5a (band edges confirmed
09-19). Nothing open.

Build order (decided): (a) M1-M3 measurements; (b) Stage A + cast review
screen; (c) Stage B reference sheet; (d) Stage C proposals + gallery;
(e) Stage D timing + export; (f) handover prompt for the player's web
client. Each on its own feature branch, merged when you have used it.
