"""
sources/dynamic_topics.py

Organic discovery-topic expansion (v2.1) — finds NEW search topics inside
authentic archival metadata the app already retrieved, so discovery can
keep roaming far beyond the curated topic pool without ever inventing
content. No external AI, no paid API: candidates come from the archives'
own subject/keyword/place metadata attached to TimeMachineItems.

What makes a candidate a good discovered topic:
  - it is institutional metadata (LOC subject headings, NASA keywords,
    IA subjects, DPLA subjects, Wikipedia categories…), i.e. an authentic
    subject label some archivist assigned to real historical material;
  - it appears on more than one record (frequency >= 2) OR is a specific
    multi-word phrase — single rare words are usually noise;
  - it is not generic ("history", "photographs", "print") and not just a
    year, and it differs from the queries that produced it.

Every candidate carries provenance (which source and parent query yielded
it) so /api/discovery/stats can show where each topic came from, and
persistence/upserting happens in DiscoveryState (bounded rows, least-used
first rotation).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from .discovery_state import normalize_text, _significant_terms, parse_year

# Generic words that make bad search topics even though archives attach
# them constantly (format/material labels rather than subjects).
GENERIC_LABELS = {
    "history", "photograph", "photographs", "photography", "print", "prints",
    "painting", "paintings", "drawing", "drawings", "map", "maps", "manuscript",
    "manuscripts", "document", "documents", "art", "artifact", "artifacts",
    "object", "objects", "image", "images", "postcard", "postcards", "book",
    "books", "paper", "papers", "letter", "letters", "album", "albums",
    "negative", "negatives", "slide", "slides", "film", "films", "video",
    "audio", "recording", "recordings", "interview", "interviews",
    "clippings", "scrapbooks", "portrait", "portraits", "views", "correspondence",
    "united states", "great britain", "new york", "france", "germany",
    "england", "london", "paris", "america", "europe", "world war", "world war i",
    "world war ii", "19th century", "20th century", "19th century photograph",
}

# Candidate length/shape bounds — a topic must be a usable search query.
MIN_CHARS = 5
MAX_CHARS = 80
MIN_WORDS = 1
MAX_WORDS = 4


def _candidate_shape_ok(text: str) -> bool:
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    words = text.split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        return False
    if parse_year(text) and len(words) == 1:
        return False  # a bare year is not a topic
    if text in GENERIC_LABELS:
        return False
    # Require at least one alphabetic word; digits-only is not a topic.
    if not any(w.isalpha() for w in words):
        return False
    # Reject strings that are mostly numbers/punctuation.
    alpha = sum(1 for ch in text if ch.isalpha())
    if alpha < len(text) * 0.5:
        return False
    return True


def _extract_strings(item) -> List[str]:
    """All authentic metadata strings attached to one record."""
    out: List[str] = []
    for topic in (item.topics or []):
        if isinstance(topic, str):
            out.append(topic)
    for place in (item.places or []):
        if isinstance(place, str):
            out.append(place)
    ev = item.evidence
    if ev is not None:
        collection = getattr(ev, "collection", None)
        if isinstance(collection, str) and collection:
            out.append(collection)
    return out


def mine_topics(items, parent_queries: Optional[List[str]] = None,
                max_topics: int = 8) -> List[Dict[str, Any]]:
    """Mine organic topics from a batch of normalized items. Returns ranked
    candidates [{query, preferredSources, provenance}] best-first, bounded
    by max_topics. Never raises; never fabricates — every candidate string
    came from the archives themselves."""
    parents = set()
    for q in (parent_queries or []):
        norm = normalize_text(q)
        if norm:
            parents.add(norm)
        words = tuple(_significant_terms(q))
        if len(words) >= 2:
            parents.add(" ".join(words))

    frequency: Dict[str, int] = {}
    sources_by_topic: Dict[str, Set[str]] = {}
    display: Dict[str, str] = {}

    for item in items or []:
        if item is None:
            continue
        source_id = item.sourceId or ""
        seen_in_item: Set[str] = set()
        for raw in _extract_strings(item):
            text = normalize_text(raw)
            if not _candidate_shape_ok(text):
                continue
            if text in parents:
                continue
            # Title-cased display form: keep the longest original casing.
            if text not in display or len(raw) > len(display[text]):
                display[text] = raw.strip()
            seen_in_item.add(text)
        for text in seen_in_item:
            frequency[text] = frequency.get(text, 0) + 1
            sources_by_topic.setdefault(text, set()).add(source_id)

    ranked = sorted(
        frequency.items(),
        key=lambda kv: (-kv[1], len(kv[0].split()), -len(kv[0])),
    )
    candidates: List[Dict[str, Any]] = []
    for text, count in ranked:
        if len(candidates) >= max_topics:
            break
        # Frequency >= 2 across records, or a specific multi-word phrase.
        if count < 2 and len(text.split()) < 2:
            continue
        sources = sorted(sources_by_topic.get(text) or [])
        candidates.append({
            "query": display.get(text) or text,
            "preferredSources": sources[:3],
            "provenance": "mined from archival metadata (subjects/keywords/places)",
        })
    return candidates
