"""
Narrative source: GDELT DOC 2.0 API, TimelineTone mode.

Uses TimelineTone because it returns average tone as a time series,
which is what we actually need. ArtList mode does not include tone per
article despite the API's query-level tone filters.

Per region, we fetch the 1-day tone timeline and take the most recent
non-null value, normalised by /5 into roughly (-1, +1).

Rate limit: strict 5.5s serialisation per GDELT's documented 1 req/5s.
One cluster x 3 regions = 3 requests plus pauses, ~40s total.

Output: scalar in (-1, +1) per region. Negative = stress dominant.
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

CLUSTER_WEIGHTS = {
    "anxiety": -1.0,
}

NARRATIVE_TTL = 900
REQUEST_PAUSE = 5.5


async def _gdelt_timeline_tone(client, query, country):
    cache_key = f"gdelt_tone:{country}:{hash(query)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    full_query = f"{query} sourcecountry:{country}"
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": full_query,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": "1d",
    }
    try:
        r = await client.get(url, params=params, timeout=15.0)
        if r.status_code != 200:
            log.warning("GDELT TimelineTone %s returned %d", country, r.status_code)
            return None
        try:
            data = r.json()
        except Exception as e:
            log.warning("GDELT TimelineTone %s JSON parse failed: %s", country, e)
            return None

        timeline = data.get("timeline", [])
        if not timeline:
            log.warning("GDELT TimelineTone %s: empty timeline", country)
            return None
        points = timeline[0].get("data", [])
        if not points:
            log.warning("GDELT TimelineTone %s: empty data points", country)
            return None

        latest_tone = None
        for point in reversed(points):
            v = point.get("value")
            if v is not None and isinstance(v, (int, float)):
                latest_tone = float(v)
                break

        if latest_tone is None:
            log.warning("GDELT TimelineTone %s: no numeric values in %d points", country, len(points))
            return None

        log.info("GDELT TimelineTone %s: latest tone = %+.2f (from %d points)", country, latest_tone, len(points))
        cache.set(cache_key, latest_tone, NARRATIVE_TTL)
        return latest_tone
    except Exception as e:
        log.warning("GDELT TimelineTone fetch failed (%s): %s", country, e)
        return None


async def _cluster_signal(client, region, cluster):
    country = COUNTRY_CODES[region]
    query = GDELT_QUERIES[cluster]

    tone = await _gdelt_timeline_tone(client, query, country)
    if tone is None:
        log.warning("narrative[%s/%s]: tone unavailable", region, cluster)
        return None

    tone_norm = clip(tone / 5.0)
    log.info("narrative[%s/%s]: raw_tone=%+.2f normalised=%+.3f", region, cluster, tone, tone_norm)
    return tone_norm


async def fetch_narrative():
    out = {}
    any_live = False

    async with httpx.AsyncClient() as client:
        first = True
        for region in ("us", "uk", "india"):
            if not first:
                await asyncio.sleep(REQUEST_PAUSE)
            first = False

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
