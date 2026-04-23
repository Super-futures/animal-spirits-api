"""
Narrative source: GDELT DOC 2.0 API.

Per-region, we query GDELT for recent news coverage matching each of the
four affect clusters (anxiety, confidence, aspiration, constraint) and
extract both volume and tone. The composite scalar per region combines:

    sign(average_tone) * magnitude(volume_z)

Where:
    tone:   GDELT's -100..+100 tone score, averaged across matching articles
    volume: article count, z-scored against 7-day rolling baseline per cluster

Rate limit: GDELT asks for ~1 request per 5 seconds. We serialise and cache
aggressively (narrative doesn't need to be sub-minute fresh).

Output: scalar in (-1, +1) per region. Negative = stress narrative dominant.
"""

import os
import asyncio
import logging
import urllib.parse
from typing import Optional

import httpx

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.narrative")

# GDELT query terms per cluster — using simple OR queries for robustness.
# Regional filtering happens via sourcecountry parameter.
GDELT_QUERIES = {
    "anxiety":    '("recession" OR "unemployment" OR "inflation" OR "crisis" OR "layoffs" OR "bankruptcy")',
    "confidence": '("growth" OR "expansion" OR "bull market" OR "investment" OR "hiring" OR "ipo")',
    "aspiration": '("luxury" OR "property boom" OR "wealth" OR "entrepreneur" OR "prosperity")',
    "constraint": '("austerity" OR "budget cuts" OR "debt" OR "poverty" OR "food bank" OR "cost of living")',
}

# GDELT sourcecountry codes
COUNTRY_CODES = {
    "us": "US",
    "uk": "UK",
    "india": "IN",
}

CLUSTER_WEIGHTS = {
    "anxiety":    -1.0,
    "confidence": +1.0,
    "aspiration": +0.5,
    "constraint": -0.7,
}

NARRATIVE_TTL = 900   # 15 min — matches GDELT's own update cadence
BASELINE_TTL  = 3600  # 1 hour — for the 7-day baseline volume z-score


async def _gdelt_query(client: httpx.AsyncClient, query: str, country: str,
                       timespan: str = "1d") -> Optional[dict]:
    """
    Single GDELT DOC 2.0 ArtList query. Returns parsed JSON or None.
    Respects the 1 req/5s norm via cache.
    """
    cache_key = f"gdelt:{country}:{timespan}:{hash(query)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    full_query = f'{query} sourcecountry:{country}'
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": full_query,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
        "maxrecords": 75,
        "sort": "DateDesc",
    }
    try:
        r = await client.get(url, params=params, timeout=10.0)
        if r.status_code != 200:
            return None
        # GDELT sometimes returns non-JSON on rate limit; guard.
        try:
            data = r.json()
        except Exception:
            return None
        cache.set(cache_key, data, NARRATIVE_TTL)
        return data
    except Exception as e:
        log.debug("GDELT fetch failed (%s / %s): %s", country, query[:40], e)
        return None


def _extract_tone_and_volume(gdelt_response: dict) -> tuple[Optional[float], int]:
    """
    Extract average tone and article count from a GDELT ArtList response.
    Tone is in roughly [-10, +10] in practice (theoretical range [-100, +100]).
    """
    articles = gdelt_response.get("articles", [])
    if not articles:
        return None, 0
    tones = []
    for a in articles:
        tone = a.get("tone")
        if tone is not None:
            try:
                tones.append(float(tone))
            except (ValueError, TypeError):
                pass
    if not tones:
        return None, len(articles)
    avg_tone = sum(tones) / len(tones)
    return avg_tone, len(articles)


async def _cluster_signal(client: httpx.AsyncClient, region: str, cluster: str) -> Optional[float]:
    """
    Compute one cluster's contribution for one region.
    Returns a signed scalar roughly in (-1, +1) or None if data unavailable.
    """
    country = COUNTRY_CODES[region]
    query = GDELT_QUERIES[cluster]

    # Current window
    current = await _gdelt_query(client, query, country, timespan="1d")
    if current is None:
        return None
    # Rate-limit politeness: pause between serialised calls
    await asyncio.sleep(5.5)
    # Baseline (prior 7 days) for volume z-score
    baseline_key = f"gdelt_baseline:{country}:{cluster}"
    baseline = cache.get(baseline_key)
    if baseline is None:
        baseline_data = await _gdelt_query(client, query, country, timespan="7d")
        if baseline_data is not None:
            _, baseline_count = _extract_tone_and_volume(baseline_data)
            # Treat 7-day count as ~7 daily samples for a rough baseline
            baseline_daily = baseline_count / 7.0
            cache.set(baseline_key, baseline_daily, BASELINE_TTL)
            baseline = baseline_daily
            await asyncio.sleep(5.5)

    tone, volume = _extract_tone_and_volume(current)
    if tone is None or volume == 0:
        return None

    # Tone: normalise to roughly [-1, +1]. GDELT tone rarely exceeds ±8 in practice.
    tone_norm = clip(tone / 5.0)

    # Volume: how anomalous is today's count vs. baseline?
    if baseline and baseline > 0:
        vol_ratio = volume / baseline
        # 1.0 = normal; 2.0 = double; 0.5 = half.
        vol_anomaly = tanh_squash(vol_ratio - 1.0, scale=1.0)
    else:
        vol_anomaly = 0.0

    # Signed composite: tone sign * (base tone + volume-amplification)
    # If tone is strongly negative AND volume is anomalously high, that's a stronger signal.
    magnitude = abs(tone_norm) * (1.0 + 0.5 * abs(vol_anomaly))
    signed = (1.0 if tone_norm >= 0 else -1.0) * magnitude
    return clip(signed)


async def fetch_narrative() -> tuple[dict[str, Optional[float]], str]:
    """
    Returns ({region: scalar}, status).
    Scalar: signed narrative tone weighted by volume anomaly, squashed to (-1, +1).
    """
    out: dict[str, Optional[float]] = {}
    any_live = False

    async with httpx.AsyncClient() as client:
        for region in ("us", "uk", "india"):
            cluster_values = []
            for cluster, weight in CLUSTER_WEIGHTS.items():
                v = await _cluster_signal(client, region, cluster)
                if v is not None:
                    cluster_values.append(weight * v)

            if not cluster_values:
                out[region] = None
            else:
                composite = sum(cluster_values) / len(cluster_values)
                out[region] = tanh_squash(composite, scale=1.0)
                any_live = True

    status = "live" if any_live else "simulated"
    return out, status
