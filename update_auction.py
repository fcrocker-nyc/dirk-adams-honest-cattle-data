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
import urllib.error
import urllib.request
from pathlib import Path

AMS_BASE = "https://www.ams.usda.gov/mnreports"
USER_AGENT = "honestcattle-auction-updater/1.0 (+https://honestcattle.net)"
REQUEST_TIMEOUT = 30

REPORTS = {
    "pays": {"id": "AMS_1776", "name": "Public Auction Yards", "day": "Friday"},
    "bls":  {"id": "AMS_1777", "name": "Billings Livestock Commission", "day": "Thursday"},
    "mt_weekly": {"id": "AMS_1778", "name": "Montana Weekly Summary", "day": "Monday"},
}

PDFTOTEXT = os.environ.get("PDFTOTEXT", "pdftotext")


# ---------------------------------------------------------------------------
# PDF download and text extraction
# ---------------------------------------------------------------------------

def download_pdf(report_id: str) -> bytes:
    url = f"{AMS_BASE}/{report_id}.pdf"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


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

def parse_report(text: str, key: str) -> dict | None:
    if not text.strip():
        return None

    report: dict = {"source_key": key}
    info = REPORTS[key]
    report["auction"] = info["name"]
    report["report_id"] = info["id"]
    report["sale_day"] = info["day"]

    # Sale date
    date_match = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\w+\s+\d+,\s+\d{4})", text
    )
    if date_match:
        try:
            report["sale_date"] = dt.datetime.strptime(
                date_match.group(1), "%b %d, %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            report["sale_date"] = date_match.group(1)
    else:
        return None

    # Report period (for weekly summary)
    period_match = re.search(
        r"Livestock Weighted Average Report for (\d+/\d+/\d+)\s*-\s*(\d+/\d+/\d+)",
        text,
    )
    if period_match:
        report["period_start"] = period_match.group(1)
        report["period_end"] = period_match.group(2)

    # Total receipts
    receipts_match = re.search(r"Total Receipts:\s+([\d,]+)", text)
    if receipts_match:
        report["total_receipts"] = int(receipts_match.group(1).replace(",", ""))

    # Breakdown
    breakdown = {}
    for cat, bkey in [
        ("Feeder", "feeder"),
        ("Slaughter", "slaughter"),
        ("Replacement", "replacement"),
    ]:
        m = re.search(rf"{cat} Cattle:\s+([\d,]+)\((\d+\.?\d*)%\)", text)
        if m:
            breakdown[bkey] = {
                "head": int(m.group(1).replace(",", "")),
                "pct": float(m.group(2)),
            }
    report["breakdown"] = breakdown

    # Market narrative
    narr_match = re.search(
        r"((?:Compared to|Special Note:|The last).*?)(?=\n\s*FEEDER CATTLE|\n\s*SLAUGHTER|\n\s*STOCK SUMMARY)",
        text,
        re.DOTALL,
    )
    if narr_match:
        narrative = re.sub(r"\s+", " ", narr_match.group(1).strip())
        report["narrative"] = narrative[:3000]

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
        default=list(REPORTS.keys()),
        choices=list(REPORTS.keys()),
        help="Which reports to fetch (default: all).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    auction_dir = args.out / "auction"
    auction_dir.mkdir(parents=True, exist_ok=True)

    changed = 0
    for key in args.reports:
        info = REPORTS[key]
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

    print(f"[auction] {changed} of {len(args.reports)} reports updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
