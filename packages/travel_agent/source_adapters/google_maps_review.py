"""Offline Google Maps UGC adapter; no client, key lookup or request entry point.

Real Places acquisition is not authorized. Caller-owned, previously obtained
documents and pure local extractors are the only inputs; there is no API wrapper
or boolean switch capable of enabling paid requests here.
"""

import hashlib
import json

from .base import Extractor, SourceAdapter, SourceDocument
from .xiaohongshu import extraction_prompt


def review_documents(payload: dict, *, captured_at: str) -> list[SourceDocument]:
    """Split compact or Places New shaped local data; no URL or translation guesses.

    At most 100 reviews per batch; malformed batches raise ValueError without
    returning partial results. Missing review URLs use local references, never
    invented review links or the whole place's URL. No HTTP status is inferred.
    """
    if not isinstance(payload, dict):
        raise ValueError("place payload must be an object")
    place_id = payload.get("place_id", payload.get("id"))
    if not isinstance(place_id, str) or not place_id.strip():
        raise ValueError("place_id or id is required")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or len(reviews) > 100:
        raise ValueError("reviews must be a list of at most 100 items")
    if not isinstance(payload.get("displayName", {}), dict):
        raise ValueError("displayName must be an object")
    documents = []
    compact_title = payload.get("name", "") if "place_id" in payload else ""
    title = payload.get("displayName", {}).get("text", compact_title)
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("authorAttribution", {}), dict):
            raise ValueError("review and authorAttribution must be objects")
        text = review.get("originalText", review.get("text", ""))
        content = text.get("text", "") if isinstance(text, dict) else text
        language = text.get("languageCode") if isinstance(text, dict) else review.get("language")
        identity = json.dumps([place_id, review],
                              ensure_ascii=False, sort_keys=True)
        local_ref = "gmaps-review:" + hashlib.sha256(identity.encode()).hexdigest()
        documents.append(SourceDocument(
            url=review.get("googleMapsUri"), local_ref=local_ref, title=title,
            content=content, language=language,
            author_hint=review.get("authorAttribution", {}).get("displayName", review.get("author")),
            published_at=review.get("publishTime", review.get("published_at")),
            captured_at=captured_at,
        ))
    return documents


class GoogleMapsReviewAdapter(SourceAdapter):
    source_type = "google_maps_review"

    def __init__(self, extractor: Extractor):
        self._extractor = extractor

    def extract(self, document: SourceDocument) -> dict:
        prompt = extraction_prompt(document)
        prompt["instructions"] += (
            "\nThis is one Google Maps traveler review, not official place data. "
            "Keep its author and original language bound to this review. "
            "Ratings do not establish truth; contradictory reviews need official verification."
        )
        return self._extractor(document, prompt)
