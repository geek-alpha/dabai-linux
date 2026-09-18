#!/usr/bin/env python3
"""在 Android WebView 里跑 CPU 采样分析，按自耗时排序打印热点函数。

    PID=$(adb shell pidof com.dabai.phoenix | tr -d '\r')
    adb forward tcp:9222 localabstract:webview_devtools_remote_$PID
    venv/bin/python tools/cdp_profile.py --seconds 8

页面卡顿时先用它定位「谁在吃主线程」，别靠猜。
"""
import argparse
import collections
import json
import sys
import time
import urllib.request

from websockets.sync.client import connect


def list_targets(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=5) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--interval", type=int, default=200, help="采样间隔(微秒)")
    a = ap.parse_args()

    pages = [t for t in list_targets(a.port) if t.get("type") == "page"]
    if not pages:
        print("没有可调试页面", file=sys.stderr)
        return 2

    seq = [0]

    with connect(pages[0]["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
        def call(method, params=None):
            seq[0] += 1
            mid = seq[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=180))
                if m.get("id") == mid:
                    if "error" in m:
                        raise RuntimeError(m["error"])
                    return m.get("result", {})

        call("Profiler.enable")
        call("Profiler.setSamplingInterval", {"interval": a.interval})
        call("Profiler.start")
        time.sleep(a.seconds)
        prof = call("Profiler.stop")["profile"]

    nodes = {n["id"]: n for n in prof.get("nodes", [])}
    samples = prof.get("samples", [])
    deltas = prof.get("timeDeltas", [])
    self_us = collections.Counter()
    for i, sid in enumerate(samples):
        self_us[sid] += deltas[i] if i < len(deltas) else 0

    total_us = sum(self_us.values()) or 1
    print("采样 %.1fs · 主线程总耗时 %.0fms · 样本 %d" % (a.seconds, total_us / 1000.0, len(samples)))
    print("%10s %6s  %s" % ("自耗时", "占比", "函数 @ 位置"))
    for nid, us in self_us.most_common(a.top):
        cf = nodes.get(nid, {}).get("callFrame", {})
        url = cf.get("url", "") or "(native)"
        for pfx in ("https://battlephoenix.tech", "https://www.battlephoenix.tech"):
            if url.startswith(pfx):
                url = url[len(pfx):]
        name = cf.get("functionName") or "(anonymous)"
        print("%8.1fms %5.1f%%  %s  %s:%d" % (
            us / 1000.0, us * 100.0 / total_us, name, url, cf.get("lineNumber", 0) + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
