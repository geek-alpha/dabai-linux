# -*- coding: utf-8 -*-
"""MCP 配置里的凭据引用：servers.json 只写 ${file:...} / ${env:...}，真值不进仓库。

背景（2026-09-25）：bambu 三个 server 的 access_code / 内网 IP / 序列号以明文躺在
skills/mcp/servers.json 里，而那个文件被 git 跟踪 —— secretscan 只认通用密钥模式，
BAMBU_ACCESS_CODE 这类自定义变量名会静默放行，明文凭据直接进 public 仓库。
"""
import json
import os
import re
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "skills", "mcp"))

import mcp_client as mc  # noqa: E402


@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bambu_lab.json").write_text(json.dumps({
        "host": "10.0.0.5",
        "access_code": "12345678",
        "mcp": {"model": "a1"},
    }), encoding="utf-8")
    monkeypatch.setattr(mc, "_ROOT", str(tmp_path))
    return tmp_path


def test_file_ref_expands(fake_root):
    cfg = {"env": {"IP": "${file:data/bambu_lab.json#host}",
                   "CODE": "${file:data/bambu_lab.json#access_code}",
                   "MODEL": "${file:data/bambu_lab.json#mcp.model}"}}
    assert mc._expand_refs(cfg)["env"] == {
        "IP": "10.0.0.5", "CODE": "12345678", "MODEL": "a1"}


def test_env_ref_expands(monkeypatch):
    monkeypatch.setenv("DABAI_TEST_TOKEN", "s3cret")
    assert mc._expand_refs("${env:DABAI_TEST_TOKEN}") == "s3cret"


def test_missing_ref_raises(fake_root):
    with pytest.raises(mc.MCPError):
        mc._expand_refs("${file:data/bambu_lab.json#nope}")
    with pytest.raises(mc.MCPError):
        mc._expand_refs("${file:data/missing.json#host}")
    with pytest.raises(mc.MCPError):
        mc._expand_refs("${env:DABAI_TEST_UNSET_VAR}")


def test_mask_swaps_known_secret_back(fake_root):
    mapping = mc._known_secret_map()
    assert mapping["12345678"] == "${file:data/bambu_lab.json#access_code}"
    assert "a1" not in mapping  # 短值不当凭据换，避免误伤
    masked = mc._mask_secrets({"env": {"CODE": "12345678", "MODEL": "a1"}}, mapping)
    assert masked["env"] == {"CODE": "${file:data/bambu_lab.json#access_code}", "MODEL": "a1"}


def test_servers_json_has_no_plaintext_credentials():
    raw = open(mc._SPECS_PATH, encoding="utf-8").read()
    assert not re.search(r"\b192\.168\.\d+\.\d+\b", raw), "内网 IP 明文进了 servers.json"
    # 全零是「没填」的占位，不是真值
    assert not re.search(r"\b(?!0{8,})\d{8,}\b", raw), "长数字串（access_code/序列号）明文进了 servers.json"
