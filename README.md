# animal-spirits-api

*Backend data pipeline for [Animal Spirits](https://propensities.github.io/animal-spirits/) — fetching, normalising, and composing collective economic affect signals across three regions.*

---

## What it does

A scheduled GitHub Actions workflow fetches three signals — attention, market, and narrative — for the US, UK, and India, normalises each to a signed scalar, and writes `data/state.json` and `data/history.jsonl`. Both are served via GitHub Pages.

The backend produces **normalised per-axis scalars only**. All coupling metrics (alignment, synchrony, lag, instability) and regime classification are computed in the frontend's signal processing layer.

There is no server. Each run is a one-shot process that produces a static artefact.

---

## Output

### `data/state.json`

Overwritten every run. Latest snapshot of all three regions:

```json
{
  "timestamp": "2026-08-13T05:57:27.973392+00:00",
  "regions": {
    "us":    { "attention": 0.797, "market": 0.111, "narrative": -0.060 },
    "uk":    { "attention": -0.320, "market": 0.067, "narrative": null },
    "india": { "attention": 0.987, "market": 0.059, "narrative": -0.374 }
  },
  "meta": {
    "attention": "live",
    "market": "live",
    "narrative": "live"
  }
}
```

All values are signed, roughly in `(-1, +1)`. A per-axis value may be `null` when that region's fetch failed. Narrative is negative under stress, positive under relief.

**`meta` reports status per axis, not per region.** An axis is marked `live` if *at least one* region returned data. In the example above, `narrative` reads `live` while the UK value is `null`. Consumers wanting per-region provenance must check for `null` directly.

### `data/history.jsonl`

Appended every run — one line per region, three lines per refresh. Pruned to a 90-day rolling window at the end of each run.

```json
{"timestamp": "...", "region": "us", "A": 0.797, "M": 0.111, "N": -0.060}
```

### `data/cache.json`

TTL-keyed cache, committed alongside the state files so that rolling baselines and API responses survive between one-shot runs. Not a data output; it is infrastructure.

---

## Sources

### Attention — Wikimedia Pageviews API

`en.wikipedia`, all-access, all-agents, daily granularity.

**32 articles per region** — 8 terms across each of 4 affect clusters (anxiety, confidence, aspiration, constraint); 96 articles in total.

Each article's most recent complete day is z-scored against its own preceding ~30-day series (window ends at yesterday, since the current day is incomplete). A cluster value is the mean z-score across its available terms. The regional composite is the mean of weighted cluster z-scores:

```
anxiety     -1.0
confidence  +1.0
aspiration  +0.5
constraint  -0.7

composite = Σ(weight × cluster_z) / n_clusters_available
A         = tanh(composite / 1.5)
```

Positive = expansion-coded attention. Negative = stress-coded attention.

Z-scoring against each term's own history yields *unusual attention relative to that term's baseline* rather than absolute volume — background popularity is factored out, curiosity spikes are retained.

Cache TTL: 30 minutes.

### Market — Alpha Vantage ETF proxies + FRED macro stress

```
composite = 0.55 × local_equity + 0.45 × global_stress
```

**Local equity** uses index-tracking ETFs rather than the indices themselves, because Alpha Vantage's free tier covers equities but not index endpoints:

| Region | Symbol | Tracks |
|---|---|---|
| US | `SPY` | S&P 500 |
| UK | `ISF.LON` | FTSE 100 |
| India | `NIFTYBEES.BSE` | Nifty 50 |

`TIME_SERIES_DAILY`, compact. The scalar compares the mean of the last 5 daily returns against the standard deviation of the prior returns in the series, squashed via `tanh(ratio / 2.0)`.

**Global stress** is drawn from FRED:

| Series | Description |
|---|---|
| `VIXCLS` | CBOE volatility index |
| `BAMLH0A0HYM2` | ICE BofA US high-yield option-adjusted spread |
| `DTWEXBGS` | Trade-weighted US dollar index (broad) |

```
stress = tanh((-0.40·z(vix) − 0.40·z(credit) − 0.20·|z(dollar)|) / 1.5)
```

**This backdrop is global and is applied identically to all three regions.** See *Limitations* below.

Cache TTL: 6 hours (equity), 1 hour (FRED). Alpha Vantage calls are issued serially with 12-second gaps to respect the free tier's per-minute limit; the 6-hour TTL is what keeps daily usage (~12 calls) under the 25/day cap, not the refresh cadence.

### Narrative — GDELT DOC 2.0, TimelineTone

One query per region, filtered by `sourcecountry` (`US`, `UK`, `IN`), over a 1-day timespan:

```
("recession" OR "unemployment" OR "inflation" OR "crisis"
 OR "layoffs" OR "bankruptcy")
```

The most recent numeric tone value in the returned timeline is taken, then:

```
N = tanh(clip(raw_tone / 5.0, -1, 1))
```

GDELT tone is used with its native sign — negative tone on these terms means narrative stress; no sign flip is applied.

Cache TTL: 15 minutes. Requests are paced 6 seconds apart with up to 3 attempts and a 10-second retry delay, using a single keep-alive client — GDELT's free endpoint frequently refuses fresh TCP connections from Actions runners.

---

## Limitations

These are design constraints, documented rather than resolved.

**The market stress backdrop is not regionally differentiated.** All three FRED series are US instruments, and the same stress scalar contributes 45% of every region's market value. Some apparent inter-regional market coupling is therefore a property of the construction rather than an observation. Regionally-specific stress instruments (e.g. RBI rates for India, gilt spreads for the UK) are a v3 direction.

**Equity failure degrades silently.** If an Alpha Vantage fetch fails for a region and FRED succeeded, that region falls back to `stress × 0.6` — a value composed entirely of US macro conditions with no local market input — and the axis still reports `live`. Nothing in the output distinguishes this from a normal reading.

**Attention is English-language only.** All three regions are read from `en.wikipedia`. Wikimedia serves Hindi, Bengali, Tamil and other editions through the same API on identical terms; they are not sampled. This is a selection, not an infrastructural constraint, and it means the India attention signal reads an anglophone subset of that population. Multilingual sampling is a v3 direction.

**Narrative is anxiety-conditioned by construction.** The GDELT query is restricted to economic stress terms, so N measures the tone of stress discourse rather than general economic media tone. This is deliberate — stress narratives propagate fastest and are the most reactive sub-signal — but it is not a neutral read of media sentiment.

**Narrative cannot reach full scale.** The `clip → tanh` sequence bounds N at ±0.762 while attention and market can reach ±1. The three axes are not on identical scales.

**Attention is a scalar collapse.** Four affect clusters are combined before writing to `state.json`. Cluster composition is not exposed, so the frontend cannot compute expressive divergence from source.

**GDELT country filtering is by outlet location**, not by audience, language, or subject.

---

## Architecture

```
run.py                  — orchestration: parallel fetch, compose, write, prune
├── sources/market.py    — Alpha Vantage + FRED, composite computation
├── sources/attention.py — Wikimedia pageviews, cluster z-score composite
├── sources/narrative.py — GDELT TimelineTone, tone normalisation
├── normalise.py         — shared primitives: z_score, tanh_squash, clip
└── cache.py             — file-backed TTL cache (data/cache.json)
```

Each source module returns `({region: scalar_or_None}, status)`. `run.py` wraps each in a 180-second timeout; a source that times out or raises returns all-`None` with status `simulated` rather than failing the run.

Signal acquisition is deliberately thin and replaceable. Richer attention pipelines, directional narrative data, or higher-frequency market signals can be substituted at the source-module level without touching normalisation, the state schema, or the frontend.

---

## Workflow

`.github/workflows/refresh.yml` — cron `*/15 * * * *`, plus `workflow_dispatch` and push-triggered runs when `run.py`, `sources/**`, `normalise.py`, or the workflow itself change. Concurrency-grouped, 10-minute timeout, Python 3.11.

1. Fetch all three axes in parallel
2. Compose `state.json` with timestamp and meta
3. Append to `history.jsonl` (one line per region)
4. Prune `history.jsonl` to the last 90 days
5. Flush the cache
6. Commit and push `state.json`, `cache.json`, and `history.jsonl` as `animal-spirits-bot`, with rebase-and-retry on push conflict

The run exits non-zero only when *all three* axes return `simulated`, so CI status reflects genuine outages rather than partial degradation.

**Observed cadence is lower than nominal.** Across 89.9 days (15 May – 13 August 2026, 1,173 refresh events) the median interval between runs was **90 minutes** — roughly 13 events per day, about 14% of the 15-minute schedule. This reflects scheduled-workflow throttling on GitHub Actions rather than source failure; no gap exceeded nine hours and the 90-day window is complete. Observed per-axis availability over the same period: attention 99.9%, market 100%, narrative **50.1%**.

---

## Configuration

Two repository secrets:

```
ALPHA_VANTAGE_API_KEY
FRED_API_KEY
```

Both free tiers. See `.env.example` for local development.

---

## v3 directions

- **Cluster vector exposure** — write the full `{anxiety, confidence, aspiration, constraint}` vector per region so the frontend can compute expressive divergence from source rather than approximating it.
- **Regionally-specific stress instruments** — replace the shared FRED backdrop with region-native series to make cross-region market comparability legitimate.
- **Per-region status reporting** — extend `meta` to per-region-per-axis provenance, including whether a market value used the equity fallback.
- **Multilingual attention** — sample `hi.wikipedia`, `bn.wikipedia`, `ta.wikipedia` alongside English for India.
- **Narrative cluster decomposition** — separate GDELT queries per affect cluster, so narrative can be read by valence rather than aggregated under stress terms.
- **Google Trends Alpha** — institutional API access for higher-frequency, query-specific attention.

---

## Related

- **Frontend:** [propensities/animal-spirits](https://github.com/propensities/animal-spirits) — `propensities.github.io/animal-spirits/`

---

*Propensities · v2.2*
