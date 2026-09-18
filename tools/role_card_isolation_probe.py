"""角色卡片按人隔离的活体探针：两个用户各切一张卡，互不影响。

为什么单独写：auth_http_selftest.py 只探了「闸门」和「没切过卡的人 active_id 为空」，
证明不了隔离本身——真正的断言是「A 切卡后 B 的 active_id 不变，且全局
settings.json / tts_config.json 一个字节都没动」。旧实现（apply 把卡片烤进全局配置）
在这两条上必红。

不碰真实用户表（auth_core.USERS_FILE 换成临时文件），只借用真实卡片库里的两张卡，
跑完把 role_card_users.json 原样还回去。

用法：venv/bin/python tools/role_card_isolation_probe.py
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import email_verify  # noqa: E402

FAILS: list = []
CODE = "123456"


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def arm_code(email: str) -> dict:
    import hashlib as _h
    salt = "t" * 16
    email_verify._CODES[email.strip().lower()] = {
        "salt": salt,
        "hash": _h.sha256((salt + CODE).encode()).hexdigest(),
        "exp": time.time() + 60,
        "tries": 0,
        "sent": time.time(),
    }
    return {"email_code": CODE}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "<missing>"


def main() -> int:
    import auth_core

    tmp = Path(tempfile.mkdtemp())
    auth_core.USERS_FILE = tmp / "users.json"
    auth_core.REG_OPEN_FILE = tmp / "registration.json"
    auth_core._REG_HIST.clear()

    import server  # noqa: E402  （必须在 patch 之后导入）
    from starlette.testclient import TestClient

    users_file = ROOT / "role_card_users.json"
    backup = users_file.read_bytes() if users_file.exists() else None

    cards = json.loads((ROOT / "character_cards.json").read_text("utf-8"))
    cards = cards.get("cards", cards) if isinstance(cards, dict) else cards
    if len(cards) < 2:
        print("  FAIL 卡片库不足两张，无法验证隔离")
        return 1
    card_a, card_b = cards[0]["id"], cards[1]["id"]
    print(f"卡片：A={card_a}({cards[0].get('name')})  B={card_b}({cards[1].get('name')})")

    cfg_files = [ROOT / "settings.json", ROOT / "tts_config.json"]
    cfg_before = {p.name: sha(p) for p in cfg_files}

    def reg(em: str):
        c = TestClient(server.app)
        r = c.post("/api/auth/register",
                   json={"name": em, "pwd": "secret123", **arm_code(em)})
        assert r.status_code == 200, r.text[:200]
        return c, r.json()["user"]["id"]

    try:
        ca, uid_a = reg("iso.a@example.com")
        cb, uid_b = reg("iso.b@example.com")
        print(f"用户：A={uid_a}  B={uid_b}")

        ra = ca.post(f"/api/character_cards/{card_a}/apply")
        check("A 切卡 200", ra.status_code == 200, f"{ra.status_code} {ra.text[:80]}")
        rb = cb.post(f"/api/character_cards/{card_b}/apply")
        check("B 切卡 200", rb.status_code == 200, f"{rb.status_code} {rb.text[:80]}")

        la = ca.get("/api/character_cards").json().get("active_id")
        lb = cb.get("/api/character_cards").json().get("active_id")
        check("A 看到自己的卡", la == card_a, f"active_id={la!r} 期望 {card_a!r}")
        check("B 看到自己的卡", lb == card_b, f"active_id={lb!r} 期望 {card_b!r}")
        check("两人互不串卡", la != lb, f"A={la!r} B={lb!r}")

        stored = json.loads(users_file.read_text("utf-8")).get("users", {})
        check("指针按 uid 落盘", stored.get(uid_a) == card_a and stored.get(uid_b) == card_b,
              f"{stored.get(uid_a)!r} / {stored.get(uid_b)!r}")

        cfg_after = {p.name: sha(p) for p in cfg_files}
        check("全局配置零改动（settings.json + tts_config.json）",
              cfg_before == cfg_after,
              f"before={cfg_before} after={cfg_after}")
    finally:
        if backup is not None:
            users_file.write_bytes(backup)
        elif users_file.exists():
            users_file.unlink()
        print(f"已还原 {users_file.name}")

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
