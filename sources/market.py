"""
Market source: Alpha Vantage ETF proxies + FRED macro stress backdrop.

Composite per region: 0.55 * local_equity + 0.45 * global_stress_backdrop

Local equity uses major index-tracking ETFs rather than the indices
themselves, because Alpha Vantage's free tier covers equities but not
index endpoints. The ETFs track their indices closely (tracking error
well under 1%) and are arguably more "affective" reads — they're what
people actually trade when they want exposure to the index.

ETF proxies:
    US:    SPY           (SPDR S&P 500 ETF, tracks S&P 500)
    UK:    ISF.LON       (iShares Core FTSE 100, tracks FTSE 100)
    India: NIFTYBEES.BSE (Nippon India ETF Nifty 50)

Upgrade path: to use the actual index series (SPX, UKX, NIFTY), upgrade
Alpha Vantage to a paid plan and change EQUITY_SYMBOLS below. No other
code changes needed.

Alpha Vantage free tier: 25 requests/day. We cache for 6 hours, so
3 symbols x 4 fetches/day = 12 calls/day. Well under limit.

FRED provides VIX, HY credit spread, and trade-weighted dollar as a
global stress backdrop applied identically to all three regions.
"""

import os
import asyncio
import logging
from typing import Optional

import httpx

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.market")

ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FRED_KEY = os.getenv("FRED_API_KEY", "")

EQUITY_SYMBOLS = {
    "us":    "SPY",
    "uk":    "ISF.LON",
    "india": "NIFTYBEES.BSE",
}

FRED_SERIES = {
    "vix":    "VIXCLS",
    "credit": "BAMLH0A0HYM2",
    "dollar": "DTWEXBGS",
}

EQUITY_TTL = 6 * 3600
FRED_TTL   = 3600


async def _fetch_alpha_vantage_series(client, symbol):
    cache_key = f"av:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not ALPHA_VANTAGE_KEY:
        log.warning("Alpha Vantage key missing for %s", symbol)
        return None

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",
        "apikey": ALPHA_VANTAGE_KEY,
    }
    try:
        r = await client.get(url, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()

        if "Note" in data:
            log.warning("Alpha Vantage rate-limited for %s: %s", symbol, data["Note"][:120])
            return None
        if "Information" in data:
            log.warning("Alpha Vantage info for %s: %s", symbol, data["Information"][:120])
            return None
        if "Error Message" in data:
            log.warning("Alpha Vantage error for %s: %s", symbol, data["Error Message"])
            return None

        series = data.get("Time Series (Daily)")
        if not series:
            log.warning("Alpha Vantage: no time series for %s (keys: %s)", symbol, list(data.keys())[:5])
            return None

        dates_sorted = sorted(series.keys())
        closes = []
        for date in dates_sorted:
            day = series[date]
            close_str = day.get("4. close")
            if close_str:
                try:
                    closes.append(float(close_str))
                except ValueError:
                    pass

        if len(closes) < 10:
            log.warning("Alpha Vantage %s: only %d closes returned", symbol, len(closes))
            return None

        log.info("Alpha Vantage %s: %d closes, latest=%.2f", symbol, len(closes), closes[-1])
        cache.set(cache_key, closes, EQUITY_TTL)
        return closes
    except Exception as e:
        log.warning("Alpha Vantage fetch failed for %s: %s", symbol, e)
        return None


async def _fetch_fred_series(client, series_id):
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


def _equity_scalar(closes):
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


def _stress_scalar(vix, credit, dollar):
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


async def fetch_market():
    async with httpx.AsyncClient() as client:
        equity_tasks = {
            region: _fetch_alpha_vantage_series(client, symbol)
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

    out = {}
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
            log.info("market[%s]: equity=%+.3f stress=%+.3f composite=%+.3f",
                     region, eq, stress if stress_live else 0.0, out[region])

    status = "live" if any_live else "simulated"
    return out, status
