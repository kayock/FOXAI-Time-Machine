"""
sources/gallica.py

BnF Gallica SRU endpoint (https://gallica.bnf.fr/SRU) — the digital library
of the Bibliothèque nationale de France. No API key required.

SRU 1.2 searchRetrieve with responseSchema=dc returns records as
oai_dc:dc Dublin-Core XML (title, creator, date, description, identifier
 carrying an ark:, type). Parsed with stdlib xml.etree — namespaces are
matched by local name, so the exact SRU namespace URIs can drift without
breaking the parser.

Responses carry one dc:identifier per record of the form
`ark:/12148/<pid>`; the canonical page is https://gallica.bnf.fr/<ark> and
a same-ark IIIF Image URL is used for the thumbnail (documented Gallica
IIIF pattern; if an individual item has no image service the frontend
shows its normal graceful placeholder).

Shapes are taken from Gallica's published SRU documentation and common
client usage — NOT verified against a live capture from this environment
(the shared sandbox IP is refused by Gallica's edge), so treat the first
live run on your own machine as the confirmation step.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_text, FetchError, cache_get, cache_set

SRU_URL = "https://gallica.bnf.fr/SRU"
# dc:type values (lower-cased) → mediaKind.
TYPE_TO_MEDIA_KIND = {
    "image": "photograph",
    "still image": "photograph",
    "manuscript": "manuscript",
    "text": "document",
    "map": "map",
    "sound": "audio",
    "moving image": "moving_image",
}


def _localname(tag) -> str:
    if not isinstance(tag, str) or "}" not in tag:
        return tag or ""
    return tag.rsplit("}", 1)[-1]


def _dc_fields(dc_element) -> dict:
    """Map an oai_dc:dc element to {localname: [values]} — namespace-agnostic."""
    fields: dict = {}
    for child in dc_element:
        name = _localname(child.tag)
        if not name:
            continue
        fields.setdefault(name, []).append(child.text or "")
    return fields


def _ark_url(identifier: Optional[str]) -> Optional[str]:
    """Identifiers arrive as `ark:/12148/<pid>` (sometimes already absolute)."""
    if not identifier or not isinstance(identifier, str):
        return None
    identifier = identifier.strip()
    if identifier.startswith("http"):
        return identifier if "ark:/12148" in identifier else None
    if identifier.startswith("ark:/12148/"):
        return f"https://gallica.bnf.fr/{identifier}"
    return None


def _ark_pid(url: Optional[str]) -> Optional[str]:
    """Extract the ark pid (ark:/12148/...) from a Gallica URL for IIIF use."""
    if not url:
        return None
    marker = "ark:/12148/"
    if marker not in url:
        return None
    pid = url.split(marker, 1)[1].split("/")[0]
    return f"ark:/12148/{pid}" if pid else None


def normalize_gallica_record(record_element) -> Optional[TimeMachineItem]:
    """Normalize one srw:record element. Returns None (never raises) on a
    malformed or unusable record."""
    try:
        dc_element = None
        for child in record_element.iter():
            if _localname(child.tag) == "dc":
                dc_element = child
                break
        if dc_element is None:
            return None
        fields = _dc_fields(dc_element)

        title = clean_text((fields.get("title") or [""])[0], max_len=200)
        ark_url = _ark_url((fields.get("identifier") or [None])[0])
        if not title or not ark_url:
            return None

        dc_type = clean_text((fields.get("type") or [""])[0], max_len=60)
        media_kind = TYPE_TO_MEDIA_KIND.get((dc_type or "").lower(), "document")

        creators = [clean_text(c, max_len=80) for c in fields.get("creator", []) if clean_text(c, max_len=80)]
        description = clean_text((fields.get("description") or [""])[0])
        date_label = clean_text((fields.get("date") or [""])[0], max_len=80)
        publisher = clean_text((fields.get("publisher") or [""])[0], max_len=120)

        thumbnail_url = None
        pid = _ark_pid(ark_url)
        if pid:
            thumbnail_url = f"https://gallica.bnf.fr/iiif/{pid}/f1/full/!512,512/0/default.jpg"

        item = TimeMachineItem.build(
            source_id="gallica",
            source_item_id=pid or ark_url,
            canonical_url=ark_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=date_label,
            location=publisher,
            sourceInstitution="Bibliothèque nationale de France (Gallica)",
            thumbnailUrl=thumbnail_url,
            imageUrl=thumbnail_url,
            people=creators[:5],
            topics=[dc_type] if dc_type else [],
        )
        item.sourceUrl = ark_url
        item.evidence = Evidence(
            institution="Bibliothèque nationale de France (Gallica)",
            sourceItemId=pid or ark_url,
            sourceUrl=ark_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class GallicaSource(SourceAdapter):
    source_id = "gallica"
    display_name = "BnF Gallica (French national library)"
    requires_key = False
    deep_pagination = True  # SRU startRecord/maximumRecords
    api_docs_url = "https://gallica.bnf.fr/SRU"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        start_record = (page - 1) * limit + 1
        # Gallica's documented "all fields" query syntax: gallica all "<terms>"
        sru_query = f'gallica all "{query}"'
        params = {
            "operation": "searchRetrieve",
            "version": "1.2",
            "query": sru_query,
            "startRecord": start_record,
            "maximumRecords": limit,
            "responseSchema": "dc",
        }
        url = f"{SRU_URL}?{urlencode(params)}"

        cache_key = f"gallica:search:{urlencode({'q': query, 'limit': limit, 'page': page, 'media': media or ''})}"
        cached = cache_get(cache_key)
        if cached is not None:
            xml_text = cached
        else:
            try:
                xml_text = http_get_text(url, timeout=12.0)
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, xml_text)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        results = []
        for record_element in root.iter():
            if _localname(record_element.tag) == "record":
                normalized = normalize_gallica_record(record_element)
                if normalized:
                    results.append(normalized)
                if len(results) >= limit:
                    break
        return results
