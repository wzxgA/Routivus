"""Sanitize MCP input schemas for OpenAI-compatible tool definitions."""

from __future__ import annotations

import copy
import re
from typing import Any


_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ALLOWED = {
    "type", "properties", "required", "items", "description", "enum", "default",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "additionalProperties", "format",
}


def exposed_tool_name(server: str, remote_name: str, max_length: int = 128) -> str:
    clean = _NAME_RE.sub("_", remote_name).strip("_") or "tool"
    return f"mcp__{server}__{clean}"[:max_length]


def sanitize_description(value: Any, max_chars: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 8)] + "…[截断]"


def sanitize_schema(schema: Any, *, max_depth: int = 8, max_properties: int = 128) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    root = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    definitions = root.get("$defs") or root.get("definitions") or {}
    resolving: set[str] = set()

    def resolve_ref(ref: str) -> dict | None:
        if not ref.startswith("#/") or ref in resolving:
            return None
        node: Any = root
        try:
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                node = node[part]
        except (KeyError, TypeError):
            return None
        if not isinstance(node, dict):
            return None
        resolving.add(ref)
        try:
            return clean(node, 1)
        finally:
            resolving.discard(ref)

    def merge_all_of(items: list, depth: int) -> dict:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for item in items:
            part = clean(item, depth + 1)
            if part.get("type") != "object":
                warnings.append("allOf 含非 object，已降级")
                continue
            merged["properties"].update(part.get("properties", {}))
            merged["required"].extend(part.get("required", []))
        merged["required"] = list(dict.fromkeys(merged["required"]))
        return merged

    def clean(node: Any, depth: int) -> dict:
        if depth > max_depth or not isinstance(node, dict):
            warnings.append("schema 深度超限，已降级为 string")
            return {"type": "string"}
        if isinstance(node.get("$ref"), str):
            resolved = resolve_ref(node["$ref"])
            if resolved is None:
                warnings.append(f"无法解析 $ref: {node['$ref']}")
                return {"type": "string", "description": "原 schema reference 无法展开"}
            return resolved
        if isinstance(node.get("allOf"), list):
            merged = merge_all_of(node["allOf"], depth)
            if node.get("description"):
                merged["description"] = sanitize_description(node["description"])
            return merged
        union = node.get("anyOf") or node.get("oneOf")
        if isinstance(union, list) and union:
            options = [clean(item, depth + 1) for item in union if isinstance(item, dict)]
            non_null = [item for item in options if item.get("type") != "null"]
            if len(non_null) == 1:
                chosen = non_null[0]
                if node.get("description"):
                    chosen["description"] = sanitize_description(node["description"])
                return chosen
            types = {str(item.get("type")) for item in non_null if item.get("type")}
            if len(types) == 1 and non_null:
                return non_null[0]
            warnings.append("anyOf/oneOf 无法无损表达，已降级为 string")
            return {"type": "string", "description": sanitize_description(node.get("description") or "联合类型参数")}

        inferred = node.get("type")
        if isinstance(inferred, list):
            values = [value for value in inferred if value != "null"]
            inferred = values[0] if len(values) == 1 else "string"
        if not isinstance(inferred, str):
            inferred = "object" if isinstance(node.get("properties"), dict) else "string"
        if inferred not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
            inferred = "string"
        out: dict[str, Any] = {"type": inferred}
        for key in _ALLOWED - {"type", "properties", "required", "items", "description", "enum"}:
            if key in node and isinstance(node[key], (str, int, float, bool)):
                out[key] = node[key]
        if node.get("description"):
            out["description"] = sanitize_description(node["description"])
        if isinstance(node.get("enum"), list):
            out["enum"] = node["enum"][:100]
            if len(node["enum"]) > 100:
                warnings.append("enum 超过 100 项，已截断")
        if inferred == "object":
            props = node.get("properties", {})
            if not isinstance(props, dict):
                props = {}
            selected = list(props.items())[:max_properties]
            if len(props) > max_properties:
                warnings.append("properties 超过上限，已截断")
            out["properties"] = {str(name): clean(value, depth + 1) for name, value in selected}
            required = node.get("required", [])
            if isinstance(required, list):
                out["required"] = [str(name) for name in required if str(name) in out["properties"]]
            if "additionalProperties" in node and isinstance(node["additionalProperties"], bool):
                out["additionalProperties"] = node["additionalProperties"]
        elif inferred == "array":
            out["items"] = clean(node.get("items", {}), depth + 1)
        return out

    cleaned = clean(root, 0)
    if cleaned.get("type") != "object":
        warnings.append("工具 inputSchema 顶层不是 object，已包装为空 object")
        cleaned = {"type": "object", "properties": {}}
    cleaned.setdefault("properties", {})
    return cleaned, warnings

