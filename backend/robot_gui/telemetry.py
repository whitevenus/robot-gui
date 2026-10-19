from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .profile import G1_29DOF_JOINTS

SOURCE_THRESHOLDS_MS = {
    "low_state": (300, 1500),
    "bms": (3000, 10000),
    "hip_imu": (300, 1500),
    "torso_imu": (1000, 3000),
    "fsm": (1000, 3000),
}


@dataclass(frozen=True)
class SourceSample:
    data: dict[str, Any] | None
    received_monotonic: float
    received_at: str
    error: str | None = None


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


class TelemetryHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, SourceSample] = {}

    def ingest(self, source: str, data: dict[str, Any]) -> None:
        if source not in SOURCE_THRESHOLDS_MS:
            raise ValueError(f"Unknown telemetry source: {source}")
        received_monotonic = time.monotonic()
        received_at = datetime.now(UTC).isoformat()
        if _contains_non_finite(data):
            sample = SourceSample(
                None,
                received_monotonic,
                received_at,
                "telemetry_contains_non_finite_value",
            )
        else:
            sample = SourceSample(data, received_monotonic, received_at)
        with self._lock:
            self._samples[source] = sample

    def mark_error(self, source: str, error: str) -> None:
        if source not in SOURCE_THRESHOLDS_MS:
            raise ValueError(f"Unknown telemetry source: {source}")
        sample = SourceSample(
            None, time.monotonic(), datetime.now(UTC).isoformat(), error
        )
        with self._lock:
            self._samples[source] = sample

    def snapshot(
        self,
        mode: str,
        interface: str | None,
        adapter_error: str | None,
    ) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        now_utc = datetime.now(UTC).isoformat()
        with self._lock:
            samples = dict(self._samples)

        sources: dict[str, dict[str, Any]] = {}
        for source, (fresh_ms, offline_ms) in SOURCE_THRESHOLDS_MS.items():
            sample = samples.get(source)
            age_ms = (
                None
                if sample is None
                else max(0, round((now_monotonic - sample.received_monotonic) * 1000))
            )
            if sample is None:
                state = "unknown"
            elif sample.error and age_ms is not None and age_ms <= offline_ms:
                state = "invalid"
            elif age_ms is not None and age_ms <= fresh_ms:
                state = "fresh"
            elif age_ms is not None and age_ms <= offline_ms:
                state = "delayed"
            else:
                state = "offline"
            sources[source] = {
                "state": state,
                "age_ms": age_ms,
                "fresh_for_ms": fresh_ms,
                "offline_after_ms": offline_ms,
                "sample_time": None if sample is None else sample.received_at,
                "timestamp_source": "server_receive_time",
                "error": None if sample is None else sample.error,
            }

        low_state = samples.get("low_state")
        low_data = (
            low_state.data
            if low_state is not None and low_state.error is None
            else None
        )
        joints_by_slot = (
            {}
            if low_data is None
            else {
                item["sdk_slot"]: item
                for item in low_data.get("joints", [])
                if isinstance(item, dict) and isinstance(item.get("sdk_slot"), int)
            }
        )
        joints = []
        for descriptor in G1_29DOF_JOINTS:
            motor = joints_by_slot.get(descriptor.sdk_slot, {})
            joints.append(
                {
                    "name": descriptor.name,
                    "urdf_joint_name": descriptor.urdf_joint_name,
                    "sdk_slot": descriptor.sdk_slot,
                    "group": descriptor.group,
                    "q_rad": motor.get("q_rad"),
                    "dq_rad_s": motor.get("dq_rad_s"),
                    "ddq_rad_s2": motor.get("ddq_rad_s2"),
                    "tau_est_nm": motor.get("tau_est_nm"),
                    "temperature_raw": motor.get("temperature_raw"),
                    "voltage_v": motor.get("voltage_v"),
                    "mode": motor.get("mode"),
                    "status_code": motor.get("status_code"),
                }
            )

        def source_data(name: str) -> dict[str, Any] | None:
            sample = samples.get(name)
            return None if sample is None or sample.error else sample.data

        low_state_source = sources["low_state"]["state"]
        if adapter_error:
            connection_state = "error"
            connection_message = adapter_error
        elif low_state_source == "fresh":
            connection_state = "connected"
            connection_message = "已收到 G1 LowState 遥测。"
        elif low_state_source in {"delayed", "invalid"}:
            connection_state = "delayed"
            connection_message = "LowState 遥测延迟或无效。"
        else:
            connection_state = "disconnected"
            connection_message = "尚未收到有效 G1 LowState 遥测。"

        fsm_data = source_data("fsm")
        return {
            "schema_version": "1.0",
            "snapshot_id": time.monotonic_ns(),
            "server_time": now_utc,
            "mode": mode,
            "connection": {
                "state": connection_state,
                "interface": interface,
                "last_data_at": sources["low_state"]["sample_time"],
                "error": adapter_error,
                "message": connection_message,
            },
            "sources": sources,
            "robot": {
                "product_id": "g1",
                "variant": "29dof",
                "joint_count": 29,
                "joints": joints,
                "mode_machine": (
                    None if low_data is None else low_data.get("mode_machine")
                ),
                "bms": source_data("bms"),
                "hip_imu": source_data("hip_imu"),
                "torso_imu": source_data("torso_imu"),
                "fsm": (
                    None
                    if fsm_data is None
                    else {
                        "id": fsm_data.get("id"),
                        "mode": fsm_data.get("mode"),
                    }
                ),
            },
            "capabilities": {
                "controls": {
                    "available": False,
                    "reason": "telemetry_release_is_read_only",
                }
            },
        }
