"""读图能力判定 —— 某个模型能不能看见注入的截图。

图片不再经读图工具中转，而是作为上下文直接进请求体（agent.py 的 [[IMG:]] 通道）。
这里只回答一个问题：这个模型看不看得见图。看不见时注入等于白传，还可能撞提供方 400。

优先级：角色卡显式 > 供应商显式 > 实测记录 > 模型名启发式 > 默认否。
显式支持敢排在「实测被拒」前面是有意的：名字启发式是猜、被拒是硬证据，但用户在配置
界面手动勾了「支持读图」= 他愿意付这次调用失败的成本，让他试；失败会如实带出来。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parents[1]
_STATE_PATH = BASE_DIR / "data" / "vision_state.json"

# 模型名里的视觉线索。宁可漏判（退化成"看不见图"）也不要误判成支持——
# 误判的代价是一次必然失败的 API 调用。
_VISION_HINTS = re.compile(
    r"(?:^|[-_/])("
    r"vision|visual|vl\d*|omni|multimodal|"
    r"gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|chatgpt-4o|"
    r"claude-[3-9]|claude-(?:opus|sonnet|haiku)|"
    r"gemini|gemma-?3|"
    r"glm-4v|glm-4\.5v|glm-5v|"
    r"qwen[\w.\-]*vl|qwen-vl|internvl|llava|pixtral|molmo|minicpm-v|"
    r"step-1v|yi-vision|phi-3-vision|phi-4-multimodal|ovis|deepseek-vl"
    r")(?:$|[-_./])",
    re.I,
)
# 名字里带这些的即便命中线索也不是视觉模型（反例白名单）
_VISION_DENY = re.compile(r"(?:embedding|rerank|tts|whisper|audio|image-gen|dall)", re.I)


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _tri(v):
    """三态归一：True/False/None（手写 settings.json 里可能是 "true"/1/off）。"""
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


def _name_says_vision(model: str) -> bool:
    m = str(model or "").strip()
    if not m or _VISION_DENY.search(m):
        return False
    return bool(_VISION_HINTS.search(m))


def _state() -> dict:
    st = _load_json(_STATE_PATH, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("rejected", {})   # 被提供方明确拒绝过 image_url 的模型
    st.setdefault("ok", {})         # 实测成功读过图的模型
    return st


def supports_vision(model: str, provider: Optional[dict] = None,
                    override=None) -> tuple[bool, str]:
    """判断模型能否读图，返回 (结论, 依据)。

    override 是角色卡级的声明，只作用于该卡正在用的那个模型。
    """
    m = str(model or "").strip()
    if not m:
        return False, "模型名为空"
    st = _state()
    ov = _tri(override)
    if ov is False:
        return False, "角色卡里显式标了「不支持读图」"
    if ov is True:
        if m in st["rejected"]:
            return True, ("角色卡里显式标了「支持读图」"
                          f"（注意：曾被拒过 {st['rejected'][m].get('reason', '')[:40]}）")
        return True, "角色卡里显式标了「支持读图」"
    v = _tri((provider or {}).get("vision"))
    if v is False:
        return False, "供应商配置里显式标了「不支持读图」"
    if v is True:
        if m in st["rejected"]:
            return True, ("供应商配置里显式标了「支持读图」"
                          f"（注意：曾被拒过 {st['rejected'][m].get('reason', '')[:40]}）")
        return True, "供应商配置里显式标了「支持读图」"
    if m in st["rejected"]:
        return False, f"曾被提供方拒绝：{st['rejected'][m].get('reason', '')[:60]}"
    if m in st["ok"]:
        return True, "实测成功过"
    if _name_says_vision(m):
        return True, "模型名含视觉线索（未经实测）"
    return False, "名字无视觉线索且无实测记录"


def _providers(cfg: dict) -> list:
    return [p for p in (cfg.get("llm_providers") or []) if isinstance(p, dict)]


def _active(cfg: dict) -> Optional[dict]:
    pid = str(cfg.get("llm_provider_id") or "").strip()
    provs = _providers(cfg)
    if pid:
        for p in provs:
            if p.get("id") == pid:
                return p
    kind = str(cfg.get("llm_provider") or "").strip()
    if kind:
        for p in provs:
            if p.get("kind") == kind:
                return p
    return provs[0] if provs else None


def active_model(cfg: dict) -> str:
    """当前实际在跑的模型名。

    应用角色卡会把卡片的 model 写进 settings.json 的 model，所以 cfg["model"]
    才是真在跑的那个；供应商的 default_model 只是没指定模型时的兜底。两者可以
    不同，判定必须用前者，否则会拿另一个模型的结论决定当前模型收不收图。
    """
    m = str(cfg.get("model") or "").strip()
    if m:
        return m
    p = _active(cfg) or {}
    return str(p.get("default_model") or "").strip()


def current_can_see(cfg: Optional[dict] = None) -> tuple[bool, str]:
    """当前在跑的模型能不能看见注入的图。"""
    if cfg is None:
        cfg = _load_json(BASE_DIR / "settings.json", {}) or {}
    return supports_vision(active_model(cfg), _active(cfg), cfg.get("llm_vision"))
