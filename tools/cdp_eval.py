#!/usr/bin/env python3
"""通过 Chrome DevTools Protocol 在 Android WebView 里执行 JS。

App 是 debug 包，WebView 调试默认开启。先建通道再跑：

    PID=$(adb shell pidof com.dabai.phoenix | tr -d '\r')
    adb forward tcp:9222 localabstract:webview_devtools_remote_$PID
    venv/bin/python tools/cdp_eval.py -f /tmp/probe.js
    venv/bin/python tools/cdp_eval.py 'document.title'

页面里没有 window.App 之外的调试口子时，这个脚本是唯一能读到
WebGL renderer / 实际帧率 / 层数量的通道。
"""
import argparse
import json
import sys
import urllib.request

from websockets.sync.client import connect


def list_targets(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=5) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("expr", nargs="?", help="要执行的 JS 表达式")
    ap.add_argument("-f", "--file", help="从文件读 JS")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--raw", action="store_true", help="原样打印字符串结果")
    a = ap.parse_args()

    js = open(a.file, encoding="utf-8").read() if a.file else a.expr
    if not js:
        ap.error("需要 expr 或 -f")

    pages = [t for t in list_targets(a.port) if t.get("type") == "page"]
    if not pages:
        print("没有可调试页面：确认 App 在前台、adb forward 指向正确的 PID", file=sys.stderr)
        return 2

    with connect(pages[0]["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            msg = json.loads(ws.recv(timeout=a.timeout))
            if msg.get("id") != 1:
                continue
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                print(json.dumps(res["exceptionDetails"], ensure_ascii=False, indent=2))
                return 1
            val = res.get("result", {}).get("value")
            if isinstance(val, str) and a.raw:
                print(val)
            elif isinstance(val, (dict, list)):
                print(json.dumps(val, ensure_ascii=False, indent=2))
            else:
                print(val)
            return 0


if __name__ == "__main__":
    sys.exit(main())
