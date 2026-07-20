"""Minimal JSON Schema validation for tool call arguments.

Python port of packages/ai/src/utils/validation.ts (MVP subset).
Full TypeBox/AJV coercion is deferred; this covers required fields and
basic type checks used by the agent loop.
"""

from __future__ import annotations

import copy
from typing import Any

from pi_ai.types import Tool, ToolCall


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> dict[str, Any]:
    """Validate tool call arguments against the tool JSON Schema.

    Returns validated arguments (a deep copy). Raises ValueError on failure.
    """
    args = copy.deepcopy(tool_call.arguments)
    schema = tool.parameters
    if not isinstance(args, dict):
        raise ValueError(
            f'Validation failed for tool "{tool_call.name}": arguments must be an object'
        )
    if schema.get("type") not in (None, "object"):
        return args
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return args
    required = schema.get("required", [])
    if isinstance(required, list):
        for key in required:
            if key not in args:
                raise ValueError(
                    f'Validation failed for tool "{tool_call.name}": missing required property "{key}"'
                )
    for key, value in args.items():
        if key in properties:
            _validate_value(value, properties[key], f"{tool_call.name}.{key}")
    return args


def _validate_value(value: Any, schema: Any, path: str) -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is None:
        return
    if isinstance(expected, list):
        if not any(_matches_type(value, t) for t in expected):
            raise ValueError(f"Validation failed at {path}: expected {expected}")
        return
    if not _matches_type(value, expected):
        raise ValueError(f"Validation failed at {path}: expected {expected}")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True
