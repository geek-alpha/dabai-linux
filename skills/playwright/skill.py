# -*- coding: utf-8 -*-
"""playwright 技能 —— 无头 Chromium 干活：截图 / 抓文本 / 跑任意脚本。

每次调用起一个 node 进程、跑完即退，不留常驻 daemon：这台机器的内存经不起
常驻浏览器，而且常驻进程会在会话结束后变成孤儿。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

_NODE_CANDIDATES = ("/usr/local/bin/node", "/usr/bin/node")
_DEFAULT_VIEWPORT = "1280x800"
_MAX_OUT = 8000


def _node() -> str:
    for p in _NODE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("node") or "node"


def _node_modules_root() -> str:
    """全局 node_modules —— playwright 装在这里，node 默认不解析它。"""
    npm = shutil.which("npm") or "/usr/local/bin/npm"
    try:
        r = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    for c in ("/usr/local/lib/node_modules", "/usr/lib/node_modules"):
        if os.path.isdir(c):
            return c
    return ""


def _env() -> dict:
    env = dict(os.environ)
    root = _node_modules_root()
    if root:
        env["NODE_PATH"] = root + (":" + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    return env


def _viewport(v) -> tuple[int, int]:
    s = str(v or _DEFAULT_VIEWPORT).lower().replace(" ", "")
    for sep in ("x", "*", ","):
        if sep in s:
            a, _, b = s.partition(sep)
            try:
                return max(200, min(4000, int(a))), max(200, min(4000, int(b)))
            except ValueError:
                break
    return 1280, 800


def _run_js(body: str, timeout: int = 90) -> tuple[int, str]:
    """把 JS 主体写临时文件交给 node 跑。临时文件而非 -e：躲开 shell 引号地狱。"""
    fd, path = tempfile.mkstemp(suffix=".cjs", prefix="pw-skill-")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        proc = subprocess.Popen(
            [_node(), path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(), cwd=tempfile.gettempdir(), start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=max(5, min(600, int(timeout))))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # 连 chromium 子进程一起收
            except Exception:
                proc.kill()
            proc.communicate()
            return 124, f"超时（{timeout}s）：脚本没跑完，已连同浏览器一起杀掉。"
        text = (out or "") + (("\n[stderr] " + err.strip()) if err.strip() else "")
        return proc.returncode, text.strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _truncate(text: str, limit: int = _MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字符）"


_PRELUDE = """const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: %(headless)s });
  const context = await browser.newContext({ viewport: { width: %(w)d, height: %(h)d } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  try {
%(body)s
  } finally {
    await browser.close();
  }
})().catch(e => { console.error('ERR ' + (e && e.message ? e.message : e)); process.exit(1); });
"""


def _wrap(body: str, headless: bool = True, w: int = 1280, h: int = 800) -> str:
    indented = "\n".join("    " + ln for ln in body.splitlines())
    return _PRELUDE % {"headless": "true" if headless else "false", "w": w, "h": h, "body": indented}


def _png_size(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return ""
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return f"{w}x{h}"
    except Exception:
        return ""


def do_shot(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "缺 url。"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    out = str(args.get("out_path") or "").strip() or f"/tmp/pw-{time.strftime('%Y%m%d-%H%M%S')}.png"
    full = args.get("full_page")
    full = True if full is None else bool(full)
    sel = str(args.get("selector") or "").strip()
    wait_ms = max(0, min(60000, int(args.get("wait_ms") or 0)))
    timeout = max(5, min(300, int(args.get("timeout") or 45)))
    w, h = _viewport(args.get("viewport"))
    body = f"""await page.goto({json.dumps(url)}, {{ waitUntil: 'domcontentloaded', timeout: {timeout * 1000} }});
if ({wait_ms}) await page.waitForTimeout({wait_ms});
const target = {f"page.locator({json.dumps(sel)}).first()" if sel else "page"};
await target.screenshot({{ path: {json.dumps(out)}, fullPage: {('true' if full else 'false')} }});
console.log(JSON.stringify({{ title: await page.title(), url: page.url() }}));"""
    code, text = _run_js(_wrap(body, True, w, h), timeout=timeout + 45)
    if code != 0:
        return f"截图失败（exit={code}）：{_truncate(text, 2000)}"
    if not os.path.exists(out):
        return f"命令跑完但没生成文件：{_truncate(text, 1000)}"
    size = os.path.getsize(out)
    dim = _png_size(out)
    meta = {}
    try:
        meta = json.loads(text.splitlines()[-1])
    except Exception:
        pass
    parts = [f"已截图：{out}", f"{size / 1024:.0f} KB" + (f"，{dim} px" if dim else "")]
    if meta.get("title"):
        parts.append(f"标题：{meta['title']}")
    return " | ".join(parts)


def do_text(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "缺 url。"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    sel = str(args.get("selector") or "").strip()
    wait_ms = max(0, min(60000, int(args.get("wait_ms") or 0)))
    limit = max(200, min(30000, int(args.get("max_chars") or 6000)))
    timeout = max(5, min(300, int(args.get("timeout") or 45)))
    body = f"""await page.goto({json.dumps(url)}, {{ waitUntil: 'domcontentloaded', timeout: {timeout * 1000} }});
if ({wait_ms}) await page.waitForTimeout({wait_ms});
const txt = {f"await page.locator({json.dumps(sel)}).first().innerText()" if sel else "await page.evaluate(() => (document.body ? document.body.innerText : ''))"};
console.log('###META ' + JSON.stringify({{ title: await page.title(), url: page.url(), len: txt.length }}));
console.log(txt.slice(0, {limit}));"""
    code, text = _run_js(_wrap(body), timeout=timeout + 45)
    if code != 0:
        return f"抓取失败（exit={code}）：{_truncate(text, 2000)}"
    lines = text.splitlines()
    meta = {}
    if lines and lines[0].startswith("###META "):
        try:
            meta = json.loads(lines[0][8:])
        except Exception:
            meta = {}
        lines = lines[1:]
    head = f"{meta.get('title') or '(无标题)'} — {meta.get('url') or url}"
    if meta.get("len"):
        head += f"（正文 {meta['len']} 字符）"
    return head + "\n" + _truncate("\n".join(lines).strip())


def do_run(args: dict) -> str:
    script = str(args.get("script") or "").strip()
    if not script:
        return "缺 script。"
    timeout = max(10, min(600, int(args.get("timeout") or 90)))
    headless = args.get("headless")
    headless = True if headless is None else bool(headless)
    code, text = _run_js(_wrap(script, headless), timeout=timeout)
    if code == 124:
        return text
    head = "脚本执行成功" if code == 0 else f"脚本失败（exit={code}）"
    return f"{head}\n{_truncate(text) if text else '(无输出——记得用 console.log 打结果)'}"


def do_doctor(args: dict) -> str:
    node = _node()
    lines = []
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        lines.append(f"node {v}（{node}）")
    except Exception as e:
        lines.append(f"node 不可用：{e}")
    root = _node_modules_root()
    lines.append(f"全局 node_modules：{root or '找不到'}")
    pw = os.path.join(root, "playwright", "package.json") if root else ""
    if pw and os.path.exists(pw):
        try:
            with open(pw, encoding="utf-8") as f:
                lines.append(f"playwright {json.load(f).get('version')}")
        except Exception:
            lines.append("playwright 已装（版本读不出）")
    else:
        lines.append("playwright 没装：npm i -g playwright")
    code, text = _run_js(
        _wrap(
            "console.log(require('playwright').chromium.executablePath());\n"
            "await page.setContent('<h1>ok</h1>');\n"
            "console.log('launch ok / h1=' + await page.locator('h1').innerText());"
        ),
        timeout=120,
    )
    lines.append(("内核：" + text.splitlines()[0]) if code == 0 and text else f"内核自检失败（exit={code}）：{_truncate(text, 600)}")
    lines.append(f"默认视口：{_DEFAULT_VIEWPORT}")
    return "\n".join(lines)


HANDLERS = {
    "pw_shot": do_shot,
    "pw_text": do_text,
    "pw_run": do_run,
    "pw_doctor": do_doctor,
}

PROMPT = (
    "【技能 浏览器自动化】无头 Chromium（Playwright）：pw_shot 截图、pw_text 抓文本、"
    "pw_run 跑任意 Playwright 脚本（预置 page/browser/context）、pw_doctor 自检。"
    "每次调用起一个浏览器、结束即退，不留常驻进程。无显示的 WSL 里只能无头。"
)
