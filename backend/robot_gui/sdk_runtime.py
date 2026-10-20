"""保证 unitree_sdk2py 的 CRC 动态库在虚拟环境里就位。

Unitree SDK2 的 setup.py 不把 `utils/lib/*.so` 打进 wheel，pip/uv 安装完成后这两个
文件会缺失。SDK 的 `utils/crc.py` 用 `ctypes.CDLL(<utils>/lib/crc_<arch>.so)` 加载它们，
缺文件时的表现是「cannot open shared object file: No such file or directory」——
指向的是共享库而不是「安装不完整」，排查方向容易跑偏。

更隐蔽的是：只读遥测路径不经过 CRC，所以后端能正常连上机器人、页面也能显示数据，
只有真正下发 LowCmd 的那一刻才失败。这里在导入 SDK 之前把 .so 补齐，让问题在启动时
就暴露成一句明确的话，而不是等到机器人接管动作的中途。

补齐失败不会抛异常：此时返回空列表，调用方据此给出可操作的提示。
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from pathlib import Path

# 注意 platform.machine() 在 Windows 上返回 "AMD64"、在 Linux 上返回 "x86_64"，
# 两种写法都要覆盖，否则开发机上会误判成"没有对应的 CRC 动态库"。
LIBRARY_NAMES = {
    "x86_64": "crc_amd64.so",
    "amd64": "crc_amd64.so",
    "aarch64": "crc_aarch64.so",
    "arm64": "crc_aarch64.so",
}

# 机器人上 SDK 的已知安装位置，按优先级排列。
SOURCE_CANDIDATES = (
    "/usr/local/src/unitree_sdk2_python/unitree_sdk2py/utils/lib",
    "/home/unitree/Program/unitree_sdk2_python/unitree_sdk2py/utils/lib",
    "/home/unitree/xr_teleoperate/unitree_sdk2_python/unitree_sdk2py/utils/lib",
)


def _library_name(machine: str | None = None) -> str | None:
    """按架构取 CRC 动态库文件名；大小写不敏感。"""
    key = (machine or platform.machine()).lower()
    return LIBRARY_NAMES.get(key)


def _installed_utils_dir() -> Path | None:
    """定位当前解释器里 unitree_sdk2py/utils 所在目录。"""
    for entry in sys.path:
        candidate = Path(entry) / "unitree_sdk2py" / "utils"
        if (candidate / "crc.py").is_file():
            return candidate
    return None


def _source_dir() -> Path | None:
    override = os.getenv("UNITREE_SDK_LIB_DIR")
    if override:
        path = Path(override)
        if path.is_dir():
            return path
    for candidate in SOURCE_CANDIDATES:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None


def ensure_crc_libraries() -> tuple[bool, str]:
    """把缺失的 CRC 动态库补进虚拟环境。

    返回 (是否就绪, 说明)。就绪时说明为空字符串。
    """
    machine = platform.machine()
    library = _library_name(machine)
    if library is None:
        return False, f"当前架构 {machine} 没有对应的 CRC 动态库"

    utils_dir = _installed_utils_dir()
    if utils_dir is None:
        return False, "当前解释器里找不到 unitree_sdk2py"

    target_dir = utils_dir / "lib"
    target = target_dir / library
    if target.is_file():
        return True, ""

    source = _source_dir()
    if source is None:
        return False, (
            f"找不到 CRC 动态库来源，可用 UNITREE_SDK_LIB_DIR 指定；"
            f"期望的目标位置是 {target}"
        )
    source_file = source / library
    if not source_file.is_file():
        return False, f"来源目录里没有 {library}：{source}"

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, target)
    for extra in LIBRARY_NAMES.values():
        candidate = source / extra
        if candidate.is_file() and not (target_dir / extra).is_file():
            shutil.copy2(candidate, target_dir / extra)

    try:
        ctypes.CDLL(str(target))
    except OSError as error:
        return False, f"已复制 {library} 但仍然无法加载：{error}"
    return True, ""


def describe_crc_status() -> str:
    """给日志用的一行说明。"""
    machine = platform.machine()
    library = _library_name(machine) or "crc_*.so"
    utils_dir = _installed_utils_dir()
    if utils_dir is None:
        return f"unitree_sdk2py 未安装（期望 CRC 库 {library}）"
    target = utils_dir / "lib" / library
    if target.is_file():
        return f"CRC 动态库就位：{target}"
    return f"CRC 动态库缺失：{target}"
