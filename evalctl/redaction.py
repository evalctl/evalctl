from __future__ import annotations

from typing import Any

from .artifacts import apply_redaction


# These are the verdict-wide limits. Keep them here because every command-scorer
# writer and reader must use the same contract values.
VERDICT_MAX_DEPTH = 32
VERDICT_MAX_NODES = 10_000
VERDICT_MAX_SERIALIZED_BYTES = 1 * 1024 * 1024


def cap_text(value: str, max_bytes: int) -> str:
    """Return a UTF-8-safe prefix with at most max_bytes encoded bytes."""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "replace")


def redact_json(value: Any, patterns: list[str], env_values: list[str], max_bytes: int) -> tuple[Any, bool]:
    """Cap and redact every string value, while preserving JSON keys and shape."""
    if isinstance(value, str):
        capped = cap_text(value, max_bytes)
        redacted, changed = apply_redaction(capped, patterns, env_values)
        return cap_text(redacted, max_bytes), changed
    if isinstance(value, list):
        changed = False
        result: list[Any] = []
        for item in value:
            item_result, item_changed = redact_json(item, patterns, env_values, max_bytes)
            result.append(item_result)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, dict):
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            item_result, item_changed = redact_json(item, patterns, env_values, max_bytes)
            result[key] = item_result
            changed = changed or item_changed
        return result, changed
    return value, False
