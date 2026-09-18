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

import gzip
import hashlib
import importlib.util
import io
import json
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
