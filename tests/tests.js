(function(){
  "use strict";
  const { test, run } = window.miniTest;
  const Lib = window.FXLib;

  /* ---------- content loads ---------- */
  test("content loads: TIME_MACHINE_CARDS is a non-empty array", (assert) => {
    assert.ok(Array.isArray(window.TIME_MACHINE_CARDS), "expected an array");
    assert.ok(window.TIME_MACHINE_CARDS.length > 0, "expected at least one card");
  });

  test("content loads: every real card passes validation", (assert) => {
    const { cards, skipped } = Lib.loadCards(window.TIME_MACHINE_CARDS);
    assert.deepEqual(skipped, [], "no cards from content/cards.json should be skipped as malformed");
    assert.ok(cards.length === window.TIME_MACHINE_CARDS.length, "all authored cards should be valid");
  });

  test("content loads: source links are preserved on every card", (assert) => {
    window.TIME_MACHINE_CARDS.forEach(c => {
      assert.ok(typeof c.sourceUrl === "string" && c.sourceUrl.startsWith("http"), `${c.id} should have an http(s) sourceUrl`);
    });
  });

  test("content loads: card ids are unique", (assert) => {
    const ids = window.TIME_MACHINE_CARDS.map(c => c.id);
    assert.equal(new Set(ids).size, ids.length, "duplicate card id found");
  });

  /* ---------- malformed content fails safely ---------- */
  test("malformed content: a card missing required fields is skipped, not thrown", (assert) => {
    const raw = [
      { id: "good-1", title: "Good Card", hook: "A fine hook.", type: "photograph", sourceUrl: "https://example.org" },
      { id: "bad-1" /* missing title, hook, type, sourceUrl */ },
      { title: "No id" }, // missing id entirely
      null,
      "not even an object"
    ];
    const { cards, skipped } = Lib.loadCards(raw);
    assert.equal(cards.length, 1, "only the one valid card should survive");
    assert.equal(cards[0].id, "good-1");
    assert.equal(skipped.length, 4, "the four malformed entries should be reported as skipped");
  });

  test("malformed content: duplicate ids — second occurrence is skipped, not a crash", (assert) => {
    const raw = [
      { id: "dup", title: "First", hook: "h", type: "document", sourceUrl: "https://example.org" },
      { id: "dup", title: "Second", hook: "h", type: "document", sourceUrl: "https://example.org" }
    ];
    const { cards, skipped } = Lib.loadCards(raw);
    assert.equal(cards.length, 1);
    assert.equal(skipped.length, 1);
    assert.equal(skipped[0].errors[0], "duplicate id");
  });

  test("malformed content: validateCard rejects non-object input without throwing", (assert) => {
    const result = Lib.validateCard(undefined);
    assert.equal(result.valid, false);
    assert.ok(result.errors.length > 0);
  });

  /* ---------- favorites / save toggle ---------- */
  test("favorites/save: toggleInArray adds then removes an id", (assert) => {
    let list = [];
    list = Lib.toggleInArray(list, "earthrise-1968");
    assert.deepEqual(list, ["earthrise-1968"]);
    list = Lib.toggleInArray(list, "earthrise-1968");
    assert.deepEqual(list, []);
  });

  test("favorites/save: toggleInArray does not mutate the original array", (assert) => {
    const original = ["a", "b"];
    const next = Lib.toggleInArray(original, "c");
    assert.deepEqual(original, ["a", "b"], "original array must be untouched");
    assert.deepEqual(next, ["a", "b", "c"]);
  });

  /* ---------- search ---------- */
  test("search: matches on title", (assert) => {
    const card = { title: "Earthrise", hook: "Apollo 8", tags: [] };
    assert.ok(Lib.matchesSearch(card, "earthrise"));
    assert.ok(Lib.matchesSearch(card, "EARTH"));
    assert.ok(!Lib.matchesSearch(card, "rosetta"));
  });

  test("search: matches on tags, people, and places", (assert) => {
    const card = { title: "X", tags: ["Cherokee"], relatedPeople: ["John Ross"], relatedPlaces: ["Tahlequah"] };
    assert.ok(Lib.matchesSearch(card, "cherokee"));
    assert.ok(Lib.matchesSearch(card, "ross"));
    assert.ok(Lib.matchesSearch(card, "tahlequah"));
  });

  test("search: empty query matches everything", (assert) => {
    assert.ok(Lib.matchesSearch({ title: "Anything" }, ""));
  });

  /* ---------- personalization ---------- */
  test("personalization: following a tag increases a matching card's score", (assert) => {
    const follow = { followedTags: ["Indigenous history"], followedPeople: [], followedPlaces: [], favorites: [] };
    const matching = { id: "a", tags: ["Indigenous history"] };
    const nonMatching = { id: "b", tags: ["space"] };
    assert.ok(Lib.personalScore(matching, follow) > Lib.personalScore(nonMatching, follow));
  });

  /* ---------- basic media/type handling ---------- */
  test("card display: unknown type falls back to a sensible label instead of throwing", (assert) => {
    const meta = Lib.typeMeta("some_totally_unknown_type");
    assert.ok(typeof meta.label === "string" && meta.label.length > 0);
  });

  test("escapeHtml: neutralizes markup so card text can't break the page", (assert) => {
    const escaped = Lib.escapeHtml('<script>alert(1)</script>&"\'');
    assert.ok(!escaped.includes("<script>"));
    assert.ok(escaped.includes("&lt;"));
  });

  /* ---------- mixed-media template registry (brief section 3) ---------- */
  test("templates: primary action wording matches the spec table exactly", (assert) => {
    const cases = {
      "moving-image-source": "Watch at Institution",
      "later-documentary": "Watch at Institution",
      "audio-oral-history": "Listen at Institution",
      "original-document": "View Original at Institution",
      "historical-photograph": "View Original at Institution",
      "artifact-object": "View at Institution",
      "artwork-illustration": "View Original at Institution",
      "map": "View Original at Institution",
      "reconstructed-story": "Open Gazette",
      "photo-story": "Open Gazette",
      "infographic": "Open Gazette"
    };
    Object.entries(cases).forEach(([templateId, expected]) => {
      assert.equal(Lib.templateInfo(templateId).primaryAction, expected, templateId);
    });
  });

  test("templates: authored templates are internal, source templates are external", (assert) => {
    ["reconstructed-story", "photo-story", "infographic", "timeline", "then-and-now", "compare-perspectives"].forEach(id => {
      assert.equal(Lib.templateInfo(id).external, false, id + " should be internal (Open Gazette)");
    });
    ["historical-photograph", "audio-oral-history", "original-document", "moving-image-source"].forEach(id => {
      assert.equal(Lib.templateInfo(id).external, true, id + " should link out to the institution");
    });
  });

  test("templates: mediaKindToTemplate maps every common kind without throwing", (assert) => {
    ["photograph", "audio", "video", "document", "artifact", "map", "unknown_kind", "", null, undefined].forEach(kind => {
      const templateId = Lib.mediaKindToTemplate(kind);
      assert.ok(typeof templateId === "string" && templateId.length > 0, `mediaKindToTemplate(${kind}) should return a string`);
    });
  });

  /* ---------- TimeStream helpers ---------- */
  test("TimeStream: cardHasUsableVisual requires a real image candidate", (assert) => {
    assert.ok(Lib.cardHasUsableVisual({ image: "https://example.org/a.jpg" }));
    assert.ok(!Lib.cardHasUsableVisual({ title: "No picture", image: null }));
    assert.ok(!Lib.cardHasUsableVisual(null));
  });

  test("TimeStream: filterStreamCards skips seen ids and prefers visuals", (assert) => {
    const cards = [
      { id: "seen-1", title: "Seen", image: "https://example.org/s.jpg" },
      { id: "visual-1", title: "Visual", image: "https://example.org/v.jpg" },
      { id: "plain-1", title: "Plain", image: null },
      { id: "visual-1", title: "Dup" }
    ];
    const visual = Lib.filterStreamCards(cards, ["seen-1"], { visualOnly: true });
    assert.equal(visual.length, 1);
    assert.equal(visual[0].id, "visual-1");
    const withFallback = Lib.filterStreamCards(cards, ["seen-1"], { includeFallback: true });
    assert.ok(withFallback.some(c => c.id === "visual-1"));
    assert.ok(withFallback.some(c => c.id === "plain-1"));
  });

  test("TimeStream: next/previous and prefetch helpers", (assert) => {
    assert.equal(Lib.stepStreamIndex(2, 1, 5), 3);
    assert.equal(Lib.stepStreamIndex(0, -1, 5), 0);
    assert.equal(Lib.stepStreamIndex(4, 1, 5), 4);
    const queue = [{ id: "a" }, { id: "b" }, { id: "c" }];
    assert.equal(Lib.nextPlayableStreamIndex(queue, 0, 1, ["b"]), 2);
    assert.equal(Lib.nextPlayableStreamIndex(queue, 2, 1, []), -1);
    assert.ok(Lib.streamShouldPrefetch(5, 8, 3));
    assert.ok(!Lib.streamShouldPrefetch(0, 8, 3));
  });

  test("classroom share: installSharedClassCard uses the nested normalized card", (assert) => {
    const store = {};
    const shared = {
      id: "apollo-11",
      title: "Apollo 11 Moon Landing",
      card: {
        id: "apollo-11",
        title: "Apollo 11 Moon Landing",
        hook: "One small step.",
        image: "https://example.org/apollo.jpg",
        sourceUrl: "https://www.nasa.gov/apollo",
        institution: "NASA"
      }
    };
    const installed = Lib.installSharedClassCard(shared, store);
    assert.equal(installed.title, "Apollo 11 Moon Landing");
    assert.equal(installed.hook, "One small step.");
    assert.equal(installed.image, "https://example.org/apollo.jpg");
    assert.equal(store["apollo-11"].sourceUrl, "https://www.nasa.gov/apollo");
    assert.ok(!Lib.installSharedClassCard({ id: "unknown-card", title: "Missing" }, {}));
  });

  /* ---------- infographic authoring path (brief section 5) ---------- */
  test("infographics: authored infographics load and are distinct from each other", (assert) => {
    assert.ok(Array.isArray(window.TIME_MACHINE_INFOGRAPHICS), "expected TIME_MACHINE_INFOGRAPHICS array");
    assert.ok(window.TIME_MACHINE_INFOGRAPHICS.length >= 2, "expected at least 2 shipped infographics to prove the renderer is generic");
    const titles = window.TIME_MACHINE_INFOGRAPHICS.map(i => i.title);
    assert.equal(new Set(titles).size, titles.length, "infographic titles should be unique");
  });

  test("infographics: every infographic has the required schema fields", (assert) => {
    window.TIME_MACHINE_INFOGRAPHICS.forEach(info => {
      assert.ok(info.title, "missing title");
      assert.ok(info.infographicType, "missing infographicType");
      assert.ok(info.status, "missing status");
      assert.ok(info.hook || info.question, "missing hook/question");
    });
  });


  // These load the REAL index.html (and a malformed-data variant of it) in
  // a hidden iframe and inspect the rendered DOM — genuinely fails if the
  // app throws, doesn't render, or crashes on bad data, instead of assuming
  // success.
  function loadIframe(src){
    return new Promise((resolve, reject) => {
      const iframe = document.createElement("iframe");
      iframe.style.cssText = "width:1px;height:1px;opacity:0;position:absolute;pointer-events:none;";
      let fatalError = null;
      iframe.addEventListener("load", () => {
        try{
          // Catch runtime errors thrown inside the iframe's own app.js.
          iframe.contentWindow.addEventListener("error", (e) => {
            fatalError = (e && e.message) || "unknown error";
          });
        }catch(e){ /* cross-origin would land here; not expected for a local iframe */ }
        setTimeout(() => resolve({ iframe, fatalError: () => fatalError }), 350);
      });
      iframe.addEventListener("error", () => reject(new Error(`iframe failed to load ${src}`)));
      document.body.appendChild(iframe);
      iframe.src = src.includes("?") ? src + "&ts=" + Date.now() : src + "?ts=" + Date.now();
    });
  }

  test("application starts: index.html boots with no fatal JS error", async (assert) => {
    const { iframe, fatalError } = await loadIframe("../index.html?offline=1");
    assert.ok(!fatalError(), `expected no runtime error, got: ${fatalError()}`);
    iframe.remove();
  });

  test("application starts: TimeFeed container exists in the rendered DOM", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    assert.ok(!!doc.getElementById("feedList"), "expected #feedList to exist");
    iframe.remove();
  });

  test("application starts: expected valid cards render into the TimeFeed", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const rendered = doc.querySelectorAll("#feedList .card").length;
    // The feed is cards from data.js PLUS authored infographics converted
    // to cards at boot (see infographicToCard in app.js) — both are
    // authored/offline content, so both should show up in the demo feed.
    const expected = iframe.contentWindow.TIME_MACHINE_CARDS.length
      + (Array.isArray(iframe.contentWindow.TIME_MACHINE_INFOGRAPHICS) ? iframe.contentWindow.TIME_MACHINE_INFOGRAPHICS.length : 0);
    assert.ok(rendered > 0, "expected at least one card to render");
    assert.equal(rendered, expected, `expected all ${expected} authored cards (incl. infographics) to render, got ${rendered}`);
    iframe.remove();
  });

  test("application starts: Gazette reconstructions are labeled, not presented as authentic", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const gazetteNav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "gazette");
    gazetteNav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    const badges = doc.querySelectorAll("#gazetteList .badge-reconstruction");
    assert.ok(badges.length > 0, "expected at least one reconstruction badge in the Gazette");
    iframe.remove();
  });

  test("application starts: malformed cards are skipped, valid cards still render, feed doesn't crash", async (assert) => {
    const { iframe, fatalError } = await loadIframe("fixture-malformed.html");
    assert.ok(!fatalError(), `fixture with malformed data should not throw, got: ${fatalError()}`);
    const doc = iframe.contentDocument;
    const rendered = [...doc.querySelectorAll("#feedList .card")].map(el => el.dataset.cardId);
    // Fixture has 6 raw entries; only valid-1 and valid-2 should survive
    // (missing fields, no id, null, and the duplicate id are all skipped).
    assert.equal(rendered.length, 2, `expected exactly 2 valid cards to render, got ${rendered.length}`);
    assert.ok(rendered.includes("valid-1") && rendered.includes("valid-2"), "expected valid-1 and valid-2 specifically");
    iframe.remove();
  });

  test("application starts: an external-template card shows its primary action as a real link to the source", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const earthrise = doc.querySelector('[data-card-id="earthrise-1968"]');
    assert.ok(earthrise, "expected the Earthrise demo card to exist");
    const primary = earthrise.querySelector("a.actbtn--primary");
    assert.ok(primary, "expected a primary <a> action on a photograph card");
    assert.equal(primary.textContent.trim(), "View Original at Institution ↗");
    assert.ok(primary.getAttribute("href").startsWith("https://www.nasa.gov"), "primary action should link straight to the institution");
    iframe.remove();
  });

  test("application starts: an internal-template (Gazette) card's primary action opens Learn More, not a link", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const gazetteCard = doc.querySelector('[data-card-id="indian-removal-gazette-1830"]');
    const primary = gazetteCard.querySelector("button.actbtn--primary");
    assert.ok(primary, "expected a primary <button> (not a link) for an internal template");
    assert.equal(primary.textContent.trim(), "Open Gazette");
    primary.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    assert.ok(!doc.getElementById("learnMoreOverlay").hidden, "clicking 'Open Gazette' should open the Learn More panel");
    iframe.remove();
  });

  test("application starts: infographic cards render generically and appear in the Gazette", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const infoCards = [...doc.querySelectorAll('[data-card-id^="infographic-"]')];
    assert.ok(infoCards.length >= 2, "expected at least 2 infographic cards in the TimeFeed");

    const first = infoCards[0];
    first.querySelector('[data-act="learnmore"]').dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    const lmHtml = doc.getElementById("lmContent").innerHTML;
    assert.ok(lmHtml.includes("The Numbers") || lmHtml.includes("Sources"), "Learn More should render the infographic's own facts/sources generically");
    doc.getElementById("lmClose").click();

    const gazetteNav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "gazette");
    gazetteNav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    const gazetteIds = [...doc.querySelectorAll("#gazetteList [data-card-id]")].map(el => el.dataset.cardId);
    assert.ok(gazetteIds.some(id => id.startsWith("infographic-")), "infographics should appear in the Gazette");
    const mast = doc.querySelector("#gazetteList .broadsheet__mast");
    assert.ok(mast && mast.textContent.indexOf("Time Machine Gazette") !== -1, "Gazette should show a newspaper masthead");
    iframe.remove();
  });

  test("application starts: Broadcast stays off and the join banner is hidden", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const banner = doc.getElementById("broadcastBanner");
    const btn = doc.getElementById("broadcastBtn");
    assert.ok(btn, "expected a Broadcast control");
    assert.ok(banner, "expected a Broadcast join banner element");
    assert.ok(banner.hidden, "Broadcast join banner should be hidden while Broadcast is off");
    const teacherNav = doc.getElementById("teacherNav");
    assert.ok(teacherNav, "expected a Teacher nav control");
    assert.ok(teacherNav.hidden, "Teacher View should stay hidden while Broadcast is off");
    const studentBar = doc.getElementById("studentClassroomBar");
    assert.ok(studentBar, "expected a student classroom indicator");
    assert.ok(studentBar.hidden, "student classroom indicator should be hidden on a normal boot");
    iframe.remove();
  });

  test("application starts: TimeStream tab exists and can show a card", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const nav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "timestream");
    assert.ok(nav, "expected a TimeStream nav control");
    assert.equal(nav.textContent.trim(), "TimeStream");
    nav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 80));
    const view = doc.getElementById("viewTimestream");
    assert.ok(view && !view.hidden, "TimeStream view should open");
    assert.ok(doc.getElementById("streamMedia"), "expected a TimeStream media stage");
    assert.ok(doc.getElementById("streamPlayBtn"), "expected Pause/Resume");
    assert.ok(doc.getElementById("streamPrevBtn") && doc.getElementById("streamNextBtn"), "expected Previous/Next");
    assert.ok(doc.getElementById("streamSaveBtn"), "expected Save");
    assert.ok(doc.getElementById("streamLearnBtn"), "expected Learn More");
    const title = (doc.getElementById("streamTitle") || {}).textContent || "";
    assert.ok(title.length > 0 && title !== "Finding history…", "TimeStream should show a card title from loaded history");
    iframe.remove();
  });

  test("application starts: TimeStream Save uses the normal Saved list", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const nav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "timestream");
    nav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 80));
    const saveBtn = doc.getElementById("streamSaveBtn");
    const title = doc.getElementById("streamTitle").textContent;
    assert.ok(title && title !== "Finding history…", "TimeStream should be showing a real card before Save");
    saveBtn.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 40));
    const savedNav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "saved");
    savedNav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 40));
    const saved = doc.getElementById("savedItems").textContent;
    assert.ok(saved.indexOf(title) !== -1, "Saved should include the TimeStream card");
    iframe.remove();
  });

  test("application starts: TimeStream Learn More uses the existing overlay", async (assert) => {
    const { iframe } = await loadIframe("../index.html?offline=1");
    const doc = iframe.contentDocument;
    const nav = [...doc.querySelectorAll(".navlink")].find(b => b.dataset.view === "timestream");
    nav.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 80));
    doc.getElementById("streamLearnBtn").dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    assert.ok(!doc.getElementById("learnMoreOverlay").hidden, "Learn More should open from TimeStream");
    const title = doc.getElementById("streamTitle").textContent;
    assert.ok(doc.getElementById("lmContent").textContent.indexOf(title) !== -1, "Learn More should show the current TimeStream card");
    iframe.remove();
  });

  test("application starts: evidence button opens Learn More and reveals the evidence section", async (assert) => {
    const { iframe } = await loadIframe("evidence-fixture.html");
    const doc = iframe.contentDocument;
    const evidenceBtn = doc.querySelector('[data-act="evidence"]');
    assert.ok(evidenceBtn, "expected an Evidence button on the fixture's live-style card");
    evidenceBtn.dispatchEvent(new iframe.contentWindow.MouseEvent("click", { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    assert.ok(!doc.getElementById("learnMoreOverlay").hidden, "Evidence button should open Learn More");
    const evidenceSection = doc.getElementById("evidence");
    assert.ok(evidenceSection, "expected an #evidence anchor inside Learn More");
    assert.ok(evidenceSection.parentElement.textContent.includes("NASA"), "evidence section should show the institution");
    iframe.remove();
  });

  run(document.getElementById("results"));
})();
