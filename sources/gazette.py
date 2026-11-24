"""
sources/gazette.py

Assemble a lightweight newspaper-style edition from already-normalized
Time Machine items. No invented quotations, dates, or newspaper prose —
stories only reuse title, description, dateLabel, location, and source
fields the adapters already provide.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from .base import TimeMachineItem


def has_visual_media(item: TimeMachineItem) -> bool:
    return bool((item.imageUrl or item.thumbnailUrl) and item.title)


def item_to_story(item: TimeMachineItem) -> Dict[str, Any]:
    """JSON shape for the Gazette front page. All values come from the item."""
    return {
        "id": item.id,
        "title": item.title,
        "hook": item.description or "",
        "dateLabel": item.dateLabel or "",
        "location": item.location or "",
        "sourceInstitution": item.sourceInstitution or "",
        "sourceUrl": item.sourceUrl or "",
        "imageUrl": item.imageUrl or item.thumbnailUrl,
        "thumbnailUrl": item.thumbnailUrl or item.imageUrl,
        "sourceId": item.sourceId,
        "people": list(item.people or [])[:4],
        "topics": list(item.topics or [])[:6],
        "mediaKind": item.mediaKind,
        "mediaTemplate": item.mediaTemplate,
        "evidence": asdict(item.evidence) if item.evidence else None,
    }


def select_edition(items: List[TimeMachineItem]) -> Dict[str, Any]:
    """
    Pick a lead, a few secondary stories, and a small 'from the archives'
    strip. Prefers items that already have usable images. Returns empty
    sections (not fabricated fillers) when the pool is thin or a source
    is down.
    """
    visual = [i for i in items if has_visual_media(i)]
    lead = visual[0] if visual else None
    rest = visual[1:]
    secondary = rest[:4]
    archives = rest[4:8]
    return {
        "lead": item_to_story(lead) if lead else None,
        "secondary": [item_to_story(i) for i in secondary],
        "archives": [item_to_story(i) for i in archives],
        "available": bool(lead),
        "storyCount": (1 if lead else 0) + len(secondary) + len(archives),
    }
