"""技能工坊（skill_dev）—— 让大白自己给自己创建、修改、校验、启停技能。

本技能是「自举」能力：用它创建的新技能同样遵循 skills/<名>/ 三件套规范
（skill.json 清单 + skill.py 实现 + SKILL.md 说明书），与手写技能完全等价。

工具（全部返回可读文本）：
- skill_dev_list        列出全部技能及状态
- skill_dev_read        读取某个技能的源文件
- skill_dev_create      脚手架新技能（三件套 + 工具桩）
- skill_dev_edit        修改 skill.json 清单字段（改名自动重命名目录）
- skill_dev_write_file  整体写入 skill.py / SKILL.md
- skill_dev_validate    静态结构校验（JSON/命名/工具/HANDLERS 一致性/编译）
- skill_dev_reload      触发热重载并汇报运行状态
- skill_dev_remove      删除技能（必须 confirm=true）

安全边界：
- 只操作 skills/ 目录内文件；绝不触碰 harness/ 与根目录核心代码（会整进程重启）
- 校验为纯静态：py_compile + AST 分析，绝不 import/执行被校验技能的代码
- 删除必须显式 confirm；写文件有大小上限；文件名白名单
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # 大白根目录
SKILLS_DIR = BASE_DIR / "skills"                        # 技能注册目录
HARNESS_STATE = BASE_DIR / "harness_state.json"         # 启停状态（name -> bool）
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")        # 技能名白名单
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")        # 工具名白名单
FILE_WHITELIST = {"skill.json", "skill.py", "SKILL.md"} # 可读取的文件
WRITEABLE_FILES = {"skill.py", "SKILL.md"}              # 可写入的文件
MANIFEST_FIELDS = {"name", "title", "description", "author",
                   "version", "enabled", "disclosure", "prompt", "tools"}
MAX_WRITE_BYTES = 512 * 1024                            # 单文件写入上限 512KB
RELOAD_PORTS = (8000, 8900, 7860)                       # 本地服务候选端口


def _err(msg: str) -> str:
    return "✘ " + msg


def _valid_name(name) -> str | None:
    """校验技能名；合法返回 None，否则返回错误描述。"""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "技能名不合法：%r（仅允许字母/数字/下划线/连字符，1-64 位）" % (name,)
    return None


def _load_manifest(skill_dir: Path):
    """读取 skill.json。返回 (manifest, error)。"""
    p = skill_dir / "skill.json"
    if not p.exists():
        return None, "缺少 skill.json：%s" % p
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, "skill.json 解析失败：%s" % e


# ---------------- 热重载 ----------------


def _post_json(url: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url, data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _trigger_reload() -> str:
    """尝试通过本地 REST 触发热重载；失败则依赖 hot_reload 守护。"""
    for port in RELOAD_PORTS:
        try:
            _post_json("http://127.0.0.1:%d/api/harness/reload" % port)
            return "✔ 已触发本地服务（127.0.0.1:%d）热重载，改动即时应答。" % port
        except Exception:
            continue
    return ("⚠ 未能直连本地服务触发重载（服务可能未在运行）；"
            "hot_reload 守护会在 1 秒内自动加载改动，若仍未生效可在 /harness 管理页手动「重载」。")


def _status_text() -> str:
    """查询本地服务运行状态（技能/插件/损坏清单）。失败返回空串。"""
    for port in RELOAD_PORTS:
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/harness/status" % port)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and "skills" in data:
                skills = data.get("skills") or []
                plugins = data.get("plugins") or []
                broken = data.get("broken") or []
                tc = data.get("tool_count") or {}
                parts = ["运行状态：技能 %d 个、插件 %d 个" % (len(skills), len(plugins))]
                if broken:
                    parts.append("损坏：%s" % "、".join(broken))
                else:
                    parts.append("无损坏")
                if tc:
                    parts.append("工具有效数：技能 %d、插件 %d" % (tc.get("skill", 0), tc.get("plugin", 0)))
                return "；".join(parts)
        except Exception:
            continue
    return ""


# ---------------- skill_dev_list ----------------


def skill_dev_list(args) -> str:
    state: dict = {}
    try:
        state = (json.loads(HARNESS_STATE.read_text(encoding="utf-8")) or {}).get("skills") or {}
    except Exception:
        pass
    if not SKILLS_DIR.is_dir():
        return _err("技能目录不存在：%s" % SKILLS_DIR)
    rows = []
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        mp = d / "skill.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            m = {}
        name = str(m.get("name") or d.name)
        enabled = state.get(name, bool(m.get("enabled", True)))
        tools = m.get("tools") or []
        rows.append("- %s｜%s｜启用=%s｜披露=%s｜工具=%d｜v%s"
                    % (name, m.get("title") or "（无标题）",
                       "是" if enabled else "否", m.get("disclosure", "full"),
                       len(tools), m.get("version", "?")))
    total = len(rows)
    lines = ["技能体系共 %d 个技能（目录：%s）" % (total, SKILLS_DIR)]
    lines += rows if rows else ["（暂无技能）"]
    st = _status_text()
    if st:
        lines.append(st)
    return "\n".join(lines)


# ---------------- skill_dev_read ----------------


def skill_dev_read(args) -> str:
    name = str(args.get("skill_name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    d = SKILLS_DIR / name
    if not d.is_dir():
        return _err("技能不存在：skills/%s（可用 skill_dev_list 查看现有技能）" % name)
    file = str(args.get("file") or "skill.json").strip()
    if file not in FILE_WHITELIST:
        return _err("file 只能是 %s 之一，收到 %r" % (sorted(FILE_WHITELIST), file))
    p = d / file
    if not p.exists():
        return "（技能 %s 没有 %s 文件——该技能可能只有部分文件）" % (name, file)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return _err("读取 %s 失败：%s" % (p, e))
    lines = text.splitlines()
    total = len(lines)
    try:
        start = max(1, int(args.get("start") or 1))
        max_lines = max(10, min(int(args.get("max_lines") or 500), 5000))
    except ValueError:
        return _err("start/max_lines 需为数字")
    start = min(start, total + 1)
    end = min(total, start + max_lines - 1)
    body = "\n".join(lines[start - 1:end])
    head = "skills/%s/%s（共 %d 行，显示 %d-%d）：\n```text\n" % (name, file, total, start, end)
    tail = "\n```"
    if total > end:
        tail += ("\n（还有 %d 行未显示：继续读传 start=%d）" % (total - end, end + 1))
    return head + body + tail


# ---------------- skill_dev_create ----------------


def _parse_tools_spec(value) -> tuple:
    """解析 tools_spec（JSON 数组字符串或已是列表）。返回 (tools, error)。"""
    if value is None:
        return [], None
    if isinstance(value, list):
        tools = value
    else:
        s = str(value).strip()
        if not s:
            return [], None
        try:
            tools = json.loads(s)
        except Exception as e:
            return [], "tools_spec 不是合法 JSON：%s" % e
    if not isinstance(tools, list):
        return [], "tools_spec 必须是 JSON 数组（每个元素是一个工具定义）"
    names = []
    for i, t in enumerate(tools):
        if not isinstance(t, dict) or t.get("type") != "function":
            return [], '第 %d 个工具定义必须形如 {"type":"function","function":{...}}' % (i + 1)
        fn = t.get("function") or {}
        tname = str(fn.get("name") or "")
        if not TOOL_NAME_RE.match(tname):
            return [], "第 %d 个工具名不合法：%r（仅字母/数字/下划线/连字符）" % (i + 1, tname)
        if tname in names:
            return [], "工具名重复：%s" % tname
        names.append(tname)
        if not isinstance(fn.get("parameters"), dict):
            return [], '工具 %s 缺少 parameters（须为 {"type":"object","properties":{...}}）' % tname
    return tools, None


def _scaffold_md(name: str, title: str, description: str, tools: list) -> str:
    if tools:
        rows = "\n".join("| `%s` | （待补全） |" % ((t.get("function") or {}).get("name")) for t in tools)
    else:
        rows = "| （暂无工具——纯提示词技能） | |"
    return (
        "# " + title + "（" + name + "）\n"
        "\n"
        "（本说明由 skill_dev 脚手架生成，待补全：本技能做什么、什么时候用、怎么用。）\n"
        "\n"
        "## 用途\n" + description + "\n"
        "\n"
        "## 适用场景\n"
        "- （待补全：触发本技能的典型用户说法）\n"
        "\n"
        "## 工具\n"
        "| 工具 | 作用 |\n"
        "| --- | --- |\n" + rows + "\n"
        "\n"
        "## 用法示例\n"
        "（待补全）\n"
        "\n"
        "## 边界与注意\n"
        "- （待补全：安全边界、不可做什么）\n"
        "\n"
        "---\n"
        "本说明通过 skill_help(\"" + name + "\") 随时查看。\n"
    )


def _scaffold_files(name: str, title: str, description: str, prompt: str,
                    disclosure: str, tools: list) -> list:
    d = SKILLS_DIR / name
    d.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": name,
        "title": title,
        "version": "1.0.0",
        "description": description,
        "author": "dabai",
        "enabled": True,
        "disclosure": disclosure,
        "prompt": prompt or "",
        "tools": tools,
    }
    (d / "skill.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")

    py = []
    py.append('"""' + title + '（' + name + '）—— 使用说明见 SKILL.md。')
    py.append("本文件由 skill_dev 脚手架生成，工具桩尚未实现：")
    py.append('用 skill_dev_write_file("' + name + '", "skill.py", <新内容>) 整体替换本文件，')
    py.append('补全各工具实现后用 skill_dev_validate("' + name + '") 校验。')
    py.append('"""')
    py.append("from __future__ import annotations")
    py.append("")
    py.append("TOOLS = " + json.dumps(tools, ensure_ascii=False, indent=2))
    py.append("")
    py.append("PROMPT = " + json.dumps(prompt or "", ensure_ascii=False))
    py.append("")
    py.append("")
    py.append("def _stub(tool_name: str):")
    py.append('    return ("⚠ 工具 %s 尚未实现：请用 skill_dev_write_file 补全 skills/'
             + name + '/skill.py 中 HANDLERS 的实现，再用 skill_dev_validate 校验。" % tool_name)')
    py.append("")
    py.append("")
    if tools:
        py.append("HANDLERS = {")
        for t in tools:
            tname = (t.get("function") or {}).get("name")
            py.append("    " + repr(tname) + ": lambda args: _stub(" + repr(tname) + "),")
        py.append("}")
    else:
        py.append("HANDLERS = {}")
        py.append("")
        py.append("")
        py.append("def execute(name, arguments):")
        py.append("    return _stub(name)")
    (d / "skill.py").write_text("\n".join(py) + "\n", encoding="utf-8")

    (d / "SKILL.md").write_text(_scaffold_md(name, title, description, tools), encoding="utf-8")
    return [str(d / "skill.json"), str(d / "skill.py"), str(d / "SKILL.md")]


def skill_dev_create(args) -> str:
    name = str(args.get("name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    title = str(args.get("title") or "").strip()
    if not title:
        return _err("请提供 title（技能标题，如「天气查询」）")
    description = str(args.get("description") or "").strip()
    if not description:
        return _err("请提供 description（一句话说明，建议 ≤88 字，会显示在技能摘要里）")
    disclosure = str(args.get("disclosure") or "on_demand").strip()
    if disclosure not in ("full", "on_demand"):
        return _err("disclosure 只能是 full 或 on_demand")
    prompt = str(args.get("prompt") or "").strip()
    tools, terr = _parse_tools_spec(args.get("tools_spec"))
    if terr:
        return _err(terr)

    d = SKILLS_DIR / name
    if d.exists():
        if not args.get("overwrite"):
            return _err("技能 %s 已存在（%s）。如确认要覆盖请传 overwrite=true；覆盖会丢掉现有文件。"
                        % (name, d))
        shutil.rmtree(d, ignore_errors=True)

    files = _scaffold_files(name, title, description, prompt, disclosure, tools)
    lines = ["✔ 技能 %s（%s）脚手架已创建：" % (name, title)]
    lines += ["  - " + f for f in files]
    if tools:
        lines.append("  - skill.py 已预生成工具桩：现在调用会提示「尚未实现」，请用 skill_dev_write_file 补全实现。")
    if not prompt:
        lines.append("  ⚠ 未提供 prompt（注入系统提示词的引导语）；建议尽快用 skill_dev_edit(field=prompt) 补上。")
    lines.append("")
    lines.append("下一步（推荐顺序）：")
    lines.append("  1. skill_dev_write_file 补全 SKILL.md（说明书）与 skill.py（实现）")
    lines.append("  2. skill_dev_validate 校验，修复全部 ✘")
    lines.append("  3. 改动已自动热重载；验收：实测调用每个新工具一次")
    lines.append(_trigger_reload())
    return "\n".join(lines)

# ---------------- skill_dev_edit ----------------


def skill_dev_edit(args) -> str:
    name = str(args.get("skill_name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    field = str(args.get("field") or "").strip()
    if field not in MANIFEST_FIELDS:
        return _err("field 只能是 %s 之一" % sorted(MANIFEST_FIELDS))
    value = args.get("value")
    if value is None:
        return _err("请提供 value")

    d = SKILLS_DIR / name
    mp = d / "skill.json"
    if not mp.exists():
        return _err("技能 %s 不存在或缺少 skill.json" % name)
    m, merr = _load_manifest(d)
    if merr:
        return _err(merr)

    new_name = None
    if field == "tools":
        tools, terr = _parse_tools_spec(value)
        if terr:
            return _err(terr)
        m["tools"] = tools
    elif field == "enabled":
        v = str(value).strip().lower()
        if v not in ("true", "false"):
            return _err("enabled 必须是 true 或 false")
        m["enabled"] = v == "true"
    elif field == "disclosure":
        v = str(value).strip()
        if v not in ("full", "on_demand"):
            return _err("disclosure 只能是 full 或 on_demand")
        m["disclosure"] = v
    elif field == "name":
        new_name = str(value).strip()
        e2 = _valid_name(new_name)
        if e2:
            return _err(e2)
        nd = SKILLS_DIR / new_name
        if nd.exists():
            return _err("目标技能名已存在：%s" % new_name)
        m["name"] = new_name
    else:
        m[field] = str(value)

    mp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["✔ 已更新 skills/%s/skill.json 的 %s 字段。" % (name, field)]
    if new_name and new_name != name:
        try:
            d.rename(SKILLS_DIR / new_name)
            lines.append("✔ 目录已重命名：skills/%s → skills/%s" % (name, new_name))
            lines.append("  （提示：harness_state.json 里旧名的启停记录不会迁移；默认启用不受影响）")
        except Exception as e:
            lines.append("⚠ 清单已改但目录重命名失败：%s。请手动把 skills/%s 改名为 skills/%s。" % (e, name, new_name))
    lines.append(_trigger_reload())
    return "\n".join(lines)


# ---------------- skill_dev_write_file ----------------


def skill_dev_write_file(args) -> str:
    name = str(args.get("skill_name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    filename = str(args.get("filename") or "").strip()
    if filename not in WRITEABLE_FILES:
        return _err("只能整体写入 %s，收到 %r（skill.json 请用 skill_dev_edit 修改字段）"
                    % (sorted(WRITEABLE_FILES), filename))
    content = args.get("content")
    if content is None:
        return _err("请提供 content（完整文件内容）")
    if not isinstance(content, str):
        content = str(content)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return _err("内容超过 %dKB 上限" % (MAX_WRITE_BYTES // 1024))
    d = SKILLS_DIR / name
    if not d.is_dir():
        return _err("技能不存在：skills/%s" % name)
    p = d / filename
    p.write_text(content.rstrip() + "\n", encoding="utf-8")
    warn = ""
    if filename == "skill.py":
        # 写入后立即做完整性自检：语法是否完整、清单工具是否都有实现。
        # 长内容一次写入被截断的典型症状：语法残缺（写到一半）或 HANDLERS 缺工具。
        try:
            compile(content, str(p), "exec")
        except SyntaxError as e:
            warn = ("⚠ 已写入但 skill.py 语法不完整（第 %s 行：%s）——"
                    "内容很可能被截断，请重新完整写入，或分块补齐后再次校验。"
                    % (e.lineno, e.msg))
        except Exception as e:
            warn = "⚠ 已写入但 skill.py 校验异常：%s" % e
        else:
            sym = _collect_code_symbols(content)
            m, merr = _load_manifest(d)
            if not merr and (m.get("tools") or []):
                declared = [(t.get("function") or {}).get("name")
                            for t in m["tools"]]
                missing = [t for t in declared
                           if t not in sym["handlers"] and not sym["has_execute"]]
                if missing:
                    warn = ("⚠ 已写入但清单声明的工具缺少实现：%s（HANDLERS 缺失，"
                            "文件可能被截断）——请补全后再次写入并校验。"
                            % "、".join(missing))
                if not warn and sym.get("stub"):
                    warn = "⚠ 已写入，但代码里仍含未实现桩（_stub/NotImplementedError），工具调用会提示未实现。"
    if warn:
        return ("✔ 已写入 skills/%s/%s（%d 字符）。\n%s\n%s"
                % (name, filename, len(content), warn, _trigger_reload()))
    tip = ("完整性自检通过（语法 OK、清单工具全部有实现）；"
           "长文件建议用 skill_dev_read(start=…, max_lines=…) 读尾部再确认一遍"
           if filename == "skill.py"
           else "skill.py 未变则无需重新校验，可选做一次 validate")
    return ("✔ 已写入 skills/%s/%s（%d 字符）。\n%s\n%s"
            % (name, filename, len(content), tip, _trigger_reload()))


# ---------------- skill_dev_validate ----------------


def _dict_get(d: ast.Dict, key: str):
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _calls_stub(node) -> bool:
    """AST 里是否调用了 _stub（未实现桩的标志）。"""
    if node is None:
        return False
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_stub":
            return True
        if isinstance(f, ast.Name) and f.id == "NotImplementedError":
            return True
        return any(_calls_stub(a) for a in node.args)
    if isinstance(node, ast.Lambda):
        return _calls_stub(node.body)
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_calls_stub(e) for e in node.elts)
    if isinstance(node, ast.BinOp):
        return _calls_stub(node.left) or _calls_stub(node.right)
    if isinstance(node, ast.Name):
        return node.id == "NotImplementedError"
    return False


def _collect_code_symbols(src: str) -> dict:
    """静态提取 skill.py 的 TOOLS 名 / HANDLERS 键 / 是否有 execute / 是否有桩。不执行代码。"""
    out = {"tools": set(), "handlers": set(), "has_execute": False, "stub": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call):
            f = sub.exc.func
            if isinstance(f, ast.Name) and f.id == "NotImplementedError":
                out["stub"] = True
                break
        elif isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Name):
            if sub.exc.id == "NotImplementedError":
                out["stub"] = True
                break
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if t.id == "TOOLS" and isinstance(node.value, (ast.List, ast.Tuple)):
                    for el in node.value.elts:
                        fn = _dict_get(el, "function") if isinstance(el, ast.Dict) else None
                        nm = _dict_get(fn, "name") if isinstance(fn, ast.Dict) else None
                        if isinstance(nm, ast.Constant) and isinstance(nm.value, str):
                            out["tools"].add(nm.value)
                elif t.id == "HANDLERS" and isinstance(node.value, ast.Dict):
                    for k, v in zip(node.value.keys, node.value.values):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            out["handlers"].add(k.value)
                            if _calls_stub(v):
                                out["stub"] = True
        elif isinstance(node, ast.FunctionDef) and node.name == "execute":
            out["has_execute"] = True
            for sub in ast.walk(node):
                if _calls_stub(sub):
                    out["stub"] = True
    return out


def skill_dev_validate(args) -> str:
    name = str(args.get("skill_name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    d = SKILLS_DIR / name
    if not d.is_dir():
        return _err("技能不存在：skills/%s（可用 skill_dev_list 查看）" % name)
    errors, warns, notes = [], [], []

    manifest_tools = []
    mp = d / "skill.json"
    if not mp.exists():
        errors.append("缺少 skill.json —— 技能无法被注册（发现逻辑会跳过此目录）")
    else:
        m, merr = _load_manifest(d)
        if merr:
            errors.append(merr)
        else:
            if str(m.get("name")) != name:
                errors.append("skill.json 的 name=%r 与目录名 %s 不一致（注册以清单 name 为准，请统一）"
                              % (m.get("name"), name))
            if not str(m.get("title") or "").strip():
                warns.append("缺少 title（技能标题，建议简短直观）")
            desc = str(m.get("description") or "").strip()
            if not desc:
                warns.append("缺少 description（一句话说明；on_demand 模式下它显示在对话的技能摘要里）")
            elif len(desc) > 88:
                warns.append("description 共 %d 字，超过 88 字会被摘要截断，建议精简" % len(desc))
            if not str(m.get("prompt") or "").strip():
                warns.append("缺少 prompt（注入系统提示词的引导语，建议以【技能名】开头写明触发条件与规则）")
            if m.get("disclosure") not in ("full", "on_demand"):
                errors.append("disclosure 必须是 full 或 on_demand，当前为 %r" % (m.get("disclosure"),))
            for i, t in enumerate(m.get("tools") or []):
                if not isinstance(t, dict) or t.get("type") != "function":
                    errors.append('第 %d 个工具定义格式错误（须为 {"type":"function","function":{...}}）' % (i + 1))
                    continue
                fn = t.get("function") or {}
                tname = str(fn.get("name") or "")
                if not TOOL_NAME_RE.match(tname):
                    errors.append("工具名 %r 不合法（仅字母/数字/下划线/连字符）" % (tname,))
                    continue
                if tname in manifest_tools:
                    errors.append("工具名重复：%s" % tname)
                manifest_tools.append(tname)
                if not str(fn.get("description") or "").strip():
                    warns.append("工具 %s 缺少 description（模型靠它决定何时调用）" % tname)
                params = fn.get("parameters")
                if not isinstance(params, dict) or params.get("type") != "object":
                    errors.append('工具 %s 缺少 parameters（形如 {"type":"object","properties":{...}}）' % tname)

    pp = d / "skill.py"
    code_tools, code_handlers, has_execute, stub = set(), set(), False, False
    if not pp.exists():
        if manifest_tools:
            errors.append("清单声明了 %d 个工具但没有 skill.py —— 工具将无法执行（纯配置技能只能提供提示词）"
                          % len(manifest_tools))
        else:
            notes.append("没有 skill.py —— 纯提示词技能（仅注入引导语、无工具），合法")
    else:
        try:
            src = pp.read_text(encoding="utf-8")
        except Exception as e:
            errors.append("skill.py 读取失败：%s" % e)
            src = ""
        if src:
            try:
                import py_compile
                cfile = str(pp) + "-check"
                py_compile.compile(str(pp), doraise=True, cfile=cfile)
                notes.append("skill.py 语法编译通过")
            except Exception as e:
                errors.append("skill.py 编译失败：%s" % e)
            finally:
                try:
                    Path(str(pp) + "-check").unlink(missing_ok=True)
                except Exception:
                    pass
            sym = _collect_code_symbols(src)
            code_tools, code_handlers, has_execute, stub = (
                sym["tools"], sym["handlers"], sym["has_execute"], sym["stub"])
            if stub:
                warns.append("代码含未实现桩（_stub/尚未实现/NotImplementedError）——对应工具调用只会返回「未实现」提示")

    if manifest_tools:
        missing_impl = [t for t in manifest_tools if t not in code_handlers and not has_execute]
        if missing_impl:
            errors.append("清单工具没有实现：%s（需在 skill.py 的 HANDLERS 中注册，或提供 execute 分发器）"
                          % "、".join(missing_impl))
    for t in sorted(code_tools):
        if t not in manifest_tools:
            warns.append("skill.py 的 TOOLS 里定义了 %s 但 skill.json 未声明（仍会注册，但建议两处保持一致）" % t)
    for h in sorted(code_handlers):
        if h not in manifest_tools and h not in code_tools:
            warns.append("HANDLERS 注册了 %s 但清单与代码 TOOLS 都没有它（不会被暴露给模型）" % h)

    mdp = d / "SKILL.md"
    if mdp.exists() and mdp.stat().st_size > 0:
        notes.append("SKILL.md 说明书存在（skill_help 的数据源）")
    else:
        warns.append("缺少 SKILL.md（强烈建议补全：它是 skill_help 的完整使用说明；缺失时只能回退到 prompt）")

    lines = ["校验技能：skills/%s/" % name]
    lines += ["  ✘ " + e for e in errors]
    lines += ["  ⚠ " + w for w in warns]
    lines += ["  ✔ " + n for n in notes]
    if not errors and not warns:
        lines.append("✅ 全部通过：无错误无警告，技能合格。")
    elif not errors:
        lines.append("⚠ 无致命错误，但建议处理上述 %d 条警告后发布。" % len(warns))
    else:
        lines.append("✘ 存在 %d 个致命问题：必须全部修复后技能才能正常工作。" % len(errors))
    return "\n".join(lines)


# ---------------- skill_dev_reload ----------------


def skill_dev_reload(args) -> str:
    lines = [_trigger_reload()]
    st = _status_text()
    if st:
        lines.append(st)
    else:
        lines.append("（本地服务未响应；如服务在运行可稍后在 /harness 管理页查看状态）")
    return "\n".join(lines)


# ---------------- skill_dev_remove ----------------


def skill_dev_remove(args) -> str:
    name = str(args.get("skill_name") or "").strip()
    err = _valid_name(name)
    if err:
        return _err(err)
    if not (args.get("confirm") in (True, "true", "True", 1, "1", "yes")):
        return _err("删除是破坏性操作且无回收站：请确认后传 confirm=true 执行（建议先 skill_dev_read 留备份）")
    d = SKILLS_DIR / name
    if not d.is_dir():
        return _err("技能不存在：skills/%s" % name)
    n = len(list(d.rglob("*")))
    shutil.rmtree(d, ignore_errors=True)
    if d.exists():
        return _err("删除失败（目录仍存在）：%s" % d)
    return "✔ 已删除技能 %s（移除 %d 个文件）。\n%s" % (name, n, _trigger_reload())


# ---------------- 注册表 ----------------


HANDLERS = {
    "skill_dev_list": skill_dev_list,
    "skill_dev_read": skill_dev_read,
    "skill_dev_create": skill_dev_create,
    "skill_dev_edit": skill_dev_edit,
    "skill_dev_write_file": skill_dev_write_file,
    "skill_dev_validate": skill_dev_validate,
    "skill_dev_reload": skill_dev_reload,
    "skill_dev_remove": skill_dev_remove,
}
