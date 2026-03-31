"""
Daily Walk & Lunch Notifier for Microsoft Teams.

Fetches the noon weather forecast and today's cafeteria lunch specials,
then posts a formatted Adaptive Card to a Teams channel via webhook.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

LATITUDE = 37.5136
LONGITUDE = -122.2006
LOCATION_LABEL = "1400 Seaport Blvd, Redwood City"

CAFE_URL = "https://c3ai.cafebonappetit.com/"

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


def _extract_noon_weather(times, temps, precips, winds, codes, date_str):
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
    max_precip = max(precips[i] for i in indices)
    avg_wind = sum(winds[i] for i in indices) / len(indices)
    worst_code = max(codes[i] for i in indices)

    return {
        "temp_f": round(avg_temp),
        "precip_pct": max_precip,
        "wind_mph": round(avg_wind),
        "condition": WMO_CODES.get(worst_code, "Unknown"),
        "weather_code": worst_code,
        "date": date_str,
    }


def fetch_weather() -> dict | None:
    """Return a dict with today's and tomorrow's noon-hour weather data."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,weather_code",
                "forecast_days": 2,
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "America/Los_Angeles",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"Weather fetch failed: {exc}", file=sys.stderr)
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precips = hourly.get("precipitation_probability", [])
    winds = hourly.get("wind_speed_10m", [])
    codes = hourly.get("weather_code", [])

    now = datetime.now(PT)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    today = _extract_noon_weather(times, temps, precips, winds, codes, today_str)
    tomorrow = _extract_noon_weather(times, temps, precips, winds, codes, tomorrow_str)

    if not today:
        print("No noon data found for today in weather response", file=sys.stderr)
        return None

    return {"today": today, "tomorrow": tomorrow}


def walk_score(w: dict) -> int:
    """Return 0-100 score for how pleasant the walk will be."""
    score = 100

    # Temperature: ideal 60-75°F, penalize distance from that range
    temp = w["temp_f"]
    if temp < 60:
        score -= min(int((60 - temp) * 1.5), 40)
    elif temp > 75:
        score -= min(int((temp - 75) * 1.5), 40)

    # Precipitation probability — very aggressive scaling
    #   Any chance at all gets a steep penalty:
    #   5% → -10,  10% → -15,  20% → -28,  40% → -48,  60% → -68,  80% → -88,  100% → -100
    precip = w["precip_pct"]
    if precip > 0:
        score -= max(10, int(precip * 1.1))

    # Wind: comfortable under 10 mph, unpleasant above 20
    wind = w["wind_mph"]
    if wind > 10:
        score -= min(int((wind - 10) * 2), 30)

    # Weather code penalties (on top of precip penalty)
    code = w["weather_code"]
    if code in RAINY_CODES:
        score -= 15
    elif code in {45, 48}:  # fog
        score -= 10
    elif code in {71, 73, 75, 77, 85, 86}:  # snow
        score -= 30

    return max(0, min(100, score))


def walk_recommendation(w: dict) -> str:
    score = walk_score(w)
    code = w["weather_code"]
    precip = w["precip_pct"]
    temp = w["temp_f"]
    wind = w["wind_mph"]

    if code in RAINY_CODES or precip >= 60:
        return "Bring an umbrella — rain is likely during your walk."
    if temp < 50:
        return "Bundle up — it's chilly out there."
    if wind >= 20:
        return "It's windy today — heads up on the walk."
    if temp > 90:
        return "It's hot — maybe stick to the shady route."
    if score >= 80:
        return "Great day for a walk!"
    if score >= 50:
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


def search_food_image(name: str, description: str = "") -> str | None:
    """Search Pexels for a food photo and return a medium thumbnail URL."""
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None

    query = _build_image_query(name, description)

    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 1, "orientation": "square"},
            timeout=10,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            return photos[0]["src"]["medium"]
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
# Teams message
# ---------------------------------------------------------------------------

def build_adaptive_card(weather: dict | None, menu: list[dict] | None) -> dict:
    today_str = datetime.now(PT).strftime("%A, %B %-d")

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
            "text": f"Walk Forecast — {today_score}/100",
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
            tmrw_score_str = f"{walk_score(tomorrow_w)}/100"

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
            _weather_row("Walk Score", f"{today_score}/100",
                         tmrw_score_str),
            _weather_row("Condition", today_w["condition"],
                         tomorrow_w["condition"] if tomorrow_w else ""),
            _weather_row("Temperature", f"{today_w['temp_f']}°F",
                         f"{tomorrow_w['temp_f']}°F" if tomorrow_w else ""),
            _weather_row("Precip. chance", f"{today_w['precip_pct']}%",
                         f"{tomorrow_w['precip_pct']}%" if tomorrow_w else ""),
            _weather_row("Wind", f"{today_w['wind_mph']} mph",
                         f"{tomorrow_w['wind_mph']} mph" if tomorrow_w else ""),
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
                display_name = item["name"]
                if station.lower() == "@melted":
                    display_name = f"{display_name} Pizza"

                tags = ""
                if item["dietary"]:
                    tags = " (" + ", ".join(item["dietary"]) + ")"

                text_items = [
                    {"type": "TextBlock", "text": f"**{display_name}**{tags}", "wrap": True},
                ]
                if item["description"]:
                    text_items.append(
                        {"type": "TextBlock", "text": item["description"], "wrap": True,
                         "size": "Small", "isSubtle": True, "spacing": "Small"},
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

def main():
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        print("ERROR: TEAMS_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print("Fetching weather forecast…")
    weather = fetch_weather()

    print("Fetching lunch menu…")
    menu = fetch_menu()

    print("Building Teams message…")
    card = build_adaptive_card(weather, menu)

    print("Sending to Teams…")
    ok = send_to_teams(card, webhook_url)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
