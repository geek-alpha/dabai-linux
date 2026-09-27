"""ASR 噪声过滤判据自测：验证 _voiced_ms_in_wav / _asr_result_is_noise 的正反两面。

反例（必须挡）：静音、短突发噪声、单个 ASCII 字符、已知幻觉短语。
正例（必须放行）：真人说话音频、正常长度的中文句子、真人短应答。
"""
import array
import io
import os
import random
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

SR = 16000
TMP = tempfile.gettempdir()
fails = []


def write_wav(name, samples, sr=SR):
    path = os.path.join(TMP, "_vadcheck_" + name + ".wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(array.array("h", [
            int(max(-1.0, min(1.0, s)) * 32767) for s in samples]).tobytes())
    return path


def silence(sec):
    return [0.0] * int(SR * sec)


def noise(sec, dbfs, seed=7):
    random.seed(seed)
    amp = 10 ** (dbfs / 20.0)
    return [random.uniform(-amp, amp) for _ in range(int(SR * sec))]


def burst(sec, dbfs, burst_ms=60, seed=11):
    """短突发噪声（键盘/桌椅响）：整段静音里嵌一个几十毫秒的尖峰。"""
    random.seed(seed)
    amp = 10 ** (dbfs / 20.0)
    out = [0.0] * int(SR * sec)
    n = int(SR * burst_ms / 1000)
    start = max(0, len(out) // 2)
    for i in range(start, min(len(out), start + n)):
        out[i] = random.uniform(-amp, amp)
    return out


def speechlike(sec, dbfs=-20.0, f0=150.0):
    """近似人声：基频谐波 + 音节包络（每 300ms 一个音节，含停顿）。"""
    import math
    amp = 10 ** (dbfs / 20.0)
    out = []
    for i in range(int(SR * sec)):
        t = i / SR
        env = 0.5 + 0.5 * math.sin(2 * math.pi * (t / 0.3) - math.pi / 2)
        s = (math.sin(2 * math.pi * f0 * t) + 0.5 * math.sin(2 * math.pi * 2 * f0 * t)
             + 0.3 * math.sin(2 * math.pi * 3 * f0 * t))
        out.append(amp * env * s / 1.8)
    return out


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append(label)
    print("%-46s %-14s %s" % (label, "期望 " + str(want), "→ " + str(got) + ("" if ok else "  ✗")))


print("== 有声时长（_voiced_ms_in_wav）==")
p = write_wav("silence", silence(1.2))
check("纯静音 1.2s → 0ms", server._voiced_ms_in_wav(p), 0.0)

p = write_wav("burst", burst(1.2, -25.0, 60))
v = server._voiced_ms_in_wav(p)
check("1.2s 静音 + 60ms 突发噪声 → <250ms", v < 250.0, True)

p = write_wav("speech", speechlike(1.5))
v = server._voiced_ms_in_wav(p)
check("1.5s 类人声 → >250ms", v > 250.0, True)

# 低电平扫描：麦克风增益差异极大（内置麦 / 远场 / 手机搁桌上），阈值必须在轻声下
# 仍放行。只测 -20dBFS 会整类漏掉「它听不见我」的误杀 —— 上一版就栽在这。
for db in (-30.0, -40.0, -45.0, -50.0, -55.0):
    p = write_wav("low%d" % int(-db), speechlike(1.5, dbfs=db))
    v = server._voiced_ms_in_wav(p)
    check("%d dBFS 类人声 1.5s → >250ms" % int(db), v > 250.0, True)

mp3 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "_silero_speech.mp3")
if os.path.exists(mp3):
    real = os.path.join(TMP, "_vadcheck_real.wav")
    rc = subprocess.run(["ffmpeg", "-y", "-i", mp3, "-ar", "16000", "-ac", "1",
                         "-sample_fmt", "s16", real],
                        capture_output=True).returncode
    if rc == 0:
        v = server._voiced_ms_in_wav(real)
        check("真人语音样本(_silero_speech.mp3) → >250ms", v > 250.0, True)
    else:
        print("（真人语音样本 ffmpeg 转码失败，跳过）")
else:
    print("（无 web/_silero_speech.mp3，跳过真人语音样本）")

print()
print("== 结果判定（_asr_result_is_noise）==")
check("单个字母 'N'（用户报的那条）", server._asr_result_is_noise("N", -1.0), True)
check("'N。'（带标点）", server._asr_result_is_noise("N。", -1.0), True)
check("'¿Qué?'（实测噪声幻觉）", server._asr_result_is_noise("¿Qué?", 1200.0), True)
check("'谢谢观看'（字幕幻觉）", server._asr_result_is_noise("谢谢观看", 800.0), True)
check("空文本", server._asr_result_is_noise("", -1.0), True)
check("'嗯。' 但只有 120ms 有声（噪声触发）", server._asr_result_is_noise("嗯。", 120.0), True)
check("'嗯。' 有声 300ms（极短档新门槛下必须挡）", server._asr_result_is_noise("嗯。", 300.0), True)
check("'好' 有声 300ms（单字，同上）", server._asr_result_is_noise("好", 300.0), True)
check("'嗯。' 有声 400ms（真人短应答，必须放行）", server._asr_result_is_noise("嗯。", 400.0), False)
check("'好的' 有声 500ms", server._asr_result_is_noise("好的", 500.0), False)
check("'今天天气怎么样' 有声 1200ms", server._asr_result_is_noise("今天天气怎么样", 1200.0), False)
check("格式未知（voiced=-1）时正常长句放行", server._asr_result_is_noise("帮我查一下天气", -1.0), False)

print()
print("失败 %d 项" % len(fails))
sys.exit(1 if fails else 0)
