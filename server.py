"""3D 虚拟 AI 角色陪聊 —— FastAPI 服务器

# mixamo no-skin session 2026-08-26（本会话热重载已临时关闭，改代码不重启）

特性：
- WebSocket 实时双向通信（文本 + 语音）
- 语音转文字 (SiliconFlow SenseVoiceSmall 云端 API)
- 文字转语音 (edge-tts)
- 3D 模型导入与管理 (glb/gltf/vrm)
- 静态前端托管 (web/)
- 绑定 0.0.0.0 实现局域网手机访问
- 技能工具调用（全部 skill 化）+ Function Calling
- 长期记忆（SQLite 持久化）
"""
import asyncio
import base64
import contextvars
import difflib
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, List, Optional


import edge_tts
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request, Query
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.websockets import WebSocketState

import attach_text
import auth_core
import email_verify
import music_lib
import peer_mesh
import turn_quota
import video_fav_lib
import video_history_lib
import video_sources_lib
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from agent import (AIAgent, TextDelta, ToolCallProgress, ToolCallResult,
                   ToolCallStart, ThinkingDelta, StreamDelta, ReasoningDelta,
                   FinalText, UsageEvent, TurnStatus, get_available_tools,
                   active_role_card_id, load_card_users, role_card_of, save_card_users,
                   migrate_legacy_active_card)
from ai_autonomy import AutonomyHub
from perception_dispatcher import PerceptionDispatcher, EventCategory
from reward_memory import RewardMemory
from rl_coordinator import get_coordinator, UnifiedMode, UnifiedState
# 离线 RL 双模块（真实闭环调用点）：rlhf_calibrator 偏好校准 + world_model_trainer 经验训练
from rlhf_calibrator import RLHFLiteTrainer
from world_model_trainer import WorldModelTrainer
import codex_runner
import platform_compat as pc  # 跨平台兼容层：目录/进程/子进程差异统一走它
from harness_bridge import get_bridge, HarnessBridgeError
from hot_reload import start_hot_reload
from media_workers import get_media_workers as _get_media_workers
from proxy_utils import is_local_url, requests_proxies, resolve_llm_proxy
from sub_agents import get_sub_agents as _get_sub_agents


def _harness():
    """harness 全局单例的薄封装（懒导入，避免导入期副作用）。

    ⚠ 这个函数此前**不存在**：本文件有 26 处调用 `_harness()`（管理台 / 任务中心 /
    队列 / 技能 / 插件 / 熔断器），每一次都抛 NameError，又被外层
    `except Exception` 吞掉，只在日志里留一行 warning。
    于是整个 Harness 管理面静默失效 —— 任务中心看不到 flow/batch 任务、
    插件 REST 路由没挂载、任务完成不推送 —— 而表面上一切正常。

    教训：`except Exception` 会把「编程错误」和「可容忍的运行时异常」一视同仁地
    吞掉。NameError / AttributeError 属于前者，必须让它响。
    """
    from harness import get_harness
    return get_harness()


def _maybe_train_offline(coord) -> dict:
    """真实调用点：按节奏触发 rlhf_calibrator（偏好校准）+ world_model_trainer（经验训练）。

    ⚠ 这个函数曾经**消失过**：c421c6c「删除游戏模式/游戏引擎/游戏AI」那次大段删除，
    把它连同紧邻的游戏处理函数一起删掉了，只留下 server.py 里的调用点。
    调用点外面包着 `except Exception`，于是每次 RL 决策都静默跳过离线训练，
    日志里只有一行 warning —— 离线训练实际上停摆了很久。

    刻意放在文件顶部、远离功能区块：这类「无状态辅助函数」不该紧邻易被整段删除的
    业务代码，否则下次重构还会被误伤。
    """
    return coord.train_offline(force=False)


# 全局主动说话冷却（秒）：所有非用户消息驱动的主动说话（RL 调度 /
# 感知派发 / 环境快照 / 前端召唤）共用此闸门，防止"自言自语"式高频说话
ACTIVE_SPEAK_COOLDOWN = 40.0

# 用户活跃窗口（秒）：用户 1 分钟内有主动对话/回复 → 禁止 AI 主动说话。
# 只有用户超过 1 分钟未主动对话/回复，或有外部触发（游戏事件/游戏结束/
# 用户召唤等）时，AI 才会开始主动对话 —— "不一直废话"原则。
ACTIVE_USER_GUARD = 60.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("server")

BASE_DIR = Path(__file__).parent.resolve()
WEB_DIR = BASE_DIR / "web"
AUDIO_DIR = BASE_DIR / "audio_cache"
MODELS_DIR = BASE_DIR / "models"
BACKGROUNDS_DIR = BASE_DIR / "backgrounds"
# 聊天附件（用户发来的图片/文件）：按用户隔离、按天分目录，入口见 /api/upload
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
SERVER_PORT = 8000
AUDIO_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
BACKGROUNDS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    orch = get_orchestrator()
    await orch.start_runner()
    # 重启/热重载后：把仍在运行的 codex/opencode 独立进程接回任务中心（热重载杀不掉它们，进度不丢）
    try:
        _recovered_n = await _recover_codex_tasks()
        if _recovered_n:
            logger.info(f"[TaskCenter] 已恢复 {_recovered_n} 个 codex/opencode 独立进程任务（热重载后继续执行）")
    except Exception as e:
        logger.warning(f"[TaskCenter] codex 独立进程恢复失败: {e}")
    logger.info("[TaskCenter] 任务中心已启动（DSH/Codex/后台任务统一调度）")
    # 角色卡片按人隔离：老状态里 active_role_card 是全机唯一一份，迁移给部署者身份，
    # 否则主人的记忆空间会从 role_card:<id> 掉回 default，旧对话凭空消失。
    try:
        _admins = [u.get("id") for u in auth_core.list_users()
                   if auth_core.is_admin(u.get("id"))]
    except Exception:
        _admins = []
    try:
        if migrate_legacy_active_card(_admins):
            logger.info("[RoleCard] 老状态已迁移到按人隔离")
    except Exception as e:
        logger.warning(f"[RoleCard] 老状态迁移失败: {e}")
    # 热重载/重启后：自主恢复被中断的角色对话轮（断点续跑，不依赖客户端在场）
    try:
        asyncio.ensure_future(_resume_pending_turns())
        logger.info("[Resume] 对话轮断点恢复已启动（扫描未完成轮次）")
    except Exception as e:
        logger.warning(f"[Resume] 对话轮断点恢复任务启动失败: {e}")
    # 把 FastAPI app 注入 harness，让插件（如 todo_list 的 /api/todo/*）挂载 REST 路由
    try:
        _harness().on_server_start(app)
        logger.info("[Harness] 插件已接入 FastAPI（REST 路由挂载完成）")
    except Exception as e:
        logger.warning(f"[Harness] 插件接入 FastAPI 失败: {e}")
    # 联邦耳朵：同伴来电当场响铃 + 回话（把异步留言变成打电话）。
    # 丢进线程池：它是个常驻循环且要调阻塞的 LLM HTTP，放事件循环里会卡住对话。
    try:
        import peer_watch
        asyncio.get_running_loop().run_in_executor(None, peer_watch.main, [])
        logger.info("[Peer] 联邦耳朵已开（同伴来电即时响铃 + 回话）")
    except Exception as e:
        logger.warning(f"[Peer] 联邦耳朵启动失败: {e}")
    # harness 任务系统完成事件 → WebSocket 实时推送（前端 toast，任务完成即知）
    try:
        async def _harness_task_notifier(report: dict):
            if not manager.active:
                return
            payload = {"type": "harness_task", "task": report}
            for ws in list(manager.active):
                await safe_send_json(ws, payload)
        _harness().tasks.set_notifier(_harness_task_notifier)
    except Exception as e:
        logger.warning(f"[Harness] 任务完成推送注册失败: {e}")
    # 媒体子智能体（media watch）：主智能体派出的「播放 + 盯到结束 + 汇报」子进程
    try:
        mw = _get_media_workers()
        mw.set_report_handler(_deliver_media_worker_report)
        mw.set_event_handler(_media_worker_event_push)
        logger.info("[MediaWorker] 媒体子智能体系统已启动（watch 播放播完自动向主智能体汇报）")
    except Exception as e:
        logger.warning(f"[MediaWorker] 媒体子智能体初始化失败: {e}")
    # 通用子智能体（sub-agents）：任意复杂任务后台并行执行，完成汇报主智能体
    try:
        sa = _get_sub_agents()
        sa.set_report_handler(_deliver_sub_agent_report)
        sa.set_event_handler(_sub_agent_event_push)
        logger.info("[SubAgent] 通用子智能体系统已启动（任意任务可下发、并行分派、完成汇报）")
    except Exception as e:
        logger.warning(f"[SubAgent] 通用子智能体初始化失败: {e}")
    # 定时任务调度器（长任务自动化）：到期任务派发为通用子智能体后台执行，
    # 完成/出错自动记录到 data/scheduled_tasks.json 并沿子智能体汇报链路转述
    try:
        from scheduler import start_scheduler, record_result

        async def _fire_scheduled_job(job: dict) -> None:
            # 定时任务从出生起就与连接解耦：广播代理 + 常驻状态——
            # 页面从未打开/已全部关闭时照样派发、执行、汇报（后台完成，落库）
            state = await _get_resident_state()
            worker = await _get_sub_agents().spawn(
                _BROADCAST_WS, state,
                str(job.get("task") or job.get("name") or ""),
                title=("定时·" + str(job.get("name") or "任务"))[:60],
                extra={"job_id": job.get("id")},
                profile=str(job.get("profile") or ""),
            )
            logger.info("[Scheduler] 已派发定时任务《%s》[%s] → [%s]",
                        job.get("name"), job.get("id"), worker.id)

        asyncio.ensure_future(start_scheduler(_fire_scheduled_job))
        logger.info("[Scheduler] 定时任务调度器已启动")
    except Exception as e:
        logger.warning(f"[Scheduler] 定时任务调度器启动失败: {e}")

    yield

    # ---- shutdown：兜底广播（主路径是 systemd ExecStop 调的 /api/system/restart_notice）----
    # 为什么不指望这里：uvicorn 的关闭顺序是「先对所有连接 transport.close()（TCP 直接断，
    # 连 close 帧都不发），再跑 lifespan shutdown」——到这里 ws 多半已是 DISCONNECTED。
    # 留着是因为 hot_reload 的 os.execv 自替换不经过 systemd（ExecStop 不触发），
    # 那是这里唯一的通知机会。日志里的送达计数就是这段到底有没有用的判据。
    try:
        delivered = 0
        for _ws in list(manager.active):
            if await safe_send_json(_ws, {"type": "server_restart",
                                          "text": "大白正在升级，几秒后自动回来…"}):
                delivered += 1
        logger.info("[Shutdown] 重启通知兜底：活跃连接 %d，送达 %d", len(manager.active), delivered)
    except Exception as e:
        logger.warning(f"[Shutdown] 重启通知广播失败: {e}")

    # ---- shutdown：通知插件清理已挂载的 REST 路由/资源 ----
    try:
        _harness().on_server_stop()
    except Exception as e:
        logger.warning(f"[Harness] 插件停止钩子执行失败: {e}")


app = FastAPI(title="白头凤【BattlePhoenix】", lifespan=lifespan)

# 大白联邦：让散落在树莓派/阿里云/WSL 上的大白互相找到、互相说话。
# 这些路由自己验共享密钥，所以路径在下面的 _AUTH_EXEMPT_PREFIX 里豁免会话中间件——
# 别的实例没有、也不该有本机的会话 cookie。
app.include_router(peer_mesh.router)

# 静态资源缓存分级（首屏/刷新性能关键）。
# 之前对 /static 一律 no-store：模型 24MB、vendor 库、87 个 .ts 模块每次刷新全量重下
# （实测 99 请求 / 27MB / 本地缓存命中 0，二次刷新照样 27MB）。
# 分级策略：
#   - 代码/配置（.ts/.js/.css/.html/.json/.map）：no-cache —— 浏览器每次都带 ETag
#     协商，文件没改返回 304（只有响应头，零 body），改了立刻生效，不必全量重下
#   - 二进制大资产（模型/音频/贴图/字体等）：immutable 长缓存 —— 刷新零传输
_CODE_ASSET_SUFFIXES = (".ts", ".js", ".mjs", ".css", ".html", ".json", ".map")
_IMMUTABLE_PREFIXES = ("/models/", "/backgrounds/", "/audio/")
_IMMUTABLE_SUFFIXES = (".vrm", ".glb", ".gltf", ".fbx", ".bin", ".ktx2", ".hdr", ".exr",
                       ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg",
                       ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".woff", ".woff2", ".ttf")


class StaticCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code != 200:
            return response
        path = request.url.path.lower()
        if path.startswith("/static/"):
            if path.endswith(_CODE_ASSET_SUFFIXES):
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith(_IMMUTABLE_PREFIXES) or path.endswith(_IMMUTABLE_SUFFIXES):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.add_middleware(StaticCacheMiddleware)

# 前端是原生可擦除语法 TS（现代浏览器 2025+ 原生支持），但浏览器对 module 脚本
# 强制执行 MIME 类型校验：.ts 若按 text/plain 返回会被拒绝执行，页面就会永远
# 卡在"连接中… / 加载模型中…"。这里把 .ts 注册为 text/javascript，
# 模块脚本才可通过校验并触发 TS 类型擦除。
import mimetypes as _mimetypes
_mimetypes.add_type("text/javascript", ".ts")

# ---- 前端 TS 源码实时转译 ----
# 前端是 TypeScript 源码直服：入口 /static/app.ts 以 module 方式引用大量 .ts
# 文件，并且代码普遍用 tsc 风格的 '.js' 后缀 import 指向同名 .ts 文件（如
# '../types/app-kernel.js' 实际只有 .ts）。浏览器对 module 脚本强制 JS MIME，
# 且原生 TS 类型擦除并非所有浏览器默认开启（直服 .ts 会报 "Unexpected token
# '{'"）。因此这里启动一个常驻 Node worker（Node>=22.6 自带的
# module.stripTypeScriptTypes 类型剥离，本项目前端已确认为纯可擦除语法），把
# /static 下的 .ts（或缺失 .js 但存在同名 .ts 的请求）实时转译为 ESM JS 下发；
# 磁盘上真实存在的 .js / 其他资源原样放行。按 (路径, mtime, size) 缓存。
# Node 不存在 / 剥离失败时退回原样直服（配合上方 .ts→text/javascript 注册）。
import subprocess as _ts_subprocess
import threading as _ts_threading
import json as _ts_json
import time as _ts_time
import atexit as _ts_atexit

_TS_WORKER_JS = (
    "const readline=require('node:readline');"
    "const {stripTypeScriptTypes}=require('node:module');"
    "const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});"
    "rl.on('line',(line)=>{let req;try{req=JSON.parse(line)}catch(e){"
    "process.stdout.write(JSON.stringify({ok:false,error:'bad json'})+String.fromCharCode(10));return}"
    "try{process.stdout.write(JSON.stringify({ok:true,code:stripTypeScriptTypes(req.code)})+String.fromCharCode(10))}"
    "catch(e){process.stdout.write(JSON.stringify({ok:false,error:String(e&&e.message||e)})+String.fromCharCode(10))}});"
)
_ts_node_worker = None
_ts_node_lock = _ts_threading.Lock()

# 懒启动 + 空闲自杀：Node worker 常驻约 99MB，而它只在浏览器拉前端时才用得上。
# 改为首次请求才启动，空闲超过 _TS_IDLE_KILL_SEC 自动回收（Popen 仍受锁保护）。
_ts_node_last_use = 0.0
_ts_node_broken = False          # node 缺失 / 启动失败：置位后不再反复重试
_TS_IDLE_KILL_SEC = 120.0
_TS_CACHE_MAX = 128              # 前端 .ts 约 69 个，留一倍余量
_ts_cache: dict = {}             # 键=文件路径（只留最新版本），值=(etag, js_bytes)

def _ts_start_worker():
    """启动常驻 worker（幂等）；失败置 _ts_node_broken，后续走原样直服。"""
    global _ts_node_worker, _ts_node_broken
    if _ts_node_worker is not None:
        return
    try:
        _ts_node_worker = _ts_subprocess.Popen(
            # CommonJS 模式（-e 默认 CJS），worker 脚本里用 require()
            ["node", "-e", _TS_WORKER_JS],
            stdin=_ts_subprocess.PIPE, stdout=_ts_subprocess.PIPE,
            stderr=_ts_subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
        )
    except Exception:
        _ts_node_worker = None
        _ts_node_broken = True

def _ts_stop_worker():
    """回收 worker（空闲自杀 / 进程退出时调用），释放约 99MB。"""
    global _ts_node_worker
    w = _ts_node_worker
    _ts_node_worker = None
    if w is None:
        return
    for step in (lambda: w.stdin.close(), lambda: w.terminate(), lambda: w.wait(timeout=3)):
        try:
            step()
        except Exception:
            pass

def _ts_reaper():
    """后台守护线程：空闲超过阈值就把 worker 收掉。"""
    while True:
        _ts_time.sleep(30)
        try:
            if _ts_node_worker is not None and (_ts_time.monotonic() - _ts_node_last_use) > _TS_IDLE_KILL_SEC:
                with _ts_node_lock:
                    _ts_stop_worker()
        except Exception:
            pass

_ts_threading.Thread(target=_ts_reaper, daemon=True, name="ts-reaper").start()
_ts_atexit.register(_ts_stop_worker)

def _ts_transpile(code: str) -> str:
    """同步向 Node worker 发送源码，返回剥离类型后的 JS；失败抛异常。"""
    global _ts_node_last_use
    if _ts_node_broken:
        raise RuntimeError("ts worker unavailable (node missing)")
    with _ts_node_lock:
        _ts_start_worker()          # 懒启动：首个 .ts 请求才真正拉起 node
        w = _ts_node_worker
        if w is None:
            raise RuntimeError("ts worker unavailable")
        _ts_node_last_use = _ts_time.monotonic()
        try:
            w.stdin.write(_ts_json.dumps({"code": code}) + "\n")
            w.stdin.flush()
            line = w.stdout.readline()
        except Exception:
            _ts_stop_worker()       # worker 已死：收掉，下次请求重开
            raise RuntimeError("ts worker broken")
        if not line:
            _ts_stop_worker()
            raise RuntimeError("ts worker closed")
        out = _ts_json.loads(line)
        if not out.get("ok"):
            raise RuntimeError("strip failed: " + str(out.get("error")))
        return out["code"]

class TSTranspileMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if _ts_node_broken or not path.startswith("/static/"):
            return await call_next(request)
        rel = path[len("/static/"):]
        if not (rel.endswith(".ts") or rel.endswith(".js")):
            return await call_next(request)
        fs_path = WEB_DIR / rel
        if rel.endswith(".js"):
            # 磁盘上有真实 .js 就交给静态服务；否则按 tsc 约定找同名 .ts
            if fs_path.is_file():
                return await call_next(request)
            fs_path = fs_path.with_suffix(".ts")
            if not fs_path.is_file():
                return await call_next(request)
        else:
            if not fs_path.is_file():
                return await call_next(request)
        try:
            st = fs_path.stat()
            cache_key = str(fs_path)
            # 协商缓存：内容未变直接 304，省掉整个模块的 body 传输
            etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={
                    "ETag": etag, "Cache-Control": "no-cache"})
            hit = _ts_cache.get(cache_key)
            if hit is not None and hit[0] == etag:
                js = hit[1]
            else:
                code = fs_path.read_text(encoding="utf-8")
                js = _ts_transpile(code).encode("utf-8")
                # 键只按路径保留最新版本（旧实现键含 mtime/size，改一次前端就永久多存一份）
                _ts_cache[cache_key] = (etag, js)
                while len(_ts_cache) > _TS_CACHE_MAX:
                    _ts_cache.pop(next(iter(_ts_cache)), None)
            resp = Response(content=js, media_type="text/javascript; charset=utf-8")
            resp.headers["ETag"] = etag
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        except Exception:
            return await call_next(request)  # 转译失败退回原样

app.add_middleware(TSTranspileMiddleware)

# ---- 文本资源 gzip 压缩（减少传输体积）----
# 实测：/static/style.css 244KB、vendor/three.module.js 1.27MB、65 个 .ts 模块
# 全部裸传，服务端没有任何压缩。这里只压「文本类且体积适中」的响应，明确排除：
#   - text/event-stream：SSE 流式输出，缓冲会破坏实时性
#   - 已压缩格式（.vrm/.glb/.mp3/.png/.woff2…）：再压白耗 CPU 且几乎无收益
#   - 超过 4MB 的响应：避免把 24MB 的 VRM 读进内存
# 必须放在最外层（最后 add_middleware），才能压到 TSTranspileMiddleware 转译后的 JS。
import gzip as _gzip

_GZIP_TYPES = ("text/html", "text/css", "text/plain", "text/javascript",
               "application/javascript", "application/json", "application/xml",
               "text/xml", "image/svg+xml", "application/manifest+json")
_GZIP_MIN_BYTES = 1024
_GZIP_MAX_BYTES = 4 * 1024 * 1024


class GZipTextMiddleware:
    """纯 ASGI 中间件：只对白名单文本类型做 gzip，其余原样透传。"""

    def __init__(self, app, min_bytes=_GZIP_MIN_BYTES, max_bytes=_GZIP_MAX_BYTES):
        self.app = app
        self.min_bytes = min_bytes
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        accept = ""
        for k, v in scope.get("headers", []):
            if k.lower() == b"accept-encoding":
                accept = v.decode("latin-1").lower()
                break
        if "gzip" not in accept:
            return await self.app(scope, receive, send)

        state = {"start": None, "compress": False, "chunks": []}

        async def send_gz(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                state["start"] = message
                raw = {}
                for k, v in message.get("headers", []):
                    raw[k.decode("latin-1").lower()] = v.decode("latin-1")
                ctype = raw.get("content-type", "").lower()
                clen = raw.get("content-length", "")
                size = int(clen) if clen.isdigit() else None
                state["compress"] = bool(
                    message.get("status") == 200
                    and "content-encoding" not in raw
                    and "event-stream" not in ctype
                    and any(ctype.startswith(t) for t in _GZIP_TYPES)
                    and size is not None
                    and self.min_bytes <= size <= self.max_bytes
                )
                if not state["compress"]:
                    await send(message)
                return
            if mtype == "http.response.body":
                if not state["compress"]:
                    await send(message)
                    return
                state["chunks"].append(message.get("body", b"") or b"")
                if message.get("more_body"):
                    return
                data = b"".join(state["chunks"])
                packed = _gzip.compress(data, 6)
                headers = [(k, v) for k, v in state["start"].get("headers", [])
                           if k.decode("latin-1").lower()
                           not in ("content-length", "content-encoding", "vary")]
                if len(packed) < len(data):
                    headers.append((b"content-encoding", b"gzip"))
                    headers.append((b"vary", b"Accept-Encoding"))
                    body = packed
                else:
                    body = data
                headers.append((b"content-length", str(len(body)).encode()))
                await send({"type": "http.response.start",
                            "status": state["start"]["status"],
                            "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, send_gz)


app.add_middleware(GZipTextMiddleware)

# ============================================================
# 账号鉴权：公网访问强制登录，局域网 / 本机直连不受影响
#
# 为什么按请求头而不是源 IP 判“来自公网”：cloudflared 隧道进程是从
# 127.0.0.1 连进来的，源 IP 永远是回环，判不出公网；而隧道一定注入
# CF-Connecting-IP / CF-Ray。局域网直连（192.168.x.x:8000）不带这些头
# → 免登录，原有“一个服务器一套状态”的用法完全不变，本机工具
# （如 code_ops 的 workspace_impl 走 127.0.0.1）也不会被挡。
#
# 中间件顺序：AuthGate 最后 add = 最外层，未登录直接短路，不走 GZip/转译。
# TSTranspileMiddleware 仍是“处理响应体”的最外层，不受影响。
# ============================================================
SESSION_COOKIE = "dabai_sid"
# 全站统一钥匙：社区站（主站 / GitHub 登录）下发的是 .battlephoenix.tech 域的
# bp_sess，用的是同一份 auth_secret 签的 token —— 读它，主站登录过的人进
# dabai 子域就不用再登一次。dabai_sid 是统一之前的旧 cookie，留着只为
# 不让老设备（含已装 App）掉线。
SHARED_COOKIE = "bp_sess"
SHARED_COOKIE_DOMAIN = ".battlephoenix.tech"
_PUBLIC_HEADERS = ("cf-connecting-ip", "cf-ray")
# 注意：不用 x-forwarded-for 做判据——nginx 前置（8000 TLS → 8001）会对所有请求
# 注入它，拿它当信号会把局域网访问也当成公网。CF 隧道头才是精准信号。
_AUTH_EXEMPT = {
    "/login", "/api/auth/login", "/api/auth/register", "/api/auth/me",
    "/api/auth/status", "/api/auth/email/send",
    "/favicon.ico", "/manifest.webmanifest", "/sw.js", "/setup", "/dabai-ca.crt",
    # App 启动就要查它，不能等登录
    "/api/frontend-build",
}
# /api/peer/ 走自己的密钥认证（peer_mesh），不认会话 cookie：联邦里的每个实例
# 都是独立进程，互相之间不存在登录关系。
_AUTH_EXEMPT_PREFIX = ("/static/", "/icons/", "/api/peer/")

# 系统级 API：普通用户一律 403。这些接口要么泄漏全局信息（任务中心能看到
# 管理员的任务与日志），要么直接改服务器状态（工作区/执行器/LLM 配置）。
# 与工具层的 sandbox 拒绝清单同一判据：超出「一个用户在自己的沙箱里玩」。
_ADMIN_API_PREFIX = (
    "/api/tasks", "/api/harness", "/api/workspace", "/api/workspaces",
    "/api/codex", "/api/llm", "/api/config", "/api/bridge", "/api/longrun",
    "/api/mixamo", "/api/agent", "/api/info", "/api/tools", "/api/vision",
    # 视频源（列源 + 启停 + 自定义源增删改）：属于部署配置层——自定义源的搜索 URL
    # 写进去就是一条任意取数通道，跟 /api/config 同级，不能交给普通用户看或改。
    "/api/video_hub/api/sources", "/api/video_hub/api/platforms",
)
# 资源类接口：普通用户可以看（首屏要列角色卡片/模型），但不能改。
_ADMIN_WRITE_PREFIX = (
    "/api/models", "/api/model/", "/api/background", "/api/character_cards",
    "/api/tts", "/api/stt",
)
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


# 普通用户也能写的例外：切换角色卡片（apply）只改「当前生效的角色」，不碰卡片内容本身。
# 卡片的增/改/删仍属部署层——写的是全机唯一一套 settings.json + tts_config.json。
_CARD_APPLY_PATH = re.compile(r"^/api/character_cards/[^/]+/apply$")


def _admin_api_blocked(path: str, method: str) -> bool:
    if path.startswith(_ADMIN_API_PREFIX):
        return True
    if method.upper() not in _WRITE_METHODS:
        return False
    if _CARD_APPLY_PATH.match(path):
        return False
    return path.startswith(_ADMIN_WRITE_PREFIX)


def _is_public_request(request: Optional[Request]) -> bool:
    if request is None:
        return False
    for h in _PUBLIC_HEADERS:
        if request.headers.get(h):
            return True
    return False


def _client_is_https(request: Optional[Request]) -> bool:
    """客户端到边缘用的是不是 HTTPS。

    不能拿 request.url.scheme 当判据：cloudflared 用 TLS 回源 nginx:8000，nginx 又
    传 X-Forwarded-Proto: https，源站看到的永远是 https —— 但用户可能是明文 HTTP
    进来的。给 HTTP 用户下发带 Secure 的 cookie，浏览器直接丢弃，登录后每个请求
    都不带 cookie、被中间件弹回登录页，表现成「登录不进去」的死循环（实测 nginx
    日志：POST /api/auth/register 200 紧跟 GET / 302，Referer 是 http:// 开头的
    登录页）。CF 的 cf-visitor 才是客户端侧协议。
    """
    if request is None:
        return False
    v = request.headers.get("cf-visitor")
    if v:
        try:
            scheme = json.loads(v).get("scheme")
            if scheme in ("http", "https"):
                return scheme == "https"
        except (ValueError, AttributeError):
            pass
    return request.url.scheme == "https"


def _session_uid(request: Optional[Request]) -> str:
    """请求携带的有效会话用户 ID；没有（局域网直连/未登录）返回空串。

    统一钥匙优先：主域下发的 bp_sess 认不出来才回落到旧的 dabai_sid。
    """
    if request is None:
        return ""
    try:
        cookies = request.cookies
    except Exception:
        return ""
    for name in (SHARED_COOKIE, SESSION_COOKIE):
        uid = auth_core.verify_token(cookies.get(name, ""))
        if uid:
            return uid
    return ""


def _uid_of(request: Optional[Request] = None, payload: Optional[dict] = None) -> str:
    """当前请求的用户身份：公网会话用户优先，否则回落单系统统一身份。

    公网路径上绝不信前端传的 user_id——那是个越权入口（改个 id 就能读别人的会话）。
    局域网直连时保持旧行为，兼容既有前端。
    """
    uid = _session_uid(request)
    if uid:
        return uid
    if payload and not _is_public_request(request):
        return payload.get("user_id") or _unified_user_id()
    return _unified_user_id()


def _card_uid(request: Optional[Request], payload: Optional[dict] = None) -> str:
    """角色卡片的用户身份：公网只认会话 cookie，局域网认前端 uid（与 WS 侧同一把钥匙）。

    局域网直连没有 cookie，而 WS 侧的用户身份来自前端 localStorage.dabai.userId——
    HTTP 侧不认它就会写成 default，与 agent 实际用的 uid 对不上，切卡看着成功却不生效。
    """
    uid = _session_uid(request)
    if uid:
        return uid
    if request is not None and not _is_public_request(request):
        raw = str((payload or {}).get("user_id") or "")
        if not raw:
            try:
                raw = str(request.query_params.get("user_id") or "")
            except Exception:
                raw = ""
        if raw.strip():
            return raw.strip()
    return _unified_user_id()


def _store_uid(request: Optional[Request] = None) -> str:
    """个性化数据（视频历史 / 视频收藏 / 音乐歌单）的存储身份。

    管理员回落空串 → 与 AI 工具调用（agent 侧不携带用户身份）共用项目根目录的
    全局文件，也就是部署者本人的一份数据：既有数据零迁移，且「大白帮我收藏」
    不会落到别人的目录里。
    普通用户用自己的 uid → data/users/<uid>/，互相看不见。
    """
    uid = _session_uid(request)
    if not uid or auth_core.is_admin(uid):
        return ""
    return uid


def _auth_exempt(path: str) -> bool:
    return path in _AUTH_EXEMPT or path.startswith(_AUTH_EXEMPT_PREFIX)


def _set_session_cookie(resp: Response, uid: str, secure: bool = True) -> None:
    """一个 token 下发两份 cookie：主域共享的 bp_sess + 本子域的 dabai_sid。

    两份内容一样，只是作用域不同 —— 前者让主站 / GitHub 登录一次全站通行，
    后者兼容统一之前就装在手机上的 App。
    """
    tok = auth_core.make_token(uid)
    resp.set_cookie(
        SESSION_COOKIE, tok,
        max_age=auth_core.TOKEN_TTL, httponly=True, samesite="lax",
        secure=secure, path="/",
    )
    resp.set_cookie(
        SHARED_COOKIE, tok, domain=SHARED_COOKIE_DOMAIN,
        max_age=auth_core.TOKEN_TTL, httponly=True, samesite="lax",
        secure=secure, path="/",
    )


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _auth_exempt(path) or not _is_public_request(request):
            return await call_next(request)
        uid = _session_uid(request)
        if uid:
            request.state.user_id = uid
            if not auth_core.is_admin(uid) and _admin_api_blocked(path, request.method):
                return JSONResponse(
                    {"error": "该接口仅管理员可用", "code": "forbidden"}, status_code=403)
            return await call_next(request)
        if path.startswith("/api/") or path.startswith("/ws"):
            return JSONResponse({"error": "未登录", "code": "unauthorized"}, status_code=401)
        # OAuth 失败时主站把 ?oauth=xxx 带回来：转发给登录页显示原因，
        # 否则用户只看到又弹一次登录框，分不清是授权被拒还是没配置。
        q = request.url.query
        return RedirectResponse("/login" + (("?" + q) if q else ""), status_code=302)


app.add_middleware(AuthGateMiddleware)


@app.get("/login")
async def login_page():
    """登录/注册页（自包含 HTML，未登录也能打开）。"""
    page = WEB_DIR / "login.html"
    if not page.is_file():
        return JSONResponse({"error": "login.html 缺失"}, status_code=500)
    return FileResponse(page)


# ---- 前端版本指纹（给 Android 壳用）----
# App 把前端预打包进 APK 换取零请求首屏，代价是「改了 web/ 但手机还是旧界面」。
# 这里实时算源码指纹，App 启动后异步比对，对不上就整体降级走网络。
# 规则必须与 apps/dabai-android/tools/build_web_assets.py 的 web_fingerprint() 完全一致：
# 扩展名白名单、跳过 vendor/anim/node_modules、按路径排序、hash(path:mtime_ns:size)。
_FP_EXTS = {".ts", ".js", ".mjs", ".css", ".html", ".webmanifest"}
_FP_SKIP_DIRS = {"vendor", "anim", "node_modules"}


def _web_source_fingerprint() -> str:
    h = hashlib.sha256()
    items = []
    for fp in WEB_DIR.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in _FP_EXTS:
            continue
        rel = fp.relative_to(WEB_DIR)
        if rel.parts and rel.parts[0] in _FP_SKIP_DIRS:
            continue
        items.append(rel)
    for rel in sorted(items, key=str):
        try:
            st = (WEB_DIR / rel).stat()
        except OSError:
            continue
        h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()[:16]


@app.get("/api/frontend-build")
async def frontend_build():
    return {"fingerprint": _web_source_fingerprint()}


def _client_device(request: Request) -> str:
    return (request.headers.get("user-agent", "") or "")[:64]


def _client_ip(request: Request) -> str:
    """注册限速的键。只对公网请求生效：局域网直连本来就是系统身份，不该被限。

    CF 隧道一定注入 cf-connecting-ip（源 IP 恒为回环，判不出公网）；x-real-ip 是
    nginx 前置注入的真实客户端。两者都可能被伪造，所以还有全局速率兜底。
    """
    if not _is_public_request(request):
        return ""
    for h in ("cf-connecting-ip", "x-real-ip"):
        v = (request.headers.get(h) or "").strip()
        if v:
            return v[:64]
    return (request.client.host if request.client else "") or "unknown"


@app.get("/api/auth/status")
async def auth_status():
    """登录页启动时问一句：要不要显示注册 tab。免登录可访问，不泄用户名单。"""
    return {"ok": True, "registration_open": auth_core.registration_open(),
            "email_ready": email_verify.smtp_ready(),
            "first_run": auth_core.user_count() == 0}


@app.post("/api/auth/email/send")
def auth_email_send(payload: dict, request: Request):
    """发注册验证码。免登录可访问 —— 所以三重限速是必须的，否则这个端点就是
    一台免费发信机（拿它轰炸别人邮箱，域名先被拉黑）。

    用 def 不用 async：SMTP 握手是阻塞的（树莓派上 1~3 秒），FastAPI 会把 def
    端点丢线程池，不会卡住事件循环。
    """
    email = str(payload.get("email", "") or "").strip().lower()
    if auth_core.find_by_name(email):
        return JSONResponse({"error": "这个邮箱已经注册过了，直接登录就行",
                             "code": "name_taken"}, status_code=400)
    try:
        out = email_verify.send_code(email, _client_ip(request) or "lan")
    except email_verify.MailError as e:
        status = 429 if e.code in ("rate_limited", "cooldown") else 400
        return JSONResponse({"error": e.msg, "code": e.code}, status_code=status)
    return out


@app.post("/api/auth/register")
async def auth_register(payload: dict, request: Request):
    """自助注册。只认两条路：邮箱（先收验证码）或 GitHub（走 OAuth，不经这里）。

    验证码在这一层换成票据、票据再进 auth_core：前端拿不到票据，也就不存在
    「捡一个票据去注册别人邮箱」的路径。预检排在换票据之前 —— 码是一次性的，
    让「密码太短」这种错白白烧掉一个码，用户得再等 60 秒。

    ip 兜一个 "lan"：局域网直连原本判不出公网 IP，但注册不能因此免闸门 ——
    同一个 WiFi 下的别人也是别人。
    """
    name = str(payload.get("name", "") or "").strip()
    pwd = payload.get("pwd", "")
    if len(str(pwd or "")) < 6:
        return JSONResponse({"error": "密码至少 6 位", "code": "bad_pwd"}, status_code=400)
    if "@" not in name:
        return JSONResponse({"error": "请用邮箱注册（收验证码），或直接用 GitHub 登录",
                             "code": "email_required"}, status_code=400)
    try:
        ticket = email_verify.check_code(name, payload.get("email_code", ""))
    except email_verify.MailError as e:
        return JSONResponse({"error": e.msg, "code": e.code}, status_code=400)
    try:
        user = auth_core.register(name, pwd, _client_device(request),
                                  ip=_client_ip(request) or "lan", email_ticket=ticket)
    except auth_core.AuthError as e:
        status = 400
        if e.code == "rate_limited":
            status = 429
        elif e.code == "registration_closed":
            status = 403
        return JSONResponse({"error": e.msg, "code": e.code}, status_code=status)
    resp = JSONResponse({"ok": True, "user": user})
    _set_session_cookie(resp, user["id"], secure=_client_is_https(request))
    logger.info(f"[Auth] 注册新用户 {user['name']} ({user['id']})")
    return resp


@app.post("/api/auth/login")
async def auth_login(payload: dict, request: Request):
    try:
        user = auth_core.login(payload.get("name", ""), payload.get("pwd", ""),
                               _client_device(request))
    except auth_core.AuthError as e:
        status = 429 if e.code == "rate_limited" else 401
        return JSONResponse({"error": e.msg, "code": e.code}, status_code=status)
    resp = JSONResponse({"ok": True, "user": user})
    _set_session_cookie(resp, user["id"], secure=_client_is_https(request))
    logger.info(f"[Auth] 登录 {user['name']} ({user['id']})")
    return resp


@app.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(SHARED_COOKIE, path="/", domain=SHARED_COOKIE_DOMAIN)
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    uid = _session_uid(request)
    if not uid:
        return JSONResponse({"error": "未登录", "code": "unauthorized"}, status_code=401)
    return {"ok": True, "user": auth_core.get_user(uid), "public": _is_public_request(request)}


def _is_admin_request(request: Request) -> bool:
    """当前请求是否具备管理员权限。

    局域网直连（不带 CF 头、无 cookie）本来就是系统身份，与工具层
    sandbox.actor_for 的口径一致——两处判据必须同源，否则会出现
    「工具能干、管理接口不能干」的怪异状态。
    """
    uid = _session_uid(request)
    if not uid:
        return not _is_public_request(request)
    return auth_core.is_admin(uid)


@app.get("/api/auth/users")
async def auth_users(request: Request):
    """用户清单（含角色），仅管理员。"""
    if not _is_admin_request(request):
        return JSONResponse({"error": "仅管理员可查看", "code": "forbidden"}, status_code=403)
    return {"ok": True, "users": auth_core.list_users()}


@app.post("/api/auth/role")
async def auth_set_role(payload: dict, request: Request):
    """改用户角色（仅管理员）。body: {uid, role: admin|user}"""
    if not _is_admin_request(request):
        return JSONResponse({"error": "仅管理员可操作", "code": "forbidden"}, status_code=403)
    try:
        user = auth_core.set_role(str(payload.get("uid") or ""), str(payload.get("role") or ""))
    except auth_core.AuthError as e:
        return JSONResponse({"error": e.msg, "code": e.code}, status_code=400)
    logger.info(f"[Auth] {_session_uid(request) or 'local'} 把 {user['name']} 角色改为 {user['role']}")
    return {"ok": True, "user": user}


# 托管生成的音频文件
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")
# 托管上传的 3D 模型
app.mount("/models", StaticFiles(directory=str(MODELS_DIR)), name="models")
# 聊天附件（用户上传的图片/文件）：不豁免鉴权，公网未登录拿不到
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
# 托管上传的 3D 背景模型
app.mount("/backgrounds", StaticFiles(directory=str(BACKGROUNDS_DIR)), name="backgrounds")
# 托管前端静态资源 (css/js)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
# 托管动作库文件 (anim/)
app.mount("/anim", StaticFiles(directory=str(WEB_DIR / "anim")), name="anim")
# 托管 AI 画图技能生成的图片（/generated/<文件名>）
GENERATED_DIR = BASE_DIR / "web" / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
app.mount("/generated", StaticFiles(directory=str(GENERATED_DIR)), name="generated")

# 托管 MoneyPrinterTurbo 生成的视频（/mpt/<任务id>/<文件名>）
# 让聊天里发的 /mpt/... 路径也能直接渲染播放（不限于 /generated）
MPT_DIR = Path("D:/AI/MoneyPrinterTurbo/storage/tasks")
if MPT_DIR.is_dir():
    app.mount("/mpt", StaticFiles(directory=str(MPT_DIR)), name="mpt")

# 托管用户下载目录的视频（/downloads/<文件名>）
# 让聊天里发的 /downloads/... 路径也能直接渲染播放
DOWNLOADS_DIR = Path("C:/Users/wangxingfeng/Downloads")
if DOWNLOADS_DIR.is_dir():
    app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")


# ============================================================
# 通用本地媒体路由：/media?path=<绝对路径>
# 让聊天里发的任意本地绝对路径（C:\xxx\a.mp4、D:\xxx\b.mp4）也能渲染播放。
# 安全：只允许读媒体扩展名文件，且必须存在，否则 404。
# ============================================================
MEDIA_EXT_WHITELIST = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp",
                       ".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv", ".flv", ".ts", ".avi"}

@app.get("/media")
def media_file(path: str = Query(..., description="本地媒体文件绝对路径")):
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in MEDIA_EXT_WHITELIST:
        raise HTTPException(status_code=404, detail="media not found or unsupported")
    return FileResponse(str(p), media_type=None, filename=p.name)
# 允许的 3D 模型扩展名
ALLOWED_MODEL_EXTS = {".glb", ".gltf", ".vrm"}
MAX_MODEL_SIZE = 80 * 1024 * 1024  # 80MB


def get_lan_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _is_global_ipv6(addr: str) -> bool:
    """判断 IPv6 地址是否为全局单播地址，排除 ::、::1、fe80:: 等非公开地址。"""
    import ipaddress
    try:
        ip = ipaddress.IPv6Address(addr.split("%")[0])
        return ip.is_global
    except (ValueError, ipaddress.AddressValueError):
        return False


def get_global_ipv6() -> str:
    """获取本机全局 IPv6 地址（用于显示，失败返回空串）。

    优先用 UDP connect 让 OS 选出出口地址；若不可用，回退枚举所有网卡地址。
    """
    # 方案1: UDP connect 让系统选出出口 IPv6
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_global_ipv6(ip):
            return ip
    except Exception:
        pass
    # 方案2: 枚举本机所有 IPv6 地址，挑一个全局单播
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6,
                                   socket.SOCK_STREAM)
        for fam, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if _is_global_ipv6(ip):
                return ip
    except Exception:
        pass
    # 方案3: 枚举网卡（Windows netifaces 兼容兜底）
    try:
        import netifaces
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET6, [])
            for a in addrs:
                ip = a.get("addr", "").split("%")[0]  # 去掉 scope id
                if _is_global_ipv6(ip):
                    return ip
    except Exception:
        pass
    return ""


def load_config():
    with open(BASE_DIR / "settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- TTS（多引擎） ----------
DEFAULT_VOICE = "zh-CN-YunxiNeural"  # 男声云稀，亲切自然

# 句子结束符（用于流式分句 TTS）
SENTENCE_END = re.compile(r"[。！？!?\n…；;~]")


def _tts_plain_text(text: str) -> str:
    """把 Markdown 转成 TTS 可自然朗读的纯文本（先渲染再读）。

    朗读版「渲染」：代码块/链接/图片/表格/行内标记等一律转成口语化文本，
    避免语音把 **、|、反引号、URL 等原始符号念出来。
    """
    s = str(text or "")
    # 代码块：整段替换为「代码」（不朗读代码内容）
    s = re.sub(r"```[\w+#-]*[ \t]*\n[\s\S]*?```", "（代码）", s)
    s = re.sub(r"```[\w+#-]*[ \t]*```", "", s)
    # 图片 ![alt](url) → alt（无 alt 则删除）
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: (m.group(1) or "").strip(), s)
    # 链接 [文字](url) → 文字
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    # 行内代码 `x` → x
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    # 加粗 / 斜体 / 删除线
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"(^|[^*])\*([^*\n]+)\*(?!\*)", r"\1\2", s)
    s = re.sub(r"(^|[^_])_([^_\n]+)_(?!_)", r"\1\2", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    # 表格：跳过分隔线行，单元格用逗号连接成一句话
    out_lines = []
    for ln in s.split("\n"):
        t = ln.strip()
        if re.fullmatch(r"\|?[\s:|-]+\|?", t):
            continue
        if t.startswith("|") and t.endswith("|"):
            cells = [c.strip() for c in t.strip("|").split("|")]
            out_lines.append("，".join(c for c in cells if c))
            continue
        out_lines.append(ln)
    s = "\n".join(out_lines)
    # 标题 / 引用 / 列表 / 分隔线标记
    s = re.sub(r"^\s*#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*(?:[-*+]\s+|\d+\.\s+)", "", s, flags=re.M)
    s = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", s, flags=re.M)
    # 普通网址直接删除（避免朗读整串 URL；图片/视频链接已被上面规则处理）
    s = re.sub(r"https?://[^\s，。！？!?\n]+", "", s)
    # 清理残留符号与多余空白
    s = re.sub(r"[*_`#>\-\[\]\(\)|!~]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


# 全局 TTS 配置（运行时可被前端 /api/tts/config 修改，持久化到 tts_config.json）
TTS_CONFIG_FILE = BASE_DIR / "tts_config.json"
DEFAULT_TTS_CONFIG = {
    "engine": "edge_tts",                       # "edge_tts" | "gpt_sovits" | "api"
    "edge_voice": DEFAULT_VOICE,                # edge_tts 音色 ShortName
    "edge_rate": "+8%",                         # 语速
    "gptsovits_url": "http://127.0.0.1:7860/",  # GPT-SoVITS API 服务地址
    "gptsovits_ref_audio": "",                  # 参考音频绝对路径
    "gptsovits_character": "星见雅",
    # 供应商 API TTS（OpenAI 兼容 /audio/speech，如硅基流动 CosyVoice）
    "api_url": "",                              # 端点；空=不启用
    "api_key": "",                              # 专用密钥；空=沿用供应商 API Key
    "api_model": "",                            # 模型名，如 FunAudioLLM/CosyVoice2-0.5B
    "api_voice": "",                            # 音色 id
}
def _load_tts_config():
    """从文件加载 TTS 配置，不存在则用默认值。"""
    if TTS_CONFIG_FILE.exists():
        try:
            with open(TTS_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg = {**DEFAULT_TTS_CONFIG, **saved}
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_TTS_CONFIG)

def _save_tts_config(cfg: dict):
    """保存 TTS 配置到文件。"""
    try:
        with open(TTS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 TTS 配置失败: {e}")

tts_config = _load_tts_config()

# 缓存 edge_tts 中文音色列表
_edge_voices_cache: list | None = None


def cleanup_old_audio():
    """清理旧音频文件，最多保留 5 个（兜底，正常流程已即时销毁）"""
    try:
        files = sorted(AUDIO_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
        for old in files[:-5]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _start_periodic_cleanup():
    """后台守护线程：每 30 分钟清理一次 audio_cache，防止磁盘/缓存无限增长"""
    def _loop():
        while True:
            time.sleep(1800)
            try:
                cleanup_old_audio()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name="audio-cache-cleanup").start()


_start_periodic_cleanup()


async def get_edge_voices() -> list:
    """获取 edge_tts 中文音色列表（带缓存）"""
    global _edge_voices_cache
    if _edge_voices_cache is None:
        voices = await edge_tts.list_voices()
        _edge_voices_cache = [
            {
                "name": v["ShortName"],
                "locale": v["Locale"],
                "gender": v.get("Gender", ""),
                "friendly": f"{v.get('Gender','')}声 · {v['ShortName'].split('-')[-1].replace('Neural','')}"
            }
            for v in voices if v["Locale"].startswith("zh-")
        ]
    return _edge_voices_cache


async def generate_tts_edge(text: str, voice: str, rate: str):
    """edge_tts 在线合成 mp3，返回 (audio_bytes, mime_type) 或 None。

    音频数据直接返回，不保留持久文件。"""
    clean = re.sub(r"[*_`#>\-\[\]\(\)]", "", text).strip()
    if not clean:
        return None
    # 空音色兜底（卡片应用/历史配置可能写入空串，导致 Invalid voice）
    voice = (voice or "").strip() or DEFAULT_VOICE
    rate = (rate or "").strip() or "+8%"
    # 使用临时文件生成，读完后立即删除
    audio_id = f"{uuid.uuid4().hex}.mp3"
    audio_path = AUDIO_DIR / audio_id
    try:
        communicate = edge_tts.Communicate(clean, voice, rate=rate, volume="+0%")
        await communicate.save(str(audio_path))
        data = audio_path.read_bytes()
        return (data, "audio/mpeg")
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass


def resolve_ref_audio_by_character(character: str) -> str:
    """按角色名从 gpt_sovits.json 读参考音频路径（对齐 voice.py 的 index_tts 逻辑）。

    查找位置（按顺序）：
      1. tools/gpt_sovits/gpt_sovits.json  （voice.py 原路径）
      2. gpt_sovits.json                   （项目根目录）
    json 格式：{ "角色名": { "ref_audio_path": "绝对路径" } }
    """
    if not character:
        return ""
    for rel in ("tools/gpt_sovits/gpt_sovits.json", "gpt_sovits.json"):
        p = BASE_DIR / rel
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get(character, {}).get("ref_audio_path", "")
            except Exception as e:
                print(f"[GPT-SoVITS] 读取 {rel} 失败: {e}")
    return ""


def generate_tts_gptsovits(text: str, ref_audio: str, url: str, character: str = ""):
    """调用本地 GPT-SoVITS（gradio 7860）合成 wav，返回 (audio_bytes, mime_type) 或 None。

    参考音频优先级：显式传入 ref_audio > 按角色名查 gpt_sovits.json > None（服务端默认）。
    音频数据直接返回，不保留持久文件。
    """
    from gradio_client import Client as GradioClient, handle_file
    # 没显式传 ref_audio 时，按角色名从配置文件查（对齐 voice.py）
    if not ref_audio or not os.path.exists(ref_audio):
        ref_audio = resolve_ref_audio_by_character(character)
    client = GradioClient(url)
    prompt = handle_file(ref_audio) if (ref_audio and os.path.exists(ref_audio)) else None
    result = client.predict(
        prompt=prompt,
        text=text,
        infer_mode="批次推理",
        max_text_tokens_per_sentence=120,
        sentences_bucket_max_size=4,
        param_5=True, param_6=0.8, param_7=30, param_8=1,
        param_9=0, param_10=3, param_11=10, param_12=600,
        api_name="/gen_single",
    )
    # result 通常是 [text_output, audio_path, ...]
    src = result[1] if isinstance(result, (list, tuple)) else result.get("value")
    if not src or not os.path.exists(src):
        raise RuntimeError("GPT-SoVITS 未返回有效音频")
    # 直接读二进制，不复制到 audio_cache
    with open(src, "rb") as f:
        data = f.read()
    return (data, "audio/wav")


async def generate_tts_api(text: str, api_url: str, api_key: str,
                             api_model: str = "", api_voice: str = ""):
    """调用供应商的 OpenAI 兼容 TTS（/audio/speech），返回 (audio_bytes, mime_type) 或 None。

    api_url 形如 https://api.siliconflow.cn/v1/audio/speech，SDK 按 base_url 自动拼端点。
    """
    clean = text.translate(dict.fromkeys(map(ord, "*_`#>[]()"), None)).strip()
    if not clean:
        return None
    if not (api_url or "").strip() or not (api_key or "").strip():
        raise RuntimeError("API TTS 未配置完整（缺 api_url / api_key）")
    base = (api_url or "").strip().rstrip("/")
    if base.endswith("/audio/speech"):
        base = base[:-len("/audio/speech")]
    from openai import AsyncOpenAI
    import httpx as _httpx
    # 按全局 llm_proxy 决定走不走 fq 代理（本地地址自动直连）
    proxy = _cloud_proxy_for(base)
    client = AsyncOpenAI(
        api_key=api_key, base_url=base,
        http_client=_httpx.AsyncClient(
            proxy=proxy, trust_env=False,
            timeout=_httpx.Timeout(120.0, connect=30.0),
        ),
    )
    model = (api_model or "").strip() or "FunAudioLLM/CosyVoice2-0.5B"
    voice = (api_voice or "").strip() or "default"
    resp = await client.audio.speech.create(model=model, voice=voice, input=clean)
    data = getattr(resp, "content", None)
    if data is None and hasattr(resp, "read"):
        data = resp.read()
    if not data:
        raise RuntimeError("API TTS 返回空音频")
    if isinstance(data, str):
        data = data.encode("utf-8")
    return (bytes(data), "audio/mpeg")


def _effective_tts_config(uid: str = "") -> dict:
    """该用户生效的 TTS 配置：卡片 tts 覆盖全局（空值不覆盖，防 Invalid voice）。"""
    cfg = dict(tts_config)
    if not uid:
        return cfg
    try:
        card = role_card_of(uid)
    except Exception:
        card = None
    if not card:
        return cfg
    tts = card.get("tts") or {}
    for k in ("engine", "edge_rate", "gptsovits_url", "gptsovits_ref_audio",
              "gptsovits_character", "api_url", "api_key", "api_model", "api_voice"):
        v = tts.get(k)
        if isinstance(v, str):
            if v.strip():
                cfg[k] = v
        elif v:
            cfg[k] = v
    if (tts.get("edge_voice") or "").strip():
        cfg["edge_voice"] = tts["edge_voice"].strip()
    return cfg


async def generate_tts(text: str, uid: str = ""):
    """根据当前引擎生成 TTS，返回 (audio_bytes, mime_type) 或 None，失败自动回退 edge_tts。

    引擎来源：edge_tts（免费在线）| gpt_sovits（本地）| api（当前供应商的 API TTS）。
    uid 非空时用该用户卡片的音色；空 uid（无头调用 / 从未切卡）用全局配置。
    """
    cfg = _effective_tts_config(uid)
    engine = cfg.get("engine", "edge_tts")
    try:
        if engine == "gpt_sovits":
            return await asyncio.to_thread(
                generate_tts_gptsovits,
                text,
                cfg["gptsovits_ref_audio"],
                cfg["gptsovits_url"],
                cfg.get("gptsovits_character", ""),
            )
        if engine == "api":
            return await generate_tts_api(
                text,
                cfg.get("api_url", ""),
                cfg.get("api_key", ""),
                cfg.get("api_model", ""),
                cfg.get("api_voice", ""),
            )
        return await generate_tts_edge(text, cfg["edge_voice"], cfg["edge_rate"])
    except Exception as e:
        print(f"[TTS] {engine} 失败，回退 edge_tts: {e}")
        try:
            fallback_voice = (cfg.get("edge_voice") or "").strip() or DEFAULT_VOICE
            fallback_rate = (cfg.get("edge_rate") or "").strip() or "+8%"
            return await generate_tts_edge(text, fallback_voice, fallback_rate)
        except Exception as e2:
            print(f"[TTS] edge_tts 也失败: {e2}")
            return None
# ---------- STT ----------
def _is_valid_audio(path: str) -> bool:
    """快速检查文件是否可能是音频（基于大小和常见魔数）。

    只过滤明显非音频的文件（太小/空文件）。移动端 MediaRecorder
    可能产生非标准封装格式，即使魔数不匹配也交给 ffmpeg 自行判断。
    """
    try:
        file_size = os.path.getsize(path)
        if file_size < 32:
            print(f"[ffmpeg] 跳过空文件: {file_size} 字节")
            return False
        return True  # 放行所有 > 32 字节的文件，ffmpeg 会自动检测格式
    except Exception:
        return False


def _is_16k_mono_pcm_wav(path: str) -> bool:
    """判断文件是否已是 16k 单声道 16bit PCM WAV（ffmpeg 缺失时可直通给 STT）。"""
    try:
        with wave.open(path, "rb") as w:
            return (w.getframerate() == 16000 and w.getnchannels() == 1
                    and w.getsampwidth() == 2 and w.getcomptype() == "NONE")
    except Exception:
        return False


def _mime_to_ext(mime_type: str) -> str:
    """将 MIME 类型映射为文件扩展名。

    容器格式优先于编解码器关键词（如 audio/webm;codecs=opus 应识别为 webm，而非 opus→ogg）。
    """
    mime_lower = (mime_type or "").lower()
    # 容器格式
    if "webm" in mime_lower:
        return ".webm"
    if "ogg" in mime_lower:
        return ".ogg"
    if "mp4" in mime_lower or "m4a" in mime_lower or "aac" in mime_lower:
        return ".m4a"
    if "wav" in mime_lower:
        return ".wav"
    # 纯编解码器关键词兜底（如只有 "opus" 而无容器信息）
    if "opus" in mime_lower:
        return ".ogg"
    return ".webm"


def _detect_ext_from_bytes(data: bytes) -> str:
    """根据文件魔数检测实际音频格式，修正 MIME 类型可能不准确的问题。"""
    if len(data) < 12:
        return ".webm"
    # WebM / Matroska: 0x1A 0x45 0xDF 0xA3
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    # Ogg: "OggS"
    if data[:4] == b"OggS":
        return ".ogg"
    # RIFF WAVE: "RIFF" ... "WAVE"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    # MP4/M4A: ftyp 在偏移 4
    if data[4:8] == b"ftyp":
        return ".m4a"
    # MP3: 同步字 0xFFE/0xFFF
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    # 裸 Opus 数据包检测：首字节为合法 Opus TOC 字节
    # TOC bits 7-5 (config): 000=SILK, 001=CELT, 010=Hybrid (1帧模式)
    if len(data) >= 1:
        config = data[0] & 0xE0
        if config in (0x00, 0x20, 0x40):
            return ".opus"
    # 默认回退
    return ".webm"


def convert_to_wav(input_path: str, output_path: str, noise_reduction: bool = False) -> bool:
    """用 ffmpeg 把任意音频格式转成 16k 单声道 wav。

    noise_reduction=False（默认快速路径）：高通/低通 + 响度归一化 + 保守静音裁剪，
    干净环境下速度快、送入 STT 的音频更短 → 识别更快；
    noise_reduction=True（降噪路径）：附加 afftdn 频域降噪，
    用于首轮识别失败（环境噪声过大）时重试，提升准确率。

    滤波链：
    - highpass=f=80      去掉低频嗡嗡（电源/空调/手震）
    - lowpass=f=8000     去掉高频无用信号（16k 采样上限 8k）
    - silenceremove      保守裁掉录音开头的确认窗口与结尾的自适应静音尾巴
                         （阈值 -55dB 仅去除真静音，绝不误伤轻声语音）
    - afftdn=nr=10       频域降噪，压制稳态背景噪声（仅降噪路径）
    - dynaudnorm         动态响度归一化，放大安静部分，小声说话也能被识别
    - aresample=soxr     高质量重采样到 16k（浏览器 48k 录音降采样更干净，
                         减少混叠噪声 → 提升识别准确率）
    """
    # 预处理：检查文件是否是常见音频格式
    if not _is_valid_audio(input_path):
        print(f"[ffmpeg] 跳过无效音频文件: {os.path.getsize(input_path)} 字节")
        return False

    # ffmpeg 缺失时不能让整条 STT 链路哑掉：前端 PCM 路径传来的本就是
    # 16k 单声道 16bit WAV，可直接交给识别接口
    if not shutil.which("ffmpeg"):
        if _is_16k_mono_pcm_wav(input_path):
            print("[ffmpeg] 未安装，输入已是 16k 单声道 PCM WAV → 直通")
            shutil.copyfile(input_path, output_path)
            return True
        print("[ffmpeg] 未安装且输入不是 16k 单声道 PCM WAV，无法转码")
        return False

    # 注意 stop_periods 必须为 -1（去掉全部结尾静音）。
    # 用 1 时 ffmpeg 会在遇到的"第一个 ≥100ms 的静音"处截断整个输出：
    # 用户句内停顿（~0.3~1.5s，非常常见）会让后半句整段丢失 → "听不清用户在干啥"。
    trim = ("silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.15"
            ":stop_periods=-1:stop_threshold=-55dB:stop_silence=0.1")
    # dynaudnorm 响度归一化：放大轻声说话（关键！否则安静录音被 STT 视为空/只识别一两个字）
    # 裁剪阈值取 -55dB（仅去除真静音），避免把轻声语音误当静音裁掉
    # soxr 重采样放在链尾，保证 48k→16k 转换质量（提升识别准确率）
    resample = "aresample=16000:resampler=soxr"
    if noise_reduction:
        af = f"highpass=f=80,lowpass=f=8000,afftdn=nr=10,{trim},dynaudnorm=p=0.9:s=5,{resample}"
        af_label = "降噪"
    else:
        af = f"highpass=f=80,lowpass=f=8000,{trim},dynaudnorm=p=0.9:s=5,{resample}"
        af_label = "裁剪"
    is_raw_opus = input_path.endswith(".opus")

    def _run_attempts(attempt_list):
        for attempt, (cmd, label) in enumerate(attempt_list):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0:
                    return True
                else:
                    stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:].strip()
                    # Windows 上退出码 > 2^31 通常是 ffmpeg 进程崩溃（如访问违例）
                    crash_hint = " (疑似崩溃)" if result.returncode > 0x7FFFFFFF else ""
                    print(f"[ffmpeg] {label} 失败 (rc={result.returncode}){crash_hint}: {stderr_tail}")
            except subprocess.TimeoutExpired:
                print(f"[ffmpeg] {label} 超时")
            except FileNotFoundError:
                print("[ffmpeg] ffmpeg 未安装或不在 PATH 中")
                return False
            except Exception as e:
                print(f"[ffmpeg] {label} 异常: {e}")
        return False

    if is_raw_opus:
        # 裸 Opus 数据包：必须使用 -f opus 显式指定 demuxer
        filtered_cmds = [
            (["ffmpeg", "-y", "-f", "opus", "-i", input_path,
              "-af", af,
              "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
              output_path], "Opus" + af_label + "版")]
        plain_cmds = [
            (["ffmpeg", "-y", "-f", "opus", "-i", input_path,
              "-ar", "16000", "-ac", "1",
              output_path], "Opus基础版")]
    else:
        # 通用容器格式尝试（WebM/Ogg/MP4 等）
        filtered_cmds = [
            # 方案1: 滤波链（裁剪或降噪）
            (["ffmpeg", "-y", "-i", input_path,
              "-af", af,
              "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
              output_path], af_label + "版"),
        ]
        plain_cmds = [
            # 方案2: 基础转换（无滤波）
            (["ffmpeg", "-y", "-i", input_path,
              "-ar", "16000", "-ac", "1",
              output_path], "基础版"),
            # 方案3: 容错 + 修复损坏的 webm（处理 MediaRecorder 未正确封包的情况）
            (["ffmpeg", "-y",
              "-err_detect", "ignore_err",
              "-fflags", "+genpts+igndts",
              "-analyzeduration", "10M",
              "-probesize", "10M",
              "-vn",
              "-i", input_path,
              "-ar", "16000", "-ac", "1",
              output_path], "修复版"),
        ]

    ok = _run_attempts(filtered_cmds + plain_cmds)
    if ok:
        return True

    # 当 .opus 格式导致 ffmpeg 崩溃时，自动回退：把文件重命名为 .webm 再重试
    if is_raw_opus:
        print("[ffmpeg] Opus 解析失败，尝试作为 WebM 容器回退重试…")
        webm_path = input_path.replace(".opus", ".webm")
        try:
            os.rename(input_path, webm_path)
            fallback_attempts = [
                (["ffmpeg", "-y", "-i", webm_path,
                  "-ar", "16000", "-ac", "1",
                  output_path], "WebM回退版"),
                (["ffmpeg", "-y",
                  "-err_detect", "ignore_err",
                  "-fflags", "+genpts+igndts",
                  "-analyzeduration", "10M",
                  "-probesize", "10M",
                  "-vn",
                  "-i", webm_path,
                  "-ar", "16000", "-ac", "1",
                  output_path], "WebM回退修复版"),
            ]
            if _run_attempts(fallback_attempts):
                return True
        except Exception as e:
            print(f"[ffmpeg] WebM 回退异常: {e}")

    return False


# ---------- STT（语音转文字）独立配置 ----------
# 与 TTS 配置同模式：独立文件 stt_config.json，运行时可经 /api/stt/config 修改。
# 只走云端 API（SiliconFlow）；本地 faster-whisper 引擎已移除，
# 省 515M 模型缓存与启动预热线程。
STT_CONFIG_FILE = BASE_DIR / "stt_config.json"
DEFAULT_STT_CONFIG = {
    "provider": "auto",                       # auto=云端优先，失败自动重试 | cloud=仅云端单次
    "api_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
    "api_key": "",                            # 语音识别专用密钥；留空沿用大语言模型 API Key
    "model": "XingChenAGI/XingChenASR-V3.2",  # 主模型（2026-09-19 实测 200 OK、1.0~1.2s）
    # 兜底模型链：模型级过载会让请求整段挂死而不报错（实测 SenseVoiceSmall 20s
    # 超时率 60%，Qwen3-ASR 连续 5 次 ReadTimeout 15~40s）→ 换模型比同模型重试有效
    "fallback_models": ["Qwen/Qwen3-ASR-1.7B"],
    "api_timeout": 10,                        # 单模型超时；挂死靠换模型兜底，不必等 30s
}



def _load_stt_config():
    """加载 STT 独立配置；首次运行从 settings.json 的 stt 段迁移旧值。"""
    cfg = dict(DEFAULT_STT_CONFIG)
    if STT_CONFIG_FILE.exists():
        try:
            with open(STT_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update({k: saved[k] for k in DEFAULT_STT_CONFIG if k in saved})
                return cfg
        except Exception as e:
            logger.warning(f"加载 STT 配置失败: {e}")
    try:
        legacy = load_config().get("stt", {})
        if isinstance(legacy, dict):
            for k in DEFAULT_STT_CONFIG:
                if k in legacy:
                    cfg[k] = legacy[k]
    except Exception:
        pass
    return cfg


def _save_stt_config(cfg: dict):
    """保存 STT 配置到独立文件。"""
    try:
        with open(STT_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 STT 配置失败: {e}")


stt_config = _load_stt_config()


def speech_to_text(wav_path: str) -> str:
    """语音识别：云端 API（SiliconFlow），按「主模型 + 兜底模型链」依次尝试。

    单个 ASR 模型过载时，请求会整段挂死而不返回任何错误码——这种故障换模型
    比同模型重试有效得多（实测同 key 同接口：SenseVoiceSmall 20s 超时率 60%，
    Qwen3-ASR 连续 5 次 ReadTimeout 15~40s，XingChenASR 200 OK 1.0~1.2s）。
    provider: auto=走完整链（只配单个模型时=同模型重试 2 次）| cloud=仅主模型单次
    """
    import requests
    provider = (stt_config.get("provider") or "cloud").strip()
    if provider not in ("auto", "cloud"):
        provider = "cloud"

    # 云端密钥：优先用 STT 专用 Key；未单独填写时兼容旧版沿用大语言模型 API Key
    dedicated_key = (stt_config.get("api_key") or "").strip()
    api_key = dedicated_key or (load_config().get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("语音识别失败：未配置 API Key")

    url = (stt_config.get("api_url") or "").strip() or DEFAULT_STT_CONFIG["api_url"]
    primary = (stt_config.get("model") or "").strip() or DEFAULT_STT_CONFIG["model"]
    fallbacks = stt_config.get("fallback_models") or []
    chain = [primary] + [str(m).strip() for m in fallbacks
                         if str(m).strip() and str(m).strip() != primary]
    if provider == "cloud":
        chain = chain[:1]
    elif len(chain) == 1:
        chain = chain * 2  # 没配兜底模型时保持旧的「同模型重试一次」行为

    headers = {"Authorization": f"Bearer {api_key}", "Expect": ""}
    # SiliconFlow 是国内 API：实测走 fq 代理(7890) 100% read timeout，直连稳定
    timeout = float(stt_config.get("api_timeout", 30) or 30)
    with open(wav_path, "rb") as f:
        audio = f.read()

    last_err: Exception = RuntimeError("未请求")
    for idx, model in enumerate(chain):
        try:
            resp = requests.post(url, headers=headers,
                                 files={"file": ("audio.wav", audio, "audio/wav")},
                                 data={"model": model}, timeout=timeout)
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            if text:
                if idx:
                    print(f"[STT-API] 主模型未出结果，兜底模型 {model} 成功")
                return text
            raise RuntimeError("API 返回空文本")
        except Exception as e:
            last_err = e
            print(f"[STT-API] {model} 第{idx + 1}/{len(chain)}次失败（{e}）")
            msg = str(e).lower()
            # 密钥/权限问题换模型也没用，直接放弃；其余（超时、空文本、5xx）继续换下一个
            if any(k in msg for k in ("401", "403", "unauthorized", "invalid api key")):
                break
    raise RuntimeError(f"语音识别失败: {last_err}")


# ---------- 路由 ----------
_INDEX_CACHE: tuple = (0, "")


def _index_template() -> str:
    """index.html 原文，按 mtime 缓存（改前端不必重启服务）。"""
    global _INDEX_CACHE
    p = WEB_DIR / "index.html"
    mtime = p.stat().st_mtime_ns
    if _INDEX_CACHE[0] != mtime:
        _INDEX_CACHE = (mtime, p.read_text(encoding="utf-8"))
    return _INDEX_CACHE[1]


@app.get("/")
async def index(request: Request):
    """主界面，把当前角色同步注入 <head>。

    前端要按角色隐藏管理员专属入口（任务中心/工作区），而按钮是模块初始化时被
    工具栏轮盘一次性收进数组的：异步 fetch /api/auth/me 拿到的角色永远晚一步，
    按结果删按钮会在轮盘里留一个空扇区。所以角色必须同步给到。
    """
    role = auth_core.ROLE_ADMIN if _is_admin_request(request) else auth_core.ROLE_USER
    boot = ("<script>window.__ROLE=" + json.dumps(role)
            + ";document.documentElement.dataset.role=" + json.dumps(role) + ";</script>\n</head>")
    # 必须注入在**真正的** head 收尾标签前：模板注释里也出现过字面 </head>，
    # 用 replace(...,1) 会命中注释里那个，脚本被吞进注释、永不执行（实测
    # window.__ROLE 为 undefined，管理员过滤整体失效）。rpartition 锁定最后一处。
    head, _, tail = _index_template().rpartition("</head>")
    html = head + boot + tail
    resp = Response(content=html, media_type="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ---------- PWA：Service Worker 与 manifest 必须走根路径 ----------
# Service Worker 能控制的 scope 上限由脚本自身 URL 决定：脚本放 /static/ 下，
# 最多只能接管 /static/，导航请求和 API 就落不进缓存层。所以这两个文件直出根路径。
@app.get("/sw.js")
async def service_worker():
    resp = FileResponse(str(WEB_DIR / "sw.js"), media_type="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    resp = FileResponse(str(WEB_DIR / "manifest.webmanifest"),
                        media_type="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/setup-qr.png")
async def setup_qr(request: Request):
    """引导页二维码：按访问主机实时生成。

    为什么必须动态生成：二维码内容里含局域网 IP，硬编码成图片后路由器一换段
    （192.168.31.x → 192.168.x.x）二维码就指向一个不存在的地址，而这张图在手机上
    看不出错，只会表现为「扫了打不开」——极难排查。实时生成永远对。

    为什么指向 HTTP 回源端口而非 HTTPS 主站：扫码的设备还没装 CA，扫 HTTPS 地址
    第一眼就是「不安全」红页，用户得手动点「高级 → 继续访问」才进得来（装证书的
    入口反而要用户先跳过证书警告，这是死循环）。HTTP 引导页无警告，装完 CA 页内
    按钮再跳 HTTPS，形成单向流程。
    """
    import io
    import qrcode

    # 8001 = http_only 模式下的回源端口（见本文件 __main__ 里 _serve_port 的定义）
    host = request.url.hostname or "127.0.0.1"
    target = f"http://{host}:8001/setup"

    q = qrcode.QRCode(box_size=8, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(target)
    q.make(fit=True)
    img = q.make_image(fill_color="#0b0a18", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-cache",
                             "X-QR-Target": target})


# ---------- 手机接入向导 ----------
# 自签 CA 必须能被手机下载安装，否则 HTTPS 不被信任、Service Worker 拒绝注册，
# 「加到主屏幕 + 离线秒开」整条链路都起不来。这个页面就是那一步的入口。
@app.get("/setup")
async def setup_page():
    resp = FileResponse(str(WEB_DIR / "setup.html"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/dabai-ca.crt")
async def dabai_ca_cert():
    """下发根证书。iOS 认 application/x-x509-ca-cert，Android 认 .crt 后缀 + 附件下载。"""
    path = WEB_DIR / "dabai-ca.crt"
    if not path.is_file():
        return JSONResponse({"error": "根证书缺失：web/dabai-ca.crt"}, status_code=404)
    resp = FileResponse(str(path), media_type="application/x-x509-ca-cert",
                        filename="dabai-ca.crt")
    resp.headers["Content-Disposition"] = 'attachment; filename="dabai-ca.crt"'
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------- 动作库测试页 ----------
@app.get("/anim-test")
async def anim_library_test():
    """Mixamo 动作库测试页"""
    resp = FileResponse(str(WEB_DIR / "animation-library-test.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/anim-download")
async def anim_download_checklist():
    """Mixamo 动作下载清单页"""
    resp = FileResponse(str(WEB_DIR / "anim" / "download-checklist.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/bigscreen")
async def bigscreen_preview():
    """网页版任务直播大屏（bigscreen.html）：浏览器直接打开即预览，
    3D 大屏用隐藏 iframe + html2canvas 每帧取帧贴纹理，两边画面一致。"""
    resp = FileResponse(str(WEB_DIR / "bigscreen.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp
@app.get("/anim-library")
async def anim_library_console():
    """动作库管理控制台"""
    resp = FileResponse(str(WEB_DIR / "anim" / "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ---------- Mixamo 代理下载服务 ----------
@app.get("/api/mixamo/status")
async def mixamo_status():
    """获取下载服务状态"""
    from mixamo_download_service import service
    return service.get_status()


@app.post("/api/mixamo/start")
async def mixamo_start(request: Request):
    """启动代理浏览器"""
    from mixamo_download_service import service
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except:
        body = {}
    proto = body.get("proto")
    try:
        result = await service.start(proto=proto)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mixamo/stop")
async def mixamo_stop():
    """关闭浏览器"""
    from mixamo_download_service import service
    try:
        result = await service.stop()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mixamo/save-cookies")
async def mixamo_save_cookies():
    """保存当前登录 cookies"""
    from mixamo_download_service import service
    try:
        result = await service.save_cookies()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mixamo/check-login")
async def mixamo_check_login():
    """基于 cookies 检测是否已登录（不打断登录页）"""
    from mixamo_download_service import service
    if not service.is_running:
        return {"is_logged_in": service.is_logged_in}
    try:
        logged = await service.check_login_cookies()
        return {"is_logged_in": logged}
    except Exception:
        return {"is_logged_in": service.is_logged_in}


@app.post("/api/mixamo/goto")
async def mixamo_goto(request: Request):
    """跳转到 Mixamo 主页"""
    from mixamo_download_service import service
    try:
        result = await service.goto_mixamo()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mixamo/download")
async def mixamo_download(request: Request):
    """下载单个动作"""
    try:
        body = await request.json()
    except:
        raise HTTPException(status_code=400, detail="invalid json")
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    from mixamo_download_service import service
    if not service.is_running:
        raise HTTPException(status_code=400, detail="浏览器未启动，请先调用 /api/mixamo/start")
    try:
        result = await service.download_animation(name)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mixamo/batch-download")
async def mixamo_batch_download(request: Request):
    """批量下载动作（后台异步执行）"""
    try:
        body = await request.json()
    except:
        raise HTTPException(status_code=400, detail="invalid json")
    names = body.get("names", [])
    if not names:
        raise HTTPException(status_code=400, detail="names required")
    from mixamo_download_service import service
    if not service.is_running:
        raise HTTPException(status_code=400, detail="浏览器未启动，请先调用 /api/mixamo/start")
    # 后台异步执行：必须持有任务引用，否则 asyncio 可能把任务 GC 掉导致"没反应"
    import asyncio
    # 取消并等待上一个未完成的批量任务，避免并发抢同一页面导航
    if getattr(service, "_task", None) and not service._task.done():
        service._task.cancel()
    service._stop_event.clear()
    service._task = asyncio.create_task(service.batch_download(names))
    return {"status": "started", "total": len(names)}


@app.post("/api/mixamo/debug-dom")
async def mixamo_debug_dom(request: Request):
    """临时调试：dump 当前搜索页真实 DOM 结构（用后删）"""
    from mixamo_download_service import service
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not service.is_running:
        return {"error": "browser not running"}
    try:
        return await service.debug_dom(body.get("query", "Idle"), bool(body.get("interact")))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/info")
async def info():
    return {"lan_ip": get_lan_ip(), "port": SERVER_PORT}


# ---------- TTS 配置 ----------
@app.get("/api/tts/voices")
async def tts_voices():
    """返回 edge_tts 中文音色列表"""
    try:
        return {"voices": await get_edge_voices()}
    except Exception as e:
        return {"voices": [], "error": str(e)}


@app.get("/api/tts/characters")
async def tts_characters():
    """返回 gpt_sovits.json 里配置的角色列表（对齐 voice.py）"""
    for rel in ("tools/gpt_sovits/gpt_sovits.json", "gpt_sovits.json"):
        p = BASE_DIR / rel
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return {"characters": list(data.keys()), "source": rel}
            except Exception as e:
                return {"characters": [], "error": str(e)}
    return {"characters": [], "error": "未找到 gpt_sovits.json"}


@app.get("/api/tts/config")
async def tts_config_get():
    return tts_config


@app.post("/api/tts/config")
async def tts_config_set(payload: dict):
    """更新 TTS 配置（部分字段）"""
    for k in ("engine", "edge_voice", "edge_rate",
              "gptsovits_url", "gptsovits_ref_audio", "gptsovits_character",
              "api_url", "api_key", "api_model", "api_voice"):
        if k in payload:
            tts_config[k] = payload[k]
    # 空音色兜底：不允许保存空白 edge_voice（会导致 Invalid voice）
    if not (tts_config.get("edge_voice") or "").strip():
        tts_config["edge_voice"] = DEFAULT_VOICE
    # 切换音色后清空缓存，便于下次重新拉取
    if "edge_voice" in payload:
        global _edge_voices_cache
        _edge_voices_cache = None
    # 持久化保存
    _save_tts_config({k: tts_config[k] for k in DEFAULT_TTS_CONFIG})
    return {"ok": True, "config": tts_config}


# ---------- STT（语音转文字）配置 ----------
@app.get("/api/stt/config")
async def stt_config_get():
    """获取语音识别独立配置（供前端表单回填）。"""
    return dict(stt_config)


@app.post("/api/stt/config")
async def stt_config_set(payload: dict):
    """更新语音识别独立配置（部分字段），立即生效并持久化到 stt_config.json。"""
    for k in DEFAULT_STT_CONFIG:
        if k in payload and payload[k] is not None:
            v = payload[k]
            stt_config[k] = str(v).strip() if isinstance(v, str) else v
    if stt_config.get("provider") not in ("auto", "cloud"):
        stt_config["provider"] = "cloud"
    _save_stt_config({k: stt_config[k] for k in DEFAULT_STT_CONFIG})
    logger.info(
        f"STT 配置更新: provider={stt_config['provider']} model={stt_config['model']} "
        f"专用密钥={'已设置' if (stt_config.get('api_key') or '').strip() else '未设置（沿用主 Key）'}"
    )
    return {"ok": True, "config": {k: stt_config[k] for k in DEFAULT_STT_CONFIG}}


# ============================================================
# 任务中心 + DSH 桥接（DeepSeek Harness ↔ 大白）
# 任务中心把 codex/opencode、后台命令、DSH 智能体统一纳入可视化调度；
# DSH 任务由角色提交 → 用户确认 → DSH 智能体执行 → 结果回传任务中心。
# ============================================================
from task_orchestrator import (
    get_orchestrator,
    set_broadcast,
    TaskOrchestrator,
    STATUS_CONFIRMING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_CANCELLED,
)

# 状态名兼容（旧前端确认卡片仍用 pending/running/done...）
_LEGACY_STATUS = {
    STATUS_CONFIRMING: "pending",
    STATUS_QUEUED: "pending",
    STATUS_RUNNING: "running",
    STATUS_DONE: "done",
    STATUS_ERROR: "error",
    STATUS_CANCELLED: "cancelled",
}


def _legacy(task) -> Optional[str]:
    return _LEGACY_STATUS.get(task.status)


async def request_harness_task(ws: WebSocket, task_text: str, cwd: Optional[str] = None):
    """角色调用 call_deepseek_harness 时由工具结果触发：登记 DSH 任务并发起确认。

    返回 orchestrator 任务（kind=dsh，confirm=True）。前端确认卡片 + 任务中心双通道。

    工作目录：优先用传入的 cwd（LLM 显式指定且有效时）；否则回退到全局工作区，
    杜绝"改了工作区但任务仍跑旧目录"（AI 可能凭记忆填旧路径或参数缺失）。
    """
    if not (cwd and os.path.isdir(cwd)):
        cwd = _workspace_current().get("cwd") or ""
    orch = get_orchestrator()
    task = await orch.create(
        kind="dsh",
        title="DSH 智能体任务",
        ws=ws,
        brief=task_text,
        confirm=True,
        channel="dsh",
        extra={"cwd": cwd},
    )
    # 兼容旧版确认卡片（新任务中心通过 task_event 同步）
    await safe_send_json(ws, {
        "type": "bridge_confirm",
        "request_id": task.id,
        "task": task_text,
        "require_confirm": True,
        "task_id": task.id,
    })
    return task


# ---------- 任务中心：codex/后台命令接入 ----------

async def _mirror_codex_event(task, data: dict) -> None:
    """把 codex 流式事件镜像成任务中心的步骤/日志/结果。"""
    try:
        kind = str(data.get("type") or "")
        if kind == "codex_start":
            await task_center_add_step_safe(task, f"已交给 {data.get('label') or data.get('tool')}（/{data.get('tool')}）")
        elif kind == "codex_log":
            # 结构化明细：工具调用开始记为里程碑步骤；参数/输出/对话行进实时日志（批量推送）
            entries = data.get("entries") or []
            if not isinstance(entries, list):
                entries = []
            logs = []
            tool_no = int((task.extra or {}).get("codex_tool_count") or 0)
            for e in entries:
                if not isinstance(e, dict):
                    continue
                et = str(e.get("type") or "")
                if et == "tool":
                    tool_no += 1
                    await task_center_add_step_safe(
                        task, f"🔧 第 {tool_no} 步 {e.get('tool') or '工具'}")
                elif et in ("args", "out", "log", "turn", "tool_end", "header"):
                    t = str(e.get("text") or "")
                    if t:
                        logs.append(t)
            if task.extra is not None:
                task.extra["codex_tool_count"] = tool_no
            if logs:
                # 每批最多 40 行进任务中心日志（完整明细在 codex 卡片/任务 trace 接口）
                await task_center_add_logs_safe(task, logs[-40:])
        elif kind == "codex_progress":
            fresh = str(data.get("fresh") or "").strip()
            if fresh:
                for line in fresh.splitlines()[:6]:
                    await task_center_add_log_safe(task, line)
        elif kind == "codex_error":
            await task_center_add_log_safe(task, "⚠️ " + str(data.get("message") or "执行出错"))
        elif kind == "codex_done":
            summary = str(data.get("summary") or data.get("raw_tail") or "").strip()
            if summary:
                steps_n = data.get("steps_total") or (task.extra or {}).get("codex_tool_count") or 0
                lines_n = data.get("lines_total") or ""
                cnt = f"，执行 {steps_n} 步 / {lines_n} 行" if steps_n or lines_n else ""
                await task_center_add_step_safe(
                    task, f"{'✅' if data.get('success') else '❌'} {data.get('label')} 完成（exit={data.get('exit_code')}{cnt}）")
            # 系统核验验收标准：把 PASS/FAIL 结果写进任务中心日志
            v = data.get("verify")
            if isinstance(v, dict):
                if v.get("found"):
                    if v.get("ok"):
                        await task_center_add_log_safe(
                            task, f"✅ 验收核验通过：PASS {v.get('pass')} / FAIL {v.get('fail')}")
                    else:
                        await task_center_add_log_safe(
                            task, f"❌ 验收核验未通过：PASS {v.get('pass')} / FAIL {v.get('fail')}（{v.get('reason')}）")
                else:
                    await task_center_add_log_safe(
                        task, "⚠ 验收核验：未提供【验收核验】块，无法系统确认达标")
            await task_center_set_result_safe(task, summary or "（无输出）", STATUS_DONE if data.get("success") else STATUS_ERROR)
        elif kind == "codex_terminated":
            await task_center_set_error_safe(task, str(data.get("message") or "任务已终止"))
        elif kind == "codex_timeout":
            await task_center_set_error_safe(task, str(data.get("message") or "任务超时"))
    except Exception:
        pass


async def _mirror_bg_report(task, report: str) -> None:
    """把后台任务 report 增量镜像为任务中心日志。"""
    for line in str(report).splitlines():
        line = line.strip()
        if line and (not task.logs or task.logs[-1] != line):
            await task_center_add_log_safe(task, line)


async def task_center_add_step_safe(task, text: str) -> None:
    orch = get_orchestrator()
    try:
        await orch.add_step(task, text)
    except Exception:
        pass


async def task_center_add_log_safe(task, text: str) -> None:
    orch = get_orchestrator()
    try:
        await orch.add_log(task, text)
    except Exception:
        pass


async def task_center_add_logs_safe(task, texts: list) -> None:
    orch = get_orchestrator()
    try:
        await orch.add_logs(task, list(texts))
    except Exception:
        pass


async def task_center_set_result_safe(task, text: str, status: str = STATUS_DONE) -> None:
    orch = get_orchestrator()
    try:
        await orch.set_result(task, text, status)
    except Exception:
        pass


async def task_center_set_error_safe(task, text: str) -> None:
    orch = get_orchestrator()
    try:
        await orch.set_error(task, text)
    except Exception:
        pass


class _TaskTeeWS:
    """把 invoke_tool_stream 推给真实 ws 的消息同时镜像到任务中心（不改 codex_runner）。"""

    def __init__(self, real_ws, task):
        self._real = real_ws
        self._task = task
        try:
            self.client_state = real_ws.client_state
        except AttributeError:
            self.client_state = None  # 仅测试/极端场景：webhook 无连接

    async def send_json(self, data: dict) -> None:
        if self._real is not None:
            await self._real.send_json(data)
        await _mirror_codex_event(self._task, data)


# 待用户确认的 codex/opencode 委派：task_id -> 确认后要启动的协程工厂。
# 与 DSH 桥接同一套安全闸门：智能体委派一律先经用户确认才真正执行（防误操作）。
_pending_codex_runners: dict[str, Callable] = {}


# ---------- 多智能体并发治理：文件域路由 + 核心域闸门 + 隔离工作树 ----------
# 文件域：web=只动 web/ 前端（可并行）；skills=只动 skills/ 插件（按目录并行）；
#         core=涉及根目录 *.py / harness/*.py（同一时间只允许 1 个任务，自动在隔离工作树执行）
SCOPE_WEB = "web"
SCOPE_SKILLS = "skills"
SCOPE_CORE = "core"

# core 强信号：任务描述命中其一即按核心域串行+隔离（宁可多排队，不可多并发）
_CORE_SIGNALS = (
    "server.py", "agent.py", "memory.py", "harness", "核心", "后端",
    "热重载", "hot_reload", "执行链路", "上下文", "记忆", "调度",
    "codex_runner", "task_orchestrator", "心跳", "重构", "热更新", "重启",
)
_WEB_SIGNALS = ("web/", ".ts", "前端", "页面", "聊天框", "视频页", "样式", "css",
                "ui", "按钮", "弹窗", "气泡", "工作区")
_SKILLS_SIGNALS = ("skills/", "技能", "skill")


def _infer_scope(task_desc: str) -> str:
    """按任务描述推断文件域：core / web / skills（默认 core 保守串行）。"""
    t = (task_desc or "").lower()
    if any(k in t for k in _CORE_SIGNALS):
        return SCOPE_CORE
    if any(k in t for k in _WEB_SIGNALS):
        return SCOPE_WEB
    if any(k in t for k in _SKILLS_SIGNALS):
        return SCOPE_SKILLS
    return SCOPE_CORE


# 核心域并发闸门：core 任务同一时间只执行 1 个，其余排队（Semaphore=1，延迟到事件循环内创建）
_core_slot: Optional[asyncio.Semaphore] = None


def _get_core_slot() -> asyncio.Semaphore:
    global _core_slot
    if _core_slot is None:
        _core_slot = asyncio.Semaphore(1)
    return _core_slot


def _wt_root_dir() -> str:
    """隔离工作树根目录：settings.json -> agent.worktree_dir，默认 <仓库父目录>/dabai_worktrees。"""
    try:
        cfg = _load_settings()
        v = str((cfg or {}).get("agent", {}).get("worktree_dir") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return str((BASE_DIR.parent / "dabai_worktrees"))


def _git_run(cwd: str, args: list, timeout: int = 60) -> tuple:
    """同步执行 git 命令，返回 (returncode, stdout, stderr)。"""
    import subprocess
    try:
        flags = pc.no_window_flags()
        r = subprocess.run(["git", "-C", cwd] + args, capture_output=True,
                           text=True, timeout=timeout, creationflags=flags)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:
        return -1, "", f"git 执行异常: {e}"


def _auto_wt_create_sync(task) -> tuple:
    """为 core 任务创建隔离工作树 codex/wt-<id>（基于 HEAD）。返回 (wt_dir, branch, err)。"""
    repo = str(BASE_DIR)
    rc, _, err = _git_run(repo, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return None, None, f"主仓库不是 git 仓库，无法隔离：{err}"
    slug = "wt-" + (task.id or "x").replace("task-", "")[:10]
    wt_root = _wt_root_dir()
    try:
        os.makedirs(wt_root, exist_ok=True)
    except Exception as e:
        return None, None, f"无法创建工作树目录 {wt_root}: {e}"
    target = os.path.join(wt_root, slug)
    branch = "codex/" + slug
    if os.path.isdir(target):
        return None, None, f"同名工作树已存在（{target}），请先人工 wt_list/wt_merge/wt_discard 处理"
    rc, out, err = _git_run(repo, ["worktree", "add", "-b", branch, "HEAD", target])
    if rc != 0:
        return None, None, f"创建工作树失败: {err or out}"
    return target, branch, None


def _auto_wt_merge_sync(task, wt_dir: str, branch: str) -> str:
    """core 任务结束后：wt 内提交 → 合回主分支 → 清理。冲突失败保留现场，不破坏任何改动。"""
    repo = str(BASE_DIR)
    out_lines = []
    rc, st, _ = _git_run(repo, ["status", "--porcelain"])
    if rc == 0 and st.strip():
        out_lines.append("⚠ 主工作区有未提交改动：若与本次改动涉及相同文件，git 会拒绝合并并保留现场（不覆盖）。")
    rc, st_wt, _ = _git_run(wt_dir, ["status", "--porcelain"])
    if rc == 0 and st_wt.strip():
        rc2, o2, e2 = _git_run(wt_dir, ["add", "-A"])
        if rc2 != 0:
            return "\n".join(out_lines + [f"✘ 隔离区 git add 失败：{e2 or o2}（工作树保留，请人工 wt_merge/wt_discard）"])
        rc2, o2, e2 = _git_run(wt_dir, ["commit", "-m", f"auto: {task.title[:80]}"])
        if rc2 != 0:
            return "\n".join(out_lines + [f"✘ 隔离区 git commit 失败：{e2 or o2}（工作树保留，请人工 wt_merge/wt_discard）"])
        out_lines.append(f"✔ 隔离区已提交 {len(st_wt.strip().splitlines())} 处改动")
    else:
        out_lines.append("ℹ 隔离区无改动（或均为忽略文件），跳过提交。")
    rc, o3, e3 = _git_run(repo, ["merge", "--no-ff", branch, "-m", f"merge worktree {os.path.basename(wt_dir)}"])
    if rc != 0:
        return "\n".join(out_lines + [f"⚠ 合并冲突/失败：{(e3 or o3)[:400]}；工作树与分支保留，可人工 wt_merge 或 wt_discard 处理。"])
    out_lines.append("✔ 已合并回主分支（--no-ff）")
    _git_run(repo, ["worktree", "remove", "--force", wt_dir])
    _git_run(repo, ["branch", "-d", branch])
    out_lines.append("✔ 已清理隔离工作树与临时分支")
    return "\n".join(out_lines)


async def _run_codex_tool_task(ws: WebSocket, tool_name: str, tcfg: dict, task_desc: str) -> None:
    """登记 /cx /ai 委派任务【需用户确认】后再真正执行（与 DSH 桥接一致的安全闸门）。

    任务进入 confirming 状态并推送确认卡片；用户在任务中心/确认卡点击
    「确认执行」后，才会启动 codex/opencode 子进程。拒绝则丢弃，绝不执行。
    """
    orch = get_orchestrator()
    # 死循环防护：相同任务在近 30 分钟内已"静默失败"≥2 次 → 不再重复提交。
    # 防止 codex/opencode 启动即失败/无输出时，同一任务被反复原样重发空跑。
    fp = re.sub(r"\s+", " ", task_desc or "").strip().lower()
    now = time.time()
    silent_fail = 0
    for t in orch._tasks.values():
        if t.kind != "codex-tool" or t.status != STATUS_ERROR:
            continue
        if now - t.updated_at > 1800:
            continue
        if re.sub(r"\s+", " ", t.brief or "").strip().lower() == fp:
            # 只拦"几乎没有有效输出"的失败（结果/错误都很短），有实质进展的不拦
            if len((t.result or "").strip()) < 200 and len((t.error or "").strip()) < 200:
                silent_fail += 1
    if silent_fail >= 2:
        logger.warning("[Delegate] 相同任务 30 分钟内已静默失败 %d 次，已拦截重复委派", silent_fail)
        await safe_send_json(ws, {
            "type": "codex_msg",
            "kind": "error",
            "text": (f"⛔ 相同任务在近 30 分钟内已连续失败 {silent_fail} 次（且都没有有效输出），"
                     "为避免重复空跑已拦截。请先到任务中心查看最近任务的日志定位原因，"
                     "或修改任务描述后再试。"),
        })
        return
    scope = _infer_scope(task_desc)  # 多智能体并发治理：按任务描述判定文件域
    task = await orch.create(
        kind="codex-tool",
        title=f"/{tool_name} {task_desc[:40]}",
        ws=ws,
        brief=task_desc,
        confirm=True,  # ← 安全闸门：所有智能体委派都要求用户确认
        channel="codex" if tool_name == "cx" else "opencode",
        extra={"scope": scope},
    )
    await task_center_add_log_safe(
        task,
        f"🗂 文件域判定：{scope}"
        + ("（web 前端，可与他人并行）" if scope == SCOPE_WEB
           else "（技能插件）" if scope == SCOPE_SKILLS
           else "（核心代码：串行执行 + 隔离工作树）"),
    )

    async def _launch():
        tee = _TaskTeeWS(ws, task)
        if scope == SCOPE_CORE:
            # 核心域并发闸门：core 任务同一时间只执行 1 个，其余排队等待
            await task_center_add_step_safe(task, "⌛ 排入核心域队列（核心文件任务同一时间只执行 1 个）…")
            await _get_core_slot().acquire()
            try:
                await _run_agent_in_workspace(task, tee)
            finally:
                _get_core_slot().release()
        else:
            await _run_agent_in_workspace(task, tee)

    async def _run_agent_in_workspace(task, tee):
        """执行 agent 任务；core 域自动在隔离工作树执行（不触发大白热重载），结束后合回主分支。"""
        wt_dir, branch, err = None, None, None
        if scope == SCOPE_CORE:
            wt_dir, branch, err = await asyncio.to_thread(_auto_wt_create_sync, task)
            if err:
                await task_center_add_log_safe(task, f"⚠ 未能创建隔离工作树（{err}），将退化为在主工作区直接执行")
            else:
                await task_center_add_step_safe(
                    task, f"🛡 已创建隔离工作树：{wt_dir}（分支 {branch}；改动不触发核心热重载）")
        try:
            await codex_runner.invoke_tool_stream(tool_name, tcfg, task_desc, tee,
                                                  task_id=task.id, work_dir=wt_dir)
        except Exception as e:
            await task_center_set_error_safe(task, f"{e.__class__.__name__}: {e}")
        if wt_dir and branch:
            try:
                msg = await asyncio.to_thread(_auto_wt_merge_sync, task, wt_dir, branch)
                await task_center_add_logs_safe(task, msg.splitlines())
            except Exception as e:
                await task_center_add_log_safe(task, f"✘ 自动合并回主分支失败: {e}。请人工 wt_list/wt_merge 处理。")

    _pending_codex_runners[task.id] = _launch
    # 兼容旧前端确认卡片（任务中心通过 task_event 同步，同一套确认 UI）
    await safe_send_json(ws, {
        "type": "bridge_confirm",
        "request_id": task.id,
        "task": task_desc,
        "require_confirm": True,
        "task_id": task.id,
    })


async def _launch_if_pending(task_id: str) -> None:
    """用户确认通过后，真正启动之前登记的 codex/opencode 执行器。"""
    runner = _pending_codex_runners.pop(task_id, None)
    if runner is not None:
        asyncio.create_task(runner())


def _drop_pending(task_id: str) -> None:
    """用户拒绝/取消时丢弃待确认的委派，确保绝不执行。"""
    _pending_codex_runners.pop(task_id, None)


# ⚠ 同名警告：本文件 3831 行还有**另一个** _BroadcastWS（完整版：含 client_state
# 属性 + 对话轮进度缓冲）。Python 后定义覆盖前定义，所以运行时生效的是 3831 行那个，
# 本类实际是历史遗留死代码（唯一的实例化在 3867 行，位于 3831 之后）。
# 千万不要因为「看着像重复」就把 3831 行那个删掉 —— 它一旦消失，本类会被启用，
# _BROADCAST_WS 将缺少 client_state，依赖它的对话轮广播会直接 AttributeError。
class _BroadcastWS:
    """恢复任务的伪 ws：把 codex 事件广播给所有已连接前端（重启后原 ws 已失效）。"""

    async def send_json(self, data: dict) -> None:
        for ws in list(manager.active):
            await safe_send_json(ws, data)


async def _recover_codex_tasks() -> int:
    """server 重启/热重载后：把仍在跑的 codex/opencode 独立进程接回任务中心并恢复监听。"""
    orch = get_orchestrator()
    n = 0
    for entry in codex_runner.recover_running_tasks():
        try:
            tid = str(entry.get("id") or "")
            if not tid or orch.get(tid):
                continue
            tool_name = str(entry.get("tool") or "")
            title = f"/{tool_name} {str(entry.get('task') or '')[:40]}" if tool_name else "编码助手（恢复）"
            channel = "codex" if tool_name == "cx" else "opencode"
            broadcast = _BROADCAST_WS
            task = await orch.restore(
                task_id=tid,
                kind="codex-tool",
                title=title,
                ws=broadcast,
                brief=str(entry.get("task") or ""),
                channel=channel,
                extra={"recovered": True, "pid": entry.get("pid"), "alive": entry.get("alive")},
            )
            if task is None:
                continue
            tee = _TaskTeeWS(broadcast, task)
            asyncio.create_task(codex_runner.invoke_tool_recover(tid, tee))
            n += 1
        except Exception as e:
            logger.warning(f"[TaskCenter] codex 任务恢复失败: {e}")
    return n


def _harness_task_snapshot(t: dict, full: bool = False) -> dict:
    """把 Harness TaskSystem 的 flow/batch 任务映射成任务中心快照格式。

    brief 列表（list_tasks）只有 progress/waiting_confirm；full 详情（status()）
    才有 action 里的步骤/条目状态，可生成 steps 摘要。
    """
    st = t.get("state") or ""
    status = {"pending": "queued", "running": "running", "succeeded": "done",
              "failed": "error", "cancelled": "cancelled"}.get(st, st or "queued")
    if t.get("waiting_confirm"):
        status = "confirming"
    steps: list[str] = []
    if full:
        act = t.get("action") or {}
        if t.get("kind") == "flow":
            ss = act.get("step_states") or {}
            for sid, s in ss.items():
                sst = s.get("state") if isinstance(s, dict) else ""
                label = {"pending": "待执行", "running": "执行中", "succeeded": "✓ 完成",
                         "failed": "✗ 失败", "cancelled": "已取消",
                         "waiting_confirm": "⏳ 待确认"}.get(sst, sst)
                steps.append(f"{sid}：{label}")
        elif t.get("kind") == "batch":
            is_ = act.get("item_states") or {}
            total = len(act.get("items") or [])
            ok = sum(1 for s in is_.values() if isinstance(s, dict) and s.get("state") == "succeeded")
            fail = sum(1 for s in is_.values() if isinstance(s, dict) and s.get("state") == "failed")
            steps.append(f"条目 {ok}/{total} 成功" + (f"，{fail} 失败" if fail else ""))
    return {
        "id": t.get("id"), "kind": t.get("kind"),
        "channel": "harness", "title": t.get("name") or "Harness 任务",
        "status": status, "brief": t.get("goal") or "",
        "steps": steps, "logs": [],
        "result": t.get("result") or "", "error": t.get("error") or "",
        "confirm": bool(t.get("waiting_confirm")),
        "extra": {"harness": True, "state": st, "progress": t.get("progress")},
        "agent": {"name": "Harness 流程", "icon": "⚙️", "color": "#7c6cf0",
                  "desc": "Harness 长任务/流程/批量执行器"},
        "created_at": int((t.get("created_at") or 0) * 1000),
        "updated_at": int((t.get("finished_at") or t.get("created_at") or 0) * 1000),
        "progress": t.get("progress"),
    }


@app.get("/api/tasks")
async def task_center_list():
    """任务中心列表（全部通道：DSH/Codex/OpenCode/后台命令 + Harness 长任务/流程/批量）。"""
    orch = get_orchestrator()
    tasks = orch.list(limit=50)
    # 合并 Harness TaskSystem 的 flow/batch 任务（统一映射：进度/步骤/待确认）
    harness_ids: set = set()
    try:
        h = _harness()
        for t in h.tasks.list_tasks(limit=50):
            if t.get("kind") not in ("flow", "batch"):
                continue
            harness_ids.add(str(t.get("id") or ""))
            tasks.append(_harness_task_snapshot(t))
    except Exception as e:
        logger.warning(f"[TaskCenter] 合并 Harness 任务失败: {e}")
    # 去重：flow/batch 在 orchestrator 里还有一条镜像条（extra.harness_tid），
    # 与上面的原生条是同一个任务 —— 不去重就会同时显示两条同名任务。
    # 只留原生条（进度/步骤/待确认状态更全）；TaskSystem 不可用时镜像条照旧兜底。
    if harness_ids:
        tasks = [t for t in tasks
                 if str((t.get("extra") or {}).get("harness_tid") or "") not in harness_ids]
    # 合并长跑引擎：它是 systemd 拉起的独立进程，既不在 orchestrator 注册表、
    # 也不在 Harness TaskSystem 里 —— 只读合成一条（数据源是它的 state/journal/
    # heartbeat 文件），让它在任务中心可见、可点开、可停。
    try:
        from tools.longrun.status_view import snapshot as _longrun_snap, TASK_ID as _LONGRUN_ID
        tasks = [t for t in tasks if str(t.get("id") or "") != _LONGRUN_ID]
        tasks.append(_longrun_snap(full=False))
    except Exception as e:
        logger.warning(f"[TaskCenter] 合并长跑引擎失败: {e}")
    tasks.sort(key=lambda x: -(x.get("created_at") or 0))
    return {"ok": True, "tasks": tasks[:50]}


@app.get("/api/tasks/{task_id}")
async def task_center_get(task_id: str):
    orch = get_orchestrator()
    task = orch.get(task_id)
    if task is not None:
        return {"ok": True, "task": task.snapshot(full=True)}
    # 长跑引擎（合成条目，不在任何注册表里）
    try:
        from tools.longrun.status_view import snapshot as _longrun_snap, TASK_ID as _LONGRUN_TID
        if task_id == _LONGRUN_TID:
            return {"ok": True, "task": _longrun_snap(full=True)}
    except Exception as e:
        logger.warning(f"[TaskCenter] 长跑任务详情失败: {e}")
    # 回退：Harness TaskSystem 的 flow/batch 任务（详情含步骤/条目状态）
    try:
        ht = _harness().tasks.status(task_id)
        if ht is not None and ht.get("kind") in ("flow", "batch"):
            return {"ok": True, "task": _harness_task_snapshot(ht, full=True)}
    except Exception as e:
        logger.warning(f"[TaskCenter] Harness 任务详情回退失败: {e}")
    raise HTTPException(status_code=404, detail="任务不存在或已过期")


@app.get("/api/tasks/{task_id}/trace")
async def task_center_trace(task_id: str, after: int = 0, limit: int = 500):
    """下钻接口：返回 codex/opencode 任务的已解析执行明细（工具/参数/输出/耗时）。
    数据直接来自 codex_logs/<task_id>.log（文件即事实源），带全局 seq 可分页回补。
    """
    try:
        res = codex_runner.get_task_trace(task_id, after=after, limit=min(int(limit), 2000))
    except Exception as e:
        logger.warning(f"[TaskCenter] trace 读取失败 {task_id}: {e}")
        res = None
    if not res:
        raise HTTPException(status_code=404, detail="该任务没有可追溯的执行明细（仅 codex/opencode 任务支持）")
    return {"ok": True, **res}


@app.get("/api/tasks/{task_id}/log")
async def task_center_log(task_id: str, offset: int = 0, max_lines: int = 1000):
    """原始日志追读：与 trace 同源（codex_logs/<task_id>.log），按字节偏移取尾部。"""
    try:
        res = codex_runner.get_task_log_tail(task_id, offset=offset, max_lines=min(int(max_lines), 5000))
    except Exception as e:
        logger.warning(f"[TaskCenter] 原始日志读取失败 {task_id}: {e}")
        res = None
    if not res:
        raise HTTPException(status_code=404, detail="该任务没有原始日志（仅 codex/opencode 任务支持）")
    return {"ok": True, **res}


@app.post("/api/tasks/{task_id}/confirm")
async def task_center_confirm(task_id: str, payload: dict):
    """用户确认/拒绝某个任务。approve=true → 执行；false → 取消。

    所有通道共用同一闸门：确认通过后，若该任务登记了待执行器（codex/opencode），
    立刻真正启动；拒绝则丢弃待执行器，保证绝不执行。
    """
    orch = get_orchestrator()
    task = await orch.confirm(task_id, bool(payload.get("approve")))
    if task is None:
        # 回退：Harness TaskSystem 的 flow/batch 任务（approve/reject 等待确认的步骤）
        try:
            ts = _harness().tasks
            note = str(payload.get("note") or "")
            if payload.get("approve"):
                ok, msg = ts.approve_step(task_id, note)
            else:
                ok, msg = ts.reject_step(task_id, note)
            if ok:
                return {"ok": True, "task_id": task_id, "status": "queued" if payload.get("approve") else "cancelled"}
        except Exception as e:
            logger.warning(f"[TaskCenter] Harness 任务确认回退失败: {e}")
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if payload.get("approve"):
        await _launch_if_pending(task_id)
    else:
        _drop_pending(task_id)
    return {"ok": True, "task_id": task.id, "status": task.status}


@app.post("/api/tasks/{task_id}/kill")
async def task_center_kill(task_id: str):
    """中断一个执行中任务（DSH 会话取消 / 后台命令 kill / 待确认委派丢弃）。"""
    orch = get_orchestrator()
    task = orch.get(task_id)
    if task is None:
        # 长跑引擎（合成条目）：中断 = 停 systemd 服务，watchdog 一并停 ——
        # 只停服务不停 watchdog，它按心跳判活会立刻把它拉起来。
        try:
            from tools.longrun.status_view import TASK_ID as _LONGRUN_TID, service_action
            if task_id == _LONGRUN_TID:
                ok, msg = service_action("stop")
                if not ok:
                    raise HTTPException(status_code=500, detail=msg)
                return {"ok": True, "task_id": task_id, "status": "cancelled", "message": msg}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[TaskCenter] 长跑中断失败: {e}")
        # 回退：Harness TaskSystem 的 flow/batch 任务（cancel 中断整个流程/批量）
        try:
            if _harness().tasks.cancel(task_id):
                return {"ok": True, "task_id": task_id, "status": "cancelled"}
        except Exception as e:
            logger.warning(f"[TaskCenter] Harness 任务中断回退失败: {e}")
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    def cancel_fn(t):
        if t.kind == "dsh":
            import codex_runner  # noqa: F401  保证模块已加载（dshell 共用）
            from harness_bridge import get_bridge as _gb
            return _gb().cancel((t.extra or {}).get("cwd"))
        if t.kind == "codex-tool":
            _drop_pending(t.id)  # 待确认委派：丢弃待执行器，绝不执行
            # 已运行的独立进程任务：按注册表整树终止（taskkill /T）
            return codex_runner.kill_task(t.id) if t.status == STATUS_RUNNING else None
        if t.kind == "media-worker":
            wid = (t.extra or {}).get("worker_id") or ""
            if wid:
                return _get_media_workers().cancel(wid, reason="用户在任务中心取消")
            return None
        if t.kind == "sub-agent":
            wid = (t.extra or {}).get("worker_id") or ""
            if wid:
                return _get_sub_agents().cancel(wid, reason="用户在任务中心取消")
            return None
        return None

    await orch.cancel(task_id, cancel_fn)
    return {"ok": True, "task_id": task_id, "status": task.status}


@app.post("/api/tasks/clear")
async def task_center_clear():
    """批量清除所有已完成/失败/已取消的终态任务（任务中心「清除已完成」按钮）。

    双源清理：内存 orchestrator + Harness TaskSystem（含持久化日志），
    避免清除后轮询刷新又恢复。
    """
    orch = get_orchestrator()
    n = orch.clear_finished()
    try:
        h = _harness()
        n += h.tasks.clear_finished()
    except Exception as e:
        logger.warning(f"[TaskCenter] 清除 Harness 任务失败: {e}")
    return {"ok": True, "cleared": n}


@app.post("/api/longrun/{action}")
async def api_longrun_ctl(action: str):
    """长跑引擎启停：start/resume、stop/pause、restart、status。

    引擎是 systemd 单元，跟 server 没有父子关系，只能经 systemctl 控；
    子进程会阻塞事件循环，丢线程里跑。
    """
    from tools.longrun.status_view import service_action
    ok, msg = await asyncio.to_thread(service_action, action)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    return {"ok": True, "action": action, "message": msg}


@app.post("/api/system/restart_notice")
async def system_restart_notice():
    """重启前广播「计划内升级」：把意外掉线变成有预告的升级。

    调用方是 systemd 的 ExecStop=（先跑这条 HTTP，再发 SIGTERM）——那一刻服务还活着、
    WS 还连着，广播发得出去。不能靠 lifespan shutdown：uvicorn 先对所有连接
    transport.close()，再跑 lifespan，到那时广播必然发不出去。
    """
    notified = 0
    for ws in list(manager.active):
        if await safe_send_json(ws, {"type": "server_restart",
                                     "text": "大白正在升级，几秒后自动回来…"}):
            notified += 1
    if notified:
        # 等浏览器把提示画出来：send_json 只是把帧交给内核，进程立刻退会丢帧
        await asyncio.sleep(1.5)
    logger.info("[Restart] 重启通知已广播：活跃连接 %d，送达 %d", len(manager.active), notified)
    return {"ok": True, "notified": notified}


@app.get("/api/longrun/trace/{cycle}")
async def api_longrun_trace(cycle: int):
    """某一轮的原始 trace（prompt 摘要 / 工具调用序列 / 退出码），任务中心下钻用。

    轮次 trace 是 append-only 文件，读它不碰引擎状态 —— 引擎正在跑也能看上一轮。
    """
    from tools.longrun.status_view import read_trace
    return await asyncio.to_thread(read_trace, cycle)


# ---------- DSH 桥接（兼容旧前端确认卡片，引擎已迁移到任务中心） ----------

@app.get("/api/bridge/info")
async def harness_bridge_info():
    bridge = get_bridge()
    reachable = await asyncio.to_thread(bridge.ping)
    orch = get_orchestrator()
    active = [t.snapshot(full=True) for t in orch.list(50)
              if t["status"] in (STATUS_CONFIRMING, STATUS_QUEUED, STATUS_RUNNING)]
    return {
        "ok": True,
        "reachable": reachable,
        "config": bridge.config_view(),
        "pending": active,
    }


@app.post("/api/bridge/confirm")
async def harness_bridge_confirm(payload: dict):
    request_id = str(payload.get("request_id") or "")
    approve = bool(payload.get("approve"))
    orch = get_orchestrator()
    task = await orch.confirm(request_id, approve)
    if task is None or task.id != request_id:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if approve:
        await _launch_if_pending(request_id)
    else:
        _drop_pending(request_id)
    # 兼容推送旧卡片状态
    await safe_send_json(task.ws, {
        "type": "bridge_status",
        "request_id": task.id,
        "task": task.brief,
        "status": _legacy(task),
        "reply": task.result,
        "error": task.error,
    }) if task.ws else None
    return {"ok": True, "request_id": request_id, "status": _legacy(task)}


@app.get("/api/bridge/status")
async def harness_bridge_status(request_id: str):
    orch = get_orchestrator()
    task = orch.get(request_id)
    if task is None:
        return {"ok": False, "reason": "not_found"}
    return {
        "ok": True,
        "request_id": task.id,
        "task": task.brief,
        "status": _legacy(task),
        "reply": task.result,
        "error": task.error,
    }


@app.post("/api/bridge/cancel")
async def harness_bridge_cancel(payload: dict):
    request_id = str(payload.get("request_id") or "")
    orch = get_orchestrator()
    task = orch.get(request_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if task.kind == "codex-tool":
        # codex/opencode：未开始（待确认）则丢弃待执行器；已运行交给任务中心 kill
        _drop_pending(request_id)
        await orch.cancel(request_id)
    else:
        from harness_bridge import get_bridge as _gb
        await orch.cancel(request_id, lambda t: _gb().cancel((t.extra or {}).get("cwd")))
    return {"ok": True, "cancelled": True, "request_id": request_id}


@app.post("/api/bridge/say")
async def harness_bridge_say(payload: dict):
    """反向通道：DSH 侧（我）通过它让「大白」对用户说一句话 / 递一条消息。

    白名单防滥用：仅接受纯文本，最长 2000 字；推送给所有已连接前端。
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    text = text[:2000]
    for ws in list(manager.active):
        await safe_send_json(ws, {"type": "bridge_say", "text": text})
    logger.info(f"[Bridge] 反向上行消息已推送: {text[:60]}")
    return {"ok": True}


# ---------- 角色卡片 ----------
CHARACTER_CARDS_FILE = BASE_DIR / "character_cards.json"


def _load_character_cards() -> list:
    """加载角色卡片列表。"""
    if CHARACTER_CARDS_FILE.exists():
        try:
            with open(CHARACTER_CARDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("cards", []) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning(f"加载角色卡片失败: {e}")
    return []


def _save_character_cards(cards: list):
    """保存角色卡片列表到文件。"""
    try:
        with open(CHARACTER_CARDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"cards": cards}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存角色卡片失败: {e}")


# ---------- 大语言模型（LLM）供应商（全局资源） ----------
# 供应商注册表：任意数量的模型供应商（Ollama 本地 / 自定义 OpenAI 兼容 API）作为
# 全局资源统一管理。角色卡片只引用「供应商 id + 模型名」，不再各自存 base_url/api_key，
# 避免"选 Ollama 本地却加载自定义 API 模型"这类串配置问题。
LLM_PROVIDER_KINDS = ("ollama", "custom")
LLM_PROVIDER_KIND_LABELS = {"ollama": "Ollama 本地", "custom": "自定义 API"}
# llm_proxy 可选档位（settings.json 顶层；自定义地址直接填 URL 字符串）
LLM_PROXY_OPTIONS = {
    "off": "直连（不走代理）",
    "auto": "自动（fq 代理，未启动自动拉起 Clash）",
}


def _cloud_proxy_for(base_url: str = "") -> str | None:
    """按 settings.json 的 llm_proxy 解析当前云端代理；本地/内网地址永远直连。"""
    if is_local_url(base_url):
        return None
    try:
        return resolve_llm_proxy(_load_settings())
    except Exception:
        return None


def _norm_vision(v):
    """视觉开关三态：True 强制支持 / False 强制排除 / None 自动判定。

    字符串要一起收：前端 select 提交的是 "true"/"false"，手写配置里还可能是 1/off。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    elif isinstance(v, (int, float)):
        return bool(v)
    return None


def _llm_provider_meta(provider: dict) -> dict:
    """供应商出参（凭证 + 视觉开关）。"""
    return {
        "id": provider.get("id", ""),
        "name": provider.get("name", ""),
        "kind": provider.get("kind", "custom"),
        "base_url": provider.get("base_url", ""),
        "api_key": provider.get("api_key", ""),
        "default_model": provider.get("default_model", ""),
        "vision": _norm_vision(provider.get("vision")),
    }
def _ensure_llm_providers(cfg: dict) -> list:
    """规范化 settings.json 的供应商注册表（llm_providers 列表）。

    兼容旧配置迁移：
    - 旧 llm_profiles（ollama/custom 双档位）→ 播种为两个供应商
    - 顶层旧 base_url/model/api_key → 兜底播种一个供应商
    """
    providers = cfg.get("llm_providers")
    if not isinstance(providers, list):
        providers = []
    norm = []
    seen = set()
    for p in providers:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip() or ("prov-" + uuid.uuid4().hex[:8])
        if pid in seen:
            continue
        seen.add(pid)
        kind = str(p.get("kind") or "").strip()
        if kind not in LLM_PROVIDER_KINDS:
            base = str(p.get("base_url") or "").strip()
            kind = "ollama" if (":11434" in base or str(p.get("api_key") or "").lower() == "ollama") else "custom"
        norm.append({
            "id": pid,
            "name": str(p.get("name") or LLM_PROVIDER_KIND_LABELS.get(kind, kind)).strip(),
            "kind": kind,
            "base_url": str(p.get("base_url") or "").strip(),
            "api_key": str(p.get("api_key") or "").strip(),
            "default_model": str(p.get("default_model") or "").strip(),
            "vision": _norm_vision(p.get("vision")),
        })
    if not norm:
        profiles = cfg.get("llm_profiles")
        if isinstance(profiles, dict):
            for kind, prof in profiles.items():
                if not isinstance(prof, dict):
                    continue
                base = str(prof.get("base_url") or "").strip()
                model = str(prof.get("model") or "").strip()
                if not (base or model):
                    continue
                k = kind if kind in LLM_PROVIDER_KINDS else ("ollama" if ":11434" in base else "custom")
                # 迁移用稳定 id：每次 GET 都得到同一供应商 id，前端/卡片引用不会漂移
                pid = "prov-" + k
                while pid in seen:
                    pid = "prov-" + k + "-" + uuid.uuid4().hex[:6]
                seen.add(pid)
                norm.append({
                    "id": pid,
                    "name": LLM_PROVIDER_KIND_LABELS.get(k, k),
                    "kind": k,
                    "base_url": base,
                    "api_key": str(prof.get("api_key") or "").strip(),
                    "default_model": model,
                })
        if not norm:
            legacy_base = str(cfg.get("base_url") or "").strip()
            legacy_model = str(cfg.get("model") or "").strip()
            if legacy_base or legacy_model:
                k = "ollama" if (":11434" in legacy_base or str(cfg.get("api_key") or "").lower() == "ollama") else "custom"
                pid = "prov-" + k
                while pid in seen:
                    pid = "prov-" + k + "-" + uuid.uuid4().hex[:6]
                seen.add(pid)
                norm.append({
                    "id": pid,
                    "name": LLM_PROVIDER_KIND_LABELS.get(k, k),
                    "kind": k,
                    "base_url": legacy_base,
                    "api_key": str(cfg.get("api_key") or "").strip(),
                    "default_model": legacy_model,
                })
    # Ollama 本地 api_key 留空补占位符（Ollama 认证随意，但 codex_runner 要求非空）
    for p in norm:
        if p["kind"] == "ollama" and not p["api_key"]:
            p["api_key"] = "ollama"
    cfg["llm_providers"] = norm
    return norm
def _sync_llm_legacy_mirror(cfg: dict, provider: dict):
    """把激活供应商同步到旧式镜像字段，供 agent / codex / STT 等既有读取路径使用。

    - 顶层 base_url / model / api_key：agent.py 每次 reload 时读取生效字段
    - llm_profiles[kind] + llm_provider：codex_runner 等按档位回退读取
    """
    p = _llm_provider_meta(provider)
    kind = p["kind"]
    cfg.setdefault("llm_profiles", {})
    cfg["llm_profiles"][kind] = {
        "base_url": p["base_url"],
        "model": p["default_model"],
        "api_key": p["api_key"],
    }
    cfg["llm_provider"] = kind
    cfg["base_url"] = p["base_url"]
    cfg["model"] = p["default_model"]
    cfg["api_key"] = p["api_key"]


def _active_provider(cfg: dict) -> Optional[dict]:
    """返回当前激活供应商（llm_provider_id 优先；兼容旧 llm_provider kind）。"""
    providers = _ensure_llm_providers(cfg)
    if not providers:
        return None
    active_id = str(cfg.get("llm_provider_id") or "").strip()
    if active_id:
        for p in providers:
            if p["id"] == active_id:
                return p
    kind = str(cfg.get("llm_provider") or "").strip()
    if kind:
        for p in providers:
            if p["kind"] == kind:
                return p
    # 首次启动：激活第一个有 base_url 的供应商
    for p in providers:
        if p["base_url"]:
            return p
    return providers[0]


def _load_settings() -> dict:
    with open(BASE_DIR / "settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _save_settings(cfg: dict):
    with open(BASE_DIR / "settings.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def _reload_shared_agents() -> int:
    """热重载全部共享 Agent 的 LLM 客户端，切换/修改供应商即时生效。"""
    n = 0
    for ag in list(_shared_agents.values()):
        try:
            await ag.reload_llm_config()
            n += 1
        except Exception as e:
            logger.warning(f"重载 Agent LLM 配置失败: {e}")
    return n


def _providers_payload(cfg: dict) -> dict:
    """供应商列表 + 当前激活信息 + 温度（供前端回填）。"""
    providers = _ensure_llm_providers(cfg)
    active = _active_provider(cfg)
    providers_out = []
    for p in providers:
        item = _llm_provider_meta(p)
        item["_active"] = bool(active and p["id"] == active["id"])
        providers_out.append(item)
    return {
        "providers": providers_out,
        "active_id": active["id"] if active else "",
        "temperature": cfg.get("temperature", 0.2),
    }


@app.get("/api/llm/config")
async def llm_config_get():
    """获取 LLM 供应商概况：供应商列表 + 当前激活 + 全局温度 + 代理设置。"""
    try:
        cfg = _load_settings()
        payload = _providers_payload(cfg)
        payload["kind_labels"] = LLM_PROVIDER_KIND_LABELS
        payload["base_url"] = cfg.get("base_url", "")
        payload["model"] = cfg.get("model", "")
        payload["api_key"] = cfg.get("api_key", "")
        payload["llm_proxy"] = str(cfg.get("llm_proxy") or "").strip()
        payload["llm_proxy_options"] = LLM_PROXY_OPTIONS
        return payload
    except Exception as e:
        return {"providers": [], "active_id": "", "temperature": 0.2, "error": str(e)}


@app.post("/api/llm/config")
async def llm_config_set(payload: dict):
    """保存 LLM 全局配置：激活指定供应商 / 更新温度。

    payload（均可选）：
      provider_id: 激活该供应商（立即全局生效，热重载 Agent）
      provider:     兼容旧档位名 ollama|custom（激活对应 kind 的第一个供应商）
      temperature:  全局采样温度
      llm_proxy:    云端 API 网络代理：off（直连）/ auto（fq 代理自动拉起）/ 自定义地址
      providers:    兼容旧双档位 dict {kind: {base_url, model, api_key}} → 更新同 kind 供应商
    """
    cfg = _load_settings()
    providers = _ensure_llm_providers(cfg)
    temperature = payload.get("temperature")
    llm_proxy = payload.get("llm_proxy")
    if llm_proxy is not None:
        cfg["llm_proxy"] = str(llm_proxy).strip()
    legacy_incoming = payload.get("providers")
    if isinstance(legacy_incoming, dict):
        for kind, prof in legacy_incoming.items():
            if not isinstance(prof, dict) or kind not in LLM_PROVIDER_KINDS:
                continue
            target = next((p for p in providers if p["kind"] == kind), None)
            if target is None:
                target = {
                    "id": "prov-" + kind + "-" + uuid.uuid4().hex[:6],
                    "name": LLM_PROVIDER_KIND_LABELS.get(kind, kind),
                    "kind": kind,
                    "base_url": "",
                    "api_key": "",
                    "default_model": "",
                }
                providers.append(target)
            for k, field in (("base_url", "base_url"), ("model", "default_model"),
                             ("api_key", "api_key")):
                if k in prof and prof[k] is not None:
                    target[field] = str(prof[k]).strip()
        cfg["llm_providers"] = providers
    provider_id = str(payload.get("provider_id") or "").strip()
    if not provider_id:
        legacy_kind = str(payload.get("provider") or "").strip()
        if legacy_kind in LLM_PROVIDER_KINDS:
            t = next((p for p in providers if p["kind"] == legacy_kind), None)
            if t:
                provider_id = t["id"]
    if provider_id:
        target = next((p for p in providers if p["id"] == provider_id), None)
        if target is None:
            raise HTTPException(400, f"供应商不存在: {provider_id}")
        active = _active_provider(cfg)
        if not active or active["id"] != provider_id:
            # 激活该供应商：LLM 凭证写入全局（model 用卡片/默认模型）
            cfg["llm_provider_id"] = provider_id
            _sync_llm_legacy_mirror(cfg, target)
    if temperature is not None and temperature != "":
        try:
            cfg["temperature"] = round(max(0.0, min(2.0, float(temperature))), 2)
        except (TypeError, ValueError):
            pass
    if not cfg.get("llm_provider_id"):
        active = _active_provider(cfg)
        if active:
            cfg["llm_provider_id"] = active["id"]
            _sync_llm_legacy_mirror(cfg, active)
    _save_settings(cfg)
    reloaded = await _reload_shared_agents()
    out = _providers_payload(cfg)
    out["reloaded"] = reloaded

    active = _active_provider(cfg)
    logger.info(
        f"LLM 供应商已切换: {active['name'] if active else '?'} "
        f"model={cfg.get('model', '')} 重载Agent={reloaded}"
    )
    return out


@app.get("/api/llm/providers")
async def llm_providers_list():
    """供应商列表（全局资源）+ 当前激活 + 温度。"""
    try:
        return {"code": 200, **_providers_payload(_load_settings())}
    except Exception as e:
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)


@app.post("/api/llm/providers")
async def llm_providers_create(payload: dict):
    """新建供应商（全局资源）。body: {name, kind, base_url, api_key, default_model, activate?}"""
    kind = str(payload.get("kind") or "").strip() or "custom"
    if kind not in LLM_PROVIDER_KINDS:
        raise HTTPException(400, f"未知供应商类型: {kind}（可选 ollama/custom）")
    name = str(payload.get("name") or "").strip() or LLM_PROVIDER_KIND_LABELS.get(kind, kind)
    provider = {
        "id": "prov-" + uuid.uuid4().hex[:8],
        "name": name,
        "kind": kind,
        "base_url": str(payload.get("base_url") or "").strip(),
        "api_key": str(payload.get("api_key") or "").strip(),
        "default_model": str(payload.get("default_model") or "").strip(),
        "vision": _norm_vision(payload.get("vision")),
    }
    if provider["kind"] == "ollama" and not provider["api_key"]:
        provider["api_key"] = "ollama"
    cfg = _load_settings()
    providers = _ensure_llm_providers(cfg)
    providers.append(provider)
    cfg["llm_providers"] = providers
    active = _active_provider(cfg)
    should_activate = (not active) or bool(payload.get("activate"))
    if should_activate:
        # 新建即启用：把该供应商设为当前 LLM
        cfg["llm_provider_id"] = provider["id"]
        _sync_llm_legacy_mirror(cfg, provider)
    _save_settings(cfg)
    reloaded = await _reload_shared_agents() if should_activate else 0
    new_active = _active_provider(cfg)
    return {
        "ok": True,
        "provider": _llm_provider_meta(provider),
        "active_id": new_active["id"] if new_active else "",
        "reloaded": reloaded,
    }


@app.put("/api/llm/providers/{pid}")
async def llm_providers_update(pid: str, payload: dict):
    """更新供应商字段（名称/类型/地址/密钥/默认模型）。修改地址后需重新选择模型。"""
    cfg = _load_settings()
    providers = _ensure_llm_providers(cfg)
    target = next((p for p in providers if p["id"] == pid), None)
    if target is None:
        raise HTTPException(404, "供应商不存在")
    if "name" in payload and str(payload.get("name") or "").strip():
        target["name"] = str(payload["name"]).strip()
    if "kind" in payload:
        k = str(payload.get("kind") or "").strip()
        if k in LLM_PROVIDER_KINDS:
            target["kind"] = k
    for k, field in (("base_url", "base_url"), ("api_key", "api_key"),
                     ("default_model", "default_model")):
        if k in payload and payload[k] is not None:
            target[field] = str(payload[k]).strip()
    if "vision" in payload:
        target["vision"] = _norm_vision(payload.get("vision"))
    if target["kind"] == "ollama" and not target["api_key"]:
        target["api_key"] = "ollama"
    cfg["llm_providers"] = providers
    active = _active_provider(cfg)
    is_active = bool(active and active["id"] == pid)
    if is_active:
        # 激活中的供应商被修改 → 重新同步生效配置
        _sync_llm_legacy_mirror(cfg, target)
    _save_settings(cfg)
    reloaded = await _reload_shared_agents() if is_active else 0
    return {"ok": True, "provider": _llm_provider_meta(target), "reloaded": reloaded}


@app.delete("/api/llm/providers/{pid}")
async def llm_providers_delete(pid: str):
    """删除供应商。若删除的是当前激活供应商，自动激活剩余第一个。"""
    cfg = _load_settings()
    providers = _ensure_llm_providers(cfg)
    remain = [p for p in providers if p["id"] != pid]
    if len(remain) == len(providers):
        raise HTTPException(404, "供应商不存在")
    active = _active_provider(cfg)
    was_active = bool(active and active["id"] == pid)
    cfg["llm_providers"] = remain
    reloaded = 0
    if was_active or str(cfg.get("llm_provider_id") or "") == pid:
        cfg["llm_provider_id"] = ""
        nxt = _active_provider(cfg)
        if nxt:
            cfg["llm_provider_id"] = nxt["id"]
            _sync_llm_legacy_mirror(cfg, nxt)
            reloaded = await _reload_shared_agents()
    _save_settings(cfg)
    return {"ok": True, "active_id": cfg.get("llm_provider_id", ""), "reloaded": reloaded}


@app.post("/api/llm/providers/{pid}/activate")
async def llm_providers_activate(pid: str):
    """把某供应商设为当前使用（立即全局生效，热重载 Agent）。"""
    cfg = _load_settings()
    providers = _ensure_llm_providers(cfg)
    target = next((p for p in providers if p["id"] == pid), None)
    if target is None:
        raise HTTPException(404, "供应商不存在")
    cfg["llm_provider_id"] = pid
    _sync_llm_legacy_mirror(cfg, target)
    _save_settings(cfg)
    reloaded = await _reload_shared_agents()
    return {"ok": True, "provider": _llm_provider_meta(target), "active_id": pid, "reloaded": reloaded}


@app.post("/api/llm/providers/test")
async def llm_providers_test(payload: dict):
    """测试供应商连通性并拉取模型列表（不保存）。

    body: {kind, base_url, api_key}；也接受 provider_id 直接测已存供应商。
    """
    kind = str(payload.get("kind") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    pid = str(payload.get("provider_id") or "").strip()
    if pid:
        cfg = _load_settings()
        providers = _ensure_llm_providers(cfg)
        p = next((x for x in providers if x["id"] == pid), None)
        if p is None:
            return JSONResponse({"code": 404, "message": "供应商不存在"}, status_code=404)
        kind = p["kind"]
        base_url = base_url or p.get("base_url", "").strip()
        api_key = api_key or p.get("api_key", "").strip()
    if not base_url:
        return {"models": [], "error": "缺少 Base URL"}
    is_local_ollama = (":11434" in base_url or "localhost" in base_url or "127.0.0.1" in base_url)
    if not api_key:
        if is_local_ollama or kind == "ollama":
            api_key = "ollama"
        else:
            return {"models": [], "error": "缺少 API Key"}
    try:
        from openai import AsyncOpenAI
        import httpx as _httpx
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url,
            http_client=_httpx.AsyncClient(
                proxy=_cloud_proxy_for(base_url), trust_env=False,
                timeout=_httpx.Timeout(60.0, connect=30.0),
            ),
        )
        names = []
        async for m in client.models.list():
            names.append(m.id)
        return {"models": sorted(names)}
    except Exception as e:
        return {"models": [], "error": str(e)}


_NON_CHAT_MODEL_HINTS = (
    "embedding", "embeddings", "reranker", "rerank", "retriever",
    "bge-", "m3e", "gte-", "jina-embeddings",
    "image", "img", "ocr", "asr", "captioner", "whisper", "speech",
    "tts", "audio", "voice", "kolors", "cogview", "flux",
    "stable-diffusion", "text2img", "text-to-image", "wan",
)


def _is_chat_model(model_id: str) -> bool:
    """粗略判断某个模型 id 是否可用于聊天补全（过滤嵌入/重排/图像/OCR/ASR/TTS 等）。"""
    mid = (model_id or "").lower()
    return not any(h in mid for h in _NON_CHAT_MODEL_HINTS)


@app.get("/api/llm/models")
async def llm_models_list(provider: str = "", base_url: str = "", api_key: str = ""):
    """严格按指定供应商加载可用模型列表（OpenAI 兼容 /models）。

    - provider=供应商id：只从该供应商的 base_url 拉取，绝不串用其它供应商配置
      （修复"选了 Ollama 本地却加载自定义 API 模型"的串配置问题）
    - 旧调用方式（显式 base_url + api_key）：仅当未传 provider 时按显式地址拉取
    - 两者都缺 → 报错提示，绝不回退到全局 base_url（那正是串配置的根源）
    """
    pid = (provider or "").strip()
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    if pid:
        cfg = _load_settings()
        providers = _ensure_llm_providers(cfg)
        p = next((x for x in providers if x["id"] == pid), None)
        if p is None:
            return {"models": [], "error": f"供应商不存在: {pid}"}
        base_url = p.get("base_url", "").strip()
        api_key = p.get("api_key", "").strip()
        if not base_url:
            return {"models": [], "error": "该供应商未配置 Base URL，请先在「供应商」里补全"}
    if not base_url:
        return {"models": [], "error": "缺少 base_url：请先选择/配置供应商"}
    is_local_ollama = (":11434" in base_url or "localhost" in base_url or "127.0.0.1" in base_url)
    if not api_key:
        if is_local_ollama:
            api_key = "ollama"
        else:
            return {"models": [], "error": "缺少 api_key"}
    try:
        from openai import AsyncOpenAI
        import httpx as _httpx
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url,
            http_client=_httpx.AsyncClient(
                proxy=_cloud_proxy_for(base_url), trust_env=False,
                timeout=_httpx.Timeout(120.0, connect=30.0),
            ),
        )
        names = []
        async for m in client.models.list():
            names.append(m.id)
        names = sorted(names)
        return {
            # 只列出可用于对话的模型，避免在角色卡片里选中嵌入/图像/OCR 等
            # 非聊天模型后，保存配置导致聊天 400「Model does not exist」。
            "models": [m for m in names if _is_chat_model(m)],
            "all_models": names,
        }
    except Exception as e:
        return {"models": [], "error": str(e)}

# ---------- 工作区（全局工作目录：DSH / Codex / OpenCode / shell 共用） ----------
def _workspace_current() -> dict:
    """读取当前工作区：codex_config.json 的 work_dir 为准，兼读桥接 cwd。"""
    cwd = ""
    try:
        with open(BASE_DIR / "codex_config.json", "r", encoding="utf-8") as f:
            cj = json.load(f)
        cwd = str((cj.get("agent") or {}).get("work_dir") or "").strip()
    except Exception:
        pass
    bridge_cwd = ""
    try:
        with open(BASE_DIR / "harness_bridge.json", "r", encoding="utf-8") as f:
            bj = json.load(f)
        bridge_cwd = str(bj.get("cwd") or "").strip()
    except Exception:
        pass
    return {
        "cwd": cwd or bridge_cwd or str(Path.home()),
        "codex_work_dir": cwd,
        "bridge_cwd": bridge_cwd,
    }


@app.get("/api/workspace")
async def workspace_get():
    """获取当前工作区路径（持久化在 codex_config.json / harness_bridge.json）。"""
    return _workspace_current()


@app.post("/api/workspace")
async def workspace_set(payload: dict):
    """设置全局工作区：写入 codex_config.json 的 work_dir + harness_bridge.json 的 cwd，
    并运行时同步 EXECUTOR/桥接配置 —— DSH、Codex、OpenCode、shell 全部围绕新工作区执行。
    """
    path = (payload.get("path") or payload.get("dir") or "").strip()
    if not path:
        raise HTTPException(400, "缺少 path")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise HTTPException(404, f"目录不存在: {path}")
    # 1) codex_config.json → agent.work_dir（Codex / OpenCode / shell EXECUTOR 的工作目录）
    try:
        cpath = BASE_DIR / "codex_config.json"
        with open(cpath, "r", encoding="utf-8") as f:
            cc = json.load(f)
        cc.setdefault("agent", {})["work_dir"] = path
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(cc, f, ensure_ascii=False, indent=2)
        codex_runner.reload_relay_config()      # 热重载 AGENT_CFG + EXECUTOR.cwd
        codex_runner.EXECUTOR.cwd = path        # 双保险：直接同步执行器
    except Exception as e:
        logger.warning(f"写入 codex_config.json 失败: {e}")
        raise HTTPException(500, f"写入 codex 配置失败: {e}")
    # 2) harness_bridge.json → cwd（DSH 智能体任务工作目录）
    try:
        bpath = BASE_DIR / "harness_bridge.json"
        with open(bpath, "r", encoding="utf-8") as f:
            hb = json.load(f)
        hb["cwd"] = path
        with open(bpath, "w", encoding="utf-8") as f:
            json.dump(hb, f, ensure_ascii=False, indent=2)
        # 运行时同步桥接实例配置（避免单例缓存旧 cwd）
        try:
            from harness_bridge import get_bridge as _gb_ws
            _bridge_ws = _gb_ws()
            if hasattr(_bridge_ws, "_file_cfg"):
                _bridge_ws._file_cfg["cwd"] = path
            if hasattr(_bridge_ws, "_session_ids"):
                # 新工作区映射新会话，避免复用旧目录会话
                _bridge_ws._session_ids = {}
        except Exception as e:
            logger.warning(f"运行时刷新 DSH 桥接 cwd 失败: {e}")
    except Exception as e:
        logger.warning(f"写入 harness_bridge.json 失败: {e}")
        raise HTTPException(500, f"写入桥接配置失败: {e}")
    logger.info(f"工作区已切换: {path}")
    return {"ok": True, **_workspace_current()}


# 常用目录（kind 交给 platform_compat 按平台解析：Windows 英文名 / POSIX XDG + 本地化名）
_USER_DIR_KINDS = (
    ("desktop", "桌面"), ("downloads", "下载"), ("documents", "文档"),
    ("videos", "视频"), ("music", "音乐"), ("pictures", "图片"),
)


@app.get("/api/workspace/roots")
async def workspace_roots():
    """列出可选工作区根目录（用户常用目录 + 盘符/挂载点，手机端也可直接选；跨平台）。"""
    roots = []
    for kind, label in _USER_DIR_KINDS:
        p = pc.user_dir(kind)
        if p is not None and p.is_dir():
            roots.append({"path": str(p), "label": label})
    for path, label in pc.browse_roots():
        roots.append({"path": path, "label": label})
    cur = _workspace_current().get("cwd") or ""
    if cur and cur not in [r["path"] for r in roots]:
        roots.insert(0, {"path": cur, "label": "当前工作区"})
    seen = set()
    out = []
    for r in roots:
        if r["path"] not in seen:
            seen.add(r["path"]); 
            out.append(r)
    return {"roots": out}


@app.get("/api/workspace/list")
async def workspace_list(path: str = ""):
    """列出指定目录下的子目录（供手机端逐级下钻浏览目录）。

    - path 为空 → 返回盘符/常用根目录（与 /api/workspace/roots 等价）
    - 返回 {"path", "parent", "dirs": [{"name", "path"}, ...]}，dirs 按名称排序
    - 权限不足/不存在 → 返回 {"path":..., "parent":..., "dirs": [], "error": ...} 不抛异常
    """
    def _readable_dirs(base: str) -> list:
        out = []
        try:
            for entry in os.scandir(base):
                try:
                    if entry.is_dir(follow_symlinks=True):
                        name = entry.name
                        # 过滤系统/隐藏目录，减少手机端噪音
                        if name.startswith(".") or name.startswith("$"):
                            continue
                        if name.lower() in ("windows", "system volume information", "recycler",
                                            "$recycle.bin", "program files", "program files (x86)",
                                            "node_modules", "__pycache__", "appdata"):
                            continue
                        out.append({"name": name, "path": entry.path})
                except OSError:
                    continue
        except OSError as e:
            return [], str(e)
        out.sort(key=lambda x: x["name"].lower())
        return out, None

    base = (path or "").strip()
    if not base:
        roots = []
        for kind, label in _USER_DIR_KINDS:
            q = pc.user_dir(kind)
            if q is not None and q.is_dir():
                roots.append({"name": label, "path": str(q)})
        for p, label in pc.browse_roots():
            roots.append({"name": label, "path": p})
        return {"path": "", "parent": "", "dirs": roots}

    base = os.path.abspath(os.path.expanduser(base))
    if not os.path.isdir(base):
        return {"path": base, "parent": "", "dirs": [], "error": "目录不存在"}
    dirs, err = _readable_dirs(base)
    parent = os.path.dirname(base) if os.path.dirname(base) != base else ""
    return {"path": base, "parent": parent, "dirs": dirs, "error": err}

# 已保存工作区列表（多工作区收藏，独立持久化文件 workspace_saved.json）
SAVED_WS_FILE = BASE_DIR / "workspace_saved.json"


def _path_platform_valid(p: str) -> bool:
    """路径是否属于当前平台（过滤跨平台残留：Linux 上剔除 Windows 盘符路径，反之亦然）。"""
    p = (p or "").strip()
    if not p:
        return False
    if os.name == "nt":
        # Windows：剔除 POSIX 风格路径（/home/...、/mnt/...）
        if p.startswith("/"):
            return False
    else:
        # POSIX：剔除 Windows 盘符路径（C:\...、D:/...、含反斜杠）
        if re.match(r"^[A-Za-z]:[\\/]", p) or "\\" in p:
            return False
    return True


def _load_saved_workspaces() -> list:
    try:
        with open(SAVED_WS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        arr = data.get("workspaces") if isinstance(data, dict) else data
        if isinstance(arr, list):
            return [str(x.get("path", "")).strip() for x in arr
                    if isinstance(x, dict) and str(x.get("path", "")).strip()
                    and _path_platform_valid(str(x.get("path", "")))]
    except Exception:
        pass
    return []

def _save_saved_workspaces(paths: list) -> None:
    arr = [{"path": p} for p in paths]
    try:
        with open(SAVED_WS_FILE, "w", encoding="utf-8") as f:
            json.dump({"workspaces": arr}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存工作区列表失败: {e}")


@app.get("/api/workspaces")
async def workspaces_list():
    """获取已保存的工作区列表 + 当前激活工作区。"""
    saved = _load_saved_workspaces()
    # 已保存但当前目录已不存在的，标记但保留（用户可删除）
    items = []
    for p in saved:
        items.append({"path": p, "exists": os.path.isdir(p)})
    return {"workspaces": items, "current": _workspace_current().get("cwd") or ""}


@app.post("/api/workspaces")
async def workspaces_add(payload: dict):
    """把指定路径添加进已保存工作区列表（去重）。不切换当前工作区。"""
    path = (payload.get("path") or payload.get("dir") or "").strip()
    if not path:
        raise HTTPException(400, "缺少 path")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise HTTPException(404, f"目录不存在: {path}")
    saved = _load_saved_workspaces()
    if path not in saved:
        saved.insert(0, path)
        _save_saved_workspaces(saved)
    return {"ok": True, "path": path, "workspaces": saved}


@app.delete("/api/workspaces")
async def workspaces_remove(payload: dict):
    """从已保存列表移除指定路径（不删磁盘目录，只是不再收藏）。"""
    path = (payload.get("path") or payload.get("dir") or "").strip()
    if not path:
        raise HTTPException(400, "缺少 path")
    path = os.path.abspath(os.path.expanduser(path))
    saved = _load_saved_workspaces()
    if path in saved:
        saved = [p for p in saved if p != path]
        _save_saved_workspaces(saved)
    return {"ok": True, "workspaces": saved}


@app.post("/api/workspaces/{path:path}/activate")
async def workspaces_activate(path: str):
    """激活已保存的工作区（与 /api/workspace 设置当前一致，所有智能体围绕它执行）。"""
    return await workspace_set({"path": path})


@app.get("/api/character_cards")
async def character_cards_list(request: Request):
    """获取所有角色卡片 + 当前用户生效的卡片（按人隔离：各人各自一份）。"""
    active_id = ""
    try:
        active_id = active_role_card_id(_card_uid(request))
    except Exception:
        pass
    return {"cards": _load_character_cards(), "active_id": active_id}


@app.get("/api/config/role")
async def role_config_get():
    """获取当前角色配置（角色名 + 系统提示词），供编辑表单回填。"""
    try:
        cfg = load_config()
        return {
            "role_name": cfg.get("role_name", "AI助手"),
            "system_prompt": cfg.get("system_prompt", ""),
        }
    except Exception as e:
        return {"role_name": "AI助手", "system_prompt": "", "error": str(e)}


@app.get("/api/config/tools")
async def tools_config_get():
    """获取当前工具配置（是否启用 + 白名单），供角色卡片编辑表单回填。"""
    try:
        cfg = load_config()
        agent_cfg = cfg.get("agent", {})
        return {
            "enable_tools": bool(agent_cfg.get("enable_tools", True)),
            "allowed_tools": agent_cfg.get("allowed_tools", []) or [],
        }
    except Exception as e:
        return {"enable_tools": True, "allowed_tools": [], "error": str(e)}


@app.get("/api/tools")
async def tools_list():
    """返回当前所有可用工具（本地 + harness 技能/插件），供角色卡片配置工具白名单。"""
    tools = get_available_tools()
    # 补充 agent 已加载的工具（本地 + harness 技能/插件，全部 skill 化）
    try:
        ag = await get_shared_agent("default")
        seen = {t["name"] for t in tools}
        for t in getattr(ag, "_all_tools", []) or []:
            fn = t.get("function", {})
            name = fn.get("name", "")
            if name and name not in seen:
                seen.add(name)
                tools.append({
                    "name": name,
                    "description": fn.get("description", ""),
                    "source": "skill",
                })
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
    return {"tools": tools}


def _normalize_card_tts(payload_tts: dict) -> dict:
    """归一化卡片 TTS 字段，缺省沿用当前全局 TTS 配置。

    API 供应商 TTS（engine=api）的地址/密钥/模型/音色也是卡片级应用配置。
    """
    return {
        "engine": payload_tts.get("engine", tts_config.get("engine", "edge_tts")),
        "edge_voice": payload_tts.get("edge_voice", tts_config.get("edge_voice", "")) or DEFAULT_VOICE,
        "edge_rate": payload_tts.get("edge_rate", tts_config.get("edge_rate", "+8%")),
        "gptsovits_url": payload_tts.get("gptsovits_url", tts_config.get("gptsovits_url", "")),
        "gptsovits_ref_audio": payload_tts.get("gptsovits_ref_audio", tts_config.get("gptsovits_ref_audio", "")),
        "gptsovits_character": payload_tts.get("gptsovits_character", tts_config.get("gptsovits_character", "")),
        "api_url": payload_tts.get("api_url", tts_config.get("api_url", "")),
        "api_key": payload_tts.get("api_key", tts_config.get("api_key", "")),
        "api_model": payload_tts.get("api_model", tts_config.get("api_model", "")),
        "api_voice": payload_tts.get("api_voice", tts_config.get("api_voice", "")),
    }


def _normalize_card_tools(payload_tools: dict) -> dict:
    """归一化卡片工具配置：是否启用 + 工具白名单（空列表=全部可用）。"""
    return {
        "enabled": bool(payload_tools.get("enabled", True)),
        "allowed": list(payload_tools.get("allowed", []) or []),
    }


def _normalize_card_animations(payload_animations: dict) -> dict:
    """归一化卡片专属动作配置：是否启用 + 允许的动作名列表（空列表=全部动作）。

    未配置（None/缺失）时返回 None，前端按"未配置 → 执行所有动作"处理。
    """
    if not isinstance(payload_animations, dict):
        return None
    return {
        "enabled": bool(payload_animations.get("enabled", True)),
        "allowed": list(payload_animations.get("allowed", []) or []),
    }


def _normalize_card_llm(payload_llm: dict) -> dict:
    """归一化卡片 LLM 字段：卡片只存「供应商 + 模型名 + 温度」。

    供应商的 base_url/api_key 统一取自全局供应商注册表（全球资源），卡片不再各自
    存一套地址——彻底避免"Ollama 本地串用自定义 API 配置"。

    兼容旧字段：provider / base_url / api_key 若传入则原样保留（旧卡迁移信息，
    应用时按 base_url 匹配供应商）；前端新保存时只写 provider_id / model / temperature / vision。

    vision 是卡片级读图三态：卡片绑的模型跟供应商的 default_model 常常不是同一个，
    只有卡片自己知道「这张卡在跑的那个模型吃不吃图」。
    """
    payload_llm = payload_llm or {}
    temperature = payload_llm.get("temperature")
    if temperature is None or temperature == "":
        temperature = None
    else:
        try:
            temperature = round(max(0.0, min(2.0, float(temperature))), 2)
        except (TypeError, ValueError):
            temperature = None
    out = {
        "provider_id": (payload_llm.get("provider_id") or "").strip(),
        "model": (payload_llm.get("model") or "").strip(),
        "temperature": temperature,
        "vision": _norm_vision(payload_llm.get("vision")),
    }
    # 卡片一旦声明了供应商（provider_id），就不再携带自己的 base_url/api_key，
    # 应用时统一从全局供应商注册表取地址/密钥；否则旧卡遗留的迁移字段会被一直带下去，
    # 造成“选了新供应商却残留旧地址/旧密钥”的串配置。
    if not out["provider_id"]:
        for legacy in ("provider", "base_url", "api_key"):
            v = payload_llm.get(legacy)
            if isinstance(v, str) and v.strip():
                out[legacy] = v.strip()
    return out


@app.post("/api/character_cards")
async def character_cards_create(payload: dict):
    """创建角色卡片。"""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "卡片名称不能为空")
    card = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "role_name": (payload.get("role_name") or "").strip(),
        "user_name": (payload.get("user_name") or "").strip(),
        "model_url": payload.get("model_url", ""),
        "model_name": payload.get("model_name", ""),
        "system_prompt": payload.get("system_prompt", ""),
        "tts": _normalize_card_tts(payload.get("tts", {})),
        "tools": _normalize_card_tools(payload.get("tools", {})),
        "animations": _normalize_card_animations(payload.get("animations")),
        "llm": _normalize_card_llm(payload.get("llm", {})),
        "created_at": int(time.time()),
    }
    cards = _load_character_cards()
    cards.insert(0, card)
    _save_character_cards(cards)
    return {"ok": True, "card": card}


@app.put("/api/character_cards/{card_id}")
async def character_cards_update(card_id: str, payload: dict):
    """更新角色卡片（局部字段）。"""
    cards = _load_character_cards()
    for card in cards:
        if card.get("id") != card_id:
            continue
        if "name" in payload:
            card["name"] = (payload.get("name") or "").strip()
        if "role_name" in payload:
            card["role_name"] = (payload.get("role_name") or "").strip()
        if "user_name" in payload:
            card["user_name"] = (payload.get("user_name") or "").strip()
        if "model_url" in payload:
            card["model_url"] = payload.get("model_url", "")
        if "model_name" in payload:
            card["model_name"] = payload.get("model_name", "")
        if "system_prompt" in payload:
            card["system_prompt"] = payload.get("system_prompt", "")
        if "tts" in payload:
            card["tts"] = _normalize_card_tts({**card.get("tts", {}), **payload["tts"]})
        if "tools" in payload:
            card["tools"] = _normalize_card_tools({**card.get("tools", {}), **payload["tools"]})
        if "animations" in payload:
            card["animations"] = _normalize_card_animations(payload["animations"])
        if "llm" in payload:
            card["llm"] = _normalize_card_llm({**card.get("llm", {}), **payload["llm"]})
        _save_character_cards(cards)
        return {"ok": True, "card": card}
    raise HTTPException(404, "卡片不存在")


@app.delete("/api/character_cards/{card_id}")
async def character_cards_delete(card_id: str):
    """删除角色卡片。"""
    cards = _load_character_cards()
    remain = [c for c in cards if c.get("id") != card_id]
    if len(remain) == len(cards):
        raise HTTPException(404, "卡片不存在")
    _save_character_cards(remain)
    # 用这张卡的人一并回落默认，否则指针指向一张已不存在的卡
    try:
        users = load_card_users()
        holders = [u for u, cid in users.items() if cid == card_id]
        if holders:
            for u in holders:
                users.pop(u, None)
            save_card_users(users)
    except Exception as e:
        logger.warning(f"清除用户角色卡片指针失败: {e}")
    return {"ok": True}


@app.get("/api/vision/check")
async def api_vision_check(provider_id: str = "", model: str = "", override: str = ""):
    """查一个模型能不能看见注入的截图。

    角色卡编辑界面用它把「自动判定」摊开显示，省得用户猜判成了什么。
    """
    try:
        cfg = _load_settings()
        providers = _ensure_llm_providers(cfg)
        pid = (provider_id or "").strip()
        prov = next((p for p in providers if p["id"] == pid), None) if pid else None
        if prov is None:
            prov = _active_provider(cfg)
        m = (model or "").strip() or str((prov or {}).get("default_model") or "").strip()
        ov = _norm_vision(override) if (override or "").strip() else None
        from harness.vision_probe import supports_vision
        ok, why = supports_vision(m, prov, ov)
        return {"code": 200, "ok": ok, "why": why, "model": m}
    except Exception as e:
        return {"code": 200, "ok": False, "why": f"判定失败：{e}", "model": model}



@app.post("/api/character_cards/{card_id}/apply")
async def character_cards_apply(card_id: str, request: Request):
    """应用角色卡片（按人隔离）：只把该用户的指针指向这张卡。

    卡片是数据源——人设 / 声音 / 模型 / LLM 全在解析时按 uid 取（agent.role_card_*）。
    全局 settings.json / tts_config.json 退居「无卡片时的兜底」，切卡不再改写它们：
    否则任一登录用户点一下卡片，全机（含主人）的人设、音色、模型都会被换掉。
    """
    cards = _load_character_cards()
    card = next((c for c in cards if c.get("id") == card_id), None)
    if not card:
        raise HTTPException(404, "卡片不存在")

    try:
        _body = await request.json()
    except Exception:
        _body = None
    uid = _card_uid(request, _body if isinstance(_body, dict) else None)
    try:
        users = load_card_users()
        users[uid] = card_id
        save_card_users(users)
    except Exception as e:
        logger.warning(f"保存用户角色卡片失败: {e}")
        raise HTTPException(500, "保存角色卡片失败")

    # 该用户的 Agent 立刻切到这张卡：重建 LLM 客户端（卡片 llm 声明覆盖全局）
    # + 绑定该卡片的记忆空间，不等重启。
    session_id = None
    try:
        agent = await get_shared_agent(uid)
        await agent.reload_llm_config()
        session_id = await agent.set_role_card_namespace(card_id)
    except Exception as e:
        logger.warning(f"切换角色卡片记忆空间失败: {e}")
    logger.info(f"应用角色卡片: {card.get('name')} (uid={uid})")
    return {
        "ok": True,
        "card": card,
        "model_url": card.get("model_url", ""),
        "model_name": card.get("model_name", ""),
        "session_id": session_id,
    }


@app.get("/api/models")
async def list_models():
    """列出所有已上传的 3D 模型"""
    models = []
    for f in MODELS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_MODEL_EXTS:
            stat = f.stat()
            models.append({
                "name": f.name,
                "url": f"/models/{f.name}",
                "size": stat.st_size,
                "type": f.suffix.lower().lstrip("."),
                "mtime": int(stat.st_mtime),
            })
    models.sort(key=lambda m: m["mtime"], reverse=True)
    return {"models": models}


# ---------- 聊天附件上传（图片 / 文档 / 任意文件） ----------
# 用户发来的文件落盘到 data/uploads/<uid|local>/<YYYYMMDD>/<sha1_16><ext>，
# 前端拿绝对路径 + URL，随消息经 WS 的 attachments 字段回传。
# 图片在 agent 侧走 [[IMG:]] 通道进多模态请求体（模型直接看像素）；
# 其余类型只给路径，由工具按需读——不做格式解析，任何类型都能发。
UPLOAD_MAX_BYTES = 30 * 1024 * 1024
UPLOAD_MAX_FILES = 8
_UPLOAD_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
# 这些扩展名存进来就是同源可执行页面（/uploads/x.html 打开即跑任意 JS），
# 静态挂载没法按文件加 CSP，只能在入口拒掉；要发就打包成 zip。
_UPLOAD_BLOCK_EXT = {".html", ".htm", ".xhtml", ".svg", ".js", ".mjs", ".ts", ".jsx", ".tsx"}


# 内联预算：能转文本的附件直接进上下文，模型当轮就能处理，不用先调工具读一遍。
# 但要卡住上限——一份 200 页 PDF 塞进上下文会把对话直接撑爆。
_ATTACH_INLINE_CHARS = 16000
_ATTACH_TOTAL_CHARS = 40000


def _attachments_prompt(attachments) -> str:
    """附件 → 给模型看的文本：图片带 [[IMG:]] 标记（agent 注入像素），
    文本/PDF/Office 直接内联正文，其余给路径由工具按需读。

    只接受落在 UPLOADS_DIR 内的路径：WS 消息是客户端可控输入，
    不校验就等于开了一条「让模型去读服务器任意文件」的通道。
    """
    if not isinstance(attachments, list):
        return ""
    lines, imgs, budget = [], 0, _ATTACH_TOTAL_CHARS
    for a in attachments[:UPLOAD_MAX_FILES]:
        raw = a.get("path") if isinstance(a, dict) else a
        if not raw:
            continue
        try:
            real = Path(str(raw)).resolve()
            real.relative_to(UPLOADS_DIR.resolve())
        except Exception:
            logger.warning(f"[Upload] 拒绝越界附件路径: {raw}")
            continue
        if not real.is_file():
            continue
        name = str((a.get("name") if isinstance(a, dict) else "") or real.name)
        size = real.stat().st_size
        if real.suffix.lower() in _UPLOAD_IMAGE_EXT:
            imgs += 1
            lines.append(f"[图片{imgs}：{name}] [[IMG:{real}]]")
            continue
        if budget > 0:
            body, why = attach_text.extract(real, _ATTACH_INLINE_CHARS)
            if body is not None:
                body, cut = attach_text.clamp(body, min(_ATTACH_INLINE_CHARS, budget))
                budget -= len(body)
                tail = (f"\n…（只给了前 {len(body)} 字符，需要更多用 read_file 读 {real}）"
                        if cut else "")
                lines.append(f"[附件：{name}，{size} 字节，{why}，路径 {real}]\n```\n{body}\n```{tail}")
                continue
        else:
            why = "本轮内联额度已用完"
        lines.append(f"[附件：{name}，{size} 字节，{why}，路径 {real}"
                     f"——要看内容用 read_file / code_read 打开]")
    if not lines:
        return ""
    return "【我上传了文件】\n" + "\n".join(lines)


_ATTACH_BLOCK_HEAD = "【我上传了文件】"
_IMG_LINE_RE = re.compile(r"^\[图片\d+：(.+?)\]\s*\[\[IMG:(.+?)\]\]$")
_FILE_LINE_RE = re.compile(r"^\[附件：(.+?)，(\d+) 字节，(.+?)\](.*)$")
_CUT_TAIL_RE = re.compile(r"^…（只给了前 \d+ 字符，需要更多用 read_file 读 (.+?)）$")


def _uploads_url(path) -> str:
    """上传文件绝对路径 → 浏览器 URL；不在 UPLOADS_DIR 内一律给空串。

    历史文本里的路径虽然是落盘时校验过的，但渲染成 <a href> 之前必须再校验一次：
    脏数据/手工构造的历史不该把任意本地路径变成可下载链接。
    """
    try:
        rel = Path(str(path)).resolve().relative_to(UPLOADS_DIR.resolve())
    except Exception:
        return ""
    return "/uploads/" + rel.as_posix()


def _split_attach_block(text):
    """把「给模型看的」附件块从用户消息里剥出来 → (展示文本, 附件元数据)。

    同一条 user 文本要服务两个相反的诉求：模型要 [[IMG:]] 绝对路径和文档正文，
    气泡要缩略图网格和文件卡片。存进历史的只能是前者，所以渲染时在这里反解一次——
    历史里的老消息（块已经连着正文存进库了）也一并受益，不必迁移数据。
    解不出来就原样返回：宁可难看，也不能把用户说的话弄丢。
    """
    if not isinstance(text, str) or _ATTACH_BLOCK_HEAD not in text:
        return text, []
    head = text.find(_ATTACH_BLOCK_HEAD)
    display = text[:head].rstrip()
    lines = text[head + len(_ATTACH_BLOCK_HEAD):].split("\n")
    atts, i = [], 0
    while i < len(lines):
        ln = lines[i].strip()
        m = _IMG_LINE_RE.match(ln)
        if m:
            atts.append({"name": m.group(1), "url": _uploads_url(m.group(2)),
                         "kind": "image"})
            i += 1
            continue
        m = _FILE_LINE_RE.match(ln)
        if not m:
            i += 1
            continue
        name, size, why, _rest = m.groups()
        real = ""
        if "，路径 " in why:          # 路径写在头行里（只给路径的、以及新格式的内联附件）
            why, real = why.split("，路径 ", 1)
            real = real.split("——")[0].strip()
        i += 1
        # 内联正文：连 ``` 围栏和「只给了前 N 字符」尾注一起跳过——正文是喂模型的，
        # 进了气泡就是一屏代码块；顺带防止正文里恰好出现的「[附件：…]」行被误判成附件
        if i < len(lines) and lines[i].startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                i += 1
            i += 1
            if i < len(lines):
                mt = _CUT_TAIL_RE.match(lines[i].strip())
                if mt:
                    real = real or mt.group(1).strip()
                    i += 1
        atts.append({"name": name, "size": int(size),
                     "url": _uploads_url(real), "kind": "file"})
    if not atts:
        return text, []      # 用户自己打了「【我上传了文件】」这几个字：别当成块切掉
    return display, atts


def _history_for_client(history):
    """历史轮次 → 前端渲染用：附件块从 user 文本剥掉，改挂 atts 元数据。

    只读不改：history 同时是连接级短期记忆（喂模型用），原地改写会污染模型上下文。
    """
    out = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        item = dict(h)
        user, atts = _split_attach_block(h.get("user") or "")
        item["user"] = user
        if atts:
            item["atts"] = atts
        out.append(item)
    return out


@app.post("/api/upload")
async def upload_attachments(request: Request, files: List[UploadFile] = File(...)):
    """聊天附件上传：图片/文档/任意文件，返回绝对路径与可访问 URL。"""
    if not files:
        raise HTTPException(400, "没有文件")
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(400, f"一次最多 {UPLOAD_MAX_FILES} 个文件")
    uid = _store_uid(request) or "local"
    target_dir = UPLOADS_DIR / uid / time.strftime("%Y%m%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        raw_name = Path(f.filename or "file").name
        ext = Path(raw_name).suffix.lower()
        if ext in _UPLOAD_BLOCK_EXT:
            raise HTTPException(400, f"出于安全考虑不支持 {ext}（可压缩成 zip 再发）")
        data = await f.read()
        if not data:
            continue
        if len(data) > UPLOAD_MAX_BYTES:
            raise HTTPException(
                413, f"{raw_name} 过大（{len(data) // 1024 // 1024}MB），"
                     f"上限 {UPLOAD_MAX_BYTES // 1024 // 1024}MB")
        # 内容哈希做文件名：同名不同内容不互相覆盖，重发同一张也不堆垃圾
        target = target_dir / f"{hashlib.sha1(data).hexdigest()[:16]}{ext}"
        if not target.exists():
            target.write_bytes(data)
        out.append({
            "name": raw_name,
            "path": str(target),
            "url": f"/uploads/{uid}/{target_dir.name}/{target.name}",
            "size": len(data),
            "ext": ext.lstrip("."),
            "kind": "image" if ext in _UPLOAD_IMAGE_EXT else "file",
        })
    if not out:
        raise HTTPException(400, "文件为空")
    logger.info(f"[Upload] {len(out)} 个附件 → {target_dir}")
    return {"ok": True, "files": out}


@app.post("/api/model/upload")
async def upload_model(file: UploadFile = File(...)):
    """上传 3D 模型文件 (glb/gltf/vrm)"""
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, f"不支持的格式 {ext}，仅支持 {', '.join(ALLOWED_MODEL_EXTS)}")

    # 读取内容并检查大小
    data = await file.read()
    if len(data) > MAX_MODEL_SIZE:
        raise HTTPException(413, f"文件过大 ({len(data)//1024//1024}MB)，上限 {MAX_MODEL_SIZE//1024//1024}MB")

    # 重命名避免覆盖与冲突：保留原名但加时间戳前缀
    safe_name = re.sub(r"[^\w.\-]", "_", file.filename)
    target = MODELS_DIR / f"{int(asyncio.get_event_loop().time() * 1000)}_{safe_name}"
    target.write_bytes(data)
    return {
        "name": target.name,
        "url": f"/models/{target.name}",
        "size": len(data),
        "type": ext.lstrip("."),
    }


@app.delete("/api/model/{name}")
async def delete_model(name: str):
    """删除指定模型"""
    # 防路径穿越
    safe = Path(name).name
    target = MODELS_DIR / safe
    if not target.is_file():
        raise HTTPException(404, "模型不存在")
    if target.suffix.lower() not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, "非模型文件")
    target.unlink()
    return {"deleted": safe}


@app.put("/api/model/{name}/rename")
async def rename_model(name: str, payload: dict):
    """重命名指定模型（保留扩展名）"""
    new_name = (payload.get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(400, "新名称不能为空")
    # 防路径穿越
    safe_old = Path(name).name
    target = MODELS_DIR / safe_old
    if not target.is_file():
        raise HTTPException(404, "模型不存在")
    if target.suffix.lower() not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, "非模型文件")
    # 保留原扩展名
    old_suffix = target.suffix
    safe_new = re.sub(r"[^\w.\-]", "_", new_name)
    if not safe_new.lower().endswith(old_suffix.lower()):
        safe_new += old_suffix
    new_path = MODELS_DIR / safe_new
    if new_path.exists():
        raise HTTPException(409, "同名文件已存在")
    target.rename(new_path)
    return {"old_name": safe_old, "new_name": safe_new, "url": f"/models/{safe_new}"}


# ---------- 3D 背景模型管理 ----------
@app.get("/api/backgrounds")
async def list_backgrounds():
    """列出所有可用的 3D 背景场景（含默认背景 'default' 星空，无文件路径）"""
    items = [{
        'name': 'default',
        'display_name': '默认背景 · 星空',
        'url': 'default',
        'size': 0,
        'type': 'default',
        'is_default': True,
    }]
    for f in BACKGROUNDS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_MODEL_EXTS:
            stat = f.stat()
            items.append({
                "name": f.name,
                "url": f"/backgrounds/{f.name}",
                "size": stat.st_size,
                "type": f.suffix.lower().lstrip("."),
                "is_default": False,
                "mtime": int(stat.st_mtime),
            })
    files = sorted(items[1:], key=lambda m: m["mtime"], reverse=True)
    return {"backgrounds": items[:1] + files}


@app.post("/api/background/upload")
async def upload_background(file: UploadFile = File(...)):
    """上传 3D 背景模型文件 (glb/gltf/vrm)"""
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, f"不支持的格式 {ext}，仅支持 {', '.join(ALLOWED_MODEL_EXTS)}")

    data = await file.read()
    if len(data) > MAX_MODEL_SIZE:
        raise HTTPException(413, f"文件过大 ({len(data)//1024//1024}MB)，上限 {MAX_MODEL_SIZE//1024//1024}MB")

    safe_name = re.sub(r"[^\w.\-]", "_", file.filename)
    target = BACKGROUNDS_DIR / f"{int(asyncio.get_event_loop().time() * 1000)}_{safe_name}"
    target.write_bytes(data)
    return {
        "name": target.name,
        "url": f"/backgrounds/{target.name}",
        "size": len(data),
        "type": ext.lstrip("."),
    }


@app.delete("/api/background/{name}")
async def delete_background(name: str):
    """删除指定背景模型"""
    safe = Path(name).name
    target = BACKGROUNDS_DIR / safe
    if not target.is_file():
        raise HTTPException(404, "背景不存在")
    if target.suffix.lower() not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, "非模型文件")
    target.unlink()
    return {"deleted": safe}


@app.put("/api/background/{name}/rename")
async def rename_background(name: str, payload: dict):
    """重命名指定背景模型（保留扩展名）"""
    new_name = (payload.get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(400, "新名称不能为空")
    safe_old = Path(name).name
    target = BACKGROUNDS_DIR / safe_old
    if not target.is_file():
        raise HTTPException(404, "背景不存在")
    if target.suffix.lower() not in ALLOWED_MODEL_EXTS:
        raise HTTPException(400, "非模型文件")
    old_suffix = target.suffix
    safe_new = re.sub(r"[^\w.\-]", "_", new_name)
    if not safe_new.lower().endswith(old_suffix.lower()):
        safe_new += old_suffix
    new_path = BACKGROUNDS_DIR / safe_new
    if new_path.exists():
        raise HTTPException(409, "同名文件已存在")
    target.rename(new_path)
    return {"old_name": safe_old, "new_name": safe_new, "url": f"/backgrounds/{safe_new}"}


# ---------- 在线音乐（music_lib：搜索 / 解析 / 自建歌单） ----------
@app.get("/api/music/search")
def music_api_search(kw: str, limit: int = 8):
    """聚合搜索在线歌曲（酷我+网易云）。返回结构化歌曲列表。"""
    kw = (kw or "").strip()
    if not kw:
        raise HTTPException(400, "缺少搜索词 kw")
    limit = max(1, min(int(limit), 30))
    try:
        songs = music_lib.search_all(kw, limit)
    except Exception as e:
        logger.error("music search failed: %s", e)
        raise HTTPException(502, f"搜索失败: {e}")
    return {"results": songs}


@app.get("/api/music/resolve")
def music_api_resolve(source: str, song_id: str):
    """解析单曲直链（供前端 <audio> 播放）。失败返回 ok=false。"""
    source = (source or "").strip().lower()
    sid = (song_id or "").strip()
    if source not in ("kuwo", "netease") or not sid:
        raise HTTPException(400, "参数错误：source=kuwo|netease, song_id 必填")
    try:
        url = music_lib.resolve(source, sid)
    except Exception as e:
        logger.error("music resolve failed: %s", e)
        url = None
    if not url:
        return {"ok": False, "error": "该歌曲暂时拿不到播放链接（可能 VIP/版权受限或已下架）"}
    return {"ok": True, "url": url}


@app.get("/api/music/boards")
def music_api_list_boards():
    """列出内置热门榜单（云音乐热歌/新歌/飙升/原创）。"""
    return {"boards": [
        {"id": pid, "name": name, "source": "netease"}
        for pid, name in music_lib._BOARDS
    ]}


@app.get("/api/music/boards/{pid}")
def music_api_board_songs(pid: str):
    """获取某个榜单的歌曲列表（网易云歌单接口实时拉取）。"""
    pid = (pid or "").strip()
    if not pid:
        raise HTTPException(400, "缺少榜单 id")
    try:
        pl = music_lib.nt_playlist(pid, 50)
    except Exception as e:
        logger.error("music board failed: %s", e)
        raise HTTPException(502, f"榜单加载失败: {e}")
    return {"board": {"id": pid, "name": pl.get("name") or pid,
                      "songs": pl.get("songs") or []}}


@app.get("/api/music/playlists")
def music_api_list_playlists(request: Request):
    """列出当前用户的自建歌单。"""
    return {"playlists": music_lib.list_playlists(_store_uid(request))}


@app.post("/api/music/playlists")
def music_api_create_playlist(payload: dict, request: Request):
    """创建自建歌单。body: {"name": "..."}"""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "歌单名不能为空")
    try:
        pl = music_lib.create_playlist(name, _store_uid(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "playlist": pl}


@app.get("/api/music/playlists/{pid}")
def music_api_get_playlist(pid: str, request: Request):
    """获取自建歌单详情（含歌曲列表）。"""
    pl = music_lib.get_playlist(pid, _store_uid(request))
    if not pl:
        raise HTTPException(404, "歌单不存在")
    return {"playlist": pl}


@app.delete("/api/music/playlists/{pid}")
def music_api_delete_playlist(pid: str, request: Request):
    """删除自建歌单。"""
    if not music_lib.delete_playlist(pid, _store_uid(request)):
        raise HTTPException(404, "歌单不存在")
    return {"ok": True, "deleted": pid}


@app.post("/api/music/playlists/{pid}/songs")
def music_api_add_song(pid: str, payload: dict, request: Request):
    """向歌单加入歌曲。body: {"song": {"source","id","name","artists"}}"""
    song = payload.get("song") or {}
    try:
        pl = music_lib.add_song(pid, song, _store_uid(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not pl:
        raise HTTPException(404, "歌单不存在")
    return {"ok": True, "playlist": pl}


@app.delete("/api/music/playlists/{pid}/songs/{song_id}")
def music_api_remove_song(pid: str, song_id: str, request: Request):
    """从歌单移除歌曲。"""
    if not music_lib.remove_song(pid, song_id, _store_uid(request)):
        raise HTTPException(404, "歌单不存在或歌曲不在歌单中")
    return {"ok": True, "removed": song_id}


# ---------- Agent & 记忆管理 API ----------
@app.get("/api/config/user_name")
async def user_name_get():
    """获取用户设置的称呼（AI 对用户的称呼，空 = 未设置）。"""
    try:
        cfg = load_config()
        return {"user_name": cfg.get("user_name", "")}
    except Exception as e:
        return {"user_name": "", "error": str(e)}


@app.post("/api/config/user_name")
async def user_name_set(payload: dict):
    """保存用户设置的称呼到 settings.json（空串 = 清除，AI 不随意称呼）。"""
    name = (payload.get("user_name") or "").strip()
    path = BASE_DIR / "settings.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["user_name"] = name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"用户称呼设置已更新: '{name}'")
        return {"ok": True, "user_name": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/agent/status")
async def agent_status():
    """获取 Agent 状态（已加载的工具数等）。"""
    try:
        agent = await get_shared_agent()
        tools_count = len(agent._all_tools) if agent._all_tools else 0
        return {
            "initialized": agent._initialized,
            "tools_count": tools_count,
            "has_memory": agent.memory is not None,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sessions")
async def list_sessions(request: Request, user_id: str = "default", q: str = ""):
    """列出当前角色卡片记忆空间下用户的所有会话。

    q 非空时按标题或消息内容模糊搜索（兼容旧调用：不带 q 行为不变，
    返回字段在原有基础上新增 message_count/summary/approx_tokens/pinned/
    archived/is_current，旧前端只读原字段不受影响）。
    """
    user_id = _uid_of(request, {"user_id": user_id})
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    sessions = await agent.get_sessions(query=(q or "").strip() or None)
    return {"sessions": sessions}


@app.post("/api/sessions/switch")
async def switch_session(payload: dict, request: Request):
    """切换到指定会话（仅限当前角色卡片记忆空间内的会话）。

    返回该会话的完整历史（user/ai 轮次，最多 300 轮）与最新摘要，
    供前端对话栏完整渲染；旧字段形状保持不变。
    """
    user_id = _uid_of(request, payload)
    session_id = payload.get("session_id", "")
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    await agent.switch_session(session_id)
    history = await agent.get_session_history(session_id, max_rounds=300)
    summary = await agent.get_session_summary(session_id)
    return {"session_id": session_id, "history": _history_for_client(history),
            "summary": summary}


@app.post("/api/sessions/rename")
async def rename_session(payload: dict, request: Request):
    """重命名指定会话（标题 1-60 字符）。"""
    user_id = _uid_of(request, payload)
    session_id = payload.get("session_id", "")
    title = (payload.get("title") or "").strip()
    if not session_id or not title:
        raise HTTPException(400, "缺少 session_id 或 title")
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    await agent.rename_session(session_id, title)
    return {"ok": True, "session_id": session_id, "title": title[:60]}


@app.post("/api/sessions/pin")
async def pin_session(payload: dict, request: Request):
    """置顶 / 取消置顶指定会话。"""
    user_id = _uid_of(request, payload)
    session_id = payload.get("session_id", "")
    pinned = bool(payload.get("pinned", True))
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    await agent.set_session_pinned(session_id, pinned)
    return {"ok": True, "session_id": session_id, "pinned": pinned}


@app.post("/api/sessions/archive")
async def archive_session(payload: dict, request: Request):
    """归档 / 取消归档指定会话（归档后不出现在默认列表、不被自动复用）。"""
    user_id = _uid_of(request, payload)
    session_id = payload.get("session_id", "")
    archived = bool(payload.get("archived", True))
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    await agent.set_session_archived(session_id, archived)
    return {"ok": True, "session_id": session_id, "archived": archived}


@app.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str, request: Request, user_id: str = "default"):
    """获取指定会话的完整历史（user/ai 轮次）与最新摘要。"""
    user_id = _uid_of(request, {"user_id": user_id})
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    history = await agent.get_session_history(session_id, max_rounds=300)
    summary = await agent.get_session_summary(session_id)
    return {"session_id": session_id, "history": _history_for_client(history),
            "summary": summary}


@app.post("/api/sessions/new")
async def new_session(payload: dict, request: Request):
    """创建新会话。"""
    user_id = _uid_of(request, payload)
    agent = await get_shared_agent(user_id)
    await agent.close_current_session()
    # 先同步当前角色卡片记忆空间，再创建新会话
    await agent.sync_memory_namespace()
    # 与 WS 的 new_session 分支同一语义：新对话必须真新建。
    # get_or_create_session 会接管命名空间里最近的活跃会话（点了新对话却回到旧对话）。
    sid = await agent.memory.create_new_session()
    return {"session_id": sid}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request, user_id: str = "default"):
    """删除指定会话。"""
    user_id = _uid_of(request, {"user_id": user_id})
    agent = await get_shared_agent(user_id)
    await agent.sync_memory_namespace()
    # 删除是不可逆的：这里原先没有归属校验，拿到别人的 session_id 就能删
    if not await agent.memory.session_visible(session_id):
        raise HTTPException(400, "该会话不属于当前角色卡片")
    await agent.delete_session(session_id)
    return {"deleted": session_id}


# ---------- Codex / OpenCode 网页透传（无缝移植飞书能力） ----------
@app.get("/api/codex/config")
async def codex_config():
    """返回当前 codex/opencode 配置（本地 codex_config.json，网页可查看/调试）"""
    codex_runner.check_config_reload()
    cfg = codex_runner.AGENT_CFG
    llm = codex_runner.LLM_CFG
    return {
        "work_dir": cfg.get("work_dir", ""),
        "sync_timeout_sec": cfg.get("sync_timeout_sec", 120),
        "max_reply_chars": cfg.get("max_reply_chars", 3800),
        "tool_progress_interval_sec": cfg.get("tool_progress_interval_sec", 60),
        "tools": cfg.get("tools", {}),
        "llm_available": codex_runner.llm_available(),
        "llm_model": llm.get("model", ""),
        "llm_base_url": llm.get("base_url", ""),
    }


@app.get("/api/codex/pwd")
async def codex_pwd():
    codex_runner.check_config_reload()
    return {"cwd": codex_runner.EXECUTOR.cwd}


@app.post("/api/codex/cd")
async def codex_cd(payload: dict):
    codex_runner.check_config_reload()
    d = (payload.get("dir") or payload.get("path") or "").strip()
    if not d:
        raise HTTPException(400, "缺少 dir")
    if not os.path.isdir(d):
        raise HTTPException(404, f"目录不存在: {d}")
    codex_runner.EXECUTOR.cwd = os.path.abspath(d)
    return {"cwd": codex_runner.EXECUTOR.cwd}


@app.post("/api/codex/tasks/{tid}/kill")
async def codex_task_kill(tid: str):
    r = codex_runner.kill_task(tid)
    if r.get("ok"):
        return {"result": "已终止独立进程任务" if r.get("reason") != "进程已结束" else "任务进程已结束"}
    return {"result": codex_runner.EXECUTOR.kill(tid)}


# ---------- WebSocket ----------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        # 连接 → 用户绑定：广播按 uid 定向投递，不同用户的流式回复/工具事件
        # 不再互串（set_user 时绑定，断连时解绑）
        self._uids: dict = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self._uids.pop(id(ws), None)

    def bind_user(self, ws: WebSocket, uid: str) -> None:
        if uid:
            self._uids[id(ws)] = uid

    def targets(self, uid: str = "") -> List[WebSocket]:
        """广播目标：按 uid 只挑同用户的连接；uid 为空（未绑定/后台任务）保持旧的全量行为。"""
        if not uid:
            return list(self.active)
        return [w for w in self.active if self._uids.get(id(w)) == uid]


manager = ConnectionManager()

# 最近一次活动的前端连接（ws/state）：定时任务等无人值守组件需要
# 「把汇报投递给用户当前所在的连接」，用这个引用兜底。
_last_chat_conn: dict = {"ws": None, "state": None}

# 常驻用户状态：与连接无关的服务端会话状态。定时任务/断点恢复/离线汇报
# 都用它——页面从未打开或已全部关闭时，后台流程仍能拿到合法 state
# （绑定全局历史 + active_task 追踪），转述/恢复照样在后台完成并落库。
_RESIDENT_STATE: dict = {"state": None}


async def _get_resident_state() -> "WSState":
    st = _RESIDENT_STATE.get("state")
    if st is None:
        st = WSState()
        st._ws_history = await _ensure_global_history()
        _RESIDENT_STATE["state"] = st
    return st
# 断点续跑去重：正在自主恢复中的用户集合（防止启动恢复与重连恢复重复触发）
_resume_inflight: set = set()


async def safe_send_json(ws: WebSocket, data: dict) -> bool:
    """安全发送 WebSocket 消息；连接已关闭/客户端不在场时静默忽略（不刷日志）。"""
    try:
        if ws is None:
            return False  # 后台续跑/无客户端在场：不发送，避免 AttributeError 刷屏
        if ws.client_state != WebSocketState.CONNECTED:
            return False  # 连接已断开（刷新/关页/断网/热重载）：正常状态，静默跳过
        await ws.send_json(data)
        return True
    except (RuntimeError, Exception) as e:
        logger.warning("[WS] 发送异常: %s, msg_type=%s", type(e).__name__, data.get("type"))
        return False


# ==================== 活跃对话轮注册表（刷新/断线不断档） ====================
# 对话轮与具体 WebSocket 连接解耦：事件经 _BroadcastWS 广播给所有活跃连接，
# 同时缓冲进度（正文累积/工具事件），前端刷新重连后通过 turn_in_progress
# 无缝接管展示。只有用户文字/语音输入（或用户主动点中断）能取消活跃轮。
class _ActiveTurn:
    __slots__ = ("task", "session_id", "state", "user_text", "resume",
                 "started_at", "full_text", "reasoning_tail", "tool_events", "done",
                 "pending_voice", "uid")

    def __init__(self, task, session_id, state, user_text, resume):
        self.task = task
        self.session_id = session_id
        self.state = state          # 启动该轮的 WSState（is_cancelled/收尾状态用）
        self.user_text = user_text
        self.resume = resume
        self.started_at = time.time()
        self.full_text = ""         # 已流出的正文累积（刷新后重放）
        self.reasoning_tail = ""    # 最近一次思考指示尾部
        self.tool_events: list = [] # 工具事件序列（刷新后重放工具链状态）
        # 工具执行期间收到的语音输入：排队等本轮结束再补发（语音不得掐掉正在跑的工具）
        self.pending_voice: list = []
        self.done = False


# 活跃对话轮按 uid 分槽：每个用户各跑各的轮，互不打断、互不接管
_ACTIVE_TURNS: dict = {}

# 当前轮所属用户（asyncio task 上下文隔离）：广播代理据此只投递给同 uid 的连接。
# 每条 WS 连接一个 task，set_user 后该 task 及它派生的对话轮 task 都能读到。
_TURN_UID: "contextvars.ContextVar[str]" = contextvars.ContextVar("dabai_turn_uid", default="")


def _turn_of(uid: str = "") -> Optional["_ActiveTurn"]:
    uid = uid or _TURN_UID.get()
    return _ACTIVE_TURNS.get(uid) if uid else None


def _turn_is_alive(uid: str = "") -> bool:
    t = _turn_of(uid)
    return bool(t and not t.done and t.task and not t.task.done())


def _register_active_turn(task, session_id, state, user_text, resume) -> None:
    """登记新的活跃对话轮（同 uid 的旧轮会被新轮取代引用，由调用方先取消）。"""
    t = _ActiveTurn(task, session_id, state, user_text, resume)
    uid = getattr(state, "user_id", "") or "default"
    t.uid = uid
    _ACTIVE_TURNS[uid] = t

    def _clear(_t, _t_ref=t):
        cur = _ACTIVE_TURNS.get(uid)
        if cur is _t_ref:
            cur.done = True
            _ACTIVE_TURNS.pop(uid, None)
            _drain_queued_voice(cur)  # 工具轮跑完 → 补发排队中的语音输入
        elif cur is not None and _t_ref.pending_voice:
            # 旧轮被新输入取代（用户打字插话）：排队语音转交新轮，别丢用户的话
            cur.pending_voice.extend(_t_ref.pending_voice)
            _t_ref.pending_voice = []
    task.add_done_callback(_clear)


def _cancel_active_turn(uid: str = "") -> bool:
    """取消后台活跃对话轮（仅用户文字/语音输入、用户点中断时调用）。"""
    t = _turn_of(uid)
    if not t or not t.task or t.task.done():
        return False
    try:
        # 流循环里大量 is_cancelled(session_id) 检查依赖该 state：先标记取消
        t.state.cancel_current()
    except Exception:
        pass
    t.task.cancel()  # CancelledError → agent 落「已暂停」断点，说『继续』可恢复
    return True


def _turn_in_tools(uid: str = "") -> bool:
    """活跃轮是否已进入工具执行阶段。

    工具一旦开跑，用户语音就只能排队（见 WS audio 分支的排队逻辑），不得取消本轮——
    否则环境噪音/VAD 误触发会把一个跑了几分钟的工具任务整轮掐掉。
    """
    t = _turn_of(uid)
    return bool(t and not t.done and t.tool_events)


def _drain_queued_voice(t) -> None:
    """工具轮结束后，把排队中的语音合并成一条用户输入补发。

    语音插话的两种命运在这里收口：能打断的（AI 只是说话/思考）当场打断；
    不能打断的（工具在跑）先排队，轮结束自动接上，用户的话一句不丢。
    """
    if t is None or not t.pending_voice:
        return
    text = "\n".join(t.pending_voice).strip()
    t.pending_voice = []
    if not text:
        return
    state = t.state
    history = getattr(state, "_ws_history", None)
    if history is None:
        logger.warning("[语音排队] 无历史镜像，丢弃排队语音: %s", text[:40])
        return
    try:
        sid = state.new_session()
        _TURN_UID.set(getattr(state, "user_id", "") or "")  # 补发轮同样只广播给本人
        state.active_task = asyncio.create_task(
            handle_user_message_stream(_BROADCAST_WS, text, history, sid, state,
                current_model=state.current_model,
                current_background=state.current_background,
                current_bgm=state.current_bgm)
        )
        _register_active_turn(state.active_task, sid, state, text, False)
        logger.info("[语音排队] 工具轮结束，补发排队语音: %s", text[:60])
    except Exception as e:
        logger.warning("[语音排队] 补发失败: %s", e)


class _BroadcastWS:
    """广播代理：对话轮事件同时投递给所有活跃连接 + 缓冲进度。

    handle_user_message_stream 只认 ws.client_state / ws.send_json 两个接口，
    传入本代理后，前端刷新页面换新连接，事件照样广播到新连接上——
    后台任务的回复进度不再依赖发起时的那条连接。
    """
    @property
    def client_state(self):
        return WebSocketState.CONNECTED  # 真实连接状态由 safe_send_json 逐个过滤

    async def send_json(self, data: dict) -> bool:
        sent = False
        uid = _TURN_UID.get()
        for w in manager.targets(uid):
            try:
                if await safe_send_json(w, data):
                    sent = True
            except Exception:
                continue
        # 活跃轮进度缓冲：重连时通过 turn_in_progress 重放
        t = _turn_of(uid)
        if t is not None and not t.done:
            mtype = data.get("type")
            if mtype == "stream_text":
                t.full_text += str(data.get("text") or "")
            elif mtype == "reasoning":
                t.reasoning_tail = str(data.get("text") or "")
            elif mtype in ("tool_call_start", "tool_call_result", "tool_call_progress"):
                t.tool_events.append(data)
                if len(t.tool_events) > 120:
                    del t.tool_events[:60]
        return sent


# 模块级单例：媒体/子智能体 worker 用 ws 身份（is）做归属匹配与去重，
# 全轮次复用同一实例才能跨轮次匹配（停止播放/播完回报的联动不丢）
_BROADCAST_WS = _BroadcastWS()

# 任务中心事件走同一个广播出口：task_event 原先只推给发起连接，前端一刷新/换设备
# 就收不到进度（背景见 task_orchestrator.set_broadcast 的注释）。
set_broadcast(_BROADCAST_WS.send_json)


def _ws_owns(worker_ws, ws) -> bool:
    """worker 归属校验：经广播代理派生的 worker 属于所有连接（单用户单系统，
    前端刷新换新连接后，播完/停止回报仍能闭环到原 worker）。"""
    return worker_ws is ws or worker_ws is _BROADCAST_WS


# 全局 Agent 实例（所有连接共享，按 user_id 区分记忆；游戏/非游戏模式共用同一实例）
_shared_agents: dict[str, AIAgent] = {}
_agent_lock = asyncio.Lock()

# ── 单系统模式：一个服务器 = 一套状态 ──────────────────────────────
# 1) 统一用户身份：前端各设备 localStorage 各自生成 user_id，服务端一律归一到
#    settings.json -> agent.unified_user_id（默认 default），所有设备共享同一份
#    记忆/会话，切换设备不断档；
# 2) 全局共享历史：所有 WebSocket 连接共用同一份 history 列表（内存镜像），
#    任何设备发消息都追加到同一份，刷新/换设备后看到的是同一条对话线。
# 按用户分桶：uid -> 内存镜像列表。多用户（公网账号）各一份，互不串线；
# 局域网统一身份走 uid=default 那一桶，行为与旧版一致。
_global_history: dict = {}
_global_history_ready: set = set()


def _unified_user_id() -> str:
    """单系统模式的统一用户 ID（settings.json -> agent.unified_user_id）。"""
    try:
        v = (_load_settings().get("agent", {}) or {}).get("unified_user_id") or ""
        return str(v).strip() or "default"
    except Exception:
        return "default"


async def _ensure_global_history(uid: str = "") -> list:
    """首次连接时把该用户的持久化历史载入内存镜像（按 uid 分桶）。"""
    global _global_history
    uid = uid or _unified_user_id()
    if uid not in _global_history_ready:
        try:
            agent = await get_shared_agent(uid)
            hist = await agent.get_history()
            if len(hist) > 200:
                hist = hist[-100:]
            _global_history[uid] = hist
        except Exception as e:
            logger.warning(f"加载全局历史失败（忽略）: {e}")
            _global_history[uid] = []
        _global_history_ready.add(uid)
    return _global_history[uid]


async def get_shared_agent(user_id: str = "default") -> AIAgent:
    """获取或创建共享 Agent 实例。"""
    if user_id not in _shared_agents:
        async with _agent_lock:
            if user_id not in _shared_agents:
                agent = AIAgent(user_id=user_id)
                await agent.initialize()
                _shared_agents[user_id] = agent
                logger.info(f"创建 Agent: user_id={user_id}")
    return _shared_agents[user_id]


# 全局奖励记忆库（LLM-as-policy 用，跨连接共享）
_shared_reward_memory: Optional[RewardMemory] = None


def get_reward_memory() -> RewardMemory:
    """获取或创建共享奖励记忆库实例（单例）。"""
    global _shared_reward_memory
    if _shared_reward_memory is None:
        _shared_reward_memory = RewardMemory()
        logger.info(f"创建 RewardMemory: {_shared_reward_memory.stats()}")
    return _shared_reward_memory


def _tool_result_preview(result: str) -> str:
    """工具结果的前端展示文本：特殊标记 JSON 转成一句话摘要（避免把完整任务 JSON
    刷进聊天面板），普通结果保留前 2000 字符（与 agent.py 的截断上限对齐，
    前端工具链卡片默认折叠为一行摘要，点击展开才显示完整文本，不会刷屏）。"""
    try:
        r = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result[:2000]
    if not isinstance(r, dict):
        return result[:2000]
    if r.get("__screen_command__"):
        return f"已执行屏幕命令：{r.get('tool')} {str(r.get('args'))[:200]}"
    if r.get("__dsh_bridge__"):
        task_tail = str(r.get("task") or "")[:200]
        return f"已提请 DSH 智能体确认任务：{task_tail}"
    if r.get("__codex_delegate__"):
        agent_id = str(r.get("agent") or r.get("tool") or "").lower()
        agent_label = {"codex": "Codex", "opencode": "OpenCode"}.get(agent_id, "编码助手")
        task_tail = str(r.get("task") or "")[:200]
        return f"已提请 {agent_label} 确认任务：{task_tail}"
    if r.get("__media_watch__"):
        kind = str(r.get("kind") or "")
        title = str(r.get("title") or "")
        return f"已派出媒体子智能体负责{kind}《{title}》，播完会自动向你汇报"
    if r.get("__media_watch_cancel__"):
        return f"已收回媒体子智能体（{str(r.get('reason') or '用户要求')[:80]}）"
    if r.get("__sub_agent_spawn__"):
        workers = r.get("workers")
        if isinstance(workers, list) and workers:
            names = "、".join(
                str((w or {}).get("title") or (w or {}).get("task") or "")[:24]
                for w in workers[:6])
            return f"已下发 {len(workers)} 个子智能体并行执行：{names}"
        task_tail = str(r.get("task") or "")[:120]
        return f"已下发子智能体执行任务：{task_tail}"
    return result[:2000]


# ============================================================
# 媒体子智能体（media watch）—— 播放类长耗时任务的子进程委托
# ============================================================

_MW_KIND_LABEL = {"music": "听歌", "playlist": "歌单看护", "video": "视频看护",
                   "general": "通用任务"}


async def _media_worker_event_push(event: str, worker) -> None:
    """媒体子智能体注册表事件 → 推送给归属前端（供 UI 实时渲染）。"""
    if worker.ws is None:
        return
    await safe_send_json(worker.ws, {
        "type": "media_worker_event",
        "kind": event,
        "event": worker.snapshot(full=False),
    })


# ---------- 通用子智能体（sub-agents） ----------

# 子智能体汇报去抖：同一连接短时间内大量子智能体完成时，合并成一条汇报给主智能体
_report_buffers: dict = {}          # ws -> list[(text, state)]
_report_timers: dict = {}           # ws -> asyncio.Task


async def _sub_agent_event_push(event: str, worker) -> None:
    """通用子智能体注册表事件 → 推送给归属前端（任务中心已由镜像 task_event 展示）。"""
    if worker.ws is None:
        return
    await safe_send_json(worker.ws, {
        "type": "sub_agent_event",
        "kind": event,
        "event": worker.snapshot(full=False),
    })


async def _spawn_sub_agent(ws: WebSocket, state, r: dict) -> None:
    """处理 __sub_agent_spawn__ 标记：登记录入通用子智能体并后台启动执行。

    支持两种形状：
    - 单个：{task, title?, note?, profile?}
    - 批量：{workers: [{task, title?, profile?}, ...]}（project_dispatch 一次派发多个
      无写集冲突的模块时用；逐条独立容错，一条坏不影响其余）
    """
    items = r.get("workers")
    if isinstance(items, list) and items:
        for it in items[:8]:
            if isinstance(it, dict):
                await _spawn_one_sub_agent(ws, state, it)
        return
    await _spawn_one_sub_agent(ws, state, r)


async def _spawn_one_sub_agent(ws: WebSocket, state, r: dict) -> None:
    """派出单个子智能体（profile = 智能体档案 id，决定它的提示词/技能/工具/模型）。"""
    task = str(r.get("task") or "").strip()
    if not task:
        logger.warning("[SubAgent] spawn 标记缺少 task，丢弃")
        return
    title = str(r.get("title") or "")[:60] or task[:40]
    note = str(r.get("note") or "")[:200]
    profile = str(r.get("profile") or "").strip()
    try:
        worker = await _get_sub_agents().spawn(
            ws, state, task, title=title,
            extra={"note": note},
            profile=profile,
        )
        logger.info(f"[SubAgent] 已下发子智能体「{title}」[{worker.id}]"
                    + (f"（档案 {profile}）" if profile else ""))
    except Exception as e:
        logger.warning(f"[SubAgent] 下发失败: {e}")


async def _deliver_sub_agent_report(worker, message: str) -> None:
    """通用子智能体完成/出错 → 去抖合并后反馈主智能体（防大量完成时刷屏）。"""
    # 定时任务：无论有无前端连接，先把执行结果记入任务历史
    job_id = ((getattr(worker, "extra", None) or {}).get("job_id"))
    if job_id:
        try:
            from scheduler import record_result
            ok = getattr(worker, "status", None) in ("done",)
            record_result(job_id, ok, message[:600])
        except Exception as e:
            logger.warning(f"[Scheduler] 记录定时任务结果失败（忽略）: {e}")
    ws = getattr(worker, "ws", None)
    state = getattr(worker, "owner", None)
    if ws is None:
        # 无人值守派发（定时任务）：优先投递给最近的活动连接
        ws = _last_chat_conn.get("ws") if _last_chat_conn else None
        if ws is None or ws not in manager.active:
            # 一个页面都不在场（服务启动后从未打开/已全部关闭）：
            # 不丢弃！切常驻状态 + 广播代理，转述照样在后台完成——
            # 结果落长期记忆 + 全局历史，用户下次打开页面即见
            ws = _BROADCAST_WS
            state = await _get_resident_state()
        else:
            state = _last_chat_conn.get("state")
    elif ws is not _BROADCAST_WS and ws not in manager.active:
        # 归属连接已断开（页面刷新/关闭）：切到广播代理，汇报照样送达在线连接
        ws = _BROADCAST_WS
    if state is None:
        # 派发时未带状态（历史调用点）：常驻状态兜底，保证历史/记忆完整
        state = await _get_resident_state()
    label = _MW_KIND_LABEL.get(getattr(worker, "kind", ""), "通用任务")
    text = f"【子智能体汇报｜{label}】{message}"
    buf = _report_buffers.setdefault(ws, [])
    buf.append((text, state))
    if ws not in _report_timers:
        async def _flush():
            await asyncio.sleep(2.0)
            _report_timers.pop(ws, None)
            items = _report_buffers.pop(ws, [])
            if not items:
                return
            state = items[0][1]
            if len(items) == 1:
                combined = items[0][0]
            else:
                # 多条合并防刷屏，但每条仍保留 400 字结论正文（只留首行会导致
                # 主智能体看得到「完成了」、看不到「做成了什么」）
                heads = [t.split("：", 1)[-1][:400] for t, _ in items]
                combined = (f"【子智能体汇报】本次共 {len(items)} 项完成：\n"
                            + "\n".join(f"{i}. {h}" for i, h in enumerate(heads, 1)))
            history = getattr(state, "_ws_history", None) or []
            ok = await _kickoff_response(ws, combined, history, state, proactive=True,
                                         external_trigger=True, msg_source="auto")
            if not ok:
                asyncio.create_task(_retry_deliver_report(ws, state, history, combined))
        _report_timers[ws] = asyncio.ensure_future(_flush())


async def _spawn_media_worker(ws: WebSocket, state, r: dict) -> None:
    """处理 __media_watch__ 标记：派出子智能体 + 注入 worker_id + 下发屏幕指令。

    子智能体在 media_workers 注册表 + 任务中心各有一份条目；worker_id 注入
    屏幕指令后，前端「播完/停止」回报时带回来，闭环匹配到同一个子进程。
    """
    kind = str(r.get("kind") or "music")
    title = str(r.get("title") or "未命名播放")
    screen = r.get("screen") or {}
    tool = str(screen.get("tool") or "")
    args = dict(screen.get("args") or {})
    if not tool:
        logger.warning("[MediaWorker] watch 标记缺少 screen.tool，丢弃")
        return
    worker = await _get_media_workers().spawn(
        ws, state, kind, title,
        brief=str(r.get("brief") or f"看护《{title}》播完并汇报"),
        extra={"tool": tool, "screen": args},
    )
    args["worker_id"] = worker.id
    # 超时兜底：时长未知或过长时给一个看护上限，防止前端事件丢失导致悬挂
    try:
        dur = float(args.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur and 0 < dur < 6 * 3600:
        await _get_media_workers().start_watchdog(worker.id, deadline=time.time() + dur + 300)
    else:
        await _get_media_workers().start_watchdog(worker.id)
    await safe_send_json(ws, {"type": "screen_command", "tool": tool, "args": args})
    # 任务中心已由 manager 同步创建条目（channel=media），主智能体可在列表看到子进程
    logger.info(f"[MediaWorker] 已派出 {kind}《{title}》[{worker.id}] → {tool}")


async def _cancel_media_worker(ws: WebSocket, r: dict) -> None:
    """处理 __media_watch_cancel__ 标记：收回子智能体 + 下发停止播放指令。"""
    wid = str(r.get("worker_id") or "").strip()
    reason = str(r.get("reason") or "用户要求收回")
    if wid:
        await _get_media_workers().cancel(wid, reason)
        logger.info(f"[MediaWorker] 已收回子智能体 {wid}（{reason}）")
    screen = r.get("screen") or {}
    tool = str(screen.get("tool") or "")
    if tool:
        await safe_send_json(ws, {"type": "screen_command", "tool": tool,
                                  "args": screen.get("args") or {}})


async def _cancel_media_on_stop(ws: WebSocket, tool: str, args: dict) -> None:
    """主智能体发出停止类屏幕指令（stop_music / control_video stop）时，
    把该连接上同类型的看护子智能体一并静默收尾（播放没了，看护也结束）。"""
    if tool == "stop_music":
        kinds = ("music", "playlist")
    elif tool == "control_video" and str(args.get("action")) == "stop":
        kinds = ("video",)
    else:
        return
    try:
        mgr = _get_media_workers()
        for w in mgr.active():
            if _ws_owns(w.ws, ws) and w.kind in kinds:
                await mgr.cancel(w.id, reason="播放被停止")
    except Exception as e:
        logger.warning(f"[MediaWorker] 停止联动收尾失败: {e}")


async def _deliver_media_worker_report(worker, message: str) -> None:
    """子智能体干完活 → 以『子智能体汇报』的形式反馈给主智能体。

    主智能体由此知道：哪个子进程干完了什么活，并自然地转述给用户。
    """
    ws = getattr(worker, "ws", None)
    if ws is None:
        return
    # 归属连接已断开（页面刷新/关闭）：切到广播代理，汇报照样送达在线连接
    if ws is not _BROADCAST_WS and ws not in manager.active:
        ws = _BROADCAST_WS
    state = getattr(worker, "owner", None)
    history = getattr(state, "_ws_history", None) or []
    label = _MW_KIND_LABEL.get(worker.kind, worker.kind)
    text = f"【子智能体汇报｜{label}】{message}"
    try:
        ok = await _kickoff_response(ws, text, history, state, proactive=True,
                                     external_trigger=True, msg_source="auto")
        if not ok:
            # 主智能体正在回复/冷却中 → 稍后补投，汇报不丢失
            asyncio.create_task(_retry_deliver_report(ws, state, history, text))
    except Exception as e:
        logger.warning(f"[MediaWorker] 汇报投递失败: {e}")


async def _retry_deliver_report(ws, state, history, text: str) -> None:
    deadline = time.time() + 600  # 慢模型下主回复可能很久才结束，汇报耐心补投
    while time.time() < deadline:
        if state is None or state.active_task is None or state.active_task.done():
            try:
                ok = await _kickoff_response(ws, text, history, state, proactive=True,
                                             external_trigger=True, msg_source="auto")
            except Exception as e:
                logger.warning(f"[MediaWorker] 补投汇报失败: {e}")
                ok = False
            if ok:
                return
        await asyncio.sleep(1.5)


async def _handle_music_end(ws: WebSocket, payload: dict) -> None:
    """前端 <audio> 单曲播完回报：闭环到看护子智能体（歌单播完最后一首才算完）。"""
    wid = str(payload.get("worker_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    final = bool(payload.get("final"))
    mgr = _get_media_workers()
    worker = mgr.get(wid) if wid else None
    # 归属校验：worker 属于这条连接或广播代理（刷新换连接后仍能闭环）；
    # wid 缺失时按连接 + 歌名模糊兜底
    if worker is not None and not _ws_owns(worker.ws, ws):
        worker = None
    if worker is None and name:
        for w in mgr.active():
            if (_ws_owns(w.ws, ws) and w.kind in ("music", "playlist")
                    and name and (w.title in name or name in w.title)):
                worker = w
                break
    if worker is None:
        return
    if worker.kind == "playlist" and not final:
        return  # 歌单还没到最后一首
    label = name or worker.title
    await mgr.complete(worker.id, f"《{label}》已经播放完毕。")


async def _handle_music_stop(ws: WebSocket, payload: dict) -> None:
    wid = str(payload.get("worker_id") or "").strip()
    mgr = _get_media_workers()
    for w in mgr.active():
        if _ws_owns(w.ws, ws) and w.kind in ("music", "playlist") and (not wid or w.id == wid):
            await mgr.cancel(w.id, reason="用户停止播放")


async def _handle_video_stop(ws: WebSocket, payload: dict) -> None:
    wid = str(payload.get("worker_id") or "").strip()
    mgr = _get_media_workers()
    for w in mgr.active():
        if _ws_owns(w.ws, ws) and w.kind == "video" and (not wid or w.id == wid):
            await mgr.cancel(w.id, reason="用户关闭大屏")


# ---------- 会话状态（per-connection） ----------
class WSState:
    """每个 WebSocket 连接的会话状态，支持打断式流式响应。"""
    def __init__(self):
        self.current_session: Optional[str] = None   # 当前活跃回复 session_id
        self.cancelled: set = set()                  # 已取消的回复 session_id 集合
        self.active_task: Optional[asyncio.Task] = None  # 当前活跃回复 task
        self.user_id: str = "default"                # 用户标识
        self.chat_session_id: Optional[str] = None   # 当前对话会话 ID（持久化用）
        self.current_model: Optional[str] = None     # 当前使用的角色模型名
        self.current_background: Optional[str] = None  # 当前使用的背景场景名
        self.current_bgm: Optional[str] = None       # 当前播放的背景音乐名
        self.current_anim: Optional[dict] = None      # 前端上报的当前动作（{name,category,emotion}）
        self._ws_history: list = []                # 连接级短期记忆（子智能体汇报等外部触发用）
        self.autonomy: AutonomyHub = AutonomyHub()   # AI 自主行为中枢（感知 + 行为决策）
        self.perception_dispatcher = PerceptionDispatcher()  # 统一感知路由调度器
        self.last_response_done: float = 0           # 上次回复完成时间戳（被动事件冷却用）
        self.last_user_message_time: float = time.time()  # 用户最后消息时间（AI 自主系统用）
        self.ai_is_moving: bool = False              # AI 是否正在自主移动
        # STT 失败冷却：连续失败 N 次后暂停接收音频，避免反复消耗资源 + 误抑制 AI 自主行为
        self._stt_fail_count: int = 0               # 连续失败计数
        self._stt_fail_window_start: float = 0      # 计数窗口起点
        self._stt_cooldown_until: float = 0         # 冷却截止时间
        # LLM-as-policy：上次决策待结算（等奖励回传时存入记忆库）
        self._pending_llm_action: Optional[dict] = None
        # ── RL 统一关系状态（非游戏模式也纳入 RL 统摄） ──
        self.affection: float = 50.0     # 好感度 [0,100]
        self.trust: float = 30.0         # 信任度 [0,100]
        self.intimacy: float = 20.0      # 亲密度 [0,100]
        self.user_emotion: int = 0       # 0积极 1中性 2消极 3愤怒 4孤独
        self.proactive_successes: int = 0
        self.proactive_attempts: int = 0
        # 全局主动说话闸门：所有非用户消息驱动的主动说话共用此时间戳冷却
        self._last_proactive_speak: float = 0
        # RL 决策的快照间隔（秒），由快照间隔控制器学习得出（大厅/游戏共用）
        self._current_snapshot_interval: float = 30.0

    def new_session(self) -> str:
        """开启新回复会话：把旧会话标记为取消，返回新 session_id。"""
        if self.current_session:
            self.cancelled.add(self.current_session)
        sid = uuid.uuid4().hex
        self.current_session = sid
        return sid

    def is_cancelled(self, sid: str) -> bool:
        return sid in self.cancelled

    def cancel_current(self):
        if self.current_session:
            self.cancelled.add(self.current_session)
            self.current_session = None


def _append_history_round(history: list, user_text: str, full_text: str,
                          proactive: bool, record_history: bool,
                          replace_last: bool = False) -> None:
    """将一轮对话写入连接级短期记忆（history）。

    用户输入轮次 → {"user": user_text, "ai": full_text}（原行为）；
    AI 主动说话轮次 → {"user": "", "ai": full_text}：空 user 作为标记，
    渲染层（memory._connection_pairs_to_records / agent 历史组装）跳过空 user
    轮次，LLM 上下文只看到「AI 之前主动说了一段话」，用户搭话时可基于此回应。
    被打断（用户插话）时也调用本函数，把已生成的部分内容补记进历史。

    replace_last=True（断点续跑完成）：若最后一条是对同一用户输入的半截记录，
    用完整回复替换它，避免同一问题在历史里出现「半截 + 完整」两条。
    """
    text = (full_text or "").strip()
    if not text or not record_history:
        return
    entry = {"user": "" if proactive else user_text, "ai": text}
    if replace_last and history:
        last = history[-1]
        if str(last.get("user") or "") == str(entry["user"] or ""):
            old = str(last.get("ai") or "").strip()
            if old and (text.startswith(old) or len(old) <= len(text)):
                history[-1] = entry
                return
    history.append(entry)
    # 防止长时间会话 history 无限增长导致内存泄漏（LLM 只用最后 10 条）
    if len(history) > 200:
        history[:] = history[-100:]


_RESUME_EXACT = {
    "继续", "继续吧", "继续做", "继续干", "继续干活", "继续工作", "继续任务",
    "继续查", "继续改", "继续写", "接着", "接着来", "接着做", "接着干",
    "接着查", "接着改", "接着写",
}
_RESUME_PREFIX = ("继续", "接着")
_RESUME_TASK_WORDS = (
    "优化", "改", "写", "查", "做", "干", "修", "任务", "项目", "接口",
    "代码", "之前", "刚才", "上回", "处理", "弄", "搞", "完成",
    "分析", "搜索", "找", "跑", "编译", "调试", "部署",
)


def _is_resume_intent(text: str) -> bool:
    """判断用户消息是否为「继续被打断的任务」的明确指令。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    if t in _RESUME_EXACT:
        return True
    if t.startswith(_RESUME_PREFIX):
        return any(w in t for w in _RESUME_TASK_WORDS)
    return False


_STOP_WORDS = {
    "停止", "停", "停下", "停一下", "停停", "别说了", "闭嘴", "取消", "暂停",
    "先停", "别干了", "别继续", "stop",
}


def _is_stop_word(text: str) -> bool:
    """用户只发停止指令（exact match）→ 立即打断；「别停/继续」等不误伤。"""
    return (text or "").strip().lower() in _STOP_WORDS


async def handle_user_message_stream(ws: WebSocket, user_text: str, history: list,
                                      session_id: str, state: WSState,
                                      current_model: Optional[str] = None,
                                      current_background: Optional[str] = None,
                                      current_bgm: Optional[str] = None,
                                      record_history: bool = True,
                                      msg_source: str = "chat",
                                      proactive: bool = False,
                                      resume_checkpoint: Optional[dict] = None):
    """流式处理：AI 流式响应（含工具调用）→ 按句切分 → 每句生成 TTS → 推送 audio_chunk。

    支持：
    - 技能工具调用状态实时推送
    - 长期记忆自动保存
    - 首句延迟 ≈ AI 首句生成时间 + 单句 TTS 时间（通常 1~2s）

    resume_checkpoint: 非空 = 断点续跑（热重载/重启打断的角色对话轮）：
        不走普通上下文重建，直接调用 agent.resume_turn 从断点继续。

    Args:
        record_history: 是否记录到短期记忆（history）。仅用户直接输入应记录，
            环境交互（感知触发/自主行为等）不进短期记忆，避免重复内容导致思维僵化。
        msg_source: 消息来源标记，写入长期记忆的 source 字段：
            'chat'=用户直接输入，'auto'=环境交互（由记忆系统处理）。
    """
    if not user_text.strip():
        return
    # —— 用户说「继续/接着做」：从断点续跑被打断的任务（方案 A）——
    # 只在明确表达继续意图且存在「已暂停」断点时触发，避免误续旧任务；
    # 断点不可用时回退到任务状态摘要重建（方案 C 兜底）。
    if resume_checkpoint is None and not proactive:
        try:
            from agent import (load_paused_turn_checkpoint, load_task_resume_state,
                               resume_fresh_seconds)
            _fresh_win = resume_fresh_seconds()
            _cp = load_paused_turn_checkpoint(state.user_id)
            if _cp and _cp.get("paused") and _is_resume_intent(user_text):
                _cp_ts = float(_cp.get("updated_at") or 0)
                # 只有「新鲜窗口」内的暂停断点才自动续跑；
                # 过期的旧任务不恢复，也不注入过期摘要——「继续」按普通消息处理
                if _cp_ts and time.time() - _cp_ts <= _fresh_win:
                    resume_checkpoint = _cp
            else:
                _st = load_task_resume_state(state.user_id)
                if _st and _is_resume_intent(user_text):
                    _st_ts = float(_st.get("ts") or 0)
                    if _st_ts and time.time() - _st_ts <= _fresh_win:
                        _summary = str(_st.get("summary") or "").strip()
                        if _summary:
                            user_text = ("继续之前被打断的任务：" + _summary +
                                         "（如果任务已经完成或信息过时，请直接告诉我）")
        except Exception:
            pass
    await safe_send_json(ws, {
        "type": "thinking",
        "session_id": session_id,
        "resume": bool(resume_checkpoint),
    })

    buffer = ""
    full_text = ""
    held_stream = ""        # 待路由的实时文本：短文本先攒着，判定工具轮/正文后再分发
    STREAM_FLUSH_CHARS = 40  # 超过该长度视为正文（通常是最终回答），实时冲刷进正文
    seq = 0
    SOFT_CUT_LEN = 60   # 兜底字符切分（主要靠句号/逗号自然分段，此值只防超长无标点句）
    TTS_LOOKAHEAD = 4   # 并行预生成句数：语音持续跟随文字，生成慢于播放也不断档
    tool_calls_made = []  # 记录本轮调用的工具
    # 真实思维链（reasoning_content）的「思考中」实时指示：
    # 服务端累积全文，按间隔节流只推尾部，让前端回复气泡底部一行持续更新，
    # 用户可据此判断对话仍在推进而非卡住/失败。不朗读、不进正文。
    reasoning_total = ""
    reasoning_last_send = 0.0
    REASONING_SEND_INTERVAL = 0.25
    REASONING_TAIL_LEN = 180

    # ---- 文本/语音解耦：文本经 stream_text 即时推送（不等语音）；
    #      语音由后台 worker 并行预生成 + 按序发送（持续跟随文字）----
    tts_q: asyncio.Queue = asyncio.Queue()
    tts_task = None
    TTS_SENTINEL = object()

    async def _gen_tts_audio(sentence: str):
        """生成单句 TTS 音频（base64 编码）。"""
        try:
            result = await generate_tts(
                sentence, _TURN_UID.get() or getattr(state, "user_id", ""))
            if result:
                audio_bytes, mime_type = result
                b64 = base64.b64encode(audio_bytes).decode("utf-8")
                del audio_bytes  # 释放内存
                return b64, mime_type
        except Exception as e:
            print(f"[TTS] 失败: {e}")
        return None, None

    async def _send_audio_chunk(seq_no: int, sentence: str, thinking: bool,
                                audio_b64, audio_mime):
        if ws is None or not manager.active:
            return  # 无客户端在场（如启动后台续跑）：只落库，不浪费 TTS
        if state.is_cancelled(session_id):
            return
        await safe_send_json(ws, {
            "type": "audio_chunk",
            "session_id": session_id,
            "seq": seq_no,
            "text": sentence,
            "audio_b64": audio_b64,
            "audio_mime": audio_mime,
            "final": False,
            "thinking": thinking,
        })

    async def _tts_worker():
        """并行预生成 + 按序发送：语音持续跟随文字，一小段一小段不中断。"""
        inflight: dict = {}   # seq -> 生成任务
        meta: dict = {}       # seq -> (sentence, thinking)
        results: dict = {}    # seq -> (sentence, thinking, audio_b64, mime)
        next_send = 1
        finished = False

        def _collect():
            for s in list(inflight.keys()):
                t = inflight[s]
                if t.done():
                    try:
                        b64, mime = t.result()
                    except Exception:
                        b64, mime = None, None
                    sentence, thinking = meta.get(s, ("", False))
                    results[s] = (sentence, thinking, b64, mime)
                    del inflight[s]
                    del meta[s]

        try:
            while True:
                try:
                    item = await asyncio.wait_for(tts_q.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    item = TTS_SENTINEL
                if item is not TTS_SENTINEL:
                    if item is None:
                        finished = True
                    else:
                        seq_no, sentence, thinking = item
                        if len(inflight) >= TTS_LOOKAHEAD:
                            # 达并发上限：先等最早一个完成再继续，控制预生成数量
                            await asyncio.wait(list(inflight.values()),
                                               return_when=asyncio.FIRST_COMPLETED)
                            _collect()
                        meta[seq_no] = (sentence, thinking)
                        inflight[seq_no] = asyncio.create_task(
                            _gen_tts_audio(sentence))
                _collect()
                # 严格按 seq 顺序发送连续就绪的块（先到先播）
                while next_send in results:
                    sentence, thinking, b64, mime = results.pop(next_send)
                    try:
                        await _send_audio_chunk(next_send, sentence,
                                                thinking, b64, mime)
                    except Exception:
                        pass
                    next_send += 1
                if finished and not inflight and not results:
                    break
        finally:
            for t in inflight.values():
                if not t.done():
                    t.cancel()

    async def _speak(text: str, thinking: bool = False):
        """把一段文字送进 TTS 队列朗读（先渲染成口语化文本，不阻塞文本流）。

        按句号/逗号等自然停顿分段朗读，避免整段一口气或按字符硬切。
        """
        nonlocal seq
        if not text:
            return
        # 先渲染再读：Markdown → 口语化纯文本，避免语音念出 **、|、URL 等符号
        sentence = _tts_plain_text(text)
        if not sentence or len(sentence) < 2:
            return
        if ws is None or not manager.active or state.is_cancelled(session_id):
            return
        # 电话体验优化：先在首个停顿点（顿号/逗号/冒号）提前切出第一小段立刻进 TTS 队列；
        # 音频按 seq 顺序发送，抢先合成首包不改变播放顺序
        first_stop = re.search(r"[，,、：:]", sentence)
        if first_stop and first_stop.start() + 1 <= 12 and len(sentence) > 12:
            seq += 1
            await tts_q.put((seq, sentence[:first_stop.start() + 1].strip(), thinking))
            sentence = sentence[first_stop.start() + 1:].strip()
        # 按句号/逗号/问号/叹号/分号/换行等自然停顿分段（标点留在段尾，读起来自然）
        parts = re.split(r"(?<=[。！？!?；;，,\n…])", sentence)
        for p in parts:
            p = p.strip()
            if len(p) < 2:
                continue
            seq += 1
            await tts_q.put((seq, p, thinking))

    async def flush(sentence: str):
        """结论分句交给后台 TTS 队列（不阻塞文本流），音频按序发送。"""
        await _speak(sentence)

    tts_task = asyncio.create_task(_tts_worker())

    async def _tts_finalize():
        """等 TTS 队列全部发完后再补发 audio_end（后台独立执行）。

        让「一轮对话完成」不再被语音播放拖住：主路径拿到 full_text 后立即
        收尾（历史/状态就绪），audio_end 由本任务等所有音频分片发完后补发，
        保证前端音频顺序仍严格「先分片后结束」，又不阻塞对话轮次。
        """
        try:
            await tts_q.put(None)  # 通知 worker 无新任务
            if tts_task:
                await tts_task      # 等 worker 发完所有序列化的音频分片
        except Exception:
            pass
        if state.is_cancelled(session_id):
            return
        await safe_send_json(ws, {
            "type": "audio_end",
            "session_id": session_id,
            "full_text": full_text,
            "tool_calls": tool_calls_made if tool_calls_made else None,
        })

    try:
        # 统一使用共享 Agent
        agent = await get_shared_agent(state.user_id)
        if resume_checkpoint is not None:
            # 断点续跑：会话/上下文已固化在检查点中，直接续跑被打断的工具轮
            cp_sid = str(resume_checkpoint.get("session_id") or "")
            if cp_sid:
                state.chat_session_id = cp_sid
                if agent.memory:
                    try:
                        await agent.sync_memory_namespace()
                        if await agent.memory.session_visible(cp_sid):
                            await agent.memory.set_session_id(cp_sid)
                    except Exception as e:
                        logger.warning(f"[Resume] 会话绑定失败: {e}")
            stream = agent.resume_turn(resume_checkpoint)
        else:
            # 将记忆绑定到当前活动角色卡片的命名空间（跨卡片记忆隔离）；
            # state 中的会话若不属于当前命名空间（如切卡后未刷新）则改用命名空间当前会话
            if agent.memory:
                await agent.sync_memory_namespace()
                if state.chat_session_id and await agent.memory.session_visible(state.chat_session_id):
                    agent.memory.session_id = state.chat_session_id
                else:
                    state.chat_session_id = agent.memory.session_id


            stream = agent.chat_stream(
                user_text, history=history,
                current_model=current_model,
                current_background=current_background,
                current_bgm=current_bgm,
                msg_source=msg_source,
                current_anim=state.current_anim,
                record_history=record_history,
                proactive=proactive,
            )

        # 断点续跑事件附加标记，前端可据此区分恢复轮（普通轮不受影响）
        is_resume = resume_checkpoint is not None
        # 将记忆绑定到当前活动角色卡片的命名空间（跨卡片记忆隔离）；
        async for event in stream:
            if state.is_cancelled(session_id):
                break

            if isinstance(event, StreamDelta):
                # 实时文本：先积攒，句末标点或超阈值才冲刷进正文（避免工具轮过程话闪进闪出）
                if event.text:
                    held_stream += event.text
                    # 电话体验优化：句末标点一到就冲刷，首句立刻进 TTS 队列，
                    # 不再等 40 字凑满（否则首响被人为拖慢一整句）
                    m_held = SENTENCE_END.search(held_stream)
                    if m_held:
                        while m_held:
                            ready, held_stream = held_stream[:m_held.end()], held_stream[m_held.end():]
                            await safe_send_json(ws, {
                                "type": "stream_text",
                                "session_id": session_id,
                                "text": ready,
                            })
                            await flush(ready)
                            m_held = SENTENCE_END.search(held_stream)
                    elif len(held_stream) >= STREAM_FLUSH_CHARS:
                        await safe_send_json(ws, {
                            "type": "stream_text",
                            "session_id": session_id,
                            "text": held_stream,
                        })
                        await flush(held_stream)
                        held_stream = ""

            elif isinstance(event, ReasoningDelta):
                # 真实思维链（reasoning_content）：回复气泡底部的「思考中」指示。
                # 节流：增量累积到发送间隔才推一次，只带尾部（避免逐字刷屏，
                # 也避免超大思维链占满 WebSocket 带宽）；正文仍只留正式回答。
                reasoning_total += event.text or ""
                _now = time.monotonic()
                if _now - reasoning_last_send >= REASONING_SEND_INTERVAL:
                    reasoning_last_send = _now
                    await safe_send_json(ws, {
                        "type": "reasoning",
                        "session_id": session_id,
                        "text": reasoning_total[-REASONING_TAIL_LEN:],
                    })

            elif isinstance(event, FinalText):
                # 最终全文：只入历史（展示/语音已实时流出）
                full_text += event.text
                # 本轮无工具调用：积攒的短文本是最终回复，冲刷进正文 + 语音
                if held_stream:
                    await safe_send_json(ws, {
                        "type": "stream_text",
                        "session_id": session_id,
                        "text": held_stream,
                    })
                    await flush(held_stream)
                    held_stream = ""

            elif isinstance(event, TextDelta):
                # 文本即时推送：不等语音，前端立刻把文字流出来
                if event.text:
                    await safe_send_json(ws, {
                        "type": "stream_text",
                        "session_id": session_id,
                        "text": event.text,
                    })
                buffer += event.text
                full_text += event.text
                # 按句末标点切分
                while True:
                    m = SENTENCE_END.search(buffer)
                    if m:
                        sentence = buffer[:m.end()]
                        buffer = buffer[m.end():]
                        await flush(sentence)
                    elif len(buffer) >= SOFT_CUT_LEN:
                        await flush(buffer)
                        buffer = ""
                        break
                    else:
                        break

            elif isinstance(event, ToolCallStart):
                # 本轮是工具轮：积攒的短文本是过程话，保留在正文里（语义连贯），
                # 不再转思考段丢弃；叙述尾巴照常刷给语音
                if buffer.strip():
                    await flush(buffer)
                    buffer = ""
                if held_stream:
                    await safe_send_json(ws, {
                        "type": "stream_text",
                        "session_id": session_id,
                        "text": held_stream,
                    })
                    await flush(held_stream)
                    held_stream = ""
                tool_calls_made.append({"name": event.tool_name, "arguments": event.arguments})
                await safe_send_json(ws, {
                    "type": "tool_call_start",
                    "session_id": session_id,
                    "tool_name": event.tool_name,
                    # 工具自己的说明（技能/插件 description 首句）：前端中央字幕直接用它，
                    # 不需要前端维护「工具名 → 人话」映射表（那表会随新增工具过期）
                    "tool_desc": getattr(event, "tool_desc", "") or "",
                    "arguments": event.arguments,
                    "resume": is_resume,
                })

            elif isinstance(event, ThinkingDelta):
                # 工具轮过程话 / 阶段进展：同样不再下发 thinking_text，
                # 过程话已由 StreamDelta/ToolCallStart 走正文链路展示。
                pass

            elif isinstance(event, ToolCallResult):
                await safe_send_json(ws, {
                    "type": "tool_call_result",
                    "session_id": session_id,
                    "tool_name": event.tool_name,
                    "result": _tool_result_preview(event.result),
                    "success": event.success,
                    "resume": is_resume,
                })
                # 同一次工具结果被 5 个子系统消费（屏幕指令/DSH 桥接/委派/媒体看护/子智能体）。
                # 只解析一次：非 JSON 结果（占绝大多数，如 shell 输出）直接跳过，
                # 避免 5 处各自 json.loads 失败各写一条 warning（实测约 1.9 行/秒的日志噪音）。
                r = None
                if event.success and isinstance(event.result, str) and event.result[:1] in ("{", "["):
                    try:
                        _parsed = json.loads(event.result)
                        if isinstance(_parsed, dict):
                            r = _parsed
                    except (json.JSONDecodeError, TypeError, ValueError):
                        logger.warning(f"[ToolResult] JSON 解析失败（结果可能被截断）: {str(event.result)[:120]!r}")
                if r is not None:
                    # 检测是否为屏幕控制工具，若是则转发给前端执行
                    if r.get("__screen_command__"):
                        _sc_tool = r.get("tool")
                        _sc_args = r.get("args") or {}
                        if _sc_tool:
                            await safe_send_json(ws, {"type": "screen_command", "tool": _sc_tool, "args": _sc_args})
                            # 停止类指令 → 联动收尾同类型的看护子智能体
                            await _cancel_media_on_stop(ws, str(_sc_tool), _sc_args)
                            logger.info(f"[ScreenCmd] 已发送: {_sc_tool} {_sc_args}")
                    # DSH 桥接工具：注册待确认任务，前端弹确认卡片
                    if r.get("__dsh_bridge__"):
                        await request_harness_task(ws, r.get("task") or "", r.get("cwd"))
                        logger.info(f"[Bridge] 已登记任务: {str(r.get('task'))[:60]}")
                    # 委派本机代码助手：转任务中心可视化执行
                    if r.get("__codex_delegate__"):
                            # 统一智能体身份：新工具传 agent（codex/opencode），旧工具传 tool（cx/ai）
                            agent_id = (str(r.get("agent") or r.get("tool") or "")).lower().strip()
                            tool = {"codex": "cx", "opencode": "ai",
                                    "cx": "cx", "ai": "ai"}.get(agent_id, "cx")
                            agent_name = {"cx": "Codex", "ai": "OpenCode"}.get(tool, agent_id)
                            task_desc = str(r.get("task") or "").strip()
                            tcfg = (codex_runner.AGENT_CFG.get("tools") or {}).get(tool)
                            if tcfg and task_desc:
                                asyncio.create_task(_run_codex_tool_task(ws, tool, tcfg, task_desc))
                                await safe_send_json(ws, {"type": "codex_msg", "kind": "bg_started",
                                                          "text": f"🧭 已提请 {agent_name} 确认任务（任务中心点「确认执行」后才会真正执行）"})
                            else:
                                logger.warning(f"[Delegate] 委派被丢弃：{agent_name} 未配置或 task 为空（len={len(task_desc)}）")
                                await safe_send_json(ws, {"type": "codex_msg", "kind": "error",
                                                          "text": f"⛔ 代码助手 {tool} 未配置或任务为空，请检查 codex_config.json"})
                            logger.info(f"[Delegate] 已提请 {agent_name} 确认: len(task)={len(task_desc)} {task_desc[:60]}")
                    # 媒体子智能体（media watch）：music_play/video_play 带 watch=true 时返回 __media_watch__
                    if r.get("__media_watch__"):
                        await _spawn_media_worker(ws, state, r)
                    elif r.get("__media_watch_cancel__"):
                        await _cancel_media_worker(ws, r)
                    # 通用子智能体（sub-agents）：sub_agent_spawn 返回 __sub_agent_spawn__
                    if r.get("__sub_agent_spawn__"):
                        await _spawn_sub_agent(ws, state, r)

            elif isinstance(event, ToolCallProgress):
                # 工具执行心跳：长任务（shell 长命令/文件搜索/媒体处理）持续告知用户仍在执行
                await safe_send_json(ws, {
                    "type": "tool_call_progress",
                    "session_id": session_id,
                    "tool_name": event.tool_name,
                    "elapsed": int(event.elapsed or 0),
                    "message": event.message or f"工具 {event.tool_name} 正在执行中……",
                    "resume": is_resume,
                })

            elif isinstance(event, TurnStatus):
                # 对话轮运行状态（网络波动自动重连等）：前端气泡底部一行提示，
                # 同时喂看门狗（noteTurnActivity），避免重试等待被误判为卡死
                await safe_send_json(ws, {
                    "type": "turn_status",
                    "session_id": session_id,
                    "text": event.text,
                })

            elif isinstance(event, UsageEvent):
                # LLM 用量事件：转发给前端实时展示（早于 audio_end 到达，符合前端时序）
                await safe_send_json(ws, {
                    "type": "usage",
                    "session_id": session_id,
                    "prompt_tokens": event.prompt_tokens,
                    "context_tokens": getattr(event, "context_tokens", 0) or 0,
                    "completion_tokens": event.completion_tokens,
                    "total_tokens": event.total_tokens,
                    "rounds": event.rounds,
                    "context_window": event.context_window,
                    "cache_hit_tokens": getattr(event, "cache_hit_tokens", 0) or 0,
                    "cache_miss_tokens": getattr(event, "cache_miss_tokens", 0) or 0,
                })

    except asyncio.CancelledError:
        # 任务被用户插话/新回复强制取消（active_task.cancel()）：已生成的部分
        # 内容补记历史，再向上传播取消——否则 AI 对自己刚说的话毫无印象
        if tts_task:
            tts_task.cancel()
        _append_history_round(history, user_text, full_text, proactive, record_history)
        # 回执中断事件：前端据此清队列、解除会话栅栏并复位状态徽章
        await safe_send_json(ws, {"type": "interrupted", "session_id": session_id})
        if tool_calls_made:
            await safe_send_json(ws, {"type": "system_msg",
                                      "text": "⏸ 任务已暂停，说『继续』可以接着干"})
        raise
    except Exception as e:
        if tts_task:
            tts_task.cancel()
        logger.error(f"Agent 错误: {e}")
        await safe_send_json(ws, {"type": "error", "message": f"AI 出错了：{e}",
                            "session_id": session_id})
        state.last_response_done = time.time()
        # 报错也补记已生成的部分，避免这一轮从短期记忆中消失——
        # 否则用户接着问时 AI 对自己刚说过/做过的事毫无印象
        try:
            _append_history_round(history, user_text, full_text,
                                  proactive, record_history)
        except Exception:
            pass
        return

    # 处理剩余尾巴
    if buffer.strip():
        await flush(buffer)
    if held_stream:
        # 最终回复尾巴（无工具轮）：冲刷进正文 + 语音
        await safe_send_json(ws, {
            "type": "stream_text",
            "session_id": session_id,
            "text": held_stream,
        })
        await flush(held_stream)
        held_stream = ""

    if state.is_cancelled(session_id):
        # 本次回复被用户插话/新输入打断：已生成的部分也补记历史，
        # 保证用户搭话时 AI 记得自己刚说过的话
        if tts_task:
            tts_task.cancel()
        _append_history_round(history, user_text, full_text, proactive, record_history)
        # 兜底：流式生成器可能因 is_cancelled 提前 break 而正常收尾
        # （不会抛 CancelledError 到 agent.chat_stream），这里显式把当前
        # 轮次的断点标记为「已暂停」，保证用户说『继续』仍能恢复完整现场。
        try:
            from agent import mark_latest_ckpt_paused
            mark_latest_ckpt_paused(state.user_id)
        except Exception:
            pass
        await safe_send_json(ws, {"type": "interrupted", "session_id": session_id})
        if tool_calls_made:
            await safe_send_json(ws, {"type": "system_msg",
                                      "text": "⏸ 任务已暂停，说『继续』可以接着干"})
        state.last_response_done = time.time()
        return

    full_text = full_text.strip()
    # 语音播放独立化：audio_end 交给后台任务等所有音频发完再补发，
    # 这里立刻收尾本轮（历史/状态），不再等语音全部播放完。
    asyncio.create_task(_tts_finalize())
    state.last_response_done = time.time()
    # 短期记忆：用户直接输入与 AI 主动说话均记录（主动说话以空 user 标记，
    # 渲染层自动跳过空 user 轮次 → LLM 只看到「AI 说了一段话」）
    _append_history_round(history, user_text, full_text, proactive, record_history,
                          replace_last=is_resume)
    # 子智能体/媒体完成汇报轮收尾：把「已完成」汇报立即固化为摘要，
    # 防止后续主动对话仍读到过期的"进行中"摘要而自相矛盾
    if msg_source == "auto" and str(user_text).startswith("【子智能体汇报"):
        try:
            if agent.memory:
                await agent.memory.force_summarize(
                    include_auto=True, auto_prefix=("【子智能体汇报",))
        except Exception as e:
            logger.warning(f"汇报轮次强制摘要失败（忽略）: {e}")


async def _quota_gate(ws: WebSocket, state: WSState) -> bool:
    """用户驱动的一轮开跑前扣当日额度；被限流时回一句提示并返回 False。

    只在「真正要开跑的用户轮」调用 —— 主动说话、UI 自动上报、断点续跑都不经过。
    """
    q = turn_quota.consume(state.user_id)
    if q["allowed"]:
        return True
    await safe_send_json(ws, {
        "type": "system_msg",
        "text": (f"今天的对话额度用完了（{q['used']}/{q['limit']} 轮），"
                 f"明天 0 点自动恢复。"),
    })
    return False


async def _kickoff_response(ws: WebSocket, text: str, history: list, state: WSState,
                           allow_interrupt: bool = False,
                           proactive: bool = False,
                           external_trigger: bool = False,
                           record_history: Optional[bool] = None,
                           msg_source: str = "chat"):
    """启动流式回复 task。

    allow_interrupt=True 时取消当前回复（用户文字/语音输入）；
    allow_interrupt=False 时若AI正在说话则直接忽略（被动事件）。

    proactive=True 表示非用户消息驱动的主动说话（RL调度/感知派发/
    环境快照等）——所有主动路径共用全局闸门 _last_proactive_speak，
    防止任何来源的"自言自语"式高频说话。返回 True 表示已启动回复。

    external_trigger=True 表示外部触发（用户召唤等，用户在场互动）
    —— 可突破用户活跃窗口守卫 ACTIVE_USER_GUARD；
    否则用户 1 分钟内有主动对话/回复时拒绝主动说话。

    record_history: 是否记录到短期记忆。None 时默认 not proactive
        （主动说话=环境交互，不记录）；环境类调用点可显式传 False。
    msg_source: 写入长期记忆的来源标记（'chat'/'auto'）。
    """
    # 记录 AI 输出时间（用户驱动回复与自主输出均记录）。
    # RL 结算据此判定"用户是否回应了本次输出"：只有用户消息发生在
    # 输出之后才算回应 —— AI 自身动作触发的话不算用户输入。
    try:
        from rl_coordinator import get_coordinator
        get_coordinator().note_agent_output()
    except Exception:
        pass
    # 全局主动说话闸门：任何主动路径（非用户消息）都受此冷却约束
    if proactive:
        if state.active_task is not None and not state.active_task.done():
            return False  # AI 正在说话，主动路径不得打断
        if _turn_is_alive(state.user_id):
            return False  # 该用户的后台对话轮仍在执行（可能来自旧连接）：主动路径同样不得打断
        if not external_trigger:
            # 用户活跃窗口守卫：用户 1 分钟内有主动对话/回复 → 禁止主动说话，
            # 避免 AI 在用户活跃交流时不断插话"废话"。
            # 仅外部触发（用户召唤等）可突破此窗口。
            if time.time() - state.last_user_message_time < ACTIVE_USER_GUARD:
                return False
        last_speak = getattr(state, "_last_proactive_speak", 0.0)
        if time.time() - last_speak < ACTIVE_SPEAK_COOLDOWN:
            return False  # 全局冷却期内，主动路径一律拒绝
    if not allow_interrupt:
        if state.active_task is not None and not state.active_task.done():
            return False  # AI正在说话，被动事件不得打断
        if _turn_is_alive(state.user_id):
            return False  # 该用户的后台对话轮仍在执行：被动事件不得打断
        if time.time() - state.last_response_done < 3.0:
            return False  # 回复刚结束冷却期，被动事件不得打断
    # 每日轮次上限：只卡用户驱动的一轮，且放在取消当前轮之前 ——
    # 被限流时不该顺手把用户正在跑的那一轮掐掉。
    if not proactive and msg_source == "chat":
        if not await _quota_gate(ws, state):
            return False
    # 取消正在进行的回复（如果有）——只有用户输入（allow_interrupt=True）可打断
    if allow_interrupt:
        _cancel_active_turn(state.user_id)  # 取消该用户注册表中的活跃轮（含旧连接启动的）
    state.cancel_current()
    if state.active_task and not state.active_task.done():
        state.active_task.cancel()
    if proactive:
        state._last_proactive_speak = time.time()  # 记录全局主动说话时间
    sid = state.new_session()
    state.active_task = asyncio.create_task(
        handle_user_message_stream(
            _BROADCAST_WS, text, history, sid, state,
            current_model=state.current_model,
            current_background=state.current_background,
            current_bgm=state.current_bgm,
            # 主动说话也记录短期记忆（历史）：AI 主动说的话必须是后续对话的上下文，
            # 否则用户插话/搭话时 AI 对自己刚说过的话毫无印象。
            # 纯环境噪音路径（环境快照等）由调用点显式传 record_history=False 排除。
            record_history=True if record_history is None else record_history,
            msg_source=msg_source,
            proactive=proactive,
        )
    )
    # 登记为全局活跃轮：事件广播 + 进度缓冲，前端刷新重连可无缝接管
    _register_active_turn(state.active_task, sid, state, text, False)

    return True


async def _kickoff_resume(ws, state, checkpoint: dict) -> bool:
    """热重载/重启后：让角色自主恢复被打断的对话轮（断点续跑）。

    ws 可能为 None（尚无客户端重连）——事件推送静默跳过，对话轮仍会在后台
    跑完并落库，客户端重连后能看到完整结果。
    """
    cp_user = str(checkpoint.get("user_id") or "default")
    if cp_user in _resume_inflight:
        return False
    cp_sid = str(checkpoint.get("session_id") or "")
    try:
        agent = await get_shared_agent(cp_user)
        if agent.memory and cp_sid:
            await agent.sync_memory_namespace()
            if not await agent.memory.session_visible(cp_sid):
                # 角色卡片已切换：断点会话不属于当前命名空间，放弃恢复
                from agent import clear_ckpt_slot
                clear_ckpt_slot(cp_user, str(checkpoint.get("turn_id") or ""))
                logger.warning(f"[Resume] 断点会话不属于当前角色卡片命名空间，放弃: {cp_sid}")
                return False
            await agent.memory.set_session_id(cp_sid)
    except Exception as e:
        logger.warning(f"[Resume] 断点会话校验失败: {e}")
        return False

    history = await _ensure_global_history()
    if state is None:
        state = WSState()
    state.user_id = cp_user
    state.chat_session_id = cp_sid
    _TURN_UID.set(cp_user)  # 恢复轮的事件只投递给该用户的连接
    _resume_inflight.add(cp_user)

    # sid 前置创建：注册活跃轮时即可携带（事件广播/重连接管都靠它）
    sid = state.new_session()

    def _run():
        return handle_user_message_stream(
            _BROADCAST_WS, str(checkpoint.get("user_message") or ""), history,
            sid, state,
            current_model=checkpoint.get("current_model"),
            current_background=checkpoint.get("current_background"),
            current_bgm=checkpoint.get("current_bgm"),
            record_history=bool(checkpoint.get("record_history", True)),
            msg_source=checkpoint.get("msg_source") or "chat",
            proactive=bool(checkpoint.get("proactive", False)),
            resume_checkpoint=checkpoint,
        )

    async def _run_wrap():
        try:
            await _run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Resume] 对话轮恢复执行失败: {e}")
        finally:
            _resume_inflight.discard(cp_user)

    state.cancel_current()
    if state.active_task and not state.active_task.done():
        state.active_task.cancel()
    state.active_task = asyncio.create_task(_run_wrap())
    _register_active_turn(state.active_task, sid, state,
                          str(checkpoint.get("user_message") or ""), True)
    logger.info(f"[Resume] 已启动对话轮自主恢复（用户={cp_user}, 会话={cp_sid}）")
    return True


async def _resume_pending_turns() -> None:
    """启动后扫描对话轮断点并自主恢复（无客户端连接时静默完成，结果落记忆库）。"""
    try:
        from agent import list_turn_checkpoints, ckpt_is_fresh
    except Exception as e:
        logger.warning(f"[Resume] 断点扫描失败: {e}")
        return
    for cp in list_turn_checkpoints():
        # 用户主动/被动暂停的断点不自动重放：等用户说『继续』再恢复，
        # 避免服务重启后旧任务抢跑/与用户新指令冲突；只自动恢复进程被杀
        # （未标记 paused）的崩溃断点。
        if cp.get("paused"):
            continue
        # 只自动恢复新鲜窗口内的崩溃断点；过期的旧断点不重放
        # （避免服务重启后把几天前的旧任务抢跑回来）
        if not ckpt_is_fresh(cp):
            continue
        cp_user = str(cp.get("user_id") or "default")
        if cp_user in _resume_inflight:
            continue
        try:
            # 常驻状态恢复：与连接无关（服务重启后通常还没有任何页面连入），
            # 恢复的对话轮经广播代理跑完，结果落库 + 进全局历史
            await _kickoff_resume(_BROADCAST_WS, await _get_resident_state(), cp)
        except Exception as e:
            logger.warning(f"[Resume] 对话轮恢复启动失败: {e}")


async def _apply_dispatch_result(ws: WebSocket, result, history: list, state: WSState,
                                 msg_type: Optional[str] = None):
    """统一应用感知调度结果。

    由 PerceptionDispatcher.dispatch() 返回的 DispatchResult 驱动：
    - behavior_cmd → 发送给前端执行
    - trigger_text + should_speak → 启动 LLM 回复

    msg_type 用于判定外部触发：用户召唤（proactive）属于外部触发，
    用户在场互动，可突破活跃窗口守卫；
    其余自动路径（大厅环境快照等）在用户活跃窗口内被抑制。
    """
    if result.behavior_cmd:
        await safe_send_json(ws, result.behavior_cmd)
    if result.should_speak and result.trigger_text:
        external = result.category == EventCategory.SUMMON
        await _kickoff_response(
            ws, result.trigger_text, history, state,
            proactive=True,  # 感知派发属于主动说话，走全局闸门
            external_trigger=external,  # 外部触发可突破用户活跃窗口
            msg_source="auto",  # 环境交互：由记忆系统处理，不进短期记忆
        )


async def _handle_rl_decision(ws: WebSocket, data: dict, state: WSState, history: list = None):
    """统一调度决策入口 —— RL 统摄派发所有 Agent。

    流程：
    1. 构建统一状态
    2. coord.schedule() 决策：Agent路由（ai_agent / engagement / silence）
    3. 执行派发：
       - ai_agent    → 回传主动说话触发（前端 sendAIAction 触发 AIAgent 回复）
       - engagement  → 回传行为指令（行为引擎）
       - silence     → 不派发
    4. 回传 rl_dispatch 计划给前端执行
    """
    from rl_coordinator import AgentChoice
    try:
        coord = get_coordinator()

        # 1. 构建统一状态
        event = data.get("event", "proactive_tick")
        unified = coord.build_state(
            user_engaged=(time.time() - state.last_user_message_time) < 120,
            seconds_since_user_message=max(0, time.time() - state.last_user_message_time),
            seconds_since_interaction=max(0, time.time() - state.last_response_done),
            affection=state.affection, trust=state.trust, intimacy=state.intimacy,
            user_emotion=state.user_emotion,
            proactive_success_rate=(state.proactive_successes / state.proactive_attempts)
            if state.proactive_attempts else 0.0,
            last_user_message_ts=state.last_user_message_time,
        )
        # 前端回传覆盖（rl_sync 已同步过关系状态）
        if "affection" in data: unified.affection = float(data["affection"])
        if "emotion" in data: unified.user_emotion = int(data["emotion"])

        # 2. RL 统一调度（传入自主行为中枢：engagement 分支生成真实行为指令）
        forced = data.get("forced_agent")
        plan = coord.schedule(unified, forced_agent=forced, event=event,
                              autonomy=state.autonomy)

        # 3. 派发执行
        if plan.agent_choice == AgentChoice.AI_AGENT:
            # 主动说话触发（非打断；AI 空闲时才执行）
            # 全局主动说话闸门由 _kickoff_response(proactive=True) 统一管控
            if plan.speak_text:
                await _kickoff_response(
                    ws, plan.speak_text, history or [], state,
                    proactive=True,
                    msg_source="auto",  # RL 主动说话：环境交互，不进短期记忆
                )

        # silence / engagement：不额外执行（前端按 plan 自行处理）

        # 4. 回传调度计划（含 RL 决策的快照间隔）
        plan_data = plan.to_dict()
        snapshot_interval = coord.snapshot_interval(unified)
        state._current_snapshot_interval = snapshot_interval
        plan_data["snapshot_interval"] = snapshot_interval
        plan_data["interval_mode"] = "lobby"
        # 真实调用点：决策后触发离线训练（rlhf 偏好校准 + 世界模型经验训练）
        try:
            plan_data["offline"] = _maybe_train_offline(coord)
        except Exception as oe:
            logger.warning(f"rl_decision 离线训练触发失败: {oe}")
        await safe_send_json(ws, {"type": "rl_dispatch", "data": plan_data})
    except Exception as e:
        logger.warning(f"rl_decision 处理失败: {e}")
        await safe_send_json(ws, {"type": "rl_dispatch", "data": {}})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # 公网来源必须带有效会话 cookie 才能握手（局域网直连免鉴权）：
    # HTTP 中间件管不到 WebSocket scope，这里单独把门。
    if _is_public_request(ws) and not _session_uid(ws):
        await ws.close(code=4401)
        return
    # 先显式完成 WebSocket 握手再进入消息循环：
    # 热重载/整进程重启瞬间，连接可能已处于半死状态，直接收消息会报
    # "WebSocket is not connected. Need to call accept first" 并让前端永久卡在“连接中”。
    # 握手失败就放弃该连接（前端有自动重连，3 秒超时后重试）。
    try:
        await ws.accept()
    except Exception as e:
        print(f"[WS] 握手失败，放弃连接: {e}")
        return
    manager.active.append(ws)
    # 该连接所属用户的历史镜像（公网登录用户按账号隔离）
    history: list = await _ensure_global_history(_session_uid(ws) or _unified_user_id())
    state = WSState()
    state._ws_history = history  # 子智能体汇报等外部触发复用同一份短期记忆
    _last_chat_conn["ws"] = ws
    _last_chat_conn["state"] = state
    try:
        await safe_send_json(ws, {"type": "ready", "message": "连接成功"})
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            # === AI对话保护门：说话期间仅用户文字/语音输入可以打断 ===
            # 基础设施消息始终放行；感知事件由 PerceptionDispatcher 内部自行保护
            _ALWAYS_ALLOW = {
                "ping", "set_user", "list_sessions", "switch_session",
                "new_session", "delete_session", "search_sessions",
                "rename_session", "pin_session", "archive_session",
                "set_avatar",
                "set_background", "set_bgm",
                "anim_state",  # 前端动作状态上报（说话时大白知道自己的当前动作）
                "interrupt", "rl_sync", "rl_decision",
                "music_end", "music_stop", "video_stop",   # 媒体子智能体回报事件
            }
            _PERCEPTION_EVENTS = {
                "environment_snapshot", "ai_behavior_result", "proactive",
            }
            _USER_INPUT = {"text", "audio"}
            _RESPONSE_COOLDOWN = 3.0  # 回复完成后冷却秒数，防止被动事件立即打断
            if mtype not in _ALWAYS_ALLOW and mtype not in _PERCEPTION_EVENTS and mtype not in _USER_INPUT:
                # AI正在说话：直接丢弃
                if state.active_task is not None and not state.active_task.done():
                    continue
                # 后台活跃轮仍在执行（可能来自旧连接，页面刷新后新连接 state 为空）：
                # 同样丢弃，保证只有用户文字/语音输入能打断
                if _turn_is_alive(state.user_id):
                    continue
                # AI刚说完：冷却期内丢弃，保证所有语音句段播放完
                if time.time() - state.last_response_done < _RESPONSE_COOLDOWN:
                    continue

            # === 动作状态上报（前端播放/停止库动作时实时同步） ===
            if mtype == "anim_state":
                anim = msg.get("anim")
                if isinstance(anim, dict):
                    state.current_anim = {
                        "name": str(anim.get("name") or "")[:60],
                        "category": str(anim.get("category") or "")[:20],
                        "emotion": str(anim.get("emotion") or "")[:20],
                    }
                else:
                    state.current_anim = None
                continue

            # === 心跳 ===
            if mtype == "ping":
                await safe_send_json(ws, {"type": "pong"})
                continue

            # === 用户身份设置 ===
            if mtype == "set_user":
                # 身份优先级：登录 cookie > 前端浏览器 uid > 统一身份。
                # 前端每个浏览器 localStorage 持有独立随机 uid——局域网下管理员/访客
                # 各用各的桶，不再全部坍缩成同一个 default（那会让两台浏览器共用
                # 同一个 Agent 实例与 session_id，互相串线）。公网 ws 已强制登录，
                # 此分支只会走到 cookie 路径，前端 uid 不会在公网被采纳。
                state.user_id = (
                    _session_uid(ws)
                    or str(msg.get("user_id") or "").strip()
                    or _unified_user_id()
                )
                manager.bind_user(ws, state.user_id)
                _TURN_UID.set(state.user_id)
                # 预初始化该用户的 Agent
                agent = await get_shared_agent(state.user_id)
                # 绑定当前活动角色卡片的记忆命名空间（角色卡片独立记忆空间）
                if agent.memory:
                    await agent.sync_memory_namespace()
                state.chat_session_id = agent.memory.session_id if agent.memory else None
                # 全局共享历史：同一用户复用同一份列表，不按连接重建
                history = await _ensure_global_history(state.user_id)
                state._ws_history = history
                await safe_send_json(ws, {
                    "type": "user_set",
                    "user_id": state.user_id,
                    "chat_session_id": state.chat_session_id,
                    "history": _history_for_client(history),
                })
                # 页面刷新/断线重连：后台对话轮仍在执行 → 发送进度快照，
                # 前端无缝接管回合气泡；后续事件经 _BroadcastWS 广播自然到达。
                # 不再触发断点续跑，避免同一任务被重复执行（旧轮还在跑）。
                if _turn_is_alive(state.user_id):
                    t = _turn_of(state.user_id)
                    await safe_send_json(ws, {
                        "type": "turn_in_progress",
                        "session_id": t.session_id,
                        "resume": t.resume,
                        "full_text": t.full_text,
                        "reasoning_tail": t.reasoning_tail,
                        "tool_events": t.tool_events[-40:],
                    })
                    continue
                # 热重载/重启后：若仍有未完成的对话轮断点，向当前连接流式恢复
                try:
                    from agent import load_turn_checkpoint, ckpt_is_fresh
                    cp = load_turn_checkpoint(state.user_id)
                    # 只自动恢复进程被杀（未标记 paused）的崩溃断点；
                    # 用户暂停的断点等用户说『继续』，不在重连时抢跑
                    # 且必须仍在新鲜窗口内，过期旧任务不重放
                    if (cp and not cp.get("paused")
                            and ckpt_is_fresh(cp)
                            and state.user_id not in _resume_inflight):
                        asyncio.create_task(_kickoff_resume(ws, state, cp))
                except Exception as e:
                    logger.warning(f"[Resume] 重连恢复触发失败: {e}")
                continue

            # === 会话管理 ===
            if mtype == "list_sessions":
                agent = await get_shared_agent(state.user_id)
                # 同步到当前活动角色卡片的记忆空间，确保会话列表只显示本卡片的历史
                await agent.sync_memory_namespace()
                q = (msg.get("q") or "").strip()
                include_archived = bool(msg.get("include_archived", False))
                sessions = await agent.get_sessions(
                    query=q or None, include_archived=include_archived)
                await safe_send_json(ws, {
                    "type": "session_list", "sessions": sessions, "query": q})
                continue

            if mtype == "search_sessions":
                # 会话搜索（覆盖归档会话）：结果仍以 session_list 下发，前端复用渲染
                agent = await get_shared_agent(state.user_id)
                await agent.sync_memory_namespace()
                q = (msg.get("q") or "").strip()
                sessions = await agent.get_sessions(query=q or None)
                await safe_send_json(ws, {
                    "type": "session_list", "sessions": sessions, "query": q})
                continue

            if mtype == "switch_session":
                sid = msg.get("session_id", "")
                if sid:
                    agent = await get_shared_agent(state.user_id)
                    # 先绑定当前角色卡片的记忆空间，拒绝跨卡片会话（防止串记忆）
                    await agent.sync_memory_namespace()
                    if not await agent.memory.session_visible(sid):
                        await safe_send_json(ws, {"type": "error", "message": "该会话不属于当前角色卡片"})
                        continue
                    await agent.switch_session(sid)
                    state.chat_session_id = sid
                    # 完整历史（最多 300 轮）供对话栏渲染；summary 为新会话摘要
                    new_hist = await agent.get_session_history(sid, max_rounds=300)
                    history[:] = new_hist          # 原地更新全局共享列表
                    state._ws_history = history
                    summary = await agent.get_session_summary(sid)
                    await safe_send_json(ws, {
                        "type": "session_switched",
                        "session_id": sid,
                        "history": _history_for_client(history),
                        "summary": summary,
                    })
                continue

            if mtype == "new_session":
                agent = await get_shared_agent(state.user_id)
                await agent.close_current_session()
                # 绑定当前角色卡片的记忆空间后再创建新会话
                await agent.sync_memory_namespace()
                # 「新对话」按钮是唯一开新对话的入口，这里必须真新建：
                # get_or_create_session 会接管命名空间里最近的活跃会话，
                # 于是点了新对话却回到旧对话里。
                await agent.memory.create_new_session()
                state.chat_session_id = agent.memory.session_id
                history[:] = []                    # 原地清空全局共享列表
                state._ws_history = history
                await safe_send_json(ws, {
                    "type": "session_created",
                    "session_id": state.chat_session_id,
                })
                continue

            if mtype == "rename_session":
                sid = msg.get("session_id", "")
                title = (msg.get("title") or "").strip()
                if sid and title:
                    agent = await get_shared_agent(state.user_id)
                    await agent.sync_memory_namespace()
                    if await agent.memory.session_visible(sid):
                        await agent.rename_session(sid, title)
                        await safe_send_json(ws, {
                            "type": "session_renamed", "session_id": sid, "title": title[:60]})
                continue

            if mtype == "pin_session":
                sid = msg.get("session_id", "")
                pinned = bool(msg.get("pinned", True))
                if sid:
                    agent = await get_shared_agent(state.user_id)
                    await agent.set_session_pinned(sid, pinned)
                    await safe_send_json(ws, {
                        "type": "session_pinned", "session_id": sid, "pinned": pinned})
                continue

            if mtype == "archive_session":
                sid = msg.get("session_id", "")
                archived = bool(msg.get("archived", True))
                if sid:
                    agent = await get_shared_agent(state.user_id)
                    await agent.set_session_archived(sid, archived)
                    if archived and sid == state.chat_session_id:
                        # 归档即当前会话：对话栏与全局历史清空（下次发消息自动续新会话）
                        state.chat_session_id = None
                        history[:] = []
                        state._ws_history = history
                        await safe_send_json(ws, {
                            "type": "session_archived", "session_id": sid,
                            "archived": True, "active_changed": True})
                    else:
                        await safe_send_json(ws, {
                            "type": "session_archived", "session_id": sid, "archived": archived})
                continue

            if mtype == "delete_session":
                sid = msg.get("session_id", "")
                if sid:
                    agent = await get_shared_agent(state.user_id)
                    await agent.delete_session(sid)
                    if sid == state.chat_session_id:
                        # 删除的是当前会话：自动续新会话，避免对话栏停在已删会话上
                        await agent.sync_memory_namespace()
                        agent.memory.session_id = None
                        await agent.memory.get_or_create_session()
                        state.chat_session_id = agent.memory.session_id
                        history[:] = []             # 原地清空全局共享列表
                        state._ws_history = history
                        await safe_send_json(ws, {
                            "type": "session_deleted", "session_id": sid,
                            "next_session_id": state.chat_session_id})
                        await safe_send_json(ws, {
                            "type": "session_created",
                            "session_id": state.chat_session_id})
                    else:
                        await safe_send_json(ws, {
                            "type": "session_deleted", "session_id": sid})
                continue

            # === 当前形象与场景同步 ===
            if mtype == "set_avatar":
                state.current_model = msg.get("name") or None
                await safe_send_json(ws, {
                    "type": "avatar_set",
                    "name": state.current_model,
                })
                continue

            if mtype == "set_background":
                state.current_background = msg.get("name") or None
                await safe_send_json(ws, {
                    "type": "background_set",
                    "name": state.current_background,
                })
                continue

            if mtype == "set_bgm":
                state.current_bgm = msg.get("name") or None
                await safe_send_json(ws, {
                    "type": "bgm_set",
                    "name": state.current_bgm,
                })
                continue

            # === RL 统一调度（统摄所有 Agent 派发） ===
            if mtype == "rl_decision":
                asyncio.create_task(_handle_rl_decision(ws, msg.get("data", {}), state, history))
                continue

            # === RL 离线模块反馈 / 训练（真实调用点） ===
            if mtype == "rlhf_feedback":
                # 前端/测试投递偏好对比对 {pairs:[{good:{s,a,r}, bad:{s,a,r}}]}
                data = msg.get("data", {})
                coord = get_coordinator()
                accepted = coord.record_rlhf_feedback(data.get("pairs") or [])
                report = coord.train_offline(force=bool(data.get("force")))
                await safe_send_json(ws, {
                    "type": "rlhf_feedback_ack",
                    "data": {"accepted": accepted, "offline": report},
                })
                continue

            if mtype == "rl_train":
                # 手动触发离线训练（偏好校准 + 世界模型经验训练）
                coord = get_coordinator()
                report = coord.train_offline(force=True)
                await safe_send_json(ws, {"type": "rl_train_done", "data": report})
                continue

            # === RL 统一状态同步（前端回传关系状态，非游戏模式纳入 RL 统摄） ===
            if mtype == "rl_sync":
                data = msg.get("data", {})
                if "affection" in data: state.affection = float(data["affection"])
                if "trust" in data: state.trust = float(data["trust"])
                if "intimacy" in data: state.intimacy = float(data["intimacy"])
                if "emotion" in data: state.user_emotion = int(data["emotion"])
                if "proactive_success" in data: state.proactive_successes += 1
                if "proactive_attempt" in data: state.proactive_attempts += 1
                # 需要 RL 统一调度决策 → 走统一入口
                if data.get("want_decision"):
                    await _handle_rl_decision(ws, data, state, history)
                    continue
                # 同步到 RL 协调器统一状态
                coord = get_coordinator()
                unified = coord.build_state(
                    seconds_since_user_message=max(0, time.time() - state.last_user_message_time),
                    affection=state.affection, trust=state.trust, intimacy=state.intimacy,
                    user_emotion=state.user_emotion,
                    last_user_message_ts=state.last_user_message_time,
                )
                stats = coord.get_stats()
                # RL 决策快照间隔（秒）
                snapshot_interval = coord.snapshot_interval(unified)
                state._current_snapshot_interval = snapshot_interval
                await safe_send_json(ws, {
                    "type": "rl_status",
                    "data": {
                        "affection": state.affection,
                        "trust": state.trust,
                        "intimacy": state.intimacy,
                        "mode": int(unified.mode),
                        "mode_name": unified.mode.name,
                        "curiosity": round(unified.curiosity_level, 3),
                        "reward": round(stats["total_reward"], 3),
                        "decisions": stats["decisions"],
                        "proactive_actions": stats["proactive_actions"],
                        "bandit_contexts": stats.get("bandit", {}).get("contexts", 0),
                        "snapshot_interval": snapshot_interval,
                        "interval_mode": "lobby",
                        "interval_slot": coord.snapshot_interval_slot(unified),
                        "rlhf_sep": stats.get("offline", {}).get("rlhf_sep"),
                        "wm_updates": stats.get("offline", {}).get("wm_updates"),
                        "offline_trains": stats.get("offline", {}).get("train_count"),
                    },
                })
                continue

            # === 统一感知路由调度 ===
            # 大厅感知事件（environment_snapshot/ai_behavior_result/proactive）
            # 由 PerceptionDispatcher 统一路由，含保护检查、冷却控制、触发决策
            if mtype in _PERCEPTION_EVENTS:
                result = state.perception_dispatcher.dispatch(
                    msg_type=mtype,
                    msg=msg,
                    autonomy=state.autonomy,
                    is_speaking=(state.active_task is not None and not state.active_task.done()),
                    last_response_time=state.last_response_done,
                    last_user_message_time=state.last_user_message_time,
                    current_model=state.current_model,
                    current_background=state.current_background,
                    current_bgm=state.current_bgm,
                    last_active_speak=getattr(state, "_last_proactive_speak", 0),
                    active_speak_cooldown=ACTIVE_SPEAK_COOLDOWN,
                )
                await _apply_dispatch_result(ws, result, history, state, mtype)
                continue

            if mtype == "ai_moving":
                # 前端通知 AI 正在自主移动
                state.ai_is_moving = msg.get("moving", False)
                continue

            # === 媒体子智能体回报（前端播放事件 → 闭环看护 worker） ===
            if mtype == "music_end":
                # <audio> 单曲/歌单最后一首播完
                asyncio.create_task(_handle_music_end(ws, msg))
                continue

            if mtype == "music_stop":
                asyncio.create_task(_handle_music_stop(ws, msg))
                continue

            if mtype == "video_stop":
                asyncio.create_task(_handle_video_stop(ws, msg))
                continue

            # === 对话打断（停止/按住说话/VAD 打断统一确认：取消任务并回执，
            #      前端据此复位状态徽章，避免打断后一直卡在「说话中」） ===
            if mtype == "interrupt":
                _interrupted_sid = state.current_session
                # 工具任务执行中：VAD 自动打断不得掐掉正在跑的工具（前端在工具轮
                # 不再发 interrupt；看门狗「中断」按钮带 force=true 仍立即取消）
                if _turn_in_tools(state.user_id) and not msg.get("force"):
                    await safe_send_json(ws, {
                        "type": "system_msg",
                        "text": "⏳ 工具任务执行中，不打断（说『停止』或点中断按钮才真停）",
                    })
                    continue
                # 跨连接：后台注册表中的活跃轮（旧连接启动的）一并取消
                _cancel_active_turn(state.user_id)
                state.cancel_current()
                if state.active_task and not state.active_task.done():
                    state.active_task.cancel()
                await safe_send_json(ws, {"type": "interrupted",
                                          "session_id": _interrupted_sid or ""})
                continue

            # === AI 自主行为触发（跳舞/走动/自言自语等） ===
            # 与用户输入严格区分：
            # 1. 不更新 last_user_message_time → 不视为"用户回应"，不产生 RL 正向奖励
            # 2. 不加好感度/信任值 → AI 自己的动作不给自己加关系奖励
            # 3. 不打断 AI → 保护门已保证：AI 说话中/冷却期内该消息被直接丢弃，
            #    只有用户 text/audio 输入（_USER_INPUT）才能打断 AI
            if mtype == "ai_action":
                text = msg.get("content", "")
                if not text.strip():
                    continue
                # 用户活跃窗口守卫：用户 1 分钟内有主动对话 →
                # 抑制 AI 自主说话（好奇心自言自语/问候等），不"一直废话"。
                if time.time() - state.last_user_message_time < ACTIVE_USER_GUARD:
                    continue
                # 环境交互：不进短期记忆（history），由记忆系统处理
                await _kickoff_response(ws, text, history, state,
                                        record_history=False, msg_source="auto")
                continue

            if mtype == "text":
                text = msg.get("content", "")
                # 附件（图片/文件）→ 文本提示 + [[IMG:]] 标记，与用户文字合成一条消息
                att_prompt = await asyncio.to_thread(
                    _attachments_prompt, msg.get("attachments"))
                if att_prompt:
                    text = f"{text}\n\n{att_prompt}".strip()
                if not text.strip():
                    continue
                # ui=True 的系统点击消息不进短期记忆（由记忆系统按 auto 处理）
                ui_auto = msg.get("ui") is True
                # 自然语言统一交给主 Agent（harness 底座）；斜杠命令通道已移除
                state.last_user_message_time = time.time()
                state.last_response_done = time.time() + 86400
                # 用户互动 → AI 好奇心涨
                state.autonomy.record_user_interaction()
                # RL 统摄：用户主动消息 → 关系状态微涨（外源奖励）
                state.affection = min(100.0, state.affection + 0.3)
                state.trust = min(100.0, state.trust + 0.15)
                state.user_emotion = 0  # 用户主动交流 → 视为积极
                # RL 统摄：记录强制路由（ai_agent），让 bandit 从用户驱动对话学习
                try:
                    from rl_coordinator import AgentChoice
                    coord = get_coordinator()
                    unified = coord.build_state(
                        seconds_since_user_message=0.0,
                        affection=state.affection, trust=state.trust, intimacy=state.intimacy,
                        user_emotion=state.user_emotion,
                        last_user_message_ts=state.last_user_message_time,
                    )
                    coord.settle_forced(AgentChoice.AI_AGENT, unified)
                except Exception as e:
                    logger.warning(f"RL强制路由记录失败: {e}")
                _ai_busy = (_turn_is_alive(state.user_id)
                            or (state.active_task is not None
                                and not state.active_task.done()))
                if _ai_busy and _is_stop_word(text):
                    _cancel_active_turn(state.user_id)
                    await safe_send_json(ws, {"type": "system_msg", "text": "⏹ 已停止"})
                    continue
                if _ai_busy and ui_auto:
                    continue  # 界面自动上报的消息不打断进行中的回复
                # 用户打字 = 打断意图：取消当前轮（落「已暂停」断点，说『继续』可恢复）后开新轮
                await _kickoff_response(
                    ws, text, history, state, allow_interrupt=True,
                    record_history=not ui_auto,
                    msg_source="auto" if ui_auto else "chat",
                )

            elif mtype == "audio":
                data = msg.get("data", "")
                now = time.time()
                # 注意：不在此处更新 last_user_message_time / 用户互动状态！
                # VAD 可能被环境噪音触发，只有 STT 转文字成功才算用户回复，
                # 否则环境噪音会让 RL 误判"用户回应了主动说话"，强化自言自语。
                if not data:
                    continue

                # STT 失败冷却：连续失败 N 次后暂停处理音频
                # 防止反复消耗 ffmpeg/whisper 资源 + 误抑制 AI 自主行为
                STT_FAIL_MAX = 3
                STT_FAIL_WINDOW = 10.0
                STT_COOLDOWN = 8.0
                if state._stt_cooldown_until > now:
                    continue
                if now - state._stt_fail_window_start > STT_FAIL_WINDOW:
                    state._stt_fail_count = 0
                    state._stt_fail_window_start = now
                # 兼容 data URL 与纯 base64
                if "," in data and data.startswith("data:"):
                    data = data.split(",", 1)[1]
                try:
                    audio_bytes = base64.b64decode(data)
                except Exception:
                    await safe_send_json(ws, {"type": "error", "message": "音频解码失败"})
                    continue

                # 根据前端实际录音格式选择扩展名（兼容 webm/ogg/mp4）
                mime_type = msg.get("mime_type", "audio/webm")
                ext_from_mime = _mime_to_ext(mime_type)
                ext_from_bytes = _detect_ext_from_bytes(audio_bytes)
                # 选择扩展名策略：
                # 当 MIME 声称是容器格式但数据没有对应魔数时，说明数据已损坏或浏览器封包错误，
                # 此时信任实际字节检测结果（裸编解码器数据），避免 ffmpeg 用错误的 demuxer 崩溃
                if ext_from_mime == ".webm" and ext_from_bytes != ".webm":
                    # MIME 说 webm 但数据不以 EBML 头 (0x1A45DFA3) 开头，浏览器封包出错
                    print(f"[STT] MIME 声称 webm 但数据无 EBML 头 (魔数={audio_bytes[:4].hex()})，"
                          f"改用字节检测结果: {ext_from_bytes}")
                    ext = ext_from_bytes
                elif ext_from_mime == ".ogg" and ext_from_bytes != ".ogg":
                    ext = ext_from_bytes
                elif ext_from_mime == ".m4a" and ext_from_bytes != ".m4a":
                    ext = ext_from_bytes
                else:
                    ext = ext_from_mime
                tmp_in = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp_in.write(audio_bytes)
                tmp_in.close()
                # 输出 wav 必须与输入不同名：前端 PCM 直传路径的 ext 本身就是 .wav，
                # 若用 replace(ext, ".wav") 会得到同一文件 → ffmpeg 读写同一文件直接崩溃
                tmp_wav = os.path.splitext(tmp_in.name)[0] + "_16k.wav"

                print(f"[STT] 接收音频: mime={mime_type}, 大小={len(audio_bytes)}, "
                      f"mime_ext={ext_from_mime}, bytes_ext={ext_from_bytes}, "
                      f"魔数={audio_bytes[:8].hex()}, tmp={tmp_in.name}")

                text = ""
                try:
                    # 第一遍：快速路径（仅裁剪静音，干净音频足够，速度快）
                    ok = await asyncio.to_thread(convert_to_wav, tmp_in.name, tmp_wav, False)
                    if ok:
                        text = await asyncio.to_thread(speech_to_text, tmp_wav)
                    # 第二遍：降噪路径（环境噪声大导致首轮识别失败时重试，提升准确率）
                    if ok and not text:
                        print("[STT] 首轮无结果，启用降噪滤波重试…")
                        ok = await asyncio.to_thread(convert_to_wav, tmp_in.name, tmp_wav, True)
                        if ok:
                            text = await asyncio.to_thread(speech_to_text, tmp_wav)
                except Exception as e:
                    print(f"[STT] ffmpeg/识别异常: {e}")
                # 清理临时文件
                for p in (tmp_in.name, tmp_in.name.replace(".opus", ".webm"), tmp_wav):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

                if not ok:
                    # ffmpeg 转换失败（可能数据损坏），通知前端重建 VAD
                    state._stt_fail_count += 1
                    if state._stt_fail_count >= STT_FAIL_MAX:
                        state._stt_cooldown_until = now + STT_COOLDOWN
                        print(f"[STT] 连续失败{state._stt_fail_count}次，暂停{STT_COOLDOWN}秒")
                    # STT 失败 → 恢复 last_response_done，避免 +86400 残留导致被动事件被永久抑制（定时播报卡死）
                    state.last_response_done = time.time()
                    await safe_send_json(ws, {"type": "restart_vad", "reason": "音频转码失败，正在重建语音模式…"})
                    continue
                if not text:
                    state._stt_fail_count += 1
                    if state._stt_fail_count >= STT_FAIL_MAX:
                        state._stt_cooldown_until = now + STT_COOLDOWN
                        print(f"[STT] 连续失败{state._stt_fail_count}次，暂停{STT_COOLDOWN}秒")
                    # STT 失败 → 恢复 last_response_done，避免 +86400 残留导致被动事件被永久抑制（定时播报卡死）
                    state.last_response_done = time.time()
                    await safe_send_json(ws, {"type": "error", "message": "没听清，请再说一遍~"})
                    continue
                # STT 成功 → 重置失败计数
                state._stt_fail_count = 0
                state._stt_cooldown_until = 0
                # 只有转文字成功（有实际语音内容）才算用户回复，排除环境噪音
                state.last_user_message_time = time.time()
                state.last_response_done = time.time() + 86400
                # 用户互动 → AI 好奇心涨
                state.autonomy.record_user_interaction()
                # RL 统摄：用户主动消息 → 关系状态微涨（外源奖励）
                state.affection = min(100.0, state.affection + 0.3)
                state.trust = min(100.0, state.trust + 0.15)
                state.user_emotion = 0  # 用户主动交流 → 视为积极
                # RL 统摄：记录强制路由（ai_agent），让 bandit 从用户驱动对话学习
                try:
                    from rl_coordinator import AgentChoice
                    coord = get_coordinator()
                    unified = coord.build_state(
                        seconds_since_user_message=0.0,
                        affection=state.affection, trust=state.trust, intimacy=state.intimacy,
                        user_emotion=state.user_emotion,
                        last_user_message_ts=state.last_user_message_time,
                    )
                    coord.settle_forced(AgentChoice.AI_AGENT, unified)
                except Exception as e:
                    logger.warning(f"RL强制路由记录失败: {e}")
                await safe_send_json(ws, {"type": "transcript", "text": text})
                # 语音插话分两种命运：
                #   ① 本轮尚未进入工具执行 → 照旧打断（正常对话插话，体验不变）
                #   ② 本轮已在跑工具 → 不取消，只把话排队，等这轮干完自动补发
                #      （VAD 被环境噪音/随口一句话触发，曾把整轮工具任务掐掉）
                # 例外：明确的停止词（「停止」「停」…）是急停指令，永远立即生效。
                if _is_stop_word(text):
                    _cancel_active_turn(state.user_id)
                    state.cancel_current()
                    if state.active_task and not state.active_task.done():
                        state.active_task.cancel()
                    await safe_send_json(ws, {"type": "system_msg", "text": "⏹ 已停止"})
                    continue
                t_active = _turn_of(state.user_id)
                if t_active is not None and not t_active.done and t_active.tool_events:
                    t_active.pending_voice.append(text)
                    await safe_send_json(ws, {
                        "type": "system_msg",
                        "text": "⏳ 工具任务执行中，这句先排队——它干完就回你",
                    })
                    continue
                # 每日额度：语音与打字同一口径 —— 都在这里开跑，都要扣
                if not await _quota_gate(ws, state):
                    continue
                # 语音转文字成功 = 用户输入：可打断当前轮（含旧连接启动的后台活跃轮）
                _cancel_active_turn(state.user_id)
                state.cancel_current()
                if state.active_task and not state.active_task.done():
                    state.active_task.cancel()
                sid = state.new_session()
                state.active_task = asyncio.create_task(
                    handle_user_message_stream(_BROADCAST_WS, text, history, sid, state,
                        current_model=state.current_model,
                        current_background=state.current_background,
                        current_bgm=state.current_bgm)
                )
                _register_active_turn(state.active_task, sid, state, text, False)

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        # 重启/握手竞态下连接可能未真正建立：按断开处理，不打 traceback 刷屏
        if "accept" in str(e) or "not connected" in str(e).lower():
            print(f"[WS] 连接未建立/已失效，按断开处理: {e}")
            manager.disconnect(ws)
            return
        import traceback
        print(f"[WS] 错误: {e}")
        traceback.print_exc()
        manager.disconnect(ws)
    finally:
        # 单系统模式：连接关闭不取消回复任务——让 AI 把话说完、把结果写进记忆，
        # 用户刷新/重新进入后能看到完整回复（safe_send_json 对已断连接静默降级）。
        # 只有该连接自己后续重连前主动发起的新一轮会正常取代旧会话。
        pass


# ==================== 百科题库服务（寻宝游戏答题） ====================
# 题目来源：天行数据「百科题库」接口（答案与解析保存在服务端，答完才下发），
# 接口不可用时自动回退到内置本地题库，保证游戏始终可玩。
# 密钥不再硬编码：由 deploy/secrets 注入环境变量（systemd EnvironmentFile）。
# 取不到时 _fetch_tianapi 直接返回 None，自动回退内置本地题库，功能不中断。
TIANAPI_QUIZ_KEY = os.environ.get("DABAI_TIANAPI_QUIZ_KEY", "").strip()
TIANAPI_QUIZ_HOST = "apis.tianapi.com"
TIANAPI_QUIZ_PATH = "/baiketiku/index"
QUIZ_PENDING_TTL = 3600  # 待校验题目保留时长（秒）


class QuizService:
    """寻宝游戏答题服务：拉取题目（不含答案）供前端作答，答后校验并返回解析。"""

    def __init__(self):
        self._queue: list = []          # 待下发题目（含答案，服务端保管）
        self._pending: dict = {}        # id -> 题目（下发后等待校验）
        self._checked: dict = {}        # id -> 校验结果缓存（防重放）
        self._lock = threading.Lock()
        self._local_idx = 0
        self._local_bank = self._build_local_bank()

    # ---------- 本地题库（天行接口不可用时的兜底） ----------
    def _build_local_bank(self) -> list:
        return [
            {"title": "下面哪个是农历五月的别称？", "options": {"A": "杏月", "B": "桃月", "C": "阳月", "D": "榴月"},
             "answer": "D", "analytic": "以花命名的农历月份别称：正月柳月、二月杏月、三月桃月、四月槐月、五月榴月、六月荷月、七月巧月、八月桂月、九月菊月、十月阳月、十一月葭月、腊月梅月。"},
            {"title": "二十四节气中，标志着“天气回暖、春雷始鸣、万物复苏”的是哪个节气？", "options": {"A": "立春", "B": "惊蛰", "C": "春分", "D": "清明"},
             "answer": "B", "analytic": "惊蛰又名“启蛰”，意为春雷乍动、惊醒了蛰伏的昆虫，天气回暖，万物开始复苏。"},
            {"title": "我国最长的河流是哪一条？", "options": {"A": "黄河", "B": "珠江", "C": "长江", "D": "黑龙江"},
             "answer": "C", "analytic": "长江全长约6300公里，是我国最长的河流，也是世界第三长河。"},
            {"title": "世界上海拔最高的山峰是？", "options": {"A": "乔戈里峰", "B": "珠穆朗玛峰", "C": "贡嘎山", "D": "冈仁波齐"},
             "answer": "B", "analytic": "珠穆朗玛峰海拔8848.86米，是地球上最高的山峰，位于中国与尼泊尔边境。"},
            {"title": "太阳系中体积最大的行星是？", "options": {"A": "土星", "B": "海王星", "C": "木星", "D": "天王星"},
             "answer": "C", "analytic": "木星是太阳系中体积和质量最大的行星，直径约为地球的11倍。"},
            {"title": "光在真空中的传播速度大约是多少？", "options": {"A": "3万千米/秒", "B": "30万千米/秒", "C": "300万千米/秒", "D": "3千米/秒"},
             "answer": "B", "analytic": "光在真空中的传播速度约为每秒30万千米（约3×10⁸ m/s）。"},
            {"title": "古诗《静夜思》的作者是谁？", "options": {"A": "杜甫", "B": "白居易", "C": "王维", "D": "李白"},
             "answer": "D", "analytic": "《静夜思》（床前明月光）是唐代大诗人李白的代表作之一。"},
            {"title": "人体最大的器官是？", "options": {"A": "肝脏", "B": "皮肤", "C": "大脑", "D": "肺"},
             "answer": "B", "analytic": "皮肤覆盖全身，总面积约1.5～2平方米，是人体面积最大、最重的器官。"},
            {"title": "水的化学式是什么？", "options": {"A": "H2O2", "B": "CO2", "C": "H2O", "D": "O2"},
             "answer": "C", "analytic": "水的化学式是 H₂O，由两个氢原子和一个氧原子构成。"},
            {"title": "一年中白天最长的一天是哪个节气？", "options": {"A": "春分", "B": "秋分", "C": "冬至", "D": "夏至"},
             "answer": "D", "analytic": "夏至这天太阳直射北回归线，北半球白昼最长、黑夜最短。"},
            {"title": "下列哪一项不属于中国的“四大发明”？", "options": {"A": "地动仪", "B": "指南针", "C": "火药", "D": "印刷术"},
             "answer": "A", "analytic": "中国四大发明指造纸术、指南针、火药和印刷术；地动仪是东汉张衡发明的地震检测仪器。"},
            {"title": "七大洲中面积最大的大洲是？", "options": {"A": "非洲", "B": "北美洲", "C": "亚洲", "D": "南极洲"},
             "answer": "C", "analytic": "亚洲面积约4458万平方公里，是世界第一大洲，占全球陆地面积约三成。"},
            {"title": "地球自转一周大约需要多长时间？", "options": {"A": "12小时", "B": "24小时", "C": "48小时", "D": "30天"},
             "answer": "B", "analytic": "地球绕自身轴自转一周约24小时（一个恒星日约23小时56分），形成了昼夜交替。"},
            {"title": "老虎在动物学上属于哪一类？", "options": {"A": "爬行动物", "B": "两栖动物", "C": "哺乳动物", "D": "节肢动物"},
             "answer": "C", "analytic": "老虎是猫科大型食肉哺乳动物，胎生、哺乳，幼崽靠母乳喂养。"},
            {"title": "中华人民共和国的国旗是？", "options": {"A": "五星红旗", "B": "星条旗", "C": "旭日旗", "D": "米字旗"},
             "answer": "A", "analytic": "五星红旗是中华人民共和国的国旗，红色旗面缀五颗黄色五角星。"},
            {"title": "食盐的主要化学成分是什么？", "options": {"A": "氯化钾", "B": "碳酸钠", "C": "硫酸钠", "D": "氯化钠"},
             "answer": "D", "analytic": "食盐的主要成分是氯化钠（NaCl），是人体必需的调味品和电解质来源。"},
            {"title": "声音在下列哪种环境中无法传播？", "options": {"A": "水中", "B": "钢铁中", "C": "真空中", "D": "空气中"},
             "answer": "C", "analytic": "声音的传播需要介质，真空环境中没有任何物质，因此声音无法在真空中传播。"},
            {"title": "世界上使用人数最多的语言是？", "options": {"A": "英语", "B": "西班牙语", "C": "汉语", "D": "法语"},
             "answer": "C", "analytic": "汉语（中文）以超过15亿的母语和习得人口位居世界第一，是联合国的官方工作语言之一。"},
            {"title": "冰融化成水属于什么变化？", "options": {"A": "化学变化", "B": "物理变化", "C": "核变化", "D": "生物变化"},
             "answer": "B", "analytic": "冰融化只是水的状态由固态变为液态，分子组成没有改变，属于物理变化。"},
            {"title": "心脏最主要的功能是什么？", "options": {"A": "消化食物", "B": "过滤血液", "C": "推动血液循环", "D": "储存氧气"},
             "answer": "C", "analytic": "心脏像一个永不停歇的“水泵”，通过有节律的收缩舒张，把血液泵送到全身各处。"},
            {"title": "被称为“红色星球”的行星是？", "options": {"A": "火星", "B": "金星", "C": "木星", "D": "水星"},
             "answer": "A", "analytic": "火星表面富含氧化铁（铁锈），呈现红褐色，因此被称为“红色星球”。"},
            {"title": "中华人民共和国的首都是哪座城市？", "options": {"A": "上海", "B": "北京", "C": "广州", "D": "西安"},
             "answer": "B", "analytic": "北京是中华人民共和国的首都，是国家的政治、文化、国际交往和科技创新中心。"},
            {"title": "一年中有几个月份有30天？", "options": {"A": "4个", "B": "6个", "C": "7个", "D": "11个"},
             "answer": "A", "analytic": "公历中4月、6月、9月、11月共4个月有30天；1、3、5、7、8、10、12月有31天，2月最特殊。"},
            {"title": "我国古代“四大名著”中，《红楼梦》的作者是谁？", "options": {"A": "罗贯中", "B": "吴承恩", "C": "施耐庵", "D": "曹雪芹"},
             "answer": "D", "analytic": "《红楼梦》是清代作家曹雪芹“披阅十载、增删五次”写成的古典小说巅峰之作。"},
        ]

    # ---------- 天行数据接口 ----------
    def _fetch_tianapi(self, timeout: float = 6.0):
        """调用天行数据百科题库接口，返回单题 dict（含 answer/analytic），失败返回 None。"""
        if not TIANAPI_QUIZ_KEY:
            return None          # 未注入密钥 → 静默走本地题库
        try:
            import http.client
            import urllib.parse as _up
            conn = http.client.HTTPSConnection(TIANAPI_QUIZ_HOST, timeout=timeout)
            params = _up.urlencode({"key": TIANAPI_QUIZ_KEY})
            headers = {"Content-type": "application/x-www-form-urlencoded"}
            conn.request("POST", TIANAPI_QUIZ_PATH, params, headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            data = json.loads(raw)
            if data.get("code") != 200:
                return None
            r = data.get("result") or {}
            title = (r.get("title") or "").strip()
            if not title:
                return None
            options = {}
            for letter in ("A", "B", "C", "D"):
                options[letter] = (r.get("answer" + letter) or "").strip()
            ans = (r.get("answer") or "").strip().upper()
            if ans not in ("A", "B", "C", "D"):
                # 兼容“答案是选项文本”的情况
                matched = None
                for letter, text in options.items():
                    if ans and text and ans in text:
                        matched = letter
                        break
                if not matched:
                    return None
                ans = matched
            # 题目解析为空 → 视为无效题（保证玩家每题都能看到解析）
            analytic = (r.get("analytic") or "").strip()
            if not analytic:
                return None
            return {
                "title": title,
                "options": options,
                "answer": ans,
                "analytic": analytic,
                "source": "tianapi",
            }
        except Exception:
            return None

    def _top_up(self, count: int):
        """尝试从天行接口补充题目到队列（每轮至多 6 题，解析为空/失败立即停止）。"""
        for _ in range(max(1, min(count, 6))):
            q = self._fetch_tianapi()
            if not q:
                break
            self._queue.append(q)

    def _next_local(self):
        bank = self._local_bank
        if not bank:
            return None
        q = bank[self._local_idx % len(bank)]
        self._local_idx += 1
        return dict(q)

    def _cleanup(self):
        now = time.time()
        for qid in list(self._pending.keys()):
            if now - self._pending[qid]["_ts"] > QUIZ_PENDING_TTL:
                del self._pending[qid]
        for qid in list(self._checked.keys()):
            if now - self._checked[qid]["_ts"] > QUIZ_PENDING_TTL:
                del self._checked[qid]

    # ---------- 对外接口 ----------
    def get_questions(self, count: int) -> list:
        """取出 count 道题（剥离开答案与解析），并为每道题登记待校验 id。"""
        with self._lock:
            self._cleanup()
            # 每次尽量补充一点天行题库，其余由本地题库兜底
            self._top_up(max(2, count))
            out = []
            for _ in range(count):
                q = self._queue.pop(0) if self._queue else self._next_local()
                if not q:
                    break
                qid = "q_" + uuid.uuid4().hex[:12]
                self._pending[qid] = {**q, "_ts": time.time()}
                out.append({"id": qid, "title": q["title"], "options": q["options"], "source": q.get("source")})
            return out

    def check(self, qid: str, answer: str):
        """校验作答：返回 {correct, answer, analytic}，题目不存在返回 None。"""
        with self._lock:
            self._cleanup()
            ans_norm = (answer or "").strip().upper()
            if qid in self._checked:
                # 缓存的是题目本身（答案+解析），正确性按本次作答实时计算
                cached = self._checked[qid]
                return {
                    "correct": ans_norm == cached["answer"],
                    "answer": cached["answer"],
                    "analytic": cached["analytic"],
                }
            q = self._pending.pop(qid, None)
            if not q:
                return None
            result = {"correct": ans_norm == q["answer"], "answer": q["answer"], "analytic": q["analytic"]}
            self._checked[qid] = {**result, "_ts": time.time()}
            return result


QUIZ_SERVICE = QuizService()


@app.get("/api/harness/status")
async def api_harness_status():
    """harness 运行时健康状态（技能/插件/工具数/最近事件）。"""
    try:
        return {"code": 200, **_harness().status()}
    except Exception as e:
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)


@app.post("/api/harness/reload")
async def api_harness_reload():
    """热重载全部技能与插件。"""
    try:
        info = _harness().reload_all()
        return {"code": 200, "message": "已热重载全部技能与插件", **info}
    except Exception as e:
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)


@app.get("/api/harness/runtime")
async def api_harness_runtime():
    """Agent 监督运行时快照：LLM/工具调用统计、熔断器、在途对话轮、token 用量。"""
    try:
        return {"code": 200, "runtime": _harness().runtime.snapshot()}
    except Exception as e:
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)


@app.post("/api/harness/runtime/reset")
async def api_harness_runtime_reset(payload: dict):
    """手动复位熔断器（body: {"name": "llm:chat" 或 "tool:music_search"}）。"""
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"code": 400, "message": "缺少 name（如 llm:chat / tool:xxx）"}, status_code=400)
    if _harness().runtime.reset_breaker(name):
        return {"code": 200, "message": f"熔断器 {name} 已复位"}
    return JSONResponse({"code": 404, "message": f"熔断器 {name} 不存在"}, status_code=404)


# ---------- 任务系统（长任务 / 批量任务 / 队列） ----------

@app.get("/api/harness/tasks")
async def api_harness_tasks(state: str = "", kind: str = "", limit: int = 20):
    """任务列表（可按状态/类型过滤）。"""
    try:
        h = _harness()
        h.tasks.ensure_started()
        return {"code": 200,
                "tasks": h.tasks.list_tasks(state=state or None, kind=kind or None,
                                            limit=max(1, min(int(limit), 100)))}
    except Exception as e:
        return JSONResponse({"code": 500, "message": str(e)}, status_code=500)


@app.get("/api/harness/tasks/{task_id}")
async def api_harness_task_detail(task_id: str):
    """任务详情（含步骤/条目级状态与结果）。"""
    st = _harness().tasks.status(task_id)
    if st is None:
        return JSONResponse({"code": 404, "message": f"任务 {task_id} 不存在"}, status_code=404)
    return {"code": 200, "task": st}


@app.post("/api/harness/tasks/{task_id}/cancel")
async def api_harness_task_cancel(task_id: str):
    if _harness().tasks.cancel(task_id):
        return {"code": 200, "message": f"任务 {task_id} 已取消"}
    return JSONResponse({"code": 404, "message": f"任务 {task_id} 不存在或已结束"}, status_code=404)


@app.post("/api/harness/tasks/{task_id}/approve")
@app.post("/api/harness/tasks/{task_id}/reject")
async def api_harness_task_confirm(task_id: str, request: Request):
    """批准/拒绝流程中等待确认的危险步骤。"""
    action = request.url.path.rstrip("/").split("/")[-1]
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    note = str((payload or {}).get("note") or "")
    ts = _harness().tasks
    ok, msg = (ts.approve_step if action == "approve" else ts.reject_step)(task_id, note)
    if ok:
        return {"code": 200, "message": msg}
    return JSONResponse({"code": 404, "message": msg}, status_code=404)


@app.post("/api/harness/tasks/{task_id}/retry")
async def api_harness_task_retry(task_id: str):
    if _harness().tasks.retry(task_id):
        return {"code": 200, "message": f"任务 {task_id} 已重试（保留已成功部分）"}
    return JSONResponse({"code": 404, "message": f"任务 {task_id} 不存在或仍在运行"}, status_code=404)


@app.get("/api/harness/queues")
async def api_harness_queues():
    """队列状态（worker 数 / 排队 / 运行中 / 暂停）。"""
    h = _harness()
    h.tasks.ensure_started()
    return {"code": 200, "queues": h.tasks.queue_stats()}


@app.post("/api/harness/queues/{name}/pause")
@app.post("/api/harness/queues/{name}/resume")
async def api_harness_queue_action(name: str, request: Request):
    action = request.url.path.rstrip("/").split("/")[-1]
    paused = action == "pause"
    if _harness().tasks.pause_queue(name, paused):
        return {"code": 200, "message": f"队列 {name} 已{'暂停' if paused else '恢复'}"}
    return JSONResponse({"code": 404, "message": f"队列 {name} 不存在"}, status_code=404)


@app.get("/api/harness/skills")
async def api_harness_skills():
    h = _harness()
    h.ensure_loaded()
    return {"code": 200, "skills": h.skills.list_info()}


@app.post("/api/harness/skills/{name}/enable")
async def api_harness_skill_enable(name: str):
    if not _harness().skills.set_enabled(name, True):
        return JSONResponse({"code": 404, "message": f"技能 {name} 不存在"}, status_code=404)
    return {"code": 200, "message": f"技能 {name} 已启用"}


@app.post("/api/harness/skills/{name}/disable")
async def api_harness_skill_disable(name: str):
    if not _harness().skills.set_enabled(name, False):
        return JSONResponse({"code": 404, "message": f"技能 {name} 不存在"}, status_code=404)
    return {"code": 200, "message": f"技能 {name} 已禁用"}


@app.post("/api/harness/skills/{name}/reload")
async def api_harness_skill_reload(name: str):
    if not _harness().skills.reload(name):
        return JSONResponse({"code": 404, "message": f"技能 {name} 不存在"}, status_code=404)
    return {"code": 200, "message": f"技能 {name} 已重载"}


@app.get("/api/harness/plugins")
async def api_harness_plugins():
    h = _harness()
    h.ensure_loaded()
    return {"code": 200, "plugins": h.plugins.list_info()}


@app.post("/api/harness/plugins/{name}/enable")
@app.post("/api/harness/plugins/{name}/disable")
@app.post("/api/harness/plugins/{name}/reload")
async def api_harness_plugin_action(name: str, request: Request):
    """插件启用/禁用/重载（动作从 URL 尾部解析）。"""
    action = request.url.path.rstrip("/").split("/")[-1]
    h = _harness()
    if action == "enable":
        ok = h.plugins.set_enabled(name, True)
    elif action == "disable":
        ok = h.plugins.set_enabled(name, False)
    else:
        ok = h.plugins.reload(name)
    if not ok:
        return JSONResponse({"code": 404, "message": f"插件 {name} 不存在"}, status_code=404)
    label = {"enable": "启用", "disable": "禁用", "reload": "重载"}.get(action, action)
    return {"code": 200, "message": f"插件 {name} 已{label}"}


# ---------- video 技能原生端点（能力来自 skills/media/video_lib.py，无外部服务） ----------
# 前端与技能全部走这些同源相对路径（/api/video_hub/*），手机/局域网与桌面端统一可用。
_VH_PASS_HEADERS = {"content-type", "content-length", "content-range",
                    "accept-ranges", "etag", "last-modified", "cache-control"}


_VIDEO_LIB_MTIME = {"ts": 0.0}
_VIDEO_LIB_LOCK = threading.Lock()


def _video_lib():
    """加载 video 技能核心库（与技能共享同一模块实例：STREAMS/队列状态互通）。

    video_lib.py 在 skills/ 下，改动只触发技能热重载（reload skill.py），
    不会刷新 sys.modules 里的 video_lib 实例。这里按文件 mtime 自动
    reload，改完 video_lib.py 后下一次 API 调用即生效，无需重启进程。
    注意：reload 会重置 STREAMS/队列等模块级状态（正在播放的流会断）。"""
    _p = str(Path(__file__).parent / "skills" / "media")
    if _p not in sys.path:
        sys.path.insert(0, _p)
    import video_lib
    try:
        mtime = (Path(_p) / "video_lib.py").stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime and mtime != _VIDEO_LIB_MTIME["ts"]:
        with _VIDEO_LIB_LOCK:
            if mtime != _VIDEO_LIB_MTIME["ts"]:
                if _VIDEO_LIB_MTIME["ts"]:  # 0.0=进程首次加载，本就是最新代码
                    importlib.reload(video_lib)
                    logger.info("video_lib.py 已变更，自动重载完成")
                _VIDEO_LIB_MTIME["ts"] = mtime
    return video_lib


@app.get("/api/video_hub/api/platforms")
async def vh_platforms():
    return {"platforms": _video_lib().PLATFORMS, "default": "all",
            "sort_modes": [{"id": "relevance", "name": "相关性最高"},
                           {"id": "hot", "name": "最热门"}]}


@app.get("/api/video_hub/api/search")
async def vh_search(q: str = "", platform: str = "all", sort: str = "relevance",
                    limit: int = 12, page: int = 1):
    q = q.strip()
    if not q:
        return JSONResponse({"error": "missing required param: q"}, status_code=400)
    if q.startswith("http"):
        return JSONResponse({"error": "this is a URL, use POST play with {\"url\": ...}"}, status_code=400)
    limit = max(1, min(limit, 24))
    page = max(1, min(page, 50))
    try:
        results = await asyncio.to_thread(_video_lib().search_videos,
                                          q, platform, limit, sort, page)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"search failed: {e}"}, status_code=502)
    return {"query": q, "platform": platform, "sort": sort,
            "count": len(results), "page": page,
            "has_more": len(results) >= limit, "results": results}


@app.get("/api/video_hub/api/hot")
async def vh_hot(platform: str = "all", limit: int = 12, page: int = 1):
    """热门 / 推荐：无关键词即可用，按平台返回官网热门列表（字段与搜索结果一致）。"""
    limit = max(1, min(limit, 24))
    page = max(1, min(page, 50))
    try:
        results = await asyncio.to_thread(_video_lib().hot_videos,
                                          platform, limit, page)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"hot fetch failed: {e}"}, status_code=502)
    return {"platform": platform, "count": len(results), "page": page,
            "has_more": len(results) >= limit, "results": results}


# ---------- 缩略图代理：B站图床有 Referer 防盗链、YouTube 图床需走代理，
# 浏览器直连外链都会失败，统一由后端按平台策略拉取转发（域名白名单防 SSRF） ----------
_VH_THUMB_HOSTS = ("hdslb.com", "bilibili.com", "acfun.cn", "aixifan.com",
                   "ytimg.com", "youtube.com", "googlevideo.com")


def _vh_thumb_fetch(url: str):
    from urllib.parse import urlparse
    pu = urlparse(url)
    host = (pu.hostname or "").lower()
    if pu.scheme not in ("http", "https") or not host:
        raise ValueError("bad url")
    if not any(host == a or host.endswith("." + a) for a in _VH_THUMB_HOSTS):
        raise PermissionError("host not allowed")
    lib = _video_lib()
    if "ytimg" in host or "youtube" in host or "googlevideo" in host:
        return lib._fq_get(url, timeout=10)  # YouTube 图床走 fq 代理
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
    if "hdslb" in host or "bilibili" in host:
        headers["Referer"] = "https://www.bilibili.com/"  # 图床防盗链
    elif "acfun" in host or "aixifan" in host:
        headers["Referer"] = "https://www.acfun.cn/"
    import requests as _rq
    return _rq.get(url, headers=headers, timeout=10)


@app.get("/api/video_hub/thumb")
async def vh_thumb(u: str = ""):
    if not u:
        return JSONResponse({"error": "missing required param: u"}, status_code=400)
    try:
        r = await asyncio.to_thread(_vh_thumb_fetch, u)
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"thumb fetch failed: {e}"}, status_code=502)
    ct = (r.headers.get("content-type") or "").split(";")[0].strip()
    if r.status_code != 200 or not ct.startswith("image/"):
        return JSONResponse({"error": f"thumb http {r.status_code}"}, status_code=502)
    return Response(content=r.content, media_type=ct,
                    headers={"Cache-Control": "public, max-age=86400"})


async def _vh_json_body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.post("/api/video_hub/api/play")
async def vh_play(request: Request):
    lib = _video_lib()
    body = await _vh_json_body(request)
    query = str(body.get("query") or "").strip()
    url = str(body.get("url") or "").strip()
    platform = body.get("platform") or "all"
    sort = body.get("sort") or "relevance"
    if not query and not url:
        return JSONResponse({"error": "need 'query' in JSON body"}, status_code=400)
    try:
        if url:
            entry = await asyncio.to_thread(lib.resolve_and_play, url,
                                            bool(body.get("force")))
        else:
            entry = await asyncio.to_thread(lib.play_by_query, query, platform, sort)
    except LookupError:
        return JSONResponse({"error": f"no results for: {query}"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"resolve failed: {e}"}, status_code=502)
    return entry


@app.api_route("/api/video_hub/api/queue", methods=["GET", "POST", "DELETE"])
async def vh_queue(request: Request):
    lib = _video_lib()
    if request.method == "GET":
        return {"queue": await asyncio.to_thread(lib.queue_list)}
    if request.method == "DELETE":
        all_flag = request.query_params.get("all") == "1"
        try:
            i = None if all_flag else int(request.query_params.get("i", -1))
        except ValueError:
            return JSONResponse({"error": "i must be int"}, status_code=400)
        await asyncio.to_thread(lib.queue_remove, i, all_flag)
        return {"ok": True}
    body = await _vh_json_body(request)
    query = str(body.get("query") or "").strip()
    url = str(body.get("url") or "").strip()
    if not query and not url:
        return JSONResponse({"error": "need 'query' or 'url'"}, status_code=400)
    try:
        entry, pos = await asyncio.to_thread(
            lib.queue_add, url or None, query or None,
            body.get("platform") or "all", body.get("sort") or "relevance")
    except LookupError:
        return JSONResponse({"error": f"no results for: {query}"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"resolve failed: {e}"}, status_code=502)
    return {"queued": entry, "position": pos}


@app.post("/api/video_hub/api/ended")
async def vh_ended(request: Request):
    # 大屏视频播完：先闭环看护子智能体（worker_id 由前端在播完回报时带回），
    # 再取队列下一部连播。
    body = await _vh_json_body(request)
    wid = str(body.get("worker_id") or "").strip()
    if wid:
        try:
            mgr = _get_media_workers()
            worker = mgr.get(wid)
            if worker is not None:
                await mgr.complete(wid, f"《{worker.title}》已经播放完毕。")
        except Exception as e:
            logger.warning(f"[MediaWorker] 视频播完回报处理失败: {e}")
    return {"next": await asyncio.to_thread(_video_lib().pop_next)}


@app.post("/api/video_hub/api/report")
async def vh_report(request: Request):
    body = await _vh_json_body(request)
    await asyncio.to_thread(_video_lib().report, body)
    return {"ok": True}


@app.post("/api/video_hub/api/control")
async def vh_control(request: Request):
    body = await _vh_json_body(request)
    action = str(body.get("action") or "")
    try:
        data = await asyncio.to_thread(_video_lib().control, action, body.get("value"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return data if isinstance(data, dict) else {"ok": True}


@app.get("/api/video_hub/api/status")
async def vh_status():
    return await asyncio.to_thread(_video_lib().public_state)


# ---------- 视频收藏夹（分类 + 收藏，本地持久化到 video_favorites.json） ----------
# ---------- 视频源管理（内置平台启停 + 自定义视频源，持久化到 video_sources.json） ----------
@app.get("/api/video_hub/api/sources")
async def vhs_list():
    """列出全部视频源（内置 + 自定义，带启用状态）。"""
    return await asyncio.to_thread(video_sources_lib.list_sources)


@app.patch("/api/video_hub/api/sources/builtin/{pid}")
async def vhs_set_builtin(pid: str, request: Request):
    """启用/禁用内置平台。body: {"enabled": true|false}"""
    body = await _vh_json_body(request)
    if "enabled" not in body:
        return JSONResponse({"error": "缺少 enabled 字段"}, status_code=400)
    if not await asyncio.to_thread(video_sources_lib.set_builtin, pid, bool(body["enabled"])):
        return JSONResponse({"error": f"平台 {pid} 不存在"}, status_code=404)
    return {"ok": True, "id": pid, "enabled": bool(body["enabled"])}


@app.post("/api/video_hub/api/sources/custom")
async def vhs_add_custom(request: Request):
    """新增自定义视频源。body: {"name": "...", "search_url": "https://...?q={kw}"}"""
    body = await _vh_json_body(request)
    try:
        src = await asyncio.to_thread(video_sources_lib.add_custom,
                                      body.get("name", ""), body.get("search_url", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "source": src}


@app.patch("/api/video_hub/api/sources/custom/{cid}")
async def vhs_update_custom(cid: str, request: Request):
    """更新自定义源（name / search_url / enabled 可部分更新）。"""
    body = await _vh_json_body(request)
    try:
        src = await asyncio.to_thread(video_sources_lib.update_custom,
                                      cid, body.get("name"), body.get("search_url"),
                                      body.get("enabled") if "enabled" in body else None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not src:
        return JSONResponse({"error": "自定义源不存在"}, status_code=404)
    return {"ok": True, "source": src}


@app.delete("/api/video_hub/api/sources/custom/{cid}")
async def vhs_remove_custom(cid: str):
    """删除自定义视频源。"""
    if not await asyncio.to_thread(video_sources_lib.remove_custom, cid):
        return JSONResponse({"error": "自定义源不存在"}, status_code=404)
    return {"ok": True, "removed": cid}


# ---------- 视频收藏夹（分类 + 收藏，本地持久化到 video_favorites.json） ----------
@app.get("/api/video_hub/api/favorites")
async def vhf_list(request: Request):
    """列出当前用户的分类与收藏视频。"""
    return await asyncio.to_thread(video_fav_lib.list_all, _store_uid(request))


@app.get("/api/video_hub/api/history")
async def vhh_list(request: Request, limit: int = 200):
    """列出当前用户的观看历史（按观看时间倒序，最新在前）。"""
    return {"history": await asyncio.to_thread(
        video_history_lib.list_history, limit, _store_uid(request))}


@app.post("/api/video_hub/api/history")
async def vhh_add(request: Request):
    """记录一次观看。body: {"video": {...}}（按 webpage_url 去重，最多保留 1000 条）"""
    body = await _vh_json_body(request)
    video = body.get("video")
    if not isinstance(video, dict):
        return JSONResponse({"error": "缺少 video 字段"}, status_code=400)
    try:
        out = await asyncio.to_thread(
            video_history_lib.add_history, video, _store_uid(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **out}


@app.delete("/api/video_hub/api/history/{hid}")
async def vhh_remove(hid: str, request: Request):
    """删除一条观看历史。"""
    if not await asyncio.to_thread(
            video_history_lib.remove_history, hid, _store_uid(request)):
        return JSONResponse({"error": "记录不存在"}, status_code=404)
    return {"ok": True, "removed": hid}


@app.delete("/api/video_hub/api/history")
async def vhh_clear(request: Request):
    """清空当前用户的全部观看历史。"""
    if not await asyncio.to_thread(video_history_lib.clear_history, _store_uid(request)):
        return JSONResponse({"error": "历史本来就是空的"}, status_code=404)
    return {"ok": True, "cleared": True}


@app.post("/api/video_hub/api/favorites/categories")
async def vhf_create_category(request: Request):
    """创建收藏分类。body: {"name": "..."}"""
    body = await _vh_json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "分类名不能为空"}, status_code=400)
    try:
        cat = await asyncio.to_thread(
            video_fav_lib.create_category, name, _store_uid(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "category": cat}


@app.patch("/api/video_hub/api/favorites/categories/{cid}")
async def vhf_rename_category(cid: str, request: Request):
    """重命名收藏分类。body: {"name": "..."}"""
    body = await _vh_json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "分类名不能为空"}, status_code=400)
    try:
        cat = await asyncio.to_thread(
            video_fav_lib.rename_category, cid, name, _store_uid(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not cat:
        return JSONResponse({"error": "分类不存在"}, status_code=404)
    return {"ok": True, "category": cat}


@app.delete("/api/video_hub/api/favorites/categories/{cid}")
async def vhf_delete_category(cid: str, request: Request):
    """删除收藏分类；分类下视频自动归入「未分类」。"""
    if not await asyncio.to_thread(
            video_fav_lib.delete_category, cid, _store_uid(request)):
        return JSONResponse({"error": "分类不存在"}, status_code=404)
    return {"ok": True, "deleted": cid}


@app.post("/api/video_hub/api/favorites")
async def vhf_add(request: Request):
    """收藏视频。body: {"video": {...}, "category_id"?: str|null}"""
    body = await _vh_json_body(request)
    video = body.get("video")
    if not isinstance(video, dict):
        return JSONResponse({"error": "缺少 video 字段"}, status_code=400)
    category_id = body.get("category_id")
    if category_id is not None:
        category_id = str(category_id) or None
    try:
        out = await asyncio.to_thread(
            video_fav_lib.add_favorite, video, category_id, _store_uid(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **out}


@app.delete("/api/video_hub/api/favorites/{fid}")
async def vhf_remove(fid: str, request: Request):
    """取消收藏。"""
    if not await asyncio.to_thread(
            video_fav_lib.remove_favorite, fid, _store_uid(request)):
        return JSONResponse({"error": "收藏不存在"}, status_code=404)
    return {"ok": True, "removed": fid}


@app.post("/api/video_hub/api/favorites/{fid}/category")
async def vhf_move(fid: str, request: Request):
    """把收藏移到指定分类（category_id=null 归入未分类）。"""
    body = await _vh_json_body(request)
    category_id = body.get("category_id")
    if category_id is not None:
        category_id = str(category_id) or None
    try:
        moved = await asyncio.to_thread(
            video_fav_lib.move_favorite, fid, category_id, _store_uid(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not moved:
        return JSONResponse({"error": "收藏不存在"}, status_code=404)
    return {"ok": True, "moved": fid}


@app.get("/api/video_hub/proxy")
def vh_proxy(request: Request, k: str = ""):
    """direct 直链流：转发 Range 实现拖进度，注入 Referer/UA 绕过防盗链。"""
    from fastapi.responses import StreamingResponse as _SR
    import requests as _rq
    try:
        upstream = _video_lib().open_proxy(k, request.headers.get("range"))
    except KeyError:
        return JSONResponse({"error": "stream expired, re-play via video_play"}, status_code=404)
    except _rq.RequestException as e:
        return JSONResponse({"error": f"upstream failed: {e}"}, status_code=502)

    passthrough = {kk: vv for kk, vv in upstream.headers.items()
                   if kk.lower() in _VH_PASS_HEADERS}
    if upstream.status_code in (200, 206):
        passthrough["Accept-Ranges"] = "bytes"

    def _stream():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return _SR(_stream(), status_code=upstream.status_code, headers=passthrough)


@app.get("/api/video_hub/relay/{key}")
def vh_relay(key: str, t: str = "", ss: float = 0.0):
    """relay 实时合流（默认 -c copy 转封装，亚秒延迟）；
    ?t=1 为转码兜底模式（源编码浏览器解不了时全转 h264+aac）；
    ?ss=N 从第 N 秒起播（断流恢复的断点续播）。"""
    from fastapi.responses import StreamingResponse as _SR
    try:
        proc, mime = _video_lib().start_relay(key, transcode=(t == "1"), ss=ss)
    except KeyError:
        return JSONResponse({"error": "stream expired, re-play via video_play"}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=501)
    except OSError as e:
        return JSONResponse({"error": f"ffmpeg spawn failed: {e}"}, status_code=500)

    def _stream():
        # 根治 ffmpeg 进程泄漏：阻塞读会让生成器卡死在 read 上，
        # 客户端断开时 finally 永远执行不到 → 进程变僵尸越积越多。
        # 改为独立线程读 + 队列轮询：ffmpeg 卡住时生成器仍可被 close，
        # finally 里的 kill 能及时执行，进程不再泄漏。
        import queue as _q
        q = _q.Queue(maxsize=64)

        def _reader():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        q.put(None)
                        break
                    q.put(chunk)
            except Exception:
                q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        try:
            while True:
                try:
                    chunk = q.get(timeout=1.0)
                except _q.Empty:
                    continue
                if chunk is None:
                    break
                yield chunk
        finally:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    return _SR(_stream(), media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/harness")
async def harness_admin_page():
    """harness 管理页面（技能/插件启停与热重载）。"""
    from fastapi.responses import FileResponse as _FR
    resp = _FR(str(WEB_DIR / "harness.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


if __name__ == "__main__":
    # Windows 控制台默认 GBK，emoji 会导致 UnicodeEncodeError 崩溃，强制 utf-8 容错
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    import ssl
    ip = get_lan_ip()

    # 检查/生成自签名证书 (HTTPS 解锁手机陀螺仪/VR模式等传感器 API)
    cert_dir = Path(__file__).parent
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    # 临时强制 HTTP 模式用于本地浏览器测试（测试完后恢复）
    # 支持环境变量 DABAI_HTTP_ONLY=1 强制走 HTTP（nginx 前置 TLS 终结时用）
    import os as _os
    import json as _json
    # nginx 前置 TLS 终结模式：DABAI_HTTP_ONLY=1 或 settings.json harness.http_only=true
    # 时 dabai 只监听回源端口 8001（纯 HTTP，uvloop 全速），TLS 交给 nginx
    _http_only = _os.environ.get("DABAI_HTTP_ONLY") == "1"
    try:
        _cfg = _json.loads((cert_dir / "settings.json").read_text(encoding="utf-8"))
        _http_only = _http_only or bool(_cfg.get("harness", {}).get("http_only", False))
    except Exception:
        pass
    if _http_only:
        use_https = False
    else:
        use_https = cert_file.exists() and key_file.exists()

    ipv6 = get_global_ipv6()
    has_ipv6 = bool(ipv6)  # 仅在有真实全局 IPv6 地址时才启用双栈

    print("\n" + "=" * 50)
    print("   3D 虚拟 AI 角色陪聊 服务器已启动")
    print("=" * 50)
    if use_https:
        print(f"  本机访问 : https://127.0.0.1:{SERVER_PORT}")
        print(f"  局域网   : https://{ip}:{SERVER_PORT}")
        if has_ipv6:
            print(f"  IPv6    : https://[{ipv6}]:{SERVER_PORT}")
        print(f"  ⚠ 手机首次访问会提示证书不安全 → 点「高级」→「继续访问」")
        print(f"  ✅ HTTPS 模式：陀螺仪/VR模式传感器 API 已解锁")
    else:
        print(f"  本机访问 : http://127.0.0.1:{SERVER_PORT}")
        print(f"  局域网   : http://{ip}:{SERVER_PORT}")
        if has_ipv6:
            print(f"  IPv6    : http://[{ipv6}]:{SERVER_PORT}")
    print(f"  手机请连接同一 Wi-Fi 后访问上述局域网地址")
    if has_ipv6:
        print(f"  ✅ IPv6 双栈监听已启用（IPv4 + IPv6 均可访问）")
    print("=" * 50 + "\n")

    # 静默 Windows asyncio WebSocket 断开时的 ConnectionResetError
    # 方案1：事件循环异常处理器（兜底）
    #   ⚠ 原来只对临时 new_event_loop() 设置处理器没用 —— uvicorn.run() 会再创建
    #   自己的事件循环，那个循环没装处理器，异常照样打印。改用事件循环策略，
    #   此后每个新建循环（含 uvicorn 实际使用的那个）都会自动挂上静默处理器。
    import asyncio as _asyncio
    def _silent_exc_handler(loop, context):
        exc = context.get('exception')
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return  # 客户端断开，正常情况，无需日志
        loop.default_exception_handler(context)

    try:
        import asyncio.proactor_events as _pe
        _orig_call_lost = _pe._ProactorBasePipeTransport._call_connection_lost

        def _patched_call_lost(self, exc):
            try:
                _orig_call_lost(self, exc)
            except OSError:
                # 为什么必须从两个分支都拦住：
                # 本机 CPython 的 _call_connection_lost 在 finally 里执行
                #   self._sock.shutdown(socket.SHUT_RDWR)   # 没有 try/except 保护
                # 远端已强制断开时这里抛 WinError 10054（ConnectionResetError），
                # 会中断 finally 中后续的 self._sock.close() 与
                # _called_connection_lost = True，导致 socket 泄漏、Proactor 循环
                # 残留 pending overlapped 读而卡死（所有 HTTP 请求超时）。
                # 注意 exc 参数不一定是连接错误（正常断开时是 None），shutdown
                # 的异常是它自己新抛的，所以不能只按 exc 的类型判断。
                # 只吞异常不够，必须补齐被中断的清理。
                if not getattr(self, '_called_connection_lost', False):
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
                    server = self._server
                    if server is not None:
                        try:
                            server._detach(self)
                        except Exception:
                            pass
                        self._server = None
                    self._called_connection_lost = True

        _pe._ProactorBasePipeTransport._call_connection_lost = _patched_call_lost

        # 兜底：让 uvicorn 实际使用的循环也带静默异常处理器（防漏网之鱼）
        class _SilentLoopPolicy(_asyncio.DefaultEventLoopPolicy):
            def new_event_loop(self):
                loop = super().new_event_loop()
                loop.set_exception_handler(_silent_exc_handler)
                return loop
        _asyncio.set_event_loop_policy(_SilentLoopPolicy())
    except Exception:
        pass  # 非 Windows 或版本差异，忽略

    # host="::" 启用 IPv6 双栈监听（IPv4 + IPv6 均可访问）
    host = "::" if has_ipv6 else "0.0.0.0"

    # 热更新守护：核心 Python 变化自动重启；技能/插件变化自动热重载
    # （settings.json 的 harness.hot_reload=false 可关闭）
    def _hot_reload_ext_callback():
        """技能/插件/工具定义文件变化 → 热重载 harness + 刷新全部共享 agent 工具。

        由 hot_reload 守护线程调用（不阻塞事件循环）；失败只记录，不影响服务。
        """
        try:
            info = _harness().reload_all()
            n_refreshed = 0
            for ag in list(_shared_agents.values()):
                try:
                    ag.refresh_local_tools()
                    n_refreshed += 1
                except Exception as e:
                    logger.warning(f"热重载刷新 agent 工具失败: {e}")
            logger.info(
                f"[HotReload] 技能/插件已热重载（技能 {info.get('skills')} 个、"
                f"插件 {info.get('plugins')} 个），已刷新 {n_refreshed} 个 agent"
            )
        except Exception as e:
            logger.warning(f"[HotReload] 技能/插件热重载失败: {e}")

    start_hot_reload(on_ext_reload=_hot_reload_ext_callback)

    if use_https:
        uvicorn.run(app, host=host, port=SERVER_PORT, log_level="warning",
                    loop="uvloop", http="httptools",
                    ssl_certfile=str(cert_file), ssl_keyfile=str(key_file))
    else:
        # nginx 前置 TLS 终结模式：回源端口 8001，对外仍由 nginx 提供 8000 HTTPS
        _serve_port = 8001 if _http_only else SERVER_PORT
        uvicorn.run(app, host=host, port=_serve_port, log_level="warning",
                    loop="uvloop", http="httptools")
