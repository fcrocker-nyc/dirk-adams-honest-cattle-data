# SOURCES — provenance of every number in `update_snotel.py`

This file exists so the next person who touches the SNOTEL automation
knows exactly which values came from the Technical Memo, which came
from public NRCS / USDM documentation, and which are working
assumptions introduced when writing `update_snotel.py`. Assumptions
are flagged loudly so a rangeland scientist or NRCS reviewer can
challenge them specifically rather than having to reverse-engineer
the script.

Short version: the *shape* of the automation (schema, data sources,
aggregation approach, update cadence) is anchored in the memo and in
standard NRCS / USDM practice. The *numeric thresholds* — both the
status bands and the forage-score ramps — are assumptions. They
have not been reviewed by a rangeland scientist.

---

## 1. JSON output schema

| Field                        | Source                             | Confidence |
|------------------------------|------------------------------------|------------|
| `county`                     | Memo §3.A example                  | Exact      |
| `date`                       | Memo §3.A example                  | Exact      |
| `swe_index`                  | Memo §3.A example                  | Exact      |
| `percent_of_median`          | Memo §3.A example                  | Exact      |
| `trend`                      | Memo §3.A example (↑ ↓ →)          | Exact      |
| `status`                     | Memo §3.A example + §1 list        | Exact      |
| `forage_score`               | Memo §3.A example + §4             | Exact      |
| `precip_ytd`                 | Added v1.1 — see §9 below          | **NEW**    |
| `drought`                    | Added v1.1 — see §10 below         | **NEW**    |

The original seven fields are taken verbatim from the memo's example
JSON object and the "Key outputs" list in §1. Field names, types, and
the use of Unicode trend arrows are all memo-sourced.

`precip_ytd` and `drought` were added in v1.1 (April 2026) to
partially close the memo's §6 limitations ("Limited incorporation of
precipitation vs. snowpack distinction") and to supply meaningful data
for the plains counties (Big Horn, Blaine, Stillwater, Yellowstone)
which have no NRCS SNOTEL coverage and therefore previously rendered
as "No Snowpack, forage 40" with nothing else to say. Both blocks are
**additive and nullable** — consumers that only read the original
seven fields are unaffected.

**Confidence: high for the original seven; medium for `precip_ytd`
and `drought`** — the values themselves come directly from NRCS and
USDM, but the schema shapes around them (`status` field, cumulative
vs non-cumulative percentages, `worst_class` derivation) are design
choices documented in §9 and §10.

---

## 2. Data sources

| Source                             | Used for                           | Confidence |
|------------------------------------|------------------------------------|------------|
| USDA NRCS SNOTEL network           | SWE, water-year precipitation      | Memo §2    |
| USDA NRCS AWDB REST API            | Data transport                     | Exact      |
| USDA Drought Monitor (NDMC)        | County-level drought classification| Added v1.1 |
| USDM Data Services API             | Data transport                     | Exact      |

### 2.1 NRCS AWDB REST

| Element                            | Source                             | Confidence |
|------------------------------------|------------------------------------|------------|
| `elements=WTEQ` for SWE            | NRCS element code convention       | Exact      |
| `elements=PREC` for water-year precip | NRCS element code (verified in dry run) | Exact |
| Multi-element query `WTEQ,PREC`    | Confirmed working against live API | Verified   |
| `duration=DAILY`                   | NRCS standard daily roll-up        | Exact      |
| `centralTendencyType=MEDIAN`       | NRCS API documentation             | Exact      |
| Client-side filter `stateCode=MT` + `networkCode=SNTL` | `/stations` server-side filters are silently ignored — confirmed via dry run | Verified |
| Station → county via `countyName`  | Standard AWDB metadata field       | Exact      |
| Batch `/data` in groups of 20       | AWDB 500s at ~30+ triplets         | Verified   |
| Recursive split on HTTP 500         | Isolates bad stations (e.g. 690:MT:SNTL / Pickfoot Creek) | Verified |
| Median read inline from latest-dated value | Confirmed via dry run — NRCS lags a day so "today" is typically absent | Verified |

**Confidence: high.** Every endpoint quirk listed as "Verified" was
surfaced during dry-run discovery against the live AWDB endpoint and
is defended by an explicit workaround in the script. Details:

- The `/stations` endpoint **silently ignores** server-side
  `stateCds`, `networkCds`, and `activeOnly` filters. It returns the
  full global station list (~4,390). The script filters client-side
  via `stateCode == "MT" and networkCode == "SNTL"`, yielding ~96
  real Montana SNOTEL stations.
- The `/data` endpoint returns HTTP 500 on station batches larger
  than ~25–30 triplets AND on specific "bad" stations that trigger
  internal errors regardless of batch size. The known-bad station is
  `690:MT:SNTL` (Pickfoot Creek, Meagher County). The script batches
  in groups of 20 and recursively splits any failing batch in half,
  ultimately skipping individual stations that still 500 with a log
  line to stderr.
- Median values come back **inline on each daily value item**
  (`{"date":"2026-04-13","value":17.3,"median":19.9}`) when
  `centralTendencyType=MEDIAN` is requested, NOT in a separate
  `centralTendencyValues` list. The script reads `v.get("median")`
  on the latest-dated value.

### 2.2 USDA Drought Monitor (USDM)

| Element                            | Source                             | Confidence |
|------------------------------------|------------------------------------|------------|
| API base `usdmdataservices.unl.edu/api` | USDM public data services       | Exact      |
| Endpoint `/CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent` | USDM API docs | Exact |
| `statisticsType=1` → area percent  | USDM API docs                      | Exact      |
| `aoi=<county FIPS>` — one call per county | USDM API docs               | Exact      |
| ISO dates (`YYYY-MM-DD`)           | Tested against M/D/YYYY too — both work | Verified |
| Cumulative percent semantics (d2 = D2-or-worse) | Confirmed by inspection of Meagher's response | Verified |

**Confidence: high.** The script queries a 3-week window (USDM
publishes weekly on Thursdays, valid-through-Tuesday) and takes the
row with the latest `validEnd`. One HTTP call per county = 10 calls
per daily run. At USDM's documented request rate this is comfortable.

**Caveat:** I could not reach the sandbox host from the container I
wrote this in; verification happened from a macOS dev workstation via
a proxied route that required disabling SSL verification locally. The
**committed script uses default SSL verification** — GitHub Actions
runners with stock Ubuntu trust NRCS and USDM certs fine, so the CI
run will be the first fully-trusted end-to-end execution. If it
fails, check the `[snotel]` stderr lines in the Actions log.

---

## 3. Station aggregation method

| Choice                                | Source                             | Confidence |
|---------------------------------------|------------------------------------|------------|
| Equal-weighted average across stations| Memo §2: "currently equal-weighted"| Exact      |
| Aggregation per county                | Memo §2                            | Exact      |
| Counties with no station → template   | Memo §3.A example (Blaine)         | Exact      |
| Flagged as limitation                 | Memo §6 + §7.A                     | Exact      |

**Confidence: high.** The memo explicitly calls equal weighting the
"current" approach and §7.A calls out elevation / grazing-area
weighting as the intended next upgrade. The script reflects "current",
not "next".

Precipitation aggregation in v1.1 follows the same equal-weighted
mean approach, across the same stations, with the same null handling
for counties that have zero SNOTEL coverage.

---

## 4. Trend calculation

| Choice                                | Source                             | Confidence |
|---------------------------------------|------------------------------------|------------|
| Rolling window length                 | Memo §3.B: "rolling 7–14 day"      | In range   |
| Chose 7 days specifically             | **ASSUMPTION**                     | Unreviewed |
| Noise floor of ±0.2" SWE for flat     | **ASSUMPTION**                     | Unreviewed |
| Arrow glyphs ↑ ↓ →                    | Memo §3.A                          | Exact      |
| Trend applies to SWE only, not precip | Design choice (see note)           | Intentional|

**Assumption flags:**

- **7-day window.** The memo says "7–14 day movement." Picked the low
  end because a 7-day window is more responsive during spring melt,
  which is when ranchers care most. A 14-day window would be
  smoother. Either is inside the memo's stated range.
- **±0.2" noise floor.** Rule of thumb for daily SNOTEL pillow noise.
  Not calibrated against actual NRCS QA/QC data. A hydrologist should
  tell us the real figure. Constant lives in `compute_trend()`.
- **No trend on precip.** Water-year PREC is monotonic (it only goes
  up as more precip falls), so a ↑/↓/→ trend is meaningless for
  `precip_ytd`. The `precip_ytd.status` field (Below/Normal/Above)
  still captures whether accumulation is keeping pace with median.

---

## 5. Status classification (ASSUMPTIONS)

### 5.1 SWE status

| Band           | Percent-of-median range | Source             |
|----------------|-------------------------|--------------------|
| No Snowpack    | 0% or SWE ≤ 0           | **ASSUMPTION**     |
| Below Normal   | 1–69%                   | **ASSUMPTION**     |
| Normal         | 70–109%                 | **ASSUMPTION**     |
| Above Normal   | 110%+                   | **ASSUMPTION**     |

The memo lists the four status labels (§1) but **never publishes the
numeric thresholds.** Picked 70 / 110 because those are the thresholds
the NRCS National Water and Climate Center uses in some of its Water
Supply Outlook bulletins — but those bulletins are about basin water
supply, not rangeland forage, and transposing the numbers without a
citation that says it's appropriate for grazing decisions is a leap.

The memo's §6 explicitly lists "static thresholds for status
classification" as a known limitation. The memo author was already
aware this was a provisional call.

### 5.2 Precipitation status (added v1.1)

| Band           | Percent-of-median range | Source             |
|----------------|-------------------------|--------------------|
| Below Normal   | <70%                    | **ASSUMPTION**     |
| Normal         | 70–109%                 | **ASSUMPTION**     |
| Above Normal   | 110%+                   | **ASSUMPTION**     |

`precip_ytd.status` uses the SAME 70/110 cutoffs as SWE. This is a
mechanical design choice, NOT a scientific claim that the same ramps
are appropriate for precipitation. The precip ramps inherit whatever
review outcome the SWE ramps get.

**Who should review:** MSU Extension rangeland specialists, the
Montana NRCS State Office (Bozeman), or a USDA ARS Fort Keogh
(Miles City) rangeland scientist. A one-hour review marking up the
bands would be enough.

---

## 6. Forage score (ASSUMPTION)

| Status        | Forage score output  | Source             |
|---------------|----------------------|--------------------|
| No Snowpack   | exactly 40           | Memo §3.A example  |
| Below Normal  | 50–65, linear in %   | **ASSUMPTION**     |
| Normal        | 65–85, linear in %   | **ASSUMPTION**     |
| Above Normal  | 85–100, capped       | **ASSUMPTION**     |

The memo says the forage score is:

> "a synthetic metric linking snowpack to expected grazing conditions"
> with inputs of "SWE levels, timing of melt, historical correlations
> with forage yield" outputting a "0–100 scale" (§4).

The **only numeric anchor** the memo publishes is the Blaine example
in §3.A, which shows `"forage_score": 40` under `"status": "No
Snowpack"`. Everything else in the scoring function is interpolation
chosen to produce a monotonic 0–100 curve that hits that one anchor.

Specifically, v1.1 does NOT:

- Consume "timing of melt".
- Consume "historical correlations with forage yield".
- Fold in the new `precip_ytd` field (even though the memo's §7.A
  explicitly calls out precipitation as a recommended addition).
- Fold in the new `drought` field.

The decision to keep precipitation and drought out of the forage
score in v1.1 is **deliberate** for two reasons:

1. The forage-score ramps themselves are already flagged as unreviewed
   (see the table above). Mixing in a second and third unreviewed
   input would compound the credibility problem rather than fix it.
2. There is no published or defensible formula anywhere in the memo
   for combining SWE%, precipitation%, and drought class into a
   single score. Anything invented here would be another assumption
   stacked on top.

So v1.1 makes precipitation and drought **present** in the JSON —
ranchers and scientists can look at `precip_ytd.percent_of_median`
and `drought.worst_class` / `drought.d2_pct` on any county page —
**without** making the forage score depend on them. Once a rangeland
scientist reviews the status thresholds and forage ramps, the same
review should define how (and whether) precipitation and drought
enter the score.

§7.B explicitly recommends replacing the whole thing with a
regression model:

> "Build regression models linking: SWE → forage yield → calf weights
> → market pricing"

Until such a regression exists, every `forage_score` field in every
county JSON is a deterministic function of SWE `percent_of_median`
via the ramps in `forage_score()`. Treat it as a smoothed restatement
of snowpack status, not as a validated forage estimate.

---

## 7. Update cadence

| Choice                               | Source                    | Confidence |
|--------------------------------------|---------------------------|------------|
| Daily refresh                        | Memo §7.C                 | Exact      |
| 11:30 UTC cron (~05:30 Mountain)     | **ASSUMPTION**            | Reasonable |
| Commit only on content change        | **ASSUMPTION**            | Reasonable |

**11:30 UTC:** NRCS daily roll-up typically finalizes overnight US
Mountain time; running at 05:30 Mountain catches fresh data without
hammering the API during the roll-up window. If NRCS changes their
roll-up schedule this cron should move with it. USDM updates
Thursdays around 08:30 UTC, so the daily cron catches fresh drought
data once a week without special handling.

**Commit-only-on-change:** Commit-history hygiene. If the site ever
wants a "last fetched at" timestamp even on unchanged days, drop
this behavior or add a separate heartbeat file.

---

## 8. Active county list

| Choice                               | Source                         | Confidence |
|--------------------------------------|--------------------------------|------------|
| The ten counties in `ACTIVE_COUNTIES`| Surveyed from honestcattle.net | Exact      |
| County → slug mapping (underscores)  | Surveyed from the data repo    | Exact      |
| County → FIPS mapping (USDM lookups) | US Census Bureau FIPS codes    | Exact      |
| Including Blaine                     | Site nav includes Blaine       | Exact      |

Pulled directly from the honestcattle.net "Counties" nav menu on
2026-04-14 and cross-checked against the filenames in the
`fcrocker-nyc/dirk-adams-honest-cattle-data` repo. FIPS codes come
from the US Census Bureau (state prefix 30 = Montana). If the site
adds or drops a county page, update both `ACTIVE_COUNTIES` and
`COUNTY_FIPS` at the top of `update_snotel.py`.

---

## 9. Water-year precipitation (`precip_ytd` field, added v1.1)

| Choice                                | Source                           | Confidence |
|---------------------------------------|----------------------------------|------------|
| Fetch `PREC` element from AWDB        | NRCS AWDB standard element code  | Verified   |
| Query alongside `WTEQ` in one call    | AWDB accepts multi-element query | Verified   |
| Value is water-year cumulative inches | NRCS convention, water year starts Oct 1 | Exact |
| Flat average across stations          | Matches §3 SWE approach          | Consistent |
| Percent-of-median derived client-side | Same as SWE                      | Consistent |
| Status via §5.2 thresholds            | **ASSUMPTION**                   | Unreviewed |
| `precip_ytd = null` if no stations    | Matches SWE "No Snowpack" gap    | Intentional|
| Precip does NOT influence forage_score| **Intentional** (see §6)         | Deliberate |

**What it does.** Each daily run now requests `WTEQ,PREC` from the
AWDB `/data` endpoint for every Montana SNOTEL station in an active
county. For each station that reports PREC, the script captures the
most recent value (inches) and the corresponding median (from the
same inline mechanism as SWE). County-level `inches` is the flat
average across reporting stations; `percent_of_median` is the county
current divided by the mean of station medians × 100.

**Sample from 2026-04-14 dry run** (all 10 counties, values I saw):

| County       | Precip inches | % of median | Status        |
|--------------|---------------|-------------|---------------|
| Big Horn     | (null)        | —           | —             |
| Blaine       | (null)        | —           | —             |
| Carbon       | 9.55          | 75          | Below Normal  |
| Gallatin     | 23.45         | 98          | Normal        |
| Lewis & Clark| 28.35         | 148         | Above Normal  |
| Meagher      | 17.28         | 107         | Normal        |
| Park         | 22.60         | 113         | Above Normal  |
| Stillwater   | (null)        | —           | —             |
| Sweet Grass  | 18.10         | 103         | Normal        |
| Yellowstone  | (null)        | —           | —             |

Plains counties with zero NRCS SNOTEL coverage (Big Horn, Blaine,
Stillwater, Yellowstone) get `"precip_ytd": null`. Their drought
block (see §10) is still populated, so those counties aren't
informationally empty anymore.

**Known caveats.**

- **Water year reset on Oct 1.** PREC resets to 0 each October 1
  (start of NRCS water year). Early in the water year, the raw
  inches look small and `percent_of_median` may be volatile with
  small absolute values. The status field handles this naturally.
- **Station-to-watershed mismatch.** A county's irrigated land may
  get water from stations outside the county's borders (e.g. Blaine
  County's water comes from St. Mary headwaters in Glacier County).
  The current implementation aggregates only stations whose NRCS
  `countyName` matches the target county. Proxy station mapping is
  future work (same as the SWE situation).
- **PREC is cumulative, not incremental.** Daily change in PREC is
  precipitation that fell that day. The script does not break this
  out, but it's available in the same response for future use.

---

## 10. USDA Drought Monitor (`drought` field, added v1.1)

| Choice                                | Source                           | Confidence |
|---------------------------------------|----------------------------------|------------|
| Fetch from USDM data services         | USDM public API                  | Exact      |
| One call per county (10 calls/run)    | USDM has no MT-state batch endpoint | Exact |
| 3-week window → take latest validEnd  | USDM updates weekly              | Reasonable |
| `statisticsType=1` (area percent)     | USDM API param docs              | Exact      |
| Cumulative percentages (d2 = D2+)     | Matches USDM public phrasing     | Verified   |
| `worst_class` derived client-side     | Convenience field                | Design     |
| Drought does NOT influence forage_score | **Intentional** (see §6)       | Deliberate |

**What it does.** Each daily run makes one `/CountyStatistics/
GetDroughtSeverityStatisticsByAreaPercent` call per active county,
querying a 3-week window and taking the row with the latest
`validEnd`. USDM publishes weekly on Thursdays (valid through the
following Tuesday), so a 3-week window guarantees we catch the most
recent map regardless of what day of the week the cron fires.

**Schema detail.** The five `dN_pct` fields are **cumulative** — a
value of 77.98 in `d2_pct` means "77.98% of the county is in D2
(Severe Drought) OR WORSE", including D3 and D4. This is the
semantic USDM publishes and matches the phrasing used in the
honestcattle.net page prose (e.g. "D2 Severe Drought across 85% of
the county"). A convenience field `worst_class` exposes the highest
drought bucket with any area ("D4", "D3", "D2", "D1", "D0", or
`None` if the county is drought-free).

To recover "percent ONLY in D2" from the cumulative form, subtract
the next level up: `only_d2 = d2_pct - d3_pct`. The script stores
the cumulative form because it's the form consumers ask about most
often ("how much of the county is in D2 or worse?").

**Sample from 2026-04-14 dry run** (all 10 counties, percentages I
saw; every county is currently in some level of drought):

| County       | validEnd   | D2+ | worst_class |
|--------------|------------|-----|-------------|
| Big Horn     | 2026-04-13 | 15.82 | D2        |
| Blaine       | 2026-04-13 | 71.18 | D2        |
| Carbon       | 2026-04-13 | 67.74 | D2        |
| Gallatin     | 2026-04-13 | 78.10 | D2        |
| Lewis & Clark| 2026-04-13 | 37.11 | D2        |
| Meagher      | 2026-04-13 | 79.53 | D2        |
| Park         | 2026-04-13 | 40.41 | D2        |
| Stillwater   | 2026-04-13 | 50.59 | D2        |
| Sweet Grass  | 2026-04-13 | 57.81 | D2        |
| Yellowstone  | 2026-04-13 | 38.18 | D2        |

All 10 counties are populated and meaningfully differentiated.
Lewis & Clark has the only non-zero `none_pct` (0.29% of the county
drought-free) in the current map.

**Known caveats.**

- **Weekly cadence vs daily cron.** USDM refreshes once a week. For
  six days out of seven, `drought.valid_end` won't change. The
  commit-only-on-change logic in `main()` handles this cleanly.
- **No forecast / projection.** USDM is a backward-looking
  classification based on observations. It does NOT predict future
  drought.
- **10 HTTP calls per run.** USDM is a low-traffic API; this is
  comfortably within reasonable rate limits. If USDM ever publishes
  a batch endpoint for all counties in a state, collapse the loop.

**What's still missing from memo §6 and §7.A.** Temperature /
melt-rate modeling, soil moisture (NRCS SCAN network has thin MT
coverage, would need proxy mapping), and weighted station averaging
(terrain or grazing-area weights) are still not implemented.
Precipitation and drought were the cheapest and most impactful
additions for the plains counties; the other three are larger
changes.

---

## Summary of what actually needs a scientist

If you only get one round of scientific review, spend it on these
items in priority order:

1. **Forage score ramps** (§6). Biggest credibility risk — the score
   appears on every county page and is interpolation from a single
   data point.
2. **SWE status thresholds** at 70% / 110% (§5.1). Drive the label
   ranchers see first on every page.
3. **Precipitation status thresholds** (§5.2). Same 70/110 cutoffs
   applied by convention to precip; needs its own review.
4. **Whether precipitation and/or drought should enter the forage
   score** (§6, §9, §10). Fresh additions in v1.1; needs a rangeland
   scientist's opinion before any downstream logic trusts them.
5. **Trend noise floor** of 0.2" SWE (§4). Smaller impact, easy to
   get right from NRCS.
6. **Equal-weighted station averaging** (§3). The memo already flags
   this for upgrade; a scientist can tell us what to weight by.

Items 1–3 determine whether a rancher ever trusts this product.
Item 4 determines whether v1.1's new data is credible as a composite
signal versus as standalone informational fields.
