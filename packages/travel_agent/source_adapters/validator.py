"""Fail-closed evidence validation for unverified source candidates."""

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import re

from ..research import _base_record
from .base import Adaptation, EVIDENCE_METHODS, LIMITS


BOUNDARIES = {"direct_quote", "summary", "inference"}
FACT_FIELDS = {"name", "title", "city", "kind"}
KINDS = {"place", "hotel", "dining", "transport", "activity"}
COORDINATES = re.compile(
    r"(?:纬度|latitude|lat)\s*[:：=]\s*([+-]?\d+(?:\.\d+)?)\s*[,，;；]\s*"
    r"(?:经度|longitude|lng|lon)\s*[:：=]\s*([+-]?\d+(?:\.\d+)?)", re.I)


def _limits(source_type):
    if source_type in {"hotel_official", "transport_authority"}:
        return "来源页面尚未经核验；价格/营业时间/预约政策以官方渠道核验结果为准。"
    return LIMITS


def _segment(document, key):
    if key in {"title", "content"}:
        return getattr(document, key)
    match = re.fullmatch(r"comments\[(\d{1,6})\]", key) if isinstance(key, str) else None
    if match and int(match[1]) < len(document.comments):
        return document.comments[int(match[1])]
    return None


def _support(document, claim):
    if not isinstance(claim, dict):
        return None
    quote, segment = claim.get("quote"), claim.get("segment")
    if not isinstance(segment, str):
        return None
    source = _segment(document, segment)
    if not isinstance(quote, str) or not quote.strip() or source is None or quote not in source:
        return None
    return {"quote": quote, "segment": segment}


def _field(document, claim, path, issues, fact=False):
    if claim is None:
        return None
    support = _support(document, claim)
    if support is None:
        issues.append(f"{path}: missing_verbatim_support")
        return None
    boundary, value = claim.get("boundary"), claim.get("value")
    if (not isinstance(boundary, str) or boundary not in BOUNDARIES
            or not isinstance(value, str) or not value.strip()):
        issues.append(f"{path}: invalid_value_or_boundary")
        return None
    if fact and boundary != "direct_quote":
        issues.append(f"{path}: non_literal_fact_rejected")
        return None
    if boundary == "direct_quote" and value not in support["quote"]:
        issues.append(f"{path}: value_not_in_quote")
        return None
    return value, boundary, support


def _facts(document, raw, issues):
    facts, boundaries, evidence = {}, {}, {}
    fields = raw.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
        issues.append("fields: invalid_object")
    for key in sorted(fields.keys() - FACT_FIELDS, key=str):
        issues.append(f"{key}: unsupported_fact_field")
    for key in sorted(FACT_FIELDS):
        result = _field(document, fields.get(key), key, issues, fact=True)
        facts[key] = result[0] if result else None
        if result and key == "kind" and result[0] not in KINDS:
            facts[key] = None
            issues.append("kind: unsupported_category")
            continue
        if result:
            _, boundaries[key], evidence[key] = result
    return facts, boundaries, evidence


def _location(document, raw, issues):
    claim = raw.get("coordinates")
    if claim is None:
        return None
    support = _support(document, claim)
    match = COORDINATES.fullmatch(support["quote"]) if support else None
    if not match or claim.get("boundary") not in (None, "direct_quote"):
        issues.append("coordinates: requires_literal_labeled_pair")
        return None
    lat, lng = float(match[1]), float(match[2])
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        issues.append("coordinates: out_of_range")
        return None
    return lat, lng, support


def _tips(document, raw, boundaries, evidence, issues):
    tips, claims = [], []
    values = raw.get("tips", [])
    if not isinstance(values, list):
        issues.append("tips: invalid_list")
        return tips, claims
    for index, claim in enumerate(values):
        result = _field(document, claim, f"tips[{index}]", issues)
        if not result:
            continue
        value, boundary, support = result
        path = f"practical_tips[{len(tips)}]"
        tips.append(f"推断（待核验）：{value}" if boundary == "inference" else value)
        boundaries[path], evidence[path] = boundary, support
        key, assertion = claim.get("fact_key"), claim.get("claim_value")
        if key is None and assertion is None:
            continue
        if (boundary != "inference" and isinstance(key, str) and key.strip()
                and isinstance(assertion, str) and assertion.strip()
                and assertion in support["quote"]):
            claims.append({"fact_key": key, "value": assertion, "tip_field": path})
        else:
            issues.append(f"tips[{index}]: unsupported_conflict_assertion")
    return tips, claims


def attach_provenance(document, source_type, boundaries, evidence):
    """Never trust model-supplied authors, source refs, excerpts or timestamps."""
    # Metadata direct_quote means copied from the caller's document envelope, not post text.
    for key in ("author_hint", "published_at", "captured_at", "language"):
        if getattr(document, key) is not None:
            boundaries[key] = "direct_quote"
            evidence[key] = {"quote": getattr(document, key), "segment": key}
    reference_field = "source_url" if document.url else "source_ref"
    boundaries[reference_field] = "direct_quote"
    evidence[reference_field] = {"quote": document.source_ref,
                                 "segment": "url" if document.url else "local_ref"}
    boundaries["checked_at"] = "direct_quote"
    evidence["checked_at"] = {"quote": document.captured_at, "segment": "captured_at"}
    excerpt = document.content or document.title or next(iter(document.comments), "")
    return {"source_type": source_type, "source_ref": document.source_ref,
            "author_hint": document.author_hint, "published_at": document.published_at,
            "captured_at": document.captured_at, "excerpt": excerpt[:200],
            "language": document.language, "corroboration_count": document.corroboration_count,
            "evidence_boundary": deepcopy(boundaries), "field_evidence": deepcopy(evidence),
            "limits": _limits(source_type)}


def _record(document, source_type, facts, boundaries, evidence):
    record = _base_record("", facts["name"], facts["title"], facts["kind"], document.url)
    # Reset legacy destination/currency defaults; never inherit Abu Dhabi/AED.
    record.update(facts)
    record.update(provider=facts["name"], tags=[], description=None,
                  checked_at=document.captured_at, constraints=[_limits(source_type)],
                  verification={"status": ("user_provided_unverified" if source_type == "user_screenshot"
                                           else "social_unverified"), "verified_fields": []},
                  price={"status": "unknown", "amount": None, "currency": None},
                  availability={"status": "unknown"}, booking_status="not_booked",
                  image_url=None, image_source_url=None)
    boundaries["provider"] = boundaries["name"]
    evidence["provider"] = deepcopy(evidence["name"])
    return record


def _candidate(document, raw, source_type, issues):
    if not isinstance(raw, dict):
        issues.append("candidate: invalid_object")
        return None
    for key in sorted(raw.keys() - {"fields", "coordinates", "tips"}, key=str):
        issues.append(f"{key}: unsupported_candidate_field")
    facts, boundaries, evidence = _facts(document, raw, issues)
    if not facts["name"]:
        issues.append("candidate: no_supported_name")
        return None
    record = _record(document, source_type, facts, boundaries, evidence)
    location = _location(document, raw, issues)
    if location:
        record.update(lat=location[0], lng=location[1], geo_status="source_stated_unverified")
        for key in ("lat", "lng"):
            boundaries[key], evidence[key] = "direct_quote", location[2]
    record["practical_tips"], record["source_claims"] = _tips(
        document, raw, boundaries, evidence, issues)
    expires = (date.fromisoformat(document.captured_at) + timedelta(days=90)).isoformat()
    record["tips_review"] = [{"field": f"practical_tips[{i}]", "expires_at": expires}
                             for i in range(len(record["practical_tips"]))]
    record["source_provenance"] = attach_provenance(document, source_type, boundaries, evidence)
    record["evidence"] = [{"source_url": document.url, "source_ref": document.source_ref,
                           "method": EVIDENCE_METHODS[source_type],
                           "supports": list(boundaries), "limits": _limits(source_type),
                           "checked_at": document.captured_at}]
    record["id"] = _candidate_id(record)
    return record


def _candidate_id(record):
    # Keep historical candidate IDs when v0.2 metadata is still unspecified.
    provenance = dict(record["source_provenance"])
    if provenance["language"] is None:
        provenance.pop("language")
    if provenance["corroboration_count"] == 0:
        provenance.pop("corroboration_count")
    payload = json.dumps({**record, "source_provenance": provenance},
                         ensure_ascii=False, sort_keys=True)
    return "source-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def normalize(document, extraction, source_type):
    """Strip unsupported fields, preserve valid evidence, report every rejection."""
    if source_type not in EVIDENCE_METHODS:
        raise ValueError("unsupported source type")
    if not isinstance(extraction, dict):
        return Adaptation([], "invalid_extraction", [])
    if extraction.get("is_travel") is not True:
        reason = extraction.get("reason")
        return Adaptation([], reason if isinstance(reason, str) and reason.strip() else "non_travel", [])
    if not isinstance(extraction.get("candidates"), list):
        return Adaptation([], "invalid_candidates", [])
    candidates, issues = [], []
    for index, raw in enumerate(extraction["candidates"]):
        local_issues = []
        candidate = _candidate(document, raw, source_type, local_issues)
        issues.extend(f"candidate[{index}].{issue}" for issue in local_issues)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return Adaptation(candidates, None if candidates else "no_supported_candidates", issues)
