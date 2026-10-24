"""wengao_ppt.contracts_rs —— rs-contracts-v1 校验器（Step 0.5）。

不引入新依赖（jsonschema 未在批准清单内），实现本套 schema 实际用到的
JSON Schema draft-07 子集：
  type / const / enum / required / properties / additionalProperties(false) /
  items / minItems / minimum / maximum / minLength / maxLength / pattern /
  if-then / allOf

用法：
  from wengao_ppt.contracts_rs import validate, ContractError
  validate({"schema": "rs.event.v1", ...})          # 按 schema 字段自动选文件
  validate(obj, "rs.scene_resolution.v1")           # 或显式指定

错误以 `ContractError` 抛出，message 形如 `$.facts[0].value: 期望 number`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "rs"

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class ContractError(ValueError):
    """契约校验失败。错误码 PIPE.CONTRACT_INVALID。"""

    code = "PIPE.CONTRACT_INVALID"


def _load_schema(schema_id: str) -> dict:
    """按契约 id 找 schema 文件。接受 'rs.xxx.v1' 与 'xxx.v1' 两种写法。"""
    for cand in (schema_id, schema_id.removeprefix("rs.")):
        p = SCHEMA_DIR / f"{cand}.schema.json"
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    raise ContractError(f"未知契约 {schema_id}（{SCHEMA_DIR / (schema_id + '.schema.json')} 不存在）")


def _check_type(value, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(expected)
    if py is None:
        return True  # 未实现的类型关键字不阻断
    return isinstance(value, py)


def _validate(value, schema: dict, path: str, errors: list) -> None:
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在允许集合内")
        return
    types = schema.get("type")
    if types is not None:
        wanted = types if isinstance(types, list) else [types]
        if not any(_check_type(value, t) for t in wanted):
            errors.append(f"{path}: 期望类型 {'/'.join(wanted)}，实际 {type(value).__name__}")
            return
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度 {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度 {len(value)} > maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不匹配 pattern {schema['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: 缺少必填字段 {req!r}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 未允许的字段 {k!r}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(value)} < minItems {schema['minItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                _validate(v, items, f"{path}[{i}]", errors)
    for sub in schema.get("allOf", []):
        _validate(value, sub, path, errors)
    if "if" in schema and "then" in schema:
        cond = schema["if"]
        # 仅支持 properties+const 条件
        ok = True
        for k, sub in (cond.get("properties") or {}).items():
            if "const" in sub and value.get(k) != sub["const"]:
                ok = False
                break
        if ok:
            _validate(value, schema["then"], path, errors)


def validate(obj, schema_id: Optional[str] = None) -> dict:
    """校验并原样返回 obj；失败抛 ContractError（含全部错误行）。"""
    if schema_id is None:
        schema_id = (obj or {}).get("schema")
    if not schema_id:
        raise ContractError("对象缺少 schema 字段，无法选择契约")
    schema = _load_schema(schema_id)
    errors: list = []
    _validate(obj, schema, "$", errors)
    if errors:
        raise ContractError("；".join(errors[:12]))
    return obj


def schema_ids() -> list:
    return sorted(p.stem.replace(".schema", "") for p in SCHEMA_DIR.glob("*.schema.json"))
