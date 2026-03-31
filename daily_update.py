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

def fetch_weather() -> dict | None:
    """Return a dict with noon-hour weather data, or None on failure."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,weather_code",
                "forecast_days": 1,
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

    noon_indices = [i for i, t in enumerate(times) if t.endswith(("T12:00", "T13:00"))]
    if not noon_indices:
        print("No noon data found in weather response", file=sys.stderr)
        return None

    avg_temp = sum(temps[i] for i in noon_indices) / len(noon_indices)
    max_precip = max(precips[i] for i in noon_indices)
    avg_wind = sum(winds[i] for i in noon_indices) / len(noon_indices)
    worst_code = max(codes[i] for i in noon_indices)

    condition = WMO_CODES.get(worst_code, "Unknown")

    return {
        "temp_f": round(avg_temp),
        "precip_pct": max_precip,
        "wind_mph": round(avg_wind),
        "condition": condition,
        "weather_code": worst_code,
    }


def walk_recommendation(weather: dict) -> str:
    code = weather["weather_code"]
    temp = weather["temp_f"]
    precip = weather["precip_pct"]
    wind = weather["wind_mph"]

    if code in RAINY_CODES or precip >= 60:
        return "Bring an umbrella — rain is likely during your walk."
    if temp < 50:
        return "Bundle up — it's chilly out there."
    if wind >= 20:
        return "It's windy today — heads up on the walk."
    if temp > 90:
        return "It's hot — maybe stick to the shady route."
    return "Great day for a walk!"


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
        })

    return items if items else None


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
            "text": f"Walk & Lunch — {today_str}",
        }
    ]

    # --- Weather section ---
    if weather:
        rec = walk_recommendation(weather)
        body.append({
            "type": "TextBlock",
            "text": f"**{rec}**",
            "wrap": True,
            "spacing": "Small",
        })

        weather_facts = [
            {"title": "Condition", "value": weather["condition"]},
            {"title": "Temperature", "value": f"{weather['temp_f']}°F"},
            {"title": "Precip. chance", "value": f"{weather['precip_pct']}%"},
            {"title": "Wind", "value": f"{weather['wind_mph']} mph"},
        ]

        body.append({
            "type": "FactSet",
            "facts": weather_facts,
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
        "size": "Medium",
        "weight": "Bolder",
        "text": "Today's Lunch Specials",
        "spacing": "Medium",
    })

    if menu:
        for item in menu:
            tags = ""
            if item["dietary"]:
                tags = "  (" + ", ".join(item["dietary"]) + ")"

            station_label = f"  *{item['station']}*" if item["station"] else ""

            lines = [f"**{item['name']}**{tags}{station_label}"]
            if item["description"]:
                lines.append(item["description"])

            body.append({
                "type": "TextBlock",
                "text": "\n\n".join(lines),
                "wrap": True,
                "spacing": "Small",
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
                    "version": "1.4",
                    "body": body,
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
