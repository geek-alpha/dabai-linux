"""STT 链路探针：ffmpeg 修复后的三段验证（真转码 / 缺 ffmpeg 直通 / 缺 ffmpeg 拒绝）。"""
import os
import sys
import time
import hashlib

sys.path.insert(0, "/home/wxf/dabai")
os.chdir("/home/wxf/dabai")
import server  # noqa: E402

SRC = "/home/wxf/dabai/web/_silero_speech.mp3"


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()[:12]


print("== 1) ffmpeg 存在：正常转码 ==")
out1 = "/tmp/_stt_t1.wav"
t0 = time.time()
ok1 = server.convert_to_wav(SRC, out1, False)
print(f"ok={ok1} 耗时={time.time()-t0:.2f}s size={os.path.getsize(out1) if os.path.exists(out1) else 0}")

print("== 2) 无 ffmpeg + 已是 16k 单声道 WAV：应直通 ==")
real_path = os.environ["PATH"]
os.environ["PATH"] = "/tmp/_empty_path"
out2 = "/tmp/_stt_t2.wav"
ok2 = server.convert_to_wav(out1, out2, False)
same = os.path.exists(out2) and md5(out1) == md5(out2)
print(f"ok={ok2} 与源文件一致={same}")

print("== 3) 无 ffmpeg + mp3：应明确拒绝 ==")
out3 = "/tmp/_stt_t3.wav"
ok3 = server.convert_to_wav(SRC, out3, False)
print(f"ok={ok3}（期望 False）")
os.environ["PATH"] = real_path

print("== 4) 端到端：转码后的 wav 送 STT ==")
t0 = time.time()
text = server.speech_to_text(out1)
print(f"耗时={time.time()-t0:.2f}s text={text!r}")
