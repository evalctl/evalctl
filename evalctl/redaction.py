from __future__ import annotations

import json
import re
from json.decoder import scanstring
from typing import Any

from .artifacts import apply_redaction


# These are the verdict-wide limits. Keep them here because every command-scorer
# writer and reader must use the same contract values.
VERDICT_MAX_DEPTH = 32
VERDICT_MAX_NODES = 10_000
VERDICT_MAX_SERIALIZED_BYTES = 1 * 1024 * 1024

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class VerdictLimitError(ValueError):
    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


class VerdictParseError(ValueError):
    pass


def parse_bounded_json(text: str) -> Any:
    """Validate JSON tokens before the standard decoder constructs a graph."""
    position = 0
    nodes = 0
    length = len(text)

    def skip_space() -> None:
        nonlocal position
        while position < length and text[position] in " \t\r\n":
            position += 1

    def parse_string() -> None:
        nonlocal position
        if position >= length or text[position] != '"':
            raise VerdictParseError("expected JSON string")
        try:
            value, position_after = scanstring(text, position + 1, True)
        except (ValueError, json.JSONDecodeError) as exc:
            raise VerdictParseError(str(exc)) from exc
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise VerdictParseError("JSON string contains a lone surrogate")
        position = position_after

    def parse_value(depth: int) -> None:
        nonlocal nodes, position
        if depth > VERDICT_MAX_DEPTH:
            raise VerdictLimitError("depth")
        nodes += 1
        if nodes > VERDICT_MAX_NODES:
            raise VerdictLimitError("node-count")
        skip_space()
        if position >= length:
            raise VerdictParseError("expected JSON value")
        token = text[position]
        if token == '"':
            parse_string()
            return
        if token == "{":
            position += 1
            skip_space()
            if position < length and text[position] == "}":
                position += 1
                return
            while True:
                parse_string()
                skip_space()
                if position >= length or text[position] != ":":
                    raise VerdictParseError("expected ':' after JSON object key")
                position += 1
                parse_value(depth + 1)
                skip_space()
                if position < length and text[position] == "}":
                    position += 1
                    return
                if position >= length or text[position] != ",":
                    raise VerdictParseError("expected ',' or '}' in JSON object")
                position += 1
                skip_space()
        if token == "[":
            position += 1
            skip_space()
            if position < length and text[position] == "]":
                position += 1
                return
            while True:
                parse_value(depth + 1)
                skip_space()
                if position < length and text[position] == "]":
                    position += 1
                    return
                if position >= length or text[position] != ",":
                    raise VerdictParseError("expected ',' or ']' in JSON array")
                position += 1
                skip_space()
        for literal in ("true", "false", "null"):
            if text.startswith(literal, position):
                position += len(literal)
                return
        match = _NUMBER.match(text, position)
        if match is not None:
            position = match.end()
            return
        raise VerdictParseError("invalid JSON value")

    skip_space()
    parse_value(1)
    skip_space()
    if position != length:
        raise VerdictParseError("extra data after JSON value")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise VerdictParseError(str(exc)) from exc


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
