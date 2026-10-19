from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

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
        self._subscribe(
            ChannelSubscriber(self._topics["bms"], BmsState_), self._on_bms
        )
        self._subscribe(
            ChannelSubscriber(self._topics["torso_imu"], IMUState_),
            self._on_secondary_imu,
        )
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
            except Exception as error:
                logging.exception("Failed to close a Unitree telemetry subscriber.")
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
            except Exception as error:
                logging.exception("Failed to poll the G1 FSM state.")
                self._report_error("fsm", str(error))
            self._stop.wait(0.2)

    def _report_error(self, source: str, error: str) -> None:
        if self._publish_error is not None:
            self._publish_error(source, error)
