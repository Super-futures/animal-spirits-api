# Animal Spirits — Static State Feed

A scheduled job that composes a single coherent field state from attention, market, and narrative signals, and publishes it as a static JSON file. Part of the [Animal Spirits](https://github.com/super-futures/animal-spirits) observatory.

## How it works

No server. No cold start. No billing.

A GitHub Actions workflow runs every 15 minutes, fetches fresh data from Twelve Data (market), FRED (macro stress), Wikimedia Pageviews (attention), and GDELT (narrative), normalises them into a unified state, and commits `data/state.json` back to the repository. GitHub Pages serves this file as a static asset with CORS enabled by default.

The frontend polls the JSON file via `https://super-futures.github.io/animal-spirits-api/data/state.json`.

## Why this architecture

The three underlying data sources update at different cadences — Wikimedia is daily-granular, FRED is daily, GDELT is every ~15 min, and even intraday market data doesn't meaningfully change every few seconds for a reflective observatory. Refreshing a static feed every 15 minutes is conceptually honest: it matches the actual update cadence of the upstream data rather than pretending to be more live than it is.

The fact that it's also free, requires no server process, has no cold start, and cannot incur surprise costs is a welcome side effect.

## API Contract

### `data/state.json`

```json
{
  "timestamp": "2026-04-24T03:15:22.841Z",
  "regions": {
    "us":    { "attention": 0.12, "market": -0.34, "narrative": -0.18 },
    "uk":    { "attention": 0.08, "market": -0.21, "narrative": -0.11 },
    "india": { "attention": 0.31, "market":  0.09, "narrative":  0.04 }
  },
  "meta": {
    "attention": "live",
    "market":    "live",
    "narrative": "live"
  }
}
```

All scalars are in roughly `[−1, +1]`. Negative values = stress/contraction-coded states; positive = expansion-coded states. `null` values indicate the source was unavailable for that region. `meta.<axis>` is `"live"` when that axis produced real data for at least one region in the most recent run, `"simulated"` when the source failed entirely.

## Normalisation Methodology

Each axis normalises against its own rolling history, not against uniform min-max scaling. This preserves the information content of unusual movements relative to each signal's own baseline.

### Attention (Wikimedia Pageviews)

- For each of four affect clusters (anxiety, confidence, aspiration, constraint), fetch ~8 English Wikipedia articles per region.
- For each article, fetch 30 days of daily pageview counts.
- Z-score the most recent day against the prior 29 days.
- Average within each cluster; weighted sum across clusters (anxiety/constraint negative, confidence/aspiration positive).
- tanh-squash to `[−1, +1]`.

### Market (Twelve Data + FRED)

A two-part composite per region:

**Local equity** (Twelve Data): regional index (SPX / UKX / NIFTY), Sharpe-like ratio of recent return to recent vol, tanh-squashed.

**Global stress backdrop** (FRED, applied to all three regions):
- VIX (`VIXCLS`): z-score, inverted
- HY credit spread (`BAMLH0A0HYM2`): z-score, inverted
- Dollar index (`DTWEXBGS`): `|z-score|`, inverted
- Composite: `−0.40·vix_z − 0.40·credit_z − 0.20·|dollar_z|`, tanh-squashed

**Regional composite:** `0.55 × local_equity + 0.45 × global_stress`

### Narrative (GDELT DOC 2.0)

- For each cluster × region, query GDELT ArtList for the last 24h filtered by `sourcecountry`.
- Extract mean tone and article count.
- Tone normalised by dividing by 5 (empirical GDELT tone range is ~±8).
- Volume anomaly: today's count vs. 7-day daily average, tanh-squashed.
- Signed composite: `sign(tone) × |tone_norm| × (1 + 0.5·|vol_anomaly|)`, cluster-weighted, tanh-squashed.

### Cross-run state

The file-backed cache (`data/cache.json`) persists rolling baselines and intermediate values between Action runs, so z-scoring remains stable across invocations. The cache prunes expired entries on each flush.

## Setup

### 1. Create the repository

Push this code to a public GitHub repository (e.g. `super-futures/animal-spirits-api`).

### 2. Add API keys as repository secrets

In GitHub: Settings → Secrets and variables → Actions → New repository secret

- `TWELVE_DATA_API_KEY` — from https://twelvedata.com/
- `FRED_API_KEY` — from https://fred.stlouisfed.org/docs/api/api_key.html

### 3. Enable GitHub Pages

In GitHub: Settings → Pages → Source = Deploy from a branch → Branch = `main` / root.

After the first successful Action run, `data/state.json` will be served at:
`https://<your-org>.github.io/animal-spirits-api/data/state.json`

### 4. Trigger the first run manually

Actions tab → "Refresh Animal Spirits state" → Run workflow.

Subsequent runs are automatic every 15 minutes.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # Fill in your keys
export $(cat .env | xargs)
python run.py
```

This runs the same code that runs in CI, writing to `data/state.json` locally. Safe to do — it won't push anything to git.

## Observability

- Each Action run logs a state summary: which axes were live, what the composed values were per region.
- The commit message on each successful run includes the UTC timestamp.
- Git history becomes an audit log of state evolution — `git log data/state.json` shows every 15-minute snapshot since deployment.

## Limitations

- **15-minute minimum cadence.** GitHub's cron has jitter; actual runs land within ~5 min of schedule. For a reflective observatory this is fine; for a trading signal it would not be.
- **No query parameters.** The feed is one endpoint, one response shape. By design.
- **Public by necessity.** GitHub Pages serves public repos only (on free tier). The data is already public; don't use this pattern for anything sensitive.

## Version

v1.0 — static JSON feed composed every 15 min via GitHub Actions.
Replaces the earlier FastAPI+Render architecture.
