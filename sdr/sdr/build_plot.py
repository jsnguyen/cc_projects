#!/usr/bin/env python3
"""Build phase_plot.html with inline temperature data from SQLite database."""
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "temps.db"


def load_all_data(db_path: Path) -> dict[str, list[dict]]:
    """Load all readings from SQLite grouped by sensor."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sensor, time, temp_f, humidity FROM readings ORDER BY sensor, time"
    ).fetchall()
    conn.close()

    data: dict[str, list[dict]] = {}
    for row in rows:
        data.setdefault(row["sensor"], []).append({
            "time": row["time"],
            "temp_f": row["temp_f"],
            "humidity": row["humidity"],
        })
    return data


if not DB_PATH.exists():
    print(f"Database not found: {DB_PATH}")
    raise SystemExit(1)

data = load_all_data(DB_PATH)
filtered = {k: v for k, v in data.items() if k.startswith("Oregon")}
json_str = json.dumps(filtered, separators=(",", ":"))

html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Phase-Folded Temperature</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: "Menlo", "Consolas", monospace; }
  h1 { text-align: center; padding: 18px 0 4px; font-size: 1.3em; color: #c8d6e5; font-weight: 400; }
  .subtitle { text-align: center; font-size: 0.8em; color: #778899; margin-bottom: 6px; }
  #chart { width: 100%; display: flex; justify-content: center; }
  .tooltip {
    position: absolute; pointer-events: none; background: rgba(10,10,30,0.95);
    border: 1px solid #556; border-radius: 4px; padding: 8px 12px;
    font-size: 12px; line-height: 1.6; color: #ddd; white-space: nowrap;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  .controls { display: flex; justify-content: center; gap: 10px; padding: 4px 0 10px; flex-wrap: wrap; }
  .toggle-btn {
    background: #2a2a4a; border: 1px solid #444; border-radius: 4px;
    color: #ccc; padding: 4px 14px; font-size: 0.78em; cursor: pointer;
    font-family: inherit; transition: opacity 0.2s;
  }
  .toggle-btn:hover { border-color: #888; }
  .toggle-btn.active { border-color: #aaa; color: #fff; }
  .toggle-btn.off { opacity: 0.4; }
  .legend { display: flex; justify-content: center; gap: 24px; padding: 2px 0 12px; font-size: 0.78em; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-swatch { width: 12px; height: 12px; border-radius: 2px; }
  .stats-table { margin: 0 auto 20px; border-collapse: collapse; font-size: 0.8em; }
  .stats-table th, .stats-table td { padding: 4px 14px; border-bottom: 1px solid #333; }
  .stats-table th { color: #99aabc; font-weight: 500; }
</style>
</head>
<body>
<h1>Phase-Folded 24h Temperature Profile</h1>
<div class="subtitle">Centered on 12:00 (noon) &mdash; data from all days overlaid</div>
<div class="controls" id="controls"></div>
<div id="chart"></div>
<div class="legend" id="legend"></div>
<table class="stats-table" id="stats"></table>
<div class="tooltip" id="tip" style="display:none;"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
var RAW_DATA = __DATA__;

var COLORS = { ch0: "#ff6b6b", ch1: "#48dbfb", ch2: "#feca57" };
var NAMES  = { ch0: "Channel 0", ch1: "Channel 1", ch2: "Channel 2" };

function parseData(raw) {
  var channels = {};
  for (var key of Object.keys(raw)) {
    var records = raw[key];
    var ch = "ch" + key.split("_ch")[1];
    channels[ch] = records.map(function(r) {
      var d = new Date(r.time);
      var h = d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
      h = h - 12;
      if (h < -12) h += 24;
      if (h > 12) h -= 24;
      return { phase: h, temp: r.temp_f, humidity: r.humidity, time: r.time, date: d.toLocaleDateString() };
    });
  }
  return channels;
}

function computeStats(points, nBins) {
  var binW = 24 / nBins;
  var bins = Array.from({ length: nBins }, function(_, i) {
    return { center: -12 + binW * (i + 0.5), temps: [] };
  });
  for (var p of points) {
    var idx = Math.floor((p.phase + 12) / binW);
    var ci = Math.max(0, Math.min(nBins - 1, idx));
    bins[ci].temps.push(p.temp);
  }
  return bins.filter(function(b) { return b.temps.length > 2; }).map(function(b) {
    var s = b.temps.slice().sort(d3.ascending);
    return {
      phase: b.center, mean: d3.mean(s), median: d3.median(s),
      p10: d3.quantile(s, 0.1), p25: d3.quantile(s, 0.25),
      p75: d3.quantile(s, 0.75), p90: d3.quantile(s, 0.9),
      min: d3.min(s), max: d3.max(s), n: s.length
    };
  });
}

var channels = parseData(RAW_DATA);

var margin = { top: 24, right: 30, bottom: 50, left: 62 };
var W = Math.min(1200, window.innerWidth - 40);
var H = 540;
var w = W - margin.left - margin.right;
var h = H - margin.top - margin.bottom;

var svg = d3.select("#chart").append("svg").attr("width", W).attr("height", H);
var g = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");

var allTemps = Object.values(channels).flat().map(function(d) { return d.temp; });
var x = d3.scaleLinear().domain([-12, 12]).range([0, w]);
var y = d3.scaleLinear().domain([d3.min(allTemps) - 1, d3.max(allTemps) + 1]).range([h, 0]).nice();

// Grid
g.append("g").selectAll("line").data(y.ticks(8)).join("line")
  .attr("x1", 0).attr("x2", w)
  .attr("y1", function(d) { return y(d); })
  .attr("y2", function(d) { return y(d); })
  .attr("stroke", "#2a2a4a").attr("stroke-dasharray", "2,4");

// Noon marker
g.append("line").attr("x1", x(0)).attr("x2", x(0)).attr("y1", 0).attr("y2", h)
  .attr("stroke", "#ffaa00").attr("stroke-width", 1.5).attr("stroke-dasharray", "6,4").attr("opacity", 0.5);
g.append("text").attr("x", x(0)).attr("y", -8).attr("text-anchor", "middle")
  .attr("fill", "#ffaa00").attr("font-size", 10).text("NOON");

// X axis
var xAxis = d3.axisBottom(x).tickValues(d3.range(-12, 13, 2))
  .tickFormat(function(d) { var hr = ((d + 12) % 24); return hr.toString().padStart(2, "0") + ":00"; });
g.append("g").attr("transform", "translate(0," + h + ")").call(xAxis)
  .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");
g.append("text").attr("x", w / 2).attr("y", h + 42).attr("text-anchor", "middle")
  .attr("fill", "#99aabc").attr("font-size", 12).text("Time of Day");

// Y axis
var yAxis = d3.axisLeft(y).ticks(8).tickFormat(function(d) { return d + "\u00b0F"; });
g.append("g").call(yAxis)
  .selectAll("text,line,path").attr("stroke", "#556").attr("fill", "#889");
g.append("text").attr("transform", "rotate(-90)").attr("x", -h / 2).attr("y", -46)
  .attr("text-anchor", "middle").attr("fill", "#99aabc").attr("font-size", 12).text("Temperature (\u00b0F)");

var tip = d3.select("#tip");
var nBins = 48;
var chKeys = Object.keys(channels).sort();

var chGroups = {};
var dotsVisible = false;

chKeys.forEach(function(ch) {
  var pts = channels[ch];
  var color = COLORS[ch];
  var stats = computeStats(pts, nBins);

  var chG = g.append("g").attr("class", "ch-group-" + ch);
  chGroups[ch] = { group: chG, dots: null };

  // 10-90 band
  var area90 = d3.area()
    .x(function(d) { return x(d.phase); })
    .y0(function(d) { return y(d.p10); })
    .y1(function(d) { return y(d.p90); })
    .curve(d3.curveBasis);
  chG.append("path").datum(stats).attr("d", area90)
    .attr("fill", color).attr("opacity", 0.12).attr("class", "band90");

  // IQR band
  var area50 = d3.area()
    .x(function(d) { return x(d.phase); })
    .y0(function(d) { return y(d.p25); })
    .y1(function(d) { return y(d.p75); })
    .curve(d3.curveBasis);
  chG.append("path").datum(stats).attr("d", area50)
    .attr("fill", color).attr("opacity", 0.22).attr("class", "bandIQR");

  // Median line
  var medLine = d3.line()
    .x(function(d) { return x(d.phase); })
    .y(function(d) { return y(d.median); })
    .curve(d3.curveBasis);
  chG.append("path").datum(stats).attr("d", medLine)
    .attr("fill", "none").attr("stroke", color).attr("stroke-width", 2.5).attr("opacity", 1);

  // Mean line (dashed)
  var meanLine = d3.line()
    .x(function(d) { return x(d.phase); })
    .y(function(d) { return y(d.mean); })
    .curve(d3.curveBasis);
  chG.append("path").datum(stats).attr("d", meanLine)
    .attr("fill", "none").attr("stroke", color).attr("stroke-width", 1.2)
    .attr("stroke-dasharray", "5,4").attr("opacity", 0.6);

  // Scatter points — hidden by default, subsampled
  var subPts = pts.filter(function(_, i) { return i % 3 === 0; });
  var dots = chG.selectAll(".dot-" + ch).data(subPts).join("circle")
    .attr("cx", function(d) { return x(d.phase); })
    .attr("cy", function(d) { return y(d.temp); })
    .attr("r", 1.5).attr("fill", color).attr("opacity", 0)
    .on("mouseover", function(ev, d) {
      d3.select(this).attr("r", 5).attr("opacity", 1).attr("stroke", "#fff").attr("stroke-width", 1);
      tip.style("display", "block").html(
        "<strong>" + NAMES[ch] + "</strong><br>" +
        "Temp: <strong>" + d.temp.toFixed(1) + "\u00b0F</strong><br>" +
        "Humidity: " + (d.humidity != null ? d.humidity + "%" : "n/a") + "<br>" +
        "Time: " + d.time.replace("T", " ") + "<br>" +
        "Date: " + d.date
      );
    })
    .on("mousemove", function(ev) {
      tip.style("left", (ev.pageX + 14) + "px").style("top", (ev.pageY - 20) + "px");
    })
    .on("mouseout", function() {
      d3.select(this).attr("r", 1.5).attr("opacity", dotsVisible ? 0.15 : 0).attr("stroke", "none");
      tip.style("display", "none");
    });
  chGroups[ch].dots = dots;

  // Invisible hover target along median
  var hoverLine = chG.selectAll(".hover-" + ch).data(stats).join("circle")
    .attr("cx", function(d) { return x(d.phase); })
    .attr("cy", function(d) { return y(d.median); })
    .attr("r", 8).attr("fill", "transparent").attr("stroke", "none")
    .on("mouseover", function(ev, d) {
      tip.style("display", "block").html(
        "<strong>" + NAMES[ch] + "</strong> (bin stats)<br>" +
        "Median: <strong>" + d.median.toFixed(1) + "\u00b0F</strong><br>" +
        "Mean: " + d.mean.toFixed(1) + "\u00b0F<br>" +
        "IQR: " + d.p25.toFixed(1) + " \u2013 " + d.p75.toFixed(1) + "\u00b0F<br>" +
        "10th\u201390th: " + d.p10.toFixed(1) + " \u2013 " + d.p90.toFixed(1) + "\u00b0F<br>" +
        "n = " + d.n + " readings"
      );
    })
    .on("mousemove", function(ev) {
      tip.style("left", (ev.pageX + 14) + "px").style("top", (ev.pageY - 20) + "px");
    })
    .on("mouseout", function() { tip.style("display", "none"); });
});

// Controls
var controls = d3.select("#controls");

var dotBtn = controls.append("button").attr("class", "toggle-btn")
  .text("Show Data Points")
  .on("click", function() {
    dotsVisible = !dotsVisible;
    d3.select(this).text(dotsVisible ? "Hide Data Points" : "Show Data Points")
      .classed("active", dotsVisible);
    chKeys.forEach(function(ch) {
      chGroups[ch].dots.transition().duration(300)
        .attr("opacity", dotsVisible ? 0.15 : 0);
    });
  });

chKeys.forEach(function(ch) {
  var visible = true;
  var btn = controls.append("button").attr("class", "toggle-btn active")
    .html('<span style="color:' + COLORS[ch] + '">\u25cf</span> ' + NAMES[ch])
    .on("click", function() {
      visible = !visible;
      d3.select(this).classed("active", visible).classed("off", !visible);
      chGroups[ch].group.transition().duration(300)
        .style("opacity", visible ? 1 : 0)
        .on("end", function() {
          chGroups[ch].group.style("pointer-events", visible ? "all" : "none");
        });
    });
});

// Legend
var leg = d3.select("#legend");
leg.append("div").attr("class", "legend-item").html(
  '<span style="color:#99a">\u2501\u2501 median &nbsp; - - mean &nbsp; dark band: IQR (25\u201375%) &nbsp; light band: 10\u201390%</span>'
);

// Stats table
var table = d3.select("#stats");
var hdr = table.append("tr");
["Channel","Records","Min","Max","Mean","Median","Std Dev"].forEach(function(t) { hdr.append("th").text(t); });
chKeys.forEach(function(ch) {
  var pts = channels[ch];
  var temps = pts.map(function(d) { return d.temp; });
  var row = table.append("tr");
  row.append("td").html('<span style="color:' + COLORS[ch] + '">' + NAMES[ch] + '</span>');
  row.append("td").text(pts.length);
  row.append("td").text(d3.min(temps).toFixed(1) + "\u00b0F");
  row.append("td").text(d3.max(temps).toFixed(1) + "\u00b0F");
  row.append("td").text(d3.mean(temps).toFixed(1) + "\u00b0F");
  row.append("td").text(d3.median(temps).toFixed(1) + "\u00b0F");
  row.append("td").text(d3.deviation(temps).toFixed(2) + "\u00b0F");
});
</script>
</body>
</html>"""

html = html.replace("__DATA__", json_str)

with open(HERE / "phase_plot.html", "w") as f:
    f.write(html)

print(f"Wrote {HERE / 'phase_plot.html'} ({len(html):,} bytes)")
