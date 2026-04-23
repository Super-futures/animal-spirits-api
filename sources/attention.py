"""
Attention source: Wikimedia Pageviews.

Per-region cluster averages, z-scored against each term's own 30-day
rolling baseline. This produces "unusual attention relative to this
term's own history" rather than absolute volume — which better captures
the *affective* dimension (curiosity spikes) vs. background popularity.

Clusters (4): anxiety, confidence, aspiration, constraint
We combine them into a single scalar per region where:
    + anxiety and + constraint push negative (stress)
    + confidence and + aspiration push positive (expansion)

Output: scalar in (-1, +1) per region.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from cache import cache
from normalise import z_score, tanh_squash, clip

log = logging.getLogger("animal-spirits.attention")

# Expanded term lists (~8 per cluster per region where meaningful).
# Using en.wikipedia across all three regions for now; India would ideally
# also sample hi.wikipedia / ta.wikipedia, but keeping English-only for v1
# to match existing frontend behaviour and avoid language-pipeline complexity.
WIKI_TERMS = {
    "anxiety": {
        "us": ["Recession", "Unemployment", "Stock_market_crash", "Inflation",
               "Layoff", "Bankruptcy", "Financial_crisis", "Economic_bubble"],
        "uk": ["Recession", "Unemployment", "Cost_of_living_crisis", "Inflation",
               "Redundancy_(law)", "Bankruptcy", "Financial_crisis", "Economic_bubble"],
        "india": ["Recession", "Unemployment_in_India", "Inflation", "Stock_market_crash",
                  "Financial_crisis", "Layoff", "Economic_bubble", "Poverty_in_India"],
    },
    "confidence": {
        "us": ["Bull_market", "Economic_growth", "Consumer_confidence", "Investment",
               "Initial_public_offering", "Venture_capital", "Employment", "Stock"],
        "uk": ["Economic_growth", "Consumer_confidence", "Investment", "Bull_market",
               "Initial_public_offering", "Employment", "Stock", "Gross_domestic_product"],
        "india": ["Economic_growth", "Bull_market", "Investment", "Initial_public_offering",
                  "Stock", "Entrepreneurship", "Venture_capital", "Gross_domestic_product"],
    },
    "aspiration": {
        "us": ["Luxury_goods", "Travel", "Real_estate", "Cryptocurrency",
               "Sports_car", "Yacht", "Private_jet", "Fine_dining"],
        "uk": ["Luxury_goods", "Travel", "Property", "Cryptocurrency",
               "Sports_car", "Yacht", "Private_jet", "Fine_dining"],
        "india": ["Luxury_goods", "Tourism", "Real_estate", "Cryptocurrency",
                  "Sports_car", "Gold_as_an_investment", "Entrepreneurship", "Fine_dining"],
    },
    "constraint": {
        "us": ["Budget", "Debt", "Frugality", "Minimum_wage",
               "Food_insecurity", "Homelessness", "Poverty", "Student_debt"],
        "uk": ["Austerity", "Budget", "Food_bank", "Cost_of_living",
               "Universal_Credit", "Poverty", "Homelessness", "Debt"],
        "india": ["Budget", "Debt", "Frugality", "Poverty_in_India",
                  "Unemployment_in_India", "Microfinance", "Minimum_wage", "Food_security"],
    },
}

# Cluster weights for composite (matching the anxiety/confidence/aspiration/constraint framework)
CLUSTER_WEIGHTS = {
    "anxiety":    -1.0,
    "confidence": +1.0,
    "aspiration": +0.5,
    "constraint": -0.7,
}

PAGEVIEW_TTL = 1800  # 30 min — Wikimedia updates daily, no need for faster


async def _fetch_article_series(client: httpx.AsyncClient, article: str) -> Optional[list[float]]:
    """
    Fetch last 30 days of pageviews for one article.
    Returns list of daily views (oldest first).
    """
    cache_key = f"wiki:{article}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    end = datetime.utcnow() - timedelta(days=1)  # Yesterday; today's data is incomplete
    start = end - timedelta(days=30)
    url = (
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"en.wikipedia/all-access/all-agents/{article}/daily/"
        f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    )
    try:
        r = await client.get(url, headers={"User-Agent": "AnimalSpirits/1.0"}, timeout=6.0)
        if r.status_code != 200:
            return None
        data = r.json()
        views = [float(item["views"]) for item in data.get("items", [])]
        if len(views) < 7:
            return None
        cache.set(cache_key, views, PAGEVIEW_TTL)
        return views
    except Exception as e:
        log.debug("Wiki fetch failed for %s: %s", article, e)
        return None


async def _cluster_z_score(client: httpx.AsyncClient, region: str, cluster: str) -> Optional[float]:
    """
    Average z-score across all terms in a cluster for a region.
    Each term's latest day is z-scored against its own 30-day history.
    """
    terms = WIKI_TERMS.get(cluster, {}).get(region, [])
    if not terms:
        return None
    series_list = await asyncio.gather(*(_fetch_article_series(client, t) for t in terms))
    z_values = []
    for series in series_list:
        if series is None or len(series) < 7:
            continue
        # z-score the most recent day against the prior history
        z = z_score(series[-1], series[:-1])
        z_values.append(z)
    if not z_values:
        return None
    return sum(z_values) / len(z_values)


async def fetch_attention() -> tuple[dict[str, Optional[float]], str]:
    """
    Returns ({region: scalar}, status).
    Scalar is the cluster-weighted composite, squashed to (-1, +1).
    Positive = expansion-coded attention (confidence/aspiration dominant).
    Negative = stress-coded attention (anxiety/constraint dominant).
    """
    out: dict[str, Optional[float]] = {}
    any_live = False

    async with httpx.AsyncClient() as client:
        for region in ("us", "uk", "india"):
            cluster_values = {}
            for cluster in CLUSTER_WEIGHTS:
                cluster_values[cluster] = await _cluster_z_score(client, region, cluster)

            # Composite: weighted sum of cluster z-scores
            contributions = []
            for cluster, z in cluster_values.items():
                if z is not None:
                    contributions.append(CLUSTER_WEIGHTS[cluster] * z)

            if not contributions:
                out[region] = None
            else:
                composite = sum(contributions) / len(contributions)
                out[region] = tanh_squash(composite, scale=1.5)
                any_live = True

    status = "live" if any_live else "simulated"
    return out, status
