#!/usr/bin/env python3
"""Fetch one day of irrigation and weather data from OpenSprinkler OTC and Weather Underground.

Reads environment variables:
  OPENSPRINKLER_OTC_TOKEN      — OpenThings Cloud token
  OPENSPRINKLER_PASSWORD_HASH  — MD5 hash of controller password
  WEATHER_UNDERGROUND_API_KEY  — Weather Underground PWS API key

Writes data/daily/YYYY-MM-DD.json with keys:
  irrigation  — station log from OpenSprinkler, or null if fetch failed
  weather     — daily summary from Weather Underground, or null if fetch failed

Exits with status 1 if either source failed (so CI flags the run), but always
writes whatever data was successfully collected.
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import time

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SEASON_START = date(2026, 5, 25)
ARIZONA = ZoneInfo("America/Phoenix")
WU_STATION = "KAZFLAGS562"
OTC_BASE = "https://cloud.openthings.io/forward/v1/{token}"
WU_HISTORY_URL = "https://api.weather.com/v2/pws/history/daily"

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "daily")


def fetch_irrigation(target: date, token: str, pw_hash: str) -> dict | None:
    # /jl returns a JSON list directly: [[station, program, duration_sec, end_ts], ...]
    # OTC intermittently drops the device connection and returns 404; retry up to 3 times.
    base = OTC_BASE.format(token=token)
    start_dt = datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=ARIZONA)
    end_dt = start_dt + timedelta(days=1) - timedelta(seconds=1)
    params = {
        "pw": pw_hash,
        "start": int(start_dt.timestamp()),
        "end": int(end_dt.timestamp()),
    }

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{base}/jl", params=params, timeout=30)
            r.raise_for_status()
            logs = r.json()
            log.info("OpenSprinkler: fetched %d log entries (attempt %d)", len(logs), attempt)
            return {"logs": logs}
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                log.warning("OpenSprinkler attempt %d failed: %s — retrying in 15s", attempt, exc)
                time.sleep(15)

    log.warning("OpenSprinkler fetch failed after 3 attempts: %s", last_exc)
    return None


def fetch_weather(target: date, api_key: str) -> dict | None:
    try:
        r = requests.get(
            WU_HISTORY_URL,
            params={
                "stationId": WU_STATION,
                "format": "json",
                "units": "e",
                "apiKey": api_key,
                "date": target.strftime("%Y%m%d"),
                "numericPrecision": "decimal",
            },
            timeout=20,
        )
        r.raise_for_status()
        obs_list = r.json().get("observations", [])
        if not obs_list:
            log.warning("Weather Underground returned no observations for %s", target)
            return None
        obs = obs_list[0]
        imperial = obs.get("imperial", {})
        result = {
            "precip_total_in": imperial.get("precipTotal"),
            "temp_high_f": imperial.get("tempHigh"),
            "temp_low_f": imperial.get("tempLow"),
            "temp_avg_f": imperial.get("tempAvg"),
            "humidity_avg_pct": obs.get("humidityAvg"),
        }
        log.info(
            "Weather Underground: precip=%.2f in, high=%s °F, low=%s °F",
            result["precip_total_in"] or 0,
            result["temp_high_f"],
            result["temp_low_f"],
        )
        return result

    except Exception as exc:
        log.warning("Weather Underground fetch failed: %s", exc)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Date to fetch (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing file.")
    args = parser.parse_args()

    target = date.fromisoformat(args.date)
    if target < SEASON_START:
        log.error("Date %s is before season start %s — skipping.", target, SEASON_START)
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, f"{target.isoformat()}.json")

    if os.path.exists(out_path) and not args.force:
        log.info("Already exists: %s (use --force to overwrite)", out_path)
        sys.exit(0)

    token = os.environ["OPENSPRINKLER_OTC_TOKEN"]
    pw_hash = os.environ["OPENSPRINKLER_PASSWORD_HASH"]
    wu_key = os.environ["WEATHER_UNDERGROUND_API_KEY"]

    irrigation = fetch_irrigation(target, token, pw_hash)
    weather = fetch_weather(target, wu_key)

    record = {
        "date": target.isoformat(),
        "irrigation": irrigation,
        "weather": weather,
    }

    with open(out_path, "w") as f:
        json.dump(record, f, indent=2)
    log.info("Wrote %s", out_path)

    if irrigation is None or weather is None:
        failed = [s for s, v in [("irrigation", irrigation), ("weather", weather)] if v is None]
        log.warning("Partial failure — missing: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
