#!/usr/bin/env python3
"""判断机器人是不是**物理锁腰**（装了腰部固定件）。

判据不能只看固件报告的 `mode_machine`（6=27DoF 锁腰 / 5=29-DoF），那只是型号线索；
物理事实要靠手掰：

  - **腰掰不动**（waist_roll / waist_pitch 的角度几乎不变）而**膝盖能掰动**
    → 装了腰部固定件，腰被机械锁死；
  - 腰也能掰动 → 没装。

**零力矩模式下采样摆幅证明不了锁没锁**：没人碰时所有关节都只动 0.003~0.005°
的编码器噪声，腰和膝一样。必须有人在采样期间掰。

用法（机器人上）：
    python3 tools/check_waist_locked.py eth0 --seconds 60

采样期间依次掰：腰部 → 膝盖 → 手臂肘部。

只读：不发任何控制消息。
"""

from __future__ import annotations

import glob
import math
import sys
import time

#: 关注的关节：腰三轴 + 两个膝盖 + 两个肘（对照组）。
WATCHED = {
    "腰 yaw": 12,
    "腰 roll": 13,
    "腰 pitch": 14,
    "左膝": 3,
    "右膝": 9,
    "左肘": 18,
    "右肘": 25,
}


def _bootstrap_sdk() -> None:
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
    seconds = 60.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    _bootstrap_sdk()

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    ChannelFactoryInitialize(0, interface)
    samples: dict[int, list[float]] = {slot: [] for slot in WATCHED.values()}
    mode_machine: list[int] = []

    def on_state(message) -> None:
        mode_machine.append(int(message.mode_machine))
        for slot in samples:
            samples[slot].append(float(message.motor_state[slot].q))

    subscriber = ChannelSubscriber("rt/lowstate", LowState_)
    subscriber.Init(on_state, 10)

    print(f"采样 {seconds:.0f} 秒。")
    print("请依次掰：1) 腰部  2) 膝盖  3) 手臂肘部", flush=True)
    time.sleep(seconds)
    subscriber.Close()

    if not samples[12]:
        print("没收到 rt/lowstate，检查网卡名。")
        return 1

    deg = 180.0 / math.pi
    print(
        f"\nmode_machine = {mode_machine[-1]}  "
        f"({'27DoF(锁腰)' if mode_machine[-1] == 6 else '29-DoF' if mode_machine[-1] == 5 else '23-DoF' if mode_machine[-1] == 4 else '未知'})"
    )
    print()
    print(f"{'关节':10s} {'最小(度)':>10s} {'最大(度)':>10s} {'摆幅(度)':>10s}")
    spans: dict[str, float] = {}
    for name, slot in WATCHED.items():
        values = samples[slot]
        span = (max(values) - min(values)) * deg
        spans[name] = span
        print(
            f"{name:10s} {min(values) * deg:10.3f} {max(values) * deg:10.3f} {span:10.3f}"
        )

    waist = max(spans["腰 roll"], spans["腰 pitch"])
    knee = max(spans["左膝"], spans["右膝"])
    print()
    if waist < 0.2 and knee > 1.0:
        print("=> 腰掰不动、膝盖能掰动：**物理锁腰**（装了腰部固定件）")
    elif waist > 1.0:
        print("=> 腰能掰动：**没有物理锁腰**")
    else:
        print("=> 摆幅都不明显：这一轮可能没真的掰，先确认机器人在零力矩模式再重试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
