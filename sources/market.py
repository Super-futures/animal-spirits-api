"""
Market source: Stooq (regional equity) + FRED (US macro stress).

Stooq serves CSV directly via simple URL params, works from GitHub Actions
runners, requires no API key, and covers all three indices we need:
    ^SPX  (S&P 500)
    ^UKX  (FTSE 100)
    ^NIFTY (Nifty 50)

FRED retained for the stress backdrop (VIX, HY credit, dollar).

Composite per region: 0.55 * local_equity + 0.45 * global_stress_backdrop
"""

import os
import asyncio
import logging
from typing import Optional
from io import StringIO
import csv

import httpx

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.market")

FRED_KEY = os.getenv("FRED_API_KEY", "")

# Stooq index tickers
EQUITY_SYMBOLS = {
    "us":    "^spx",
    "uk":    "^ukx",
    "india": "^nifty",
}

FRED_SERIES = {
    "vix":    "VIXCLS",
    "credit": "BAMLH0A0HYM2",
    "dollar": "DTWEXBGS",
}

EQUITY_TTL = 300
FRED_TTL   = 3600


async def _fetch_stooq_series(client: httpx.AsyncClient, symbol: str) -> Optional[list[float]]:
    """
    Fetch daily closes for a Stooq symbol as CSV.
    Stooq returns full history; we take the last ~40 rows for our window.
    """
    cache_key = f"stooq:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        r = await client.get(url, timeout=10.0, headers={
            "User-Agent": "AnimalSpirits/1.0 (research; https://github.com/super-futures/animal-spirits-api)"
        })
        if r.status_code != 200:
            log.warning("Stooq returned %d for %s", r.status_code, symbol)
            return None
        text = r.text
        # Stooq error responses are small (< 100 chars) or the literal string "No data"
        if len(text) < 100 or "No data" in text:
            log.warning("Stooq returned empty/error body for %s: %r", symbol, text[:80])
            return None
        # CSV: Date,Open,High,Low,Close,Volume
        reader = csv.DictReader(StringIO(text))
        closes = []
        for row in reader:
            close_str = row.get("Close", "").strip()
            if close_str and close_str != "-":
                try:
                    closes.append(float(close_str))
                except ValueError:
                    pass
        if len(closes) < 10:
            log.warning("Stooq returned insufficient data for %s: %d closes", symbol, len(closes))
            return None
        # Use last 40 rows for our window
        closes = closes[-40:]
        cache.set(cache_key, closes, EQUITY_TTL)
        return closes
    except Exception as e:
        log.warning("Stooq fetch failed for %s: %s", symbol, e)
        return None


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
            region: _fetch_stooq_series(client, symbol)
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
