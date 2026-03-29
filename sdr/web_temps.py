#!/usr/bin/env python3
"""Real-time temperature dashboard served over HTTP with SSE updates.

Usage:
    python sdr/web_temps.py [--port 8433] [--dir sdr/sdr]

Serves a live chart of the last 24 hours from .npz chunk files.
Browser connects to / for the page and /stream for Server-Sent Events.
"""

import argparse
import json
import sys
import time
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import numpy as np

DATA_DIR = Path("sdr/sdr")
POLL_INTERVAL = 10  # seconds between npz re-reads


def find_chunks(data_dir: Path) -> list[Path]:
    chunks = sorted(data_dir.glob("temp_log_????????_????????.npz"))
    if not chunks and (data_dir / "temp_log.npz").exists():
        chunks = [data_dir / "temp_log.npz"]
    return chunks


def load_last_24h(data_dir: Path) -> dict:
    """Load sensor data from the last 24 hours across all chunks."""
    cutoff = datetime.now() - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    result = {}
    for chunk_path in find_chunks(data_dir):
        npz = np.load(chunk_path)
        keys = sorted({n[:-5] for n in npz.files if n.endswith("_time")})
        for key in keys:
            times = npz[f"{key}_time"]
            temps = npz[f"{key}_temp_f"]
            humids = npz[f"{key}_humidity"]
            # Filter to last 24h
            mask = times >= cutoff_str
            if not mask.any():
                continue
            t = times[mask]
            tf = temps[mask]
            h = humids[mask]
            records = [
                {"time": str(ti), "temp_f": round(float(tfi), 2),
                 "humidity": round(float(hi), 1)}
                for ti, tfi, hi in zip(t, tf, h)
            ]
            result.setdefault(key, []).extend(records)
    return result


# Shared state for SSE clients
_data_lock = threading.Lock()
_current_data = {}
_data_version = 0


def poll_data():
    """Background thread that re-reads npz files periodically."""
    global _current_data, _data_version
    while True:
        try:
            data = load_last_24h(DATA_DIR)
            with _data_lock:
                _current_data = data
                _data_version += 1
        except Exception as e:
            print(f"[poll] error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


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
  #chart { width: 100%; display: flex; justify-content: center; }
  .tooltip {
    position: absolute; pointer-events: none; background: rgba(10,10,30,0.95);
    border: 1px solid #556; border-radius: 4px; padding: 8px 12px;
    font-size: 12px; line-height: 1.6; color: #ddd; white-space: nowrap;
  }
</style>
</head>
<body>
<h1>Temperature &mdash; Last 24 Hours</h1>
<div class="status" id="status">connecting...</div>
<div class="current" id="current"></div>
<div id="chart"></div>
<div class="tooltip" id="tip" style="display:none;"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
var COLORS = ["#ff6b6b", "#48dbfb", "#feca57", "#a29bfe", "#fd79a8", "#55efc4"];
var channels = {};
var channelOrder = [];

var margin = { top: 20, right: 30, bottom: 50, left: 62 };
var W, H, w, h, svg, g, x, y, xAxisG, yAxisG, tip;

function initChart() {
  W = Math.min(1200, window.innerWidth - 40);
  H = 440;
  w = W - margin.left - margin.right;
  h = H - margin.top - margin.bottom;

  svg = d3.select("#chart").append("svg").attr("width", W).attr("height", H);
  g = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  x = d3.scaleTime().range([0, w]);
  y = d3.scaleLinear().range([h, 0]);

  // Grid
  g.append("g").attr("class", "grid");

  xAxisG = g.append("g").attr("transform", "translate(0," + h + ")");
  yAxisG = g.append("g");

  // Axis labels
  g.append("text").attr("x", w / 2).attr("y", h + 42).attr("text-anchor", "middle")
    .attr("fill", "#99aabc").attr("font-size", 12).text("Time");
  g.append("text").attr("transform", "rotate(-90)").attr("x", -h / 2).attr("y", -46)
    .attr("text-anchor", "middle").attr("fill", "#99aabc").attr("font-size", 12)
    .text("Temperature (\u00b0F)");

  tip = d3.select("#tip");
}

function updateChart(data) {
  var keys = Object.keys(data).sort();
  channelOrder = keys;

  var allPoints = [];
  var series = {};
  keys.forEach(function(key, i) {
    var pts = data[key].map(function(r) {
      return { time: new Date(r.time), temp: r.temp_f, humidity: r.humidity, key: key };
    });
    series[key] = pts;
    allPoints = allPoints.concat(pts);
  });

  if (allPoints.length === 0) return;

  var now = new Date();
  var t0 = new Date(now - 24 * 3600 * 1000);
  x.domain([t0, now]);

  var temps = allPoints.map(function(d) { return d.temp; });
  y.domain([d3.min(temps) - 1, d3.max(temps) + 1]).nice();

  // Grid lines
  var gridSel = g.select(".grid").selectAll("line").data(y.ticks(8));
  gridSel.enter().append("line").merge(gridSel)
    .attr("x1", 0).attr("x2", w)
    .attr("y1", function(d) { return y(d); })
    .attr("y2", function(d) { return y(d); })
    .attr("stroke", "#2a2a4a").attr("stroke-dasharray", "2,4");
  gridSel.exit().remove();

  // X axis
  xAxisG.call(d3.axisBottom(x).ticks(8).tickFormat(d3.timeFormat("%H:%M")))
    .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");

  // Y axis
  yAxisG.call(d3.axisLeft(y).ticks(8).tickFormat(function(d) { return d + "\u00b0F"; }))
    .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");

  // Lines
  var line = d3.line()
    .x(function(d) { return x(d.time); })
    .y(function(d) { return y(d.temp); })
    .curve(d3.curveMonotoneX);

  keys.forEach(function(key, i) {
    var color = COLORS[i % COLORS.length];
    var sel = g.selectAll(".line-" + i).data([series[key]]);
    sel.enter().append("path").attr("class", "line-" + i)
      .attr("fill", "none").attr("stroke", color).attr("stroke-width", 2)
      .merge(sel).attr("d", line);

    // Hover dots (sparse)
    var sparse = series[key].filter(function(_, j) { return j % 5 === 0; });
    var dots = g.selectAll(".dot-" + i).data(sparse);
    dots.enter().append("circle").attr("class", "dot-" + i)
      .attr("r", 3).attr("fill", color).attr("opacity", 0)
      .on("mouseover", function(ev, d) {
        d3.select(this).attr("opacity", 1).attr("r", 5).attr("stroke", "#fff").attr("stroke-width", 1);
        var label = key.split("_ch");
        var ch = label.length > 1 ? "ch" + label[1] : key;
        tip.style("display", "block").html(
          "<strong>" + ch + "</strong><br>" +
          d.temp.toFixed(1) + "\u00b0F, " + d.humidity.toFixed(0) + "% humidity<br>" +
          d.time.toLocaleTimeString()
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
      .attr("cx", function(d) { return x(d.time); })
      .attr("cy", function(d) { return y(d.temp); });
    dots.exit().remove();
  });

  // Update current reading cards
  updateCurrent(data, keys);
}

function updateCurrent(data, keys) {
  var container = d3.select("#current");
  container.selectAll("*").remove();

  keys.forEach(function(key, i) {
    var records = data[key];
    if (!records || records.length === 0) return;
    var last = records[records.length - 1];
    var label = key.split("_ch");
    var ch = label.length > 1 ? "Channel " + label[1] : key;
    var color = COLORS[i % COLORS.length];

    var card = container.append("div").attr("class", "sensor-card");
    card.append("div").attr("class", "label").text(ch);
    card.append("div").attr("class", "temp").style("color", color)
      .text(last.temp_f.toFixed(1) + "\u00b0F");
    var hum = isNaN(last.humidity) ? "n/a" : last.humidity.toFixed(0) + "% humidity";
    card.append("div").attr("class", "humid").text(hum);
    var t = last.time.replace("T", " ").substring(0, 19);
    card.append("div").attr("class", "time").text(t);
  });
}

initChart();

// SSE connection
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last_version = -1
            try:
                while True:
                    with _data_lock:
                        version = _data_version
                        data = _current_data
                    if version != last_version:
                        payload = json.dumps(data, separators=(",", ":"))
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        last_version = version
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Quiet unless error
        if args and str(args[0]).startswith("4"):
            super().log_message(format, *args)


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser(description="Temperature web dashboard")
    parser.add_argument("--port", type=int, default=8433)
    parser.add_argument("--dir", default="sdr/sdr",
                        help="Directory with .npz chunks")
    args = parser.parse_args()
    DATA_DIR = Path(args.dir)

    # Initial load
    data = load_last_24h(DATA_DIR)
    with _data_lock:
        global _current_data, _data_version
        _current_data = data
        _data_version = 1

    n = sum(len(v) for v in data.values())
    print(f"Loaded {n} records from {len(find_chunks(DATA_DIR))} chunk(s)")
    print(f"Serving on http://0.0.0.0:{args.port}")

    # Start background poller
    t = threading.Thread(target=poll_data, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
