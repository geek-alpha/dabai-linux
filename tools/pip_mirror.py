#!/usr/bin/env python3
"""pip 源自动选择 —— 国内镜像并发探测，选中即用，装失败自动换下一个。

为什么需要这个文件：pip 默认走 pypi.org，国内直连常常几 KB/s 甚至超时；
但写死某一个国内镜像同样不行——清华 pypi 对云厂商 IP 段直接返回 403
（实测阿里云 ECS 39.106.53.2 访问 /simple/pip/ 得 403，家宽却正常），
所以「哪个源能用」只能当场探测，不能靠猜。

用法：
    python3 tools/pip_mirror.py -r requirements-core.txt   # 选源 + 安装，失败换源
    python3 tools/pip_mirror.py --upgrade pip         # 参数原样透传给 pip install
    python3 tools/pip_mirror.py --probe               # 只探测，打印各源状态与耗时
    python3 tools/pip_mirror.py --print-index         # 只打印选中的源 URL（供 shell 捕获）

环境变量：
    DABAI_PIP_INDEX     显式指定源，跳过探测（也读 PIP_INDEX_URL）
    DABAI_PIP_TIMEOUT   单次 pip 请求超时秒数（默认 30）
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

# 探测顺序即优先级；pypi.org 放最后兜底，它通了就说明用户网络本来没问题。
# 阿里内网源 mirrors.cloud.aliyuncs.com 故意不收：只在阿里云 ECS 内网可达，
# 换个机房就是死链，当默认值会把非阿里云用户全坑一遍。
MIRRORS: list[tuple[str, str]] = [
    ("aliyun", "https://mirrors.aliyun.com/pypi/simple"),
    ("ustc", "https://mirrors.ustc.edu.cn/pypi/simple"),
    ("tencent", "https://mirrors.cloud.tencent.com/pypi/simple"),
    ("huawei", "https://repo.huaweicloud.com/repository/pypi/simple"),
    ("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("pypi", "https://pypi.org/simple"),
]

PROBE_TIMEOUT = 6.0
# 探测用哪个包：挑十年老包，任何镜像都不会漏同步，页面也小。
PROBE_PKG = "six"
# 换源重试上限：探测已经筛过一轮，再全量重试只是把等待时间翻倍。
MAX_ATTEMPTS = 3


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def configured_index() -> str:
    """用户已配置的源：环境变量 > pip.conf。它排第一，但照样要探测。"""
    v = _env("DABAI_PIP_INDEX", "PIP_INDEX_URL")
    if v:
        return v
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "config", "get", "global.index-url"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _probe(name: str, url: str) -> tuple[str, str, bool, float, str]:
    """真实下载探测：只看 index 页不够。

    实测清华 pypi 对云厂商 IP 段间歇限流——同一个源上 /simple/pip/ 返回 200，
    而 /packages/.../*.whl 返回 403。只探 index 页会把它判成可用，用户照样装不上。
    所以这里真去取一次包文件（Range 只要 1 字节，不下载整个 wheel）。
    """
    t0 = time.monotonic()
    base = url.rstrip("/")
    # 页面真实地址是 <index>/<pkg>/，必须拿它当 urljoin 的基准：镜像页面里的
    # wheel 链接是相对路径（如 ../../packages/xx/six-1.17.0-py2.py3-none-any.whl），
    # 拿 <index>/ 当基准会少掉路径前缀，拼出 404（实测阿里云镜像）。
    page_url = f"{base}/{PROBE_PKG}/"
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "pip/24.0"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            if not 200 <= r.status < 300:
                return name, url, False, time.monotonic() - t0, f"HTTP {r.status}"
            page = r.read(400000).decode("utf-8", "replace")
        m = re.search(r'href="([^"#]+\.whl)', page)
        if not m:
            # 页面里没有 wheel（镜像只给源码包 / 结构变了）：index 通了就算通，
            # 真装不上还有 run_pip 的换源兜底。
            return name, url, True, time.monotonic() - t0, "index only"
        req2 = urllib.request.Request(urljoin(page_url, m.group(1)),
                                      headers={"User-Agent": "pip/24.0",
                                               "Range": "bytes=0-0"})
        with urllib.request.urlopen(req2, timeout=PROBE_TIMEOUT) as r2:
            ok = 200 <= r2.status < 300
            return name, url, ok, time.monotonic() - t0, f"dl {r2.status}"
    except urllib.error.HTTPError as e:
        return name, url, False, time.monotonic() - t0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 —— 探测不该因任何网络异常中断
        return name, url, False, time.monotonic() - t0, type(e).__name__


def probe_all() -> list[tuple[str, str, bool, float, str]]:
    """并发探测全部候选源，总耗时 = 最慢那个，而不是逐个累加。"""
    cands: list[tuple[str, str]] = []
    cfg = configured_index()
    if cfg:
        cands.append(("已配置", cfg))
    for n, u in MIRRORS:
        if u not in [c[1] for c in cands]:
            cands.append((n, u))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cands)) as ex:
        return list(ex.map(lambda c: _probe(*c), cands))


def pick(verbose: bool = False) -> str:
    """返回选中的源 URL；一个都不通就返回空串。"""
    results = probe_all()
    ok = [r for r in results if r[2]]
    if verbose:
        for name, url, good, cost, detail in results:
            mark = "OK  " if good else "FAIL"
            print(f"  [{mark}] {name:<10} {cost:5.2f}s  {detail:<12} {url}", file=sys.stderr)
    if not ok:
        return ""
    # 已配置的源只要通了就优先（用户可能在公司代理后面，别自作主张换掉）；
    # 其余按实测耗时取最快，比列表顺序更贴近当下网络。
    for name, url, _good, _cost, _d in ok:
        if name == "已配置":
            return url
    best = min(ok, key=lambda r: r[3])
    if verbose:
        print(f"  → 选中 {best[0]}（{best[3]:.2f}s）", file=sys.stderr)
    return best[1]


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]


def run_pip(args: list[str], index: str) -> int:
    timeout = _env("DABAI_PIP_TIMEOUT") or "30"
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--index-url", index,
        "--trusted-host", _host_of(index),  # 内网/自建 http 源需要；https 源无害
        "--timeout", timeout,
        "--retries", "2",
        "--no-input",
        "--disable-pip-version-check",
        *args,
    ]
    return subprocess.run(cmd).returncode


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--probe":
        results = probe_all()
        for name, url, good, cost, detail in results:
            print(f"[{'OK  ' if good else 'FAIL'}] {name:<10} {cost:5.2f}s  {detail:<12} {url}")
        return 0 if any(r[2] for r in results) else 1
    if argv[0] == "--print-index":
        idx = pick()
        if not idx:
            print("所有候选 pip 源都不可达", file=sys.stderr)
            return 1
        print(idx)
        return 0

    results = probe_all()
    ok = [r for r in results if r[2]]
    if not ok:
        print("[X] 所有候选 pip 源都探测失败，请检查网络或设置 DABAI_PIP_INDEX", file=sys.stderr)
        return 1
    for name, url, good, cost, detail in results:
        print(f"  [{'OK  ' if good else 'FAIL'}] {name:<10} {cost:5.2f}s  {detail:<12} {url}")

    tried: list[str] = []
    for name, url, _g, _c, _d in ok[:MAX_ATTEMPTS]:
        print(f"== 使用源 {name}：{url} ==")
        if run_pip(argv, url) == 0:
            return 0
        tried.append(name)
        print(f"[!] {name} 安装失败，换下一个源重试…", file=sys.stderr)
    print(f"[X] 依次尝试 {'、'.join(tried)} 均失败，请检查网络或设置 DABAI_PIP_INDEX", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
