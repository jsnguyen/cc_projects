#!/usr/bin/env python3
"""SDR temperature logger — reads rtl_433 JSON from stdin, writes weekly .npz chunks.

Usage:
    rtl_433 -F json -M time:iso | python sdr/sdr_logger.py [--dir sdr/sdr]
"""

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

log = logging.getLogger("sdr_logger")

MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
SAVE_INTERVAL = 60  # seconds between disk flushes

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


def week_bounds(dt: datetime) -> tuple[datetime, datetime]:
    monday = dt - timedelta(days=dt.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def chunk_path(out_dir: Path, start: datetime, end: datetime) -> Path:
    s = start.strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    return out_dir / f"temp_log_{s}_{e}.npz"


def find_chunks(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("temp_log_????????_????????.npz"))


def total_chunk_size(out_dir: Path) -> int:
    return sum(p.stat().st_size for p in find_chunks(out_dir))


def enforce_cap(out_dir: Path):
    while total_chunk_size(out_dir) > MAX_TOTAL_BYTES:
        oldest = find_chunks(out_dir)
        if not oldest:
            break
        oldest[0].unlink()
        log.info("cap: deleted %s", oldest[0].name)


def load_sensors(npz_path: Path) -> dict[str, dict]:
    sensors: dict[str, dict] = {}
    if not npz_path.exists():
        log.info("no existing chunk at %s", npz_path.name)
        return sensors
    try:
        npz = np.load(npz_path)
    except Exception as e:
        log.error("failed to load %s: %s — starting fresh", npz_path.name, e)
        return sensors
    keys = sorted({n[:-5] for n in npz.files if n.endswith("_time")})
    for key in keys:
        try:
            sensors[key] = {
                "time": npz[f"{key}_time"],
                "temp_f": npz[f"{key}_temp_f"],
                "humidity": npz[f"{key}_humidity"],
            }
        except Exception as e:
            log.error("corrupt arrays for %s in %s: %s — skipping", key, npz_path.name, e)
            continue
    log.info("loaded %d sensors from %s", len(keys), npz_path.name)
    return sensors


def save_sensors(sensors: dict, out_dir: Path, week_start: datetime, week_end: datetime):
    npz_path = chunk_path(out_dir, week_start, week_end)
    npz_data = {}
    for key, arrs in sensors.items():
        npz_data[f"{key}_time"] = arrs["time"]
        npz_data[f"{key}_temp_f"] = arrs["temp_f"]
        npz_data[f"{key}_humidity"] = arrs["humidity"]
    if npz_data:
        # Atomic write: temp file -> rename, so readers never see partial data
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=out_dir)
        os.close(fd)
        try:
            np.savez_compressed(tmp, **npz_data)
            os.replace(tmp, npz_path)
        except Exception:
            os.unlink(tmp)
            raise
        total = sum(len(a["time"]) for a in sensors.values())
        log.info("save: %d records -> %s", total, npz_path.name)
    else:
        log.info("save: nothing to write")


def append_record(sensors: dict, sensor_key: str, time_str: str, temp_f: float, humidity: float):
    safe = sensor_key.replace(" ", "_")
    if safe not in sensors:
        sensors[safe] = {
            "time": np.array([], dtype="U25"),
            "temp_f": np.array([], dtype=np.float32),
            "humidity": np.array([], dtype=np.float32),
        }
    s = sensors[safe]
    s["time"] = np.append(s["time"], time_str)
    s["temp_f"] = np.append(s["temp_f"], np.float32(temp_f))
    s["humidity"] = np.append(s["humidity"], np.float32(humidity))


def main():
    parser = argparse.ArgumentParser(description="SDR temperature logger")
    parser.add_argument("--dir", default="sdr/sdr", help="Directory for .npz chunks")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    week_start, week_end = week_bounds(now)
    npz = chunk_path(out_dir, week_start, week_end)
    sensors = load_sensors(npz)

    n = sum(len(a["time"]) for a in sensors.values())
    used = total_chunk_size(out_dir)
    n_chunks = len(find_chunks(out_dir))
    log.info("loaded %d records from %s", n, npz.name)
    log.info("storage: %.1f MB across %d chunk(s), cap %.0f GB",
             used / 1e6, n_chunks, MAX_TOTAL_BYTES / 1e9)

    def save():
        save_sensors(sensors, out_dir, week_start, week_end)

    def shutdown(sig, frame):
        log.info("shutdown signal received (sig=%d)", sig)
        try:
            sys.stdin.close()
        except Exception:
            pass
        try:
            save()
        except Exception as e:
            log.error("save on shutdown failed: %s", e)
        log.info("exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    seen: dict[str, tuple[str, float]] = {}
    count = 0
    last_save = time.monotonic()

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
        humidity = msg.get("humidity", float("nan"))

        # Deduplicate
        dedup = (time_str[:19], temp_c)
        if seen.get(sensor_key) == dedup:
            continue
        seen[sensor_key] = dedup

        # Check week boundary
        try:
            msg_dt = datetime.fromisoformat(time_str)
        except ValueError:
            msg_dt = datetime.now()

        if msg_dt >= week_end:
            save()
            enforce_cap(out_dir)
            week_start, week_end = week_bounds(msg_dt)
            sensors = load_sensors(chunk_path(out_dir, week_start, week_end))
            log.info("rotate: new chunk %s", chunk_path(out_dir, week_start, week_end).name)

        temp_f = round(c_to_f(temp_c), 2)
        append_record(sensors, sensor_key, time_str, temp_f, humidity)

        # Log latest readings
        ts = time_str[11:19] if len(time_str) >= 19 else time_str
        parts = []
        for key in sorted(sensors):
            arrs = sensors[key]
            if len(arrs["time"]) == 0:
                continue
            name = channel_name(key)
            t = float(arrs["temp_f"][-1])
            h = float(arrs["humidity"][-1])
            parts.append(f"{name}: {t:.1f}°F {h:.0f}%")
        log.info("%s  %s", ts, "  |  ".join(parts))

        # Periodic save
        if time.monotonic() - last_save >= SAVE_INTERVAL:
            try:
                save()
            except Exception as e:
                log.error("save error: %s", e)
            last_save = time.monotonic()

    log.warning("stdin EOF — no more data")
    save()


if __name__ == "__main__":
    main()
