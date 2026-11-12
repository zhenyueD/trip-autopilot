"""Session-only format degradation; strict native/autopilot callers are unchanged."""
from copy import deepcopy
import json

from .autopilot_model import _assistant, _Rejected
from .autopilot_tokenizer import MAX_TOOL_CALLS, strict_json, validate_calls
from .writer_tokenizer import WriterTokenizerError


def _schema_error(value, schema, path='arguments'):
    """Validate the small schema vocabulary advertised by session tools, unchanged."""
    kind = schema.get('type')
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
               'string': isinstance(value, str), 'number': type(value) in (int, float)}
    if not matches.get(kind, False):
        return f'{path}: type must be {kind}'
    if 'enum' in schema and value not in schema['enum']:
        return f'{path}: enum must be {schema["enum"]}'
    if kind == 'string' and len(value) > schema.get('maxLength', len(value)):
        return f'{path}: maxLength={schema["maxLength"]}'
    if kind == 'object':
        missing = sorted(set(schema.get('required', [])) - set(value))
        if missing:
            return f'{path}: required={missing}'
        extra = sorted(set(value) - set(schema.get('properties', {})))
        if extra and schema.get('additionalProperties') is False:
            return f'{path}: additionalProperties=false; unexpected={extra}'
        children = ((item, schema['properties'][key], f'{path}.{key}')
                    for key, item in value.items() if key in schema.get('properties', {}))
    elif kind == 'array':
        children = ((item, schema['items'], f'{path}[{index}]') for index, item in enumerate(value))
    else:
        children = ()
    for item, child, location in children:
        error = _schema_error(item, child, location)
        if error:
            return error
    return None


def session_assistant(body, tools, previous_ids):
    """Return protocol message and per-call rejects, preserving the ordinal cap.

    Only provider metadata is removed. Arguments are never repaired/coerced.
    Envelope errors remain fatal; a bad call cannot discard unrelated calls.
    """
    schemas = {t['function']['name']: t['function']['parameters'] for t in tools}
    choices = body.get('choices')
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get('message'), dict)):
        return _assistant(body, set(schemas), previous_ids), []
    choice, message = choices[0], choices[0]['message']
    raw_calls = message.get('tool_calls')
    if raw_calls is None or raw_calls == []:
        return _assistant(body, set(schemas), previous_ids), []
    # Check the response envelope before isolating individual tool calls.
    envelope = deepcopy(body)
    envelope['choices'][0]['message'].pop('tool_calls', None)
    if message.get('content') in (None, ''):
        envelope['choices'][0]['message']['content'] = 'Tool call format feedback follows.'
    envelope['choices'][0]['finish_reason'] = 'stop'
    _assistant(envelope, set(schemas), previous_ids)
    if choice.get('finish_reason') != 'tool_calls':
        raise _Rejected('inconsistent_finish_reason', 'format')
    accepted, rejected, ids = [], [], set(previous_ids)
    for ordinal, raw in enumerate(raw_calls if isinstance(raw_calls, list) else [raw_calls], 1):
        function = raw.get('function') if isinstance(raw, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        rule = None
        call = raw
        if isinstance(raw, dict) and isinstance(function, dict):
            call = {key: raw[key] for key in ('id', 'type', 'function') if key in raw}
            call['function'] = {key: function[key] for key in ('name', 'arguments') if key in function}
        try:
            if not isinstance(raw_calls, list):
                rule = 'tool_calls: type must be array'
            elif ordinal > MAX_TOOL_CALLS:
                rule = f'tool_calls: MAX_TOOL_CALLS={MAX_TOOL_CALLS}; ordinal={ordinal}'
            else:
                call_ids = validate_calls([call], set(schemas))
                if call_ids & ids:
                    rule = 'duplicate_tool_call_id'
                else:
                    rule = _schema_error(strict_json(call['function']['arguments']), schemas[name])
            if rule is None:
                accepted.append(call)
                ids.update(call_ids)
        except (WriterTokenizerError, ValueError, RecursionError) as error:
            rule = getattr(error, 'code', None) or type(error).__name__
            if rule == 'invalid_tool_arguments':
                arguments = call['function'].get('arguments')
                if not isinstance(arguments, str):
                    rule = 'arguments: type must be JSON string'
                else:
                    try:
                        parsed = strict_json(arguments)
                        if not isinstance(parsed, dict):
                            rule = 'arguments: decoded type must be object'
                    except json.JSONDecodeError as detail:
                        rule = f'arguments: JSON syntax at line {detail.lineno} column {detail.colno}: {detail.msg}'
                    except (WriterTokenizerError, ValueError, RecursionError) as detail:
                        rule = 'arguments: ' + (getattr(detail, 'code', None) or type(detail).__name__)
        if rule:
            rejected.append({'ordinal': ordinal, 'name': name if isinstance(name, str) else None,
                             'id': raw.get('id') if isinstance(raw, dict) else None,
                             'rule': rule, 'status': 'discarded'})
    clean = deepcopy(body)
    clean['choices'][0]['message']['tool_calls'] = accepted
    if accepted:
        return _assistant(clean, set(schemas), previous_ids), rejected
    # No invented tool IDs/results: a plain assistant plus user feedback is a
    # valid continuation, including when every original ID was malformed.
    return _assistant(envelope, set(schemas), previous_ids), rejected


def degradation_feedback():
    return {'role': 'user', 'content': 'All tool calls in the preceding response were discarded '
            'by local format validation; no tools executed. Submit valid calls using the advertised '
            f'schema, unique IDs and at most {MAX_TOOL_CALLS} calls. Save/finish is still required.'}
