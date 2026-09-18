"""发布层：X API v2，OAuth 1.0a 签名手写实现（不引 tweepy）。

为什么手写签名：整个项目只多一个 HTTP 请求，却要为此装一个带自己依赖树的库。
OAuth 1.0a 的签名算法是固定的（RFC 5849 §3.4.1），写 30 行就够，还能跑官方向量自检。

网络：api.x.com 直连超时，必须走代理（默认 127.0.0.1:7890）。
凭证：tools/x_ip/x_credentials.json，或用环境变量覆盖。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.environ.get("X_IP_CRED", os.path.join(HERE, "x_credentials.json"))
PROXY = os.environ.get("X_IP_PROXY", "http://127.0.0.1:7890")
API = "https://api.x.com/2"

CRED_TEMPLATE = {
    "_note": "在 https://developer.x.com 建 App → Keys and tokens 里取。需 write 权限。",
    "_steps": [
        "1. developer.x.com 建 Project + App（Free 档每月 500 条写入，够个人 IP 用）",
        "2. App 的 User authentication settings 设为 Read and write",
        "3. 生成 API Key/Secret 与 Access Token/Secret（Access Token 必须是 Read and write 权限）",
        "4. 填到本文件，然后跑：venv/bin/python -m tools.x_ip.cli verify",
    ],
    "api_key": "",
    "api_secret": "",
    "access_token": "",
    "access_token_secret": "",
}


def load_cred(path: str = CRED_PATH) -> dict:
    """凭证优先取环境变量，其次取文件。缺哪个字段就报哪个。"""
    env = {
        "api_key": os.environ.get("X_API_KEY", ""),
        "api_secret": os.environ.get("X_API_SECRET", ""),
        "access_token": os.environ.get("X_ACCESS_TOKEN", ""),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    }
    if all(env.values()):
        return env
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    for k in env:
        if d.get(k):
            env[k] = d[k]
    return env


def missing(cred: dict) -> list[str]:
    return [k for k, v in cred.items() if not v]


def _pct(s: str) -> str:
    """RFC 3986 编码：除 A-Za-z0-9-._~ 外全部转义。"""
    return urllib.parse.quote(str(s), safe="~")


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()


def _auth_header(
    method: str, url: str, cred: dict, extra: dict | None = None, nonce: str = "", ts: str = ""
) -> str:
    oauth = {
        "oauth_consumer_key": cred["api_key"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts or str(int(time.time())),
        "oauth_token": cred["access_token"],
        "oauth_version": "1.0",
    }
    # 签名只吃 oauth_* 和 query 参数；JSON body 不进签名
    allp = dict(oauth)
    allp.update(extra or {})
    norm = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(allp.items()))
    base = "&".join([method.upper(), _pct(url), _pct(norm)])
    key = f"{_pct(cred['api_secret'])}&{_pct(cred['access_token_secret'])}"
    sig = _b64(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest())
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def _request(
    method: str, path: str, cred: dict, body: dict | None = None, timeout: float = 30.0
) -> tuple[int, dict | str]:
    url = API + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": _auth_header(method, url, cred), "User-Agent": "bp-x-ip/1.0"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw[:600]


def verify(cred: dict | None = None) -> dict:
    """校验凭证：GET /2/users/me。返回 {ok, status, data/error, user}。"""
    c = cred or load_cred()
    miss = missing(c)
    if miss:
        return {"ok": False, "status": 0, "error": "缺凭证字段：" + ", ".join(miss)}
    code, data = _request("GET", "/users/me", c)
    if code == 200 and isinstance(data, dict):
        u = data.get("data") or {}
        return {"ok": True, "status": code, "user": u}
    return {"ok": False, "status": code, "error": data}


def post_text(text: str, cred: dict | None = None, dry_run: bool = False) -> dict:
    """发一条推文。dry_run=True 时只返回将要发送的载荷，不真发。"""
    c = cred or load_cred()
    payload = {"text": text}
    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload}
    miss = missing(c)
    if miss:
        return {"ok": False, "status": 0, "error": "缺凭证字段：" + ", ".join(miss), "payload": payload}
    code, data = _request("POST", "/tweets", c, payload)
    out = {"ok": code in (200, 201), "status": code, "payload": payload, "raw": data}
    if isinstance(data, dict) and data.get("data"):
        tid = data["data"].get("id", "")
        out["tweet_id"] = tid
        out["tweet_url"] = f"https://x.com/i/status/{tid}" if tid else ""
    else:
        out["error"] = data
    return out


def delete_tweet(tweet_id: str, cred: dict | None = None) -> dict:
    c = cred or load_cred()
    code, data = _request("DELETE", f"/tweets/{tweet_id}", c)
    return {"ok": code == 200, "status": code, "raw": data}


def selftest() -> dict:
    """用 X 官方文档的签名示例向量自检签名实现——没有真凭证也能验正确性。

    向量来源（2026-09-18 核实）：X 官方文档 “Creating a signature” 的示例，
    在 stackoverflow 49735086 里被逐字引用为 Expected:<hCtSmYh+iHYCEqBWrE7C7hYmtUk=>。
    该示例的签名参数同时含 status 和 include_entities（OAuth 1.0a 会把
    application/x-www-form-urlencoded 的 body 参数并进签名）。

    关键区别：本文件发给 X API v2 的是 JSON body，而 OAuth 1.0a 不签 JSON body，
    所以 _request 里 body 参数不进签名——这也意味着换签名实现时不能顺手把 body 加进去。
    """
    # 这四个值是 X 官方文档 “Creating a signature” 页面上的示例凭证，公开可查
    # （核实：developer.twitter.com/en/docs/basics/authentication/guides/creating-a-signature），
    # 不是真凭证。扫描器按行判定，所以标记要写在值所在行。
    cred = {
        "api_key": "xvz1evFS4wEEPTGEFPHBog",  # allowlist secret
        "api_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",  # allowlist secret
        "access_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",  # allowlist secret
        "access_token_secret": "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",  # allowlist secret
    }
    url = "https://api.twitter.com/1.1/statuses/update.json"
    header = _auth_header(
        "POST",
        url,
        cred,
        extra={
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
            "include_entities": "true",
        },
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        ts="1318622958",
    )
    got = ""
    for part in header.replace("OAuth ", "").split(", "):
        if part.startswith("oauth_signature="):
            got = urllib.parse.unquote(part.split("=", 1)[1].strip('"'))
    want = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="
    return {"ok": got == want, "got": got, "want": want}


if __name__ == "__main__":
    st = selftest()
    print(("✓" if st["ok"] else "✗") + f" OAuth 签名自检  得到={st['got']}  期望={st['want']}")
    c = load_cred()
    miss = missing(c)
    print("凭证：" + ("缺失 " + ", ".join(miss) if miss else "已配置"))
    if not miss:
        print("校验：", json.dumps(verify(c), ensure_ascii=False)[:300])
