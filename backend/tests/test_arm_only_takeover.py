"""接管范围的不变量：**只接管双臂**。

躯干前倾的真因（2026-10-20 真机实测）：`rt/arm_sdk` 使能后固件不再支撑腰，
腰一旦被我们接管，内置的腰部平衡回路就退让，躯干从前倾 -1.49° 一路倒到 +10.01°，
而且动作结束后还在继续恶化到 t=10.86s 才见峰值。

所以这里守住三条不变量：
1. 发布的报文里**腰的三个槽位不被接管**（mode=0、增益与目标全 0）；
2. 漂移守卫**只看腿 + 腰**，手臂在动作里本来就在动，不算漂移；
3. 含腰的动作（arms_waist 组）直接被拒，不下发任何控制命令。
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


def build_command(feedforward=None, waist_slot_mode: int = 0):
    """用 patched 的命令构造函数造一条 LowCmd。"""
    import g1_action_studio.robot as studio_robot

    from robot_gui.gravity import install_gravity_command_builder

    install_gravity_command_builder(feedforward, waist_slot_mode)

    class FakeCrc:
        @staticmethod
        def Crc(_message):  # noqa: N802 - 对齐 SDK 的命名
            return 0

    q = np.arange(17, dtype=float) * 0.1
    kp = np.full(17, 60.0)
    kd = np.full(17, 1.5)
    return studio_robot.build_command(q, np.zeros(17), kp, kd, 0.5, FakeCrc())


def test_command_leaves_waist_slots_to_the_firmware(sdk_available: Any) -> None:
    """腰的槽位必须写成 mode=0 且增益/目标全 0，固件才继续用它自己的平衡回路。"""
    command = build_command()

    for slot in WAIST_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == 0, f"slot {slot} 仍被 arm_sdk 使能"
        assert motor.kp == 0.0
        assert motor.kd == 0.0
        assert motor.q == 0.0
        assert motor.dq == 0.0
        assert motor.tau == 0.0


def test_waist_slots_equal_the_sdk_default_command(sdk_available: Any) -> None:
    """腰槽位的值必须**逐字段等于 SDK 的默认空指令**。

    这说明「显式写 0」和「根本不写这一路」在报文上完全等价：
    SDK 的 `unitree_hg_msg_dds__MotorCmd_()` 默认就是
    `MotorCmd_(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)`，而 LowCmd_ 构造时 35 个槽位
    都是这个默认值（见 unitree_sdk2py/idl/default.py）。

    显式写出来只是为了把「腰不接管」这层意图留在代码里，并让上面那条测试
    断言的是我们的意图，而不是碰巧等于 SDK 默认值。
    """
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__MotorCmd_

    command = build_command()
    default = unitree_hg_msg_dds__MotorCmd_()

    for slot in WAIST_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == default.mode
        assert motor.q == default.q
        assert motor.dq == default.dq
        assert motor.tau == default.tau
        assert motor.kp == default.kp
        assert motor.kd == default.kd


def test_command_still_drives_both_arms(sdk_available: Any) -> None:
    """双臂 14 个槽位照常使能并带增益。"""
    command = build_command()

    for slot in ARM_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == 1, f"slot {slot} 没有被 arm_sdk 使能"
        assert motor.kp == pytest.approx(60.0)
        assert motor.kd == pytest.approx(1.5)

    assert command.motor_cmd[29].q == pytest.approx(0.5)


def test_diagnostic_mode_writes_waist_mode_one(sdk_available: Any) -> None:
    """诊断档（waist_slot_mode=1）：腰槽位写 mode=1，其余字段不变。

    这是真机对照实验用的档位——**唯一变量就是腰槽位的 mode**，用来判别
    `mode=0` 到底是"交回固件"还是"断力矩"。它不是可用的控制模式：
    腰被 arm_sdk 名义上接管却给零增益，若固件真的把腰交出来，腰会瘫。
    """
    command = build_command(waist_slot_mode=1)

    for slot in WAIST_SDK_SLOTS:
        motor = command.motor_cmd[slot]
        assert motor.mode == 1
        assert motor.kp == 0.0
        assert motor.kd == 0.0
        assert motor.q == 0.0
        assert motor.dq == 0.0
        assert motor.tau == 0.0

    # 双臂不受影响，仍然照常使能并带增益。
    for slot in ARM_SDK_SLOTS:
        assert command.motor_cmd[slot].mode == 1


def test_command_builder_is_installed_even_without_gravity_feedforward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关掉重力前馈也必须装命令构造函数补丁。

    补丁同时负责"只接管双臂"和重力前馈。只在前馈开启时装的话，
    `ROBOT_GUI_GRAVITY_COMPENSATION=0` 时会退回 studio 原版 `build_command`，
    **那会把腰一起接管**——正好是要避免的事。
    """
    from robot_gui import control

    calls: list[tuple[object, int]] = []
    monkeypatch.setattr(
        control,
        "install_gravity_command_builder",
        lambda feedforward, waist_slot_mode=0: calls.append((feedforward, waist_slot_mode)),
    )

    control.ControlService(
        settings=ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)),
        telemetry=object(),
        action_store=object(),
        feedforward=None,
    )

    assert calls == [(None, 0)]


def test_waist_slot_mode_setting_rejects_other_values() -> None:
    from robot_gui.main import RuntimeSettings

    assert RuntimeSettings().waist_slot_mode == 0
    with pytest.raises(ValueError):
        RuntimeSettings(waist_slot_mode=2).validate()


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
    assert not any(name.startswith(("left_shoulder", "right_shoulder", "left_elbow", "right_elbow")) for name in LOWER_BODY_JOINT_NAMES)


def test_waist_gains_are_inert() -> None:
    """腰不接管：waist 增益全 0，配置里留着的旧值不会生效。"""
    assert set(DEFAULT_GAINS) == {"arms", "waist"}
    assert all(value == 0.0 for value in DEFAULT_GAINS["waist"].values())
    assert DEFAULT_GAINS["arms"]["hold_kp"] == 60.0


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
    """含腰的动作直接被拒，且在下发任何控制命令之前返回。"""
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
