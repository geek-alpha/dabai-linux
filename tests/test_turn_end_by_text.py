# -*- coding: utf-8 -*-
"""「轮次结束 = 文本输出完成」与「自我迭代强制唤醒」的接线守卫。

两件事都只靠源码文本可验证，跑不出运行时行为，所以用接线守卫钉住：

1. 一轮对话的结束点必须是文本输出完成。audio_end 要等 TTS 队列把所有分片发完
   才补发（server.py 的 _tts_finalize），把轮收尾挂在它上面，TTS 一慢一挂整轮
   就悬着：工具期保护不解除、工具链不收尾、游戏回合不结算。
2. 自我迭代循环必须有外部唤醒：重启会杀掉正在执行的轮（dispatched 已扣预算、
   record 没落），只靠 40 分钟的排期等于每重启一次白丢一轮。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRV = (ROOT / "server.py").read_text(encoding="utf-8")
WS = (ROOT / "web/js/network/09_websocket.ts").read_text(encoding="utf-8")
TTS = (ROOT / "web/js/core/10_tts_lipsync.ts").read_text(encoding="utf-8")
PROTO = (ROOT / "web/js/types/ws-protocol.ts").read_text(encoding="utf-8")


# ---------- 轮次结束判据 ----------

def test_文本完成事件先于音频收尾():
    """顺序即语义：先发「文本完成」，audio_end 交给后台等音频发完。"""
    i = SRV.index('"type": "turn_text_done"')
    j = SRV.index("asyncio.create_task(_tts_finalize())", i)
    assert i < j


def test_前端按文本完成收尾():
    i = WS.index("case 'turn_text_done':")
    seg = WS[i:WS.index("case 'audio_end':", i)]
    assert "App._turnInTools = false" in seg          # 工具期保护解除
    assert "toolChainEndTurn" in seg                  # 工具链收尾 + 回合结算


def test_音频路径不再重复收尾():
    """toolChainEndTurn 会结算游戏回合，重复调用会重复加分。"""
    i = TTS.index("App.handleAudioEnd = function")
    seg = TTS[i:TTS.index("App.handleInterrupted", i)]
    assert "toolChainEndTurn" not in seg


def test_audio_end仍有兜底且判标志():
    """文本完成事件没到时（老客户端/异常路径）audio_end 必须还能收尾。"""
    i = WS.index("case 'audio_end':")
    seg = WS[i:WS.index("case 'usage':", i)]
    assert "_turnTextDone" in seg


def test_新轮复位文本完成标志():
    i = WS.index("case 'thinking':")
    seg = WS[i:WS.index("case 'listening'", i)]
    assert "App._turnTextDone = false" in seg


def test_协议声明了文本完成事件():
    assert "turn_text_done" in PROTO


# ---------- 自我迭代强制唤醒 ----------

def test_看门狗已接线():
    assert "asyncio.ensure_future(_self_iterate_watchdog())" in SRV


def test_看门狗消费停摆判据并立即派发():
    i = SRV.index("async def _self_iterate_watchdog()")
    seg = SRV[i:SRV.index('@app.get("/api/self-iterate/status")', i)]
    assert "stall_plan(" in seg
    for act in ("resume", "release", "wake"):
        assert f'"{act}"' in seg
    # 只清标记不派发 = 仍然等下一个 40 分钟，等于没修
    assert "run_now(" in seg
    # 未知动作不许静默走到「立即派发」：那是真花钱的动作，必须有白名单
    assert "看门狗动作不认识" in seg


def test_看门狗扫描间隔远短于排期间隔():
    m = re.search(r"SELF_ITERATE_WATCHDOG_SEC = (\d+)", SRV)
    assert m, "找不到看门狗扫描间隔常量"
    assert int(m.group(1)) < 300 < 2400


def test_看门狗留痕():
    """「机制在跑」和「机制真救过人」是两件事，动作必须落盘可查。"""
    i = SRV.index("async def _self_iterate_watchdog()")
    seg = SRV[i:SRV.index('@app.get("/api/self-iterate/status")', i)]
    assert "note_wake(" in seg


def test_批判缺失不再静默():
    """轮末被重启掐掉批判时，下一轮必须被要求补上，而不是无指引自由发挥。"""
    si = (ROOT / "tools" / "self_iterate.py").read_text(encoding="utf-8")
    i = si.index("def user_input_block()")
    seg = si[i:si.index("def brief()", i)]
    assert "补跑第 5 步" in seg
