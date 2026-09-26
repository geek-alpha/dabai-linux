"""长期事业台账的退役路径：archive / unarchive / archived。

为什么值得单独测：退役是「清掉过期事业」的唯一入口，两种错都致命——
① 判错把还活着的线扫出去（任务链断在半路）；
② 退役了却还从活跃面漏回来（提示词注入 / 自我迭代选题池仍看得见），
   那等于没清，而台账会越长越假。
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mod(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "lh_under_test", ROOT / "tools" / "long_horizon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FILE", tmp_path / "long_horizon.json")
    return mod


def _seed(mod, projects, archived=None):
    mod.FILE.write_text(json.dumps(
        {"projects": projects, "questions": [], "archived": archived or []},
        ensure_ascii=False), encoding="utf-8")


def _proj(pid, logs=("做了一件事",)):
    return {"id": pid, "title": f"标题-{pid}", "why": "w", "value": "v",
            "done_when": "d", "stage": "active", "progress": 0, "next": "n",
            "created": "2026-09-01",
            "log": [{"t": "2026-09-20", "what": x, "ev": "证据"} for x in logs]}


def _run(mod, monkeypatch, argv, capsys):
    monkeypatch.setattr(sys, "argv", ["long_horizon.py"] + argv)
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_archive_moves_project_out_and_keeps_its_log(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("dead", ["第一件", "第二件"]), _proj("alive")])

    rc, out = _run(mod, monkeypatch, ["archive", "dead", "--why", "主人 09-26 已砍"], capsys)

    assert rc == 0 and "已退役" in out
    d = json.loads(mod.FILE.read_text(encoding="utf-8"))
    assert [p["id"] for p in d["projects"]] == ["alive"]
    got = d["archived"][0]
    assert got["id"] == "dead"
    assert len(got["log"]) == 2, "log 必须整段保留——退役不是删除"
    assert got["archive_why"] == "主人 09-26 已砍"
    assert got["archived_at"]


def test_archived_project_disappears_from_active_list(tmp_path, monkeypatch, capsys):
    """反证：归档若没让项目从活跃面消失，这次清理就是假的。"""
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("dead"), _proj("alive")])

    _run(mod, monkeypatch, ["archive", "dead", "--why", "过期"], capsys)
    rc, out = _run(mod, monkeypatch, ["list"], capsys)

    assert rc == 0
    assert "alive" in out
    assert "dead" not in out
    # 注入端与选题池读的是同一个 projects 字段
    assert [p["id"] for p in mod.load()["projects"]] == ["alive"]


def test_archive_is_idempotent(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("dead")])

    _run(mod, monkeypatch, ["archive", "dead", "--why", "过期"], capsys)
    rc, out = _run(mod, monkeypatch, ["archive", "dead", "--why", "再点一次"], capsys)

    assert rc == 0 and "已经在归档区" in out
    d = json.loads(mod.FILE.read_text(encoding="utf-8"))
    assert len(d["archived"]) == 1, "重复归档不能进两条"
    assert d["archived"][0]["archive_why"] == "过期"


def test_archive_unknown_id_is_refused(tmp_path, monkeypatch, capsys):
    """反证：不存在的 id 必须拒绝，且一个字都不许写进文件。"""
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("alive")])
    before = mod.FILE.read_text(encoding="utf-8")

    rc, out = _run(mod, monkeypatch, ["archive", "typo", "--why", "x"], capsys)

    assert rc == 1 and "没有这个项目" in out
    assert mod.FILE.read_text(encoding="utf-8") == before


def test_unarchive_restores_and_clears_marks(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("dead")])
    _run(mod, monkeypatch, ["archive", "dead", "--why", "过期"], capsys)

    rc, out = _run(mod, monkeypatch, ["unarchive", "dead"], capsys)

    assert rc == 0 and "已还原" in out
    d = json.loads(mod.FILE.read_text(encoding="utf-8"))
    assert d["archived"] == []
    assert [p["id"] for p in d["projects"]] == ["dead"]
    assert "archived_at" not in d["projects"][0]
    assert "archive_why" not in d["projects"][0]


def test_show_and_archived_surface_the_reason(tmp_path, monkeypatch, capsys):
    """退役理由必须查得到——不然以后没人知道这条线为什么不做了。"""
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [_proj("dead")])
    _run(mod, monkeypatch, ["archive", "dead", "--why", "目录已删"], capsys)

    _, out = _run(mod, monkeypatch, ["archived"], capsys)
    assert "dead" in out and "目录已删" in out

    _, out = _run(mod, monkeypatch, ["show", "dead"], capsys)
    assert "已退役" in out and "目录已删" in out


def test_archived_empty_is_not_an_error(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    _seed(mod, [])
    rc, out = _run(mod, monkeypatch, ["archived"], capsys)
    assert rc == 0 and "空" in out


def test_legacy_file_without_archived_key_still_loads(tmp_path, monkeypatch):
    """老台账没有 archived 字段（现存 26 条就是这样），不许 KeyError。"""
    mod = _mod(tmp_path, monkeypatch)
    mod.FILE.write_text(json.dumps({"projects": [_proj("a")]}, ensure_ascii=False),
                        encoding="utf-8")

    d = mod.load()

    assert d["archived"] == []
    assert d["questions"] == []
