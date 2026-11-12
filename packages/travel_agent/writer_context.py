"""Scope-owned model context and fail-closed request admission.

This module decides what a revision model may *see*.  Edit authority remains in
``writer_scope.validate_scope_diff`` and the complete candidate validators.
Keeping those responsibilities separate prevents a read expansion from
silently becoming permission to rewrite unrelated days.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from .detail_reviews import source_withdrawn
from .source_policy_view import source_policy_view
from .validation import PlanError


SCOPED_WRITER_INPUT_LIMIT = 24_000
FULL_WRITER_INPUT_LIMIT = 48_000
SCOPED_CRITIC_INPUT_BYTE_LIMIT = 160_000
FULL_CRITIC_INPUT_BYTE_LIMIT = 320_000
_REFERENCE_FIELDS = frozenset((
    'id', 'source_id', 'source_ids', 'evidence_ids', 'material_id', 'detail_id',
    'detail_ids', 'guide_step_ids', 'supply_id', 'supply_ids', 'item_id',
    'item_ids', 'related_item_ids',
))
_SCOPED_BROAD_ROOTS = frozenset((
    '/plan', '/plan/days', '/materials', '/candidates', '/source_evidence',
    '/pending_materials', '/detail_context',
))


class ContextAdmissionError(PlanError):
    """A request was rejected locally before any provider dispatch."""

    code = 'context_input_too_large'

    def __init__(self, *, actual, limit, mode, consumer):
        self.actual = actual
        self.limit = limit
        self.mode = mode
        self.consumer = consumer
        super().__init__(
            f'{self.code}: {consumer}上下文为{actual} tokens，超过{mode}上限{limit}；'
            '请求未发送，请缩小scope或使用分片')


@dataclass(frozen=True)
class ContextSpec:
    """Explicit context and admission policy for one model consumer."""

    mode: str
    consumer: str
    read_paths: tuple[str, ...]
    edit_paths: tuple[str, ...]
    max_input_tokens: int | None = None
    max_input_bytes: int | None = None
    max_reference_hops: int = 1

    def __post_init__(self):
        object.__setattr__(self, 'read_paths', tuple(self.read_paths))
        object.__setattr__(self, 'edit_paths', tuple(self.edit_paths))
        if self.mode not in ('scoped', 'full'):
            raise PlanError('上下文模式必须为scoped或full')
        if self.consumer not in ('writer', 'critic', 'large_call'):
            raise PlanError('上下文consumer不受支持')
        for value, label in ((self.max_input_tokens, 'token'),
                             (self.max_input_bytes, 'byte')):
            if value is not None and (type(value) is not int or value <= 0):
                raise PlanError('上下文' + label + '上限必须为正整数')
        if self.max_input_tokens is None and self.max_input_bytes is None:
            raise PlanError('上下文必须有发送前上限')
        if self.max_reference_hops != 1:
            raise PlanError('上下文依赖只允许一跳，避免无界扩散')


@dataclass(frozen=True)
class ContextBundle:
    payload_nodes: dict
    included_paths: tuple[str, ...]
    read_only_paths: tuple[str, ...]
    supporting_paths: tuple[str, ...]
    editable_paths: tuple[str, ...]
    directory: dict
    manifest: dict


@dataclass(frozen=True)
class PreparedContext:
    messages: tuple[dict, ...]
    measurement: dict


def _token(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def _parts(path):
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 1000:
        raise PlanError('读取路径必须是有效JSON Pointer')
    pieces = path[1:].split('/')
    for piece in pieces:
        if '~' in piece.replace('~0', '').replace('~1', ''):
            raise PlanError('JSON Pointer转义无效')
    return [piece.replace('~1', '/').replace('~0', '~') for piece in pieces]


def _at(document, path):
    value = document
    try:
        for key in _parts(path):
            if isinstance(value, list):
                if not key.isdigit() or str(int(key)) != key:
                    raise KeyError(key)
                value = value[int(key)]
            else:
                value = value[key]
    except (KeyError, IndexError, TypeError):
        raise PlanError('读取路径不存在：' + path[:200]) from None
    return value


def _references(value):
    result = set()

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in _REFERENCE_FIELDS:
                    values = child if isinstance(child, list) else [child]
                    result.update(item for item in values if isinstance(item, str) and item)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return result


def _record_refs(value):
    if not isinstance(value, dict):
        return set()
    refs = set()
    for key in _REFERENCE_FIELDS:
        child = value.get(key)
        values = child if isinstance(child, list) else [child]
        refs.update(item for item in values if isinstance(item, str) and item)
    return refs


def _field_references(value, fields):
    result = set()

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in fields:
                    values = child if isinstance(child, list) else [child]
                    result.update(item for item in values if isinstance(item, str) and item)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return result


def _append(nodes, path, value):
    if path not in nodes:
        nodes[path] = source_policy_view(copy.deepcopy(value))


def _selected_day_indices(paths):
    result = set()
    for path in paths:
        pieces = _parts(path)
        if len(pieces) >= 3 and pieces[:2] == ['plan', 'days'] and pieces[2].isdigit():
            result.add(int(pieces[2]))
    return result


def _boundary_nodes(document, selected_days, nodes):
    days = document.get('plan', {}).get('days', [])
    boundaries = []
    for index in sorted(selected_days):
        if index > 0 and index - 1 not in selected_days:
            items = days[index - 1].get('items', [])
            if items:
                path = f'/plan/days/{index - 1}/items/{len(items) - 1}'
                _append(nodes, path, items[-1])
                boundaries.append(path)
        if index + 1 < len(days) and index + 1 not in selected_days:
            items = days[index + 1].get('items', [])
            if items:
                path = f'/plan/days/{index + 1}/items/0'
                _append(nodes, path, items[0])
                boundaries.append(path)
    return boundaries


def _plan_dependencies(document, refs, nodes):
    plan = document.get('plan', {})
    groups = (
        ('decisions', plan.get('decisions', [])),
        ('preparation', plan.get('preparation', [])),
    )
    for name, records in groups:
        for index, row in enumerate(records if isinstance(records, list) else []):
            if _record_refs(row).intersection(refs):
                _append(nodes, f'/plan/{name}/{index}', row)
    pack = plan.get('preparation_pack') or {}
    actions = pack.get('actions', []) if isinstance(pack, dict) else []
    for index, row in enumerate(actions if isinstance(actions, list) else []):
        if _record_refs(row).intersection(refs):
            _append(nodes, f'/plan/preparation_pack/actions/{index}', row)


def _evidence_dependencies(document, seed_nodes, nodes):
    # A common supply_id can have hundreds of historical receipts.  It may pull
    # the one supply catalog record, but never every evidence row.  Evidence and
    # materials require their own explicit source/material/detail reference.
    supplies = _field_references(seed_nodes, {'supply_id', 'supply_ids'})
    sources = _field_references(seed_nodes, {'source_id', 'source_ids', 'evidence_ids'})
    materials = _field_references(seed_nodes, {'material_id'})
    details = _field_references(seed_nodes, {'detail_id', 'detail_ids', 'guide_step_ids'})

    for index, row in enumerate(document.get('candidates', [])):
        if isinstance(row, dict) and row.get('id') in supplies:
            _append(nodes, f'/candidates/{index}', row)
    for name, accepted in (('materials', materials | sources),
                           ('source_evidence', sources),
                           ('pending_materials', materials | sources)):
        for index, row in enumerate(document.get(name, [])):
            identities = ({row.get(key) for key in (
                'id', 'material_id', 'source_id', 'evidence_id')} - {None}
                if isinstance(row, dict) else set())
            if identities.intersection(accepted):
                _append(nodes, f'/{name}/{index}', row)

    detail = document.get('detail_context') or {}
    ledger = detail.get('detail_ledger') or {}
    for index, row in enumerate(ledger.get('materials', [])):
        if source_withdrawn(row):
            continue
        related = _record_refs(row)
        for child in row.get('details', []) + row.get('guide_steps', []):
            if isinstance(child, dict):
                related.update(_record_refs(child))
        if (({row.get('material_id')} - {None}).intersection(materials | sources)
                or related.intersection(details)):
            _append(nodes, f'/detail_context/detail_ledger/materials/{index}', row)
    for index, row in enumerate(detail.get('research_gaps', [])):
        if (_field_references(row, {'source_id', 'material_id'}).intersection(sources | materials)
                or _field_references(row, {'detail_id', 'detail_ids', 'guide_step_ids'}).intersection(details)):
            _append(nodes, f'/detail_context/research_gaps/{index}', row)
    for index, row in enumerate(ledger.get('corroborations', [])):
        if _field_references(row, {'detail_id', 'detail_ids'}).intersection(details):
            _append(nodes, f'/detail_context/detail_ledger/corroborations/{index}', row)


def _preview(value):
    if isinstance(value, dict):
        fields = ('id', 'day', 'date', 'city', 'title', 'task', 'category', 'status',
                  'time', 'end_time', 'supply_id', 'related_item_ids')
        value = {key: value[key] for key in fields if key in value} or {'fields': list(value)[:20]}
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return encoded[:240] + ('…' if len(encoded) > 240 else '')


def _local_directory(document, nodes, selected_days, boundaries):
    days = document.get('plan', {}).get('days', [])
    outline = []
    for index, day in enumerate(days):
        if index in selected_days or any(path.startswith(f'/plan/days/{index}/') for path in boundaries):
            outline.append({key: day[key] for key in ('day', 'date', 'city', 'title') if key in day})
    return {
        'nodes': [{'path': path, 'preview': _preview(value)} for path, value in nodes.items()],
        'day_outline': outline,
        'scope_note': '目录仅覆盖本次scope；未列内容由程序原样保留，不代表可编辑。',
    }


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        source_policy_view(copy.deepcopy(value)), ensure_ascii=False,
        sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def _manifest(spec, nodes, boundaries, directory, *, payload=None):
    included = tuple(nodes)
    read_only = tuple(path for path in included if path in boundaries)
    supporting = tuple(path for path in included if path not in read_only and not any(
        path == root or path.startswith(root + '/') or root.startswith(path + '/')
        for root in spec.edit_paths))
    directory_paths = tuple(row['path'] for row in directory['nodes'])
    body = {
        'manifest_version': 2,
        'mode': spec.mode,
        'consumer': spec.consumer,
        'included_paths': list(included),
        'read_only_paths': list(read_only),
        'supporting_paths': list(supporting),
        'editable_paths': list(spec.edit_paths),
        'directory_paths': list(directory_paths),
        'unindexed_paths': [path for path in included if path not in directory_paths],
        'max_reference_hops': spec.max_reference_hops,
        'max_input_tokens': spec.max_input_tokens,
        'max_input_bytes': spec.max_input_bytes,
        'payload_sha256': _canonical_sha256(nodes if payload is None else payload),
        'directory_sha256': _canonical_sha256(directory),
    }
    return {**body, 'manifest_sha256': _canonical_sha256(body)}


def bind_context_manifest(bundle, payload, *, consumer=None):
    """Bind an already assembled boundary to the exact next model payload.

    Critic payloads add program-derived before/after nodes after the writer bundle
    was assembled.  Those values may be reviewed, but they must not become seeds
    for another catalog scan.  Rebinding records their paths and content while
    preserving the bundle's read-only boundaries and edit authority.
    """
    if not isinstance(bundle, ContextBundle) or not isinstance(payload, dict):
        raise PlanError('上下文manifest绑定输入无效')
    mode = bundle.manifest.get('mode')
    target = consumer or bundle.manifest.get('consumer')
    if target == 'writer':
        token_limit = SCOPED_WRITER_INPUT_LIMIT if mode == 'scoped' else FULL_WRITER_INPUT_LIMIT
        byte_limit = None
    elif target == 'critic':
        token_limit = None
        byte_limit = (SCOPED_CRITIC_INPUT_BYTE_LIMIT if mode == 'scoped'
                      else FULL_CRITIC_INPUT_BYTE_LIMIT)
    elif target == 'large_call':
        token_limit = bundle.manifest.get('max_input_tokens')
        byte_limit = bundle.manifest.get('max_input_bytes')
    else:
        raise PlanError('上下文consumer不受支持')
    nodes = copy.deepcopy(bundle.payload_nodes)
    for field in ('previous_nodes', 'candidate_nodes'):
        visible = payload.get(field)
        if isinstance(visible, dict):
            for path, value in visible.items():
                if isinstance(path, str):
                    nodes[path] = copy.deepcopy(value)
    if 'candidate' in payload:
        nodes['/candidate'] = copy.deepcopy(payload['candidate'])
    spec = ContextSpec(
        mode=mode,
        consumer=target,
        read_paths=tuple(nodes),
        edit_paths=bundle.editable_paths,
        max_input_tokens=token_limit,
        max_input_bytes=byte_limit,
        max_reference_hops=bundle.manifest.get('max_reference_hops', 1),
    )
    return _manifest(spec, nodes, set(bundle.read_only_paths), bundle.directory,
                     payload=payload)


def assemble_context(document, spec, *, existing_paths=()):
    """Return a stable full/scoped projection without mutating ``document``."""
    if not isinstance(document, dict) or not isinstance(spec, ContextSpec):
        raise PlanError('上下文装配输入无效')
    requested = list(dict.fromkeys((*spec.read_paths, *tuple(existing_paths))))
    if not requested:
        raise PlanError('上下文至少需要一个读取路径')
    broad = sorted(set(requested).intersection(_SCOPED_BROAD_ROOTS))
    if spec.mode == 'scoped' and broad:
        raise PlanError('scoped上下文不能读取全量根：' + ', '.join(broad)
                        + '；请选择具体日期/资料路径，或重新论证full必要性')
    requested_days = _selected_day_indices(requested)
    editable_days = _selected_day_indices(spec.edit_paths)
    selected_days = (requested_days & editable_days) if editable_days else requested_days
    if spec.mode == 'scoped' and selected_days:
        day_roots = tuple(f'/plan/days/{index}' for index in sorted(selected_days))
        requested = list(dict.fromkeys(
            next((root for root in day_roots if path == root or path.startswith(root + '/')), path)
            for path in requested))
    nodes = {}
    for path in requested:
        _append(nodes, path, _at(document, path))
    boundaries = []
    if spec.mode == 'scoped':
        # References are frozen before adding boundaries/dependencies: one hop
        # is useful context; newly discovered IDs never recursively seed a scan.
        seed_nodes = copy.deepcopy(nodes)
        seed_refs = _references(seed_nodes)
        boundaries = _boundary_nodes(document, selected_days, nodes)
        _plan_dependencies(document, seed_refs, nodes)
        _evidence_dependencies(document, seed_nodes, nodes)
        directory = _local_directory(document, nodes, selected_days, boundaries)
    else:
        directory = _local_directory(document, nodes, selected_days, boundaries)
    manifest = _manifest(spec, nodes, boundaries, directory)
    return ContextBundle(
        payload_nodes=nodes,
        included_paths=tuple(nodes),
        read_only_paths=tuple(manifest['read_only_paths']),
        supporting_paths=tuple(manifest['supporting_paths']),
        editable_paths=tuple(spec.edit_paths),
        directory=directory,
        manifest=manifest,
    )


def _token_count(measurement):
    if type(measurement) is int:
        return measurement, {'preflight_input_tokens': measurement}
    if not isinstance(measurement, dict):
        raise PlanError('上下文token度量必须返回整数或对象')
    actual = measurement.get('preflight_input_tokens')
    if type(actual) is not int or actual < 0:
        raise PlanError('上下文token度量缺少preflight_input_tokens')
    return actual, copy.deepcopy(measurement)


def admit_messages(messages, spec, *, token_meter, caller=None, call_kwargs=None):
    """Measure final messages and optionally dispatch only after admission."""
    if not isinstance(spec, ContextSpec):
        raise PlanError('上下文发送策略无效')
    if spec.max_input_tokens is None:
        raise PlanError('该上下文没有可用的token门')
    measurement = token_meter(messages)
    actual, facts = _token_count(measurement)
    if actual > spec.max_input_tokens:
        raise ContextAdmissionError(actual=actual, limit=spec.max_input_tokens,
                                    mode=spec.mode, consumer=spec.consumer)
    prepared = PreparedContext(tuple(copy.deepcopy(messages)), facts)
    if caller is None:
        return prepared
    return caller(list(prepared.messages), **dict(call_kwargs or {}))


def enforce_preflight_budget(measurement, *, limit, mode, consumer):
    """Use an already-computed official preflight measurement without retokenizing."""
    spec = ContextSpec(mode=mode, consumer=consumer, read_paths=('/plan',),
                       edit_paths=('/plan',), max_input_tokens=limit)
    actual, _ = _token_count(measurement)
    if actual > limit:
        raise ContextAdmissionError(actual=actual, limit=limit, mode=mode, consumer=consumer)
    return spec
