"""Audited, non-semantic handling of empty extras at model output boundaries.

Only an empty string at a field explicitly forbidden by a published schema may
be removed. Declared values are never coerced; existing validators retain their
native lossless type acceptance. Open dictionaries and ambiguous schemas stay
unchanged. This component neither validates a response nor changes a verdict.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

BOUNDARY_ARTIFACT_ROOT = Path.home() / '.cache' / 'trip-autopilot' / 'model-boundary'
_ANNOTATIONS = {'$defs', 'definitions', '$schema', '$id', 'title', 'description',
                'default', 'examples', 'readOnly', 'writeOnly', 'deprecated'}


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _resolve(schema, root, seen=()):
    if not isinstance(schema, dict) or '$ref' not in schema:
        return schema
    ref = schema['$ref']
    if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
        return {}  # No remote resolution or guesses about recursive definitions.
    target = root
    try:
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
    except (KeyError, TypeError):
        return {}
    resolved = _resolve(target, root, (*seen, ref))
    siblings = {key: value for key, value in schema.items()
                if key != '$ref' and key not in _ANNOTATIONS}
    return {'allOf': [resolved, siblings]} if siblings else resolved


def _compatible(value, schema, root):
    """Prune only outer-shape mismatches, never failed field validation.

    Missing required fields or wrong declared types must not eliminate an
    object branch: doing so can turn that branch's declared field into an
    extra in another branch and erase the very error the validator must see.
    """
    schema = _resolve(schema, root)
    if schema is False:
        return False
    if not isinstance(schema, dict):
        return True
    kind = schema.get('type')
    types = kind if isinstance(kind, list) else [kind]
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
        'string': isinstance(value, str), 'integer': type(value) is int,
        'number': type(value) in (int, float), 'boolean': type(value) is bool,
        'null': value is None, None: True}
    return any(matches.get(item, True) for item in types)


def _shared_changes(value, schemas, root, path):
    """Only edits agreed by every possible branch are safe."""
    changes = [_walk(value, item, root, path)[1] for item in schemas]
    if not changes:
        return copy.deepcopy(value), []
    common = [entry for entry in changes[0] if all(entry in rows for rows in changes[1:])]
    result = copy.deepcopy(value)
    for entry in common:
        relative = entry['segments'][len(path):]
        parent = result
        for segment in relative[:-1]:
            parent = parent[segment]
        parent.pop(relative[-1])
    return result, common


def _walk(value, schema, root, path=()):
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return copy.deepcopy(value), []
    for keyword in ('anyOf', 'oneOf', 'allOf'):
        if keyword in schema:
            branches = schema[keyword]
            if keyword != 'allOf':
                branches = [item for item in branches if _compatible(value, item, root)]
            # Siblings are simultaneous constraints; a sibling declaration must
            # protect its field even if a branch would otherwise strip it.
            siblings = {key: item for key, item in schema.items() if key != keyword
                        and key not in _ANNOTATIONS}
            if siblings:
                branches = [{'allOf': [item, siblings]} for item in branches]
            return _shared_changes(value, branches, root, path)
    if any(key in schema for key in ('if', 'then', 'else', 'not', 'dependentSchemas')):
        return copy.deepcopy(value), []
    if isinstance(value, list):
        result, changes = [], []
        for index, item in enumerate(value):
            prefix = schema.get('prefixItems', [])
            child_schema = prefix[index] if index < len(prefix) else schema.get('items', {})
            child, rows = _walk(item, child_schema, root, (*path, index))
            result.append(child)
            changes.extend(rows)
        return result, changes
    if not isinstance(value, dict):
        return copy.deepcopy(value), []
    result, changes = {}, []
    properties = schema.get('properties', {})
    for key, item in value.items():
        matches = [child for pattern, child in schema.get('patternProperties', {}).items()
                   if re.search(pattern, key)]
        if key in properties:
            matches.insert(0, properties[key])
        if not matches and schema.get('additionalProperties') is False and type(item) is str and item == '':
            changes.append({'segments': [*path, key], 'original_value': '',
                            'operation': 'remove_undeclared_empty_string'})
            continue
        child_schema = ({'allOf': matches} if len(matches) > 1 else matches[0]) if matches else schema.get('additionalProperties', {})
        child, rows = _walk(item, child_schema, root, (*path, key))
        result[key] = child
        changes.extend(rows)
    return result, changes


def normalize_model_output(value, schema, *, request_id):
    """Return a fresh value plus a JSON-serializable audit; input never mutates."""
    schema = schema.model_json_schema() if hasattr(schema, 'model_json_schema') else schema
    normalized, changes = _walk(value, schema, schema or {})
    entries = [{**row, 'request_id': request_id,
                'path': '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1')
                                     for part in row['segments'])} for row in changes]
    audit = {'version': 1, 'request_id': request_id, 'changes': entries,
             'raw_sha256': _digest(value), 'normalized_sha256': _digest(normalized),
             'schema_status': 'available' if schema is not None else 'unavailable',
             'status': 'normalized' if changes else 'unchanged',
             'type_policy': 'no coercion; existing validator native acceptance only',
             'validation_performed': False, 'semantic_truth_verified': False}
    return normalized, audit


def recorded_model_output(value, schema, *, request_id, artifacts, preserve_raw=True):
    """Persist raw input before processing and persist every edit before validation."""
    from .comparison_card_budget import private_json
    artifacts = Path(artifacts)
    if preserve_raw:
        private_json(artifacts / f'{request_id}-generated-raw.json', value)
    normalized, audit = normalize_model_output(value, schema, request_id=request_id)
    audit['input_stage'] = 'original_model_value' if preserve_raw else 'existing_adapter_canonical'
    audit['raw_artifact'] = str(artifacts / f'{request_id}-generated-raw.json')
    private_json(artifacts / f'{request_id}-boundary-normalization.json', audit)
    private_json(artifacts / f'{request_id}-boundary-normalized.json', normalized)
    return normalized


def boundary_output_type(model_type, *, request_id, artifacts):
    """Attach the same pre-validation boundary to every Pydantic Agent output."""
    from typing import Annotated
    from pydantic import BeforeValidator
    def before(value):
        return recorded_model_output(value, model_type, request_id=request_id, artifacts=artifacts)
    return Annotated[model_type, BeforeValidator(before)]


def normalize_json_response(value, schema, metrics):
    """Legacy transport: keep private raw evidence outside public usage metrics."""
    from .comparison_card_budget import private_json
    request_id = metrics.get('provider_request_id') or 'local-' + uuid4().hex
    artifacts = BOUNDARY_ARTIFACT_ROOT / uuid4().hex
    private_json(artifacts / 'raw.json', value)
    normalized, audit = normalize_model_output(value, schema, request_id=request_id)
    private_json(artifacts / 'normalized.json', normalized)
    private_json(artifacts / 'audit.json', audit)
    metrics['model_boundary'] = {**audit, 'artifacts': str(artifacts)}
    return normalized
