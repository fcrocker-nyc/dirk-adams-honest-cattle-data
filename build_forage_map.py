#!/usr/bin/env python3
"""
build_forage_map.py — render the statewide "Forage Outlook" hub map from the live
per-county forage scores, so the map on /counties-in-montana-counties/ stays current
and consistent with the county pages (instead of the old static snowpack SVG).

Reads each <slug>.json (forage_score + drought + date), colours the baked Montana
county geometry (mt_county_geometry.json) by the SAME 0-100 score / category bands the
county pages use, and writes forage_map.json = {"html": <fragment>, "updated": ...}.
A tiny WP shortcode [hc_forage_map] fetches that JSON and echoes the html.

Called at the end of update_snotel.py's run (so the daily GitHub Actions job keeps it
fresh and commits it), and runnable standalone:  python build_forage_map.py --out .
"""
from __future__ import annotations
import argparse, glob, json, math, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
GEOMETRY = os.path.join(HERE, "mt_county_geometry.json")

# 2026-09-11: score + bands now match what the county pages show (WP plugin
# "County Conditions Dashboard" v2.0.3, hc_forage_v2 / hc_forage_category), so the hub map,
# the hub cards and every county page print the same number and the same category.
def v2_score(d):
    """HCFS v2 = 0.40*VR + 0.35*MI + 0.15*DC + 0.10*LU from forage_model (PHP round =
    half away from zero); falls back to legacy forage_score exactly like the plugin."""
    fm = d.get("forage_model") or {}
    try:
        x = (0.40 * float(fm["vr"]) + 0.35 * float(fm["mi"])
             + 0.15 * float(fm["dc"]) + 0.10 * float(fm["lu"]))
        return max(0, min(100, int(math.floor(x + 0.5))))
    except (KeyError, TypeError, ValueError):
        fs = d.get("forage_score")
        return None if fs is None else max(0, min(100, int(fs)))

def band(score):
    if score is None: return ("na", "#c9c2b4", "No data")
    if score <= 25:   return ("poor", "#8B0000", "Poor")
    if score <= 50:   return ("fair", "#B5651D", "Fair")
    if score <= 75:   return ("good", "#4a5e3a", "Good")
    return ("exc", "#3a4b2e", "Excellent")

LEGEND = [("#8B0000", "Poor", "0-25"), ("#B5651D", "Fair", "26-50"),
          ("#4a5e3a", "Good", "51-75"), ("#3a4b2e", "Excellent", "76-100")]


def _norm(s: str) -> str:
    toks = [t for t in re.split(r"[\s_\-]+", s.lower()) if t and t != "and"]
    return re.sub(r"[^a-z]", "", "".join(toks))


def load_counties(out_dir):
    """slug/name-normalised -> {name, score, cat, drought, url}."""
    rows = {}
    for p in glob.glob(os.path.join(out_dir, "*.json")):
        base = os.path.basename(p)
        if base in ("counties.json", "forage_map.json", "vegetation.json",
                    "forecasts_recent.json"):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if not isinstance(d, dict) or "forage_score" not in d or not d.get("county"):
            continue
        fm = d.get("forage_model") or {}
        dr = d.get("drought") or {}
        rows[_norm(d["county"])] = {
            "score": v2_score(d),
            "cat": fm.get("category"),
            "drought": dr.get("worst_class") or "No drought",
            "date": d.get("date", ""),
        }
    return rows


def build_html(out_dir):
    geo = json.load(open(GEOMETRY))
    data = load_counties(out_dir)
    as_of = max((v["date"] for v in data.values() if v.get("date")), default="")

    paths, labels, scored = [], [], []
    for name, g in geo.items():
        c = data.get(_norm(name))
        score = c["score"] if c else None
        b_key, color, b_lbl = band(score)
        url = g.get("url", "#")
        drought = c["drought"] if c else "?"
        title = (f"{name} County — Forage {score}/100 · {drought}" if score is not None
                 else f"{name} County — no data")
        paths.append(
            f'<a href="{url}" class="hc-fm-link" aria-label="{title}" data-hc-county="{name}" '
            f'data-hc-score="{"" if score is None else score}" data-hc-drought="{drought}" '
            f'data-hc-band="{b_key}">'
            f'<path d="{g["d"]}" fill="{color}" stroke="#f4efe4" stroke-width="0.8">'
            f'<title>{title}</title></path></a>')
        lb = g.get("label")
        if lb:
            paths.append(
                f'<text x="{lb["x"]:.1f}" y="{lb["y"]:.1f}" font-size="{lb["fs"]:.1f}" '
                f'fill="#f7f4ec" text-anchor="middle" style="pointer-events:none;'
                f'font-family:ui-sans-serif,system-ui,sans-serif;font-weight:600">'
                f'{"" if score is None else score}</text>')
        if score is not None:
            scored.append((score, name, url, drought))

    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:12px">'
        f'<span style="width:13px;height:13px;border-radius:3px;background:{col};'
        f'display:inline-block"></span>{lbl} <span style="color:#8a8578">{rng}</span></span>'
        for col, lbl, rng in LEGEND)

    worst = sorted(scored)[:4]
    stress = "".join(
        f'<a href="{url}" style="text-decoration:none;flex:1 1 130px;min-width:120px;'
        f'background:#fbf8f2;border:1px solid #e3dccb;border-radius:10px;padding:10px 12px">'
        f'<div style="font-family:Georgia,serif;font-weight:700;color:#2C2C2C;font-size:15px">{name}</div>'
        f'<div style="font-size:12px;color:#6b6559;margin-top:2px">Forage <b style="color:#8B0000">{score}</b>'
        f'/100 · {drought}</div></a>'
        for score, name, url, drought in worst)

    return {
        "updated": as_of,
        "as_of": as_of,
        "html": (
            '<div id="hc-forage-map" style="margin:8px 0">'
            '<style>#hc-forage-map .hc-fm-link path{transition:opacity .12s}'
            '#hc-forage-map .hc-fm-link:hover path{opacity:.75}</style>'
            '<p style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;'
            'color:#6b6559;margin:0 0 8px">All 56 counties at a glance — colour and number are the '
            'current Forage Score. Hover any county for its score and drought class; click to open '
            'the county page. Updated each morning.</p>'
            '<div style="overflow-x:auto"><svg viewBox="0 0 718 410" '
            'xmlns="http://www.w3.org/2000/svg" role="img" '
            'aria-label="Montana counties forage outlook map" '
            'style="width:100%;max-width:760px;height:auto;background:#eef2e6;border-radius:12px">'
            + "".join(paths) + '</svg></div>'
            '<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:12.5px;'
            'color:#4a4a42;margin:10px 0 4px;display:flex;flex-wrap:wrap;align-items:center">'
            + legend + '</div>'
            '<h3 style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.1em;color:#8a5a2b;font-weight:800;'
            'margin:16px 0 8px">Highest stress right now</h3>'
            '<div style="display:flex;flex-wrap:wrap;gap:10px">' + stress + '</div>'
            '<p style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;'
            'color:#8a8578;font-style:italic;margin:12px 0 0">Forage Score is a 0-100 composite of '
            'U.S. Drought Monitor status, growing-season rainfall (NCEI county rank), and satellite '
            'rangeland vegetation — relative to each county&rsquo;s own history. Snowpack is not used '
            'in the growing-season read. Regional screening signal, not a pasture-level inventory. '
            f'Refreshed each morning{(" · as of " + as_of) if as_of else ""}. Prepared by Dirk Adams '
            'with the assistance of AI.</p>'
            '</div>'
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    frag = build_html(args.out)
    outp = os.path.join(args.out, "forage_map.json")
    with open(outp, "w") as f:
        f.write(json.dumps(frag, ensure_ascii=False) + "\n")
    n = frag["html"].count("hc-fm-link")
    print(f"[forage_map] wrote {outp}: {n} counties, as_of {frag['as_of']}, "
          f"{len(frag['html'])} chars")


if __name__ == "__main__":
    main()
