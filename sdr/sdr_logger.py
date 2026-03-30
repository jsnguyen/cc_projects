#!/usr/bin/env python3
"""SDR temperature logger — reads rtl_433 JSON from stdin, writes to SQLite.

Usage:
    rtl_433 -F json -M time:iso | python sdr/sdr_logger.py [--dir sdr/sdr]
"""

import argparse
import json
import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

log = logging.getLogger("sdr_logger")

MAX_DB_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
CAP_CHECK_INTERVAL = 300  # check size cap every 5 minutes

CHANNEL_NAMES = {
    "0": "Bedroom",
    "1": "Living Room",
    "2": "Garage",
}


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def channel_name(sensor_key: str) -> str:
    parts = sensor_key.split("_ch")
    ch = parts[1] if len(parts) > 1 else sensor_key
    return CHANNEL_NAMES.get(ch, f"Channel {ch}")


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            sensor TEXT NOT NULL,
            time   TEXT NOT NULL,
            temp_f REAL NOT NULL,
            humidity REAL,
            PRIMARY KEY (sensor, time)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_time ON readings(time)")
    conn.commit()
    return conn


def enforce_cap(conn: sqlite3.Connection, db_path: Path):
    try:
        size = db_path.stat().st_size
    except OSError:
        return
    if size <= MAX_DB_BYTES:
        return
    # Delete oldest 10% of rows
    total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    if total == 0:
        return
    delete_count = max(1, total // 10)
    conn.execute("""
        DELETE FROM readings WHERE rowid IN (
            SELECT rowid FROM readings ORDER BY time LIMIT ?
        )
    """, (delete_count,))
    conn.commit()
    log.info("cap: deleted %d oldest rows (db was %.1f MB)", delete_count, size / 1e6)


def main():
    parser = argparse.ArgumentParser(description="SDR temperature logger")
    parser.add_argument("--dir", default="sdr/sdr", help="Directory for temps.db")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "temps.db"

    conn = init_db(db_path)
    n = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    size = db_path.stat().st_size
    log.info("opened %s: %d rows, %.1f MB", db_path, n, size / 1e6)

    # Track latest readings in memory for log display
    latest: dict[str, dict] = {}

    def shutdown(sig, frame):
        log.info("shutdown signal received (sig=%d)", sig)
        try:
            sys.stdin.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        log.info("exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    seen: dict[str, tuple[str, float]] = {}
    count = 0
    last_cap_check = time.monotonic()

    log.info("listening for sensor data on stdin...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        count += 1
        if count == 1:
            log.info("first stdin line received")

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.warning("invalid JSON: %s", line[:100])
            continue

        temp_c = msg.get("temperature_C")
        if temp_c is None:
            if count <= 5:
                log.debug("non-temp message: model=%s", msg.get("model", "?"))
            continue

        model = msg.get("model", "unknown")
        channel = msg.get("channel", "?")
        sensor_key = f"{model}_ch{channel}"
        time_str = msg.get("time", datetime.now().isoformat())
        humidity = msg.get("humidity")

        # Deduplicate
        dedup = (time_str[:19], temp_c)
        if seen.get(sensor_key) == dedup:
            continue
        seen[sensor_key] = dedup

        temp_f = round(c_to_f(temp_c), 2)

        # Insert into DB
        try:
            conn.execute(
                "INSERT OR REPLACE INTO readings (sensor, time, temp_f, humidity) VALUES (?, ?, ?, ?)",
                (sensor_key, time_str, temp_f, humidity),
            )
            conn.commit()
        except sqlite3.Error as e:
            log.error("db insert failed: %s", e)
            continue

        # Update latest for logging
        latest[sensor_key] = {"temp_f": temp_f, "humidity": humidity, "time": time_str}

        # Log latest readings
        ts = time_str[11:19] if len(time_str) >= 19 else time_str
        parts = []
        for key in sorted(latest):
            v = latest[key]
            name = channel_name(key)
            h = v["humidity"]
            h_str = f"{h:.0f}%" if h is not None else "n/a"
            parts.append(f"{name}: {v['temp_f']:.1f}°F {h_str}")
        log.info("%s  %s", ts, "  |  ".join(parts))

        # Periodic cap check
        if time.monotonic() - last_cap_check >= CAP_CHECK_INTERVAL:
            try:
                enforce_cap(conn, db_path)
            except Exception as e:
                log.error("cap enforcement error: %s", e)
            last_cap_check = time.monotonic()

    log.warning("stdin EOF — no more data")
    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
