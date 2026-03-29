#!/usr/bin/env python3
"""Telegram bot that reports latest temperatures from the SDR server.

Usage:
    python sdr/tg_temps.py [--url http://localhost:8433]

Config: ~/.sdr_tg.json with bot_token, user_id, server_url
Or env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, SDR_SERVER_URL

Commands:
    /temps  — latest readings from all sensors
    /start  — same as /temps
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

log = logging.getLogger("tg_temps")

CONFIG_PATH = Path.home() / ".sdr_tg.json"


def load_config() -> dict:
    """Load config from ~/.sdr_tg.json, fall back to env vars."""
    config = {}
    if CONFIG_PATH.exists():
        log.info("loading config from %s", CONFIG_PATH)
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    else:
        log.info("no config file at %s, using env vars", CONFIG_PATH)
    return {
        "bot_token": config.get("bot_token", os.environ.get("TELEGRAM_BOT_TOKEN", "")),
        "user_id": int(config.get("user_id", os.environ.get("TELEGRAM_USER_ID", "0"))),
        "server_url": config.get("server_url", os.environ.get("SDR_SERVER_URL", "http://localhost:8433")),
    }


cfg = load_config()
BOT_TOKEN = cfg["bot_token"]
ALLOWED_USER = cfg["user_id"]
SERVER_URL = cfg["server_url"]
POLL_TIMEOUT = 30


def tg_request(method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as resp:
        return json.loads(resp.read())


def fetch_temps() -> str:
    log.info("fetching temps from %s/temps", SERVER_URL)
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/temps", timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.error("failed to fetch temps: %s", e)
        return f"Error fetching temps: {e}"

    if not data:
        log.warning("no sensor data returned")
        return "No sensor data available."

    lines = []
    for name, v in sorted(data.items()):
        hum = f"{v['humidity']:.0f}%" if v["humidity"] == v["humidity"] else "n/a"
        t = v["time"].replace("T", " ")[:19]
        lines.append(f"*{name}*: {v['temp_f']:.1f}°F, {hum}\n_{t}_")

    return "\n\n".join(lines)


def handle_update(update: dict):
    msg = update.get("message")
    if not msg:
        log.debug("non-message update: %s", update.get("update_id"))
        return

    user_id = msg.get("from", {}).get("id", 0)
    username = msg.get("from", {}).get("username", str(user_id))
    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]

    log.info("message from %s (id=%d): %s", username, user_id, text)

    if ALLOWED_USER and user_id != ALLOWED_USER:
        log.warning("rejected message from unauthorized user %s (id=%d)", username, user_id)
        return

    if text in ("/temps", "/start", "/temp", "/t"):
        reply = fetch_temps()
        log.info("sending reply to %s (%d chars)", username, len(reply))
        tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": reply,
            "parse_mode": "Markdown",
        })
    else:
        log.info("ignoring unrecognized command: %s", text)


def main():
    global SERVER_URL

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=SERVER_URL)
    args = parser.parse_args()
    SERVER_URL = args.url

    if not BOT_TOKEN:
        log.error("no bot token. set in %s or TELEGRAM_BOT_TOKEN env var", CONFIG_PATH)
        sys.exit(1)
    if not ALLOWED_USER:
        log.error("no user ID. set in %s or TELEGRAM_USER_ID env var", CONFIG_PATH)
        sys.exit(1)

    log.info("config: user_id=%d, server=%s", ALLOWED_USER, SERVER_URL)
    log.info("bot token: %s...%s", BOT_TOKEN[:8], BOT_TOKEN[-4:])

    # Verify token on startup
    try:
        me = tg_request("getMe")
        bot_name = me.get("result", {}).get("username", "unknown")
        log.info("connected to Telegram as @%s", bot_name)
    except Exception as e:
        log.error("failed to connect to Telegram API: %s", e)
        log.error("check bot token in %s", CONFIG_PATH)
        sys.exit(1)

    log.info("polling for updates (timeout=%ds)...", POLL_TIMEOUT)
    offset = 0
    poll_count = 0
    while True:
        try:
            result = tg_request("getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
            })
            poll_count += 1
            updates = result.get("result", [])
            if updates:
                log.info("received %d update(s)", len(updates))
            elif poll_count % 10 == 0:
                log.debug("poll #%d: no updates (still alive)", poll_count)
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)
        except urllib.error.URLError as e:
            log.error("poll error: %s", e)
            time.sleep(5)
        except Exception as e:
            log.error("unexpected error: %s", e, exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
