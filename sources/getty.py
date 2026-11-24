"""
sources/getty.py

J. Paul Getty Museum Collection API (data.getty.edu) — Linked.Art JSON-LD.
No API key required.

The official REST surface deliberately exposes record-by-UUID retrieval and
a SPARQL endpoint, but NO keyword-list endpoint ("We currently don't
provide a way to get a list of all of the objects" — data.getty.edu docs).
Deep exploration is therefore implemented on the documented SPARQL query
service:

    GET https://data.getty.edu/museum/collection/sparql
        ?format=application/sparql-results+json
        &query=<SELECT ... ORDER BY ... LIMIT n OFFSET m>

Search matches object labels (rdfs:label) with CONTAINS over objects typed
E22_Human-Made_Object; ORDER BY label makes OFFSET stable, so pages of a
query can be walked deterministically and exhaustion is detectable.
Object details (title, accession, artist, date, IIIF image, rights) come
from the documented REST record endpoint
    GET https://data.getty.edu/museum/collection/object/<uuid>

Both shapes were verified against live responses captured in this
environment (e.g. "Irises", Van Gogh, 90.PA.20). Images are requested at a
bounded IIIF size (600px) — nothing large is downloaded. Open Content /
CC0 status is read from the record's rights section when present.
"""
from __future__ import annotations

from typing import List, Optional
import re
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, http_get_text, FetchError, cache_get, cache_set
from .discovery_state import parse_year as _parse_year

SPARQL_URL = "https://data.getty.edu/museum/collection/sparql"
OBJECT_URL = "https://data.getty.edu/museum/collection/object/{uuid}"
CANONICAL_URL = "https://www.getty.edu/art/collection/object/{uuid}"

SPARQL_TIMEOUT = 20.0   # the SPARQL endpoint answers text searches in ~10-20s
RECORD_TIMEOUT = 12.0
E22_TYPE = "http://www.cidoc-crm.org/cidoc-crm/E22_Human-Made_Object"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
CC0_IDS = (
    "http://creativecommons.org/publicdomain/zero/1.0",
    "https://creativecommons.org/publicdomain/zero/1.0",
)


def _sparql_search(query: str, limit: int, offset: int) -> Optional[List[str]]:
    """One page of object UUIDs whose label contains the search term,
    deterministically ordered by label. Returns None on transport/parse
    failure so callers can fail soft."""
    sql = (
        "SELECT ?obj ?label WHERE { "
        f"?obj <{RDF_TYPE}> <{E22_TYPE}> . "
        f"?obj <{RDFS_LABEL}> ?label . "
        f"FILTER(CONTAINS(LCASE(STR(?label)), \"{query.lower()}\")) "
        "} ORDER BY ASC(STR(?label)) "
        f"LIMIT {int(limit)} OFFSET {int(offset)}"
    )
    params = {"format": "application/sparql-results+json", "query": sql}
    url = f"{SPARQL_URL}?{urlencode(params)}"
    try:
        text = http_get_text(url, timeout=SPARQL_TIMEOUT)
        import json
        raw = json.loads(text)
        bindings = ((raw or {}).get("results") or {}).get("bindings") or []
        uuids = []
        for b in bindings:
            uri = ((b.get("obj") or {}).get("value") or "")
            if "/object/" in uri:
                uuids.append(uri.rsplit("/object/", 1)[1].split("/")[0])
        return uuids
    except FetchError:
        # Propagate transport/HTTP failures: the registry classifies them so
        # discovery never mistakes temporary trouble for an empty page.
        raise
    except (ValueError, KeyError, TypeError):
        raise FetchError(f"Malformed SPARQL response for: {term}", kind="malformed")


def _image_url(record: dict) -> Optional[str]:
    """representation[] carries full-size IIIF URLs; request a bounded 600px
    variant of the same IIIF asset instead of the full-size file."""
    for rep in record.get("representation") or []:
        url = rep.get("id") if isinstance(rep, dict) else None
        if isinstance(url, str) and "/iiif/image/" in url and url.endswith("default.jpg"):
            for full in ("/full/full/", "/full/90,/"):
                if full in url:
                    return url.replace(full, "/full/600,", 1)
            return url
    return None


_GENERIC_RIGHTS_LABELS = re.compile(r"rights statement|rights \(legal", re.I)


def _rights(record: dict) -> Optional[str]:
    """CC0 detection scans every classification row first (the CC0 URI row
    can sit behind generic 'Rights Statement' rows); otherwise a meaningful
    rights label, else None (caller shows the fallback text)."""
    for st in record.get("subject_to") or []:
        if not isinstance(st, dict):
            continue
        classified_rows = st.get("classified_as") or []
        for classified in classified_rows:
            if isinstance(classified, dict):
                lic_id = classified.get("id") or ""
                if any(lic_id.startswith(c) for c in CC0_IDS):
                    return "CC0 — Getty Open Content"
        for classified in classified_rows:
            if isinstance(classified, dict):
                label = clean_text(classified.get("_label"), max_len=80)
                if label and not _GENERIC_RIGHTS_LABELS.search(label):
                    return f"Rights: {label}"
    return None


def _label_of(identified: list, want: str) -> Optional[str]:
    """Value from identifier/name rows. In Getty's Linked.Art records the
    row's `content` carries the actual accession/title/date string, while
    `_label` labels the row itself (often generic, e.g. "Alternate Title")
    — so content is preferred."""
    for row in identified or []:
        if isinstance(row, dict) and row.get("type") == want:
            label = clean_text(row.get("content"), max_len=200) or clean_text(
                row.get("_label"), max_len=200)
            if label:
                return label
    return None


def _title_of(record: dict) -> Optional[str]:
    """Prefer the record's own Name identifier (clean title); the object
    _label sometimes carries a suffix like "Irises (90.PA.20)"."""
    named = _label_of(record.get("identified_by") or [], "Name")
    if named:
        return named
    label = clean_text(record.get("_label"), max_len=200)
    if not label:
        return None
    # Drop a trailing accession-number parenthetical, e.g. "… (84.XB.871.7.4)".
    cleaned = re.sub(r"\s*\(([^()]*\d[^()]*)\)\s*$", "", label)
    return cleaned or label


def _date_of(record: dict) -> Optional[str]:
    """Timespan identifiers carry the display date ("1889"); the timespan
    _label is often a generic "Dates"."""
    produced = record.get("produced_by") or {}
    if not isinstance(produced, dict):
        return None
    timespan = produced.get("timespan") or {}
    if not isinstance(timespan, dict):
        return None
    named = _label_of(timespan.get("identified_by") or [], "Name")
    if named and not _GENERIC_DATE_LABELS.match(named) and _parse_year(named) is not None:
        return named
    label = clean_text(timespan.get("_label"), max_len=80)
    if label and not _GENERIC_DATE_LABELS.match(label) and _parse_year(label) is not None:
        return label
    return None


_GENERIC_DATE_LABELS = re.compile(r"^(dates?|period|production dates?)$", re.I)


def normalize_getty_record(record: dict) -> Optional[TimeMachineItem]:
    """Normalize one Linked.Art object record. Returns None (never raises)
    for malformed or imageless records."""
    try:
        record_id = record.get("id") or ""
        uuid = record_id.rsplit("/object/", 1)[-1].split("/")[0] if "/object/" in record_id else None
        title = _title_of(record)
        image = _image_url(record)
        if not uuid or not title or not image:
            return None

        identified = record.get("identified_by") or []
        accession = _label_of(identified, "Identifier")  # first Identifier = accession
        artist = None
        produced = record.get("produced_by") or {}
        for creator in produced.get("carried_out_by") or []:
            if isinstance(creator, dict) and creator.get("_label"):
                artist = clean_text(creator.get("_label"), max_len=80)
                break
        date_label = _date_of(record)

        department = None
        for member in record.get("member_of") or []:
            if isinstance(member, dict):
                label = clean_text(member.get("_label"), max_len=120)
                if label and "Exhibited" not in label:
                    department = label
                    break

        canonical_url = CANONICAL_URL.format(uuid=uuid)
        rights = _rights(record)
        item = TimeMachineItem.build(
            source_id="getty",
            source_item_id=uuid,
            canonical_url=canonical_url,
            title=title,
            media_kind="artwork",
            description=f"Accession {accession}" if accession else None,
            dateLabel=date_label,
            sourceInstitution="J. Paul Getty Museum",
            thumbnailUrl=image,
            imageUrl=image,
            people=[artist] if artist else [],
            topics=[department] if department else [],
            rights=rights or "Rights on object record",
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="J. Paul Getty Museum",
            sourceItemId=uuid,
            sourceUrl=canonical_url,
            collection=accession,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class GettySource(SourceAdapter):
    source_id = "getty"
    display_name = "J. Paul Getty Museum"
    requires_key = False
    # SPARQL OFFSET/ORDER BY gives stable, walkable pages; exhaustion is
    # detectable when a page comes back short.
    deep_pagination = True
    api_docs_url = "https://data.getty.edu/museum/collection/docs"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        term = (query or "").strip()
        if not term:
            return []
        if any(ch in term for ch in "\"'\\;{}") or len(term) > 80:
            # Keep the SPARQL FILTER literal simple and bounded.
            term = "".join(ch for ch in term if ch not in "\"'\\;{}")[:80].strip() or "history"
        offset = (page - 1) * limit

        cache_key = f"getty:page:{urlencode({'q': term, 'limit': limit, 'offset': offset})}"
        uuids = cache_get(cache_key)
        if uuids is None:
            uuids = _sparql_search(term, limit * 2, offset) or []
            cache_set(cache_key, uuids)

        results: List[TimeMachineItem] = []
        for uuid in uuids:
            if len(results) >= limit:
                break
            obj_cache = f"getty:object:{uuid}"
            record = cache_get(obj_cache)
            if record is None:
                try:
                    record = http_get_json(OBJECT_URL.format(uuid=uuid), timeout=RECORD_TIMEOUT)
                except FetchError:
                    continue
                cache_set(obj_cache, record)
            normalized = normalize_getty_record(record or {})
            if normalized:
                results.append(normalized)
        return results
