"""
make_masters.py
---------------
Builds a FLAC master beside every published chapter:

    F:\\AUDIOBOOK-HOST-AAC\\<book>\\chapter_NNN.m4a
        ->  F:\\AUDIOBOOK-HOST-MASTER\\<book>\\chapter_NNN.flac

## What these masters are, and are not

They are NOT lossless originals. The audio inside is what the `.m4a`
decodes to, so everything AAC threw away at 64k is already gone and no
amount of FLAC will bring it back.

What they are is a fixed point. Repairing a chapter means decoding it,
splicing in new audio and encoding again - and doing that repeatedly to
an `.m4a` stacks a fresh generation of AAC loss every time. With a master,
every repair is exactly ONE generation away from today's audio however
many times a chapter is repaired, and the chunk being spliced in arrives
pristine because it comes straight from the TTS.

Chapters rendered after the generator learns to keep its own master get a
genuinely lossless one; these are the best that can be done for the
library that already exists.

## Verification

Each master is checked, not assumed: ffmpeg emits the FLAC and the MD5 of
the source's decoded PCM in ONE pass, then the FLAC is decoded and its
MD5 compared. Both sides are forced to s16 so the comparison is of the
same thing. A mismatch leaves the file in place and is reported - it has
never happened in testing, and if it ever does it means something is
wrong that a silent retry would hide.

Every result is recorded in `_masters.json` at the destination root, so a
second run skips what is already verified and a later audit can tell when
each master was made and from what.

Usage:
    uv run --project ../seiyuu-audition python make_masters.py
    uv run --project ../seiyuu-audition python make_masters.py --force
    uv run --project ../seiyuu-audition python make_masters.py --dry-run

(It is stdlib-only, so any Python will do; the project flag just picks a
convenient interpreter.)
"""

import argparse
import json
import os
import subprocess
import sys
import time

SRC_ROOT = os.path.join("F:" + os.sep, "AUDIOBOOK-HOST-AAC")
DST_ROOT = os.path.join("F:" + os.sep, "AUDIOBOOK-HOST-MASTER")
MANIFEST = "_masters.json"

# Level 8 measured at ~600x realtime on this machine and 3.8% smaller than
# level 5, so the slower setting costs seconds across the whole library.
COMPRESSION_LEVEL = "8"


def log(message):
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


def find_chapters(src_root):
    """Every .m4a under <src_root>/<book>/, as (book, filename, full path)."""
    found = []
    for book in sorted(os.listdir(src_root)):
        book_dir = os.path.join(src_root, book)
        if not os.path.isdir(book_dir):
            continue
        for name in sorted(os.listdir(book_dir)):
            if name.lower().endswith(".m4a"):
                found.append((book, name, os.path.join(book_dir, name)))
    return found


def parse_md5(output):
    """ffmpeg's md5 muxer writes `MD5=<hex>`."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("MD5="):
            return line[4:]
    return None


def encode_master(src, dst):
    """Writes the FLAC and returns the MD5 of the SOURCE's decoded PCM.

    One invocation, two outputs, one decode: the flac encoder and the md5
    muxer both read the same decoded stream. `-sample_fmt s16` on the FLAC
    matters - left to itself ffmpeg may store s32, and the verification
    decode would then not match the s16 taken here."""
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", src,
        "-map", "0:a", "-c:a", "flac", "-sample_fmt", "s16",
        "-compression_level", COMPRESSION_LEVEL, dst,
        "-map", "0:a", "-c:a", "pcm_s16le", "-f", "md5", "-",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed: "
                           + (result.stderr or "").strip()[-400:])
    digest = parse_md5(result.stdout)
    if not digest:
        raise RuntimeError("ffmpeg produced no MD5 for " + src)
    return digest


def decoded_md5(path):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", path,
        "-c:a", "pcm_s16le", "-f", "md5", "-",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed reading " + path)
    return parse_md5(result.stdout)


def load_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_manifest(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=SRC_ROOT)
    parser.add_argument("--dst", default=DST_ROOT)
    parser.add_argument("--force", action="store_true",
                        help="Rebuild masters that are already verified.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.src):
        sys.exit(f"Source folder not found: {args.src}")
    os.makedirs(args.dst, exist_ok=True)

    manifest_path = os.path.join(args.dst, MANIFEST)
    manifest = load_manifest(manifest_path)

    chapters = find_chapters(args.src)
    log(f"{len(chapters)} chapter(s) under {args.src}")

    done = skipped = failed = 0
    src_bytes = dst_bytes = 0
    started = time.time()

    for position, (book, name, src) in enumerate(chapters, start=1):
        rel = f"{book}/{os.path.splitext(name)[0]}.flac"
        dst = os.path.join(args.dst, book, os.path.splitext(name)[0] + ".flac")
        record = manifest.get(rel)
        src_size = os.path.getsize(src)

        # A master counts as current when it exists, was verified, and the
        # .m4a has not changed size since - a repaired chapter therefore
        # correctly rebuilds its master.
        if (not args.force and record and record.get("verified")
                and os.path.isfile(dst)
                and record.get("source_size") == src_size):
            skipped += 1
            continue

        if args.dry_run:
            log(f"[{position}/{len(chapters)}] would build {rel}")
            continue

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            source_md5 = encode_master(src, dst)
            master_md5 = decoded_md5(dst)
            if source_md5 != master_md5:
                failed += 1
                log(f"[{position}/{len(chapters)}] ! {rel} MISMATCH "
                    f"(source {source_md5}, master {master_md5}) - kept for "
                    f"inspection, NOT marked verified")
                manifest[rel] = {"verified": False, "source_md5": source_md5,
                                 "master_md5": master_md5,
                                 "source_size": src_size}
                continue
        except (RuntimeError, OSError) as e:
            failed += 1
            log(f"[{position}/{len(chapters)}] ! {rel} failed: {e}")
            continue

        flac_size = os.path.getsize(dst)
        manifest[rel] = {
            "verified": True,
            "pcm_md5": source_md5,
            "source": f"{book}/{name}",
            "source_size": src_size,
            "master_size": flac_size,
            "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_manifest(manifest_path, manifest)     # resumable after a kill
        done += 1
        src_bytes += src_size
        dst_bytes += flac_size
        log(f"[{position}/{len(chapters)}] {rel}  "
            f"{src_size/2**20:.1f} MB -> {flac_size/2**20:.1f} MB  verified")

    elapsed = time.time() - started
    log("-" * 60)
    log(f"built {done}, skipped {skipped}, failed {failed}, "
        f"in {elapsed/60:.1f} min")
    if done:
        log(f"{src_bytes/2**20:.0f} MB of .m4a -> {dst_bytes/2**30:.2f} GB "
            f"of FLAC")
    log(f"manifest: {manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
