"""
Narrative source: GDELT DOC 2.0 API, TimelineTone mode.

Tone is GDELT's native output. Negative tone on anxiety keywords = 
narrative stress, used directly without sign flip.

Connection strategy: one keep-alive httpx client, 30s timeouts, retry
on ConnectTimeout. GDELT's free endpoint is slow and sometimes refuses
fresh TCP connections from GitHub Actions runners; reusing an open
connection sidesteps that.

Output: scalar in (-1, +1) per region. Negative = stress dominant.
"""

import asyncio
import logging

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
REQUEST_PAUSE = 6.0
MAX_ATTEMPTS = 3
RETRY_DELAY = 10.0


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

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                log.warning("GDELT TimelineTone %s attempt %d: HTTP %d", country, attempt, r.status_code)
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return None
            try:
                data = r.json()
            except Exception as e:
                log.warning("GDELT TimelineTone %s attempt %d: JSON parse failed: %s", country, attempt, e)
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
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
                log.warning("GDELT TimelineTone %s: no numeric values", country)
                return None

            log.info("GDELT TimelineTone %s: latest tone = %+.2f (from %d points, attempt %d)",
                     country, latest_tone, len(points), attempt)
            cache.set(cache_key, latest_tone, NARRATIVE_TTL)
            return latest_tone

        except httpx.ConnectTimeout:
            log.warning("GDELT TimelineTone %s attempt %d: ConnectTimeout", country, attempt)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except httpx.ReadTimeout:
            log.warning("GDELT TimelineTone %s attempt %d: ReadTimeout", country, attempt)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except Exception as e:
            log.warning("GDELT TimelineTone %s attempt %d: %s", country, attempt, repr(e))
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None

    return None


async def fetch_narrative():
    out = {}
    any_live = False

    # Keep-alive client, generous timeout.
    timeout = httpx.Timeout(30.0, connect=15.0)
    limits = httpx.Limits(max_keepalive_connections=1, keepalive_expiry=60.0)

    async with httpx.AsyncClient(timeout=timeout, limits=limits,
                                 headers={"User-Agent": "AnimalSpirits/1.0"}) as client:
        for region in ("us", "uk", "india"):
            await asyncio.sleep(REQUEST_PAUSE)

            country = COUNTRY_CODES[region]
            tone = await _gdelt_timeline_tone(client, country)

            if tone is None:
                out[region] = None
                continue

            tone_norm = clip(tone / 5.0)
            out[region] = tanh_squash(tone_norm, scale=1.0)
            any_live = True
            log.info("narrative[%s]: raw_tone=%+.2f -> scalar=%+.3f", region, tone, out[region])

    status = "live" if any_live else "simulated"
    return out, status
