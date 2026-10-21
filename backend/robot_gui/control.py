"""真机控制会话：接管双臂、播放动作、返回基线、交还控制权。

控制链路复用 g1-action-studio 的模块（不是它的 CLI）：
  - RobotSession  负责 rt/arm_sdk 的 LowCmd 发送、weight 0→1→0 的接管与交还、
                  以及异常路径上的降权重（这正是 2026-10-16 事故复盘里修好的两条）
  - plan_sequence 负责「从当前实测姿态 → 动作首帧 → 动作 → 返回基线」的连续轨迹，
                  并在发送任何控制命令之前对整条样条做关节限位与自碰撞判定

**接管范围：双臂 14 关节 + 腰 3 关节的姿态保持。** 动作本身是 14 维（arms 组），
腰的目标角恒等于接管瞬间的姿态（锁在定值），腿（slot 0-11）由内置运控负责。

腰的槽位必须由我们下发：2026-10-21 真机实测，arm_sdk 的权重一旦升到 1，腰的控制权
就无条件归我们（per-motor mode 写 0 或 1 没有区别，两档前倾峰值 34.01° vs 34.15%）。
腰槽位留空时腰在接管后 0.8 秒内被推到 +32.8° 的机械限位，固件的腰部平衡回路退让。

顺序是刻意的：先在只读状态下规划并校验，规划失败就一个控制命令都不发；
规划通过后才 acquire()（创建 rt/arm_sdk 发布器）并播放。

同一时刻只允许一个控制会话。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import comb
from pathlib import Path
from typing import Any

import numpy as np
from g1_action_studio.model import ArmModel
from g1_action_studio.motion import Action, StartPose, Trajectory
from g1_action_studio.planner import connect, plan_sequence_with_marks
from g1_action_studio.robot import RobotSession
from loguru import logger
from scipy.interpolate import PPoly

from .actions import ActionStore, ActionStoreError
from .audio import G1AudioPort
from .gravity import GravityFeedforward, install_gravity_command_builder
from .profile import LOWER_BODY_JOINT_NAMES, WAIST_JOINT_NAMES
from .records import (
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_FAULT,
    OUTCOME_PAUSED,
    ExecutionRecorder,
    RecordStore,
)

#: 本工作台的接管范围：双臂 14 关节 + 腰 3 关节。
#:
#: 腰虽然不由动作驱动（动作库是 arms 组 14 维，腰的目标角恒等于接管瞬间的姿态），
#: 但**必须由我们下发指令**：`rt/arm_sdk` 的权重一旦升到 1，腰的控制权就无条件归我们，
#: `motor_cmd[i].mode` 写 0 或 1 都没有区别（2026-10-21 实测两档前倾峰值 34.01° vs
#: 34.15°，曲线形状一致）。腰槽位留空（增益全 0）时，腰在接管后 0.8 秒内被推到
#: +32.8° 的机械限位，固件的腰部平衡回路退让，躯干前倾 +40°。
ARM_JOINT_COUNT = 14

#: 增益取值。
#:
#: 腰的 kp 决定"躯干前倾多少度才被拉住"：waist_pitch 的力矩上限是 ±50 Nm
#: （官方 G1 29DoF MJCF 的 ctrlrange），行程 ±0.52 rad = ±29.8°。
#: kp=120 时实测前倾 +10.01°，kp=0 时瘫到 +32.8° 限位；kp=300 对应饱和误差
#: 50/300 = 0.167 rad = 9.5°，前倾会停在饱和界以内。kd 取 5，与官方
#: unitree_mujoco 的 G1 FixStand 腰增益（200/5）同量级。
#:
#: 手臂的 kp 决定"够不够得到目标"：肩 pitch 力矩上限 ±25 Nm，kp=120 时饱和误差
#: 25/120 = 0.208 rad = 11.9°，实测最大跟踪偏差 0.146 rad = 8.4° 还没到饱和。
#: kd 按 √kp 比例微调，保持阻尼比不变。
DEFAULT_GAINS: dict[str, dict[str, float]] = {
    "arms": {"hold_kp": 120.0, "hold_kd": 3.5, "teach_kp": 20.0, "teach_kd": 1.5},
    "waist": {"hold_kp": 300.0, "hold_kd": 5.0, "teach_kp": 20.0, "teach_kd": 1.5},
}

#: 官方接口下 12-28 槽位为腰部和上肢电机参数，29.q 为 0-1 权重；
#: 腿部 0-11 由内置运控负责，本工作台不接管。
UPPER_JOINT_COUNT = 17


class ControlPhase(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    ACQUIRING = "acquiring"
    PLAYING = "playing"
    PAUSED = "paused"
    RELEASING = "releasing"
    FAULT = "fault"


class ControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ControlSettings:
    interface: str
    enabled: bool
    fsm_whitelist: tuple[int, ...]
    transition_seconds: float = 2.0
    replay_speed: float = 1.0
    lower_body_drift_deg: float = 5.0
    #: 动作结束后的沉降观察窗口。前倾是延迟发展的：实测峰值出现在动作结束后约 2 秒
    #: （轨迹 8.9 秒，腰 14 峰值 t=10.86s），1.5 秒的窗口会漏掉峰值。
    hold_soak_s: float = 3.0
    gains: dict[str, dict[str, float]] | None = None

    def validate(self) -> None:
        if self.transition_seconds <= 0:
            raise ValueError("transition-seconds must be positive")
        if not 0 < self.replay_speed <= 2.0:
            raise ValueError("replay-speed must be in (0, 2]")


class TrackingMonitor:
    """逐关节跟踪误差监视（只看被接管的双臂 14 关节）。

    上肢"下垂"有两种完全不同的原因，动作本身低垂，或者关节没跟上目标。
    这里在回放期间同时记录命令目标角与实测角，按关节给出最大偏差和均值，
    一次回放就能分辨是哪一种。

    腰不在其中：腰不由我们驱动，它的目标角在轨迹里只是占位，拿它跟实测比没有意义。
    """

    def __init__(self, joint_names: tuple[str, ...]) -> None:
        self._names = joint_names
        self._max = np.zeros(len(joint_names))
        self._sum = np.zeros(len(joint_names))
        self._count = 0
        self._last_target = np.zeros(len(joint_names))
        self._last_measured = np.zeros(len(joint_names))

    def update(self, target: np.ndarray, measured: np.ndarray | None) -> None:
        if measured is None:
            return
        error = np.abs(
            np.asarray(target, dtype=float) - np.asarray(measured, dtype=float)
        )
        self._max = np.maximum(self._max, error)
        self._sum += error
        self._count += 1
        self._last_target = np.asarray(target, dtype=float)
        self._last_measured = np.asarray(measured, dtype=float)

    def report(self) -> dict[str, Any]:
        if self._count == 0:
            return {}
        return {
            "samples": self._count,
            "joints": {
                name: {
                    "max_error_rad": round(float(self._max[index]), 4),
                    "mean_error_rad": round(float(self._sum[index] / self._count), 4),
                    "target_rad": round(float(self._last_target[index]), 4),
                    "measured_rad": round(float(self._last_measured[index]), 4),
                }
                for index, name in enumerate(self._names)
            },
        }


class LowerBodyMonitor:
    """下盘（腿 + 腰）漂移守卫。

    **只看腿（slot 0-11）与腰（slot 12-14），手臂不参与判定。**
    手臂在动作里本来就要大幅运动，把它算进漂移基线只会让守卫每次都误报
    （2026-10-20 真机实测：把手臂算进去后最大漂移 77.11° @right_wrist_roll，
    而真实腰 14 漂移只有 0.17°，远在 5° 阈值内）。

    躯干前倾要看**腰 14 的关节角**，不能用 IMU 判：IMU 装在骨盆上，
    腰倒 23° 时 IMU pitch 只变 2.4°，根本反映不出来。

    **腰 14 按"绝对前倾角"判，腿按"相对接管瞬间的偏离"判**（2026-10-21 改口径）：

    - 腰 14 的危险是**前倾**（机械限位 +29.8°，失控实测瘫到 +32.8°），
      判据是绝对角：峰值前倾 < 5° 说明支撑力矩够用。
    - 早先腰 14 也按"相对接管瞬间的偏离"判，等于把**接管瞬间本身就后仰的那
      1.4°** 也算进去：前倾 3.85° + 初始 −1.4° = 5.26°，于是 9 个动作里有 2 个
      误报越限，而同一批数据按"前倾峰值"判全部在 5° 内。两个口径不是一回事，
      守卫要跟判读口径一致。
    - 腿没有这个问题：它的危险是底盘歪掉，相对接管姿态的偏离才是对的量。

    漂移是**延迟**出现的：手臂停住后平衡控制器才慢慢挪腰/腿，约 1 秒量级，
    而且发生在整条计划跑完之后，只看动作期间的同步检查永远抓不到。
    """

    def __init__(
        self,
        threshold_deg: float = 5.0,
        joint_names: frozenset[str] = LOWER_BODY_JOINT_NAMES,
    ) -> None:
        self._threshold = threshold_deg
        self._joint_names = joint_names
        self._baseline: dict[str, float] = {}
        self._max_drift = 0.0
        self._max_joint = ""
        self._waist_pitch_drift = 0.0
        self._waist_pitch_lean = 0.0

    def capture_baseline(self, angles: dict[str, float]) -> None:
        self._baseline = {
            name: value for name, value in angles.items() if name in self._joint_names
        }
        self._max_drift = 0.0
        self._max_joint = ""
        self._waist_pitch_drift = 0.0
        self._waist_pitch_lean = 0.0

    def update(self, angles: dict[str, float]) -> float:
        if not self._baseline:
            return 0.0
        worst = 0.0
        worst_joint = ""
        for name in self._joint_names:
            # 腰 14 单独按绝对前倾角判，不参与"相对接管姿态"的偏离统计。
            if name == "waist_pitch":
                continue
            baseline = self._baseline.get(name)
            if baseline is None:
                continue
            value = angles.get(name)
            if value is None:
                continue
            drift = abs(value - baseline)
            if drift > worst:
                worst = drift
                worst_joint = name
        drift_deg = float(np.degrees(worst))
        if drift_deg > self._max_drift:
            self._max_drift = drift_deg
            self._max_joint = worst_joint
        waist = angles.get("waist_pitch")
        if waist is not None:
            # 前倾为正：峰值前倾角是绝对量，初始后仰不该算进前倾。
            self._waist_pitch_lean = max(
                self._waist_pitch_lean, float(np.degrees(max(0.0, waist)))
            )
            if "waist_pitch" in self._baseline:
                self._waist_pitch_drift = float(
                    np.degrees(abs(waist - self._baseline["waist_pitch"]))
                )
        return drift_deg

    @property
    def violated(self) -> bool:
        return (
            self._max_drift > self._threshold
            or self._waist_pitch_lean > self._threshold
        )

    def report(self) -> dict[str, Any]:
        return {
            "threshold_deg": self._threshold,
            "max_drift_deg": round(self._max_drift, 3),
            "max_drift_joint": self._max_joint,
            "waist_pitch_lean_deg": round(self._waist_pitch_lean, 3),
            "waist_pitch_drift_deg": round(self._waist_pitch_drift, 3),
            "violated": self.violated,
        }


class ControlService:
    """上肢动作回放的唯一控制源。"""

    def __init__(
        self,
        *,
        settings: ControlSettings,
        telemetry: Any,
        action_store: ActionStore,
        audio_port: G1AudioPort | None = None,
        feedforward: GravityFeedforward | None = None,
        records: RecordStore | None = None,
    ) -> None:
        self._settings = settings
        self._telemetry = telemetry
        self._store = action_store
        self._audio = audio_port
        self._feedforward = feedforward
        self._records = records
        # 必须是可重入锁：release() 会在持锁状态下调用 state()。
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._pause_requested = threading.Event()
        self._phase = ControlPhase.IDLE
        self._message = ""
        self._action_name = ""
        self._error_code = ""
        self._worker: threading.Thread | None = None
        self._trajectory_duration = 0.0
        self._tracking: dict[str, Any] = {}
        self._lower_body: dict[str, Any] = {}
        # 本次回放用的速度（前端下拉传进来的），与设置里的默认值分开记。
        self._current_speed = settings.replay_speed
        # 暂停后要接着播的那几段动作。
        self._resume_names: tuple[str, ...] = ()
        self._resume_speed = settings.replay_speed
        self._resume_kind = "action"
        # 本次会话的动作清单、各段在轨迹里的起始时刻、轨迹开始播放的时刻。
        # 暂停时要用它们算出"播到哪一段了"。
        self._run_names: list[str] = []
        self._run_marks: list[float] = []
        self._run_kind = "action"
        self._play_started: float | None = None
        # 当前会话与当前轨迹：暂停时要拿它们把动作停在当前位置。
        self._session: RobotSession | None = None
        self._trajectory: Any = None
        self._pause_trajectory: Trajectory | None = None
        # 正在播"回到接管姿态"的那一段（监视线程要跟着它继续采样）。
        self._pause_returning = False
        # 必须无条件安装：补丁负责给所有被接管的关节（含腰）加重力前馈。
        # 不装它就会退回 studio 原版 build_command，腰没有前馈、撑不住躯干。
        install_gravity_command_builder(feedforward)

    # ---- 状态 ----

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase": self._phase.value,
                "message": self._message,
                "action": self._action_name,
                "error_code": self._error_code,
                "available": self._settings.enabled,
                "trajectory_duration_s": round(self._trajectory_duration, 3),
                "audio_playing": bool(self._audio.playing) if self._audio else False,
                "gravity_compensation": self._feedforward is not None,
                "tracking": self._tracking,
                "lower_body": self._lower_body,
                # 前端速度下拉的默认值来自设置（ROBOT_GUI_REPLAY_SPEED），
                # 实际这一把用的是 current_speed。
                "default_speed": self._settings.replay_speed,
                "current_speed": self._current_speed,
                "resume_actions": list(self._resume_names),
            }

    def _measured_arms(self) -> np.ndarray | None:
        """当前实测的双臂 14 维关节角（studio 顺序：左臂 7 + 右臂 7）。"""
        angles = self._telemetry.latest_joint_angles()
        if not angles:
            return None
        try:
            arms = np.array([angles[name] for name in arm_joint_names()])
        except KeyError:
            return None
        if not np.isfinite(arms).all():
            return None
        return arms

    def _measured_named(self) -> dict[str, float]:
        return self._telemetry.latest_joint_angles() or {}

    def _monitor_loop(
        self,
        trajectory: Any,
        started: float,
        monitor: TrackingMonitor,
        lower: LowerBodyMonitor,
        cancel: threading.Event,
        soak_s: float,
        recorder: ExecutionRecorder | None = None,
    ) -> None:
        deadline = started + trajectory.duration + soak_s
        while not cancel.is_set():
            elapsed = time.monotonic() - started
            angles = self._measured_named()
            if elapsed > trajectory.duration:
                # 动作结束后的沉降期：漂移是延迟出现的，只看动作期间抓不到。
                lower.update(angles)
                # 暂停时正在播"回到接管姿态"的那一段，监视要跟着它继续跑。
                if elapsed > deadline and not self._pause_returning:
                    break
            else:
                target, _ = trajectory.sample(elapsed)
                # studio 的 upper_q 顺序是双臂在前、腰在后，只取前 14 位与实测对齐。
                arms_target = np.asarray(target, dtype=float)[:ARM_JOINT_COUNT]
                measured = self._measured_arms()
                monitor.update(arms_target, measured)
                lower.update(angles)
            if recorder is not None:
                self._sample_record(recorder, elapsed, angles)
            cancel.wait(0.02)

    def _sample_record(
        self,
        recorder: ExecutionRecorder,
        elapsed: float,
        angles: dict[str, float],
    ) -> None:
        """把这一刻的姿态记进执行记录：腰 14 的角度与 mode 回读、两路 IMU。

        采样窗口跟着回放走：慢放时轨迹更长，记录也就更长，不需要另设参数。
        """
        modes = self._telemetry.latest_joint_modes() or {}
        hip_rpy, torso_rpy = self._telemetry.latest_imu_rpy()
        recorder.sample(
            elapsed,
            waist_pitch=angles.get("waist_pitch"),
            waist_modes=(
                [modes.get(name) for name in WAIST_JOINT_NAMES] if modes else None
            ),
            torso_pitch=_pitch_of(torso_rpy),
            hip_pitch=_pitch_of(hip_rpy),
        )

    def _set_phase(
        self, phase: ControlPhase, message: str = "", error_code: str = ""
    ) -> None:
        with self._lock:
            self._phase = phase
            self._message = message
            self._error_code = error_code

    # ---- 前置检查 ----

    def _preflight(self, action_names: Sequence[str], confirmed: bool) -> list[Action]:
        if not self._settings.enabled:
            raise ControlError(
                "capability_disabled",
                "未开启真机控制：请设置 ROBOT_GUI_CONTROLS_ENABLED=1 并确认现场安全。",
            )
        if not confirmed:
            raise ControlError("confirmation_required", "真机回放需要操作员明确确认。")
        snapshot = self._telemetry.snapshot("real", self._settings.interface, None)
        low_state = snapshot["sources"]["low_state"]
        if low_state["state"] != "fresh":
            raise ControlError(
                "telemetry_stale",
                f"LowState 遥测不是最新（状态 {low_state['state']}），拒绝下发控制命令。",
            )
        mode_machine = snapshot["robot"]["mode_machine"]
        if mode_machine not in (None, 5):
            raise ControlError(
                "mode_machine_unsupported",
                f"mode_machine={mode_machine}，本工作台只支持 29DoF（mode_machine=5）。",
            )
        fsm = snapshot["robot"]["fsm"]
        fsm_id = None if fsm is None else fsm.get("id")
        if fsm_id is None:
            raise ControlError(
                "fsm_unknown", "读不到机器人 FSM 状态，拒绝下发控制命令。"
            )
        if fsm_id not in self._settings.fsm_whitelist:
            raise ControlError(
                "fsm_not_allowed",
                f"FSM {fsm_id} 不在允许列表 {list(self._settings.fsm_whitelist)} 中。",
            )
        actions: list[Action] = []
        for name in action_names:
            try:
                action = self._store.load(name)
            except ActionStoreError as error:
                raise ControlError(error.code, str(error)) from error
            if action.group != "arms":
                raise ControlError(
                    "action_group_unsupported",
                    f"动作组 {action.group} 不被接管：本工作台只回放 arms 组（14 维动作），"
                    "腰的三维锁在接管瞬间的姿态、不执行动作里的腰轨迹，腿由内置运控负责。",
                )
            actions.append(action)
        return actions

    # ---- 控制 ----

    def request_replay(
        self,
        action_names: Sequence[str],
        *,
        confirmed: bool,
        audio_clips: dict[str, bytes] | None = None,
        speed: float | None = None,
        kind: str = "action",
    ) -> dict[str, Any]:
        """回放一个动作，或课程清单里的**一整套**。

        一整套 = 多个动作按顺序规划成**一条连续轨迹**（动作之间是规划出来的衔接段，
        不是各播各的再回基线），讲解语音按每个动作在轨迹里的起始时刻拼成一条
        连续的 PCM，两边都按同一份时刻表走。

        `speed` 是这一把的速度倍率（页面上拉给的），不给就用配置里的默认值。
        """
        names = list(action_names)
        if not names:
            raise ControlError("action_required", "没有要回放的动作。")
        speed = self._settings.replay_speed if speed is None else float(speed)
        if not 0 < speed <= 2.0:
            raise ControlError("replay_speed_invalid", "回放速度必须在 (0, 2] 之间。")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise ControlError("control_busy", "已有控制会话在执行，拒绝并发接管。")
        actions = self._preflight(names, confirmed)
        self._cancel = threading.Event()
        self._pause_requested = threading.Event()
        label = names[0] if len(names) == 1 else f"整套 {len(names)} 段"
        self._set_phase(ControlPhase.PLANNING, f"正在规划 {label}")
        worker = threading.Thread(
            target=self._run,
            args=(names, actions, dict(audio_clips or {}), speed, kind),
            name="robot-gui-control",
            daemon=False,
        )
        with self._lock:
            self._worker = worker
            self._action_name = label
            self._current_speed = speed
            self._run_names = list(names)
            self._run_marks = []
            self._run_kind = kind
            self._play_started = None
        worker.start()
        return self.state()

    def release(self) -> dict[str, Any]:
        """请求停止当前会话：固定最后目标、降权重、交还控制权。"""
        self._cancel.set()
        with self._lock:
            if self._phase in (ControlPhase.IDLE, ControlPhase.FAULT):
                return self.state()
            self._phase = ControlPhase.RELEASING
            self._message = "正在交还控制权"
        return self.state()

    def pause(self) -> dict[str, Any]:
        """暂停当前回放：停语音，把机器人动态带回接管姿态，再交还控制权。

        **rt/arm_sdk 没有暂停原语**：它只认"每个控制周期发什么目标角和增益"，
        没有"停在这里"的概念。所以暂停是我们自己拼出来的三步：

        1. **停在这里**：把 studio 正在播的轨迹换成长度=当前已播时长的那一段，
           目标角连续不跳，studio 下一帧就认为这段播完了（动作停住，权重仍是 1）；
        2. **动态回到接管瞬间的姿态**：在同一个会话里播一段规划出来的连接段，
           从停住的位置回到接管时的姿态（腰也一起回基线）；
        3. **再交还控制权**：会话结束时 studio 用 2 秒把权重斜坡降到 0。

        这样机器人不会停在半个手势里交给固件，而是先自己走回基线再放手。
        接管全程没断过，腰的支撑力矩一直是我们在给。

        暂停时按轨迹时刻算出播到第几段：整课续播从**下一段**的语音和动作开始；
        单个动作续播重播这一段（它还没播完）。
        """
        with self._lock:
            if self._phase not in (
                ControlPhase.PLANNING,
                ControlPhase.ACQUIRING,
                ControlPhase.PLAYING,
            ):
                raise ControlError("control_idle", "当前没有可暂停的回放。")
            started = self._play_started
            session = self._session
            trajectory = self._trajectory
        elapsed = 0.0 if started is None else time.monotonic() - started
        remaining = self._remaining_after(elapsed)
        with self._lock:
            self._resume_names = tuple(remaining)
            self._resume_speed = self._current_speed
            self._resume_kind = self._run_kind
            self._pause_requested.set()
            self._phase = ControlPhase.RELEASING
            self._message = "正在回到接管姿态并交还控制权"
        if self._audio is not None:
            self._audio.stop()
        truncated = _truncate(trajectory, elapsed) if session is not None else None
        if truncated is not None:
            with self._lock:
                self._pause_trajectory = truncated
            # 换掉 studio 正在播的轨迹：它下一帧就把这一段播完，于是动作停住。
            # studio 没有公开的"截短"接口，只能换掉它手里的轨迹对象——
            # 换的是同类型的 Trajectory，不改子模块的代码（子模块在机器人上是
            # 单独同步的，改它要额外一步，docs/deployment-g1.md §2 有说明）。
            session._trajectory = truncated  # noqa: SLF001
        else:
            # 还在规划或刚接管：没有动作在播，直接中止这次会话。
            self._cancel.set()
        return self.state()

    def _play_return_to_baseline(
        self,
        session: RobotSession,
        model: Any,
        baseline: np.ndarray,
        cancel: threading.Event,
    ) -> None:
        """把机器人动态带回接管瞬间的姿态，然后才交还控制权。

        起点是**停住那一刻的目标角**（不是实测角）：实测角带着跟踪偏差，
        用它当起点等于让目标角跳一下，手臂会猛甩一下。
        """
        with self._lock:
            truncated = self._pause_trajectory
            original = self._trajectory
        if truncated is not None:
            source = np.asarray(truncated.last_q, dtype=float)
        elif original is not None:
            source = np.asarray(original.last_q, dtype=float)
        else:
            return
        target = np.asarray(baseline, dtype=float)
        if source.shape != target.shape or np.allclose(source, target):
            return
        # 回基线的路可能比动作之间的衔接段长，给它按距离留够时间：
        # 五次样条的峰值速度约 1.875×距离/时长，这里压在 ~1 rad/s 上下。
        distance = float(np.linalg.norm(target - source))
        seconds = max(self._settings.transition_seconds, 1.875 * distance)
        try:
            returning = connect(
                model,
                source,
                target,
                {"arms_waist"},
                seconds,
                cancel,
            )
        except Exception as error:  # noqa: BLE001 - 回不去也要把控制权交还
            logger.warning("回到接管姿态的连接段规划失败：{}", error)
            return
        if returning is None:
            return
        session.play(returning)

    def _remaining_after(self, elapsed: float) -> list[str]:
        """暂停后要接着播的动作：从**下一段**开始。

        marks 是每段动作**本体**的起始时刻，已经起播的段数就是"播到哪儿了"。
        整课：下一段（被打断的那段不重播）。单个动作：它自己——它还没播完，
        续播就是重播这一段。
        """
        with self._lock:
            names = list(self._run_names)
            marks = list(self._run_marks)
        played = sum(1 for mark in marks if mark <= elapsed)
        remaining = names[played:]
        if not remaining:
            remaining = names[max(0, played - 1) :]
        return remaining

    def resume(self) -> dict[str, Any]:
        """从暂停处继续：按当前实测姿态重新规划，只播还没播完的那几段。

        语音也从下一段重新起播：官方音频接口没有"从中间续播"这回事，
        同一个 stream_id 重推会让机器人把已播内容再播一遍。
        """
        with self._lock:
            if self._phase != ControlPhase.PAUSED:
                raise ControlError("not_paused", "当前没有可继续的暂停会话。")
            names = list(self._resume_names)
            speed = self._resume_speed
            kind = self._resume_kind
        if not names:
            raise ControlError("not_paused", "没有可继续的动作。")
        clips: dict[str, bytes] = {}
        for name in names:
            pcm = self._store.read_audio(name)
            if pcm is not None:
                clips[name] = pcm
        return self.request_replay(
            names,
            confirmed=True,
            audio_clips=clips,
            speed=speed,
            kind=kind,
        )

    def wait_idle(self, timeout: float | None = None) -> dict[str, Any]:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.state()

    # ---- 执行体 ----

    def _run(
        self,
        names: list[str],
        actions: list[Action],
        audio_clips: dict[str, bytes],
        speed: float,
        kind: str,
    ) -> None:
        cancel = self._cancel
        session: RobotSession | None = None
        audio_thread: threading.Thread | None = None
        monitor: TrackingMonitor | None = None
        lower: LowerBodyMonitor | None = None
        recorder = ExecutionRecorder(
            kind=kind,
            label=names[0] if len(names) == 1 else f"整套 {len(names)} 段",
            actions=names,
            speed=speed,
            slug=names[0] if kind == "action" else "course",
        )
        started = time.monotonic()
        outcome = OUTCOME_COMPLETED
        error_code = ""
        error_message = ""
        marks: list[float] = []
        try:
            self._set_phase(ControlPhase.PLANNING, "等待机器人反馈")
            # arms 组是 14 维动作，腰的三维在轨迹里恒等于接管瞬间的姿态；
            # 但腰的槽位仍要由我们下 PD + 重力前馈，否则躯干会前倾。
            gains = dict(self._settings.gains or DEFAULT_GAINS)
            with RobotSession(
                self._settings.interface,
                gains,
                cancel,
            ) as session:
                start: StartPose = session.start_pose
                if self._feedforward is not None:
                    # 腿部姿态取会话开始时的实测值：上肢动作的几秒内腿基本不动，
                    # 而腰关节的重力力矩对腿部姿态很敏感（直腿 vs 蹲姿差 3.5 Nm）。
                    self._feedforward.bind_legs(np.asarray(start.leg_q, dtype=float))
                self._set_phase(ControlPhase.PLANNING, "规划轨迹并做限位与避碰检查")
                model = ArmModel(start.leg_q)
                trajectory, marks = plan_sequence_with_marks(
                    actions,
                    start,
                    model,
                    speed,
                    self._settings.transition_seconds,
                    cancel,
                )
                with self._lock:
                    self._trajectory_duration = float(trajectory.duration)
                    self._run_marks = list(marks)
                self._set_phase(ControlPhase.ACQUIRING, "接管双臂控制权")
                session.acquire()
                if self._audio is not None and audio_clips:
                    clips = self._build_clips(names, marks, audio_clips)
                    if clips:
                        # 语音线程在接管后立刻起步，和轨迹同时开始；各段的推流
                        # 时刻由子进程按动作的起始时刻执行。
                        audio_thread = self._start_audio(clips, cancel)
                self._set_phase(
                    ControlPhase.PLAYING,
                    f"回放 {actions[0].group}，轨迹 {trajectory.duration:.1f} 秒",
                )
                monitor = TrackingMonitor(arm_joint_names())
                lower = LowerBodyMonitor(self._settings.lower_body_drift_deg)
                lower.capture_baseline(self._measured_named())
                monitor_thread = threading.Thread(
                    target=self._monitor_loop,
                    args=(
                        trajectory,
                        time.monotonic(),
                        monitor,
                        lower,
                        cancel,
                        self._settings.hold_soak_s,
                        recorder,
                    ),
                    name="robot-gui-tracking",
                    daemon=True,
                )
                monitor_thread.start()
                with self._lock:
                    # 轨迹时钟从这一刻起算：暂停时用它算"播到哪一段了"。
                    self._play_started = time.monotonic()
                    self._session = session
                    self._trajectory = trajectory
                session.play(trajectory)
                if self._pause_requested.is_set():
                    # 暂停：动作已经停在当前这一刻，接着把它动态带回接管姿态，
                    # 带回之后才交还控制权（接管全程没断，腰一直有支撑力矩）。
                    outcome = OUTCOME_PAUSED
                    self._pause_returning = True
                    try:
                        self._play_return_to_baseline(
                            session,
                            model,
                            start.upper_q,
                            cancel,
                        )
                    finally:
                        self._pause_returning = False
                # 沉降期：下盘漂移是延迟出现的，必须等它跑完再判定。
                monitor_thread.join(timeout=self._settings.hold_soak_s + 2.0)
                if lower.violated:
                    outcome = OUTCOME_FAULT
                    error_code = "lower_body_drift"
                    error_message = f"下盘漂移 {lower.report()['max_drift_deg']}° 超过阈值，已交还控制权"
                    self._set_phase(
                        ControlPhase.FAULT,
                        error_message,
                        error_code,
                    )
                else:
                    self._set_phase(ControlPhase.RELEASING, "回放完成，交还控制权")
                if audio_thread is not None:
                    audio_thread.join(timeout=5.0)
            # 会话退出 → studio 把接管权重在 2 秒内斜坡降到 0，控制权真正交还。
            if outcome == OUTCOME_PAUSED:
                self._set_phase(
                    ControlPhase.PAUSED,
                    "已暂停：机器人已回到接管姿态，控制权已交还",
                )
            elif outcome != OUTCOME_FAULT:
                # 交还控制权之后回到空闲；但下盘越限的故障状态要留住，别被覆盖掉。
                self._set_phase(ControlPhase.IDLE, "已交还控制权")
        except Exception as error:  # noqa: BLE001 - 控制路径必须把任何异常收敛成状态
            if cancel.is_set() and isinstance(error, InterruptedError):
                # 操作员按了停止：不是故障。
                outcome = OUTCOME_CANCELLED
                error_message = "已停止并交还控制权"
                self._set_phase(ControlPhase.IDLE, error_message)
            elif self._pause_requested.is_set():
                # 暂停是操作员主动的：不是故障，控制权已经交还。
                outcome = OUTCOME_PAUSED
                error_message = "已暂停"
                self._set_phase(
                    ControlPhase.PAUSED,
                    "已暂停：控制权已交还，可继续播放剩余动作",
                )
            else:
                outcome = OUTCOME_FAULT
                error_code = getattr(error, "code", "") or "control_failed"
                error_message = str(error) or type(error).__name__
                self._set_phase(ControlPhase.FAULT, error_message, error_code)
        finally:
            if audio_thread is not None and audio_thread.is_alive():
                cancel.set()
                audio_thread.join(timeout=2.0)
            if self._audio is not None and self._audio.playing:
                self._audio.stop()
            tracking = monitor.report() if monitor is not None else {}
            lower_body = lower.report() if lower is not None else {}
            with self._lock:
                self._tracking = tracking
                self._lower_body = lower_body
                self._worker = None
                self._session = None
                self._trajectory = None
                self._pause_trajectory = None
            self._write_record(
                recorder,
                outcome=outcome,
                elapsed_s=time.monotonic() - started,
                trajectory_duration_s=self._trajectory_duration,
                tracking=tracking,
                lower_body=lower_body,
                marks=marks,
                names=names,
                error_code=error_code,
                error_message=error_message,
            )

    def _write_record(
        self,
        recorder: ExecutionRecorder,
        *,
        outcome: str,
        elapsed_s: float,
        trajectory_duration_s: float,
        tracking: dict[str, Any],
        lower_body: dict[str, Any],
        marks: list[float],
        names: list[str],
        error_code: str,
        error_message: str,
    ) -> None:
        """把这次执行落成一条记录（项目目录里的 JSON，不上数据库）。"""
        if self._records is None:
            return
        segments = [
            {"name": name, "start_s": round(mark, 3)}
            for name, mark in zip(names, marks, strict=False)
        ]
        record = recorder.build(
            outcome=outcome,
            elapsed_s=elapsed_s,
            trajectory_duration_s=trajectory_duration_s,
            tracking=tracking,
            lower_body=lower_body,
            segments=segments,
            error_code=error_code,
            error_message=error_message,
        )
        try:
            self._records.write(record)
        except Exception:  # noqa: BLE001 - 记录写失败不能影响控制流程
            logger.exception("执行记录写入失败：{}", record.get("id"))

    @staticmethod
    def _build_clips(
        names: list[str], marks: list[float], clips: dict[str, bytes]
    ) -> list[tuple[float, bytes]]:
        """各段讲解按动作在轨迹里的起始时刻排好。

        时刻是**相对轨迹 t=0 的绝对值**：语音线程在接管后立刻起步（和轨迹同时
        开始），子进程自己按这些时刻把一段段推出去——每一段是独立的语音，
        各自用自己的 stream_id，段与段之间不会互相打断。
        """
        return [
            (mark, clips[name])
            for name, mark in zip(names, marks, strict=True)
            if name in clips
        ]

    def _start_audio(
        self,
        clips: list[tuple[float, bytes]],
        cancel: threading.Event,
    ) -> threading.Thread:
        """让讲解语音与动作本体对齐：每段语音在它自己那一段动作开始的时刻起播。

        **只走课程包原声的 PCM 推流，不做 TTS 兜底。** 兜底播的是云端合成的另一套
        音色，和课程包对不上；讲不出原声就不出声，比出个错的音色好。
        """
        assert self._audio is not None

        def worker() -> None:
            error = self._audio.play(clips, cancel)
            if error:
                if self._pause_requested.is_set():
                    # 暂停时是我们主动停的语音，不是推流故障。
                    logger.info("讲解语音已随暂停中断（{}）", error)
                else:
                    logger.warning("PCM 推流失败（{}）", error)
                return
            logger.info("讲解语音播放完成（{} 段）", len(clips))

        thread = threading.Thread(target=worker, name="robot-gui-audio", daemon=True)
        thread.start()
        return thread


def arm_joint_names() -> tuple[str, ...]:
    """被接管的双臂 14 关节名（studio 顺序：左臂 7 + 右臂 7）。"""
    from g1_action_studio.model import GROUPS, UPPER_JOINTS

    return tuple(UPPER_JOINTS[index].replace("_joint", "") for index in GROUPS["arms"])


def _pitch_of(rpy: list[float] | None) -> float | None:
    """IMU 的 pitch（rpy 的第二个分量，弧度）；拿不到就返回 None，不猜。"""
    if rpy is None or len(rpy) < 2:
        return None
    value = rpy[1]
    return float(value) if isinstance(value, (int, float)) else None


def _restrict_polynomial(polynomial: Any, end: float) -> Any:
    """把样条限制到 [x0, end]：整段区间系数照搬，最后一段换基到新端点。

    scipy 的 `PPoly` 没有 slice 方法（1.16 也没有），所以自己换基：
    区间内的写法是 p(t) = Σ c_j (t - x_i)^j，令 u = t - end 就有
    t - x_i = u + shift，展开后 new[m] = Σ_{j≥m} c_j C(j,m) shift^(j-m)。
    """
    x = np.asarray(polynomial.x, dtype=float)
    c = np.asarray(polynomial.c, dtype=float)
    interval = int(np.searchsorted(x, end, side="right")) - 1
    interval = min(max(interval, 0), len(x) - 2)
    shift = end - x[interval]
    if shift <= 0:
        # end 正好落在断点上：前面那些整段原样保留。
        return PPoly(
            c[:, :interval], x[: interval + 1], extrapolate=polynomial.extrapolate
        )
    degree = c.shape[0] - 1
    # c 的形状是 (阶数, 区间数, 关节维度)，最后一段要按每个关节分别换基。
    last = np.zeros_like(c[:, interval])
    for j in range(degree + 1):
        for m in range(j + 1):
            last[m] += c[j, interval] * comb(j, m) * shift ** (j - m)
    new_x = np.concatenate([x[: interval + 1], [end]])
    new_c = np.concatenate([c[:, :interval], last[:, None, :]], axis=1)
    return PPoly(new_c, new_x, extrapolate=polynomial.extrapolate)


def _truncate(trajectory: Any, elapsed: float) -> Trajectory | None:
    """把轨迹截成"已经播过的那一段"，用来让动作停在当前这一刻。

    studio 下一帧再采样这条轨迹时，采样时刻已经越过它的时长，于是它认为
    这一段播完了（动作停住、目标角保持在当前位置，不会跳）。
    """
    if trajectory is None:
        return None
    duration = float(getattr(trajectory, "duration", 0.0))
    if not 0.05 < elapsed < duration:
        return None
    polynomial = _restrict_polynomial(trajectory.polynomial, elapsed)
    position = np.asarray(polynomial(elapsed), dtype=float)
    return Trajectory(polynomial, elapsed, trajectory.first_q, position)


def measured_pose_provider(hub: Any):
    """把遥测快照适配成重力前馈要的 (腿部 12 维, 上肢 17 维) 实测关节角。"""
    from g1_action_studio.model import LEG_JOINTS, UPPER_JOINTS

    def provider() -> tuple[np.ndarray, np.ndarray] | None:
        angles = hub.latest_joint_angles()
        if not angles:
            return None
        try:
            leg = np.array([angles[name.replace("_joint", "")] for name in LEG_JOINTS])
            upper = np.array(
                [angles[name.replace("_joint", "")] for name in UPPER_JOINTS]
            )
        except KeyError:
            return None
        if not (np.isfinite(leg).all() and np.isfinite(upper).all()):
            return None
        return leg, upper

    return provider


def load_gains(path: Path | None) -> dict[str, dict[str, float]] | None:
    """从 JSON 读增益配置；缺省时用内置值（arms 120/3.5、waist 300/5）。

    需要 arms 与 waist 两组：腰虽然不由动作驱动，但它的槽位由我们下发，
    必须有刚度（kp）才撑得住躯干。teach 组是手掰示教用的，目前未开放。
    """
    if path is None:
        return None
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "arms" not in raw:
        raise ValueError("增益配置必须包含 arms 组")
    unknown = set(raw) - {"arms", "waist"}
    if unknown:
        raise ValueError(f"增益配置含未知分组：{', '.join(sorted(unknown))}")
    for group, values in raw.items():
        if not isinstance(values, dict) or set(values) != {
            "hold_kp",
            "hold_kd",
            "teach_kp",
            "teach_kd",
        }:
            raise ValueError(f"增益配置 {group} 缺少 hold/teach 的 kp/kd")
        for name, value in values.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"增益配置 {group}.{name} 必须是非负数")
    return raw
