# Technical Memo: SNOTEL Automation & County Navigation System

**Date:** April 17, 2026
**Author:** Claude Opus 4.6 (via Claude Code)
**Site:** honestcattle.net
**Repo:** github.com/fcrocker-nyc/dirk-adams-honest-cattle-data

---

## 1. System Architecture

The honestcattle.net county moisture system has three layers:

1. **Data pipeline** (`update_snotel.py` + GitHub Actions) — runs daily at 11:30 UTC, pulls from 5 public APIs, writes 56 JSON files to the GitHub repo
2. **WordPress plugin** (`Honest Cattle — County Conditions Dashboard` mu-plugin, v1.1.0) — reads JSON files via raw.githubusercontent.com and renders a 6-tile dashboard via the `[hc_snotel county="slug"]` shortcode
3. **WordPress pages** — 56 county detail pages + 1 county grid/navigation page, all using the classic editor (Elementor disabled via `_elementor_edit_mode: ""`)

These layers are fully decoupled. The pipeline writes files. The plugin reads files. The pages contain shortcodes. Changing any one layer doesn't break the others.

---

## 2. Data Pipeline

### 2.1 Script: `update_snotel.py`

Location: repo root. Standard library only (no pip deps). Runs on Python 3.11 in GitHub Actions.

**Five data sources, fetched in order:**

| Source | API | What it provides | Coverage |
|---|---|---|---|
| NRCS AWDB | `wcc.sc.egov.usda.gov/awdbRestApi/services/v1` | SWE + water-year precip (WTEQ, PREC elements) | 26 mountain counties |
| USDA Drought Monitor | `usdmdataservices.unl.edu/api` | D0–D4 county drought classification | All 56 |
| USGS Water Services | `waterservices.usgs.gov/nwis` | Daily discharge (cfs) + day-of-year percentile | 51 counties (5 null) |
| Montana Mesonet | `mesonet.climate.umt.edu/api/v2` | Soil VWC at shallow/deep profiles | 37 counties |
| NOAA NCEI CAG | `ncei.noaa.gov/cag/county/mapping` | 1/3/12-month county precip anomaly | All 56 |

### 2.2 NRCS AWDB Quirks (Critical — discovered via dry runs)

These are NOT documented in the NRCS API docs. They were found empirically:

- **Server-side filters are silently ignored.** `stateCds=MT` and `networkCds=SNTL` passed to `/stations` are accepted without error but return ALL 4,390 stations nationwide. The script filters client-side by `stateCode == "MT"` and `networkCode == "SNTL"` (96 real MT SNOTEL stations).

- **Do NOT `.title()` the countyName field.** NRCS returns `"Lewis and Clark"` (lowercase "and"). Python's `.title()` converts it to `"Lewis And Clark"` which breaks the dictionary lookup. Use the raw value.

- **`/data` endpoint 500s on batches > ~25 triplets.** The script batches station triplets in groups of 20. Even so, specific stations cause 500s independently:
  - `690:MT:SNTL` (Pickfoot Creek, Meagher County) — consistently 500s
  - `693:MT:SNTL` (Pike Creek, Pondera County) — intermittently 500s
  - The `_fetch_data_chunk()` function recursively splits failing batches in half, isolating bad stations to single-station requests, then skipping with a log line.

- **Median is inline on each value item, not in `centralTendencyValues`.** When `centralTendencyType=MEDIAN` is requested, each daily value comes back as `{"date":"2026-04-13","value":17.3,"median":19.9}`. The `centralTendencyValues` array is always empty. Read `v.get("median")` from the latest-dated value.

- **NRCS data lags one day.** Requesting through today's date typically returns values only through yesterday. The script reads the median from whichever is the latest-dated value rather than checking for today's exact date.

### 2.3 USGS Streamflow Quirks

- **USGS `/nwis/stat/` occasionally returns empty `p90_va` for spring dates.** On high-variance spring days (April), USGS omits the 90th-percentile column for some gauges. The script extrapolates `p90 ≈ p75 × 1.3` when missing. This means percentiles above 75 may be slightly inflated for those gauges on those dates. Verified: p10/p25/p50/p75 percentiles are accurate when present.

- **One gauge per county is hardcoded in `COUNTY_GAUGES`.** These were hand-picked by searching `waterservices.usgs.gov/nwis/site/?format=rdb&countyCd={fips}&siteType=ST&parameterCd=00060&siteStatus=active`. A hydrologist may disagree with some picks. The mapping lives at the top of `update_snotel.py`.

- **Five counties have no active in-county USGS discharge gauge:** Carter, Fallon, Garfield, Prairie, Wibaux. Their `streamflow` field is `null`. Handled gracefully by the plugin.

### 2.4 USDM (Drought Monitor)

- **Weekly data, daily fetch.** USDM publishes Thursdays. The script queries a 3-week window and takes the row with the latest `validEnd`. For 6 of 7 days per week, the drought data doesn't change — the script's content-change detection avoids unnecessary commits.

- **Cumulative percentages.** `d2_pct = 40` means "40% of the county is in D2 (Severe) OR WORSE" — includes D3 and D4. This matches the USDM's own phrasing and the county page prose.

- **NCEI state code ≠ FIPS.** Montana is **24** in NCEI's system, not 30 (which is New York). The constant `NCEI_STATE_MT = 24` is at the top of the script. Getting this wrong returns New York counties.

### 2.5 Montana Mesonet

- **The `type` parameter is for output FORMAT, not station type.** Use `?type=json` to get JSON. The Mesonet API uses human-readable field names in responses: `"Soil VWC @ -5 cm [%]": 27.95`. The script parses these via regex.

- **`has_swp` flag.** Only stations with soil water profile sensors (`has_swp: true`) report VWC. The script filters on this field.

- **19 counties have no Mesonet SWP coverage** — mostly western mountain counties (Flathead, Glacier, Granite, Jefferson, Lincoln, Mineral, Missoula, etc.). NRCS SCAN was evaluated as an alternative but only has 1 station in our target counties (Table Mountain in Gallatin). Mesonet was chosen because it has 91+ SWP stations across the target set.

### 2.6 JSON Schema (v1.2)

Each `<county>.json` file:

```json
{
  "county": "park",
  "date": "2026-04-17",
  "swe_index": 13.34,
  "percent_of_median": 78,
  "trend": "→",
  "status": "Normal",
  "forage_score": 69,
  "precip_ytd": {"inches": 23.49, "percent_of_median": 115, "status": "Above Normal"},
  "drought": {"valid_end": "2026-04-13", "none_pct": 0, "d0_pct": 100, "d1_pct": 85.56, "d2_pct": 40.41, "d3_pct": 0, "d4_pct": 0, "worst_class": "D2"},
  "streamflow": {"gauge_name": "Yellowstone River near Livingston", "site_no": "06192500", "cfs": 3350, "percentile": 91, "status": "Above Normal"},
  "soil_moisture": {"shallow_vwc_pct": 24.6, "deep_vwc_pct": 21.3, "station_count": 3, "source": "Montana Mesonet"},
  "precip_anomaly": {"month_end": "2026-03", "m1": {"inches": -0.13, "normal": 1.58, "anomaly": -0.13, "rank": 45}, "m3": {...}, "m12": {...}}
}
```

The original 7 fields (`county` through `forage_score`) are the v1.0 schema from the original Technical Memo. All v1.1/v1.2 additions (`precip_ytd`, `drought`, `streamflow`, `soil_moisture`, `precip_anomaly`) are additive and nullable. If a data source is unavailable for a county, that block is `null`.

### 2.7 Workflow: `.github/workflows/snotel-update.yml`

- Cron: `30 11 * * *` (11:30 UTC ≈ 5:30 AM Mountain)
- Also: `workflow_dispatch` for manual runs from the Actions tab
- Permissions: `contents: write` (uses `GITHUB_TOKEN`)
- **Critical fix (commit 284c5f7):** Added `git pull --rebase origin main` before `git push` to handle concurrent pushes during the ~6 minute run window. Without this, any push during the run causes a non-fast-forward rejection.
- Commit message format: `auto update: SNOTEL refresh YYYY-MM-DD`
- Only commits when file content actually changes (avoids daily noise)

---

## 3. WordPress Architecture

### 3.1 Plugin

**Name:** Honest Cattle — County Conditions Dashboard
**Location:** `wp-content/mu-plugins/` (must-use plugin, auto-loaded)
**Version:** 1.1.0
**Shortcode:** `[hc_snotel county="slug"]`

The plugin registers the shortcode on the `wp_loaded` hook (NOT at plugin load time) to win the race against Code Snippets, which defines an older `[hc_snotel]` on `init`. Late registration + explicit `remove_shortcode()` ensures the plugin's version always wins.

The plugin fetches JSON from `raw.githubusercontent.com/fcrocker-nyc/dirk-adams-honest-cattle-data/main/{slug}.json` and renders a 6-tile dashboard. Tiles: Snowpack/SWE, Water-Year Precip, Drought Monitor, Streamflow, Soil Moisture, Precip Anomaly. Tiles with `null` data show "not available" gracefully.

**Shortcode slugs use underscores:** `deer_lodge`, `lewis_clark`, `sweet_grass`, `big_horn`, `golden_valley`, `judith_basin`, `powder_river`, `silver_bow`. WordPress page URL slugs use hyphens. These are completely decoupled — changing a page URL never breaks the shortcode.

### 3.2 County Pages

All 56 county pages:
- Use the **classic editor** (Elementor disabled: `_elementor_edit_mode: ""`)
- Parent page: ID 1643 (`/counties-in-montana-counties/`)
- Each contains `[hc_snotel county="slug"]` in a beige-styled div under "Summary of Current Conditions"
- Content structure follows Park County template: H1 centered title, H2 sections for Overview, Weather & Moisture, Water Rights, Hay & Winter Feed, Cattle Production, County Logistics, H3 Data Sources

**Exception — 5 original Elementor pages:** Big Horn, Carbon, Gallatin, Park, Blaine still have `_elementor_edit_mode: "builder"`. Their shortcodes live in Elementor shortcode widgets. These work fine — the plugin renders in both contexts. If you ever need to edit these pages, use the Elementor editor (not classic).

**Slug inconsistencies in the original 10 pages:**
- `big-horn-county` (not `big-horn-county-montana`)
- `carbon-county` (not `carbon-county-montana`)
- `gallatin-county` (not `gallatin-county-montana`)
- `lewis-and-clark-county` (not `lewis-and-clark-county-montana`)
- All newer 46 pages use `{name}-county-montana`

The county grid JavaScript handles these overrides in the `counties.json` slug mapping.

### 3.3 County Grid Page (Page 1643)

The interactive grid at `/counties-in-montana-counties/` has three parts:

1. **Static HTML** (in `post_content`) — CSS, heading, "Five Daily Data Sources" explainer, glossary table, search input, filter buttons, empty grid container. WordPress's content sanitizer strips `<script>` tags from saved content, so NO JavaScript lives here.

2. **JavaScript** (via Code Snippet shortcode `[hc_county_grid_js]`) — fetches `counties.json` from the GitHub repo at page load, renders 56 county cards into the grid container, handles search/filter/region-param logic. The shortcode approach bypasses content sanitization. **Must use `no_texturize_shortcodes` filter** to prevent WordPress's `wptexturize` from converting straight quotes to curly quotes inside the script.

3. **`counties.json`** (in the GitHub repo) — static metadata array with name, slug, region (western/central/eastern), hasSnotel, hasStreamflow for each county. Updated by pushing to the repo; the grid page fetches it at runtime.

### 3.4 Code Snippets (3 active snippets)

The Code Snippets plugin runs PHP snippets on `init`. Three snippets are active:

1. **Original `hc_snotel` shortcode** (predates the mu-plugin) — the mu-plugin's `wp_loaded` registration overwrites this. Can be safely deactivated but isn't harmful.

2. **County Grid JavaScript** — defines `[hc_county_grid_js]` shortcode with `no_texturize_shortcodes` filter. Fetches `counties.json`, renders cards, handles search/filter. Only used on page 1643.

3. **Dashboard Card Helpers** — `wp_head` action that outputs CSS `::after` pseudo-elements on each tile article (`#hc-tile-snotel::after`, etc.) with plain-English explanations of each data source. Applies to all 56 county pages automatically.

### 3.5 CSS Lessons Learned

- **WordPress strips `<script>` tags from `post_content`** at save time via `wp_kses`. This is a security feature, not a bug. Scripts must be delivered via shortcodes, `wp_footer`, `wp_head`, or plugins.

- **`wptexturize` mangles JavaScript** in shortcode output by converting `"` to curly quotes. Use `add_filter('no_texturize_shortcodes', ...)` for any shortcode that returns JavaScript.

- **CSS `content` strings cannot span multiple lines.** PHP heredocs and `echo` statements that include newlines inside CSS `content:"..."` values will silently break the rule. Use PHP arrays and single-line echo to guarantee no embedded newlines.

- **CSS `::before` inside a `display:flex` footer** renders as a squished flex item, not a visible block. Use `::after` on the parent element instead.

- **The plugin's CSS hides all bare `<footer>` elements** (`footer { display:none !important }`), but overrides this for `.hc-tile footer` with `display:flex !important`. Any CSS targeting tile footers must account for this specificity.

### 3.6 Navigation

The top-nav "Counties" dropdown has 3 items:
- Western Montana → `/counties-in-montana-counties/?region=western`
- Central Montana → `/counties-in-montana-counties/?region=central`
- Eastern Montana → `/counties-in-montana-counties/?region=eastern`

The `?region=` parameter is read by the grid page's JavaScript on load and pre-selects the filter.

**Region assignments (18 / 20 / 18):**
- Western (18): Beaverhead, Deer Lodge, Flathead, Glacier, Granite, Lake, Lewis and Clark, Lincoln, Madison, Mineral, Missoula, Pondera, Powell, Ravalli, Sanders, Silver Bow, Teton, Toole
- Central (20): Big Horn, Broadwater, Carbon, Cascade, Chouteau, Fergus, Gallatin, Golden Valley, Hill, Jefferson, Judith Basin, Liberty, Meagher, Musselshell, Park, Petroleum, Stillwater, Sweet Grass, Wheatland, Yellowstone
- Eastern (18): Blaine, Carter, Custer, Daniels, Dawson, Fallon, Garfield, McCone, Phillips, Powder River, Prairie, Richland, Roosevelt, Rosebud, Sheridan, Treasure, Valley, Wibaux

---

## 4. Known Limitations & Future Work

### 4.1 Forage Score

The `forage_score` (0–100) is currently computed from SWE `percent_of_median` only, using interpolation ramps anchored on a single data point from the original Technical Memo (Blaine County: "No Snowpack" = 40). None of the v1.1/v1.2 signals (precipitation, drought, streamflow, soil moisture, precip anomaly) influence the score yet. This is documented in `SOURCES.md §6` and flagged for rangeland-scientist review.

### 4.2 Status Thresholds

SWE and precip status bands (Below Normal < 70% < Normal < 110% < Above Normal) are assumptions, not NRCS-published thresholds. See `SOURCES.md §5`.

### 4.3 Counties with No SNOTEL Coverage

30 of 56 counties have zero NRCS SNOTEL stations and report "No Snowpack" with `swe_index: 0`. For these counties, drought, streamflow, soil moisture, and precip anomaly are the meaningful signals. The pipeline correctly treats "No Snowpack" as the real NRCS answer for plains counties, not missing data.

### 4.4 Proxy Station Mapping

Several county pages reference SNOTEL stations in neighboring counties as "proxy" stations (e.g., Blaine references Rocky Boy in Hill County and Many Glacier in Glacier County for Milk River water supply context). The pipeline does NOT use proxy stations — it only aggregates stations whose NRCS `countyName` matches. Adding a proxy-station mapping table is a future enhancement that would make Big Horn, Stillwater, and Yellowstone counties more informative.

### 4.5 Stillwater Soil Moisture

Stillwater County has zero NRCS SNOTEL stations (correctly reports "No Snowpack"). The site page prose references "Mystic Lake" and "Woodbine" as stations, but neither is in the NRCS SNOTEL network. Mystic Lake is likely a Bureau of Reclamation reservoir gauge. Montana Mesonet provides soil moisture for Stillwater via 5+ stations.

### 4.6 USGS p90 Extrapolation

When USGS omits the p90 percentile for a gauge on a specific day-of-year, the script extrapolates `p90 ≈ p75 × 1.3`. This affects percentiles above the 75th mark — they may be slightly inflated toward 100. Values 0–75 are unaffected.

### 4.7 Page Cache

WordPress.com Atomic uses server-side page caching. After updating page content or activating a new Code Snippet, changes may take 1–2 minutes to appear. Hard-refresh (Cmd+Shift+R) bypasses browser cache but not server cache. If changes don't appear after a hard refresh, wait 2 minutes and try again.

---

## 5. Key File Inventory

| File | Purpose |
|---|---|
| `update_snotel.py` | Daily data pipeline script (v1.2, 56 counties, 5 data sources) |
| `.github/workflows/snotel-update.yml` | GitHub Actions daily cron + workflow_dispatch |
| `counties.json` | Static county metadata for the grid page (name, slug, region, data flags) |
| `SOURCES.md` | Provenance document for every number and assumption in the pipeline |
| `CLAUDE.md` | Claude Code session context (repo structure, pipeline docs, known quirks) |
| `*.json` (56 files) | Per-county data files, auto-refreshed daily |

WordPress-side (not in repo):
| Component | Location |
|---|---|
| mu-plugin | `wp-content/mu-plugins/hc-county-conditions.php` (or similar) |
| Code Snippet: County Grid JS | Code Snippets plugin, defines `[hc_county_grid_js]` shortcode |
| Code Snippet: Dashboard Card Helpers | Code Snippets plugin, `wp_head` CSS `::after` on tiles |
| Code Snippet: Original hc_snotel | Code Snippets plugin, legacy (overwritten by mu-plugin) |

---

## 6. Runbook: Common Operations

### Add a new county
1. Add to `ACTIVE_COUNTIES`, `COUNTY_FIPS`, and `COUNTY_GAUGES` in `update_snotel.py`
2. Push to main — next cron run creates the JSON file
3. Create a WordPress page (classic editor, parent 1643, `[hc_snotel county="slug"]` in content)
4. Add to `counties.json` for the grid

### Fix a bad USGS gauge
1. Search `waterservices.usgs.gov/nwis/site/?format=rdb&countyCd={fips}&siteType=ST&parameterCd=00060&siteStatus=active`
2. Update `COUNTY_GAUGES` in `update_snotel.py`
3. Push to main

### Diagnose a workflow failure
1. Go to `github.com/fcrocker-nyc/dirk-adams-honest-cattle-data/actions`
2. Click the failed run → check which STEP failed
3. If "Run SNOTEL updater" failed: API issue (NRCS down, rate limit, new bad station). Check `[snotel] skip` lines in the log.
4. If "Commit updated county files" failed: concurrent push during run (the rebase fix should handle this, but check if someone force-pushed)

### Update the grid page
1. Edit `counties.json` in the repo (add/remove counties, change regions)
2. Push to main — the grid page fetches this at runtime, no WordPress changes needed
3. For HTML/CSS changes: update page 1643 via the WordPress MCP or wp-admin classic editor

### Update card helper descriptions
1. Edit the "Dashboard Card Helpers" Code Snippet in wp-admin
2. Modify the PHP array values (the `$t` array)
3. Save — applies to all 56 county pages immediately (CSS is injected via `wp_head`)
