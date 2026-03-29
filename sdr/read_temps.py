#!/usr/bin/env python3
"""Read latest temperature sensor data from .npz chunk files.

Usage:
    python sdr/read_temps.py [options] [data_dir]

Options:
    -n N        Show last N readings per sensor (default: 1)
    -a, --all   Show all sensors (default: only latest per sensor)
    --json      Output as JSON
    data_dir    Directory containing .npz chunks (default: sdr/sdr)
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def find_chunks(data_dir: Path) -> list[Path]:
    chunks = sorted(data_dir.glob("temp_log_????????_????????.npz"))
    if not chunks and (data_dir / "temp_log.npz").exists():
        chunks = [data_dir / "temp_log.npz"]
    return chunks


def load_latest(data_dir: Path, n: int = 1) -> dict[str, list[dict]]:
    """Load last N records per sensor from the most recent chunk(s)."""
    result: dict[str, list[dict]] = {}
    # Walk chunks newest-first, stop once every sensor has enough records
    for chunk_path in reversed(find_chunks(data_dir)):
        npz = np.load(chunk_path)
        keys = sorted({name[:-5] for name in npz.files if name.endswith("_time")})
        for key in keys:
            if key in result and len(result[key]) >= n:
                continue
            times = npz[f"{key}_time"]
            temps = npz[f"{key}_temp_f"]
            humids = npz[f"{key}_humidity"]
            need = n - len(result.get(key, []))
            tail = slice(-need, None) if need < len(times) else slice(None)
            records = [
                {"time": str(t), "temp_f": float(tf), "humidity": float(h)}
                for t, tf, h in zip(times[tail], temps[tail], humids[tail])
            ]
            result.setdefault(key, [])
            result[key] = records + result[key]  # prepend older records
    return result


def main():
    parser = argparse.ArgumentParser(description="Read temperature sensor data")
    parser.add_argument("data_dir", nargs="?", default="sdr/sdr",
                        help="Directory with .npz chunks (default: sdr/sdr)")
    parser.add_argument("-n", type=int, default=1,
                        help="Last N readings per sensor (default: 1)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"No data directory: {data_dir}", file=sys.stderr)
        sys.exit(1)

    chunks = find_chunks(data_dir)
    if not chunks:
        print("No .npz chunk files found.", file=sys.stderr)
        sys.exit(1)

    readings = load_latest(data_dir, n=args.n)
    if not readings:
        print("No sensor data found.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json.dump(readings, sys.stdout, indent=2)
        print()
        return

    # Pretty-print
    for sensor, records in sorted(readings.items()):
        label = sensor.replace("_", " ")
        print(f"\n  {label}")
        print(f"  {'─' * 50}")
        for r in records:
            t = r["time"].replace("T", " ")
            hum = f"{r['humidity']:.0f}%" if not np.isnan(r["humidity"]) else "n/a"
            print(f"    {t}  {r['temp_f']:6.1f}°F  {hum:>4s}")

    # Summary line
    total_chunks = len(chunks)
    total_size = sum(p.stat().st_size for p in chunks)
    total_records = 0
    npz = np.load(chunks[-1])
    for name in npz.files:
        if name.endswith("_time"):
            total_records += len(npz[name])
    print(f"\n  {total_chunks} chunk(s), {total_size / 1e6:.1f} MB total, "
          f"{total_records} records in latest chunk")


if __name__ == "__main__":
    main()
