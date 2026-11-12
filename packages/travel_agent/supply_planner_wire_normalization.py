"""Planner-only, audited deletion of one uniquely removable JSON closing delimiter.

The original transport records and charges the unmodified response first. This
adapter never infers missing content or verifies the truth of the resulting plan.
"""
from __future__ import annotations

import hashlib
import json
import math

import httpx2 as httpx

from .comparison_card_budget import private_json
from .comparison_card_model import RecordedTransport
from .model_boundary import normalize_model_output
from .supply_output_normalization import normalize_output_envelope


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('Non-finite JSON number')
    return value


def _reject_constant(_text):
    raise ValueError('Non-JSON constant')


def _strict_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=_finite_float)


def _delimiters(raw):
    quoted = escaped = False
    for position, char in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{}[]':
            yield position, char


def _first_mismatched_closer(raw):
    stack = []
    for position, char in _delimiters(raw):
        if char in '{[':
            stack.append(char)
        elif not stack or {'}': '{', ']': '['}[char] != stack[-1]:
            return position, ''.join(stack)
        else:
            stack.pop()
    return None, None


def _validated_deletion(arguments, position, plan_type, request_id=None):
    fixed = arguments[:position] + arguments[position + 1:]
    parsed = _strict_json(fixed)
    canonical, envelope = normalize_output_envelope(parsed, expected_fields=plan_type.model_fields)
    canonical, boundary = normalize_model_output(canonical, plan_type, request_id=request_id)
    envelope['model_boundary'] = boundary
    plan_type.model_validate(canonical, strict=True)
    return fixed, envelope


def _repair_arguments(arguments, plan_type, audit):
    audit.update(original_arguments=arguments, original_arguments_sha256=_digest(arguments.encode()))
    try:
        _strict_json(arguments)
    except json.JSONDecodeError as error:
        position = error.pos
        audit['json_error'] = {'message': error.msg, 'unicode_codepoint_index': position}
    else:
        audit.update(status='unchanged', reason='arguments_already_valid_json')
        return None
    mismatch, stack = _first_mismatched_closer(arguments)
    if mismatch != position or position >= len(arguments) or arguments[position] not in '}]':
        audit['reason'] = 'decoder_error_is_not_unmatched_closer'
        return None
    audit['reason'] = 'deletion_does_not_validate_as_strict_plan'
    fixed, envelope = _validated_deletion(arguments, position, plan_type, audit['request_id'])
    admissible = [position]
    for candidate, char in _delimiters(arguments):
        if char not in '}]' or candidate == position:
            continue
        try:
            _validated_deletion(arguments, candidate, plan_type, audit['request_id'])
        except (ValueError, TypeError, RecursionError):
            continue
        admissible.append(candidate)
    audit['admissible_deletion_positions'] = sorted(admissible)
    if len(admissible) != 1:
        audit['reason'] = 'ambiguous_single_closer_deletion'
        return None
    byte_start = len(arguments[:position].encode())
    audit.update(status='repaired', reason='unique_single_unmatched_closer_deletion',
        canonical_arguments=fixed, canonical_arguments_sha256=_digest(fixed.encode()),
        envelope_normalization=envelope,
        edit={'deleted': arguments[position], 'unicode_codepoint_span': [position, position + 1],
              'utf8_byte_span': [byte_start, byte_start + 1], 'stack_before': stack,
              'span_convention': 'zero_based_half_open', 'outside_string': True})
    return fixed


def _final_result_function(body):
    if not isinstance(body, dict) or not isinstance(body.get('choices'), list) or len(body['choices']) != 1:
        return None
    choice = body['choices'][0]
    if not isinstance(choice, dict) or choice.get('finish_reason') != 'tool_calls':
        return None
    message = choice.get('message')
    if (not isinstance(message, dict) or message.get('role') != 'assistant'
            or message.get('refusal') or message.get('function_call') is not None):
        return None
    calls = message.get('tool_calls')
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return None
    call = calls[0]
    function = call.get('function')
    if (call.get('type') != 'function' or not isinstance(function, dict)
            or function.get('name') != 'final_result' or not isinstance(function.get('arguments'), str)):
        return None
    return function


def normalize_planner_response(raw: bytes, *, plan_type, request_id=None) -> tuple[bytes, dict]:
    """Return original bytes on every refusal; only one validated edit may reach the SDK."""
    audit = {'version': 1, 'request_id': request_id, 'status': 'rejected', 'reason': 'invalid_outer_json_or_protocol',
             'raw_body_sha256': _digest(raw), 'sdk_body_sha256': _digest(raw),
             'semantic_truth_verified': False, 'unrestricted_model_intent_verified': False}
    try:
        body = _strict_json(raw)
        function = _final_result_function(body)
        if function is None:
            return raw, audit
        audit['reason'] = 'non_strict_arguments_json'
        fixed = _repair_arguments(function['arguments'], plan_type, audit)
        if fixed is None:
            return raw, audit
        function['arguments'] = fixed
        normalized = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        audit['status'] = 'rejected'
        return raw, audit
    audit['sdk_body_sha256'] = _digest(normalized)
    return normalized, audit


class PlannerRecordedTransport(RecordedTransport):
    """Keep shared recording/accounting authoritative, then adapt only planner output."""

    def __init__(self, *args, plan_type, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan_type = plan_type

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if not response.is_success:
            return response
        raw = await response.aread()
        normalized, audit = normalize_planner_response(raw, plan_type=self.plan_type,
                                                      request_id=self.request_id)
        audit['raw_recording_precedes_adapter'] = True
        private_json(self.artifacts / f'{self.request_id}-wire-normalization.json', audit)
        if normalized == raw:
            return response
        headers = [(key, value) for key, value in response.headers.multi_items()
                   if key.lower() not in {'content-length', 'content-encoding'}]
        return httpx.Response(response.status_code, content=normalized, headers=headers,
                              extensions=response.extensions)
