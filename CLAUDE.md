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

## Zone numbering

Station indices in `/jl` log entries are 0-based.
Station 99 and ≥ 64 are virtual/master entries — filtered out by `sid < 64`.
Station 254 has appeared in controller logs (firmware artifact) — also filtered.

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

## Season start

`SEASON_START = date(2026, 5, 25)` in both `fetch_log.py` and `summarize.py`.
Update both if the season boundary changes.

## Workflow

Nightly at 07:00 UTC (= Arizona midnight).
Order: `recover_missing.py` → `fetch_log.py` → `summarize.py` → commit → push to website.
`recover_missing.py` re-fetches any daily file where `irrigation` or `weather` is null.
