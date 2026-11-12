"""Offline source normalization contracts. No acquisition or production wiring."""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
from typing import Callable
from urllib.parse import urlsplit


CandidateAtom = dict
EVIDENCE_METHODS = {
    "xiaohongshu_post": "xiaohongshu_public_post",
    "instagram_post": "instagram_public_post",
    "blog_article": "blog_article",
    "user_screenshot": "user_screenshot",
    "tripadvisor_forum": "tripadvisor_forum",
    "reddit_post": "reddit_post",
    "google_maps_review": "google_maps_review",
    "hotel_official": "hotel_official",
    "transport_authority": "transport_authority",
}
LIMITS = "网友分享，非官方信息；价格/营业时间/预约政策以官方渠道为准。"


@dataclass(frozen=True)
class SourceDocument:
    """Caller-owned envelope; unknown language is null, unreported corroboration is 0.

    corroboration_count records caller-established independent corroboration.
    It is not inferred from this document, computed by the model, or a trust gate.
    """

    url: str | None
    title: str
    content: str
    comments: tuple[str, ...] = ()
    author_hint: str | None = None
    published_at: str | None = None
    captured_at: str = ""
    local_ref: str | None = None
    language: str | None = None
    corroboration_count: int = 0

    def __post_init__(self):
        for key in ("title", "content", "captured_at"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"{key} must be text")
        for key in ("url", "author_hint", "published_at", "local_ref"):
            value = getattr(self, key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be text or null")
        if self.language is not None and (
                not isinstance(self.language, str) or not self.language.strip()):
            raise ValueError("language must be nonblank text or null")
        if type(self.corroboration_count) is not int or self.corroboration_count < 0:
            raise ValueError("corroboration_count must be a nonnegative integer, not bool")
        if not isinstance(self.comments, (tuple, list)) or not all(
                isinstance(value, str) for value in self.comments):
            raise ValueError("comments must contain text")
        object.__setattr__(self, "comments", tuple(self.comments))
        # The caller supplies capture time: no clock or publication-date guessing.
        captured = date.fromisoformat(self.captured_at)
        if captured > date.max - timedelta(days=90):
            raise ValueError("captured_at must allow a 90-day review window")
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("url must be an http(s) reference without credentials")

    @property
    def source_ref(self):
        return self.url or self.local_ref

    def fingerprint(self):
        values = asdict(self)
        # Defaults preserve v0.1 replay keys; supplied v0.2 metadata remains bound.
        if self.language is None:
            values.pop("language")
        if self.corroboration_count == 0:
            values.pop("corroboration_count")
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Adaptation:
    candidates: list[CandidateAtom]
    reason: str | None
    issues: list[str]


Extractor = Callable[[SourceDocument, dict], dict]


class RecordedExtraction:
    """Replay caller-supplied model results, bound to the exact source document.

    Use this for the MVP offline pipeline. Any model/provider execution happens
    outside the adapter; no credentials, clients, or network calls live here.
    """

    def __init__(self, responses: dict[str, dict]):
        self._responses = deepcopy(responses)

    def __call__(self, document: SourceDocument, prompt: dict) -> dict:
        return deepcopy(self._responses.get(document.fingerprint(), {
            "is_travel": False, "reason": "missing_document_bound_extraction",
            "candidates": [],
        }))


class SourceAdapter(ABC):
    """Implement extract(), then share normalization and provenance validation.

    Extractors are pure local functions. Network-backed callables violate this
    contract; use RecordedExtraction to consume previously obtained results.
    """

    source_type: str

    @abstractmethod
    def extract(self, document: SourceDocument) -> dict:
        """Return structured extraction with per-field quoted support."""

    def adapt_with_report(self, document: SourceDocument) -> Adaptation:
        from .validator import normalize
        if not document.source_ref or not document.source_ref.strip():
            return Adaptation([], "missing_source_reference", [])
        return normalize(document, self.extract(document), self.source_type)

    def adapt(self, document: SourceDocument) -> list[CandidateAtom]:
        return self.adapt_with_report(document).candidates


def conflict_gaps(candidates: list[CandidateAtom]) -> list[dict]:
    """Different assertions for the same entity/key require official review.

    Conservative comparison, not semantic arbitration. Identity is exact name
    + city (case/whitespace normalized); absent cities trigger identity review.
    Claims are model-grouped tip assertions, never verified fact fields.
    """
    groups = {}
    for candidate in candidates:
        if not candidate.get("name"):
            continue
        identity = tuple(" ".join((candidate.get(k) or "").casefold().split())
                         for k in ("name", "city"))
        provenance = candidate["source_provenance"]
        for claim in candidate.get("source_claims", []):
            key = (*identity, claim["fact_key"])
            item = {**deepcopy(claim), "candidate_id": candidate["id"],
                    "source_ref": provenance["source_ref"],
                    "source_provenance": deepcopy(provenance)}
            groups.setdefault(key, []).append(item)
    gaps = []
    for key, claims in sorted(groups.items()):
        if len({c["source_ref"] for c in claims}) < 2 or len({c["value"] for c in claims}) < 2:
            continue
        gaps.append({"kind": "official_verification", "status": "open",
                     "entity": {"name": key[0], "city": key[1] or None}, "fact_key": key[2],
                     "identity_requires_review": not bool(key[1]),
                     "reason": "来源说法不同，保留全部说法，须官方核验；不按发布时间仲裁。",
                     "claims": claims})
    return gaps


def review_queue(candidates: list[CandidateAtom], as_of: str) -> list[dict]:
    """Return due tips; this is a staging queue, not a background scheduler."""
    today = date.fromisoformat(as_of)
    return [{"candidate_id": c["id"], **deepcopy(tip),
             "source_provenance": deepcopy(c["source_provenance"]), "status": "review_due"}
            for c in candidates for tip in c.get("tips_review", [])
            if date.fromisoformat(tip["expires_at"]) <= today]
