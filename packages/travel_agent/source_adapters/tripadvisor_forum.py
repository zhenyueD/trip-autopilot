"""Offline adapter for caller-shared forum text; no collection or verification."""

from .base import Extractor, SourceAdapter, SourceDocument
from .xiaohongshu import extraction_prompt as shared_extraction_prompt


def extraction_prompt(document: SourceDocument) -> dict:
    """Reuse the shared evidence protocol, adding forum acquisition limits."""
    prompt = shared_extraction_prompt(document)
    prompt.update(source_type="tripadvisor_forum", collection_policy="user_shared_only")
    prompt["instructions"] += (
        "\nTripAdvisor forum input is caller-shared text only. Do not fetch or crawl. "
        "A source URL is attribution, never HTTP validation or a clickable-link permit. "
        "Unknown author_hint stays null; never infer an author from comments."
    )
    return prompt


class TripAdvisorForumAdapter(SourceAdapter):
    source_type = "tripadvisor_forum"

    def __init__(self, extractor: Extractor):
        self._extractor = extractor

    def extract(self, document: SourceDocument) -> dict:
        return self._extractor(document, extraction_prompt(document))
