#!/usr/bin/env python3
"""发行/更新体系的对抗测试。

这里测的不是「正常情况能不能跑通」，而是**坏情况能不能被挡住**：

  - 包被投毒，清单里塞进受保护路径   → 整包作废，且不做部分更新
  - 包内文件被改过，与清单哈希不符   → 拒绝
  - 包哈希与 .sha256 不符            → 拒绝
  - 更新之后，经历文件是否一个字节都没动
  - 坏了之后能不能回滚回去
  - 内嵌地板与 paths.py 有没有漂移

最后一条最关键：它证明的是「经历不会被覆盖」这个承诺，不是靠代码写得好，
而是靠一个可以被反复验证的断言。
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import importlib.util
import io
import json
import pytest
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REL = REPO / "deploy" / "release"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


update = _load("dabai_update_mod", REL / "update.py")
paths = _load("dabai_paths_mod", REL / "paths.py")
manifest_mod = _load("dabai_manifest_mod", REL / "manifest.py")
build_mod = _load("dabai_build_mod", REL / "build_release.py")


# ── 测试夹具 ─────────────────────────────────────────────────────────────
def make_package(
    tmp: Path,
    files: dict,
    version: str = "2.0.0",
    *,
    tamper: tuple | None = None,
    extra_manifest_paths: list | None = None,
):
    """造一个发行包。tamper=(路径, 新内容) 时包内内容与清单哈希故意不符。"""
    entries, blobs = [], {}
    for rel, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        blobs[rel] = data
        entries.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data), "mode": 0o644})
    for rel in (extra_manifest_paths or []):
        data = b"PWNED\n"
        blobs[rel] = data
        entries.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data), "mode": 0o644})
    entries.sort(key=lambda e: e["path"])
    man = {
        "schema": 1, "version": version, "built_at": "2026-01-01T00:00:00Z",
        "built_on": "test", "entry": "server.py", "commit": "0" * 40,
        "file_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "files": entries,
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel in sorted(blobs):
            data = blobs[rel]
            if tamper and rel == tamper[0]:
                data = tamper[1]
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o644
            tar.addfile(ti, io.BytesIO(data))
        blob = (json.dumps(man, ensure_ascii=False, indent=1) + "\n").encode()
        ti = tarfile.TarInfo("MANIFEST.json")
        ti.size, ti.mtime, ti.mode = len(blob), 0, 0o644
        tar.addfile(ti, io.BytesIO(blob))
    tar_path = tmp / f"dabai-{version}.tar.gz"
    with open(tar_path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (tmp / f"dabai-{version}.tar.gz.sha256").write_text(
        f"{digest}  dabai-{version}.tar.gz\n", encoding="utf-8")
    return tar_path, man, digest


EXPERIENCE = {
    "conviction.json": '{"convictions":[{"id":"c1","text":"从第一性原理出发"}]}',
    "long_horizon.json": '{"projects":[{"id":"dabai-core"}]}',
    "gene_stats.json": '{"g1":{"hits":7}}',
    "data/peer_inbox.jsonl": '{"from":"aliyun","text":"我在"}\n',
    "skills/tasks/data/tasks.json": '{"tasks":[{"id":"t1"}]}',
}


def make_instance(tmp: Path, version: str = "1.0.0"):
    root = tmp / "inst"
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text("OLD SERVER\n", encoding="utf-8")
    (root / "agent.py").write_text("OLD AGENT\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    for rel, content in EXPERIENCE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def snapshot(root: Path) -> dict:
    return {rel: (root / rel).read_bytes() for rel in EXPERIENCE}


def run_update(args, timeout: int = 180):
    p = subprocess.run(
        [sys.executable, str(REL / "update.py")] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, (p.stdout + p.stderr)


def apply_args(root: Path, state: Path, tar: Path):
    return ["--root", str(root), "--state", str(state),
            "--local-tarball", str(tar), "--apply", "--no-restart"]


# ── 契约不漂移 ───────────────────────────────────────────────────────────
def test_floor_matches_paths():
    """update.py 内嵌地板必须与 paths.py 的 FLOOR_GLOBS 完全一致。

    两份清单是有意重复的（更新器不能依赖被更新的仓库），但重复就必须有断言兜住，
    否则一边加了保护、另一边没加，缺口会安静地存在很久。
    """
    assert set(update.FLOOR_GLOBS) == set(paths.FLOOR_GLOBS), (
        "地板漂移了："
        f"只在 update.py 里 {sorted(set(update.FLOOR_GLOBS) - set(paths.FLOOR_GLOBS))}，"
        f"只在 paths.py 里 {sorted(set(paths.FLOOR_GLOBS) - set(update.FLOOR_GLOBS))}"
    )


def test_packed_assets_match():
    """受管资产白名单同样两份，必须不漂移。

    放行面比禁写面更危险：一边放行、另一边照旧拒写，模型要么进不了包、
    要么装不上，而且不会有任何报错 —— 装完只是 3D 角色空的。
    """
    only_update = sorted(set(update.PACKED_ASSETS) - set(paths.PACKED_ASSETS))
    only_paths = sorted(set(paths.PACKED_ASSETS) - set(update.PACKED_ASSETS))
    assert not only_update and not only_paths, (
        f"受管资产漂移了：只在 update.py 里 {only_update}，只在 paths.py 里 {only_paths}"
    )


def test_packed_assets_pass_floor():
    """点名放行的资产两边都放行；同类目录里没点名的仍被地板拦住。"""
    for rel in paths.PACKED_ASSETS:
        assert paths.classify(rel) == paths.CODE, rel
        assert not paths.floor_violation(rel), rel
        assert not update.is_forbidden(rel), rel
    for rel in ("models/别的角色.vrm", "models/白头凤_draco3.vrm",
                "backgrounds/太空飞船走廊.glb"):
        assert paths.classify(rel) == paths.LOCAL, rel
        assert paths.floor_violation(rel), rel
        assert update.is_forbidden(rel), rel


def test_validators_agree():
    """update.py 自带校验器与 manifest.py 的判定必须一致。

    两份实现是有意重复的（更新器不能依赖被更新的仓库），重复就必须有断言兜住 ——
    否则哪天只在一边加了规则，另一边的判定会安静地松掉。
    """
    good = {"schema": 1, "version": "1.0.0", "built_at": "x", "built_on": "y",
            "entry": "server.py",
            "files": [{"path": "a.py", "sha256": "a" * 64, "size": 1, "mode": 420}]}
    cases = [
        good,
        {**good, "schema": 99},
        {**good, "files": []},
        {**good, "files": [{"path": "../etc/passwd", "sha256": "a" * 64, "size": 1, "mode": 420}]},
        {**good, "files": [{"path": "/abs.py", "sha256": "a" * 64, "size": 1, "mode": 420}]},
        {**good, "files": [{"path": "a.py", "sha256": "short", "size": 1, "mode": 420}]},
        {**good, "files": [{"path": "a.py", "sha256": "a" * 64, "size": 1, "mode": 420},
                           {"path": "a.py", "sha256": "a" * 64, "size": 1, "mode": 420}]},
        {**good, "files": ["not-a-dict"]},
        {"version": "1.0.0"},
    ]
    for c in cases:
        a = bool(update.validate_manifest(c))
        b = bool(manifest_mod.validate_manifest(c))
        assert a == b, f"两份校验器判定不一致：{c}  update={a} manifest={b}"


def test_hidden_file_keeps_leading_dot():
    """lstrip('./') 会把 .gitattributes 吃成 gitattributes —— 回归断言。"""
    assert manifest_mod.norm_rel(".gitattributes") == ".gitattributes"
    assert manifest_mod.norm_rel("./.gitignore") == ".gitignore"
    assert manifest_mod.norm_rel("./a/b.py") == "a/b.py"


def test_forbidden_covers_ancestors_and_future_paths():
    assert update.is_forbidden("data")
    assert update.is_forbidden("data/any/new/path.json")      # 未来新增也要拦
    assert update.is_forbidden("venv/bin/python")
    assert update.is_forbidden("conviction.json")
    assert update.is_forbidden("skills/tasks/data/tasks.json")
    assert not update.is_forbidden("agent.py")
    assert not update.is_forbidden("deploy/release/update.py")


# ── 正常路径 ─────────────────────────────────────────────────────────────
def test_update_writes_code_and_spares_experience(tmp_path):
    root = make_instance(tmp_path)
    before = snapshot(root)
    tar, _man, _d = make_package(tmp_path, {
        "server.py": "NEW SERVER\n",
        "agent.py": "NEW AGENT\n",
        "VERSION": "2.0.0\n",
    })
    rc, out = run_update(apply_args(root, tmp_path / "state", tar))
    assert rc == 0, out
    assert (root / "server.py").read_text() == "NEW SERVER\n"
    assert (root / "agent.py").read_text() == "NEW AGENT\n"
    for rel, data in before.items():
        assert (root / rel).read_bytes() == data, f"经历文件被动了：{rel}"


def test_dry_run_writes_nothing(tmp_path):
    root = make_instance(tmp_path)
    before = snapshot(root)
    tar, _man, _d = make_package(tmp_path, {"server.py": "NEW SERVER\n"})
    rc, out = run_update(["--root", str(root), "--state", str(tmp_path / "state"),
                          "--local-tarball", str(tar), "--dry-run"])
    assert rc == 0, out
    assert (root / "server.py").read_text() == "OLD SERVER\n"
    assert snapshot(root) == before


# ── 坏情况必须被挡住 ─────────────────────────────────────────────────────
def test_refuses_package_declaring_protected_path(tmp_path):
    """投毒包：清单里塞 conviction.json。必须整包作废，不做部分更新。"""
    root = make_instance(tmp_path)
    before = snapshot(root)
    tar, _man, _d = make_package(
        tmp_path, {"server.py": "NEW SERVER\n"},
        extra_manifest_paths=["conviction.json"])
    rc, out = run_update(apply_args(root, tmp_path / "state", tar))
    assert rc != 0, out
    assert "受保护路径" in out or "整包作废" in out, out
    assert (root / "conviction.json").read_bytes() == before["conviction.json"]
    assert (root / "server.py").read_text() == "OLD SERVER\n", "整包拒绝后不该有任何文件被改"


def test_refuses_tampered_file(tmp_path):
    """包内内容与清单哈希不符 → 拒绝，且不写盘。"""
    root = make_instance(tmp_path)
    tar, _man, _d = make_package(
        tmp_path, {"server.py": "NEW SERVER\n"},
        tamper=("server.py", b"EVIL SERVER\n"))
    rc, out = run_update(apply_args(root, tmp_path / "state", tar))
    assert rc != 0, out
    assert "不符" in out, out
    assert (root / "server.py").read_text() == "OLD SERVER\n"


def test_refuses_wrong_package_hash(tmp_path):
    root = make_instance(tmp_path)
    tar, _man, _d = make_package(tmp_path, {"server.py": "NEW SERVER\n"})
    (tmp_path / "dabai-2.0.0.tar.gz.sha256").write_text("0" * 64 + "  x\n", encoding="utf-8")
    rc, out = run_update(apply_args(root, tmp_path / "state", tar))
    assert rc != 0, out
    assert "包哈希不符" in out, out
    assert (root / "server.py").read_text() == "OLD SERVER\n"


def test_refuses_downgrade_without_force(tmp_path):
    root = make_instance(tmp_path, version="9.0.0")
    tar, _man, _d = make_package(tmp_path, {"server.py": "NEW SERVER\n"}, version="2.0.0")
    rc, out = run_update(apply_args(root, tmp_path / "state", tar))
    assert rc == 0, out
    assert "跳过" in out, out
    assert (root / "server.py").read_text() == "OLD SERVER\n"


# ── 回滚 ─────────────────────────────────────────────────────────────────
def test_rollback_restores_previous_and_spares_experience(tmp_path):
    root = make_instance(tmp_path)
    before = snapshot(root)
    state = tmp_path / "state"
    tar, _man, _d = make_package(tmp_path, {
        "server.py": "NEW SERVER\n", "agent.py": "NEW AGENT\n"})
    rc, out = run_update(apply_args(root, state, tar))
    assert rc == 0, out
    assert (root / "server.py").read_text() == "NEW SERVER\n"

    rc, out = run_update(["--root", str(root), "--state", str(state),
                          "--rollback", "--no-restart"])
    assert rc == 0, out
    assert (root / "server.py").read_text() == "OLD SERVER\n"
    assert (root / "agent.py").read_text() == "OLD AGENT\n"
    assert snapshot(root) == before


# ── 真实仓库端到端 ───────────────────────────────────────────────────────
def test_real_repo_package_has_no_protected_path(tmp_path):
    """在真仓库上打一次包，断言：能打出来、包内无受保护路径、经历文件不在包里。"""
    p = subprocess.run(
        [sys.executable, str(REL / "build_release.py"), "--out", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=900)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "解包回验通过" in p.stdout, p.stdout

    tars = sorted(tmp_path.glob("dabai-*.tar.gz"))
    assert tars, "没打出包"
    with tarfile.open(tars[0], "r:gz") as tar:
        names = [manifest_mod.norm_rel(m.name) for m in tar.getmembers()]
    for rel in EXPERIENCE:
        assert rel not in names, f"经历文件进了包：{rel}"
    bad = [n for n in names if paths.is_protected(n) and n != "MANIFEST.json"]
    assert not bad, f"包内出现受保护路径：{bad[:5]}"


# ── import 完整性：包内代码 import 的本地模块必须在包里 ──────────────────
def test_import_gap_is_detected(tmp_path):
    """模块没进包时必须报出来。

    这正是 auth_core / peer_mesh / turn_quota 那次事故的形态：server.py 逐个
    import 它们，三个文件却从未入仓，打包器一声不响。
    """
    (tmp_path / "server.py").write_text("import auth_core\nimport json\n", encoding="utf-8")
    (tmp_path / "auth_core.py").write_text("x = 1\n", encoding="utf-8")
    pairs = [("server.py", tmp_path / "server.py")]
    gaps = build_mod.local_import_gaps(tmp_path, pairs)
    assert gaps, "缺模块竟然没报出来"
    assert "auth_core" in gaps[0]

    pairs.append(("auth_core.py", tmp_path / "auth_core.py"))
    assert build_mod.local_import_gaps(tmp_path, pairs) == []


def test_third_party_imports_are_not_flagged(tmp_path):
    """第三方库不能被误报成缺口 —— 否则这条检查天天红，等于没有。"""
    (tmp_path / "server.py").write_text(
        "import os\nimport fastapi\nfrom pathlib import Path\n", encoding="utf-8")
    pairs = [("server.py", tmp_path / "server.py")]
    assert build_mod.local_import_gaps(tmp_path, pairs) == []


def test_real_repo_has_no_import_gaps():
    """真仓库不许有缺口：以后新增模块忘了 git add，这条测试会红。"""
    pairs, _missing, _excluded = build_mod.collect(REPO)
    gaps = build_mod.local_import_gaps(REPO, pairs)
    assert not gaps, "有模块被 import 但没入仓：\n" + "\n".join(gaps)


# ── 前端引用完整性：包内前端引的本地资源必须在包里 ──────────────────────
def test_frontend_gap_is_detected(tmp_path):
    """TS import / HTML 与 manifest 引用指向仓外文件时必须报出来。

    事故形态：web/app.ts:54 import ./js/ui/42_attach.ts，web/index.html:13/875
    引 /manifest.webmanifest 与 /sw.js —— 三个文件都在仓外，包里的前端 import
    直接 404、PWA 整体失效。同一个洞，Python 侧有 local_import_gaps 兜，前端
    侧当时一个都没有。
    """
    (tmp_path / "web" / "js" / "ui").mkdir(parents=True)
    (tmp_path / "web" / "app.ts").write_text(
        "import init_42_attach from './js/ui/42_attach.ts';\n", encoding="utf-8")
    (tmp_path / "web" / "js" / "ui" / "42_attach.ts").write_text("export default 1;\n", encoding="utf-8")
    (tmp_path / "web" / "index.html").write_text(
        '<link rel="manifest" href="/manifest.webmanifest">\n'
        '<script src="/static/app.ts?v=173"></script>\n'
        "<script>navigator.serviceWorker.register('/sw.js', { scope: '/' });</script>\n",
        encoding="utf-8")
    (tmp_path / "web" / "manifest.webmanifest").write_text(
        '{"icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]}\n', encoding="utf-8")
    (tmp_path / "web" / "sw.js").write_text("self.addEventListener('fetch', () => {});\n", encoding="utf-8")
    (tmp_path / "web" / "icons").mkdir()
    (tmp_path / "web" / "icons" / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    pairs = [
        ("web/app.ts", tmp_path / "web" / "app.ts"),
        ("web/index.html", tmp_path / "web" / "index.html"),
    ]
    gaps, soft = build_mod.frontend_gap(tmp_path, pairs)
    assert any("42_attach.ts" in g for g in gaps), gaps
    assert any("manifest.webmanifest" in g for g in gaps), gaps
    assert any("/sw.js" in g for g in gaps), gaps
    assert not soft, soft

    # 补齐入仓后，同一批引用必须一条不剩（manifest 的 icons 也算引用）
    for rel in ("web/js/ui/42_attach.ts", "web/manifest.webmanifest", "web/sw.js",
                "web/icons/icon-192.png"):
        pairs.append((rel, tmp_path / rel))
    assert build_mod.frontend_gap(tmp_path, pairs) == ([], [])


def test_frontend_manifest_icons_are_checked(tmp_path):
    """manifest 的 icons[].src 也要查 —— 图标缺失时 PWA 是白框，不报错。"""
    (tmp_path / "web" / "icons").mkdir(parents=True)
    (tmp_path / "web" / "manifest.webmanifest").write_text(
        '{"icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]}\n', encoding="utf-8")
    (tmp_path / "web" / "icons" / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    pairs = [("web/manifest.webmanifest", tmp_path / "web" / "manifest.webmanifest")]
    gaps, _soft = build_mod.frontend_gap(tmp_path, pairs)
    assert gaps and "icon-192.png" in gaps[0], gaps


def test_frontend_external_refs_are_not_flagged(tmp_path):
    """外链、运行时端点、锚点不能误报 —— 它们本来就不在磁盘上。"""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text(
        '<script src="https://cdn.example.com/three.js"></script>\n'
        '<link href="//fonts.example.com/x.css" rel="stylesheet">\n'
        '<a href="#top">顶</a>\n'
        '<img src="/api/avatar/me">\n'
        '<link href="/static/style.css">\n', encoding="utf-8")
    pairs = [("web/index.html", tmp_path / "web" / "index.html")]
    assert build_mod.frontend_gap(tmp_path, pairs) == ([], [])



def test_real_repo_has_no_frontend_gaps():
    """真仓库不许有前端缺口：以后新增 .ts 忘了 git add，这条测试会红。"""
    pairs, _missing, _excluded = build_mod.collect(REPO)
    gaps, _soft = build_mod.frontend_gap(REPO, pairs)
    assert not gaps, "有前端资源被引用但没入仓：\n" + "\n".join(gaps)


def test_vendor_is_packaged():
    """web/vendor 必须进包：index.html:27-29 的 importmap 指向它。

    它曾是 paths.py 里的「本机私有」（只提示、不拦打包），结果新机器装完
    3D 前端直接 404。这条测试锁住这个结论：谁把 web/vendor/ 加回 .gitignore
    或 paths.py 的忽略表，它就红。
    """
    pairs, _missing, _excluded = build_mod.collect(REPO)
    packaged = {rel for rel, _ in pairs}
    for target in (
        "web/vendor/three/build/three.module.js",
        "web/vendor/three-vrm/lib/three-vrm.module.min.js",
    ):
        assert target in packaged, f"{target} 没进包，importmap 会 404"
    hard, soft = build_mod.frontend_gap(REPO, pairs)
    assert not [g for g in hard + soft if "web/vendor" in g]




def test_release_sha256_is_file_hash_and_updater_accepts_it(tmp_path):
    """真产物喂真更新器：.sha256 语义两端必须对得上。

    这条曾经真的错了：build_release 把 gzip 前的 tar 内容哈希写进 .sha256，
    而 update.py 下载后算的是文件哈希 —— 两个不同的对象，永远不可能相等。
    症状极隐蔽：打包成功、解包回验通过、测试套全绿，发布后每台机器都在第一步
    「包哈希不符」拒绝更新。原因是测试夹具自己用的就是文件哈希，全绿恰恰掩盖了
    生产端的错 —— 两端各自自洽，接口对不上。
    """
    out = tmp_path / "dist"
    p = subprocess.run(
        [sys.executable, str(REL / "build_release.py"), "--out", str(out)],
        cwd=str(REPO), capture_output=True, text=True, timeout=900)
    assert p.returncode == 0, p.stdout + p.stderr

    version = build_mod.read_version(REPO)
    tar = out / f"dabai-{version}.tar.gz"
    sha_file = out / f"dabai-{version}.tar.gz.sha256"
    assert tar.is_file() and sha_file.is_file(), sorted(x.name for x in out.iterdir())

    want = update.parse_sha256_file(sha_file.read_text(encoding="utf-8"))
    file_hash = hashlib.sha256(tar.read_bytes()).hexdigest()
    assert want, ".sha256 里读不出合法哈希"
    assert want == file_hash, (
        f".sha256 里不是 .tar.gz 文件哈希：文件写 {want[:16]}…，"
        f"更新器算的是 {file_hash[:16]}… —— 每台机器都会在第一步拒绝更新")

    with gzip.open(tar, "rb") as gz:
        content_hash = hashlib.sha256(gz.read()).hexdigest()
    assert content_hash != file_hash, "夹具假设坏了：这两个哈希本该不同"
    assert want != content_hash, "又写回 gzip 前的内容哈希了"

    # 光靠上面的算术不够：让消费端自己说这包能过校验
    root = make_instance(tmp_path, version="0.9.0")
    rc, o = run_update(["--root", str(root), "--state", str(tmp_path / "state"),
                        "--local-tarball", str(tar), "--dry-run"], timeout=600)
    assert rc == 0, o
    assert "包哈希校验通过" in o, o


# ── workflow 自身的一致性：上传的产物要覆盖它后续要读的文件 ──────────────
def test_workflow_artifact_covers_every_dist_file_it_reads():
    """release.yml 上传的产物必须覆盖它后续要读的 dist 文件。

    首次真实发布就是这么挂的：upload-artifact 只收 dist/dabai-*，而 publish 读
    dist/MANIFEST.json（不带版本前缀，不匹配该模式）→ FileNotFoundError，
    release 建不出来，而 build job 全绿、日志里一点征兆都没有。
    这类「两个步骤各自都对、接口对不上」的洞本地跑不到，只能靠静态断言。
    """
    yml = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    # 取 upload-artifact 块（name → if-no-files-found 之间），里面的 dist/... 就是上传范围
    m = re.search(r"name:[ \t]*dabai-package(.*?)if-no-files-found", yml, re.S)
    assert m, "读不出 upload-artifact 块"
    pats = re.findall(r"dist/[A-Za-z0-9_.*-]+", m.group(1))
    assert pats, "上传范围是空的"

    refs = set(re.findall(r"dist/[A-Za-z0-9_.*-]+", yml))
    assert refs, "workflow 里没引用任何 dist 文件？"
    for r in sorted(refs):
        assert any(fnmatch.fnmatch(r, p) for p in pats), (
            f"{r} 被 workflow 引用，却不在 upload-artifact 的上传范围 {pats} 内 —— "
            "发布时会 FileNotFoundError")


def test_update_switches_to_named_tag(tmp_path, monkeypatch):
    """--tag 必须走 releases/tags 端点，且允许降级。

    版本切换的用法就是「退回一个已知可用的旧版」，而版本判定默认只升不降 ——
    不显式豁免，--tag v1.0.0 会被当成「不比本地新」直接跳过：命令返回 0、
    什么都没做，看起来像成功了。
    """
    root = make_instance(tmp_path, version="2.0.0")
    tar, _man, _d = make_package(tmp_path, {"server.py": "NEW SERVER\n"}, version="1.0.0")
    sha_text = f"{hashlib.sha256(tar.read_bytes()).hexdigest()}  dabai-1.0.0.tar.gz\n"
    seen = []

    def fake_gh(url, token, timeout, raw=False):
        seen.append(url)
        if raw:
            return sha_text.encode()
        return {"tag_name": "v1.0.0", "assets": [
            {"name": "dabai-1.0.0.tar.gz", "url": "https://api.github.com/asset/tar"},
            {"name": "dabai-1.0.0.tar.gz.sha256", "url": "https://api.github.com/asset/sha"},
        ]}

    monkeypatch.setattr(update, "gh_request", fake_gh)
    monkeypatch.setattr(update, "get_token", lambda: "fake")
    monkeypatch.setattr(update, "download",
                        lambda url, dest, token, timeout, **kw: shutil.copyfile(tar, dest))

    args = argparse.Namespace(
        root=str(root), state=str(tmp_path / "state"), repo="o/r", service="", port="",
        check=False, dry_run=True, apply=False, rollback=False, tag="v1.0.0",
        local_tarball="", local_manifest="", force=False, prune=False,
        no_restart=True, ignore_active_turn=False, keep_backups=0)
    rc = update.run(args)
    assert rc == 0, rc
    assert any("/releases/tags/v1.0.0" in u for u in seen), seen
    assert not any(u.endswith("/releases/latest") for u in seen), seen


def test_stale_updater_copy_is_detected(tmp_path):
    """更新器副本落后必须查得出来 —— 它是「装了新版但新能力用不了」的唯一征兆。

    更新器跑在仓库之外（仓库正是被更新的对象），所以它更新不了自己：
    装发行版只刷新仓库里那份，systemd 跑的是 /usr/local/lib/dabai-update/ 的副本。
    v1.0.0 的包里没有 --tag，装了它的机器反而切不了版本，且全程没有任何提示。
    """
    root = tmp_path / "repo"
    packaged = root / "deploy" / "release" / "update.py"
    packaged.parent.mkdir(parents=True)
    packaged.write_text("# v1.0.1，带 --tag\n", encoding="utf-8")

    # 直接跑仓库里那份（开发/演练）→ 不是副本，没什么可比的
    assert update.updater_copy_stale(root, me=packaged) is None

    # 副本已同步 → 内容相同，不该报警
    synced = tmp_path / "usr-local" / "update.py"
    synced.parent.mkdir(parents=True)
    shutil.copyfile(packaged, synced)
    assert update.updater_copy_stale(root, me=synced) is None

    # 副本落后（装着 v1.0.0 那版）→ 必须查出来，且提示里给出重装命令
    synced.write_text("# v1.0.0，没有 --tag\n", encoding="utf-8")
    stale = update.updater_copy_stale(root, me=synced)
    assert stale is not None
    assert stale[0] == synced.resolve() and stale[1] == packaged.resolve()
    note = "\n".join(update.stale_updater_note(stale))
    assert "install-update.sh" in note
    assert update.stale_updater_note(None) == []

    # 仓库里那份不存在（更新器被单独部署）→ 无从比较，不误报
    packaged.unlink()
    assert update.updater_copy_stale(root, me=synced) is None


def test_download_retries_after_bad_cdn_ip(tmp_path, monkeypatch):
    """撞上无响应的 CDN 地址要重试，且单次尝试不许吃满 HTTP_TIMEOUT。

    release-assets.githubusercontent.com 解析出 4 个 IP，其中 185.199.111.133
    连 443 无响应。单次尝试撞上它就白等到 HTTP_TIMEOUT 见底（60s），
    整轮更新失败 —— 而其余三个地址 0.1~1.2s 就通。
    """
    seen = []

    def flaky(url, token, timeout, raw=False):
        seen.append(timeout)
        if len(seen) < 3:
            raise TimeoutError("连接 release-assets 超时")
        return b"tarball-bytes"

    monkeypatch.setattr(update, "gh_request", flaky)
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    retried = []
    dest = tmp_path / "dabai-1.0.2.tar.gz"
    update.download("https://api.github.com/asset/tar", dest, "tok", 60,
                    on_retry=lambda n, ex: retried.append(n))

    assert dest.read_bytes() == b"tarball-bytes"
    assert retried == [1, 2]
    # 关键断言：单次尝试的超时被压到 ATTEMPT_TIMEOUT，而不是传进来的 60
    assert seen == [update.DOWNLOAD_ATTEMPT_TIMEOUT] * 3
    assert not list(tmp_path.glob("*.part"))


def test_download_gives_up_loudly(tmp_path, monkeypatch):
    """重试用尽要抛错，且不留半截文件 —— 半截文件会被下游当成完整包去校验。"""

    def dead(url, token, timeout, raw=False):
        raise TimeoutError("连接超时")

    monkeypatch.setattr(update, "gh_request", dead)
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    dest = tmp_path / "dabai-1.0.2.tar.gz"
    with pytest.raises(RuntimeError) as ei:
        update.download("https://api.github.com/asset/tar", dest, "tok", 60)

    assert "3 次" in str(ei.value)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))


def test_download_failure_exits_cleanly(tmp_path, monkeypatch):
    """下载彻底失败时 run() 返回 1 并写明原因，不是把栈扔给 systemd。"""
    root = make_instance(tmp_path, version="1.0.0")
    sha_text = f"{'a' * 64}  dabai-1.0.2.tar.gz\n"

    def fake_gh(url, token, timeout, raw=False):
        if raw:
            return sha_text.encode()
        return {"tag_name": "v1.0.2", "assets": [
            {"name": "dabai-1.0.2.tar.gz", "url": "https://api.github.com/asset/tar"},
            {"name": "dabai-1.0.2.tar.gz.sha256", "url": "https://api.github.com/asset/sha"},
        ]}

    monkeypatch.setattr(update, "gh_request", fake_gh)
    monkeypatch.setattr(update, "get_token", lambda: "fake")

    def dead(url, dest, token, timeout, **kw):
        raise RuntimeError("下载失败（已尝试 3 次）：连接 release-assets 超时")

    monkeypatch.setattr(update, "download", dead)
    args = argparse.Namespace(
        root=str(root), state=str(tmp_path / "state"), repo="o/r", service="", port="",
        check=False, dry_run=True, apply=False, rollback=False, tag="",
        local_tarball="", local_manifest="", force=True, prune=False,
        no_restart=True, ignore_active_turn=False, keep_backups=0)
    rc = update.run(args)
    assert rc == 1, rc
