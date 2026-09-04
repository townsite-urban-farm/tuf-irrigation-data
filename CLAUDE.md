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
whose fields are string type codes rather than numeric station/duration values
(e.g. `[0, 'rd', 44, end_ts]` for a rain delay).
`summarize.py` skips any entry whose fields don't parse as integers (logged as
"Skipping non-run log entry").

## Controller error responses (`{"result": 2}`) — auth failure since 2026-07-28

Instead of a log list, `/jl` can answer with an OpenSprinkler error object such as
`{"result": 2, "item": ""}`.
Result codes: 1 success, 2 unauthorized (password hash rejected), 3 mismatch, 16 data
missing, 17 out of range, 18 data format error, 32 page not found, 48 not permitted.

Every fetch from the 2026-07-28 file onward stored `{"result": 2, "item": ""}` as
`irrigation.logs` (found 2026-09-04).
The GitHub secrets have not changed since 2026-05-30, so the controller password (or the
MD5 hash stored in `OPENSPRINKLER_PASSWORD_HASH`) is what changed; the OpenSprinkler app
still authenticated on 2026-08-11 (the log export worked), so the app holds the current
password.
Until the hash is fixed, no irrigation runs are being recorded — the last logged run is
2026-08-10 10:20 AZ, and the season/weekly totals flat-line from then on.

This was also the true cause of the 2026-07-29 → 2026-08-11 nightly failures: iterating
the dict yielded the key `"result"`, and `int("r")` raised `ValueError` (misdiagnosed at
the time as a special event record whose first field was `'r'`).
The `sid < 64`/int-parsing skip added 2026-08-11 turned the crash into a silent success.

Current handling (added 2026-09-04):
- `fetch_log.py` treats a non-list `/jl` response as a failure without retrying (it is
  deterministic), so `irrigation` is stored as null and the workflow exits non-zero.
- `recover_missing.py` also re-fetches any daily file whose `irrigation.logs` is not a
  list, so the 2026-07-28 → present files are retried nightly and heal automatically
  once the hash is fixed (subject to the `/jl` rolling window — older runs need the app
  export procedure below).
- `summarize.py` prints one `WARNING: <date>: irrigation logs is not a list` per such file.

To fix: get the current controller password, `echo -n '<password>' | md5` (macOS), and
update the `OPENSPRINKLER_PASSWORD_HASH` secret, then `gh workflow run fetch-and-deploy.yml`.
Then recover 2026-08-11 → fix date via the app export procedure.

The 2026-07-28 → 2026-08-10 runs were recovered on 2026-08-11 — see
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
  (Farm: outdoor) into nectarine/apple (2 GPH each) + pear (8 GPH, since 2026-08-27) +
  beans/corn/pumpkin (58/21/21% of the remainder). These are estimates, not independently
  metered.

### Adding a plant to a line (pattern established 2026-08-27, pear tree)

Append one crop entry to the zone's `crops` list with `fixed_gph` (sum of its drippers'
rated GPH) and `start_date` (planting date, ISO):

```json
{"key": "pear", "label": "Pear", "fixed_gph": 8.0, "emitters": "2 x 4 GPH drippers", "start_date": "2026-08-27"}
```

Semantics: the crop draws nothing before `start_date`; from that date its GPH is **added
on top of the zone's measured `flow_rates` GPM** (the line runs below capacity, so new
drippers do not reduce flow to the existing ones — purely additive).
Crops without `start_date` were already on the line when the flow rate was measured and
are part of that measurement.
`emitters` is documentation only.
Zone gallons, per-crop gallons, and CSVs all follow this from the start date; the website
shows "(since YYYY-MM-DD)" next to such crops in the season-by-crop table, and weekly rows
appear only from the week containing the start date.
Elderberry is planned for September 2026 — same pattern.

If a zone's flow rate is re-measured after additions, fold the additions into the new
`gpm` and remove their `start_date` — but note that this changes gallons for dates before
the re-measurement; a dated `flow_rates` history would be needed to avoid that.

## Per-crop breakdown

`summarize.py` derives a per-crop breakdown from the same deduplicated zone runs (see
`split_crops`). Because the effective flow rate can change on a crop's `start_date`, zone
gallons and crop splits are computed per calendar date (`zone_gpm_on`, `zone_gallons`,
`sum_crop_splits`) and summed, unrounded, into week and season totals.
`flow_rate_gpm` in the outputs is the effective rate on the latest data date.
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
`recover_missing.py` re-fetches any daily file where `irrigation` or `weather` is null
(or where `irrigation.logs` is a controller error object rather than a list), and fetches
any season date with no daily file at all (protects against runs that fail after fetching
but before committing).
For such backfilled dates, weather is fully recoverable (WU history API is date-addressed)
but irrigation is limited to the `/jl` rolling window — older runs are lost unless
recovered from the controller on the LAN, where `/jl` may honor `start`/`end`.

The deploy runs only on the nightly `schedule` and manual `workflow_dispatch` — **not** on
push. To deploy on demand: `gh workflow run fetch-and-deploy.yml`. The website is a Hugo
site on Cloudflare Pages; pushing data to the website repo's `main` triggers its rebuild.
