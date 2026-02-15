#!/usr/bin/env python3
"""Speech-to-text CLI using faster-whisper. Transcribes audio files (ogg, wav, mp3, etc.)."""

import argparse
import json
import sys

from faster_whisper import WhisperModel


def transcribe(
    file_path: str,
    model_size: str,
    language: str | None,
    device: str,
    compute_type: str,
    beam_size: int,
    vad_filter: bool,
) -> list[dict]:
    """Transcribe an audio file, returning a list of segment dicts."""
    print(f"Loading model '{model_size}' ({compute_type}) on {device}...", file=sys.stderr)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    print(f"Transcribing '{file_path}'...", file=sys.stderr)
    segments, info = model.transcribe(
        file_path,
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
    )

    results = []
    for seg in segments:
        results.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
            "confidence": round(seg.avg_logprob, 4),
        })
    return results


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio using faster-whisper")
    parser.add_argument("file", help="Path to audio file (ogg, wav, mp3, etc.)")
    parser.add_argument("--model", default="tiny", help="Whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument("--language", default=None, help="Language hint (e.g. 'en', 'ru') — speeds up detection")
    parser.add_argument("--device", default="auto", help="Compute device (cpu/cuda/auto)")
    parser.add_argument("--compute-type", default="int8", help="Quantization (int8/float16/float32)")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size (1=greedy, fastest)")
    parser.add_argument("--no-vad", action="store_true", help="Disable VAD filter (on by default)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON with timestamps and confidence")
    args = parser.parse_args()

    try:
        segments = transcribe(
            args.file, args.model, args.language, args.device,
            args.compute_type, args.beam_size, vad_filter=not args.no_vad,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.as_json:
        print(json.dumps(segments, ensure_ascii=False, indent=2))
    else:
        print(" ".join(seg["text"] for seg in segments))


if __name__ == "__main__":
    main()
