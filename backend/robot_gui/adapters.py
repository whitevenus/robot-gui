from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any, Protocol

from loguru import logger

from .profile import G1_29DOF_JOINTS

TelemetryCallback = Callable[[str, dict[str, Any]], None]
TelemetryErrorCallback = Callable[[str, str], None]


class TelemetryAdapter(Protocol):
    def start(
        self,
        publish: TelemetryCallback,
        publish_error: TelemetryErrorCallback,
    ) -> None: ...

    def stop(self) -> None: ...

    def switch_mode(self, fsm_id: int) -> tuple[int, str]:
        """请求切换到指定 FSM ID 的模式。返回 (错误码, 补充说明)。"""
        ...

    def refresh_mode(self) -> dict[str, Any]:
        """立刻回读当前 FSM 状态。"""
        ...


class UnitreeSdkTelemetryAdapter:
    def __init__(
        self,
        interface: str,
        low_state_topic: str,
        bms_topic: str,
        secondary_imu_topic: str,
    ) -> None:
        self._interface = interface
        self._topics = {
            "low_state": low_state_topic,
            "bms": bms_topic,
            "torso_imu": secondary_imu_topic,
        }
        self._publish: TelemetryCallback | None = None
        self._publish_error: TelemetryErrorCallback | None = None
        self._subscribers: list[Any] = []
        self._stop = threading.Event()
        self._fsm_thread: threading.Thread | None = None
        self._loco_client: Any = None

    def start(
        self,
        publish: TelemetryCallback,
        publish_error: TelemetryErrorCallback,
    ) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import (
            BmsState_,
            IMUState_,
            LowState_,
        )

        self._publish = publish
        self._publish_error = publish_error
        ChannelFactoryInitialize(0, self._interface)
        self._subscribe(
            ChannelSubscriber(self._topics["low_state"], LowState_),
            self._on_low_state,
        )
        self._subscribe(ChannelSubscriber(self._topics["bms"], BmsState_), self._on_bms)
        self._subscribe(
            ChannelSubscriber(self._topics["torso_imu"], IMUState_),
            self._on_secondary_imu,
        )
        # LocoClient 这条 RPC 通道同时负责读 FSM（GetFsmId）和切 FSM（SetFsmId），
        # 是官方《高层运动服务接口》指定的整机控制接口。初始化它不会改变机器人模式。
        self._loco_client = LocoClient()
        self._loco_client.SetTimeout(1.0)
        self._loco_client.Init()
        self._stop.clear()
        self._fsm_thread = threading.Thread(
            target=self._poll_fsm, name="g1-fsm-telemetry", daemon=True
        )
        self._fsm_thread.start()

    def stop(self) -> None:
        self._stop.set()
        close_errors: list[Exception] = []
        if self._fsm_thread is not None:
            self._fsm_thread.join(timeout=2)
            if self._fsm_thread.is_alive():
                close_errors.append(
                    RuntimeError("G1 FSM telemetry worker did not stop.")
                )
            else:
                self._fsm_thread = None
        for subscriber in reversed(self._subscribers):
            try:
                subscriber.Close()
            except Exception as error:  # noqa: BLE001 - 清理失败要收集起来统一抛
                logger.exception("Failed to close a Unitree telemetry subscriber.")
                close_errors.append(error)
        self._subscribers.clear()
        if close_errors:
            raise ExceptionGroup("Unitree telemetry cleanup failed.", close_errors)

    def _subscribe(self, subscriber: Any, callback: Callable[[Any], None]) -> None:
        self._subscribers.append(subscriber)
        subscriber.Init(callback, 1)

    def _on_low_state(self, message: Any) -> None:
        motors = message.motor_state
        if len(motors) < 29:
            self._report_error(
                "low_state",
                f"LowState motor_state has {len(motors)} entries; expected at least 29.",
            )
            return
        joints = []
        for joint in G1_29DOF_JOINTS:
            motor = motors[joint.sdk_slot]
            joints.append(
                {
                    "sdk_slot": joint.sdk_slot,
                    "q_rad": float(motor.q),
                    "dq_rad_s": float(motor.dq),
                    "ddq_rad_s2": float(motor.ddq),
                    "tau_est_nm": float(motor.tau_est),
                    "temperature_raw": list(motor.temperature),
                    "voltage_v": float(motor.vol),
                    "mode": int(motor.mode),
                    "status_code": int(motor.motorstate),
                }
            )
        self._publish(
            "low_state",
            {"joints": joints, "mode_machine": int(message.mode_machine)},
        )
        self._publish("hip_imu", self._imu_payload(message.imu_state))

    def _on_bms(self, message: Any) -> None:
        self._publish(
            "bms",
            {
                "soc_pct": int(message.soc),
                "soh_pct": int(message.soh),
                "current_raw": int(message.current),
                "voltage_raw": list(message.bmsvoltage),
                "temperature_raw": list(message.temperature),
                "cycle_count": int(message.cycle),
            },
        )

    def _on_secondary_imu(self, message: Any) -> None:
        self._publish("torso_imu", self._imu_payload(message))

    @staticmethod
    def _imu_payload(message: Any) -> dict[str, Any]:
        return {
            "quaternion": list(message.quaternion),
            "gyroscope": list(message.gyroscope),
            "accelerometer": list(message.accelerometer),
            "rpy": list(message.rpy),
            "temperature_raw": int(message.temperature),
        }

    def _poll_fsm(self) -> None:
        while not self._stop.is_set():
            try:
                result, fsm_id = self._loco_client.GetFsmId()
                if result != 0 or fsm_id is None:
                    self._report_error("fsm", f"GetFsmId returned {result}.")
                else:
                    self._publish("fsm", {"id": int(fsm_id), "mode": None})
            except Exception as error:  # noqa: BLE001 - 轮询失败要上报给前端
                logger.exception("Failed to poll the G1 FSM state.")
                self._report_error("fsm", str(error))
            self._stop.wait(0.2)

    # ---- FSM 模式切换 ----

    def refresh_mode(self) -> dict[str, Any]:
        """立刻回读当前 FSM 状态（fsm_id + fsm_mode）。"""
        fsm_id: int | None = None
        try:
            result, value = self._loco_client.GetFsmId()
            if result == 0 and value is not None:
                fsm_id = int(value)
        except Exception:  # noqa: BLE001 - 回读失败降级成"未知"，不能打断调用方
            logger.exception("回读 FSM ID 失败。")
        return {"id": fsm_id, "mode": self._read_fsm_mode()}

    def _read_fsm_mode(self) -> int | None:
        """回读 fsm_mode：0=静态（允许切换到其他模式），1=动态（固件拒绝大部分切换）。

        SDK 的 LocoClient 没有暴露 GetFsmMode，只能走底层的通用 RPC 通道
        （ROBOT_API_ID_LOCO_GET_FSM_MODE = 7002），返回 `{"data": 0|1}`。
        """
        try:
            from unitree_sdk2py.g1.loco.g1_loco_api import (
                ROBOT_API_ID_LOCO_GET_FSM_MODE,
            )

            result, data = self._loco_client._Call(  # noqa: SLF001 - SDK 没有 GetFsmMode，只能走底层 _Call
                ROBOT_API_ID_LOCO_GET_FSM_MODE, "{}"
            )
            if result != 0 or not data:
                return None
            return int(json.loads(data).get("data"))
        except Exception:  # noqa: BLE001 - 回读失败降级成"未知模式"，不能打断切换流程
            logger.opt(exception=True).debug("回读 fsm_mode 失败。")
            return None

    def switch_mode(self, fsm_id: int) -> tuple[int, str]:
        """请求切换到指定 FSM ID 的模式。返回 (错误码, 补充说明)。

        官方《高层运动服务接口》的专家接口 `SetFsmId(int)`。注意它返回 0 只代表
        内置运控接收了请求，真正的模式切换是否成功要看内置运控的状态切换逻辑，
        所以调用方必须再回读一次 `GetFsmId()` 核对。
        """
        try:
            code = self._loco_client.SetFsmId(int(fsm_id))
        except Exception as error:  # noqa: BLE001 - 切换失败要收敛成错误码返回
            logger.exception("切换 FSM 模式失败：{}", fsm_id)
            return -1, str(error)
        return int(code), ""

    def _report_error(self, source: str, error: str) -> None:
        if self._publish_error is not None:
            self._publish_error(source, error)
