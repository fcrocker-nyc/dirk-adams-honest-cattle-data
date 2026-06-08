"""Rangeland vegetation-response (VR) percentiles from RAP herbaceous NPP.

This module runs inside the ``vegetation-update`` GitHub Actions workflow,
which authenticates to Google Earth Engine with a service account and writes
``vegetation.json`` for the forage model to consume.

Approach
--------
The vegetation-response signal (VR, 0-100) is a *percentile rank* of the
current season's herbaceous productivity against the county's own history.

For each year in a rolling 10-year baseline (most recent
BASELINE_YEARS, inclusive of the current year) we:

  1. Sum the herbaceous net-primary-production bands ``afgNPP`` (annual forbs
     and grasses) + ``pfgNPP`` (perennial forbs and grasses) from the RAP
     16-day partitioned-NPP product.
  2. Accumulate that sum over the growing-season window Apr 1 -> the current
     day-of-year (so every year is compared over the same elapsed window).
  3. Mask to rangeland using NLCD land-cover classes 52 (shrubland) and
     71 (grassland/herbaceous).
  4. Reduce to the county mean.

The current year is then percentile-ranked against that 10-year
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

# Rangeland mask: NLCD shrubland (52) + grassland/herbaceous (71) ONLY.
# Pasture/hay (81) and cultivated crops (82) are deliberately EXCLUDED so
# that irrigated hay and cropland never inflate the rangeland VR signal.
RANGELAND_NLCD_CLASSES = [52, 71]

# Irrigated hay/pasture mask: NLCD class 81 only. Reported as a SEPARATE
# sub-layer (never folded into VR or the forage score).
HAY_PASTURE_NLCD_CLASS = 81

# Growing-season window start (month, day). Apr 1.
SEASON_START = (4, 1)

# Percentile baseline: rolling window of the most recent BASELINE_YEARS
# (inclusive of the current year). RAP_FIRST_YEAR is the hard floor so we
# never request composites from before the RAP record begins (1986).
RAP_FIRST_YEAR = 1986
BASELINE_YEARS = 10

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


def _nlcd_landcover(ee):
    """Most recent NLCD annual land-cover image.

    NLCD is published less often than RAP, so we use the latest available
    annual release.
    """
    nlcd = (ee.ImageCollection("USGS/NLCD_RELEASES/2021_REL/NLCD")
            .select("landcover"))
    img = nlcd.sort("system:time_start", False).first()
    return ee.Image(img).select("landcover")


def _class_mask(ee, classes):
    """Boolean mask for the given NLCD land-cover class value(s)."""
    lc = _nlcd_landcover(ee)
    cls_list = classes if isinstance(classes, (list, tuple)) else [classes]
    mask = lc.eq(cls_list[0])
    for cls in cls_list[1:]:
        mask = mask.Or(lc.eq(cls))
    return mask


def _rangeland_mask(ee, year: int):
    """Boolean rangeland mask (NLCD 52 + 71 only)."""
    return _class_mask(ee, RANGELAND_NLCD_CLASSES)


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
    summed = herb.sum().rename("herbNPP")
    # RAP's 16-day NPP is published with a lag, so the current (and any not-
    # yet-released) year returns an empty collection -> a 0-band image, which
    # would crash updateMask. Guarantee a single "herbNPP" band: when the
    # window is empty, return a fully-masked constant so the downstream
    # reduceRegion yields None for that year (handled as "no data") instead
    # of throwing and poisoning the whole server-side series.
    empty = ee.Image.constant(0).rename("herbNPP").updateMask(ee.Image(0))
    return ee.Image(ee.Algorithms.If(coll.size().gt(0), summed, empty))


def _county_mean(ee, image, geom, mask):
    return (image.updateMask(mask)
            .reduceRegion(reducer=ee.Reducer.mean(),
                          geometry=geom, scale=30, maxPixels=1e10,
                          tileScale=4)
            .get("herbNPP"))


def _year_window(today: dt.date) -> list[int]:
    """Rolling percentile baseline: the most recent BASELINE_YEARS, inclusive
    of the current year, floored at RAP_FIRST_YEAR so we never request
    composites from before the RAP record begins."""
    first = max(RAP_FIRST_YEAR, today.year - (BASELINE_YEARS - 1))
    return list(range(first, today.year + 1))


def compute_county_vr(ee, fips5: str, geom, today: dt.date) -> dict | None:
    """Compute the VR percentile for one county.

    ``geom`` is an ee.Geometry for the county. Always returns a dict carrying
    ``vr`` (the 0-100 percentile, or None) plus ``vr_status`` and ``vr_note``
    that disclose to the reader exactly why VR is null when it is (e.g. RAP
    has not yet published the current year, or too little history exists).
    """
    doy_end = today.timetuple().tm_yday
    years = _year_window(today)

    # Rangeland mask is year-independent (latest NLCD); build it once.
    mask = _rangeland_mask(ee, today.year)

    def _year_mean(y):
        y = ee.Number(y).toInt()
        img = _herb_npp_image(ee, y, doy_end)
        val = (img.updateMask(mask)
               .reduceRegion(reducer=ee.Reducer.mean(),
                             geometry=geom, scale=30, maxPixels=1e10,
                             tileScale=4)
               .get("herbNPP"))
        return ee.Feature(None, {"year": y, "val": val})

    # One server-side graph for the whole window -> a single getInfo().
    fc = ee.FeatureCollection(ee.List(years).map(_year_mean)).getInfo()

    series: dict[int, float] = {}
    for feat in fc["features"]:
        props = feat["properties"]
        v = props.get("val")
        if v is not None:
            series[int(props["year"])] = float(v)

    cur = series.get(today.year)
    history = [v for y, v in series.items() if y != today.year]

    # Provenance shared by every return path so the reader always knows what
    # the VR number means -- or, when it is null, exactly why.
    base = {
        "fips5": fips5,
        "vr": None,
        "n_years": len(history),
        "window": f"{SEASON_START[0]:02d}-{SEASON_START[1]:02d}..doy{doy_end}",
        "asset": RAP_NPP_16DAY,
        "bands": HERB_NPP_BANDS,
        "nlcd_classes": RANGELAND_NLCD_CLASSES,
        "baseline_years": BASELINE_YEARS,
    }

    if cur is None:
        # RAP publishes its 16-day NPP with a lag, so early in the season the
        # current year has no composites yet. VR (current-vs-history) cannot
        # exist until that data lands; the forage model falls back to the soil
        # proxy in the meantime.
        base["vr_status"] = "awaiting_current_year_rap"
        base["vr_note"] = (
            f"RAP {RAP_NPP_16DAY.split('/')[-1]} has not yet published "
            f"{today.year} composites for the Apr 1..doy{doy_end} window, so "
            "the current-season value needed for a percentile is unavailable. "
            "VR will populate automatically once RAP releases the data; until "
            "then the score uses the soil-moisture proxy."
        )
        return base

    if len(history) < 5:
        base["vr_status"] = "insufficient_history"
        base["current_npp"] = round(cur, 2)
        base["vr_note"] = (
            f"Only {len(history)} prior year(s) of RAP data fall in the "
            f"{BASELINE_YEARS}-year baseline window -- too few for a stable "
            "percentile (minimum 5)."
        )
        return base

    vr = _percentile_midrank(cur, history)
    base["vr"] = round(vr, 1)
    base["n_years"] = len(history) + 1
    base["current_npp"] = round(cur, 2)
    base["vr_status"] = "ok"
    base["vr_note"] = (
        f"Percentile rank of {today.year} season-to-date herbaceous NPP "
        f"against the prior {len(history)} years (mid-rank; resolution is "
        f"limited by the {BASELINE_YEARS}-year baseline)."
    )
    return base


def _ndvi_season_mean(ee, year: int, doy_end: int, geom, mask):
    """County-mean MODIS NDVI over Apr 1..doy_end for ``year`` within mask.

    MODIS/061/MOD13Q1 NDVI is scaled by 1e4 in the source; we divide so the
    returned value is a conventional -1..1 NDVI. Returns a server-side number
    (may be None if no pixels fall in the mask).
    """
    start = ee.Date.fromYMD(year, SEASON_START[0], SEASON_START[1])
    end = ee.Date.fromYMD(year, 1, 1).advance(doy_end, "day")
    coll = (ee.ImageCollection(MODIS_NDVI)
            .filterDate(start, end)
            .select("NDVI"))
    ndvi = coll.mean().multiply(0.0001).rename("ndvi")
    return (ndvi.updateMask(mask)
            .reduceRegion(reducer=ee.Reducer.mean(),
                          geometry=geom, scale=250, maxPixels=1e10,
                          tileScale=4)
            .get("ndvi"))


# Minimum class-81 pixel count for a county to get a hay/pasture layer.
# Below this we treat the irrigated-ag footprint as negligible (dryland east)
# and emit null. NLCD is 30 m, so ~500 px ~= 0.45 sq km.
HAY_PASTURE_MIN_PIXELS = 500


def compute_county_hay_pasture(ee, fips5: str, geom, today: dt.date) -> dict | None:
    """Irrigated hay/pasture (NLCD 81) greenness percentile via MODIS NDVI.

    SEPARATE from VR: this is reported on its own and never folded into the
    rangeland forage score. RAP is rangeland-focused and is typically null
    over cultivated pasture/hay, so MODIS NDVI is the primary signal here and
    a null RAP percentile never blocks the layer.

    Returns None for counties whose class-81 footprint is negligible.
    """
    hp_mask = _class_mask(ee, HAY_PASTURE_NLCD_CLASS)

    # Negligible-area gate: count class-81 pixels in the county.
    px = (hp_mask.selfMask()
          .reduceRegion(reducer=ee.Reducer.count(),
                        geometry=geom, scale=30, maxPixels=1e10,
                        tileScale=4)
          .get("landcover"))
    n_px = px.getInfo() if px is not None else 0
    if not n_px or n_px < HAY_PASTURE_MIN_PIXELS:
        return None

    doy_end = today.timetuple().tm_yday
    years = _year_window(today)

    def _year_ndvi(y):
        y = ee.Number(y).toInt()
        val = _ndvi_season_mean(ee, y, doy_end, geom, hp_mask)
        return ee.Feature(None, {"year": y, "val": val})

    # One server-side graph for the whole window -> a single getInfo().
    fc = ee.FeatureCollection(ee.List(years).map(_year_ndvi)).getInfo()

    series: dict = {}
    for feat in fc["features"]:
        props = feat["properties"]
        v = props.get("val")
        if v is not None:
            series[int(props["year"])] = float(v)

    cur = series.get(today.year)
    history = [v for y, v in series.items() if y != today.year]
    if cur is None or len(history) < 5:
        ndvi_pctl = None
        n_years = len(history) + (1 if cur is not None else 0)
    else:
        ndvi_pctl = round(_percentile_midrank(cur, history), 1)
        n_years = len(history) + 1

    return {
        "ndvi_pctl": ndvi_pctl,
        "rap_pctl": None,  # RAP is rangeland-focused; null over cultivated ag
        "n_years": n_years,
        "asof": today.isoformat(),
        "nlcd_class": HAY_PASTURE_NLCD_CLASS,
        "signal": "modis_ndvi",
    }


def write_vegetation_json(records: dict, today: dt.date) -> None:
    """Write vegetation.json keyed by county slug."""
    out = {
        "updated": today.isoformat(),
        "source": "RAP npp-partitioned-16day-v3 (afgNPP+pfgNPP)",
        "method": ("season-to-date herbaceous NPP percentile vs the county's "
                   "own rolling " + str(BASELINE_YEARS) + "-year RAP baseline"),
        "vr_data_note": ("VR is the percentile rank of this year's "
                         "season-to-date rangeland (NLCD 52+71) herbaceous "
                         "productivity against the prior " + str(BASELINE_YEARS - 1) +
                         " years. RAP's 16-day NPP is released with a lag, so "
                         "early each season the current year has no data yet "
                         "and VR is reported as null with a vr_status of "
                         "'awaiting_current_year_rap'; the forage score uses "
                         "the soil-moisture proxy until RAP publishes. Each "
                         "county also carries vr_status and vr_note explaining "
                         "any null."),
        "resolution_note": ("Percentiles are computed over a " +
                            str(BASELINE_YEARS) + "-year window, so their "
                            "resolution is coarse (~" +
                            str(round(100 / BASELINE_YEARS)) +
                            " points); read them as broad bands, not exact "
                            "ranks."),
        "hay_pasture_note": ("Irrigated Hay/Pasture (NLCD 81) -- NDVI greenness "
                             "vs local normal (beta); separate from the "
                             "Rangeland Forage Condition score, never folded "
                             "into VR/mi/score."),
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



# --------------------------------------------------------------------------
# Earth Engine runner (invoked by the vegetation-update workflow).
# --------------------------------------------------------------------------

# US Census TIGER counties in Earth Engine; county geometry is selected by
# 5-digit GEOID (state FIPS 30 for Montana + 3-digit county code).
TIGER_COUNTIES = "TIGER/2018/Counties"


def _county_geom(ee, fips5: str):
    """ee.Geometry for the county whose GEOID == fips5."""
    fc = (ee.FeatureCollection(TIGER_COUNTIES)
          .filter(ee.Filter.eq("GEOID", fips5)))
    return fc.first().geometry()


def run_all(verbose: bool = False) -> int:
    """Compute VR for every active county and write vegetation.json.

    Slug->FIPS comes from update_snotel.COUNTY_FIPS (the single source of
    truth). Earth Engine auth failures propagate so the workflow stops with
    a clear EE error rather than writing a partial/empty file.
    """
    ee = _ee()  # raises on auth failure -> workflow surfaces EE error
    if verbose:
        print("[vegetation] Earth Engine initialised")

    # Import lazily so the module's pure-Python helpers stay importable
    # without pulling in the full updater.
    from update_snotel import COUNTY_FIPS

    today = dt.date.today()
    records: dict = {}
    for slug, fips5 in COUNTY_FIPS.items():
        geom = None
        try:
            geom = _county_geom(ee, fips5)
            rec = compute_county_vr(ee, fips5, geom, today)
        except Exception as exc:  # noqa: BLE001 - per-county fail-soft
            print(f"[vegetation] {slug} ({fips5}) VR failed: "
                  f"{type(exc).__name__}: {exc}")
            rec = None

        # Irrigated hay/pasture (NLCD 81) is a SEPARATE sub-layer computed
        # independently of VR; a null RAP/VR never blocks it. Reuse the
        # same county geometry.
        hay = None
        if geom is not None:
            try:
                hay = compute_county_hay_pasture(ee, fips5, geom, today)
            except Exception as exc:  # noqa: BLE001 - per-county fail-soft
                print(f"[vegetation] {slug} ({fips5}) hay_pasture failed: "
                      f"{type(exc).__name__}: {exc}")
                hay = None

        # rec is always a dict from compute_county_vr (carrying vr + status +
        # note), or None if the EE call hard-errored. A county is emitted if
        # it has either a VR record or a hay/pasture record.
        if rec is None and hay is None:
            if verbose:
                print(f"[vegetation] {slug}: no VR record, no hay/pasture")
            continue

        if rec is not None:
            out_rec = dict(rec)
        else:
            out_rec = {
                "vr": None,
                "vr_status": "compute_error",
                "vr_note": "RAP VR computation raised an error for this "
                           "county; see the workflow log. The score falls "
                           "back to the soil-moisture proxy.",
            }
        if hay is not None:
            out_rec["hay_pasture"] = hay
        records[slug] = out_rec
        if verbose:
            if rec is not None:
                vr_txt = rec["vr"] if rec["vr"] is not None else f"--({rec.get('vr_status')})"
            else:
                vr_txt = "--(compute_error)"
            hp_txt = hay["ndvi_pctl"] if hay is not None else "--"
            print(f"[vegetation] {slug}: vr={vr_txt} hay_ndvi_pctl={hp_txt}")

    write_vegetation_json(records, today)
    if verbose:
        print(f"[vegetation] wrote {OUT_PATH} with {len(records)} counties")
    return 0

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
    import sys
    if "--run" in sys.argv:
        verbose = "--verbose" in sys.argv
        raise SystemExit(run_all(verbose=verbose))
    _self_test()
