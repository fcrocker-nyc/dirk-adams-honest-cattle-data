#!/usr/bin/env python3
"""
Build moisture_outlook.json — the rolling 1/3/6/12-month precipitation outlook
for every Montana county, parsed from the published county pages so the app
and the website can never disagree.

Source of truth: the "Moisture Outlook: 1, 3, 6 and 12 Months" table rendered
on each county page at honestcattle.net. Those numbers are NOAA Climate
Prediction Center outlook odds read across the county and applied to the
county's 1991-2020 precipitation record (NOAA NCEI nClimDiv).

Columns are located BY HEADER, never by position: a column added to the
published table must not silently shift a value into the wrong field.
Every parsed county is range-checked before it is written.

Refresh: monthly, after CPC issues the new outlooks (mid-month seasonal,
end-of-month monthly).

Usage:  python3 build_moisture_outlook.py [-o moisture_outlook.json]
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timezone, datetime
from html import unescape

WP_PAGES = "https://honestcattle.net/wp-json/wp/v2/pages"
UA = "honest-cattle-data-pipeline"

# Horizon label -> month count. The published table uses these exact labels.
HORIZON_MONTHS = {"1 month": 1, "3 months": 3, "6 months": 6, "12 months": 12}

# Plausibility bounds for a Montana county (inches of precipitation).
BOUNDS = {1: (0.05, 8.0), 3: (0.3, 22.0), 6: (1.0, 40.0), 12: (3.0, 70.0)}


def strip_tags(s):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def county_slug(title):
    """'Gallatin County, Montana' -> 'gallatin' (matches the per-county JSON names)."""
    s = strip_tags(title).lower()
    s = s.split(",")[0]
    s = re.sub(r"\bcounty\b", " ", s)
    s = s.replace(" and ", " ").replace(" & ", " ").replace("'", "").replace(".", "")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", "_", s) or None


def num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group(0)) if m else None


def find_outlook_table(html):
    """Return the <table> that holds the 1/3/6/12-month outlook, or None.

    Identified by its own header row, not by document order: other tables on
    the page (the 2-week rain table in particular) must never be mistaken for it.
    """
    for m in re.finditer(r"<table[^>]*>.*?</table>", html, re.S):
        t = m.group(0)
        heads = [strip_tags(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
        if not heads:
            continue
        joined = " ".join(heads)
        if "horizon" in joined and "normal" in joined and "expected" in joined:
            return t, heads
    return None, None


def parse_county(html, slug, label, page_url):
    table, heads = find_outlook_table(html)
    if table is None:
        return None, "no outlook table"

    def col(*terms):
        for i, h in enumerate(heads):
            if all(t in h for t in terms):
                return i
        return None

    c_h = col("horizon")
    c_w = col("window")
    c_n = col("normal")
    c_e = col("expected")
    c_p = col("%", "normal") or col("percent")
    c_l = col("lean")
    c_c = col("confidence")
    # "Normal (in)" and "% of Normal" both contain "normal" — disambiguate.
    if c_n is not None and c_p is not None and c_n == c_p:
        for i, h in enumerate(heads):
            if "normal" in h and "%" not in h and "percent" not in h:
                c_n = i
                break
    missing = [n for n, v in
               (("horizon", c_h), ("normal", c_n), ("expected", c_e)) if v is None]
    if missing:
        return None, f"missing column(s): {', '.join(missing)} in {heads}"

    body = re.search(r"<tbody[^>]*>.*?</tbody>", table, re.S)
    rows = re.findall(r"<tr[^>]*>.*?</tr>", body.group(0) if body else table, re.S)

    horizons = []
    for row in rows:
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if not cells or len(cells) <= max(x for x in (c_h, c_n, c_e) if x is not None):
            continue
        hz = cells[c_h].strip().lower()
        if hz not in HORIZON_MONTHS:
            continue
        months = HORIZON_MONTHS[hz]
        normal = num(cells[c_n])
        expected = num(cells[c_e])
        if normal is None or expected is None:
            return None, f"{hz}: unreadable normal/expected"
        lo, hi = BOUNDS[months]
        for name, v in (("normal", normal), ("expected", expected)):
            if not (lo <= v <= hi):
                return None, f"{hz} {name} {v} outside {lo}-{hi} in"
        pct = num(cells[c_p]) if c_p is not None and c_p < len(cells) else None
        if pct is None:
            pct = round(expected / normal * 100) if normal else None
        if pct is not None and not (20 <= pct <= 300):
            return None, f"{hz} pct_of_normal {pct} implausible"
        horizons.append({
            "horizon": hz,
            "months": months,
            "window": cells[c_w].strip() if c_w is not None and c_w < len(cells) else None,
            "normal_in": round(normal, 2),
            "expected_in": round(expected, 2),
            "pct_of_normal": int(round(pct)) if pct is not None else None,
            "lean": cells[c_l].strip() if c_l is not None and c_l < len(cells) else None,
            "confidence": cells[c_c].strip() if c_c is not None and c_c < len(cells) else None,
        })

    got = {h["months"] for h in horizons}
    if got != {1, 3, 6, 12}:
        return None, f"expected horizons 1/3/6/12, got {sorted(got)}"
    # Cumulative windows must not shrink as the horizon lengthens.
    horizons.sort(key=lambda h: h["months"])
    for a, b in zip(horizons, horizons[1:]):
        if b["normal_in"] < a["normal_in"]:
            return None, (f"normal {b['horizon']} {b['normal_in']} < "
                          f"{a['horizon']} {a['normal_in']} (not cumulative)")

    text = strip_tags(html)
    rec = {
        "county": slug,
        "county_label": label,
        "page_url": page_url,
        "horizons": horizons,
    }

    m = re.search(r"seasonal outlook issued ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if m:
        rec["issued_seasonal"] = m.group(1)
    m = re.search(r"monthly outlook ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if m:
        rec["issued_monthly"] = m.group(1)

    def odds(pattern):
        m = re.search(pattern, text)
        if not m:
            return None
        d, n, w = (int(m.group(i)) for i in (1, 2, 3))
        return {"drier": d, "near": n, "wetter": w} if 90 <= d + n + w <= 110 else None

    o = odds(r"Odds, [^:]{0,40}?:\s*(\d+)% chance of a drier-than-normal month,\s*"
             r"(\d+)% near normal,\s*(\d+)% wetter")
    if o:
        rec["odds_1_month"] = o
    o = odds(r"Odds, [^:]{0,40}?:\s*(\d+)% drier than normal,\s*"
             r"(\d+)% near normal,\s*(\d+)% wetter")
    if o:
        rec["odds_3_month"] = o

    m = re.search(r"Spring growing season \(([^)]+)\):.*?normally bring ([\d.]+) in.*?"
                  r"(\d+)% drier, (\d+)% near normal, (\d+)% wetter.*?"
                  r"expected ([\d.]+) in \((\d+)% of normal\)", text)
    if m:
        rec["spring_growing_season"] = {
            "window": m.group(1),
            "normal_in": float(m.group(2)),
            "expected_in": float(m.group(6)),
            "pct_of_normal": int(m.group(7)),
            "drier": int(m.group(3)),
            "near": int(m.group(4)),
            "wetter": int(m.group(5)),
        }

    m = re.search(r"Temperature:\s*(.+?)(?:Spring growing season|What this means)", text)
    if m:
        rec["temperature_note"] = m.group(1).strip()[:400]

    return rec, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="moisture_outlook.json")
    ap.add_argument("--min-counties", type=int, default=40,
                    help="refuse to write a feed thinner than this")
    args = ap.parse_args()

    pages = []
    for p in range(1, 5):
        try:
            batch = get_json(f"{WP_PAGES}?per_page=100&page={p}&_fields=id,slug,title,link")
        except urllib.error.HTTPError:
            break
        if not batch:
            break
        pages += batch

    cands = [p for p in pages
             if "county" in p["slug"] and "texas" not in p["slug"]
             and "water-rights" not in p["slug"]
             and "counties" not in p["slug"]]

    out, failures, seen = {}, [], set()
    for p in sorted(cands, key=lambda x: x["slug"]):
        label = strip_tags(p["title"]["rendered"]).split(",")[0].replace(" County", "").strip()
        slug = county_slug(p["title"]["rendered"])
        if not slug or slug in seen:
            continue
        try:
            full = get_json(f"{WP_PAGES}/{p['id']}?_fields=content,link")
        except Exception as e:
            failures.append((slug, f"fetch failed: {e}"))
            continue
        rec, err = parse_county(full["content"]["rendered"], slug, label,
                                full.get("link") or p.get("link"))
        if err:
            failures.append((slug, err))
            continue
        seen.add(slug)
        out[slug] = rec
        print(f"  ok   {slug:16} 12mo {rec['horizons'][-1]['expected_in']:>5.2f}/"
              f"{rec['horizons'][-1]['normal_in']:>5.2f} in "
              f"({rec['horizons'][-1]['pct_of_normal']}%)")

    for slug, why in failures:
        print(f"  SKIP {slug:16} {why}", file=sys.stderr)

    if len(out) < args.min_counties:
        print(f"\nREFUSING TO WRITE: only {len(out)} counties parsed "
              f"(minimum {args.min_counties}). Feed left unchanged.", file=sys.stderr)
        return 1

    feed = {
        "updated": date.today().isoformat(),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_url": "https://honestcattle.net/measuring-montana-moisture/",
        "source": ("NOAA Climate Prediction Center outlook odds applied to the county's "
                   "1991-2020 precipitation record (NOAA NCEI nClimDiv). A tilt in the "
                   "odds, not a promise; confidence fades with distance."),
        "note": ("Inches are county-wide averages that include mountain ground, so valley "
                 "and bench ranches usually receive less. Percent of normal carries over "
                 "better than the inches."),
        "county_count": len(out),
        "counties": out,
    }
    with open(args.out, "w") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nwrote {args.out}: {len(out)} counties, {len(failures)} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
