"""技能系统（Skill Registry）。

一个技能 = skills/<技能名>/ 目录：
- skill.json  清单（name/title/version/description/author/enabled/prompt/tools）
- skill.py    实现（可选；TOOLS/PROMPT/execute 或 HANDLERS/on_load/on_unload）
- SKILL.md    说明文档（可选）

技能能做的三件事：
1. 向对话注入 system prompt 片段（skill.json 的 prompt 或 skill.py 的 PROMPT）；
2. 注册 OpenAI function-calling 工具定义（skill.json 的 tools 或 skill.py 的 TOOLS）；
3. 提供工具执行实现（skill.py 的 execute(name, args) 分发，或 HANDLERS 表）。

生命周期钩子 on_load(ctx) / on_unload(ctx)、execute/HANDLERS 均为可空；
工具定义与提示词可以只来自 skill.json（纯配置技能），执行实现则必须来自 skill.py。

作者规范（skill.py 模板）：
    TOOLS = [ {"type":"function","function":{"name":"my_tool","description":"...",
               "parameters":{"type":"object","properties":{...}}}} ]
    PROMPT = "（注入 system prompt 的说明文字）"
    HANDLERS = {"my_tool": async def handler(args: dict) -> str}
    # 或单一分发器：
    # async def execute(name: str, arguments: dict) -> str
    def on_load(ctx): ...    # 可选，同步
    def on_unload(ctx): ...  # 可选，同步

启停状态持久化在 harness_state.json；热重载见 Harness.reload_all()。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .state import StateStore
from ._reload import evict_dir_modules

logger = logging.getLogger("harness.skills")


class SkillError(Exception):
    """技能加载/执行错误。"""


def _load_code_module(mod_path: Path, name: str):
    """按文件路径导入一个 Python 模块（避免污染 sys.modules 只读场景）。"""
    spec = importlib.util.spec_from_file_location(f"harness_skill_{name}", mod_path)
    if spec is None or spec.loader is None:
        raise SkillError(f"无法加载技能模块: {mod_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        sys.modules.pop(spec.name, None)
        raise SkillError(f"技能 {name} 代码执行失败: {e}\n{traceback.format_exc()}") from e


class Skill:
    """技能描述对象（向技能代码注入的上下文，作者也可以直接继承它）。"""

    def __init__(self, name: str, title: str = "", version: str = "1.0.0",
                 description: str = "", author: str = "",
                 enabled_default: bool = True):
        self.name = name
        self.title = title or name
        self.version = version
        self.description = description
        self.author = author
        self.enabled_default = enabled_default


class SkillRegistry:
    """技能发现 / 加载 / 启停 / 执行。"""

    def __init__(self, harness, base_dir: Path, directory: Path):
        self.harness = harness
        self.base_dir = base_dir
        self.directory = Path(directory)
        self.state: StateStore = harness.state
        # name -> {info, tools, prompt, module, dispatch, handlers, broken}
        self._loaded: dict[str, dict] = {}
        import threading
        self._load_lock = threading.Lock()
        # discover 缓存：以各技能 skill.json 的 (mtime_ns, size) 指纹失效。
        # 避免 ensure_loaded/is_enabled/tool_specs 等在每轮对话重复 open+json.load 全部清单。
        self._discover_cache: Optional[list[dict]] = None
        self._discover_cache_key: Optional[tuple] = None

    # ---------- 发现 ----------

    def discover(self) -> list[dict]:
        """扫描技能目录，返回清单元数据（未加载的也能列出）。

        带内存缓存：以各技能 skill.json 的 (mtime_ns, size) 指纹失效。
        只有清单文件被编辑/删除/新增时才重新扫描，避免 ensure_loaded / is_enabled /
        tool_specs 等在每轮对话重复 open + json.load 全部清单（技能一多就是 O(n²) 磁盘 IO）。
        """
        if not self.directory.is_dir():
            return []
        # 构建当前目录指纹：dir mtime 足够粗，改用遍历各技能 skill.json 的 mtime_ns+size
        manifest_map: dict[str, tuple] = {}
        try:
            for child in sorted(self.directory.iterdir()):
                if not child.is_dir():
                    continue
                man = child / "skill.json"
                if not man.exists():
                    continue
                try:
                    st = man.stat()
                except OSError:
                    continue
                manifest_map[str(man)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            manifest_map = {}
        if self._discover_cache is not None and self._discover_cache_key == tuple(manifest_map.items()):
            return self._discover_cache
        out: list[dict] = []
        for child in sorted(self.directory.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "skill.json"
            if not manifest_path.exists():
                continue
            info = self._read_manifest(manifest_path, child.name)
            if info:
                out.append(info)
        self._discover_cache = out
        self._discover_cache_key = tuple(manifest_map.items())
        return out

    def _read_manifest(self, manifest_path: Path, fallback_name: str) -> Optional[dict]:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict):
                return None
        except Exception as e:
            logger.warning("技能清单 %s 解析失败: %s", manifest_path, e)
            return None
        name = str(m.get("name") or "").strip() or fallback_name
        disclosure = str(m.get("disclosure") or "").strip().lower()
        if disclosure not in ("full", "on_demand"):
            disclosure = "full"
        return {
            "name": name,
            "title": str(m.get("title") or "").strip() or name,
            "version": str(m.get("version") or "1.0.0"),
            "description": str(m.get("description") or "").strip(),
            "author": str(m.get("author") or "").strip(),
            "enabled": bool(m.get("enabled", True)),
            "disclosure": disclosure,
            "path": str(manifest_path.parent),
            "broken": False,
            "error": "",
        }

    # ---------- 加载 ----------

    def ensure_loaded(self) -> None:
        """确保全部技能已加载（幂等；加载失败不阻断其他技能）。"""
        with self._load_lock:
            need = {m["name"] for m in self.discover()}
            for name in need:
                if name not in self._loaded:
                    self._load(name)

    def _load(self, name: str) -> Optional[dict]:
        """加载单个技能到内存。失败时记录 broken 条目并继续。"""
        info = None
        for m in self.discover():
            if m["name"] == name:
                info = m
                break
        if info is None:
            self._loaded.pop(name, None)
            return None
        entry = {"info": info, "tools": {}, "prompt": "", "module": None,
                 "dispatch": None, "handlers": {}, "broken": False, "error": "",
                 "disclosure": str(info.get("disclosure", "full") or "full"),
                 "detail": "", "brief": ""}
        skill_dir = Path(info["path"])
        manifest_tools = self._manifest_tools(skill_dir / "skill.json")
        for t in manifest_tools:
            fn = (t.get("function") or {}).get("name")
            if fn:
                entry["tools"][fn] = t
        manifest_prompt = ""
        try:
            with open(skill_dir / "skill.json", "r", encoding="utf-8") as f:
                mp = (json.load(f) or {}).get("prompt", "")
            manifest_prompt = str(mp or "")
        except Exception:
            pass
        entry["prompt"] = manifest_prompt

        mod_path = skill_dir / "skill.py"
        if mod_path.exists():
            try:
                module = _load_code_module(mod_path, name)
                entry["module"] = module
                # 代码里的 PROMPT 优先于清单 prompt
                code_prompt = getattr(module, "PROMPT", "") or ""
                if code_prompt:
                    entry["prompt"] = str(code_prompt)
                for t in getattr(module, "TOOLS", []) or []:
                    fn = (t.get("function") or {}).get("name")
                    if fn:
                        entry["tools"][fn] = t
                entry["dispatch"] = getattr(module, "execute", None)
                entry["handlers"] = dict(getattr(module, "HANDLERS", {}) or {})
            except SkillError as e:
                entry["broken"] = True
                entry["error"] = str(e)
                self._loaded[name] = entry
                self.harness.record("skill", f"技能 {name} 加载失败: {e}")
                return entry

        # 渐进式披露：detail = SKILL.md 说明书（优先）或 manifest/代码 prompt；
        # brief = 一句话摘要（渐进模式下注入 system prompt）
        md_path = skill_dir / "SKILL.md"
        if md_path.exists():
            try:
                md_text = md_path.read_text(encoding="utf-8").strip()
                if md_text:
                    entry["detail"] = md_text
            except Exception as e:
                logger.warning("技能 %s 读取 SKILL.md 失败: %s", name, e)
        if not entry["detail"]:
            entry["detail"] = (entry.get("prompt") or "").strip()
        entry["brief"] = self._build_brief(entry)

        # 生命周期钩子（同步；失败不致命）
        try:
            ctx = Skill(name=name, title=info["title"], version=info["version"],
                        description=info["description"], author=info["author"],
                        enabled_default=info["enabled"])
            if entry.get("module") is not None:
                on_load = getattr(entry["module"], "on_load", None)
                if callable(on_load):
                    on_load(ctx)
        except Exception as e:
            logger.warning("技能 %s on_load 钩子失败: %s", name, e)

        self._loaded[name] = entry
        return entry

    # ---------- 启停 / 重载 ----------

    def is_enabled(self, name: str) -> bool:
        # 已加载的技能直接复用 entry 的 enabled 默认值，不再逐个重扫 discover。
        # 未加载的（例如刚加载列表里出现但还没 _load）才回退到 discover 查默认。
        entry = self._loaded.get(name)
        default = True
        if entry is not None:
            default = bool(entry.get("info", {}).get("enabled", True))
        else:
            for m in self.discover():
                if m["name"] == name:
                    default = bool(m.get("enabled", True))
                    break
        return self.state.is_enabled("skills", name, default=default)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用技能（持久化）。返回操作是否成功。"""
        self.ensure_loaded()
        if name not in self._loaded:
            return False
        self.state.set_enabled("skills", name, enabled)
        self.harness.record("skill", f"技能 {name} 已{'启用' if enabled else '禁用'}")
        try:
            self.harness._rebuild_index()
        except Exception:
            pass
        return True

    def reload(self, name: str) -> bool:
        """热重载单个技能：卸载后重新加载。"""
        with self._load_lock:
            self._unload(name)
            entry = self._load(name)
            try:
                self.harness._rebuild_index()
            except Exception:
                pass
            return entry is not None

    def _unload(self, name: str) -> None:
        entry = self._loaded.pop(name, None)
        if entry and entry.get("module") is not None:
            try:
                on_unload = getattr(entry["module"], "on_unload", None)
                if callable(on_unload):
                    on_unload(Skill(name=name))
            except Exception as e:
                logger.warning("技能 %s on_unload 钩子失败: %s", name, e)
            # 热重载生效的关键：清掉该技能自己导入的子模块缓存
            try:
                evict_dir_modules(entry["info"].get("path"), self.directory, label="技能")
            except Exception:
                pass

    # ---------- 查询 ----------

    def _manifest_tools(self, manifest_path: Path) -> list:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict):
                return list(m.get("tools", []) or [])
        except Exception:
            pass
        return []

    def tool_specs(self) -> list:
        """返回全部启用技能的 OpenAI 工具定义。"""
        self.ensure_loaded()
        out = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            out.extend(list(entry.get("tools", {}).values()))
        return out

    def tool_specs_progressive(self) -> list:
        """渐进披露模式下的工具定义：只返回 full 技能的 OpenAI 工具。

        on_demand 技能的工具不注入（其用法经 skill_help 按需拉取），
        从而大幅削减每轮固定注入的工具 schema 体积。"""
        self.ensure_loaded()
        out = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            if entry.get("disclosure") == "on_demand":
                continue
            out.extend(list(entry.get("tools", {}).values()))
        return out

    def tool_specs_for(self, name: str) -> list:
        """返回单个技能的全部 OpenAI 工具定义（无论披露级别）。

        供 skill_help 按需注册：模型读完某技能说明书后，把它的工具动态加入
        可调用列表。技能不存在 / 加载失败 / 被禁用时返回空列表。"""
        self.ensure_loaded()
        entry = self._loaded.get(name)
        if entry is None or entry.get("broken"):
            return []
        if not self.is_enabled(name):
            return []
        return list(entry.get("tools", {}).values())

    def prompt_extras(self) -> str:
        """返回全部启用技能注入 system prompt 的片段（拼接）。"""
        self.ensure_loaded()
        parts = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            p = (entry.get("prompt") or "").strip()
            if p:
                parts.append(p)
        return "\n\n".join(parts)

    # ---------- 渐进式披露 ----------

    def _build_brief(self, entry: dict) -> str:
        """生成技能的「一句话摘要」：渐进披露模式下注入 system prompt 的内容。"""
        info = entry["info"]
        name = info["name"]
        title = info.get("title") or name
        desc = (info.get("description") or "").strip()
        if len(desc) > 88:
            desc = desc[:85] + "…"
        # 不再在摘要里罗列工具名：on_demand 技能的工具需经 skill_help 激活后才可调用，
        # 摘要列了工具名会诱导模型直接调 → 报"尚未注册"。工具名只在激活后随工具定义一起注入。
        return (f"【技能 {title}】{desc}。"
                f"需要完整用法时调用 skill_help(\"{name}\") 查看说明书。")

    def brief_extras(self, active_skills=None) -> str:
        """渐进披露模式下的 system prompt 片段（与工具注册同步）。

        - `full` 技能：注入完整 prompt（行为不变）；
        - `on_demand` 技能：若已在 `active_skills`（即已通过 skill_help 激活、工具已注册）
          则注入其一句话摘要；否则只汇总成一行技能名清单——不再逐条常驻，避免摘要常驻
          却调不出工具（诱导模型直接调未注册工具）。
        这保证「工具定义说明」跟随「技能激活状态」收敛（激活集按会话保持，
        与 agent._stabilize_tools_for_new_turn 一致）。
        """
        self.ensure_loaded()
        active = set(active_skills or [])
        parts = []
        on_demand_names = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            if entry.get("disclosure") == "on_demand":
                if name in active:
                    parts.append(entry.get("brief") or self._build_brief(entry))
                else:
                    on_demand_names.append(name)
            else:
                p = (entry.get("prompt") or "").strip()
                if p:
                    parts.append(p)
        if on_demand_names:
            parts.append(
                "【可按需加载的技能】" + ", ".join(sorted(on_demand_names)) +
                "。如需使用其中某个技能，请调用 skill_help(\"技能名\") 按需加载其工具与说明。"
            )
        return "\n\n".join(parts)

    def help_text(self, name: str) -> Optional[str]:
        """返回技能的完整使用说明书（skill_help 工具的数据源）。

        优先使用 SKILL.md 全文；没有则回退为 manifest/代码 prompt + 工具参数说明。
        返回 None 表示技能不存在。"""
        self.ensure_loaded()
        entry = self._loaded.get(name)
        if entry is None:
            return None
        if entry.get("broken"):
            return f"技能 {name} 加载失败，说明书不可用：{entry.get('error', '')}"
        detail = (entry.get("detail") or "").strip()
        if detail:
            return detail
        # 兜底：prompt + 工具参数说明
        info = entry["info"]
        lines = [
            f"# {info.get('title', name)}（技能）v{info.get('version', '1.0.0')}",
            f"说明：{(info.get('description') or '（无描述）').strip()}",
        ]
        p = (entry.get("prompt") or "").strip()
        if p:
            lines.append("\n使用说明：\n" + p)
        tools = entry.get("tools", {})
        if tools:
            lines.append("\n工具：")
            for tname in sorted(tools.keys()):
                t = tools[tname] or {}
                fn = t.get("function", {}) or {}
                tdesc = (fn.get("description") or "").strip()
                params = ((fn.get("parameters") or {}).get("properties") or {})
                req = ((fn.get("parameters") or {}).get("required") or [])
                arg_txt = ", ".join(
                    f"{k}: {v.get('type', '').replace('string', '文本').replace('integer', '整数').replace('number', '数字').replace('boolean', '布尔')}"
                    + ("（必填）" if k in req else "")
                    for k, v in params.items()
                ) or "无参数"
                lines.append(f"- {tname}({arg_txt})：{tdesc}")
        return "\n".join(lines)

    async def execute_tool(self, skill_name: str, tool_name: str, arguments: dict) -> tuple:
        """执行技能内的某个工具。返回 (result_text, 'skill')；未命中返回 (None, '')。"""
        # 沙箱闸门：非管理员只能碰自己沙箱内的路径，危险工具直接拒（见 sandbox.py）
        import sandbox as _sb

        actor = _sb.current()
        deny = _sb.check_tool(actor, tool_name)
        if deny:
            return deny, "skill"
        try:
            arguments = _sb.prepare_args(actor, tool_name, arguments)
        except _sb.SandboxError as e:
            return f"沙箱拒绝：{e}", "skill"
        entry = self._loaded.get(skill_name)
        if entry is None:
            return None, ""
        if entry.get("broken"):
            return f"技能 {skill_name} 加载失败，无法执行：{entry.get('error', '')}", "skill"
        if not self.is_enabled(skill_name):
            return f"技能 {skill_name} 已禁用（可在 /harness 管理页启用）", "skill"
        # 单一分发器：把工具名传进去
        dispatch = entry.get("dispatch")
        if callable(dispatch):
            try:
                # 同步处理器一律进线程池执行，绝不阻塞事件循环——
                # 否则工具里的 subprocess/网络/大文件读写会冻结整个服务
                # （心跳/推理指示/超时全部失效，表现为"莫名其妙卡死无显示"）。
                # 用工具专用线程池，避免长工具占满默认池拖死记忆库写入。
                if asyncio.iscoroutinefunction(dispatch):
                    result = await dispatch(tool_name, arguments)
                else:
                    from .tool_thread import run_in_tool_thread
                    result = await run_in_tool_thread(dispatch, tool_name, arguments)
                return str(result), "skill"
            except Exception as e:
                return f"技能 {skill_name} 执行工具 {tool_name} 失败: {e}", "skill"
        # HANDLERS 表
        handler = entry.get("handlers", {}).get(tool_name)
        if handler is None:
            return None, ""
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(arguments)
            else:
                from .tool_thread import run_in_tool_thread
                result = await run_in_tool_thread(handler, arguments)
            return str(result), "skill"
        except Exception as e:
            return f"技能 {skill_name} 执行工具 {tool_name} 失败: {e}", "skill"

    def module(self, name: str):
        """取已加载技能的模块对象（服务端查技能内部状态用，如视觉判定）。"""
        self.ensure_loaded()
        return (self._loaded.get(name) or {}).get("module")

    def list_info(self) -> list[dict]:
        """返回技能清单 + 运行时状态（供管理界面/API）。"""
        self.ensure_loaded()
        out = []
        for name in sorted(self._loaded.keys()):
            entry = self._loaded[name]
            info = entry["info"]
            out.append({
                "name": name,
                "title": info.get("title", name),
                "version": info.get("version", "1.0.0"),
                "description": info.get("description", ""),
                "author": info.get("author", ""),
                "disclosure": str(entry.get("disclosure", "full") or "full"),
                "enabled": self.is_enabled(name),
                "broken": bool(entry.get("broken")),
                "error": entry.get("error", ""),
                "tools": sorted(entry.get("tools", {}).keys()),
                "path": info.get("path", ""),
            })
        return out
