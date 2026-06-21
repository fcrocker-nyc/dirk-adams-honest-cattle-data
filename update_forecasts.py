#!/usr/bin/env python3
"""
update_forecasts.py — rebuild forecasts_recent.json from honestcattle.net.

Pulls the four most recent posts in the "Weekly Forecasts" category (ID 1367)
via the WordPress REST API, finds the quarterly calf-price table marked with
CSS class "hc-forecast" in each post, parses it, and writes the rolling
4-week forecast JSON consumed by the HonestCattle iOS app and the WP shortcode.

Stdlib only — runs in CI without pip dependencies (matches update_snotel.py).

Required table shape inside the WP post:
    <table class="hc-forecast">
      <thead>
        <tr><th>Quarter</th><th>Status</th>
            <th>Steer Range</th><th>Steer Mid</th>
            <th>Heifer Range</th><th>Heifer Mid</th></tr>
      </thead>
      <tbody>
        <tr><td>Q2 2026</td><td>HELD</td>
            <td>460–495</td><td>478</td>
            <td>435–475</td><td>455</td></tr>
        ...
      </tbody>
    </table>

A heifer cell may be empty or "—" if not yet published; that quarter's
heifer band serializes as null.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib import request, error

WP_API = "https://honestcattle.net/wp-json/wp/v2/posts"
CATEGORY_ID = 1367  # "Weekly Forecasts"
TARGET_TABLE_CLASS = "hc-forecast"
HISTORY_LIMIT = 4
USER_AGENT = "hc-forecast-updater/1.0 (+https://github.com/fcrocker-nyc/dirk-adams-honest-cattle-data)"


# ---------- WP fetch ----------------------------------------------------------

def fetch_recent_posts(limit: int = HISTORY_LIMIT) -> list[dict]:
    url = (
        f"{WP_API}?categories={CATEGORY_ID}"
        f"&per_page={limit}&orderby=date&order=desc&_fields=id,date,link,title,content,slug"
    )
    req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# ---------- HTML table extractor ----------------------------------------------

class TableCollector(HTMLParser):
    """
    Streams a WP post body and records EVERY top-level <table> as
    (class_attr, rows), where each row is a list of flattened <th>/<td>
    texts. The forecast table is then chosen by class OR header signature
    (see ``select_forecast_rows``), so a dropped ``hc-forecast`` class on a
    post no longer silently breaks the parse and freezes the app forecast.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[tuple[str, list[list[str]]]] = []
        self._depth = 0  # table nesting depth
        self._cur_class = ""
        self._cur_rows: list[list[str]] | None = None
        self.in_row = False
        self.in_cell = False
        self.current_row: list[str] = []
        self.current_cell: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._cur_rows = []
                self._cur_class = ""
                for k, v in attrs:
                    if k == "class" and v:
                        self._cur_class = v
                        break
            return
        if self._depth == 0:
            return
        if tag == "tr" and self._depth == 1:
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []
        elif tag == "br" and self.in_cell:
            self.current_cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1 and self._cur_rows is not None:
                self.tables.append((self._cur_class, self._cur_rows))
                self._cur_rows = None
            if self._depth > 0:
                self._depth -= 1
            return
        if self._depth == 0:
            return
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"\s+", " ", "".join(self.current_cell)).strip()
            self.current_row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row and self._cur_rows is not None:
                self._cur_rows.append(self.current_row)
            self.in_row = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def _is_forecast_header(row: list[str]) -> bool:
    joined = " ".join(c.lower() for c in row)
    return "quarter" in joined and "steer" in joined


def select_forecast_rows(
    tables: list[tuple[str, list[list[str]]]],
) -> list[list[str]] | None:
    """Pick the forecast table: prefer the hc-forecast class, else a table
    whose header row carries the Quarter/Steer signature."""
    for cls, rows in tables:
        if TARGET_TABLE_CLASS in cls.split() and rows:
            return rows
    for _cls, rows in tables:
        if rows and _is_forecast_header(rows[0]):
            return rows
    return None


# ---------- Row parsing -------------------------------------------------------

# Accepts en-dash (–), em-dash (—), or hyphen-minus separating low/high.
RANGE_SEP = r"[–—‒\-]"
RANGE_RE = re.compile(rf"^\s*\$?(\d+(?:\.\d+)?)\s*{RANGE_SEP}\s*\$?(\d+(?:\.\d+)?)\s*$")
NUM_RE = re.compile(r"^\s*\$?(\d+(?:\.\d+)?)\s*$")


def parse_range(cell: str) -> tuple[float, float] | None:
    m = RANGE_RE.match(cell or "")
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def parse_number(cell: str) -> float | None:
    if not cell:
        return None
    m = NUM_RE.match(cell)
    if not m:
        return None
    return float(m.group(1))


def is_empty_cell(cell: str) -> bool:
    if not cell:
        return True
    stripped = cell.strip()
    return stripped in {"", "—", "–", "-", "N/A", "n/a", "TBD", "tbd"}


def normalize_status(raw: str) -> str:
    s = (raw or "").strip().upper()
    s = re.sub(r"\s+", " ", s)
    if "UP" in s:
        return "ADJUSTED UP"
    if "DOWN" in s:
        return "ADJUSTED DOWN"
    if "HELD" in s or "HOLD" in s or "STEAD" in s:
        return "HELD"
    return s or "HELD"


def parse_quarter_row(row: list[str]) -> dict | None:
    """
    Expected columns:
        0  Quarter (e.g. "Q2 2026")
        1  Status  (HELD / ADJUSTED UP / ADJUSTED DOWN)
        2  Steer Range  (e.g. "460–495")
        3  Steer Mid    (e.g. "478")
        4  Heifer Range
        5  Heifer Mid
    """
    if len(row) < 6:
        return None
    label = row[0].strip()
    if not re.match(r"^Q[1-4]\s+\d{4}$", label):
        return None

    status = normalize_status(row[1])
    steer = _band(row[2], row[3])
    heifer = _band(row[4], row[5])

    return {
        "label": label,
        "status": status,
        "steer": steer,
        "heifer": heifer,
    }


def _band(range_cell: str, mid_cell: str) -> dict | None:
    if is_empty_cell(range_cell) and is_empty_cell(mid_cell):
        return None
    rng = parse_range(range_cell)
    mid = parse_number(mid_cell)
    if rng is None and mid is None:
        return None
    if rng is None:
        return {"low": mid, "high": mid, "mid": mid}
    low, high = rng
    return {"low": low, "high": high, "mid": mid}


# ---------- Post → week record ------------------------------------------------

def _band_key(disp: str) -> str | None:
    """'550–599' / '600-649' → '550_599' / '600_649'."""
    nums = re.findall(r"\d+", disp or "")
    return f"{nums[0]}_{nums[1]}" if len(nums) >= 2 else None


def parse_quarter_band_row(row: list[str]) -> dict | None:
    """v1.6 split-band row:
        0 Quarter  1 Band  2 Status  3 Steer Range  4 Steer Mid  5 Heifer Range  6 Heifer Mid
    """
    if len(row) < 7:
        return None
    label = row[0].strip()
    if not re.match(r"^Q[1-4]\s+\d{4}$", label):
        return None
    band = _band_key(row[1])
    if not band:
        return None
    return {
        "label": label,
        "band": band,
        "status": normalize_status(row[2]),
        "steer": _band(row[3], row[4]),
        "heifer": _band(row[5], row[6]),
    }


def _blend(bands: dict) -> dict | None:
    """Quarter-level steer/heifer (550–650 equivalent) blended from the two
    bands, so consumers reading the legacy flat fields keep working."""
    out: dict = {}
    for sex in ("steer", "heifer"):
        parts = [b[sex] for b in bands.values() if b.get(sex)]
        if not parts:
            out[sex] = None
            continue
        lows = [p["low"] for p in parts if p.get("low") is not None]
        highs = [p["high"] for p in parts if p.get("high") is not None]
        mids = [p["mid"] for p in parts if p.get("mid") is not None]
        out[sex] = {
            "low": min(lows) if lows else None,
            "high": max(highs) if highs else None,
            "mid": round(sum(mids) / len(mids)) if mids else None,
        }
    return out


def post_to_week(post: dict) -> dict | None:
    body = (post.get("content") or {}).get("rendered") or ""
    collector = TableCollector()
    collector.feed(body)

    rows = select_forecast_rows(collector.tables)
    if not rows:
        return None

    # Detect the header row + whether this is the v1.6 split-band layout
    # (a "Band" column → two rows per quarter).
    split = False
    if rows and rows[0] and rows[0][0].strip().lower() == "quarter":
        split = "band" in " ".join(c.lower() for c in rows[0])
        rows = rows[1:]

    quarters: list[dict] = []
    if split:
        by_quarter: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            q = parse_quarter_band_row(r)
            if not q:
                continue
            label = q["label"]
            if label not in by_quarter:
                by_quarter[label] = {"label": label, "status": q["status"], "bands": {}}
                order.append(label)
            by_quarter[label]["bands"][q["band"]] = {"steer": q["steer"], "heifer": q["heifer"]}
        for label in order:
            qd = by_quarter[label]
            blended = _blend(qd["bands"])
            qd["steer"] = blended.get("steer")
            qd["heifer"] = blended.get("heifer")
            quarters.append(qd)
    else:
        for r in rows:
            q = parse_quarter_row(r)
            if q:
                quarters.append(q)

    if not quarters:
        return None

    iso_date = (post.get("date") or "")[:10]
    title = ((post.get("title") or {}).get("rendered") or "").strip()
    title = re.sub(r"<[^>]+>", "", title)

    return {
        "as_of": iso_date,
        "source_url": post.get("link") or "",
        "source_title": title,
        "class_note": ("550–599 and 600–649 lb Montana-origin steers / heifers, FOB auction"
                       if split else
                       "550–650 lb Montana-origin steers / heifers, FOB auction"),
        "quarters": quarters,
    }


# ---------- Main --------------------------------------------------------------

def build_payload(verbose: bool = False) -> dict | None:
    posts = fetch_recent_posts(limit=HISTORY_LIMIT)
    if verbose:
        print(f"[forecasts] fetched {len(posts)} posts from category {CATEGORY_ID}", file=sys.stderr)

    weeks: list[dict] = []
    for post in posts:
        week = post_to_week(post)
        if week is None:
            if verbose:
                date = (post.get("date") or "")[:10]
                link = post.get("link") or ""
                print(f"[forecasts] skipped {date} ({link}): no forecast table (hc-forecast class or Quarter/Steer header) or unparseable rows", file=sys.stderr)
            continue
        weeks.append(week)

    if not weeks:
        return None

    weeks.sort(key=lambda w: w["as_of"], reverse=True)
    weeks = weeks[:HISTORY_LIMIT]

    return {
        "updated": weeks[0]["as_of"],
        "weeks": weeks,
    }


def write_if_changed(payload: dict, out_path: Path, verbose: bool = False) -> bool:
    new_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if out_path.exists() and out_path.read_text(encoding="utf-8") == new_text:
        if verbose:
            print(f"[forecasts] {out_path.name} unchanged", file=sys.stderr)
        return False
    out_path.write_text(new_text, encoding="utf-8")
    if verbose:
        print(f"[forecasts] wrote {out_path} ({len(payload['weeks'])} weeks)", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=".", help="Output directory (default: cwd)")
    parser.add_argument("--verbose", action="store_true", help="Log progress to stderr")
    args = parser.parse_args(argv)

    try:
        payload = build_payload(verbose=args.verbose)
    except (error.URLError, error.HTTPError) as exc:
        print(f"[forecasts] WP REST API fetch failed: {exc}", file=sys.stderr)
        return 2

    if payload is None:
        # Not a failure: every recent post is still in prose / lacks the
        # hc-forecast table. Leave the existing forecasts_recent.json in
        # place and exit clean so the workflow doesn't show as failed.
        print("[forecasts] no posts in the new table format yet — leaving existing JSON unchanged", file=sys.stderr)
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_if_changed(payload, out_dir / "forecasts_recent.json", verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
