#!/usr/bin/env python3
"""
update_auction.py
=================

Daily updater that pulls the latest USDA AMS livestock auction reports
for the two dominant Montana cattle auction yards (PAYS and BLS) plus
the statewide weekly summary, parses them into structured JSON, and
writes the results to the repo.

Data sources (all publicly accessible, no auth):
    AMS_1776  Public Auction Yards (PAYS) - Billings, MT (Fri)
    AMS_1777  Billings Livestock Commission (BLS) - Billings, MT (Thu)
    AMS_1778  Montana Weekly Livestock Auction Summary
    AMS_2772  Northern Livestock Video Auction (NLVA)   - seasonal video
    AMS_2713  Superior Livestock Auction                 - seasonal video
    AMS_3242  Western Video Market (WVM)                 - seasonal video

The three video sales are consolidated into auction/video_latest.json (the
freshest sale across them), which backs the iOS Market page video card.

Each PDF is a USDA AMS Livestock Weighted Average Report containing:
    - Sale date, total receipts, category breakdown
    - Market narrative (analyst commentary on demand, trends, CME)
    - Price tables by category: Head | Wt Range | Avg Wt | Price Range | Avg Price

The script downloads the PDFs, extracts text via pdftotext, parses
the structured data, and writes JSON files. It also maintains a
running history file for trend analysis.

Requires pdftotext (poppler-utils) on the system PATH.
Standard library only otherwise — runs in GitHub Actions with
poppler pre-installed on ubuntu-latest.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

AMS_BASE = "https://www.ams.usda.gov/mnreports"
USER_AGENT = "honestcattle-auction-updater/1.0 (+https://honestcattle.net)"
REQUEST_TIMEOUT = 30

REPORTS = {
    "pays": {"id": "AMS_1776", "name": "Public Auction Yards", "day": "Friday", "channel": "auction"},
    "bls":  {"id": "AMS_1777", "name": "Billings Livestock Commission", "day": "Thursday", "channel": "auction"},
    "mt_weekly": {"id": "AMS_1778", "name": "Montana Weekly Summary", "day": "Monday", "channel": "auction"},
}

# Video / satellite auctions — seasonal (summer), USDA AMS same report format.
# NLVA is Montana's home video sale; Superior and WVM carry Montana consignments.
VIDEO_REPORTS = {
    "nlva":     {"id": "AMS_2772", "name": "Northern Livestock Video Auction", "day": "Seasonal", "channel": "video"},
    "superior": {"id": "AMS_2713", "name": "Superior Livestock Auction", "day": "Ongoing", "channel": "video"},
    "wvm":      {"id": "AMS_3242", "name": "Western Video Market", "day": "Seasonal", "channel": "video"},
}

ALL_REPORTS = {**REPORTS, **VIDEO_REPORTS}

PDFTOTEXT = os.environ.get("PDFTOTEXT", "pdftotext")


# ---------------------------------------------------------------------------
# PDF download and text extraction
# ---------------------------------------------------------------------------

def download_pdf(report_id: str) -> bytes:
    """Fetch an AMS report PDF with a hard wall-clock budget.

    urllib's ``timeout`` parameter only governs the socket connect; it does
    NOT enforce a read deadline. AMS occasionally accepts the connection
    on `search.ams.usda.gov` and then drips bytes slowly, which can hang
    for hours. We wrap the whole call (connect + redirect follow + read)
    in a wall-clock deadline and retry on transient errors.
    """
    url = f"{AMS_BASE}/{report_id}.pdf"
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                chunks: list[bytes] = []
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"AMS read exceeded {REQUEST_TIMEOUT}s budget"
                        )
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_exc = exc
            backoff = 2 ** (attempt - 1) * 5  # 5, 10, 20 seconds
            print(
                f"[auction] {report_id} attempt {attempt}/3 failed: {exc}; "
                f"retrying in {backoff}s",
                file=sys.stderr,
            )
            if attempt < 3:
                time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError("download_pdf: unknown failure")


def pdf_to_text(pdf_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [PDFTOTEXT, "-layout", tmp_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[auction] pdftotext error: {result.stderr}", file=sys.stderr)
            return ""
        return result.stdout
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Report parser
# ---------------------------------------------------------------------------

def _to_iso_named(s: str) -> str | None:
    """'June 18, 2026' or 'Jun 18, 2026' -> '2026-06-18'."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_iso_slash(s: str) -> str | None:
    """'6/17/2026' -> '2026-06-17'."""
    try:
        return dt.datetime.strptime(s.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_report(text: str, key: str) -> dict | None:
    if not text.strip():
        return None

    report: dict = {"source_key": key}
    info = ALL_REPORTS[key]
    report["auction"] = info["name"]
    report["report_id"] = info["id"]
    report["sale_day"] = info["day"]
    report["channel"] = info.get("channel", "auction")

    # Report period (barn weekly + video sale window) — parsed first so it can
    # serve as a sale-date fallback for video reports.
    period_match = re.search(
        r"Livestock Weighted Average Report for (\d+/\d+/\d+)\s*-\s*(\d+/\d+/\d+)",
        text,
    )
    if period_match:
        report["period_start"] = period_match.group(1)
        report["period_end"] = period_match.group(2)

    # Sale date. Barn reports prefix a weekday ("Mon June 18, 2026"); video
    # reports print a bare "June 18, 2026" header plus the window above. Try
    # weekday-prefixed → sale-window end → any bare "Month DD, YYYY".
    sale_date = None
    m = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+(\w+\s+\d+,\s+\d{4})", text
    )
    if m:
        sale_date = _to_iso_named(m.group(1))
    if sale_date is None and period_match:
        sale_date = _to_iso_slash(period_match.group(2))
    if sale_date is None:
        m = re.search(
            r"\b((?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},\s+\d{4})\b",
            text,
        )
        if m:
            sale_date = _to_iso_named(m.group(1))
    if sale_date is None:
        return None
    report["sale_date"] = sale_date

    # Total receipts. Barn reports list a single head count; video reports list
    # two columns — Offered then Reported (sold) — followed by a %PO. Capture
    # the sold count as total_receipts (the meaningful sale size), and record
    # offered separately when present.
    vid_receipts = re.search(
        r"Total Receipts:\s+([\d,]+)\s+([\d,]+)\s+[\d.]+\s*%", text
    )
    if vid_receipts:
        report["receipts_offered"] = int(vid_receipts.group(1).replace(",", ""))
        report["total_receipts"] = int(vid_receipts.group(2).replace(",", ""))
    else:
        receipts_match = re.search(r"Total Receipts:\s+([\d,]+)", text)
        if receipts_match:
            report["total_receipts"] = int(receipts_match.group(1).replace(",", ""))

    # Breakdown. Barn: "Feeder Cattle: 1,234(56.7%)". Video: an Offered and a
    # Reported count precede the pct, e.g. "Feeder Cattle: 75,424 64,503 (99.4%)"
    # — take the reported (sold) head when both are present.
    breakdown = {}
    for cat, bkey in [
        ("Feeder", "feeder"),
        ("Slaughter", "slaughter"),
        ("Replacement", "replacement"),
    ]:
        m = re.search(
            rf"{cat} Cattle:\s+([\d,]+)(?:\s+([\d,]+))?\s*\((\d+\.?\d*)%\)", text
        )
        if m:
            head = int((m.group(2) or m.group(1)).replace(",", ""))
            breakdown[bkey] = {"head": head, "pct": float(m.group(3))}
    report["breakdown"] = breakdown

    # Market narrative. Barn reports open with "Compared to/Special Note/The
    # last"; video reports (esp. NLVA/WVM) run a free-form paragraph right after
    # the receipts/breakdown block. Try the keyed opener, then fall back to the
    # prose that sits between the breakdown lines and the first price table.
    narr_match = re.search(
        r"((?:Compared to|Special Note:|The last).*?)(?=\n\s*FEEDER CATTLE|\n\s*SLAUGHTER|\n\s*STOCK SUMMARY)",
        text,
        re.DOTALL,
    )
    if narr_match:
        report["narrative"] = re.sub(r"\s+", " ", narr_match.group(1).strip())[:3000]
    else:
        anchor = 0
        for mm in re.finditer(r"(?:Feeder|Slaughter|Replacement) Cattle:[^\n]*", text):
            anchor = mm.end()
        tail = text[anchor:]
        stop = re.search(
            r"\n\s*(?:FEEDER CATTLE|SLAUGHTER|STOCK SUMMARY|STEERS|HEIFERS|"
            r"COWS|BULLS|Source:|Email us)",
            tail,
        )
        chunk = re.sub(r"\s+", " ", (tail[:stop.start()] if stop else tail[:2000]).strip())
        if len(chunk) > 40 and "." in chunk:
            report["narrative"] = chunk[:3000]

    # Parse price tables
    entries = _parse_price_tables(text)
    report["entries"] = entries

    # Build summary by weight class for quick access
    report["summary"] = _build_summary(entries)

    report["parsed_at"] = dt.datetime.utcnow().isoformat() + "Z"
    return report


def _parse_price_tables(text: str) -> list[dict]:
    entries: list[dict] = []

    # Find category sections
    # Pattern: "ANIMAL_TYPE - Grade (Per Cwt / Actual Wt)"
    sections = re.finditer(
        r"(STEERS|HEIFERS|COWS|BULLS|STOCK COWS|BRED COWS|BRED HEIFERS|COW-CALF PAIRS)"
        r"\s*[-–]\s*"
        r"(.*?)"
        r"\s*\(Per (Cwt|Head|Unit).*?\)",
        text,
    )

    for section_match in sections:
        animal_type = section_match.group(1).strip()
        grade = section_match.group(2).strip()
        pricing = section_match.group(3).strip()
        start = section_match.end()

        # Find the end of this section (next category header or end of text)
        next_section = re.search(
            r"\n\s*(?:STEERS|HEIFERS|COWS|BULLS|STOCK|BRED|COW-CALF|REPLACEMENT|SLAUGHTER|Source:)",
            text[start:],
        )
        end = start + next_section.start() if next_section else len(text)
        table_text = text[start:end]

        # Parse data rows
        row_pattern = re.compile(
            r"^\s*(\d+)\s+"  # Head
            r"([\d,]+-?[\d,]*)\s+"  # Wt Range
            r"([\d,]+)\s+"  # Avg Wt
            r"([\d,.]+(?:-[\d,.]+)?)\s+"  # Price Range
            r"([\d,.]+)"  # Avg Price
            r"(?:\s+(\w+))?",  # Optional note (Fleshy, Full, Thin, etc.)
            re.MULTILINE,
        )

        for m in row_pattern.finditer(table_text):
            wt_parts = m.group(2).replace(",", "").split("-")
            price_parts = m.group(4).replace(",", "").split("-")

            entry = {
                "type": animal_type,
                "grade": grade,
                "pricing": pricing.lower(),
                "head": int(m.group(1)),
                "wt_low": int(wt_parts[0]),
                "wt_high": int(wt_parts[-1]),
                "avg_wt": int(m.group(3).replace(",", "")),
                "price_low": float(price_parts[0]),
                "price_high": float(price_parts[-1]),
                "avg_price": float(m.group(5).replace(",", "")),
            }
            if m.group(6):
                entry["note"] = m.group(6)
            entries.append(entry)

    return entries


def _build_summary(entries: list[dict]) -> dict:
    """Build a quick-reference summary by weight class for feeders."""
    summary: dict = {"steers": {}, "heifers": {}}

    for e in entries:
        if e.get("pricing") != "cwt":
            continue
        if e["type"] == "STEERS" and "Medium and Large 1" in e["grade"]:
            bucket = _weight_bucket(e["avg_wt"])
            if bucket:
                summary["steers"].setdefault(bucket, []).append(e)
        elif e["type"] == "HEIFERS" and "Medium and Large 1" in e["grade"]:
            bucket = _weight_bucket(e["avg_wt"])
            if bucket:
                summary["heifers"].setdefault(bucket, []).append(e)

    # Aggregate each bucket
    for gender in ("steers", "heifers"):
        for bucket, rows in summary[gender].items():
            total_head = sum(r["head"] for r in rows)
            wtd_price = (
                sum(r["avg_price"] * r["head"] for r in rows) / total_head
                if total_head
                else 0
            )
            summary[gender][bucket] = {
                "head": total_head,
                "wtd_avg_price": round(wtd_price, 2),
                "price_low": min(r["price_low"] for r in rows),
                "price_high": max(r["price_high"] for r in rows),
            }

    return summary


def _weight_bucket(wt: int) -> str | None:
    if wt < 400:
        return "under_400"
    elif wt < 500:
        return "400_499"
    elif wt < 550:
        return "500_549"
    elif wt < 600:
        return "550_599"
    elif wt < 650:
        return "600_649"
    elif wt < 700:
        return "650_699"
    elif wt < 750:
        return "700_749"
    elif wt < 800:
        return "750_799"
    elif wt < 900:
        return "800_899"
    else:
        return "900_plus"


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

def update_history(history_path: Path, report: dict) -> bool:
    """Append a sale to the history file if it's new. Returns True if added."""
    history: list = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            history = []

    # Check for duplicate (same auction + sale_date)
    key = (report.get("source_key"), report.get("sale_date"))
    for existing in history:
        if (existing.get("source_key"), existing.get("sale_date")) == key:
            return False

    # Strip the full entries list from history records to keep size manageable
    slim = {k: v for k, v in report.items() if k != "entries"}
    slim["entry_count"] = len(report.get("entries", []))
    history.append(slim)

    history.sort(key=lambda r: (r.get("sale_date", ""), r.get("source_key", "")))
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("."),
        help="Output directory (default: current dir = repo root).",
    )
    parser.add_argument(
        "--reports",
        nargs="*",
        default=list(ALL_REPORTS.keys()),
        choices=list(ALL_REPORTS.keys()),
        help="Which reports to fetch (default: all auction + video).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    auction_dir = args.out / "auction"
    auction_dir.mkdir(parents=True, exist_ok=True)

    changed = 0
    for key in args.reports:
        info = ALL_REPORTS[key]
        if args.verbose:
            print(f"[auction] Fetching {info['id']} ({info['name']})...")

        try:
            pdf_bytes = download_pdf(info["id"])
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(
                f"[auction] Failed to download {info['id']}: {exc}",
                file=sys.stderr,
            )
            continue

        text = pdf_to_text(pdf_bytes)
        if not text:
            print(f"[auction] pdftotext returned empty for {info['id']}", file=sys.stderr)
            continue

        report = parse_report(text, key)
        if not report:
            print(f"[auction] Failed to parse {info['id']}", file=sys.stderr)
            continue

        # Write latest report
        latest_path = auction_dir / f"{key}_latest.json"
        new_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

        # Check if sale date changed
        if latest_path.exists():
            try:
                existing = json.loads(latest_path.read_text(encoding="utf-8"))
                if existing.get("sale_date") == report.get("sale_date"):
                    if args.verbose:
                        print(
                            f"[auction] {key}: unchanged (sale date {report['sale_date']})"
                        )
                    continue
            except (json.JSONDecodeError, ValueError):
                pass

        latest_path.write_text(new_text, encoding="utf-8")
        changed += 1

        # Update history
        history_path = auction_dir / "history.json"
        added = update_history(history_path, report)

        if args.verbose:
            bd = report.get("breakdown", {})
            summary = report.get("summary", {})
            steer_500 = summary.get("steers", {}).get("500_549", {})
            steer_price = steer_500.get("wtd_avg_price", "n/a")
            print(
                f"[auction] {key}: {report['sale_date']} | "
                f"{report.get('total_receipts', '?')} head | "
                f"{len(report.get('entries', []))} entries | "
                f"500-549 steers ${steer_price}/cwt | "
                f"{'added to history' if added else 'already in history'}"
            )

    # Consolidate the freshest video sale across NLVA / Superior / WVM into a
    # single video_latest.json the app reads. Reads from disk (not just this
    # run's results) so an unchanged-but-present sale still surfaces. The app
    # decides seasonality from sale_date freshness.
    consolidate_video_latest(auction_dir, verbose=args.verbose)

    print(f"[auction] {changed} of {len(args.reports)} reports updated")
    return 0


def consolidate_video_latest(auction_dir: Path, verbose: bool = False) -> None:
    candidates: list[dict] = []
    for key in VIDEO_REPORTS:
        path = auction_dir / f"{key}_latest.json"
        if not path.exists():
            continue
        try:
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            continue

    if not candidates:
        if verbose:
            print("[auction] video: no per-sale files yet — video_latest.json not written")
        return

    # Freshest by sale_date (ISO sorts lexically); ties broken by receipts.
    freshest = max(
        candidates,
        key=lambda r: (r.get("sale_date", ""), r.get("total_receipts", 0)),
    )
    freshest = dict(freshest)
    freshest["channel"] = "video"
    # Roster of the sales we track, so the app can name the series even off-season.
    freshest["series_roster"] = [
        {"key": k, "name": v["name"], "report_id": v["id"]}
        for k, v in VIDEO_REPORTS.items()
    ]

    out_path = auction_dir / "video_latest.json"
    new_text = json.dumps(freshest, ensure_ascii=False, indent=2) + "\n"
    if out_path.exists() and out_path.read_text(encoding="utf-8") == new_text:
        if verbose:
            print(f"[auction] video_latest.json unchanged ({freshest.get('sale_date')})")
        return
    out_path.write_text(new_text, encoding="utf-8")
    if verbose:
        print(
            f"[auction] video_latest.json → {freshest.get('auction')} "
            f"{freshest.get('sale_date')} ({freshest.get('total_receipts', '?')} head)"
        )


if __name__ == "__main__":
    raise SystemExit(main())
