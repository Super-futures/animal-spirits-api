# animal-spirits-api

*Backend data pipeline for [Animal Spirits](https://super-futures.github.io/animalspirits) — computing and serving collective economic affect signals across three regions.*

---

## What it does

A scheduled GitHub Actions workflow that fetches, normalises, and composes three economic affect signals — attention, market, and narrative — across US, UK, and India. Output is written to `data/state.json` and served via GitHub Pages with permissive CORS headers, allowing the frontend to make a single fetch.

The backend serves raw normalised signals only. All coupling metrics (C_align, C_sync, C_lag, I) and regime classification are computed in the frontend's signal processing layer.

---

## Output

`data/state.json` — updated on schedule by the workflow:

```json
{
  "timestamp": "2026-04-25T07:52:59.536930+00:00",
  "regions": {
    "us":    { "attention": 0.063, "market": 0.191, "narrative": -0.545 },
    "uk":    { "attention": 0.022, "market": -0.010, "narrative": 0.188 },
    "india": { "attention": -0.009, "market": 0.065, "narrative": -0.408 }
  },
  "meta": {
    "attention": "live",
    "market": "live",
    "narrative": "live"
  }
}
```

All values are signed and normalised. Narrative is negative under stress, positive under relief. Meta reports live/simulated status per axis.

**Served at:** `https://super-futures.github.io/animal-spirits-api/data/state.json`

---

## Three axes

### Attention (A)

Wikimedia Pageviews API — 7-day rolling average across four affect cluster term sets per region:

| Cluster | Terms (example) |
|---------|----------------|
| anxiety | Recession, Unemployment, Stock_market_crash |
| confidence | Bull_market, Economic_growth, Consumer_confidence |
| aspiration | Luxury_goods, Travel, Real_estate |
| constraint | Budget, Debt, Frugality |

Each cluster uses region-specific term sets. Combined as RMS across clusters to preserve magnitude without mean-cancellation.

### Market (M)

Two sources combined into a composite signal:

- **Equity return** — Yahoo Finance via Alpha Vantage (`SPY` for US, `ISF.LON` for UK, `NIFTYBEES.BSE` for India). 100-day close history, latest normalised return.
- **Stress indicator** — FRED series: VIX (volatility), BAMLH0A0HYM2 (high-yield spread), DTWEXBGS (dollar index). Normalised and combined.

```
market_composite = 0.5 · equity_return + 0.5 · stress_normalised
```

### Narrative (N)

GDELT TimelineTone API — per-region tone scalar from economic keyword queries over a 1-day timespan. Queries use terms across recession, unemployment, inflation, crisis, layoffs, bankruptcy per region. Raw tone is normalised to a signed scalar:

```
narrative_scalar = raw_tone / 5.6  (approximate normalisation)
```

Negative = stress propagating. Positive = relief/confidence propagating. GDELT requests are rate-sensitive; the workflow retries up to 3 times with 25-second intervals on timeout.

---

## Architecture

```
run.py
├── market.py     — Alpha Vantage + FRED fetches, composite computation
├── sentiment.py  — Wikimedia pageview fetches, RMS aggregation
├── narrative.py  — GDELT TimelineTone fetches, tone normalisation
└── state.py      — Composition, normalisation, state.json write
```

---

## Workflow

`.github/workflows/refresh.yml` — runs on schedule (hourly or as configured).

Steps:
1. Fetch all signals in parallel where possible
2. Compose `state.json` with timestamp and meta
3. Commit and push to `main`
4. GitHub Pages serves the updated file

The workflow uses a bot commit identity (`animal-spirits-bot`) to avoid polluting the commit history with authored commits.

---

## Known constraints

**GDELT rate limiting** — TimelineTone endpoint enforces a strict rate limit. US and India queries frequently timeout on first attempt; the workflow retries with backoff. Total state refresh time is approximately 2–3 minutes when GDELT is slow.

**Alpha Vantage free tier** — 25 API calls/day limit. The three equity symbols use 3 calls per refresh; FRED uses 3 more. Well within limits at hourly refresh.

**Wikimedia baseline** — raw pageview counts are normalised against a fixed 50,000 daily view baseline. This is a pragmatic threshold, not empirically derived. High-traffic events (market crashes, major news) may saturate the normalisation. A rolling baseline is a v3 direction.

---

## v3 directions

- **Cluster vector exposure** — currently the four attention clusters are combined into a scalar before writing to `state.json`. Exposing the full cluster vector `{ anxiety, confidence, aspiration, constraint }` per region would allow the frontend to compute expressive divergence directly from source rather than approximating it from composite components.
- **Rolling attention baseline** — replace the fixed 50,000 normalisation threshold with a 30-day rolling baseline per term, making attention readings relative to recent history rather than absolute pageview counts.
- **Google Trends Alpha** — institutional API access would provide higher-frequency, query-specific attention signals as an alternative or supplement to Wikimedia pageviews.
- **Narrative cluster weighting** — currently a single GDELT query per region covers all economic stress terms. Separate queries per affect cluster (anxiety terms vs confidence terms) would allow narrative to be decomposed by affect valence rather than aggregated.
- **Historical state log** — append each refresh tick to a `history.jsonl` file alongside `state.json`, retaining a 90-day rolling window. Enables frontend trajectory analysis, dwell-time computation, and lead-lag stability evaluation.

---

## Related

- **Frontend:** [Super-futures/animalspirits](https://github.com/Super-futures/animalspirits) — `super-futures.github.io/animalspirits`

---

*Superfutures · v2.2*
