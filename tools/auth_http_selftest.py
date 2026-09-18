"""鉴权闸门的 HTTP 层自检：用 ASGI TestClient 真跑中间件与路由。

为什么单独一个文件：sandbox_selftest.py 的定位是「不起服务、直接跑核心断言」，
而下面三件事只有经过真实 ASGI 栈（中间件 + 路由 + cookie）才测得出来：
  1) 免登录白名单：/api/auth/status 未登录可访问，/api/tasks 未登录 401；
  2) 注册闸门：公网同 IP 限速 429、关闸 403、用户名自助注册 400（只剩邮箱验码/GitHub）；
  3) 角色注入：/ 返回的 HTML 里 data-role 与请求身份一致，普通用户系统 API 403。

全程用临时用户表与临时注册开关文件，绝不碰真实 data/users.json。
import server 不会启动服务（uvicorn.run 在 __main__ 保护里），约 10s。

用法：venv/bin/python tools/auth_http_selftest.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import email_verify  # noqa: E402

FAILS: list = []
CODE = "123456"


def arm_code(email: str) -> dict:
    """给这个邮箱预置一个验证码（不真发信），返回可拼进注册请求的片段。

    同进程才这么干得成：码存在 email_verify 的进程内存里，TestClient 和测试共享它。
    注册现在只认「邮箱验码」或「GitHub」，所以每个 HTTP 层注册用例都得先过这道闸。
    """
    import hashlib
    import time
    salt = "t" * 16
    email_verify._CODES[email.strip().lower()] = {
        "salt": salt,
        "hash": hashlib.sha256((salt + CODE).encode()).hexdigest(),
        "exp": time.time() + 60,
        "tries": 0,
        "sent": time.time(),
    }
    return {"email_code": CODE}


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    import auth_core

    tmp = Path(tempfile.mkdtemp())
    auth_core.USERS_FILE = tmp / "users.json"
    auth_core.REG_OPEN_FILE = tmp / "registration.json"
    auth_core._REG_HIST.clear()

    import server  # noqa: E402  （必须在上面的 patch 之后：server 持有 auth_core 模块引用）
    from starlette.testclient import TestClient

    pub = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "selftest"}
    client = TestClient(server.app)   # 模拟公网手机：每次请求带 CF 头
    local = TestClient(server.app)    # 模拟局域网直连：无 CF 头 = 系统身份

    section("免登录白名单")
    r = client.get("/api/auth/status")
    check("status 免登录可访问", r.status_code == 200, r.text[:120])
    check("status 报出注册开放", r.json().get("registration_open") is True, r.text[:120])
    check("全新部署 first_run 为真", r.json().get("first_run") is True, r.text[:120])
    check("未登录 /api/auth/me 401", client.get("/api/auth/me").status_code == 401)
    check("未登录系统 API 401", client.get("/api/tasks", headers=pub).status_code == 401)
    check("局域网无 cookie 视为系统身份", local.get("/api/auth/users").status_code == 200)

    section("注册闸门（HTTP 层）")
    codes = []
    for i in range(auth_core._REG_PER_IP):
        em = f"gate{i}@example.com"
        rr = client.post("/api/auth/register", headers=pub,
                         json={"name": em, "pwd": "secret123", **arm_code(em)})
        codes.append(rr.status_code)
    check(f"同 IP 前 {auth_core._REG_PER_IP} 次注册 200",
          codes == [200] * auth_core._REG_PER_IP, str(codes))
    em = "gate-over@example.com"
    r = client.post("/api/auth/register", headers=pub,
                    json={"name": em, "pwd": "secret123", **arm_code(em)})
    check("同 IP 超限 429", r.status_code == 429, f"{r.status_code} {r.text[:80]}")
    check("错误码 rate_limited", r.json().get("code") == "rate_limited", r.text[:120])

    section("身份来源闸门（用户名自助注册已关闭）")
    nope = {"cf-connecting-ip": "203.0.113.71", "cf-ray": "selftest"}
    r = client.post("/api/auth/register", headers=nope,
                    json={"name": "纯用户名", "pwd": "secret123"})
    check("用户名自助注册 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    check("错误码 email_required", r.json().get("code") == "email_required", r.text[:120])
    r = client.post("/api/auth/register", headers=nope,
                    json={"name": "noverify@example.com", "pwd": "secret123"})
    check("邮箱没验码 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    # 端点是先 check_code 再进 auth_core，所以这里拿到的是 no_code；
    # email_unverified（拿着无效票据）只在直接调 auth_core 时出现，
    # 那条路由 tools/reg_gate_selftest.py 覆盖。
    check("错误码 no_code", r.json().get("code") == "no_code", r.text[:120])
    r = client.post("/api/auth/register", headers=nope,
                    json={"name": "noverify@example.com", "pwd": "secret123",
                          "email_code": "000000"})
    check("错码 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    em = "verified@example.com"
    r = client.post("/api/auth/register", headers=nope,
                    json={"name": em, "pwd": "secret123", **arm_code(em)})
    check("邮箱+正确码 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    check("email_verified 为真", r.json().get("user", {}).get("email_verified") is True,
          r.text[:120])
    check("响应不含 pwd/salt",
          not ({"pwd", "salt"} & set(r.json().get("user", {}))), r.text[:120])

    section("角色注入与系统 API 分权")
    # 断注入产物本身（window.__ROLE="..."），不要断 data-role="..."：
    # 后者在 index.html 的 <style> 里作为选择器字面量恒存在，断它等于永远通过。
    r = client.get("/", headers=pub)
    check("公网普通用户拿到 __ROLE=user", 'window.__ROLE="user"' in r.text, r.text[:120])
    check("HTML 带 admin-only 标记", "data-admin-only" in r.text)
    check("普通用户 /api/tasks 403", client.get("/api/tasks", headers=pub).status_code == 403)
    check("普通用户 /api/workspaces 403",
          client.get("/api/workspaces", headers=pub).status_code == 403)
    check("普通用户改角色 403",
          client.post("/api/auth/role", headers=pub,
                      json={"uid": "u_x", "role": "admin"}).status_code == 403)
    check("普通用户看用户清单 403",
          client.get("/api/auth/users", headers=pub).status_code == 403)
    r = local.get("/")
    check("局域网直连拿到 __ROLE=admin", 'window.__ROLE="admin"' in r.text, r.text[:120])

    section("注册开关")
    auth_core.REG_OPEN_FILE.write_text('{"open": false}', encoding="utf-8")
    r = client.get("/api/auth/status")
    check("status 反映关闸", r.json().get("registration_open") is False, r.text[:120])
    em = "closed@example.com"
    r = client.post("/api/auth/register", headers={**pub, "cf-connecting-ip": "198.51.100.9"},
                    json={"name": em, "pwd": "secret123", **arm_code(em)})
    check("关闸后公网注册 403", r.status_code == 403, f"{r.status_code} {r.text[:80]}")
    check("错误码 registration_closed", r.json().get("code") == "registration_closed",
          r.text[:120])
    # 局域网直连过去算「系统身份」、不受开关管；现在注册一律要验码或 GitHub，
    # 局域网也一样 —— 同一个 WiFi 下的别人也是别人。
    em = "lan@example.com"
    r = local.post("/api/auth/register",
                   json={"name": em, "pwd": "secret123", **arm_code(em)})
    check("关闸后局域网也注册不了", r.status_code == 403, f"{r.status_code} {r.text[:80]}")
    auth_core.REG_OPEN_FILE.unlink()

    section("用户数上限")
    keep_max = auth_core.MAX_USERS
    auth_core.MAX_USERS = auth_core.user_count()
    try:
        em = "over-max@example.com"
        r = local.post("/api/auth/register",
                       json={"name": em, "pwd": "secret123", **arm_code(em)})
        check("达上限 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
        check("错误码 too_many_users", r.json().get("code") == "too_many_users", r.text[:120])
    finally:
        auth_core.MAX_USERS = keep_max

    section("登录态")
    r = client.post("/api/auth/login", headers=pub,
                    json={"name": "gate0@example.com", "pwd": "wrong-pwd"})
    check("错密码 401", r.status_code == 401, f"{r.status_code} {r.text[:80]}")
    r = client.post("/api/auth/login", headers=pub,
                    json={"name": "gate0@example.com", "pwd": "secret123"})
    check("对密码 200", r.status_code == 200, r.text[:120])
    # 临时用户表是空的，所以第一个注册者自动是管理员（与真实首启同一逻辑）
    check("首个注册者是管理员", r.json().get("user", {}).get("role") == "admin", r.text[:120])
    r = client.get("/api/auth/me", headers=pub)
    check("带 cookie 的 me 返回本人", r.json().get("user", {}).get("name") == "gate0@example.com",
          r.text[:120])
    r = client.post("/api/auth/login", headers=pub,
                    json={"name": "gate1@example.com", "pwd": "secret123"})
    check("后续注册者是普通用户", r.json().get("user", {}).get("role") == "user", r.text[:120])

    section("视频源仅管理员（列源与改源都锁）")
    # 独立 client：前面几个 client 的 cookie 已被管理员会话占用，混用测不出分权
    plain = TestClient(server.app)
    r = plain.post("/api/auth/login", headers=pub,
                   json={"name": "gate1@example.com", "pwd": "secret123"})
    check("普通用户登录成功", r.status_code == 200, r.text[:120])
    check("普通用户列视频源 403",
          plain.get("/api/video_hub/api/sources", headers=pub).status_code == 403)
    check("普通用户列平台 403",
          plain.get("/api/video_hub/api/platforms", headers=pub).status_code == 403)
    check("普通用户启停内置平台 403",
          plain.patch("/api/video_hub/api/sources/builtin/bilibili", headers=pub,
                      json={"enabled": False}).status_code == 403)
    check("普通用户加自定义源 403",
          plain.post("/api/video_hub/api/sources/custom", headers=pub,
                     json={"name": "x", "search_url": "https://e.com/?q={kw}"}).status_code == 403)
    check("普通用户删自定义源 403",
          plain.delete("/api/video_hub/api/sources/custom/custom_x", headers=pub).status_code == 403)
    # 管理员侧只测只读：写操作会动真实 video_sources.json
    r = local.get("/api/video_hub/api/sources")
    check("管理员列视频源 200", r.status_code == 200, r.text[:120])
    check("列源含内置与自定义两段", "builtin" in r.json() and "custom" in r.json(), r.text[:120])
    check("页面里视频源页签标了 admin-only",
          'id="video-tab-sources" class="music-tab" data-admin-only' in local.get("/").text)

    section("模型/背景/供应商仅管理员（写接口 403 + 界面入口隐藏）")
    glb = {"file": ("probe.glb", b"glTF", "model/gltf-binary")}
    check("普通用户上传模型 403",
          plain.post("/api/model/upload", headers=pub, files=glb).status_code == 403)
    check("普通用户上传背景 403",
          plain.post("/api/background/upload", headers=pub, files=glb).status_code == 403)
    check("普通用户列供应商 403",
          plain.get("/api/llm/providers", headers=pub).status_code == 403)
    # 界面入口：服务端给普通用户注入 data-role=user，CSS 的
    # html[data-role="user"] [data-admin-only]{display:none} 才藏得掉这些按钮
    html = plain.get("/", headers=pub).text
    check("普通用户 HTML 注入 user 角色", 'dataset.role="user"' in html)
    for sel in ('id="gyro-btn" class="stage-tool-btn" data-admin-only',
                'id="llm-provider-btn" class="stage-tool-btn" data-admin-only',
                'id="rc-model-upload-btn" type="button" data-admin-only',
                'id="rc-llm-manage-btn" type="button" data-admin-only',
                'id="role-card-create-btn" class="upload-card" data-admin-only',
                'id="rc-apply-btn" class="btn-primary" data-admin-only',
                'class="upload-card" for="bg-file-input" data-admin-only'):
        check("入口带 admin-only：" + sel[:38], sel in html)

    section("角色卡片：普通用户可切换，编辑仍仅管理员")
    # 切换（apply）只把「该用户的指针」指向卡片（按人隔离：人设/音色/模型/LLM 全在
    # 解析时按 uid 取），普通用户放行；新建/改/删写的是全机唯一一套卡片库，一律 403。
    check("普通用户列卡片 200",
          plain.get("/api/character_cards", headers=pub).status_code == 200)
    check("普通用户改卡片 403",
          plain.put("/api/character_cards/probe", headers=pub,
                    json={"name": "x"}).status_code == 403)
    check("普通用户新建卡片 403",
          plain.post("/api/character_cards", headers=pub,
                     json={"name": "x"}).status_code == 403)
    # 用不存在的卡片 id 探闸门：中间件放行后路由走到「卡片不存在」404，
    # 既证明放行、又不会真把生产的人设/语音改掉（selftest 不许动全局配置）。
    check("普通用户应用卡片放行（不存在的卡片 → 404）",
          plain.post("/api/character_cards/probe/apply", headers=pub).status_code == 404)
    # 按人隔离的硬证据：没切过卡的 uid 拿到空 active_id（旧行为下这里会返回全局激活卡片）
    _probe = plain.get("/api/character_cards?user_id=__probe_never_used__", headers=pub)
    check("卡片列表按用户返回 active_id",
          _probe.status_code == 200 and _probe.json().get("active_id") == "",
          f"http={_probe.status_code} active_id={_probe.json().get('active_id')!r}")
    check("普通用户删卡片 403",
          plain.delete("/api/character_cards/probe", headers=pub).status_code == 403)
    ts = local.get("/static/js/ui/25_character_cards.ts")
    check("卡片面板前端带管理员闸门",
          ts.status_code == 200 and "canManageCards" in ts.text,
          f"http={ts.status_code} len={len(ts.text)}")
    # 两个闸门必须只留在编辑侧：apply 里再挂一道就是「服务端放行、前端拦下」
    parts = ts.text.split("App.applyRoleCard = async function")
    apply_fn = parts[1].split("App.restoreActiveRoleCard")[0] if len(parts) > 1 else ""
    check("applyRoleCard 不带管理员闸门",
          bool(apply_fn) and "canManageCards" not in apply_fn, apply_fn[:60])
    # 前端必须带用户身份：局域网直连没有 cookie，不带就是服务端按 unified 身份写，
    # 与 WS 侧 agent 用的 uid 对不上，切卡看着成功却不生效。
    check("applyRoleCard 携带用户身份",
          bool(apply_fn) and "dabai.userId" in apply_fn)
    ts = local.get("/static/js/character/07_click_interact.ts")
    # 触碰身体（戳一戳）只留给管理员：非管理员进 triggerPokeAt 直接 return，
    # 击退摇晃 / 相机聚焦 / 亲密互动消息三条副作用一并关闭，且不留任何提示反馈
    silent = "if (!App.IS_ADMIN) return;" in ts.text
    check("触碰身体前端带管理员闸门（静默拦截）",
          ts.status_code == 200 and silent and "pokeDenyAt" not in ts.text,
          f"http={ts.status_code} silent={silent}")
    ts = local.get("/static/js/core/02_three_scene.ts")
    # 相机护栏值：上下 60°、最近水平距离 1m。缩放下限不再写死，改为由基准距离联动推导
    # （距离滑块调到多小，镜头都恰好推到 1m 为止）；每帧兜底与缩放下限共用同一常量
    check("相机护栏 60°/1m 已生效",
          ts.status_code == 200
          and "USER_ORBIT_PITCH_LIMIT = Math.PI / 3" in ts.text
          and "USER_MIN_CAM_HORIZ = 1.0" in ts.text
          and "const minHoriz = App.USER_MIN_CAM_HORIZ;" in ts.text
          and "USER_MIN_ZOOM" not in ts.text,
          f"http={ts.status_code} len={len(ts.text)}")

    section("邮箱当名字用（单元级：规范化 + 畸形矩阵）")
    auth_core._REG_HIST.clear()   # 前几段已把「每 IP 每小时 3 次」的注册额度用光
    # 邮箱不是新体系：_name_key 的「NFKC + 大小写折叠 + 忽略空白」本来就是邮箱要的
    # 语义，所以这里验的是「邮箱能不能当名字用」，而不是「有没有第二张邮箱表」。
    for raw, want in (("You@Example.COM", "you@example.com"),
                      ("a.b+tag@sub.domain.co", "a.b+tag@sub.domain.co"),
                      ("x_y-z@a-b.com", "x_y-z@a-b.com")):
        try:
            got = auth_core._check_name(raw)
            check(f"邮箱规范化 {raw} → {want}", got == want, got)
        except auth_core.AuthError as e:
            check(f"邮箱规范化 {raw} → {want}", False, f"{e.code} {e.msg}")

    for bad in ("@example.com", "a@b", "a..b@x.com", ".a@x.com", "a@x..com",
                "a@-x.com", "a@x.com.", "a" * 70 + "@x.com", "a@" + "b" * 250 + ".com"):
        try:
            check(f"畸形邮箱被拒 {bad[:20]}", False, "竟然通过：" + auth_core._check_name(bad))
        except auth_core.AuthError as e:
            check(f"畸形邮箱被拒 {bad[:20]}", e.code == "bad_email", f"{e.code} {e.msg}")

    section("邮箱注册与登录（HTTP 层）")
    # 换一个出口 IP：pub 的「每 IP 每小时 3 次」额度已在注册闸门那节用光。
    mc_ip = {"cf-connecting-ip": "203.0.113.88", "cf-ray": "selftest"}
    mc = TestClient(server.app)
    raw = "Selftest.User@Example.COM"
    r = mc.post("/api/auth/register", headers=mc_ip,
                json={"name": raw, "pwd": "secret123", **arm_code(raw)})
    check("邮箱注册 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    mu = r.json().get("user", {})
    check("名字落成小写邮箱", mu.get("name") == "selftest.user@example.com", str(mu.get("name")))
    check("email 字段同值", mu.get("email") == "selftest.user@example.com", str(mu.get("email")))
    check("邮箱已标记为已验证", mu.get("email_verified") is True,
          str(mu.get("email_verified")))
    check("响应不含 pwd/salt", not ({"pwd", "salt"} & set(mu)), str(sorted(mu)))

    r = mc.post("/api/auth/register", headers=mc_ip,
                json={"name": "SELFTEST.USER@example.com", "pwd": "secret123",
                      **arm_code("selftest.user@example.com")})
    check("同邮箱换大小写判重 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check("错误码 name_taken", r.json().get("code") == "name_taken", r.text[:120])

    lg = TestClient(server.app)
    r = lg.post("/api/auth/login", headers=pub,
                json={"name": "SELFTEST.USER@Example.com", "pwd": "secret123"})
    check("邮箱大写形态能登录", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    check("登录拿到同一 uid", r.json().get("user", {}).get("id") == mu.get("id"),
          f"{r.json().get('user', {}).get('id')} vs {mu.get('id')}")
    r = lg.get("/api/auth/me", headers=pub)
    check("邮箱账号会话有效", r.status_code == 200
          and r.json().get("user", {}).get("name") == "selftest.user@example.com", r.text[:120])
    r = lg.post("/api/auth/login", headers=pub,
                json={"name": "selftest.user@example.com", "pwd": "wrong-pwd"})
    check("邮箱账号错密码 401", r.status_code == 401, f"{r.status_code} {r.text[:80]}")
    # 畸形邮箱：先给码才会走到名字格式校验那一步（没码时先报「没要过码」）
    bad = "不是邮箱@"
    r = mc.post("/api/auth/register", headers=mc_ip,
                json={"name": bad, "pwd": "secret123", **arm_code(bad)})
    check("含 @ 的畸形串 400 bad_email", r.status_code == 400
          and r.json().get("code") == "bad_email", f"{r.status_code} {r.text[:120]}")

    return 0


if __name__ == "__main__":
    main()
    print("\n" + ("全部通过 ✔" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    sys.exit(1 if FAILS else 0)
