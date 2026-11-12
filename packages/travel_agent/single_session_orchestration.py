"""Five bounded stages over an injected backend; no model/network calls here.

Candidate schema: {atom: lifecycle FIELDS, source_text: str | None,
 authority?: str, scope_key?: str}. Adjudication returns candidate/source references.
Only s_level, expires_on and notes may change; evidence stays in the pipeline.
Season suitability is independent of expiry; coverage is audited without rewriting evidence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
from uuid import uuid4

from .lifecycle_atoms import AtomStore, CATEGORIES, FIELDS
from .season_scope_coverage import season_coverage

CONSTRAINTS = {
    'sections': ['这趟怎么定下来的', '出行前待办与应急方案', '逐日',
                 '钱', '出发前先核这几条', '核查记录'],
    's1_categories': ['开放', '票价', '交通'],
    'money': {'verified': 'source-bound amounts only',
              'estimates': 'separate estimate label, assumptions and multiplication; '
                           'existing source-bound price markers only; unknown remains unknown'},
    'wording': ['单源未交叉确认', '旧季节不升级为当季', '未知不写确定值',
                'S2就地标注', '未来拥挤与实时可用性不保证'],
    'conflict_priority': ['当季压往年', '官方压转述', '冬季专项压夏季通用'],
}


def _save(directory, name, value):
    path = directory / name
    temporary = directory / f'.{name}.{uuid4().hex}.tmp'
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _iso(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date must be YYYY-MM-DD')
    return value


async def freeze_spec(intake, backend, as_of):
    spec = deepcopy(await backend.freeze(deepcopy(intake)))
    for key in ('destination', 'season_scope'):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError(f'spec requires {key}')
    if spec.get('departure_date') is not None:
        _iso(spec['departure_date'])
    else:
        spec['departure_date'] = None
    categories = spec.get('categories')
    if not isinstance(categories, list) or not categories or any(
            category not in CATEGORIES for category in categories):
        raise ValueError('spec categories must use lifecycle categories')
    spec['categories'] = list(dict.fromkeys(categories))
    scopes = spec.get('reusable_scopes', [spec['season_scope']])
    if not isinstance(scopes, list) or any(not isinstance(s, str) or not s.strip() for s in scopes):
        raise ValueError('reusable_scopes must list explicit source scopes')
    spec['reusable_scopes'] = list(dict.fromkeys([spec['season_scope'], *scopes]))
    spec['scope_notice'] = 'Reusing historical scopes does not establish current-season applicability.'
    spec['as_of'] = _iso(as_of)
    spec['constraints'] = deepcopy(CONSTRAINTS)
    spec['intake'] = deepcopy(intake)
    return spec


async def research_fanout(spec, store, backend):
    active = store.list_atoms(spec['destination'], 'active')
    warm = [{'atom': atom, 'source_text': None, 'origin': 'warm'} for atom in active
            if atom.get('verbatim_quote') and atom.get('checked_date')
            and atom['expires_on'] >= spec['as_of']
            and atom['season_scope'] in spec['reusable_scopes']
            and atom['category'] in spec['categories']]
    available = {item['atom']['category'] for item in warm}
    missing = [category for category in spec['categories'] if category not in available]
    async def research_category(category):
        try:
            group = await backend.research(deepcopy(spec), category)
            if not isinstance(group, list):
                raise ValueError('research must return candidate list')
            validated = []
            filtered = []
            for index, item in enumerate(group):
                item = deepcopy(item)
                atom = item.get('atom', {})
                if atom.get('destination') != spec['destination']:
                    raise ValueError('research candidate partition/category mismatch')
                if atom.get('category') != category:
                    filtered.append({'index': index, 'source_id': item.get('source_id'),
                                     'category': atom.get('category')})
                    continue
                item['origin'] = 'research'
                validated.append(item)
            gap = ({'category': category, 'status': 'degraded',
                    'verification_status': 'unverified', 'reason': 'cross_category_filtered',
                    'filtered_candidates': filtered, 'retained_count': len(validated)}
                   if filtered else None)
            return validated, gap
        except Exception as exc:
            # Validate the whole partition before admitting anything. Cancellation
            # and process-exit signals inherit BaseException and must propagate.
            return [], {'category': category, 'status': 'degraded',
                        'verification_status': 'unverified', 'reason': 'research_category_failed',
                        'error': {'type': type(exc).__name__, 'message': str(exc)}}

    groups = await asyncio.gather(*(research_category(category) for category in missing))
    candidates = list(warm)
    category_gaps = []
    for group, gap in groups:
        candidates.extend(group)
        if gap is not None:
            category_gaps.append(gap)
    outcomes = getattr(backend, 'research_parse_outcomes', {})
    diagnostics = [deepcopy(outcomes[c]) for c in missing if c in outcomes]
    degraded = (backend.research_gaps(missing) if hasattr(backend, 'research_gaps') else
                [item for item in diagnostics if item['status'] == 'degraded'])
    degraded = [*degraded, *category_gaps]
    searches = [item for c in missing
                for item in getattr(backend, 'research_search_outcomes', {}).get(c, [])]
    return candidates, {'research_categories': missing, 'research_count': len(missing),
                        'warm_count': len(warm), 'candidate_count': len(candidates),
                        'research_parse_retry_count': sum(x['retry_count'] for x in diagnostics),
                        'research_parse_error_count': sum(x['parse_error_count'] for x in diagnostics),
                        'research_search_retry_count': sum(x['retry_count'] for x in searches),
                        'research_search_transport_error_count': sum(x['transport_error_count'] for x in searches),
                        'research_search_degraded_count': sum(x['status'] == 'degraded' for x in searches),
                        'research_search_recovered_count': sum(x['status'] == 'recovered' for x in searches),
                        'research_degraded_count': len({x['category'] for x in degraded}),
                        'research_degraded': degraded}


def _candidate_references(candidates):
    referenced = deepcopy(candidates)
    for index, candidate in enumerate(referenced, 1):
        candidate['candidate_id'] = f'candidate-{index:04d}'
        if not candidate.get('source_id'):
            candidate['source_id'] = ('atom-' + candidate['atom']['atom_id']
                                      if candidate['atom'].get('atom_id') else candidate['candidate_id'])
    return referenced


def _record_shape(audit, group, index, row, field, rule, value, *, provenance='model'):
    event = {'path': f'{group}[{index}].{field}', 'candidate_id': row.get('candidate_id'),
             'source_id': row.get('source_id'), 'field': field, 'rule': rule,
             'before_present': field in row, 'before': deepcopy(row.get(field)),
             'after': deepcopy(value), 'provenance': provenance}
    audit['events'].append(event)
    key = f'{group}.{field}:{rule}'
    audit['counts'][key] = audit['counts'].get(key, 0) + 1
    audit['total'] += 1
    row[field] = deepcopy(value)


def _normalize_adjudication(candidates, result, audit):
    """Normalize only lossless field shapes; no decision or evidence inference."""
    if not isinstance(result, dict):
        return
    by_id = {c['candidate_id']: c for c in candidates}
    for group in ('accepted', 'rejected', 'decisions'):
        rows = result.get(group)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            strings = ('candidate_id', 'source_id', 's_level', 'expires_on') if group == 'accepted' else (
                'candidate_id', 'source_id', 'reason')
            for field in strings:
                value = row.get(field)
                if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
                    _record_shape(audit, group, index, row, field, 'singleton_array_to_string', value[0])
            if group != 'accepted':
                continue
            notes = row.get('notes')
            if isinstance(notes, str):
                _record_shape(audit, group, index, row, 'notes', 'string_to_array', [notes])
            elif notes is None:
                cid = row.get('candidate_id')
                candidate = by_id.get(cid) if isinstance(cid, str) else None
                if candidate is None or candidate['source_id'] != row.get('source_id'):
                    continue
                original = candidate['atom'].get('notes')
                if not isinstance(original, list) or any(not isinstance(n, str) for n in original):
                    raise ValueError('adjudication missing notes requires original string array')
                rule = 'null_to_original' if 'notes' in row else 'missing_to_original'
                _record_shape(audit, group, index, row, 'notes', rule, original,
                              provenance='candidate.atom.notes')


def _validate_decisions(candidates, result):
    """Require a complete, unambiguous partition before any store mutation."""
    if not isinstance(result, dict) or set(result) != {'accepted', 'rejected', 'decisions'}:
        raise ValueError('adjudication requires only accepted, rejected and decisions')
    known = {c['candidate_id']: c['source_id'] for c in candidates}
    groups = {}
    for name in ('accepted', 'rejected', 'decisions'):
        rows = result.get(name)
        if not isinstance(rows, list):
            raise ValueError('adjudication requires ' + name + ' list')
        seen = set()
        fields = ({'candidate_id', 'source_id', 's_level', 'expires_on', 'notes'}
                  if name == 'accepted' else {'candidate_id', 'source_id', 'reason'})
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError('adjudication must return reference decisions only')
            cid = row['candidate_id']
            if not isinstance(cid, str) or cid not in known or known[cid] != row['source_id'] or cid in seen:
                raise ValueError('adjudication unknown, duplicate or mismatched reference')
            if name != 'accepted' and (not isinstance(row['reason'], str) or not row['reason'].strip()):
                raise ValueError('adjudication requires decision reason')
            seen.add(cid)
        groups[name] = seen
    if (groups['accepted'] & groups['rejected'] or
            groups['accepted'] | groups['rejected'] != set(known) or groups['decisions'] != set(known)):
        raise ValueError('adjudication incomplete or overlapping decisions')


def _validate_accepted(spec, candidates, accepted, *, season_audit=None):
    validated = []
    mutable = {'s_level', 'expires_on', 'notes'}
    by_id = {c['candidate_id']: c for c in candidates}
    for item in accepted:
        candidate = by_id.get(item.get('candidate_id'))
        if candidate is None or item.get('source_id') != candidate['source_id']:
            raise ValueError('adjudication unknown or mismatched reference')
        atom = deepcopy(candidate['atom'])
        atom.update({key: deepcopy(item[key]) for key in mutable})
        if not isinstance(atom['notes'], list) or any(not isinstance(n, str) for n in atom['notes']):
            raise ValueError('adjudication notes must be strings')
        if atom.get('s_level') not in ('S1', 'S2', 'S3') or not atom.get('expires_on'):
            raise ValueError('adjudication must explicitly assign S level and expiry')
        _iso(atom['expires_on'])
        coverage = season_coverage(spec, atom.get('season_scope'))
        if season_audit is not None:
            season_audit.append({**coverage, 'candidate_id': item['candidate_id'],
                                 'source_id': item['source_id']})
        if not coverage['covered']:
            raise ValueError('accepted atom does not match frozen season scope')
        if atom['expires_on'] < spec['as_of']:
            raise ValueError('accepted atom is expired')
        if candidate['origin'] == 'research':
            quote, source = atom.get('verbatim_quote'), candidate.get('source_text')
            if not isinstance(quote, str) or not quote.strip() or not isinstance(source, str) or quote not in source:
                raise ValueError('research quote missing from source text')
            if not atom.get('checked_date') or atom.get('evidence_kind') not in ('recorded', 'fixture'):
                raise ValueError('research date and evidence kind must be explicit')
        elif any(atom.get(k) != candidate['atom'].get(k) for k in ('s_level', 'expires_on')):
            raise ValueError('warm evidence cannot be renewed by adjudication')
        validated.append({**deepcopy(candidate), 'atom': deepcopy(atom)})
    return validated


def _admit_atoms(spec, candidates, rows, store, audit):
    """Isolate evidence/record failures; infrastructure failures still propagate."""
    accepted, saved = [], []
    for row in rows:
        phase = 'validation'
        try:
            item = _validate_accepted(spec, candidates, [row], season_audit=audit['seasons'])[0]
            phase = 'harvest'
            record = {key: value for key, value in item['atom'].items() if key in FIELDS}
            atom = store.harvest(spec['destination'], [record])[0]
        except (ValueError, TypeError, KeyError) as exc:
            audit['degraded'].append({'candidate_id': row['candidate_id'],
                'source_id': row['source_id'], 'phase': phase,
                'reason': str(exc), 'error_type': type(exc).__name__,
                'provenance': 'runtime_validation', 'status': 'discarded'})
            continue
        accepted.append(item)
        saved.append(atom)
    return accepted, saved


async def adjudicate_candidates(spec, candidates, store, backend, *, research_gaps=None,
                                normalization_audit=None, semantic_audit=None):
    candidates = _candidate_references(candidates)
    adjudication_spec = deepcopy(spec)
    if research_gaps is not None:
        adjudication_spec['research_gaps'] = deepcopy(research_gaps)
    result = deepcopy(await backend.adjudicate(adjudication_spec, deepcopy(candidates)))
    audit = normalization_audit if normalization_audit is not None else {'total': 0, 'counts': {}, 'events': []}
    _normalize_adjudication(candidates, result, audit)
    _validate_decisions(candidates, result)
    semantic = semantic_audit if semantic_audit is not None else {'seasons': [], 'degraded': []}
    before = store.list_atoms(spec['destination'])
    accepted, saved = _admit_atoms(spec, candidates, result['accepted'], store, semantic)
    # Ledger replay returns the original record, never the proposed refreshed one.
    canonical = []
    seen = set()
    for candidate, atom in zip(accepted, saved):
        if not atom.get('verbatim_quote') or not atom.get('checked_date'):
            continue
        if atom['status'] != 'active' or atom['expires_on'] < spec['as_of']:
            continue
        if not season_coverage(spec, atom['season_scope'])['covered']:
            continue
        if atom['atom_id'] not in seen:
            canonical.append({**candidate, 'atom': atom})
            seen.add(atom['atom_id'])
    result['accepted'] = canonical
    result['degraded'] = deepcopy(semantic['degraded'])
    after = store.list_atoms(spec['destination'])
    net = len(after) - len(before)
    return result, {'adjudication_normalization': audit,
                    'adjudication_semantic': semantic,
                    'net_new': net, 'dedup_count': len(saved) - net,
                    'active_count': len(store.list_atoms(spec['destination'], 'active'))}


def _check_composition(composition, adjudicated):
    if not isinstance(composition.get('markdown'), str) or not composition['markdown'].strip():
        raise ValueError('composition requires markdown')
    ids = {item['atom']['atom_id'] for item in adjudicated['accepted']}
    facts = composition.get('fact_atom_ids')
    if not isinstance(facts, list) or any(item not in ids for item in facts):
        raise ValueError('composition references unadjudicated facts')
    estimates = composition.get('estimates')
    if not isinstance(estimates, list):
        raise ValueError('composition requires separate estimates list')
    estimate_ids = set()
    for item in estimates:
        identifier = item.get('estimate_id')
        if not isinstance(identifier, str) or not identifier or identifier in estimate_ids:
            raise ValueError('estimate requires unique estimate_id')
        estimate_ids.add(identifier)
        if item.get('label') != 'estimate' or identifier in ids:
            raise ValueError('estimate contaminated verified facts')
        if any(source in facts for source in item.get('source_atom_ids', [])):
            raise ValueError('estimate source is also classified as verified fact')
        if any(source not in ids for source in item.get('source_atom_ids', [])):
            raise ValueError('estimate references unknown source')
    check = composition.get('self_check', {})
    if check.get('passed') is not True:
        raise ValueError('composition contamination self-check failed')
    return check


async def compose_document(spec, adjudicated, backend):
    composition = deepcopy(await backend.compose(deepcopy(spec), deepcopy(adjudicated)))
    gaps = adjudicated.get('research_gaps', [])
    if gaps and isinstance(composition.get('markdown'), str) and composition['markdown'].strip():
        categories = list(dict.fromkeys(gap['category'] for gap in gaps))
        notice = '\n'.join('- ' + category + '：本次研究未完成核实，出发前需补充确认。'
                           for category in categories)
        lines = composition['markdown'].splitlines()
        heading = next((i for i, line in enumerate(lines)
                        if line.startswith('## ') and '出发前先核' in line), None)
        if heading is None:
            lines.extend(['', '## 出发前先核这几条', '', notice])
        else:
            lines[heading + 1:heading + 1] = ['', notice, '']
        composition['markdown'] = '\n'.join(lines)
    return composition


async def layout_stage(spec, directory, backend, *, composition=None, adjudicated=None):
    """The same stage preflights before writing and renders after composition."""
    if composition is None:
        receipt = await backend.preflight(deepcopy(spec), directory)
        if not isinstance(receipt, dict) or receipt.get('passed') is not True:
            raise ValueError('layout preflight failed')
        return receipt
    return await backend.render(deepcopy(spec), deepcopy(composition),
                                deepcopy(adjudicated), directory)


async def run_pipeline(intake, *, directory, store: AtomStore, backend, as_of=None):
    """Run offline or live with identical orchestration and durable phase receipts.

    An interrupted run is safe to replay: store.harvest owns replay/supersession.
    Checkpoint is diagnostic, not permission to skip or repeat external actions.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    as_of = as_of.isoformat() if isinstance(as_of, date) else as_of or date.today().isoformat()
    ledger = {'run_id': uuid4().hex, 'status': 'running', 'events': [], 'metrics': {}}

    def checkpoint(phase, value=None):
        ledger['phase'] = phase
        if value is not None:
            _save(directory, phase + '.json', value)
            ledger['events'].append({'phase': phase, 'artifact': phase + '.json'})
        _save(directory, 'orchestration_ledger.json', ledger)

    try:
        checkpoint('freeze')
        spec = await freeze_spec(intake, backend, as_of)
        checkpoint('spec', spec)
        checkpoint('research')
        candidates, metrics = await research_fanout(spec, store, backend)
        ledger['metrics'].update(metrics)
        checkpoint('candidates', candidates)
        checkpoint('adjudication')
        normalization_audit = {'total': 0, 'counts': {}, 'events': []}
        ledger['metrics']['adjudication_normalization'] = normalization_audit
        semantic_audit = {'seasons': [], 'degraded': []}
        ledger['metrics']['adjudication_semantic'] = semantic_audit
        adjudicated, metrics = await adjudicate_candidates(
            spec, candidates, store, backend,
            research_gaps=ledger['metrics']['research_degraded'],
            normalization_audit=normalization_audit, semantic_audit=semantic_audit)
        if ledger['metrics']['research_degraded']:
            adjudicated['research_gaps'] = deepcopy(ledger['metrics']['research_degraded'])
        ledger['metrics'].update(metrics)
        checkpoint('adjudicated', adjudicated)
        checkpoint('layout_preflight')
        preflight = await layout_stage(spec, directory, backend)
        checkpoint('preflight', preflight)
        checkpoint('compose')
        composition = await compose_document(spec, adjudicated, backend)
        ledger['self_check'] = composition.get('self_check')
        checkpoint('composition', composition)
        _check_composition(composition, adjudicated)
        checkpoint('render')
        rendered = await layout_stage(spec, directory, backend, composition=composition,
                                      adjudicated=adjudicated)
        checkpoint('rendered', rendered)
        if not isinstance(rendered, dict) or rendered.get('passed') is False or rendered.get('status') in ('failed', 'blocked'):
            raise ValueError('final render failed')
        ledger['status'] = 'complete'
        checkpoint('done')
        return {'spec': spec, 'candidates': candidates, 'adjudicated': adjudicated,
                'composition': composition, 'rendered': rendered, 'preflight': preflight,
                'metrics': ledger['metrics']}
    except BaseException as exc:
        ledger['status'] = 'failed'
        ledger['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        checkpoint(ledger['phase'])
        raise
