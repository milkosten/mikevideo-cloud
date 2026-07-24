#!/usr/bin/env python3
"""Standalone faster-whisper transcriber — invoked as a SUBPROCESS by the
transcription worker (server/transcribe.py), the same way ffmpeg is shelled out.

Why a subprocess: the ~1.5 GB CTranslate2 model memory is isolated and freed when
this process exits, a crash can't take down the API, and the FastAPI process never
imports faster-whisper. Quality-first (this is a background batch job that blocks
nothing): Whisper large-v3, full beam search, VAD, word-level timestamps.

Reads a 16 kHz mono WAV path, writes a JSON result to --out:
    {"language","language_probability","duration","segments":[{start,end,text,
      words:[{start,end,word,probability}]}],"text"}

Never prints the transcript to stdout (faster-whisper logging is silenced); the
worker reads the --out file. Exit 0 on success (incl. no-speech → empty segments).
"""
import argparse
import json
import logging
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="path to a 16kHz mono WAV")
    ap.add_argument("--out", required=True, help="path to write the JSON result")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--cpu-threads", type=int, default=4,
                    help="cap CPU threads so we stay a gentle neighbour to the OSM stack")
    ap.add_argument("--cache", default=os.environ.get("ASR_MODEL_CACHE", "/data/mikevideo/models"))
    ap.add_argument("--language", default="", help="force a language code, else auto-detect")
    args = ap.parse_args()

    # Keep every library quiet — only our JSON file is the output contract.
    logging.disable(logging.WARNING)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # Belt-and-braces thread caps (CTranslate2 also reads cpu_threads below).
    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, args.cpu_threads)))

    from faster_whisper import WhisperModel  # imported here so the API never loads it

    model = WhisperModel(
        args.model, device="cpu", compute_type=args.compute_type,
        cpu_threads=max(1, args.cpu_threads), download_root=args.cache or None)

    segments_gen, info = model.transcribe(
        args.audio,
        language=(args.language or None),
        beam_size=args.beam_size,
        vad_filter=True,                    # bundled Silero VAD → kills silence/music hallucinations
        word_timestamps=True,               # click-to-seek search + precise subtitles
        condition_on_previous_text=True,
    )

    segments = []
    words_total = 0
    for s in segments_gen:                  # generator → iterating runs the transcription
        words = []
        for w in (s.words or []):
            words.append({
                "start": round(float(w.start), 3),
                "end": round(float(w.end), 3),
                "word": w.word,
                "probability": round(float(w.probability), 4),
            })
        words_total += len(words)
        segments.append({
            "start": round(float(s.start), 3),
            "end": round(float(s.end), 3),
            "text": s.text.strip(),
            "words": words,
        })

    result = {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4)
        if info.language_probability is not None else None,
        "duration": round(float(info.duration), 3) if info.duration is not None else None,
        "segments": segments,
        "word_count": words_total,
        "text": " ".join(s["text"] for s in segments if s["text"]).strip(),
    }

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, args.out)               # atomic — the worker only ever sees a complete file
    return 0


if __name__ == "__main__":
    sys.exit(main())
