#!/usr/bin/env python3
"""
forecast_accuracy.py
====================

Scores the Honest Cattle quarterly calf-price forecast against realized
Montana auction prices and writes auction/forecast_accuracy.json.

It pairs each realized weekly observation with the HC forecast that was *in
effect* for that week (the most recent forecast published on/before the sale,
the quarter containing the sale date, the matching weight band), then reports:

    n            paired observations (per band + overall)
    bias         mean(actual − forecast)        $/cwt   (sign = over/under-forecast)
    MPE          mean((actual − forecast)/actual) × 100  %  (signed % error)
    MAD / MAE    mean(|actual − forecast|)       $/cwt
    MAPE         mean(|actual − forecast|/actual) × 100  %
    MSE / RMSE   mean(err²) / sqrt              $/cwt²,  $/cwt
    calibration  OLS actual = a + b·forecast → slope, intercept, R²

The forecast history defaults to the local rolling file (forecasts_recent.json,
typically 4 weeks). Pass --fetch to pull the full Weekly-Forecasts post archive
from the WP REST API (reuses the update_forecasts parser) for a deeper backtest
— that path is what the CI job uses.

NOTE ON SAMPLE SIZE: early on n is small and the metrics are indicative, not
statistical. Output carries `sufficient` flags (n >= MIN_N) so the weekly
forecast section can hedge honestly until the sample matures.

Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

MIN_N = 8                         # below this, label metrics "indicative"
MIN_HEAD = 20                     # skip thin realized bands (noisy wtd-avg)
REALIZED_SOURCE = "mt_weekly"     # statewide MT auction = the forecast's basis
BANDS = ["550_599", "600_649"]    # tight calf bands (v1.6)


# --------------------------------------------------------------------------- io
def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def quarter_of(iso: str) -> str | None:
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return None
    return f"Q{(d.month - 1)//3 + 1} {d.year}"


# --------------------------------------------------------------------------- forecast history
def forecast_history(repo: Path, fetch: bool) -> list[dict]:
    """Return [{as_of, quarters:[{label, steer, heifer, bands?}]}] newest-first."""
    if fetch:
        try:
            import update_forecasts as uf  # same dir; reuses the WP table parser
            posts = uf.fetch_recent_posts(limit=52)  # ~1 yr of weeklies
            weeks = [w for w in (uf.post_to_week(p) for p in posts) if w]
            if weeks:
                weeks.sort(key=lambda w: w["as_of"], reverse=True)
                return weeks
        except Exception as exc:  # noqa: BLE001
            print(f"[accuracy] WP fetch failed ({exc}); using local file", file=sys.stderr)
    local = load_json(repo / "forecasts_recent.json") or {}
    return local.get("weeks", [])


def forecast_in_effect(weeks: list[dict], sale_date: str) -> dict | None:
    """Latest forecast published on/before sale_date, quarter containing it."""
    q = quarter_of(sale_date)
    if not q:
        return None
    candidates = [w for w in weeks if w.get("as_of", "") <= sale_date]
    candidates.sort(key=lambda w: w["as_of"], reverse=True)
    for w in candidates:
        for quarter in w.get("quarters", []):
            if quarter.get("label") == q:
                return {"as_of": w["as_of"], "quarter": quarter}
    return None


def forecast_mid(quarter: dict, band: str, sex: str = "steer") -> float | None:
    """Band-specific mid if the v1.6 split is present, else the blended mid."""
    bands = quarter.get("bands")
    if isinstance(bands, dict) and bands.get(band):
        b = (bands[band] or {}).get(sex) or {}
        if b.get("mid") is not None:
            return float(b["mid"])
    flat = quarter.get(sex) or {}
    return float(flat["mid"]) if flat.get("mid") is not None else None


# --------------------------------------------------------------------------- realized actuals
def realized_observations(repo: Path) -> list[dict]:
    """[{date, band, actual, head}] of mt_weekly feeder-steer wtd-avg prices.

    Prefers the deep AMS_1778 backfill (mt_realized_history.json, built by
    backfill_mt_realized.py) and merges in any newer weeks from the daily
    history.json, deduped by (date, band) — backfill wins on overlap.
    """
    records: list[dict] = []
    deep = load_json(repo / "auction" / "mt_realized_history.json") or {}
    deep_records = deep.get("records", [])
    records += deep_records
    # Only add history.json weeks NEWER than the deep series covers — the two
    # sources are the same AMS_1778 report and label the same week differently
    # (Monday report_date vs the PDF's parsed date), so overlapping them would
    # double-count a week under two labels.
    latest_deep = max((r.get("sale_date", "") for r in deep_records), default="")
    records += [r for r in (load_json(repo / "auction" / "history.json") or [])
                if r.get("source_key") == REALIZED_SOURCE
                and (r.get("sale_date", "") > latest_deep)]

    seen: set[tuple[str, str]] = set()
    obs = []
    for r in records:  # backfill records come first → win on dedup
        date = r.get("sale_date")
        steers = (r.get("summary") or {}).get("steers") or {}
        for band in BANDS:
            key = (date, band)
            if key in seen:
                continue
            cell = steers.get(band)
            if cell and cell.get("wtd_avg_price") and (cell.get("head") or 0) >= MIN_HEAD:
                seen.add(key)
                obs.append({"date": date, "band": band,
                            "actual": float(cell["wtd_avg_price"]), "head": cell.get("head")})
    return obs


# --------------------------------------------------------------------------- metrics
def error_metrics(pairs: list[tuple[float, float]]) -> dict:
    """pairs = [(actual, forecast)]."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    errs = [a - f for a, f in pairs]
    mpe = sum((a - f) / a for a, f in pairs if a) / n * 100
    mape = sum(abs(a - f) / a for a, f in pairs if a) / n * 100
    mad = sum(abs(e) for e in errs) / n
    mse = sum(e * e for e in errs) / n
    bias = sum(errs) / n
    return {
        "n": n,
        "bias_cwt": round(bias, 2),
        "mpe_pct": round(mpe, 2),
        "mad_cwt": round(mad, 2),
        "mape_pct": round(mape, 2),
        "mse": round(mse, 2),
        "rmse_cwt": round(math.sqrt(mse), 2),
        "sufficient": n >= MIN_N,
    }


def calibration_regression(pairs: list[tuple[float, float]]) -> dict | None:
    """OLS actual = a + b·forecast. slope b≈1 & intercept≈0 = well-calibrated."""
    n = len(pairs)
    if n < 3:
        return {"n": n, "note": "need >=3 pairs for a regression"}
    xs = [f for _a, f in pairs]   # forecast = predictor
    ys = [a for a, _f in pairs]   # actual = response
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        return {"n": n, "note": "no variance in forecast values"}
    b = sxy / sxx
    a = my - b * mx
    r2 = (sxy * sxy) / (sxx * syy) if syy else None
    return {
        "n": n,
        "slope": round(b, 3),
        "intercept_cwt": round(a, 2),
        "r_squared": round(r2, 3) if r2 is not None else None,
        "sufficient": n >= MIN_N,
    }


# --------------------------------------------------------------------------- build
def build(repo: Path, fetch: bool) -> dict:
    weeks = forecast_history(repo, fetch)
    obs = realized_observations(repo)

    paired: list[dict] = []
    for o in obs:
        fe = forecast_in_effect(weeks, o["date"])
        if not fe:
            continue
        f = forecast_mid(fe["quarter"], o["band"], "steer")
        if f is None:
            continue
        paired.append({
            "date": o["date"], "band": o["band"], "quarter": fe["quarter"]["label"],
            "forecast_as_of": fe["as_of"], "forecast": f, "actual": o["actual"],
            "error_cwt": round(o["actual"] - f, 2),
        })

    by_band = {}
    for band in BANDS:
        bp = [(p["actual"], p["forecast"]) for p in paired if p["band"] == band]
        by_band[band] = {**error_metrics(bp), "calibration": calibration_regression(bp)}
    allp = [(p["actual"], p["forecast"]) for p in paired]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "realized_source": REALIZED_SOURCE,
        "forecast_field": "steer mid, $/cwt",
        "min_n_for_significance": MIN_N,
        "overall": {**error_metrics(allp), "calibration": calibration_regression(allp)},
        "by_band": by_band,
        "n_forecasts_available": len(weeks),
        "n_realized_obs": len(obs),
        "pairs": sorted(paired, key=lambda p: (p["date"], p["band"])),
        "note": (
            "Metrics pair each realized mt_weekly steer wtd-avg with the HC quarterly "
            "forecast mid in effect that week. Driver regression (calf price ~ "
            "Choice-Select / feeder-corn / PTI / slaughter) is the next stage once a "
            "signifier time-series is logged. Treat metrics with n < "
            f"{MIN_N} as indicative."
        ),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--fetch", action="store_true", help="pull full WP forecast archive")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    payload = build(args.repo, args.fetch)
    out = args.repo / "auction" / "forecast_accuracy.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    o = payload["overall"]
    print(f"[accuracy] {out}: {o.get('n',0)} pairs "
          f"({payload['n_forecasts_available']} forecasts × {payload['n_realized_obs']} obs) | "
          f"MPE {o.get('mpe_pct','-')}% · MAD {o.get('mad_cwt','-')} · RMSE {o.get('rmse_cwt','-')} "
          f"· bias {o.get('bias_cwt','-')} | sufficient={o.get('sufficient', False)}")
    if args.verbose:
        for p in payload["pairs"]:
            print(f"   {p['date']} {p['band']} {p['quarter']}: F {p['forecast']} A {p['actual']} → {p['error_cwt']:+}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
