#!/usr/bin/env python3
"""读机器人的运动模式状态（fsm_id + fsm_mode）。

fsm_mode 是官方《高层运动服务接口》里的"主运控是否处于站立状态"：
  0 = 静态，允许切换到其他模式
  1 = 动态，固件会拒绝除阻尼以外的大部分切换

SDK 的 LocoClient 没有暴露 GetFsmMode，只能走底层通用 RPC
（ROBOT_API_ID_LOCO_GET_FSM_MODE = 7002），返回 `{"data": 0|1}`。

用法（机器人上）：
    python3 tools/inspect_fsm.py eth0

只读：不发任何控制消息。
"""

from __future__ import annotations

import glob
import json
import sys


def _bootstrap_sdk() -> None:
    """机器人上 SDK 装在后端 venv 里，这里把它加进 sys.path。"""
    try:
        import unitree_sdk2py  # noqa: F401

        return
    except ImportError:
        pass
    for pattern in (
        "/usr/local/src/robot-gui/backend/.venv/lib/python*/site-packages",
        "/home/unitree/Program/unitree_sdk2_python",
    ):
        for path in glob.glob(pattern):
            sys.path.insert(0, path)
            try:
                import unitree_sdk2py  # noqa: F401

                return
            except ImportError:
                continue


def main() -> int:
    interface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
    _bootstrap_sdk()

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    ChannelFactoryInitialize(0, interface)
    client = LocoClient()
    client.SetTimeout(5.0)
    client.Init()

    code, fsm_id = client.GetFsmId()
    mode_code, mode_data = client._Call(7002, "{}")  # noqa: SLF001 - SDK 没暴露这个接口
    fsm_mode = None
    if mode_code == 0 and mode_data:
        try:
            fsm_mode = json.loads(mode_data).get("data")
        except (TypeError, ValueError):
            fsm_mode = None

    labels = {
        0: "零力矩",
        1: "阻尼",
        2: "位控下蹲",
        3: "位控落座",
        4: "锁定站立",
        500: "常规运控(两自由度腰)",
        501: "常规运控(三自由度腰)",
        801: "走跑运控",
        802: "走跑运控",
    }
    label = labels.get(fsm_id, "未知")
    state = "静态（允许切换）" if fsm_mode == 0 else "运动（固件会拒绝大部分切换）"
    print(f"fsm_id   = {fsm_id}  ({label})")
    print(f"fsm_mode = {fsm_mode}  ({state})")
    if code != 0:
        print(f"读 FSM 失败，错误码 {code}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
