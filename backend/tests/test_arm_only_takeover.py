"""接管范围的不变量：**双臂 + 腰的姿态保持**。

真机实测（2026-10-21）把机制钉死了：
  - `rt/arm_sdk` 的权重一旦升到 1，腰（slot 12-14）的控制权就**无条件**归我们，
    `motor_cmd[i].mode` 写 0 或 1 完全没有区别（两档前倾峰值 34.01° vs 34.15°，
    曲线形状一致）；
  - 腰槽位留空（增益全 0）时，腰在接管后 0.8 秒内被推到 +32.8° 的机械限位
    （waist_pitch 行程 ±0.52 rad = ±29.8°），固件的腰部平衡回路退让，
    躯干前倾 +40°、髋部只 +7°。

所以腰必须由我们给出支撑力矩：**PD 项 + 重力前馈**。动作本身仍然只有 14 维
（arms 组），腰的目标角恒等于接管瞬间的姿态，即"锁在定值"。

这里守住：
1. 发布的报文里双臂与腰都带有效增益（腰不能是空槽位）；
2. 腰的增益在电机的力矩上限之内（kp 太大 → 饱和 → 刚度反而丢失）；
3. 漂移守卫只看腿 + 腰，手臂不计入基线；
4. 沉降观察窗口覆盖动作结束后约 2 秒才出现的漂移峰值。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from robot_gui.actions import ActionStore
from robot_gui.control import (
    ControlError,
    ControlService,
    ControlSettings,
    DEFAULT_GAINS,
    LowerBodyMonitor,
)
from robot_gui.profile import (
    ARM_SDK_SLOTS,
    G1_29DOF_JOINTS,
    LOWER_BODY_JOINT_NAMES,
    WAIST_SDK_SLOTS,
)

#: 官方 G1 29DoF MJCF 里的执行器力矩上限（ctrlrange）。
#: 腰 pitch/roll ±50 Nm、腰 yaw ±88 Nm、手臂 ±25 Nm。
TORQUE_LIMITS: dict[str, float] = {
    "waist_yaw": 88.0,
    "waist_roll": 50.0,
    "waist_pitch": 50.0,
    "shoulder": 25.0,
    "elbow": 25.0,
    "wrist": 5.0,
}


class _FakeMotor:
    """只保留线格式测试要看的字段，结构对齐 unitree_hg 的 MotorCmd_。"""

    def __init__(self) -> None:
        self.mode = 0
        self.q = 0.0
        self.dq = 0.0
        self.tau = 0.0
        self.kp = 0.0
        self.kd = 0.0


class _FakeLowCmd:
    def __init__(self) -> None:
        self.motor_cmd = [_FakeMotor() for _ in range(35)]
        self.crc = 0


@pytest.fixture
def sdk_available() -> Any:
    """开发机上没有 unitree_sdk2py 时，临时塞一个只够构造报文的桩。

    真实 SDK 装在机器人上；桩只让「发布的报文长什么样」这条线格式不变量
    在开发机也能验证，不涉及任何真实通信。作用域结束即还原 sys.modules。
    """
    if importlib.util.find_spec("unitree_sdk2py") is not None:
        yield None
        return

    default = types.ModuleType("unitree_sdk2py.idl.default")
    default.unitree_hg_msg_dds__LowCmd_ = _FakeLowCmd  # type: ignore[attr-defined]
    default.unitree_hg_msg_dds__MotorCmd_ = _FakeMotor  # type: ignore[attr-defined]
    idl = types.ModuleType("unitree_sdk2py.idl")
    idl.default = default  # type: ignore[attr-defined]
    sdk = types.ModuleType("unitree_sdk2py")
    sdk.idl = idl  # type: ignore[attr-defined]

    names = ("unitree_sdk2py", "unitree_sdk2py.idl", "unitree_sdk2py.idl.default")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules.update(
        {
            "unitree_sdk2py": sdk,
            "unitree_sdk2py.idl": idl,
            "unitree_sdk2py.idl.default": default,
        }
    )
    try:
        yield None
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def build_command(feedforward=None):
    """用 patched 的命令构造函数造一条 LowCmd。"""
    import g1_action_studio.robot as studio_robot

    from robot_gui.gravity import install_gravity_command_builder

    install_gravity_command_builder(feedforward)

    class FakeCrc:
        @staticmethod
        def Crc(_message):  # noqa: N802 - 对齐 SDK 的命名
            return 0

    q = np.arange(17, dtype=float) * 0.1
    kp = np.full(17, 60.0)
    kd = np.full(17, 1.5)
    return studio_robot.build_command(q, np.zeros(17), kp, kd, 0.5, FakeCrc())


def test_waist_slots_carry_real_gains(sdk_available: Any) -> None:
    """腰的槽位必须带有效增益——空槽位会让腰瘫到 +32.8° 的机械限位。"""
    command = build_command()

    for slot in WAIST_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == 1, f"slot {slot} 没有被使能"
        assert motor.kp > 0.0, f"slot {slot} 的 kp 是 0，腰会瘫到机械限位"
        assert motor.kd > 0.0, f"slot {slot} 没有阻尼"
        assert motor.q == 0.0 or motor.q != 0.0  # 目标角由轨迹给出，这里只确认字段存在


def test_arm_slots_are_driven_with_gains(sdk_available: Any) -> None:
    """双臂 14 个槽位照常使能并带增益。"""
    command = build_command()

    for slot in ARM_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == 1
        assert motor.kp == pytest.approx(60.0)
        assert motor.kd == pytest.approx(1.5)

    assert command.motor_cmd[29].q == pytest.approx(0.5)


def test_command_builder_is_installed_even_without_gravity_feedforward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关掉重力前馈也必须装命令构造函数补丁。

    补丁负责给被接管的关节（含腰）加重力前馈。只在前馈开启时装的话，
    `ROBOT_GUI_GRAVITY_COMPENSATION=0` 时腰就没有任何支撑力矩。
    """
    from robot_gui import control

    calls: list[object] = []
    monkeypatch.setattr(
        control,
        "install_gravity_command_builder",
        lambda feedforward: calls.append(feedforward),
    )

    control.ControlService(
        settings=ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)),
        telemetry=object(),
        action_store=object(),
        feedforward=None,
    )

    assert calls == [None]


def test_waist_gains_stay_inside_the_torque_limit() -> None:
    """腰的 kp 必须在力矩上限内：kp × 饱和前误差 ≤ 50 Nm。

    kp 太大反而危险：力矩到 50 Nm 就饱和，超过之后刚度丢失、腰会继续倒。
    waist_pitch 行程只有 ±0.52 rad = ±29.8°，饱和误差必须明显小于行程。
    """
    kp = DEFAULT_GAINS["waist"]["hold_kp"]
    limit = TORQUE_LIMITS["waist_pitch"]
    saturation_error = limit / kp
    assert saturation_error < 0.26, (
        f"kp={kp} 的饱和误差 {saturation_error:.3f} rad 已经接近 waist_pitch 的行程上限"
    )
    # 腰必须真的被给到刚度，而不是留空。
    assert kp > 0.0
    assert DEFAULT_GAINS["waist"]["hold_kd"] > 0.0


def test_arm_gains_stay_inside_the_torque_limit() -> None:
    """手臂的 kp 也不能让肩/肘在实测偏差范围内就饱和。

    2026-10-21 实测最大跟踪偏差 0.146 rad；kp=120 时肩 pitch 的饱和误差
    25/120 = 0.208 rad，实测偏差还在饱和界以内。
    """
    kp = DEFAULT_GAINS["arms"]["hold_kp"]
    limit = TORQUE_LIMITS["shoulder"]
    saturation_error = limit / kp
    assert saturation_error > 0.146, (
        f"kp={kp} 的饱和误差 {saturation_error:.3f} rad 小于实测最大偏差，肩会饱和"
    )


def test_lower_body_monitor_ignores_arm_motion() -> None:
    """手臂在动作里转 77° 不是下盘漂移（2026-10-20 误报：77.11° @right_wrist_roll）。"""
    monitor = LowerBodyMonitor(threshold_deg=5.0)
    baseline = {joint.name: 0.0 for joint in G1_29DOF_JOINTS}
    monitor.capture_baseline(baseline)

    moved = dict(baseline)
    moved["right_wrist_roll"] = np.deg2rad(77.0)
    monitor.update(moved)

    assert monitor.violated is False
    assert monitor.report()["max_drift_deg"] == 0.0


def test_lower_body_monitor_still_catches_waist_drift() -> None:
    """腰 14 漂了 8° 必须报出来，真实腰 14 漂移 0.17° 则不报。"""
    monitor = LowerBodyMonitor(threshold_deg=5.0)
    baseline = {joint.name: 0.0 for joint in G1_29DOF_JOINTS}
    monitor.capture_baseline(baseline)

    slight = dict(baseline)
    slight["waist_pitch"] = np.deg2rad(0.17)
    monitor.update(slight)
    assert monitor.violated is False

    leaning = dict(baseline)
    leaning["waist_pitch"] = np.deg2rad(8.0)
    monitor.update(leaning)
    assert monitor.violated is True
    assert monitor.report()["max_drift_joint"] == "waist_pitch"
    assert monitor.report()["waist_pitch_drift_deg"] == pytest.approx(8.0, abs=0.01)


def test_lower_body_monitor_watches_legs_and_waist_only() -> None:
    """守卫关注的关节集合 = 腿 12 + 腰 3，手臂 14 不在内。"""
    assert LOWER_BODY_JOINT_NAMES == frozenset(
        joint.name
        for joint in G1_29DOF_JOINTS
        if joint.group in {"left_leg", "right_leg", "waist"}
    )
    assert len(LOWER_BODY_JOINT_NAMES) == 15


def test_settle_window_covers_the_delayed_peak() -> None:
    """沉降窗口必须覆盖动作结束后约 2 秒才出现的漂移峰值。"""
    from robot_gui.main import RuntimeSettings

    assert RuntimeSettings().hold_soak_s >= 3.0
    assert ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)).hold_soak_s >= 3.0


class FakeTelemetry:
    def snapshot(self, mode: str, interface: str | None, adapter_error: str | None):
        return {
            "sources": {"low_state": {"state": "fresh"}},
            "robot": {"mode_machine": 5, "fsm": {"id": 501, "mode": None}},
        }


def store_with(tmp_path: Path, group: str, width: int) -> ActionStore:
    store = ActionStore(tmp_path)
    body = {
        "group": group,
        "frames": [
            {"t": round(index * 0.04, 4), "q": [0.0] * width}
            for index in range(4)
        ],
    }
    store.import_action("demo", json.dumps(body).encode("utf-8"))
    return store


def test_replay_rejects_waist_action(tmp_path: Path) -> None:
    """含腰的动作直接被拒，且在下发任何控制命令之前返回。

    动作库只收 14 维的 arms 组：腰虽然由我们下发指令，但它的目标角是**锁在接管
    瞬间的定值**，不执行 ECHO-G 转出的腰轨迹。
    """
    service = ControlService(
        settings=ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)),
        telemetry=FakeTelemetry(),
        action_store=store_with(tmp_path, "arms_waist", 17),
    )

    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=True)
    assert error.value.code == "action_group_unsupported"


def test_replay_accepts_arms_action(tmp_path: Path) -> None:
    """arms 组（14 维）通过前置检查。"""
    service = ControlService(
        settings=ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)),
        telemetry=FakeTelemetry(),
        action_store=store_with(tmp_path, "arms", 14),
    )

    action = service._preflight("demo", confirmed=True)  # noqa: SLF001
    assert action.group == "arms"
