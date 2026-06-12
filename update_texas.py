#!/usr/bin/env python3
"""
update_texas.py
===============

Daily moisture/forage updater for the Texas county pages on honestcattle.net,
modeled on update_snotel.py (Montana). Pulls live public data, aggregates to
each Texas county, and writes one JSON file per county into ``texas/`` at the
repo root (subfolder avoids filename collisions with the Montana files —
Madison/Liberty/Hill/Jefferson exist in both states).

Texas has no NRCS SNOTEL coverage, so the Montana snowpack term is replaced by
the Texas A&M Forest Service **KBDI** (Keetch-Byram Drought Index) — the
standard Texas dryness/fire index. Everything else reuses the proven Montana
fetchers.

Data sources
------------
    1. USDA Drought Monitor (USDM)        — county drought classification (by FIPS)
    2. NOAA NCEI Climate at a Glance       — county precip: value/normal/anomaly/rank/pctavg (1/3/12-mo)
    3. Texas A&M Forest Service (TFS)      — county KBDI mean/min/max  (ArcGIS FeatureServer)
    4. USGS Water Services (NWIS)          — stream discharge + day-of-year percentile (gauge per county, optional)
    5. USDA NASS Quick Stats               — Texas pasture & range condition (statewide, weekly in season, optional)

Output schema (HC Forage TX v1.0)
---------------------------------
    {
      "county": "bell-county-texas",     # matches WordPress page slug + filename
      "name": "Bell",
      "date": "2026-06-12",
      "forage_score": 58,
      "forage_model": {"sp":60,"mi":..,"vr":..,"dc":..,"lu":50,
                       "category":"Fair","confidence":"High",
                       "model":"HC Forage TX v1.0","kbdi_mean":412.3},
      "kbdi":        {"mean":412.3,"max":540,"min":180,"date":"06-11-2026","status":"Dry"},
      "precip":      {"month_end":"2026-05",
                      "m1":  {"inches":6.86,"normal":4.38,"anomaly":2.48,"rank":111,"pctavg":156.7},
                      "m3":  {...}, "m12": {...}},
      "drought":     {"valid_end":..,"none_pct":..,"d0_pct":..,...,"worst_class":"D2"},
      "streamflow":  {"gauge_name":..,"site_no":..,"cfs":..,"percentile":..,"status":..} | null,
      "soil_moisture": null               # enrichment: TexMesonet/TxSON/SCAN, added later
    }

Every enrichment block is nullable — the script degrades gracefully if any one
source fails or lacks coverage for a county.  Stdlib only — no pip deps in CI.

Usage
-----
    python update_texas.py --out . --verbose        # writes ./texas/<slug>.json
    python update_texas.py --config texas_counties.json --out .

NASS_API_KEY (optional) is read from the environment for the range-condition
pull (free key at https://quickstats.nass.usda.gov/api/).
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Transient-network-error tuple (same policy as update_snotel.py)
# ---------------------------------------------------------------------------
TRANSIENT_NET_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    http.client.HTTPException,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    OSError,
    json.JSONDecodeError,
    ValueError,
)

USDM_BASE  = "https://usdmdataservices.unl.edu/api"
USGS_BASE  = "https://waterservices.usgs.gov/nwis"
NCEI_CAG_BASE = "https://www.ncei.noaa.gov/cag/county/mapping"
NASS_BASE  = "https://quickstats.nass.usda.gov/api/api_GET"
KBDI_QUERY = ("https://twcgis.tamu.edu/arcgis/rest/services/KBDI/"
              "KBDI_Current_Map/FeatureServer/0/query")

USER_AGENT = "honestcattle-texas-updater/1.0 (+https://honestcattle.net)"
REQUEST_TIMEOUT = 30
NASS_API_KEY = __import__("os").environ.get("NASS_API_KEY", "")

# NCEI state code for Texas (NCEI's own 2-digit system, NOT FIPS). Montana = 24.
NCEI_STATE_TX = 41

# Static per-county Structural Potential (SP) by Honest Cattle sub-region, used
# in the forage model. Gulf coastal humid > Brazos Valley bottomland > drier
# Cross-Timbers north. Overridden per county by texas_counties.json if present.
SP_BY_REGION = {"north": 55, "central": 60, "gulf": 62}

# Streamflow status bands by day-of-year percentile.
STREAMFLOW_STATUS_THRESHOLDS = [(25, "Below Normal"), (75, "Normal"), (101, "Above Normal")]

_RETRY_BACKOFF_SECONDS = (5, 15)


# ---------------------------------------------------------------------------
# HTTP helpers (retry with backoff) — copied from update_snotel.py
# ---------------------------------------------------------------------------

def _request(url: str, params: dict | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # 404 is meaningful to callers (e.g. NCEI month not published) — re-raise immediately.
            if exc.code == 404:
                raise
            last_exc = exc
        except TRANSIENT_NET_ERRORS as exc:
            last_exc = exc
        if attempt < len(_RETRY_BACKOFF_SECONDS):
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    raise last_exc if last_exc else RuntimeError(f"request failed: {url}")


def _get(url: str, params: dict | None = None) -> object:
    return json.loads(_request(url, params).decode("utf-8", "replace"))


def _get_text(url: str, params: dict | None = None) -> str:
    return _request(url, params).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# USDA Drought Monitor — county drought (by federal FIPS) — reused from MT
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
    except TRANSIENT_NET_ERRORS as exc:
        print(f"[tx] USDM fetch failed for {fips}: {exc}", file=sys.stderr)
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
    return {
        "valid_end": (latest.get("validEnd") or "")[:10] or None,
        "none_pct": _pct("none"),
        "d0_pct": d0, "d1_pct": d1, "d2_pct": d2, "d3_pct": d3, "d4_pct": d4,
        "worst_class": worst,
    }


# ---------------------------------------------------------------------------
# USGS Water Services — streamflow (by site_no) — reused from MT
# ---------------------------------------------------------------------------

def _rdb_parse(text: str) -> list[dict]:
    lines = [l for l in text.split("\n") if l and not l.startswith("#")]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
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


def _streamflow_percentile(cfs, p10, p25, p50, p75, p90) -> int | None:
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
    try:
        dv = _get(f"{USGS_BASE}/dv/", {
            "format": "json", "sites": site_no,
            "parameterCd": "00060", "siteStatus": "active",
        })
    except TRANSIENT_NET_ERRORS as exc:
        print(f"[tx] USGS dv fetch failed for {site_no}: {exc}", file=sys.stderr)
        return None
    ts = dv.get("value", {}).get("timeSeries", [])
    if not ts:
        return None
    values = ts[0].get("values", [{}])[0].get("value", [])
    if not values:
        return None
    try:
        cfs = float(values[-1].get("value"))
    except (TypeError, ValueError):
        return None
    if cfs < 0:
        return None
    try:
        rdb = _get_text(f"{USGS_BASE}/stat/", {
            "format": "rdb", "sites": site_no, "parameterCd": "00060",
            "statReportType": "daily", "statTypeCd": "p10,p25,p50,p75,p90",
        })
    except TRANSIENT_NET_ERRORS as exc:
        print(f"[tx] USGS stat fetch failed for {site_no}: {exc}", file=sys.stderr)
        return {"gauge_name": gauge_name, "site_no": site_no,
                "cfs": round(cfs, 0), "percentile": None, "status": None}

    def _num(x):
        s = str(x).strip() if x is not None else ""
        try:
            return float(s) if s else None
        except ValueError:
            return None

    percentile = None
    for row in _rdb_parse(rdb):
        try:
            if int(row.get("month_nu", 0)) != today.month or int(row.get("day_nu", 0)) != today.day:
                continue
        except (TypeError, ValueError):
            continue
        p10, p25, p50, p75, p90 = (_num(row.get(f"p{p}_va")) for p in (10, 25, 50, 75, 90))
        if p50 is None:
            break
        if p10 is None and p25 is not None: p10 = p25 * 0.6
        if p25 is None and p10 is not None: p25 = (p10 + p50) / 2
        if p75 is None: p75 = p50 * 1.3
        if p90 is None and p75 is not None: p90 = p75 * 1.3
        if None in (p10, p25, p50, p75, p90):
            break
        percentile = _streamflow_percentile(cfs, p10, p25, p50, p75, p90)
        break
    return {"gauge_name": gauge_name, "site_no": site_no, "cfs": round(cfs, 0),
            "percentile": percentile, "status": _classify_streamflow(percentile)}


# ---------------------------------------------------------------------------
# NOAA NCEI Climate at a Glance — county precip (state 41 = Texas)
# Keys are TX-<fips county>, e.g. TX-027 = Bell (FIPS 48027).
# ---------------------------------------------------------------------------

def fetch_cag_precip(today: dt.date) -> dict:
    """Return {'TX-027': {'month_end':'2026-05',
                          'm1':{inches,normal,anomaly,rank,pctavg}, 'm3':..,'m12':..}, ...}."""
    cm = today.replace(day=1)
    candidate_months = [cm, (cm - dt.timedelta(days=1)).replace(day=1)]
    periods = [("m1", 1), ("m3", 3), ("m12", 12)]
    by_county: dict[str, dict] = {}
    for period_key, n in periods:
        data = None
        month_used = None
        for cand in candidate_months:
            url = f"{NCEI_CAG_BASE}/{NCEI_STATE_TX}-pcp-{cand.strftime('%Y%m')}-{n}.json"
            try:
                data = _get(url)
                month_used = cand.strftime("%Y-%m")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                print(f"[tx] NCEI {period_key} fetch failed: {exc}", file=sys.stderr)
                break
            except TRANSIENT_NET_ERRORS as exc:
                print(f"[tx] NCEI {period_key} fetch failed: {exc}", file=sys.stderr)
                break
        if not data:
            continue
        for key, v in (data.get("data") or {}).items():
            if not isinstance(v, dict):
                continue
            entry = by_county.setdefault(key, {"month_end": month_used})

            def _num(k):
                val = v.get(k)
                try:
                    return round(float(val), 2) if val is not None else None
                except (TypeError, ValueError):
                    return None

            entry[period_key] = {
                "inches": _num("value"), "normal": _num("mean"),
                "anomaly": _num("anomaly"), "rank": v.get("rank"),
                "pctavg": _num("pctavg"),
            }
    return by_county


# ---------------------------------------------------------------------------
# Texas A&M Forest Service — county KBDI (one query, all counties)
# ---------------------------------------------------------------------------

def _kbdi_status(mean: float | None) -> str | None:
    if mean is None:
        return None
    if mean <= 150: return "Moist"
    if mean <= 300: return "Moderate"
    if mean <= 450: return "Dry"
    if mean <= 600: return "Very Dry"
    return "Severe"


def fetch_kbdi() -> dict:
    """Return {county_name_lower: {mean,max,min,date,status}} for every TX county."""
    try:
        data = _get(KBDI_QUERY, {
            "where": "1=1",
            "outFields": "NAME,KBDI_MEAN,KBDI_MAX,KBDI_MIN,DATE",
            "returnGeometry": "false",
            "f": "json",
        })
    except TRANSIENT_NET_ERRORS as exc:
        print(f"[tx] TFS KBDI fetch failed: {exc}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for feat in (data.get("features") or []):
        a = feat.get("attributes") or {}
        name = (a.get("NAME") or "").strip()
        if not name:
            continue
        mean = a.get("KBDI_MEAN")
        mean = round(float(mean), 1) if mean is not None else None
        out[name.lower()] = {
            "mean": mean,
            "max": a.get("KBDI_MAX"),
            "min": a.get("KBDI_MIN"),
            "date": a.get("DATE"),
            "status": _kbdi_status(mean),
        }
    return out


# ---------------------------------------------------------------------------
# USDA NASS — Texas pasture & range condition (statewide, optional)
# ---------------------------------------------------------------------------

def fetch_tx_nass_range_condition() -> dict | None:
    """Latest TX pasture & range condition as {vp,p,f,g,e,week_ending} or None.
    Best-effort: requires NASS_API_KEY; returns None off-season or on any error."""
    if not NASS_API_KEY:
        return None
    cats = {
        "PCT VERY POOR": "vp", "PCT POOR": "p", "PCT FAIR": "f",
        "PCT GOOD": "g", "PCT EXCELLENT": "e",
    }
    year = dt.date.today().year
    out: dict = {}
    week_ending = None
    try:
        for unit, key in cats.items():
            rows = _get(NASS_BASE, {
                "key": NASS_API_KEY,
                "source_desc": "SURVEY",
                "commodity_desc": "PASTURE & RANGE",
                "statisticcat_desc": "CONDITION",
                "unit_desc": unit,
                "state_alpha": "TX",
                "year": str(year),
                "format": "JSON",
            }).get("data", [])
            if not rows:
                continue
            rows.sort(key=lambda r: r.get("week_ending") or "")
            latest = rows[-1]
            try:
                out[key] = float(latest.get("Value"))
            except (TypeError, ValueError):
                continue
            week_ending = latest.get("week_ending") or week_ending
    except TRANSIENT_NET_ERRORS as exc:
        print(f"[tx] NASS fetch failed: {exc}", file=sys.stderr)
        return None
    if len(out) < 3:
        return None
    out["week_ending"] = week_ending
    return out


# ---------------------------------------------------------------------------
# HC Forage Score — Texas variant (KBDI replaces snowpack term)
# HC = 0.20(SP) + 0.25(MI) + 0.25(VR) + 0.15(DC) + 0.15(LU)
# ---------------------------------------------------------------------------

def _precip_component(pct_normal: float | None) -> float:
    if pct_normal is None: return 50.0
    if pct_normal >= 120: return 95.0
    if pct_normal >= 100: return 75.0
    if pct_normal >= 90:  return 60.0
    if pct_normal >= 80:  return 45.0
    if pct_normal >= 70:  return 30.0
    if pct_normal >= 60:  return 15.0
    return 5.0


def _kbdi_component(kbdi_mean: float | None) -> float:
    """Higher KBDI = drier soil = lower forage potential. 0–800 scale inverted."""
    if kbdi_mean is None: return 50.0
    if kbdi_mean <= 100: return 92.0
    if kbdi_mean <= 200: return 78.0
    if kbdi_mean <= 300: return 60.0
    if kbdi_mean <= 400: return 45.0
    if kbdi_mean <= 500: return 30.0
    if kbdi_mean <= 600: return 18.0
    return 8.0


def _basin_precip_component(pct: float | None) -> float:
    if pct is None: return 50.0
    if pct >= 125: return 90.0
    if pct >= 100: return 70.0
    if pct >= 85:  return 55.0
    if pct >= 70:  return 35.0
    if pct >= 55:  return 15.0
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


def _soil_vr_proxy(soil_moisture: dict | None, precip: dict | None) -> float | None:
    """Vegetation-response proxy. Soil moisture is null for TX v1 (enrichment),
    so this leans on the recent precip anomaly until TexMesonet/TxSON is wired."""
    components, weights = [], []
    if soil_moisture:
        shallow = soil_moisture.get("shallow_vwc_pct")
        if shallow is not None:
            sv = (85.0 if shallow >= 35 else 70.0 if shallow >= 28 else
                  55.0 if shallow >= 22 else 40.0 if shallow >= 16 else
                  25.0 if shallow >= 10 else 10.0)
            components.append(sv); weights.append(0.65)
    if precip:
        m3 = precip.get("m3") or {}
        m1 = precip.get("m1") or {}
        pctavg = m3.get("pctavg") if m3.get("pctavg") is not None else m1.get("pctavg")
        if pctavg is not None:
            pv = (85.0 if pctavg >= 120 else 70.0 if pctavg >= 100 else
                  55.0 if pctavg >= 85 else 40.0 if pctavg >= 70 else
                  25.0 if pctavg >= 50 else 10.0)
            components.append(pv); weights.append(0.35)
    if not components:
        return None
    return sum(c * w for c, w in zip(components, weights)) / sum(weights)


def _streamflow_adjustment(streamflow: dict | None) -> float:
    if not streamflow:
        return 0.0
    pctl = streamflow.get("percentile")
    if pctl is None: return 0.0
    if pctl >= 75: return 5.0
    if pctl >= 50: return 2.0
    if pctl >= 25: return 0.0
    if pctl >= 10: return -3.0
    return -5.0


def _forage_category(score: int) -> str:
    if score >= 80: return "Excellent"
    if score >= 65: return "Good"
    if score >= 50: return "Fair"
    if score >= 35: return "Poor"
    return "Very Poor"


def _nass_condition_score(nass: dict) -> float:
    return (nass.get("vp", 0)*0.05 + nass.get("p", 0)*0.25 + nass.get("f", 0)*0.55
            + nass.get("g", 0)*0.80 + nass.get("e", 0)*1.00)


def forage_score(sp: float, kbdi: dict | None, precip: dict | None,
                 drought: dict | None, streamflow: dict | None,
                 soil_moisture: dict | None,
                 nass_condition: dict | None) -> tuple[int, dict]:
    m1 = (precip or {}).get("m1") or {}
    m12 = (precip or {}).get("m12") or {}
    precip_pct = m1.get("pctavg")
    basin_pct = m12.get("pctavg")
    kbdi_mean = (kbdi or {}).get("mean")

    mi = (0.45 * _precip_component(precip_pct) +
          0.35 * _kbdi_component(kbdi_mean) +
          0.20 * _basin_precip_component(basin_pct))

    vr_val = _soil_vr_proxy(soil_moisture, precip)
    vr = vr_val if vr_val is not None else 50.0

    nass_score, nass_live = 50.0, False
    if nass_condition and all(k in nass_condition for k in ("vp", "p", "f", "g", "e")):
        nass_score, nass_live = _nass_condition_score(nass_condition), True
    dc = 0.60 * _drought_component(drought) + 0.40 * nass_score
    lu = 50.0

    raw = 0.20*sp + 0.25*mi + 0.25*vr + 0.15*dc + 0.15*lu + _streamflow_adjustment(streamflow)
    score = max(0, min(100, int(round(raw))))

    missing = (kbdi_mean is None) + (precip_pct is None) + (vr_val is None) + (not nass_live)
    detail = {
        "sp": round(sp, 1), "mi": round(mi, 1), "vr": round(vr, 1),
        "dc": round(dc, 1), "lu": round(lu, 1),
        "category": _forage_category(score),
        "confidence": "High" if missing == 0 else ("Medium" if missing <= 1 else "Low"),
        "model": "HC Forage TX v1.0",
        "kbdi_mean": kbdi_mean,
    }
    if nass_live:
        detail["nass_week"] = nass_condition.get("week_ending")
    return score, detail


# ---------------------------------------------------------------------------
# Per-county record + main
# ---------------------------------------------------------------------------

def build_record(c: dict, today: dt.date, kbdi: dict | None, precip: dict | None,
                 drought: dict | None, streamflow: dict | None,
                 soil_moisture: dict | None, nass_condition: dict | None) -> dict:
    sp = float(c.get("structural_potential") or SP_BY_REGION.get(c.get("region"), 58))
    score, model = forage_score(sp, kbdi, precip, drought, streamflow,
                                soil_moisture, nass_condition)
    return {
        "county": c["slug"],
        "name": c["name"],
        "date": today.isoformat(),
        "forage_score": score,
        "forage_model": model,
        "kbdi": kbdi,
        "precip": precip,
        "drought": drought,
        "streamflow": streamflow,
        "soil_moisture": soil_moisture,
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", type=Path, default=Path(__file__).with_name("texas_counties.json"),
                   help="County config JSON (name, slug, fips, region, structural_potential, gauge).")
    p.add_argument("--out", type=Path, default=Path("."),
                   help="Repo root; files are written to <out>/texas/<slug>.json.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    counties = json.loads(args.config.read_text(encoding="utf-8"))
    out_dir = args.out / "texas"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()

    # ---- one-shot statewide pulls ----
    kbdi_by_name = fetch_kbdi()
    if args.verbose:
        print(f"[tx] TFS KBDI: {len(kbdi_by_name)} counties loaded")
    cag_by_county = fetch_cag_precip(today)
    if args.verbose:
        print(f"[tx] NCEI CAG: {len(cag_by_county)} TX counties loaded")
    nass_condition = fetch_tx_nass_range_condition()
    if args.verbose:
        if nass_condition:
            print(f"[tx] NASS: TX range condition week {nass_condition.get('week_ending')}")
        else:
            print(f"[tx] NASS: skipped ({'no API key' if not NASS_API_KEY else 'no data / off-season'})")

    changed = 0
    for c in counties:
        fips = c["fips"]
        try:
            drought = fetch_county_drought(fips, today)
        except TRANSIENT_NET_ERRORS as exc:
            print(f"[tx] {c['slug']}: drought failed ({type(exc).__name__}): {exc}", file=sys.stderr)
            drought = None

        gauge = c.get("gauge")
        try:
            streamflow = fetch_streamflow(gauge[0], gauge[1], today) if gauge else None
        except TRANSIENT_NET_ERRORS as exc:
            print(f"[tx] {c['slug']}: streamflow failed ({type(exc).__name__}): {exc}", file=sys.stderr)
            streamflow = None

        kbdi = kbdi_by_name.get(c["name"].lower())
        precip = cag_by_county.get(f"TX-{fips[-3:]}")
        soil_moisture = None  # enrichment (TexMesonet / TxSON / SCAN) — added later

        record = build_record(c, today, kbdi, precip, drought, streamflow,
                              soil_moisture, nass_condition)
        path = out_dir / f"{c['slug']}.json"
        new_text = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == new_text:
            if args.verbose:
                print(f"[tx] {c['slug']}: unchanged")
            continue
        path.write_text(new_text, encoding="utf-8")
        changed += 1
        if args.verbose:
            kb = (kbdi or {}).get("mean")
            dr = (drought or {}).get("worst_class")
            pa = (precip or {}).get("m1", {}).get("pctavg")
            print(f"[tx] {c['slug']}: forage {record['forage_score']} "
                  f"({record['forage_model']['category']}) "
                  f"KBDI={kb} drought={dr} precip%={pa}")

    print(f"[tx] {changed} of {len(counties)} Texas county files updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
