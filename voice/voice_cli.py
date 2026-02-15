#!/usr/bin/env python3
"""Unified voice CLI: transcribe audio and generate speech.

Usage:
  voice_cli.py listen audio.ogg              # transcribe to text
  voice_cli.py speak "Hello" -o reply.ogg    # text to speech
  voice_cli.py voices                        # list available TTS voices
"""

import argparse
import sys


def cmd_listen(args):
    from whisper_listen import transcribe
    import json

    segments = transcribe(
        args.file, args.model, args.language, args.device,
        args.compute_type, args.beam_size, vad_filter=not args.no_vad,
    )
    if args.json:
        print(json.dumps(segments, ensure_ascii=False, indent=2))
    else:
        print(" ".join(s["text"] for s in segments))


def cmd_speak(args):
    from voice_speak import speak
    speak(args.text, args.output, args.engine, args.voice)
    print(args.output)


def cmd_voices(args):
    from voice_speak import list_voices
    for v in list_voices(args.engine, args.language):
        print(v["name"])


def main():
    p = argparse.ArgumentParser(prog="voice_cli", description="Voice tools: listen & speak")
    sub = p.add_subparsers(dest="command", required=True)

    # -- listen --
    ls = sub.add_parser("listen", help="Transcribe audio to text")
    ls.add_argument("file")
    ls.add_argument("--model", default="tiny")
    ls.add_argument("--language")
    ls.add_argument("--device", default="auto")
    ls.add_argument("--compute-type", default="int8")
    ls.add_argument("--beam-size", type=int, default=1)
    ls.add_argument("--no-vad", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_listen)

    # -- speak --
    sp = sub.add_parser("speak", help="Convert text to audio")
    sp.add_argument("text")
    sp.add_argument("-o", "--output", default="reply.ogg")
    sp.add_argument("--engine", default="openai", choices=["piper", "openai"])
    sp.add_argument("--voice")
    sp.set_defaults(func=cmd_speak)

    # -- voices --
    vo = sub.add_parser("voices", help="List available TTS voices")
    vo.add_argument("--engine", default="openai", choices=["piper", "openai"])
    vo.add_argument("--language")
    vo.set_defaults(func=cmd_voices)

    args = p.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
