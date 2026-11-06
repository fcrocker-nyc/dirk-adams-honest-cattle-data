# HonestCattle Data Feeds — Client Contract

This document is the **platform-neutral contract** for every JSON feed the
HonestCattle apps read. It exists so the **Android client can reach full parity
with the iOS client** on the data layer: both apps consume the exact same feeds,
at the same URLs, with the same schema and the same null semantics. Nothing in
this repository is iOS-specific — the feeds are plain UTF-8 JSON over HTTPS.

A machine-readable version of this contract lives at
[`feeds.json`](feeds.json). **Clients should fetch `feeds.json` first for feed
discovery** (URLs, cadence, and the county → feed-URL index) instead of
hard-coding paths. This Markdown file is the human-readable companion and the
schema reference that `feeds.json` points at.

- **Repo:** `fcrocker-nyc/dirk-adams-honest-cattle-data`
- **Raw base:** `https://raw.githubusercontent.com/fcrocker-nyc/dirk-adams-honest-cattle-data/main/`
- **Encoding:** UTF-8, literal glyphs (e.g. the trend arrow is a literal `↓`, not
  `↓`). Any conformant JSON parser — `org.json`, Moshi, kotlinx.serialization,
  Gson on Android; `JSONSerialization`/`Codable` on iOS — decodes these directly.
- **Content type:** `application/json` (served by raw.githubusercontent.com as
  `text/plain`; parse by body, not by header).

---

## Feed index

| Feed | Path | Cadence (UTC) | Consumers |
|---|---|---|---|
| Montana county conditions | `{key}.json` (56) | daily ~11:30 | app + web |
| Montana county roster | `counties.json` | on change | app + web |
| Rangeland vegetation (RAP) | `vegetation.json` | weekly Mon ~12:00 | app + web |
| Texas county conditions | `texas/{key}.json` (39) | daily ~11:50 | app + web |
| Texas county roster | `texas_counties.json` | on change | app + web |
| Market forecast (4-week) | `forecasts_recent.json` | daily ~13:30 | app + web |
| Video-auction card | `auction/video_latest.json` | daily ~12:00 | app + web |
| Per-house auction latest | `auction/{house}_latest.json` | daily ~12:00 | app + web |
| Auction history (trend) | `auction/history.json` | daily ~12:00 | app |
| Forecast accuracy scorecard | `auction/forecast_accuracy.json` | daily ~12:00 | app |

`{house}` ∈ `pays`, `bls`, `mt_weekly` (year-round Montana yards) and `superior`,
`nlva`, `wvm`, `blc_video` (seasonal video houses).

**Cadence is "not before," not "at."** Each workflow only commits when the
content actually changes, so a feed may not update on a given day. Always treat
the payload's own `date` / `as_of` / `sale_date` / `updated` field as the source
of truth for freshness — never the fetch time.

---

## Cross-platform consumption rules

These are the rules an Android client must follow to match iOS behavior:

1. **Nullable blocks.** On county conditions, the core fields (`county` … 
   `forage_score`) are always present. The richer blocks (`precip_ytd`,
   `drought`, `streamflow`, `soil_moisture`, `precip_anomaly`, `hay_pasture`)
   are **additive and nullable** — when a data source has no coverage for a
   county the whole block is `null` (not an empty object, not an absent key).
   Model these as optional/nullable types and render "not available" gracefully,
   exactly as the WordPress plugin does.
2. **County → feed URL.** The roster's `slug` is the *WordPress page slug*
   (`beaverhead-county-montana`), **not** the data-file key (`beaverhead`). Do
   not derive one from the other by string munging — the mapping is not 1:1
   (underscores vs. hyphens, the `-county-montana` suffix, and legacy slugs like
   `gallatin-county`). Use the explicit `counties.montana` / `counties.texas`
   index in [`feeds.json`](feeds.json), where each entry carries both `key` and
   a ready-built `url`.
3. **Seasonality.** Video-auction feeds are seasonal (~April–October). Off-season
   they hold the most recent sale, so `sale_date` may be weeks old. Use the
   `season` block on `video_latest.json` (`in_season_window`, `days_since_latest`,
   `stale`, `banner`) rather than computing seasonality on-device.
4. **Missing quarters / bands.** In the market forecast, a heifer band not yet
   published serializes as `null`. Never assume both steer and heifer are present.
5. **Numbers.** Prices are USD per hundredweight (cwt). SWE is inches. Percentages
   are whole-number `int` unless noted. `swe_index` and prices are `float`.

---

## Schemas

### <a id="county-conditions"></a>County conditions — `{key}.json`, `texas/{key}.json`

```json
{
  "county": "park",
  "date": "2026-04-17",
  "swe_index": 13.34,
  "percent_of_median": 78,
  "trend": "→",
  "status": "Normal",
  "forage_score": 69,
  "precip_ytd":     {"inches": 23.49, "percent_of_median": 115, "status": "Above Normal"},
  "drought":        {"valid_end": "2026-04-13", "d0_pct": 100, "d1_pct": 85.5, "d2_pct": 40.4, "d3_pct": 0, "d4_pct": 0, "worst_class": "D2"},
  "streamflow":     {"gauge_name": "Yellowstone River near Livingston", "site_no": "06192500", "cfs": 3350, "percentile": 91, "status": "Above Normal"},
  "soil_moisture":  {"shallow_vwc_pct": 24.6, "deep_vwc_pct": 21.3, "station_count": 3, "source": "Montana Mesonet"},
  "precip_anomaly": {"month_end": "2026-03", "m1": {"...": "..."}, "m3": {"...": "..."}, "m12": {"...": "..."}},
  "hay_pasture":    {"ndvi_pctl": 62, "rap_pctl": null, "n_years": 24, "asof": "2026-04-14"}
}
```

- Core fields `county`…`forage_score` are always present.
- `trend` ∈ `↑` / `↓` / `→`. `status` ∈ `No Snowpack` / `Well Below Normal` /
  `Below Normal` / `Normal` / `Above Normal`.
- Every block after `forage_score` is nullable (see rule 1). Counties with no
  SNOTEL coverage report `swe_index: 0`, `percent_of_median: 0`,
  `status: "No Snowpack"` — that is the real answer for plains counties, not
  missing data.
- Texas county files share this shape; see `update_texas.py` for Texas-specific
  fields.

### <a id="county-roster"></a>County roster — `counties.json`, `texas_counties.json`

`counties.json` is a JSON array of 56 objects:

```json
{"name": "Beaverhead", "slug": "beaverhead-county-montana", "region": "western", "snotel": true, "stream": true}
```

`texas_counties.json` is a JSON array of 39 objects (adds `fips`, `gauge`,
`lat`, `lon`, `structural_potential`). `slug` is the web page slug — for the
data-file URL use the manifest index (rule 2).

### <a id="vegetation"></a>Vegetation — `vegetation.json`

Per-county rangeland vegetation response (VR) and irrigated hay/pasture
percentiles from the Rangeland Analysis Platform, keyed by county. Each county
carries `vr`, `vr_status`, `vr_note`, and a `hay_pasture` block. `vr` may be
`null` early in the year (RAP publication lag); `vr_status` /`vr_note` explain
why (`awaiting_current_year_rap`, `insufficient_history`, `compute_error`).
Top-level `vr_data_note` / `resolution_note` headers document the lag and
resolution. Refreshed weekly (Mondays).

### <a id="market-forecast"></a>Market forecast — `forecasts_recent.json`

```json
{
  "updated": "2026-06-22",
  "weeks": [
    {
      "as_of": "2026-06-22",
      "source_url": "https://honestcattle.net/2026/06/22/...",
      "source_title": "2026-06-22 Honest Cattle Weekly Market Forecast",
      "class_note": "550–599 and 600–649 lb Montana-origin steers / heifers, FOB auction",
      "quarters": [
        {
          "label": "Q2 2026",
          "status": "HELD",
          "bands": {
            "550_599": {"steer": {"low": 490.0, "high": 520.0, "mid": 505.0},
                        "heifer": {"low": 470.0, "high": 500.0, "mid": 485.0}},
            "600_649": {"steer": {"...": "..."}, "heifer": {"...": "..."}}
          },
          "steer":  {"low": 460.0, "high": 520.0, "mid": 490},
          "heifer": {"low": 440.0, "high": 500.0, "mid": 470}
        }
      ]
    }
  ]
}
```

Rolling window of the 4 most recent weekly forecasts (newest first). `bands` are
the tight per-weight bands; the top-level `steer`/`heifer` are the wide combined
band. A heifer cell not yet published → that band is `null`. Prices are USD/cwt.

### <a id="auction-latest"></a>Auction latest — `auction/video_latest.json`, `auction/{house}_latest.json`

```json
{
  "source_key": "superior",
  "auction": "Superior Livestock Auction",
  "report_id": "AMS_2713",
  "sale_day": "Ongoing",
  "channel": "video",
  "base": "Fort Worth, TX",
  "period_start": "6/16/2026",
  "period_end": "6/17/2026",
  "sale_date": "2026-06-17",
  "total_receipts": 64911,
  "breakdown": {"feeder": {"head": 64503, "pct": 99.37}},
  "narrative": "Compared to the last sale: Feeder steers sold 3.00-8.00 higher …",
  "summary": {"steers": {"550_599": {"head": 0, "wtd_avg_price": 0}}, "heifers": {"...": "..."}}
}
```

`video_latest.json` additionally carries `season` (seasonality banner — see
rule 3), `series_roster` (houses tracked, so the app can name the series
off-season), `terms_comparison` (cross-house sale terms), `houses` (per-house
calf-band summary), and `mt_region_disclaimer`. `channel` is `video` or
`auction`. Per-house files (`pays`, `bls`, `mt_weekly`, …) share the base shape.

### <a id="auction-history"></a>Auction history — `auction/history.json`

JSON array of past auction reports (same base shape as an auction-latest object,
plus `owner_group`) used for trend/series charts. Newest and oldest sales
coexist; filter/sort by `sale_date` client-side.

### <a id="forecast-accuracy"></a>Forecast accuracy — `auction/forecast_accuracy.json`

```json
{
  "generated_at": "…",
  "realized_source": "…",
  "forecast_field": "…",
  "min_n_for_significance": 8,
  "overall": {"n": 4, "bias_cwt": 2.08, "mpe_pct": 0.15, "mad_cwt": 19.59,
              "mape_pct": 4.02, "mse": 663.35, "rmse_cwt": 25.76,
              "sufficient": false, "calibration": {"...": "..."}},
  "by_band": {"...": "..."},
  "pairs": ["…"],
  "note": "…"
}
```

Scorecard comparing prior forecasts to realized auction prices. `sufficient` is
`false` until `n >= min_n_for_significance`; hide or caveat the scorecard while
`sufficient` is `false`.

---

## Android parity checklist

To bring the Android client to parity with iOS, implement against this contract:

- [ ] Fetch [`feeds.json`](feeds.json) on launch for feed discovery (URLs +
      county index) rather than hard-coding paths.
- [ ] County conditions: all 56 MT + 39 TX counties, using the `key`/`url` from
      the manifest index (not the roster page slug).
- [ ] Model all post-`forage_score` blocks as nullable; render "not available"
      when `null`.
- [ ] Market tab: forecast card (`forecasts_recent.json`), video card
      (`video_latest.json` incl. the `season` banner), per-house cards, history
      chart, and the accuracy scorecard (respect `sufficient`).
- [ ] Vegetation overlay (`vegetation.json`) with `vr_status`/`vr_note` handling.
- [ ] Treat each payload's date field as the freshness source of truth.
- [ ] UTF-8 decode; render literal glyphs (`↑ ↓ → –`) directly.

When a new feed is added to the pipeline, add it here and to `feeds.json` so both
clients stay in parity automatically.
