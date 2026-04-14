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
| `precip_ytd`                 | Added v1.1 — see §9 below          | v1.1       |
| `drought`                    | Added v1.1 — see §10 below         | v1.1       |
| `streamflow`                 | Added v1.2 — see §11 below         | **NEW**    |
| `soil_moisture`              | Added v1.2 — see §12 below         | **NEW**    |
| `precip_anomaly`             | Added v1.2 — see §13 below         | **NEW**    |

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

**What was still missing at v1.1.** Temperature / melt-rate modeling,
soil moisture (NRCS SCAN network has only 1 target-county station),
and weighted station averaging. Streamflow, soil moisture (via
Montana Mesonet), and county precipitation anomalies were added in
v1.2 — see §§11–13 below. Temperature/melt-rate modeling and weighted
station averaging are still not implemented.

---

## 11. USGS streamflow (`streamflow` field, added v1.2)

| Choice                                | Source                           | Confidence |
|---------------------------------------|----------------------------------|------------|
| USGS Water Services NWIS              | Public API, no auth              | Exact      |
| `/nwis/dv/` for current daily value   | USGS standard                    | Exact      |
| `/nwis/stat/` for historical percentiles | USGS standard                 | Exact      |
| Parameter code `00060` (discharge)    | USGS standard                    | Exact      |
| One gauge per county (`COUNTY_GAUGES`)| **Curated by hand**              | Unreviewed |
| Linear interpolation between p10/p25/p50/p75/p90 bands | Standard method    | Reasonable |
| Status bands 25 / 75 / above          | **ASSUMPTION**                   | Unreviewed |

**What it does.** For each county, looks up a single stream gauge on
the river that county's page prose actually discusses (e.g. Gallatin
River at Logan for Gallatin County, Milk River at Havre for Blaine
County, Boulder River at Big Timber for Sweet Grass County). Fetches
the most recent daily discharge (cfs) and compares it against the
historical daily stats for that day-of-year (p10, p25, p50, p75, p90
from the full period of record). The percentile is a linear
interpolation between the bands — below p10 extrapolates toward 0,
above p90 toward 100.

**Known caveat — missing p90.** USGS occasionally omits `p90_va` on
high-variance spring days for some gauges (I saw this on 5 of 10
target gauges for April 14: Gallatin, Clarks Fork, Yellowstone at
Livingston, Boulder, Bighorn). When p90 is missing, the script
extrapolates from p75 × 1.3 as a synthetic upper band. This means
percentiles **above the 90th mark may be slightly inflated** toward
100 — if current flow is 2× p75, you'll get 96–100 rather than the
"real" 94 you'd get with actual p90 data. Values in the 0–75 range
are unaffected by this extrapolation and are accurate. The p50
sanity-check on April 14 confirmed the core math: Gallatin at 934
cfs vs p50 966 cfs returns 46 (just below median, correct).

**County → gauge mapping.** The ten gauges in `COUNTY_GAUGES` were
hand-picked from the USGS NWIS site service (`stateCd=mt`,
`siteType=ST`, `parameterCd=00060`, `siteStatus=active`) to match
the river each county's page prose already cites as its principal
water supply. A hydrologist may disagree with some choices — for
instance, Stillwater County could arguably use either the Stillwater
River at Absarokee (the named river in the page) or the Yellowstone
River at Billings (the mainstem the county's lower benchlands pull
from). Update `COUNTY_GAUGES` at the top of `update_snotel.py` if a
different gauge better represents a county's agricultural water
picture.

**Status bands.** I chose 0-24 = Below Normal, 25-75 = Normal,
76-100 = Above Normal. This is the USGS WaterWatch convention and
maps cleanly onto the rest of the schema's Below/Normal/Above
framing. A more granular "Much Below / Below / Normal / Above / Much
Above" at 10/25/75/90 is available if a rancher-facing review asks
for it.

---

## 12. Soil moisture via Montana Mesonet (`soil_moisture` field, added v1.2)

| Choice                                | Source                           | Confidence |
|---------------------------------------|----------------------------------|------------|
| Montana Mesonet API v2                | mesonet.climate.umt.edu/api/v2   | Exact      |
| `/stations/?type=json`                | Mesonet OpenAPI spec             | Exact      |
| Filter `has_swp == true`              | Mesonet field for SWP sensors    | Exact      |
| Volumetric water content (VWC) %      | Mesonet element                  | Exact      |
| Shallow = 5/10 cm, deep = 50/100 cm   | **ASSUMPTION**                   | Unreviewed |
| Flat average across stations in county| Matches §3 SWE approach          | Consistent |
| Soil moisture does NOT influence forage_score | **Intentional** (see §6)  | Deliberate |

**Why Mesonet instead of NRCS SCAN.** The NRCS SCAN network has 8
stations in Montana, but **only one** (Table Mountain in Gallatin) is
in one of our 10 target counties. That gives 1/10 counties with soil
moisture coverage from NRCS. The Montana Mesonet (operated by the
Montana Climate Office at UMT) has **211 stations statewide with 26
`has_swp=true` stations across 8 of the 10 target counties**
(Big Horn, Blaine, Carbon, Gallatin, Park, Stillwater, Sweet Grass,
Yellowstone). Lewis and Clark and Meagher currently have zero
Mesonet SWP stations and return `soil_moisture: null`.

**Field parser.** Mesonet encodes sensor data in its `/latest/`
response using human-readable keys like `"Soil VWC @ -5 cm [%]": 27.95`.
The script parses these via regex `^Soil VWC @ -(\d+) cm` and
extracts the depth in centimeters.

**Depth bucketing.** Shallow = {5, 10} cm = grass/forb root zone,
most responsive to recent precipitation and melt. Deep = {50, 100}
cm = subsoil storage reached by shrub and tree roots, slower to
respond, indicative of season-long conditions. A rangeland soil
scientist may prefer a different split (e.g. 0-20 cm vs 20-50 cm, or
reporting all 5 depths without bucketing). The bucketing happens in
`aggregate_mesonet_soil_moisture()` — it's two lines to change.

**Sample from 2026-04-14 dry run:**

| County       | Shallow VWC % | Deep VWC % | Stations |
|--------------|---------------|------------|----------|
| Big Horn     | 22.4          | 18.9       | 6        |
| Blaine       | 28.0          | 23.3       | 3        |
| Carbon       | 18.8          | 16.2       | 3        |
| Gallatin     | 18.6          | 12.1       | 2        |
| Lewis & Clark| null          | null       | 0        |
| Meagher      | null          | null       | 0        |
| Park         | 24.8          | 19.7       | 2        |
| Stillwater   | 24.7          | 22.0       | 5        |
| Sweet Grass  | 24.7          | 20.4       | 3        |
| Yellowstone  | 22.2          | 19.6       | 2        |

Eight of ten counties now have soil moisture data — compared to
1/10 if the script had tried to use NRCS SCAN only.

**Why soil moisture does not enter the forage score.** Same rationale
as precipitation in §9: unreviewed ramps plus an undefined combining
formula. v1.2 makes the data **present** without making the score
depend on it. Review together with the SWE ramps in §6.

---

## 13. NOAA NCEI county precipitation anomaly (`precip_anomaly` field, added v1.2)

| Choice                                | Source                           | Confidence |
|---------------------------------------|----------------------------------|------------|
| NOAA NCEI Climate at a Glance (CAG)   | Public endpoint                  | Exact      |
| `/cag/county/mapping/{state}-pcp-{yyyymm}-{n}.json` | CAG URL pattern     | Verified   |
| NCEI state code 24 = Montana          | NCEI nCLIMDIV convention, NOT FIPS | Exact   |
| County keyed as `MT-NNN` where NNN = last 3 of FIPS | CAG response shape    | Verified   |
| Periods: 1-month, 3-month, 12-month   | **Chosen** for short/medium/long context | Design |
| Try current month, fall back to previous | NCEI publishes early in following month | Reasonable |

**What it does.** For each target county, reports precipitation for
1-month, 3-month, and 12-month periods ending in the most recent
completed NCEI calendar month. Each period returns:

- `inches` — actual accumulated precipitation
- `normal` — 1901–2000 climatological mean for the same period
- `anomaly` — difference (inches, signed)
- `rank` — rank of this period against the full historical record
  (1 = driest, higher = wetter)

The script fetches 3 NCEI URLs per run (one per period), each of
which returns **all 56 Montana counties in one response**. Total
NCEI network cost: 3 HTTP calls for 10 counties.

**Sample from 2026-04-14 dry run** (month_end = 2026-03, which was
the latest available at NCEI on April 14):

| County       | 1-mo anomaly | 3-mo anomaly | 12-mo anomaly | 3-mo rank |
|--------------|--------------|--------------|---------------|-----------|
| Big Horn     | +0.05        | −0.70        | +3.40         | 24        |
| Blaine       | +0.09        | −0.69        | +0.71         | 13        |
| Carbon       | +0.10        | −1.81        | −0.54         | 11        |
| **Gallatin** | −0.76        | **−2.13**    | −3.06         | **9**     |
| Lewis & Clark| +1.05        | −1.09        | −2.05         | 23        |
| Meagher      | +0.32        | −0.98        | −3.22         | 34        |
| Park         | −0.13        | −1.58        | −1.72         | 19        |
| Stillwater   | +0.20        | −1.03        | +0.29         | 30        |
| Sweet Grass  | +0.07        | −1.45        | −2.06         | 21        |
| Yellowstone  | +0.30        | −0.35        | +4.12         | 51        |

Note the signal divergence at different timescales: Big Horn and
Yellowstone have near-normal 1-month totals but a strong **wet**
12-month anomaly. Gallatin's 3-month rank of 9 (out of ~130 years of
record) makes this winter the 9th-driest January–March on record
for that county — a signal no other field in the JSON captures.

**Known caveats.**

- **Monthly resolution.** NCEI publishes by completed calendar
  month. On April 14, the most recent available data is March 2026.
  True "last 30 days" rolling anomalies would need a daily-gridded
  product like PRISM or CPC (substantially more complex).
- **NCEI state code ≠ FIPS.** NCEI uses its own 2-digit state code
  system derived from the old NWS numbering. Montana is 24 in NCEI,
  not 30. (NCEI's 30 is New York.) The constant `NCEI_STATE_MT = 24`
  at the top of the script captures this.
- **No forecast.** NCEI is observational only.
- **Rank denominator.** Rank denominators vary slightly by period
  length because the period-of-record the NCEI historical series
  covers differs; I'm storing the rank integer but not the total, so
  it's not directly interpretable as a percentile without cross-
  referencing NCEI's documentation. A future enhancement could
  derive `rank_out_of` or `rank_percentile` client-side.

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
4. **Streamflow status bands** at 25 / 75 percentile (§11). Different
   semantic from % of median — based on day-of-year percentile bands.
5. **Which of precipitation, drought, streamflow, soil moisture, or
   county precip anomaly should enter the forage score** (§§6, 9,
   10, 11, 12, 13). Five new informational inputs added across v1.1
   and v1.2; need rangeland-scientist opinion before any of them
   influence a composite score.
6. **Soil moisture depth bucketing** (§12). Shallow = 5/10 cm, deep
   = 50/100 cm was a choice; a soil scientist may want a different
   split or no bucketing at all.
7. **USGS gauge selection per county** (§11). Each of the 10 gauges
   in `COUNTY_GAUGES` was hand-picked; a hydrologist may disagree
   with some of the choices.
8. **Trend noise floor** of 0.2" SWE (§4). Smaller impact, easy to
   get right from NRCS.
9. **Equal-weighted station averaging** (§3). The memo already flags
   this for upgrade; a scientist can tell us what to weight by.

Items 1–3 determine whether a rancher ever trusts this product.
Item 5 determines whether v1.1 + v1.2's new data fields get composed
into a single score or stay as separate informational signals.
Items 6 and 7 are data-quality reviews that refine the v1.2
additions but don't block them from being useful.
