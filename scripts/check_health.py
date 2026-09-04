#!/usr/bin/env python3
"""Fail (exit 1) if the nightly data acquisition is unhealthy.

Runs as the last workflow step, after data has been committed and deployed, so a
red run means "look at this" without blocking the pipeline. GitHub then sends
its built-in "Run failed" email to the workflow's actor.

Checks:
  1. Yesterday's daily file exists and holds a real irrigation log list and weather.
  2. At least one irrigation run has been logged within the last STALE_DAYS days
     (skipped once SEASON_END has passed — the controller stays on over winter
     with its programs stopped, so no runs is then expected).

Usage: check_health.py [--simulate-failure]   (the flag forces exit 1, to test alerting)
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fetch_log import SEASON_END, SEASON_START

DATA_DIR = Path(__file__).parent.parent / "data" / "daily"
ARIZONA = ZoneInfo("America/Phoenix")
STALE_DAYS = 3


def main() -> None:
    problems: list[str] = []
    if "--simulate-failure" in sys.argv[1:]:
        problems.append("simulated failure requested (--simulate-failure)")

    yesterday = date.today() - timedelta(days=1)
    path = DATA_DIR / f"{yesterday.isoformat()}.json"
    if not path.exists():
        problems.append(f"no daily file for {yesterday}")
    else:
        d = json.loads(path.read_text())
        irr = d.get("irrigation")
        if irr is None:
            problems.append(f"{yesterday}: irrigation fetch failed (null)")
        elif not isinstance(irr.get("logs"), list):
            problems.append(f"{yesterday}: irrigation logs is not a list: {irr.get('logs')!r}")
        if d.get("weather") is None:
            problems.append(f"{yesterday}: weather fetch failed (null)")

    latest: date | None = None
    for p in DATA_DIR.glob("[0-9]*.json"):
        logs = (json.loads(p.read_text()).get("irrigation") or {}).get("logs", [])
        if not isinstance(logs, list):
            continue
        for e in logs:
            if len(e) >= 4 and all(isinstance(x, int) for x in e[:4]):
                run_date = datetime.fromtimestamp(e[3], tz=ARIZONA).date()
                if latest is None or run_date > latest:
                    latest = run_date
    in_season = SEASON_START <= yesterday and (SEASON_END is None or yesterday <= SEASON_END)
    if in_season:
        if latest is None:
            problems.append("no irrigation runs logged at all")
        elif (yesterday - latest).days >= STALE_DAYS:
            problems.append(
                f"no irrigation runs logged since {latest} "
                f"({(yesterday - latest).days} days; programs stopped, controller unreachable, or auth failure?)"
            )
    else:
        print(f"Off season (SEASON_END={SEASON_END}); skipping staleness check.")

    if problems:
        print("DATA HEALTH CHECK FAILED:")
        for msg in problems:
            print(f"  - {msg}")
        sys.exit(1)
    print(f"Data health OK: {yesterday} complete; latest irrigation run {latest}.")


if __name__ == "__main__":
    main()
