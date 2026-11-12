"""Two mechanical output limits; no semantic source or claim certification.

Read, non-derivative originals can ground conditional candidates. Official hard
facts additionally require reader type evidence traceable to the actual body.
Free prose has no source links, so a batch with unverified critical claims uses
verified statements and explicit uncertainty in place of its unbound prose.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .supply_evidence import SOCIAL_TYPES, content_backed_type, lead_only_reason

UNKNOWN = '未知：缺少可支撑该断言的合格原文。'
NO_OFFICIAL = '未知：未读到具备正文来源证明的官方或经营者原文，营业、入场、场次和预约信息均未确认。'
CONDITIONAL = '仅保留为待核实的候选方向，适配性尚未确认。'
TRADEOFF = '资料不足，暂不能据此判断取舍。'
PROJECTED_TITLE = '待核验条件下的候选比较'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _leaves(value, path=()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _leaves(child, (*path, index))


def _path(parts):
    return '$' + ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in parts)


def _key_leaves(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path, key
            yield from _key_leaves(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _key_leaves(child, (*path, index))


def _permit(proofs, path, value, disposition, **evidence):
    proofs[path] = {'value': value, 'disposition': disposition, 'evidence': evidence}


def _structural_keys():
    from .supply_candidates import (SupplyCandidates, SupplyCandidate, CandidateClaim,
                                    OffListCandidate, SourceWitness, SupplySource)

    models = (SupplyCandidates, SupplyCandidate, CandidateClaim, OffListCandidate, SourceWitness, SupplySource)
    return set().union(*(set(model.model_fields) for model in models), _CONTEXT_ENUMS, {
        'brief', 'options', 'sources', 'research_context', 'verification_context', 'source_ledger',
        'candidate_set', 'supply_candidates', 'layered', 'plan', 'selected_query', 'search_queries',
        'query', 'context', 'angle', 'priority', 'official_questions', 'official_url_leads',
        'planner_identity', 'canonical_name', 'display_name', 'destination', 'source_provenance',
        'source_type_evidence', 'source_type_content_bound', 'lead_only', 'lead_only_reason',
        'retrieved_at', 'published_at', 'experienced_at', 'freshness_basis', 'freshness_window_days',
        'freshness_eligible', 'current_experience_eligible', 'evidence_policy', 'primary_for',
        'background_only', 'social_unverified', 'body_sha256', 'content_sha256', 'content_complete',
        'content_fingerprint_scope', 'origin_refs', 'independence_group', 'independence_reason',
        'provider', 'entity_match', 'basis', 'requested_name', 'explicit_name_forms', 'value',
        'entry_assessments', 'selected_index', 'destination_component_evidence', 'matched_count',
        'paginated', 'semantic_identity_verified', 'fields', 'field_evidence', 'website_lead_status',
        'official_rules_verified', 'reader_invoked'})


def _project_tree(value, proofs, *, fallback=UNKNOWN, preserve_keys=False):
    """Every string needs a path-specific receipt; unknown nested text is denied.

    Receipts carry hashes, never raw rejected text, so the export receipt itself
    is safe to persist alongside (but outside) the C1 model payload.
    """
    rows, keys = [], []
    structural = _structural_keys()

    def visit(node, path, output_path):
        if isinstance(node, str):
            proof = proofs.get(path)
            output = proof['value'] if proof else fallback
            rows.append({'path': _path(output_path), 'input_sha256': _digest(node),
                         'output_sha256': _digest(output),
                         'disposition': proof['disposition'] if proof else 'neutral_replacement',
                         'evidence': proof['evidence'] if proof else {'template': 'unknown_text'}})
            return output
        if isinstance(node, list):
            return [visit(child, (*path, index), (*output_path, index)) for index, child in enumerate(node)]
        if isinstance(node, dict):
            result, used = {}, set(node)
            for index, (key, child) in enumerate(node.items()):
                known = key in structural or bool(re.fullmatch(r'projected_field_\d+', key))
                output_key = key
                if not known and not preserve_keys:
                    while (output_key := f'projected_field_{index}') in used:
                        index += 1
                    used.add(output_key)
                keys.append({'parent_path': _path(output_path), 'output_key': output_key,
                             'input_sha256': _digest(key), 'output_sha256': _digest(output_key),
                             'disposition': ('structural_key' if known else
                                             'no_unverified_s1' if preserve_keys else 'neutral_key')})
                result[output_key] = visit(child, (*path, key), (*output_path, output_key))
            return result
        return copy.deepcopy(node)

    output = visit(value, (), ())
    paths = {_path(path) for path, _ in _leaves(output)}
    if paths != {row['path'] for row in rows} or len(paths) != len(rows):
        raise ValueError('Projection text coverage is incomplete')
    return output, {'input_sha256': _digest(value), 'output_sha256': _digest(output),
                    'text_leaf_count': len(rows), 'accounted_text_count': len(rows),
                    'unaccounted_text_count': 0, 'fields': rows, 'key_fields': keys,
                    'key_count': len(keys), 'accounted_key_count': len(keys), 'unaccounted_key_count': 0}


def _quote_receipt(text, sources, source_id=None):
    for source in sources:
        if source_id is not None and source.get('id') != source_id:
            continue
        body = source.get('content', '')
        if (source.get('read_status', source.get('original_read_status')) == 'read'
                and isinstance(body, str) and text and text in body):
            start = body.index(text)
            return {'source_id': source['id'], 'body_sha256': _digest(body),
                    'start': start, 'end': start + len(text)}
    return None


def _candidate_proofs(enriched, value, annotations, active):
    proofs, sources = {}, enriched.get('sources', [])
    source_ids = {source['id'] for source in sources}
    candidate_ids = {item['id'] for item in value['candidates']}
    rows = {(row['candidate_id'], row['projected_claim_index']): row for row in annotations}
    if not active:
        for path, text in _leaves(value):
            _permit(proofs, path, text, 'no_unverified_s1', batch_state='no_pending_s1')
    for index, candidate in enumerate(value['candidates']):
        base = ('candidates', index)
        _permit(proofs, (*base, 'id'), candidate['id'], 'identifier_closure',
                closure_sha256=_digest(sorted(candidate_ids)))
        if active:
            quote = _quote_receipt(candidate['name'], sources)
            name = candidate['name'] if quote else '候选 ' + candidate['id']
            _permit(proofs, (*base, 'name'), name,
                    'verbatim_quote' if quote else 'neutral_replacement', **(quote or {'template': 'candidate_label'}))
            for key in ('fit_reason', 'tradeoff', 'unknowns'):
                for path, text in _leaves(candidate[key], (*base, key)):
                    _permit(proofs, path, text, 'verification_state_reconstruction',
                            candidate_id=candidate['id'], state='pending_s1_batch')
        for claim_index, claim in enumerate(candidate['claims']):
            _claim_proofs(proofs, (*base, 'claims', claim_index), claim,
                          rows[(candidate['id'], claim_index)], sources, active)
    for path, text in _leaves(value):
        if (path[0] == 'suggested_pair_ids' or path[-1] == 'candidate_id') and text in candidate_ids:
            _permit(proofs, path, text, 'identifier_closure', closure_sha256=_digest(sorted(candidate_ids)))
        if 'source_ids' in path and text in source_ids:
            _permit(proofs, path, text, 'identifier_closure', closure_sha256=_digest(sorted(source_ids)))
        if active and (path[0] in {'title', 'framing', 'unknowns'} or
                       path == ('off_list_candidate', 'reason')):
            _permit(proofs, path, text, 'verification_state_reconstruction', state='pending_s1_batch')
    return proofs


def _claim_proofs(proofs, base, claim, annotation, sources, active):
    from typing import get_args
    from .supply_candidates import CandidateClaim

    for key in ('claim_type', 'risk_level', 'status', 'temporal_scope'):
        allowed = get_args(CandidateClaim.model_fields[key].annotation)
        if claim[key] in allowed:
            _permit(proofs, (*base, key), claim[key], 'schema_enum', enum=list(allowed))
    for group in ('source_witnesses', 'date_exception_witnesses'):
        for index, witness in enumerate(claim[group]):
            quote = _quote_receipt(witness['quote'], sources, witness['source_id'])
            if quote:
                _permit(proofs, (*base, group, index, 'quote'), witness['quote'], 'verbatim_quote', **quote)
            if witness['source_id'] in claim['source_ids']:
                _permit(proofs, (*base, group, index, 'source_id'), witness['source_id'],
                        'identifier_closure', closure_sha256=_digest(sorted(claim['source_ids'])))
    if annotation['confirmed']:
        _permit(proofs, (*base, 'text'), claim['text'], 'verified_claim',
                candidate_id=annotation['candidate_id'], claim_index=annotation['claim_index'])
    elif active:
        original = annotation['original_claim']['text']
        quote = next((receipt for witness in claim['source_witnesses']
                      if (receipt := _quote_receipt(original, sources, witness['source_id']))), None)
        if claim['risk_level'] == 'S1':
            _permit(proofs, (*base, 'text'), claim['text'], 'verification_state_reconstruction', state='unverified_s1')
        elif quote:
            _permit(proofs, (*base, 'text'), claim['text'], 'verbatim_quote',
                    annotation_prefix='【未核验声明】', temporal_scope=claim['temporal_scope'], **quote)
        elif claim['temporal_scope'] == 'regular_schedule' and claim['status'] != 'unknown':
            _permit(proofs, (*base, 'text'), UNKNOWN + '；目标日期例外未核验',
                    'neutral_replacement', template='unknown_regular_schedule')
    if active and isinstance(claim['uncertainty'], str):
        marker = (f"【核验：来源等级={annotation['source_grade']}；"
                  f"{annotation['verification']}；未交叉确认】")
        if claim['temporal_scope'] == 'regular_schedule' and claim['status'] != 'unknown':
            marker += '；目标日期例外未核验'
        _permit(proofs, (*base, 'uncertainty'), marker, 'verification_state_reconstruction',
                candidate_id=annotation['candidate_id'], claim_index=annotation['claim_index'])


_CONTEXT_ENUMS = {
    'source_type': {'official', 'operator', 'unknown', 'aggregator', 'ota', 'editorial',
                    'guide', 'maps_places', 'maps', 'google_maps', 'blog', 'social', 'review', 'forum'},
    'read_status': {'read', 'unavailable', 'blocked', 'failed', 'not_read'},
    'original_read_status': {'read', 'unavailable', 'blocked', 'failed', 'not_read'},
    'source_layer': {'structured', 'official', 'discovery', 'experience'},
    'freshness_status': {'recent', 'outdated', 'future', 'unknown', 'not_applicable'},
    'experience_freshness_status': {'recent', 'outdated', 'future', 'unknown', 'not_applicable'},
    'independence_status': {'unassessed', 'independent', 'dependent', 'unknown'},
    'firsthand_status': {'unassessed', 'unknown', 'firsthand', 'not_firsthand'},
    'applicability_status': {'unassessed', 'unknown'},
    'default_source_status': {'uncorroborated'}, 'default_claim_status': {'unknown'},
    'status': {'unknown', 'unassessed', 'read', 'failed', 'unavailable', 'not_run',
               'single_source', 'uncorroborated', 'social_unverified', 'conflict',
               'model_proposal_not_verified'},
}


def _context_proofs(enriched, value, active):
    proofs = {}
    sources = enriched.get('sources', [])
    identifiers = {source['id'] for source in sources}
    identifiers.update(row['source_id'] for row in enriched.get('source_ledger', [])
                       if isinstance(row, dict) and isinstance(row.get('source_id'), str))
    urls = {source.get('url') for source in sources}
    urls.update(row.get('url') for row in enriched.get('source_ledger', []) if isinstance(row, dict))
    for path, text in _leaves(value):
        key = path[-1]
        if not active:
            _permit(proofs, path, text, 'no_unverified_s1', batch_state='no_pending_s1')
        if key in _CONTEXT_ENUMS and text in _CONTEXT_ENUMS[key]:
            _permit(proofs, path, text, 'schema_enum', enum=sorted(_CONTEXT_ENUMS[key]))
        elif key in {'source_id', 'id'} and text in identifiers:
            _permit(proofs, path, text, 'identifier_closure', closure_sha256=_digest(sorted(identifiers)))
        elif key == 'url' and text in urls:
            _permit(proofs, path, text, 'source_address', address_sha256=_digest(text))
        elif isinstance(key, str) and key.endswith('_sha256') and re.fullmatch(r'[0-9a-f]{64}', text):
            _permit(proofs, path, text, 'structural_digest', format='sha256')
        elif path[0] == 'sources' and len(path) == 3 and key == 'content':
            _permit(proofs, path, text, 'source_original', source_id=sources[path[1]]['id'],
                    body_sha256=_digest(text), start=0, end=len(text))
        elif key == 'quote':
            parent, source_id = value, None
            for component in path[:-1]:
                parent = parent[component]
                if isinstance(parent, dict):
                    source_id = parent.get('source_id', parent.get('id', source_id))
            quote = (_quote_receipt(text, sources, source_id)
                     if isinstance(source_id, str) and source_id in identifiers else None)
            if quote:
                _permit(proofs, path, text, 'verbatim_quote', **quote)
    return proofs


class ProjectionStateError(ValueError):
    """The supplied floor receipt cannot authorize this candidate/context pair."""


def _floor_state(enriched, candidate_set, projected_ids, annotations):
    return {'version': 1, 'candidate_set_sha256': _digest(candidate_set),
            'context_sha256': _digest(enriched),
            'pending_s1_batch': bool(projected_ids),
            'projected_candidate_ids': list(projected_ids),
            'verification_state': [
                {key: row[key] for key in ('candidate_id', 'claim_index',
                                           'projected_claim_index', 'confirmed')}
                for row in annotations]}


def _bound_floor_state(enriched, candidate_set, floor_audit):
    state = floor_audit.get('floor_state') if isinstance(floor_audit, dict) else None
    if not isinstance(state, dict):
        raise ProjectionStateError('Floor projection state is missing')
    if floor_audit.get('floor_state_sha256') != _digest(state):
        raise ProjectionStateError('Floor projection state hash does not match its receipt')
    required = {'version', 'candidate_set_sha256', 'context_sha256', 'pending_s1_batch',
                'projected_candidate_ids', 'verification_state'}
    if set(state) != required or type(state['version']) is not int or state['version'] != 1:
        raise ProjectionStateError('Floor projection state is malformed')
    if (state['candidate_set_sha256'] != _digest(candidate_set)
            or state['context_sha256'] != _digest(enriched)):
        raise ProjectionStateError('Floor projection state does not match candidate/context hashes')
    ids = [candidate['id'] for candidate in candidate_set['candidates']]
    projected = state['projected_candidate_ids']
    if (type(state['pending_s1_batch']) is not bool or not isinstance(projected, list)
            or projected not in ([], ids) or state['pending_s1_batch'] != bool(projected)):
        raise ProjectionStateError('Floor projection state has inconsistent projected IDs')
    expected = {(candidate['id'], index) for candidate in candidate_set['candidates']
                for index, _ in enumerate(candidate['claims'])}
    rows, found = state['verification_state'], set()
    if not isinstance(rows, list):
        raise ProjectionStateError('Floor verification state is missing')
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) != {'candidate_id', 'claim_index', 'projected_claim_index', 'confirmed'}
                or not isinstance(row['candidate_id'], str)
                or type(row['claim_index']) is not int or row['claim_index'] < 0
                or type(row['projected_claim_index']) is not int or row['projected_claim_index'] < 0
                or type(row['confirmed']) is not bool):
            raise ProjectionStateError('Floor verification state is malformed')
        key = row['candidate_id'], row['projected_claim_index']
        if key in found:
            raise ProjectionStateError('Floor verification state has duplicate claims')
        found.add(key)
    if found != expected:
        raise ProjectionStateError('Floor verification state does not cover delivered claims')
    return copy.deepcopy(state)


def project_comparison_context(enriched, candidate_set, *, floor_audit=None):
    """Project C1's copied context; the candidate is an existing floor output.

    Candidate validation is not semantic verification. C1 does not reinterpret
    uncertainty prose as a critic result: its candidate receipt references the
    exact floor output digest in the durably written claim-audit artifact.
    """
    state = _bound_floor_state(enriched, candidate_set, floor_audit)
    active = state['pending_s1_batch']
    original = {key: copy.deepcopy(enriched.get(key, [] if key in {'sources', 'source_ledger'} else None))
                for key in ('sources', 'research_context', 'source_ledger', 'verification_context')}
    projected, receipt = _project_tree(original, _context_proofs(enriched, original, active),
                                      preserve_keys=not active)
    candidate_hash = _digest(candidate_set)
    receipt.update(candidate_set_sha256=candidate_hash, pending_s1_batch=active,
                   floor_state=state, floor_state_sha256=_digest(state),
                   candidate_fields=[{'path': _path(path), 'output_sha256': _digest(text),
                                      'disposition': 'floor_projection_reference',
                                      'floor_output_sha256': candidate_hash}
                                     for path, text in _leaves(candidate_set)])
    return projected, receipt


def comparison_projection_receipt(seed, receipt):
    """Account for every text leaf at the actual C1 payload boundary, including copies."""
    projection = copy.deepcopy(receipt)
    context = {row['path']: row for row in receipt['fields']}
    candidates = {item['id']: index for index, item in enumerate(seed['candidate_set']['candidates'])}
    payload = {key: seed.get(key) for key in
               ('brief', 'options', 'sources', 'research_context', 'verification_context', 'candidate_set', 'source_ledger')}
    rows = []
    for path, text in _leaves(payload):
        original_path, candidate_path = path, None
        if path[0] == 'candidate_set':
            candidate_path = path[1:]
        elif path[0] == 'options':
            index = candidates[seed['options'][path[1]]['id']]
            candidate_path = ('candidates', index, *path[2:])
        elif path[:3] == ('verification_context', 'supply_candidates', 'candidate_set'):
            candidate_path = path[3:]
        elif path[:3] == ('verification_context', 'supply_candidates', 'source_ledger'):
            original_path = ('source_ledger', *path[3:])
        if candidate_path is not None:
            evidence = {'floor_output_sha256': receipt['candidate_set_sha256'],
                        'floor_field_path': _path(candidate_path)}
            disposition = 'floor_projection_reference'
        elif path == ('brief',):
            evidence, disposition = {'origin': 'explicit_user_input_fields'}, 'user_input'
        elif _path(original_path) in context:
            row = context[_path(original_path)]
            evidence, disposition = row['evidence'], row['disposition']
        elif path[-1] in _CONTEXT_ENUMS and text in _CONTEXT_ENUMS[path[-1]]:
            evidence, disposition = {'enum': sorted(_CONTEXT_ENUMS[path[-1]])}, 'schema_enum'
        else:
            raise ValueError('C1 text has no projection receipt: ' + _path(path))
        rows.append({'path': _path(path), 'output_sha256': _digest(text),
                     'disposition': disposition, 'evidence': evidence})
    projection.update(export_sha256=_digest(payload), export_fields=rows,
                      export_text_leaf_count=len(rows), export_accounted_text_count=len(rows),
                      export_unaccounted_text_count=0)
    structural, keys = _structural_keys(), []
    for path, key in _key_leaves(payload):
        known = key in structural or bool(re.fullmatch(r'projected_field_\d+', key))
        if not known and receipt['pending_s1_batch']:
            raise ValueError('C1 dictionary key has no projection receipt')
        keys.append({'parent_path': _path(path), 'output_key': key, 'output_sha256': _digest(key),
                     'disposition': 'structural_key' if known else 'no_unverified_s1'})
    projection.update(export_key_fields=keys, export_key_count=len(keys),
                      export_accounted_key_count=len(keys), export_unaccounted_key_count=0)
    return projection


def _source_decision(source):
    identifier = source.get('id', source.get('source_id'))
    content = source.get('content')
    read = source.get('read_status', source.get('original_read_status')) == 'read'
    lead_reason = lead_only_reason(source, content)
    if source.get('lead_only') is True:
        # Negative reader evidence survives later excerpt truncation.
        lead_reason = lead_reason or source.get('lead_only_reason') or 'reader_declared_lead_only'
    kind = source.get('source_type', 'unknown')
    reasons = []
    if not read or not isinstance(content, str) or not content.strip():
        reasons.append('original_not_read')
    if lead_reason:
        reasons.append(lead_reason)
    if any(source.get(key) in {'outdated', 'future'} for key in
           ('freshness_status', 'experience_freshness_status')):
        reasons.append('outdated_or_future_evidence')
    if kind in SOCIAL_TYPES and not (source.get('freshness_eligible') is True
                                    and source.get('freshness_status') == 'recent'):
        reasons.append('current_experience_freshness_not_established')
    qualified = bool(isinstance(identifier, str) and identifier and not reasons)
    official = bool(qualified and kind in {'official', 'operator'}
                    and content_backed_type(source, content))
    if qualified and kind in {'official', 'operator'} and not official:
        reasons.append('official_identity_not_content_backed')
    return {'source_id': identifier, 'qualified': qualified, 'official': official,
            'lead_only': bool(lead_reason), 'reasons': reasons,
            'qualification': ('official_type_traceable_not_fact_verified' if official else
                              'conditional_original_not_fact_verified' if qualified else 'unqualified')}


def evidence_floor(enriched):
    """Return eligibility only; domains and search metadata never certify type."""
    decisions = [_source_decision(source) for source in enriched.get('sources', [])
                 if isinstance(source, dict)]
    qualified = list(dict.fromkeys(row['source_id'] for row in decisions if row['qualified']))
    official = list(dict.fromkeys(row['source_id'] for row in decisions if row['official']))
    reasons = []
    if not qualified:
        reasons.append('no_qualified_originals')
    if not official:
        reasons.append('no_content_backed_official_source')
    return {'status': 'qualified' if qualified else 'insufficient_evidence',
            'qualified_source_ids': qualified, 'official_source_ids': official,
            'reasons': reasons, 'source_decisions': decisions}


def _project_candidate_prose(candidate, verified_claims, pending_count):
    """Unbound prose cannot independently restore a rejected critical claim.

    Keep whole verified statements rather than guessing which paraphrase depends
    on the rejected claim. The original prose stays in original_candidate_set.
    """
    statements = []
    for claim in verified_claims:
        text = claim['text']
        proposed = '已核验依据（未交叉确认）：' + '；'.join([*statements, text])
        if len(proposed) <= 400:
            statements.append(text)
    candidate['fit_reason'] = ('已核验依据（未交叉确认）：' + '；'.join(statements)
                               if statements else CONDITIONAL)
    candidate['tradeoff'] = (
        f'仍有 {pending_count} 项关键条件未核验；确认前只能作为候选方向，不能据此确定行程。'
        if pending_count else '比较依据限于已核验声明；其他取舍尚待确认。')
    candidate['unknowns'] = [candidate['tradeoff']]


def apply_evidence_floor(enriched, candidate_set, *, mechanical_audit=None, critic_audit=None):
    """Quarantine bad bindings and project unverified critical promises safely.

    All removed claims and replaced uncertainty strings survive in the audit.
    Status is never promoted by a deterministic source-count heuristic.
    """
    from .supply_candidates import audit_candidate_claims, SupplyCandidates
    result, floor = copy.deepcopy(candidate_set), evidence_floor(enriched)
    mechanical = (audit_candidate_claims(enriched, candidate_set)
                  if mechanical_audit is None else mechanical_audit)
    by_key = {(r['candidate_id'], r['claim_index']): r for r in mechanical}
    reviews = {(r['candidate_id'], r['claim_index']): r
               for r in (critic_audit or {}).get('reviews', [])}
    official = set(floor['official_source_ids'])
    sources = {s['id']: s for s in enriched.get('sources', [])}
    quarantined, annotations, projections = [], [], []
    for candidate in result['candidates']:
        retained, verified_claims, pending_s1 = [], [], 0
        for index, claim in enumerate(candidate['claims']):
            key = candidate['id'], index
            check = by_key.get(key)
            if not check or not check['mechanical_pass']:
                pending_s1 += claim['risk_level'] == 'S1'
                quarantined.append({'candidate_id': key[0], 'claim_index': index,
                                    'original_claim': copy.deepcopy(claim),
                                    'reasons': check['reasons'] if check else ['missing_audit']})
                marker = f'声明 {index + 1} 已隔离：出处或数字未通过机械核验。'
                if marker not in candidate['unknowns'] and len(candidate['unknowns']) < 12:
                    candidate['unknowns'].append(marker)
                continue
            refs = set(claim['source_ids'])
            grade = ('官方' if refs & official else '聚合' if any(
                sources.get(sid, {}).get('source_type') in {'aggregator', 'ota', 'editorial', 'guide', 'maps_places', 'maps', 'google_maps'}
                for sid in refs) else '未知')
            review = reviews.get(key)
            supported = bool(review and review['supported'])
            batch_degraded = bool((critic_audit or {}).get('batch_degraded'))
            s1_ready = bool(grade == '官方' and supported and
                            (critic_audit or {}).get('s1_all_supported'))
            verified = supported and not batch_degraded and (
                claim['risk_level'] != 'S1' or s1_ready)
            state = '语义核验通过' if verified else '未核验'
            if review and not supported:
                state = '语义核验不成立'
            if batch_degraded:
                state += '；批次抽查未达标'
            original = copy.deepcopy(claim)
            # A model-supplied confirmation status never survives absent evidence.
            if not verified:
                claim['status'] = 'unknown' if claim['risk_level'] == 'S1' or review else 'uncorroborated'
            elif claim['status'] == 'corroborated':
                # Semantic support alone does not certify independent corroboration.
                claim['status'] = 'uncorroborated'
            unverified_s1 = claim['risk_level'] == 'S1' and not verified
            if unverified_s1:
                pending_s1 += 1
                # The raw assertion and its uncertainty remain in original_claim.
                # A label cannot neutralize an affirmative unsupported promise.
                claim['text'] = '【未核验声明】该关键条件尚未核验，不能作为行程安排的确定依据。'
            elif not verified:
                prefix = '【未核验声明】'
                if not claim['text'].startswith(prefix):
                    if len(prefix + claim['text']) > 400:
                        quarantined.append({'candidate_id': key[0], 'claim_index': index,
                                            'original_claim': original,
                                            'reasons': ['annotation_exceeds_strict_text_limit']})
                        continue
                    claim['text'] = prefix + claim['text']
            marker = f'【核验：来源等级={grade}；{state}；未交叉确认】'
            previous = ('' if unverified_s1 else
                        (claim.get('uncertainty') or '').split('【核验：', 1)[0].rstrip('；'))
            claim['uncertainty'] = (previous[:max(0, 399 - len(marker))] + '；' + marker
                                    if previous else marker)
            retained.append(claim)
            if verified:
                verified_claims.append(claim)
            annotations.append({'candidate_id': key[0], 'claim_index': index,
                                'projected_claim_index': len(retained) - 1,
                                'source_grade': grade, 'verification': state,
                                'confirmed': verified, 'original_claim': original})
        candidate['claims'] = retained
        projections.append((candidate, verified_claims, pending_s1))
    projected_candidates = []
    active = any(pending for _, _, pending in projections)
    if active:
        # Unbound descriptions may refer to another candidate's rejected S1.
        for candidate, verified_claims, pending in projections:
            _project_candidate_prose(candidate, verified_claims, pending)
            projected_candidates.append(candidate['id'])
        result['title'] = PROJECTED_TITLE
        result['framing'] = '比较已有依据与待核验条件；关键条件确认前，不据此确定行程。'
        result['unknowns'] = ['部分候选的关键条件未核验；原始声明和自由描述已保留审计。']
        if result['off_list_candidate']:
            result['off_list_candidate']['reason'] = '作为补充候选比较；相关关键条件仍需核验。'
    result = SupplyCandidates.model_validate(result).model_dump()
    result, projection = _project_tree(result, _candidate_proofs(enriched, result, annotations, active),
                                      preserve_keys=not active)
    if SupplyCandidates.model_validate(result).model_dump() != result:
        raise ValueError('Projection must already include deterministic schema annotations')
    projection['original_candidate_set_sha256'] = _digest(candidate_set)
    state = _floor_state(enriched, result, projected_candidates, annotations)
    _, context_projection = project_comparison_context(
        enriched, result, floor_audit={'floor_state': state, 'floor_state_sha256': _digest(state)})
    return result, {'evidence_floor': floor, 'mechanical_audit': mechanical,
                    'critic_audit': critic_audit, 'quarantined_claims': quarantined,
                    'annotations': annotations, 'original_candidate_set': copy.deepcopy(candidate_set),
                    'original_c1_context': {key: copy.deepcopy(enriched.get(key)) for key in
                                            ('sources', 'research_context', 'source_ledger', 'verification_context')},
                    'delivery_projection': projection, 'c1_context_projection': context_projection,
                    'floor_state': state, 'floor_state_sha256': _digest(state),
                    'projected_candidate_ids': projected_candidates,
                    'downgraded_claim_count': sum(not r['confirmed'] for r in annotations),
                    'semantic_truth_verified': False, 'identifier_fields_preserved': True}
