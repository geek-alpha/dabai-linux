#!/usr/bin/env python3
"""把大白联邦（peer_mesh）接进 server.py，并配置本实例的身份 / 密钥 / 地址簿。

幂等：已改过的直接跳过，重复跑不会插出第二份 include_router。
三处改动全部走文本锚点而不是行号 —— 三台机器上的 server.py 差一两行是常态。

用法：
  python tools/peer_mesh_install.py --id rpi --label 树莓派              # 首台：生成新密钥
  python tools/peer_mesh_install.py --id aliyun --label 阿里云 --key K   # 后续：用集群已有密钥
  python tools/peer_mesh_install.py --check                              # 只体检不改
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
sys.path.insert(0, str(ROOT))

import peer_mesh  # noqa: E402  （必须在 sys.path 之后）

# 联邦默认地址簿：每个实例的公开域名就是它的电话号码。
# 装的时候自动写入「除自己以外」的全部节点，省掉每台机器手工 add-peer。
DEFAULT_PEERS = {
    "rpi": ("https://dabai.battlephoenix.tech", "树莓派"),
    "aliyun": ("https://aliyun.battlephoenix.tech", "阿里云"),
    "wsl": ("https://wsl.battlephoenix.tech", "WSL"),
}

IMPORT_ANCHOR = "import music_lib\n"
APP_ANCHOR = 'app = FastAPI(title="白头凤【BattlePhoenix】", lifespan=lifespan)\n'
APP_ADD = """
# 大白联邦：让散落在树莓派/阿里云/WSL 上的大白互相找到、互相说话。
# 这些路由自己验共享密钥，所以路径在下面的 _AUTH_EXEMPT_PREFIX 里豁免会话中间件——
# 别的实例没有、也不该有本机的会话 cookie。
app.include_router(peer_mesh.router)
"""
EXEMPT_OLD = '_AUTH_EXEMPT_PREFIX = ("/static/", "/icons/")\n'
EXEMPT_NEW = ('# /api/peer/ 走自己的密钥认证（peer_mesh），不认会话 cookie：联邦里的每个实例\n'
              '# 都是独立进程，互相之间不存在登录关系。\n'
              '_AUTH_EXEMPT_PREFIX = ("/static/", "/icons/", "/api/peer/")\n')


def patch_server(check: bool) -> list[str]:
    """按锚点做三处改动。返回每一步的结果描述。"""
    if not SERVER.is_file():
        return [f"✗ 找不到 {SERVER}"]
    src = SERVER.read_text(encoding="utf-8")
    out = src
    steps: list[str] = []

    if "import peer_mesh" in out:
        steps.append("= import peer_mesh 已存在")
    elif IMPORT_ANCHOR in out:
        out = out.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "import peer_mesh\n", 1)
        steps.append("+ 加 import peer_mesh")
    else:
        steps.append("✗ 找不到 import 锚点（server.py 结构变了？）")

    if "include_router(peer_mesh.router)" in out:
        steps.append("= include_router 已存在")
    elif APP_ANCHOR in out:
        out = out.replace(APP_ANCHOR, APP_ANCHOR + APP_ADD, 1)
        steps.append("+ 加 app.include_router")
    else:
        steps.append("✗ 找不到 app 锚点")

    if '"/api/peer/"' in out:
        steps.append("= 豁免前缀已存在")
    elif EXEMPT_OLD in out:
        out = out.replace(EXEMPT_OLD, EXEMPT_NEW, 1)
        steps.append("+ 加 /api/peer/ 豁免")
    else:
        steps.append("✗ 找不到 _AUTH_EXEMPT_PREFIX 锚点")

    if out != src and not check:
        backup = SERVER.with_suffix(f".py.bak-peer{int(__import__('time').time())}")
        backup.write_text(src, encoding="utf-8")
        SERVER.write_text(out, encoding="utf-8")
        steps.append(f"  备份 {backup.name}")
    return steps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", dest="node_id")
    ap.add_argument("--label", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        for s in patch_server(check=True):
            print(s)
        info = peer_mesh.node_info(create=False)
        print(f"身份: {info.get('node_id') or '(未配置)'}")
        print(f"密钥: {'已配置' if peer_mesh.cluster_key(create=False) else '(未配置)'}")
        print(f"地址簿: {sorted(peer_mesh.peers().keys()) or '(空)'}")
        return 0

    if a.key:
        peer_mesh._atomic_write(peer_mesh.KEY_FILE, a.key.strip() + "\n")
    if a.node_id:
        peer_mesh._atomic_write(
            peer_mesh.NODE_FILE,
            json.dumps({"node_id": a.node_id, "label": a.label or a.node_id},
                       ensure_ascii=False, indent=2))

    info = peer_mesh.node_info()
    key = peer_mesh.cluster_key().decode("utf-8")
    for nid, (url, label) in DEFAULT_PEERS.items():
        if nid != info["node_id"]:
            peer_mesh.set_peer(nid, url, label)

    for s in patch_server(check=False):
        print(s)
    print(f"身份: {info['node_id']}（{info['label']}）")
    print(f"密钥: {key}")
    print(f"地址簿: {sorted(peer_mesh.peers().keys())}")
    print("重启 myservice 后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
