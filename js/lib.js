/* =========================================================
   FOXAI TIME MACHINE — lib.js
   Small, pure, dependency-free helper functions.
   Kept separate from app.js so they're easy to unit test
   (see tests/tests.js) without touching the DOM.
   ========================================================= */
(function(root){
  "use strict";

  const TYPE_META = {
    photograph: { label: "Photograph", icon: "📷" },
    painting:   { label: "Painting", icon: "🎨" },
    illustration:{ label: "Illustration", icon: "🖌" },
    newspaper:  { label: "Newspaper", icon: "📰" },
    gazette:    { label: "Gazette", icon: "📰" },
    poster:     { label: "Poster", icon: "🖼" },
    advertisement:{ label:"Advertisement", icon:"🖼" },
    map:        { label: "Map", icon: "🗺" },
    letter:     { label: "Letter", icon: "✉️" },
    manuscript: { label: "Manuscript", icon: "📜" },
    document:   { label: "Document", icon: "📄" },
    cartoon:    { label: "Political Cartoon", icon: "🖋" },
    artifact:   { label: "Artifact", icon: "🏺" },
    speech:     { label: "Speech", icon: "🎙" },
    audio:      { label: "Audio", icon: "🔊" },
    oral_history:{ label:"Oral History", icon:"🔊" },
    film:       { label: "Film", icon: "🎞" },
    video:      { label: "Video", icon: "🎞" },
    infographic:{ label: "Infographic", icon: "📊" }
  };

  // Mirrors sources/base.py's TEMPLATES for the frontend — classification
  // label + primary action text per mixed-media template (brief section 8).
  // Kept in sync by hand since it's a small, stable vocabulary; if this
  // drifts from the Python side, template_registry tests below will show it.
  const TEMPLATES = {
    "moving-image-source": { classificationDefault: "PERIOD FOOTAGE", primaryAction: "Watch at Institution", external: true },
    "later-documentary": { classificationDefault: "LATER DOCUMENTARY", primaryAction: "Watch at Institution", external: true },
    "audio-oral-history": { classificationDefault: "ORAL HISTORY", primaryAction: "Listen at Institution", external: true },
    "original-document": { classificationDefault: "ORIGINAL DOCUMENT", primaryAction: "View Original at Institution", external: true },
    "historical-photograph": { classificationDefault: null, primaryAction: "View Original at Institution", external: true },
    "artifact-object": { classificationDefault: null, primaryAction: "View at Institution", external: true },
    "map": { classificationDefault: null, primaryAction: "View Original at Institution", external: true },
    "artwork-illustration": { classificationDefault: null, primaryAction: "View Original at Institution", external: true },
    "reconstructed-story": { classificationDefault: "HISTORICAL RECONSTRUCTION", primaryAction: "Open Gazette", external: false },
    "photo-story": { classificationDefault: null, primaryAction: "Open Gazette", external: false },
    "infographic": { classificationDefault: "FOXAI INFOGRAPHIC", primaryAction: "Open Gazette", external: false },
    "timeline": { classificationDefault: "TIMELINE", primaryAction: "Open Gazette", external: false },
    "then-and-now": { classificationDefault: "THEN-AND-NOW COMPARISON", primaryAction: "Open Gazette", external: false },
    "compare-perspectives": { classificationDefault: "COMPARE PERSPECTIVES", primaryAction: "Open Gazette", external: false }
  };
  function templateInfo(templateId){
    return TEMPLATES[templateId] || TEMPLATES["artifact-object"];
  }

  // Mirrors sources/base.py's MEDIA_KIND_TO_TEMPLATE, for demo/offline
  // cards that only carry a "type" (mediaKind) and no explicit mediaTemplate.
  const MEDIA_KIND_TO_TEMPLATE = {
    photograph: "historical-photograph",
    artwork: "artwork-illustration",
    illustration: "artwork-illustration",
    map: "map",
    document: "original-document",
    manuscript: "original-document",
    letter: "original-document",
    artifact: "artifact-object",
    object: "artifact-object",
    moving_image: "moving-image-source",
    video: "moving-image-source",
    documentary: "later-documentary",
    audio: "audio-oral-history",
    oral_history: "audio-oral-history",
    gazette: "reconstructed-story",
    speech: "audio-oral-history",
    infographic: "infographic"
  };
  function mediaKindToTemplate(kind){
    return MEDIA_KIND_TO_TEMPLATE[(kind || "").toLowerCase()] || "artifact-object";
  }

  function typeMeta(t){
    return TYPE_META[t] || { label: t ? (String(t).charAt(0).toUpperCase()+String(t).slice(1)) : "Item", icon: "🕰" };
  }

  function escapeHtml(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, m => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[m]));
  }

  // A card needs at minimum: id, title, and either a hook or longerContext,
  // plus a type and a source. Everything else is optional (see README).
  function validateCard(card){
    const errors = [];
    if(!card || typeof card !== "object"){ return { valid:false, errors:["card is not an object"] }; }
    if(!card.id || typeof card.id !== "string") errors.push("missing id");
    if(!card.title || typeof card.title !== "string") errors.push("missing title");
    if(!card.hook && !card.longerContext) errors.push("missing hook/longerContext");
    if(!card.type || typeof card.type !== "string") errors.push("missing type");
    if(!card.sourceUrl) errors.push("missing sourceUrl");
    return { valid: errors.length === 0, errors };
  }

  // Filters a raw card array down to the valid ones, and reports what was
  // skipped and why — this is what keeps one bad card from breaking the feed.
  function loadCards(rawArray){
    const cards = [];
    const skipped = [];
    const seenIds = new Set();
    (Array.isArray(rawArray) ? rawArray : []).forEach((card, index) => {
      const { valid, errors } = validateCard(card);
      if(!valid){ skipped.push({ index, id: card && card.id, errors }); return; }
      if(seenIds.has(card.id)){ skipped.push({ index, id: card.id, errors: ["duplicate id"] }); return; }
      seenIds.add(card.id);
      cards.push(card);
    });
    return { cards, skipped };
  }

  function matchesSearch(card, query){
    if(!query) return true;
    if(!card) return false;
    const hay = [
      card.title, card.hook, card.date, card.location, card.institution, card.creator,
      ...(card.tags||[]), ...(card.relatedPeople||[]), ...(card.relatedPlaces||[])
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(String(query).toLowerCase());
  }

  // Pure toggle: returns a NEW array with id added or removed.
  // A card can supply an image via any of these fields (checked in this
  // priority order). All are optional — see README for the image guide.
  function imageCandidates(card){
    if(!card) return [];
    return [card.image, card.thumbnailUrl, card.iiifImageUrl, card.localImagePath]
      .filter(v => typeof v === "string" && v.trim().length > 0);
  }

  function cardHasUsableVisual(card){
    return imageCandidates(card).length > 0;
  }

  function filterStreamCards(cards, seenIds, opts){
    opts = opts || {};
    const seen = seenIds instanceof Set ? seenIds : new Set(seenIds || []);
    const visual = [];
    const fallback = [];
    const used = new Set();
    (Array.isArray(cards) ? cards : []).forEach(card => {
      if(!card || !card.id || seen.has(card.id) || used.has(card.id)) return;
      used.add(card.id);
      if(cardHasUsableVisual(card)) visual.push(card);
      else fallback.push(card);
    });
    if(opts.visualOnly) return visual;
    if(visual.length) return visual.concat(opts.includeFallback ? fallback : []);
    return fallback;
  }

  function streamShouldPrefetch(index, length, remainingThreshold){
    const thresh = remainingThreshold == null ? 3 : remainingThreshold;
    if(!(length > 0)) return true;
    return (length - index - 1) <= thresh;
  }

  function stepStreamIndex(index, delta, length){
    if(!(length > 0)) return 0;
    const next = (index || 0) + (delta || 0);
    if(next < 0) return 0;
    if(next >= length) return length - 1;
    return next;
  }

  function nextPlayableStreamIndex(queue, fromIndex, direction, brokenIds){
    const len = Array.isArray(queue) ? queue.length : 0;
    if(!len) return -1;
    const dir = direction < 0 ? -1 : 1;
    const broken = brokenIds instanceof Set ? brokenIds : new Set(brokenIds || []);
    let i = fromIndex;
    for(let n = 0; n < len; n++){
      i += dir;
      if(i < 0 || i >= len) return -1;
      const card = queue[i];
      if(card && card.id && !broken.has(card.id)) return i;
    }
    return -1;
  }

  function installSharedClassCard(shared, cardsById){
    const store = cardsById && typeof cardsById === "object" ? cardsById : {};
    if(!shared || typeof shared !== "object") return null;
    if(shared.card && typeof shared.card === "object" && shared.card.id){
      store[shared.card.id] = shared.card;
      return shared.card;
    }
    if(shared.id && store[shared.id]) return store[shared.id];
    return null;
  }

  function toggleInArray(list, id){
    const arr = Array.isArray(list) ? list.slice() : [];
    const i = arr.indexOf(id);
    if(i === -1) arr.push(id); else arr.splice(i, 1);
    return arr;
  }

  function personalScore(card, followState){
    followState = followState || { followedTags:[], followedPeople:[], followedPlaces:[], favorites:[] };
    let score = 0;
    (card.tags||[]).forEach(t => { if(followState.followedTags.includes(t)) score += 3; });
    (card.relatedPeople||[]).forEach(p => { if(followState.followedPeople.includes(p)) score += 3; });
    (card.relatedPlaces||[]).forEach(p => { if(followState.followedPlaces.includes(p)) score += 3; });
    if(followState.favorites && followState.favorites.includes(card.id)) score += 1;
    return score;
  }

  root.FXLib = {
    typeMeta, escapeHtml, validateCard, loadCards, matchesSearch, personalScore,
    toggleInArray, imageCandidates, templateInfo, mediaKindToTemplate,
    cardHasUsableVisual, filterStreamCards, streamShouldPrefetch, stepStreamIndex,
    nextPlayableStreamIndex, installSharedClassCard
  };

})(typeof window !== "undefined" ? window : globalThis);
