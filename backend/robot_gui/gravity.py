"""上肢关节的重力前馈力矩。

`rt/arm_sdk` 报文里 `motor_cmd[i].tau` 是**前馈关节力矩**，接收端把它加在 PD 项之外：

    总力矩 = kp*(q_target - q) + kd*(dq_target - dq) + tau

没有 tau 时全靠 PD 扛重力，上肢前伸就会明显下垂。官方 xr_teleoperate 用 Pinocchio 的
`pin.rnea(model, data, q, v, 0)` 算这个值（逆运动学，静止时即维持姿态所需的力矩），
本模块用 MuJoCo 的等价做法：

    tau = -qfrc_bias[upper_dof]

`qfrc_bias` 是 C(q,v)+g(q)，在 v=0 时只剩重力项；取负号就是抵消重力所需的关节力矩。

两点实现取舍：

1. **用实测姿态算，不用目标姿态。** 官方用 IK 解算出的目标角，那在快速运动时前馈会
   偏离实际重力。这里改成每帧用 LowState 的实测角计算，PD 的跟踪误差与重力补偿互相
   正交；实测拿不到时回退到目标角。
2. **腿部姿态必须用实测值。** 腰关节的重力力矩对腿部姿态非常敏感：模型默认腿姿
   （直腿）下 waist_pitch 约 +4.9 Nm，而实测蹲姿约 +1.4 Nm，差 3.5 Nm。
   上肢动作的几秒内腿基本不动，所以腿部取会话开始时的实测值即可。

**腰也在接管范围内**：腰的槽位照样下发 PD + 重力前馈。arm_sdk 的权重一旦升到 1，
腰的控制权就无条件归我们，per-motor mode 写 0 或 1 都没有区别（2026-10-21 实测）。
腰的角度仍然参与重力模型，因为手臂的重力力矩依赖躯干姿态。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

import numpy as np

# 测量姿态提供者：返回 (腿部 12 维, 上肢 17 维) 的实测关节角。
MeasuredPose = tuple[np.ndarray, np.ndarray]
PoseProvider = Callable[[], MeasuredPose | None]


def default_model_path() -> Path:
    """g1-action-studio 自带的官方 G1 29DoF MJCF。"""
    import g1_action_studio.model as studio_model

    return Path(studio_model.__file__).resolve().parent / "resources" / "g1_29dof.xml"


class GravityFeedforward:
    """按当前实测姿态计算上肢的重力前馈力矩。

    模型按 17 维上肢算（腰角参与重力模型），但**只应用在双臂的 14 个关节上**——
    腰不由我们驱动。
    """

    def __init__(
        self,
        xml_path: Path,
        pose_provider: PoseProvider | None = None,
    ) -> None:
        import mujoco
        from g1_action_studio.model import LEG_JOINTS, UPPER_JOINTS

        self._mujoco = mujoco
        self._model = mujoco.MjModel.from_xml_path(str(xml_path))
        self._data = mujoco.MjData(self._model)

        leg_ids = [self._model.joint(name).id for name in LEG_JOINTS]
        upper_ids = [self._model.joint(name).id for name in UPPER_JOINTS]
        self._leg_qpos = self._model.jnt_qposadr[leg_ids]
        self._upper_qpos = self._model.jnt_qposadr[upper_ids]
        self._upper_dof = self._model.jnt_dofadr[upper_ids]
        self._leg_count = len(leg_ids)
        self._upper_count = len(upper_ids)
        self._joint_names = tuple(UPPER_JOINTS)

        # 执行器力矩限位：超过就裁掉，避免模型误差把力矩推到不安全区间。
        self._tau_low = np.empty(self._upper_count)
        self._tau_high = np.empty(self._upper_count)
        for index, name in enumerate(UPPER_JOINTS):
            actuator = self._model.actuator(name.replace("_joint", ""))
            self._tau_low[index] = actuator.ctrlrange[0]
            self._tau_high[index] = actuator.ctrlrange[1]

        self._pose_provider = pose_provider
        self._leg_q = np.zeros(self._leg_count)
        self._last_torques = np.zeros(self._upper_count)

    @property
    def last_torques(self) -> np.ndarray:
        return self._last_torques.copy()

    def bind_legs(self, leg_q: np.ndarray) -> None:
        """绑定会话开始时的实测腿部姿态（上肢动作期间腿基本不动）。"""
        self._leg_q = np.asarray(leg_q, dtype=float).reshape(self._leg_count)

    def torques(self, upper_target: np.ndarray) -> np.ndarray:
        """返回 17 维前馈力矩。upper_target 是当前命令的目标角。"""
        upper = np.asarray(upper_target, dtype=float).reshape(self._upper_count)
        leg = self._leg_q
        if self._pose_provider is not None:
            measured = self._pose_provider()
            if measured is not None:
                leg_measured, upper_measured = measured
                leg = np.asarray(leg_measured, dtype=float).reshape(self._leg_count)
                upper = np.asarray(upper_measured, dtype=float).reshape(self._upper_count)

        data = self._data
        data.qpos[:] = self._model.qpos0
        data.qpos[self._leg_qpos] = leg
        data.qpos[self._upper_qpos] = upper
        data.qvel[:] = 0.0
        self._mujoco.mj_fwdPosition(self._model, data)
        self._mujoco.mj_fwdVelocity(self._model, data)
        tau = -data.qfrc_bias[self._upper_dof]
        self._last_torques = np.clip(tau, self._tau_low, self._tau_high)
        return self._last_torques


class _StudioRobot(Protocol):
    build_command: object


_ACTIVE_FEEDFORWARD: GravityFeedforward | None = None
_PATCHED = False


def active_feedforward() -> GravityFeedforward | None:
    return _ACTIVE_FEEDFORWARD


def install_gravity_command_builder(feedforward: GravityFeedforward | None) -> None:
    """给 g1-action-studio 装上重力前馈。

    g1-action-studio 的 `build_command` 把 `motor.tau` 硬编码为 0，没有重力前馈。
    参考实现按项目约束是只读的，所以这里替换它的命令构造函数，而不是改参考仓库的源码。
    替换后只有一处行为变化：tau 从 0 变成按实测姿态算出的前馈力矩。

    **腰也在接管范围内。** 2026-10-21 真机实测：`rt/arm_sdk` 的权重一旦升到 1，
    腰（slot 12-14）的控制权就无条件归我们，`motor_cmd[i].mode` 写 0 或 1 完全没有区别
    （两档实测前倾峰值 34.01° vs 34.15°，曲线形状一致）。腰槽位留空（增益全 0）时，
    腰在接管后 0.8 秒内被推到 +32.8° 的机械限位（waist_pitch 行程 ±0.52 rad = ±29.8°），
    固件的腰部平衡回路退让，躯干前倾 +40°、髋部只 +7°。

    所以腰必须由我们给出足够的支撑力矩：**PD 项 + 重力前馈**。
    腰的目标角取轨迹里的值（arms 组动作里腰恒等于接管瞬间的姿态），即"锁在定值"。
    """
    global _ACTIVE_FEEDFORWARD, _PATCHED

    import g1_action_studio.robot as studio_robot

    _ACTIVE_FEEDFORWARD = feedforward
    if _PATCHED:
        return

    from g1_action_studio.model import UPPER_SLOTS

    def build_command(q, dq, kp, kd, weight, crc):
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_

        command = unitree_hg_msg_dds__LowCmd_()
        feedforward = _ACTIVE_FEEDFORWARD
        tau = feedforward.torques(q) if feedforward is not None else None
        for index, slot in enumerate(UPPER_SLOTS):
            motor = command.motor_cmd[slot]
            # mode=1 表示使能该电机（SDK 文档：0=Disable / 1=Enable）。
            # 参考实现 demo1/fk/g1_arm.py 每次写指令都带 mode，缺了它电机是不使能的。
            motor.mode = 1
            motor.q = float(q[index])
            motor.dq = float(dq[index])
            motor.kp = float(kp[index])
            motor.kd = float(kd[index])
            motor.tau = float(tau[index]) if tau is not None else 0.0
        command.motor_cmd[29].q = float(weight)
        command.crc = crc.Crc(command)
        return command

    studio_robot.build_command = build_command
    _PATCHED = True
