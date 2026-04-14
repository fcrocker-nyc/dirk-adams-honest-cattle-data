# Repository

Per-county SNOTEL snapshots for the ten active honestcattle.net Montana counties.
Each `<county>.json` at the repo root is read directly by a `[hc_snotel county="…"]`
WordPress shortcode on the corresponding county page (`honestcattle.net/counties/…`).

## Pipeline

`update_snotel.py` pulls live SWE + median from the USDA NRCS AWDB REST API
(`wcc.sc.egov.usda.gov/awdbRestApi/services/v1`), aggregates equal-weighted
across each county's SNOTEL stations, classifies status, and writes one file
per county. Runs daily via `.github/workflows/snotel-update.yml` at 11:30 UTC
and commits any changed files as `auto update: SNOTEL refresh YYYY-MM-DD`.
Stdlib only — no pip deps in CI.

## File shape

```json
{"county": "gallatin", "date": "2026-04-14", "swe_index": 10.91,
 "percent_of_median": 55, "trend": "↓", "status": "Below Normal",
 "forage_score": 62}
```

- `county` (string, snake_case) — matches filename
- `date` (string, `YYYY-MM-DD`) — UTC date of the run
- `swe_index` (number, inches) — equal-weighted mean current SWE across the county's NRCS SNOTEL stations
- `percent_of_median` (int) — county SWE divided by mean of station medians for the latest reading
- `trend` (string) — `↑` / `↓` / `→` based on 7-day SWE delta (>0.2 inches = up/down)
- `status` (string) — `No Snowpack` / `Below Normal` / `Normal` / `Above Normal` from % of median thresholds (0 / 70 / 110 / 200)
- `forage_score` (int, 0–100) — synthetic from percent_of_median (see `forage_score()` in `update_snotel.py`)

## Counties with no NRCS SNOTEL coverage

Big Horn, Blaine, Stillwater, and Yellowstone have zero MT SNOTEL stations
classified to their county in NRCS metadata. The script correctly emits a
`No Snowpack` record for those (`swe_index: 0.0`, `percent_of_median: 0`,
`forage_score: 40`). That's the right answer for Big Horn / Blaine /
Yellowstone (plains). Stillwater is less obvious — the honestcattle.net page
prose references Mystic Lake / Woodbine, but neither is in the NRCS SNOTEL
network; future work could add proxy station mapping (e.g. from Park or
Sweet Grass) if that's the desired behavior.

## Known quirks

- NRCS `/stations` silently ignores server-side `stateCds` / `networkCds`
  filters — the script filters client-side by `stateCode == "MT"` and
  `networkCode == "SNTL"`.
- NRCS `/data` 500s on large batches and also on specific bad stations
  (e.g. `690:MT:SNTL` / Pickfoot Creek). `_fetch_data_chunk` chunks into
  groups of 20 and recursively splits on 500, skipping single-station
  batches that still 500.
- `json.dumps(..., ensure_ascii=False)` writes literal `↓` etc. instead of
  `\u2193`; earlier repo files used the escaped form, the new files don't.
