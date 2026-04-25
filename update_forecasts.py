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

class ForecastTableFinder(HTMLParser):
    """
    Streams a WP post body and captures rows of the first <table> whose
    class attribute contains the target class. Only <th>/<td> text content
    is recorded; nested formatting (<strong>, <em>, etc.) is flattened.
    """

    def __init__(self, target_class: str = TARGET_TABLE_CLASS) -> None:
        super().__init__()
        self.target_class = target_class
        self.in_target_table = False
        self.captured = False  # we only take the first matching table per post
        self.in_row = False
        self.in_cell = False
        self.current_row: list[str] = []
        self.current_cell: list[str] = []
        self.rows: list[list[str]] = []
        self._table_depth = 0  # handle nested tables defensively

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            classes = ""
            for k, v in attrs:
                if k == "class" and v:
                    classes = v
                    break
            if not self.captured and self._matches(classes):
                self.in_target_table = True
                self._table_depth = 1
                return
            if self.in_target_table:
                self._table_depth += 1
            return

        if not self.in_target_table:
            return

        if tag == "tr" and self._table_depth == 1:
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []
        elif tag == "br" and self.in_cell:
            self.current_cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self.in_target_table:
                self._table_depth -= 1
                if self._table_depth <= 0:
                    self.in_target_table = False
                    self.captured = True
            return

        if not self.in_target_table:
            return

        if tag in ("td", "th") and self.in_cell:
            text = "".join(self.current_cell).strip()
            text = re.sub(r"\s+", " ", text)
            self.current_row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_row = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

    def _matches(self, class_attr: str) -> bool:
        tokens = class_attr.split()
        return self.target_class in tokens


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

def post_to_week(post: dict) -> dict | None:
    body = (post.get("content") or {}).get("rendered") or ""
    finder = ForecastTableFinder()
    finder.feed(body)

    if not finder.rows:
        return None

    # Detect & skip a header row by looking for the literal "Quarter" cell.
    rows = finder.rows
    if rows and rows[0] and rows[0][0].strip().lower() == "quarter":
        rows = rows[1:]

    quarters: list[dict] = []
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
        "class_note": "550–650 lb Montana-origin steers / heifers, FOB auction",
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
                print(f"[forecasts] skipped {date} ({link}): no '{TARGET_TABLE_CLASS}' table or unparseable rows", file=sys.stderr)
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
