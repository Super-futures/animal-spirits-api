// ═══════════════════════════════════════════════════════════════
// FRONTEND PATCH — drop-in replacement for the live data section
// ═══════════════════════════════════════════════════════════════
//
// Architecture: the frontend now polls a static JSON file served by
// GitHub Pages. That file is refreshed every 15 min by a scheduled
// GitHub Action. No API server, no cold start, no wake-up dance.
//
// Replace the entire "// LIVE DATA" block in animalspirits_11.html
// (from `const API_URL = ...` down through the end of the script) with this.
//
// Delete: fetchWithWake, fetchWikiViews, fetchSentiment,
//         fetchAllSentiment, fetchGDELT
// Keep:   axisStatus, updateAPIBadge (both renamed below for clarity),
//         the window.liveX shape (unchanged), and every downstream
//         pipeline function (getRegionState, processors, draw, panels).
//
// API response contract (what state.json looks like):
//   {
//     timestamp: ISO string,
//     regions: { us: {attention, market, narrative}, uk: {...}, india: {...} },
//     meta: { attention: 'live'|'simulated', market: ..., narrative: ... }
//   }
// ═══════════════════════════════════════════════════════════════

// Update <your-org> once you've created the repo.
const STATE_URL = 'https://super-futures.github.io/animal-spirits-api/data/state.json';

window.liveMarkets   = null;
window.liveSentiment = null;
window.liveNarrative = null;
const axisStatus = { market: false, sentiment: false, narrative: false };

function updateAPIBadge() {
  const badge = document.getElementById('api-badge');
  if (!badge) return;
  const m = axisStatus.market    ? 'M●' : 'M○';
  const s = axisStatus.sentiment ? 'S●' : 'S○';
  const n = axisStatus.narrative ? 'N●' : 'N○';
  badge.innerHTML =
    `<span style="color:${axisStatus.market    ? '#4A8C5C' : 'var(--text3)'}">${m}</span> ` +
    `<span style="color:${axisStatus.sentiment ? '#E8803A' : 'var(--text3)'}">${s}</span> ` +
    `<span style="color:${axisStatus.narrative ? '#A855C8' : 'var(--text3)'}">${n}</span>`;
  badge.title =
    `Market: ${axisStatus.market    ? 'live' : 'simulated'} · ` +
    `Attention: ${axisStatus.sentiment ? 'live' : 'simulated'} · ` +
    `Narrative: ${axisStatus.narrative ? 'live' : 'simulated'}`;
}

async function fetchState() {
  try {
    // Cache-bust via querystring so we always get the latest commit.
    // GitHub Pages has a short CDN cache; this avoids stale reads.
    const res = await fetch(STATE_URL + '?t=' + Date.now(), { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    console.warn('State fetch failed:', e);
    return null;
  }
}

async function fetchLiveData() {
  const state = await fetchState();

  if (!state || !state.regions || !state.meta) {
    updateAPIBadge();
    return;
  }

  // Map the composed state into the shape the existing pipeline expects.
  // getRegionState() and its downstream processing remain untouched.
  const liveMarkets   = {};
  const liveSentiment = {};
  const liveNarrative = {};

  for (const region of ['us', 'uk', 'india']) {
    const r = state.regions[region] || {};

    // Market: already a signed scalar in [-1, +1].
    if (r.market !== null && r.market !== undefined) {
      liveMarkets[region] = { field_value: r.market };
    }

    // Attention: backend returns [-1, +1]; frontend expects [0, 1].
    // Remap: -1 → 0, 0 → 0.5, +1 → 1.
    if (r.attention !== null && r.attention !== undefined) {
      liveSentiment[region] = {
        combined: { value: (r.attention + 1) / 2 }
      };
    }

    // Narrative: backend returns signed [-1, +1]; tone_normalised is the
    // field name the existing pipeline already understands.
    if (r.narrative !== null && r.narrative !== undefined) {
      liveNarrative[region] = {
        combined: { tone_normalised: r.narrative }
      };
    }
  }

  window.liveMarkets   = Object.keys(liveMarkets).length   ? liveMarkets   : null;
  window.liveSentiment = Object.keys(liveSentiment).length ? liveSentiment : null;
  window.liveNarrative = Object.keys(liveNarrative).length ? liveNarrative : null;

  axisStatus.market    = state.meta.market    === 'live';
  axisStatus.sentiment = state.meta.attention === 'live';
  axisStatus.narrative = state.meta.narrative === 'live';

  updateAPIBadge();
  invalidateStateCache();
  panels();
  draw();
}

// Initial fetch + periodic refresh.
// The source file only updates every 15 min, so polling more often is wasted.
// We poll every 5 min to catch new commits reasonably quickly.
updateAPIBadge();
setTimeout(fetchLiveData, 200);
setInterval(fetchLiveData, 5 * 60 * 1000);
