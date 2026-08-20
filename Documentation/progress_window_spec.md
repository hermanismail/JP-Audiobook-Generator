# Progress Window — Design & Build Spec
**JP-Audiobook-Generator — replaces the raw PowerShell console on "Save & Run"**

Status: **Approved** (2026-08-19) — implementation in progress, following the build order in §6.

---

## 1. What the mockup already gets right

Reviewed `Documentation/progress_window.png` (both the "In Progress" and "Completed" states). This was already a solid, modern layout — no need to start over. Specifically it already follows current best practice for long-running-task UI:

- **Determinate progress + live context text** ("Processing: chapter_055 … 72%") instead of a bare spinner — pairing a percentage with *what* is happening reduces perceived wait time far more than a spinner alone.
- **Color-coded status pill** (orange "In Progress" → green "Completed") — correct use of color + icon to distinguish states at a glance.
- **Stat chips row** (Total Chapters / Completed / In Progress / Elapsed Time) — a compact "bento-card" summary, the dominant 2026 dashboard layout pattern for dense-but-scannable stats.
- **Dark, monospace, timestamped, color-tagged log panel** — matches how installers/CI tools (VS Code, Docker Desktop, GitHub Desktop) present verbose output without it looking like a raw terminal.
- **State-appropriate footer actions** (disabled Close while running → Open Output Folder + Close when done).

## 2. Recommended enhancements

| # | Enhancement | Why |
|---|---|---|
| 1 | Add a **Cancel** button while running (not just a greyed-out Close) | Never leave a user with *no way out* of a running process — a disabled Close with nothing else is exactly that trap. Cancel gracefully terminates the subprocess and marks the log with `>>> Cancelled by user`. |
| 2 | Keep the **Process Log fully expanded by default** — don't collapse it into a "Show details" toggle like a typical consumer installer | The chunking/silence logic is still evolving (trailing-punctuation bug, bracket-silence stacking, etc.), so the raw log *is* the debugging tool, not clutter. |
| 3 | Add a thin **secondary "overall" bar** (all chapters) above the per-chapter bar for multi-chapter runs | The primary bar tracks progress *within* the current chapter; a second slim bar gives a true sense of "2 of 12 chapters" for bigger books. |
| 4 | Keep animation minimal (bar fill + one spinner, nothing more) | 2026 UI guidance increasingly flags motion as something to justify, not default to. |
| 5 | Native Windows notification on completion | Since a full run can take a while, a Windows 11 Action Center toast means you don't have to babysit the window. Optional, see §5C. |

## 3. Layout checklist (implementation reference)

- [x] Header: state icon badge, title, subtitle, status pill (top-right)
- [x] Primary progress bar: determinate, with % and "Processing: <chapter/chunk>" label
- [x] Secondary bar: overall chapters-complete progress (multi-chapter runs only)
- [x] Stat chip row: Total Chapters / Completed / In Progress / Elapsed·Total Time
- [x] Process Log: scrollable, monospace, color-tagged by line type, auto-scroll, "Clear Log", expanded by default
- [x] Footer: **Cancel** (running) → **Open Output Folder + Close** (completed) → **Close** (failed/cancelled)
- [x] Window behavior: non-modal (no `grab_set()`), owns its own close-button confirmation while running

## 4. Pros & cons of this direction

**Pros**
- Big usability jump over a raw PowerShell window — readable, on-brand, matches the rest of the CustomTkinter GUI
- Determinate progress + log together means both reassurance (bar/%) and the debugging detail actually relied on
- Same window pattern is reusable later for batch/queue runs
- No exotic dependencies required — buildable entirely on the existing stack

**Cons / trade-offs**
- More code than a console redirect: threading + queue plumbing + log formatting is a genuinely new subsystem
- Determinate progress requires the pipeline to report structured progress (`PROGRESS <done> <total> <label>`), not just prose log lines — the one pipeline-side change the feature depends on
- A long `CTkTextbox` log can get slow after hundreds of lines — mitigated with a rendered-line cap (see §6, step 1: `MAX_LOG_LINES`, trims the widget only, full record still goes to disk)
- `CTkToplevel` secondary windows can be a little fiddly around focus/always-on-top on Windows — worth a smoke test
- Toast notifications are Windows-only and need an AppUserModelID set for correct icon/branding — small one-time setup cost

## 5. Module choices

### A. Window & widgets — CustomTkinter (no new dependency)
`CTkToplevel` + `CTkProgressBar` + `CTkTextbox` + `CTkFrame` cards, reusing `gui_settings.py`'s existing design tokens and `IconBadge` widget directly (`progress_window.py` imports them rather than redefining). ttkbootstrap/sv_ttk considered and rejected — different toolkit than CustomTkinter, would look inconsistent.

### B. Subprocess → GUI streaming
`subprocess.Popen(cmd, stdout=PIPE, stderr=STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, text=True)` piped into a background `threading.Thread` → `queue.Queue`, drained on the Tk main thread via `self.after(100, ...)`. `run_audiobook.py` gains one extra `PROGRESS <done> <total> <label>` print alongside its existing logging.

### C. Optional OS-level notification
`windows-toasts` (actively maintained, no `pywin32`) or `win11toast` (simpler API, live-updating progress toast). Lives in the GUI venv only, never in the Irodori-TTS venv. `win10toast`/`win10toast-click` explicitly not recommended (unmaintained, unreliable on Windows 11).

## 6a. Visual review round (2026-08-19, post-mockup)

A few refinements came out of iterating on the HTML mockup against a live screenshot of step 1's actual CTk output, applied directly to `progress_window.py`:

- Dropped the secondary "overall chapters" bar entirely — it duplicated the Total Chapters/Completed stat chips. Only the single per-chapter bar remains, now 12px tall (was 10px) and color-coded by state via `BAR_COLORS`: blue `#4DA6FF` while running, green `#2FB668` once completed (failed/cancelled reuse those states' own red/gray). Previously both bars were a fixed purple regardless of state.
- Header title/subtitle and the chapter-progress row (chapter label, current-item label, percent) sized up ~20% for readability.
- Stat chips rebuilt: icon + number now sit side by side as one centered unit (previously icon above a left-aligned number), cards keep themselves perfectly square via a `<Configure>` binding (CTk has no native aspect-ratio), padding is tighter so content actually fills the card, and caption text is ~40% bigger than the original (two successive +20% passes). The Time chip specifically drops its icon so the `HH:MM:SS` value centers in the full card — `_format_duration` already always zero-pads to a fixed 8 characters, so this is a look, not a functional overflow fix.

## 6. Build order

1. **[Done]** `progress_window.py` — standalone `ProgressWindow` (CTkToplevel), fed by scripted mock data via its own `__main__` demo block. No pipeline changes. Run with `uv run python progress_window.py` to visually compare against `progress_window.png`. See §6a for the visual-review refinements folded in after the initial pass.
2. Wire real subprocess redirection (`CREATE_NO_WINDOW` + pipe + thread + queue) into `gui_settings.py`'s `on_save_and_run()` — the PowerShell popup disappears, raw log lines flow into the panel, no accurate percentage yet.
3. Add the `PROGRESS n/total label` line to `run_audiobook.py` so the bar/%/stat chips reflect the real run.
4. Wire `on_cancel` (terminate the subprocess) / `on_open_output` (open the output folder) into the real `ProgressWindow` instance created in step 2.
5. *(Optional, later)* layer in `win11toast`/`windows-toasts` for a completion notification.

---

### Research references
- UX Playbook — [UI Design Best Practices 2026](https://uxplaybook.org/articles/ui-fundamentals-best-practices-for-ux-designers)
- Appcues — [Modal window design best practices](https://www.appcues.com/blog/modal-dialog-windows)
- Midrocket — [UI Design Trends 2026](https://midrocket.com/en/guides/ui-design-trends-2026/)
- Python Tutorial — [Progressbar + threading in Tkinter](https://www.pythontutorial.net/tkinter/tkinter-thread-progressbar/)
- DataCamp — [Progress Bars in Python](https://www.datacamp.com/tutorial/progress-bars-in-python)
- Python docs — [`subprocess` Windows creation flags incl. `CREATE_NO_WINDOW`](https://github.com/python/cpython/pull/4150/files)
- PyPI — [`win11toast`](https://pypi.org/project/win11toast/) · [`windows-toasts`](https://windows-toasts.readthedocs.io/)
