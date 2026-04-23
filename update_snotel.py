#!/usr/bin/env python3
"""
update_snotel.py
================

Drop-in daily updater for the fcrocker-nyc/dirk-adams-honest-cattle-data
repository. Pulls live moisture data from five public sources, aggregates
to the ten active honestcattle.net Montana counties, and writes one JSON
file per county at the repo root.

Data sources (v2.0 — HC Forage Score model integrated):
    1. NRCS AWDB (SNOTEL)            — SWE + water-year precipitation
    2. USDA Drought Monitor (USDM)   — county drought classification
    3. USGS Water Services (NWIS)    — stream discharge + day-of-year percentile
    4. Montana Mesonet (UMT)         — station-level soil VWC
    5. NOAA NCEI Climate at a Glance — monthly county precip anomaly (1/3/12 mo)

Output schema (v2.0 — forage_model added with full component breakdown):

    {
      "county": "gallatin",
      "date": "2026-04-14",
      "swe_index": 10.91,
      "percent_of_median": 55,
      "trend": "↓",
      "status": "Below Normal",
      "forage_score": 47,
      "forage_model": {
        "sp": 62.0, "mi": 32.5, "vr": 40.0, "dc": 45.0, "lu": 50.0,
        "category": "Poor", "confidence": "Medium",
        "model": "HC Forage v2.0"
      },
      "precip_ytd": { ... },
      "drought":    { ... },
      "streamflow": { ... },
      "soil_moisture": { ... },
      "precip_anomaly": { ... }
    }

All three new blocks are nullable — the script degrades gracefully if
any source fails or lacks coverage for a given county. The original
seven fields remain unchanged, so the existing [hc_snotel] shortcode
on honestcattle.net keeps working without modification.

forage_score now uses the full HC Forage Score model (v2.0):
    HC = 0.20(SP) + 0.25(MI) + 0.25(VR) + 0.15(DC) + 0.15(LU)
with streamflow adjustment. See HC_Forage_Score_Automation_Plan.docx.
A new forage_model block in the JSON provides component breakdown.

Standard library only. Runs in GitHub Actions without pip deps.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
USDM_BASE = "https://usdmdataservices.unl.edu/api"
USGS_BASE = "https://waterservices.usgs.gov/nwis"
MESONET_BASE = "https://mesonet.climate.umt.edu/api/v2"
NCEI_CAG_BASE = "https://www.ncei.noaa.gov/cag/county/mapping"
NASS_BASE = "https://quickstats.nass.usda.gov/api/api_GET"
USER_AGENT = "honestcattle-snotel-updater/2.0 (+https://honestcattle.net)"
REQUEST_TIMEOUT = 30
NASS_API_KEY = __import__("os").environ.get("NASS_API_KEY", "")

# Active honestcattle.net Montana counties. Maps the canonical NRCS
# countyName to the repo's filename slug.
ACTIVE_COUNTIES: dict[str, str] = {
    "Beaverhead":      "beaverhead",
    "Big Horn":        "big_horn",
    "Blaine":          "blaine",
    "Broadwater":      "broadwater",
    "Carbon":          "carbon",
    "Carter":          "carter",
    "Cascade":         "cascade",
    "Chouteau":        "chouteau",
    "Custer":          "custer",
    "Daniels":         "daniels",
    "Dawson":          "dawson",
    "Deer Lodge":      "deer_lodge",
    "Fallon":          "fallon",
    "Fergus":          "fergus",
    "Flathead":        "flathead",
    "Gallatin":        "gallatin",
    "Garfield":        "garfield",
    "Glacier":         "glacier",
    "Golden Valley":   "golden_valley",
    "Granite":         "granite",
    "Hill":            "hill",
    "Jefferson":       "jefferson",
    "Judith Basin":    "judith_basin",
    "Lake":            "lake",
    "Lewis and Clark": "lewis_clark",
    "Liberty":         "liberty",
    "Lincoln":         "lincoln",
    "Madison":         "madison",
    "McCone":          "mccone",
    "Meagher":         "meagher",
    "Mineral":         "mineral",
    "Missoula":        "missoula",
    "Musselshell":     "musselshell",
    "Park":            "park",
    "Petroleum":       "petroleum",
    "Phillips":        "phillips",
    "Pondera":         "pondera",
    "Powder River":    "powder_river",
    "Powell":          "powell",
    "Prairie":         "prairie",
    "Ravalli":         "ravalli",
    "Richland":        "richland",
    "Roosevelt":       "roosevelt",
    "Rosebud":         "rosebud",
    "Sanders":         "sanders",
    "Sheridan":        "sheridan",
    "Silver Bow":      "silver_bow",
    "Stillwater":      "stillwater",
    "Sweet Grass":     "sweet_grass",
    "Teton":           "teton",
    "Toole":           "toole",
    "Treasure":        "treasure",
    "Valley":          "valley",
    "Wheatland":       "wheatland",
    "Wibaux":          "wibaux",
    "Yellowstone":     "yellowstone",
}

# USDM API uses federal county FIPS codes. MT state prefix = 30.
COUNTY_FIPS: dict[str, str] = {
    "beaverhead":    "30001",
    "big_horn":      "30003",
    "blaine":        "30005",
    "broadwater":    "30007",
    "carbon":        "30009",
    "carter":        "30011",
    "cascade":       "30013",
    "chouteau":      "30015",
    "custer":        "30017",
    "daniels":       "30019",
    "dawson":        "30021",
    "deer_lodge":    "30023",
    "fallon":        "30025",
    "fergus":        "30027",
    "flathead":      "30029",
    "gallatin":      "30031",
    "garfield":      "30033",
    "glacier":       "30035",
    "golden_valley": "30037",
    "granite":       "30039",
    "hill":          "30041",
    "jefferson":     "30043",
    "judith_basin":  "30045",
    "lake":          "30047",
    "lewis_clark":   "30049",
    "liberty":       "30051",
    "lincoln":       "30053",
    "madison":       "30057",
    "mccone":        "30055",
    "meagher":       "30059",
    "mineral":       "30061",
    "missoula":      "30063",
    "musselshell":   "30065",
    "park":          "30067",
    "petroleum":     "30069",
    "phillips":      "30071",
    "pondera":       "30073",
    "powder_river":  "30075",
    "powell":        "30077",
    "prairie":       "30079",
    "ravalli":       "30081",
    "richland":      "30083",
    "roosevelt":     "30085",
    "rosebud":       "30087",
    "sanders":       "30089",
    "sheridan":      "30091",
    "silver_bow":    "30093",
    "stillwater":    "30095",
    "sweet_grass":   "30097",
    "teton":         "30099",
    "toole":         "30101",
    "treasure":      "30103",
    "valley":        "30105",
    "wheatland":     "30107",
    "wibaux":        "30109",
    "yellowstone":   "30111",
}

# USGS stream gauge per county (site_no, display name).
# Picked the most agriculturally-relevant active gauge on the river the
# honestcattle.net page prose actually references for each county.
# Gauges discovered via USGS NWIS site service (stateCd=mt, siteType=ST,
# parameterCd=00060, siteStatus=active).
# Carter, Fallon, Garfield, Prairie, and Wibaux have no active in-county
# discharge gauge, so they are omitted and their streamflow field stays null.
COUNTY_GAUGES: dict[str, tuple[str, str]] = {
    "beaverhead":    ("06017000", "Beaverhead River at Dillon"),
    "big_horn":      ("06287000", "Bighorn River below Yellowtail Afterbay Dam near St. Xavier"),
    "blaine":        ("06155030", "Milk River near Dodson"),
    "broadwater":    ("06054500", "Missouri River at Toston"),
    "carbon":        ("06207500", "Clarks Fork Yellowstone River near Belfry"),
    "cascade":       ("06090300", "Missouri River near Great Falls"),
    "chouteau":      ("06090800", "Missouri River at Fort Benton"),
    "custer":        ("06309000", "Yellowstone River at Miles City"),
    "daniels":       ("06178000", "Poplar River at international boundary"),
    "dawson":        ("06327500", "Yellowstone River at Glendive"),
    "deer_lodge":    ("12323770", "Warm Springs Creek at Warm Springs"),
    "fergus":        ("06111800", "Big Spring Creek at Lewistown"),
    "flathead":      ("12363000", "Flathead River at Columbia Falls"),
    "gallatin":      ("06052500", "Gallatin River at Logan"),
    "glacier":       ("05017500", "St. Mary River near Babb"),
    "golden_valley": ("06125600", "Musselshell River at Lavina"),
    "granite":       ("12331500", "Flint Creek near Drummond"),
    "hill":          ("06139500", "Big Sandy Creek near Havre"),
    "jefferson":     ("06033000", "Boulder River near Boulder"),
    "judith_basin":  ("06110020", "Judith River above Carr Creek near Utica"),
    "lake":          ("12372000", "Flathead River near Polson"),
    "lewis_clark":   ("06073500", "Dearborn River near Craig"),
    "liberty":       ("06101500", "Marias River near Chester"),
    "lincoln":       ("12301933", "Kootenai River below Libby Dam near Libby"),
    "madison":       ("06018500", "Beaverhead River near Twin Bridges"),
    "mccone":        ("06177500", "Redwater River at Circle"),
    "meagher":       ("06076690", "Smith River near Fort Logan"),
    "mineral":       ("12354500", "Clark Fork at St. Regis"),
    "missoula":      ("12340500", "Clark Fork above Missoula"),
    "musselshell":   ("06126500", "Musselshell River near Roundup"),
    "park":          ("06192500", "Yellowstone River near Livingston"),
    "petroleum":     ("06130500", "Musselshell River at Mosby"),
    "phillips":      ("06155500", "Milk River at Malta"),
    "pondera":       ("06097000", "Birch Creek at Robare"),
    "powder_river":  ("06324500", "Powder River at Moorhead"),
    "powell":        ("12324200", "Clark Fork at Deer Lodge"),
    "ravalli":       ("12350250", "Bitterroot River at Bell Crossing near Victor"),
    "richland":      ("06329500", "Yellowstone River near Sidney"),
    "roosevelt":     ("06181000", "Poplar River near Poplar"),
    "rosebud":       ("06295000", "Yellowstone River at Forsyth"),
    "sanders":       ("12389000", "Clark Fork near Plains"),
    "sheridan":      ("06183450", "Big Muddy Creek near Antelope"),
    "silver_bow":    ("06024580", "Big Hole River near Wise River"),
    "stillwater":    ("06205000", "Stillwater River near Absarokee"),
    "sweet_grass":   ("06200000", "Boulder River at Big Timber"),
    "teton":         ("06102500", "Teton River below South Fork near Choteau"),
    "toole":         ("06099500", "Marias River near Shelby"),
    "treasure":      ("06294500", "Bighorn River above Tullock Creek near Bighorn"),
    "valley":        ("06174500", "Milk River at Nashua"),
    "wheatland":     ("06120500", "Musselshell River at Harlowton"),
    "yellowstone":   ("06214500", "Yellowstone River at Billings"),
}

# Structural Potential (SP) — static per-county value representing
# inherent rangeland productivity. Western irrigated valleys higher,
# eastern dryland plains lower. Values from HC Forage Score Automation
# Plan where available; remainder assigned by ag region.
STRUCTURAL_POTENTIAL: dict[str, int] = {
    "beaverhead": 70, "big_horn": 55, "blaine": 50, "broadwater": 60,
    "carbon": 63, "carter": 48, "cascade": 62, "chouteau": 55,
    "custer": 52, "daniels": 48, "dawson": 50, "deer_lodge": 62,
    "fallon": 45, "fergus": 58, "flathead": 65, "gallatin": 62,
    "garfield": 45, "glacier": 58, "golden_valley": 55, "granite": 60,
    "hill": 52, "jefferson": 60, "judith_basin": 60, "lake": 65,
    "lewis_clark": 58, "liberty": 50, "lincoln": 55, "madison": 68,
    "mccone": 48, "meagher": 62, "mineral": 52, "missoula": 60,
    "musselshell": 55, "park": 68, "petroleum": 45, "phillips": 50,
    "pondera": 58, "powder_river": 48, "powell": 58, "prairie": 45,
    "ravalli": 65, "richland": 52, "roosevelt": 50, "rosebud": 52,
    "sanders": 55, "sheridan": 48, "silver_bow": 55, "stillwater": 65,
    "sweet_grass": 64, "teton": 60, "toole": 55, "treasure": 52,
    "valley": 50, "wheatland": 55, "wibaux": 45, "yellowstone": 58,
}

# Classification thresholds on percent-of-median SWE. See SOURCES.md §5.
STATUS_THRESHOLDS = [
    (0,   "No Snowpack"),
    (70,  "Below Normal"),
    (110, "Normal"),
    (200, "Above Normal"),
]

# Same percent-of-median thresholds applied to water-year precipitation.
PRECIP_STATUS_THRESHOLDS = [
    (70,  "Below Normal"),
    (110, "Normal"),
    (200, "Above Normal"),
]

# Streamflow status bands, based on day-of-year percentile.
# Below 25th percentile = below normal, 25-75 = normal, above 75 = above normal.
STREAMFLOW_STATUS_THRESHOLDS = [
    (25,  "Below Normal"),
    (75,  "Normal"),
    (101, "Above Normal"),
]

TREND_UP, TREND_DOWN, TREND_FLAT = "\u2191", "\u2193", "\u2192"

# Multi-element AWDB query: WTEQ = SWE, PREC = water-year precip accumulation.
ELEMENTS_REQUESTED = "WTEQ,PREC"

# NCEI state code for Montana (NOT FIPS — NCEI uses its own 2-digit system).
NCEI_STATE_MT = 24

# Mesonet Soil VWC field name parser: "Soil VWC @ -5 cm [%]"
_MESONET_VWC_RE = re.compile(r"^Soil VWC @ -(\d+) cm")

# Mesonet soil moisture depth buckets in cm.
# Shallow = 5/10 cm (grass & forb root zone, responsive to recent precip).
# Deep    = 50/100 cm (subsoil storage, slower season-long signal).
MESONET_SHALLOW_CM = {5, 10}
MESONET_DEEP_CM = {50, 100}


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


def _get_text(url: str, params: dict | None = None) -> str:
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# NRCS AWDB — SWE + water-year precipitation
# ---------------------------------------------------------------------------

def fetch_mt_stations() -> list[dict]:
    # NRCS AWDB /stations silently ignores server-side filters; filter
    # client-side by stateCode + networkCode. Keep raw countyName (already
    # matches our keys, e.g. "Lewis and Clark" — don't .title() it).
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
    with >1 triplet, recursively splits the chunk so a single bad
    station (e.g. 690:MT:SNTL / Pickfoot Creek) doesn't poison the batch.
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
# USDA Drought Monitor
# ---------------------------------------------------------------------------

def fetch_county_drought(fips: str, today: dt.date) -> dict | None:
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
# USGS Water Services — streamflow
# ---------------------------------------------------------------------------

def _rdb_parse(text: str) -> list[dict]:
    """Parse USGS RDB tab-separated-values into a list of dicts. Skips
    lines starting with '#' and the type-descriptor line (line after
    the header that has entries like '5s', '15s', etc.)."""
    lines = [l for l in text.split("\n") if l and not l.startswith("#")]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    # Skip the type-descriptor line (all cells look like "\d+s" or "\d+n")
    start_idx = 1
    if start_idx < len(lines) and re.match(r"^[\ds]+$", lines[1].replace("\t", "").replace("n", "")):
        start_idx = 2
    out: list[dict] = []
    for line in lines[start_idx:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue
        out.append(dict(zip(header, parts)))
    return out


def _streamflow_percentile(cfs: float, p10: float, p25: float, p50: float,
                           p75: float, p90: float) -> int:
    """Linear interpolation of a discharge value against day-of-year
    percentile bands. Returns an integer 0-100."""
    if cfs is None:
        return None
    if cfs <= p10:
        return max(0, int(round(10 * cfs / p10))) if p10 > 0 else 0
    if cfs <= p25:
        return int(round(10 + 15 * (cfs - p10) / (p25 - p10)))
    if cfs <= p50:
        return int(round(25 + 25 * (cfs - p25) / (p50 - p25)))
    if cfs <= p75:
        return int(round(50 + 25 * (cfs - p50) / (p75 - p50)))
    if cfs <= p90:
        return int(round(75 + 15 * (cfs - p75) / (p90 - p75)))
    return min(100, int(round(90 + 10 * (cfs - p90) / max(p90, 1.0))))


def _classify_streamflow(percentile: int | None) -> str | None:
    if percentile is None:
        return None
    for threshold, label in STREAMFLOW_STATUS_THRESHOLDS:
        if percentile < threshold:
            return label
    return "Above Normal"


def fetch_streamflow(site_no: str, gauge_name: str, today: dt.date) -> dict | None:
    """Fetch current daily discharge + day-of-year historical percentile
    bands for a USGS stream gauge. Returns None on any failure.
    """
    # 1. Current daily value
    try:
        dv = _get(f"{USGS_BASE}/dv/", {
            "format": "json",
            "sites": site_no,
            "parameterCd": "00060",
            "siteStatus": "active",
        })
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[snotel] USGS dv fetch failed for {site_no}: {exc}", file=sys.stderr)
        return None
    ts = dv.get("value", {}).get("timeSeries", [])
    if not ts:
        return None
    values = ts[0].get("values", [{}])[0].get("value", [])
    if not values:
        return None
    latest = values[-1]
    try:
        cfs = float(latest.get("value"))
    except (TypeError, ValueError):
        return None
    # "-999999" is USGS's no-data sentinel
    if cfs < 0:
        return None

    # 2. Historical day-of-year percentile bands for this site
    try:
        rdb = _get_text(f"{USGS_BASE}/stat/", {
            "format": "rdb",
            "sites": site_no,
            "parameterCd": "00060",
            "statReportType": "daily",
            "statTypeCd": "p10,p25,p50,p75,p90",
        })
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[snotel] USGS stat fetch failed for {site_no}: {exc}", file=sys.stderr)
        return {
            "gauge_name": gauge_name,
            "site_no": site_no,
            "cfs": round(cfs, 0),
            "percentile": None,
            "status": None,
        }

    rows = _rdb_parse(rdb)
    percentile = None

    def _num(x: object) -> float | None:
        if x is None:
            return None
        s = str(x).strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    for row in rows:
        try:
            m = int(row.get("month_nu", 0))
            d = int(row.get("day_nu", 0))
        except (TypeError, ValueError):
            continue
        if m != today.month or d != today.day:
            continue
        p10 = _num(row.get("p10_va"))
        p25 = _num(row.get("p25_va"))
        p50 = _num(row.get("p50_va"))
        p75 = _num(row.get("p75_va"))
        p90 = _num(row.get("p90_va"))
        # USGS occasionally omits p90 (and rarely p10) on high-variance
        # spring days. Extrapolate from adjacent bands to keep the
        # percentile calculation meaningful rather than dropping it.
        if p50 is None:
            break
        if p10 is None and p25 is not None:
            p10 = p25 * 0.6
        if p25 is None and p10 is not None and p50 is not None:
            p25 = (p10 + p50) / 2
        if p75 is None and p50 is not None:
            p75 = p50 * 1.3
        if p90 is None and p75 is not None:
            p90 = p75 * 1.3
        if None in (p10, p25, p50, p75, p90):
            break
        percentile = _streamflow_percentile(cfs, p10, p25, p50, p75, p90)
        break

    return {
        "gauge_name": gauge_name,
        "site_no": site_no,
        "cfs": round(cfs, 0),
        "percentile": percentile,
        "status": _classify_streamflow(percentile),
    }


# ---------------------------------------------------------------------------
# Montana Mesonet — soil moisture (VWC at multiple depths)
# ---------------------------------------------------------------------------

def fetch_mesonet_stations() -> list[dict]:
    """Fetch all Mesonet stations. Returns a list of dicts with keys:
    station, name, county, has_swp, etc."""
    try:
        data = _get(f"{MESONET_BASE}/stations", {"type": "json"})
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[snotel] Mesonet stations fetch failed: {exc}", file=sys.stderr)
        return []
    return data or []


def fetch_mesonet_latest(station_ids: list[str]) -> list[dict]:
    """Fetch latest observations for a batch of Mesonet station IDs."""
    if not station_ids:
        return []
    try:
        data = _get(f"{MESONET_BASE}/latest", {
            "stations": ",".join(station_ids),
            "type": "json",
        })
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[snotel] Mesonet latest fetch failed: {exc}", file=sys.stderr)
        return []
    return data or []


def aggregate_mesonet_soil_moisture(
    obs: list[dict],
) -> dict | None:
    """Aggregate a list of station observation dicts into a county-level
    soil-moisture summary. Expects each obs to have fields like
    'Soil VWC @ -5 cm [%]' with numeric values."""
    if not obs:
        return None
    shallow_vals: list[float] = []
    deep_vals: list[float] = []
    contributing = 0
    for station_obs in obs:
        if not isinstance(station_obs, dict):
            continue
        station_has_vwc = False
        for key, val in station_obs.items():
            m = _MESONET_VWC_RE.match(str(key))
            if not m:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            depth_cm = int(m.group(1))
            if depth_cm in MESONET_SHALLOW_CM:
                shallow_vals.append(v)
                station_has_vwc = True
            elif depth_cm in MESONET_DEEP_CM:
                deep_vals.append(v)
                station_has_vwc = True
        if station_has_vwc:
            contributing += 1
    if contributing == 0:
        return None
    return {
        "shallow_vwc_pct": round(sum(shallow_vals) / len(shallow_vals), 1)
                           if shallow_vals else None,
        "deep_vwc_pct":    round(sum(deep_vals) / len(deep_vals), 1)
                           if deep_vals else None,
        "station_count":   contributing,
        "source":          "Montana Mesonet",
    }


# ---------------------------------------------------------------------------
# NOAA NCEI Climate at a Glance — county precipitation anomaly
# ---------------------------------------------------------------------------

def fetch_cag_precip_anomaly(today: dt.date) -> dict:
    """Fetch 1/3/12-month county-level precipitation anomaly for every
    Montana county from NCEI's Climate at a Glance mapping endpoint.

    Returns a dict keyed by county FIPS (e.g. 'MT-059'):
        {
          'MT-059': {
            'month_end': '2026-03',
            'm1':  {'inches': 1.98, 'normal': 1.66, 'anomaly': 0.32, 'rank': 97},
            'm3':  {'inches': 3.38, 'normal': 4.36, 'anomaly': -0.98, 'rank': 34},
            'm12': {'inches': 18.74,'normal': 21.96,'anomaly': -3.22, 'rank': 30}
          },
          ...
        }

    NCEI publishes data by completed calendar month. Try the current
    month first; if it 404s, fall back to the previous month.
    """
    # Figure out the most recent month NCEI has data for.
    candidate_months = []
    cm = today.replace(day=1)
    candidate_months.append(cm)
    prev = (cm - dt.timedelta(days=1)).replace(day=1)
    candidate_months.append(prev)

    periods = [("m1", 1), ("m3", 3), ("m12", 12)]

    # By-county accumulator.
    by_county: dict[str, dict] = {}
    month_end_used: str | None = None

    for period_key, n_months in periods:
        data = None
        month_used = None
        for cand in candidate_months:
            yyyymm = cand.strftime("%Y%m")
            url = f"{NCEI_CAG_BASE}/{NCEI_STATE_MT}-pcp-{yyyymm}-{n_months}.json"
            try:
                data = _get(url)
                month_used = cand.strftime("%Y-%m")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                print(f"[snotel] NCEI CAG {period_key} fetch failed: {exc}", file=sys.stderr)
                break
            except urllib.error.URLError as exc:
                print(f"[snotel] NCEI CAG {period_key} fetch failed: {exc}", file=sys.stderr)
                break
        if data is None:
            continue
        if month_end_used is None:
            month_end_used = month_used
        for key, v in (data.get("data") or {}).items():
            if not isinstance(v, dict):
                continue
            entry = by_county.setdefault(key, {"month_end": month_used})
            def _num(k: str) -> float | None:
                val = v.get(k)
                try:
                    return round(float(val), 2) if val is not None else None
                except (TypeError, ValueError):
                    return None
            entry[period_key] = {
                "inches":  _num("value"),
                "normal":  _num("mean"),
                "anomaly": _num("anomaly"),
                "rank":    v.get("rank"),
            }

    return by_county


def fetch_nass_range_condition() -> dict | None:
    """Fetch latest Montana pasture & range condition from NASS Quick Stats.

    Returns dict with VP/P/F/G/E percentages and week_ending, or None
    if the API key is missing or the query fails. Data is statewide
    (Montana), published weekly April–October.

    Requires NASS_API_KEY environment variable (free signup at
    https://quickstats.nass.usda.gov/api/).
    """
    if not NASS_API_KEY:
        return None

    today = dt.date.today()
    rows = []
    for year in (today.year, today.year - 1):
        params = urllib.parse.urlencode({
            "key": NASS_API_KEY,
            "commodity_desc": "PASTURELAND",
            "statisticcat_desc": "CONDITION",
            "state_alpha": "MT",
            "year": year,
            "format": "JSON",
        })
        url = f"{NASS_BASE}/?{params}"
        try:
            data = _get(url)
            rows = data.get("data", [])
            if rows:
                break
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[snotel] NASS condition fetch ({year}): {exc}", file=sys.stderr)
            continue

    if not rows:
        return None

    latest_week = max(
        (r.get("week_ending") or r.get("end_code", "") for r in rows),
        default="",
    )
    if not latest_week:
        return None

    week_rows = [r for r in rows if (r.get("week_ending") or r.get("end_code", "")) == latest_week]

    result: dict = {"week_ending": latest_week, "source": "NASS Quick Stats"}
    for r in week_rows:
        desc = (r.get("short_desc") or "").upper()
        try:
            val = float(str(r.get("Value", "0")).replace(",", ""))
        except (ValueError, TypeError):
            continue
        if "VERY POOR" in desc:
            result["vp"] = val
        elif "POOR" in desc:
            result["p"] = val
        elif "FAIR" in desc:
            result["f"] = val
        elif "EXCELLENT" in desc:
            result["e"] = val
        elif "GOOD" in desc:
            result["g"] = val

    needed = {"vp", "p", "f", "g", "e"}
    if not needed.issubset(result.keys()):
        return None

    return result


# ---------------------------------------------------------------------------
# Aggregation helpers
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


# ---------------------------------------------------------------------------
# HC Forage Score — full 5-component model
# HC = 0.20(SP) + 0.25(MI) + 0.25(VR) + 0.15(DC) + 0.15(LU)
# From HC_Forage_Score_Automation_Plan.docx
# ---------------------------------------------------------------------------

def _precip_component(pct_normal: float | None) -> float:
    if pct_normal is None:
        return 50.0
    if pct_normal >= 120: return 95.0
    if pct_normal >= 100: return 75.0
    if pct_normal >= 90:  return 60.0
    if pct_normal >= 80:  return 45.0
    if pct_normal >= 70:  return 30.0
    if pct_normal >= 60:  return 15.0
    return 5.0


def _swe_component(swe_pct: int | None) -> float:
    if swe_pct is None or swe_pct == 0:
        return 5.0
    if swe_pct >= 120: return 95.0
    if swe_pct >= 100: return 75.0
    if swe_pct >= 85:  return 55.0
    if swe_pct >= 70:  return 35.0
    if swe_pct >= 55:  return 15.0
    return 5.0


def _basin_precip_component(basin_pct: float | None) -> float:
    if basin_pct is None:
        return 50.0
    if basin_pct >= 125: return 90.0
    if basin_pct >= 100: return 70.0
    if basin_pct >= 85:  return 55.0
    if basin_pct >= 70:  return 35.0
    if basin_pct >= 55:  return 15.0
    return 5.0


def _drought_component(drought: dict | None) -> float:
    if not drought:
        return 50.0
    d0 = drought.get("d0_pct", 0) or 0
    d1 = drought.get("d1_pct", 0) or 0
    d2 = drought.get("d2_pct", 0) or 0
    d3 = drought.get("d3_pct", 0) or 0
    d4 = drought.get("d4_pct", 0) or 0
    weighted = 0.10*d0 + 0.30*d1 + 0.55*d2 + 0.80*d3 + 1.00*d4
    return max(5.0, min(95.0, 100 - weighted))


def _soil_vr_proxy(soil_moisture: dict | None,
                   precip_anomaly: dict | None) -> float | None:
    """Vegetation-response proxy from soil moisture + precip trend.

    Combines Mesonet soil VWC with recent precipitation anomaly to
    approximate vegetation health when NDVI is unavailable.
    """
    components: list[float] = []
    weights: list[float] = []

    if soil_moisture:
        shallow = soil_moisture.get("shallow_vwc_pct")
        if shallow is not None:
            if shallow >= 35:   sv = 85.0
            elif shallow >= 28: sv = 70.0
            elif shallow >= 22: sv = 55.0
            elif shallow >= 16: sv = 40.0
            elif shallow >= 10: sv = 25.0
            else:               sv = 10.0
            components.append(sv)
            weights.append(0.65)

    if precip_anomaly:
        m1 = precip_anomaly.get("m1") or {}
        m3 = precip_anomaly.get("m3") or {}
        anomaly = m3.get("anomaly") if m3.get("anomaly") is not None else m1.get("anomaly")
        normal = m3.get("normal") if m3.get("normal") is not None else m1.get("normal")
        if anomaly is not None and normal and normal > 0:
            pct_dep = (anomaly / normal) * 100
            if pct_dep >= 20:    pv = 85.0
            elif pct_dep >= 0:   pv = 70.0
            elif pct_dep >= -15: pv = 55.0
            elif pct_dep >= -30: pv = 40.0
            elif pct_dep >= -50: pv = 25.0
            else:                pv = 10.0
            components.append(pv)
            weights.append(0.35)

    if not components:
        return None
    total_w = sum(weights)
    return sum(c * w for c, w in zip(components, weights)) / total_w


def _streamflow_adjustment(streamflow: dict | None) -> float:
    """Small adjustment from streamflow percentile (+/- up to 5 points)."""
    if not streamflow:
        return 0.0
    pctl = streamflow.get("percentile")
    if pctl is None:
        return 0.0
    if pctl >= 75:   return 5.0
    if pctl >= 50:   return 2.0
    if pctl >= 25:   return 0.0
    if pctl >= 10:   return -3.0
    return -5.0


def _forage_category(score: int) -> str:
    if score >= 80: return "Excellent"
    if score >= 65: return "Good"
    if score >= 50: return "Fair"
    if score >= 35: return "Poor"
    return "Very Poor"


def _nass_condition_score(nass: dict) -> float:
    """Convert NASS VP/P/F/G/E percentages to a 0-100 score."""
    vp = nass.get("vp", 0)
    p = nass.get("p", 0)
    f = nass.get("f", 0)
    g = nass.get("g", 0)
    e = nass.get("e", 0)
    return vp * 0.05 + p * 0.25 + f * 0.55 + g * 0.80 + e * 1.00


def forage_score(
    slug: str,
    swe_pct: int | None,
    status: str,
    precip_ytd: dict | None,
    drought: dict | None,
    streamflow: dict | None,
    soil_moisture: dict | None,
    precip_anomaly: dict | None,
    nass_condition: dict | None = None,
) -> tuple[int, dict]:
    """Full forage model. Returns (score, model_detail_dict)."""
    sp = float(STRUCTURAL_POTENTIAL.get(slug, 55))

    precip_pct = None
    if precip_anomaly:
        m1 = precip_anomaly.get("m1") or {}
        inches = m1.get("inches")
        normal = m1.get("normal")
        if inches is not None and normal and normal > 0:
            precip_pct = (inches / normal) * 100
    if precip_pct is None and precip_ytd:
        precip_pct = precip_ytd.get("percent_of_median")

    basin_pct = None
    if precip_ytd:
        basin_pct = precip_ytd.get("percent_of_median")

    mi = (0.45 * _precip_component(precip_pct) +
          0.35 * _swe_component(swe_pct) +
          0.20 * _basin_precip_component(basin_pct))

    vr_val = _soil_vr_proxy(soil_moisture, precip_anomaly)
    vr = vr_val if vr_val is not None else 50.0

    nass_score = 50.0
    nass_live = False
    if nass_condition and all(k in nass_condition for k in ("vp", "p", "f", "g", "e")):
        nass_score = _nass_condition_score(nass_condition)
        nass_live = True

    dc = 0.60 * _drought_component(drought) + 0.40 * nass_score

    lu = 50.0

    raw = 0.20*sp + 0.25*mi + 0.25*vr + 0.15*dc + 0.15*lu
    raw += _streamflow_adjustment(streamflow)
    score = max(0, min(100, int(round(raw))))

    missing = 0
    if precip_pct is None: missing += 1
    if vr_val is None: missing += 1
    if not nass_live: missing += 1

    detail = {
        "sp": round(sp, 1),
        "mi": round(mi, 1),
        "vr": round(vr, 1),
        "dc": round(dc, 1),
        "lu": round(lu, 1),
        "category": _forage_category(score),
        "confidence": "High" if missing == 0 else ("Medium" if missing <= 1 else "Low"),
        "model": "HC Forage v2.1",
    }
    if nass_live:
        detail["nass_week"] = nass_condition.get("week_ending")
    return score, detail


def aggregate_precip(
    station_current: list[float],
    station_median: list[float],
) -> dict | None:
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
                 drought: dict | None,
                 streamflow: dict | None,
                 soil_moisture: dict | None,
                 precip_anomaly: dict | None,
                 nass_condition: dict | None = None) -> dict:
    precip_ytd = aggregate_precip(prec_current, prec_medians)

    if not swe_current:
        score, model = forage_score(
            slug, 0, "No Snowpack",
            precip_ytd, drought, streamflow, soil_moisture, precip_anomaly,
            nass_condition,
        )
        return {
            "county": slug,
            "date": today.isoformat(),
            "swe_index": 0.0,
            "percent_of_median": 0,
            "trend": TREND_DOWN if today.month >= 4 else TREND_FLAT,
            "status": "No Snowpack",
            "forage_score": score,
            "forage_model": model,
            "precip_ytd": precip_ytd,
            "drought": drought,
            "streamflow": streamflow,
            "soil_moisture": soil_moisture,
            "precip_anomaly": precip_anomaly,
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
    score, model = forage_score(
        slug, percent, status,
        precip_ytd, drought, streamflow, soil_moisture, precip_anomaly,
        nass_condition,
    )
    return {
        "county": slug,
        "date": today.isoformat(),
        "swe_index": swe,
        "percent_of_median": percent,
        "trend": compute_trend(county_series),
        "status": status,
        "forage_score": score,
        "forage_model": model,
        "precip_ytd": precip_ytd,
        "drought": drought,
        "streamflow": streamflow,
        "soil_moisture": soil_moisture,
        "precip_anomaly": precip_anomaly,
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

    # -------- NRCS SNOTEL (SWE + PREC) --------
    # NRCS AWDB bootstrap is the only hard-fail path in this script — every
    # other source degrades gracefully. Retry transient URLErrors with
    # exponential backoff before giving up; a single hiccup from
    # wcc.sc.egov.usda.gov should not kill the whole 56-county refresh.
    stations: list = []
    for attempt in range(3):
        try:
            stations = fetch_mt_stations()
            break
        except urllib.error.URLError as exc:
            if attempt == 2:
                print(f"[snotel] station fetch failed after 3 attempts: {exc}", file=sys.stderr)
                return 2
            delay = 20 * (attempt + 1)
            print(f"[snotel] station fetch attempt {attempt+1} failed ({exc}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)

    if args.verbose:
        print(f"[snotel] discovered {len(stations)} MT SNOTEL stations")

    triplets_by_county: dict[str, list[str]] = {c: [] for c in ACTIVE_COUNTIES}
    for s in stations:
        cn = s.get("countyName") or ""
        triplet = s.get("stationTriplet")
        if cn in ACTIVE_COUNTIES and triplet:
            triplets_by_county[cn].append(triplet)

    all_triplets = [t for v in triplets_by_county.values() for t in v]
    station_data: list = []
    for attempt in range(3):
        try:
            station_data = fetch_station_data(
                all_triplets, days_back=max(args.trend_days, 7)
            )
            break
        except urllib.error.URLError as exc:
            if attempt == 2:
                print(f"[snotel] data fetch failed after 3 attempts: {exc}", file=sys.stderr)
                return 2
            delay = 20 * (attempt + 1)
            print(f"[snotel] data fetch attempt {attempt+1} failed ({exc}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)

    # -------- Mesonet soil moisture (one pull, grouped by county) --------
    mesonet_stns = fetch_mesonet_stations()
    mesonet_ids_by_county: dict[str, list[str]] = {c: [] for c in ACTIVE_COUNTIES}
    for ms in mesonet_stns:
        county = ms.get("county") or ""
        if county in ACTIVE_COUNTIES and ms.get("has_swp") and ms.get("station"):
            mesonet_ids_by_county[county].append(ms["station"])
    if args.verbose:
        total_mesonet = sum(len(v) for v in mesonet_ids_by_county.values())
        print(f"[snotel] Mesonet: {total_mesonet} SWP-equipped stations in target counties")

    # -------- NCEI CAG county precip anomaly (one pull for all MT counties) --------
    cag_by_county = fetch_cag_precip_anomaly(today)
    if args.verbose:
        print(f"[snotel] NCEI CAG: {len(cag_by_county)} MT counties loaded")

    # -------- NASS pasture & range condition (one pull, statewide MT) --------
    nass_condition = fetch_nass_range_condition()
    if args.verbose:
        if nass_condition:
            print(f"[snotel] NASS: MT range condition week {nass_condition.get('week_ending')} "
                  f"(G={nass_condition.get('g')}% F={nass_condition.get('f')}% P={nass_condition.get('p')}%)")
        else:
            nass_reason = "no API key" if not NASS_API_KEY else "no data (off-season?)"
            print(f"[snotel] NASS: skipped ({nass_reason})")

    # -------- Per-county assembly --------
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

        # Drought (once per county via USDM).
        fips = COUNTY_FIPS.get(slug)
        drought = fetch_county_drought(fips, today) if fips else None

        # Streamflow (once per county via USGS).
        gauge = COUNTY_GAUGES.get(slug)
        streamflow = fetch_streamflow(gauge[0], gauge[1], today) if gauge else None

        # Soil moisture (once per county via Mesonet).
        mesonet_ids = mesonet_ids_by_county.get(county, [])
        mesonet_obs = fetch_mesonet_latest(mesonet_ids) if mesonet_ids else []
        soil_moisture = aggregate_mesonet_soil_moisture(mesonet_obs)

        # Precip anomaly (looked up from the pre-fetched CAG dict).
        cag_key = f"MT-{fips[-3:]}" if fips else None
        precip_anomaly = cag_by_county.get(cag_key) if cag_key else None

        record = build_record(
            slug, today,
            swe_current, swe_medians, swe_serieses,
            prec_current, prec_medians,
            drought, streamflow, soil_moisture, precip_anomaly,
            nass_condition,
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
            sf = record.get("streamflow") or {}
            sm = record.get("soil_moisture") or {}
            pa = record.get("precip_anomaly") or {}
            pr_desc = (f"precip={pr.get('inches')}\"" if pr else "precip=none")
            dr_desc = (f"{dr.get('worst_class')}" if dr else "no-drought")
            sf_desc = (f"{sf.get('cfs')}cfs p{sf.get('percentile')}" if sf else "no-flow")
            sm_desc = (f"vwc_sh={sm.get('shallow_vwc_pct')}%" if sm else "no-vwc")
            pa1 = (pa.get("m1") or {}).get("anomaly") if pa else None
            pa_desc = (f"pa1={pa1}" if pa1 is not None else "no-pa")
            print(
                f"[snotel] {slug}: swe={record['swe_index']} "
                f"%med={record['percent_of_median']} "
                f"{record['trend']} {record['status']} "
                f"(forage {record['forage_score']}, {pr_desc}, {dr_desc}, "
                f"{sf_desc}, {sm_desc}, {pa_desc})"
            )

    print(f"[snotel] {changed} of {len(ACTIVE_COUNTIES)} county files updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
