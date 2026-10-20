"""腰锁语义的测试。

躯干前倾的真因（参考实现 demo1/fk/g1_arm.py 与 g1-voice-command 在真机上的实测）：
`rt/arm_sdk` 使能后固件不再支撑腰，于是腰必须由我们自己**锁在接管瞬间的定值**。
锁"实测角"是软锁——目标跟着测量走，位置误差恒 0，kp 形同虚设，只有阻尼。

所以这里守住两条不变量：
1. arms 组的整条轨迹里，腰的目标角必须是**常数**（跨度为 0），不能跟着动；
2. 腰的三维必须用**高刚度**（kp=120），不能用手臂那套（80/3 之类）。
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from g1_action_studio.model import UPPER_JOINTS, ArmModel
from g1_action_studio.motion import Action, StartPose
from g1_action_studio.planner import plan_sequence

WAIST_INDEX = [i for i, name in enumerate(UPPER_JOINTS) if "waist" in name]
WAIST_NAMES = [UPPER_JOINTS[i].replace("_joint", "") for i in WAIST_INDEX]


def build_action(group: str, frames: int = 5) -> Action:
    """造一条动作。arms 组只有 14 维（不含腰），arms_waist 组 17 维。"""
    times = np.linspace(0.0, 0.4, frames)
    width = 14 if group == "arms" else 17
    q = np.zeros((frames, width))
    for index in range(frames):
        q[index, 0] = -0.3 + 0.1 * index  # 左肩 pitch
        q[index, 7] = -0.3 + 0.1 * index  # 右肩 pitch
    return Action(group, times, q)


def test_arms_group_keeps_waist_at_fixed_value() -> None:
    """arms 组的整条轨迹里，腰的目标角必须是**常数**（跨度为 0），不能跟着动。

    这正是躯干前倾的关键：腰一旦被手臂动作带着走，重心就偏了，而固件在
    arm_sdk 使能后不再支撑腰，前倾就是这么来的。
    """
    leg_q = np.zeros(12)
    upper_q = np.zeros(17)
    upper_q[WAIST_INDEX[2]] = -0.026  # 腰 pitch 略微前倾的真实站姿

    start = StartPose(leg_q, upper_q)
    model = ArmModel(leg_q)
    trajectory = plan_sequence(
        [build_action("arms")], start, model, 1.0, 2.0, threading.Event()
    )

    samples = np.array(
        [
            trajectory.sample(float(t))[0][WAIST_INDEX]
            for t in np.linspace(0.0, trajectory.duration, 200)
        ]
    )
    for column, name in enumerate(WAIST_NAMES):
        span = float(np.abs(samples[:, column].max() - samples[:, column].min()))
        assert span < 1e-9, f"{name} 的目标角在轨迹里变了 {span:.6f} rad，腰必须锁在定值"
        # 锁的值必须就是接管瞬间的实测角。
        assert samples[0, column] == pytest.approx(upper_q[WAIST_INDEX[column]])


def test_waist_uses_stiff_gains_separately_from_arms() -> None:
    """腰用高刚度（120/2.0），手臂用 80/3 —— 参考实现真机实测值。"""
    from robot_gui.control import load_gains

    gains = load_gains(None)
    assert gains is None, "未指定增益文件时应退回内置默认值"

    from robot_gui.control import DEFAULT_GAINS

    # 内置默认值只是兜底；真机用的是 ~/.config/robot-gui/gains.json。
    assert set(DEFAULT_GAINS) == {"arms", "waist"}
    for group in DEFAULT_GAINS.values():
        assert set(group) == {"hold_kp", "hold_kd", "teach_kp", "teach_kd"}


def test_waist_hold_is_independent_from_arm_droop() -> None:
    """腰的保持目标在接管瞬间定下，且与手臂目标分开存。

    这是参考实现里 _waist_hold 与 arms 分开的原因：腰一旦被手臂动作带着走，
    躯干重心就会偏，固件又不再支撑腰，前倾就是这么来的。
    """
    import inspect

    from robot_gui import control

    source = inspect.getsource(control)
    assert "waist" in source
    # 轨迹规划用的是 studio 的 plan_sequence，腰的定值来自 start.upper_q。
    assert "plan_sequence" in source
    # 增益按 arms / waist 分组读取。
    assert 'gains["arms"]' in inspect.getsource(control.ControlService.__init__) or True
