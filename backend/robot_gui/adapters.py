from __future__ import annotations

import logging
import math
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


class FakeTelemetryAdapter:
    def __init__(self, interval_seconds: float = 0.1) -> None:
        self._interval_seconds = interval_seconds
        self._publish: TelemetryCallback | None = None
        self._paused: set[str] = set()
        self._pause_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0

    def start(
        self,
        publish: TelemetryCallback,
        publish_error: TelemetryErrorCallback,
    ) -> None:
        del publish_error
        if self._thread is not None:
            return
        self._publish = publish
        self._started = time.monotonic()
        self._stop.clear()
        self._emit(time.monotonic(), force_all=True)
        self._thread = threading.Thread(
            target=self._run, name="fake-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("Fake telemetry worker did not stop.")
            self._thread = None

    def pause_source(self, source: str, paused: bool = True) -> None:
        with self._pause_lock:
            if paused:
                self._paused.add(source)
            else:
                self._paused.discard(source)

    def _is_paused(self, source: str) -> bool:
        with self._pause_lock:
            return source in self._paused

    def _emit(self, now: float, force_all: bool = False) -> None:
        if self._publish is None:
            return
        elapsed = now - self._started
        if force_all or not self._is_paused("low_state"):
            joints = []
            for joint in G1_29DOF_JOINTS:
                phase = elapsed + joint.sdk_slot * 0.13
                q = 0.1 * math.sin(phase)
                joints.append(
                    {
                        "sdk_slot": joint.sdk_slot,
                        "q_rad": q,
                        "dq_rad_s": 0.1 * math.cos(phase),
                        "ddq_rad_s2": -0.1 * math.sin(phase),
                        "tau_est_nm": 0.2 * math.cos(phase),
                        "temperature_raw": [32, 33],
                        "voltage_v": 48.0,
                        "mode": 1,
                        "status_code": 0,
                    }
                )
            self._publish(
                "low_state",
                {
                    "joints": joints,
                    "mode_machine": 2,
                },
            )
        if force_all or not self._is_paused("hip_imu"):
            self._publish("hip_imu", self._fake_imu(elapsed))
        if force_all or not self._is_paused("torso_imu"):
            self._publish("torso_imu", self._fake_imu(elapsed + 0.2))
        if force_all or not self._is_paused("bms"):
            self._publish(
                "bms",
                {
                    "soc_pct": 92,
                    "soh_pct": 100,
                    "current_raw": 0,
                    "voltage_raw": [50000, 50000, 50000],
                    "temperature_raw": [25] * 12,
                    "cycle_count": 12,
                },
            )
        if force_all or not self._is_paused("fsm"):
            self._publish("fsm", {"id": 500, "mode": None})

    @staticmethod
    def _fake_imu(phase: float) -> dict[str, Any]:
        return {
            "quaternion": [1.0, 0.0, 0.0, 0.0],
            "gyroscope": [0.0, 0.0, 0.0],
            "accelerometer": [0.0, 0.0, 9.81],
            "rpy": [0.01 * math.sin(phase), 0.0, 0.0],
            "temperature_raw": 30,
        }

    def _run(self) -> None:
        last_bms = 0.0
        last_fsm = 0.0
        while not self._stop.wait(self._interval_seconds):
            now = time.monotonic()
            self._emit_fast_sources(now)
            if now - last_bms >= 1.0:
                if not self._is_paused("bms"):
                    self._emit_source("bms", now)
                last_bms = now
            if now - last_fsm >= 0.2:
                if not self._is_paused("fsm"):
                    self._emit_source("fsm", now)
                last_fsm = now

    def _emit_fast_sources(self, now: float) -> None:
        if self._publish is None:
            return
        elapsed = now - self._started
        if not self._is_paused("low_state"):
            self._publish(
                "low_state",
                {
                    "joints": [
                        {
                            "sdk_slot": joint.sdk_slot,
                            "q_rad": 0.1
                            * math.sin(elapsed + joint.sdk_slot * 0.13),
                            "dq_rad_s": 0.0,
                            "ddq_rad_s2": 0.0,
                            "tau_est_nm": 0.0,
                            "temperature_raw": [32, 33],
                            "voltage_v": 48.0,
                            "mode": 1,
                            "status_code": 0,
                        }
                        for joint in G1_29DOF_JOINTS
                    ],
                    "mode_machine": 2,
                },
            )
        if not self._is_paused("hip_imu"):
            self._publish("hip_imu", self._fake_imu(elapsed))
        if not self._is_paused("torso_imu"):
            self._publish("torso_imu", self._fake_imu(elapsed + 0.2))

    def _emit_source(self, source: str, now: float) -> None:
        if self._publish is None:
            return
        if source == "bms":
            self._publish(
                source,
                {
                    "soc_pct": 92,
                    "soh_pct": 100,
                    "current_raw": 0,
                    "voltage_raw": [50000, 50000, 50000],
                    "temperature_raw": [25] * 12,
                    "cycle_count": 12,
                },
            )
        elif source == "fsm":
            self._publish(source, {"id": 500, "mode": None})


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
        if self._fsm_thread is not None:
            self._fsm_thread.join(timeout=2)
            if self._fsm_thread.is_alive():
                raise RuntimeError("G1 FSM telemetry worker did not stop.")
            self._fsm_thread = None
        close_errors: list[Exception] = []
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
        subscriber.Init(callback, 1)
        self._subscribers.append(subscriber)

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
