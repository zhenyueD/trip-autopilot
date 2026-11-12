"""Assembly/review wire projections; original state and audit stay untouched.

References share exact structures and unchanged record fields, never summarize
facts. Logical JSON pointers address the expanded input, including inherited
fields of a record. The model still cites the original alignment state keys.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter


REFERENCE_CONTRACT = (
    '本输入使用精确引用减重，不改变任何审查标准。$ref 是本 JSON 的逻辑 JSON pointer；'
    '先展开引用，再读取其子路径。$base 引用同类记录；复制基底对象，删除 omit 中的键，'
    '再用 fields 覆盖同名键（不递归合并）。未覆盖字段逐字继承，覆盖值即使为 null/false/[]'
    '也必须保留；不同 id 的记录并非同一事实或独立证据。所有引用值均在本输入内。'
    'shared_text 是本 case 重复字符串的逐字索引，$ref 展开后仍是字符串。'
    'local_audit_ref 是文件定位或已对账重复审计原值 SHA256 的前12位；完整原件仍保留本地，'
    '此短引用不是事实证据。'
    'shared 仅放本 case 已有的计划、来源、原子和来源状态，不增加用户确认。'
    'delivery_alignment 的 context_sha256 和 basis_ids 沿用原输入：只引用 '
    'delivery_context.states 原有键或其原始子路径，不使用 shared/$ref/$base 地址作为 basis_ids。'
    '来源原文、日期、适用条件、未交叉确认、拒绝理由、价格缺口和正式审查范围均仍有效。'
)
_MARKERS = frozenset(('$ref', '$base', 'local_audit_sha256', 'local_audit_ref', 'shared_text'))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _audit_reference(value):
    return dict(local_audit_ref=hashlib.sha256(_json(value).encode()).hexdigest()[:12])


def _source_locations(record):
    """Only importer bookkeeping and capture locations leave the wire copy."""
    if not isinstance(record, dict):
        return
    for key in ('import_provenance', 'imported_from'):
        value = record.get(key)
        if (isinstance(value, dict) and set(value) <=
                {'json_index', 'producer_import', 'path', 'file_sha256'}
                and value.get('producer_import') in (None, {})
                and isinstance(value.get('path'), str) and value['path'].startswith('/')):
            record[key] = _audit_reference(value)
    provenance = record.get('verification_provenance')
    if isinstance(provenance, dict):
        path = provenance.get('capture_path')
        if isinstance(path, str) and path.startswith('/'):
            provenance['capture_path'] = _audit_reference(path)


def _context_locations(states):
    for key, value in states.items():
        if key.startswith('evidence:') and isinstance(value, dict):
            _source_locations(value.get('source'))
            _source_locations(value.get('seed'))


def _review_shared(case):
    """Keep substantive before/after material and decisions; bind duplicates."""
    from .delivery_consistency import _recorded_projection

    handbook = case['handbook']
    audit = handbook.get('audit', {})
    alignment = audit.get('delivery_alignment', {})
    # Prove these receipts are reproducible before omitting their repeated text.
    # Invalid or older partial ledgers remain fully visible to the reviewer.
    try:
        _recorded_projection(alignment)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    else:
        for key in ('retained', 'reference_normalization'):
            if key in alignment:
                alignment[key] = _audit_reference(alignment[key])
    paths = audit.get('runtime_reference_delivery', {}).get('original_paths')
    if (isinstance(paths, dict) and paths and
            all(isinstance(value, str) and value.startswith('/') for value in paths.values())):
        audit['runtime_reference_delivery']['original_paths'] = _audit_reference(paths)
    states = alignment.get('states', {})
    _context_locations(states)
    for source in handbook.get('sources', []):
        _source_locations(source)
    shared = {key: states[key] for key in ('plan', 'provenance') if key in states}
    shared['sources'] = handbook.get('sources', [])
    shared['supply_seeds'] = [state['seed'] for key, state in states.items()
        if key.startswith('evidence:') and isinstance(state, dict) and state.get('seed')]
    return shared


def _token(key):
    return str(key).replace('~', '~0').replace('/', '~1')


class _References:
    """Exact structure reuse and per-record overrides, scoped to one case."""

    def __init__(self):
        self.exact, self.identities, self.collections = {}, {}, {}

    def _base(self, value, path, collection):
        identifier = value.get('id', value.get('seed_id'))
        if not isinstance(identifier, str) or not identifier:
            return None
        candidates = list(self.identities.get(identifier, []))
        candidates += [row for row in self.collections.get(collection, [])
                       if collection is not None and row[1].keys() == value.keys()]
        best, size = None, len(_json(value))
        for pointer, original in candidates:
            fields = {key: child for key, child in value.items()
                      if key not in original or _json(child) != _json(original[key])}
            candidate = {'$base': pointer, 'fields': fields}
            omitted = sorted(original.keys() - value.keys())
            if omitted:
                candidate['omit'] = omitted
            candidate_size = len(_json(candidate))
            if candidate_size < size:
                best, size = candidate, candidate_size
        self.identities.setdefault(identifier, []).append((path, value))
        if collection is not None:
            self.collections.setdefault(collection, []).append((path, value))
        return best

    def project(self, value, path='#', collection=None):
        if isinstance(value, str) and len(value.encode()) >= 40:
            encoded = _json(value)
            pointer = self.exact.get(encoded)
            if pointer is None or len(path) < len(pointer):
                self.exact[encoded] = path
            if pointer is not None and len(_json({'$ref': pointer}).encode()) < len(encoded.encode()):
                return {'$ref': pointer}
            return value
        if not isinstance(value, (dict, list)):
            return value
        encoded = _json(value)
        pointer = self.exact.get(encoded)
        if pointer is not None and len(_json({'$ref': pointer})) < len(encoded):
            return {'$ref': pointer}
        self.exact[encoded] = path
        if isinstance(value, list):
            return [self.project(child, path + '/' + str(index), collection=path)
                    for index, child in enumerate(value)]
        base = self._base(value, path, collection)
        if base is not None:
            base['fields'] = {key: self.project(child, path + '/' + _token(key))
                              for key, child in base['fields'].items()}
            return base
        return {key: self.project(child, path + '/' + _token(key))
                for key, child in value.items()}


def _visit_text(value, transform):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_visit_text(child, transform) for child in value]
    if isinstance(value, dict):
        return {key: child if key in ('$ref', '$base', 'omit')
                else _visit_text(child, transform) for key, child in value.items()}
    return value


def _shared_text(value, path='#'):
    """Intern only repeated text whose shared representation is smaller."""
    counts = Counter()
    _visit_text(value, lambda text: counts.update([text]) if len(text.encode()) >= 24 else None)
    texts, references = [], {}
    for text, count in counts.items():
        reference = {'$ref': path + '/shared_text/' + str(len(texts))}
        size, ref_size = len(_json(text).encode()), len(_json(reference).encode())
        if count * size > size + count * ref_size + 2:
            texts.append(text)
            references[text] = reference
    if not texts:
        return value
    result = _visit_text(value, lambda text: copy.deepcopy(references.get(text, text)))
    return dict(shared_text=texts, **result)


def _check_markers(value):
    if isinstance(value, dict):
        if value.keys() & _MARKERS:
            raise ValueError('Stage input contains reserved reference keys')
        for child in value.values():
            _check_markers(child)
    elif isinstance(value, list):
        for child in value:
            _check_markers(child)


def project_stage_input(payload, stage):
    """Return a wire-only copy; never feed this projection to state validators.

    The full request-payload artifact and model-independent alignment context
    remain the audit authority. Case-local catalogs cannot lend evidence across
    review cases. Unknown fields remain exact rather than being allowlisted out.
    """
    if stage not in ('assembly', 'review'):
        raise ValueError('Unsupported projected stage: ' + str(stage))
    _check_markers(payload)
    value = copy.deepcopy(payload)
    if stage == 'assembly':
        for key in ('sources', 'supply_seeds'):
            for record in value.get(key, []):
                _source_locations(record)
        _context_locations(value.get('delivery_context', {}).get('states', {}))
        value = _shared_text(_References().project(value))
    elif 'cases' in value:
        cases = []
        for index, case in enumerate(value['cases']):
            shared = _review_shared(case)
            path = '#/cases/' + str(index)
            cases.append(_shared_text(_References().project(dict(shared=shared, **case), path), path))
        value['cases'] = cases
    elif 'handbook' in value:
        shared = _review_shared(value)
        value = _shared_text(_References().project(dict(shared=shared, **value)))
    else:
        value = _shared_text(_References().project(value))
    return dict(reference_contract=REFERENCE_CONTRACT, **value)
