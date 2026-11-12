"""Offline full native-message preflight using the pinned official V4 encoder.

Counts describe the public reference template, never provider billing. Only text
messages and function tools are admitted; unmeasurable extensions fail closed.
"""
import copy
import hashlib
import json
import math
import re
from functools import lru_cache
from types import ModuleType

from .writer_tokenizer import (WriterTokenizerError, _base_facts, _load_tokenizer,
                               TOKENIZER_ASSET, SUPPORTED_MODEL,
                               PUBLIC_REFERENCE_ENCODER_SHA256)

MAX_TOOL_CALLS = 16
ENCODER_ASSET = TOKENIZER_ASSET.with_name('encoding_dsv4.py')


def _reject(code):
    raise WriterTokenizerError(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject('duplicate_json_key')
        result[key] = value
    return result


def strict_json(text):
    def invalid_constant(value):
        _reject('non_finite_json')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            _reject('non_finite_json')
        return number
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid_constant,
                      parse_float=finite_float)


def validate_tools(tools):
    if not isinstance(tools, list) or not 1 <= len(tools) <= 128:
        _reject('invalid_tools')
    names = set()
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {'type', 'function'}
                or tool['type'] != 'function' or not isinstance(tool['function'], dict)):
            _reject('invalid_tools')
        function = tool['function']
        name = function.get('name')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
                or name in names or not isinstance(function.get('parameters'), dict)
                or function['parameters'].get('type') != 'object'):
            _reject('invalid_tools')
        names.add(name)
    return names


def validate_calls(calls, names):
    if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_TOOL_CALLS:
        _reject('invalid_tool_calls')
    ids = set()
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {'id', 'type', 'function'}
                or call.get('type') != 'function' or not isinstance(call.get('function'), dict)):
            _reject('invalid_tool_call_shape')
        ident, function = call.get('id'), call['function']
        # Provider IDs are opaque correlation values, not function names.
        if not isinstance(ident, str) or not 1 <= len(ident) <= 256:
            _reject('invalid_tool_call_id')
        if ident in ids:
            _reject('duplicate_tool_call_id')
        if set(function) != {'name', 'arguments'}:
            _reject('invalid_tool_function_shape')
        if not isinstance(function.get('name'), str) or function['name'] not in names:
            _reject('unregistered_tool_name')
        if not isinstance(function.get('arguments'), str):
            _reject('invalid_tool_arguments')
        try:
            arguments = strict_json(function['arguments'])
        except (ValueError, RecursionError):
            _reject('invalid_tool_arguments')
        if not isinstance(arguments, dict):
            _reject('invalid_tool_arguments')
        ids.add(ident)
    return ids


def validate_messages(messages, names):
    if not isinstance(messages, list) or not messages:
        _reject('invalid_messages')
    pending, seen = set(), set()
    for message in messages:
        if not isinstance(message, dict):
            _reject('invalid_message')
        role = message.get('role')
        allowed = {'role', 'content'}
        if role == 'assistant':
            allowed |= {'tool_calls', 'reasoning_content'}
        elif role == 'tool':
            allowed |= {'tool_call_id'}
        elif role not in ('system', 'user'):
            _reject('unsupported_role')
        if set(message) - allowed:
            _reject('unsupported_message_field')
        content = message.get('content')
        if not isinstance(content, str) and not (role == 'assistant' and content is None):
            _reject('invalid_content')
        if role != 'tool' and pending:
            _reject('missing_tool_result')
        if role == 'tool':
            ident = message.get('tool_call_id')
            if not isinstance(ident, str) or ident not in pending:
                _reject('unexpected_tool_result')
            pending.remove(ident)
        if role == 'assistant':
            reasoning = message.get('reasoning_content')
            if reasoning is not None and not isinstance(reasoning, str):
                _reject('invalid_reasoning_content')
            if message.get('tool_calls'):
                pending = validate_calls(message['tool_calls'], names)
                if pending & seen:
                    _reject('duplicate_tool_call_id')
                seen |= pending
            elif message.get('tool_calls') not in (None, []):
                _reject('invalid_tool_calls')
    if pending or messages[-1]['role'] not in ('user', 'tool'):
        _reject('incomplete_conversation')


@lru_cache(maxsize=1)
def _load_encoder():
    try:
        source = ENCODER_ASSET.read_bytes()
    except OSError:
        _reject('encoder_unavailable')
    if hashlib.sha256(source).hexdigest() != PUBLIC_REFERENCE_ENCODER_SHA256:
        _reject('encoder_hash_mismatch')
    module = ModuleType('pinned_deepseek_v4_encoder')
    exec(compile(source, str(ENCODER_ASSET), 'exec'), module.__dict__)
    return module


def measure_native_input(messages, *, tools, model, reasoning_effort='none'):
    if model != SUPPORTED_MODEL:
        _reject('unsupported_model')
    if reasoning_effort not in ('none', 'high', 'max'):
        _reject('unsupported_reasoning_effort')
    names = validate_tools(tools)
    validate_messages(messages, names)
    encoded_messages = copy.deepcopy(messages)
    if encoded_messages[0]['role'] != 'system':
        encoded_messages.insert(0, {'role': 'system', 'content': ''})
    encoded_messages[0]['tools'] = copy.deepcopy(tools)
    mode = 'chat' if reasoning_effort == 'none' else 'thinking'
    try:
        prompt = _load_encoder().encode_messages(
            encoded_messages, thinking_mode=mode, drop_thinking=False,
            reasoning_effort=None if reasoning_effort == 'none' else reasoning_effort)
        count = len(_load_tokenizer().encode(prompt, add_special_tokens=False).ids)
    except WriterTokenizerError:
        raise
    except Exception:
        _reject('tokenization_failed')
    raw = prompt.encode('utf-8')
    return {**_base_facts(), 'preflight_input_tokens': count, 'preflight_status': 'ok',
            'preflight_thinking_mode': mode, 'preflight_prompt_bytes': len(raw),
            'preflight_prompt_sha256': hashlib.sha256(raw).hexdigest(),
            'preflight_message_count': len(messages), 'preflight_tool_count': len(tools)}
