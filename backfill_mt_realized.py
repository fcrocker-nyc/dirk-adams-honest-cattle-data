#!/usr/bin/env python3
"""
backfill_mt_realized.py
=======================

Builds a DEEP realized Montana calf-price series for the forecast accuracy
backtest, from the AMS Market News API (marsapi) report AMS_1778 (Montana
Weekly Livestock Auction Summary). The daily auction pipeline only accumulates
mt_weekly going forward; this pulls the full historical line-item archive so
the ~6 machine-readable forecasts can be scored against many realized weeks.

For each report week it head-weights the feeder-steer Medium-and-Large-1 rows
into the tight calf bands (550–599 / 600–649 lb by avg_weight) and writes
auction/mt_realized_history.json — same record shape as auction/history.json
mt_weekly entries, so forecast_accuracy.py reads it directly.

Auth: AMS API key from env AMSKEY (basic auth, key as username, empty password).
Local runs where urllib SSL is blocked: fetch with
    curl -s -u "$AMSKEY:" https://marsapi.ams.usda.gov/services/v1.2/reports/1778 -o /tmp/ams1778.json
then run with --input /tmp/ams1778.json.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

MARS = "https://marsapi.ams.usda.gov/services/v1.2/reports/1778"
BANDS = {"550_599": (550, 600), "600_649": (600, 650)}
MIN_YEAR = 2024  # keep the series compact; forecasts only exist in 2026


def fetch_via_api() -> dict:
    key = os.environ.get("AMSKEY", "")
    if not key:
        sys.exit("AMSKEY not set. Either export it or pass --input <curl'd json>.")
    req = urllib.request.Request(MARS, headers={"Authorization": _basic(key)})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _basic(key: str) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


def to_iso(mdy: str) -> str | None:
    try:
        return dt.datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def band_for(wt) -> str | None:
    if wt is None:
        return None
    for name, (lo, hi) in BANDS.items():
        if lo <= wt < hi:
            return name
    return None


def build(raw: dict) -> list[dict]:
    rows = raw.get("results", raw if isinstance(raw, list) else [])
    # week -> band -> [(price, head)]
    acc: dict[str, dict[str, list[tuple[float, int]]]] = {}
    for r in rows:
        if str(r.get("commodity", "")).lower() != "feeder cattle":
            continue
        if str(r.get("class", "")).lower() != "steers":
            continue
        if str(r.get("muscle_grade", "")).strip() != "1":
            continue
        if "medium and large" not in str(r.get("frame", "")).lower():
            continue
        band = band_for(r.get("avg_weight"))
        if not band:
            continue
        price, head = r.get("avg_price"), r.get("head_count")
        if price is None or not head:
            continue
        # Anchor on the publish-Monday = report_end_date (Saturday) + 2 days.
        # That matches the weekly forecast's as_of (forecast drops the Monday
        # after the report week ends) and the history.json convention, so
        # realized weeks pair with the right forecast.
        end = to_iso(r.get("report_end_date"))
        iso = None
        if end:
            iso = (dt.date.fromisoformat(end) + dt.timedelta(days=2)).isoformat()
        else:
            iso = to_iso(r.get("report_date"))
        if not iso or int(iso[:4]) < MIN_YEAR:
            continue
        acc.setdefault(iso, {}).setdefault(band, []).append((float(price), int(head)))

    records = []
    for iso in sorted(acc):
        steers = {}
        for band, lots in acc[iso].items():
            total_head = sum(h for _p, h in lots)
            if total_head <= 0:
                continue
            wavg = sum(p * h for p, h in lots) / total_head
            steers[band] = {"head": total_head, "wtd_avg_price": round(wavg, 2)}
        if steers:
            records.append({
                "source_key": "mt_weekly",
                "sale_date": iso,
                "summary": {"steers": steers},
            })
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", type=Path, help="pre-fetched marsapi JSON (else fetch via AMSKEY)")
    ap.add_argument("--out", type=Path, default=Path("auction/mt_realized_history.json"))
    args = ap.parse_args()

    raw = json.loads(args.input.read_text()) if args.input else fetch_via_api()
    records = build(raw)
    payload = {
        "source": "AMS_1778 Montana Weekly Livestock Auction Summary (marsapi)",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": "Feeder steer M&L 1, head-weighted by avg_weight band. Realized "
                "actuals for the forecast accuracy backtest (forecast_accuracy.py).",
        "records": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    spans = [r["sale_date"] for r in records]
    print(f"[backfill] {args.out}: {len(records)} weeks "
          f"({spans[0]}→{spans[-1] if spans else '-'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
