"""
Daily Walk & Lunch Notifier for Microsoft Teams.

Fetches the noon weather forecast and today's cafeteria lunch specials,
then posts a formatted Adaptive Card to a Teams channel via webhook.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LATITUDE = 37.5136
LONGITUDE = -122.2006
LOCATION_LABEL = "1400 Seaport Blvd, Redwood City"

CAFE_URL = "https://c3ai.cafebonappetit.com/"

DATA_DIR = Path(__file__).resolve().parent / "data"
MENUS_DIR = DATA_DIR / "menus"
HISTORY_FILE = DATA_DIR / "menu-history.json"
LAYOUT_DIR = DATA_DIR / "layout"
MAP_SOURCE = Path(__file__).resolve().parent / "assets" / "cafeteria-map.png"

# Passes fetched data from the generate phase to the send phase (untracked).
STATE_FILE = Path(__file__).resolve().parent / ".run-state.json"

PT = timezone(timedelta(hours=-7))  # PDT; close enough year-round for display

# WMO weather interpretation codes → human-readable descriptions
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

RAINY_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

WALK_HOURS = ("T11:00", "T12:00", "T13:00", "T14:00")


def _extract_noon_weather(times, temps, feels, precips, winds, codes, uvs, date_str):
    """Extract 11:30am-2pm weather for a specific date (YYYY-MM-DD).

    Open-Meteo reports hourly, so we use the 11:00, 12:00, 13:00, and 14:00
    hours to cover the full 11:30am-2:00pm window.
    """
    indices = [
        i for i, t in enumerate(times)
        if t.startswith(date_str) and t.endswith(WALK_HOURS)
    ]
    if not indices:
        return None

    avg_temp = sum(temps[i] for i in indices) / len(indices)
    avg_feels = sum(feels[i] for i in indices) / len(indices)
    max_precip = max(precips[i] for i in indices)
    avg_wind = sum(winds[i] for i in indices) / len(indices)
    worst_code = max(codes[i] for i in indices)
    max_uv = max(uvs[i] for i in indices)

    return {
        "temp_f": round(avg_temp),
        "feels_like_f": round(avg_feels),
        "precip_pct": max_precip,
        "wind_mph": round(avg_wind),
        "uv_index": round(max_uv, 1),
        "condition": WMO_CODES.get(worst_code, "Unknown"),
        "weather_code": worst_code,
        "date": date_str,
    }


def fetch_weather() -> dict | None:
    """Return a dict with today's and tomorrow's noon-hour weather data."""
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": LATITUDE,
                    "longitude": LONGITUDE,
                    "hourly": "temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m,weather_code,uv_index",
                    "forecast_days": 2,
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "America/Los_Angeles",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:
            print(f"Weather fetch attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            if attempt < 2:
                import time
                time.sleep(5)
            else:
                return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    feels = hourly.get("apparent_temperature", []) or temps
    precips = hourly.get("precipitation_probability", [])
    winds = hourly.get("wind_speed_10m", [])
    codes = hourly.get("weather_code", [])
    uvs = hourly.get("uv_index", [])
    if not uvs:
        uvs = [0] * len(times)
    now = datetime.now(PT)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    today = _extract_noon_weather(times, temps, feels, precips, winds, codes, uvs, today_str)
    tomorrow = _extract_noon_weather(times, temps, feels, precips, winds, codes, uvs, tomorrow_str)

    if not today:
        print(f"No noon data found for today ({today_str}) in weather response.", file=sys.stderr)
        print(f"  Times available: {times[:5]}...{times[-3:]}" if times else "  No times returned", file=sys.stderr)
        return None

    return {"today": today, "tomorrow": tomorrow}


def walk_score(w: dict) -> float:
    """Return 0-10 score for how pleasant the walk will be."""
    score = 100

    # Temperature: use feels-like so wind chill (cold) and heat index (hot)
    # are reflected in the score. Ideal range 65-75°F.
    temp = w.get("feels_like_f", w["temp_f"])
    if temp < 65:
        score -= min(int((65 - temp) * 2), 40)
    elif temp > 75:
        score -= min(int((temp - 75) * 2), 40)

    # Precipitation probability — very aggressive scaling
    precip = w["precip_pct"]
    if precip > 0:
        score -= max(10, int(precip * 1.1))

    # Wind: comfortable under 7 mph, increasingly unpleasant above that
    wind = w["wind_mph"]
    if wind > 7:
        score -= min(int((wind - 7) * 4), 50)

    # Weather code penalties (on top of precip penalty)
    code = w["weather_code"]
    if code in RAINY_CODES:
        score -= 15
    elif code in {45, 48}:  # fog
        score -= 10
    elif code in {71, 73, 75, 77, 85, 86}:  # snow
        score -= 30

    # UV index: comfortable under 5, harsh above that
    uv = w.get("uv_index", 0)
    if uv > 5:
        score -= min(int((uv - 5) * 5), 30)

    return round(max(0, min(100, score)) / 10, 1)


def _score_emoji(score: float) -> str:
    if score >= 7:
        return "\U0001f7e2"  # green circle
    if score >= 4:
        return "\U0001f7e1"  # yellow circle
    return "\U0001f534"      # red circle


def walk_recommendation(w: dict) -> str:
    score = walk_score(w)
    code = w["weather_code"]
    precip = w["precip_pct"]
    temp = w["temp_f"]
    wind = w["wind_mph"]

    if code in RAINY_CODES or precip > 20:
        return "Bring an umbrella — rain is likely during your walk."
    if temp < 50:
        return "Bundle up — it's chilly out there."
    if wind >= 15:
        return "It's windy today — heads up on the walk."
    if temp >= 80:
        return "It's hot — maybe stick to the shady route."
    if score >= 8:
        return "Great day for a walk!"
    if score >= 5:
        return "Decent conditions for a walk."
    return "Tough conditions today — consider an indoor walk."


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

DIETARY_SHORT = {
    "Vegetarian": "V",
    "Vegan": "VG",
    "Made without Gluten-Containing Ingredients": "GF",
    "Farm to Fork": "F2F",
    "Seafood Watch": "SW",
    "Humane": "H",
}


FILLER_WORDS = {
    "with", "and", "of", "the", "a", "an", "in", "on", "or", "choice",
    "your", "made", "to", "order", "from", "two", "cafe", "café",
    "hand", "tossed", "served", "topped", "fresh", "classic",
}


def _build_image_query(name: str, description: str) -> str:
    """Build a concise Pexels search query from a dish name and description.

    Extracts the most meaningful food words so that dishes like
    'Fennel Faratto' with description 'farro with fire roasted tomatoes,
    fennel, parmesan cheese' produce a query like
    'fennel faratto farro tomatoes parmesan'.
    """
    desc_words = [
        w for w in description.lower().replace(",", "").split()
        if w not in FILLER_WORDS and len(w) > 2
    ]
    # Take up to 5 key words from the description to keep the query focused
    key_desc = " ".join(desc_words[:5])
    return f"{name} {key_desc}".strip()


def _best_photo(photos: list[dict], name: str, description: str) -> str:
    """Pick the photo whose alt text best matches the dish name/description."""
    if len(photos) == 1:
        return photos[0]["src"]["medium"]

    keywords = {
        w.lower() for w in (name + " " + description).replace(",", "").split()
        if w.lower() not in FILLER_WORDS and len(w) > 2
    }

    best_url = photos[0]["src"]["medium"]
    best_hits = -1
    for photo in photos:
        alt = (photo.get("alt") or "").lower()
        hits = sum(1 for kw in keywords if kw in alt)
        if hits > best_hits:
            best_hits = hits
            best_url = photo["src"]["medium"]
    return best_url


def search_food_image(name: str, description: str = "") -> str | None:
    """Search Pexels for a food photo and return the best-matching medium URL."""
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None

    query = _build_image_query(name, description)

    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": f"{query} food dish", "per_page": 8, "orientation": "square"},
            timeout=10,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            return _best_photo(photos, name, description)
    except Exception as exc:
        print(f"Pexels search failed for '{name}': {exc}", file=sys.stderr)
    return None


def fetch_menu() -> list[dict] | None:
    """Return a list of lunch-special dicts, or None on failure."""
    try:
        resp = requests.get(CAFE_URL, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        print(f"Menu fetch failed: {exc}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    lunch_section = soup.find(attrs={"data-daypart-id": "3"})
    if not lunch_section:
        print("Could not find lunch section in page", file=sys.stderr)
        return None

    # The first tab-content panel holds the daily specials
    specials_panel = lunch_section.find("div", class_="c-tab__content")
    if not specials_panel:
        specials_panel = lunch_section

    items = []
    for item_div in specials_panel.find_all("div", class_="site-panel__daypart-item"):
        title_btn = item_div.find("button", class_="site-panel__daypart-item-title")
        if not title_btn:
            continue

        name = title_btn.get("aria-label", "").replace("More info about ", "").strip()
        if not name:
            name = title_btn.get_text(strip=True)
        name = name.title()

        desc_div = item_div.find("div", class_="site-panel__daypart-item-description")
        description = desc_div.get_text(strip=True) if desc_div else ""
        if description:
            description = description[0].upper() + description[1:]

        station_div = item_div.find("div", class_="site-panel__daypart-item-station")
        station = station_div.get_text(strip=True) if station_div else ""

        icons_span = item_div.find("span", class_="site-panel__daypart-item-cor-icons")
        dietary = []
        if icons_span:
            for img in icons_span.find_all("img"):
                alt = img.get("alt", "")
                for full, short in DIETARY_SHORT.items():
                    if full in alt:
                        dietary.append(short)
                        break

        items.append({
            "name": name,
            "description": description,
            "station": station,
            "dietary": dietary,
            "image_url": None,
        })

    if not items:
        return None

    print(f"Searching images for {len(items)} menu items…")
    for item in items:
        item["image_url"] = search_food_image(item["name"], item["description"])

    return items


# ---------------------------------------------------------------------------
# Allergens, sides, and menu history
# ---------------------------------------------------------------------------

# Word-boundary matching avoids false positives like "butternut squash",
# "nutmeg", "doughnut", and "water chestnut".
NUT_PATTERN = re.compile(
    r"\b(peanuts?|walnuts?|almonds?|pecans?|cashews?|pistachios?|"
    r"hazelnuts?|macadamias?|pine nuts?|brazil nuts?|filberts?|"
    r"mixed nuts?|tree nuts?|praline|marzipan|nutella)\b",
    re.IGNORECASE,
)

SHELLFISH_PATTERN = re.compile(
    r"\b(shellfish|shrimps?|prawns?|crabs?|lobsters?|crawfish|crayfish|"
    r"clams?|mussels?|oysters?|scallops?|calamari|squid|octopus)\b",
    re.IGNORECASE,
)

# Case-sensitive and no leading word boundary: the site often concatenates the
# marker straight onto the text ("...browned butter rouxSIDES: dirty rice").
SIDES_PATTERN = re.compile(r"\s*SIDES?\s*:\s*")

# Dishes that repeat so often a "last served" label would be noise.
REPEAT_EXEMPT_PATTERN = re.compile(r"\b(pizza|cookies?)\b", re.IGNORECASE)


def find_allergens(pattern: re.Pattern, text: str) -> list[str]:
    """Return sorted unique matches, title-cased (e.g. ['Almonds', 'Walnuts'])."""
    return sorted({m.group(1).title() for m in pattern.finditer(text)})


def split_sides(description: str) -> tuple[str, str | None]:
    """Split a 'SIDES:' segment out of a description. Returns (main, sides)."""
    m = SIDES_PATTERN.search(description)
    if not m:
        return description.strip(), None
    main = description[: m.start()].strip()
    sides = description[m.end():].strip()
    return main, (sides or None)


def _normalize_name(name: str) -> str:
    return " ".join(name.lower().split())


def _is_repeat_exempt(name: str, station: str) -> bool:
    """Pizza and cookies repeat constantly; @melted is the pizza station."""
    return bool(REPEAT_EXEMPT_PATTERN.search(name)) or station.lower() == "@melted"


def load_history() -> dict:
    """Read data/menu-history.json ({normalized name: [YYYY-MM-DD, ...]})."""
    try:
        return json.loads(HISTORY_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"Could not read menu history: {exc}", file=sys.stderr)
        return {}


def record_menu(items: list[dict], today_str: str) -> dict:
    """Write today's snapshot to data/menus/ and update the history index.

    Returns the updated history. Idempotent for the same day.
    """
    MENUS_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = [
        {k: item.get(k) for k in ("name", "description", "station", "dietary")}
        for item in items
    ]
    snapshot_path = MENUS_DIR / f"{today_str}.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

    history = load_history()
    for item in items:
        key = _normalize_name(item["name"])
        if not key:
            continue
        dates = history.setdefault(key, [])
        if today_str not in dates:
            dates.append(today_str)
            dates.sort()
    HISTORY_FILE.write_text(json.dumps(history, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"Recorded {len(items)} items to {snapshot_path.name} and menu-history.json")
    return history


def last_served_label(name: str, station: str, history: dict, today_str: str) -> str | None:
    """Return e.g. 'Last served 2 weeks ago', or None if new/exempt."""
    if _is_repeat_exempt(name, station):
        return None
    dates = history.get(_normalize_name(name), [])
    prior = [d for d in dates if d < today_str]
    if not prior:
        return None
    days = (date.fromisoformat(today_str) - date.fromisoformat(max(prior))).days
    if days <= 0:
        return None
    if days == 1:
        return "Last served yesterday"
    if days < 7:
        return f"Last served {days} days ago"
    if days < 30:
        weeks = max(1, round(days / 7))
        return "Last served 1 week ago" if weeks == 1 else f"Last served {weeks} weeks ago"
    months = max(1, round(days / 30))
    return "Last served 1 month ago" if months == 1 else f"Last served {months} months ago"


def display_item_name(name: str, station: str) -> str:
    """Apply display transforms: 'Pizza' suffix for @melted, strip 'Soy-Enriched'."""
    if station.lower() == "@melted" and not name.lower().rstrip().endswith("pizza"):
        name = f"{name} Pizza"
    if station.lower() == "@sweets":
        name = name.replace("Soy-Enriched ", "").replace("Soy Enriched ", "")
    return name


# ---------------------------------------------------------------------------
# Cafeteria map rendering
# ---------------------------------------------------------------------------

# Callout definitions in base-map coordinates (1024 x 549). Each station's
# items are drawn inside a subtle rounded container connected to the station
# by a line.
#   anchor: point on the station shape where the connector line starts
#   box:    optional fixed (x, y, max_width); stations without one share a row
#           of identical, evenly spaced columns divided among the stations
#           that actually have items, maximizing the empty lower area so the
#           shared auto-fit can pick the largest possible text size.
MAP_RENDER_SCALE = 2  # render at 2x so text stays crisp when Teams scales it

# All callout coordinates below are expressed in this base coordinate space.
# The source asset may be exported at any multiple of it; larger exports are
# used as-is (downscaled to exactly 2x base) so the background stays sharp.
MAP_BASE_SIZE = (1024, 549)

MAP_CALLOUTS = {
    "@melted":  {"anchor": (72, 114),  "color": (123, 88, 0)},
    # Anchor sits partway up the BITES diagonal edge so the connector drops at
    # roughly 45 degrees into its callout below.
    "@bites":   {"anchor": (192, 92), "color": (74, 124, 32)},
    "@grown":   {"anchor": (360, 81),  "color": (110, 110, 110)},
    "@charred": {"anchor": (521, 81),  "color": (67, 53, 214)},
    "@broiled": {"anchor": (751, 81),  "color": (110, 110, 110)},
    "@spiced":  {"anchor": (936, 81),  "color": (214, 94, 10)},
    # SWEETS sits on the right edge, so its callout hangs below the station.
    "@sweets":  {"anchor": (993, 424), "box": (866, 455, 150), "color": (194, 24, 91),
                 "max_h": 86},
}

# Shared row geometry for the evenly sized columns (stops clear of SWEETS).
MAP_ROW_Y = 147
MAP_ROW_X0, MAP_ROW_X1 = 8, 953
MAP_COL_GAP = 12

MAP_MAX_CALLOUT_H = 288  # default max callout height (base coords) before font shrinks

MAP_WARNING_COLOR = (196, 32, 32)   # allergen warnings
MAP_LABEL_COLOR = (128, 128, 128)   # "last served" labels

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# Bundled Inter (OFL license) first so rendering matches on any machine,
# then system fallbacks just in case.
FONT_PATHS = {
    "semibold": [
        str(FONTS_DIR / "Inter-SemiBold.ttf"),
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "medium": [
        str(FONTS_DIR / "Inter-Medium.ttf"),
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}


def _load_font(size: int, weight: str = "semibold"):
    from PIL import ImageFont
    for path in FONT_PATHS.get(weight, []):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}" if cur else word
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _tint(color: tuple[int, int, int], toward_white: float) -> tuple[int, int, int]:
    """Blend a color toward white; toward_white=1.0 gives pure white."""
    return tuple(round(c + (255 - c) * toward_white) for c in color)


def render_menu_map(
    items: list[dict],
    out_path: Path,
    history: dict | None = None,
    today_str: str | None = None,
) -> bool:
    """Draw today's menu items in callout boxes connected to their stations."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not installed; skipping map render.", file=sys.stderr)
        return False
    if not MAP_SOURCE.exists():
        print(f"Map asset missing ({MAP_SOURCE}); skipping map render.", file=sys.stderr)
        return False

    if history is None:
        history = load_history()
    if today_str is None:
        today_str = datetime.now(PT).strftime("%Y-%m-%d")

    try:
        s = MAP_RENDER_SCALE
        img = Image.open(MAP_SOURCE).convert("RGB")
        target = (MAP_BASE_SIZE[0] * s, MAP_BASE_SIZE[1] * s)
        if img.size != target:
            if img.width < target[0]:
                print(
                    f"Note: map asset is {img.width}x{img.height}; exporting it at "
                    f"{target[0]}x{target[1]} (or larger) would avoid upscaling blur.",
                    file=sys.stderr,
                )
            img = img.resize(target, Image.LANCZOS)
        draw = ImageDraw.Draw(img)

        grouped: dict[str, list[dict]] = {}
        for item in items:
            grouped.setdefault((item.get("station") or "").lower(), []).append(item)

        pad = 12 * s
        radius = 7 * s

        active = [
            (station, spec, grouped[station])
            for station, spec in MAP_CALLOUTS.items()
            if grouped.get(station)
        ]

        # Callout geometry (base coords). Stations with a fixed box keep it;
        # the rest share the row, divided into identical evenly spaced columns
        # so the day's active stations use all of the available width.
        boxes: dict[str, tuple[int, int, int]] = {
            st: spec["box"] for st, spec, _ in active if "box" in spec
        }
        row = sorted(
            (st for st, spec, _ in active if "box" not in spec),
            key=lambda st: MAP_CALLOUTS[st]["anchor"][0],
        )
        if row:
            n = len(row)
            col_w = (MAP_ROW_X1 - MAP_ROW_X0 - (n - 1) * MAP_COL_GAP) // n
            for i, st in enumerate(row):
                x = MAP_ROW_X0 + i * (col_w + MAP_COL_GAP)
                boxes[st] = (x, MAP_ROW_Y, col_w)

        def callout_lines(station, spec, entries, size):
            """Lay out one callout's text at the given title size.

            Returns a list of (text, font, line_height, fill, gap_before).
            """
            text_w = boxes[station][2] * s - 2 * pad
            color = spec["color"]
            # Descriptions use a desaturated blend of the station color.
            muted = tuple(round(c * 0.55 + 128 * 0.45) for c in color)
            title_font = _load_font(size)
            desc_size = max(round(size * 0.78), 8 * s)
            # Main descriptions are a step heavier than the sides line so the
            # two read as distinct levels.
            desc_font = _load_font(desc_size, weight="semibold")
            sub_font = _load_font(desc_size, weight="medium")
            title_lh = size + size // 4
            desc_lh = desc_size + desc_size // 4
            item_gap = round(size * 1.3)   # blank space between menu items
            desc_gap = size // 5           # small gap between title and description
            sides_gap = desc_size // 3     # slight gap between description and sides

            lines: list[tuple[str, object, int, tuple, int]] = []
            for i, it in enumerate(entries):
                name = display_item_name(it["name"], station)
                for j, ln in enumerate(_wrap_text(draw, name, title_font, text_w)):
                    gap = item_gap if (j == 0 and i > 0) else 0
                    lines.append((ln, title_font, title_lh, color, gap))

                served = last_served_label(it["name"], station, history, today_str)
                if served:
                    for ln in _wrap_text(draw, served, sub_font, text_w):
                        lines.append((ln, sub_font, desc_lh, MAP_LABEL_COLOR, 0))

                full_text = name + " " + (it.get("description") or "")
                warnings = (
                    find_allergens(NUT_PATTERN, full_text)
                    + find_allergens(SHELLFISH_PATTERN, full_text)
                )
                if warnings:
                    warn_text = "⚠ Contains " + ", ".join(warnings)
                    for ln in _wrap_text(draw, warn_text, sub_font, text_w):
                        lines.append((ln, sub_font, desc_lh, MAP_WARNING_COLOR, 0))

                main, sides = split_sides(it.get("description") or "")
                if main:
                    for j, ln in enumerate(_wrap_text(draw, main, desc_font, text_w)):
                        lines.append((ln, desc_font, desc_lh, muted,
                                      desc_gap if j == 0 else 0))
                if sides:
                    for j, ln in enumerate(_wrap_text(draw, f"Sides: {sides}", sub_font, text_w)):
                        lines.append((ln, sub_font, desc_lh, muted,
                                      sides_gap if j == 0 else 0))
            return lines

        def content_height(lines) -> int:
            return sum(lh + gap for _, _, lh, _, gap in lines)

        # One common text size for every callout: the largest size at which
        # all callouts fit their max height. Only shrinks when space runs out.
        min_size, max_size = 9 * s, 20 * s
        common_size = min_size
        for size in range(max_size, min_size - 1, -s):
            if all(
                content_height(callout_lines(st, spec, entries, size)) + 2 * pad
                <= spec.get("max_h", MAP_MAX_CALLOUT_H) * s
                for st, spec, entries in active
            ):
                common_size = size
                break

        for station, spec, entries in active:
            max_h = spec.get("max_h", MAP_MAX_CALLOUT_H) * s
            bx, by, bw = [v * s for v in boxes[station]]
            ax, ay = [v * s for v in spec["anchor"]]
            color = spec["color"]

            chosen = callout_lines(station, spec, entries, common_size)
            box_h = min(content_height(chosen) + 2 * pad, max_h)

            # Connector: runs perfectly straight when the anchor lines up with
            # the callout, otherwise angles to the nearest point on that edge.
            # The endpoint overshoots into the box (hidden under its fill) so
            # angled lines meet the border cleanly even on rounded corners.
            if ay <= by:  # station above the box -> drop through the top edge
                cx = min(max(ax, bx + radius), bx + bw - radius)
                cy2 = by + radius
            else:  # station beside the box -> run through the right edge
                cx = bx + bw - radius
                cy2 = min(max(ay, by + radius), by + box_h - radius)
            draw.line([(ax, ay), (cx, cy2)], fill=_tint(color, 0.35), width=2 * s)
            r = 3 * s
            draw.ellipse([ax - r, ay - r, ax + r, ay + r], fill=_tint(color, 0.35))

            # Subtle container: light tinted fill with a soft matching border.
            draw.rounded_rectangle(
                [bx, by, bx + bw, by + box_h],
                radius=radius,
                fill=_tint(color, 0.93),
                outline=_tint(color, 0.55),
                width=1 * s,
            )

            ty = by + pad
            for text, font, lh, fill, gap in chosen:
                ty += gap
                if ty + lh > by + box_h - pad + lh // 2:
                    break  # box full; remaining details are in the card's text list
                draw.text((bx + pad, ty), text, fill=fill, font=font)
                ty += lh

        stamp_font = _load_font(12 * s, weight="medium")
        stamp = datetime.now(PT).strftime("Menu for %A, %B %-d, %Y")
        draw.text((8 * s, img.height - 20 * s), stamp, fill=(150, 150, 150), font=stamp_font)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG", optimize=True)
        print(f"Rendered menu map to {out_path}")
        return True
    except Exception as exc:
        print(f"Map render failed (continuing without it): {exc}", file=sys.stderr)
        return False


def map_image_url(today_str: str) -> str:
    """Public raw URL where the committed map will live once pushed."""
    repo = os.environ.get("GITHUB_REPOSITORY", "c3-maxchan/walk-lunch-notifier")
    return f"https://raw.githubusercontent.com/{repo}/main/data/layout/{today_str}.png"


# ---------------------------------------------------------------------------
# Teams message
# ---------------------------------------------------------------------------

def build_adaptive_card(weather: dict | None, menu: list[dict] | None, history: dict | None = None, map_url: str | None = None) -> dict:
    today_str = datetime.now(PT).strftime("%A, %B %-d")
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    history = history or {}

    body = [
        {
            "type": "TextBlock",
            "size": "Large",
            "weight": "Bolder",
            "text": today_str,
        }
    ]

    # --- Weather section ---
    if weather:
        today_w = weather["today"]
        tomorrow_w = weather.get("tomorrow")

        today_score = walk_score(today_w)
        rec = walk_recommendation(today_w)
        today_date = datetime.strptime(today_w["date"], "%Y-%m-%d")
        today_label = today_date.strftime("%a %-m/%-d")

        body.append({
            "type": "TextBlock",
            "text": f"{_score_emoji(today_score)} Walk Forecast — {today_score}/10",
            "wrap": True,
            "spacing": "Medium",
            "size": "Large",
            "weight": "Bolder",
        })

        body.append({
            "type": "TextBlock",
            "text": f"{rec}  ·  11:30 AM – 2:00 PM",
            "wrap": True,
            "spacing": "Small",
            "isSubtle": True,
        })

        tmrw_label = ""
        tmrw_score_str = ""
        if tomorrow_w:
            tmrw_date = datetime.strptime(tomorrow_w["date"], "%Y-%m-%d")
            tmrw_label = tmrw_date.strftime("%a %-m/%-d")
            tmrw_score_val = walk_score(tomorrow_w)
            tmrw_score_str = f"{_score_emoji(tmrw_score_val)} {tmrw_score_val}/10"

        def _weather_row(field, today_val, tmrw_val):
            cells = [
                {"type": "TableCell", "verticalContentAlignment": "Center", "items": [
                    {"type": "TextBlock", "text": f"**{field}**", "wrap": True},
                ]},
                {"type": "TableCell", "verticalContentAlignment": "Center", "items": [
                    {"type": "TextBlock", "text": f"**{today_val}**", "weight": "Bolder", "wrap": True},
                ]},
            ]
            if tomorrow_w:
                cells.append(
                    {"type": "TableCell", "verticalContentAlignment": "Center", "items": [
                        {"type": "TextBlock", "text": str(tmrw_val), "wrap": True, "isSubtle": True},
                    ]}
                )
            return {"type": "TableRow", "cells": cells}

        header_cells = [
            {"type": "TableCell", "items": [
                {"type": "TextBlock", "text": " ", "wrap": True},
            ]},
            {"type": "TableCell", "items": [
                {"type": "TextBlock", "text": f"**Today** ({today_label})", "weight": "Bolder", "wrap": True},
            ]},
        ]
        col_defs = [{"width": 1}, {"width": 1}]

        if tomorrow_w:
            header_cells.append(
                {"type": "TableCell", "items": [
                    {"type": "TextBlock", "text": f"Tomorrow ({tmrw_label})", "wrap": True, "isSubtle": True},
                ]}
            )
            col_defs.append({"width": 1})

        header_row = {"type": "TableRow", "style": "accent", "cells": header_cells}

        rows = [
            header_row,
            _weather_row("Walk Score", f"{_score_emoji(today_score)} {today_score}/10",
                         tmrw_score_str),
            _weather_row("Condition", today_w["condition"],
                         tomorrow_w["condition"] if tomorrow_w else ""),
            _weather_row("Temperature", f"{today_w['temp_f']}°F",
                         f"{tomorrow_w['temp_f']}°F" if tomorrow_w else ""),
            _weather_row("Feels Like", f"{today_w['feels_like_f']}°F",
                         f"{tomorrow_w['feels_like_f']}°F" if tomorrow_w else ""),
            _weather_row("Precip. chance", f"{today_w['precip_pct']}%",
                         f"{tomorrow_w['precip_pct']}%" if tomorrow_w else ""),
            _weather_row("Wind", f"{today_w['wind_mph']} mph",
                         f"{tomorrow_w['wind_mph']} mph" if tomorrow_w else ""),
            _weather_row("UV Index", f"{today_w['uv_index']}",
                         f"{tomorrow_w['uv_index']}" if tomorrow_w else ""),
        ]

        body.append({
            "type": "Table",
            "gridStyle": "accent",
            "showGridLines": True,
            "firstRowAsHeader": True,
            "columns": col_defs,
            "rows": rows,
            "spacing": "Small",
        })
    else:
        body.append({
            "type": "TextBlock",
            "text": "_Weather data unavailable today._",
            "wrap": True,
            "isSubtle": True,
        })

    # --- Separator ---
    body.append({
        "type": "TextBlock",
        "text": "---",
        "spacing": "Medium",
    })

    # --- Menu section ---
    body.append({
        "type": "TextBlock",
        "size": "Large",
        "weight": "Bolder",
        "text": "Today's Lunch Specials",
        "spacing": "Medium",
    })

    if menu and map_url:
        body.append({
            "type": "Image",
            "url": map_url,
            "size": "Stretch",
            "altText": "Cafeteria map showing today's menu items at each station",
            "spacing": "Small",
        })

    if menu:
        STATION_ORDER = [
            "@charred", "@spiced", "@bites", "@melted",
            "@sweets", "@broiled", "@grown",
        ]

        grouped = {}
        for item in menu:
            station = item["station"] or "Other"
            grouped.setdefault(station, []).append(item)

        def station_sort_key(station):
            s = station.lower()
            if s in STATION_ORDER:
                return STATION_ORDER.index(s)
            return len(STATION_ORDER)

        for station in sorted(grouped, key=station_sort_key):
            items = grouped[station]
            station_display = station.lstrip("@").title()
            body.append({
                "type": "TextBlock",
                "text": f"**{station_display}**",
                "wrap": True,
                "spacing": "Large",
                "weight": "Bolder",
                "size": "Medium",
            })

            for idx, item in enumerate(items):
                display_name = display_item_name(item["name"], station)

                tags = ""
                if item["dietary"]:
                    tags = " (" + ", ".join(item["dietary"]) + ")"

                full_text = display_name + " " + item.get("description", "")
                nuts = find_allergens(NUT_PATTERN, full_text)
                shellfish = find_allergens(SHELLFISH_PATTERN, full_text)

                main_desc, sides = split_sides(item.get("description", ""))

                title = f"**{display_name}**{tags}"
                served = last_served_label(item["name"], station, history, today_iso)
                if served:
                    title += f" — _{served}_"

                text_items = [
                    {"type": "TextBlock", "text": title, "wrap": True},
                ]
                if nuts:
                    text_items.append(
                        {"type": "TextBlock", "text": "⚠️ Contains " + ", ".join(nuts), "wrap": True,
                         "size": "Small", "color": "Attention", "spacing": "None"},
                    )
                if shellfish:
                    text_items.append(
                        {"type": "TextBlock", "text": "⚠️ Contains " + ", ".join(shellfish), "wrap": True,
                         "size": "Small", "color": "Attention", "spacing": "None"},
                    )
                if main_desc:
                    text_items.append(
                        {"type": "TextBlock", "text": main_desc, "wrap": True,
                         "size": "Small", "isSubtle": True, "spacing": "Small"},
                    )
                if sides:
                    text_items.append(
                        {"type": "TextBlock", "text": f"**Sides:** {sides}", "wrap": True,
                         "size": "Small", "isSubtle": True, "spacing": "None"},
                    )

                columns = []
                if item.get("image_url"):
                    columns.append({
                        "type": "Column",
                        "width": "auto",
                        "spacing": "None",
                        "items": [{
                            "type": "Image",
                            "url": item["image_url"],
                            "width": "80px",
                            "style": "default",
                            "altText": item["name"],
                        }],
                    })
                columns.append({
                    "type": "Column",
                    "width": "stretch",
                    "spacing": "Small",
                    "verticalContentAlignment": "Center",
                    "items": text_items,
                })

                body.append({
                    "type": "ColumnSet",
                    "columns": columns,
                    "spacing": "Small",
                    "separator": idx > 0,
                })

        body.append({
            "type": "TextBlock",
            "text": "_Food photos provided by [Pexels](https://www.pexels.com)_",
            "wrap": True,
            "isSubtle": True,
            "size": "Small",
            "spacing": "Medium",
        })
    else:
        body.append({
            "type": "TextBlock",
            "text": "_No lunch specials posted today. Check the [café website](https://c3ai.cafebonappetit.com/#lunch) directly._",
            "wrap": True,
            "isSubtle": True,
        })

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": body,
                    "msteams": {"width": "Full"},
                },
            }
        ],
    }
    return card


def send_to_teams(card: dict, webhook_url: str) -> bool:
    try:
        resp = requests.post(webhook_url, json=card, timeout=30)
        resp.raise_for_status()
        print("Message sent to Teams successfully.")
        return True
    except Exception as exc:
        print(f"Teams send failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def save_state(weather: dict | None, menu: list[dict] | None) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"weather": weather, "menu": menu}))
    except Exception as exc:
        print(f"Could not save run state: {exc}", file=sys.stderr)


def load_state() -> tuple[dict | None, list[dict] | None]:
    try:
        state = json.loads(STATE_FILE.read_text())
        return state.get("weather"), state.get("menu")
    except Exception:
        return None, None


def main():
    parser = argparse.ArgumentParser(description="Daily walk & lunch Teams update")
    parser.add_argument(
        "--phase",
        choices=["generate", "send", "all"],
        default="all",
        help="generate: fetch data, record menu, render map (no Teams send); "
             "send: build card from saved state and post to Teams; all: both",
    )
    args = parser.parse_args()

    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    weather = None
    menu = None
    history = {}

    if args.phase in ("generate", "all"):
        print("Fetching weather forecast…")
        weather = fetch_weather()

        print("Fetching lunch menu…")
        menu = fetch_menu()

        if menu:
            try:
                history = record_menu(menu, today_iso)
            except Exception as exc:
                print(f"Menu recording failed (continuing): {exc}", file=sys.stderr)
                history = load_history()
            render_menu_map(menu, LAYOUT_DIR / f"{today_iso}.png", history, today_iso)

        save_state(weather, menu)
        if args.phase == "generate":
            print("Generate phase complete.")
            return

    if args.phase == "send":
        weather, menu = load_state()
        if weather is None and menu is None:
            print("No saved state found; fetching fresh data…", file=sys.stderr)
            weather = fetch_weather()
            menu = fetch_menu()
        history = load_history()

    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        print("ERROR: TEAMS_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    map_url = None
    if menu and (LAYOUT_DIR / f"{today_iso}.png").exists():
        map_url = map_image_url(today_iso)

    print("Building Teams message…")
    card = build_adaptive_card(weather, menu, history, map_url)

    print("Sending to Teams…")
    ok = send_to_teams(card, webhook_url)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
