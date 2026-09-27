"""ASR 幻觉探针：静音 / 低幅噪声送 STT，看云端返回什么。

VAD 误触发时录音里没有真实人声，若 ASR 仍返回文本（如单个字母），
服务端会把噪声当用户输入 —— 这就是「什么都没说它突然冒一个字」的链路。
"""
import io
import os
import random
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

SR = 16000


def wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            int(max(-1.0, min(1.0, s)) * 32767).to_bytes(2, "little", signed=True)
            for s in samples))
    return buf.getvalue()


def silence(sec):
    return [0.0] * int(SR * sec)


def noise(sec, dbfs, seed=7):
    random.seed(seed)
    amp = 10 ** (dbfs / 20.0)
    return [random.uniform(-amp, amp) for _ in range(int(SR * sec))]


def tone(sec, freq=300.0, dbfs=-30.0):
    amp = 10 ** (dbfs / 20.0)
    return [amp * (0.6 * __import__("math").sin(2 * 3.14159265 * freq * i / SR)
                   + 0.4 * __import__("math").sin(2 * 3.14159265 * 2 * freq * i / SR))
            for i in range(int(SR * sec))]


CASES = [
    ("silence_1.2s", silence(1.2)),
    ("silence_0.4s", silence(0.4)),
    ("noise_-50dB_1.2s", noise(1.2, -50)),
    ("noise_-40dB_1.2s", noise(1.2, -40)),
    ("noise_-40dB_0.4s", noise(0.4, -40)),
    ("hum_50Hz_-35dB_1.2s", tone(1.2, 50.0, -35)),
]

for name, samples in CASES:
    path = os.path.join(tempfile.gettempdir(), "_vadprobe_" + name + ".wav")
    with open(path, "wb") as f:
        f.write(wav_bytes(samples))
    t0 = time.time()
    try:
        text = server.speech_to_text(path)
    except Exception as e:
        text = "<异常: %s>" % e
    print("%-22s 耗时%.2fs → %r" % (name, time.time() - t0, text), flush=True)
