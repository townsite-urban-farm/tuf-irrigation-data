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
        for entry in irr.get("logs", []):
            if len(entry) < 4:
                continue
            sid, dur, end_ts = int(entry[0]), int(entry[2]), int(entry[3])
            if sid >= 64:  # exclude master/sensor virtual stations (e.g. 99, 254)
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
            entry["gallons"] = round(gpm * dur_min, 1)
            entry["flow_rate_gpm"] = gpm
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
        any_missing = False
        for r in day_records:
            w = r.get("weather")
            if w is None:
                any_missing = True
                continue
            if w.get("precip_total_in") is not None:
                precip_total += w["precip_total_in"]
            if w.get("temp_high_f") is not None:
                temps_high.append(w["temp_high_f"])
            if w.get("temp_low_f") is not None:
                temps_low.append(w["temp_low_f"])
            if w.get("humidity_avg_pct") is not None:
                humidities.append(w["humidity_avg_pct"])

        humidity_avg = round(sum(humidities) / len(humidities), 1) if humidities else None

        weather_summary = {
            "precip_total_in": round(precip_total, 2),
            "precip_display": f"{precip_total:.2f}" if not any_missing else f"{precip_total:.2f}*",
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
                entry["gallons"] = round(gpm * dur_min, 1)
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
                    gpm, _ = zone_flow(idx)
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

    # Summary JSON for Hugo
    summary = {
        "last_updated": date.today().isoformat(),
        "season_start": SEASON_START.isoformat(),
        "grant_zone": grant_zone,
        "default_visible_zones": default_visible_zones,
        "flow_rate_estimated": any_flow_estimated,
        "zones": zones,
        "season_totals": season_totals,
        "weekly": weekly_summaries,
    }
    with open(SUMMARY_OUT, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {SUMMARY_OUT}")
    print(f"Wrote {len(weekly_summaries)} weekly CSV(s) to {WEEKLY_DIR}/")
    print(f"Wrote {all_csv_path}")
    if any(not wk["weather"]["data_complete"] for wk in weekly_summaries):
        print("NOTE: some weeks have incomplete weather data (marked with * in precip_display).")


if __name__ == "__main__":
    main()
