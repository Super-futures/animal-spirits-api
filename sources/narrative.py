"""
Narrative source: GDELT DOC 2.0 API.

Per-region, we query GDELT for recent news coverage matching affect clusters
and extract both volume and tone. The composite scalar per region combines:

    sign(average_tone) * magnitude(volume_anomaly)

Where:
    tone:   GDELT's tone score (roughly -8..+8), averaged across articles
    volume: article count, compared to 7-day rolling baseline per cluster

Two clusters (anxiety, confidence) instead of four — these capture the
dominant affective axis cleanly while keeping total request budget low
enough to finish within the GitHub Actions job timeout.

Rate limit: serialised with ~2s pauses. GDELT's documented limit is 1 req/5s
but in practice they tolerate 2s spacing; if we see 429s we back off.

Output: scalar in (-1, +1) per region. Negative = stress narrative dominant.
"""

import asyncio
import logging
from typing import Optional

import httpx

from cache import cache
from normalise import tanh_squash, clip

log = logging.getLogger("animal-spirits.narrative")

# Just two clusters — captures the core anxiety ↔ confidence axis.
# Aspiration and constraint can be re-added later if the job budget permits.
GDELT_QUERIES = {
    "anxiety":    '("recession" OR "unemployment" OR "inflation" OR "crisis" OR "layoffs" OR "bankruptcy")',
    "confidence": '("growth" OR "expansion" OR "bull market" OR "investment" OR "hiring" OR "ipo")',
}

COUNTRY_CODES = {
    "us": "US",
    "uk": "UK",
    "india": "IN",
}

CLUSTER_WEIGHTS = {
    "anxiety":    -1.0,
    "confidence": +1.0,
}

NARRATIVE_TTL = 900   # 15 min — matches GDELT's own update cadence
BASELINE_TTL  = 3600  # 1 hour — baseline volume can be stale
REQUEST_PAUSE = 2.0   # seconds between serialised GDELT calls


async def _gdelt_query(client: httpx.AsyncClient, query: str, country: str,
                       timespan: str = "1d") -> Optional[dict]:
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
        r = await client.get(url, params=params, timeout=15.0)
        if r.status_code != 200:
            log.debug("GDELT %s %s returned %d", country, timespan, r.status_code)
            return None
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
    country = COUNTRY_CODES[region]
    query = GDELT_QUERIES[cluster]

    current = await _gdelt_query(client, query, country, timespan="1d")
    if current is None:
        return None
    await asyncio.sleep(REQUEST_PAUSE)

    baseline_key = f"gdelt_baseline:{country}:{cluster}"
    baseline = cache.get(baseline_key)
    if baseline is None:
        baseline_data = await _gdelt_query(client, query, country, timespan="7d")
        if baseline_data is not None:
            _, baseline_count = _extract_tone_and_volume(baseline_data)
            baseline_daily = baseline_count / 7.0
            cache.set(baseline_key, baseline_daily, BASELINE_TTL)
            baseline = baseline_daily
            await asyncio.sleep(REQUEST_PAUSE)

    tone, volume = _extract_tone_and_volume(current)
    if tone is None or volume == 0:
        return None

    tone_norm = clip(tone / 5.0)

    if baseline and baseline > 0:
        vol_ratio = volume / baseline
        vol_anomaly = tanh_squash(vol_ratio - 1.0, scale=1.0)
    else:
        vol_anomaly = 0.0

    magnitude = abs(tone_norm) * (1.0 + 0.5 * abs(vol_anomaly))
    signed = (1.0 if tone_norm >= 0 else -1.0) * magnitude
    return clip(signed)


async def fetch_narrative() -> tuple[dict[str, Optional[float]], str]:
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
