/* =========================================================
   FOXAI TIME MACHINE — app.js
   Vanilla JS. No build step, no framework, no backend.
   State (favorites/saved/follows/recents) lives in localStorage,
   so it is personal to this browser/device only.
   ========================================================= */

(function(){
  "use strict";

  const RAW_INFOGRAPHICS = Array.isArray(window.TIME_MACHINE_INFOGRAPHICS) ? window.TIME_MACHINE_INFOGRAPHICS : [];

  // Turns one authored infographic entry (brief section 9's schema) into
  // the same lightweight card shape everything else uses. Generic on
  // purpose — works for any infographicType, not just the shipped example.
  function infographicToCard(info, idx){
    const template = { classificationDefault: "FOXAI INFOGRAPHIC", primaryAction: "Open Gazette" };
    const sources = Array.isArray(info.sources) ? info.sources : [];
    const slug = String(info.title || `infographic-${idx}`).toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/(^-+|-+$)/g, "") || `infographic-${idx}`;
    return {
      id: `infographic-${slug}`,
      title: info.title || "Untitled Infographic",
      hook: info.hook || info.question || "",
      longerContext: info.question || info.hook || "",
      date: "",
      location: "",
      type: "infographic",
      mediaTemplate: "infographic",
      classificationLabel: template.classificationDefault,
      image: null,
      sourceUrl: sources[0] ? sources[0].url : null,
      sourceName: "FOXAI Time Machine",
      institution: "FOXAI Time Machine",
      actionLabel: template.primaryAction,
      tags: [info.infographicType].filter(Boolean),
      relatedPeople: [],
      relatedPlaces: [],
      relatedCardIds: [],
      evidence: sources.length ? { institution: "FOXAI Time Machine (authored)", sourceItemId: null, sourceUrl: sources[0].url, collection: null, provider: null } : null,
      reconstruction: false,
      indigenous: false,
      infographicData: info   // raw authored data — the generic renderer in openLearnMore reads this
    };
  }
  const INFOGRAPHIC_CARDS = RAW_INFOGRAPHICS.map(infographicToCard);

  const { cards: DEMO_CARDS, skipped: SKIPPED_CARDS } = window.FXLib.loadCards([...window.TIME_MACHINE_CARDS, ...INFOGRAPHIC_CARDS]);
  if(SKIPPED_CARDS.length){
    console.warn("FOXAI Time Machine: skipped", SKIPPED_CARDS.length, "malformed card(s) so the feed keeps working:", SKIPPED_CARDS);
  }
  const WORLDLINES = Array.isArray(window.TIME_MACHINE_WORLDLINES) ? window.TIME_MACHINE_WORLDLINES : [];

  // ACTIVE_CARDS is what the TimeFeed/Gazette/Explore actually render.
  // It starts as the offline demo set and is replaced with normalized
  // live items from the local Time Machine service if that service is
  // reachable (see loadLiveFeedIfAvailable below). content/cards.json is
  // an offline/demo fallback now, not the primary content pipeline.
  let ACTIVE_CARDS = DEMO_CARDS;
  let cardsById = Object.fromEntries(ACTIVE_CARDS.map(c => [c.id, c]));
  let usingLiveSource = false;
  let baseCards = ACTIVE_CARDS; // what "clear search" returns to
  let broadcastState = { enabled: false, isHost: false, joinUrl: null, connected: 0, sharedCard: null };

  // Continuous TimeFeed / Surprise Me session memory — ids already shown
  // this browsing session (including trimmed-away cards) so load-more and
  // Surprise Me can exclude them without rewriting favorites/saves.
  const MAX_RENDERED_CARDS = 180;
  const LOAD_MORE_BATCH = 8;
  const LOAD_MORE_NEAR_PX = 900;
  const LOAD_MORE_EMPTY_COOLDOWN_MS = 10000;
  const SESSION_SEEN_CAP = 2000;
  let sessionSeenIds = new Set(ACTIVE_CARDS.map(c => c.id));
  let loadMoreInFlight = false;
  let loadMoreCooldownUntil = 0;
  let loadMoreBackoffMs = 1500;
  // Discovery roam depth (v2.0): each successful surprise batch advances
  // this page counter, which the server turns into deeper query-pool
  // rotation and real per-archive pagination. Resets whenever the feed is
  // freshly seeded from the featured seeds.
  let discoveryPage = 1;
  // Soft session memory of institutions seen in continuous/Surprise batches
  // so the server can gently prefer underrepresented sources.
  let recentSourceIds = [];
  const RECENT_SOURCES_CAP = 40;

  const STREAM_INTERVALS = [8000, 15000, 30000];
  const STREAM_DEFAULT_MS = 15000;
  const STREAM_PREFETCH_REMAINING = 3;
  let streamQueue = [];
  let streamIndex = 0;
  let streamAutoplay = true;
  let streamTimer = null;
  let streamIntervalMs = STREAM_DEFAULT_MS;
  let streamFetchInFlight = false;
  let streamSeenIds = new Set();
  let streamBrokenIds = new Set();
  let streamSeeded = false;

  function noteRecentSources(items){
    (items || []).forEach(it => {
      const id = it && it.sourceId;
      if(id) recentSourceIds.push(id);
    });
    if(recentSourceIds.length > RECENT_SOURCES_CAP){
      recentSourceIds = recentSourceIds.slice(-RECENT_SOURCES_CAP);
    }
  }

  function recentSourcesParam(){
    return encodeURIComponent(recentSourceIds.join(","));
  }

  /* ---------------- local state (persisted) ---------------- */
  const STORAGE_KEY = "foxai_timemachine_v1";
  const defaultState = {
    favorites: [],       // card ids
    saved: [],           // card ids
    followedTags: [],
    followedPeople: [],
    followedPlaces: [],
    recent: [],          // card ids, most recent first
    itemCache: {}         // id -> full card data, so a saved/favorited remote
                           // item can still be shown if its source or the
                           // live service is temporarily unavailable later
  };
  let state = loadState();

  function loadState(){
    try{
      const raw = localStorage.getItem(STORAGE_KEY);
      if(!raw) return structuredCloneSafe(defaultState);
      const parsed = JSON.parse(raw);
      return Object.assign(structuredCloneSafe(defaultState), parsed);
    }catch(e){
      console.warn("Could not read saved Time Machine data, starting fresh.", e);
      return structuredCloneSafe(defaultState);
    }
  }
  function structuredCloneSafe(o){ return JSON.parse(JSON.stringify(o)); }
  function saveState(){
    try{ localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch(e){ console.warn("Could not save Time Machine data locally.", e); }
  }
  function toggleInList(listName, id){
    state[listName] = window.FXLib.toggleInArray(state[listName], id);
    saveState();
  }

  // A saved/favorited remote item keeps enough of its own data to redisplay
  // later even if the live source or local service is temporarily down
  // (brief section 14) — we never re-download the original archival media,
  // just the small normalized record already shown in the card.
  function cacheItemData(id){
    if(cardsById[id]){
      state.itemCache[id] = cardsById[id];
      saveState();
    }
  }
  function lookupCard(id){
    return cardsById[id] || state.itemCache[id] || null;
  }
  function pushRecent(id){
    state.recent = [id, ...state.recent.filter(x => x !== id)].slice(0, 30);
    pruneItemCache();
    saveState();
  }
  // Keeps itemCache from growing forever — only hang onto data for cards
  // that are still referenced by favorites, saves, or the recent list.
  function pruneItemCache(){
    const keep = new Set([...state.favorites, ...state.saved, ...state.recent]);
    Object.keys(state.itemCache).forEach(id => {
      if(!keep.has(id)) delete state.itemCache[id];
    });
  }

  /* ---------------- normalized remote item -> card shape ---------------- */
  // Bridges sources/base.py's TimeMachineItem (mediaKind/mediaTemplate/
  // evidence/etc.) into the same lightweight "card" shape the existing
  // renderCard()/openLearnMore() already know how to draw, so the UI itself
  // didn't need to be redesigned to accept live items.
  function normalizedItemToCard(item){
    const template = (window.FXLib.templateInfo ? window.FXLib.templateInfo(item.mediaTemplate) : null) || {};
    return {
      id: item.id,
      title: item.title,
      hook: item.description || "",
      longerContext: item.description || "",
      date: item.dateLabel || "",
      location: item.location || "",
      type: item.mediaKind,
      mediaTemplate: item.mediaTemplate,
      classificationLabel: item.mediaClassification || template.classificationDefault || null,
      image: item.imageUrl || null,
      thumbnailUrl: item.thumbnailUrl || null,
      audioUrl: item.audioUrl || null,
      sourceUrl: item.sourceUrl,
      sourceName: item.sourceInstitution,
      institution: item.sourceInstitution,
      actionLabel: template.primaryAction || "View Original at Institution",
      tags: item.topics || [],
      relatedPeople: item.people || [],
      relatedPlaces: item.places || [],
      relatedCardIds: [],
      evidence: item.evidence || null,
      contextStatus: item.contextStatus || "not-required",
      reconstruction: false,
      indigenous: false
    };
  }

  function rememberSeenIds(ids){
    ids.forEach(id => { if(id) sessionSeenIds.add(id); });
    if(sessionSeenIds.size > SESSION_SEEN_CAP){
      const kept = [...sessionSeenIds].slice(-Math.floor(SESSION_SEEN_CAP * 0.75));
      sessionSeenIds = new Set(kept);
    }
  }

  function protectedFeedIds(){
    return new Set([...(state.favorites || []), ...(state.saved || []), ...(state.recent || [])]);
  }

  function setActiveCards(cards, opts){
    opts = opts || {};
    ACTIVE_CARDS = cards;
    // Preserve any favorited/saved card objects that aren't in this batch.
    const keep = protectedFeedIds();
    keep.forEach(id => {
      if(!cardsById[id] && state.itemCache && state.itemCache[id]){
        cardsById[id] = state.itemCache[id];
      }
    });
    cards.forEach(c => { if(c && c.id) cardsById[c.id] = c; });
    feedOrder = ACTIVE_CARDS.map(c => c.id);
    if(opts.replaceSeen){
      sessionSeenIds = new Set(ACTIVE_CARDS.map(c => c.id));
    }else{
      rememberSeenIds(ACTIVE_CARDS.map(c => c.id));
    }
  }

  // Drop oldest ordinary rendered cards when the feed grows too large.
  // Favorites / saved / recent stay available via itemCache + cardsById.
  function trimRenderedFeed(){
    const keep = protectedFeedIds();
    while(ACTIVE_CARDS.length > MAX_RENDERED_CARDS){
      let removeAt = -1;
      for(let i = 0; i < ACTIVE_CARDS.length; i++){
        if(!keep.has(ACTIVE_CARDS[i].id)){ removeAt = i; break; }
      }
      if(removeAt < 0) break;
      const removed = ACTIVE_CARDS.splice(removeAt, 1)[0];
      feedOrder = feedOrder.filter(id => id !== removed.id);
      if(removed && removed.id && !keep.has(removed.id)){
        // Prefer keeping a cached copy if the user later favorites from recents;
        // ordinary trimmed cards are dropped from the live feed index only.
        if(!(state.itemCache && state.itemCache[removed.id])){
          delete cardsById[removed.id];
        }
      }
    }
    baseCards = ACTIVE_CARDS;
  }

  function appendFeedCards(cards){
    if(!cards || !cards.length) return [];
    const existing = new Set(ACTIVE_CARDS.map(c => c.id));
    const fresh = cards.filter(c => c && c.id && !existing.has(c.id));
    if(!fresh.length) return [];
    rememberSeenIds(fresh.map(c => c.id));
    ACTIVE_CARDS = ACTIVE_CARDS.concat(fresh);
    fresh.forEach(c => { cardsById[c.id] = c; });
    feedOrder = feedOrder.concat(fresh.map(c => c.id));
    trimRenderedFeed();
    baseCards = ACTIVE_CARDS;
    return fresh;
  }

  async function loadLiveFeedIfAvailable(){
    try{
      // offline=1 and the hermetic HTML fixtures under /tests/ must not
      // replace their authored cards with a live archive request.
      const path = window.location.pathname || "";
      if(/(?:^|[?&])offline=1(?:&|$)/.test(window.location.search || "")
         || path.indexOf("/tests/") !== -1){
        usingLiveSource = false;
        return;
      }
      const res = await fetch("/api/feed?limit=24");
      if(!res.ok) throw new Error("feed endpoint returned " + res.status);
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      if(items.length === 0){
        usingLiveSource = false;
        return;
      }
      discoveryPage = 1; // fresh featured seed — restart discovery roam depth
      const cards = items.map(normalizedItemToCard);
      setActiveCards(cards, { replaceSeen: true });
      baseCards = cards;
      noteRecentSources(items);
      usingLiveSource = true;
      if(streamSeeded){
        absorbStreamCards(cards);
        if(currentView === "timestream"){
          renderTimestream();
          ensureStreamCards();
        }
      }
    }catch(e){
      // No local service running, or it genuinely has nothing live right
      // now — Time Machine stays fully usable on the offline demo cards.
      usingLiveSource = false;
    }
  }


  const typeMeta = window.FXLib.typeMeta;
  const escapeHtml = window.FXLib.escapeHtml;
  const matchesSearch = window.FXLib.matchesSearch;
  const personalScore = (card) => window.FXLib.personalScore(card, state);

  /* ---------------- placeholder media (graceful fallback) ---------------- */
  function placeholderMedia(card){
    const meta = typeMeta(card.type);
    if(card.type === "audio" || card.type === "oral_history"){
      // No fake play button here — a card with no real audioUrl should never
      // *look* playable. See buildAudioMedia() for the real <audio> player.
      return `<div class="card__media--placeholder">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 9v6h4l5 4V5L8 9H4z"/></svg>
        <span>${meta.icon}&nbsp; No playable recording linked yet — use “Learn More” to open the source and listen there.</span>
      </div>`;
    }
    return `<div class="card__media--placeholder">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="12" cy="12" r="9"/></svg>
      <span>${meta.icon}&nbsp; ${escapeHtml(meta.label)} · no cached image yet — see source</span>
    </div>`;
  }

  // Native <audio> playback — no custom audio engine, just the browser's own.
  // Only used when a card supplies a real, playable audioUrl.
  function buildAudioMedia(card, opts){
    opts = opts || {};
    if(!card.audioUrl) return placeholderMedia(card);
    if(opts.mini){
      return `<div class="card__media--placeholder" style="padding:0;"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 9v6h4l5 4V5L8 9H4z"/></svg></div>`;
    }
    return `<div class="audio-player">
      <audio controls preload="none" style="width:100%; display:block;">
        <source src="${escapeHtml(card.audioUrl)}">
        Your browser doesn't support inline audio.
        <a href="${escapeHtml(card.sourceUrl || card.audioUrl)}" target="_blank" rel="noopener">Open the recording</a>.
      </audio>
    </div>`;
  }

  /* ---------------- card rendering ---------------- */
  function buildMediaHtml(card, opts){
    opts = opts || {};
    if(card.type === "audio" || card.type === "oral_history"){
      return buildAudioMedia(card, opts);
    }
    const candidates = window.FXLib.imageCandidates(card);
    if(candidates.length === 0) return placeholderMedia(card);
    const first = candidates[0];
    const rest = candidates.slice(1);
    const imgStyle = opts.mini ? ' style="width:100%;height:100%;object-fit:cover;"' : "";
    // data-remaining carries any further candidate URLs to try if this one 404s/fails.
    return `<img src="${escapeHtml(first)}" alt="${escapeHtml(card.title)}" loading="lazy"${imgStyle}
      data-remaining='${escapeHtml(JSON.stringify(rest))}'
      onerror="window.__fxImgError(this, '${escapeHtml(card.id)}')">`;
  }

  // Exposed globally so the inline onerror attribute above can reach it.
  // Tries each remaining image candidate in turn; once none are left, falls
  // back to the type-appropriate placeholder so the card stays usable and
  // the original source link is never lost.
  window.__fxImgError = function(imgEl, cardId){
    let remaining = [];
    try{ remaining = JSON.parse(imgEl.getAttribute("data-remaining") || "[]"); }catch(e){ remaining = []; }
    if(remaining.length > 0){
      const next = remaining.shift();
      imgEl.setAttribute("data-remaining", JSON.stringify(remaining));
      imgEl.src = next;
      return;
    }
    const card = cardsById[cardId];
    const container = imgEl.closest(".card__media");
    if(container) container.innerHTML = card ? placeholderMedia(card) : "";
  };
  function renderCard(card, opts){
    opts = opts || {};
    const meta = typeMeta(card.type);
    const isFav = state.favorites.includes(card.id);
    const isSaved = state.saved.includes(card.id);

    const mediaHtml = buildMediaHtml(card);

    const reconBadge = card.reconstruction ? `<span class="badge-reconstruction">Time Machine Reconstruction</span>` : "";
    const indigBadge = card.indigenous ? `<span class="badge-indigenous">Indigenous history</span>` : "";
    const gazetteStamp = card.type === "gazette" ? `<div class="gazette-stamp">The Time Machine Gazette${card.reconstruction ? "" : " · Archival Front Page"}</div>` : "";

    const tags = (card.tags || []).slice(0, 6).map(t =>
      `<button class="tagchip" data-explore-tag="${escapeHtml(t)}">#${escapeHtml(t)}</button>`
    ).join("");

    // The mixed-media template decides the primary action (brief section 3):
    // external templates (photograph, document, audio, moving image...)
    // link straight to the institution; Time Machine-authored templates
    // (reconstruction, infographic, timeline...) open the Gazette/Learn
    // More panel instead. Favorite/Save/Share/Learn More stay as secondary
    // actions regardless.
    const templateId = card.mediaTemplate || window.FXLib.mediaKindToTemplate(card.type);
    const template = window.FXLib.templateInfo(templateId);
    // The template contract (brief section 3) owns the exact wording of the
    // primary action ("View Original at Institution", "Watch at
    // Institution", etc.) — it takes priority over any older ad-hoc
    // actionLabel a card might carry, so the action shown is always the
    // spec's exact string for that template.
    const primaryLabel = template.primaryAction || card.actionLabel || "Learn More";
    const primaryHtml = (template.external && card.sourceUrl)
      ? `<a class="actbtn actbtn--primary" href="${escapeHtml(card.sourceUrl)}" target="_blank" rel="noopener" data-id="${escapeHtml(card.id)}">${escapeHtml(primaryLabel)} ↗</a>`
      : `<button class="actbtn actbtn--primary" data-act="learnmore" data-id="${escapeHtml(card.id)}">${escapeHtml(primaryLabel)}</button>`;
    const evidenceHtml = card.evidence
      ? `<button class="actbtn" data-act="evidence" data-id="${escapeHtml(card.id)}">📎 Evidence</button>`
      : "";
    const shareClassHtml = (broadcastState && broadcastState.enabled && broadcastState.isHost)
      ? `<button class="actbtn" data-act="shareclass" data-id="${escapeHtml(card.id)}">Share with class</button>`
      : "";

    return `
    <article class="card card--${escapeHtml(card.type || "document")}" data-card-id="${escapeHtml(card.id)}">
      <div class="card__head">
        <span class="card__typebadge">${meta.icon} ${escapeHtml(card.classificationLabel || meta.label)}</span>
        ${indigBadge}${reconBadge}
        <span class="card__date">${escapeHtml(card.date || "")}</span>
      </div>
      <div class="card__body">
        <span class="card__title">${escapeHtml(card.sourceName || card.institution || "")}</span>
        <h2 class="card__hook">${escapeHtml(card.title)}</h2>
        ${card.hook ? `<p style="margin:0 0 8px;">${escapeHtml(card.hook)}</p>` : ""}
        ${card.location ? `<p class="card__location">📍 ${escapeHtml(card.location)}</p>` : ""}
        ${card.duration && template.showsDuration !== false ? `<p class="card__location">⏱ ${escapeHtml(card.duration)}</p>` : ""}
      </div>
      ${gazetteStamp}
      <div class="card__media">${mediaHtml}</div>
      <div class="card__actions">
        <button class="actbtn ${isFav ? "is-active":""}" data-act="fav" data-id="${escapeHtml(card.id)}">${isFav ? "♥" : "♡"} Favorite</button>
        <button class="actbtn ${isSaved ? "is-active":""}" data-act="save" data-id="${escapeHtml(card.id)}">🔖 ${isSaved ? "Saved" : "Save"}</button>
        <button class="actbtn" data-act="share" data-id="${escapeHtml(card.id)}">↗ Share</button>
        ${evidenceHtml}
        <button class="actbtn" data-act="learnmore" data-id="${escapeHtml(card.id)}">Learn More</button>
        ${primaryHtml}
        ${shareClassHtml}
      </div>
      ${tags ? `<div class="card__tags">${tags}</div>` : ""}
    </article>`;
  }

  function renderMiniCard(card){
    const meta = typeMeta(card.type);
    const mediaHtml = buildMediaHtml(card, { mini: true });
    return `<article class="card card--mini" data-card-id="${escapeHtml(card.id)}">
      <div class="card__media">${mediaHtml}</div>
      <div class="mini-info">
        <h4>${escapeHtml(card.title)}</h4>
        <span>${meta.icon} ${escapeHtml(card.date || "")}${card.location ? " · " + escapeHtml(card.location) : ""}</span>
      </div>
    </article>`;
  }

  /* ---------------- Learn More panel ---------------- */
  const lmOverlay = document.getElementById("learnMoreOverlay");
  const lmContent = document.getElementById("lmContent");
  document.getElementById("lmClose").addEventListener("click", closeLearnMore);
  lmOverlay.addEventListener("click", e => { if(e.target === lmOverlay) closeLearnMore(); });
  document.addEventListener("keydown", e => { if(e.key === "Escape") closeLearnMore(); });

  function closeLearnMore(){
    lmOverlay.hidden = true;
    lmContent.innerHTML = "";
    if(currentView === "timestream" && streamAutoplay) startStreamTimer();
  }

  function openLearnMore(cardId){
    const card = lookupCard(cardId);
    if(!card) return;
    if(currentView === "timestream") stopStreamTimer();
    pushRecent(cardId);
    state.itemCache[cardId] = card; // opening Learn More is enough to keep it findable later
    saveState();
    heartbeatSession(cardId);

    const perspectives = (card.perspectives || []).map(p => `
      <div class="perspective">
        <div class="perspective__label">${escapeHtml(p.label)}</div>
        <p>${escapeHtml(p.text)}</p>
        <div class="perspective__source">${escapeHtml(p.source || "")}${p.sourceUrl ? ` — <a href="${escapeHtml(p.sourceUrl)}" target="_blank" rel="noopener">source</a>` : ""}</div>
      </div>`).join("");

    const relatedIds = (card.relatedCardIds || []).filter(id => cardsById[id]);
    const relatedHtml = relatedIds.length ? `
      <div class="lm-section-label">Continue exploring</div>
      <div class="lm-related">${relatedIds.map(id => `<button class="chip" data-open-card="${escapeHtml(id)}">${escapeHtml(cardsById[id].title)}</button>`).join("")}</div>
    ` : "";

    const peopleHtml = (card.relatedPeople && card.relatedPeople.length) ? `
      <div class="lm-section-label">People</div>
      <div class="lm-related">${card.relatedPeople.map(p => `<button class="chip" data-follow="people:${escapeHtml(p)}">${state.followedPeople.includes(p) ? "✓ " : "+ Follow "}${escapeHtml(p)}</button>`).join("")}</div>` : "";

    const placesHtml = (card.relatedPlaces && card.relatedPlaces.length) ? `
      <div class="lm-section-label">Places</div>
      <div class="lm-related">${card.relatedPlaces.map(p => `<button class="chip" data-follow="places:${escapeHtml(p)}">${state.followedPlaces.includes(p) ? "✓ " : "+ Follow "}${escapeHtml(p)}</button>`).join("")}</div>` : "";

    // Evidence: kept minimal on purpose (brief section 15) — a line or two
    // of provenance, not a bibliography stapled to every card.
    const ev = card.evidence;
    const evidenceLine = ev ? [ev.institution, ev.collection, ev.provider].filter(Boolean).join(" · ") : "";
    const evidenceHtml = ev && evidenceLine ? `
      <div class="lm-section-label" id="evidence">Evidence</div>
      <p style="font-size:0.85rem;color:var(--ink-soft);">${escapeHtml(evidenceLine)}${ev.sourceItemId ? ` · item ${escapeHtml(ev.sourceItemId)}` : ""}</p>
    ` : "";

    // Generic authored-infographic rendering (brief section 5) — reads
    // whatever facts/visualSpecification/sources/etc. this particular
    // infographic happens to have. Not hard-coded to any one example.
    const infographicHtml = card.infographicData ? buildInfographicDetailHtml(card.infographicData) : "";
    const imgCandidates = window.FXLib.imageCandidates(card);

    lmContent.innerHTML = `
      <h2>${escapeHtml(card.title)}</h2>
      <div class="lm-meta">${escapeHtml(card.date || "")}${card.location ? " · " + escapeHtml(card.location) : ""}${card.institution ? " · " + escapeHtml(card.institution) : ""}</div>
      ${imgCandidates.length ? `<img class="lm-hero" src="${escapeHtml(imgCandidates[0])}" alt="${escapeHtml(card.title)}">` : ""}
      ${card.creator ? `<p><strong>${escapeHtml(card.creator)}</strong></p>` : ""}
      <p>${escapeHtml(card.longerContext || card.hook || "")}</p>
      ${infographicHtml}
      ${card.perspectives && card.perspectives.length ? `<div class="lm-section-label">Compare Perspectives</div>${perspectives}` : ""}
      ${peopleHtml}
      ${placesHtml}
      ${relatedHtml}
      ${evidenceHtml}
      ${card.sourceUrl ? `<a class="lm-source-link" href="${escapeHtml(card.sourceUrl)}" target="_blank" rel="noopener">${escapeHtml((window.FXLib.templateInfo(card.mediaTemplate || window.FXLib.mediaKindToTemplate(card.type)).primaryAction) || card.actionLabel || "View Original Source")} ↗</a>` : ""}
    `;
    lmOverlay.hidden = false;
  }

  // Generic infographic renderer — works for any infographicType (numbers,
  // migration, timeline, comparison, ...) because it only reads the fields
  // the authoring schema actually defines, not a per-topic hard-coded shape.
  function buildInfographicDetailHtml(data){
    const factsHtml = (Array.isArray(data.facts) && data.facts.length) ? `
      <div class="lm-section-label">The Numbers</div>
      <div style="display:grid;grid-template-columns:1fr auto;gap:4px 14px;font-size:0.9rem;">
        ${data.facts.map(f => `<span style="color:var(--ink-soft);">${escapeHtml(f.label || "")}</span><strong>${escapeHtml(f.value || "")}</strong>`).join("")}
      </div>` : "";

    const visualNote = data.visualSpecification && data.visualSpecification.notes ? `
      <div class="lm-section-label">How This Is Visualized</div>
      <p style="font-size:0.85rem;color:var(--ink-soft);">${escapeHtml(data.visualSpecification.notes)}</p>` : "";

    const sourcesHtml = (Array.isArray(data.sources) && data.sources.length) ? `
      <div class="lm-section-label">Sources</div>
      <ul style="margin:0;padding-left:18px;font-size:0.88rem;">
        ${data.sources.map(s => `<li><a href="${escapeHtml(s.url || "#")}" target="_blank" rel="noopener">${escapeHtml(s.label || s.url || "Source")}</a></li>`).join("")}
      </ul>` : "";

    const a11y = data.accessibilityDescription ? `
      <div class="lm-section-label">Accessibility Description</div>
      <p style="font-size:0.85rem;font-style:italic;color:var(--ink-soft);">${escapeHtml(data.accessibilityDescription)}</p>` : "";

    const context = data.optionalContext ? `
      <div class="lm-section-label">Context</div>
      <p style="font-size:0.85rem;color:var(--ink-soft);">${escapeHtml(data.optionalContext)}</p>` : "";

    const typeChip = data.infographicType ? `<span class="tagchip" style="pointer-events:none;">#${escapeHtml(data.infographicType)}</span>` : "";

    return `${typeChip}${factsHtml}${visualNote}${sourcesHtml}${a11y}${context}`;
  }

  /* ---------------- event delegation for card actions ---------------- */
  document.addEventListener("click", (e) => {
    const actBtn = e.target.closest("[data-act]");
    if(actBtn){
      const id = actBtn.dataset.id;
      const act = actBtn.dataset.act;
      if(act === "fav"){ toggleInList("favorites", id); cacheItemData(id); rerenderCurrentView(); }
      else if(act === "save"){ toggleInList("saved", id); cacheItemData(id); rerenderCurrentView(); }
      else if(act === "learnmore"){ openLearnMore(id); }
      else if(act === "share"){ shareCard(id); }
      else if(act === "evidence"){ openLearnMore(id); scrollToEvidence(); }
      else if(act === "shareclass"){ shareCardWithClass(id); }
      return;
    }
    const openCardBtn = e.target.closest("[data-open-card]");
    if(openCardBtn){ openLearnMore(openCardBtn.dataset.openCard); return; }

    const tagBtn = e.target.closest("[data-explore-tag]");
    if(tagBtn){ goToExploreWithTag(tagBtn.dataset.exploreTag); return; }

    const followBtn = e.target.closest("[data-follow]");
    if(followBtn){
      const [kind, ...rest] = followBtn.dataset.follow.split(":");
      const value = rest.join(":");
      const listName = kind === "people" ? "followedPeople" : kind === "places" ? "followedPlaces" : "followedTags";
      toggleInList(listName, value);
      followBtn.textContent = (state[listName].includes(value) ? "✓ " : "+ Follow ") + value;
      followBtn.classList.toggle("is-followed");
      return;
    }

    // clicking a mini card / worldline step / card body opens Learn More
    // (but not when the click was on a button OR a link — e.g. the
    // template's primary "View at Institution" anchor should just navigate).
    const cardEl = e.target.closest("[data-card-id]");
    if(cardEl && !e.target.closest("button") && !e.target.closest("a")){
      openLearnMore(cardEl.dataset.cardId);
    }
  });

  // Evidence is deliberately minimal (brief section 4) — clicking it opens
  // the same Learn More panel and just scrolls to the small evidence line
  // already rendered there, rather than being a separate view.
  function scrollToEvidence(){
    setTimeout(() => {
      const el = document.getElementById("evidence");
      if(el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "start" });
    }, 30);
  }

  function shareCard(id){
    const card = cardsById[id];
    if(!card) return;
    const text = `${card.title} — ${card.hook || ""}`.trim();
    if(navigator.share){
      navigator.share({ title: card.title, text, url: card.sourceUrl || location.href }).catch(()=>{});
    }else if(navigator.clipboard){
      navigator.clipboard.writeText(`${text}\n${card.sourceUrl || ""}`).then(()=>{
        toast("Copied to clipboard");
      }).catch(()=>{ toast(card.sourceUrl || "Nothing to copy"); });
    }
  }

  let toastTimer;
  function toast(msg){
    let el = document.getElementById("fxToast");
    if(!el){
      el = document.createElement("div");
      el.id = "fxToast";
      el.style.cssText = "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#241d14;color:#f3ead9;padding:10px 18px;border-radius:999px;font-size:0.85rem;z-index:200;box-shadow:0 10px 30px rgba(0,0,0,.4);";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = "1";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(()=>{ el.style.opacity = "0"; }, 1800);
  }

  /* ---------------- views / nav ---------------- */
  let currentView = "feed";
  let feedOrder = ACTIVE_CARDS.map(c => c.id); // authored order by default
  let activeSearch = "";

  const navlinks = document.querySelectorAll(".navlink");
  navlinks.forEach(btn => btn.addEventListener("click", () => switchView(btn.dataset.view)));

  function switchView(view){
    const leaving = currentView;
    currentView = view;
    document.querySelectorAll(".view").forEach(v => v.hidden = true);
    navlinks.forEach(b => b.removeAttribute("aria-current"));
    const btn = document.querySelector(`.navlink[data-view="${view}"]`);
    if(btn) btn.setAttribute("aria-current", "page");

    if(view === "feed"){ document.getElementById("viewFeed").hidden = false; renderFeed(); }
    else if(view === "gazette"){ document.getElementById("viewGazette").hidden = false; renderGazette(); }
    else if(view === "explore"){ document.getElementById("viewExplore").hidden = false; renderExplore(); }
    else if(view === "worldlines"){ document.getElementById("viewWorldlines").hidden = false; renderWorldlines(); }
    else if(view === "saved"){ document.getElementById("viewSaved").hidden = false; renderSaved(); }
    else if(view === "timestream"){
      const streamView = document.getElementById("viewTimestream");
      if(streamView){ streamView.hidden = false; enterTimestream(); }
    }
    else if(view === "teacher"){
      const teacherView = document.getElementById("viewTeacher");
      if(teacherView){ teacherView.hidden = false; renderTeacherView(); loadTeacherSessions(); }
    }
    if(leaving === "timestream" && view !== "timestream") stopStreamTimer();
    window.scrollTo({top:0, behavior:"instant" in window ? "instant" : "auto"});
  }

  function rerenderCurrentView(){
    if(currentView === "feed") renderFeed();
    else if(currentView === "gazette") renderGazette();
    else if(currentView === "saved") renderSaved();
    else if(currentView === "timestream") renderTimestream();
    else if(currentView === "teacher") renderTeacherView();
  }

  /* ---------------- FEED ---------------- */
  function renderFeed(){
    const list = document.getElementById("feedList");
    const hint = document.getElementById("feedHint");
    let ids = feedOrder.filter(id => matchesSearch(cardsById[id], activeSearch));

    const hasPersonalization = state.followedTags.length || state.followedPeople.length || state.followedPlaces.length;
    const sourceNote = usingLiveSource ? "Live results from real archives." : "Offline demo cards — live archival discovery unavailable.";
    if(hasPersonalization){
      ids = [...ids].sort((a,b) => personalScore(cardsById[b]) - personalScore(cardsById[a]));
      hint.textContent = `Feed weighted toward what you follow. ${sourceNote}`;
    }else{
      hint.textContent = activeSearch
        ? `Showing results for "${activeSearch}" · ${sourceNote}`
        : (usingLiveSource
          ? `Scroll for more history — or Surprise Me for a fresh batch. ${sourceNote}`
          : `Scroll to fall down a rabbit hole. ${sourceNote}`);
    }

    if(ids.length === 0){
      list.innerHTML = `<p class="empty-note">Nothing matches yet. Try a different search, or clear it.</p>`;
      return;
    }
    list.innerHTML = ids.map(id => renderCard(cardsById[id])).join("");
  }

  function setFeedLoadingMore(on){
    const el = document.getElementById("feedLoadingMore");
    if(el) el.hidden = !on;
  }

  async function loadMoreHistory(){
    if(!usingLiveSource || currentView !== "feed" || activeSearch) return;
    if(loadMoreInFlight) return;
    if(Date.now() < loadMoreCooldownUntil) return;

    loadMoreInFlight = true;
    setFeedLoadingMore(true);
    try{
      const excludeIds = [...sessionSeenIds].join(",");
      const res = await fetch(`/api/feed?limit=${LOAD_MORE_BATCH}&surprise=true&exclude=${encodeURIComponent(excludeIds)}&recentSources=${recentSourcesParam()}&page=${discoveryPage}`);
      if(res.status === 429){
        loadMoreBackoffMs = Math.min(loadMoreBackoffMs * 2, 60000);
        loadMoreCooldownUntil = Date.now() + loadMoreBackoffMs;
        return;
      }
      if(!res.ok){
        loadMoreCooldownUntil = Date.now() + LOAD_MORE_EMPTY_COOLDOWN_MS;
        return;
      }
      loadMoreBackoffMs = 1500;
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      if(items.length === 0){
        loadMoreCooldownUntil = Date.now() + LOAD_MORE_EMPTY_COOLDOWN_MS;
        return;
      }
      discoveryPage += 1; // next batch roams deeper
      noteRecentSources(items);
      const cards = items.map(normalizedItemToCard);
      const added = appendFeedCards(cards);
      if(added.length === 0){
        loadMoreCooldownUntil = Date.now() + LOAD_MORE_EMPTY_COOLDOWN_MS;
        return;
      }
      const hasPersonalization = state.followedTags.length || state.followedPeople.length || state.followedPlaces.length;
      if(hasPersonalization){
        renderFeed();
      }else{
        const list = document.getElementById("feedList");
        if(list){
          list.insertAdjacentHTML("beforeend", added.map(c => renderCard(c)).join(""));
        }else{
          renderFeed();
        }
      }
    }catch(e){
      // Network blip — keep existing cards usable; brief cooldown before retry.
      loadMoreCooldownUntil = Date.now() + LOAD_MORE_EMPTY_COOLDOWN_MS;
    }finally{
      loadMoreInFlight = false;
      setFeedLoadingMore(false);
    }
  }

  function maybeLoadMoreFromScroll(){
    if(!usingLiveSource || currentView !== "feed" || activeSearch) return;
    if(loadMoreInFlight || Date.now() < loadMoreCooldownUntil) return;
    const doc = document.documentElement;
    const remaining = doc.scrollHeight - (window.scrollY + window.innerHeight);
    if(remaining <= LOAD_MORE_NEAR_PX) loadMoreHistory();
  }

  /* ---------------- TimeStream ---------------- */
  function readStreamInterval(){
    try{
      const n = parseInt(sessionStorage.getItem("fx_timestream_interval") || "", 10);
      if(STREAM_INTERVALS.indexOf(n) !== -1) return n;
    }catch(e){}
    return STREAM_DEFAULT_MS;
  }
  streamIntervalMs = readStreamInterval();

  function currentStreamCard(){
    return streamQueue[streamIndex] || null;
  }

  function absorbStreamCards(cards){
    if(!cards || !cards.length) return 0;
    let picked = window.FXLib.filterStreamCards(cards, streamSeenIds, { visualOnly: true });
    if(picked.length < 3){
      const extra = window.FXLib.filterStreamCards(cards, streamSeenIds, { visualOnly: false, includeFallback: true });
      const have = new Set(picked.map(c => c.id));
      extra.forEach(c => {
        if(c && c.id && !have.has(c.id)){
          picked.push(c);
          have.add(c.id);
        }
      });
    }
    let added = 0;
    picked.forEach(card => {
      if(!card || !card.id || streamSeenIds.has(card.id)) return;
      streamSeenIds.add(card.id);
      streamQueue.push(card);
      cardsById[card.id] = card;
      added += 1;
    });
    return added;
  }

  async function fetchDiscoveryCards(limit){
    const excludeIds = [...sessionSeenIds].join(",");
    const res = await fetch(`/api/feed?limit=${limit || LOAD_MORE_BATCH}&surprise=true&exclude=${encodeURIComponent(excludeIds)}&recentSources=${recentSourcesParam()}&page=${discoveryPage}`);
    if(res.status === 429 || !res.ok) return [];
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    if(!items.length) return [];
    discoveryPage += 1; // next stream prefetch roams deeper
    noteRecentSources(items);
    const cards = items.map(normalizedItemToCard);
    rememberSeenIds(cards.map(c => c.id));
    cards.forEach(c => { if(c && c.id) cardsById[c.id] = c; });
    return cards;
  }

  async function ensureStreamCards(){
    if(streamFetchInFlight) return;
    if(!usingLiveSource) return;
    streamFetchInFlight = true;
    try{
      const cards = await fetchDiscoveryCards(LOAD_MORE_BATCH);
      absorbStreamCards(cards);
      if(currentView === "timestream") renderTimestream();
    }catch(e){
      /* keep playing whatever is already queued */
    }finally{
      streamFetchInFlight = false;
    }
  }

  function stopStreamTimer(){
    if(streamTimer){
      clearInterval(streamTimer);
      streamTimer = null;
    }
    paintStreamChrome();
  }

  function startStreamTimer(){
    stopStreamTimer();
    if(!streamAutoplay) return;
    if(currentView !== "timestream") return;
    if(lmOverlay && !lmOverlay.hidden) return;
    streamTimer = setInterval(() => streamGo(1, { fromTimer: true }), streamIntervalMs);
    paintStreamChrome();
  }

  function restartStreamTimerIfPlaying(){
    if(streamAutoplay) startStreamTimer();
    else paintStreamChrome();
  }

  function toggleStreamPlayback(){
    streamAutoplay = !streamAutoplay;
    if(streamAutoplay) startStreamTimer();
    else stopStreamTimer();
  }

  function streamGo(direction, opts){
    opts = opts || {};
    if(!streamQueue.length){
      ensureStreamCards();
      renderTimestream();
      return;
    }
    const next = window.FXLib.nextPlayableStreamIndex(streamQueue, streamIndex, direction, streamBrokenIds);
    if(next >= 0) streamIndex = next;
    else if(direction > 0) ensureStreamCards();
    renderTimestream();
    if(window.FXLib.streamShouldPrefetch(streamIndex, streamQueue.length, STREAM_PREFETCH_REMAINING)){
      ensureStreamCards();
    }
    if(!opts.fromTimer) restartStreamTimerIfPlaying();
  }

  function paintStreamChrome(){
    const status = document.getElementById("streamStatus");
    const playBtn = document.getElementById("streamPlayBtn");
    const playing = !!(streamAutoplay && streamTimer);
    if(status) status.textContent = playing ? "Playing" : "Paused";
    if(playBtn){
      playBtn.textContent = streamAutoplay ? "Pause" : "Resume";
      playBtn.setAttribute("aria-pressed", streamAutoplay ? "false" : "true");
      playBtn.setAttribute("aria-label", streamAutoplay ? "Pause" : "Resume");
    }
    const shareBtn = document.getElementById("streamShareBtn");
    if(shareBtn) shareBtn.hidden = !(broadcastState.enabled && broadcastState.isHost);
  }

  function renderTimestream(){
    const titleEl = document.getElementById("streamTitle");
    if(!titleEl) return;
    const card = currentStreamCard();
    const media = document.getElementById("streamMedia");
    const hookEl = document.getElementById("streamHook");
    const metaEl = document.getElementById("streamMeta");
    const saveBtn = document.getElementById("streamSaveBtn");
    const sourceLink = document.getElementById("streamSourceLink");
    const shareBtn = document.getElementById("streamShareBtn");
    if(!card){
      titleEl.textContent = "Finding history…";
      if(hookEl) hookEl.textContent = usingLiveSource
        ? "TimeStream is requesting another batch from the archives."
        : "No history cards are loaded yet.";
      if(metaEl) metaEl.textContent = "";
      if(media) media.innerHTML = "";
      ensureStreamCards();
      paintStreamChrome();
      return;
    }
    titleEl.textContent = card.title || "";
    if(hookEl) hookEl.textContent = card.hook || "";
    if(metaEl){
      metaEl.textContent = [card.date || card.era, card.location, card.sourceName || card.institution]
        .filter(Boolean).join(" · ");
    }
    if(media){
      if(streamBrokenIds.has(card.id) || !window.FXLib.cardHasUsableVisual(card)){
        media.innerHTML = placeholderMedia(card);
      }else{
        const candidates = window.FXLib.imageCandidates(card);
        const rest = candidates.slice(1);
        media.innerHTML = `<img src="${escapeHtml(candidates[0])}" alt="${escapeHtml(card.title || "")}"
          data-remaining='${escapeHtml(JSON.stringify(rest))}'
          data-stream-id="${escapeHtml(card.id)}"
          onerror="window.__fxStreamImgError(this)">`;
      }
    }
    if(saveBtn){
      const saved = state.saved.includes(card.id);
      saveBtn.textContent = saved ? "Saved" : "Save";
      saveBtn.setAttribute("aria-pressed", saved ? "true" : "false");
    }
    if(sourceLink){
      if(card.sourceUrl){
        sourceLink.href = card.sourceUrl;
        sourceLink.hidden = false;
      }else{
        sourceLink.removeAttribute("href");
        sourceLink.hidden = true;
      }
    }
    if(shareBtn) shareBtn.hidden = !(broadcastState.enabled && broadcastState.isHost);
    heartbeatSession(card.id);
    paintStreamChrome();
  }

  window.__fxStreamImgError = function(imgEl){
    let remaining = [];
    try{ remaining = JSON.parse(imgEl.getAttribute("data-remaining") || "[]"); }catch(e){ remaining = []; }
    if(remaining.length > 0){
      imgEl.setAttribute("data-remaining", JSON.stringify(remaining.slice(1)));
      imgEl.src = remaining[0];
      return;
    }
    const id = imgEl.getAttribute("data-stream-id");
    if(id) streamBrokenIds.add(id);
    const nextIdx = window.FXLib.nextPlayableStreamIndex(streamQueue, streamIndex, 1, streamBrokenIds);
    if(nextIdx >= 0) streamIndex = nextIdx;
    renderTimestream();
    if(window.FXLib.streamShouldPrefetch(streamIndex, streamQueue.length, STREAM_PREFETCH_REMAINING)){
      ensureStreamCards();
    }
  };

  function enterTimestream(){
    if(!streamSeeded){
      streamIntervalMs = readStreamInterval();
      const sel = document.getElementById("streamIntervalSelect");
      if(sel) sel.value = String(streamIntervalMs);
      absorbStreamCards(ACTIVE_CARDS);
      if(!streamQueue.length) absorbStreamCards(DEMO_CARDS);
      streamSeeded = true;
    }
    renderTimestream();
    if(streamAutoplay) startStreamTimer();
    if(window.FXLib.streamShouldPrefetch(streamIndex, streamQueue.length, STREAM_PREFETCH_REMAINING)){
      ensureStreamCards();
    }
  }

  const streamPrevBtn = document.getElementById("streamPrevBtn");
  if(streamPrevBtn) streamPrevBtn.addEventListener("click", () => streamGo(-1));
  const streamNextBtn = document.getElementById("streamNextBtn");
  if(streamNextBtn) streamNextBtn.addEventListener("click", () => streamGo(1));
  const streamPlayBtn = document.getElementById("streamPlayBtn");
  if(streamPlayBtn) streamPlayBtn.addEventListener("click", toggleStreamPlayback);
  const streamSaveBtn = document.getElementById("streamSaveBtn");
  if(streamSaveBtn){
    streamSaveBtn.addEventListener("click", () => {
      const card = currentStreamCard();
      if(!card) return;
      toggleInList("saved", card.id);
      cacheItemData(card.id);
      renderTimestream();
      toast(state.saved.includes(card.id) ? "Saved" : "Removed from Saved");
    });
  }
  const streamLearnBtn = document.getElementById("streamLearnBtn");
  if(streamLearnBtn){
    streamLearnBtn.addEventListener("click", () => {
      const card = currentStreamCard();
      if(card) openLearnMore(card.id);
    });
  }
  const streamShareBtn = document.getElementById("streamShareBtn");
  if(streamShareBtn){
    streamShareBtn.addEventListener("click", () => {
      const card = currentStreamCard();
      if(card) shareCardWithClass(card.id);
    });
  }
  const streamIntervalSelect = document.getElementById("streamIntervalSelect");
  if(streamIntervalSelect){
    streamIntervalSelect.value = String(streamIntervalMs);
    streamIntervalSelect.addEventListener("change", () => {
      const n = parseInt(streamIntervalSelect.value, 10);
      if(STREAM_INTERVALS.indexOf(n) === -1) return;
      streamIntervalMs = n;
      try{ sessionStorage.setItem("fx_timestream_interval", String(n)); }catch(e){}
      restartStreamTimerIfPlaying();
    });
  }

  document.addEventListener("keydown", (e) => {
    if(currentView !== "timestream") return;
    const target = e.target;
    const tag = (target && target.tagName) || "";
    if(tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;
    if(e.key === "ArrowLeft"){
      e.preventDefault();
      streamGo(-1);
    }else if(e.key === "ArrowRight"){
      e.preventDefault();
      streamGo(1);
    }else if(e.key === " " || e.code === "Space"){
      if(tag === "BUTTON" || tag === "A") return;
      e.preventDefault();
      toggleStreamPlayback();
    }
  });

  let scrollLoadTimer = null;
  window.addEventListener("scroll", () => {
    if(scrollLoadTimer) return;
    scrollLoadTimer = setTimeout(() => {
      scrollLoadTimer = null;
      maybeLoadMoreFromScroll();
    }, 120);
  }, { passive: true });

  // "Surprise Me" fetches a fresh, bounded batch of real archival results
  // from the local service via general discovery (beyond the 15 featured
  // seeds) — it does NOT just reshuffle what's already loaded. It excludes
  // what's currently visible so repeat clicks don't just hand back the
  // same cards, and gracefully falls back to a local reshuffle if the
  // service/sources aren't reachable (offline demo mode, or a live
  // request that fails).
  const shuffleBtn = document.getElementById("shuffleBtn");
  shuffleBtn.addEventListener("click", async () => {
    if(usingLiveSource){
      const originalLabel = shuffleBtn.textContent;
      shuffleBtn.disabled = true;
      shuffleBtn.textContent = "🔀 Fetching fresh history…";
      try{
        const excludeIds = [...sessionSeenIds].join(",");
        const res = await fetch(`/api/feed?limit=20&surprise=true&exclude=${encodeURIComponent(excludeIds)}&recentSources=${recentSourcesParam()}&page=${discoveryPage}`);
        if(res.status === 429){
          toast("Archives asked us to slow down — try again shortly");
          shuffleBtn.disabled = false;
          shuffleBtn.textContent = originalLabel;
          return;
        }
        if(res.ok){
          const data = await res.json();
          const items = Array.isArray(data.items) ? data.items : [];
          if(items.length > 0){
            noteRecentSources(items);
            discoveryPage += 1; // next Surprise Me roams deeper
            const cards = items.map(normalizedItemToCard);
            setActiveCards(cards);
            baseCards = cards;
            loadMoreCooldownUntil = 0;
            renderFeed();
            window.scrollTo({ top: 0, behavior: "smooth" });
            toast(`Surprised you with ${cards.length} archival find(s)`);
            shuffleBtn.disabled = false;
            shuffleBtn.textContent = originalLabel;
            return;
          }
        }
        toast("Archives had nothing new right now — reshuffled what's loaded");
      }catch(e){
        toast("Couldn't reach the archives — reshuffled what's loaded");
      }
      shuffleBtn.disabled = false;
      shuffleBtn.textContent = originalLabel;
    }
    // Offline/demo mode, or a live fetch that failed: fall back to
    // reordering the cards already on hand rather than doing nothing.
    feedOrder = shuffle([...feedOrder]);
    renderFeed();
    if(!usingLiveSource) toast("Shuffled — enjoy the rabbit hole");
  });
  function shuffle(arr){
    for(let i = arr.length -1; i>0; i--){
      const j = Math.floor(Math.random()*(i+1));
      [arr[i],arr[j]] = [arr[j],arr[i]];
    }
    return arr;
  }

  const stopServerBtn = document.getElementById("stopServerBtn");
  if(stopServerBtn){
    stopServerBtn.addEventListener("click", async () => {
      if(!confirm("Stop the local Time Machine server? The page will stay open, but live archive requests will stop until you start serve.py again.")){
        return;
      }
      try{
        const res = await fetch("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if(res.ok){
          usingLiveSource = false;
          stopServerBtn.disabled = true;
          toast("Time Machine server stopped");
        }else{
          toast("Could not stop the server from here");
        }
      }catch(e){
        usingLiveSource = false;
        stopServerBtn.disabled = true;
        toast("Server already unreachable (likely stopped)");
      }
    });
  }

  const SESSION_STORE_KEY = "foxai_timemachine_session";
  let sessionId = null;
  try{ sessionId = sessionStorage.getItem(SESSION_STORE_KEY); }catch(e){ sessionId = null; }

  function recentCardsForHeartbeat(){
    return (state.recent || []).slice(0, 8).map(id => {
      const card = lookupCard(id);
      return { id: id, title: card && card.title ? card.title : "" };
    });
  }

  function heartbeatSession(currentCardId){
    const cardId = currentCardId || (state.recent && state.recent[0]) || null;
    const card = cardId ? lookupCard(cardId) : null;
    const body = {
      sessionId: sessionId || undefined,
      currentCardId: cardId,
      currentCardTitle: card ? card.title : "",
      savedCount: (state.saved || []).length,
      recentCards: recentCardsForHeartbeat()
    };
    fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(res => res.ok ? res.json() : null).then(data => {
      if(data && data.sessionId){
        sessionId = data.sessionId;
        try{ sessionStorage.setItem(SESSION_STORE_KEY, sessionId); }catch(err){ /* private mode */ }
      }
      if(data && data.sharedCard !== undefined && !broadcastState.isHost){
        broadcastState.sharedCard = data.sharedCard;
        paintStudentClassroomBar();
      }
    }).catch(() => {});
  }

  function paintTeacherNav(){
    const nav = document.getElementById("teacherNav");
    if(!nav) return;
    const show = !!(broadcastState.enabled && broadcastState.isHost);
    nav.hidden = !show;
    if(!show && currentView === "teacher") switchView("feed");
  }

  function paintStudentClassroomBar(){
    const bar = document.getElementById("studentClassroomBar");
    const sharedLine = document.getElementById("sharedClassLine");
    const titleEl = document.getElementById("sharedClassTitle");
    if(!bar) return;
    const show = !!(broadcastState.enabled && broadcastState.isHost === false);
    bar.hidden = !show;
    const shared = broadcastState.sharedCard;
    if(sharedLine){
      const has = !!(show && shared && shared.id);
      sharedLine.hidden = !has;
      if(has && titleEl) titleEl.textContent = shared.title || "a card";
    }
  }

  function paintBroadcastBanner(){
    const banner = document.getElementById("broadcastBanner");
    const btn = document.getElementById("broadcastBtn");
    const join = document.getElementById("broadcastJoinLine");
    const count = document.getElementById("broadcastCountLine");
    if(btn){
      btn.classList.toggle("is-broadcasting", !!broadcastState.enabled);
      btn.textContent = broadcastState.enabled ? "Broadcasting" : "Broadcast";
      btn.hidden = broadcastState.isHost === false;
    }
    if(banner){
      const show = !!(broadcastState.enabled && broadcastState.isHost);
      banner.hidden = !show;
      if(join){
        join.textContent = broadcastState.joinUrl
          ? ("Students: open " + broadcastState.joinUrl)
          : "Students: open this computer's address on port 8420";
      }
      if(count){
        const n = broadcastState.connected;
        count.textContent = (typeof n === "number")
          ? (n + " classroom device" + (n === 1 ? "" : "s") + " connected")
          : "";
      }
    }
    paintTeacherNav();
    paintStudentClassroomBar();
    paintStreamChrome();
  }

  function applyBroadcastPayload(data, assumeHost){
    const wasShare = !!(broadcastState.enabled && broadcastState.isHost);
    broadcastState = {
      enabled: !!data.enabled,
      isHost: assumeHost ? true : !!data.isHost,
      joinUrl: data.joinUrl || (data.joinUrls && data.joinUrls[0]) || null,
      connected: data.connected,
      sharedCard: data.sharedCard || null
    };
    const nowShare = !!(broadcastState.enabled && broadcastState.isHost);
    paintBroadcastBanner();
    if(currentView === "teacher") renderTeacherView();
    if(nowShare !== wasShare && currentView === "feed"){
      renderFeed();
    }
  }

  async function refreshBroadcastStatus(){
    try{
      const res = await fetch("/api/broadcast");
      if(!res.ok) throw new Error("broadcast " + res.status);
      applyBroadcastPayload(await res.json());
    }catch(e){
      paintBroadcastBanner();
    }
  }

  async function setBroadcastEnabled(next){
    const res = await fetch("/api/broadcast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: next })
    });
    if(res.status === 403){
      toast("Broadcast can only be turned on from the host computer");
      return false;
    }
    if(!res.ok) throw new Error("broadcast toggle failed");
    applyBroadcastPayload(await res.json(), true);
    toast(broadcastState.enabled ? "Broadcasting on this Wi-Fi" : "Broadcast off — localhost only");
    setTimeout(refreshBroadcastStatus, 800);
    if(broadcastState.enabled) setTimeout(loadTeacherSessions, 900);
    return true;
  }

  async function shareCardWithClass(cardId){
    const card = lookupCard(cardId);
    if(!card) return;
    try{
      const res = await fetch("/api/class-card", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cardId: card.id,
          title: card.title || "",
          card: structuredCloneSafe(card)
        })
      });
      if(res.status === 403){
        toast("Only the host can share a class card");
        return;
      }
      if(!res.ok) throw new Error("class-card failed");
      const data = await res.json();
      broadcastState.sharedCard = data.sharedCard || { id: card.id, title: card.title };
      paintBroadcastBanner();
      if(currentView === "teacher") renderTeacherView();
      toast("Shared with the class");
    }catch(e){
      toast("Could not share that card");
    }
  }

  function openSharedClassCard(){
    const shared = broadcastState.sharedCard;
    if(!shared) return;
    const installed = window.FXLib.installSharedClassCard(shared, cardsById);
    if(installed && installed.id){
      openLearnMore(installed.id);
      return;
    }
    if(!shared.id) return;
    if(!lookupCard(shared.id)){
      cardsById[shared.id] = {
        id: shared.id,
        title: shared.title || "Shared card",
        hook: "Shared by the teacher for the class.",
        longerContext: "Shared by the teacher for the class.",
        date: "",
        location: "",
        type: "document",
        image: null,
        sourceUrl: null,
        sourceName: "Classroom Broadcast",
        institution: "Classroom Broadcast",
        tags: [],
        relatedPeople: [],
        relatedPlaces: [],
        relatedCardIds: [],
        evidence: null,
        reconstruction: false,
        indigenous: false
      };
    }
    openLearnMore(shared.id);
  }

  function renderTeacherView(){
    const list = document.getElementById("teacherSessionList");
    const empty = document.getElementById("teacherEmpty");
    const joinHint = document.getElementById("teacherJoinHint");
    const sharedHint = document.getElementById("teacherSharedHint");
    if(joinHint){
      joinHint.textContent = broadcastState.joinUrl
        ? ("Students: open " + broadcastState.joinUrl)
        : "";
    }
    if(sharedHint){
      sharedHint.textContent = broadcastState.sharedCard
        ? ("Class card: " + (broadcastState.sharedCard.title || broadcastState.sharedCard.id))
        : "No class card shared yet — use “Share with class” on a TimeFeed or TimeStream card.";
    }
    if(!list) return;
    const sessions = renderTeacherView._sessions || [];
    if(!sessions.length){
      list.innerHTML = "";
      if(empty) empty.hidden = false;
      return;
    }
    if(empty) empty.hidden = true;
    list.innerHTML = sessions.map(row => {
      const presence = row.presence === "online" ? "Online" : "Recently seen";
      const current = row.currentCardTitle || row.currentCardId || "—";
      const recents = (row.recentCards || []).slice(0, 8);
      const recentHtml = recents.length
        ? `<ul>${recents.map(c => `<li>${escapeHtml(c.title || c.id)}</li>`).join("")}</ul>`
        : "<p>No recently viewed cards yet.</p>";
      return `<article class="teacher-session">
        <h3>${escapeHtml(row.device || "Browser")}</h3>
        <p class="teacher-session__meta">${presence} · ${Number(row.savedCount || 0)} saved</p>
        <p>Now: ${escapeHtml(current)}</p>
        ${recentHtml}
      </article>`;
    }).join("");
  }

  async function loadTeacherSessions(){
    if(!(broadcastState.enabled && broadcastState.isHost)) return;
    try{
      const res = await fetch("/api/sessions");
      if(res.status === 403) return;
      if(!res.ok) throw new Error("sessions " + res.status);
      const data = await res.json();
      if(typeof data.connected === "number") broadcastState.connected = data.connected;
      if(data.joinUrl) broadcastState.joinUrl = data.joinUrl;
      if(data.sharedCard !== undefined) broadcastState.sharedCard = data.sharedCard;
      renderTeacherView._sessions = Array.isArray(data.sessions) ? data.sessions : [];
      paintBroadcastBanner();
      if(currentView === "teacher") renderTeacherView();
    }catch(e){ /* keep last snapshot */ }
  }

  const broadcastBtn = document.getElementById("broadcastBtn");
  if(broadcastBtn){
    broadcastBtn.addEventListener("click", async () => {
      try{
        await setBroadcastEnabled(!broadcastState.enabled);
      }catch(e){
        toast("Could not change Broadcast");
      }
    });
  }

  const teacherRefreshBtn = document.getElementById("teacherRefreshBtn");
  if(teacherRefreshBtn){
    teacherRefreshBtn.addEventListener("click", () => loadTeacherSessions());
  }
  const teacherCopyJoinBtn = document.getElementById("teacherCopyJoinBtn");
  if(teacherCopyJoinBtn){
    teacherCopyJoinBtn.addEventListener("click", async () => {
      const url = broadcastState.joinUrl;
      if(!url){ toast("No join address yet"); return; }
      try{
        if(navigator.clipboard && navigator.clipboard.writeText){
          await navigator.clipboard.writeText(url);
        }else{
          throw new Error("no clipboard");
        }
        toast("Copied " + url);
      }catch(e){
        toast(url);
      }
    });
  }
  const teacherEndBroadcastBtn = document.getElementById("teacherEndBroadcastBtn");
  if(teacherEndBroadcastBtn){
    teacherEndBroadcastBtn.addEventListener("click", async () => {
      try{
        await setBroadcastEnabled(false);
      }catch(e){
        toast("Could not end Broadcast");
      }
    });
  }
  const openSharedClassBtn = document.getElementById("openSharedClassBtn");
  if(openSharedClassBtn){
    openSharedClassBtn.addEventListener("click", openSharedClassCard);
  }

  const searchInput = document.getElementById("searchInput");
  let searchTimer;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => performSearch(searchInput.value.trim()), 200);
  });

  // Shared by the search box and by clicking a topic tag on a card.
  // Offline demo mode filters the bundled cards locally; live mode asks
  // the local Time Machine service to query real sources (brief section 20).
  async function performSearch(query){
    if(currentView !== "feed") switchView("feed");
    if(!query){
      activeSearch = "";
      setActiveCards(baseCards);
      renderFeed();
      return;
    }
    if(usingLiveSource){
      activeSearch = ""; // results already come back query-matched from the service
      try{
        const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
        if(res.ok){
          const data = await res.json();
          const cards = (Array.isArray(data.items) ? data.items : []).map(normalizedItemToCard);
          setActiveCards(cards);
        }
      }catch(e){ /* leave whatever was already showing rather than erroring out */ }
      renderFeed();
    }else{
      activeSearch = query;
      renderFeed();
    }
  }

  function goToExploreWithTag(tag){
    searchInput.value = tag;
    performSearch(tag);
  }

  /* ---------------- GAZETTE ---------------- */
  function gazetteStoryToCard(story){
    return {
      id: story.id,
      title: story.title,
      hook: story.hook || "",
      longerContext: story.hook || "",
      date: story.dateLabel || "",
      location: story.location || "",
      type: story.mediaKind || "artwork",
      mediaTemplate: story.mediaTemplate || window.FXLib.mediaKindToTemplate(story.mediaKind || "artwork"),
      image: story.imageUrl || null,
      thumbnailUrl: story.thumbnailUrl || null,
      sourceUrl: story.sourceUrl,
      sourceName: story.sourceInstitution,
      institution: story.sourceInstitution,
      tags: story.topics || [],
      relatedPeople: story.people || [],
      relatedPlaces: story.location ? [story.location] : [],
      relatedCardIds: [],
      evidence: story.evidence || null,
      reconstruction: false,
      indigenous: false
    };
  }

  function rememberGazetteStory(story){
    if(!story || !story.id) return;
    cardsById[story.id] = gazetteStoryToCard(story);
  }

  function authoredGazetteCards(){
    return DEMO_CARDS.filter(c => {
      const templateId = c.mediaTemplate || window.FXLib.mediaKindToTemplate(c.type);
      return c.type === "gazette" || !window.FXLib.templateInfo(templateId).external;
    });
  }

  function storyMeta(story){
    return [story.dateLabel, story.location].filter(Boolean).join(" · ");
  }

  function renderStoryTile(story, headingTag){
    rememberGazetteStory(story);
    const img = story.imageUrl || story.thumbnailUrl;
    const heading = headingTag === "h2"
      ? `<h2>${escapeHtml(story.title)}</h2>`
      : headingTag === "h4"
        ? `<h4>${escapeHtml(story.title)}</h4>`
        : `<h3>${escapeHtml(story.title)}</h3>`;
    const imgHtml = img ? `<img src="${escapeHtml(img)}" alt="${escapeHtml(story.title)}" loading="lazy">` : "";
    return `<article class="broadsheet__story" data-card-id="${escapeHtml(story.id)}">
      ${imgHtml}
      <p class="broadsheet__meta">${escapeHtml(storyMeta(story))}</p>
      ${heading}
      ${story.hook ? `<p class="broadsheet__hook">${escapeHtml(story.hook)}</p>` : ""}
      ${story.sourceUrl ? `<p class="broadsheet__source"><a href="${escapeHtml(story.sourceUrl)}" target="_blank" rel="noopener">${escapeHtml(story.sourceInstitution || "View source")} ↗</a></p>` : ""}
    </article>`;
  }

  function defaultBroadsheetHtml(note){
    return `<div class="broadsheet broadsheet--empty">
      <div class="broadsheet__mast">The Time Machine Gazette</div>
      <p class="broadsheet__kicker">Archival front page</p>
      <p class="broadsheet__note">${escapeHtml(note || "Setting the type from the archives…")}</p>
    </div>`;
  }

  function renderBroadsheet(data){
    if(!data || !data.available || !data.lead){
      return defaultBroadsheetHtml(data && data.note ? data.note : "Live archives did not return illustrated stories just now.");
    }
    rememberGazetteStory(data.lead);
    (data.secondary || []).forEach(rememberGazetteStory);
    (data.archives || []).forEach(rememberGazetteStory);
    const lead = data.lead;
    const leadImg = lead.imageUrl || lead.thumbnailUrl;
    const secondary = (data.secondary || []).map(s => renderStoryTile(s, "h3")).join("");
    const archives = (data.archives || []).map(s => renderStoryTile(s, "h4")).join("");
    return `<div class="broadsheet">
      <div class="broadsheet__mast">The Time Machine Gazette</div>
      <p class="broadsheet__kicker">From the archives · not invented copy</p>
      <article class="broadsheet__lead" data-card-id="${escapeHtml(lead.id)}">
        ${leadImg ? `<img src="${escapeHtml(leadImg)}" alt="${escapeHtml(lead.title)}">` : ""}
        <p class="broadsheet__meta">${escapeHtml(storyMeta(lead))}</p>
        <h2>${escapeHtml(lead.title)}</h2>
        ${lead.hook ? `<p class="broadsheet__hook">${escapeHtml(lead.hook)}</p>` : ""}
        ${lead.sourceUrl ? `<p class="broadsheet__source"><a href="${escapeHtml(lead.sourceUrl)}" target="_blank" rel="noopener">${escapeHtml(lead.sourceInstitution || "View source")} ↗</a></p>` : ""}
      </article>
      ${secondary ? `<div class="broadsheet__grid">${secondary}</div>` : ""}
      ${archives ? `<section class="broadsheet__archives"><h2>From the archives</h2><div class="broadsheet__archive-row">${archives}</div></section>` : ""}
    </div>`;
  }

  function paintGazette(frontHtml){
    const list = document.getElementById("gazetteList");
    const authored = authoredGazetteCards();
    authored.forEach(c => { cardsById[c.id] = c; });
    const authoredHtml = authored.length
      ? `<section class="gazette-authored"><h2 class="gazette-section-label">Time Machine reconstructions &amp; infographics</h2>${authored.map(c => renderCard(c)).join("")}</section>`
      : "";
    const front = frontHtml || defaultBroadsheetHtml();
    list.innerHTML = front + authoredHtml;
    if(!authored.length && !(frontHtml && frontHtml.indexOf("broadsheet__lead") !== -1)){
      // Keep the newspaper shell even when the authored set is empty.
    }
  }

  async function loadGazetteFrontPage(){
    try{
      const res = await fetch("/api/gazette");
      if(!res.ok) throw new Error("gazette " + res.status);
      return renderBroadsheet(await res.json());
    }catch(e){
      return defaultBroadsheetHtml("Live archives unavailable — reconstructions below still work.");
    }
  }

  function renderGazette(){
    const list = document.getElementById("gazetteList");
    paintGazette(list.dataset.frontHtml || defaultBroadsheetHtml());
    loadGazetteFrontPage().then(html => {
      if(currentView !== "gazette") return;
      list.dataset.frontHtml = html;
      paintGazette(html);
    });
  }

  /* ---------------- EXPLORE ---------------- */
  function renderExplore(){
    const tagCounts = {}, peopleCounts = {}, placeCounts = {};
    ACTIVE_CARDS.forEach(c => {
      (c.tags||[]).forEach(t => tagCounts[t] = (tagCounts[t]||0)+1);
      (c.relatedPeople||[]).forEach(p => peopleCounts[p] = (peopleCounts[p]||0)+1);
      (c.relatedPlaces||[]).forEach(p => placeCounts[p] = (placeCounts[p]||0)+1);
    });
    renderChipGroup("exploreTopics", tagCounts, "tags", state.followedTags);
    renderChipGroup("explorePeople", peopleCounts, "people", state.followedPeople);
    renderChipGroup("explorePlaces", placeCounts, "places", state.followedPlaces);
  }
  function renderChipGroup(elId, counts, kind, followedList){
    const el = document.getElementById(elId);
    const entries = Object.entries(counts).sort((a,b)=>b[1]-a[1]);
    if(entries.length === 0){ el.innerHTML = `<p class="empty-note">Nothing here yet.</p>`; return; }
    const listName = kind === "people" ? "followedPeople" : kind === "places" ? "followedPlaces" : "followedTags";
    el.innerHTML = entries.map(([name,count]) => {
      const followed = followedList.includes(name);
      return `<button class="chip ${followed?"is-followed":""}" data-follow="${kind}:${escapeHtml(name)}">${followed?"✓ ":""}${escapeHtml(name)} <span style="opacity:.6">(${count})</span></button>`;
    }).join("");
  }

  /* ---------------- WORLDLINES ---------------- */
  function renderWorldlines(){
    const list = document.getElementById("worldlineList");
    const detail = document.getElementById("worldlineDetail");
    detail.hidden = true; detail.innerHTML = "";
    if(WORLDLINES.length === 0){
      list.innerHTML = `<p class="empty-note">No Worldlines yet.</p>`;
      return;
    }
    list.hidden = false;
    list.innerHTML = WORLDLINES.map(w => `
      <div class="worldline-card" data-worldline="${escapeHtml(w.id)}">
        <h3>${escapeHtml(w.title)}</h3>
        <p>${escapeHtml(w.description || "")}</p>
        <span class="worldline-card__count">${w.cardIds.length} stops</span>
      </div>`).join("");
    list.querySelectorAll("[data-worldline]").forEach(el => {
      el.addEventListener("click", () => openWorldline(el.dataset.worldline));
    });
  }
  function openWorldline(id){
    const w = WORLDLINES.find(x => x.id === id);
    if(!w) return;
    document.getElementById("worldlineList").hidden = true;
    const detail = document.getElementById("worldlineDetail");
    detail.hidden = false;
    const steps = w.cardIds.filter(cid => cardsById[cid]).map((cid, i, arr) => `
      <div class="worldline-step">
        <div class="worldline-step__rail">
          <div class="worldline-step__dot"></div>
          ${i < arr.length-1 ? `<div class="worldline-step__line"></div>` : ""}
        </div>
        <div class="worldline-step__card">${renderCard(cardsById[cid])}</div>
      </div>`).join("");
    detail.innerHTML = `
      <button class="worldline-detail__back" id="worldlineBack">← All Worldlines</button>
      <h2 style="font-family:var(--font-display);color:var(--brass-bright);">${escapeHtml(w.title)}</h2>
      <p style="color:#a8a2c0;max-width:56ch;">${escapeHtml(w.description||"")}</p>
      <div class="worldline-path" style="margin-top:18px;">${steps}</div>
    `;
    document.getElementById("worldlineBack").addEventListener("click", renderWorldlines);
  }

  /* ---------------- SAVED / LIBRARY ---------------- */
  function renderSaved(){
    fillMiniList("savedFavorites", "favoritesEmpty", state.favorites);
    fillMiniList("savedItems", "savedEmpty", state.saved);
    fillMiniList("recentList", "recentEmpty", state.recent);

    const followEl = document.getElementById("followingList");
    const followEmpty = document.getElementById("followingEmpty");
    const chips = [
      ...state.followedTags.map(t => ({kind:"tags", name:t})),
      ...state.followedPeople.map(t => ({kind:"people", name:t})),
      ...state.followedPlaces.map(t => ({kind:"places", name:t})),
    ];
    if(chips.length === 0){
      followEl.innerHTML = ""; followEmpty.style.display = "block";
    }else{
      followEmpty.style.display = "none";
      followEl.innerHTML = chips.map(c => `<button class="chip is-followed" data-follow="${c.kind}:${escapeHtml(c.name)}">✓ ${escapeHtml(c.name)}</button>`).join("");
    }
  }
  function fillMiniList(elId, emptyId, ids){
    const el = document.getElementById(elId);
    const empty = document.getElementById(emptyId);
    const valid = ids.filter(id => lookupCard(id));
    if(valid.length === 0){ el.innerHTML = ""; empty.style.display = "block"; return; }
    empty.style.display = "none";
    el.innerHTML = valid.map(id => renderMiniCard(lookupCard(id))).join("");
  }

  document.getElementById("resetDataBtn").addEventListener("click", () => {
    if(confirm("This clears your favorites, saved items, follows, and recently viewed on this device. Continue?")){
      state = structuredCloneSafe(defaultState);
      saveState();
      renderSaved();
      toast("Time Machine data reset");
    }
  });

  /* ---------------- boot ---------------- */
  async function boot(){
    // Try the local Time Machine service first (real online sources).
    // If it's not running or has nothing live right now, fall back to the
    // bundled offline demo cards — Time Machine stays fully usable either
    // way, it just says so (brief section 13).
    await loadLiveFeedIfAvailable();
    await refreshBroadcastStatus();
    heartbeatSession();
    setInterval(function(){ heartbeatSession(); refreshBroadcastStatus(); loadTeacherSessions(); }, 20000);
    if(ACTIVE_CARDS.length === 0){
      document.getElementById("feedList").innerHTML = `<p class="empty-note">No history loaded. Check content/cards.json and js/data.js.</p>`;
      return;
    }
    renderFeed();
  }
  boot();

})();
