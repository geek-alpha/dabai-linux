# -*- coding: utf-8 -*-
"""技能/插件热重载的公共逻辑：卸载前清掉该目录内导入的模块缓存。

判据是模块的 __file__ 落在目标目录内，而不是加载期快照 _mods_added：
后者只记录加载那一刻新出现的模块，函数体内延迟 import 的兄弟模块
（如 adb_ui 里的 `import viewtree`）不在其中，热重载后会一直读旧代码。

目标目录必须先通过「是 base_dir 的真子目录」校验。这不是多余的谨慎：
Path("").resolve() 得到的是 cwd，一旦清单里 path 字段缺失，就会把整个项目
的模块从 sys.modules 里摘掉（实测误清 harness.plugins / runtime / skills）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("harness.reload")

# 与 server 共用同一模块实例的共享模块（video_lib 的 STREAMS/队列状态互通），
# 清除会让两者状态分裂；连同它的子模块一起保护。
DEFAULT_SHARED_MODULES = frozenset({"video_lib"})


def is_shared_module(mod_name: str, shared=DEFAULT_SHARED_MODULES) -> bool:
    """共享模块及其子模块（video_lib.util 等）都不能清，否则实例状态分裂。"""
    return any(mod_name == s or mod_name.startswith(s + ".")
               for s in shared)


def evict_dir_modules(dir_path, base_dir, *, shared=DEFAULT_SHARED_MODULES,
                      label: str = "模块") -> bool:
    """清掉 dir_path 内所有模块的 sys.modules 缓存，并删除其 __pycache__。

    dir_path 必须是 base_dir 的真子目录，否则一律不动手（fail-safe：宁可这次
    热重载不生效，也不能误清别的模块）。返回 True 表示校验通过并执行了清理。
    """
    try:
        raw = str(dir_path or "").strip()
        base_raw = str(base_dir or "").strip()
        if not raw:
            logger.warning("跳过清理%s缓存：目录路径为空", label)
            return False
        if not base_raw:
            logger.warning("跳过清理%s缓存：基准目录为空，无法校验 %s", label, raw)
            return False
        root = Path(raw).resolve()
        base = Path(base_raw).resolve()
        if not root.is_dir():
            logger.warning("跳过清理%s缓存：%s 不是目录", label, root)
            return False
        if root == base or base not in root.parents:
            logger.warning("跳过清理%s缓存：%s 不在 %s 之下", label, root, base)
            return False

        for mod_name, mod in list(sys.modules.items()):
            if is_shared_module(mod_name, shared):
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

        # 字节码缓存可能残留旧 pyc（同秒/同长度编辑时 pyc 校验会误判为未变），
        # 连同目录的 __pycache__ 一起清掉，保证热重载一定读到新代码
        pycache = root / "__pycache__"
        if pycache.is_dir():
            for pyc in list(pycache.glob("*.pyc")):
                try:
                    pyc.unlink()
                except Exception:
                    pass
        return True
    except Exception as e:
        logger.warning("清除%s子模块缓存失败: %s", label, e)
        return False
