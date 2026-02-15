#!/usr/bin/env python3
"""TTS module. Agent API: speak(text, output) -> path"""

import subprocess
import sys
import tempfile
from pathlib import Path


def speak(text: str, output: str = "reply.ogg", engine: str = "openai", voice: str | None = None) -> str:
    """Convert text to audio file. Returns output path."""
    engines = {"piper": _piper, "openai": _openai}
    if engine not in engines:
        raise ValueError(f"Unknown engine: {engine}. Use: {', '.join(engines)}")
    engines[engine](text, voice, output)
    return output


def _piper(text: str, voice: str | None, output: str) -> None:
    import wave
    from piper import PiperVoice

    model_path = voice or "en_US-lessac-medium"
    pv = PiperVoice.load(model_path)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(pv.config.sample_rate)
            # Access the pcm bytes from audio_int16_bytes
            for chunk in pv.synthesize(text):
                wf.writeframes(chunk.audio_int16_bytes)
    _convert(tmp_path, output)
    Path(tmp_path).unlink(missing_ok=True)


def _openai(text: str, voice: str | None, output: str) -> None:
    import os
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    fmt = {".mp3": "mp3", ".wav": "wav", ".flac": "flac"}.get(Path(output).suffix.lower(), "opus")
    resp = client.audio.speech.create(model="tts-1", voice=voice or "alloy", input=text, response_format=fmt)
    resp.stream_to_file(output)


def _convert(src: str, dst: str) -> None:
    if Path(src).suffix == Path(dst).suffix:
        Path(dst).write_bytes(Path(src).read_bytes())
    else:
        subprocess.run(["ffmpeg", "-y", "-i", src, dst], check=True, capture_output=True)


def list_voices(engine: str = "openai", language: str | None = None) -> list[dict]:
    """List available voices. For human/interactive use."""
    if engine == "openai":
        return [
            {"name": "alloy"}, {"name": "ash"}, {"name": "ballad"}, {"name": "coral"},
            {"name": "echo"}, {"name": "fable"}, {"name": "nova"}, {"name": "onyx"},
            {"name": "sage"}, {"name": "shimmer"}, {"name": "verse"},
        ]
    if engine == "piper":
        import json, urllib.request
        with urllib.request.urlopen("https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json", timeout=10) as r:
            catalog = json.loads(r.read())
        voices = [{"name": k, "language": v.get("language", {}).get("name_english", "")} for k, v in sorted(catalog.items())]
        if language:
            q = language.lower()
            voices = [v for v in voices if q in v["name"].lower() or q in v["language"].lower()]
        return voices
    return []


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TTS: speak(text, output)")
    p.add_argument("text", nargs="?")
    p.add_argument("-o", "--output", default="reply.ogg")
    p.add_argument("--engine", default="openai", choices=["piper", "openai"])
    p.add_argument("--voice")
    p.add_argument("--list-voices", action="store_true")
    p.add_argument("--language")
    a = p.parse_args()

    if a.list_voices:
        for v in list_voices(a.engine, a.language):
            print(v["name"])
        sys.exit(0)

    if not a.text:
        p.error("text required (or --list-voices)")

    try:
        speak(a.text, a.output, a.engine, a.voice)
        print(a.output)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
