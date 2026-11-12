"""Reapply app-owned semantic restrictions to normalized material evidence.

The material normalizer computes content_sha256 from the actual input content.
Review metadata supplied by models or caches is never an approval authority.
Restrictions retain literal evidence; downstream consumers must use usable_claim.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


_REGISTRY = json.loads(Path(__file__).with_name('detail_review_registry.json').read_text(
    encoding='utf-8'))['by_content_sha256']
_DETAIL_EVIDENCE = ('value', 'quote', 'segment', 'entity', 'conditions')
_GUIDE_EVIDENCE = ('action', 'place', 'time', 'cost', 'booking', 'caution',
                   'conditions', 'detail_ids')


def source_withdrawn(material):
    """App-owned exact-content withdrawal, shared by raw and ledger consumers.

    Raw content wins over a supplied fingerprint; normalized ledgers already
    carry the normalizer's content hash. A new document cannot inherit a veto.
    """
    if not isinstance(material, dict):
        return False
    content = next((material.get(key) for key in ('content', 'text', 'excerpt')
                    if isinstance(material.get(key), str) and material[key].strip()), None)
    fingerprint = (hashlib.sha256(json.dumps(content, ensure_ascii=False,
        sort_keys=True, allow_nan=False).encode()).hexdigest()
        if isinstance(content, str) else material.get('content_sha256'))
    rule = _REGISTRY.get(fingerprint) if isinstance(fingerprint, str) else None
    return isinstance(rule, dict) and rule.get('source_status') == 'withdrawn'


def usable_claim(row):
    """Whether an already normalized detail or guide is free of a review veto."""
    review = row.get('semantic_review') if isinstance(row, dict) else None
    return not isinstance(review, dict) or review.get('usable') is not False


def focus_materials(ledger):
    """Keep all source fields, but send repeated audit metadata only once.

    raw_evidence duplicates the original value/quote/entity/conditions verbatim;
    removing that duplicate from a prompt does not remove a claim or its source.
    The full normalized ledger remains the validation and audit authority.
    """
    materials = deepcopy([row for row in ledger.get('materials', []) if not source_withdrawn(row)])
    for material in materials:
        for row in _rows(material.get('details')) + _rows(material.get('guide_steps')):
            row.pop('raw_evidence', None)
            review = row.get('semantic_review')
            if isinstance(review, dict):
                row['semantic_review'] = {key: review[key] for key in
                    ('status', 'usable', 'effective_value', 'policy') if key in review}
                row['semantic_review']['review_ref'] = material.get('content_sha256')
    return materials


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _review(content_sha256, rule):
    return {
        'schema_version': 1, 'origin': 'application_registry',
        'status': 'withdrawn' if rule.get('source_status') == 'withdrawn' else 'unknown',
        'usable': False, 'effective_value': None,
        'policy': 'exclude_source' if rule.get('source_status') == 'withdrawn' else 'block_action_reference',
        'content_sha256': content_sha256,
        'blocked_fields': deepcopy(rule['blocked_fields']),
        'reason_codes': deepcopy(rule['reason_codes']), 'reason': rule['reason'],
        'reviewed_at': rule['reviewed_at'], 'evidence': deepcopy(rule['evidence']),
    }


def _block(row, review, evidence_keys):
    row['raw_evidence'] = {key: deepcopy(row[key]) for key in evidence_keys if key in row}
    row['semantic_review'] = deepcopy(review)


def _apply_material(material, details, guides, rule):
    review = _review(material['content_sha256'], rule)
    material['semantic_review'] = deepcopy(review)
    blocked_ids = set()
    withdrawn = rule.get('source_status') == 'withdrawn'
    for detail in details:
        if withdrawn or detail.get('field') in rule['blocked_fields']:
            _block(detail, review, _DETAIL_EVIDENCE)
            blocked_ids.update(detail[key] for key in ('id', 'local_id')
                               if isinstance(detail.get(key), str))
    for step in guides:
        refs = step.get('detail_ids')
        refs = refs if isinstance(refs, list) else []
        blocked_ref = any(isinstance(ref, str) and ref in blocked_ids for ref in refs)
        blocked_value = any(step.get(field) is not None for field in rule['blocked_fields'])
        if withdrawn or blocked_ref or blocked_value:
            _block(step, review, _GUIDE_EVIDENCE)


def apply_review(ledger):
    """Deep-copy a normalized ledger and rebuild reviews from exact content hashes.

    Call after both fresh extraction and cache normalization. A changed or absent
    content hash cannot inherit a review from a previous version of the material.
    Material review metadata describes the field restriction, not a blanket veto
    on other fields; usable_claim is intended for detail and guide rows.
    """
    result = deepcopy(ledger)
    for material in _rows(result.get('materials')):
        details, guides = _rows(material.get('details')), _rows(material.get('guide_steps'))
        for row in [material, *details, *guides]:
            row.pop('semantic_review', None)
        fingerprint = material.get('content_sha256')
        rule = _REGISTRY.get(fingerprint) if isinstance(fingerprint, str) else None
        if rule is not None:
            _apply_material(material, details, guides, rule)
    return result
