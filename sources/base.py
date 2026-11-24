"""
sources/base.py

The normalized Time Machine item, stable identity rules, the mixed-media
template registry, and the SourceAdapter contract every adapter implements.

Kept deliberately small: real archive data is messy, so almost every field
here is optional except identity/title/source. Standard library only.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------------------
# Stable identity
# ---------------------------------------------------------------------------

def make_stable_id(source_id: str, source_item_id: Optional[str], canonical_url: Optional[str]) -> str:
    """
    Remote items must keep the same local id across feed refreshes so
    favorites/saves keep working (see README section "Stable identities").

    Preferred: sourceId + sourceItemId.
    Fallback: a deterministic hash of the canonical source URL.
    Never: a random id.
    """
    if source_item_id:
        raw = f"{source_id}:{source_item_id}"
    elif canonical_url:
        raw = f"{source_id}:{canonical_url}"
    else:
        raise ValueError("make_stable_id requires either source_item_id or canonical_url")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{source_id}-{digest}"


# ---------------------------------------------------------------------------
# Mixed-media template registry (section 7/8 of the brief)
# ---------------------------------------------------------------------------
# Each template says how a normalized item should be *presented*: what its
# classification badge says, what its primary action button says, and
# whether that action opens the institution externally or opens the Gazette.

TEMPLATES: Dict[str, Dict[str, Any]] = {
    # --- source-media templates ---
    "moving-image-source": {
        "classificationDefault": "PERIOD FOOTAGE",
        "primaryAction": "Watch at Institution",
        "external": True,
        "showsDuration": True,
        "autoplay": False,
    },
    "later-documentary": {
        "classificationDefault": "LATER DOCUMENTARY",
        "primaryAction": "Watch at Institution",
        "external": True,
        "showsDuration": True,
        "autoplay": False,
    },
    "audio-oral-history": {
        "classificationDefault": "ORAL HISTORY",
        "primaryAction": "Listen at Institution",
        "external": True,
        "showsDuration": True,
        "autoplay": False,
    },
    "original-document": {
        "classificationDefault": "ORIGINAL DOCUMENT",
        "primaryAction": "View Original at Institution",
        "external": True,
        "showsDuration": False,
        "autoplay": False,
    },
    "historical-photograph": {
        "classificationDefault": None,
        "primaryAction": "View Original at Institution",
        "external": True,
        "showsDuration": False,
        "autoplay": False,
    },
    "artifact-object": {
        "classificationDefault": None,
        "primaryAction": "View at Institution",
        "external": True,
        "showsDuration": False,
        "autoplay": False,
    },
    "map": {
        "classificationDefault": None,
        "primaryAction": "View Original at Institution",
        "external": True,
        "showsDuration": False,
        "autoplay": False,
    },
    "artwork-illustration": {
        "classificationDefault": None,
        "primaryAction": "View Original at Institution",
        "external": True,
        "showsDuration": False,
        "autoplay": False,
    },
    # --- Time Machine-authored templates ---
    "reconstructed-story": {
        "classificationDefault": "HISTORICAL RECONSTRUCTION",
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
    "photo-story": {
        "classificationDefault": None,
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
    "infographic": {
        "classificationDefault": "FOXAI INFOGRAPHIC",
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
    "timeline": {
        "classificationDefault": "TIMELINE",
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
    "then-and-now": {
        "classificationDefault": "THEN-AND-NOW COMPARISON",
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
    "compare-perspectives": {
        "classificationDefault": "COMPARE PERSPECTIVES",
        "primaryAction": "Open Gazette",
        "external": False,
        "showsDuration": False,
        "autoplay": False,
    },
}

# mediaKind -> template. This is the *default* mapping used when a source
# adapter doesn't have a more specific reason to pick a different template
# (e.g. NASA video vs NASA photo, or a document that is actually a map).
MEDIA_KIND_TO_TEMPLATE = {
    "photograph": "historical-photograph",
    "artwork": "artwork-illustration",
    "illustration": "artwork-illustration",
    "map": "map",
    "document": "original-document",
    "manuscript": "original-document",
    "letter": "original-document",
    "artifact": "artifact-object",
    "object": "artifact-object",
    "moving_image": "moving-image-source",
    "video": "moving-image-source",
    "documentary": "later-documentary",
    "audio": "audio-oral-history",
    "oral_history": "audio-oral-history",
}


def choose_template(media_kind: str, media_classification: Optional[str] = None) -> str:
    """
    Pick a template id for a normalized item. A fossil like Dracorex or SUE
    uses artifact-object with a classification subtype rather than a
    parallel "fossil" template system (see brief section 7).
    """
    key = (media_kind or "").strip().lower()
    return MEDIA_KIND_TO_TEMPLATE.get(key, "artifact-object")


def template_info(template_id: str) -> Dict[str, Any]:
    return TEMPLATES.get(template_id, TEMPLATES["artifact-object"])


# ---------------------------------------------------------------------------
# Normalized Time Machine item
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    institution: Optional[str] = None
    collection: Optional[str] = None
    sourceItemId: Optional[str] = None
    sourceUrl: Optional[str] = None
    manifestUrl: Optional[str] = None
    provider: Optional[str] = None
    extra: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class TimeMachineItem:
    id: str
    sourceId: str
    sourceItemId: Optional[str]
    sourceUrl: Optional[str]

    title: str
    dateLabel: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None

    sourceInstitution: Optional[str] = None
    collection: Optional[str] = None

    mediaKind: str = "document"          # photograph, document, artifact, map, moving_image, audio, artwork...
    mediaClassification: Optional[str] = None  # e.g. "PERIOD FOOTAGE" — falls back to template default
    mediaTemplate: str = "artifact-object"

    thumbnailUrl: Optional[str] = None
    imageUrl: Optional[str] = None
    iiifManifestUrl: Optional[str] = None
    audioUrl: Optional[str] = None

    duration: Optional[str] = None

    rights: Optional[str] = None
    contextStatus: str = "not-required"   # not-required | required | required-pending

    people: List[str] = field(default_factory=list)
    places: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)

    evidence: Optional[Evidence] = None
    relatedLinks: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TimeMachineItem":
        """Rebuild an item from to_dict() output. Unknown keys are ignored
        so a newer payload can still be read by this version."""
        raw = dict(data or {})
        evidence_raw = raw.pop("evidence", None)
        evidence = None
        if isinstance(evidence_raw, dict):
            known_ev = set(Evidence.__dataclass_fields__)
            evidence = Evidence(**{k: v for k, v in evidence_raw.items() if k in known_ev})
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in raw.items() if k in known}
        item = cls(**kwargs)
        item.evidence = evidence
        return item

    @staticmethod
    def build(*, source_id: str, source_item_id: Optional[str], canonical_url: Optional[str],
              title: str, media_kind: str = "document", media_classification: Optional[str] = None,
              **kwargs) -> "TimeMachineItem":
        item_id = make_stable_id(source_id, source_item_id, canonical_url)
        template = choose_template(media_kind, media_classification)
        return TimeMachineItem(
            id=item_id,
            sourceId=source_id,
            sourceItemId=source_item_id,
            sourceUrl=canonical_url,
            title=title,
            mediaKind=media_kind,
            mediaClassification=media_classification,
            mediaTemplate=template,
            **kwargs,
        )


def normalize_page(page) -> int:
    """A 1-based page index. Invalid input becomes page 1. This does not
    clamp a high page back down onto some earlier page — that repeated the
    last in-window page forever."""
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 1
    return 1 if number < 1 else number


def page_in_window(page, maximum: Optional[int]) -> Optional[int]:
    """Page to request, or None when `page` is past a documented provider
    result window. Callers must not refetch the last allowed page in that
    case — returning None means 'stop, do not repeat'."""
    number = normalize_page(page)
    if maximum is not None and number > int(maximum):
        return None
    return number


def clean_text(value: Optional[str], max_len: int = 600) -> Optional[str]:
    """Collapse whitespace and cap length — remote descriptions are messy."""
    if not value or not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return None
    return value[:max_len]


# ---------------------------------------------------------------------------
# SourceAdapter contract
# ---------------------------------------------------------------------------

class SourceAvailability:
    def __init__(self, available: bool, reason: Optional[str] = None):
        self.available = available
        self.reason = reason

    def to_dict(self):
        return {"available": self.available, "reason": self.reason}


class SourceAdapter:
    """
    Every source adapter implements this contract. A source adapter must
    never raise out of search()/availability() in a way that crashes the
    service — the registry catches exceptions defensively too, but adapters
    should fail soft internally (return [] / SourceAvailability(False, ...))
    wherever practical.

    Whether this adapter walks real provider pagination (page/offset/
    cursor). Adapters that cannot verify pagination support declare False
    explicitly — the capability matrix reports it honestly.
    """
    source_id: str = "base"
    display_name: str = "Base Source"
    requires_key: bool = False
    deep_pagination: bool = False

    def availability(self) -> SourceAvailability:
        raise NotImplementedError

    def search(self, query: str, media: Optional[str] = None, limit: int = 10) -> List[TimeMachineItem]:
        raise NotImplementedError
