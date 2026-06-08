"""Rangeland vegetation-response (VR) percentiles from RAP herbaceous NPP.

This module runs inside the ``vegetation-update`` GitHub Actions workflow,
which authenticates to Google Earth Engine with a service account and writes
``vegetation.json`` for the forage model to consume.

Approach
--------
The vegetation-response signal (VR, 0-100) is a *percentile rank* of the
current season's herbaceous productivity against the county's own history.

For each year 1986..present we:

  1. Sum the herbaceous net-primary-production bands ``afgNPP`` (annual forbs
     and grasses) + ``pfgNPP`` (perennial forbs and grasses) from the RAP
     16-day partitioned-NPP product.
  2. Accumulate that sum over the growing-season window Apr 1 -> the current
     day-of-year (so every year is compared over the same elapsed window).
  3. Mask to rangeland using NLCD land-cover classes 52 (shrubland) and
     71 (grassland/herbaceous).
  4. Reduce to the county mean.

The current year is then percentile-ranked against the full historical
distribution:  vr = (rank - 0.5) / N * 100 (mid-rank, never pinned to
exactly 0 or 100).

Because percentile rank is invariant under any monotonic transform, we do
NOT convert NPP to biomass: the percentile of summed herbaceous NPP equals
the percentile of biomass. No unit conversion, no hard-coded constant.

Assets (verified against the live Earth Engine / RAP catalog):
  * RAP 16-day partitioned NPP:
        projects/rap-data-365417/assets/npp-partitioned-16day-v3
    (yearly ``npp-partitioned-v3`` is an acceptable fallback)
  * NLCD land cover:  USGS/NLCD_RELEASES/<year>_REL/...
  * MODIS NDVI (secondary QA only):  MODIS/061/MOD13Q1
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

# RAP herbaceous productivity (16-day partitioned NPP, v3).
RAP_NPP_16DAY = "projects/rap-data-365417/assets/npp-partitioned-16day-v3"
RAP_NPP_YEARLY = "projects/rap-data-365417/assets/npp-partitioned-v3"

# Herbaceous bands: annual + perennial forb-and-grass NPP. Sum them.
HERB_NPP_BANDS = ["afgNPP", "pfgNPP"]

# Rangeland mask: NLCD shrubland (52) + grassland/herbaceous (71).
# (Pasture/hay 81 intentionally excluded for now -- native rangeland only.)
RANGELAND_NLCD_CLASSES = [52, 71]

# Growing-season window start (month, day). Apr 1.
SEASON_START = (4, 1)

# Percentile baseline: full RAP record.
RAP_FIRST_YEAR = 1986

# MODIS NDVI, secondary QA signal only.
MODIS_NDVI = "MODIS/061/MOD13Q1"

OUT_PATH = Path("vegetation.json")


def _percentile_midrank(value: float, history: list[float]) -> float:
    """Mid-rank percentile of ``value`` within ``history`` (0-100).

    rank counts historical values strictly below ``value`` plus half of the
    ties, plus 0.5 for the current observation; percentile = rank / N * 100,
    clamped so it never pins to exactly 0 or 100. ``history`` EXCLUDES the
    current value; N = len(history) + 1.
    """
    n = len(history) + 1
    below = sum(1 for h in history if h < value)
    ties = sum(1 for h in history if h == value)
    rank = below + 0.5 * ties + 0.5
    pct = rank / n * 100.0
    return max(0.5, min(99.5, pct))


def _ee():
    """Import and initialise Earth Engine from the service account.

    Reads the service-account email from $GEE_SERVICE_ACCOUNT and the JSON
    key contents from $GEE_SA_KEY. Raises on failure so the workflow
    surfaces an EE-auth error and stops rather than writing an empty file.
    """
    import os
    import ee

    sa_email = os.environ["GEE_SERVICE_ACCOUNT"]
    sa_key = os.environ["GEE_SA_KEY"]
    creds = ee.ServiceAccountCredentials(sa_email, key_data=sa_key)
    ee.Initialize(creds)
    return ee


def _rangeland_mask(ee, year: int):
    """Boolean rangeland mask (NLCD 52 + 71).

    NLCD is published less often than RAP, so we use the most recent annual
    release image available.
    """
    nlcd = (ee.ImageCollection("USGS/NLCD_RELEASES/2021_REL/NLCD")
            .select("landcover"))
    img = nlcd.sort("system:time_start", False).first()
    lc = ee.Image(img).select("landcover")
    mask = lc.eq(RANGELAND_NLCD_CLASSES[0])
    for cls in RANGELAND_NLCD_CLASSES[1:]:
        mask = mask.Or(lc.eq(cls))
    return mask


def _herb_npp_image(ee, year: int, doy_end: int):
    """Season-to-date summed herbaceous NPP image for ``year``.

    Sums afgNPP+pfgNPP across the 16-day composites whose start falls in
    [Apr 1, day-of-year ``doy_end``], then sums those composites.
    """
    start = ee.Date.fromYMD(year, SEASON_START[0], SEASON_START[1])
    end = ee.Date.fromYMD(year, 1, 1).advance(doy_end, "day")
    coll = (ee.ImageCollection(RAP_NPP_16DAY)
            .filterDate(start, end)
            .select(HERB_NPP_BANDS))
    herb = coll.map(lambda im: im.reduce(ee.Reducer.sum()))
    return herb.sum().rename("herbNPP")


def _county_mean(ee, image, geom, mask):
    return (image.updateMask(mask)
            .reduceRegion(reducer=ee.Reducer.mean(),
                          geometry=geom, scale=30, maxPixels=1e10)
            .get("herbNPP"))


def compute_county_vr(ee, fips5: str, geom, today: dt.date) -> dict | None:
    """Compute the VR percentile for one county.

    ``geom`` is an ee.Geometry for the county. Returns a dict with the
    percentile and provenance, or None if RAP lacks data / history.
    """
    doy_end = today.timetuple().tm_yday
    years = list(range(RAP_FIRST_YEAR, today.year + 1))

    series: dict[int, float] = {}
    for y in years:
        img = _herb_npp_image(ee, y, doy_end)
        mask = _rangeland_mask(ee, y)
        v = _county_mean(ee, img, geom, mask).getInfo()
        if v is not None:
            series[y] = float(v)

    cur = series.get(today.year)
    history = [v for y, v in series.items() if y != today.year]
    if cur is None or len(history) < 5:
        return None

    vr = _percentile_midrank(cur, history)
    return {
        "fips5": fips5,
        "vr": round(vr, 1),
        "n_years": len(history) + 1,
        "current_npp": round(cur, 2),
        "window": f"{SEASON_START[0]:02d}-{SEASON_START[1]:02d}..doy{doy_end}",
        "asset": RAP_NPP_16DAY,
        "bands": HERB_NPP_BANDS,
        "nlcd_classes": RANGELAND_NLCD_CLASSES,
    }


def write_vegetation_json(records: dict, today: dt.date) -> None:
    """Write vegetation.json keyed by county slug."""
    out = {
        "updated": today.isoformat(),
        "source": "RAP npp-partitioned-16day-v3 (afgNPP+pfgNPP)",
        "method": "season-to-date herbaceous NPP percentile vs RAP record",
        "counties": records,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


# --------------------------------------------------------------------------
# Consumer-side helpers (imported by update_snotel.py; no Earth Engine here).
# --------------------------------------------------------------------------

FRESHNESS_DAYS = 21  # 3-week gate.


def load_vegetation(path=OUT_PATH):
    """Load vegetation.json if present; return parsed dict else None."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def vr_for_county(veg, slug: str, today: dt.date):
    """Return (vr, source) for a county from loaded vegetation data.

    Applies the 3-week freshness gate on the top-level ``updated`` timestamp:
    if vegetation.json is older than FRESHNESS_DAYS the VR is stale and None
    is returned so the caller falls back to the soil proxy.
    """
    if not veg:
        return None, "unavailable"
    updated = veg.get("updated")
    if updated:
        try:
            u = dt.date.fromisoformat(updated)
            if (today - u).days > FRESHNESS_DAYS:
                return None, "stale"
        except ValueError:
            return None, "stale"
    rec = (veg.get("counties") or {}).get(slug)
    if not rec or rec.get("vr") is None:
        return None, "unavailable"
    return float(rec["vr"]), "rap_npp"


def _self_test() -> None:
    """Self-test for the pure-Python helpers (no Earth Engine)."""
    hist = [10, 20, 30, 40]  # N=5 with current
    assert abs(_percentile_midrank(25, hist) - 50.0) < 1e-6, _percentile_midrank(25, hist)
    assert _percentile_midrank(5, hist) >= 0.5
    assert _percentile_midrank(5, hist) < 20
    assert _percentile_midrank(99, hist) <= 99.5
    assert _percentile_midrank(99, hist) > 80

    today = dt.date(2026, 6, 1)
    fresh = {"updated": "2026-05-28", "counties": {"gallatin": {"vr": 62.0}}}
    vr, src = vr_for_county(fresh, "gallatin", today)
    assert vr == 62.0 and src == "rap_npp", (vr, src)

    stale = {"updated": "2026-04-01", "counties": {"gallatin": {"vr": 62.0}}}
    vr, src = vr_for_county(stale, "gallatin", today)
    assert vr is None and src == "stale", (vr, src)

    missing = {"updated": "2026-05-28", "counties": {}}
    vr, src = vr_for_county(missing, "gallatin", today)
    assert vr is None and src == "unavailable", (vr, src)

    vr, src = vr_for_county(None, "gallatin", today)
    assert vr is None and src == "unavailable", (vr, src)

    print("forage_vegetation self-test: all checks passed")


if __name__ == "__main__":
    _self_test()
