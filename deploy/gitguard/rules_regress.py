#!/usr/bin/env python3
"""secretscan 判据回归 —— 每条用例都对应真实项目里踩过的一次坑。

左列是「真实项目里被误报的写法」，右列是「必须仍然抓得到的真密钥」。
改判据前先跑这个：误报放宽容易，把真密钥一起放过就麻烦了。

    python3 deploy/gitguard/rules_regress.py

退出码 0 = 全过；1 = 有用例不符合预期。

注意：本文件里的密钥字面量一律在运行时拼出来（"sk-" + "proj-" + …）。
源码里若留下完整的密钥形态，这个文件自己就会被扫出噪音 ——
install.sh --check 会扫全部被跟踪文件，到时候天天报警的就是它。
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_scanner():
    # 树莓派上叫 secretscan.py，Windows 工具目录里叫 gitguard.py —— 同一个文件。
    for name in ("secretscan.py", "gitguard.py"):
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("secretscan", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit(f"找不到扫描器（在 {_HERE} 里找 secretscan.py / gitguard.py）")


def _cases():
    # 运行时拼装，源码里不留完整密钥形态
    k_openai = "sk-" + "proj-" + "9fK2mQ7pL4vN8sT1wR6yZ3aB5cD0eF"
    hex_a = "c9b6f1e0" + "d8a74b23" + "95c6f0e1" + "d2a3b4c5"
    hex_b = "444e1a2b" + "3c4d5e6f" + "7a8b9c0d" + "1e2f3a4b"
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----"
    pwd = "x9K2mQ7p" + "L4vN8sT1" + "wR6yZ3"
    apikey_v = "aB3kL9mN" + "2pQ7rS4t" + "U6vW8xY1z"
    akia_v = "AKIA" + "IOSFODNN7EXAMPLE"

    return [
        # ---------- 真实项目里抓到的误报：必须不报 ----------
        ("PEM 头当字符串处理", "xianyu-auto-reply/alipay_service.py",
         "for tag in ('" + pem + "', 'x'):", False),
        ("路由常量 password 前缀", "xianyu-auto-reply/accounts.ts",
         "const PASSWORD_LOGIN_PREFIX = '/api/v1/password-login'", False),
        ("路由常量 token url", "xianyu-auto-reply/tokenApiModeSettings.ts",
         "const TOKEN_REMOTE_URL = '/api/v1/sys/token/remote'", False),
        ("业务码 serviceCode", "xianyu-auto-reply/xianyu_direct_publisher.py",
         '{"enable": False, "serviceCode": "FAST_DELIVERY_CODE"}', False),
        ("API 方法名 api", "xianyu-auto-reply/qr_login/manager.py",
         '"api": "mtop.gaia.queryUserInfoById"', False),
        ("数量配置 max_tokens", "通用",
         "MAX_TOKENS = 8192", False),
        ("复合 ID state_key", "通用",
         'state_key = "2|2|20260909"', False),
        ("hash 常量", "通用",
         'sha = "a3f9c2e1b4d7"', False),
        ("monkey 里含 key", "词根边界",
         'monkey = "abcdefghijklmnop1234"', False),
        ("tokenizer 里含 token", "词根边界",
         'tokenizer_name = "cl100k_base_v2_final"', False),
        ("普通 URL", "通用",
         'TOKEN_ENDPOINT = "https://api.example.com/v1/oauth/token"', False),

        # ---------- 真密钥：必须报 ----------
        ("OpenAI key", "有固定前缀",
         'OPENAI_API_KEY = "' + k_openai + '"', True),
        ("自建 32 位 hex key", "无固定前缀",
         'AMAP_WEB_KEY = "' + hex_a + '"', True),
        ("appKey 驼峰", "无固定前缀",
         'appKey = "' + hex_b + '"', True),
        ("password 字段", "密钥词根",
         'password = "' + pwd + '"', True),
        ("apikey 连写", "密钥词根",
         'apikey = "' + apikey_v + '"', True),
        ("AWS AKIA", "有固定前缀",
         'aws = "' + akia_v + '"', True),
        ("真私钥独占一行", "私钥文件头",
         pem, True),
    ]


def main() -> int:
    ss = _load_scanner()
    cases = _cases()
    failed = []
    for desc, source, snippet, want in cases:
        got = bool(ss.scan_text(snippet, "<regress>"))
        if got != want:
            failed.append((desc, source, want, got, snippet))
    if failed:
        for desc, source, want, got, snippet in failed:
            verdict = "报了但应该不报（误报）" if got else "没报但应该报（漏报）"
            print(f"  FAIL {desc} [{source}] {verdict}")
            print(f"       片段: {snippet[:90]}")
            for f in ss.scan_text(snippet, "<regress>"):
                print(f"       -> {f}")
    print(f"{len(cases) - len(failed)}/{len(cases)} 用例通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
