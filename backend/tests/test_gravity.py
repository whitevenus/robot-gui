"""重力前馈的测试。

最重要的一条是「施加前馈后上肢关节必须静止」：它同时验证了符号约定和数值，
比对着物理直觉推理可靠得多。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from robot_gui.gravity import (
    GravityFeedforward,
    default_model_path,
    install_gravity_command_builder,
)


@pytest.fixture(scope="module")
def feedforward() -> GravityFeedforward:
    return GravityFeedforward(default_model_path())


def upper_joints_accel(
    feedforward: GravityFeedforward, leg_q: np.ndarray, upper_q: np.ndarray
) -> float:
    """按前馈力矩求解后，上肢关节的最大 |qacc|。"""
    import mujoco

    model = feedforward._model  # noqa: SLF001 - 测试需要直接读模型的 DOF 布局
    data = feedforward._data  # noqa: SLF001
    data.qpos[:] = model.qpos0
    data.qpos[feedforward._leg_qpos] = leg_q  # noqa: SLF001
    data.qpos[feedforward._upper_qpos] = upper_q  # noqa: SLF001
    data.qvel[:] = 0.0
    mujoco.mj_fwdPosition(model, data)
    mujoco.mj_fwdVelocity(model, data)
    tau = -data.qfrc_bias[feedforward._upper_dof]  # noqa: SLF001
    base_dof = model.jnt_dofadr[model.joint("floating_base_joint").id]
    mujoco.mj_fwdActuation(model, data)
    # 基座自由落体会通过惯性耦合污染关节加速度，虚拟一个保持力矩模拟腿撑住基座
    data.qfrc_actuator[feedforward._upper_dof] = tau  # noqa: SLF001
    data.qfrc_actuator[base_dof] = -data.qfrc_bias[base_dof]
    mujoco.mj_fwdAcceleration(model, data)
    return float(np.abs(data.qacc[feedforward._upper_dof]).max())  # noqa: SLF001


LEG = np.array(
    [-0.348, -0.007, 0.005, 0.623, 0.029, -0.012,
     -0.350, -0.019, -0.011, 0.635, 0.033, -0.010]
)


@pytest.mark.parametrize(
    ("tag", "upper"),
    [
        ("双臂自然下垂", [0.0] * 17),
        ("双肩前伸 1.2", [1.2, 0, 0, 0, 0, 0, 0, 1.2, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
        (
            "右肘弯曲 0.98（真机实测）",
            [0.291, 0, 0, 0.983, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.002, 0.001, -0.032],
        ),
        ("双臂外展", [0, 1.5, 0, 0, 0, 0, 0, 0, -1.5, 0, 0, 0, 0, 0, 0, 0, 0]),
        ("腰部扭转", [0.0] * 14 + [0.5, 0.0, -0.032]),
    ],
)
def test_feedforward_makes_upper_body_static(
    feedforward: GravityFeedforward, tag: str, upper: list[float]
) -> None:
    feedforward.bind_legs(LEG)
    upper_array = np.array(upper)

    # 先确认没有前馈时关节确实会被重力带偏（有下垂/摆动）
    without = upper_joints_accel(feedforward, LEG, upper_array)

    feedforward.torques(upper_array)
    feedforward.bind_legs(LEG)
    with_ff = upper_joints_accel(feedforward, LEG, upper_array)

    assert with_ff < 1e-6, f"{tag}: 施加前馈后仍有加速度 {with_ff}"
    assert without > 1e-3 or with_ff < 1e-6


def test_feedforward_respects_actuator_limits(feedforward: GravityFeedforward) -> None:
    feedforward.bind_legs(LEG)
    tau = feedforward.torques(np.zeros(17))
    assert tau.shape == (17,)
    assert np.isfinite(tau).all()
    assert (tau >= feedforward._tau_low - 1e-9).all()  # noqa: SLF001
    assert (tau <= feedforward._tau_high + 1e-9).all()  # noqa: SLF001


def test_feedforward_uses_measured_pose_when_available() -> None:
    measured = (LEG, np.array([0.3, 0, 0, 1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
    feedforward = GravityFeedforward(default_model_path(), lambda: measured)
    feedforward.bind_legs(LEG)
    tau = feedforward.torques(np.zeros(17))
    assert tau.shape == (17,)
    assert np.isfinite(tau).all()


def test_patched_build_command_carries_feedforward(feedforward: GravityFeedforward) -> None:
    # Unitree SDK2 只装在机器人上；开发机没有 SDK 时跳过这条。
    pytest.importorskip("unitree_sdk2py")
    import g1_action_studio.robot as studio_robot

    install_gravity_command_builder(feedforward)
    feedforward.bind_legs(LEG)

    class FakeCrc:
        @staticmethod
        def Crc(_message):  # noqa: N802 - 对齐 SDK 的命名
            return 0

    command = studio_robot.build_command(
        np.zeros(17), np.zeros(17), np.full(17, 60.0), np.full(17, 1.5), 0.5, FakeCrc()
    )
    taus = [command.motor_cmd[slot].tau for slot in range(30)]
    assert any(abs(value) > 1e-6 for value in taus[:17]), "前馈力矩没有写进报文"
    assert command.motor_cmd[29].q == pytest.approx(0.5)
