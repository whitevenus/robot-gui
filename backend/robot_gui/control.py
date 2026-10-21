"""真机控制会话：接管双臂、播放动作、返回基线、交还控制权。

控制链路复用 g1-action-studio 的模块（不是它的 CLI）：
  - RobotSession  负责 rt/arm_sdk 的 LowCmd 发送、weight 0→1→0 的接管与交还、
                  以及异常路径上的降权重（这正是 2026-10-16 事故复盘里修好的两条）
  - plan_sequence 负责「从当前实测姿态 → 动作首帧 → 动作 → 返回基线」的连续轨迹，
                  并在发送任何控制命令之前对整条样条做关节限位与自碰撞判定

**接管范围只有双臂（14 关节）。** 腰（slot 12-14）和腿（slot 0-11）都不下发，
由机器人内置运控负责：arm_sdk 一旦接管腰，内置的腰部平衡回路就退让，躯干会前倾
（2026-10-20 真机实测：腰 14 从 -1.49° 前倾到 +10.01°，动作结束后仍在恶化）。

顺序是刻意的：先在只读状态下规划并校验，规划失败就一个控制命令都不发；
规划通过后才 acquire()（创建 rt/arm_sdk 发布器）并播放。

同一时刻只允许一个控制会话。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from g1_action_studio.model import ArmModel
from g1_action_studio.motion import Action, StartPose
from g1_action_studio.planner import plan_sequence
from g1_action_studio.robot import RobotSession

from .actions import ActionStore, ActionStoreError
from .audio import G1AudioPort, pcm_duration_s
from .gravity import GravityFeedforward, install_gravity_command_builder
from .profile import LOWER_BODY_JOINT_NAMES

logger = logging.getLogger(__name__)

#: 本工作台只接管双臂（14 关节）。腰与腿都不下发，由机器人内置运控负责。
ARM_JOINT_COUNT = 14

#: 与 g1-action-studio 的示例增益一致：保持阶段 kp/kd = 60/1.5。
DEFAULT_GAINS: dict[str, dict[str, float]] = {
    "arms": {"hold_kp": 60.0, "hold_kd": 1.5, "teach_kp": 20.0, "teach_kd": 1.5},
    # 腰不接管。waist 组只是为了满足 studio 的增益结构（它按 arms/waist 两组拼 17 维），
    # 腰的槽位根本不会下发，这里的值不产生任何作用。
    "waist": {"hold_kp": 0.0, "hold_kd": 0.0, "teach_kp": 0.0, "teach_kd": 0.0},
}

#: 官方接口下 12-28 槽位为腰部和上肢电机参数，29.q 为 0-1 权重；
#: 腿部 0-11 由内置运控负责，本工作台不接管；腰部 12-14 同样不接管。
UPPER_JOINT_COUNT = 17


class ControlPhase(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    ACQUIRING = "acquiring"
    PLAYING = "playing"
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
    #: 腰槽位写入的 motor_cmd.mode。0=不接管腰（默认）；1=诊断档，arm_sdk 名义上
    #: 接管腰但增益仍为 0，用来在真机上判别 mode 语义。1 有让腰瘫掉的风险。
    waist_slot_mode: int = 0
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
        error = np.abs(np.asarray(target, dtype=float) - np.asarray(measured, dtype=float))
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

    def capture_baseline(self, angles: dict[str, float]) -> None:
        self._baseline = {
            name: value
            for name, value in angles.items()
            if name in self._joint_names
        }
        self._max_drift = 0.0
        self._max_joint = ""
        self._waist_pitch_drift = 0.0

    def update(self, angles: dict[str, float]) -> float:
        if not self._baseline:
            return 0.0
        worst = 0.0
        worst_joint = ""
        for name in self._joint_names:
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
        if waist is not None and "waist_pitch" in self._baseline:
            self._waist_pitch_drift = float(
                np.degrees(abs(waist - self._baseline["waist_pitch"]))
            )
        return drift_deg

    @property
    def violated(self) -> bool:
        return self._max_drift > self._threshold

    def report(self) -> dict[str, Any]:
        return {
            "threshold_deg": self._threshold,
            "max_drift_deg": round(self._max_drift, 3),
            "max_drift_joint": self._max_joint,
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
    ) -> None:
        self._settings = settings
        self._telemetry = telemetry
        self._store = action_store
        self._audio = audio_port
        self._feedforward = feedforward
        # 必须是可重入锁：release() 会在持锁状态下调用 state()。
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._phase = ControlPhase.IDLE
        self._message = ""
        self._action_name = ""
        self._error_code = ""
        self._worker: threading.Thread | None = None
        self._trajectory_duration = 0.0
        self._tracking: dict[str, Any] = {}
        self._lower_body: dict[str, Any] = {}
        # 必须无条件安装：补丁同时负责"只接管双臂"和重力前馈。
        # 关掉重力前馈时不装它，就会退回 studio 原版 build_command 把腰一起接管。
        install_gravity_command_builder(feedforward, self._settings.waist_slot_mode)

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
    ) -> None:
        deadline = started + trajectory.duration + soak_s
        while not cancel.is_set():
            elapsed = time.monotonic() - started
            if elapsed > trajectory.duration:
                # 动作结束后的沉降期：漂移是延迟出现的，只看动作期间抓不到。
                lower.update(self._measured_named())
                if elapsed > deadline:
                    return
            else:
                target, _ = trajectory.sample(elapsed)
                # studio 的 upper_q 顺序是双臂在前、腰在后，只取前 14 位与实测对齐。
                arms_target = np.asarray(target, dtype=float)[:ARM_JOINT_COUNT]
                measured = self._measured_arms()
                monitor.update(arms_target, measured)
                lower.update(self._measured_named())
            cancel.wait(0.02)

    def _set_phase(self, phase: ControlPhase, message: str = "", error_code: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._message = message
            self._error_code = error_code

    # ---- 前置检查 ----

    def _preflight(self, action_name: str, confirmed: bool) -> Action:
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
            raise ControlError("fsm_unknown", "读不到机器人 FSM 状态，拒绝下发控制命令。")
        if fsm_id not in self._settings.fsm_whitelist:
            raise ControlError(
                "fsm_not_allowed",
                f"FSM {fsm_id} 不在允许列表 {list(self._settings.fsm_whitelist)} 中。",
            )
        try:
            action = self._store.load(action_name)
        except ActionStoreError as error:
            raise ControlError(error.code, str(error)) from error
        if action.group != "arms":
            raise ControlError(
                "action_group_unsupported",
                f"动作组 {action.group} 不被接管：本工作台只接管双臂（arms 组 14 维），"
                "腰与腿由机器人内置运控负责。",
            )
        return action

    # ---- 控制 ----

    def request_replay(
        self,
        action_name: str,
        *,
        confirmed: bool,
        audio_pcm: bytes | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise ControlError("control_busy", "已有控制会话在执行，拒绝并发接管。")
        action = self._preflight(action_name, confirmed)
        self._cancel = threading.Event()
        self._set_phase(ControlPhase.PLANNING, f"正在规划 {action_name}")
        worker = threading.Thread(
            target=self._run,
            args=(action, audio_pcm),
            name="robot-gui-control",
            daemon=False,
        )
        with self._lock:
            self._worker = worker
            self._action_name = action_name
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

    def wait_idle(self, timeout: float | None = None) -> dict[str, Any]:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.state()

    # ---- 执行体 ----

    def _run(
        self,
        action: Action,
        audio_pcm: bytes | None,
    ) -> None:
        cancel = self._cancel
        session: RobotSession | None = None
        audio_thread: threading.Thread | None = None
        try:
            self._set_phase(ControlPhase.PLANNING, "等待机器人反馈")
            gains = dict(self._settings.gains or DEFAULT_GAINS)
            # 腰不接管：即使配置里还留着 waist 组也一律作废，免得以后误以为腰受控。
            gains["waist"] = dict(DEFAULT_GAINS["waist"])
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
                trajectory = plan_sequence(
                    [action],
                    start,
                    model,
                    self._settings.replay_speed,
                    self._settings.transition_seconds,
                    cancel,
                )
                with self._lock:
                    self._trajectory_duration = float(trajectory.duration)
                self._set_phase(ControlPhase.ACQUIRING, "接管双臂控制权")
                session.acquire()
                if audio_pcm and self._audio is not None:
                    audio_thread = self._start_audio(audio_pcm, cancel)
                self._set_phase(
                    ControlPhase.PLAYING,
                    f"回放 {action.group}，轨迹 {trajectory.duration:.1f} 秒",
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
                    ),
                    name="robot-gui-tracking",
                    daemon=True,
                )
                monitor_thread.start()
                session.play(trajectory)
                # 沉降期：下盘漂移是延迟出现的，必须等它跑完再判定。
                monitor_thread.join(timeout=self._settings.hold_soak_s + 2.0)
                with self._lock:
                    self._tracking = monitor.report()
                    self._lower_body = lower.report()
                if lower.violated:
                    self._set_phase(
                        ControlPhase.FAULT,
                        f"下盘漂移 {lower.report()['max_drift_deg']}° 超过阈值，"
                        "已交还控制权",
                        "lower_body_drift",
                    )
                if audio_thread is not None:
                    audio_thread.join(timeout=5.0)
                self._set_phase(ControlPhase.RELEASING, "回放完成，交还控制权")
            self._set_phase(ControlPhase.IDLE, "已交还控制权")
        except Exception as error:  # noqa: BLE001 - 控制路径必须把任何异常收敛成状态
            code = getattr(error, "code", "") or "control_failed"
            message = str(error) or type(error).__name__
            self._set_phase(ControlPhase.FAULT, message, code)
        finally:
            if audio_thread is not None and audio_thread.is_alive():
                cancel.set()
                audio_thread.join(timeout=2.0)
            if self._audio is not None and self._audio.playing:
                self._audio.stop()
            with self._lock:
                self._worker = None

    def _start_audio(
        self,
        pcm: bytes,
        cancel: threading.Event,
    ) -> threading.Thread:
        """让讲解语音与动作本体对齐：过渡段结束后再起播。

        **只走课程包原声的 PCM 推流，不做 TTS 兜底。** 兜底播的是云端合成的另一套
        音色，和课程包对不上；讲不出原声就不出声，比出个错的音色好。
        """
        assert self._audio is not None

        def worker() -> None:
            if cancel.wait(self._settings.transition_seconds):
                return
            error = self._audio.play(pcm, cancel)
            if error:
                logger.warning("PCM 推流失败（%s）", error)
                return
            logger.info("讲解语音播放完成（PCM 推流 %.2f 秒）", pcm_duration_s(pcm))

        thread = threading.Thread(target=worker, name="robot-gui-audio", daemon=True)
        thread.start()
        return thread


def arm_joint_names() -> tuple[str, ...]:
    """被接管的双臂 14 关节名（studio 顺序：左臂 7 + 右臂 7）。"""
    from g1_action_studio.model import GROUPS, UPPER_JOINTS

    return tuple(
        UPPER_JOINTS[index].replace("_joint", "") for index in GROUPS["arms"]
    )


def measured_pose_provider(hub: Any):
    """把遥测快照适配成重力前馈要的 (腿部 12 维, 上肢 17 维) 实测关节角。"""
    from g1_action_studio.model import LEG_JOINTS, UPPER_JOINTS

    def provider() -> tuple[np.ndarray, np.ndarray] | None:
        angles = hub.latest_joint_angles()
        if not angles:
            return None
        try:
            leg = np.array([angles[name.replace("_joint", "")] for name in LEG_JOINTS])
            upper = np.array([angles[name.replace("_joint", "")] for name in UPPER_JOINTS])
        except KeyError:
            return None
        if not (np.isfinite(leg).all() and np.isfinite(upper).all()):
            return None
        return leg, upper

    return provider


def load_gains(path: Path | None) -> dict[str, dict[str, float]] | None:
    """从 JSON 读增益配置；缺省时用内置的 hold 60/1.5。

    只接管双臂，所以只要求 arms 一组。waist 组是接管范围收窄之前的遗留，
    填了也不生效（腰的槽位不会下发），留着只是为了兼容旧的配置文件。
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
            "hold_kp", "hold_kd", "teach_kp", "teach_kd"
        }:
            raise ValueError(f"增益配置 {group} 缺少 hold/teach 的 kp/kd")
        for name, value in values.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"增益配置 {group}.{name} 必须是非负数")
    return raw
