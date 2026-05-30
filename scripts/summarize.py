#!/usr/bin/env python3
"""Aggregate daily irrigation+weather records into weekly summaries and downloadable CSV reports.

Reads:
  data/daily/*.json     — one file per day from fetch_log.py
  zone_config.json      — label overrides and grant zone index

Writes:
  irrigation_summary.json         — consumed by Hugo website template
  reports/all-weeks.csv           — full-season download
  reports/weekly/YYYY-Www.csv     — one per ISO week
"""

import csv
import json
from datetime import date, timedelta
from pathlib import Path

SEASON_START = date(2026, 5, 25)
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


def parse_zone_durations(log_entries: list) -> dict[int, int]:
    """Return {station_index: total_duration_sec} from a day's log entries."""
    totals: dict[int, int] = {}
    for entry in log_entries:
        if len(entry) < 3:
            continue
        sid, dur = int(entry[1]), int(entry[2])
        totals[sid] = totals.get(sid, 0) + dur
    return totals


def load_zone_config() -> dict:
    with open(ZONE_CONFIG_PATH) as f:
        cfg = json.load(f)
    # strip the _note field — it's documentation only
    cfg.pop("_note", None)
    return cfg


def load_daily_records() -> list[dict]:
    records = []
    for path in sorted(DATA_DIR.glob("[0-9]*.json")):
        with open(path) as f:
            records.append(json.load(f))
    return [r for r in records if date.fromisoformat(r["date"]) >= SEASON_START]


def main() -> None:
    config = load_zone_config()
    grant_zone: int = config["grant_zone"]
    label_overrides: dict[str, str] = config.get("labels", {})

    records = load_daily_records()
    print(f"Loaded {len(records)} daily records from {SEASON_START} onward.")

    # Discover all station indices and their controller-reported names
    station_names: dict[int, str] = {}
    for r in records:
        irr = r.get("irrigation")
        if irr:
            for i, name in enumerate(irr.get("station_names", [])):
                if name and i not in station_names:
                    station_names[i] = name

    all_indices = sorted(station_names.keys())

    def zone_label(idx: int) -> str:
        return label_overrides.get(str(idx), station_names.get(idx, f"Zone {idx + 1}"))

    zones = [{"index": i, "label": zone_label(i)} for i in all_indices]

    # Season totals
    season_sec: dict[int, int] = {}
    for r in records:
        irr = r.get("irrigation")
        if not irr:
            continue
        for sid, dur in parse_zone_durations(irr.get("logs", [])).items():
            season_sec[sid] = season_sec.get(sid, 0) + dur

    season_totals = [
        {
            "index": i,
            "label": zone_label(i),
            "duration_min": round(season_sec.get(i, 0) / 60, 1),
            "duration_hhmm": fmt_hhmm(season_sec.get(i, 0) / 60),
        }
        for i in all_indices
    ]

    # Group records by ISO week
    by_week: dict[str, list[dict]] = {}
    for r in records:
        wk = iso_week_label(date.fromisoformat(r["date"]))
        by_week.setdefault(wk, []).append(r)

    # Build weekly summaries and per-week CSVs
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    weekly_summaries = []

    for wk_label, day_records in sorted(by_week.items()):
        first_day = date.fromisoformat(day_records[0]["date"])
        monday, sunday = week_bounds(first_day)
        week_start = max(monday, SEASON_START)
        week_end = sunday

        # Aggregate irrigation by zone
        week_sec: dict[int, int] = {}
        for r in day_records:
            irr = r.get("irrigation")
            if not irr:
                continue
            for sid, dur in parse_zone_durations(irr.get("logs", [])).items():
                week_sec[sid] = week_sec.get(sid, 0) + dur

        # Aggregate weather across days
        precip_total = 0.0
        temps_high: list[float] = []
        temps_low: list[float] = []
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
            "data_complete": not any_missing,
        }

        zone_entries = [
            {
                "index": i,
                "label": zone_label(i),
                "duration_min": round(week_sec.get(i, 0) / 60, 1),
                "duration_hhmm": fmt_hhmm(week_sec.get(i, 0) / 60),
            }
            for i in all_indices
        ]

        weekly_summaries.append({
            "week": wk_label,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "zones": zone_entries,
            "weather": weather_summary,
        })

        # Per-week CSV: one row per day × zone
        csv_path = WEEKLY_DIR / f"{wk_label}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Date", "Zone Index", "Zone Label", "Duration (min)",
                "Precip (in)", "Temp High (°F)", "Temp Low (°F)",
            ])
            for r in sorted(day_records, key=lambda x: x["date"]):
                irr = r.get("irrigation")
                day_dur = parse_zone_durations(irr.get("logs", [])) if irr else {}
                w = r.get("weather") or {}
                for idx in all_indices:
                    dur_min = round(day_dur.get(idx, 0) / 60, 1)
                    writer.writerow([
                        r["date"],
                        idx,
                        zone_label(idx),
                        dur_min,
                        w.get("precip_total_in", ""),
                        w.get("temp_high_f", ""),
                        w.get("temp_low_f", ""),
                    ])

    # All-weeks CSV: one row per week × zone
    all_csv_path = REPORTS_DIR / "all-weeks.csv"
    with open(all_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Week", "Week Start", "Week End", "Zone Index", "Zone Label",
            "Duration (min)", "Precip (in)", "Temp High (°F)", "Temp Low (°F)",
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
                    w.get("precip_total_in", ""),
                    w.get("temp_high_f", ""),
                    w.get("temp_low_f", ""),
                ])

    # Summary JSON for Hugo
    summary = {
        "last_updated": date.today().isoformat(),
        "season_start": SEASON_START.isoformat(),
        "grant_zone": grant_zone,
        "zones": zones,
        "season_totals": season_totals,
        "weekly": weekly_summaries,
    }
    with open(SUMMARY_OUT, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {SUMMARY_OUT}")
    print(f"Wrote {len(weekly_summaries)} weekly CSV(s) to {WEEKLY_DIR}/")
    print(f"Wrote {all_csv_path}")
    if any_missing := any(not wk["weather"]["data_complete"] for wk in weekly_summaries):
        print("NOTE: some weeks have incomplete weather data (marked with * in precip_display).")


if __name__ == "__main__":
    main()
