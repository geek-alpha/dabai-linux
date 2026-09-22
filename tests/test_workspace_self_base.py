# -*- coding: utf-8 -*-
"""workspace_* 自请求基址必须跟随 server 的真实监听地址。

为什么钉死：nginx 前置 TLS 终结模式下（settings.json harness.http_only=true）server
只监听回源端口 8001，本机 8000 无人监听。硬编码 https://127.0.0.1:8000 会让工作区面板
的全部工具静默连不上 —— 实测 09-22 workspace_get 报 Cannot connect to host 127.0.0.1:8000。
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "code_ops"


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.syspath_prepend(str(SKILL_DIR))
    monkeypatch.delenv("DABAI_SELF_BASE", raising=False)
    monkeypatch.delenv("DABAI_HTTP_ONLY", raising=False)
    sys.modules.pop("workspace_impl", None)
    return importlib.import_module("workspace_impl")


def _settings(tmp_path, http_only):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"harness": {"http_only": http_only}}), encoding="utf-8")
    return p


def test_env_wins_and_trailing_slash_stripped(mod, monkeypatch):
    """server 启动时写入的 env 是唯一真相，优先于一切推导。"""
    monkeypatch.setenv("DABAI_SELF_BASE", "http://127.0.0.1:9999/")
    assert mod._server_base() == "http://127.0.0.1:9999"


def test_http_only_uses_relay_port(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_SETTINGS_PATH", _settings(tmp_path, True))
    assert mod._server_base() == "http://127.0.0.1:8001"


def test_https_mode_uses_8000(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_SETTINGS_PATH", _settings(tmp_path, False))
    assert mod._server_base() == "https://127.0.0.1:8000"


def test_env_var_forces_http_only(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("DABAI_HTTP_ONLY", "1")
    monkeypatch.setattr(mod, "_SETTINGS_PATH", _settings(tmp_path, False))
    assert mod._server_base() == "http://127.0.0.1:8001"


def test_missing_settings_defaults_to_https(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_SETTINGS_PATH", tmp_path / "nope.json")
    assert mod._server_base() == "https://127.0.0.1:8000"
