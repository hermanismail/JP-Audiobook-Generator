"""End-to-end test of the repair on a COPY of a real chapter.

Never touches the published library: a sandbox library and master root are
built from one chapter, repaired there, and checked. The "replacement" is
a segment cut from elsewhere in the same chapter, so the test needs no GPU
and still exercises splice, re-encode, tagging, faststart and every
timestamp in sync.json and the .srt.
"""
import json
import os
import shutil
import subprocess
import sys

import repair

SAND = os.path.join("F:" + os.sep, "tmp", "repair-sandbox")
LIB = os.path.join(SAND, "lib")
MASTER = os.path.join(SAND, "master")
BOOK = "after-dark"
BASE = "chapter_001"

if os.path.isdir(SAND):
    shutil.rmtree(SAND)
os.makedirs(os.path.join(LIB, BOOK))
os.makedirs(os.path.join(MASTER, BOOK))

real = repair.load_settings()
src_lib = os.path.join(real["library_root"], BOOK)
for ext in (".m4a", ".sync.json", ".srt"):
    shutil.copy2(os.path.join(src_lib, BASE + ext),
                 os.path.join(LIB, BOOK, BASE + ext))
shutil.copy2(os.path.join(real["master_root"], BOOK, BASE + ".flac"),
             os.path.join(MASTER, BOOK, BASE + ".flac"))

settings = dict(real)
settings.update({"library_root": LIB, "master_root": MASTER,
                 "backup_root": os.path.join(SAND, "backup"),
                 "work_root": os.path.join(SAND, "work")})
chapter = repair.Chapter(settings, BOOK, BASE)

before = chapter.load_sync()["chunks"]
before_cues = repair.read_srt(chapter.srt_path)
before_tags = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format_tags",
     "-of", "json", chapter.m4a], capture_output=True, text=True).stdout
INDEX = 40
target = before[INDEX]
print(f"chapter: {len(before)} chunks, {len(before_cues)} cues, "
      f"total {before[-1]['end']}s")
print(f"repairing #{INDEX}: {target['start']} -> {target['end']} "
      f"({target['end'] - target['start']:.3f}s)")

# The "regenerated" chunk: a DIFFERENT chunk's audio, so the swap is
# audible and the duration genuinely changes.
donor = before[41]
replacement = os.path.join(SAND, "replacement.wav")
repair.extract_segment(chapter.audio_source(), donor["start"], donor["end"],
                       replacement)
new_dur = repair.audio_duration(replacement)
print(f"replacement: {new_dur:.3f}s")

plan = repair.plan_repair(chapter, INDEX, replacement)
print("plan:", json.dumps(plan, indent=2, default=str)[:400])

result = repair.apply_repair(chapter, INDEX, replacement, print, plan)
print("result:", result)

# ----------------------------------------------------------------- checks
failures = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


after = chapter.load_sync()["chunks"]
after_cues = repair.read_srt(chapter.srt_path)
delta = plan["delta"]

check("chunk count unchanged", len(after) == len(before))
check("chunks before the repair are untouched",
      all(after[i] == before[i] for i in range(INDEX)))
check("repaired chunk keeps its start",
      abs(after[INDEX]["start"] - before[INDEX]["start"]) < 1e-9)
check("repaired chunk has the new duration",
      abs((after[INDEX]["end"] - after[INDEX]["start"]) - new_dur) < 0.0011,
      f"{after[INDEX]['end'] - after[INDEX]['start']:.3f} vs {new_dur:.3f}")
check("every later chunk shifted by exactly delta",
      all(abs((after[i]["start"] - before[i]["start"]) - delta) < 0.0011
          and abs((after[i]["end"] - before[i]["end"]) - delta) < 0.0011
          for i in range(INDEX + 1, len(after))))
check("text never changed",
      all(a["text"] == b["text"] for a, b in zip(after, before)))
check("timeline still monotonic",
      all(after[i]["end"] <= after[i + 1]["start"] + 1e-9
          for i in range(len(after) - 1)))

gaps_before = [round(before[i + 1]["start"] - before[i]["end"], 3)
               for i in range(len(before) - 1)]
gaps_after = [round(after[i + 1]["start"] - after[i]["end"], 3)
              for i in range(len(after) - 1)]
check("silence gaps preserved exactly", gaps_before == gaps_after)

check("srt cue count unchanged", len(after_cues) == len(before_cues))
check("srt text never changed",
      all(a["text"] == b["text"] for a, b in zip(after_cues, before_cues)))
mismatched = [i for i, (cue, chunk) in enumerate(zip(after_cues, after))
              if abs(cue["start"] - chunk["start"]) > 0.0011
              or abs(cue["end"] - chunk["end"]) > 0.0011]
check("srt still mirrors sync.json", not mismatched,
      f"{len(mismatched)} mismatched" if mismatched else "")

real_duration = repair.audio_duration(chapter.m4a)
check("m4a duration matches the new last end",
      abs(real_duration - after[-1]["end"]) < 0.05,
      f"{real_duration:.3f} vs {after[-1]['end']:.3f}")
master_duration = repair.audio_duration(chapter.master)
check("master duration matches the m4a",
      abs(master_duration - real_duration) < 0.05,
      f"{master_duration:.3f} vs {real_duration:.3f}")

atoms = repair.atom_order(chapter.m4a)
check("faststart preserved", "moov" in atoms and "mdat" in atoms
      and atoms.index("moov") < atoms.index("mdat"), str(atoms[:4]))

after_tags = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format_tags",
     "-of", "json", chapter.m4a], capture_output=True, text=True).stdout
b = json.loads(before_tags).get("format", {}).get("tags", {})
a = json.loads(after_tags).get("format", {}).get("tags", {})
check("title survived the re-encode", a.get("title") == b.get("title"),
      repr(a.get("title")))
check("artist and album survived",
      a.get("artist") == b.get("artist") and a.get("album") == b.get("album"))
from mutagen.mp4 import MP4
check("cover art survived", bool(MP4(chapter.m4a).tags.get("covr")))

backup = result["backup"]
check("backup holds all four files",
      sorted(os.listdir(backup)) == sorted(
          [BASE + ".m4a", BASE + ".sync.json", BASE + ".srt", BASE + ".flac"]),
      str(sorted(os.listdir(backup))))


# ------------------------------------------------- batch of three at once
# The single-repair arithmetic above is the easy case. A batch is where a
# running shift can be applied to the wrong chunk, or an edit spliced out
# of order, so it gets its own pass on a fresh copy.
print()
print("=== batch: three chunks in one pass ===")
shutil.rmtree(os.path.join(SAND, "work"), ignore_errors=True)
for ext in (".m4a", ".sync.json", ".srt"):
    shutil.copy2(os.path.join(src_lib, BASE + ext),
                 os.path.join(LIB, BOOK, BASE + ext))
shutil.copy2(os.path.join(real["master_root"], BOOK, BASE + ".flac"),
             os.path.join(MASTER, BOOK, BASE + ".flac"))

chapter = repair.Chapter(settings, BOOK, BASE)
base_chunks = chapter.load_sync()["chunks"]
base_cues = repair.read_srt(chapter.srt_path)

# Deliberately queued OUT OF ORDER, and with donors of different lengths,
# so both the sorting and the per-edit deltas are exercised.
pairs = [(70, 12), (5, 60), (33, 34)]
selections = []
for position, (target, donor) in enumerate(pairs):
    wav = os.path.join(SAND, f"batch_{position}.wav")
    repair.extract_segment(chapter.audio_source(), base_chunks[donor]["start"],
                           base_chunks[donor]["end"], wav)
    selections.append({"index": target, "wav": wav})

plan = repair.plan_repairs(chapter, selections)
print("queued:", plan["indices"], "overall", f"{plan['delta']:+.3f}s")
repair.apply_repairs(chapter, selections, lambda m: print("   ", m), plan)

after = chapter.load_sync()["chunks"]
after_cues = repair.read_srt(chapter.srt_path)
edits = {e["index"]: e for e in plan["edits"]}

check("batch: queued in sorted order", plan["indices"] == [5, 33, 70])
check("batch: chunk count unchanged", len(after) == len(base_chunks))
check("batch: text never changed",
      all(a["text"] == b["text"] for a, b in zip(after, base_chunks)))
check("batch: chunks before the first repair are untouched",
      all(after[i] == base_chunks[i] for i in range(5)))

# Every chunk must carry the running sum of the deltas of the repairs
# strictly before it, and a repaired chunk must take its new duration.
ok_shift = True
shift = 0.0
for i, (a, b) in enumerate(zip(after, base_chunks)):
    expected_start = round(b["start"] + shift, 3)
    if abs(a["start"] - expected_start) > 0.0011:
        ok_shift = False
        print(f"      chunk {i}: start {a['start']} expected {expected_start}")
        break
    if i in edits:
        shift += edits[i]["new_duration"] - (edits[i]["end"] - edits[i]["start"])
check("batch: every chunk carries the running shift", ok_shift)

check("batch: each repaired chunk has its new duration",
      all(abs((after[i]["end"] - after[i]["start"]) - edits[i]["new_duration"])
          < 0.0011 for i in edits))
check("batch: timeline still monotonic",
      all(after[i]["end"] <= after[i + 1]["start"] + 1e-9
          for i in range(len(after) - 1)))
check("batch: silence gaps preserved exactly",
      [round(base_chunks[i + 1]["start"] - base_chunks[i]["end"], 3)
       for i in range(len(base_chunks) - 1)]
      == [round(after[i + 1]["start"] - after[i]["end"], 3)
          for i in range(len(after) - 1)])
check("batch: total matches the plan",
      abs(after[-1]["end"] - plan["new_total"]) < 0.0011,
      f"{after[-1]['end']} vs {plan['new_total']}")

check("batch: srt text never changed",
      all(a["text"] == b["text"] for a, b in zip(after_cues, base_cues)))
bad = [i for i, (cue, chunk) in enumerate(zip(after_cues, after))
       if abs(cue["start"] - chunk["start"]) > 0.0011
       or abs(cue["end"] - chunk["end"]) > 0.0011]
check("batch: srt still mirrors sync.json", not bad,
      f"{len(bad)} mismatched" if bad else "")

batch_duration = repair.audio_duration(chapter.m4a)
check("batch: m4a duration matches the new last end",
      abs(batch_duration - after[-1]["end"]) < 0.05,
      f"{batch_duration:.3f} vs {after[-1]['end']:.3f}")
atoms = repair.atom_order(chapter.m4a)
check("batch: faststart preserved", "moov" in atoms and "mdat" in atoms
      and atoms.index("moov") < atoms.index("mdat"))
check("batch: cover art survived", bool(MP4(chapter.m4a).tags.get("covr")))

try:
    repair.plan_repairs(chapter, [{"index": 5, "wav": selections[0]["wav"]},
                                  {"index": 5, "wav": selections[1]["wav"]}])
    check("batch: the same chunk twice is refused", False)
except ValueError:
    check("batch: the same chunk twice is refused", True)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
