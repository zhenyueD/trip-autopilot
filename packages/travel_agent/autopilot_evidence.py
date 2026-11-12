"""Pure adaptation of one delegate response to the existing material ledger.

No model, network or state writer lives here. Literal checks are traceability;
applicability and semantic associations retain the existing unverified labels.
"""
from copy import deepcopy

from .material_details import (FIELDS, MAX_CONTENT_CHARS, MAX_DETAILS, MAX_MATERIALS,
    VERSION, PROMPT, CORROBORATION_PROMPT, _hash, _normalize, corroborate_material_details)


DELEGATE_STRUCTURE_PROMPT = '''
For evidence/materials/counterexamples tasks, also return optional structured:
{"materials":[{"source_id":"exact input evidence id", "source":{},
"details":[],"guide_steps":[]}], "groups":[]}.
Each materials entry follows the extraction contract below; apply it to that
input record's content/text, not to search summaries or the current plan.
Use source_id copied from the enclosing evidence.id for every detail and step.
source.url and source.classification must be null: identity/trust comes only
from the resolver record. Source language may be und when unknown. Keep dates,
units, conditions, contradictory claims and missing fields explicit. Return
groups using the association contract below. Do all work within this ONE reply;
there is no follow-up extraction or corroboration call.
EXTRACTION CONTRACT (nested in structured.materials, not the outer response):
''' + PROMPT + '\nASSOCIATION CONTRACT (groups goes in structured):\n' + CORROBORATION_PROMPT


def _material(evidence):
    record = evidence['record'] if isinstance(evidence['record'], dict) else {}
    material = deepcopy(record)
    receipt = record.get('reading_receipt')
    receipt = receipt if isinstance(receipt, dict) else {}
    content = record.get('content', record.get('text', receipt.get('text', '')))
    issues = []
    if receipt and receipt.get('text') == content:
        for key in ('url', 'method', 'http_status', 'verification', 'source_type', 'language',
                    'checked_at', 'fetched_at', 'source_provenance', 'truncated'):
            if key not in material and key in receipt:
                material[key] = deepcopy(receipt[key])
    elif receipt:
        issues.append('reading_receipt_content_mismatch')
    material.update(id=evidence['id'], content=content)
    context = record.get('applicability') or receipt.get('applicability') or {}
    return material, deepcopy(context), issues


def _index_extractions(raw, identifiers, issues):
    rows = raw.get('materials') if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        issues.append('structured_materials_missing_or_invalid')
        return {}
    result = {}
    for index, row in enumerate(rows):
        identifier = row.get('source_id') if isinstance(row, dict) else None
        if index >= MAX_MATERIALS:
            issues.append(f'materials[{index}]: material_limit_exceeded')
        elif not isinstance(identifier, str) or identifier not in identifiers:
            issues.append(f'materials[{index}]: unresolved_source_id')
        elif identifier in result:
            issues.append(f'materials[{index}]: duplicate_source_id')
        else:
            result[identifier] = row
    return result


def _normalize_entry(evidence, extraction, target, index):
    material, context, issues = _material(evidence)
    row = {'material_id': evidence['id'], 'status': 'not_processed', 'details': [],
           'guide_steps': [], 'unknown_fields': list(FIELDS), 'issues': issues,
           'research_context': context}
    content = material['content']
    if material.get('status') != 'read' or not isinstance(content, str) or not content.strip():
        issues.append('material_not_read')
    elif (material.get('method') == 'google_search_grounded' or
          material.get('verification') == 'search_grounded_not_direct_page'):
        issues.append('search_lead_not_original')
    elif index >= MAX_MATERIALS or len(content) > MAX_CONTENT_CHARS:
        issues.append('material_budget_exceeded')
    elif extraction is None:
        issues.append('material_extraction_missing')
    else:
        extraction = deepcopy(extraction)
        source = extraction.get('source')
        source = source if isinstance(source, dict) else {}
        # These fields come from resolver-owned receipts. Even quoted identity
        # cannot replace the source that was actually admitted/read.
        if any(source.get(key) is not None for key in ('url', 'classification',
                                                     'independent_origin_id', 'verified')):
            issues.append('source_model_identity_fields_ignored')
        extraction['source'] = {key: source.get(key) for key in
                                ('language', 'attribution', 'indirectness')}
        fingerprint = _hash({'material': material, 'target': target, 'version': VERSION})
        normalized = _normalize(material, extraction, target, fingerprint)
        normalized['issues'] = issues + normalized['issues']
        normalized['research_context'] = context
        return normalized
    return row


def organize_delegate_evidence(raw, evidence, request):
    """Validate literals/references using material_details; retain every gap."""
    issues = []
    ledger = {'version': VERSION, 'materials': [], 'corroborations': [],
              'issues': issues, 'corroboration_status': 'not_requested'}
    if not isinstance(raw, dict):
        issues.append('structured_output_missing_or_invalid')
    extractions = _index_extractions(raw, {item['id'] for item in evidence}, issues)
    target = {'task': request['task'], 'purpose': request['purpose']}
    ledger['materials'] = [_normalize_entry(item, extractions.get(item['id']), target, index)
                           for index, item in enumerate(evidence)]
    groups = raw.get('groups') if isinstance(raw, dict) else None
    if not isinstance(groups, list):
        issues.append('structured_groups_missing_or_invalid')
    else:
        for index, group in enumerate(groups[:MAX_DETAILS]):
            normalized = corroborate_material_details(ledger, [group])
            if normalized:
                ledger['corroborations'].extend(normalized)
            else:
                issues.append(f'groups[{index}]: unsupported_group_reference_or_relation')
        if len(groups) > MAX_DETAILS:
            issues.append('groups: limit_exceeded_remaining_not_processed')
        ledger['corroboration_status'] = 'completed'
    supported = any(row['details'] or row['guide_steps'] for row in ledger['materials'])
    incomplete = bool(issues or any(row['issues'] for row in ledger['materials']))
    return {'status': ('partial' if incomplete else 'available') if supported else 'unavailable',
            'detail_ledger': ledger}
