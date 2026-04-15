#!/usr/bin/env python3
"""SDR temperature web dashboard — reads from SQLite, serves live dashboard.

Usage:
    python sdr/sdr_web.py [--port 8433] [--dir sdr/sdr]

Endpoints:
    GET /             — D3.js dashboard with interactive charts
    GET /temps        — JSON of latest readings per sensor
    GET /stats?days=N — JSON summary stats per sensor (min/max/avg/std)
    GET /stream?days=N — Server-Sent Events for real-time chart updates
"""

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

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

POLL_INTERVAL = 10  # seconds between DB checks
SSE_TIMEOUT = 300   # drop idle SSE connections after 5 minutes

CHANNEL_NAMES = {
    "0": "Bedroom",
    "1": "Living Room",
    "2": "Garage",
}

# --- Shared state ---

_lock = threading.Lock()
_version = 0
_db_path: Path = Path("sdr/sdr/temps.db")


# --- DB helpers ---

def _get_conn() -> sqlite3.Connection:
    """Open a read-only connection to the DB."""
    uri = f"file:{_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def channel_name(sensor_key: str) -> str:
    parts = sensor_key.split("_ch")
    ch = parts[1] if len(parts) > 1 else sensor_key
    return CHANNEL_NAMES.get(ch, f"Channel {ch}")


def is_known_channel(sensor_key: str) -> bool:
    parts = sensor_key.split("_ch")
    ch = parts[1] if len(parts) > 1 else ""
    return ch in CHANNEL_NAMES


def get_latest_temps() -> dict:
    try:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT sensor, time, temp_f, humidity
            FROM readings
            WHERE (sensor, time) IN (
                SELECT sensor, MAX(time) FROM readings GROUP BY sensor
            )
            ORDER BY sensor
        """).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.error("get_latest_temps query failed: %s", e)
        return {}

    result = {}
    for row in rows:
        if not is_known_channel(row["sensor"]):
            continue
        result[channel_name(row["sensor"])] = {
            "temp_f": round(row["temp_f"], 2),
            "humidity": round(row["humidity"], 1) if row["humidity"] is not None else None,
            "time": row["time"],
        }
    return result


def get_last_nd(days: int = 7) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = _get_conn()
        if days >= 14:
            # Downsample to 5-minute averages for large ranges
            rows = conn.execute("""
                SELECT sensor,
                       strftime('%%Y-%%m-%%dT%%H:', time) ||
                       printf('%%02d', (CAST(strftime('%%M', time) AS INT)/5)*5) ||
                       ':00' AS time,
                       AVG(temp_f) AS temp_f, AVG(humidity) AS humidity
                FROM readings
                WHERE time >= ?
                GROUP BY sensor, 2
                ORDER BY sensor, time
            """, (cutoff,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT sensor, time, temp_f, humidity
                FROM readings
                WHERE time >= ?
                ORDER BY sensor, time
            """, (cutoff,)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.error("get_last_%dd query failed: %s", days, e)
        return {}

    result: dict[str, list] = {}
    for row in rows:
        sensor = row["sensor"]
        if not is_known_channel(sensor):
            continue
        result.setdefault(sensor, []).append({
            "time": row["time"],
            "temp_f": round(row["temp_f"], 2),
            "humidity": round(row["humidity"], 1) if row["humidity"] is not None else None,
        })
    return result


def get_stats(days: int = 7) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT sensor,
                   MIN(temp_f) AS min_f, MAX(temp_f) AS max_f,
                   AVG(temp_f) AS avg_f,
                   COUNT(*) AS n,
                   MIN(humidity) AS min_hum, MAX(humidity) AS max_hum,
                   AVG(humidity) AS avg_hum
            FROM readings
            WHERE time >= ?
            GROUP BY sensor
            ORDER BY sensor
        """, (cutoff,)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.error("get_stats query failed: %s", e)
        return {}

    # Compute stddev in a second pass (SQLite lacks built-in STDDEV)
    std_map = {}
    try:
        conn = _get_conn()
        for row in rows:
            sensor = row["sensor"]
            avg = row["avg_f"]
            std_row = conn.execute("""
                SELECT AVG((temp_f - ?) * (temp_f - ?)) AS var_f
                FROM readings
                WHERE sensor = ? AND time >= ?
            """, (avg, avg, sensor, cutoff)).fetchone()
            std_map[sensor] = (std_row["var_f"] or 0) ** 0.5
        conn.close()
    except sqlite3.Error:
        pass

    result = {}
    for row in rows:
        sensor = row["sensor"]
        if not is_known_channel(sensor):
            continue
        result[channel_name(sensor)] = {
            "temp_f": {
                "min": round(row["min_f"], 2), "max": round(row["max_f"], 2),
                "avg": round(row["avg_f"], 2), "std": round(std_map.get(sensor, 0), 2),
            },
            "humidity": {
                "min": round(row["min_hum"], 1) if row["min_hum"] is not None else None,
                "max": round(row["max_hum"], 1) if row["max_hum"] is not None else None,
                "avg": round(row["avg_hum"], 1) if row["avg_hum"] is not None else None,
            },
            "count": row["n"],
        }
    return result


# --- DB poller thread ---

def db_poller():
    """Check for new data and bump version when found."""
    global _version
    last_max_time = None
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            conn = _get_conn()
            row = conn.execute("SELECT MAX(time) AS mt FROM readings").fetchone()
            conn.close()
            max_time = row["mt"] if row else None
        except sqlite3.Error as e:
            log.error("db poll error: %s", e)
            continue

        if max_time != last_max_time:
            last_max_time = max_time
            with _lock:
                _version += 1
            if max_time:
                log.info("new data detected (latest: %s)", max_time)


# --- HTML dashboard ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Temperature Dashboard</title>
<style>
  :root {
    --bg: #1a1a2e; --card-bg: #16213e; --text: #e0e0e0; --muted: #889;
    --border: #333; --grid: #2a2a4a; --ctrl-bg: #16213e; --ctrl-border: #444;
    --ctrl-text: #ccd; --btn-bg: #2a3a5e; --btn-hover: #3a4a6e;
    --tip-bg: rgba(10,10,30,0.95); --axis: #556; --axis-text: #889; --label: #99aabc;
  }
  .light {
    --bg: #f4f5f7; --card-bg: #fff; --text: #222; --muted: #667;
    --border: #ddd; --grid: #e0e0e0; --ctrl-bg: #fff; --ctrl-border: #ccc;
    --ctrl-text: #333; --btn-bg: #e8ecf0; --btn-hover: #d0d5dc;
    --tip-bg: rgba(255,255,255,0.96); --axis: #aaa; --axis-text: #666; --label: #556;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: "Menlo", "Consolas", monospace; transition: background 0.2s, color 0.2s; }
  h1 { text-align: center; padding: 16px 0 4px; font-size: 1.2em; color: var(--label); font-weight: 400; }
  .status { text-align: center; font-size: 0.75em; color: var(--muted); margin-bottom: 8px; }
  .status.live { color: #2ecc71; }
  .current { display: flex; justify-content: center; gap: 24px; padding: 8px 16px 12px; flex-wrap: wrap; }
  .sensor-card {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 24px; text-align: center; min-width: 160px; transition: background 0.2s;
  }
  .sensor-card .label { font-size: 0.8em; color: var(--muted); margin-bottom: 4px; }
  .sensor-card .temp { font-size: 2em; font-weight: 600; }
  .sensor-card .humid { font-size: 0.85em; color: var(--muted); margin-top: 2px; }
  .sensor-card .time { font-size: 0.7em; color: var(--muted); margin-top: 4px; }
  .sensor-card .ago { font-size: 0.7em; color: var(--muted); margin-top: 2px; }
  .controls {
    display: flex; justify-content: center; gap: 16px; padding: 6px 16px 10px;
    flex-wrap: wrap; align-items: center;
  }
  .ctrl-group { display: flex; align-items: center; gap: 5px; }
  .ctrl-group label { font-size: 0.72em; color: var(--muted); }
  .ctrl-group select, .ctrl-group input[type=number] {
    background: var(--ctrl-bg); color: var(--ctrl-text); border: 1px solid var(--ctrl-border); border-radius: 4px;
    padding: 3px 6px; font-family: inherit; font-size: 0.78em;
  }
  .ctrl-group input[type=number] { width: 52px; }
  .ctrl-group select { cursor: pointer; }
  .ctrl-btn {
    background: var(--btn-bg); color: var(--ctrl-text); border: 1px solid var(--ctrl-border); border-radius: 4px;
    padding: 3px 10px; font-family: inherit; font-size: 0.78em; cursor: pointer; transition: background 0.15s;
  }
  .ctrl-btn:hover { background: var(--btn-hover); }
  .theme-btn { font-size: 1em; padding: 2px 8px; line-height: 1; }
  #chart-area { width: 100%; display: flex; flex-direction: column; align-items: center; position: relative; }
  .tooltip {
    position: absolute; pointer-events: none; background: var(--tip-bg);
    border: 1px solid var(--axis); border-radius: 4px; padding: 8px 12px;
    font-size: 12px; line-height: 1.6; color: var(--text); white-space: nowrap; z-index: 10;
  }
  .legend-item { cursor: pointer; }
  .legend-item.hidden { opacity: 0.25; }
  .anomaly-dot { animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { r: 4; } 50% { r: 7; } }
  #reset-zoom {
    position: absolute; top: 24px; right: 40px; z-index: 5; display: none;
  }
  @media (max-width: 700px) {
    h1 { font-size: 1em; padding: 10px 0 2px; }
    .current { gap: 10px; padding: 6px 8px 8px; }
    .sensor-card { min-width: 120px; padding: 8px 12px; }
    .sensor-card .temp { font-size: 1.4em; }
    .controls { gap: 8px; padding: 4px 8px 8px; }
    .ctrl-group label { font-size: 0.68em; }
  }
</style>
</head>
<body>
<h1 id="title">Temperature Dashboard</h1>
<div class="status" id="status">connecting...</div>
<div class="current" id="current"></div>

<div class="controls">
  <div class="ctrl-group">
    <label>Range:</label>
    <select id="days-sel">
      <option value="1">1d</option>
      <option value="3">3d</option>
      <option value="7" selected>7d</option>
      <option value="14">14d</option>
      <option value="30">30d</option>
    </select>
  </div>
  <div class="ctrl-group">
    <label>Plot:</label>
    <select id="plot-sel">
      <option value="overlay">24h Overlay</option>
      <option value="timeline">Timeline</option>
      <option value="daily">Daily Min/Avg/Max</option>
      <option value="rolling_avg">Rolling Avg</option>
      <option value="rolling_med">Rolling Median</option>
    </select>
  </div>
  <div class="ctrl-group" id="window-group" style="display:none;">
    <label>Window:</label>
    <select id="window-sel">
      <option value="6">6h</option>
      <option value="12">12h</option>
      <option value="24" selected>1d</option>
      <option value="48">2d</option>
      <option value="72">3d</option>
    </select>
  </div>
  <div class="ctrl-group">
    <label>Y:</label>
    <input id="y-min" type="number" value="65"> <span style="color:var(--muted);font-size:0.72em">&ndash;</span>
    <input id="y-max" type="number" value="85">
  </div>
  <div class="ctrl-group">
    <label><input id="y-auto" type="checkbox"> Auto</label>
  </div>
  <div class="ctrl-group">
    <select id="export-sel" class="ctrl-btn" style="padding:3px 6px;">
      <option value="" disabled selected>Export</option>
      <option value="csv">CSV</option>
      <option value="json">JSON</option>
    </select>
  </div>
  <button class="ctrl-btn theme-btn" id="theme-btn" title="Toggle theme"></button>
</div>

<div id="chart-area">
  <button class="ctrl-btn" id="reset-zoom">Reset zoom</button>
</div>
<div class="tooltip" id="tip" style="display:none;"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
var COLORS = ["#ff6b6b", "#48dbfb", "#feca57", "#a29bfe", "#fd79a8", "#55efc4"];
var CHANNEL_NAMES = {"0": "Bedroom", "1": "Living Room", "2": "Garage"};
var rawData = null, es = null;
var hiddenSensors = new Set();

// --- Theme ---
function applyTheme(light) {
  document.body.classList.toggle("light", light);
  document.getElementById("theme-btn").textContent = light ? "\u263e" : "\u2600";
  try { localStorage.setItem("sdr_theme", light ? "light" : "dark"); } catch(e) {}
}
(function() {
  var saved = null;
  try { saved = localStorage.getItem("sdr_theme"); } catch(e) {}
  applyTheme(saved === "light");
})();
document.getElementById("theme-btn").addEventListener("click", function() {
  applyTheme(!document.body.classList.contains("light"));
  render();
});

// --- Helpers ---
function chName(key) {
  var parts = key.split("_ch");
  var ch = parts.length > 1 ? parts[1] : key;
  return CHANNEL_NAMES[ch] || ("Channel " + ch);
}
function phaseHour(d) { return d.time.getHours() + d.time.getMinutes() / 60 + d.time.getSeconds() / 3600; }
function fmtPhaseHour(h) {
  var hr = Math.floor(h);
  if (hr === 0 || hr === 24) return "12a";
  if (hr === 12) return "12p";
  return hr < 12 ? hr + "a" : (hr - 12) + "p";
}
function timeAgo(dateStr) {
  var diff = Math.floor((Date.now() - new Date(dateStr)) / 1000);
  if (diff < 5) return "just now";
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}
function getYDomain(allTemps) {
  if (document.getElementById("y-auto").checked) {
    return [d3.min(allTemps) - 1, d3.max(allTemps) + 1];
  }
  return [+document.getElementById("y-min").value, +document.getElementById("y-max").value];
}
function isLight() { return document.body.classList.contains("light"); }
function gridColor() { return getComputedStyle(document.body).getPropertyValue("--grid").trim(); }
function axisColor() { return getComputedStyle(document.body).getPropertyValue("--axis").trim(); }
function axisTextColor() { return getComputedStyle(document.body).getPropertyValue("--axis-text").trim(); }
function labelColor() { return getComputedStyle(document.body).getPropertyValue("--label").trim(); }

function parseSeries(data) {
  var keys = Object.keys(data).sort();
  var series = {};
  keys.forEach(function(key) {
    series[key] = data[key].map(function(r) {
      return { time: new Date(r.time), temp: r.temp_f, humidity: r.humidity, key: key };
    }).sort(function(a, b) { return a.time - b.time; });
  });
  return { keys: keys, series: series };
}

function visibleKeys(keys) {
  return keys.filter(function(k) { return !hiddenSensors.has(k); });
}

// --- Export ---
document.getElementById("export-sel").addEventListener("change", function() {
  var fmt = this.value;
  this.selectedIndex = 0;
  if (!rawData) return;
  var rows = [];
  Object.keys(rawData).sort().forEach(function(key) {
    rawData[key].forEach(function(r) {
      rows.push({ sensor: chName(key), time: r.time, temp_f: r.temp_f, humidity: r.humidity });
    });
  });
  var blob, fname;
  if (fmt === "csv") {
    var lines = ["sensor,time,temp_f,humidity"];
    rows.forEach(function(r) { lines.push(r.sensor + "," + r.time + "," + r.temp_f + "," + (r.humidity != null ? r.humidity : "")); });
    blob = new Blob([lines.join("\n")], { type: "text/csv" });
    fname = "sdr_temps.csv";
  } else {
    blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" });
    fname = "sdr_temps.json";
  }
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = fname;
  a.click();
  URL.revokeObjectURL(a.href);
});

// --- Chart infrastructure ---
function makeChart(containerId, heightVal) {
  var margin = { top: 16, right: 30, bottom: 44, left: 56 };
  var W = Math.min(1200, window.innerWidth - 40);
  var H = heightVal;
  var w = W - margin.left - margin.right;
  var h = H - margin.top - margin.bottom;
  var svg = d3.select(containerId).append("svg").attr("width", W).attr("height", H);
  var g = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");
  return { svg: svg, g: g, w: w, h: h, margin: margin, W: W, H: H };
}
function drawGrid(g, y, w) {
  g.selectAll(".gridline").data(y.ticks(6)).enter().append("line").attr("class", "gridline")
    .attr("x1", 0).attr("x2", w)
    .attr("y1", function(d) { return y(d); }).attr("y2", function(d) { return y(d); })
    .attr("stroke", gridColor()).attr("stroke-dasharray", "2,4");
}
function styleAxis(sel) {
  sel.selectAll("text").attr("fill", axisTextColor()).attr("stroke", "none");
  sel.selectAll("line,path").attr("stroke", axisColor());
}
function attachTooltip(sel, tipFn) {
  var tip = d3.select("#tip");
  sel.on("mouseover", function(ev, d) {
    d3.select(this).attr("opacity", 1).attr("r", 5).attr("stroke", isLight() ? "#333" : "#fff").attr("stroke-width", 1);
    tip.style("display", "block").html(tipFn(d));
  }).on("mousemove", function(ev) {
    tip.style("left", (ev.pageX + 14) + "px").style("top", (ev.pageY - 20) + "px");
  }).on("mouseout", function() {
    d3.select(this).attr("opacity", 0.3).attr("r", 2).attr("stroke", "none");
    tip.style("display", "none");
  });
}
function drawLegend(g, allKeys, w) {
  var leg = g.append("g").attr("transform", "translate(" + (w - 10) + ",0)");
  allKeys.forEach(function(key, i) {
    var yy = i * 18;
    var item = leg.append("g").attr("class", "legend-item" + (hiddenSensors.has(key) ? " hidden" : ""))
      .attr("transform", "translate(0," + yy + ")")
      .style("cursor", "pointer")
      .on("click", function() { toggleSensor(key); });
    item.append("circle").attr("r", 5).attr("fill", COLORS[i % COLORS.length]);
    item.append("text").attr("x", -10).attr("y", 4).attr("text-anchor", "end")
      .attr("fill", axisTextColor()).attr("font-size", 11).text(chName(key));
  });
}
function toggleSensor(key) {
  if (hiddenSensors.has(key)) hiddenSensors.delete(key);
  else hiddenSensors.add(key);
  render();
}
function yLabel(g, h) {
  g.append("text").attr("transform", "rotate(-90)").attr("x", -h / 2).attr("y", -42)
    .attr("text-anchor", "middle").attr("fill", labelColor()).attr("font-size", 11).text("Temperature (\u00b0F)");
}

// --- Anomaly detection ---
function detectAnomalies(pts, windowMs) {
  if (pts.length < 10) return [];
  var anomalies = [];
  for (var i = 0; i < pts.length; i++) {
    var t = pts[i].time.getTime();
    var vals = [];
    for (var j = i; j >= 0 && t - pts[j].time.getTime() <= windowMs; j--) vals.push(pts[j].temp);
    for (var j = i + 1; j < pts.length && pts[j].time.getTime() - t <= windowMs; j++) vals.push(pts[j].temp);
    if (vals.length < 5) continue;
    var avg = d3.mean(vals), std = d3.deviation(vals) || 0;
    if (std > 0 && Math.abs(pts[i].temp - avg) > 2 * std) {
      anomalies.push({ idx: i, dev: (pts[i].temp - avg) / std });
    }
  }
  return anomalies;
}

// --- Render dispatch ---
function render() {
  if (!rawData) return;
  d3.select("#chart-area").selectAll("svg,.hum-label").remove();
  document.getElementById("reset-zoom").style.display = "none";
  var mode = document.getElementById("plot-sel").value;
  var days = +document.getElementById("days-sel").value;
  var winHours = +document.getElementById("window-sel").value;
  var labels = {
    overlay: "24h Overlay", timeline: "Timeline", daily: "Daily Min/Avg/Max",
    rolling_avg: winHours + "h Rolling Avg", rolling_med: winHours + "h Rolling Median"
  };
  document.getElementById("title").textContent = "Temperature \u2014 " + days + "d \u2014 " + labels[mode];
  document.getElementById("window-group").style.display = (mode === "rolling_avg" || mode === "rolling_med") ? "flex" : "none";

  var parsed = parseSeries(rawData);
  if (parsed.keys.length === 0) return;

  if (mode === "overlay") drawOverlay(parsed);
  else if (mode === "timeline") drawTimeline(parsed);
  else if (mode === "daily") drawDaily(parsed);
  else if (mode === "rolling_avg") drawRolling(parsed, "avg");
  else if (mode === "rolling_med") drawRolling(parsed, "med");

  drawHumidity(parsed);
  updateCurrent(rawData, parsed.keys);
}

// --- Overlay ---
function drawOverlay(parsed) {
  var c = makeChart("#chart-area", 380);
  var vk = visibleKeys(parsed.keys);
  var allTemps = [];
  vk.forEach(function(k) { parsed.series[k].forEach(function(d) { allTemps.push(d.temp); }); });
  if (allTemps.length === 0) allTemps = [65, 85];

  var x = d3.scaleLinear().domain([0, 24]).range([0, c.w]);
  var y = d3.scaleLinear().domain(getYDomain(allTemps)).range([c.h, 0]);

  drawGrid(c.g, y, c.w);
  var xTicks = [0, 3, 6, 9, 12, 15, 18, 21, 24];
  styleAxis(c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(d3.axisBottom(x).tickValues(xTicks).tickFormat(fmtPhaseHour)));
  styleAxis(c.g.append("g").call(d3.axisLeft(y).ticks(6).tickFormat(function(d) { return d + "\u00b0"; })));
  yLabel(c.g, c.h);
  drawLegend(c.g, parsed.keys, c.w);

  vk.forEach(function(key) {
    var i = parsed.keys.indexOf(key);
    var color = COLORS[i % COLORS.length];
    var pts = parsed.series[key];
    var dots = c.g.selectAll(".dot-" + i).data(pts).enter().append("circle")
      .attr("class", "dot-" + i).attr("r", 2).attr("fill", color).attr("opacity", 0.3)
      .attr("cx", function(d) { return x(phaseHour(d)); }).attr("cy", function(d) { return y(d.temp); });
    attachTooltip(dots, function(d) {
      return "<strong>" + chName(d.key) + "</strong><br>" +
        d.temp.toFixed(1) + "\u00b0F" + (d.humidity != null ? ", " + d.humidity.toFixed(0) + "%" : "") +
        "<br>" + d.time.toLocaleString();
    });
  });
}

// --- Timeline with zoom ---
function drawTimeline(parsed) {
  var c = makeChart("#chart-area", 380);
  var vk = visibleKeys(parsed.keys);
  var allTemps = [], allTimes = [];
  vk.forEach(function(k) {
    parsed.series[k].forEach(function(d) { allTemps.push(d.temp); allTimes.push(d.time); });
  });
  if (allTemps.length === 0) { allTemps = [65, 85]; allTimes = [new Date()]; }

  var x0 = d3.scaleTime().domain(d3.extent(allTimes)).range([0, c.w]);
  var x = x0.copy();
  var y = d3.scaleLinear().domain(getYDomain(allTemps)).range([c.h, 0]);

  // Clip path
  c.g.append("defs").append("clipPath").attr("id", "clip")
    .append("rect").attr("width", c.w).attr("height", c.h);
  var plotArea = c.g.append("g").attr("clip-path", "url(#clip)");

  drawGrid(c.g, y, c.w);
  var xAxisG = c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(d3.axisBottom(x).ticks(8));
  styleAxis(xAxisG);
  styleAxis(c.g.append("g").call(d3.axisLeft(y).ticks(6).tickFormat(function(d) { return d + "\u00b0"; })));
  yLabel(c.g, c.h);
  drawLegend(c.g, parsed.keys, c.w);

  // Anomaly window = 6 hours
  var anomalyWindowMs = 6 * 3600 * 1000;

  vk.forEach(function(key) {
    var idx = parsed.keys.indexOf(key);
    var color = COLORS[idx % COLORS.length];
    var pts = parsed.series[key];

    var line = d3.line().x(function(d) { return x(d.time); }).y(function(d) { return y(d.temp); }).curve(d3.curveMonotoneX);
    plotArea.append("path").datum(pts).attr("class", "line-" + idx).attr("fill", "none")
      .attr("stroke", color).attr("stroke-width", 1.5).attr("opacity", 0.6).attr("d", line);

    var dots = plotArea.selectAll(".dot-" + idx).data(pts).enter().append("circle")
      .attr("class", "dot-" + idx).attr("r", 2).attr("fill", color).attr("opacity", 0.3)
      .attr("cx", function(d) { return x(d.time); }).attr("cy", function(d) { return y(d.temp); });
    attachTooltip(dots, function(d) {
      return "<strong>" + chName(d.key) + "</strong><br>" +
        d.temp.toFixed(1) + "\u00b0F" + (d.humidity != null ? ", " + d.humidity.toFixed(0) + "%" : "") +
        "<br>" + d.time.toLocaleString();
    });

    // Anomalies
    var anoms = detectAnomalies(pts, anomalyWindowMs);
    anoms.forEach(function(a) {
      var d = pts[a.idx];
      plotArea.append("circle").attr("class", "anomaly-dot")
        .attr("cx", x(d.time)).attr("cy", y(d.temp))
        .attr("data-time", d.time.getTime()).attr("data-temp", d.temp)
        .attr("r", 4).attr("fill", "#ff4757").attr("opacity", 0.9).attr("stroke", "#fff").attr("stroke-width", 0.5);
    });
  });

  // Zoom
  var resetBtn = document.getElementById("reset-zoom");
  var zoom = d3.zoom().scaleExtent([1, 20]).translateExtent([[0, 0], [c.w, c.h]])
    .extent([[0, 0], [c.w, c.h]])
    .on("zoom", function(ev) {
      var nx = ev.transform.rescaleX(x0);
      x.domain(nx.domain());
      xAxisG.call(d3.axisBottom(x).ticks(8));
      styleAxis(xAxisG);
      vk.forEach(function(key) {
        var idx = parsed.keys.indexOf(key);
        var pts = parsed.series[key];
        var line = d3.line().x(function(d) { return x(d.time); }).y(function(d) { return y(d.temp); }).curve(d3.curveMonotoneX);
        plotArea.select(".line-" + idx).attr("d", line(pts));
        plotArea.selectAll(".dot-" + idx).attr("cx", function(d) { return x(d.time); });
      });
      plotArea.selectAll(".anomaly-dot").each(function() {
        var el = d3.select(this);
        el.attr("cx", x(new Date(+el.attr("data-time"))));
      });
      resetBtn.style.display = ev.transform.k > 1.01 ? "block" : "none";
    });

  c.svg.call(zoom);
  resetBtn.onclick = function() { c.svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity); };
}

// --- Daily Min/Avg/Max ---
function drawDaily(parsed) {
  var c = makeChart("#chart-area", 380);
  var vk = visibleKeys(parsed.keys);

  var dailyData = {};
  vk.forEach(function(key) {
    dailyData[key] = {};
    parsed.series[key].forEach(function(d) {
      var ds = d.time.toISOString().slice(0, 10);
      if (!dailyData[key][ds]) dailyData[key][ds] = [];
      dailyData[key][ds].push(d.temp);
    });
  });

  var allDates = new Set(), allTemps = [], barData = [];
  vk.forEach(function(key) {
    var ki = parsed.keys.indexOf(key);
    Object.keys(dailyData[key]).forEach(function(ds) {
      var vals = dailyData[key][ds];
      var lo = d3.min(vals), hi = d3.max(vals), avg = d3.mean(vals);
      allDates.add(ds); allTemps.push(lo, hi);
      barData.push({ key: key, ki: ki, date: ds, min: lo, max: hi, avg: avg });
    });
  });
  if (allTemps.length === 0) allTemps = [65, 85];

  var dates = Array.from(allDates).sort();
  var x = d3.scaleBand().domain(dates).range([0, c.w]).padding(0.3);
  var y = d3.scaleLinear().domain(getYDomain(allTemps)).range([c.h, 0]);

  drawGrid(c.g, y, c.w);
  var xAxis = c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(
    d3.axisBottom(x).tickFormat(function(d) { return d.slice(5); })
  );
  styleAxis(xAxis);
  xAxis.selectAll("text").attr("transform", "rotate(-40)").style("text-anchor", "end");
  styleAxis(c.g.append("g").call(d3.axisLeft(y).ticks(6).tickFormat(function(d) { return d + "\u00b0"; })));
  yLabel(c.g, c.h);
  drawLegend(c.g, parsed.keys, c.w);

  var nKeys = vk.length || 1;
  var subW = x.bandwidth() / nKeys;

  barData.forEach(function(d, di) {
    var color = COLORS[d.ki % COLORS.length];
    var vki = vk.indexOf(d.key);
    var bx = x(d.date) + vki * subW;
    c.g.append("rect").attr("x", bx + subW * 0.15).attr("width", subW * 0.7)
      .attr("y", y(d.max)).attr("height", Math.max(1, y(d.min) - y(d.max)))
      .attr("fill", color).attr("opacity", 0.3).attr("rx", 2);
    c.g.append("line").attr("x1", bx).attr("x2", bx + subW)
      .attr("y1", y(d.avg)).attr("y2", y(d.avg))
      .attr("stroke", color).attr("stroke-width", 2);
  });
}

// --- Rolling average / median with min/max bands ---
function rollingWindow(pts, windowMs, method) {
  var result = [];
  for (var i = 0; i < pts.length; i++) {
    var t = pts[i].time.getTime(), vals = [];
    for (var j = i; j >= 0 && t - pts[j].time.getTime() <= windowMs; j--) vals.push(pts[j].temp);
    var value = method === "avg" ? d3.mean(vals) : d3.median(vals);
    var lo = d3.min(vals), hi = d3.max(vals);
    result.push({ time: pts[i].time, temp: pts[i].temp, smoothed: value, rmin: lo, rmax: hi, humidity: pts[i].humidity, key: pts[i].key });
  }
  return result;
}

function drawRolling(parsed, method) {
  var c = makeChart("#chart-area", 380);
  var vk = visibleKeys(parsed.keys);
  var windowMs = +document.getElementById("window-sel").value * 3600 * 1000;
  var allTemps = [], allTimes = [], smoothed = {};

  vk.forEach(function(key) {
    var sm = rollingWindow(parsed.series[key], windowMs, method);
    smoothed[key] = sm;
    sm.forEach(function(d) { allTemps.push(d.smoothed, d.rmin, d.rmax); allTimes.push(d.time); });
  });
  if (allTemps.length === 0) { allTemps = [65, 85]; allTimes = [new Date()]; }

  var x = d3.scaleTime().domain(d3.extent(allTimes)).range([0, c.w]);
  var y = d3.scaleLinear().domain(getYDomain(allTemps)).range([c.h, 0]);

  drawGrid(c.g, y, c.w);
  styleAxis(c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(d3.axisBottom(x).ticks(8)));
  styleAxis(c.g.append("g").call(d3.axisLeft(y).ticks(6).tickFormat(function(d) { return d + "\u00b0"; })));
  yLabel(c.g, c.h);
  drawLegend(c.g, parsed.keys, c.w);

  vk.forEach(function(key) {
    var i = parsed.keys.indexOf(key);
    var color = COLORS[i % COLORS.length];
    var pts = smoothed[key];

    // Min/max band
    var area = d3.area()
      .x(function(d) { return x(d.time); })
      .y0(function(d) { return y(d.rmin); })
      .y1(function(d) { return y(d.rmax); })
      .curve(d3.curveMonotoneX);
    c.g.append("path").datum(pts).attr("fill", color).attr("opacity", 0.1).attr("d", area);

    // Faint raw dots
    c.g.selectAll(".rawdot-" + i).data(pts).enter().append("circle")
      .attr("class", "rawdot-" + i).attr("r", 1.5).attr("fill", color).attr("opacity", 0.12)
      .attr("cx", function(d) { return x(d.time); }).attr("cy", function(d) { return y(d.temp); });

    // Smoothed line
    var line = d3.line().x(function(d) { return x(d.time); }).y(function(d) { return y(d.smoothed); }).curve(d3.curveMonotoneX);
    c.g.append("path").datum(pts).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2.5).attr("opacity", 0.85).attr("d", line);

    // Tooltip targets
    var dots = c.g.selectAll(".dot-" + i).data(pts).enter().append("circle")
      .attr("class", "dot-" + i).attr("r", 3).attr("fill", "transparent").attr("opacity", 0)
      .attr("cx", function(d) { return x(d.time); }).attr("cy", function(d) { return y(d.smoothed); });
    var ml = method === "avg" ? "avg" : "median";
    attachTooltip(dots, function(d) {
      return "<strong>" + chName(d.key) + "</strong><br>" +
        "Raw: " + d.temp.toFixed(1) + "\u00b0F | " + ml + ": " + d.smoothed.toFixed(1) + "\u00b0F<br>" +
        "Range: " + d.rmin.toFixed(1) + " \u2013 " + d.rmax.toFixed(1) + "\u00b0F" +
        (d.humidity != null ? "<br>" + d.humidity.toFixed(0) + "% hum" : "") +
        "<br>" + d.time.toLocaleString();
    });
  });
}

// --- Humidity subplot ---
function drawHumidity(parsed) {
  var vk = visibleKeys(parsed.keys);
  var hasHum = false;
  vk.forEach(function(k) { parsed.series[k].forEach(function(d) { if (d.humidity != null) hasHum = true; }); });
  if (!hasHum) return;

  var mode = document.getElementById("plot-sel").value;
  d3.select("#chart-area").append("div").attr("class", "hum-label")
    .style("margin-top", "4px").style("text-align", "center")
    .append("span").style("color", "var(--muted)").style("font-size", "0.72em").text("Humidity (%)");

  var c = makeChart("#chart-area", 160);
  var allHum = [], allTimes = [];
  vk.forEach(function(k) {
    parsed.series[k].forEach(function(d) {
      if (d.humidity != null) { allHum.push(d.humidity); allTimes.push(d.time); }
    });
  });
  if (allHum.length === 0) return;

  var yH = d3.scaleLinear().domain([d3.min(allHum) - 2, d3.max(allHum) + 2]).nice().range([c.h, 0]);
  drawGrid(c.g, yH, c.w);

  var xH;
  if (mode === "overlay") {
    xH = d3.scaleLinear().domain([0, 24]).range([0, c.w]);
    styleAxis(c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(
      d3.axisBottom(xH).tickValues([0, 6, 12, 18, 24]).tickFormat(fmtPhaseHour)));
  } else {
    xH = d3.scaleTime().domain(d3.extent(allTimes)).range([0, c.w]);
    styleAxis(c.g.append("g").attr("transform", "translate(0," + c.h + ")").call(d3.axisBottom(xH).ticks(8)));
  }
  styleAxis(c.g.append("g").call(d3.axisLeft(yH).ticks(4).tickFormat(function(d) { return d + "%"; })));

  vk.forEach(function(key) {
    var i = parsed.keys.indexOf(key);
    var color = COLORS[i % COLORS.length];
    var pts = parsed.series[key].filter(function(d) { return d.humidity != null; });
    if (pts.length === 0) return;

    if (mode === "timeline" || mode === "rolling_avg" || mode === "rolling_med") {
      var line = d3.line()
        .x(function(d) { return xH(d.time); })
        .y(function(d) { return yH(d.humidity); }).curve(d3.curveMonotoneX);
      c.g.append("path").datum(pts).attr("fill", "none").attr("stroke", color).attr("stroke-width", 1.2).attr("opacity", 0.5).attr("d", line);
    }

    var dots = c.g.selectAll(".hdot-" + i).data(pts).enter().append("circle")
      .attr("class", "hdot-" + i).attr("r", 1.5).attr("fill", color).attr("opacity", 0.2)
      .attr("cx", function(d) { return mode === "overlay" ? xH(phaseHour(d)) : xH(d.time); })
      .attr("cy", function(d) { return yH(d.humidity); });
    attachTooltip(dots, function(d) {
      return "<strong>" + chName(d.key) + "</strong><br>" + d.humidity.toFixed(0) + "% humidity<br>" + d.time.toLocaleString();
    });
  });
}

// --- Current readings ---
var _agoId = null;
function updateCurrent(data, keys) {
  var container = d3.select("#current");
  container.selectAll("*").remove();
  keys.forEach(function(key, i) {
    var records = data[key];
    if (!records || records.length === 0) return;
    var last = records.reduce(function(a, b) { return new Date(a.time) > new Date(b.time) ? a : b; });
    var color = COLORS[i % COLORS.length];
    var card = container.append("div").attr("class", "sensor-card");
    card.append("div").attr("class", "label").text(chName(key));
    card.append("div").attr("class", "temp").style("color", color).text(last.temp_f.toFixed(1) + "\u00b0F");
    var hum = (last.humidity != null && !isNaN(last.humidity)) ? last.humidity.toFixed(0) + "%" : "n/a";
    card.append("div").attr("class", "humid").text(hum);
    card.append("div").attr("class", "time").text(last.time.replace("T", " ").slice(0, 19));
    card.append("div").attr("class", "ago").attr("data-time", last.time).text(timeAgo(last.time));
  });
  if (_agoId) clearInterval(_agoId);
  _agoId = setInterval(function() {
    d3.selectAll(".ago").each(function() { var el = d3.select(this); el.text(timeAgo(el.attr("data-time"))); });
  }, 5000);
}

// --- SSE ---
var statusEl = document.getElementById("status");
function connect() {
  if (es) es.close();
  var days = document.getElementById("days-sel").value;
  es = new EventSource("/stream?days=" + days);
  es.onopen = function() { statusEl.textContent = "live"; statusEl.className = "status live"; };
  es.onmessage = function(ev) { rawData = JSON.parse(ev.data); render(); };
  es.onerror = function() {
    statusEl.textContent = "reconnecting..."; statusEl.className = "status";
    es.close(); setTimeout(connect, 3000);
  };
}

// --- Events ---
document.getElementById("days-sel").addEventListener("change", connect);
document.getElementById("plot-sel").addEventListener("change", render);
document.getElementById("window-sel").addEventListener("change", render);
document.getElementById("y-min").addEventListener("change", render);
document.getElementById("y-max").addEventListener("change", render);
document.getElementById("y-auto").addEventListener("change", function() {
  document.getElementById("y-min").disabled = this.checked;
  document.getElementById("y-max").disabled = this.checked;
  render();
});
window.addEventListener("resize", function() { clearTimeout(window._rsz); window._rsz = setTimeout(render, 200); });

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
        elif self.path.startswith("/stats"):
            qs = parse_qs(urlparse(self.path).query)
            days = min(int(qs.get("days", [7])[0]), 90)
            payload = json.dumps(get_stats(days), indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
        elif self.path.startswith("/stream"):
            qs = parse_qs(urlparse(self.path).query)
            days = min(int(qs.get("days", [7])[0]), 90)
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
                        data = get_last_nd(days)
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
    global _version, _db_path

    parser = argparse.ArgumentParser(description="SDR temperature web dashboard")
    parser.add_argument("--port", type=int, default=8433)
    parser.add_argument("--dir", default="sdr/sdr", help="Directory containing temps.db")
    args = parser.parse_args()

    _db_path = Path(args.dir) / "temps.db"
    if not _db_path.exists():
        log.error("database %s does not exist", _db_path)
        sys.exit(1)

    # Check we can open it
    try:
        conn = _get_conn()
        n = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        conn.close()
        log.info("opened %s: %d rows", _db_path, n)
    except sqlite3.Error as e:
        log.error("failed to open database: %s", e)
        sys.exit(1)

    _version = 1 if n > 0 else 0

    log.info("starting db poller (interval=%ds)...", POLL_INTERVAL)
    threading.Thread(target=db_poller, daemon=True).start()

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
