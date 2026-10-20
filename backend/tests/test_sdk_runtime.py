"""Unitree SDK2 CRC 动态库自愈逻辑的测试。

真实缺陷：SDK 的 setup.py 不把 utils/lib/*.so 打进 wheel，安装后缺失，
只有下发 LowCmd 的路径会失败（只读遥测不经过 CRC），错误信息却指向共享库本身。
"""
from __future__ import annotations

import platform
from pathlib import Path

import pytest

from robot_gui import sdk_runtime
from robot_gui.sdk_runtime import ensure_crc_libraries, describe_crc_status


def test_crc_library_is_copied_when_missing(monkeypatch, tmp_path: Path) -> None:
    utils = tmp_path / "utils"
    utils.mkdir()
    (utils / "crc.py").write_text("# stand-in", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    library = sdk_runtime._library_name()
    assert library is not None
    (source / library).write_bytes(b"\x7fELF fake")

    monkeypatch.setattr(sdk_runtime, "_installed_utils_dir", lambda: utils)
    monkeypatch.setattr(sdk_runtime, "_source_dir", lambda: source)
    monkeypatch.setattr(sdk_runtime.ctypes, "CDLL", lambda path: object())

    ready, detail = ensure_crc_libraries()

    assert ready is True
    assert detail == ""
    assert (utils / "lib" / library).is_file()


def test_crc_library_reports_missing_source(monkeypatch, tmp_path: Path) -> None:
    utils = tmp_path / "utils"
    utils.mkdir()
    (utils / "crc.py").write_text("# stand-in", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    monkeypatch.setattr(sdk_runtime, "_installed_utils_dir", lambda: utils)
    monkeypatch.setattr(sdk_runtime, "_source_dir", lambda: empty)

    ready, detail = ensure_crc_libraries()

    assert ready is False
    assert "来源目录里没有" in detail
    assert "crc_" in detail


def test_crc_library_reports_missing_sdk(monkeypatch) -> None:
    monkeypatch.setattr(sdk_runtime, "_installed_utils_dir", lambda: None)

    ready, detail = ensure_crc_libraries()

    assert ready is False
    assert "unitree_sdk2py" in detail


def test_crc_status_description_is_informative(monkeypatch, tmp_path: Path) -> None:
    utils = tmp_path / "utils"
    utils.mkdir()
    (utils / "crc.py").write_text("# stand-in", encoding="utf-8")
    library = sdk_runtime._library_name()
    assert library is not None

    monkeypatch.setattr(sdk_runtime, "_installed_utils_dir", lambda: utils)
    assert "缺失" in describe_crc_status()

    (utils / "lib").mkdir()
    (utils / "lib" / library).write_bytes(b"\x7fELF fake")
    assert "就位" in describe_crc_status()
