"""
sources/dpla.py

Digital Public Library of America API (https://api.dp.la/v2/items). Requires
a free DPLA API key (see config.example.json). Field shapes below
(sourceResource.title/description/date, object as thumbnail, isShownAt as
the canonical page, dataProvider/provider) are taken from DPLA's published
API documentation — NOT verified against a live capture, since no key is
configured in this environment.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://api.dp.la/v2/items"
KEY_NAME = "DPLA_API_KEY"


def normalize_dpla_item(doc: dict) -> Optional[TimeMachineItem]:
    try:
        source = doc.get("sourceResource") or {}
        title_field = source.get("title")
        title = clean_text(title_field[0] if isinstance(title_field, list) else title_field, max_len=200)
        canonical_url = doc.get("isShownAt")
        doc_id = doc.get("id")
        if not title or not canonical_url or not doc_id:
            return None

        description_field = source.get("description")
        description = clean_text(description_field[0] if isinstance(description_field, list) else description_field)

        date_field = source.get("date") or {}
        date_label = date_field.get("displayDate") if isinstance(date_field, dict) else None

        provider = (doc.get("provider") or {}).get("name")
        data_provider = (doc.get("dataProvider") or None)
        if isinstance(data_provider, dict):
            data_provider = data_provider.get("name")

        media_type_field = source.get("type")
        media_type = media_type_field[0] if isinstance(media_type_field, list) else media_type_field
        media_kind = {
            "image": "photograph", "text": "document", "physical object": "artifact",
            "moving image": "moving_image", "sound": "audio",
        }.get((media_type or "").lower(), "document")

        item = TimeMachineItem.build(
            source_id="dpla",
            source_item_id=doc_id,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=date_label,
            sourceInstitution=data_provider or provider or "DPLA partner institution",
            thumbnailUrl=doc.get("object"),
            topics=source.get("subject", []) if isinstance(source.get("subject"), list) else [],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(institution=data_provider or "DPLA", provider=provider,
                                  sourceItemId=doc_id, sourceUrl=canonical_url)
        return item
    except Exception:  # noqa: BLE001
        return None


class DplaSource(SourceAdapter):
    source_id = "dpla"
    display_name = "Digital Public Library of America"
    requires_key = True
    deep_pagination = True  # documented page/page_size parameters
    api_docs_url = "https://pro.dp.la/developers/api-codes"

    def availability(self) -> SourceAvailability:
        if not get_key(KEY_NAME):
            return SourceAvailability(False, "API key not configured")
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        key = get_key(KEY_NAME)
        if not key:
            return []
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        params = {"q": query, "page_size": limit, "page": page, "api_key": key}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"dpla:search:{urlencode({'q': query, 'page_size': limit, 'page': page})}"
        cached = cache_get(cache_key)
        if cached is not None:
            raw = cached
        else:
            try:
                raw = http_get_json(url)
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, raw)

        docs = (raw or {}).get("docs") or []
        results = []
        for doc in docs:
            normalized = normalize_dpla_item(doc)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
