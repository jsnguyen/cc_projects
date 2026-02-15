# Telegram Agent Tools

CLI tools and API wrappers for a Telegram AI agent.

## astro/ — Astronomy Research Tools

| Script | Description |
|--------|-------------|
| `arxiv.py` | Search arXiv papers via the public API with local caching |
| `arxiv_digest.py` | Daily astro-ph digest — scrapes new submissions filtered by `topics.txt` |
| `scix.py` | Search NASA ADS / SciX papers (requires `ADS_API_TOKEN`) |
| `scixhub.py` | Full SciX/ADS API wrapper (search, export, metrics, citations, etc.) |
| `simbad.py` | SIMBAD astronomical database API wrapper (TAP/ADQL) |
| `simbad_search.py` | CLI to search SIMBAD by object name, coordinates, or criteria |
| `topics.txt` | Topic filters for `arxiv_digest.py` (one per line) |

## voice/ — Voice Tools (STT + TTS)

| Script | Description |
|--------|-------------|
| `voice_cli.py` | Unified CLI — `listen`, `speak`, and `voices` subcommands |
| `whisper_listen.py` | Speech-to-text using faster-whisper (ogg/wav/mp3 → text) |
| `voice_speak.py` | Text-to-speech module with piper (local) and OpenAI engines |

**Quick start:**
```bash
# Transcribe audio
python voice/voice_cli.py listen message.ogg

# Generate speech
python voice/voice_cli.py speak "Hello" -o reply.ogg

# List voices
python voice/voice_cli.py voices
```

**Agent API:**
```python
from voice.voice_speak import speak
speak("Hello", "reply.ogg")
```

## weather/ — Weather Forecast

| Script | Description |
|--------|-------------|
| `weather.py` | Fetch weather forecast for a US location by name (NWS API) |
