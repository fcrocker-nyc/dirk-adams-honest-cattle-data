#!/usr/bin/env python3
"""
forage_percentiles.py
=====================

NCEI rank-based percentile helper for the HC Forage Score moisture-input
(MI) component used by update_snotel.py.

Background
---------
NOAA NCEI "Climate at a Glance" county precipitation responses include an
integer ``rank`` for each county/period. The convention is::

    rank = 1   -> driest on record
    rank = N   -> wettest on record

where ``N`` is the number of years in the period of record (published in the
response ``description["period of record"]`` field, e.g. "132 years"). The
per-county objects do NOT carry the record length, so update_snotel.py parses
it once from the description and attaches it as ``precip_anomaly["n_years"]``.

We convert a rank to a percentile (0-100) with the mid-rank ("Hazen") plotting
position so the result never pins to exactly 0 or 100::

    percentile = (rank - 0.5) / N * 100

This percentile is used directly as the 0-100 MI signal when available. When
the rank or record length is missing, update_snotel.py falls back to its
existing "% of normal" moisture path. The percentile drives MI off the
3-month (m3) window, which smooths single-month noise while still tracking the
current spring moisture regime.

Standard library only. No third-party dependencies.
"""

from __future__ import annotations

DEFAULT_RECORD_LENGTH = 132  # NCEI MT county POR fallback if N cannot be parsed


def rank_to_percentile(rank, n_years=None):
    """Convert an NCEI 1..N precipitation rank to a 0-100 percentile.

    Uses the mid-rank plotting position ``(rank - 0.5) / N * 100`` so the
    result is strictly inside (0, 100). ``rank`` follows the NCEI convention
    of 1 = driest, N = wettest.

    Returns ``None`` if ``rank`` is missing or not a sane integer in [1, N].
    ``n_years`` defaults to :data:`DEFAULT_RECORD_LENGTH` when not supplied.
    """
    if rank is None:
        return None
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return None

    n = DEFAULT_RECORD_LENGTH
    if n_years is not None:
        try:
            n = int(n_years)
        except (TypeError, ValueError):
            n = DEFAULT_RECORD_LENGTH
    if n <= 0:
        n = DEFAULT_RECORD_LENGTH

    # Reject out-of-range ranks rather than silently clamping; a rank outside
    # [1, N] signals a malformed response and should trigger the fallback.
    if r < 1 or r > n:
        return None

    pct = (r - 0.5) / n * 100.0
    # Numerical safety; mid-rank already keeps us off the 0/100 endpoints.
    if pct < 0.0:
        pct = 0.0
    elif pct > 100.0:
        pct = 100.0
    return round(pct, 1)


def mi_from_ncei_rank(precip_anomaly):
    """Compute the MI moisture signal (0-100) from the NCEI 3-month rank.

    Expects the per-county ``precip_anomaly`` dict produced by
    ``fetch_cag_precip_anomaly`` in update_snotel.py, which looks like::

        {
          "month_end": "2026-04",
          "n_years": 132,
          "m1":  {"inches": ..., "normal": ..., "anomaly": ..., "rank": 97},
          "m3":  {"inches": ..., "normal": ..., "anomaly": ..., "rank": 64},
          "m12": {...},
        }

    Returns the 0-100 percentile derived from the m3 rank, or ``None`` if the
    rank or record length is unavailable (caller should then fall back to the
    "% of normal" path).
    """
    if not isinstance(precip_anomaly, dict):
        return None
    m3 = precip_anomaly.get("m3") or {}
    if not isinstance(m3, dict):
        return None
    rank = m3.get("rank")
    n_years = precip_anomaly.get("n_years")
    return rank_to_percentile(rank, n_years)


def _self_test():
    """Lightweight self-test. Run via ``python forage_percentiles.py``."""
    failures = []

    def check(label, got, want, tol=0.05):
        ok = (got is None and want is None) or (
            got is not None and want is not None and abs(got - want) <= tol
        )
        status = "ok" if ok else "FAIL"
        print(f"  [{status}] {label}: got={got!r} want={want!r}")
        if not ok:
            failures.append(label)

    # Mid-rank percentile, N = 132.
    check("driest rank=1/132", rank_to_percentile(1, 132), 0.4)
    check("wettest rank=132/132", rank_to_percentile(132, 132), 99.6)
    check("median rank=66/132", rank_to_percentile(66, 132), 49.6)
    # Live-verified examples (Beaverhead, MT).
    check("m3 rank=85/132", rank_to_percentile(85, 132), 64.0)
    check("m1 rank=82/132", rank_to_percentile(82, 132), 61.7)
    # Default N when not supplied.
    check("rank=85 default N", rank_to_percentile(85), 64.0)
    # Endpoints stay strictly inside (0, 100).
    p_lo = rank_to_percentile(1, 132)
    p_hi = rank_to_percentile(132, 132)
    check("never exactly 0", (p_lo is not None and p_lo > 0.0), True)
    check("never exactly 100", (p_hi is not None and p_hi < 100.0), True)
    # Bad / missing inputs return None (-> caller falls back).
    check("rank None", rank_to_percentile(None, 132), None)
    check("rank 0 invalid", rank_to_percentile(0, 132), None)
    check("rank > N invalid", rank_to_percentile(140, 132), None)
    check("non-int rank", rank_to_percentile("x", 132), None)
    # N parsing edge cases.
    check("N None -> default", rank_to_percentile(66), 49.6)
    check("N zero -> default", rank_to_percentile(66, 0), 49.6)
    check("N non-int -> default", rank_to_percentile(66, "bad"), 49.6)
    # mi_from_ncei_rank wiring.
    check(
        "mi from m3",
        mi_from_ncei_rank({"n_years": 132, "m3": {"rank": 85}}),
        64.0,
    )
    check("mi no m3", mi_from_ncei_rank({"n_years": 132}), None)
    check("mi no rank", mi_from_ncei_rank({"n_years": 132, "m3": {}}), None)
    check("mi not dict", mi_from_ncei_rank(None), None)
    check(
        "mi default N",
        mi_from_ncei_rank({"m3": {"rank": 85}}),
        64.0,
    )

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} case(s): {failures}")
        return 1
    print("SELF-TEST PASSED")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
