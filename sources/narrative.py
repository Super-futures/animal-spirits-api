"""
Narrative source: GDELT DOC 2.0 API, TimelineTone mode.

We use TimelineTone because it's purpose-built for what we need: a time
series of *average tone* across matching articles, rather than article-level
metadata (which ArtList returns). Tone is what GDELT was designed to
measure, and TimelineTone exposes it directly.

Per region/cluster, we fetch a 1-day timeline of tone and take the most
recent non-null value. No baseline fetch needed — tone is already in
GDELT's native scale (roughly -10 to +10) and doesn't require rolling
normalisation the way volume would.

Rate limit: strict 5.5s serialisation per GDELT's documented 1 req/5s.
One cluster × 3 regions = 3 requests × ~13s each = ~40s total.

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
REQUEST_PAUSE = 5.5


async def _gdelt_timeline_tone(client: httpx.AsyncClient, query: str, country: str) -> Optional[float]:
    """
    Fetch TimelineTone for a query+country over the last day.
    Returns the most recent non-null tone value, or None on failure.
    """
    cache_key = f"gdelt_tone:{country}:{hash(query)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    full_query = f'{query} sourcecountry:{country}'
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

        # Structure: { "timeline": [ { "data": [ { "date": ..., "value": ... }, ... ] } ] }
        timeline = data.get("timeline", [])
        if not timeline:
            log.warning("GDELT TimelineTone %s: empty timeline", country)
            return None
        points = timeline[0].get("data", [])
        if not points:
            log.warning("GDELT TimelineTone %s: empty data points", country)
            return None

        # Take the most recent point that has a non-null numeric value
        latest_tone = None
        for point in reversed(points):
            v = point.get("value")
            if v is not None and isinstance(v, (int, float)):
                latest_tone = float(v)
                break

        if latest_tone is None:
            log.warning("GDELT TimelineTone %s: no numeric values in %d points", country, len(points))
            return None

        log.info("GDELT TimelineTone %s: latest tone = %+.2f (from %d points)",
                 country, latest_tone, len(points))
        cache.set(cache_key, latest_tone, NARRATIVE_TTL)
        return latest_tone
    except Exception as e:
        log.warning("GDELT TimelineTone fetch failed (%s): %s", country, e)
        return None


async def _cluster_signal(client: httpx.AsyncClient, region: str, cluster: str) -> Optional[float]:
    """
    One cluster's contribution for one region. Returns a signed scalar in (-1, +1).
    """
    country = COUNTRY_CODES[region]
    query = GDELT_QUERIES[cluster]

    tone = await
