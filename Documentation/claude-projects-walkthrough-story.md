# A Solo Dev's First Project: Building "Yomikikase" with Claude

*A fictional story, told to illustrate how Claude Projects actually work in practice.*

---

## Evening One: The Empty Workspace

Arata closes his laptop lid halfway, then opens it again. It's 9:40 PM, the apartment is quiet, and he's been putting this off for three weekends now. He has a folder of `.txt` files — light novel chapters he owns and has permission to convert, meant only for his own commute listening — and a Python script that half-works, cobbled together from tutorials and late-night debugging. He wants it to actually be *good*. Reliable. Something he can hand a folder of text to and walk away from.

He opens Claude and, instead of starting a normal chat, clicks **New Project**.

A blank form appears: a name field, a description field, an empty knowledge base.

> **Name:** `Yomikikase — Audiobook Pipeline`
> **Description:** `Personal tool that converts my own Japanese text files into narrated audiobooks. Solo hobby project, private use only.`

He pauses on the description longer than expected. It feels almost like naming a git repository — a small act of commitment.

> 💡 **Tip woven into the moment:** A Project's name and description aren't just labels — Claude reads them as part of its context every time you open a new chat inside the Project. A vague name like "Audio Stuff" gives Claude nothing to anchor to. A specific one — what the tool does, who it's for, what it's *not* for — quietly does a lot of the work later, especially months from now when Arata has forgotten the details himself.

Next, Claude asks — gently, not as a form field but as a genuine first message in the empty chat — what he's trying to build and what would help it understand the shape of the work. Arata explains: a pipeline that cleans raw Japanese text, splits it into sentences, feeds each one to a local TTS engine, and stitches the results into a chapter-length MP3 with natural pauses. He mentions the script already exists but is fragile — hardcoded paths, no error recovery, no way to know if it's stuck or just slow.

Claude asks one clarifying question rather than five: does he want to evolve the existing script, or rebuild the pipeline from scratch? Arata says evolve it — there's logic in there he's tuned by hand over weeks (the punctuation cleanup rules especially) and he doesn't want to lose that.

> 💡 **Best practice:** Notice Claude asked *one* focused question instead of interrogating him. When you're vague, a good back-and-forth beats Claude guessing — but it should still converge fast. If you find yourself fielding a wall of questions before any real work starts, it's fair to just say "pick reasonable defaults and go."

---

## Setting Up Custom Instructions

Before writing any code, Arata clicks into the Project's **Custom Instructions** panel — the standing context that will apply to *every* conversation inside this Project, not just the one he's in now.

He writes:

```
This project is a personal, private audiobook pipeline for text I own or
have rights to use. Never assume content is meant for redistribution.

Environment: Windows 11, RTX 4060 GPU, using `uv` for Python package
management. Prefer pathlib over raw string paths where possible.

Style: explain trade-offs briefly, then show code. I'd rather re-ask a
question than have you guess silently on something structural.
```

He almost adds "always be extremely detailed and thorough" — then deletes it. He remembers reading that vague style requests like "be more thorough" tend to just make everything longer, not better; better to say what he actually wants when he wants it, per response.

> 💡 **Tip:** Custom instructions are best kept to durable facts and preferences — your environment, your constraints, how you like to work — not one-off task details. Project-specific instructions that change every session belong in the chat itself, not here. Think of this panel as "what would still be true about this project in a month," which is exactly the same instinct as writing a good README.

---

## Building the Knowledge Base

Arata drags three files into the Project's knowledge base:

1. `run_audiobook.py` — the existing script
2. A sample `chapter_01.txt` — representative input text
3. A short `notes.md` he writes on the spot, listing the punctuation quirks he's hand-tuned over the weeks ("`……` should become a pause, not vanish entirely," "keep `？` — the model reads pitch off it")

Now every new chat he opens inside this Project can reference these without him re-pasting the script or re-explaining his cleaning rules from memory.

> 💡 **Tip:** This is the single biggest quality-of-life difference between working in a Project versus a regular chat. In a normal chat, if you come back three days later, you're re-uploading files and re-explaining context from scratch. Inside a Project, the knowledge base persists — new threads inherit it automatically. It's less like re-opening a notebook and more like walking back into a room where everything is still on the desk where you left it.

He also notices something: the knowledge base isn't limited to files. He pastes in a short block of text — a snippet from the Irodori-TTS repo's README about its CLI flags — directly into a note, no file creation needed.

---

## The First Real Session

Arata starts a new chat inside the Project — his first actual working session — and asks Claude to look at the existing script and suggest what to fix first, in priority order, not all at once.

Claude reads the uploaded script (already in the knowledge base, no re-upload needed) and comes back with a short, ranked list: hardcoded absolute paths, no resume-on-crash, no parallelism, no visibility into progress. It doesn't rewrite the whole thing unprompted — it asks whether he wants to tackle these one at a time or as one larger pass.

He picks "one at a time, config first."

Claude opens a code file in the **Artifacts** panel next to the chat — a side-by-side space where code renders and can be iterated on without cluttering the conversation itself. It proposes a `config.yaml` layer to replace the hardcoded Windows paths, with `pathlib`-based resolution.

> 💡 **Tip:** Artifacts are for anything you'll look at, edit, or reuse — code, documents, structured content — as opposed to short conversational answers. The rule of thumb Arata's settled into: if he'd copy-paste it into a file anyway, it should already *be* a file, rendered where he can see it evolve, not buried in chat scrollback.

He reads the proposed config, doesn't love one part — the silence duration is buried three levels deep in a nested dict — and says so directly: "Flatten that, I want `silence_duration` at the top level." Claude adjusts the artifact in place. No re-explaining the whole file, no re-pasting; the change lands surgically.

---

## Running Code, Not Just Reading It

A few days later, Arata comes back to a new chat in the same Project — a fresh thread, but the config refactor from before is still visible in earlier chat history, and the knowledge base still holds the current script version he uploaded at the end of last session.

He wants to test the *text-cleaning* logic in isolation, before touching anything TTS-related — no GPU needed for that part. Claude writes a small test harness and actually **runs it**, in a sandboxed environment, against a few tricky lines from his sample chapter: nested ellipses, a stray closing bracket, a sentence ending mid-quote.

One case breaks: two ellipses in a row followed by a question mark produces a double pause that shouldn't be there. Claude catches this because it *executed* the code rather than just reasoning about what it should do — the output literally shows `、、？` where it should show `、？`.

> 💡 **Tip:** This is worth calling out explicitly: Claude can write code and separately *run* it, inspect real output, and iterate based on what actually happened — not just what should theoretically happen. For anything with edge cases (text processing, regex, data cleaning), this catches bugs that pure reasoning would miss. Arata makes a habit from here on: for any non-trivial logic change, ask for it to be run against a few real edge cases before calling it done.

They fix the regex order, re-run, and it holds. Claude explains *why* the fix works — the double-replacement issue was about rule ordering, not the regex pattern itself — which turns out to matter, because it's the same category of bug Arata had silently worked around three separate times in the old script without understanding it.

---

## Hitting a Wall, and Asking for Outside Information

A different night, a different problem: the FFmpeg concat step is throwing an error Arata doesn't recognize — something about stream mismatch between the silence file and the generated speech clips. He's not sure if it's a sample-rate issue or a channel-count issue.

Claude doesn't guess from memory alone — recent FFmpeg behavior and flag quirks aren't the kind of thing worth being overconfident about — so it searches the web for the specific error pattern, cross-references it against the `anullsrc` invocation already in the script, and comes back with a specific diagnosis: the TTS output is mono at 24kHz, but the silence generator step doesn't guarantee a matching sample rate if the model version changed its output format.

> 💡 **Tip:** Claude reaching for a web search mid-conversation isn't a fallback for ignorance — it's a specific judgment call: this is the kind of fast-moving, version-specific detail (FFmpeg flags, library APIs, current tool behavior) where "probably right from training data" isn't good enough. For historical facts or stable concepts, no search happens — you'd notice it just answering directly.

The fix is a one-line addition to the `anullsrc` command, explicit about the sample rate. Small, but the kind of thing that would've taken Arata forty minutes of forum archaeology on his own.

---

## Giving the Pipeline a Face

By the third week, the backend is solid: resumable, config-driven, cross-platform-safe paths. Arata mentions, almost offhand, that he'd like *some* way to see progress without staring at a terminal — nothing fancy, since this never leaves his machine.

Claude proposes a small local HTML dashboard — drop a chapter file in, watch a progress bar per sentence, see the final MP3 link when done — and builds it as a live **Artifact**: real HTML and JavaScript, rendered immediately in the side panel, not just described in text. Arata can see the layout before a single file hits his disk.

He doesn't like the color scheme — too corporate-dashboard, not fitting a personal hobby tool — and says so. It's adjusted in the same artifact, in place, without restarting the conversation or losing the working version underneath.

> 💡 **Tip:** For anything visual, seeing beats describing. Arata's rule by now: if he catches himself typing three sentences trying to *describe* what he wants something to look like, that's the signal to just ask for a rough version and react to it instead — corrections are faster than specifications.

Once he's happy with the layout, he asks for the actual files — the finished HTML, the small local Python server to run it, the wiring between the dashboard and the existing pipeline. These get created as real files, not just shown in chat, ready to save to his machine.

> 💡 **Tip:** There's a meaningful difference between an Artifact rendered for *preview* and a *file* meant to be saved and run. Arata learned to ask explicitly once he's happy — "make this a real file I can run" — rather than assuming a nice-looking preview is automatically usable on disk.

---

## The Second Thread: Keeping Things Separate

A week later, Arata wants to try something unrelated to the pipeline itself: batch-renaming years of old chapter files into a consistent naming scheme before feeding them in. Same Project, same overall goal — but a genuinely separate task.

Instead of dragging it into his ongoing "main pipeline" conversation, he opens a **new chat thread** inside the same Project. It still has the custom instructions and the full knowledge base — the script, his notes, the conventions he's settled into — but the conversation itself starts clean, without the accumulated back-and-forth of debugging FFmpeg from two weeks ago cluttering the context.

> 💡 **Tip:** A Project isn't one long conversation — it's a shared foundation (instructions + documents) underneath *many* conversations. When a new task is meaningfully distinct from what you were just doing, a fresh thread is usually better than tacking it onto an old one. It keeps each conversation focused, and it's easier later to find "the renaming discussion" instead of scrolling through an unrelated wall of TTS debugging to find it.

---

## Remembering, Without Being Asked To

Two months in, Arata starts a new session and asks a quick question about "the usual silence duration I settled on." He never restates what that number is.

Claude answers correctly — 0.8 seconds, the value they'd landed on together after he'd complained the default felt like a robot taking a breath mid-sentence — without him re-explaining it. It's not magic; it's context accumulated across the Project's history, surfaced only because it was actually relevant to the question, not recited unprompted as a party trick.

> 💡 **Tip:** This is worth trusting but also worth *steering*. If Claude ever surfaces something from past context that's stale — a preference Arata has since changed, a detail from an abandoned approach — saying so directly ("that's outdated now, I switched to X") is the fastest way to correct it. Persistent context is a convenience, not a fixed record; it's meant to be corrected in passing, the same way you'd correct a colleague who remembered something slightly wrong.

---

## What the Finished Project Looks Like

By the end of it, Arata's Project sidebar shows something that no longer resembles the empty form from Evening One:

- **Custom instructions**, lightly updated twice as his environment details settled
- **A knowledge base** holding the current script, his cleaning-rule notes, and a short glossary of Irodori-TTS flags he got tired of re-searching
- **Several chat threads**, each scoped to one real sub-problem — config refactor, text-cleaning edge cases, FFmpeg stitching, the dashboard, the renaming utility — rather than one sprawling conversation trying to be everything
- **A handful of artifacts** — the dashboard HTML, the config schema, a small test suite — each one something he actually saved and uses, not just admired mid-chat

None of it required him to re-explain his setup from zero more than once. None of it required him to remember, unaided, the exact FFmpeg incantation from three weeks prior. And critically — because he'd been careful from the start about custom instructions and a project description that said *personal, private, rights-owned text only* — every suggestion Claude made along the way stayed grounded in that scope, without him having to repeat the caveat every single time.

He drops a fresh chapter file into his dashboard, watches the progress bar move sentence by sentence, and for the first time in a month, doesn't think about the pipeline at all — just the story it's reading to him.

---

### A short recap of what showed up in this story

| Feature | What it did for Arata |
|---|---|
| **Project name & description** | Anchored every future conversation to the tool's actual purpose and constraints |
| **Custom instructions** | Carried his environment (Windows, `uv`, GPU) and working style into every new thread automatically |
| **Knowledge base (files + pasted text)** | Let him stop re-uploading and re-explaining the same script and notes every session |
| **Multiple chat threads in one Project** | Kept unrelated sub-tasks (renaming vs. TTS debugging) from tangling together |
| **Artifacts** | Gave him a live, editable view of code and a real UI mockup, correctable in place |
| **Code execution** | Caught a real regex bug by running the code against edge cases, not just reasoning about it |
| **Web search** | Diagnosed a version-specific FFmpeg error instead of guessing from stale memory |
| **File creation vs. artifact preview** | Distinguished "look at this" from "give me the real file to run" |
| **Persistent context across sessions** | Recalled a setting he'd tuned weeks earlier, only when it was actually relevant |
| **Direct, corrective feedback** | Kept the whole thing steerable — every wrong guess was fixable in one sentence, not a do-over |
