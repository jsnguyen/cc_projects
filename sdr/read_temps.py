#!/usr/bin/env python3
"""Read latest temperature sensor data from SQLite database.

Usage:
    python sdr/read_temps.py [options] [data_dir]

Options:
    -n N        Show last N readings per sensor (default: 1)
    -a, --all   Show all sensors (default: only latest per sensor)
    --json      Output as JSON
    data_dir    Directory containing temps.db (default: sdr/sdr)
"""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

CHANNEL_NAMES = {
    "0": "Bedroom",
    "1": "Living Room",
    "2": "Garage",
}


def channel_name(sensor_key: str) -> str:
    parts = sensor_key.split("_ch")
    ch = parts[1] if len(parts) > 1 else sensor_key
    return CHANNEL_NAMES.get(ch, f"Channel {ch}")


def load_latest(db_path: Path, n: int = 1) -> dict[str, list[dict]]:
    """Load last N records per sensor."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    sensors = [r[0] for r in conn.execute("SELECT DISTINCT sensor FROM readings ORDER BY sensor")]
    result: dict[str, list[dict]] = {}
    for sensor in sensors:
        rows = conn.execute(
            "SELECT time, temp_f, humidity FROM readings WHERE sensor = ? ORDER BY time DESC LIMIT ?",
            (sensor, n),
        ).fetchall()
        result[sensor] = [
            {"time": r["time"], "temp_f": r["temp_f"], "humidity": r["humidity"]}
            for r in reversed(rows)  # oldest first
        ]

    conn.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="Read temperature sensor data")
    parser.add_argument("data_dir", nargs="?", default="sdr/sdr",
                        help="Directory with temps.db (default: sdr/sdr)")
    parser.add_argument("-n", type=int, default=1,
                        help="Last N readings per sensor (default: 1)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    db_path = Path(args.data_dir) / "temps.db"
    if not db_path.exists():
        print(f"No database: {db_path}", file=sys.stderr)
        sys.exit(1)

    readings = load_latest(db_path, n=args.n)
    if not readings:
        print("No sensor data found.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json.dump(readings, sys.stdout, indent=2)
        print()
        return

    # Pretty-print
    for sensor, records in sorted(readings.items()):
        label = channel_name(sensor)
        print(f"\n  {label} ({sensor})")
        print(f"  {'─' * 50}")
        for r in records:
            t = r["time"].replace("T", " ")
            h = r["humidity"]
            h_str = f"{h:.0f}%" if h is not None and not math.isnan(h) else "n/a"
            print(f"    {t}  {r['temp_f']:6.1f}°F  {h_str:>4s}")

    # Summary
    try:
        conn = sqlite3.connect(str(db_path))
        total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        span = conn.execute("SELECT MIN(time), MAX(time) FROM readings").fetchone()
        conn.close()
        size = db_path.stat().st_size
        print(f"\n  {total} rows, {size / 1e6:.1f} MB")
        if span[0]:
            print(f"  range: {span[0]} → {span[1]}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
