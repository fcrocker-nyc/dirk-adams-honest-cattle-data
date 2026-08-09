#!/usr/bin/env python3
"""
feed_passthrough.py — the AMPC/Marsh feed-cost pass-through, re-estimated on current USDA
data, emitted as a keyless JSON feed for the HC Weekly Forecast (Section 3, CME Futures & Corn).

Method (Marsh): what a feedlot pays for a calf = expected fed-cattle value minus the cost of
gain (corn) minus margin. So calf price rises with the fed-cattle (output) price and falls as
corn rises; controlling for the output price isolates the corn (feed-cost) effect.

Data: USDA NASS Quick Stats monthly U.S. prices received —
  calves  = CATTLE, CALVES ($/cwt)                        (the Montana cow-calf product)
  steers  = CATTLE, STEERS & HEIFERS, GE 500 LBS ($/cwt)  (output / fed-cattle side)
  corn    = CORN, GRAIN ($/bu)                             (feed cost)

Writes auction/feed_passthrough_latest.json. The forecast runner reads it (no key, no compute)
and folds the cross-check into Section 3. Run: python3 feed_passthrough.py --emit
"""
import json, os, ssl, urllib.parse, urllib.request
from datetime import datetime, timezone
import numpy as np
try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()

KEY = os.environ.get("NASS_API_KEY", "0691EF2C-4DDB-3FE9-A995-70B703130032")
API = "https://quickstats.nass.usda.gov/api/api_GET/"
MON = {m: i for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"], 1)}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auction", "feed_passthrough_latest.json")


def nass(commodity, short_desc, y0=2000, y1=2027):
    p = {"key": KEY, "source_desc": "SURVEY", "commodity_desc": commodity,
         "statisticcat_desc": "PRICE RECEIVED", "agg_level_desc": "NATIONAL",
         "freq_desc": "MONTHLY", "format": "JSON", "year__GE": y0, "year__LE": y1}
    url = API + "?" + urllib.parse.urlencode(p)
    d = json.load(urllib.request.urlopen(url, context=CTX, timeout=90)).get("data", [])
    out = {}
    for r in d:
        if r["short_desc"] != short_desc:
            continue
        mo = MON.get(r.get("reference_period_desc", "").strip())
        if not mo:
            continue
        try:
            out[(int(r["year"]), mo)] = float(str(r["Value"]).replace(",", ""))
        except ValueError:
            pass
    return out


def _ols(X, y):
    XtXi = np.linalg.inv(X.T @ X)
    beta = XtXi @ X.T @ y
    resid = y - X @ beta
    r2 = 1 - (resid @ resid) / (((y - y.mean()) ** 2).sum())
    return beta, r2


def compute():
    calf   = nass("CATTLE", "CATTLE, CALVES - PRICE RECEIVED, MEASURED IN $ / CWT")
    steers = nass("CATTLE", "CATTLE, STEERS & HEIFERS, GE 500 LBS - PRICE RECEIVED, MEASURED IN $ / CWT")
    corn   = nass("CORN",   "CORN, GRAIN - PRICE RECEIVED, MEASURED IN $ / BU")
    keys = sorted(set(calf) & set(steers) & set(corn))
    if len(keys) < 60:
        raise RuntimeError(f"too few merged obs: {len(keys)}")
    yv = np.array([calf[k] for k in keys]); sv = np.array([steers[k] for k in keys]); cv = np.array([corn[k] for k in keys])
    ly, ls, lc = np.log(yv), np.log(sv), np.log(cv)

    # Spec A: log-levels (long-run elasticities)
    bA, r2A = _ols(np.column_stack([np.ones_like(ly), ls, lc]), ly)
    # Spec B: log first differences (one-month pass-through)
    dY, dS, dC = np.diff(ly), np.diff(ls), np.diff(lc)
    bB, _ = _ols(np.column_stack([np.ones_like(dY), dS, dC]), dY)
    # Backtest: train <=2022, predict 2023+
    tr = np.array([k[0] <= 2022 for k in keys]); te = ~tr
    btr, _ = _ols(np.column_stack([np.ones(tr.sum()), ls[tr], lc[tr]]), ly[tr])
    pred = np.exp(np.column_stack([np.ones(te.sum()), ls[te], lc[te]]) @ btr)
    mape = float(np.mean(np.abs(pred - yv[te]) / yv[te]) * 100)
    corr = float(np.corrcoef(pred, yv[te])[0, 1])

    # Current read: latest month vs 12 months earlier
    k = keys[-1]; ky = (k[0] - 1, k[1])
    cur = None
    if ky in calf:
        dcorn = float(np.log(corn[k] / corn[ky])); dst = float(np.log(steers[k] / steers[ky]))
        imp_corn = float(bA[2] * dcorn * 100); imp_out = float(bA[1] * dst * 100)
        implied = imp_corn + imp_out
        actual = float((calf[k] / calf[ky] - 1) * 100)
        cur = {"window": f"{ky[0]}-{ky[1]:02d} to {k[0]}-{k[1]:02d}",
               "corn_now": round(corn[k], 2), "corn_yoy_pct": round(dcorn * 100),
               "fedcattle_yoy_pct": round(dst * 100),
               "implied_calf_pct": round(implied, 1), "actual_calf_pct": round(actual, 1),
               "scarcity_residual_pct": round(actual - implied, 1)}

    e_corn = round(float(bA[2]), 3); e_out = round(float(bA[1]), 3); e_corn_1m = round(float(bB[2]), 3)
    prose = _prose(e_corn, e_out, cur)
    return {
        "model": "HC feed-cost pass-through v1.0 (AMPC/Marsh method, re-estimated)",
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_data_month": f"{k[0]}-{k[1]:02d}",
        "sample": f"{keys[0][0]}-{keys[0][1]:02d} to {keys[-1][0]}-{keys[-1][1]:02d} monthly (n={len(keys)})",
        "elasticity": {"corn_longrun": e_corn, "corn_one_month": e_corn_1m, "output_longrun": e_out},
        "fit": {"r2_levels": round(float(r2A), 3), "backtest_mape_pct": round(mape, 1),
                "backtest_corr": round(corr, 3), "backtest_window": "train<=2022, test 2023+"},
        "current_read": cur,
        "prose": prose,
        "attribution": ("Method from MSU Agricultural Marketing Policy Center (John Marsh); "
                        "coefficients re-estimated by Honest Cattle on current USDA data. "
                        "Descriptive association, not trading advice."),
        "sources": ["USDA NASS Quick Stats, U.S. monthly prices received: CATTLE CALVES, "
                    "CATTLE STEERS & HEIFERS GE 500 LBS, CORN GRAIN"],
    }


def _prose(e_corn, e_out, cur):
    base = (f"Feed-cost pass-through check (AMPC/Marsh method, re-estimated on 2000-present USDA "
            f"monthly data): each +10% in corn shaves about {abs(e_corn)*10:.0f}% off calf value with "
            f"fed-cattle prices held constant, while a +10% fed-cattle move lifts calves about "
            f"{e_out*10:.0f}%.")
    if cur:
        base += (f" Right now, corn near ${cur['corn_now']:.2f}/bu ({cur['corn_yoy_pct']:+d}% YoY) and "
                 f"fed cattle {cur['fedcattle_yoy_pct']:+d}% imply calves about "
                 f"{cur['implied_calf_pct']:+.0f}% vs a year ago; calves are actually running "
                 f"{cur['actual_calf_pct']:+.0f}%, leaving roughly {cur['scarcity_residual_pct']:+.0f} "
                 f"points as herd-cycle scarcity beyond what feed and fed-cattle prices explain.")
    base += (" Method borrowed from MSU AMPC (Marsh); estimates are Honest Cattle's, current, and "
             "descriptive, not advice.")
    return base


if __name__ == "__main__":
    import sys
    rec = compute()
    if "--emit" in sys.argv:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {OUT}")
    print(json.dumps(rec, ensure_ascii=False, indent=1))
