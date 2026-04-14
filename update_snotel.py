#!/usr/bin/env python3
"""
update_snotel.py
================

Drop-in daily updater for the fcrocker-nyc/dirk-adams-honest-cattle-data
repository. Pulls live SNOTEL data from the USDA NRCS Air and Water
Database (AWDB) REST API, aggregates to the ten active honestcattle.net
Montana counties, and writes one JSON file per county at the repo root
using the exact filenames and schema honestcattle.net already expects:

    big_horn.json, blaine.json, carbon.json, gallatin.json,
    lewis_clark.json, meagher.json, park.json, stillwater.json,
    sweet_grass.json, yellowstone.json

Each file matches the schema the honestcattle.net county pages already
read:

    {
      "county": "big_horn",
      "date": "2026-04-14",
      "swe_index": 11.3,
      "percent_of_median": 78,
      "trend": "↓",
      "status": "Below Normal",
      "forage_score": 64
    }

Before this script existed the repo had one stale "auto update" commit
from 2026-04-11 in which every county reported the same 80% / Below
Normal / flat-trend placeholder — this script replaces that with real
county-level aggregated SWE.

Standard library only, so it runs fine in GitHub Actions without any
pip dependencies.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
USER_AGENT = "honestcattle-snotel-updater/1.0 (+https://honestcattle.net)"
REQUEST_TIMEOUT = 30

# Active honestcattle.net Montana counties. Maps the canonical NRCS
# `countyName` to the repo's filename slug (underscores, no "and").
ACTIVE_COUNTIES: dict[str, str] = {
    "Big Horn":        "big_horn",
    "Blaine":          "blaine",
    "Carbon":          "carbon",
    "Gallatin":        "gallatin",
    "Lewis and Clark": "lewis_clark",
    "Meagher":         "meagher",
    "Park":            "park",
    "Stillwater":      "stillwater",
    "Sweet Grass":     "sweet_grass",
    "Yellowstone":     "yellowstone",
}

# Classification thresholds on percent-of-median SWE.
STATUS_THRESHOLDS = [
    (0,   "No Snowpack"),
    (70,  "Below Normal"),
    (110, "Normal"),
    (200, "Above Normal"),
]

TREND_UP, TREND_DOWN, TREND_FLAT = "\u2191", "\u2193", "\u2192"


# ---------------------------------------------------------------------------
# NRCS AWDB REST calls
# ---------------------------------------------------------------------------

def _get(path: str, params: dict) -> object:
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}, doseq=True
    )
    req = urllib.request.Request(
        f"{AWDB_BASE}{path}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_mt_stations() -> list[dict]:
    # NRCS AWDB /stations silently ignores server-side filters, so we
    # request the full list and filter client-side by stateCode + networkCode.
    # Using the raw countyName value — it already matches our county keys
    # (e.g. "Lewis and Clark" with lowercase "and"); .title() would break that.
    data = _get("/stations", {})
    out: list[dict] = []
    for s in data or []:
        if s.get("stateCode") != "MT":
            continue
        if s.get("networkCode") != "SNTL":
            continue
        cn = (s.get("countyName") or "").strip()
        if cn:
            s["countyName"] = cn
        out.append(s)
    return out


def _fetch_data_chunk(triplets: list[str], begin: dt.date, end: dt.date) -> list:
    """Fetch WTEQ + median for a chunk of triplets. On HTTP 500 with >1
    triplet, splits the chunk in half and retries each half so a single
    bad station (e.g. 690:MT:SNTL / Pickfoot Creek) doesn't poison the
    whole batch. Individual stations that still 500 are skipped and logged.
    """
    if not triplets:
        return []
    params = {
        "stationTriplets": ",".join(triplets),
        "elements": "WTEQ",
        "duration": "DAILY",
        "beginDate": begin.isoformat(),
        "endDate": end.isoformat(),
        "centralTendencyType": "MEDIAN",
        "returnFlags": "false",
        "returnOriginalValues": "false",
        "returnSuspectData": "false",
    }
    try:
        return _get("/data", params) or []
    except urllib.error.HTTPError as exc:
        if exc.code != 500:
            raise
        if len(triplets) == 1:
            print(f"[snotel] skip {triplets[0]}: AWDB 500", file=sys.stderr)
            return []
        mid = len(triplets) // 2
        return (_fetch_data_chunk(triplets[:mid], begin, end)
                + _fetch_data_chunk(triplets[mid:], begin, end))


def fetch_swe_series(triplets: list[str], days_back: int) -> dict:
    if not triplets:
        return {}
    end = dt.date.today()
    begin = end - dt.timedelta(days=days_back)
    today_iso = end.isoformat()
    out: dict = {}
    # NRCS /data silently drops stations past ~20 triplets and 500s at ~30+,
    # so we chunk the request into small batches and merge the results.
    BATCH_SIZE = 20
    for i in range(0, len(triplets), BATCH_SIZE):
        chunk = triplets[i : i + BATCH_SIZE]
        payload = _fetch_data_chunk(chunk, begin, end)
        for block in payload or []:
            triplet = block.get("stationTriplet")
            if not triplet:
                continue
            series: list[tuple[str, float]] = []
            latest_date: str | None = None
            latest_median: float | None = None
            for el in block.get("data", []):
                if el.get("stationElement", {}).get("elementCode") != "WTEQ":
                    continue
                for v in el.get("values", []):
                    d = v.get("date")
                    val = v.get("value")
                    if not d or val is None:
                        continue
                    try:
                        series.append((d, float(val)))
                    except (TypeError, ValueError):
                        continue
                    # With centralTendencyType=MEDIAN, NRCS attaches a `median`
                    # key inline on each value item. Capture the one on the
                    # latest-dated value — NRCS data lags a day so "today"
                    # often isn't present yet.
                    if latest_date is None or d > latest_date:
                        latest_date = d
                        m = v.get("median")
                        if m is not None:
                            try:
                                latest_median = float(m)
                            except (TypeError, ValueError):
                                latest_median = None
            series.sort(key=lambda t: t[0])
            out[triplet] = {"series": series, "median_today": latest_median}
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def classify(percent: int | None, swe: float) -> str:
    if swe <= 0.0 or not percent:
        return "No Snowpack"
    for threshold, label in STATUS_THRESHOLDS:
        if percent < threshold:
            return label
    return "Above Normal"


def compute_trend(series: list[float]) -> str:
    valid = [v for v in series if v is not None]
    if len(valid) < 2:
        return TREND_FLAT
    delta = valid[-1] - valid[0]
    if delta > 0.2:
        return TREND_UP
    if delta < -0.2:
        return TREND_DOWN
    return TREND_FLAT


def forage_score(percent: int | None, status: str) -> int:
    if status == "No Snowpack":
        return 40
    p = percent or 0
    if p < 70:
        return int(round(50 + (p / 70.0) * 15))
    if p < 110:
        return int(round(65 + ((p - 70) / 40.0) * 20))
    return min(100, int(round(85 + ((min(p, 200) - 110) / 90.0) * 15)))


def build_record(slug: str, today: dt.date,
                 station_current: list[float],
                 station_median: list[float],
                 station_series: list[list[float]]) -> dict:
    if not station_current:
        return {
            "county": slug,
            "date": today.isoformat(),
            "swe_index": 0.0,
            "percent_of_median": 0,
            "trend": TREND_DOWN if today.month >= 4 else TREND_FLAT,
            "status": "No Snowpack",
            "forage_score": 40,
        }

    swe = round(sum(station_current) / len(station_current), 2)
    meds = [m for m in station_median if m and m > 0]
    percent = int(round((swe / (sum(meds) / len(meds))) * 100)) if meds else 0

    if station_series:
        length = min(len(s) for s in station_series)
        county_series = [
            sum(s[i] for s in station_series) / len(station_series)
            for i in range(length)
        ]
    else:
        county_series = []

    status = classify(percent, swe)
    return {
        "county": slug,
        "date": today.isoformat(),
        "swe_index": swe,
        "percent_of_median": percent,
        "trend": compute_trend(county_series),
        "status": status,
        "forage_score": forage_score(percent, status),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("."),
                        help="Output directory (default: current dir = repo root).")
    parser.add_argument("--trend-days", type=int, default=7,
                        help="Trailing window in days for trend calculation.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    today = dt.date.today()
    args.out.mkdir(parents=True, exist_ok=True)

    try:
        stations = fetch_mt_stations()
    except urllib.error.URLError as exc:
        print(f"[snotel] station fetch failed: {exc}", file=sys.stderr)
        return 2

    if args.verbose:
        print(f"[snotel] discovered {len(stations)} MT SNOTEL stations")

    triplets_by_county: dict[str, list[str]] = {c: [] for c in ACTIVE_COUNTIES}
    for s in stations:
        cn = s.get("countyName") or ""
        triplet = s.get("stationTriplet")
        if cn in ACTIVE_COUNTIES and triplet:
            triplets_by_county[cn].append(triplet)

    all_triplets = [t for v in triplets_by_county.values() for t in v]
    try:
        station_data = fetch_swe_series(all_triplets, days_back=max(args.trend_days, 7))
    except urllib.error.URLError as exc:
        print(f"[snotel] data fetch failed: {exc}", file=sys.stderr)
        return 2

    changed = 0
    for county, slug in ACTIVE_COUNTIES.items():
        current, medians, serieses = [], [], []
        for triplet in triplets_by_county[county]:
            d = station_data.get(triplet, {})
            series = d.get("series", [])
            if not series:
                continue
            vals = [v for _, v in series]
            current.append(vals[-1])
            serieses.append(vals[-args.trend_days:])
            m = d.get("median_today")
            if m is not None:
                medians.append(m)

        record = build_record(slug, today, current, medians, serieses)
        path = args.out / f"{slug}.json"

        # Only rewrite if the content actually changed, so the commit
        # history stays clean on days with no material change.
        new_text = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == new_text:
            if args.verbose:
                print(f"[snotel] {slug}: unchanged")
            continue
        path.write_text(new_text, encoding="utf-8")
        changed += 1
        if args.verbose:
            print(
                f"[snotel] {slug}: swe={record['swe_index']} "
                f"%med={record['percent_of_median']} "
                f"{record['trend']} {record['status']} "
                f"(forage {record['forage_score']}, "
                f"{len(triplets_by_county[county])} stations)"
            )

    print(f"[snotel] {changed} of {len(ACTIVE_COUNTIES)} county files updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
