# animal-spirits-api

The backend for [Animal Spirits](https://super-futures.github.io/animal-spirits/) — a live affective observatory reading three axes of collective state across three regions.

This repo is not a server. It is a **scheduled composition job** that runs every 15 minutes via GitHub Actions, fetches live data from four independent upstream sources, composes a single unified state, and commits the result as a static JSON file served by GitHub Pages.

**Live feed:** [super-futures.github.io/animal-spirits-api/data/state.json](https://super-futures.github.io/animal-spirits-api/data/state.json)

---

## Why this shape

Historically, the project ran as a FastAPI service behind a Render free dyno. That architecture had three problems: cold starts of 20–90 seconds, tight free-tier request limits, and the observatory being coupled to a server that could go down. The current design removes the server entirely. There is no runtime code. A scheduled job computes the state, commits it to git, and GitHub Pages serves the file statically. Cost is zero, and uptime is whatever GitHub Pages' is.

A side effect worth naming: `git log data/state.json` is now a full-time series of every sample ever composed. The commit history *is* the research dataset.

## The output contract

```json
{
  "timestamp": "2026-04-24T03:45:41Z",
  "regions": {
    "us":    {"attention": 0.031, "market": 0.242, "narrative": -0.268},
    "uk":    {"attention": 0.091, "market": 0.100, "narrative":  0.000},
    "india": {"attention":-0.108, "market": 0.172, "narrative": -0.199}
  },
  "meta": {
    "attention": "live",
    "market":    "live",
    "narrative": "live"
  }
}
```

Each scalar is in approximately `[−1, +1]`. Negative = stress-coded, positive = expansion-coded, zero = neutral or unavailable. The `meta` field records whether each axis is currently backed by real fetched data (`"live"`) or has fallen back to a synthetic default (`"simulated"`).

## The three axes

### Attention — Wikimedia Pageviews
Per region, we fetch the 30-day daily pageview history for 8 Wikipedia articles per affect cluster (anxiety, confidence, aspiration, constraint) for a total of 32 articles per region, localised to the region's relevant terms. The latest day's views are z-scored against the preceding 29-day history, weighted and composited per cluster, and tanh-squashed to `[−1, +1]`. Anxiety and constraint push the composite negative; confidence and aspiration push it positive.

### Market — Alpha Vantage (ETFs) + FRED (macro stress)
Two sources composited:

- **Local equity** via ETF proxies: `SPY` (US S&P 500 via SPDR), `ISF.LON` (UK FTSE 100 via iShares Core), `NIFTYBEES.BSE` (India Nifty 50 via Nippon India). Daily closes, recent-return Sharpe-style scalar. ETFs chosen because Alpha Vantage's free tier covers equities but not index endpoints; tracking error is well under 1% and ETFs are arguably more "affective" (they are what people actually trade).
- **Global macro-stress backdrop** via FRED: `VIXCLS`, `BAMLH0A0HYM2` (high-yield credit spread), `DTWEXBGS` (trade-weighted dollar). Z-scored, weighted, tanh-squashed. Applied identically to all three regions as a shared stress environment.

Composite per region: `0.55 × local_equity + 0.45 × global_stress`.

### Narrative — GDELT DOC 2.0
Per region, a single request to the `TimelineTone` endpoint with anxiety keywords scoped by `sourcecountry`. Returns the average tone of matching news over the last 24 hours as a time series; we take the most recent non-null value and normalise `/5` into `[−1, +1]`. Negative tone is read directly as narrative stress (no sign flip).

GDELT rate-limits free endpoints aggressively and occasionally refuses TCP connections from GitHub Actions runner IPs. The fetcher uses keep-alive connections with 30-second timeouts and retries up to 3× with 10-second backoff.

## Architecture

```
.github/workflows/refresh.yml     GitHub Actions cron (every 15 min)
run.py                            Entry point, calls composer
state.py                          Composes unified state from sources
sources/
  attention.py                    Wikimedia Pageviews fetcher
  market.py                       Alpha Vantage + FRED fetcher
  narrative.py                    GDELT TimelineTone fetcher
cache.py                          File-backed TTL cache (persists to data/cache.json)
normalise.py                      z_score, tanh_squash, clip helpers
data/
  state.json                      ← This is the live output
  cache.json                      Persisted cache baselines
index.html                        Landing page
```

The workflow writes both `data/state.json` and `data/cache.json` each run. Persisting the cache across runs means baseline windows (Wikimedia 30-day history, FRED series, etc.) survive between invocations.

## Rate limits and caching

| Source | Limit | Cache TTL | Net usage |
|--------|-------|-----------|-----------|
| Alpha Vantage | 5 req/min, 500 req/day | 6 hours | ~12 calls/day |
| FRED | Unlimited (practical) | 1 hour | 3 calls/run |
| Wikimedia | Polite ~100 req/min | 1 hour | ~32 calls/run, once per hour |
| GDELT | ~1 req / 5s | 15 min | 3 calls/run |

Alpha Vantage calls are serialised with 12-second gaps (= exactly the 5/min limit). GDELT calls are serialised with 6-second gaps and use HTTP keep-alive to sidestep connection-refusal timeouts. FRED and Wikimedia calls run concurrently.

## Commit-push conflict handling

Because scheduled and manually-dispatched runs can overlap, the workflow uses a hard-reset-then-overwrite pattern rather than merging. On each push attempt:

1. Copy freshly-generated `state.json` and `cache.json` to a tmp location
2. `git fetch` + `git reset --hard origin/main`
3. Overwrite with the fresh data files
4. Commit and push
5. If push still fails, loop up to 5 times

This is safe because both files are regenerated wholesale each run; there is no semantic value to preserving "both sides" of a conflict.

## Upgrade hooks

If the Alpha Vantage plan is upgraded to paid tier, replace the ETF symbols in `sources/market.py`:

```python
EQUITY_SYMBOLS = {
    "us":    "SPX",        # S&P 500 index directly
    "uk":    "UKX",        # FTSE 100
    "india": "NIFTY",      # Nifty 50
}
```

No other code changes required.

## Running locally

```bash
pip install -r requirements.txt
export ALPHA_VANTAGE_API_KEY=your_key
export FRED_API_KEY=your_key
python run.py
```

`data/state.json` will be written to the working directory. No GDELT, Wikimedia, or narrative keys are needed — both are free public endpoints.

## Secrets required

Set as GitHub repo secrets for the workflow to run:

- `ALPHA_VANTAGE_API_KEY` — [get one here](https://www.alphavantage.co/support/#api-key), free tier is fine
- `FRED_API_KEY` — [get one here](https://fred.stlouisfed.org/docs/api/api_key.html), free

## Attribution

- GDELT Project — computational-narrative infrastructure
- Wikimedia Foundation — Pageviews API
- Alpha Vantage — equity time-series API
- Federal Reserve Bank of St. Louis — FRED API

## Versions

- **v1.0** (current) — static JSON feed architecture, Alpha Vantage ETF proxies replacing prior Yahoo / Stooq / Twelve Data dead ends, GDELT TimelineTone replacing prior ArtList mode.

## Sibling repo

The frontend observatory that consumes this feed: [super-futures/animal-spirits](https://github.com/super-futures/animal-spirits).

## Maintainer

Leon, at [Superfutures](https://github.com/super-futures), Auckland.
