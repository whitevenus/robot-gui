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

#: SDK 槽位 → 产品 schema 关节名。适配器发布的是槽位，关节名在这里还原。
_JOINT_NAME_BY_SLOT = {joint.sdk_slot: joint.name for joint in G1_29DOF_JOINTS}


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

    def latest_joint_angles(self) -> dict[str, float] | None:
        """最近一次 LowState 的关节角，按产品 schema 的关节名索引。

        给控制回路用：重力前馈每个控制周期都要读一次，不能走完整的 snapshot 构造。
        注意适配器发布的是 sdk_slot，关节名要在这里按产品 schema 映射出来。
        """
        with self._lock:
            sample = self._samples.get("low_state")
            if sample is None or sample.error or not sample.data:
                return None
            joints = sample.data.get("joints")
        if not isinstance(joints, list):
            return None
        angles: dict[str, float] = {}
        for item in joints:
            if not isinstance(item, dict):
                continue
            slot = item.get("sdk_slot")
            value = item.get("q_rad")
            if not isinstance(slot, int) or not isinstance(value, (int, float)):
                continue
            name = _JOINT_NAME_BY_SLOT.get(slot)
            if name is not None:
                angles[name] = float(value)
        return angles or None

    def latest_joint_modes(self) -> dict[str, int] | None:
        """最近一次 LowState 的电机 mode 回读，按产品 schema 的关节名索引。

        执行记录要留着腰 12/13/14 的 mode 回读：它是"腰到底有没有被接管"的
        现场证据（per-motor mode 写 0 或 1 对腰的行为没有区别，见 control.py）。
        """
        with self._lock:
            sample = self._samples.get("low_state")
            if sample is None or sample.error or not sample.data:
                return None
            joints = sample.data.get("joints")
        if not isinstance(joints, list):
            return None
        modes: dict[str, int] = {}
        for item in joints:
            if not isinstance(item, dict):
                continue
            slot = item.get("sdk_slot")
            value = item.get("mode")
            if not isinstance(slot, int) or not isinstance(value, int):
                continue
            name = _JOINT_NAME_BY_SLOT.get(slot)
            if name is not None:
                modes[name] = value
        return modes or None

    def latest_imu_rpy(self) -> tuple[list[float] | None, list[float] | None]:
        """最近的髋部 / 躯干 IMU 姿态角（弧度），给执行记录的时间序列用。

        顺序固定为 (髋部, 躯干)，与遥测快照里的字段一致。
        """
        with self._lock:
            samples = {
                name: self._samples.get(name) for name in ("hip_imu", "torso_imu")
            }
        result: list[list[float] | None] = []
        for sample in samples.values():
            data = None if sample is None or sample.error else sample.data
            rpy = data.get("rpy") if isinstance(data, dict) else None
            result.append(rpy if isinstance(rpy, list) else None)
        return result[0], result[1]

    def snapshot(
        self,
        mode: str,
        interface: str | None,
        adapter_error: str | None,
        controls_available: bool = False,
        controls_reason: str = "controls_disabled",
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
                        # 官方只用数字 ID 表示模式（见《高层运动服务接口》模式 ID 表），
                        # 页面按那张表翻译成中文标签。
                        "id": fsm_data.get("id"),
                        "mode": fsm_data.get("mode"),
                    }
                ),
            },
            "capabilities": {
                "controls": {
                    "available": controls_available,
                    "reason": controls_reason,
                }
            },
        }
