"""Pinned, offline token estimates for DeepSeek-V4-Pro writer requests.

These counts reproduce DeepSeek's published V4 message encoding against a
pinned official ``tokenizer.json``.  They are preflight/reference counts, not
provider billing usage.  Provider-returned usage remains authoritative.
"""

from functools import lru_cache
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence


SUPPORTED_MODEL = "deepseek-v4-pro"
TOKENIZER_SOURCE = "deepseek-ai/DeepSeek-V4-Pro"
TOKENIZER_REVISION = "b5968e9190ef611bbf34a7229255be88a0e937c1"
TOKENIZER_SHA256 = "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
PUBLIC_REFERENCE_ENCODER_SHA256 = (
    "bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0"
)
TOKEN_COUNT_KIND = "official_public_reference"
TOKENIZER_ASSET = (
    Path(__file__).with_name("tokenizer_assets")
    / "deepseek_v4_pro"
    / "tokenizer.json"
)

_BOS = "<｜begin▁of▁sentence｜>"
_USER = "<｜User｜>"
_ASSISTANT = "<｜Assistant｜>"
_THINKING_START = "<think>"
_THINKING_END = "</think>"
_SUPPORTED_ROLES = (("system", "user"), ("system", "user", "system"))
_THINKING_EFFORTS = frozenset(("low", "medium", "high", "max"))
_MAX_REASONING_PREFIX = (
    "Reasoning Effort: Absolute maximum with no shortcuts permitted.\n"
    "You MUST be very thorough in your thinking and comprehensively decompose "
    "the problem to resolve the root cause, rigorously stress-testing your logic "
    "against all potential paths, edge cases, and adversarial scenarios.\n"
    "Explicitly write out your entire deliberation process, documenting every "
    "intermediate step, considered alternative, and rejected hypothesis to ensure "
    "absolutely no assumption is left unchecked.\n\n"
)


class WriterTokenizerError(RuntimeError):
    """Fail-closed tokenizer error containing only safe, non-prompt facts."""

    def __init__(self, code: str, safe_facts: Mapping[str, Any] | None = None):
        self.code = code
        self.safe_facts = {
            **_base_facts(),
            "preflight_status": "error",
            "preflight_error_code": code,
            "preflight_input_tokens": None,
            **dict(safe_facts or {}),
        }
        super().__init__(f"writer tokenizer preflight failed: {code}")


def _base_facts() -> dict[str, Any]:
    return {
        "preflight_tokenizer": TOKENIZER_SOURCE,
        "preflight_tokenizer_revision": TOKENIZER_REVISION,
        "preflight_tokenizer_sha256": TOKENIZER_SHA256,
        "preflight_encoder_sha256": PUBLIC_REFERENCE_ENCODER_SHA256,
        "preflight_token_count_kind": TOKEN_COUNT_KIND,
    }


def _require_model(model: Any) -> None:
    if model != SUPPORTED_MODEL:
        raise WriterTokenizerError("unsupported_model")


@lru_cache(maxsize=1)
def _load_tokenizer():
    try:
        from tokenizers import Tokenizer
    except (ImportError, ModuleNotFoundError):
        raise WriterTokenizerError("dependency_unavailable") from None

    try:
        asset = TOKENIZER_ASSET.read_bytes()
    except OSError:
        raise WriterTokenizerError("asset_unavailable") from None
    if hashlib.sha256(asset).hexdigest() != TOKENIZER_SHA256:
        raise WriterTokenizerError("asset_hash_mismatch")
    try:
        return Tokenizer.from_buffer(asset)
    except Exception:
        raise WriterTokenizerError("tokenizer_load_failed") from None


def _content_measurements(messages: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    chars = 0
    encoded_bytes = 0
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise WriterTokenizerError("invalid_message")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise WriterTokenizerError("invalid_message")
        chars += len(content)
        encoded_bytes += len(content.encode("utf-8"))
    return chars, encoded_bytes


def measure_writer_request_size(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Measure raw message characters and bytes before tokenizer admission."""
    if not isinstance(messages, (list, tuple)):
        raise WriterTokenizerError("invalid_messages")
    request_chars, request_bytes = _content_measurements(messages)
    return {"request_chars": request_chars, "request_bytes": request_bytes}


def _render_public_reference(
    messages: Sequence[Mapping[str, str]], reasoning_effort: str
) -> tuple[str, str, str]:
    roles = tuple(message["role"] for message in messages)
    if roles not in _SUPPORTED_ROLES:
        raise WriterTokenizerError("unsupported_message_shape")
    if reasoning_effort == "none":
        thinking_mode = "chat"
    elif reasoning_effort in _THINKING_EFFORTS:
        thinking_mode = "thinking"
    else:
        raise WriterTokenizerError("unsupported_reasoning_effort")

    prompt = _BOS
    if reasoning_effort == "max":
        prompt += _MAX_REASONING_PREFIX
    prompt += messages[0]["content"]
    prompt += _USER + messages[1]["content"]
    if roles == ("system", "user"):
        prompt += _ASSISTANT
        prompt += _THINKING_END if thinking_mode == "chat" else _THINKING_START
    else:
        # This unusual trailing-system repair shape is the current physical
        # retry payload.  DeepSeek's published encoder appends its content
        # literally and does not add an assistant transition afterward.
        prompt += messages[2]["content"]
    return prompt, thinking_mode, ",".join(roles)


def _token_count(text: str) -> int:
    try:
        return len(_load_tokenizer().encode(text, add_special_tokens=False).ids)
    except WriterTokenizerError:
        raise
    except Exception:
        raise WriterTokenizerError("tokenization_failed") from None


def measure_writer_input(
    messages: Sequence[Mapping[str, Any]], *, model: str, reasoning_effort: str
) -> dict[str, Any]:
    """Measure one final physical writer request without changing ``messages``.

    The supported inputs are the current ``system,user`` request and its
    ``system,user,system`` JSON-repair retry.  Anything else fails before
    transport so an unknown token count cannot be recorded as zero.
    """

    _require_model(model)
    size = measure_writer_request_size(messages)
    prompt, thinking_mode, role_shape = _render_public_reference(
        messages, reasoning_effort
    )
    prompt_bytes = prompt.encode("utf-8")
    count = _token_count(prompt)
    return {
        **_base_facts(),
        **size,
        "preflight_input_tokens": count,
        "preflight_status": "ok",
        "preflight_thinking_mode": thinking_mode,
        "preflight_message_roles": role_shape,
        "preflight_prompt_chars": len(prompt),
        "preflight_prompt_bytes": len(prompt_bytes),
        "preflight_prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
    }


def count_writer_text(text: str, *, model: str) -> int:
    """Estimate output text tokens with the same pinned tokenizer asset."""

    _require_model(model)
    if not isinstance(text, str):
        raise WriterTokenizerError("invalid_text")
    return _token_count(text)
