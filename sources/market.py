"""
Market source: Yahoo Finance (regional equity) + FRED (US macro stress).

The composite scalar per region is:
    0.55 * local_equity + 0.45 * global_stress_backdrop

Where:
    local_equity  = recent return / recent vol (Sharpe-like), per region
    global_stress = US VIX + HY credit spread + dollar anomaly, inverted

Yahoo via yfinance for equity because GitHub Actions runners have
unrestricted egress, avoiding the cloud-IP blocking that affected earlier
hosted-proxy attempts. FRED retained for the stress backdrop — these
indicators have no clean equivalents elsewhere.
"""

import os
import asyncio
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import httpx
import yfinance as yf

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.market")

FRED_KEY = os.getenv("FRED_API_KEY", "")

# Regional equity indices on Yahoo Finance
EQUITY_SYMBOLS = {
    "us":    "^GSPC",  # S&P 500
    "uk":    "^FTSE",  # FTSE 100
    "india": "^NSEI",  # Nifty 50
}

# FRED series IDs for macro stress backdrop
FRED_SERIES = {
    "vix":    "VIXCLS",       # CBOE Volatility Index
    "credit": "BAMLH0A0HYM2", # ICE BofA US High Yield Index OAS
    "dollar": "DTWEXBGS",     # Trade-Weighted US Dollar Index: Broad
}

EQUITY_TTL = 300
FRED_TTL   = 3600


def _fetch_yahoo_sync(symbol: str) -> Optional[list[float]]:
    """Synchronous yfinance call. Runs in a threadpool from async context."""
    try:
        ticker = yf.Ticker(symbol)
        # ~2 months of daily history gives us enough for the short-horizon Sharpe.
        hist = ticker.history(period="2mo", interval="1d")
        if hist.empty or len(hist) < 10:
            return None
        closes = [float(c) for c in hist["Close"].dropna().tolist()]
        if len(closes) < 10:
            return None
        return closes
    except Exception as e:
        log.warning("Yahoo fetch failed for %s: %s", symbol, e)
        return None


async def _fetch_yahoo_series(symbol: str) -> Optional[list[float]]:
    cache_key = f"yahoo:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        closes = await loop.run_in_executor(pool, _fetch_yahoo_sync, symbol)

    if closes is None:
        return None
    cache.set(cache_key, closes, EQUITY_TTL)
    return closes


async def _fetch_fred_series(client: httpx.AsyncClient, series_id: str) -> Optional[list[float]]:
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
        values = list(reversed(values))
        if len(values) < 5:
            return None
        cache.set(cache_key, values, FRED_TTL)
        return values
    except Exception as e:
        log.warning("FRED fetch failed for %s: %s", series_id, e)
        return None


def _equity_scalar(closes: list[float]) -> float:
    if len(closes) < 10:
        return 0.0
    returns = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            returns.append((closes[i] / closes[i-1]) - 1.0)
    if len(returns) < 6:
        return 0.0
    recent = returns[-5:]
    baseline = returns[:-5]
    if len(baseline) < 3:
        return 0.0
    recent_mean = sum(recent) / len(recent)
    baseline_std = (sum((r - sum(baseline)/len(baseline))**2 for r in baseline) / len(baseline)) ** 0.5 or 1e-6
    ratio = recent_mean / baseline_std
    return tanh_squash(ratio, scale=2.0)


def _stress_scalar(vix: list[float], credit: list[float], dollar: list[float]) -> float:
    if not vix or not credit or not dollar:
        return 0.0
    vix_z    = z_score(vix[-1], vix[:-1])
    credit_z = z_score(credit[-1], credit[:-1])
    dollar_z = z_score(dollar[-1], dollar[:-1])
    stress = (
        -0.40 * vix_z
        -0.40 * credit_z
        -0.20 * abs(dollar_z)
    )
    return tanh_squash(stress, scale=1.5)


async def fetch_market() -> tuple[dict[str, Optional[float]], str]:
    async with httpx.AsyncClient() as client:
        equity_tasks = {
            region: _fetch_yahoo_series(symbol)
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

    for d in (equity_results, fred_results):
        for k, v in list(d.items()):
            if isinstance(v, Exception) or v is None:
                d[k] = None

    stress = _stress_scalar(
        fred_results.get("vix") or [],
        fred_results.get("credit") or [],
        fred_results.get("dollar") or [],
    )
    stress_live = all(fred_results.get(k) for k in ("vix", "credit", "dollar"))

    out: dict[str, Optional[float]] = {}
    any_live = False
    for region, closes in equity_results.items():
        if closes is None:
            if stress_live:
                out[region] = clip(stress * 0.6)
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
