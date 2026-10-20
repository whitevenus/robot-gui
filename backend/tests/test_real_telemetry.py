import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from robot_gui.adapters import (
    TelemetryAdapter,
    TelemetryCallback,
    TelemetryErrorCallback,
    UnitreeSdkTelemetryAdapter,
)
from robot_gui.main import RuntimeSettings, create_app
from robot_gui.profile import G1_29DOF_JOINTS
from robot_gui.telemetry import TelemetryHub


class TelemetryTestAdapter:
    def __init__(self, samples: list[tuple[str, dict[str, Any]]] | None = None):
        self.samples = samples if samples is not None else telemetry_samples()
        self.start_calls = 0

    def start(
        self,
        publish: TelemetryCallback,
        publish_error: TelemetryErrorCallback,
    ) -> None:
        self.start_calls += 1
        for source, data in self.samples:
            publish(source, data)

    def stop(self) -> None:
        return


class FailingTelemetryTestAdapter:
    def start(
        self,
        publish: TelemetryCallback,
        publish_error: TelemetryErrorCallback,
    ) -> None:
        raise RuntimeError("test SDK2/DDS startup failure")

    def stop(self) -> None:
        return


def telemetry_samples() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "low_state",
            {
                "joints": [
                    {
                        "sdk_slot": joint.sdk_slot,
                        "q_rad": joint.sdk_slot / 100,
                        "dq_rad_s": 0.1,
                        "ddq_rad_s2": 0.2,
                        "tau_est_nm": 0.3,
                        "temperature_raw": [32, 33],
                        "voltage_v": 48.0,
                        "mode": 1,
                        "status_code": 0,
                    }
                    for joint in G1_29DOF_JOINTS
                ],
                "mode_machine": 2,
            },
        ),
        ("hip_imu", {"rpy": [0.1, 0.2, 0.3]}),
        ("torso_imu", {"rpy": [0.0, 0.0, 0.0]}),
        (
            "bms",
            {
                "soc_pct": 92,
                "soh_pct": 100,
                "current_raw": 0,
                "voltage_raw": [50000, 50000, 50000],
                "temperature_raw": [25] * 12,
                "cycle_count": 12,
            },
        ),
        ("fsm", {"id": 500, "mode": None}),
    ]


def real_test_app(adapter: TelemetryAdapter | None = None) -> FastAPI:
    return create_app(
        RuntimeSettings(mode="real", dds_interface="test0"),
        telemetry_adapter=adapter or TelemetryTestAdapter(),
    )


def test_real_status_has_29dof_telemetry_and_controls_disabled() -> None:
    app = real_test_app()

    with TestClient(app) as client:
        health = client.get("/api/v1/health").json()
        snapshot = client.get("/api/v1/status").json()

    assert health == {"status": "ok", "mode": "real"}
    assert snapshot["schema_version"] == "1.0"
    assert snapshot["mode"] == "real"
    assert snapshot["connection"]["state"] == "connected"
    assert snapshot["connection"]["interface"] == "test0"
    assert snapshot["sources"]["low_state"]["state"] == "fresh"
    assert snapshot["sources"]["low_state"]["fresh_for_ms"] == 300
    assert snapshot["sources"]["low_state"]["offline_after_ms"] == 1500
    assert len(snapshot["robot"]["joints"]) == 29
    assert snapshot["capabilities"]["controls"]["available"] is False


def test_telemetry_test_adapter_is_injected_only_and_control_stays_closed(tmp_path) -> None:
    adapter = TelemetryTestAdapter()
    app = create_app(
        RuntimeSettings(mode="real", dds_interface="test0", data_dir=tmp_path),
        telemetry_adapter=adapter,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/control/replay", json={"action": "demo", "confirmed": True}
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "capability_disabled"
    assert adapter.start_calls == 1


def test_real_websocket_sends_versioned_joint_and_auxiliary_snapshot() -> None:
    with TestClient(real_test_app()) as client:
        with client.websocket_connect("/ws/telemetry") as websocket:
            snapshot = websocket.receive_json()

    assert snapshot["schema_version"] == "1.0"
    assert snapshot["mode"] == "real"
    assert len(snapshot["robot"]["joints"]) == 29
    assert snapshot["sources"]["fsm"]["state"] == "fresh"
    assert snapshot["robot"]["fsm"]["id"] == 500
    assert snapshot["robot"]["bms"]["soc_pct"] == 92
    assert snapshot["robot"]["hip_imu"]["rpy"] == [0.1, 0.2, 0.3]
    assert snapshot["capabilities"]["controls"]["available"] is False


def test_snapshot_uses_profile_mapping_and_exposes_every_joint_field() -> None:
    with TestClient(real_test_app()) as client:
        snapshot = client.get("/api/v1/status").json()

    joints = snapshot["robot"]["joints"]
    assert [joint["name"] for joint in joints] == [
        joint.name for joint in G1_29DOF_JOINTS
    ]
    assert [joint["sdk_slot"] for joint in joints] == list(range(29))
    assert all(joint["q_rad"] is not None for joint in joints)
    assert all(joint["dq_rad_s"] is not None for joint in joints)
    assert all(joint["ddq_rad_s2"] is not None for joint in joints)
    assert all(joint["tau_est_nm"] is not None for joint in joints)
    assert all(joint["temperature_raw"] is not None for joint in joints)
    assert all(joint["voltage_v"] is not None for joint in joints)
    assert all(joint["mode"] is not None for joint in joints)
    assert all(joint["status_code"] is not None for joint in joints)
    assert all(source["age_ms"] is not None for source in snapshot["sources"].values())


def test_source_age_transitions_to_delayed_and_offline() -> None:
    hub = TelemetryHub()
    hub.ingest("low_state", {"joints": []})

    time.sleep(0.35)
    delayed = hub.snapshot("real", "test0", None)
    time.sleep(1.2)
    offline = hub.snapshot("real", "test0", None)

    assert delayed["sources"]["low_state"]["state"] == "delayed"
    assert offline["sources"]["low_state"]["state"] == "offline"
    assert offline["connection"]["state"] == "disconnected"


def test_non_finite_source_is_invalid_and_not_presented_as_valid_joint_data() -> None:
    hub = TelemetryHub()
    hub.ingest("low_state", {"joints": [{"sdk_slot": 0, "q_rad": float("nan")}]})

    snapshot = hub.snapshot("real", "test0", None)

    assert snapshot["sources"]["low_state"]["state"] == "invalid"
    assert snapshot["robot"]["joints"][0]["q_rad"] is None
    assert snapshot["robot"]["joints"][1]["q_rad"] is None
    assert snapshot["sources"]["low_state"]["error"] == (
        "telemetry_contains_non_finite_value"
    )


def test_unitree_adapter_maps_low_state_slots_without_sdk_install() -> None:
    adapter = UnitreeSdkTelemetryAdapter(
        interface="test0",
        low_state_topic="rt/lowstate",
        bms_topic="rt/lf/bmsstate",
        secondary_imu_topic="rt/lf/secondary_imu",
    )
    received: dict[str, dict[str, object]] = {}
    adapter._publish = lambda source, data: received.__setitem__(source, data)
    imu = SimpleNamespace(
        quaternion=[1.0, 0.0, 0.0, 0.0],
        gyroscope=[0.0, 0.0, 0.0],
        accelerometer=[0.0, 0.0, 9.81],
        rpy=[0.1, 0.2, 0.3],
        temperature=30,
    )
    motors = [
        SimpleNamespace(
            q=float(slot),
            dq=1.0,
            ddq=2.0,
            tau_est=3.0,
            temperature=[32, 33],
            vol=48.0,
            mode=1,
            motorstate=0,
        )
        for slot in range(35)
    ]
    adapter._on_low_state(
        SimpleNamespace(motor_state=motors, mode_machine=2, imu_state=imu)
    )

    low_state = received["low_state"]
    joints = low_state["joints"]
    assert len(joints) == 29
    assert [joint["sdk_slot"] for joint in joints] == list(range(29))
    assert joints[0]["q_rad"] == 0.0
    assert joints[28]["q_rad"] == 28.0
    assert low_state["mode_machine"] == 2
    assert received["hip_imu"]["rpy"] == [0.1, 0.2, 0.3]


def test_adapter_closes_subscriber_when_initialization_fails() -> None:
    class PartiallyInitializedSubscriber:
        close_calls = 0

        def Init(self, _callback: Any, _queue_size: int) -> None:
            raise RuntimeError("test subscriber initialization failure")

        def Close(self) -> None:
            self.close_calls += 1

    adapter = UnitreeSdkTelemetryAdapter(
        interface="test0",
        low_state_topic="rt/lowstate",
        bms_topic="rt/lf/bmsstate",
        secondary_imu_topic="rt/lf/secondary_imu",
    )
    subscriber = PartiallyInitializedSubscriber()

    with pytest.raises(RuntimeError, match="subscriber initialization failure"):
        adapter._subscribe(subscriber, lambda _message: None)

    adapter.stop()

    assert subscriber.close_calls == 1


def test_missing_sdk_or_dds_fails_startup_without_mock_fallback() -> None:
    app = create_app(
        RuntimeSettings(mode="real", dds_interface="test0"),
        telemetry_adapter=FailingTelemetryTestAdapter(),
    )

    with pytest.raises(RuntimeError, match="test SDK2/DDS startup failure"):
        with TestClient(app):
            pass


def test_runtime_rejects_mock_mode_missing_interface_and_bad_ranges(tmp_path) -> None:
    invalid_settings = (
        RuntimeSettings(mode="mock", dds_interface="test0"),
        RuntimeSettings(mode="real"),
        RuntimeSettings(mode="real", dds_interface="test0", telemetry_push_hz=20.1),
        RuntimeSettings(
            mode="real",
            dds_interface="test0",
            controls_enabled=True,
            fsm_whitelist=(),
        ),
    )
    expected_messages = (
        "requires ROBOT_GUI_MODE=real",
        "ROBOT_GUI_DDS_INTERFACE is required",
        "between 1 and 20",
        "ROBOT_GUI_FSM_WHITELIST must list at least one FSM id",
    )

    for settings, expected_message in zip(invalid_settings, expected_messages):
        with pytest.raises(ValueError, match=expected_message):
            with TestClient(
                create_app(settings, telemetry_adapter=TelemetryTestAdapter())
            ):
                pass
