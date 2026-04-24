"""
Narrative source: GDELT DOC 2.0 API.

Single cluster for v1 — 'anxiety' — because Keynesian animal spirits are
asymmetric: fear moves faster than confidence. This is the single most
information-dense cluster for a regime-reading system. Confidence and other
clusters can be layered in later once the pipeline is stable and GDELT's
rate-limit budget is better understood.

Rate limit: GDELT documents 1 req/5s. We respect it strictly with 5.5s
pauses; a single cluster × 3 regions × (current + baseline on first run)
= 6 requests ≈ 90s total for a cold run, well inside the 10-minute job.
Subsequent runs hit cache for baselines: ~30s.

Output: scalar in (-1, +1) per region. Negative = stress narrative dominant.
"""

import asyncio
import logging
from typing import Optional

import httpx

from cache import cache
from normalise import tanh_squash, clip

log = logging.getLogger("animal-spirits.narrative")

GDELT_QUERIES = {
    "anxiety": '("recession" OR "unemployment" OR "inflation" OR "crisis" OR "layoffs" OR "bankruptcy")',
}

COUNTRY_CODES = {
    "us": "US",
    "uk": "UK",
    "india": "IN",
}

# Anxiety cluster pushes the composite negative (stress dominant)
CLUSTER_WEIGHTS = {
    "anxiety": -1.0,
}

NARRATIVE_TTL = 900
BASELINE_TTL  = 3600
REQUEST_PAUSE = 5.5


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
            log.warning("GDELT %s %s returned %d", country, timespan, r.status_code)
            return None
        try:
            data = r.json()
        except Exception:
            return None
        cache.set(cache_key, data, NARRATIVE_TTL)
        return data
    except Exception as e:
        log.warning("GDELT fetch failed (%s / %s): %s", country, query[:40], e)
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
        log.warning("narrative[%s/%s]: 1d query returned None", region, cluster)
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
            log.info("narrative[%s/%s]: baseline set to %.2f/day", region, cluster, baseline_daily)
            await asyncio.sleep(REQUEST_PAUSE)
        else:
            log.warning("narrative[%s/%s]: 7d baseline query returned None", region, cluster)

    tone, volume = _extract_tone_and_volume(current)
    log.info("narrative[%s/%s]: tone=%s volume=%d baseline=%s",
             region, cluster,
             f"{tone:+.2f}" if tone is not None else "None",
             volume,
             f"{baseline:.2f}" if baseline is not None else "None")

    if tone is None or volume == 0:
        log.warning("narrative[%s/%s]: returning None (tone=%s, volume=%d)",
                    region, cluster, tone, volume)
        return None

    tone_norm = clip(tone / 5.0)

    if baseline and baseline > 0:
        vol_ratio = volume / baseline
        vol_anomaly = tanh_squash(vol_ratio - 1.0, scale=1.0)
    else:
        vol_anomaly = 0.0

    magnitude = abs(tone_norm) * (1.0 + 0.5 * abs(vol_anomaly))
    signed = (1.0 if tone_norm >= 0 else -1.0) * magnitude
    result = clip(signed)
    log.info("narrative[%s/%s]: signal=%+.3f", region, cluster, result)
    return result


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
