# -*- coding: utf-8 -*-
"""工具调用参数严格校验 —— JSON Schema 精简实现。

用途：模型发起工具调用后、真正执行前，按工具定义（OpenAI function schema：
`function.parameters`，与技能工具 inputSchema 同构）校验参数，避免类型错误 / 缺参
导致工具执行失败或被静默吞错。

规则（严格但务实）：
- required 必填缺失 → 报错，返回给模型自行修正；
- 类型不匹配 → 先尝试宽松转换（数字字符串→数字、布尔字符串→布尔、整型浮点→整数），
  无法转换时报错；
- enum 枚举越界 / 长度、数值边界越界 → 报错；
- 嵌套 object / array items 递归校验；
- 未知字段默认放行（不少技能 schema 未声明 additionalProperties），避免误伤。

本模块不依赖任何第三方库，错误信息为中文，可直接作为 tool 结果回填给模型，
让模型在下一轮修正参数后重试。
"""
from __future__ import annotations

from typing import Optional


def _coerce_scalar(v, t: str):
    """尝试把值转换为目标标量类型。返回 (是否成功, 转换后的值)。"""
    if t == "string":
        if isinstance(v, str):
            return True, v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True, str(v)
        return False, v
    if t == "integer":
        if isinstance(v, bool):
            return False, v
        if isinstance(v, int):
            return True, v
        if isinstance(v, float) and v.is_integer():
            return True, int(v)
        if isinstance(v, str):
            s = v.strip()
            if s.lstrip("+-").isdigit():
                return True, int(s)
        return False, v
    if t == "number":
        if isinstance(v, bool):
            return False, v
        if isinstance(v, (int, float)):
            return True, v
        if isinstance(v, str):
            s = v.strip()
            try:
                return True, float(s)
            except ValueError:
                pass
        return False, v
    if t == "boolean":
        if isinstance(v, bool):
            return True, v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes", "是", "对"):
                return True, True
            if s in ("false", "0", "no", "否", "错"):
                return True, False
        return False, v
    return True, v  # 未知类型放行


def _validate_value(value, schema, path: str, errors: list):
    """按单层 schema 校验 value，错误写入 errors（带字段路径）。"""
    if schema is None or not isinstance(schema, dict):
        return
    stype = schema.get("type")
    if stype is not None:
        if stype == "array":
            if not isinstance(value, list):
                errors.append(f"{path}: 期望数组(array)，实际是 {type(value).__name__}")
                return
            items = schema.get("items")
            if isinstance(items, dict):
                for i, item in enumerate(value):
                    _validate_value(item, items, f"{path}[{i}]", errors)
            return
        if stype == "object":
            if not isinstance(value, dict):
                errors.append(f"{path}: 期望对象(object)，实际是 {type(value).__name__}")
                return
            props = schema.get("properties") or {}
            for key, sub in props.items():
                if key in value:
                    _validate_value(value[key], sub, f"{path}.{key}", errors)
            return
        # 标量类型
        ok, converted = _coerce_scalar(value, stype)
        if not ok:
            errors.append(
                f"{path}: 类型不符，期望 {stype}，实际是 {type(value).__name__}"
                f"（值: {str(value)[:60]!r}）")

    enum = schema.get("enum")
    if enum is not None and isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: 取值不在允许范围内，可选值: {enum}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度不足，至少 {schema['minLength']} 个字符")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度超限，最多 {schema['maxLength']} 个字符")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path}: 数值过小，最小为 {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path}: 数值过大，最大为 {schema['maximum']}")


def _errors_for_key(errors: list, key: str) -> list:
    return [e for e in errors
            if e.startswith(key + ":") or e.startswith(key + ".") or e.startswith(key + "[")]


def validate_arguments(tool_spec: dict, arguments: dict):
    """校验模型提交的工具参数。

    Args:
        tool_spec: 工具定义（含 function.parameters JSON Schema）。
        arguments: 模型提交的参数（dict）。

    Returns:
        (cleaned_args, error)：校验通过时返回 (清洗/转换后的参数, None)；
        失败时返回 (None, 中文错误描述)。
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None, f"参数必须是 JSON 对象，实际是 {type(arguments).__name__}"
    fn = (tool_spec or {}).get("function") or {}
    params = fn.get("parameters") or {}
    if not isinstance(params, dict) or not params:
        # 无 schema：放行（保持向后兼容）
        return dict(arguments), None

    cleaned = dict(arguments)
    errors: list = []

    # 必填检查
    required = params.get("required") or []
    if isinstance(required, list):
        for key in required:
            if key not in cleaned or cleaned[key] is None:
                errors.append(f"缺少必填参数: {key}")

    props = params.get("properties") or {}
    if isinstance(props, dict):
        for key, value in list(cleaned.items()):
            sub = props.get(key)
            if sub is None:
                continue  # 未知字段放行
            _validate_value(value, sub, key, errors)
            # 标量类型转换结果回填（避免字符串数字传给期望 int 的工具）
            stype = sub.get("type") if isinstance(sub, dict) else None
            if stype in ("integer", "number", "boolean", "string") and not _errors_for_key(errors, key):
                ok, converted = _coerce_scalar(value, stype)
                if ok and converted != value:
                    cleaned[key] = converted

    if errors:
        return None, "工具参数校验失败：" + "；".join(errors[:6])
    return cleaned, None


def find_tool_spec(tools: list, tool_name: str) -> Optional[dict]:
    """按名称在工具列表中查找定义（OpenAI function spec）。"""
    if not tools:
        return None
    name = str(tool_name or "")
    for t in tools:
        fn = (t or {}).get("function") or {}
        if fn.get("name") == name:
            return t
    return None
