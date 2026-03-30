#!/usr/bin/env python3
"""SDR temperature web dashboard — reads .npz chunks from disk, serves live dashboard.

Usage:
    python sdr/sdr_web.py [--port 8433] [--dir sdr/sdr]

Endpoints:
    GET /       — D3.js dashboard (last 24 hours)
    GET /temps  — JSON of latest readings per sensor
    GET /stream — Server-Sent Events for real-time updates
"""

import argparse
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

log = logging.getLogger("sdr_web")

# --- Config ---

POLL_INTERVAL = 10  # seconds between disk reads
SSE_TIMEOUT = 300   # drop idle SSE connections after 5 minutes

CHANNEL_NAMES = {
    "0": "Bedroom",
    "1": "Living Room",
    "2": "Garage",
}

# --- Shared state ---

_lock = threading.Lock()
_sensors: dict[str, dict] = {}
_version = 0
_out_dir = Path("sdr/sdr")


# --- Numpy storage helpers ---

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


def load_sensors_from_disk(out_dir: Path) -> dict[str, dict]:
    """Load sensors from chunks covering the last 7 days."""
    now = datetime.now()
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    sensors: dict[str, dict] = {}

    # Load current week + previous week (covers any 7-day window)
    week_start, week_end = week_bounds(now)
    paths_to_load = [chunk_path(out_dir, week_start, week_end)]
    prev_start = week_start - timedelta(days=7)
    prev_end = week_start
    prev = chunk_path(out_dir, prev_start, prev_end)
    if prev.exists():
        paths_to_load.insert(0, prev)

    for npz_path in paths_to_load:
        if not npz_path.exists():
            continue
        try:
            npz = np.load(npz_path)
        except Exception as e:
            log.error("failed to load %s: %s", npz_path.name, e)
            continue
        keys = sorted({n[:-5] for n in npz.files if n.endswith("_time")})
        for key in keys:
            t = npz[f"{key}_time"]
            tf = npz[f"{key}_temp_f"]
            h = npz[f"{key}_humidity"]
            if key in sensors:
                # Append to existing
                sensors[key]["time"] = np.concatenate([sensors[key]["time"], t])
                sensors[key]["temp_f"] = np.concatenate([sensors[key]["temp_f"], tf])
                sensors[key]["humidity"] = np.concatenate([sensors[key]["humidity"], h])
            else:
                sensors[key] = {"time": t, "temp_f": tf, "humidity": h}

    # Filter to last 24h only
    for key in list(sensors):
        mask = sensors[key]["time"] >= cutoff
        if not mask.any():
            del sensors[key]
            continue
        sensors[key] = {
            "time": sensors[key]["time"][mask],
            "temp_f": sensors[key]["temp_f"][mask],
            "humidity": sensors[key]["humidity"][mask],
        }

    return sensors


# --- Data access ---

def channel_name(sensor_key: str) -> str:
    parts = sensor_key.split("_ch")
    ch = parts[1] if len(parts) > 1 else sensor_key
    return CHANNEL_NAMES.get(ch, f"Channel {ch}")


def get_latest_temps() -> dict:
    with _lock:
        sensors = _sensors
    result = {}
    for key in sorted(sensors):
        arrs = sensors[key]
        if len(arrs["time"]) == 0:
            continue
        result[channel_name(key)] = {
            "temp_f": round(float(arrs["temp_f"][-1]), 2),
            "humidity": round(float(arrs["humidity"][-1]), 1),
            "time": str(arrs["time"][-1]),
        }
    return result


def get_last_7d() -> dict:
    with _lock:
        snapshot = {k: {f: v.copy() for f, v in arrs.items()} for k, arrs in _sensors.items()}
    result = {}
    for key, arrs in sorted(snapshot.items()):
        if len(arrs["time"]) == 0:
            continue
        result[key] = [
            {"time": str(t), "temp_f": round(float(tf), 2), "humidity": round(float(h), 1)}
            for t, tf, h in zip(arrs["time"], arrs["temp_f"], arrs["humidity"])
        ]
    return result


# --- Disk poller thread ---

def disk_poller():
    """Periodically reload sensor data from npz files on disk."""
    global _sensors, _version
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            new_sensors = load_sensors_from_disk(_out_dir)
            # Check if data actually changed
            new_count = sum(len(a["time"]) for a in new_sensors.values())
            with _lock:
                old_count = sum(len(a["time"]) for a in _sensors.values())
            if new_count != old_count:
                with _lock:
                    _sensors = new_sensors
                    _version += 1
                log.info("reload: %d records from disk", new_count)
        except Exception as e:
            log.error("disk poll error: %s", e)


# --- HTML dashboard ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Temperature Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: "Menlo", "Consolas", monospace; }
  h1 { text-align: center; padding: 16px 0 4px; font-size: 1.2em; color: #c8d6e5; font-weight: 400; }
  .status { text-align: center; font-size: 0.75em; color: #556; margin-bottom: 8px; }
  .status.live { color: #2ecc71; }
  .current { display: flex; justify-content: center; gap: 40px; padding: 8px 0 16px; }
  .sensor-card {
    background: #16213e; border: 1px solid #333; border-radius: 8px;
    padding: 14px 28px; text-align: center; min-width: 180px;
  }
  .sensor-card .label { font-size: 0.8em; color: #889; margin-bottom: 4px; }
  .sensor-card .temp { font-size: 2em; font-weight: 600; }
  .sensor-card .humid { font-size: 0.85em; color: #778899; margin-top: 2px; }
  .sensor-card .time { font-size: 0.7em; color: #556; margin-top: 4px; }
  .sensor-card .ago { font-size: 0.7em; color: #666; margin-top: 2px; }
  #chart { width: 100%; display: flex; justify-content: center; }
  .tooltip {
    position: absolute; pointer-events: none; background: rgba(10,10,30,0.95);
    border: 1px solid #556; border-radius: 4px; padding: 8px 12px;
    font-size: 12px; line-height: 1.6; color: #ddd; white-space: nowrap;
  }
</style>
</head>
<body>
<h1>Temperature &mdash; Last 7 Days (12h Phase Fold)</h1>
<div class="status" id="status">connecting...</div>
<div class="current" id="current"></div>
<div id="chart"></div>
<div class="tooltip" id="tip" style="display:none;"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
var COLORS = ["#ff6b6b", "#48dbfb", "#feca57", "#a29bfe", "#fd79a8", "#55efc4"];
var CHANNEL_NAMES = {"0": "Bedroom", "1": "Living Room", "2": "Garage"};

var margin = { top: 20, right: 30, bottom: 50, left: 62 };
var W, H, w, h, svg, g, x, y, xAxisG, yAxisG, tip;

function initChart() {
  W = Math.min(1200, window.innerWidth - 40);
  H = 440;
  w = W - margin.left - margin.right;
  h = H - margin.top - margin.bottom;

  svg = d3.select("#chart").append("svg").attr("width", W).attr("height", H);
  g = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  x = d3.scaleLinear().domain([0, 12]).range([0, w]);
  y = d3.scaleLinear().range([h, 0]);

  g.append("g").attr("class", "grid");
  xAxisG = g.append("g").attr("transform", "translate(0," + h + ")");
  yAxisG = g.append("g");

  g.append("text").attr("x", w / 2).attr("y", h + 42).attr("text-anchor", "middle")
    .attr("fill", "#99aabc").attr("font-size", 12).text("Time of Day (12h folded)");
  g.append("text").attr("transform", "rotate(-90)").attr("x", -h / 2).attr("y", -46)
    .attr("text-anchor", "middle").attr("fill", "#99aabc").attr("font-size", 12)
    .text("Temperature (\u00b0F)");

  tip = d3.select("#tip");
}

function chName(key) {
  var parts = key.split("_ch");
  var chNum = parts.length > 1 ? parts[1] : key;
  return CHANNEL_NAMES[chNum] || ("Channel " + chNum);
}

function phaseHour(d) {
  // Fold 24h into 12h: hour-of-day mod 12 as fractional hours
  var t = d.time;
  return (t.getHours() % 12) + t.getMinutes() / 60 + t.getSeconds() / 3600;
}

function fmtPhaseHour(h) {
  var hr = Math.floor(h);
  if (hr === 0) return "12 AM";
  if (hr < 12) return hr + " AM";
  return "12 PM";
}

function updateChart(data) {
  var keys = Object.keys(data).sort();

  var allPoints = [];
  var series = {};
  keys.forEach(function(key) {
    var pts = data[key].map(function(r) {
      return { time: new Date(r.time), temp: r.temp_f, humidity: r.humidity, key: key };
    });
    // Sort by phase hour so lines don't zig-zag
    pts.sort(function(a, b) { return phaseHour(a) - phaseHour(b); });
    series[key] = pts;
    allPoints = allPoints.concat(pts);
  });

  if (allPoints.length === 0) return;

  var temps = allPoints.map(function(d) { return d.temp; });
  y.domain([d3.min(temps) - 1, d3.max(temps) + 1]).nice();

  var gridSel = g.select(".grid").selectAll("line").data(y.ticks(8));
  gridSel.enter().append("line").merge(gridSel)
    .attr("x1", 0).attr("x2", w)
    .attr("y1", function(d) { return y(d); })
    .attr("y2", function(d) { return y(d); })
    .attr("stroke", "#2a2a4a").attr("stroke-dasharray", "2,4");
  gridSel.exit().remove();

  var xTicks = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  xAxisG.call(d3.axisBottom(x).tickValues(xTicks).tickFormat(fmtPhaseHour))
    .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");

  yAxisG.call(d3.axisLeft(y).ticks(8).tickFormat(function(d) { return d + "\u00b0F"; }))
    .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");

  var line = d3.line()
    .x(function(d) { return x(phaseHour(d)); })
    .y(function(d) { return y(d.temp); })
    .curve(d3.curveMonotoneX);

  keys.forEach(function(key, i) {
    var color = COLORS[i % COLORS.length];
    var sel = g.selectAll(".line-" + i).data([series[key]]);
    sel.enter().append("path").attr("class", "line-" + i)
      .attr("fill", "none").attr("stroke", color).attr("stroke-width", 2)
      .merge(sel).attr("d", line);

    var sparse = series[key].filter(function(_, j) { return j % 5 === 0; });
    var dots = g.selectAll(".dot-" + i).data(sparse);
    dots.enter().append("circle").attr("class", "dot-" + i)
      .attr("r", 3).attr("fill", color).attr("opacity", 0)
      .on("mouseover", function(ev, d) {
        d3.select(this).attr("opacity", 1).attr("r", 5).attr("stroke", "#fff").attr("stroke-width", 1);
        var ampm = d.time.getHours() < 12 ? "AM" : "PM";
        tip.style("display", "block").html(
          "<strong>" + chName(d.key) + "</strong> (" + ampm + ")<br>" +
          d.temp.toFixed(1) + "\u00b0F, " + d.humidity.toFixed(0) + "% humidity<br>" +
          d.time.toLocaleString()
        );
      })
      .on("mousemove", function(ev) {
        tip.style("left", (ev.pageX + 14) + "px").style("top", (ev.pageY - 20) + "px");
      })
      .on("mouseout", function() {
        d3.select(this).attr("opacity", 0).attr("r", 3).attr("stroke", "none");
        tip.style("display", "none");
      })
      .merge(dots)
      .attr("cx", function(d) { return x(phaseHour(d)); })
      .attr("cy", function(d) { return y(d.temp); });
    dots.exit().remove();
  });

  updateCurrent(data, keys);
}

function timeAgo(dateStr) {
  var then = new Date(dateStr);
  var diff = Math.floor((Date.now() - then) / 1000);
  if (diff < 5) return "just now";
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

var _agoIntervalId = null;

function updateCurrent(data, keys) {
  var container = d3.select("#current");
  container.selectAll("*").remove();

  keys.forEach(function(key, i) {
    var records = data[key];
    if (!records || records.length === 0) return;
    var last = records[records.length - 1];
    var name = chName(key);
    var color = COLORS[i % COLORS.length];

    var card = container.append("div").attr("class", "sensor-card");
    card.append("div").attr("class", "label").text(name);
    card.append("div").attr("class", "temp").style("color", color)
      .text(last.temp_f.toFixed(1) + "\u00b0F");
    var hum = isNaN(last.humidity) ? "n/a" : last.humidity.toFixed(0) + "% humidity";
    card.append("div").attr("class", "humid").text(hum);
    var t = last.time.replace("T", " ").substring(0, 19);
    card.append("div").attr("class", "time").text(t);
    card.append("div").attr("class", "ago").attr("data-time", last.time).text(timeAgo(last.time));
  });

  if (_agoIntervalId) clearInterval(_agoIntervalId);
  _agoIntervalId = setInterval(function() {
    d3.selectAll(".ago").each(function() {
      var el = d3.select(this);
      el.text(timeAgo(el.attr("data-time")));
    });
  }, 5000);
}

initChart();

var statusEl = document.getElementById("status");
function connect() {
  var es = new EventSource("/stream");
  es.onopen = function() {
    statusEl.textContent = "live";
    statusEl.className = "status live";
  };
  es.onmessage = function(ev) {
    var data = JSON.parse(ev.data);
    updateChart(data);
  };
  es.onerror = function() {
    statusEl.textContent = "disconnected \u2014 reconnecting...";
    statusEl.className = "status";
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
</script>
</body>
</html>"""


# --- HTTP handler ---

class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == "/temps":
            payload = json.dumps(get_latest_temps(), indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last_ver = -1
            deadline = time.monotonic() + SSE_TIMEOUT
            try:
                while time.monotonic() < deadline:
                    with _lock:
                        ver = _version
                    if ver != last_ver:
                        data = get_last_7d()
                        payload = json.dumps(data, separators=(",", ":"))
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        last_ver = ver
                        deadline = time.monotonic() + SSE_TIMEOUT
                    time.sleep(1)
                self.wfile.write(b": timeout\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        if args and str(args[0]).startswith(("4", "5")):
            log.warning("HTTP %s %s", args[0], args[1] if len(args) > 1 else "")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# --- Main ---

def main():
    global _sensors, _version, _out_dir

    parser = argparse.ArgumentParser(description="SDR temperature web dashboard")
    parser.add_argument("--port", type=int, default=8433)
    parser.add_argument("--dir", default="sdr/sdr", help="Directory for .npz chunks")
    args = parser.parse_args()

    _out_dir = Path(args.dir)
    if not _out_dir.exists():
        log.error("data directory %s does not exist", _out_dir)
        sys.exit(1)

    # Initial load
    _sensors = load_sensors_from_disk(_out_dir)
    n = sum(len(a["time"]) for a in _sensors.values())
    log.info("loaded %d records (last 24h) from %s", n, _out_dir)
    _version = 1 if _sensors else 0

    # Start disk poller
    log.info("starting disk poller (interval=%ds)...", POLL_INTERVAL)
    threading.Thread(target=disk_poller, daemon=True).start()

    def shutdown(sig, frame):
        log.info("shutdown signal received (sig=%d)", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("serving on http://0.0.0.0:%d", args.port)
    server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
