"""
Narrative source: GDELT DOC 2.0 API, TimelineTone mode.

Tone is what GDELT measures natively. We fetch the 1-day tone timeline
per region and take the most recent non-null value. Negative tone on
anxiety keywords = narrative stress (directly, no sign flip needed).

Rate limit: GDELT documents 1 req/5s. We use 8s pauses including before
the first request, to give the server margin.

Output: scalar in (-1, +1) per region. Negative = stress dominant.
"""

import asyncio
import logging
from typing import Optional

import httpx

from cache import cache
from normalise import tanh_squash, clip

log = logging.getLogger("animal-spirits.narrative")

GDELT_QUERY = '("recession" OR "unemployment" OR "inflation" OR "crisis" OR "layoffs" OR "bankruptcy")'

COUNTRY_CODES = {
    "us": "US",
    "uk": "UK",
    "india": "IN",
}

NARRATIVE_TTL = 900
REQUEST_PAUSE = 8.0


async def _gdelt_timeline_tone(client, country):
    cache_key = f"gdelt_tone:{country}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    full_query = f"{GDELT_QUERY} sourcecountry:{country}"
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": full_query,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": "1d",
    }
    try:
        r = await client.get(url, params=params, timeout=20.0)
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
        log.warning("GDELT TimelineTone fetch failed (%s): %s", country, repr(e))
        return None


async def fetch_narrative():
    out = {}
    any_live = False

    async with httpx.AsyncClient() as client:
        for region in ("us", "uk", "india"):
            # Pause before every request, including the first,
            # to give GDELT's rate-limit window some margin.
            await asyncio.sleep(REQUEST_PAUSE)

            country = COUNTRY_CODES[region]
            tone = await _gdelt_timeline_tone(client, country)

            if tone is None:
                out[region] = None
                continue

            # Negative tone on anxiety keywords = stress signal directly.
            # Normalise: GDELT tone is roughly [-10, +10] in practice.
            # Divide by 5 and squash.
            tone_norm = clip(tone / 5.0)
            out[region] = tanh_squash(tone_norm, scale=1.0)
            any_live = True
            log.info("narrative[%s]: raw_tone=%+.2f -> scalar=%+.3f", region, tone, out[region])

    status = "live" if any_live else "simulated"
    return out, status
