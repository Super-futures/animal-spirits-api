"""
Market source: Twelve Data (regional equity) + FRED (US macro stress).

The composite scalar per region is:
    0.55 * local_equity + 0.45 * global_stress_backdrop

Where:
    local_equity  = recent return / recent vol (Sharpe-like), per region
    global_stress = US VIX + HY credit spread + dollar anomaly, inverted

Rationale: US stress indicators function as a global risk-on/off backdrop
that genuinely pressures UK and Indian markets. This lets us apply a single
macro-stress reading across all three regions without pretending the UK/IN
local indices have equivalent coverage to the US.

All values z-scored against rolling history, then tanh-squashed.
Output: scalar in (-1, +1). Negative = stress/contraction, Positive = expansion.
"""

import os
import asyncio
import logging
from typing import Optional

import httpx

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.market")

TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
FRED_KEY = os.getenv("FRED_API_KEY", "")

# Regional equity indices on Twelve Data
EQUITY_SYMBOLS = {
    "us":    "SPX",    # S&P 500
    "uk":    "UKX",    # FTSE 100
    "india": "NIFTY",  # Nifty 50
}

# FRED series IDs for macro stress backdrop
FRED_SERIES = {
    "vix":    "VIXCLS",       # CBOE Volatility Index
    "credit": "BAMLH0A0HYM2", # ICE BofA US High Yield Index OAS
    "dollar": "DTWEXBGS",     # Trade-Weighted US Dollar Index: Broad
}

# TTLs (seconds)
EQUITY_TTL = 300    # 5 min — intraday prices move, but we're not trading
FRED_TTL   = 3600   # 1 hour — these are daily series, huge TTL is fine

# Twelve Data free tier: 800 req/day, 8 req/min. Comfortable.
# FRED: 120 req/min. Comfortable.


async def _fetch_twelve_data_series(client: httpx.AsyncClient, symbol: str) -> Optional[list[float]]:
    """
    Fetch ~30 daily closes for a symbol. Returns list of closes (oldest first).
    """
    cache_key = f"td:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not TWELVE_DATA_KEY:
        return None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": 30,
        "apikey": TWELVE_DATA_KEY,
    }
    try:
        r = await client.get(url, params=params, timeout=8.0)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            log.warning("Twelve Data error for %s: %s", symbol, data.get("message"))
            return None
        values = data.get("values", [])
        # Twelve Data returns newest first; reverse to oldest first.
        closes = [float(v["close"]) for v in reversed(values)]
        if len(closes) < 5:
            return None
        cache.set(cache_key, closes, EQUITY_TTL)
        return closes
    except Exception as e:
        log.warning("Twelve Data fetch failed for %s: %s", symbol, e)
        return None


async def _fetch_fred_series(client: httpx.AsyncClient, series_id: str) -> Optional[list[float]]:
    """
    Fetch ~60 recent daily observations for a FRED series.
    Returns list of values (oldest first), skipping missing values.
    """
    cache_key = f"fred:{series_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not FRED_KEY:
        return None

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 60,
    }
    try:
        r = await client.get(url, params=params, timeout=8.0)
        r.raise_for_status()
        data = r.json()
        obs = data.get("observations", [])
        values = []
        for o in obs:
            v = o.get("value", ".")
            if v and v != ".":
                try:
                    values.append(float(v))
                except ValueError:
                    pass
        # Reverse to oldest first
        values = list(reversed(values))
        if len(values) < 5:
            return None
        cache.set(cache_key, values, FRED_TTL)
        return values
    except Exception as e:
        log.warning("FRED fetch failed for %s: %s", series_id, e)
        return None


def _equity_scalar(closes: list[float]) -> float:
    """
    Sharpe-like short-horizon return/vol ratio over the last ~5 sessions.
    Returns a value roughly in (-1, +1) after tanh squash.
    """
    if len(closes) < 10:
        return 0.0
    # Daily log returns
    returns = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            returns.append((closes[i] / closes[i-1]) - 1.0)
    if len(returns) < 6:
        return 0.0
    # Short window: last 5 sessions
    recent = returns[-5:]
    baseline = returns[:-5]
    if len(baseline) < 3:
        return 0.0
    recent_mean = sum(recent) / len(recent)
    baseline_std = (sum((r - sum(baseline)/len(baseline))**2 for r in baseline) / len(baseline)) ** 0.5 or 1e-6
    ratio = recent_mean / baseline_std
    return tanh_squash(ratio, scale=2.0)


def _stress_scalar(vix: list[float], credit: list[float], dollar: list[float]) -> float:
    """
    Global macro stress, z-scored and inverted (high stress -> negative contribution).

    VIX:    high = stress (invert)
    Credit: high spread = stress (invert)
    Dollar: sharp move either way = flight signal (use |z|, invert)

    Returns a value roughly in (-1, +1). Negative = stress.
    """
    if not vix or not credit or not dollar:
        return 0.0

    vix_z    = z_score(vix[-1], vix[:-1])
    credit_z = z_score(credit[-1], credit[:-1])
    dollar_z = z_score(dollar[-1], dollar[:-1])

    # Invert VIX and credit: positive z = stress = negative contribution
    stress = (
        -0.40 * vix_z
        -0.40 * credit_z
        -0.20 * abs(dollar_z)  # magnitude only for dollar
    )
    return tanh_squash(stress, scale=1.5)


async def fetch_market() -> tuple[dict[str, Optional[float]], str]:
    """
    Returns ({region: scalar or None}, status) where status is 'live' or 'simulated'.
    """
    async with httpx.AsyncClient() as client:
        equity_tasks = {
            region: _fetch_twelve_data_series(client, symbol)
            for region, symbol in EQUITY_SYMBOLS.items()
        }
        fred_tasks = {
            name: _fetch_fred_series(client, series_id)
            for name, series_id in FRED_SERIES.items()
        }
        all_results = await asyncio.gather(
            *equity_tasks.values(), *fred_tasks.values(),
            return_exceptions=True,
        )

    equity_results = dict(zip(equity_tasks.keys(), all_results[:3]))
    fred_results = dict(zip(fred_tasks.keys(), all_results[3:]))

    # Treat exceptions as None
    for d in (equity_results, fred_results):
        for k, v in list(d.items()):
            if isinstance(v, Exception) or v is None:
                d[k] = None

    # Global stress backdrop
    stress = _stress_scalar(
        fred_results.get("vix") or [],
        fred_results.get("credit") or [],
        fred_results.get("dollar") or [],
    )
    stress_live = all(fred_results.get(k) for k in ("vix", "credit", "dollar"))

    # Per-region composite
    out: dict[str, Optional[float]] = {}
    any_live = False
    for region, closes in equity_results.items():
        if closes is None:
            # No equity — if we have stress, use stress alone. Else None.
            if stress_live:
                out[region] = clip(stress * 0.6)  # dampen when equity missing
                any_live = True
            else:
                out[region] = None
        else:
            eq = _equity_scalar(closes)
            composite = 0.55 * eq + (0.45 * stress if stress_live else 0.0)
            out[region] = clip(composite)
            any_live = True

    status = "live" if any_live else "simulated"
    return out, status
