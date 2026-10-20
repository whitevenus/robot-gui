"""真机控制会话：接管上肢、播放动作、返回基线、交还控制权。

控制链路复用 g1-action-studio 的模块（不是它的 CLI）：
  - RobotSession  负责 rt/arm_sdk 的 LowCmd 发送、weight 0→1→0 的接管与交还、
                  以及异常路径上的降权重（这正是 2026-10-16 事故复盘里修好的两条）
  - plan_sequence 负责「从当前实测姿态 → 动作首帧 → 动作 → 返回基线」的连续轨迹，
                  并在发送任何控制命令之前对整条样条做关节限位与自碰撞判定

顺序是刻意的：先在只读状态下规划并校验，规划失败就一个控制命令都不发；
规划通过后才 acquire()（创建 rt/arm_sdk 发布器）并播放。

同一时刻只允许一个控制会话。
"""
from __future__ import annotations

import threading
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
from .audio import G1AudioPort
from .gravity import GravityFeedforward, install_gravity_command_builder

# 与 g1-action-studio 的示例增益一致：保持阶段 kp/kd = 60/1.5。
DEFAULT_GAINS: dict[str, dict[str, float]] = {
    "arms": {"hold_kp": 60.0, "hold_kd": 1.5, "teach_kp": 20.0, "teach_kd": 1.5},
    "waist": {"hold_kp": 60.0, "hold_kd": 1.5, "teach_kp": 20.0, "teach_kd": 1.5},
}

# 官方接口下 12-28 槽位为腰部和上肢电机参数，29.q 为 0-1 权重；
# 腿部 0-11 由内置运控负责，本工作台不接管。
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
    gains: dict[str, dict[str, float]] | None = None

    def validate(self) -> None:
        if self.transition_seconds <= 0:
            raise ValueError("transition-seconds must be positive")
        if not 0 < self.replay_speed <= 2.0:
            raise ValueError("replay-speed must be in (0, 2]")


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
        if feedforward is not None:
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
            }

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
            return self._store.load(action_name)
        except ActionStoreError as error:
            raise ControlError(error.code, str(error)) from error

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

    def _run(self, action: Action, audio_pcm: bytes | None) -> None:
        cancel = self._cancel
        session: RobotSession | None = None
        audio_thread: threading.Thread | None = None
        try:
            self._set_phase(ControlPhase.PLANNING, "等待机器人反馈")
            with RobotSession(
                self._settings.interface,
                self._settings.gains or DEFAULT_GAINS,
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
                self._set_phase(ControlPhase.ACQUIRING, "接管上肢控制权")
                session.acquire()
                if audio_pcm and self._audio is not None:
                    audio_thread = self._start_audio(audio_pcm, cancel)
                self._set_phase(
                    ControlPhase.PLAYING,
                    f"回放 {action.group}，轨迹 {trajectory.duration:.1f} 秒",
                )
                session.play(trajectory)
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

    def _start_audio(self, pcm: bytes, cancel: threading.Event) -> threading.Thread:
        """让讲解语音与动作本体对齐：过渡段结束后再起播。"""
        assert self._audio is not None

        def worker() -> None:
            if cancel.wait(self._settings.transition_seconds):
                return
            self._audio.play(pcm, cancel)

        thread = threading.Thread(target=worker, name="robot-gui-audio", daemon=True)
        thread.start()
        return thread


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
    """从 JSON 读增益配置；缺省时用内置的 hold 60/1.5。"""
    if path is None:
        return None
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"arms", "waist"}:
        raise ValueError("增益配置必须且只能包含 arms 与 waist 两组")
    for group, values in raw.items():
        if not isinstance(values, dict) or set(values) != {
            "hold_kp", "hold_kd", "teach_kp", "teach_kd"
        }:
            raise ValueError(f"增益配置 {group} 缺少 hold/teach 的 kp/kd")
        for name, value in values.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"增益配置 {group}.{name} 必须是非负数")
    return raw
