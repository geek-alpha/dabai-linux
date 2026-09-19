# -*- coding: utf-8 -*-
'''
大白网页版 codex/opencode 执行引擎 —— 深度集成、独立运行
（能力源自飞书中转站 agent.py 的移植，现已完全自持，不依赖中转站目录）

保留原有能力：
- 工作目录 /cd /pwd
- 同步命令 /cmd 与后台任务 /bg /tasks /out /kill
- opencode (/ai) 与 codex (/cx) 长任务：流式进度播报、报错只提示不自动终止（等智能体自行完成）、
  超时终止、LLM 摘要压缩
- 自然语言分流：@LLM 判定闲聊/操作/编码工具任务

适配网页交互：
- 回复通过 WebSocket 推送（safe_send_json），前端实时渲染
- 支持多客户端：执行器全局单例，任务按 ws 连接隔离推送

配置（codex_config.json）：
- agent: 工作目录 / 超时 / 工具（ai=opencode, cx=codex）
- llm:   自然语言分流与摘要用的 LLM；留空则自动跟随大白
         settings.json 当前激活的模型档位（深度集成）
'''
import asyncio
import ctypes
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

try:
    import msvcrt  # Windows 跨进程文件锁
except ImportError:
    msvcrt = None

import platform_compat as pc  # 跨平台兼容层：进程/文件锁/子进程差异统一走它

import requests

BASE_DIR = Path(__file__).parent.resolve()
# 本地独立配置（删除飞书中转站后完全自持）
CONFIG_PATH = BASE_DIR / 'codex_config.json'
# 旧版配置位置（仅作一次性迁移的安全兜底，目录删除后自然失效）
_LEGACY_RELAY_PATH = BASE_DIR / '飞书中转站' / 'config.json'

# 独立进程运行时的持久化状态（热重载/重启后据此找回正在跑的 codex/opencode）
RUNTIME_FILE = BASE_DIR / 'codex_runtime.json'
LOG_DIR = BASE_DIR / 'codex_logs'
LOG_DIR.mkdir(exist_ok=True)
_registry_lock = threading.Lock()

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

DANGER_PATTERNS = [
    r'format\s+[a-z]:', r'shutdown', r'logoff', r'diskpart', r'bcdedit',
    r'vssadmin', r'cipher\s+/w', r'reg\s+delete',
    r'rm\s+(-rf|-r\s|--recursive)', r'mkfs', r'dd\s+if=',
    r'rd\s+/s(\s|$)', r'del\s+/[sfq]', r'remove-item\s+.*-recurse.*-force',
]

HELP = (
    '📖 网页命令行可用指令：\n'
    '/cmd <命令> — 执行并返回输出\n'
    '/bg <命令> — 后台执行长任务\n'
    '/tasks — 查看后台任务\n'
    '/out <id> — 查看任务输出\n'
    '/kill <id> — 终止任务\n'
    '/cd <目录> — 切换工作目录\n'
    '/pwd — 查看当前目录\n'
    '/ai <任务> — 遥控 opencode 执行编码任务\n'
    '/cx <任务> — 遥控 codex 执行编码任务\n'
    '直接发文字 = AI 分流：闲聊直接回答，操作类自动转命令/编码任务'
)

# 只用于「提示性」告警的强致命信号：编程助手输出里常见的 Error:/Traceback/failed to
# 等大多是过程性试错（编译报错、测试失败、网络 403…），助手会自行处理并继续，
# 因此这些常见字符串一律不再当错误处理，更绝不用于终止任务。
ERROR_SUBSTRINGS = (
    'fatal error', 'traceback (most recent call last)',
    'panic:', 'segmentation fault', 'core dumped',
    'command not found',
)

# ---------- 配置加载（本地 codex_config.json，深度集成大白主配置） ----------

def _load_relay_config() -> dict:
    '''加载本地 codex 配置；本地缺失时尝试从旧版飞书中转站配置迁移一次'''
    for path in (CONFIG_PATH, _LEGACY_RELAY_PATH):
        if not path.exists():
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception:
            continue
        # 只取网页引擎需要的部分（剥离 app_id/secret 等飞书专属字段）
        slim = {}
        if isinstance(raw.get('agent'), dict):
            slim['agent'] = raw['agent']
        if isinstance(raw.get('llm'), dict):
            slim['llm'] = raw['llm']
        if path == _LEGACY_RELAY_PATH:
            try:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(slim, f, ensure_ascii=False, indent=2)
                print(f'[codex_runner] 已从旧配置 {path.name} 迁移 → {CONFIG_PATH.name}')
            except Exception:
                pass
        return slim
    return {}


def _dabai_llm_fallback() -> dict:
    '''深度集成：codex_config.json 未单独配置 llm 时，跟随大白 settings.json 当前激活档位'''
    try:
        with open(BASE_DIR / 'settings.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        profiles = cfg.get('llm_profiles') or {}
        prof = profiles.get(cfg.get('llm_provider') or '') or {}
        return {
            'base_url': (prof.get('base_url') or cfg.get('base_url') or '').strip(),
            'model': (prof.get('model') or cfg.get('model') or '').strip(),
            'api_key': (prof.get('api_key') or cfg.get('api_key') or '').strip(),
        }
    except Exception:
        return {'base_url': '', 'model': '', 'api_key': ''}


def _merge_llm_cfg(raw: dict) -> dict:
    '''显式配置优先，缺失字段回落到大白主模型档位'''
    d = {'base_url': '', 'model': '', 'api_key': ''}
    for k in d:
        d[k] = (raw.get(k) or '').strip()
    if not (d['base_url'] and d['model']):
        fb = _dabai_llm_fallback()
        for k in d:
            if not d[k]:
                d[k] = fb[k]
    return d


def _default_codex_config() -> dict:
    return {
        'work_dir': str(BASE_DIR),
        'sync_timeout_sec': 120,
        'max_reply_chars': 3800,
        'tool_progress_interval_sec': 60,
        'tools': {
            'ai': {'cmd': ['opencode', 'run', '--auto'], 'timeout_sec': 900},
            'cx': {'cmd': ['codex', 'exec', '--approve-for-me'], 'timeout_sec': 900},
        },
        'name': 'web-agent',
        'relay_api_key': '',
    }


_RELAY_CFG = _load_relay_config()
_AGENT_CFG_RAW = _RELAY_CFG.get('agent', {}) if _RELAY_CFG else {}
_LLM_CFG_RAW = _RELAY_CFG.get('llm', {}) if _RELAY_CFG else {}

# 合并默认，确保字段完整
def _merge_agent_cfg(raw: dict) -> dict:
    d = _default_codex_config()
    for k in ('work_dir', 'sync_timeout_sec', 'max_reply_chars',
              'tool_progress_interval_sec', 'name'):
        if k in raw:
            d[k] = raw[k]
    if 'tools' in raw and isinstance(raw['tools'], dict) and raw['tools']:
        d['tools'] = raw['tools']
    # 兼容 work_dir 为空：回落到大白根目录
    if not d.get('work_dir') or not os.path.isdir(d['work_dir']):
        d['work_dir'] = str(BASE_DIR)
    return d


AGENT_CFG = _merge_agent_cfg(_AGENT_CFG_RAW)
LLM_CFG = _merge_llm_cfg(_LLM_CFG_RAW)
_cfg_mtime = 0.0
try:
    _cfg_mtime = os.path.getmtime(str(CONFIG_PATH))
except Exception:
    pass

_session = requests.Session()


def reload_relay_config() -> bool:
    '''热重载本地 codex 配置（若文件已更新），返回是否发生变化'''
    global _RELAY_CFG, AGENT_CFG, LLM_CFG, _cfg_mtime
    try:
        mt = os.path.getmtime(str(CONFIG_PATH))
        if mt == _cfg_mtime:
            return False
    except Exception:
        return False
    new = _load_relay_config()
    if not new:
        return False
    _RELAY_CFG = new
    raw = new.get('agent', {})
    AGENT_CFG.clear()
    AGENT_CFG.update(_merge_agent_cfg(raw))
    LLM_CFG.clear()
    LLM_CFG.update(_merge_llm_cfg(new.get('llm', {})))
    _cfg_mtime = mt
    # 同步工作目录到执行器（进程级覆盖优先：worker 的独立工作区不被全局配置顶掉）
    try:
        if not _env_workspace():
            EXECUTOR.cwd = AGENT_CFG.get('work_dir', EXECUTOR.cwd)
    except Exception:
        pass
    llm_state = '开' if llm_available() else '关'
    print(f'[codex_runner] 配置已热更新 work_dir={EXECUTOR.cwd} llm={llm_state}')
    return True


def llm_available() -> bool:
    return bool(LLM_CFG.get('api_key') and LLM_CFG.get('base_url'))


def check_config_reload():
    try:
        reload_relay_config()
    except Exception:
        pass


# ---------- 工具 ----------

def decode_bytes(b: bytes) -> str:
    for enc in ('utf-8', 'gbk'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', errors='replace')


def is_dangerous(cmd: str) -> bool:
    low = cmd.lower()
    if any(re.search(p, low) for p in DANGER_PATTERNS):
        return True
    # 删除白名单保护：删除命令必须所有目标都命中白名单，否则一律拦截
    if _is_delete_command(cmd):
        targets = _delete_targets(cmd)
        if not targets:
            return True  # 无法确认删除目标 → 拦截
        if not all(_target_allowed(t) for t in targets):
            return True
    return False


# ---- 删除白名单保护（清理类任务）：只允许删除"明确列出"的临时/可再生文件 ----
DELETE_VERBS = (
    r'\bdel\b', r'\berase\b', r'\brm\b', r'\brmdir\b', r'\brd\b',
    r'\bremove-item\b', r'\bunlink\b', r'git\s+rm', r'git\s+clean',
)
# 允许删除的文件名模式（临时/可再生产物）
CLEANUP_ALLOWED_NAME = (
    r'\.pyc-check$', r'\.pyc$', r'\.pyo$', r'\.tmp$', r'\.bak-\d[\w.\-]*$',
    r'_check\d*\.txt$', r'_diag[\w.\-]*\.pyc-check$',
)
# 允许删除的目录标记（运行时生成目录，非项目资产）
CLEANUP_ALLOWED_DIR = (
    '\\codex_logs\\', '\\audio_cache\\', '\\__pycache__\\',
    '\\web\\generated\\', '\\node_modules\\', '\\dist\\', '\\build\\',
)


def _is_delete_command(cmd: str) -> bool:
    low = cmd.lower()
    return any(re.search(v, low) for v in DELETE_VERBS)


def _delete_targets(cmd: str) -> list:
    """粗略提取删除命令里的目标路径（引号内优先，其次裸词）。"""
    targets = re.findall(r"['\"]([^'\"]+)['\"]", cmd)
    if not targets:
        tokens = cmd.split()
        targets = [t for t in tokens if '\\' in t or '/' in t or '.' in t]
    return targets


def _target_allowed(target: str) -> bool:
    low = target.lower()
    if any(m in low for m in CLEANUP_ALLOWED_DIR):
        return True
    if any(re.search(p, low) for p in CLEANUP_ALLOWED_NAME):
        return True
    temp = os.environ.get('TEMP') or os.environ.get('TMP') or ''
    return bool(temp and temp.lower() in low)


# ---------- 清理安全守则 ----------

CLEANUP_KEYWORDS = (
    '清理', '删除', '清除', 'cleanup', 'clean up', 'delete temp',
    'remove temp', '删除临时', '临时文件', '垃圾文件', '残留', '清理临时',
)

CLEANUP_SAFETY_RULES = (
    '\n\n【清理任务操作规范（删除不可逆，先确认再动手）】\n'
    '1. 用户明确点名要删的文件/目录直接删；用户只是笼统说「清理/删除」时，'
    '动手前先列出将要删除的完整清单（路径+原因）展示给用户，确认后再删；\n'
    '2. 不限制文件类型与目录：git 已跟踪文件、递归删除、整目录删除均可，只要用户同意；\n'
    '3. 拿不准是否该删的文件一律跳过并在总结中说明，绝不「顺手删掉」。'
)


def _is_cleanup_task(task_desc: str) -> bool:
    low = str(task_desc or '').lower()
    return any(k.lower() in low for k in CLEANUP_KEYWORDS)


def _cleanup_safety_appendix(task_desc: str) -> str:
    """清理类任务的强制安全附录：白名单式约束，随任务一起下发给编程助手。"""
    return CLEANUP_SAFETY_RULES if _is_cleanup_task(task_desc) else ''


# ---- 复杂任务：先写任务规范（目标 / 验收标准），再动代码 ----
COMPLEX_TASK_KEYWORDS = (
    '优化', '重构', '项目', '大型', '模块', '系统', '架构', '新功能', '实现',
    'overhaul', 'refactor', 'project', 'architecture', 'feature', 'integrate',
)

TASK_SPEC_TEMPLATE = (
    '\n\n【任务执行规范（复杂任务强制：先写规范，再动代码）】\n'
    '在动手改任何代码之前，先在工作目录新建或更新 TASK_SPEC.md（若已存在先完整读取再更新），'
    '内容必须包含以下 5 节：\n'
    '1. 目标：一两句话说清要达成的最终结果；\n'
    '2. 范围与不做：明确本次改动边界，并列出明确不做的事（防止越改越多）；\n'
    '3. 验收标准：至少 3 条可验证的检查点，每条都能"执行一条命令或查看一个文件"来验证'
    '（如：语法检查/测试通过、某接口返回预期、旧功能不回归）；\n'
    '4. 实施步骤：按小步列出，每完成一步立即验证（py_compile / 运行测试 / 冒烟），'
    '不要攒到最后才一次验证；\n'
    '5. 风险与回滚：涉及核心文件时先 git diff 确认改动面，说明出问题如何回滚。\n'
    '写完 TASK_SPEC.md 后再开始改代码；全部完成后逐条对照验收标准自检，'
    '并在最终汇报里附上每条验收标准的验证证据（命令输出 / 测试结果），'
    '且必须包含如下机器可核验的验收块（每行一条，PASS/FAIL 后写标准与证据）：\n'
    '【验收核验】\n'
    'PASS <验收标准>（证据）\n'
    'FAIL <验收标准>（证据）\n'
    '【验收核验结束】\n'
    '补充：需要了解现有代码时，禁止整读超大文件（如几 MB 的日志/构建产物）；'
    '先看文件大小，用关键词搜索（rg / search_text / findstr）或 Tail 只读相关片段；'
    '需要生成/写入长文件（预计超 300 行，如完整技能 skill.py、大型模块）时，'
    '禁止一次性生成全部内容——模型输出有长度上限，一次写完必被截断（历史事故：'
    '技能文件被截成 78 行、6 个工具全丢）；先写骨架（结构+注册表+空实现桩）落盘，'
    '再用 apply_patch/追加方式分段补全，全部完成后重读文件尾部 + 语法/导入校验确认完整；'
    '遇到同一错误连续出现时，'
    '先停下来诊断根因，不要换着方法反复硬试。'
)


def _is_complex_task(task_desc: str) -> bool:
    """复杂任务判定：较长且含优化/重构/项目/实现等关键词，或本身就是长任务描述。"""
    low = str(task_desc or '').lower()
    if len(low) >= 200:
        return True
    return len(low) >= 60 and any(k.lower() in low for k in COMPLEX_TASK_KEYWORDS)


def _task_spec_enabled() -> bool:
    """复杂任务是否强制「先写 TASK_SPEC.md + 输出验收核验块」：
    settings.json -> harness.task_spec_required（默认关闭）。

    默认关闭的理由：这套仪式不产出代码，却把每个稍长的任务都拉长一截——
    判定阈值极宽（描述 ≥200 字，或 ≥60 字且含「优化/重构/项目/模块/系统/
    架构/实现」任一关键词，几乎覆盖所有真实开发任务），命中后还要模型额外
    复述一遍验收标准。需要严格流程留痕时再打开。
    """
    try:
        p = Path(__file__).resolve().parent / "settings.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            h = (cfg or {}).get("harness", {}) or {}
            return bool(h.get("task_spec_required", False))
    except Exception:
        pass
    return False


def _task_spec_appendix(task_desc: str) -> str:
    """复杂任务的规范模板：先写目标与验收标准，再动手（默认关闭，见 _task_spec_enabled）。"""
    if not _task_spec_enabled():
        return ''
    return TASK_SPEC_TEMPLATE if _is_complex_task(task_desc) else ''


# ---- 续接任务指引：优先读 TASK_SPEC.md 检查点，不整读旧日志 ----
CONTINUE_KEYWORDS = (
    '继续', '接着', '续接', '上次', '被打断', '暂停', '接着完成', '继续完成',
    'continue', 'resume', 'from where', 'pick up',
)

CONTINUE_APPENDIX = (
    '\n\n【续接任务指引】\n'
    '1. 若工作目录存在 TASK_SPEC.md，先完整读取它作为任务规范与检查点，'
    '按其中的验收标准继续，不要从头重读旧日志；\n'
    '2. 先用 git status / git diff 查看上次改动是否已落盘，只在未完成的部分继续，'
    '不要重做已完成的工作；\n'
    '3. 禁止整读超大日志文件（先看文件大小，用 -Tail / Select-String 只读所需片段）；\n'
    '4. 上次写入的长文件（技能/模块/脚本）先读文件尾部和校验，确认没被截断'
    '（语法残缺、函数/字典写到一半、工具缺失都是截断迹象）；截断就先补齐再继续；\n'
    '5. 完成时对照 TASK_SPEC.md 的验收标准自检，并在汇报里附上验证证据'
    '（命令输出 / 测试结果），不要只写一句「已完成」。'
)


def _is_continuation_task(task_desc: str) -> bool:
    low = str(task_desc or '').lower()
    return any(k.lower() in low for k in CONTINUE_KEYWORDS)


def _continuation_appendix(task_desc: str) -> str:
    """续接类任务的指引：把 TASK_SPEC.md 当检查点，续接成本降到最低。"""
    return CONTINUE_APPENDIX if _is_continuation_task(task_desc) else ''


# ---- 长文件写入任务：防截断守则（先骨架 + 分段补全 + 写后完整性校验） ----
LONG_FILE_KEYWORDS = (
    '新建技能', '创建技能', '开发技能', '写一个技能', '生成技能', '写技能',
    '技能文件', 'skill.py', 'skill 文件', '新建 skill', '创建 skill',
    '写 skill', '生成 skill', 'skill_dev', '新建模块', '新模块',
)

LONG_FILE_APPENDIX = (
    '\n\n【长文件写入守则（强制）】\n'
    '1. 本次任务需要生成/写入长文件（技能 skill.py、大型模块/脚本等）时，'
    '禁止一次生成全部内容：先写骨架（模块结构 + 工具/函数注册表 + 空实现桩）落盘，'
    '再分段补全（apply_patch 追加/插入），每补完一段立即做语法校验；\n'
    '2. 模型输出有长度上限，一次性写完必被截断（历史事故：技能文件被截成 78 行、'
    '6 个工具全丢）；宁可多调用几次工具，也不要赌一次写完；\n'
    '3. 全部完成后：重读文件尾部确认结尾完整（不是写到一半）、编译/导入通过、'
    '清单声明的每个工具都有实现；任何一项不满足就补齐后重验，'
    '并在最终汇报中说明文件行数与完整性校验结果。'
)


def _long_file_appendix(task_desc: str) -> str:
    """写长文件/技能类任务的防截断守则。"""
    low = str(task_desc or '').lower()
    return LONG_FILE_APPENDIX if any(k.lower() in low for k in LONG_FILE_KEYWORDS) else ''


def _verify_acceptance(task_desc: str, raw: str, body: str) -> Optional[dict]:
    """系统核验验收标准：扫描输出/摘要中的【验收核验】块，统计 PASS/FAIL。

    返回 None = 非复杂任务不核验；found=False = 复杂任务但没输出验收块（无法确认）；
    found=True 时 ok 表示 PASS>=1 且 FAIL==0。
    """
    if not _task_spec_enabled() or not _is_complex_task(task_desc):
        return None
    text = "\n".join(x for x in (str(raw or ''), str(body or '')) if x)[-8000:]
    m = re.search(r'【验收核验】\s*(.*?)(?:【验收核验结束】|$)', text, re.S)
    if not m or not m.group(1).strip():
        return {'found': False, 'pass': 0, 'fail': 0, 'ok': None,
                'reason': '输出中未找到【验收核验】块，无法系统确认达标'}
    block = m.group(1)
    passes = len(re.findall(r'^\s*PASS\b', block, re.M | re.I))
    fails = len(re.findall(r'^\s*FAIL\b', block, re.M | re.I))
    ok = passes >= 1 and fails == 0
    return {'found': True, 'pass': passes, 'fail': fails, 'ok': ok,
            'reason': '' if ok else f'FAIL {fails} 条 / PASS {passes} 条'}


def _is_noise(line: str) -> bool:
    low = (line or '').strip()
    if not low:
        return True
    if re.match(r'^message=', low):
        return True
    if low.startswith('$ ') or low.startswith('Wrote file successfully'):
        return True
    return False


def _condense(line: str) -> str:
    line = re.sub(r'timestamp=\S+', '', line)
    line = re.sub(r'level=\w+', '', line)
    line = re.sub(r'run=\S+', '', line)
    line = re.sub(r'\b\d+\.[\d.]+s\b', '', line)
    line = re.sub(r'\s+', ' ', line).strip(' |·')
    return '' if _is_noise(line) else line


def _is_error_line(line: str) -> bool:
    low = line.lower()
    return any(s.lower() in low for s in ERROR_SUBSTRINGS)


# ---------- 结构化执行明细（下钻用） ----------
# codex/opencode CLI 的 stdout 会被完整写入 codex_logs/<task_id>.log。
# 这里把每一行归类成可下钻的条目：header / turn（user/codex 角色）/ tool（工具调用开始）/
# args（调用参数）/ out（中间输出）/ tool_end（工具结束+耗时）/ log（其他原始行）。
# 每条目带全局递增 seq，前端按 seq 增量渲染、断档时通过 /api/tasks/<id>/trace 回补，
# 保证展示与后台真实日志文件始终一致（日志文件即唯一事实源）。

_ROLE_MARKERS = {'user', 'codex', 'opencode', 'assistant', 'system', 'developer', 'tool'}

_TOOL_NAMES = {
    'exec', 'bash', 'pwsh', 'shell', 'cmd', 'read', 'write', 'edit', 'apply_patch',
    'grep', 'glob', 'search', 'list', 'view', 'read_image', 'remove', 'rename', 'copy',
    'fetch', 'web_search', 'web_fetch', 'todo_write', 'subagent', 'subagent_fork',
    'send_message', 'interrupt_agent', 'list_agents', 'create_goal', 'update_goal',
    'get_goal', 'ask_user_question', 'job_list', 'job_output', 'job_kill', 'evaluate',
    'run_code', 'skill', 'workflow', 'ralph', 'exit_plan_mode', 'plan',
}

_TOOL_END_RE = re.compile(
    r'^(?:succeeded|failed|completed|error|timeout|exited)\b.*?'
    r'(?:in\s+)?([\d.]+)\s*ms\b.*$',
    re.I,
)


def _classify_line(line: str):
    """返回 (kind, payload)；kind ∈ tool / tool_end / turn / log。"""
    s = (line or '').strip()
    if not s:
        return None
    if s in _ROLE_MARKERS:
        return ('turn', s)
    m = _TOOL_END_RE.match(s)
    if m and len(s) <= 80:
        return ('tool_end', m.group(1))
    if s in _TOOL_NAMES:
        return ('tool', s)
    return ('log', s)


class TraceState:
    """把原始日志行流式归类为带 seq 的条目，维护工具栈以正确归属参数/输出。"""

    MAX_ENTRIES = 50000  # 内存保留上限（超出丢最旧，seq 仍单调递增）

    def __init__(self):
        self.seq = 0
        self.tool_count = 0
        self.entries = []
        self.open_tools = []          # 未结束的 tool 条目下标（相对 entries）
        self._pending_args = False    # tool 行之后的下一行当作参数
        self._in_turn = None          # 当前叙述角色（user/codex/...）

    def feed(self, raw: str):
        """喂入一行原始日志，返回生成的条目 dict（可能为 None）。"""
        kind, payload = (None, None)
        cls = _classify_line(raw)
        if cls is None:
            return None
        kind, payload = cls
        self.seq += 1
        entry = {
            'seq': self.seq,
            'type': kind,
            'text': (raw or '').rstrip(),
        }
        if kind == 'tool':
            self.tool_count += 1
            entry['tool'] = payload
            entry['idx'] = self.tool_count
            entry['status'] = 'running'
            self.entries.append(entry)
            self.open_tools.append(len(self.entries) - 1)
            self._pending_args = True
            self._in_turn = None
        elif kind == 'tool_end':
            dur = None
            try:
                dur = int(round(float(payload))) if payload else None
            except (TypeError, ValueError):
                dur = None
            low = (raw or '').lower()
            entry['status'] = 'success' if ('succeeded' in low or 'completed' in low) else 'error'
            entry['dur_ms'] = dur
            entry['tool'] = self._last_open_tool_name()
            self.entries.append(entry)
            # 结束最近一个未结束且名字匹配（或栈顶）的工具
            if self.open_tools:
                idx = self.open_tools.pop()
                self.entries[idx]['status'] = entry['status']
                if dur is not None:
                    self.entries[idx]['dur_ms'] = dur
            self._pending_args = False
        elif kind == 'turn':
            entry['role'] = payload
            self.entries.append(entry)
            self._pending_args = False
            self._in_turn = payload
        else:
            entry['tool'] = self._last_open_tool_name()
            if self._pending_args:
                entry['type'] = 'args'
                self._pending_args = False
            elif self.open_tools:
                entry['type'] = 'out'
            else:
                entry['type'] = 'log'
                if self._in_turn:
                    entry['role'] = self._in_turn
            self.entries.append(entry)
        # 封顶：丢最旧，但 seq 不回退
        if len(self.entries) > self.MAX_ENTRIES:
            drop = len(self.entries) - self.MAX_ENTRIES
            del self.entries[:drop]
            self.open_tools = [i - drop for i in self.open_tools if i >= drop]
        return dict(entry)

    def _last_open_tool_name(self):
        if not self.open_tools:
            return ''
        return str(self.entries[self.open_tools[-1]].get('tool') or '')

    def seed_from_file(self, log_path, upto_bytes=None):
        """预解析文件已有内容（重启恢复时对齐 seq / 工具栈），不保留条目。"""
        try:
            with open(log_path, 'rb') as f:
                if upto_bytes is not None:
                    size = os.path.getsize(log_path)
                    f.seek(min(int(upto_bytes), size))
                else:
                    f.seek(0)
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    line = ANSI_RE.sub('', decode_bytes(raw)).rstrip()
                    if line:
                        self.feed(line)
                    if upto_bytes is not None and f.tell() >= int(upto_bytes):
                        break
        except OSError:
            pass

    def slice(self, after: int, limit: int):
        first_seq = self.entries[0]['seq'] if self.entries else 0
        # 只有真的丢过最旧条目（first_seq > 1）且请求早于保留起点时才需要回退文件全量；
        # 从头开始（first_seq == 1）时 after=0 属于正常增量请求，直接返回。
        if after < first_seq and first_seq > 1:
            return None  # 被内存封顶丢掉的历史 → 需要回到文件全量解析
        out = [e for e in self.entries if e['seq'] > after][:limit]
        return out


_LIVE_TRACES: dict = {}
_LIVE_TRACES_LOCK = threading.Lock()
_TRACE_FILE_CACHE: dict = {}


def _register_live_trace(task_id: str, state: TraceState) -> None:
    with _LIVE_TRACES_LOCK:
        _LIVE_TRACES[task_id] = state
        if len(_LIVE_TRACES) > 80:
            for k in list(_LIVE_TRACES)[:40]:
                _LIVE_TRACES.pop(k, None)


def _parse_log_file(log_path: str, after: int = 0, limit: int = 500) -> tuple:
    """从日志文件全量解析（文件即事实源）。返回 (entries_after, state)。
    中等大小文件按 (size, mtime) 缓存；超大文件每次现解析（解析后即释放）。
    """
    state = TraceState()
    try:
        st = os.stat(log_path)
        key = (str(log_path), st.st_size, st.st_mtime_ns)
        cached = _TRACE_FILE_CACHE.get(key)
        if cached is not None:
            return _slice_cached(cached, after, limit)
        return _parse_full(log_path, key, state, after, limit)
    except OSError:
        return [], state


def _slice_cached(cached, after: int, limit: int) -> tuple:
    """从缓存状态切片出 after 之后的条目。"""
    cached_out = []
    for e in cached.entries:
        if e['seq'] > after:
            cached_out.append(e)
            if len(cached_out) >= limit:
                break
    return cached_out, cached


def _parse_full(log_path: str, key, state, after: int, limit: int) -> tuple:
    """全量解析日志文件，中等大小结果写入缓存。"""
    out = []
    reached = False
    with open(log_path, 'rb') as f:
        while True:
            raw = f.readline()
            if not raw:
                break
            line = ANSI_RE.sub('', decode_bytes(raw)).rstrip()
            if not line:
                continue
            e = state.feed(line)
            if e and e['seq'] > after and not reached:
                out.append(e)
                if len(out) >= limit:
                    # 结果已够，但仍需喂完剩余行以得到准确的 seq/计数
                    reached = True
    # 只缓存中等文件（约 ≤2 万条），超大文件避免长期占内存
    if state.seq <= 20000:
        if len(_TRACE_FILE_CACHE) >= 4:
            _TRACE_FILE_CACHE.pop(next(iter(_TRACE_FILE_CACHE)), None)
        _TRACE_FILE_CACHE[key] = state
    return out, state


def get_task_trace(task_id: str, after: int = 0, limit: int = 500):
    """返回任务的已解析执行明细（供前端下钻/回补）。无日志文件的任务返回 None。"""
    try:
        entry = _reg_get(task_id)
    except Exception:
        entry = {}
    log_path = str(entry.get('log_path') or '')
    if not log_path or not os.path.exists(log_path):
        return None
    # 优先用本进程内的实时解析状态（快且与推送一致）；历史断档回退文件全量
    live = None
    with _LIVE_TRACES_LOCK:
        live = _LIVE_TRACES.get(task_id)
    if live is not None:
        entries = live.slice(after, limit)
        if entries is not None:
            return {
                'task_id': task_id,
                'status': entry.get('status', 'running'),
                'entries': entries,
                'next_seq': live.seq,
                'lines_total': live.seq,
                'steps_total': live.tool_count,
                'log_size': os.path.getsize(log_path),
            }
    entries, state = _parse_log_file(log_path, after=after, limit=limit)
    return {
        'task_id': task_id,
        'status': entry.get('status', 'running'),
        'entries': entries,
        'next_seq': state.seq,  # 全量解析后的总 seq，limit 截断时也正确
        'lines_total': state.seq,
        'steps_total': state.tool_count,
        'log_size': os.path.getsize(log_path),
    }


def get_task_log_tail(task_id: str, offset: int = 0, max_lines: int = 1000):
    """按字节偏移追读原始日志（可与 trace 对照，保证可追溯）。"""
    try:
        entry = _reg_get(task_id)
    except Exception:
        entry = {}
    log_path = str(entry.get('log_path') or '')
    if not log_path or not os.path.exists(log_path):
        return None
    offset = max(0, int(offset))
    max_lines = max(1, min(int(max_lines), 5000))
    lines = []
    try:
        size = os.path.getsize(log_path)
        with open(log_path, 'rb') as f:
            f.seek(min(offset, size))
            while len(lines) < max_lines:
                raw = f.readline()
                if not raw:
                    break
                lines.append(ANSI_RE.sub('', decode_bytes(raw)).rstrip())
            end = f.tell()
    except OSError:
        size = 0
        end = offset
    return {
        'task_id': task_id,
        'status': entry.get('status', 'running'),
        'offset': end,
        'size': size,
        'lines': lines,
        'truncated': end < size,
    }


def _match_tool(name: str):
    tools = AGENT_CFG.get('tools') or {}
    n = str(name or '').strip().lower()
    if not n:
        return None
    if n in tools:
        return n
    for k, v in tools.items():
        cmd = [str(c).lower() for c in (v.get('cmd') or [])]
        if not cmd:
            continue
        base = os.path.basename(cmd[0])
        base = re.sub(r'\.(exe|cmd|bat)$', '', base)
        if n == base or n in cmd:
            return k
    return None


# ---------- LLM 规划分流 ----------

ASSISTANT_SYS = ''  # 懒初始化，依赖 AGENT_CFG tools


def _build_assistant_sys() -> str:
    tools = AGENT_CFG.get('tools') or {}
    tool_desc = '、'.join(f"{k}(={','.join(v.get('cmd', []))})" for k, v in tools.items())
    return (
        '你是运行在用户Windows电脑上的AI助手，能通过命令行操作电脑，还能调度外部AI编程工具。'
        f'可用工具：{tool_desc}。'
        '分析用户消息后只返回一个JSON对象（禁止任何多余文字和markdown代码块）：'
        '{"reply":"给用户的自然语言回答，闲聊/问答时填这里，无则空字符串",'
        '"steps":[{"desc":"步骤说明","cmd":"Windows cmd命令"}],'
        '"tool":"","task":""}'
        '规则：'
        '1) 打招呼、闲聊、问答、咨询→只填reply；'
        '2) 简单电脑操作（查文件、开程序、看状态）→填steps；'
        '3) 用户提到opencode/codex或要求编写完整项目/游戏/应用等大型编码任务→填tool(工具名)和task(给工具的完整任务描述)，reply和steps留空；'
        '   但如果用户明确说要使用 DSH/DeepSeek Harness 智能体，禁止选 tool，reply 也留空（放行给主 Agent 走 DSH 桥接）；'
        '4) 历史消息会附在下方【对话历史】中。用户说"继续/接着/修改/刚才那个/这个"等指代时，'
        '必须结合历史判断指的是什么，把完整的上下文写进task，让工具无需翻历史也能执行；'
        '5) 严禁破坏性命令。'
        '6) 画图/图片/壁纸/立绘/头像/插画/海报/生成图片/绘画/绘图/视频生成等视觉类需求'
        '（用户想"得到一张图"而非"处理/分析已有图片"）→ 禁止填 tool 和 steps，reply 留空，'
        '放行给主 Agent 用 image_gen_create 技能现场生成；不要把视觉生成当编码任务路由给工具。'
        '7) 【路径禁臆造】打开/播放/删除用户文件时，严禁猜测完整路径——你并不知道文件在哪。'
        '正确做法：第一步先用查找命令定位（如 dir /s /b "C:\\Users\\*文件名*" 或 '
        'dir /s /b D:\\*关键词*.mp4），后续步骤再使用查到的真实完整路径；'
        '宁可多一步查找，也绝不编造一个不存在的路径。'
    )


def _llm_post(path_suffix: str, payload: dict, timeout: int):
    base = LLM_CFG['base_url'].rstrip('/')
    candidates = [base] if re.search(r'/v\d+', base) else [base + '/v1', base]
    key = LLM_CFG.get('api_key', '')
    last_err = None
    for u in candidates:
        try:
            resp = _session.post(
                u + path_suffix,
                headers={'Authorization': f'Bearer {key}'} if key else {},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 404:
                last_err = f'{u}{path_suffix} 404'
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_err = f'{u}: {e}'
            continue
    raise RuntimeError(f'LLM接口不可达：{last_err}')


def llm_respond(task: str, history: list = None, cwd: str = None) -> dict:
    cwd = cwd or AGENT_CFG.get('work_dir', '')
    sys_prompt = _build_assistant_sys()
    msgs = [{'role': 'system', 'content': sys_prompt}]
    for h in (history or [])[-20:]:
        role = 'assistant' if h.get('role') == 'assistant' else 'user'
        text = str(h.get('text') or '')[:1500]
        if text:
            msgs.append({'role': role, 'content': text})
    msgs.append({'role': 'user', 'content': f'当前目录: {cwd}\n用户消息: {task}'})
    resp = _llm_post(
        '/chat/completions',
        {
            'model': LLM_CFG.get('model', ''),
            'temperature': 0.2,
            'messages': msgs,
        },
        timeout=60,
    )
    content = resp.json()['choices'][0]['message']['content']
    content = re.sub(r'^```(json)?\s*|\s*```$', '', content.strip(), flags=re.M).strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            raise ValueError(f'LLM返回非JSON：{content[:200]}')
        data = json.loads(m.group(0))
        print(f'[codex_runner] LLM输出含杂质已提取JSON', flush=True)
    assert isinstance(data, dict)
    return data


def _llm_summarize(text: str, max_chars: int = 900) -> str:
    if not llm_available() or not text.strip():
        return ''
    try:
        resp = _llm_post(
            '/chat/completions',
            {
                'model': LLM_CFG.get('model', ''),
                'temperature': 0.2,
                'messages': [
                    {'role': 'system', 'content': (
                        '你是消息摘要助手。把一段AI编码工具的运行输出整理成精炼、面向普通用户的中文摘要。'
                        '规则：只输出摘要正文，禁止任何前缀、代码块标记、标题或多余客套；'
                        '保留关键结果、文件路径、关键数字、任务是否成功、错误原因；'
                        '删除内部日志、过程性噪音、重复内容；'
                        f'全文控制在{max_chars}字以内。'
                    )},
                    {'role': 'user', 'content': text[:8000]},
                ],
            },
            timeout=60,
        )
        content = resp.json()['choices'][0]['message']['content']
        content = re.sub(r'^```(json)?\s*|\s*```$', '', content.strip(), flags=re.M).strip()
        if not content:
            return ''
        return content[:max_chars]
    except Exception:
        return ''


# ---------- 执行器 ----------

def _env_workspace() -> str:
    """进程级工作区覆盖：长跑 worker 用独立工作区跑，产出不落到主项目。

    全局 work_dir 是 server 与所有子进程共享的配置，改它会污染主会话；
    环境变量天然只作用于本进程及其子进程，所以隔离走这里。
    """
    return (os.environ.get('DABAI_WORKSPACE') or '').strip()


class Executor:
    def __init__(self):
        self.cwd = _env_workspace() or AGENT_CFG.get('work_dir') or str(BASE_DIR)
        # 保证目录存在
        try:
            os.makedirs(self.cwd, exist_ok=True)
        except Exception:
            self.cwd = str(BASE_DIR)
        self.tasks = {}

    def run_sync(self, cmd: str, timeout: int = None, argv: list = None,
                 cwd: str = None) -> str:
        """执行一条命令。argv 非空时直传 exec（不走 shell），cwd 覆盖实例默认工作目录。

        argv/cwd 是给沙箱用的：普通用户的命令要经 bwrap 包装后按 argv 直接执行，
        且工作目录必须锁进他的沙箱目录。
        """
        timeout = timeout or AGENT_CFG.get('sync_timeout_sec', 120)
        work = cwd or self.cwd
        # 自成进程组：超时要杀整棵进程树，否则 shell 只是直接子进程，curl/编译等
        # 孙进程变孤儿继续占网络与资源——「命令超时了机器还一直慢」的来源。
        kw = pc.spawn_kwargs(new_group=True)
        try:
            if argv:
                p = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **kw)
            else:
                p = subprocess.Popen(cmd, shell=True, cwd=work,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        except Exception as e:
            return f'$ {cmd}\n[异常] {e}'
        try:
            out_b, err_b = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            ok, note = pc.terminate_tree(p.pid, timeout=15)
            try:
                out_b, err_b = p.communicate(timeout=5)
            except Exception:
                out_b, err_b = b'', b''
            got = (decode_bytes(out_b or b'').strip() + '\n'
                   + decode_bytes(err_b or b'').strip()).strip()
            snippet = f'\n[超时前输出] …{got[-500:]}' if got else ''
            return (f'$ {cmd}\n[超时：超过{timeout}秒，已终止整棵进程树（{note}）]'
                    f'{snippet}')
        text = decode_bytes(out_b)
        err = decode_bytes(err_b)
        if err.strip():
            text += '\n[stderr]\n' + err
        text = text.strip() or '(无输出)'
        return f'$ {cmd}\n[exit={p.returncode}]\n{text}'

    def start_bg(self, cmd: str) -> str:
        tid = uuid.uuid4().hex[:6]
        proc = subprocess.Popen(
            cmd, shell=True, cwd=self.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        buf = deque(maxlen=500)
        self.tasks[tid] = {'proc': proc, 'buf': buf, 'cmd': cmd, 'started': time.time()}

        def reader(proc=proc, buf=buf):
            for raw in iter(proc.stdout.readline, b''):
                buf.append(decode_bytes(raw).rstrip())

        threading.Thread(target=reader, daemon=True).start()
        return tid

    def task_report(self, tid: str) -> str:
        t = self.tasks.get(tid)
        if not t:
            return f'任务 {tid} 不存在。现有：{", ".join(self.tasks) or "无"}'
        code = t['proc'].poll()
        out = '\n'.join(list(t['buf'])[-40:]) or '(暂无输出)'
        status = '运行中' if code is None else f'已结束(exit={code})'
        return f'[任务{tid}] {t["cmd"]}\n状态: {status}\n{out}'

    def kill(self, tid: str) -> str:
        t = self.tasks.get(tid)
        if not t:
            return f'任务 {tid} 不存在'
        if t['proc'].poll() is None:
            t['proc'].kill()
            return f'任务 {tid} 已终止'
        return f'任务 {tid} 已结束，无需终止'

    def cleanup(self) -> None:
        for tid, t in list(self.tasks.items()):
            if t['proc'].poll() is not None and time.time() - t['started'] > 3600:
                self.tasks.pop(tid, None)

    def _resolve_argv(self, base_argv: list):
        argv = list(base_argv)
        path = shutil.which(argv[0])
        if path:
            argv[0] = path
            if path.lower().endswith(('.cmd', '.bat')):
                argv = ['cmd', '/c'] + argv
            return argv
        return argv if os.path.isabs(argv[0]) else None


EXECUTOR = Executor()

# ---------- 任务传递方式 ----------
# 本机 codex/opencode 均为 .CMD 垫片，实际经 `cmd /c` 启动：
# - cmd.exe 命令行总长上限约 8191 字符，超长任务会被拒绝或截断；
# - cmd 会对命令串做二次解析（%VAR% 展开、引号配对等），任务文本里的 % 或引号可能被替换/篡改。
# 因此 codex/opencode 一律通过 stdin 传入完整任务（两者均支持"无消息参数时从 stdin 读取"），
# 彻底规避上述截断/篡改；其余自定义工具保持命令行传参（超长时明确报错，绝不静默截断）。
_STDIN_TASK_AGENTS = ('codex', 'opencode')
# 非 stdin 工具的 argv 方式安全上限（保守低于 cmd.exe 的 8191 字符）
_CMDLINE_SAFE_LIMIT = 6000


def _build_spawn(base_argv: list, base_cmd: list, task: str):
    """决定如何把任务交给 CLI，返回 (启动参数列表, stdin 文本或 None)。"""
    argv = list(base_argv)
    head = (base_cmd[0] if base_cmd else '')
    name = os.path.splitext(os.path.basename(head))[0].lower()
    if name in _STDIN_TASK_AGENTS:
        return argv, task
    cmdline_len = len(subprocess.list2cmdline([*argv, task]))
    if cmdline_len > _CMDLINE_SAFE_LIMIT:
        raise ValueError(
            f'任务内容过长（命令行 {cmdline_len} 字符，超过 Windows '
            f'{_CMDLINE_SAFE_LIMIT} 字符安全上限），且工具 {head or "?"} '
            '不支持通过 stdin 接收任务，请缩短任务描述后重试'
        )
    return argv + [task], None

# 初始化时同步 mtime
try:
    _cfg_mtime = os.path.getmtime(str(CONFIG_PATH))
except Exception:
    pass


# ---------- 异步网页推送封装（供 server.py 调用） ----------

async def _ws_send(ws, payload: dict):
    '''安全推送到前端（ws 可能已关闭）'''
    try:
        if ws is None:
            return
        # 兼容 FastAPI WebSocketState
        try:
            from starlette.websockets import WebSocketState
            if ws.client_state != WebSocketState.CONNECTED:
                return
        except Exception:
            pass
        await ws.send_json(payload)
    except Exception:
        pass


def _get_label(base: list) -> str:
    return os.path.basename(base[0]) if base else 'tool'


# ---------- 独立进程：注册表 / 进程探测 / 日志追读 ----------

_kernel32 = None


def _win_kernel32():
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    return _kernel32


def _pid_alive(pid: int) -> bool:
    """探测进程是否仍在运行（不创建新进程，跨平台；僵尸进程视为已退出）。"""
    if not pid or pid <= 0:
        return False
    if os.name != 'nt':
        return pc.pid_alive(pid)
    k32 = _win_kernel32()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
    if not h:
        # ERROR_ACCESS_DENIED(5)=进程存在但无权打开；ERROR_INVALID_PARAMETER(87)=不存在
        return ctypes.get_last_error() == 5
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True  # 取不到退出码时按存活处理（保守）
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _process_exit_code(pid: int):
    """读取已退出进程的退出码；仍在运行或无法读取时返回 None。"""
    if not pid or pid <= 0:
        return None
    if os.name != 'nt':
        return pc.process_exit_code(pid)
    k32 = _win_kernel32()
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return None
        v = code.value
        return None if v == 259 else v
    finally:
        k32.CloseHandle(h)


def _terminate_pid(pid: int) -> None:
    """单进程强杀兜底（Windows: TerminateProcess；POSIX: SIGKILL）。"""
    if not pid or pid <= 0:
        return
    if os.name != 'nt':
        pc.kill_pid(pid)
        return
    k32 = _win_kernel32()
    h = k32.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
    if not h:
        return
    try:
        k32.TerminateProcess(h, 1)
    finally:
        k32.CloseHandle(h)


def _load_registry() -> dict:
    """读取独立进程注册表（task_id -> 任务条目）。"""
    try:
        with open(RUNTIME_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tasks = data.get('tasks') or {}
        return tasks if isinstance(tasks, dict) else {}
    except Exception:
        return {}


_REGISTRY_CROSS_LOCK_FH = None


def _lock_registry_cross_process() -> None:
    """跨进程互斥（Windows msvcrt / POSIX fcntl 文件锁）：读-改-写期间防止另一进程并发覆盖。
    进程内已由 _registry_lock 串行；跨进程（server + 测试/热重载重叠窗口）靠文件锁兜底。
    """
    global _REGISTRY_CROSS_LOCK_FH
    if os.name == 'nt' and msvcrt is None:
        return
    try:
        if _REGISTRY_CROSS_LOCK_FH is None or _REGISTRY_CROSS_LOCK_FH.closed:
            _REGISTRY_CROSS_LOCK_FH = open(str(RUNTIME_FILE) + '.lock', 'a+', encoding='utf-8')
        fh = _REGISTRY_CROSS_LOCK_FH
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write('\x00')
            fh.flush()
        fh.seek(0)
        # Windows msvcrt.LK_LOCK / POSIX fcntl.flock(LOCK_EX)：均为阻塞等待，不静默跳过临界区
        pc.lock_file(fh)
    except Exception:
        pass


def _unlock_registry_cross_process() -> None:
    if _REGISTRY_CROSS_LOCK_FH is None or _REGISTRY_CROSS_LOCK_FH.closed:
        return
    try:
        pc.unlock_file(_REGISTRY_CROSS_LOCK_FH)
    except Exception:
        pass


def _save_registry(tasks: dict) -> None:
    """原子写回注册表：每次用唯一临时文件 + fsync + os.replace。
    固定临时路径会被并发写者互相覆盖（写一半的 .tmp 被 replace 上线）；
    唯一文件名保证两个进程各写各的，replace 永远拿到完整内容。
    """
    try:
        fd, tmp = tempfile.mkstemp(prefix='codex_runtime.', suffix='.tmp', dir=str(BASE_DIR))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'tasks': tasks}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, RUNTIME_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _reg_get(task_id: str) -> dict:
    if not task_id:
        return {}
    with _registry_lock:
        return dict(_load_registry().get(str(task_id)) or {})


def _reg_upsert(task_id: str, entry: dict) -> None:
    with _registry_lock:
        _lock_registry_cross_process()
        try:
            tasks = _load_registry()
            # 简单清理：只保留最近一天的完成记录，避免无限膨胀
            if len(tasks) > 200:
                old = [tid for tid, e in tasks.items()
                       if e.get('status') != 'running'
                       and (time.time() - float(e.get('updated') or 0)) > 86400]
                for tid in old:
                    tasks.pop(tid, None)
            tasks[str(task_id)] = entry
            _save_registry(tasks)
        finally:
            _unlock_registry_cross_process()


def _reg_patch(task_id: str, **kw) -> None:
    with _registry_lock:
        _lock_registry_cross_process()
        try:
            tasks = _load_registry()
            e = tasks.get(str(task_id))
            if e is None:
                return
            e.update(kw)
            _save_registry(tasks)
        finally:
            _unlock_registry_cross_process()


def _reg_remove(task_id: str) -> None:
    """删除注册表条目（测试自清理 / 显式清理幽灵任务）。"""
    with _registry_lock:
        _lock_registry_cross_process()
        try:
            tasks = _load_registry()
            if tasks.pop(str(task_id), None) is not None:
                _save_registry(tasks)
        finally:
            _unlock_registry_cross_process()


def _spawn_independent(argv: list, cwd: str, stdin_text, log_path: str):
    """以独立进程方式启动 codex/opencode：
    - 输出重定向到日志文件，父进程（server）热重载/重启不会断输出、不会杀进程；
    - Windows：新建进程组 + 脱离 job + 无控制台（Ctrl+C/控制台关闭都波及不到）；
    - 非 Windows：start_new_session 脱离会话。
    """
    os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
    logf = open(log_path, 'ab')
    kwargs = dict(
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    try:
        if os.name == 'nt':
            # 不能再用 DETACHED_PROCESS：codex/opencode 是 .cmd 垫片（经 cmd /c 启动），
            # DETACHED_PROCESS 下 stdin 管道与输出重定向对 Node 类 CLI 均异常——
            # 表现为“进程能建出来，但无任何输出、约 1 秒后以退出码 1 静默失败”。
            # CREATE_NO_WINDOW 同样无控制台窗口、父进程/控制台关闭波及不到，
            # 且 stdin 传任务、日志重定向都正常工作。
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                return subprocess.Popen(
                    argv,
                    creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                    **kwargs,
                )
            except OSError:
                # 外层 job 不允许 breakaway 时退回普通独立进程组
                return subprocess.Popen(argv, creationflags=flags, **kwargs)
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    finally:
        try:
            logf.close()
        except Exception:
            pass


def _tail_file(log_path: str, start_offset: int, stop: threading.Event,
               out_lines: list, lock: threading.Lock, shared: dict,
               task_id: str = None, proc=None, pid: int = None) -> None:
    """持续追读独立进程的日志文件，直到进程结束或被停止；解析后的行写入 out_lines。
    shared['offset'] 记录已读字节数，供重启恢复时从断点继续。
    """
    try:
        f = open(log_path, 'rb')
    except OSError:
        return
    try:
        try:
            size = os.path.getsize(log_path)
            f.seek(min(start_offset, size))
        except OSError:
            f.seek(0)
        last_persist = time.time()
        lines_since_persist = 0
        while True:
            raw = f.readline()
            if raw:
                line = ANSI_RE.sub('', decode_bytes(raw)).rstrip()
                if line:
                    print(line, flush=True)
                    with lock:
                        out_lines.append(line)
                shared['offset'] = f.tell()
                lines_since_persist += 1
                if task_id and (lines_since_persist >= 200 or time.time() - last_persist >= 5):
                    _reg_patch(task_id, read_offset=shared.get('offset', 0), updated=time.time())
                    last_persist = time.time()
                    lines_since_persist = 0
                continue
            if stop.is_set():
                break
            if proc is not None and proc.poll() is not None:
                break
            if pid and not _pid_alive(pid):
                break
            time.sleep(0.2)
        # 进程已结束/被停止：排空剩余内容
        while True:
            raw = f.readline()
            if not raw:
                break
            line = ANSI_RE.sub('', decode_bytes(raw)).rstrip()
            if line:
                print(line, flush=True)
                with lock:
                    out_lines.append(line)
            shared['offset'] = f.tell()
    finally:
        try:
            f.close()
        except Exception:
            pass


def kill_task(task_id: str) -> dict:
    """终止独立进程任务（连同其子进程树），并更新注册表状态。"""
    entry = _reg_get(task_id)
    if not entry:
        return {'ok': False, 'reason': '任务不存在'}
    pid = int(entry.get('pid') or 0)
    if not pid or not _pid_alive(pid):
        _reg_patch(task_id, status='killed', updated=time.time())
        return {'ok': True, 'reason': '进程已结束'}
    try:
        # Windows: taskkill /T /F；POSIX: killpg(SIGTERM→SIGKILL) —— 统一走兼容层
        ok, _reason = pc.terminate_tree(pid, timeout=15)
        if not ok and _pid_alive(pid):
            _terminate_pid(pid)
    except Exception:
        try:
            _terminate_pid(pid)
        except Exception:
            pass
    _reg_patch(task_id, status='killed', updated=time.time())
    return {'ok': True, 'pid': pid}


def recover_running_tasks() -> list:
    """热重载/重启后调用：找出仍需接管的 codex/opencode 任务（含已结束待收尾的）。

    同时修复状态不同步：无 pid 的 running 条目（测试残留/从未真正启动）直接落
    killed，避免任务中心把幽灵任务恢复成"执行中"；pid 已退出的条目保持 running，
    由 invoke_tool_recover 补发终态事件并落 done。
    """
    with _registry_lock:
        _lock_registry_cross_process()
        try:
            tasks = _load_registry()
            out = []
            changed = False
            for tid, e in tasks.items():
                if e.get('status') != 'running':
                    continue
                pid = int(e.get('pid') or 0)
                alive = bool(pid) and _pid_alive(pid)
                e2 = dict(e)
                e2['id'] = tid
                e2['alive'] = alive
                if not pid:
                    e['status'] = 'killed'
                    e['updated'] = time.time()
                    changed = True
                out.append(e2)
            if changed:
                _save_registry(tasks)
            return out
        finally:
            _unlock_registry_cross_process()


async def _monitor_codex_progress(ws, task_id: str, tool_name: str, label: str,
                                   lines: list, lock, shared: dict,
                                   start: float, timeout_sec: float,
                                   trace: TraceState, is_alive) -> dict:
    """0.5s 级轮询：新日志行实时解析为结构化条目并推送 codex_log；
    无新输出时按心跳推送 codex_progress（计数/耗时）；报错行只告警不终止。
    返回 {'exited', 'exit_code', 'elapsed', 'timed_out'}，收尾（摘要/终态事件）由调用方处理。
    """
    interval = max(3.0, min(
        float(AGENT_CFG.get('tool_progress_interval_sec', 60) or 60.0), 10.0))
    last_report = time.time()
    last_activity = time.time()   # 最近一次真实输出时间（停滞超时判定）
    sent_idx = 0
    err_scan_idx = 0
    alerted: set = set()
    last_err_alert = 0.0

    async def _check_alive():
        """兼容同步回调与异步协程两种 is_alive（测试用协程，生产用同步 lambda）。"""
        try:
            r = is_alive()
            if asyncio.iscoroutine(r) or inspect.isawaitable(r):
                r = await r
            return bool(r)
        except Exception:
            return False

    def _flush_remaining():
        """把 tail 线程已累积的行解析并发送；返回是否有新条目。"""
        nonlocal sent_idx
        with lock:
            total = len(lines)
            new_lines = lines[sent_idx:total]
            if not new_lines:
                return False
            sent_idx = total
            batch = []
            for ll in new_lines:
                e = trace.feed(ll)
                if e:
                    e['text'] = str(e.get('text') or '')[:16000]
                    batch.append(e)
        return batch

    while True:
        await asyncio.sleep(0.5)
        now = time.time()
        elapsed = int(now - start)
        exited = not await _check_alive()

        with lock:
            total = len(lines)
            new_errs = {}
            for ll in lines[err_scan_idx:]:
                if _is_noise(ll):
                    continue
                if _is_error_line(ll):
                    sig = _condense(ll)[:100]
                    if sig:
                        new_errs[sig] = new_errs.get(sig, 0) + 1
            err_scan_idx = total

        # 新行 → 结构化条目，逐批实时推送（每批最多 120 条，避免单帧消息过大）
        batch = _flush_remaining()
        if batch:
            chunk = 120
            for i in range(0, len(batch), chunk):
                part = batch[i:i + chunk]
                await _ws_send(ws, {
                    'type': 'codex_log',
                    'tool': tool_name,
                    'label': label,
                    'task_id': task_id,
                    'base_seq': part[0]['seq'],
                    'entries': part,
                    'steps_total': trace.tool_count,
                    'lines_total': trace.seq,
                    'elapsed': elapsed,
                })
            try:
                _reg_patch(task_id,
                           trace_seq=trace.seq,
                           steps_total=trace.tool_count,
                           lines_total=trace.seq,
                           read_offset=shared.get('offset', 0),
                           updated=time.time())
            except Exception:
                pass
            last_report = now
            last_activity = now

        # 报错只做提示，绝不终止（编程助手会自己消化常见错误并继续）
        fresh_sigs = [s for s in new_errs if s not in alerted]
        if fresh_sigs and now - last_err_alert >= 5:
            sample = fresh_sigs[0]
            extra = f' 等共{len(fresh_sigs)}种' if len(fresh_sigs) > 1 else ''
            await _ws_send(ws, {
                'type': 'codex_error',
                'tool': tool_name,
                'label': label,
                'message': f'⚠️ [{label}] 检测到报错输出（智能体会自行处理并继续）：{sample}{extra}',
                'sigs': fresh_sigs[:3],
            })
            alerted.update(fresh_sigs)
            last_err_alert = now

        # 心跳：即使没有新输出也按 interval 播报一次（计数/耗时保持新鲜）
        if now - last_report >= interval:
            await _ws_send(ws, {
                'type': 'codex_progress',
                'tool': tool_name,
                'label': label,
                'task_id': task_id,
                'elapsed': elapsed,
                'steps_total': trace.tool_count,
                'lines_total': trace.seq,
                'fresh': '',
            })
            last_report = now

        # 停滞超时：只要任务还在持续产出新输出就不掐断（固定总时长会误杀长任务）；
        # 连续无新输出超过 timeout_sec 才判定停滞并终止。
        if now - last_activity > timeout_sec:
            try:
                kill_task(task_id)
            except Exception:
                pass
            try:
                _reg_patch(task_id, status='timeout', updated=time.time())
            except Exception:
                pass
            return {'exited': True, 'exit_code': None, 'elapsed': elapsed, 'timed_out': True}

        if exited:
            await asyncio.sleep(0.6)  # 等 tail 线程排空最后输出
            # 再排空 3 轮，确保进程结束瞬间写出的行也被解析推送
            for _ in range(3):
                b = _flush_remaining()
                if b is False:
                    b = []
                if b:
                    for i in range(0, len(b), 120):
                        part = b[i:i + 120]
                        await _ws_send(ws, {
                            'type': 'codex_log',
                            'tool': tool_name,
                            'label': label,
                            'task_id': task_id,
                            'base_seq': part[0]['seq'],
                            'entries': part,
                            'steps_total': trace.tool_count,
                            'lines_total': trace.seq,
                            'elapsed': elapsed,
                        })
                    last_report = time.time()
                await asyncio.sleep(0.2)
            return {'exited': True, 'exit_code': None, 'elapsed': elapsed, 'timed_out': False}


async def invoke_tool_stream(tool_name: str, tcfg: dict, task: str, ws, task_id: str = None,
                            work_dir: str = None) -> None:
    """可选 work_dir：覆盖 EXECUTOR.cwd（任务中心文件域隔离：worktree 任务在隔离区执行）。"""
    '''
    网页版 _invoke_tool：流式向 ws 推送进度，结果回传聊天面板。
    推送类型：
      - codex_start      （任务已启动；前端据此建卡并回补历史）
      - codex_log        （新日志行实时解析成结构化条目：工具/参数/输出/耗时，带全局 seq）
      - codex_progress   （心跳：无新输出时按 interval 播报计数与耗时）
      - codex_error      （签名首次出现即告警，仅提示不终止）
      - codex_done       （exit_code + 摘要/原文尾部）
      - codex_timeout
    '''
    cwd = work_dir or EXECUTOR.cwd
    check_config_reload()
    base = list(tcfg.get('cmd') or [])
    timeout = int(tcfg.get('timeout_sec', 900))
    if not base:
        await _ws_send(ws, {'type': 'codex_error', 'tool': tool_name, 'message': f'工具 {tool_name} 未配置 cmd'})
        return
    label = _get_label(base)
    interval = max(10.0, float(AGENT_CFG.get('tool_progress_interval_sec', 60)))
    # 热重载/重启恢复：注册表里已有该任务 → 交给恢复流程接管（独立进程还在跑，不重复启动）
    _existing = _reg_get(task_id) if task_id else None
    if _existing:
        await invoke_tool_recover(task_id, ws)
        return
    task_id = task_id or ('codex-' + uuid.uuid4().hex[:10])
    log_path = str(LOG_DIR / f'{task_id}.log')
    argv = EXECUTOR._resolve_argv(base)
    if argv is None:
        await _ws_send(ws, {'type': 'codex_error', 'tool': tool_name, 'message': f'未找到命令 {base[0]}：请先安装并加入 PATH'})
        return

    # 任务传递：codex/opencode 走 stdin（规避 cmd /c 的 8191 长度上限与 % 变量展开截断），
    # 其余自定义工具保持命令行参数（超长时报错，不会静默截断任务）。
    # 下发前附加：执行经验参考（复盘库）+ 清理类任务的安全守则（白名单约束）。
    _agent_name = os.path.splitext(os.path.basename(base[0] if base else ''))[0].lower()
    task_payload = task
    if _agent_name in _STDIN_TASK_AGENTS:
        task_payload = (task + _cleanup_safety_appendix(task)
                        + _task_spec_appendix(task)
                        + _continuation_appendix(task)
                        + _long_file_appendix(task))
    try:
        spawn_argv, stdin_text = _build_spawn(argv, base, task_payload)
    except ValueError as e:
        await _ws_send(ws, {'type': 'codex_error', 'tool': tool_name, 'label': label, 'message': f'[{label} 启动失败] {e}'})
        return

    # 起始通知（task 完整下发，不在通知中截断；前端卡片可全文查看）
    await _ws_send(ws, {
        'type': 'codex_start',
        'tool': tool_name,
        'label': label,
        'task_id': task_id,
        'task': task,
        'work_dir': cwd,
        'timeout_sec': timeout,
        'interval_sec': int(interval),
    })

    try:
        proc = _spawn_independent(spawn_argv, cwd, stdin_text, log_path)
        _reg_upsert(task_id, {
            'tool': tool_name,
            'label': label,
            'task': task,
            'work_dir': cwd,
            'cmd': base,
            'pid': proc.pid,
            'log_path': log_path,
            'read_offset': 0,
            'started': time.time(),
            'timeout_sec': timeout,
            'status': 'running',
            'updated': time.time(),
        })
    except Exception as e:
        await _ws_send(ws, {'type': 'codex_error', 'tool': tool_name, 'label': label, 'message': f'[{label} 启动失败] {e}'})
        return

    if stdin_text is not None:
        # 把完整任务写入子进程 stdin 后关闭（子进程读到 EOF 才会开始执行）。
        # 用独立线程写入，避免任务较大时管道缓冲区写满造成死锁。
        def _feed_stdin(proc=proc, data=stdin_text):
            try:
                proc.stdin.write(data.encode('utf-8'))
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=_feed_stdin, daemon=True).start()

    lines: list = []
    lock = threading.Lock()
    stop = threading.Event()
    shared = {'offset': 0}
    threading.Thread(
        target=_tail_file,
        args=(log_path, 0, stop, lines, lock, shared, task_id, proc, None),
        daemon=True,
    ).start()

    start = time.time()
    trace = TraceState()
    _register_live_trace(task_id, trace)

    result = await _monitor_codex_progress(
        ws, task_id, tool_name, label, lines, lock, shared, start, timeout, trace,
        is_alive=lambda: proc.poll() is None,
    )
    elapsed = result['elapsed']
    code = proc.returncode
    with lock:
        raw = '\n'.join(c for c in (_condense(l) for l in lines) if c)
    if not raw:
        raw = '(无输出)'
    if result['timed_out']:
        body = ''
        try:
            body = await asyncio.to_thread(_llm_summarize, raw[-12000:], 500)
        except Exception:
            body = ''
        body = body or raw[-2500:].strip()
        try:
            _reg_patch(task_id, status='timeout', trace_seq=trace.seq,
                       steps_total=trace.tool_count, lines_total=trace.seq,
                       read_offset=shared.get('offset', 0),
                       summary=body[:3000], updated=time.time())
        except Exception:
            pass
        await _ws_send(ws, {
            'type': 'codex_timeout',
            'tool': tool_name,
            'label': label,
            'task_id': task_id,
            'elapsed': elapsed,
            'steps_total': trace.tool_count,
            'lines_total': trace.seq,
            'message': f'⌛ {label} 已停滞 {timeout} 秒无新输出，已终止\n{body[-2500:].strip()}',
            'summary': body[-2500:].strip(),
        })
        return

    body = ''
    try:
        body = await asyncio.to_thread(_llm_summarize, raw, 900)
    except Exception:
        body = ''
    body = body or raw
    # 系统核验验收标准：复杂任务必须输出【验收核验】块，FAIL>0 或缺失即标记未完成
    verify = _verify_acceptance(task, raw, body)
    success = code == 0
    if verify is not None:
        if verify.get('found') and not verify.get('ok'):
            success = False
            body = body + (f"\n⚠ 验收核验未通过：PASS {verify['pass']} / FAIL {verify['fail']}"
                           f"（{verify.get('reason')}）")
        elif not verify.get('found'):
            body = body + "\n⚠ 验收核验：未提供【验收核验】块，无法系统确认达标"
    try:
        _reg_patch(task_id, status='done', exit_code=code,
                   trace_seq=trace.seq, steps_total=trace.tool_count,
                   lines_total=trace.seq,
                   read_offset=shared.get('offset', 0),
                   summary=body[:3000], updated=time.time(),
                   verify_pass=(verify or {}).get('pass'),
                   verify_fail=(verify or {}).get('fail'),
                   verify_ok=(verify or {}).get('ok'))
    except Exception:
        pass
    await _ws_send(ws, {
        'type': 'codex_done',
        'tool': tool_name,
        'label': label,
        'task_id': task_id,
        'exit_code': code,
        'elapsed': elapsed,
        'success': success,
        'steps_total': trace.tool_count,
        'lines_total': trace.seq,
        'summary': body[-3000:] or '（无输出）',
        'raw_tail': raw[-3000:] if body != raw else '',
        'verify': verify,
    })


async def invoke_tool_recover(task_id: str, ws) -> None:
    """热重载/重启后接管仍在运行的 codex/opencode 独立进程：
    - 从 codex_runtime.json 找回 pid / 日志路径 / 已读偏移；
    - 从断点继续追读日志、报错告警、进度推送；
    - 进程结束或超时后收尾，推送 codex_done / codex_timeout。
    """
    entry = _reg_get(task_id)
    if not entry:
        await _ws_send(ws, {'type': 'codex_error', 'tool': '', 'label': 'recover',
                            'message': f'任务 {task_id} 不存在或已清理'})
        return
    tool_name = str(entry.get('tool') or '')
    label = str(entry.get('label') or tool_name or 'tool')
    log_path = str(entry.get('log_path') or '')
    timeout = int(entry.get('timeout_sec') or 900)
    interval = max(10.0, float(AGENT_CFG.get('tool_progress_interval_sec', 60)))
    pid = int(entry.get('pid') or 0)
    start = float(entry.get('started') or time.time())
    offset = int(entry.get('read_offset') or 0)
    status = str(entry.get('status') or 'running')

    # 已终止/超时的任务：补发最终事件（重启前的收尾可能没来得及推送）
    if status in ('killed', 'timeout'):
        summary = str(entry.get('summary') or '')[:2500] or '（无输出）'
        if status == 'killed':
            await _ws_send(ws, {
                'type': 'codex_terminated',
                'tool': tool_name,
                'label': label,
                'message': f'⛔ {label} 已被终止\n{summary}',
            })
        else:
            await _ws_send(ws, {
                'type': 'codex_timeout',
                'tool': tool_name,
                'label': label,
                'elapsed': int(time.time() - start),
                'message': f'⌛ {label} 已停滞无新输出，已终止\n{summary}',
                'summary': summary,
            })
        return

    await _ws_send(ws, {
        'type': 'codex_start',
        'tool': tool_name,
        'label': label,
        'task_id': task_id,
        'task': entry.get('task') or '',
        'work_dir': entry.get('work_dir') or EXECUTOR.cwd,
        'timeout_sec': timeout,
        'interval_sec': int(interval),
        'recovered': True,
        'pid': pid,
    })

    lines: list = []
    lock = threading.Lock()
    stop = threading.Event()
    shared = {'offset': offset}
    trace = TraceState()
    # 重启恢复：先预解析文件里已有内容（对齐 seq/工具栈），避免把历史重复播报
    if log_path and os.path.exists(log_path):
        trace.seed_from_file(log_path, upto_bytes=offset)
        threading.Thread(
            target=_tail_file,
            args=(log_path, offset, stop, lines, lock, shared, task_id, None, pid),
            daemon=True,
        ).start()
    _register_live_trace(task_id, trace)

    result = await _monitor_codex_progress(
        ws, task_id, tool_name, label, lines, lock, shared, start, timeout, trace,
        is_alive=lambda: bool(pid) and _pid_alive(pid),
    )
    elapsed = result['elapsed']
    code = _process_exit_code(pid) if pid else None
    with lock:
        raw = '\n'.join(c for c in (_condense(l) for l in lines) if c)
    if not raw:
        raw = '(无输出)'
    if result['timed_out']:
        body = ''
        try:
            body = await asyncio.to_thread(_llm_summarize, raw[-12000:], 500)
        except Exception:
            body = ''
        body = body or raw[-2500:].strip()
        try:
            _reg_patch(task_id, status='timeout', trace_seq=trace.seq,
                       steps_total=trace.tool_count, lines_total=trace.seq,
                       read_offset=shared.get('offset', 0),
                       summary=body[:3000], updated=time.time())
        except Exception:
            pass
        await _ws_send(ws, {
            'type': 'codex_timeout',
            'tool': tool_name,
            'label': label,
            'task_id': task_id,
            'elapsed': elapsed,
            'steps_total': trace.tool_count,
            'lines_total': trace.seq,
            'message': f'⌛ {label} 已停滞 {timeout} 秒无新输出，已终止\n{body[-2500:].strip()}',
            'summary': body[-2500:].strip(),
        })
        return

    body = ''
    try:
        body = await asyncio.to_thread(_llm_summarize, raw, 900)
    except Exception:
        body = ''
    body = body or raw
    # 系统核验验收标准（恢复接管路径同样执行）
    verify = _verify_acceptance(entry.get('task') or '', raw, body)
    success = (code if code is not None else 0) == 0
    if verify is not None:
        if verify.get('found') and not verify.get('ok'):
            success = False
            body = body + (f"\n⚠ 验收核验未通过：PASS {verify['pass']} / FAIL {verify['fail']}"
                           f"（{verify.get('reason')}）")
        elif not verify.get('found'):
            body = body + "\n⚠ 验收核验：未提供【验收核验】块，无法系统确认达标"
    try:
        _reg_patch(task_id, status='done', exit_code=code,
                   trace_seq=trace.seq, steps_total=trace.tool_count,
                   lines_total=trace.seq,
                   read_offset=shared.get('offset', 0),
                   summary=body[:3000], updated=time.time(),
                   verify_pass=(verify or {}).get('pass'),
                   verify_fail=(verify or {}).get('fail'),
                   verify_ok=(verify or {}).get('ok'))
    except Exception:
        pass
    await _ws_send(ws, {
        'type': 'codex_done',
        'tool': tool_name,
        'label': label,
        'task_id': task_id,
        'exit_code': code,
        'elapsed': elapsed,
        'success': success,
        'steps_total': trace.tool_count,
        'lines_total': trace.seq,
        'summary': body[-3000:] or '（无输出）',
        'raw_tail': raw[-3000:] if body != raw else '',
        'verify': verify,
    })
