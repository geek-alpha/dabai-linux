"""插件系统（Plugin Manager）。

一个插件 = plugins/<插件名>/ 目录：
- plugin.json  清单（name/title/version/description/author/enabled/entry）
- plugin.py    实现（插件入口）

两种编写风格，任选其一：

风格 A —— 继承 Plugin 基类（推荐，能力最全）：
    from harness import Plugin
    class MyPlugin(Plugin):
        name = "my_plugin"
        title = "我的插件"
        version = "1.0.0"
        description = "..."
        prompt = "（注入 system prompt 的片段）"
        def on_load(self): ...               # 可选
        def on_unload(self): ...             # 可选
        def define_tools(self) -> list:      # 可选，返回 OpenAI 工具定义
            return [...]
        async def execute_tool(self, name, arguments) -> str:
            ...

风格 B —— 模块级函数（快速上手）：
    PLUGIN_NAME = "my_plugin"
    PLUGIN_TITLE = "我的插件"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_DESCRIPTION = "..."
    PLUGIN_PROMPT = "（可选）"
    PLUGIN_ENABLED_DEFAULT = True
    PLUGIN_TOOLS = [...]                      # 可选
    async def execute(name, arguments) -> str: ...
    def on_load(ctx): ...                     # 可选
    def on_unload(): ...                      # 可选

钩子 on_load / on_unload 为同步函数；工具执行函数必须可被 await（async def）。
启停状态持久化在 harness_state.json；支持热重载（Harness.reload_all()）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

from .state import StateStore

logger = logging.getLogger("harness.plugins")


class PluginError(Exception):
    """插件加载/执行错误。"""


def _load_code_module(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"harness_plugin_{name}", mod_path)
    if spec is None or spec.loader is None:
        raise PluginError(f"无法加载插件模块: {mod_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        sys.modules.pop(spec.name, None)
        raise PluginError(f"插件 {name} 代码执行失败: {e}\n{traceback.format_exc()}") from e


# 与技能层一致：热重载时清除插件自己导入的子模块缓存，
# 改 impl/依赖文件后无需重启即可生效（跳过 server 共享模块）。
_SHARED_PLUGIN_MODULES = frozenset({"video_lib"})


def _evict_plugin_submodules(entry: dict, plugin_dir: Path) -> None:
    try:
        root = Path(plugin_dir).resolve()
        for mod_name in list(entry.get("_mods_added") or []):
            if mod_name in _SHARED_PLUGIN_MODULES:
                continue
            mod = sys.modules.get(mod_name)
            if mod is None:
                continue
            try:
                f = getattr(mod, "__file__", None)
            except Exception:
                f = None
            if not f:
                continue
            try:
                p = Path(f).resolve()
            except Exception:
                continue
            if root == p or root in p.parents:
                sys.modules.pop(mod_name, None)
        # 字节码缓存可能残留旧 pyc（同秒/同长度编辑时 pyc 校验会误判为未变）
        pycache = root / "__pycache__"
        if pycache.is_dir():
            for pyc in list(pycache.glob("*.pyc")):
                try:
                    pyc.unlink()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("清除插件子模块缓存失败: %s", e)


class Plugin:
    """插件基类 —— 继承并覆写钩子即可（风格 A）。"""

    name: str = ""
    title: str = ""
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    prompt: str = ""
    enabled_default: bool = True

    def __init__(self, manager: "PluginManager"):
        self.manager = manager
        self._tools: dict = {}

    def define_tools(self) -> list:
        """返回本插件的 OpenAI 工具定义列表（空列表 = 无工具）。"""
        return []

    def on_load(self) -> None:
        """插件加载钩子（同步；可选）。"""

    def on_unload(self) -> None:
        """插件卸载钩子（同步；可选）。"""

    def on_server_start(self, app) -> None:
        """服务端启动钩子（可选）：拿到 FastAPI app，可挂载 REST 路由。

        由 server.py 的 startup 事件经 Harness.on_server_start 调用；应在实现里幂等。
        """

    def on_server_stop(self) -> None:
        """服务端停止钩子（可选）：清理已挂载的路由/资源。"""

    async def execute_tool(self, name: str, arguments: dict) -> str:
        """执行插件工具。未实现时抛 PluginError。"""
        raise PluginError(f"插件 {self.name} 未实现工具 {name}")


class _FunctionStyleAdapter(Plugin):
    """把风格 B（模块级函数）适配成统一 Plugin 接口。"""

    def __init__(self, manager: "PluginManager", module):
        self._module = module
        super().__init__(manager)
        self.name = str(getattr(module, "PLUGIN_NAME", "") or "").strip()
        self.title = str(getattr(module, "PLUGIN_TITLE", "") or "").strip() or self.name
        self.version = str(getattr(module, "PLUGIN_VERSION", "1.0.0"))
        self.description = str(getattr(module, "PLUGIN_DESCRIPTION", "") or "")
        self.author = str(getattr(module, "PLUGIN_AUTHOR", "") or "")
        self.prompt = str(getattr(module, "PLUGIN_PROMPT", "") or "")
        self.enabled_default = bool(getattr(module, "PLUGIN_ENABLED_DEFAULT", True))
        self._tools = {t["function"]["name"]: t for t in (getattr(module, "PLUGIN_TOOLS", []) or [])
                       if (t.get("function") or {}).get("name")}

    def define_tools(self) -> list:
        return list(self._tools.values())

    def on_load(self):
        fn = getattr(self._module, "on_load", None)
        if callable(fn):
            fn(self)

    def on_unload(self):
        fn = getattr(self._module, "on_unload", None)
        if callable(fn):
            fn()

    def on_server_start(self, app):
        fn = getattr(self._module, "on_server_start", None)
        if callable(fn):
            fn(self, app)

    def on_server_stop(self):
        fn = getattr(self._module, "on_server_stop", None)
        if callable(fn):
            fn()

    async def execute_tool(self, name: str, arguments: dict) -> str:
        fn = getattr(self._module, "execute", None)
        if not callable(fn):
            raise PluginError(f"插件 {self.name} 未定义 execute() 或工具 {name} 的实现")
        # 同步处理器进线程池，防止阻塞事件循环（与技能层一致）
        if asyncio.iscoroutinefunction(fn):
            result = await fn(name, arguments)
        else:
            from .tool_thread import run_in_tool_thread
            result = await run_in_tool_thread(fn, name, arguments)
        return str(result)


class PluginManager:
    """插件发现 / 加载 / 启停 / 执行 / 热重载。"""

    def __init__(self, harness, base_dir: Path, directory: Path):
        self.harness = harness
        self.base_dir = base_dir
        self.directory = Path(directory)
        self.state: StateStore = harness.state
        import threading
        self._load_lock = threading.Lock()
        # name -> {instance, tools, info, broken, error}
        self._loaded: dict[str, dict] = {}

    # ---------- 发现 ----------

    def discover(self) -> list[dict]:
        """扫描插件目录，返回清单（含默认值）。"""
        out: list[dict] = []
        if not self.directory.is_dir():
            return out
        for child in sorted(self.directory.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "plugin.json"
            if not manifest_path.exists():
                # 允许纯代码插件：目录里有 plugin.py 但没有清单
                if (child / "plugin.py").exists():
                    out.append(self._default_info(child.name, child))
                continue
            info = self._read_manifest(manifest_path, child.name, child)
            if info:
                out.append(info)
        return out

    def _default_info(self, name: str, child: Path) -> dict:
        return {
            "name": name, "title": name, "version": "1.0.0",
            "description": "", "author": "", "enabled": True,
            "path": str(child), "broken": False, "error": "",
        }

    def _read_manifest(self, manifest_path: Path, fallback_name: str, child: Path) -> Optional[dict]:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict):
                return self._default_info(fallback_name, child)
        except Exception as e:
            logger.warning("插件清单 %s 解析失败: %s", manifest_path, e)
            return self._default_info(fallback_name, child)
        name = str(m.get("name") or "").strip() or fallback_name
        return {
            "name": name,
            "title": str(m.get("title") or "").strip() or name,
            "version": str(m.get("version") or "1.0.0"),
            "description": str(m.get("description") or "").strip(),
            "author": str(m.get("author") or "").strip(),
            "enabled": bool(m.get("enabled", True)),
            "entry": str(m.get("entry") or "plugin.py"),
            "path": str(child),
            "broken": False,
            "error": "",
        }

    # ---------- 加载 ----------

    def ensure_loaded(self) -> None:
        with self._load_lock:
            need = {m["name"] for m in self.discover()}
            for name in need:
                if name not in self._loaded:
                    self._load(name)

    def _load(self, name: str) -> Optional[dict]:
        info = None
        for m in self.discover():
            if m["name"] == name:
                info = m
                break
        if info is None:
            self._loaded.pop(name, None)
            return None
        entry = {"instance": None, "tools": {}, "info": info,
                 "broken": False, "error": ""}
        plugin_dir = Path(info["path"])
        entry_path = plugin_dir / str(info.get("entry", "plugin.py") or "plugin.py")
        if not entry_path.exists():
            entry_path = plugin_dir / "plugin.py"
        if not entry_path.exists():
            # 缺入口文件：标记 broken，避免"已启用但 0 工具"的静默空壳
            entry["broken"] = True
            entry["error"] = (f"缺少插件入口文件 {plugin_dir.name}/plugin.py"
                              f"（manifest 声明 entry={info.get('entry')}）")
            self._loaded[name] = entry
            self.harness.record("plugin", f"插件 {name} 缺少入口文件，已标记 broken")
            return entry
        try:
            if entry_path.exists():
                _mods_before = set(sys.modules)
                module = _load_code_module(entry_path, name)
                instance = self._instantiate(module, name, info)
                entry["instance"] = instance
                entry["_mods_added"] = sorted(set(sys.modules) - _mods_before)
                try:
                    instance.on_load()
                except Exception as e:
                    logger.warning("插件 %s on_load 钩子失败（继续加载）: %s", name, e)
                for t in instance.define_tools() or []:
                    fn = (t.get("function") or {}).get("name")
                    if fn:
                        entry["tools"][fn] = t
            self._loaded[name] = entry
            return entry
        except PluginError as e:
            entry["broken"] = True
            entry["error"] = str(e)
            self._loaded[name] = entry
            self.harness.record("plugin", f"插件 {name} 加载失败: {e}")
            return entry

    def _instantiate(self, module, name: str, info: dict) -> Plugin:
        """找到模块里定义的 Plugin 子类；找不到就用风格 B 适配器。"""
        candidates = []
        for _, value in vars(module).items():
            if isinstance(value, type) and issubclass(value, Plugin)                     and value is not Plugin and value.__module__ == module.__name__:
                candidates.append(value)
        if candidates:
            cls = candidates[0]
            inst = cls(self)
            if not inst.name:
                inst.name = name
            return inst
        return _FunctionStyleAdapter(self, module)

    # ---------- 启停 / 重载 ----------

    def is_enabled(self, name: str) -> bool:
        default = True
        for m in self.discover():
            if m["name"] == name:
                default = bool(m.get("enabled", True))
                break
        return self.state.is_enabled("plugins", name, default=default)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        self.ensure_loaded()
        if name not in self._loaded:
            return False
        self.state.set_enabled("plugins", name, enabled)
        self.harness.record("plugin", f"插件 {name} 已{'启用' if enabled else '禁用'}")
        try:
            self.harness._rebuild_index()
        except Exception:
            pass
        return True

    def reload(self, name: str) -> bool:
        """热重载单个插件（卸载 → 重新加载）。"""
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
        instance = entry.get("instance") if entry else None
        if instance is not None:
            try:
                instance.on_unload()
            except Exception as e:
                logger.warning("插件 %s on_unload 钩子失败: %s", name, e)
        if entry:
            try:
                _evict_plugin_submodules(entry, Path(entry["info"].get("path") or ""))
            except Exception:
                pass

    # ---------- 查询 / 执行 ----------

    def tool_specs(self) -> list:
        self.ensure_loaded()
        out = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            out.extend(list(entry.get("tools", {}).values()))
        return out

    def prompt_extras(self) -> str:
        self.ensure_loaded()
        parts = []
        for name, entry in self._loaded.items():
            if entry.get("broken"):
                continue
            if not self.is_enabled(name):
                continue
            inst = entry.get("instance")
            if inst is None:
                continue
            p = (getattr(inst, "prompt", "") or "").strip()
            if p:
                parts.append(p)
        return "\n\n".join(parts)

    async def execute_tool(self, plugin_name: str, tool_name: str, arguments: dict) -> tuple:
        entry = self._loaded.get(plugin_name)
        if entry is None:
            return None, ""
        if entry.get("broken"):
            return f"插件 {plugin_name} 加载失败，无法执行：{entry.get('error', '')}", "plugin"
        if not self.is_enabled(plugin_name):
            return f"插件 {plugin_name} 已禁用（可在 /harness 管理页启用）", "plugin"
        instance = entry.get("instance")
        if instance is None:
            return None, ""
        try:
            method = instance.execute_tool
            # 同步实现进线程池执行，防止事件循环被工具阻塞
            if asyncio.iscoroutinefunction(method):
                result = await method(tool_name, arguments)
            else:
                from .tool_thread import run_in_tool_thread
                result = await run_in_tool_thread(method, tool_name, arguments)
            return str(result), "plugin"
        except PluginError as e:
            return None, ""  # 未实现 → 让路由继续找下一家
        except Exception as e:
            return f"插件 {plugin_name} 执行工具 {tool_name} 失败: {e}", "plugin"

    def list_info(self) -> list[dict]:
        self.ensure_loaded()
        out = []
        for name in sorted(self._loaded.keys()):
            entry = self._loaded[name]
            info = entry["info"]
            inst = entry.get("instance")
            out.append({
                "name": name,
                "title": info.get("title", name),
                "version": info.get("version", "1.0.0"),
                "description": info.get("description", "") or (getattr(inst, "description", "") if inst else ""),
                "author": info.get("author", "") or (getattr(inst, "author", "") if inst else ""),
                "enabled": self.is_enabled(name),
                "broken": bool(entry.get("broken")),
                "error": entry.get("error", ""),
                "tools": sorted(entry.get("tools", {}).keys()),
                "path": info.get("path", ""),
            })
        return out
