"""
irodori_batch.py
----------------
Generates every chunk of ONE chapter from a single process, loading the
Irodori-TTS model once instead of once per chunk.

Why this exists: run_audiobook.py used to shell out to infer.py per chunk,
and infer.py loads the DiT checkpoint, the DACVAE codec and SilentCipher
before it can say a single sentence. Measured on this machine (2026-09-14):
19.1 s per chunk, of which only ~1.9 s was generation - about 91% of a run
was re-loading the same model. Loading once and looping brings a 314-chunk
chapter from roughly 100 minutes down to about 11.

It is a separate script rather than code inside run_audiobook.py because
run_audiobook.py stays stdlib-only (see CLAUDE.md); this one imports torch
and irodori_tts, so it runs in the Irodori-TTS venv - which is also where
run_audiobook.py itself runs, so no extra environment is involved. One
worker per CHAPTER, not per book: a crash can then only cost one chapter,
and the model is released from the card before translation wants it.

Input is a job file written by run_audiobook.py:

    {
      "uv_project_dir": "C:/Irodori-TTS",     # put on sys.path for the import
      "checkpoint": "Aratako/Irodori-TTS-v4.1-Small",
      "checkpoint_is_hf": true,
      "speaker_path": "...speaker.safetensors",
      "duration_scale": 1.1,
      "trim_tail": true,
      "watermark": true,        # false skips SilentCipher
      "seed": null,                            # null = fresh draw per chunk,
      "jobs": [{"index": 1, "text": "...",     #        exactly as before
                "output_wav": "...wav"}, ...]
    }

Protocol on stdout, one line each, flushed immediately so the GUI progress
window keeps moving (see CLAUDE.md on why unbuffered output matters):

    MODEL_LOADED <seconds>
    CHUNK_START <index>
    CHUNK_DONE <index> <used_seed>
    CHUNK_FAIL <index> <message>
    BATCH_DONE ok=<n> fail=<n>

Everything the libraries print themselves (SilentCipher's per-chunk notices,
torch warnings, HF chatter) is redirected into <job file>.log instead, so the
run log stays readable. A failing chunk still reports its exception on the
CHUNK_FAIL line, and the log path is printed at the end.
"""

import argparse
import contextlib
import json
import os
import sys
import time
import traceback

# The real stdout, kept before the libraries' output is redirected away.
_EMIT = sys.stdout


def emit(message):
    _EMIT.write(message + "\n")
    _EMIT.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True,
                        help="Path to the job file described in the module docstring.")
    args = parser.parse_args()

    with open(args.jobs, "r", encoding="utf-8") as f:
        spec = json.load(f)

    # The script lives in the GUI project but imports irodori_tts, which sits
    # in the Irodori-TTS project - running by absolute path puts THIS file's
    # folder on sys.path, not the working directory, so say it explicitly.
    sys.path.insert(0, spec["uv_project_dir"])

    log_path = args.jobs + ".log"
    jobs = spec["jobs"]
    ok = 0
    failed = 0

    with open(log_path, "w", encoding="utf-8") as logf:
        with contextlib.redirect_stdout(logf), contextlib.redirect_stderr(logf):
            started = time.perf_counter()
            from irodori_tts.inference_runtime import (
                InferenceRuntime,
                RuntimeKey,
                SamplingRequest,
                default_runtime_device,
                download_hf_checkpoint,
                resolve_cfg_scales,
                save_wav,
            )

            checkpoint = spec["checkpoint"]
            if spec.get("checkpoint_is_hf"):
                checkpoint = download_hf_checkpoint(checkpoint)

            runtime = InferenceRuntime.from_key(RuntimeKey(
                checkpoint=checkpoint,
                model_device=default_runtime_device(),
                codec_device=default_runtime_device(),
            ))
            emit(f"MODEL_LOADED {time.perf_counter() - started:.1f}")

            if not spec.get("watermark", True):
                # InferenceRuntime.__init__ always builds the watermarker,
                # so the cheapest honest way to skip it is to drop the
                # loaded model: watermark.py treats that as "unavailable"
                # and passes the audio through untouched. That skips both
                # the embed (~60-200 ms per chunk on the GPU) and the
                # 48k -> 44.1k -> 48k resample round trip SilentCipher
                # does around it, which is the part that actually touches
                # the waveform.
                runtime.watermarker.model = None
                emit("WATERMARK off")

            # infer.py resolves the cfg scales through this same call before
            # building its request; the SamplingRequest defaults alone are not
            # equivalent, because the resolution depends on which conditions
            # the checkpoint actually uses.
            cfg_text, cfg_caption, cfg_speaker, _messages = resolve_cfg_scales(
                cfg_guidance_mode="independent",
                cfg_scale_text=3.0,
                cfg_scale_caption=3.0,
                cfg_scale_speaker=5.0,
                cfg_scale=None,
                use_caption_condition=False,
                use_speaker_condition=bool(
                    runtime.model_cfg.use_speaker_condition_resolved),
            )

            seed = spec.get("seed")
            # SamplingRequest.max_seconds defaults to 30.0 and the runtime
            # CLAMPS the predicted duration to it
            # (inference_runtime.py: latent_steps = max(min_frames,
            # min(max_frames, latent_steps))). A chunk whose text needs
            # longer is not truncated - it is crammed into 30 s, which is
            # what garbles the middle of very long chunks. 68 chunks in the
            # published library sit at exactly 30.00 s for this reason.
            #
            # Absent from a job file, nothing changes: the engine default
            # applies, exactly as every chapter rendered so far. Only the
            # repair tool sets it, and only for a chunk that needs it.
            extra = {}
            if spec.get("max_seconds"):
                extra["max_seconds"] = float(spec["max_seconds"])
                emit(f"MAX_SECONDS {extra['max_seconds']:.1f}")

            for job in jobs:
                index = int(job["index"])
                emit(f"CHUNK_START {index}")
                try:
                    result = runtime.synthesize(SamplingRequest(
                        text=job["text"],
                        ref_embed=spec["speaker_path"],
                        duration_scale=float(spec["duration_scale"]),
                        trim_tail=bool(spec["trim_tail"]),
                        seed=None if seed is None else int(seed),
                        cfg_scale_text=cfg_text,
                        cfg_scale_caption=cfg_caption,
                        cfg_scale_speaker=cfg_speaker,
                        **extra,
                    ))
                    save_wav(job["output_wav"], result.audio, result.sample_rate)
                    emit(f"CHUNK_DONE {index} {result.used_seed}")
                    ok += 1
                except Exception as exc:
                    # One bad chunk must not cost the rest of the chapter -
                    # the per-chunk-process version skipped and carried on,
                    # and so does this. The traceback goes to the log.
                    traceback.print_exc()
                    emit(f"CHUNK_FAIL {index} {type(exc).__name__}: {exc}")
                    failed += 1
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass

    emit(f"BATCH_DONE ok={ok} fail={failed}")
    if failed:
        emit(f"BATCH_LOG {log_path}")
    # Non-zero only when nothing at all was produced: a chapter with some
    # chunks missing is still worth stitching, exactly as before.
    return 1 if jobs and ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
