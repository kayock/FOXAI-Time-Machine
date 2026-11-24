"""
sources/seeds.py

Turns a discovery seed (seeds/discovery_seeds.json — a title + search
queries + preferred sources + a template hint + an optional fallback URL)
into a real, truthful TimeMachineItem, or an honest "unresolved" result.

Resolution order (brief section 1):
  1. Search the seed's preferred adapters.
  2. Choose a useful real institutional result.
  3. Prefer a result whose OWN, truthfully-computed template matches the
     seed's templateHint — never force-relabel a result to match the hint.
  4. If nothing usable came back and the seed has an optionalSeedUrl, fall
     back to the institutional-page resolver on exactly that URL.
  5. Otherwise report the seed unresolved. Never fabricate a result.

Dependency-injected on purpose (search_fn / institutional_fn) so this is
fully hermetically testable without any network access — see
tests/python/test_seed_resolution.py.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Dict, Any

from .base import TimeMachineItem

# Bounded: only try a seed's first couple of preferred sources and first
# couple of search queries — this is discovery, not an exhaustive crawl.
MAX_SOURCES_TRIED_PER_SEED = 2
MAX_QUERIES_TRIED_PER_SOURCE = 2
RESULTS_PER_SOURCE_QUERY = 5

# Templates that represent an actual, distinct media requirement — a result
# claiming one of these must actually BE that media kind, not just
# "roughly about the same topic." Photograph/artwork hints are included so
# a modern video conversation about Earthrise cannot satisfy a
# historical-photograph seed; softer hints (document, artifact, map) still
# accept a useful on-topic result even when the presentation label differs.
STRICT_MEDIA_HINTS = {
    "audio-oral-history", "moving-image-source", "later-documentary",
    "historical-photograph", "artwork-illustration",
}

# Titles containing any of these are treated as weak matches regardless of
# keyword overlap — retrospective commentary, anniversary messages, program
# overviews, and the like are usually ABOUT a topic without being the
# primary archival material a featured seed is looking for (brief section 1
# examples: a 2024 Apollo 11 anniversary message, a 2017 Earth
# visualization, a generic blog post).
WEAK_SIGNAL_PHRASES = (
    "anniversary", "years later", "message from", "administrator's message",
    "visualization", "reflects on", "looks back", "remembers", "commemorat",
    "blog post", "op-ed", "retrospective", "tribute", "in memory of",
    "program overview", "mission statement", "visitor guide",
    "women@nasa", "for women@", "conversation with", "google+ hangout",
    "congressional gold medal", "centennial celebration", "celebration of",
    "th congress", "documentation act", "national trails system",
    "renames facility", "naming ceremony", "street renaming",
)

_STOPWORDS = {"the", "and", "of", "a", "an", "in", "on", "at", "to", "for",
              "from", "by", "with", "is", "are", "this", "that", "voices"}

RELEVANCE_THRESHOLD = 0.5


def _significant_terms(text: Optional[str]) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) >= 2}


def _has_weak_signal(title: Optional[str]) -> bool:
    t = (title or "").lower()
    return any(phrase in t for phrase in WEAK_SIGNAL_PHRASES)


def _best_query_relevance(seed: Dict[str, Any], item: TimeMachineItem) -> Optional[float]:
    """
    How well does this result's title match ANY of the seed's search
    queries? Takes the best match across queries (they're alternatives, not
    a combined checklist), so a result doesn't get penalized for not
    matching every phrasing of the topic.

    Returns None (rather than 0.0) when none of the seed's queries have any
    meaningfully comparable terms to judge against — a query too short or
    generic to score shouldn't be treated as "no overlap, therefore weak."
    """
    queries = seed.get("searchQueries") or [seed.get("title", "")]
    item_terms = _significant_terms(item.title)
    best = None
    for q in queries:
        q_terms = _significant_terms(q)
        if not q_terms:
            continue
        overlap = q_terms & item_terms
        ratio = len(overlap) / len(q_terms)
        best = ratio if best is None else max(best, ratio)
    return best


def _is_weak_candidate(seed: Dict[str, Any], item: TimeMachineItem, template_hint: Optional[str]) -> bool:
    """
    A result is "weak" — not good enough to count as resolved — when either:
    - the hint represents a real media requirement (audio/video/photograph/
      artwork) the result doesn't actually satisfy, or
    - its title doesn't look like it's actually about the seed's subject
      (low keyword overlap with every search query), or
    - its title reads like retrospective commentary/an anniversary post
      rather than primary archival material.
    This never changes what a result IS (see _apply_hint_to_generic_fallback
    for the one place presentation is adjusted) — it only decides whether a
    real result is good enough to accept.
    """
    if template_hint in STRICT_MEDIA_HINTS and item.mediaTemplate != template_hint:
        return True
    if _has_weak_signal(item.title):
        return True
    relevance = _best_query_relevance(seed, item)
    if relevance is not None and relevance < RELEVANCE_THRESHOLD:
        return True
    return False


def _media_kind_for_strict_hint(template_hint: Optional[str]) -> Optional[str]:
    """Map a strict template hint to the adapter media filter when one exists.
    NASA/LOC accept image/video/audio; photograph seeds ask for images."""
    return {
        "audio-oral-history": "audio",
        "moving-image-source": "video",
        "later-documentary": "video",
        "historical-photograph": "image",
        "artwork-illustration": "image",
    }.get(template_hint or "")


@dataclass
class SeedResolution:
    title: str
    resolved: bool
    method: str                      # "source-search" | "institutional-fallback" | "unresolved"
    item: Optional[TimeMachineItem]
    sourceId: Optional[str]
    templateHint: Optional[str]
    matchedTemplateHint: Optional[bool]   # None if no hint was given

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "resolved": self.resolved,
            "method": self.method,
            "item": self.item.to_dict() if self.item else None,
            "sourceId": self.sourceId,
            "templateHint": self.templateHint,
            "matchedTemplateHint": self.matchedTemplateHint,
        }


def resolve_seed(
    seed: Dict[str, Any],
    search_fn: Callable[..., List[TimeMachineItem]],
    institutional_fn: Callable[[str], Optional[TimeMachineItem]],
    randomize: bool = False,
) -> SeedResolution:
    title = seed.get("title", "Untitled seed")
    queries = seed.get("searchQueries") or ([title] if title else [])
    preferred_sources = seed.get("preferredSources") or []
    template_hint = seed.get("templateHint")
    optional_seed_url = seed.get("optionalSeedUrl")

    candidates: List[tuple] = []  # (sourceId, TimeMachineItem)
    sources_with_strong = 0
    media_filter = _media_kind_for_strict_hint(template_hint)

    for source_id in preferred_sources:
        if sources_with_strong >= MAX_SOURCES_TRIED_PER_SEED:
            break
        got_strong_from_this_source = False
        for query in queries[:MAX_QUERIES_TRIED_PER_SOURCE]:
            try:
                # Pass media= when the hint is strict so adapters that support
                # it (NASA) don't waste the budget on incompatible kinds.
                results = search_fn(
                    source_id, query, limit=RESULTS_PER_SOURCE_QUERY, media=media_filter,
                )
            except TypeError:
                # Older/fake search_fn without a media kwarg — hermetic tests.
                try:
                    results = search_fn(source_id, query, limit=RESULTS_PER_SOURCE_QUERY)
                except Exception:  # noqa: BLE001
                    results = []
            except Exception:  # noqa: BLE001 — one broken source must not break seed resolution
                results = []
            batch = []
            for item in results or []:
                candidates.append((source_id, item))
                batch.append(item)
            # Keep trying alternate queries on this source until something
            # strong appears — a first-page of only weak/retrospective hits
            # must not block a better second query (e.g. Katherine Johnson
            # after a Women@NASA program page, or AS08-14-2383 after a
            # conversation video about Earthrise).
            if any(not _is_weak_candidate(seed, item, template_hint) for item in batch):
                got_strong_from_this_source = True
                break
        if got_strong_from_this_source:
            sources_with_strong += 1

    # Do not accept the first vaguely relevant result — only strong,
    # genuinely-on-topic (and, where the hint demands it, media-compatible)
    # candidates count. Weak ones are treated the same as "no result" so
    # resolution falls through to the institutional fallback instead of
    # settling for a loosely-related match.
    strong_candidates = [(sid, item) for sid, item in candidates if not _is_weak_candidate(seed, item, template_hint)]

    if strong_candidates:
        chosen_source_id, chosen_item = _choose_best_candidate(strong_candidates, template_hint, randomize=randomize)
        matched_hint = (chosen_item.mediaTemplate == template_hint) if template_hint else None
        return SeedResolution(
            title=title, resolved=True, method="source-search", item=chosen_item,
            sourceId=chosen_source_id, templateHint=template_hint, matchedTemplateHint=matched_hint,
        )

    if optional_seed_url:
        try:
            fallback_item = institutional_fn(optional_seed_url)
        except Exception:  # noqa: BLE001
            fallback_item = None
        if fallback_item:
            fallback_item = _apply_hint_to_generic_fallback(fallback_item, template_hint)
            matched_hint = (fallback_item.mediaTemplate == template_hint) if template_hint else None
            return SeedResolution(
                title=title, resolved=True, method="institutional-fallback", item=fallback_item,
                sourceId="institutional_page", templateHint=template_hint, matchedTemplateHint=matched_hint,
            )

    return SeedResolution(
        title=title, resolved=False, method="unresolved", item=None,
        sourceId=None, templateHint=template_hint, matchedTemplateHint=None,
    )


# Presentation-only template hints that are safe to apply to a generic
# institutional-page fallback: they only change classification/action
# wording (e.g. "View at Institution" vs "View Original at Institution"),
# never imply media capability the page hasn't actually demonstrated.
# audio-oral-history / moving-image-source are deliberately excluded here —
# the institutional-page resolver has no way to confirm a page actually
# offers playable audio/video, so applying those hints would risk exactly
# the kind of relabeling the brief prohibits ("never relabel source
# material in a way that contradicts what it actually is").
_SAFE_FALLBACK_PRESENTATION_HINTS = {
    "artifact-object", "artwork-illustration", "map", "original-document",
}


def _apply_hint_to_generic_fallback(item: TimeMachineItem, template_hint: Optional[str]) -> TimeMachineItem:
    """
    The institutional-page fallback (sources/institutional_page.py) always
    normalizes a page as the generic "document" media kind — it has no
    reliable way to detect the real subject from arbitrary page metadata.
    When the seed's own templateHint names an unambiguous, non-media-claiming
    presentation (e.g. Dracorex's fossil page should present as
    artifact-object, not a generic document), apply it — but only when the
    fallback's own detection was still generic, and only for hints that
    don't assert playback capability the page never demonstrated.
    """
    if not template_hint or template_hint not in _SAFE_FALLBACK_PRESENTATION_HINTS:
        return item
    if item.mediaKind != "document":
        return item  # the resolver already had a confident, real signal — leave it alone
    item.mediaTemplate = template_hint
    return item


def _choose_best_candidate(candidates: List[tuple], template_hint: Optional[str], randomize: bool = False) -> tuple:
    """
    Among real, already-vetted-as-strong results, prefer one whose OWN
    normalized template happens to match the hint. This never changes what
    a result IS — it only changes which truthful result we show first when
    more than one is available (brief section 2: "must not contradict the
    real source").

    When randomize=True (Surprise Me), pick randomly within whichever tier
    (hint-matching, or all strong candidates) — this is what gives repeated
    "fresh batch" requests real variety instead of always returning the
    same top pick, while every candidate in the pool has already passed the
    same relevance/compatibility bar.
    """
    pool = candidates
    if template_hint:
        matching = [c for c in candidates if c[1].mediaTemplate == template_hint]
        if matching:
            pool = matching
    return random.choice(pool) if randomize else pool[0]


def resolve_all_seeds(
    seeds: List[Dict[str, Any]],
    search_fn: Callable[..., List[TimeMachineItem]],
    institutional_fn: Callable[[str], Optional[TimeMachineItem]],
    limit: Optional[int] = None,
    randomize: bool = False,
) -> List[SeedResolution]:
    ordered = seeds
    if randomize:
        ordered = random.sample(seeds, k=len(seeds))  # same 15 seeds, fresh order each call
    bounded = ordered[: limit] if limit else ordered
    return [resolve_seed(seed, search_fn, institutional_fn, randomize=randomize) for seed in bounded]
