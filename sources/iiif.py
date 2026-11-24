"""
sources/iiif.py

Generic IIIF Presentation API adapter. Time Machine treats IIIF as a core
standard (brief section 4) rather than writing a bespoke adapter per
IIIF-publishing institution: give this module any manifest URL and it
extracts a normalized TimeMachineItem.

Supports the common shape of both Presentation 2.x and 3.0 manifests
(label, description/summary, metadata, thumbnail, canvases, image
services, attribution/provider, rights, homepage/related links, duration).
Every field is read defensively — a manifest is not required to contain
every possible IIIF property.
"""
from __future__ import annotations

from typing import List, Optional, Dict, Any

from .base import TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set


def _lang_map_to_string(value: Any) -> Optional[str]:
    """
    IIIF v3 uses language maps: {"en": ["Some Title"]}.
    IIIF v2 often uses a plain string, or a list of {"@value", "@language"}.
    Handle all three without raising.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        # v3 language map — prefer "en", else "none", else first available
        for lang_key in ("en", "none"):
            if lang_key in value and isinstance(value[lang_key], list) and value[lang_key]:
                return clean_text(" ".join(str(v) for v in value[lang_key]))
        for v in value.values():
            if isinstance(v, list) and v:
                return clean_text(" ".join(str(x) for x in v))
    if isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, dict) and "@value" in entry:
                parts.append(str(entry["@value"]))
            elif isinstance(entry, str):
                parts.append(entry)
        if parts:
            return clean_text(" ".join(parts))
    return None


def _extract_thumbnail(manifest: Dict[str, Any]) -> Optional[str]:
    thumb = manifest.get("thumbnail")
    if isinstance(thumb, list) and thumb:
        entry = thumb[0]
        return entry.get("id") or entry.get("@id") if isinstance(entry, dict) else None
    if isinstance(thumb, dict):
        return thumb.get("id") or thumb.get("@id")
    if isinstance(thumb, str):
        return thumb
    return None


def _extract_canvas_body(manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Walk to the first canvas's painting body and return it whole (id, type,
    format) — not just a URL — so the caller can tell an image from a video
    from an audio recording instead of guessing from unrelated fields.
    v3: items[0] (canvas) -> items[0] (AnnotationPage) -> items[0]
        (Annotation) -> body {id, type, format}
    v2: sequences[0].canvases[0].images[0].resource {@id, @type, format}
    """
    try:
        items = manifest.get("items")
        if isinstance(items, list) and items:
            canvas = items[0]
            canvas_items = canvas.get("items") if isinstance(canvas, dict) else None
            if isinstance(canvas_items, list) and canvas_items:
                anno_page = canvas_items[0]
                page_items = anno_page.get("items") if isinstance(anno_page, dict) else None
                if isinstance(page_items, list) and page_items:
                    body = page_items[0].get("body") if isinstance(page_items[0], dict) else None
                    if isinstance(body, dict):
                        return {
                            "id": body.get("id") or body.get("@id"),
                            "type": body.get("type") or body.get("@type"),
                            "format": body.get("format"),
                            "duration": body.get("duration"),
                        }
    except Exception:  # noqa: BLE001
        pass
    try:
        sequences = manifest.get("sequences")
        if isinstance(sequences, list) and sequences:
            canvases = sequences[0].get("canvases")
            if isinstance(canvases, list) and canvases:
                images = canvases[0].get("images")
                if isinstance(images, list) and images:
                    resource = images[0].get("resource")
                    if isinstance(resource, dict):
                        return {
                            "id": resource.get("@id") or resource.get("id"),
                            "type": resource.get("@type") or resource.get("type"),
                            "format": resource.get("format"),
                            "duration": resource.get("duration"),
                        }
    except Exception:  # noqa: BLE001
        pass
    return None


# IIIF v3 body "type" values and v2 "@type" (dctypes:*) values, mapped to a
# Time Machine mediaKind. Checked before falling back to MIME sniffing.
_BODY_TYPE_TO_MEDIA_KIND = {
    "image": "photograph",
    "dctypes:image": "photograph",
    "video": "moving_image",
    "movingimage": "moving_image",
    "dctypes:movingimage": "moving_image",
    "sound": "audio",
    "dctypes:sound": "audio",
    "text": "manuscript",
    "dctypes:text": "manuscript",
}


def _media_kind_from_body(body: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Determine media kind from the canvas body's declared type, falling back
    to its MIME format prefix. Returns None (caller decides the default)
    rather than guessing from unrelated signals like whether a duration is
    present — a manifest can carry a duration on an image canvas's metadata
    without the canvas itself being time-based media, so duration alone is
    not evidence of video (this was the bug being fixed here).
    """
    if not body:
        return None
    body_type = (body.get("type") or "").strip().lower()
    if body_type in _BODY_TYPE_TO_MEDIA_KIND:
        return _BODY_TYPE_TO_MEDIA_KIND[body_type]
    fmt = (body.get("format") or "").strip().lower()
    if fmt.startswith("image/"):
        return "photograph"
    if fmt.startswith("video/"):
        return "moving_image"
    if fmt.startswith("audio/"):
        return "audio"
    if fmt.startswith("text/") or fmt == "application/pdf":
        return "manuscript"
    return None


def _extract_first_canvas_image(manifest: Dict[str, Any]) -> Optional[str]:
    """Backwards-compatible helper: just the body's URL, image or not."""
    body = _extract_canvas_body(manifest)
    return body.get("id") if body else None


def _extract_duration(manifest: Dict[str, Any]) -> Optional[str]:
    try:
        items = manifest.get("items")
        if isinstance(items, list) and items:
            duration = items[0].get("duration")
            if isinstance(duration, (int, float)):
                minutes, seconds = divmod(int(duration), 60)
                return f"{minutes}:{seconds:02d}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _extract_provider(manifest: Dict[str, Any]) -> Optional[str]:
    provider = manifest.get("provider")
    if isinstance(provider, list) and provider:
        return _lang_map_to_string(provider[0].get("label")) if isinstance(provider[0], dict) else None
    attribution = manifest.get("attribution")
    if attribution:
        return _lang_map_to_string(attribution)
    required = manifest.get("requiredStatement")
    if isinstance(required, dict):
        return _lang_map_to_string(required.get("value"))
    return None


def _extract_homepage(manifest: Dict[str, Any]) -> Optional[str]:
    homepage = manifest.get("homepage")
    if isinstance(homepage, list) and homepage:
        entry = homepage[0]
        return entry.get("id") or entry.get("@id") if isinstance(entry, dict) else None
    related = manifest.get("related")
    if isinstance(related, dict):
        return related.get("@id")
    if isinstance(related, str):
        return related
    return None


def _extract_metadata_pairs(manifest: Dict[str, Any]) -> List[Dict[str, str]]:
    pairs = []
    for entry in manifest.get("metadata") or []:
        if not isinstance(entry, dict):
            continue
        label = _lang_map_to_string(entry.get("label"))
        value = _lang_map_to_string(entry.get("value"))
        if label and value:
            pairs.append({"label": label, "value": value})
    return pairs


def normalize_iiif_manifest(manifest: Dict[str, Any], manifest_url: str) -> Optional[TimeMachineItem]:
    """Normalize a IIIF manifest dict into a TimeMachineItem. Returns None
    (never raises) if the manifest is too malformed to produce a usable item
    (per brief section 22: 'IIIF manifest malformed -> report item
    unavailable without crashing feed')."""
    try:
        manifest_id = manifest.get("id") or manifest.get("@id") or manifest_url
        title = _lang_map_to_string(manifest.get("label"))
        if not title:
            return None

        description = _lang_map_to_string(manifest.get("summary") or manifest.get("description"))
        thumbnail = _extract_thumbnail(manifest)
        body = _extract_canvas_body(manifest)
        image = body.get("id") if body and _media_kind_from_body(body) == "photograph" else None
        audio_url = body.get("id") if body and _media_kind_from_body(body) == "audio" else None
        video_url = body.get("id") if body and _media_kind_from_body(body) == "moving_image" else None
        duration = _extract_duration(manifest)
        provider = _extract_provider(manifest)
        rights = manifest.get("rights")
        if isinstance(rights, list):
            rights = rights[0] if rights else None
        homepage = _extract_homepage(manifest)
        canonical_url = homepage or manifest_id

        # Media kind comes from the canvas body's own declared type/format —
        # never inferred from unrelated signals like "a duration field is
        # present" (that was the bug: a manifest can carry timing metadata
        # without the canvas actually being time-based media). If the body
        # doesn't say, fall back safely to "manuscript" rather than guessing
        # video or audio.
        media_kind = _media_kind_from_body(body) or ("photograph" if thumbnail else "manuscript")

        metadata_pairs = _extract_metadata_pairs(manifest)
        date_label = next((p["value"] for p in metadata_pairs if p["label"].lower() in ("date", "date created")), None)
        location = next((p["value"] for p in metadata_pairs if p["label"].lower() in ("origin", "place", "location")), None)

        item = TimeMachineItem.build(
            source_id="iiif",
            source_item_id=None,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=date_label,
            location=location,
            sourceInstitution=provider,
            thumbnailUrl=thumbnail,
            imageUrl=image,
            iiifManifestUrl=manifest_url,
            audioUrl=audio_url,
            duration=duration if media_kind in ("audio", "moving_image") else None,
            rights=rights,
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(institution=provider, manifestUrl=manifest_url,
                                  provider=provider, sourceUrl=canonical_url)
        if video_url and not item.imageUrl:
            item.relatedLinks.append({"label": "Video", "url": video_url})
        return item
    except Exception:  # noqa: BLE001
        return None


def fetch_and_normalize_iiif_manifest(manifest_url: str) -> Optional[TimeMachineItem]:
    """Fetch a supplied manifest URL and normalize it. Returns None on any
    network or parse failure — never raises out to the caller."""
    cache_key = f"iiif:manifest:{manifest_url}"
    cached = cache_get(cache_key)
    if cached is not None:
        manifest = cached
    else:
        try:
            manifest = http_get_json(manifest_url)
        except FetchError:
            return None
        cache_set(cache_key, manifest)
    if not isinstance(manifest, dict):
        return None
    return normalize_iiif_manifest(manifest, manifest_url)
