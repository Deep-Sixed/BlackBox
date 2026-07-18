"""Minimal YAML parser for claim frontmatter blocks (stdlib-only)."""

from __future__ import annotations

import re
from typing import Any


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in ("null", "~", ""):
        return None
    if value in ("true", "false"):
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",") if part.strip()]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return _strip_quotes(value)
    return value


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse flat and lightly nested YAML used by claim blocks."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]

    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        container = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(container, list):
                raise ValueError(f"Unexpected list item at indent {indent}")
            item_text = stripped[2:].strip()
            if ":" in item_text:
                key, _, rest = item_text.partition(":")
                list_item = {key.strip(): _parse_scalar(rest)}
                container.append(list_item)
                stack.append((indent, list_item))
            else:
                container.append(_parse_scalar(item_text))
            continue

        if not isinstance(container, dict):
            raise ValueError(f"Expected mapping at indent {indent}, got list")

        if ":" not in stripped:
            continue

        key, _, rest = stripped.partition(":")
        key = key.strip()
        value_part = rest.strip()

        if not value_part:
            if key == "sources":
                new_list: list[Any] = []
                container[key] = new_list
                stack.append((indent, new_list))
            else:
                nested: dict[str, Any] = {}
                container[key] = nested
                stack.append((indent, nested))
            continue

        container[key] = _parse_scalar(value_part)

    return root


def extract_yaml_blocks(markdown: str) -> list[str]:
    pattern = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    return [m.group(1).strip() for m in pattern.finditer(markdown)]
