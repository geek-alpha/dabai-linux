"""探测 B 站搜索翻页能持续扩展多少页：每页去重后新增条数 + has_more。

「持续扩展播放列表」的物理上限就在这：哪一页开始零新增，就是自然终点。
用法：venv/bin/python tools/vh_deep_probe.py   （默认 25 页）
"""
import sys
import urllib3
import requests

urllib3.disable_warnings()
BASE = "https://127.0.0.1:8008"
S = requests.Session()
S.verify = False
PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 25

seen = set()
stop = None
for page in range(1, PAGES + 1):
    r = S.get(f"{BASE}/api/video_hub/api/search",
              params={"q": "猫", "platform": "bilibili", "sort": "relevance",
                      "limit": 12, "page": page}, timeout=30)
    if r.status_code != 200:
        print(f"page {page}: HTTP {r.status_code} {r.text[:120]}")
        break
    d = r.json()
    items = d.get("results") or []
    new = 0
    for it in items:
        u = it.get("webpage_url") or ""
        if u and u not in seen:
            seen.add(u)
            new += 1
    print(f"page {page}: 返回 {len(items):2d} 条 / 去重后新增 {new:2d} / has_more={d.get('has_more')}")
    if new == 0 and stop is None:
        stop = page
    if not items:
        break

print(f"\n累计去重条目 {len(seen)} 条；首次零新增页 = {stop or '未出现（探测范围内一直有新增）'}")
