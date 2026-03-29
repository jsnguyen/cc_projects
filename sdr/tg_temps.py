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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("TELEGRAM_USER_ID", "0"))
SERVER_URL = "http://localhost:8433"
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
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN env var", file=sys.stderr)
        sys.exit(1)
    if not ALLOWED_USER:
        print("Set TELEGRAM_USER_ID env var", file=sys.stderr)
        sys.exit(1)

    # Parse optional --url arg
    global SERVER_URL
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=SERVER_URL)
    args = parser.parse_args()
    SERVER_URL = args.url

    print(f"Bot started (user_id={ALLOWED_USER}, server={SERVER_URL})")

    offset = 0
    while True:
        try:
            result = tg_request("getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
            })
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
        except urllib.error.URLError as e:
            print(f"[tg] poll error: {e}", file=sys.stderr)
            time.sleep(5)
        except Exception as e:
            print(f"[tg] error: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
