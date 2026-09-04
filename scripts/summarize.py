#!/usr/bin/env python3
"""Aggregate daily irrigation+weather records into weekly summaries and downloadable CSV reports.

Reads:
  data/daily/*.json     — one file per day from fetch_log.py
  zone_config.json      — label overrides, flow rates, and grant zone index

Writes:
  irrigation_summary.json         — consumed by Hugo website template
  reports/all-weeks.csv           — full-season download
  reports/weekly/YYYY-Www.csv     — one per ISO week

Note on deduplication: the /jl API via OTC ignores start/end time parameters and returns
a rolling window of recent entries. Consecutive daily fetches therefore overlap — the same
run appears in two daily files. We deduplicate by end_ts (unique per physical run) before
aggregating, then assign each run to the correct Arizona-local date via its end_ts.
"""

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEASON_START = date(2026, 5, 25)
ARIZONA = ZoneInfo("America/Phoenix")
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data" / "daily"
REPORTS_DIR = ROOT / "reports"
WEEKLY_DIR = REPORTS_DIR / "weekly"
ZONE_CONFIG_PATH = ROOT / "zone_config.json"
SUMMARY_OUT = ROOT / "irrigation_summary.json"


def fmt_hhmm(minutes: float) -> str:
    total_min = int(round(minutes))
    return f"{total_min // 60}:{total_min % 60:02d}"


def crop_started(crop: dict, on: date) -> bool:
    """True if the crop is on the line on ``on`` (no start_date = since season start)."""
    return "start_date" not in crop or date.fromisoformat(crop["start_date"]) <= on


def added_gph(crops: list[dict], on: date) -> float:
    """Gallons per hour added to a zone's measured flow by crops planted after
    the flow rate was measured (those carrying a ``start_date``)."""
    return sum(
        c["fixed_gph"] for c in crops
        if "fixed_gph" in c and "start_date" in c and crop_started(c, on)
    )


def split_crops(total_sec: int, gpm: float, crops: list[dict], on: date) -> list[dict]:
    """Estimate per-crop gallons for one date's watering time in a zone.

    ``gpm`` is the zone's effective flow rate on that date (measured base plus
    ``added_gph``). Crops with ``fixed_gph`` draw that many gallons per hour of
    run time; the remaining gallons are divided among ``remainder_pct`` crops.
    A crop with a ``start_date`` draws nothing before it. The remainder is
    clamped at zero so a very short run can never produce negative crop gallons.
    Returns unrounded ``duration_sec`` / ``gallons`` so callers can sum across
    dates before rounding.
    """
    hours = total_sec / 3600
    total_gal = gpm * (total_sec / 60)
    active = [c for c in crops if crop_started(c, on)]
    fixed_total = sum(c["fixed_gph"] * hours for c in active if "fixed_gph" in c)
    remainder = max(0.0, total_gal - fixed_total)
    out = []
    for c in crops:
        if c not in active:
            sec, gal = 0, 0.0
        elif "fixed_gph" in c:
            sec, gal = total_sec, c["fixed_gph"] * hours
        else:
            sec, gal = total_sec, remainder * c.get("remainder_pct", 0.0)
        out.append({"key": c["key"], "label": c["label"], "duration_sec": sec, "gallons": gal})
    return out


def crop_entry(crop: dict, total_sec: float, gallons: float) -> dict:
    entry = {
        "key": crop["key"],
        "label": crop["label"],
        "duration_min": round(total_sec / 60, 1),
        "gallons": round(gallons, 1),
    }
    if "start_date" in crop:
        entry["start_date"] = crop["start_date"]
    return entry


def iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def load_zone_config() -> dict:
    with open(ZONE_CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.pop("_note", None)
    return cfg


def load_daily_records() -> list[dict]:
    records = []
    for path in sorted(DATA_DIR.glob("[0-9]*.json")):
        with open(path) as f:
            records.append(json.load(f))
    return [r for r in records if date.fromisoformat(r["date"]) >= SEASON_START]


def collect_all_runs(records: list[dict]) -> list[tuple[int, int, date]]:
    """Return deduplicated (station_idx, duration_sec, az_date) for every physical irrigation run.

    Deduplicates by end_ts: the /jl API returns overlapping windows across consecutive nightly
    fetches, so the same run appears in multiple daily files. Each physical run has a unique
    end_ts, so deduplication by end_ts gives exactly one entry per run. The Arizona-local date
    of end_ts is used to assign the run to the correct calendar day.
    """
    seen: dict[int, tuple[int, int]] = {}  # end_ts → (station, duration)
    for r in records:
        irr = r.get("irrigation")
        if not irr:
            continue
        logs = irr.get("logs", [])
        if not isinstance(logs, list):
            # Controller error object stored by an older fetch_log.py
            # (e.g. {"result": 2} = unauthorized); recover_missing.py re-fetches these.
            print(f"WARNING: {r.get('date')}: irrigation logs is not a list ({logs!r}); no runs")
            continue
        for entry in logs:
            if len(entry) < 4:
                continue
            # /jl record format: [program_id, station_id, duration_sec, end_ts].
            # program_id 99 = manual run, 254 = run-once program — real watering,
            # so entries are never filtered by program.
            try:
                pid, sid, dur, end_ts = (int(x) for x in entry[:4])
            except (TypeError, ValueError):
                # Special event records (rain delay, sensor, water level, ...)
                # carry string type codes instead of numeric fields.
                print(f"Skipping non-run log entry: {entry!r}")
                continue
            if sid >= 64:  # defensive: physical stations only
                continue
            if end_ts not in seen:
                seen[end_ts] = (sid, dur)

    runs = []
    for end_ts, (sid, dur) in seen.items():
        az_date = datetime.fromtimestamp(end_ts, tz=ARIZONA).date()
        runs.append((sid, dur, az_date))
    return sorted(runs, key=lambda x: x[2])


def main() -> None:
    config = load_zone_config()
    grant_zone: int = config["grant_zone"]
    label_overrides: dict[str, str] = config.get("labels", {})
    default_visible_zones: list[int] = config.get("default_visible_zones", [grant_zone])
    flow_rate_cfg: dict[str, dict] = config.get("flow_rates", {})
    crop_splits_cfg: dict[str, dict] = {
        k: v for k, v in config.get("crop_splits", {}).items() if k != "_note"
    }

    records = load_daily_records()
    print(f"Loaded {len(records)} daily records from {SEASON_START} onward.")

    # Enumerate all zones from zone_config.json labels (not discovered from log entries)
    # so zones that haven't run yet still appear in the output.
    all_indices = sorted(int(k) for k in label_overrides.keys())
    all_indices_set = set(all_indices)

    def zone_label(idx: int) -> str:
        return label_overrides.get(str(idx), f"Zone {idx + 1}")

    def zone_flow(idx: int) -> tuple[float, bool]:
        fr = flow_rate_cfg.get(str(idx), {})
        return fr.get("gpm", 0.0), fr.get("estimated", False)

    def zone_crops(idx: int) -> list[dict]:
        return crop_splits_cfg.get(str(idx), {}).get("crops", [])

    def zone_gpm_on(idx: int, on: date) -> float:
        """Effective flow rate on a date: measured base + drippers added since measurement."""
        base, _ = zone_flow(idx)
        return base + added_gph(zone_crops(idx), on) / 60 if base else 0.0

    any_flow_estimated = any(
        flow_rate_cfg.get(str(i), {}).get("estimated", False) for i in all_indices
    )

    zones = [
        {
            "index": i,
            "label": zone_label(i),
            "default_visible": i in default_visible_zones,
        }
        for i in all_indices
    ]

    # Deduplicated runs for all irrigation aggregation
    all_runs = collect_all_runs(records)
    print(f"Found {len(all_runs)} unique irrigation runs (after deduplication).")

    # Pre-build per-date and per-week irrigation totals from deduplicated runs
    season_sec: dict[int, int] = {}
    runs_by_date: dict[date, dict[int, int]] = {}   # az_date → {sid: total_sec}
    runs_by_week: dict[str, dict[int, int]] = {}    # week_label → {sid: total_sec}

    for sid, dur, az_date in all_runs:
        if sid not in all_indices_set or az_date < SEASON_START:
            continue
        season_sec[sid] = season_sec.get(sid, 0) + dur
        runs_by_date.setdefault(az_date, {})
        runs_by_date[az_date][sid] = runs_by_date[az_date].get(sid, 0) + dur
        wk = iso_week_label(az_date)
        runs_by_week.setdefault(wk, {})
        runs_by_week[wk][sid] = runs_by_week[wk].get(sid, 0) + dur

    # Last date the dataset covers, for weather-completeness checks and the
    # "current" flow rate reported alongside totals.
    latest_data_date = max(
        [date.fromisoformat(r["date"]) for r in records] + list(runs_by_date.keys())
    )

    def zone_gallons(idx: int, dates) -> float:
        """Gallons over ``dates``: each day's run time at that day's effective flow rate."""
        return sum(
            zone_gpm_on(idx, d) * runs_by_date.get(d, {}).get(idx, 0) / 60 for d in dates
        )

    def sum_crop_splits(idx: int, dates, crops: list[dict]) -> list[dict]:
        acc = {c["key"]: [0.0, 0.0] for c in crops}
        for d in dates:
            sec = runs_by_date.get(d, {}).get(idx, 0)
            for part in split_crops(sec, zone_gpm_on(idx, d), crops, d):
                acc[part["key"]][0] += part["duration_sec"]
                acc[part["key"]][1] += part["gallons"]
        return [crop_entry(c, *acc[c["key"]]) for c in crops]

    # Season totals
    season_totals = []
    for i in all_indices:
        gpm, estimated = zone_flow(i)
        dur_min = round(season_sec.get(i, 0) / 60, 1)
        entry: dict = {
            "index": i,
            "label": zone_label(i),
            "duration_min": dur_min,
            "duration_hhmm": fmt_hhmm(season_sec.get(i, 0) / 60),
        }
        if gpm:
            entry["gallons"] = round(zone_gallons(i, runs_by_date), 1)
            entry["flow_rate_gpm"] = round(zone_gpm_on(i, latest_data_date), 3)
            entry["flow_rate_estimated"] = estimated
        season_totals.append(entry)

    # Group daily records by ISO week (for weather aggregation)
    by_week: dict[str, list[dict]] = {}
    for r in records:
        wk = iso_week_label(date.fromisoformat(r["date"]))
        by_week.setdefault(wk, []).append(r)

    # Collect all dates with irrigation runs or daily records, grouped by week
    all_dates_by_week: dict[str, set[date]] = {}
    for az_date in runs_by_date:
        wk = iso_week_label(az_date)
        all_dates_by_week.setdefault(wk, set()).add(az_date)
    for r in records:
        d = date.fromisoformat(r["date"])
        wk = iso_week_label(d)
        all_dates_by_week.setdefault(wk, set()).add(d)

    # Map file-date → weather dict for lookups in weekly CSVs
    date_to_weather: dict[date, dict] = {
        date.fromisoformat(r["date"]): r.get("weather") or {}
        for r in records
    }

    # Build weekly summaries and per-week CSVs
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    weekly_summaries = []

    all_week_labels = sorted(set(by_week.keys()) | set(all_dates_by_week.keys()))

    for wk_label in all_week_labels:
        day_records = by_week.get(wk_label, [])

        # Use the first record's date to determine week bounds
        week_dates = sorted(all_dates_by_week.get(wk_label, set()))
        if not week_dates and not day_records:
            continue
        anchor = date.fromisoformat(day_records[0]["date"]) if day_records else week_dates[0]
        monday, sunday = week_bounds(anchor)
        week_start = max(monday, SEASON_START)
        week_end = sunday

        # Irrigation totals from deduplicated runs
        week_sec = runs_by_week.get(wk_label, {})

        # Aggregate weather across the week's daily files
        precip_total = 0.0
        temps_high: list[float] = []
        temps_low: list[float] = []
        humidities: list[float] = []
        days_with_weather: set[date] = set()
        for r in day_records:
            w = r.get("weather")
            if w is None:
                continue
            days_with_weather.add(date.fromisoformat(r["date"]))
            if w.get("precip_total_in") is not None:
                precip_total += w["precip_total_in"]
            if w.get("temp_high_f") is not None:
                temps_high.append(w["temp_high_f"])
            if w.get("temp_low_f") is not None:
                temps_low.append(w["temp_low_f"])
            if w.get("humidity_avg_pct") is not None:
                humidities.append(w["humidity_avg_pct"])

        # Weather is complete only if every day from week_start through the
        # last date the dataset covers (or the week's end) has a weather
        # record — days with no daily file at all count as missing.
        expected_end = min(week_end, latest_data_date)
        n_expected = (expected_end - week_start).days + 1
        any_missing = len(days_with_weather) < n_expected
        has_weather = bool(days_with_weather)

        humidity_avg = round(sum(humidities) / len(humidities), 1) if humidities else None

        weather_summary = {
            "precip_total_in": round(precip_total, 2) if has_weather else None,
            "precip_display": (
                f"{precip_total:.2f}{'*' if any_missing else ''}" if has_weather else ""
            ),
            "temp_high_f": max(temps_high) if temps_high else None,
            "temp_low_f": min(temps_low) if temps_low else None,
            "temp_display": (
                f"{max(temps_high):.0f} / {min(temps_low):.0f}"
                if temps_high and temps_low
                else ""
            ),
            "humidity_avg_pct": humidity_avg,
            "humidity_display": f"{humidity_avg:.0f}" if humidity_avg is not None else "",
            "data_complete": not any_missing,
        }

        zone_entries = []
        for i in all_indices:
            gpm, estimated = zone_flow(i)
            dur_min = round(week_sec.get(i, 0) / 60, 1)
            entry: dict = {
                "index": i,
                "label": zone_label(i),
                "duration_min": dur_min,
                "duration_hhmm": fmt_hhmm(week_sec.get(i, 0) / 60),
            }
            if gpm:
                entry["gallons"] = round(zone_gallons(i, week_dates), 1)
                entry["flow_rate_estimated"] = estimated
            zone_entries.append(entry)

        weekly_summaries.append({
            "week": wk_label,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "zones": zone_entries,
            "weather": weather_summary,
        })

        # Per-week CSV: one row per AZ-date × zone (using deduplicated run dates)
        csv_path = WEEKLY_DIR / f"{wk_label}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Date", "Zone Index", "Zone Label", "Duration (min)", "Gallons (est.)",
                "Precip (in)", "Temp High (°F)", "Temp Low (°F)", "Humidity Avg (%)",
            ])
            for target_date in week_dates:
                day_dur = runs_by_date.get(target_date, {})
                w = date_to_weather.get(target_date, {})
                for idx in all_indices:
                    gpm = zone_gpm_on(idx, target_date)
                    dur_min = round(day_dur.get(idx, 0) / 60, 1)
                    gallons = round(gpm * dur_min, 1) if gpm else ""
                    writer.writerow([
                        target_date.isoformat(),
                        idx,
                        zone_label(idx),
                        dur_min,
                        gallons,
                        w.get("precip_total_in", ""),
                        w.get("temp_high_f", ""),
                        w.get("temp_low_f", ""),
                        w.get("humidity_avg_pct", ""),
                    ])

        # Per-week crop CSV: one row per AZ-date × crop for each split zone
        if crop_splits_cfg:
            crop_csv_path = WEEKLY_DIR / f"{wk_label}-crops.csv"
            with open(crop_csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Date", "Zone Index", "Zone Label", "Crop", "Duration (min)",
                    "Gallons (est.)", "Precip (in)", "Temp High (°F)", "Temp Low (°F)",
                    "Humidity Avg (%)",
                ])
                for target_date in week_dates:
                    day_dur = runs_by_date.get(target_date, {})
                    w = date_to_weather.get(target_date, {})
                    for zi_str in crop_splits_cfg:
                        zi = int(zi_str)
                        crops = [c for c in zone_crops(zi) if crop_started(c, target_date)]
                        for crop in sum_crop_splits(zi, [target_date], crops):
                            writer.writerow([
                                target_date.isoformat(),
                                zi,
                                zone_label(zi),
                                crop["label"],
                                crop["duration_min"],
                                crop["gallons"],
                                w.get("precip_total_in", ""),
                                w.get("temp_high_f", ""),
                                w.get("temp_low_f", ""),
                                w.get("humidity_avg_pct", ""),
                            ])

    # All-weeks CSV: one row per week × zone
    all_csv_path = REPORTS_DIR / "all-weeks.csv"
    with open(all_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Week", "Week Start", "Week End", "Zone Index", "Zone Label",
            "Duration (min)", "Gallons (est.)", "Precip (in)",
            "Temp High (°F)", "Temp Low (°F)", "Humidity Avg (%)",
        ])
        for wk in weekly_summaries:
            for ze in wk["zones"]:
                w = wk["weather"]
                writer.writerow([
                    wk["week"],
                    wk["week_start"],
                    wk["week_end"],
                    ze["index"],
                    ze["label"],
                    ze["duration_min"],
                    ze.get("gallons", ""),
                    w.get("precip_total_in", ""),
                    w.get("temp_high_f", ""),
                    w.get("temp_low_f", ""),
                    w.get("humidity_avg_pct", ""),
                ])

    # Per-crop breakdown (estimated grant sub-metering) for zones in crop_splits.
    # Derived from the same deduplicated runs as the zone totals.
    crop_breakdown = []
    for zi_str in crop_splits_cfg:
        zi = int(zi_str)
        _, estimated = zone_flow(zi)
        crops = zone_crops(zi)
        weekly_crops = []
        for wk in weekly_summaries:
            week_end = date.fromisoformat(wk["week_end"])
            week_dates = sorted(all_dates_by_week.get(wk["week"], set()))
            weekly_crops.append({
                "week": wk["week"],
                "week_start": wk["week_start"],
                "week_end": wk["week_end"],
                "crops": sum_crop_splits(
                    zi, week_dates, [c for c in crops if crop_started(c, week_end)]
                ),
            })
        crop_breakdown.append({
            "zone_index": zi,
            "zone_label": zone_label(zi),
            "flow_rate_gpm": round(zone_gpm_on(zi, latest_data_date), 3),
            "flow_rate_estimated": estimated,
            "estimated": True,
            "season": sum_crop_splits(zi, runs_by_date, crops),
            "weekly": weekly_crops,
        })

    # All-weeks crop CSV: one row per week × crop for each split zone
    if crop_splits_cfg:
        crops_all_csv_path = REPORTS_DIR / "crops-all-weeks.csv"
        with open(crops_all_csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Week", "Week Start", "Week End", "Zone Index", "Zone Label", "Crop",
                "Duration (min)", "Gallons (est.)", "Precip (in)",
                "Temp High (°F)", "Temp Low (°F)", "Humidity Avg (%)",
            ])
            week_weather = {wk["week"]: wk["weather"] for wk in weekly_summaries}
            for zb in crop_breakdown:
                for wk in zb["weekly"]:
                    w = week_weather.get(wk["week"], {})
                    for crop in wk["crops"]:
                        writer.writerow([
                            wk["week"],
                            wk["week_start"],
                            wk["week_end"],
                            zb["zone_index"],
                            zb["zone_label"],
                            crop["label"],
                            crop["duration_min"],
                            crop["gallons"],
                            w.get("precip_total_in", ""),
                            w.get("temp_high_f", ""),
                            w.get("temp_low_f", ""),
                            w.get("humidity_avg_pct", ""),
                        ])

    # Summary JSON for Hugo
    summary = {
        "last_updated": datetime.now(ZoneInfo("America/Phoenix")).strftime("%Y-%m-%d %H:%M MST"),
        "season_start": SEASON_START.isoformat(),
        "grant_zone": grant_zone,
        "default_visible_zones": default_visible_zones,
        "flow_rate_estimated": any_flow_estimated,
        "zones": zones,
        "season_totals": season_totals,
        "weekly": weekly_summaries,
        "crop_breakdown": crop_breakdown,
    }
    with open(SUMMARY_OUT, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {SUMMARY_OUT}")
    print(f"Wrote {len(weekly_summaries)} weekly CSV(s) to {WEEKLY_DIR}/")
    print(f"Wrote {all_csv_path}")
    if crop_splits_cfg:
        print(f"Wrote per-crop breakdown for zone(s) {sorted(int(k) for k in crop_splits_cfg)} "
              f"and {REPORTS_DIR / 'crops-all-weeks.csv'}")
    if any(not wk["weather"]["data_complete"] for wk in weekly_summaries):
        print("NOTE: some weeks have incomplete weather data (marked with * in precip_display).")


if __name__ == "__main__":
    main()
