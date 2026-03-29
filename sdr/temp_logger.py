#!/usr/bin/env python3
"""Log 433 MHz temperature sensor data from rtl_433 to weekly .npz files.

Usage:
    rtl_433 -F json -M time:iso | python sdr/temp_logger.py [output_dir]

Output dir defaults to 'sdr/sdr'. Creates weekly chunk files:
    {dir}/temp_log_YYYYMMDD_YYYYMMDD.npz

Old chunks are deleted when total size exceeds MAX_TOTAL_BYTES (default 8 GB).
"""

import json
import sys
import signal
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB

PLOT_WIDTH = 60
PLOT_HEIGHT = 12
PLOT_POINTS = 60  # last N readings per sensor

SENSOR_SYMBOLS = ["#", "*", "+", "o", "x", "~", "=", "@"]

# ANSI escape helpers
ESC = "\033["
CLEAR_LINE = f"{ESC}2K"
MOVE_UP = f"{ESC}1A"
HIDE_CURSOR = f"{ESC}?25l"
SHOW_CURSOR = f"{ESC}?25h"

prev_plot_lines = 0  # how many lines the last plot used


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def ascii_plot(sensors: dict[str, dict], status: str = ""):
    """Redraw a rolling ASCII temperature plot in-place.

    sensors values are dicts with numpy arrays: time, temp_f, humidity.
    """
    global prev_plot_lines

    err = sys.stderr

    series = {}
    for i, (key, arrs) in enumerate(sorted(sensors.items())):
        temps = arrs["temp_f"][-PLOT_POINTS:]
        if len(temps) > 0:
            label = key.split("_ch")
            short = f"ch{label[1]}" if len(label) > 1 else key[:8]
            series[short] = (temps, SENSOR_SYMBOLS[i % len(SENSOR_SYMBOLS)])

    if not series:
        return

    all_temps = np.concatenate([t for t, _ in series.values()])
    t_min = float(all_temps.min()) - 1
    t_max = float(all_temps.max()) + 1
    if t_max - t_min < 2:
        t_min -= 1
        t_max += 1
    t_range = t_max - t_min

    grid = [[" "] * PLOT_WIDTH for _ in range(PLOT_HEIGHT)]

    for temps, sym in series.values():
        n = len(temps)
        for i, t in enumerate(temps):
            x = int((i / max(n - 1, 1)) * (PLOT_WIDTH - 1))
            y = int((float(t) - t_min) / t_range * (PLOT_HEIGHT - 1))
            y = PLOT_HEIGHT - 1 - y
            y = max(0, min(PLOT_HEIGHT - 1, y))
            x = max(0, min(PLOT_WIDTH - 1, x))
            grid[y][x] = sym

    lines = []
    if status:
        lines.append(status)
    lines.append("")
    for i, row in enumerate(grid):
        if i == 0:
            label = f"{t_max:.0f}°F"
        elif i == PLOT_HEIGHT - 1:
            label = f"{t_min:.0f}°F"
        else:
            label = ""
        lines.append(f"  {label:>6s} |{''.join(row)}|")
    lines.append(f"         +{'-' * PLOT_WIDTH}+")
    legend = "  legend: " + "  ".join(
        f"{sym} {name}" for name, (_, sym) in series.items()
    )
    lines.append(legend)
    lines.append("")

    if prev_plot_lines > 0:
        err.write(f"{ESC}{prev_plot_lines}A")
        err.write(f"{ESC}0G")

    for line in lines:
        err.write(f"{CLEAR_LINE}{line}\n")

    err.flush()
    prev_plot_lines = len(lines)


def week_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Return (Monday 00:00, next Monday 00:00) for the week containing dt."""
    monday = dt - timedelta(days=dt.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start, end


def chunk_path(out_dir: Path, start: datetime, end: datetime) -> Path:
    """Return npz path for a weekly chunk."""
    s = start.strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    return out_dir / f"temp_log_{s}_{e}.npz"


def find_chunks(out_dir: Path) -> list[Path]:
    """Return all chunk .npz files sorted oldest first."""
    return sorted(out_dir.glob("temp_log_????????_????????.npz"))


def total_chunk_size(out_dir: Path) -> int:
    """Sum of all chunk .npz file sizes."""
    return sum(p.stat().st_size for p in find_chunks(out_dir))


def enforce_cap(out_dir: Path):
    """Delete oldest weekly chunks until total size is under MAX_TOTAL_BYTES."""
    while total_chunk_size(out_dir) > MAX_TOTAL_BYTES:
        oldest = find_chunks(out_dir)
        if not oldest:
            break
        victim = oldest[0]
        victim.unlink()
        print(f"[cap] deleted {victim.name} to stay under "
              f"{MAX_TOTAL_BYTES / 1e9:.0f} GB", file=sys.stderr)


def sensor_keys_from_npz(npz) -> list[str]:
    """Extract unique sensor keys from npz array names."""
    keys = set()
    for name in npz.files:
        if name.endswith("_time"):
            keys.add(name[:-5])  # strip _time suffix
    return sorted(keys)


def load_sensors(npz_path: Path) -> dict[str, dict]:
    """Load sensors dict from npz. Returns {sensor_key: {time, temp_f, humidity}}."""
    sensors: dict[str, dict] = {}
    if not npz_path.exists():
        return sensors
    npz = np.load(npz_path)
    for key in sensor_keys_from_npz(npz):
        sensors[key] = {
            "time": npz[f"{key}_time"],
            "temp_f": npz[f"{key}_temp_f"],
            "humidity": npz[f"{key}_humidity"],
        }
    return sensors


def append_record(sensors: dict[str, dict], sensor_key: str,
                  time_str: str, temp_f: float, humidity: float):
    """Append a single record to the in-memory sensors dict."""
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


def save_chunk(npz_path: Path, sensors: dict[str, dict]):
    """Write sensors dict to npz."""
    npz_data = {}
    for key, arrs in sensors.items():
        npz_data[f"{key}_time"] = arrs["time"]
        npz_data[f"{key}_temp_f"] = arrs["temp_f"]
        npz_data[f"{key}_humidity"] = arrs["humidity"]
    np.savez_compressed(npz_path, **npz_data)
    total = sum(len(a["time"]) for a in sensors.values())
    print(f"[saved] {total} records -> {npz_path.name}", file=sys.stderr)


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "sdr/sdr")
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    week_start, week_end = week_bounds(now)
    npz_path = chunk_path(out_dir, week_start, week_end)

    sensors = load_sensors(npz_path)
    if sensors:
        n = sum(len(a["time"]) for a in sensors.values())
        print(f"Loaded {n} existing records from {npz_path.name}", file=sys.stderr)

    used = total_chunk_size(out_dir)
    n_chunks = len(find_chunks(out_dir))
    print(f"Storage: {used / 1e6:.1f} MB across {n_chunks} chunk(s), "
          f"cap {MAX_TOTAL_BYTES / 1e9:.0f} GB", file=sys.stderr)

    seen: dict[str, tuple[str, float]] = {}
    count = 0

    def save():
        save_chunk(npz_path, sensors)

    def cleanup():
        sys.stderr.write(SHOW_CURSOR)
        sys.stderr.flush()

    def handle_signal(sig, frame):
        cleanup()
        print("\nShutting down...", file=sys.stderr)
        save()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    sys.stderr.write(HIDE_CURSOR)
    print("Waiting for sensor data on stdin...", file=sys.stderr)

    latest: dict[str, str] = {}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        temp_c = msg.get("temperature_C")
        if temp_c is None:
            continue

        model = msg.get("model", "unknown")
        channel = msg.get("channel", "?")
        sensor_key = f"{model}_ch{channel}"
        time_str = msg.get("time", datetime.now().isoformat())
        humidity = msg.get("humidity", float("nan"))

        # Check if we've crossed into a new week
        try:
            msg_dt = datetime.fromisoformat(time_str)
        except ValueError:
            msg_dt = datetime.now()

        if msg_dt >= week_end:
            save()
            enforce_cap(out_dir)
            week_start, week_end = week_bounds(msg_dt)
            npz_path = chunk_path(out_dir, week_start, week_end)
            sensors = load_sensors(npz_path)
            print(f"[rotate] new chunk: {npz_path.name}", file=sys.stderr)

        # Deduplicate
        dedup = (time_str[:19], temp_c)
        if seen.get(sensor_key) == dedup:
            continue
        seen[sensor_key] = dedup

        temp_f = round(c_to_f(temp_c), 2)
        append_record(sensors, sensor_key, time_str, temp_f, humidity)
        count += 1

        ts = time_str[11:19] if len(time_str) >= 19 else time_str
        short = f"ch{channel}"
        latest[short] = f"{temp_f:.1f}°F {humidity:.0f}%"

        status = f"  [{ts}]  " + "  ".join(
            f"{k}: {v}" for k, v in sorted(latest.items())
        )
        total = sum(len(a["time"]) for a in sensors.values())
        status += f"  ({total} records)"

        if count % max(len(sensors), 1) == 0 or count % 3 == 0:
            ascii_plot(sensors, status)

        if count % 10 == 0:
            save()

    cleanup()
    save()


if __name__ == "__main__":
    main()
