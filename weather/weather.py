#!/usr/bin/env python3
"""Fetch weather forecast for a US location by name."""

import argparse
import json
import re
import sys
import requests

USER_AGENT = "weather-cli/1.0"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NWS_BASE = "https://api.weather.gov"


def geocode(location: str) -> tuple[float, float, str]:
    """Geocode a location name to (lat, lon, display_name) via Nominatim."""
    resp = requests.get(NOMINATIM_URL, params={
        "q": location,
        "format": "json",
        "countrycodes": "us",
        "limit": 1,
    }, headers={"User-Agent": USER_AGENT}, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        print(f"Error: Could not find location '{location}'")
        sys.exit(1)
    match = results[0]
    # Use name + state from display_name (e.g. "San Francisco, California, United States")
    parts = [p.strip() for p in match["display_name"].split(",")]
    name = parts[0]
    state = parts[1] if len(parts) > 1 else ""
    display = f"{name}, {state}"
    return float(match["lat"]), float(match["lon"]), display


def get_forecast(lat: float, lon: float) -> list:
    """Fetch the 7-day forecast from NWS for the given coordinates."""
    headers = {"User-Agent": USER_AGENT}

    points_resp = requests.get(
        f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}",
        headers=headers, timeout=10,
    )
    points_resp.raise_for_status()
    props = points_resp.json()["properties"]

    forecast_url = props["forecast"]
    fc_resp = requests.get(forecast_url, headers=headers, timeout=10)
    fc_resp.raise_for_status()
    return fc_resp.json()["properties"]["periods"]


def weather_emoji(forecast: str) -> str:
    """Map a short forecast string to an emoji."""
    f = forecast.lower()
    if "thunder" in f:
        return "⛈️"
    if "snow" in f or "blizzard" in f:
        return "🌨️"
    if "rain" in f or "showers" in f or "drizzle" in f:
        return "🌧️"
    if "fog" in f or "haze" in f or "mist" in f:
        return "🌫️"
    if "sunny" in f or "clear" in f:
        if "partly" in f:
            return "⛅"
        if "mostly" in f:
            return "🌤️"
        return "☀️"
    if "cloudy" in f:
        if "partly" in f:
            return "⛅"
        if "mostly" in f:
            return "🌥️"
        return "☁️"
    return "🌡️"


def extract_rain(detail: str) -> str | None:
    """Extract rainfall amount from detailedForecast text."""
    m = re.search(r"[Nn]ew rainfall amounts (.+?)\.", detail)
    if m:
        return m.group(1)
    return None


def pair_periods(periods: list) -> list[dict]:
    """Combine day/night periods into single entries."""
    paired = []
    i = 0
    while i < len(periods):
        p = periods[i]
        if p["isDaytime"]:
            day = p
            night = periods[i + 1] if i + 1 < len(periods) and not periods[i + 1]["isDaytime"] else None
            i += 2 if night else 1
        else:
            day = None
            night = p
            i += 1
        paired.append({"day": day, "night": night})
    return paired


def select_days(paired: list, when: str) -> tuple[list, str]:
    """Select paired entries based on a 'when' specifier. Returns (entries, label)."""
    if when == "tomorrow":
        if len(paired) < 2:
            return paired, "Tomorrow"
        return [paired[1]], "Tomorrow"
    if when == "weekend":
        weekend_names = {"Saturday", "Sunday"}
        selected = []
        for entry in paired:
            name = entry["day"]["name"] if entry["day"] else ""
            if name in weekend_names:
                selected.append(entry)
        if not selected:
            return paired[:2], "Weekend"
        return selected, "Weekend"
    # numeric: already sliced by caller
    n = len(paired)
    label = "Today" if n == 1 else f"{n}-day forecast"
    return paired, label


def display(location: str, paired: list, label: str):
    """Print the forecast in a readable format."""
    print(f"\n{label} for {location}")
    print("-" * 40)

    for entry in paired:
        day, night = entry["day"], entry["night"]
        name = day["name"] if day else night["name"].replace(" Night", "").replace("Overnight", "Tonight")
        primary = day or night
        emoji = weather_emoji(primary["shortForecast"])

        parts = []
        if day:
            parts.append(f"H:{day['temperature']}°")
        if night:
            parts.append(f"L:{night['temperature']}°")
        temps = " ".join(parts)

        if day and night:
            if day["shortForecast"] == night["shortForecast"]:
                desc = day["shortForecast"]
            else:
                desc = f"{day['shortForecast']} / {night['shortForecast']}"
        else:
            desc = primary["shortForecast"]

        rain_day = extract_rain(day.get("detailedForecast", "")) if day else None
        rain_night = extract_rain(night.get("detailedForecast", "")) if night else None
        rain = rain_day or rain_night

        line = f"  {emoji} {name:<16} {temps:<14} {desc}"
        if rain:
            line += f"  ({rain})"
        print(line)

    print()


def display_brief(location: str, paired: list):
    """Print a single-line SMS-friendly forecast."""
    lines = []
    for entry in paired:
        day, night = entry["day"], entry["night"]
        primary = day or night
        emoji = weather_emoji(primary["shortForecast"])

        parts = []
        if day:
            parts.append(f"{day['temperature']}°")
        if night:
            parts.append(f"{night['temperature']}°")
        temps = "/".join(parts)

        # Use daytime forecast as primary description
        desc = primary["shortForecast"]
        # Shorten common long phrases
        desc = desc.replace("Slight Chance ", "").replace("Chance ", "")

        rain = None
        if day:
            rain = extract_rain(day.get("detailedForecast", ""))
        if not rain and night:
            rain = extract_rain(night.get("detailedForecast", ""))

        line = f"{emoji} {temps} {desc}"
        if rain:
            line += f" ({rain})"
        lines.append(line)

    header = location
    if len(lines) == 1:
        print(f"{header}: {lines[0]}")
    else:
        print(header)
        for line in lines:
            print(f"  {line}")


def build_json(location: str, paired: list, label: str) -> dict:
    """Build a JSON-serializable dict of the forecast."""
    days = []
    for entry in paired:
        day, night = entry["day"], entry["night"]
        name = day["name"] if day else night["name"].replace(" Night", "").replace("Overnight", "Tonight")
        record = {"name": name}
        if day:
            record["high"] = day["temperature"]
            record["day_forecast"] = day["shortForecast"]
            record["day_detail"] = day["detailedForecast"]
            record["day_wind"] = f"{day['windSpeed']} {day['windDirection']}"
        if night:
            record["low"] = night["temperature"]
            record["night_forecast"] = night["shortForecast"]
            record["night_detail"] = night["detailedForecast"]
            record["night_wind"] = f"{night['windSpeed']} {night['windDirection']}"
        rain = None
        if day:
            rain = extract_rain(day.get("detailedForecast", ""))
        if not rain and night:
            rain = extract_rain(night.get("detailedForecast", ""))
        if rain:
            record["rainfall"] = rain
        days.append(record)
    return {"location": location, "label": label, "days": days}



def main():
    parser = argparse.ArgumentParser(
        description="Fetch US weather forecast for a given location.",
        epilog="""examples:
  %(prog)s "Denver, CO"              today's forecast
  %(prog)s "Denver, CO" tomorrow     tomorrow's forecast
  %(prog)s "Denver, CO" weekend      Saturday & Sunday
  %(prog)s "Denver, CO" -d 3         next 3 days
  %(prog)s "Denver, CO" -d 7         full 7-day forecast
  %(prog)s "Denver, CO" -b           brief one-liner for today
  %(prog)s "Denver, CO" -b -d 3     brief 3-day summary""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("location", help="US location name (e.g. \"San Francisco, CA\")")
    parser.add_argument(
        "when", nargs="?", default=None,
        help="'tomorrow', 'weekend', or omit for today",
    )
    parser.add_argument(
        "-d", "--days", type=int, default=None, choices=range(1, 8), metavar="N",
        help="number of days to show (1-7)",
    )
    parser.add_argument(
        "-b", "--brief", action="store_true",
        help="brief output, suitable for texts/notifications",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="output as JSON",
    )
    args = parser.parse_args()

    when = args.when or ("today" if args.days is None else None)
    days = args.days or 1

    try:
        lat, lon, location = geocode(args.location)
        periods = get_forecast(lat, lon)
        paired = pair_periods(periods)
        if when in ("tomorrow", "weekend"):
            paired, label = select_days(paired, when)
        else:
            paired = paired[:days]
            _, label = select_days(paired, "numeric")
        if args.json:
            print(json.dumps(build_json(location, paired, label), indent=2))
        elif args.brief:
            display_brief(location, paired)
        else:
            display(location, paired, label)
    except requests.HTTPError as e:
        print(f"API error: {e}")
        sys.exit(1)
    except requests.ConnectionError:
        print("Error: Could not connect to the API. Check your internet connection.")
        sys.exit(1)
    except (KeyError, IndexError) as e:
        print(f"Error: Unexpected API response format: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
