"""Graders: exact / contains / json_schema (bộ kiểm schema tối giản, không phụ thuộc thư viện ngoài)."""

from __future__ import annotations

import json
from typing import Any

from zeus.brain.text import strip_accents


def _norm(s: str) -> str:
    return " ".join(strip_accents(s).split())


def grade_exact(output: str, spec: dict[str, Any]) -> tuple[bool, str]:
    ok = output.strip() == str(spec["value"]).strip()
    return ok, "" if ok else "khác giá trị mong đợi"


def grade_contains(output: str, spec: dict[str, Any]) -> tuple[bool, str]:
    out = _norm(output)
    missing = [v for v in spec["values"] if _norm(str(v)) not in out]
    return (not missing), ("" if not missing else f"thiếu: {missing}")


_TYPES: dict[str, Any] = {
    "string": str, "boolean": bool, "array": list, "object": dict, "null": type(None),
}


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t:
        if t == "integer":
            okt = isinstance(value, int) and not isinstance(value, bool)
        elif t == "number":
            okt = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            okt = isinstance(value, _TYPES[t]) and not (t != "boolean" and isinstance(value, bool))
        if not okt:
            return [f"{path}: cần {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: ngoài enum {schema['enum']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: thiếu")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in value:
                errs += validate_schema(value[k], sub, f"{path}.{k}")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k}: không được phép" for k in value if k not in props]
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate_schema(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: < minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: > maximum")
    return errs


def grade_json_schema(output: str, spec: dict[str, Any]) -> tuple[bool, str]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        return False, f"không phải JSON: {exc.msg}"
    errs = validate_schema(data, spec["schema"])
    return (not errs), "; ".join(errs)


GRADERS = {"exact": grade_exact, "contains": grade_contains, "json_schema": grade_json_schema}


def grade(output: str, spec: dict[str, Any]) -> tuple[bool, str]:
    fn = GRADERS.get(spec.get("type", ""))
    if fn is None:
        return False, f"grader lạ: {spec.get('type')}"
    return fn(output, spec)
