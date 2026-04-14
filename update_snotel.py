#!/usr/bin/env python3
"""
update_snotel.py
================

Drop-in daily updater for the fcrocker-nyc/dirk-adams-honest-cattle-data
repository. Pulls live SNOTEL data from the USDA NRCS Air and Water
Database (AWDB) REST API plus USDA Drought Monitor classifications,
aggregates to the ten active honestcattle.net Montana counties, and
writes one JSON file per county at the repo root.

Output schema (v1.1 — precip_ytd + drought added, all prior fields unchanged):

    {
      "county": "gallatin",
      "date": "2026-04-14",
      "swe_index": 10.91,
      "percent_of_median": 55,
      "trend": "↓",
      "status": "Below Normal",
      "forage_score": 62,
      "precip_ytd": {
        "inches": 14.2,
        "percent_of_median": 92,
        "status": "Normal"
      },
      "drought": {
        "valid_end": "2026-04-06",
        "none_pct": 0.0,
        "d0_pct": 100.0,
        "d1_pct": 100.0,
        "d2_pct": 77.98,
        "d3_pct": 0.0,
        "d4_pct": 0.0,
        "worst_class": "D2"
      }
    }

The `precip_ytd` block is null for counties with no MT SNOTEL stations
(Big Horn, Blaine, Stillwater, Yellowstone). The `drought` block comes
from USDM and is populated for all 10 counties regardless of SNOTEL
coverage. Both are additive — consumers that only read the original
six fields are unaffected.

Notes on USDM semantics: the d0..d4 percentages are **cumulative** —
`d2_pct` is the percent of the county in D2 or worse, not just "in
exactly D2". This matches the common USDM phrasing ("D2 Severe Drought
across 78% of the county") used in the county page prose. `worst_class`
is a convenience field: the highest drought bucket with any area.

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
USDM_BASE = "https://usdmdataservices.unl.edu/api"
USER_AGENT = "honestcattle-snotel-updater/1.1 (+https://honestcattle.net)"
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

# USDM API uses federal county FIPS codes. MT state prefix is 30.
# Source: US Census Bureau FIPS county codes for Montana.
COUNTY_FIPS: dict[str, str] = {
    "big_horn":    "30003",
    "blaine":      "30005",
    "carbon":      "30009",
    "gallatin":    "30031",
    "lewis_clark": "30049",
    "meagher":     "30059",
    "park":        "30067",
    "stillwater":  "30095",
    "sweet_grass": "30097",
    "yellowstone": "30111",
}

# Classification thresholds on percent-of-median SWE.
# NOTE: thresholds are unreviewed assumptions. See SOURCES.md §5.
STATUS_THRESHOLDS = [
    (0,   "No Snowpack"),
    (70,  "Below Normal"),
    (110, "Normal"),
    (200, "Above Normal"),
]

# Same percent-of-median thresholds applied to water-year precipitation,
# minus "No Snowpack" which doesn't apply to precip. See SOURCES.md §9.
PRECIP_STATUS_THRESHOLDS = [
    (70,  "Below Normal"),
    (110, "Normal"),
    (200, "Above Normal"),
]

TREND_UP, TREND_DOWN, TREND_FLAT = "\u2191", "\u2193", "\u2192"

# Elements requested from AWDB /data in a single call:
#   WTEQ = snow water equivalent (inches)
#   PREC = water-year precipitation accumulation (inches, resets Oct 1)
ELEMENTS_REQUESTED = "WTEQ,PREC"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> object:
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# NRCS AWDB
# ---------------------------------------------------------------------------

def fetch_mt_stations() -> list[dict]:
    # NRCS AWDB /stations silently ignores server-side filters, so we
    # pull the full list and filter client-side by stateCode + networkCode.
    # Using the raw countyName value — it already matches our county keys
    # (e.g. "Lewis and Clark" with lowercase "and"); .title() would break that.
    data = _get(f"{AWDB_BASE}/stations")
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
    """Fetch WTEQ + PREC + median for a chunk of triplets. On HTTP 500
    with >1 triplet, splits the chunk in half and retries each half so
    a single bad station (e.g. 690:MT:SNTL / Pickfoot Creek) doesn't
    poison the batch. Individual stations that still 500 are skipped.
    """
    if not triplets:
        return []
    params = {
        "stationTriplets": ",".join(triplets),
        "elements": ELEMENTS_REQUESTED,
        "duration": "DAILY",
        "beginDate": begin.isoformat(),
        "endDate": end.isoformat(),
        "centralTendencyType": "MEDIAN",
        "returnFlags": "false",
        "returnOriginalValues": "false",
        "returnSuspectData": "false",
    }
    try:
        return _get(f"{AWDB_BASE}/data", params) or []
    except urllib.error.HTTPError as exc:
        if exc.code != 500:
            raise
        if len(triplets) == 1:
            print(f"[snotel] skip {triplets[0]}: AWDB 500", file=sys.stderr)
            return []
        mid = len(triplets) // 2
        return (_fetch_data_chunk(triplets[:mid], begin, end)
                + _fetch_data_chunk(triplets[mid:], begin, end))


def fetch_station_data(triplets: list[str], days_back: int) -> dict:
    """Fetch WTEQ and PREC per station triplet in a single multi-element
    /data call, chunked into batches of 20.

    Returns a dict keyed by station triplet:

        {
            "360:MT:SNTL": {
                "swe_series":   [("2026-04-08", 9.1), ..., ("2026-04-14", 8.7)],
                "swe_median":   11.4,
                "prec_current": 24.2,
                "prec_median":  21.4,
            },
            ...
        }

    All values in inches. Medians are read inline from the latest-dated
    value (NRCS data lags a day, so "today" often isn't present yet).
    """
    if not triplets:
        return {}
    end = dt.date.today()
    begin = end - dt.timedelta(days=days_back)

    out: dict = {}
    BATCH_SIZE = 20
    for i in range(0, len(triplets), BATCH_SIZE):
        chunk = triplets[i : i + BATCH_SIZE]
        payload = _fetch_data_chunk(chunk, begin, end)
        for block in payload:
            triplet = block.get("stationTriplet")
            if not triplet:
                continue
            row: dict = {
                "swe_series": [],
                "swe_median": None,
                "prec_current": None,
                "prec_median": None,
            }
            for el in block.get("data", []):
                se = el.get("stationElement", {}) or {}
                code = se.get("elementCode")
                values = el.get("values", []) or []
                if code == "WTEQ":
                    latest_date = None
                    for v in values:
                        d = v.get("date")
                        val = v.get("value")
                        if not d or val is None:
                            continue
                        try:
                            row["swe_series"].append((d, float(val)))
                        except (TypeError, ValueError):
                            continue
                        if latest_date is None or d > latest_date:
                            latest_date = d
                            m = v.get("median")
                            if m is not None:
                                try:
                                    row["swe_median"] = float(m)
                                except (TypeError, ValueError):
                                    row["swe_median"] = None
                    row["swe_series"].sort(key=lambda t: t[0])
                elif code == "PREC":
                    latest_date = None
                    for v in values:
                        d = v.get("date")
                        val = v.get("value")
                        if not d or val is None:
                            continue
                        if latest_date is None or d > latest_date:
                            latest_date = d
                            try:
                                row["prec_current"] = float(val)
                            except (TypeError, ValueError):
                                row["prec_current"] = None
                            m = v.get("median")
                            if m is not None:
                                try:
                                    row["prec_median"] = float(m)
                                except (TypeError, ValueError):
                                    row["prec_median"] = None
            out[triplet] = row
    return out


# ---------------------------------------------------------------------------
# USDA Drought Monitor (USDM)
# ---------------------------------------------------------------------------

def fetch_county_drought(fips: str, today: dt.date) -> dict | None:
    """Fetch the most recent weekly USDM drought classification for a
    county and return cumulative percent-of-area by drought category.

    `d0_pct` = "D0 or worse" (abnormally dry +), `d1_pct` = "D1 or worse"
    (moderate drought +), etc. That cumulative form matches the common
    USDM phrasing used in the county page prose.

    Returns None on API failure.
    """
    # USDM publishes weekly on Thursdays. Query a 3-week window and
    # take the latest row to guarantee coverage regardless of weekday.
    begin = today - dt.timedelta(days=21)
    params = {
        "aoi": fips,
        "startdate": begin.isoformat(),
        "enddate": today.isoformat(),
        "statisticsType": "1",
    }
    try:
        rows = _get(
            f"{USDM_BASE}/CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent",
            params,
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[snotel] USDM fetch failed for {fips}: {exc}", file=sys.stderr)
        return None
    if not rows or not isinstance(rows, list):
        return None

    rows.sort(key=lambda r: r.get("validEnd") or "")
    latest = rows[-1]

    def _pct(key: str) -> float:
        v = latest.get(key)
        try:
            return round(float(v), 2) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    d0, d1, d2, d3, d4 = (_pct(k) for k in ("d0", "d1", "d2", "d3", "d4"))
    worst = None
    for label, v in (("D4", d4), ("D3", d3), ("D2", d2), ("D1", d1), ("D0", d0)):
        if v > 0:
            worst = label
            break

    valid_end = (latest.get("validEnd") or "")[:10] or None
    return {
        "valid_end": valid_end,
        "none_pct": _pct("none"),
        "d0_pct": d0,
        "d1_pct": d1,
        "d2_pct": d2,
        "d3_pct": d3,
        "d4_pct": d4,
        "worst_class": worst,
    }


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


def classify_precip(percent: int | None) -> str | None:
    if percent is None:
        return None
    for threshold, label in PRECIP_STATUS_THRESHOLDS:
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
    # NOTE: these ramps are unreviewed. See SOURCES.md §6. Precipitation
    # and drought are NOT yet folded into this score — they are written
    # to the JSON as informational fields.
    if status == "No Snowpack":
        return 40
    p = percent or 0
    if p < 70:
        return int(round(50 + (p / 70.0) * 15))
    if p < 110:
        return int(round(65 + ((p - 70) / 40.0) * 20))
    return min(100, int(round(85 + ((min(p, 200) - 110) / 90.0) * 15)))


def aggregate_precip(
    station_current: list[float],
    station_median: list[float],
) -> dict | None:
    """County-level water-year precipitation aggregate from station
    values. Returns None if no stations reported precip."""
    if not station_current:
        return None
    inches = round(sum(station_current) / len(station_current), 2)
    meds = [m for m in station_median if m and m > 0]
    percent = (
        int(round((inches / (sum(meds) / len(meds))) * 100))
        if meds
        else None
    )
    return {
        "inches": inches,
        "percent_of_median": percent,
        "status": classify_precip(percent),
    }


def build_record(slug: str, today: dt.date,
                 swe_current: list[float],
                 swe_medians: list[float],
                 swe_serieses: list[list[float]],
                 prec_current: list[float],
                 prec_medians: list[float],
                 drought: dict | None) -> dict:
    precip_ytd = aggregate_precip(prec_current, prec_medians)

    if not swe_current:
        return {
            "county": slug,
            "date": today.isoformat(),
            "swe_index": 0.0,
            "percent_of_median": 0,
            "trend": TREND_DOWN if today.month >= 4 else TREND_FLAT,
            "status": "No Snowpack",
            "forage_score": 40,
            "precip_ytd": precip_ytd,
            "drought": drought,
        }

    swe = round(sum(swe_current) / len(swe_current), 2)
    meds = [m for m in swe_medians if m and m > 0]
    percent = int(round((swe / (sum(meds) / len(meds))) * 100)) if meds else 0

    if swe_serieses:
        length = min(len(s) for s in swe_serieses)
        county_series = [
            sum(s[i] for s in swe_serieses) / len(swe_serieses)
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
        "precip_ytd": precip_ytd,
        "drought": drought,
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
        station_data = fetch_station_data(
            all_triplets, days_back=max(args.trend_days, 7)
        )
    except urllib.error.URLError as exc:
        print(f"[snotel] data fetch failed: {exc}", file=sys.stderr)
        return 2

    changed = 0
    for county, slug in ACTIVE_COUNTIES.items():
        swe_current, swe_medians, swe_serieses = [], [], []
        prec_current, prec_medians = [], []
        for triplet in triplets_by_county[county]:
            d = station_data.get(triplet, {})
            series = d.get("swe_series", [])
            if series:
                vals = [v for _, v in series]
                swe_current.append(vals[-1])
                swe_serieses.append(vals[-args.trend_days:])
                m = d.get("swe_median")
                if m is not None:
                    swe_medians.append(m)
            pc = d.get("prec_current")
            if pc is not None:
                prec_current.append(pc)
                pm = d.get("prec_median")
                if pm is not None:
                    prec_medians.append(pm)

        fips = COUNTY_FIPS.get(slug)
        drought = fetch_county_drought(fips, today) if fips else None

        record = build_record(
            slug, today,
            swe_current, swe_medians, swe_serieses,
            prec_current, prec_medians,
            drought,
        )
        path = args.out / f"{slug}.json"

        new_text = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == new_text:
            if args.verbose:
                print(f"[snotel] {slug}: unchanged")
            continue
        path.write_text(new_text, encoding="utf-8")
        changed += 1
        if args.verbose:
            pr = record.get("precip_ytd") or {}
            dr = record.get("drought") or {}
            pr_desc = (
                f"precip={pr.get('inches')}\" ({pr.get('percent_of_median')}%)"
                if pr else "precip=none"
            )
            dr_desc = (
                f"drought={dr.get('worst_class')}@d2+{dr.get('d2_pct')}%"
                if dr else "drought=none"
            )
            print(
                f"[snotel] {slug}: swe={record['swe_index']} "
                f"%med={record['percent_of_median']} "
                f"{record['trend']} {record['status']} "
                f"(forage {record['forage_score']}, "
                f"{len(triplets_by_county[county])} stn, {pr_desc}, {dr_desc})"
            )

    print(f"[snotel] {changed} of {len(ACTIVE_COUNTIES)} county files updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
