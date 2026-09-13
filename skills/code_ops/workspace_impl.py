"""工作区切换技能 —— 与前端工作区面板同一套 API（/api/workspace*）。

所有操作通过 HTTP 调用 server.py 的 /api/workspace* 接口（与前端 32_workspace_ui.ts 完全同源）：
- GET  /api/workspace                当前工作区
- POST /api/workspace                设置/切换工作区（写 codex_config.json + harness_bridge.json，热同步执行器）
- GET  /api/workspace/roots          可选根目录（盘符 + 常用目录 + 当前）
- GET  /api/workspace/list?path=     逐级下钻浏览子目录
- GET  /api/workspaces               已保存（收藏）工作区列表 + 当前
- POST /api/workspaces               收藏一个路径
- DELETE /api/workspaces             移出收藏（不删磁盘目录）
- POST /api/workspaces/{path}/activate  激活已保存工作区

绝不另写一套持久化 —— 与前端工作区面板完全同源，DSH/Codex/OpenCode/shell 全部围绕新工作区执行。

修复记录：原实现用同步 urllib 在 server.py 进程内请求自身 HTTPS 接口，
会阻塞 uvicorn 事件循环导致 SSL 握手超时；改为 aiohttp 异步请求，不再阻塞事件循环。
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.parse

import aiohttp

# 与 server.py 的 SERVER_PORT 保持一致（server.py:79 SERVER_PORT = 8000）
# server 实际以 HTTPS 运行（cert.pem/key.pem 存在即 use_https=True，server.py:5754）
SERVER_BASE = "https://127.0.0.1:8000"

# 自签名证书：默认校验证书会抛 SSL 错误，这里显式不校验（与前端浏览器访问一致）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


async def _req(method: str, path: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    """异步请求 server 的 /api/workspace* 接口（不阻塞 uvicorn 事件循环）。"""
    url = SERVER_BASE + path
    try:
        connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.request(
                method, url, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body}
                if resp.status >= 400:
                    detail = data.get("detail") or data if isinstance(data, dict) else data
                    return {"ok": False, "error": f"HTTP {resp.status}", "detail": detail}
                return data
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


async def workspace_get(args: dict) -> str:
    """获取当前全局工作区路径。"""
    data = await _req("GET", "/api/workspace")
    if data.get("ok") is False:
        return f"读取当前工作区失败：{data.get('error')} {data.get('detail')}"
    cwd = data.get("cwd") or ""
    codex = data.get("codex_work_dir") or ""
    bridge = data.get("bridge_cwd") or ""
    parts = [f"当前工作区：{cwd}"]
    if codex and codex != cwd:
        parts.append(f"codex_work_dir：{codex}")
    if bridge and bridge != cwd:
        parts.append(f"bridge_cwd：{bridge}")
    return "；".join(parts)


def _workspace_locked() -> str:
    """独立工作区模式下禁止改全局工作区（长跑 worker 专用）。

    全局工作区是所有会话共享的配置，worker 一改，主人的主对话也跟着被带偏。
    """
    if (os.environ.get("DABAI_WORKSPACE") or "").strip():
        return "当前进程运行在独立工作区（DABAI_WORKSPACE），禁止切换全局工作区。"
    return ""


async def workspace_set(args: dict) -> str:
    """设置/切换全局工作区。"""
    if locked := _workspace_locked():
        return locked
    path = str(args.get("path") or "").strip()
    if not path:
        return "请提供目标工作区路径（path 必填）。"
    data = await _req("POST", "/api/workspace", {"path": path})
    if data.get("ok") is False:
        return f"切换工作区失败：{data.get('error')} {data.get('detail')}"
    cwd = data.get("cwd") or path
    return f"已切换到工作区：{cwd}"


async def workspace_roots(args: dict) -> str:
    """列出可选工作区根目录。"""
    data = await _req("GET", "/api/workspace/roots")
    if data.get("ok") is False:
        return f"获取根目录失败：{data.get('error')} {data.get('detail')}"
    roots = data.get("roots") or []
    lines = [f"可选工作区根目录（{len(roots)} 个）："]
    for r in roots:
        if isinstance(r, dict):
            lines.append(f"- {r.get('path')}（{r.get('label') or ''}）")
        else:
            lines.append(f"- {r}")
    return "\n".join(lines)


async def workspace_list(args: dict) -> str:
    """列出指定目录下的子目录。"""
    path = str(args.get("path") or "").strip()
    qs = urllib.parse.urlencode({"path": path}) if path else ""
    data = await _req("GET", f"/api/workspace/list?{qs}")
    if data.get("ok") is False:
        return f"浏览目录失败：{data.get('error')} {data.get('detail')}"
    dirs = data.get("dirs") or []
    current = data.get("path") or path or "/"
    lines = [f"目录 {current} 下的子目录（{len(dirs)} 个）："]
    for d in dirs:
        if isinstance(d, dict):
            lines.append(f"- {d.get('path')}")
        else:
            lines.append(f"- {d}")
    return "\n".join(lines)


async def workspaces_list(args: dict) -> str:
    """获取已保存（收藏）的工作区列表 + 当前激活工作区。"""
    data = await _req("GET", "/api/workspaces")
    if data.get("ok") is False:
        return f"获取收藏列表失败：{data.get('error')} {data.get('detail')}"
    saved = data.get("saved") or data.get("workspaces") or []
    current = data.get("current") or data.get("cwd") or ""
    lines = [f"当前工作区：{current}", f"已收藏（{len(saved)} 个）："]
    for s in saved:
        if isinstance(s, dict):
            path = s.get("path") or s.get("name") or ""
            mark = " ← 当前" if path == current else ""
            lines.append(f"- {path}{mark}")
        else:
            lines.append(f"- {s}")
    return "\n".join(lines)


async def workspaces_add(args: dict) -> str:
    """把指定路径收藏进已保存工作区列表（去重，不切换当前）。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return "请提供要收藏的目录路径（path 必填）。"
    data = await _req("POST", "/api/workspaces", {"path": path})
    if data.get("ok") is False:
        return f"收藏失败：{data.get('error')} {data.get('detail')}"
    return f"已收藏工作区：{path}"


async def workspaces_remove(args: dict) -> str:
    """把指定路径移出已保存工作区列表（不删磁盘目录）。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return "请提供要移出收藏的目录路径（path 必填）。"
    data = await _req("DELETE", "/api/workspaces", {"path": path})
    if data.get("ok") is False:
        return f"移出收藏失败：{data.get('error')} {data.get('detail')}"
    return f"已移出收藏：{path}"


async def workspaces_activate(args: dict) -> str:
    """激活一个已保存的工作区。"""
    if locked := _workspace_locked():
        return locked
    path = str(args.get("path") or "").strip()
    if not path:
        return "请提供要激活的工作区路径（path 必填）。"
    path_enc = urllib.parse.quote(path, safe="")
    data = await _req("POST", f"/api/workspaces/{path_enc}/activate")
    if data.get("ok") is False:
        return f"激活工作区失败：{data.get('error')} {data.get('detail')}"
    cwd = data.get("cwd") or path
    return f"已激活工作区：{cwd}"


HANDLERS = {
    "workspace_get": workspace_get,
    "workspace_set": workspace_set,
    "workspace_roots": workspace_roots,
    "workspace_list": workspace_list,
    "workspaces_list": workspaces_list,
    "workspaces_add": workspaces_add,
    "workspaces_remove": workspaces_remove,
    "workspaces_activate": workspaces_activate,
}
