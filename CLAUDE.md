# tuf-irrigation-data — Claude Notes

Automated daily irrigation + weather logging for Townsite Urban Farm.
Data is fetched nightly via GitHub Actions and cross-pushed to the website repo.
See README for full architecture.

## Critical API quirk: /jl returns overlapping windows

The OpenSprinkler `/jl` log endpoint via OTC (OpenThings Cloud) **ignores the `start`/`end`
time-window parameters** and returns a rolling window of recent entries regardless of the
requested range.
Consecutive nightly fetches therefore overlap — the same physical run appears in two
consecutive daily files.

`summarize.py` handles this by **deduplicating all log entries by `end_ts`** before
aggregating.
Each physical run has a unique `end_ts`; dedup gives exactly one entry per run.
Runs are then assigned to the correct Arizona-local calendar date via their `end_ts`.

Do not remove or weaken this deduplication — without it, all totals are inflated ~2×.

## Log entry format (corrected 2026-08-11)

`/jl` run records are `[program_id, station_id, duration_sec, end_ts]` — program FIRST,
matching the official OpenSprinkler API docs.
Until 2026-08-11 `summarize.py` read `entry[0]` as the station, which attributed water to
programs instead of zones and silently dropped all manual runs (program 99) and run-once
runs (program 254) via the `sid < 64` filter.
Evidence for the corrected order: program IDs 99/254 are documented special values, station
values are exactly 0–3, and consecutive entries sharing `entry[0]` are spaced exactly by
their durations (a program running its stations sequentially).
Manual (99) and run-once (254) programs are real watering and are included in totals.
Station indices are 0-based; `sid < 64` is kept as a defensive filter only.

## Special event log entries

`/jl` occasionally returns special event records (rain delay, sensor, water level, ...)
whose fields are string type codes rather than numeric station/duration values.
One such entry (first field `'r'`) appeared in every nightly fetch window from
2026-07-28 onward and crashed `summarize.py` (`ValueError` on `int(entry[0])`), so the
workflow failed every night 2026-07-29 → 2026-08-11 and no data was committed for
2026-07-28 → 2026-08-10.
`summarize.py` now skips any entry whose fields don't parse as integers (logged as
"Skipping non-run log entry").

The lost irrigation runs for that window were recovered on 2026-08-11 — see
"Recovering lost irrigation logs" below.

Zone labels come from `zone_config.json` because `/jn` (station names) returns 404
on this firmware via OTC.

## zone_config.json

- `grant_zone`: index of the zone required for grant reporting (currently 3, Farm: outdoor)
- `default_visible_zones`: zones shown checked by default on the website [2, 3]
- `flow_rates`: estimated GPM per zone; `"estimated": true` until validated by water meter
  — Farm zones use drip tape; landscaping zones use spray heads (estimates only)
- `crop_splits`: optional per-zone sub-metering estimate for grant reporting, keyed by
  zone index. Each zone lists ordered `crops`; a crop has either `fixed_gph` (gallons per
  hour of the zone's run time) or `remainder_pct` (share of the gallons left after the
  fixed-rate crops). `remainder_pct` values must sum to 1.0. Currently splits zone 3
  (Farm: outdoor) into nectarine/apple (2 GPH each) + beans/corn/pumpkin (58/21/21% of
  the remainder). These are estimates, not independently metered.

## Per-crop breakdown

`summarize.py` derives a per-crop breakdown from the same deduplicated zone runs (see
`split_crops`). The split is linear in watering time, so it is applied to aggregated
seconds at each level (season, week, per-day CSV rows) identically to splitting each run.
Outputs: a `crop_breakdown` section in `irrigation_summary.json`, `reports/crops-all-weeks.csv`,
and `reports/weekly/YYYY-Www-crops.csv`. Zone totals are left unchanged. Because the tree
crops are a fixed GPH draw and the row crops absorb the remainder, the split stays correct
if a zone's measured flow rate is later revised.

The deploy step copies these to the website: `crops-all-weeks.csv` has its own `cp` line,
and the per-week `*-crops.csv` files transfer via the existing `weekly/*.csv` glob. The
website template `layouts/water-systems/water-usage.html` (in the `website` repo) renders
`crop_breakdown` as weekly + season tables with CSV download links for the grant program team.

## Recovering lost irrigation logs (procedure used 2026-08-11)

If the workflow fails after fetching but before committing, daily files are never written and that night's data is gone from the runner.
`recover_missing.py` backfills such dates automatically on the next successful run, but only weather is fully recoverable that way — the WU history API is date-addressed, while `/jl` via OTC serves only a short rolling window of recent runs.

To recover older irrigation runs:

1. Open the OpenSprinkler web UI (works remotely via OTC) and use the app's log export for a date range covering the gap plus a few days of padding on each side.
   The export is the same raw `/jl` JSON array, `[[program, station, duration_sec, end_ts], ...]`; overlap with existing data is harmless because of dedup-by-end_ts.
2. Merge the exported entries into an existing daily file that has COMPLETE weather.
   Never use a file `recover_missing.py` might `--force` re-fetch (any file with null irrigation or weather) — the re-fetch would overwrite the merged logs with the short rolling window.
   Which file holds an entry does not matter: `summarize.py` pools log entries from all daily files and assigns each run to its calendar date via `end_ts`.
   Merge means appending only entries whose `end_ts` is not already in the file:

   ```python
   import json
   p = "data/daily/YYYY-MM-DD.json"   # a file with complete weather
   d = json.load(open(p))
   export = json.load(open("export.json"))
   have = {e[3] for e in d["irrigation"]["logs"] if len(e) >= 4}
   d["irrigation"]["logs"] += [
       e for e in export
       if len(e) >= 4 and isinstance(e[3], int) and e[3] not in have
   ]
   json.dump(d, open(p, "w"), indent=2)
   ```

3. Run `python3 scripts/summarize.py` and commit the daily file together with the regenerated reports and `irrigation_summary.json`.

The 2026-07-28 → 2026-08-10 gap was recovered this way into `data/daily/2026-07-27.json` (chosen because it was the newest file with complete weather).

## Season start

`SEASON_START = date(2026, 5, 25)` in both `fetch_log.py` and `summarize.py`.
Update both if the season boundary changes.

## Workflow

Nightly at 07:00 UTC (= Arizona midnight).
Order: `recover_missing.py` → `fetch_log.py` → `summarize.py` → commit → push to website.
`recover_missing.py` re-fetches any daily file where `irrigation` or `weather` is null,
and fetches any season date with no daily file at all (protects against runs that fail
after fetching but before committing).
For such backfilled dates, weather is fully recoverable (WU history API is date-addressed)
but irrigation is limited to the `/jl` rolling window — older runs are lost unless
recovered from the controller on the LAN, where `/jl` may honor `start`/`end`.

The deploy runs only on the nightly `schedule` and manual `workflow_dispatch` — **not** on
push. To deploy on demand: `gh workflow run fetch-and-deploy.yml`. The website is a Hugo
site on Cloudflare Pages; pushing data to the website repo's `main` triggers its rebuild.
