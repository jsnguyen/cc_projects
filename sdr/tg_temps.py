#!/usr/bin/env python3
"""Telegram bot that reports latest temperatures from the SDR server.

Usage:
    TELEGRAM_BOT_TOKEN=xxx TELEGRAM_USER_ID=123 python sdr/tg_temps.py [--url http://localhost:8433]

Commands:
    /temps  — latest readings from all sensors
    /start  — same as /temps
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_PATH = Path.home() / ".sdr_tg.json"


def load_config() -> dict:
    """Load config from ~/.sdr_tg.json, fall back to env vars."""
    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
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
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/temps", timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return f"Error fetching temps: {e}"

    if not data:
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
        return

    user_id = msg.get("from", {}).get("id", 0)
    if ALLOWED_USER and user_id != ALLOWED_USER:
        return

    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]

    if text in ("/temps", "/start", "/temp", "/t"):
        reply = fetch_temps()
        tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": reply,
            "parse_mode": "Markdown",
        })


def main():
    global SERVER_URL

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=SERVER_URL)
    args = parser.parse_args()
    SERVER_URL = args.url

    if not BOT_TOKEN:
        print(f"No bot token. Set in {CONFIG_PATH} or TELEGRAM_BOT_TOKEN env var.",
              file=sys.stderr)
        sys.exit(1)
    if not ALLOWED_USER:
        print(f"No user ID. Set in {CONFIG_PATH} or TELEGRAM_USER_ID env var.",
              file=sys.stderr)
        sys.exit(1)

    # Verify token on startup
    try:
        me = tg_request("getMe")
        bot_name = me.get("result", {}).get("username", "unknown")
        print(f"Bot @{bot_name} started (user_id={ALLOWED_USER}, server={SERVER_URL})")
    except Exception as e:
        print(f"ERROR: could not connect to Telegram API: {e}", file=sys.stderr)
        print(f"Check your bot token in {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    offset = 0
    while True:
        try:
            result = tg_request("getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
            })
            updates = result.get("result", [])
            if updates:
                print(f"[tg] received {len(updates)} update(s)")
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                user = msg.get("from", {})
                text = msg.get("text", "")
                print(f"[tg] {user.get('username', user.get('id', '?'))}: {text}")
                handle_update(update)
        except urllib.error.URLError as e:
            print(f"[tg] poll error: {e}", file=sys.stderr)
            time.sleep(5)
        except Exception as e:
            print(f"[tg] error: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
