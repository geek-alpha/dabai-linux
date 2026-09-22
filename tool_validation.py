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

import json
import re
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


_STR_ARRAY_SEPS = re.compile(r"[\n,，;；]")


def _coerce_string_array(v, schema):
    """字符串 → 字符串数组（仅当 items.type == "string"）。返回 (是否成功, 值)。

    分隔符沿用各工具实现自己的约定（如 code_ops_impl.py:421 的 [\\n,，;；]）：校验层只做
    类型翻译，不发明新约定，也不去重/截断（那是实现的职责）。

    实测 4 次该错里 2 次是多关键词（28.jsonl 换行 6 个、9.jsonl 逗号 4 个），所以必须
    切分：整串包成单元素会让检索静默搜不到，比报错更糟。
    """
    if isinstance(v, list):
        return True, v
    if not isinstance(v, str):
        return False, v
    items = schema.get("items")
    if not isinstance(items, dict) or items.get("type") != "string":
        return False, v
    return True, [p for p in (x.strip() for x in _STR_ARRAY_SEPS.split(v)) if p]


def _deeper_error(err: str, path: str) -> bool:
    """错误是否指向子元素/子字段（path[0] / path.x），比「path: 类型不符」更具体。"""
    return err.startswith(path + "[") or err.startswith(path + ".")


def _validate_value(value, schema, path: str, errors: list):
    """按单层 schema 校验 value，错误写入 errors（带字段路径）。"""
    if schema is None or not isinstance(schema, dict):
        return
    stype = schema.get("type")
    if isinstance(stype, list):
        # 联合类型（JSON Schema 的 type 数组，如 search_batch.queries 的
        # ["string","array"]）：任一分支通过即可。子分支只做结构校验，enum/长度/边界留给
        # 下面的公共检查统一报——否则枚举越界会被误报成「类型不符」，把修正方向带偏。
        structural = {k: v for k, v in schema.items()
                      if k not in ("type", "enum", "minLength", "maxLength",
                                   "minimum", "maximum")}
        specific: list = []
        for t in stype:
            sub_errors: list = []
            _validate_value(value, {**structural, "type": t}, path, sub_errors)
            if not sub_errors:
                break
            if not specific:
                # 元素/字段级错误（path[0]、path.x）比一句「类型不符」更能指路，
                # 全分支失败时优先报它，否则模型不知道该改哪个元素。
                specific = [e for e in sub_errors if _deeper_error(e, path)]
        else:
            if specific:
                errors.extend(specific[:6])
            else:
                errors.append(f"{path}: 类型不符，期望 {' 或 '.join(str(t) for t in stype)}，"
                              f"实际是 {type(value).__name__}（值: {str(value)[:60]!r}）")
            return
    elif stype is not None:
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


def normalize_arguments(tool_spec: dict, arguments: dict) -> tuple:
    """把「参数被包进单键 arguments 信封」的调用还原成平铺参数。

    实测 data/longrun/traces：11/1027 次 ToolCallStart 的 arguments 是
    `{"arguments": {...}}`，其中 7 次内层是 **JSON 字符串**（双重编码）。信封形状下
    顶层只有 arguments 一个键 → 必填检查必然失败（F 类「必填参数缺失」），模型看到
    的报错是「缺少 command」，完全看不出真正原因是自己多包了一层。

    只在**证据充分**时才剥，避免误伤真带 arguments 参数的工具：
      ① 顶层键集合恰好是 {"arguments"}（多一个键都不算）；
      ② 内层是 dict，或是能 json.loads 成 dict 的字符串；
      ③ 内层键必须是该工具 schema 声明过的参数名（子集）——集合为空/超集都不剥；
      ④ 工具自己就声明了名为 arguments 的参数时不剥（这时单键形态是合法调用，
         剥了反而会把合法参数变成「缺必填」，是比原 bug 更差的回归）。

    Args:
        tool_spec: 工具定义（可为 None：拿不到 schema 时跳过第 ③ 条）。
        arguments: 模型提交的原始参数。

    Returns:
        (还原后的参数, 说明)：说明为 "arguments_envelope" 表示确实剥了一层，
        否则为 None（含未命中、以及按第 ③ 条判定不像信封而拒绝剥的情况）。
    """
    if not isinstance(arguments, dict) or list(arguments.keys()) != ["arguments"]:
        return arguments, None
    inner = arguments["arguments"]
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return arguments, None
    if not isinstance(inner, dict) or not inner:
        return arguments, None
    fn = (tool_spec or {}).get("function") or {}
    props = ((fn.get("parameters") or {}).get("properties") or {})
    if isinstance(props, dict) and props:
        if "arguments" in props:
            return arguments, None
        if not set(inner.keys()) <= set(props):
            return arguments, None
    return inner, "arguments_envelope"


def _required_hint(fn: dict, params: dict, errors: list) -> str:
    """必填缺失时补一句「本工具必填什么 + 最小调用示例」。

    为什么只给必填缺失补：F 类 4 次里 2 次是参数信封（normalize_arguments 已治），
    剩下 1 次是模型整组传错参数（code_read 收到的是 skill_name）——校验层治不了这种，
    能做的只有让报错本身携带「这个工具到底要什么」，而不是只丢一个参数名。
    """
    if not any(e.startswith("缺少必填参数: ") for e in errors):
        return ""
    props = params.get("properties") or {}
    required = [k for k in (params.get("required") or []) if isinstance(k, str)]
    if not required:
        return ""
    parts = []
    for key in required:
        sub = props.get(key) if isinstance(props.get(key), dict) else {}
        stype = sub.get("type") or "string"
        if isinstance(sub.get("enum"), list) and sub["enum"]:
            sample = sub["enum"][0]
        elif stype == "array":
            sample = []
        elif stype == "object":
            sample = {}
        elif stype == "integer":
            sample = 1
        elif stype == "number":
            sample = 1
        elif stype == "boolean":
            sample = True
        else:
            sample = "<%s>" % key
        parts.append("%s=%s" % (key, json.dumps(sample, ensure_ascii=False)))
    name = fn.get("name") or "工具名"
    return ("；本工具必填参数: %s；正确调用示例: %s(%s)"
            % ("、".join(required), name, ", ".join(parts)))


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
    # 信封还原必须在校验之前（且与 tools/replay_validation.py 共用同一函数）：
    # 历史 11/1027 次调用的参数被包了一层 arguments，顶层因此「缺必填参数」；
    # 两处各写一份剥法就会出现「重放说修好了、运行时仍报错」的假读数。
    arguments, _envelope = normalize_arguments(tool_spec, arguments)
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
            stype = sub.get("type") if isinstance(sub, dict) else None
            # 数组归一化必须在校验之前：否则 _validate_value 已记下类型错误，
            # 回填被 _errors_for_key 拦掉，包装结果传不到工具手里。
            if stype == "array":
                ok, converted = _coerce_string_array(value, sub)
                if ok and converted is not value:
                    cleaned[key] = value = converted
            _validate_value(value, sub, key, errors)
            # 标量类型转换结果回填（避免字符串数字传给期望 int 的工具）
            if stype in ("integer", "number", "boolean", "string") and not _errors_for_key(errors, key):
                ok, converted = _coerce_scalar(value, stype)
                if ok and converted != value:
                    cleaned[key] = converted

    if errors:
        return None, ("工具参数校验失败：" + "；".join(errors[:6])
                      + _required_hint(fn, params, errors))
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
