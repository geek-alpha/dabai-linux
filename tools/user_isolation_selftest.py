# -*- coding: utf-8 -*-
"""个性化数据按用户隔离的自检（视频历史 / 视频收藏 / 音乐歌单）。

两层：
  单元层 —— 直接调三个 lib，验证按 uid 分文件、互不可见、畸形 uid 不回落全局；
  HTTP 层 —— 真实 ASGI 栈（中间件 + cookie）跑两个账号，验证 A 的收藏/历史/歌单
             在 B 的接口里彻底看不见，且 B 删不掉 A 的东西。

全程用临时 users.json + 临时全局根 + 临时 data/users 根，绝不碰真实数据。
用法：venv/bin/python tools/user_isolation_selftest.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

FAILS: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    import auth_core
    import user_store

    tmp = Path(tempfile.mkdtemp())
    auth_core.USERS_FILE = tmp / "users.json"
    auth_core.REG_OPEN_FILE = tmp / "registration.json"
    auth_core._REG_HIST.clear()
    # 数据根全部搬进临时目录：连「全局文件」也搬，否则断言会读到主人的真实数据
    (tmp / "global").mkdir()
    user_store.ROOT = tmp / "global"
    user_store.USER_DATA_ROOT = tmp / "users"

    import video_history_lib
    import video_fav_lib
    import music_lib

    v_a = {"webpage_url": "https://example.com/a", "title": "A 的视频"}
    v_b = {"webpage_url": "https://example.com/b", "title": "B 的视频"}

    section("单元层：三个库按 uid 分文件")
    video_fav_lib.add_favorite(v_a, uid="u_aaa")
    video_fav_lib.add_favorite(v_b, uid="u_bbb")
    fa = video_fav_lib.list_all(uid="u_aaa")["favorites"]
    fb = video_fav_lib.list_all(uid="u_bbb")["favorites"]
    check("A 只看到自己的 1 条收藏",
          [f["video"]["title"] for f in fa] == ["A 的视频"], str(fa)[:120])
    check("B 只看到自己的 1 条收藏",
          [f["video"]["title"] for f in fb] == ["B 的视频"], str(fb)[:120])
    check("主人（uid 空）看不到任何人的收藏",
          video_fav_lib.list_all()["favorites"] == [])

    video_history_lib.add_history(v_a, uid="u_aaa")
    check("A 历史 1 条", len(video_history_lib.list_history(uid="u_aaa")) == 1)
    check("B 历史 0 条", len(video_history_lib.list_history(uid="u_bbb")) == 0)
    check("主人历史 0 条", len(video_history_lib.list_history()) == 0)

    music_lib.create_playlist("A 的歌单", uid="u_aaa")
    check("A 有 1 个歌单", len(music_lib.list_playlists(uid="u_aaa")) == 1)
    check("B 有 0 个歌单", len(music_lib.list_playlists(uid="u_bbb")) == 0)
    check("每个用户一个独立目录",
          (tmp / "users" / "u_aaa").is_dir() and (tmp / "users" / "u_bbb").is_dir(),
          str(sorted(p.name for p in (tmp / "users").iterdir())))

    section("单元层：畸形 uid 不回落全局")
    video_fav_lib.add_favorite(v_a, uid="../../etc")
    # ../../etc 清洗后剩 etc：分隔符被剥掉，穿越不成立，但也不许落到全局
    check("穿越型 uid 的目录被压成一层",
          (tmp / "users" / "etc" / "video_favorites.json").is_file(),
          str(sorted(p.name for p in (tmp / "users").iterdir())))
    check("穿越型 uid 没污染全局文件", video_fav_lib.list_all()["favorites"] == [])
    # 清洗后为空（全是点）→ 隔离到 _invalid，同样不许回落全局
    video_fav_lib.add_favorite(v_a, uid="...")
    check("清洗后为空的 uid 落到 _invalid",
          (tmp / "users" / "_invalid" / "video_favorites.json").is_file())
    check("没在项目根/上层生成杂文件",
          not (ROOT / "etc" / "video_favorites.json").exists())

    section("HTTP 层：两个账号互相看不见")
    import server  # noqa: E402  （patch 之后才 import：server 持有 auth_core 模块引用）
    from starlette.testclient import TestClient

    pub = {"cf-connecting-ip": "203.0.113.9", "cf-ray": "iso"}
    admin = TestClient(server.app)
    user = TestClient(server.app)

    admin.post("/api/auth/register", headers=pub,
               json={"name": "隔离管理员", "pwd": "secret123"})
    user.post("/api/auth/register", headers=pub,
              json={"name": "隔离普通用户", "pwd": "secret123"})
    roles = {u["name"]: u["role"] for u in auth_core.list_users()}
    check("首个账号=管理员，第二个=普通用户",
          roles.get("隔离管理员") == "admin" and roles.get("隔离普通用户") == "user",
          str(roles))

    r = admin.post("/api/video_hub/api/favorites", headers=pub,
                   json={"video": {"webpage_url": "https://example.com/av",
                                   "title": "管理员的视频"}})
    check("管理员收藏成功", r.status_code == 200, r.text[:120])
    r = user.post("/api/video_hub/api/favorites", headers=pub,
                  json={"video": {"webpage_url": "https://example.com/uv",
                                  "title": "普通用户的视频"}})
    check("普通用户收藏成功", r.status_code == 200, r.text[:120])

    ta = [f["video"]["title"] for f in
          admin.get("/api/video_hub/api/favorites", headers=pub).json()["favorites"]]
    tu = [f["video"]["title"] for f in
          user.get("/api/video_hub/api/favorites", headers=pub).json()["favorites"]]
    check("管理员只看到自己的收藏", ta == ["管理员的视频"], str(ta))
    check("普通用户只看到自己的收藏", tu == ["普通用户的视频"], str(tu))

    admin_fid = admin.get("/api/video_hub/api/favorites", headers=pub).json()["favorites"][0]["id"]
    r = user.delete(f"/api/video_hub/api/favorites/{admin_fid}", headers=pub)
    check("普通用户删不掉管理员的收藏", r.status_code == 404, str(r.status_code))
    check("删后管理员收藏仍在",
          len(admin.get("/api/video_hub/api/favorites", headers=pub).json()["favorites"]) == 1)

    r = admin.post("/api/video_hub/api/history", headers=pub,
                   json={"video": {"webpage_url": "https://example.com/ah",
                                   "title": "管理员看的"}})
    check("管理员记历史成功", r.status_code == 200, r.text[:120])
    check("普通用户看不到管理员的历史",
          user.get("/api/video_hub/api/history", headers=pub).json()["history"] == [])
    r = user.delete("/api/video_hub/api/history", headers=pub)
    check("普通用户清空历史清不掉管理员的",
          r.status_code == 404 and
          len(admin.get("/api/video_hub/api/history", headers=pub).json()["history"]) == 1,
          f"{r.status_code}")

    r = admin.post("/api/music/playlists", headers=pub, json={"name": "管理员的歌单"})
    check("管理员建歌单成功", r.status_code == 200, r.text[:120])
    user.post("/api/music/playlists", headers=pub, json={"name": "普通用户的歌单"})
    pa = [p["name"] for p in admin.get("/api/music/playlists", headers=pub).json()["playlists"]]
    pu = [p["name"] for p in user.get("/api/music/playlists", headers=pub).json()["playlists"]]
    check("管理员只看到自己的歌单", pa == ["管理员的歌单"], str(pa))
    check("普通用户只看到自己的歌单", pu == ["普通用户的歌单"], str(pu))

    admin_pid = admin.get("/api/music/playlists", headers=pub).json()["playlists"][0]["id"]
    r = user.get(f"/api/music/playlists/{admin_pid}", headers=pub)
    check("普通用户打不开管理员的歌单详情", r.status_code == 404, str(r.status_code))
    r = user.delete(f"/api/music/playlists/{admin_pid}", headers=pub)
    check("普通用户删不掉管理员的歌单", r.status_code == 404, str(r.status_code))
    check("删后管理员歌单仍在",
          len(admin.get("/api/music/playlists", headers=pub).json()["playlists"]) == 1)

    section("HTTP 层：无 cookie（AI 工具调用）与管理员的全局数据同源")
    admin_pls = [p["name"] for p in
                 admin.get("/api/music/playlists", headers=pub).json()["playlists"]]
    ai_pls = [p["name"] for p in server.music_lib.list_playlists()]
    check("AI 侧歌单 = 管理员的歌单", ai_pls == admin_pls == ["管理员的歌单"], str(ai_pls))
    ai_favs = [f["video"]["title"] for f in server.video_fav_lib.list_all()["favorites"]]
    check("AI 侧收藏 = 管理员的收藏", ai_favs == ["管理员的视频"], str(ai_favs))
    check("普通用户的数据不在全局文件里", "普通用户的歌单" not in ai_pls)

    # 源头隔离：技能层不传 uid，身份由沙箱身份层（Actor contextvar）决定。
    # 这是真正的防线 —— 技能层铺身份传递必然漏调用点。
    section("源头层：不传 uid 时按当前执行者身份落文件")
    import asyncio

    import sandbox

    actor = sandbox.Actor(uid="u_src", role="user", sandbox=tmp / "sb_u_src")
    admin_actor = sandbox.Actor(uid="u_boss", role="admin", sandbox=ROOT / "data")
    rel = lambda p: str(Path(p).relative_to(tmp))  # noqa: E731

    token = sandbox.push(actor)
    try:
        check("普通用户身份下：歌单不传 uid → 落自己的目录",
              rel(user_store.user_file("music_playlists.json")) == "users/u_src/music_playlists.json",
              rel(user_store.user_file("music_playlists.json")))
        check("普通用户身份下：收藏不传 uid → 落自己的目录",
              "users/u_src" in rel(user_store.user_file("video_favorites.json")))
        check("普通用户身份下：历史不传 uid → 落自己的目录",
              "users/u_src" in rel(user_store.user_file("video_history.json")))
        # 技能层真的写进去（端到端：不传 uid 调用技能函数）
        music_lib.create_playlist("源头的歌单")
        video_fav_lib.add_favorite({"webpage_url": "https://example.com/src",
                                    "title": "源头的视频"})
        check("技能层写入落进该用户目录（不靠调用点传 uid）",
              "源头的歌单" in [p["name"] for p in music_lib.list_playlists()]
              and "源头的视频" in [f["video"]["title"]
                                    for f in video_fav_lib.list_all()["favorites"]],
              f"{rel(music_lib.user_store.user_file('music_playlists.json'))}")
    finally:
        sandbox.pop(token)

    check("身份撤销后回到全局文件（不污染）",
          rel(user_store.user_file("music_playlists.json")) == "global/music_playlists.json")

    token = sandbox.push(admin_actor)
    try:
        check("管理员身份下 → 仍走全局文件（既有数据零迁移）",
              rel(user_store.user_file("music_playlists.json")) == "global/music_playlists.json")
    finally:
        sandbox.pop(token)

    # 工具真实执行路径：harness.tool_thread 显式 copy_context 才能把身份带进工具线程
    from harness.tool_thread import run_in_tool_thread

    async def _in_tool_thread() -> str:
        token = sandbox.push(actor)
        try:
            return await run_in_tool_thread(
                lambda: rel(user_store.user_file("music_playlists.json")))
        finally:
            sandbox.pop(token)

    check("工具线程池内身份不丢（copy_context 生效）",
          "users/u_src" in asyncio.run(_in_tool_thread()))
    check("普通用户看不到管理员的歌单（源头隔离后）",
          "管理员的歌单" not in [p["name"] for p in music_lib.list_playlists(uid="u_src")])

    # 待办库：原来是技能目录里一份全局 tasks.json，普通用户能读到主人的经营待办，
    # 所以 todo_ 一直被沙箱拒用（sandbox._DENY_PREFIX）。改成按用户分目录后才放开。
    section("待办库按用户隔离（todo）")
    # 技能目录加进 sys.path 后按模块名导入 —— 与 harness 加载插件的方式一致
    sys.path.insert(0, str(ROOT / "skills" / "tasks"))
    import todo_impl

    token = sandbox.push(actor)
    try:
        svc = todo_impl._get_service()
        check("普通用户的待办落自己的目录",
              rel(svc._data_path) == "users/u_src/todo/tasks.json", rel(svc._data_path))
        todo_impl._do_create({"title": "乙的待办"})
        check("待办工具真的写进该用户目录",
              [t["title"] for t in svc.get_tasks()] == ["乙的待办"],
              str([t["title"] for t in svc.get_tasks()]))
    finally:
        sandbox.pop(token)

    other = sandbox.Actor(uid="u_other", role="user", sandbox=tmp / "sb_u_other")
    token = sandbox.push(other)
    try:
        svc2 = todo_impl._get_service()
        check("另一个用户的待办库是空的", svc2.get_tasks() == [],
              str([t["title"] for t in svc2.get_tasks()]))
        check("两个用户不是同一份文件",
              rel(svc2._data_path) == "users/u_other/todo/tasks.json",
              rel(svc2._data_path))
    finally:
        sandbox.pop(token)

    check("普通用户的待办没写进主人的全局 tasks.json",
          "乙的待办" not in (ROOT / "skills/tasks/data/tasks.json").read_text(encoding="utf-8"))
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
