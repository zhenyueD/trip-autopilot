"""Offline source adapters; not connected to the production organizer."""

from .base import (Adaptation, CandidateAtom, RecordedExtraction, SourceAdapter,
                   SourceDocument, conflict_gaps, review_queue)
from .xiaohongshu import XiaohongshuAdapter, extraction_prompt
from .transport_authority import TransportAuthorityAdapter
from .hotel_official import HotelOfficialAdapter
from .tripadvisor_forum import TripAdvisorForumAdapter
from .google_maps_review import GoogleMapsReviewAdapter

__all__ = ["Adaptation", "CandidateAtom", "RecordedExtraction", "SourceAdapter",
           "SourceDocument", "XiaohongshuAdapter", "conflict_gaps", "review_queue",
           "extraction_prompt", "TransportAuthorityAdapter", "HotelOfficialAdapter",
           "TripAdvisorForumAdapter", "GoogleMapsReviewAdapter"]
