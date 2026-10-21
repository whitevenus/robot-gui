"""全程姿态采样：给 robot-gui-track.sh 用。

同时订阅三路 DDS，把判断"腰到底有没有被接管"需要的量一次采全：

  rt/lowstate         → 腰 12/13/14 的角度、**motor_state.mode 回读**、髋部 IMU
  rt/lf/secondary_imu → 躯干 IMU
  rt/arm_sdk          → 我们自己发出的报文：接管权重 + 腰槽位的 mode/kp

以前这个脚本放在 /tmp，机器人重启就没了，track.sh 直接跑不起来。现在放进仓库，
跟 track.sh 一起部署。

用法：

    python3 pose_sampler.py eth0 25            # 网卡 + 秒数
    python3 tools/pose_sampler.py eth0 25 --out logs/pose_samples.json

**纯订阅，不发任何控制命令**，不会干扰正在跑的回放。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any

WAIST_SLOTS = (12, 13, 14)

#: 项目根目录（tools/pose_sampler.py → 上一级）。采样数据写项目内的 logs/，
#: 不散落到 /tmp 或家目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEIGHT_SLOT = 29
SAMPLE_HZ = 20.0


class Collector:
    """把 DDS 回调里的最新快照收进共享状态，主循环按固定频率取样。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waist_q: dict[int, float] = {}
        self._waist_mode: dict[int, int] = {}
        self._hip_pitch: float | None = None
        self._torso_pitch: float | None = None
        self._cmd_weight: float | None = None
        self._cmd_waist_mode: int | None = None
        self._cmd_waist_kp: float | None = None
        self._counts = {"low_state": 0, "arm_sdk": 0, "torso_imu": 0}

    def on_low_state(self, message: Any) -> None:
        motors = message.motor_state
        with self._lock:
            self._counts["low_state"] += 1
            for slot in WAIST_SLOTS:
                self._waist_q[slot] = float(motors[slot].q)
                self._waist_mode[slot] = int(motors[slot].mode)
            self._hip_pitch = float(message.imu_state.rpy[1])

    def on_torso_imu(self, message: Any) -> None:
        with self._lock:
            self._counts["torso_imu"] += 1
            self._torso_pitch = float(message.rpy[1])

    def on_arm_sdk(self, message: Any) -> None:
        motors = message.motor_cmd
        with self._lock:
            self._counts["arm_sdk"] += 1
            self._cmd_weight = float(motors[WEIGHT_SLOT].q)
            self._cmd_waist_mode = int(motors[14].mode)
            self._cmd_waist_kp = float(motors[14].kp)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "waist_q": dict(self._waist_q),
                "waist_mode": dict(self._waist_mode),
                "hip_pitch": self._hip_pitch,
                "torso_pitch": self._torso_pitch,
                "cmd_weight": self._cmd_weight,
                "cmd_waist_mode": self._cmd_waist_mode,
                "cmd_waist_kp": self._cmd_waist_kp,
                "counts": dict(self._counts),
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface", help="DDS 网卡名，例如 eth0")
    parser.add_argument("seconds", type=float, help="采样时长（秒）")
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "logs" / "pose_samples.json",
        help="JSON 输出路径（默认写项目内的 logs/）",
    )
    parser.add_argument("--low-state-topic", default="rt/lowstate")
    parser.add_argument("--torso-imu-topic", default="rt/lf/secondary_imu")
    parser.add_argument("--arm-sdk-topic", default="rt/arm_sdk")
    parser.add_argument(
        "--stop-file",
        type=Path,
        default=None,
        help="出现这个文件就提前收尾并写盘（回放结束后由 track 脚本创建）",
    )
    args = parser.parse_args()

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import IMUState_, LowCmd_, LowState_

    collector = Collector()
    ChannelFactoryInitialize(0, args.interface)

    low_state = ChannelSubscriber(args.low_state_topic, LowState_)
    low_state.Init(collector.on_low_state, 10)

    arm_sdk = ChannelSubscriber(args.arm_sdk_topic, LowCmd_)
    arm_sdk.Init(collector.on_arm_sdk, 10)

    try:
        torso = ChannelSubscriber(args.torso_imu_topic, IMUState_)
        torso.Init(collector.on_torso_imu, 10)
    except Exception as error:  # noqa: BLE001 - 躯干 IMU 缺失不影响主判据
        print(f"[warn] 躯干 IMU 订阅失败：{error}")

    print(f"采样 {args.seconds:.0f} 秒，开始。", flush=True)

    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    period = 1.0 / SAMPLE_HZ
    stopped_early = False
    while True:
        elapsed = time.monotonic() - started
        if elapsed > args.seconds:
            break
        # 回放结束就收尾：采样窗口是估算值，实际时长由后端的轨迹长度决定。
        if args.stop_file is not None and args.stop_file.exists():
            stopped_early = True
            break
        snapshot = collector.snapshot()
        rows.append(
            {
                "t": round(elapsed, 3),
                "waist_yaw": snapshot["waist_q"].get(12),
                "waist_roll": snapshot["waist_q"].get(13),
                "waist_pitch": snapshot["waist_q"].get(14),
                "waist_modes": [
                    snapshot["waist_mode"].get(slot) for slot in WAIST_SLOTS
                ],
                "hip_pitch": snapshot["hip_pitch"],
                "torso_pitch": snapshot["torso_pitch"],
                "cmd_weight": snapshot["cmd_weight"],
                "cmd_waist_mode": snapshot["cmd_waist_mode"],
                "cmd_waist_kp": snapshot["cmd_waist_kp"],
            }
        )
        time.sleep(period)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    counts = collector.snapshot()["counts"]
    tail = "（回放结束提前收尾）" if stopped_early else ""
    print(f"采样完成：{len(rows)} 行 → {args.out}（{counts}）{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
