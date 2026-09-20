"""全新机器首次自举密钥：并发生成不能崩，也不能各拿一把。

真实翻车（本轮复现）：survey() 用 ThreadPoolExecutor 并发探三台，三个线程同时
发现 cluster.key 不存在、同时生成，并同时写同一个 tmp 名 —— 先落盘的那个
os.replace 把 tmp 移走，剩下的 chmod 打在空气上，FileNotFoundError 直接崩。
"""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("peer_mesh_key_under_test",
                                                  BASE / "peer_mesh.py")
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    monkeypatch.setattr(pm, "KEY_FILE", tmp_path / "cluster.key")
    return pm


def test_concurrent_key_bootstrap_agrees(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as ex:
        keys = list(ex.map(lambda _: pm.cluster_key(), range(8)))
    assert len(set(keys)) == 1, "并发自举拿到了不同的密钥，落选那些会让请求全变 403"
    on_disk = pm.KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
    assert keys[0] == on_disk


def test_key_bootstrap_leaves_no_tmp(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    pm.cluster_key()
    assert [p.name for p in tmp_path.iterdir()] == ["cluster.key"]
