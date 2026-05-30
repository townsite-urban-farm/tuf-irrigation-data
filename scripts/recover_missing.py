#!/usr/bin/env python3
"""Re-fetch any daily files where irrigation or weather data is null.

Called by the nightly workflow before fetch_log.py so that transient API
failures from previous runs are retried automatically.
"""

import json
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "daily"
FETCH_SCRIPT = Path(__file__).parent / "fetch_log.py"


def main() -> None:
    recovered = 0
    for path in sorted(DATA_DIR.glob("[0-9]*.json")):
        with open(path) as f:
            d = json.load(f)
        missing = [k for k in ("irrigation", "weather") if d.get(k) is None]
        if not missing:
            continue
        print(f"Re-fetching {d['date']} (missing: {', '.join(missing)})")
        result = subprocess.run(
            [sys.executable, str(FETCH_SCRIPT), "--date", d["date"], "--force"],
            check=False,
        )
        if result.returncode == 0:
            recovered += 1
        else:
            print(f"  WARNING: still incomplete after re-fetch for {d['date']}")
    if recovered:
        print(f"Recovered data for {recovered} day(s).")
    else:
        print("No missing-data files found.")


if __name__ == "__main__":
    main()
