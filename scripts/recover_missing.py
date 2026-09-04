#!/usr/bin/env python3
"""Re-fetch any daily files where irrigation or weather data is null,
and fetch any season days that have no daily file at all (e.g. days lost
when a later workflow step failed before the commit step).

Called by the nightly workflow before fetch_log.py so that transient API
failures from previous runs are retried automatically.

Note: weather is fully recoverable for past dates (the WU history API is
date-addressed), but OpenSprinkler /jl via OTC only returns a rolling
window of recent entries, so irrigation runs older than that window
cannot be recovered here.
"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from fetch_log import SEASON_START

DATA_DIR = Path(__file__).parent.parent / "data" / "daily"
FETCH_SCRIPT = Path(__file__).parent / "fetch_log.py"


def fetch(day: str, reason: str) -> bool:
    print(f"Fetching {day} ({reason})")
    result = subprocess.run(
        [sys.executable, str(FETCH_SCRIPT), "--date", day, "--force"],
        check=False,
    )
    if result.returncode != 0:
        print(f"  WARNING: still incomplete after fetch for {day}")
    return result.returncode == 0


def main() -> None:
    recovered = 0
    existing = set()
    for path in sorted(DATA_DIR.glob("[0-9]*.json")):
        with open(path) as f:
            d = json.load(f)
        existing.add(d["date"])
        missing = [k for k in ("irrigation", "weather") if d.get(k) is None]
        irr = d.get("irrigation")
        if irr is not None and not isinstance(irr.get("logs"), list):
            # Older fetches stored the controller's error object (e.g. {"result": 2},
            # unauthorized) as if it were a log list; re-fetch those days too.
            missing.append("irrigation (error response)")
        if missing:
            recovered += fetch(d["date"], f"missing: {', '.join(missing)}")

    day = SEASON_START
    yesterday = date.today() - timedelta(days=1)
    while day <= yesterday:
        if day.isoformat() not in existing:
            recovered += fetch(day.isoformat(), "no daily file")
        day += timedelta(days=1)

    if recovered:
        print(f"Recovered data for {recovered} day(s).")
    else:
        print("No missing-data files found.")


if __name__ == "__main__":
    main()
