import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from robot_gui.adapters import UnitreeSdkTelemetryAdapter
from robot_gui.main import RuntimeSettings, create_app
from robot_gui.profile import G1_29DOF_JOINTS
from robot_gui.telemetry import TelemetryHub


class UnexpectedSdkFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        raise AssertionError("Mock mode must not construct the real SDK adapter.")


class FailingTelemetryAdapter:
    def start(self, publish: object, publish_error: object) -> None:
        raise RuntimeError("test adapter startup failure")

    def stop(self) -> None:
        pass


def test_status_explicitly_reports_mock_and_control_disabled() -> None:
    sdk_factory = UnexpectedSdkFactory()
    app = create_app(sdk_factory=sdk_factory)

    with TestClient(app) as client:
        response = client.get("/api/v1/status")

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["schema_version"] == "1.0"
    assert snapshot["mode"] == "mock"
    assert snapshot["connection"]["state"] == "mock"
    assert "未连接真实机器人" in snapshot["connection"]["message"]
    assert snapshot["sources"]["low_state"]["state"] == "fresh"
    assert snapshot["sources"]["low_state"]["fresh_for_ms"] == 300
    assert snapshot["sources"]["low_state"]["offline_after_ms"] == 1500
    assert snapshot["capabilities"]["controls"]["available"] is False
    assert len(snapshot["robot"]["joints"]) == 29
    assert app.state.sdk_adapter is None
    assert sdk_factory.calls == 0


def test_mock_mode_rejects_control_session_without_sdk_call() -> None:
    sdk_factory = UnexpectedSdkFactory()
    app = create_app(sdk_factory=sdk_factory)

    with TestClient(app) as client:
        response = client.post("/api/v1/control/sessions", json={"confirmed": True})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "capability_disabled"
    assert sdk_factory.calls == 0
    assert app.state.sdk_adapter is None


def test_telemetry_websocket_sends_versioned_mock_snapshot() -> None:
    app = create_app(RuntimeSettings(telemetry_push_hz=10))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as websocket:
            snapshot = websocket.receive_json()

    assert snapshot["schema_version"] == "1.0"
    assert snapshot["mode"] == "mock"
    assert snapshot["connection"]["state"] == "mock"
    assert len(snapshot["robot"]["joints"]) == 29
    assert snapshot["sources"]["fsm"]["state"] == "fresh"
    assert snapshot["robot"]["fsm"]["id"] == 500
    assert snapshot["capabilities"]["controls"]["available"] is False


def test_mock_snapshot_has_all_g1_joint_fields_and_auxiliary_sources() -> None:
    with TestClient(create_app()) as client:
        snapshot = client.get("/api/v1/status").json()

    joints = snapshot["robot"]["joints"]
    assert [joint["name"] for joint in joints] == [
        joint.name for joint in G1_29DOF_JOINTS
    ]
    assert [joint["sdk_slot"] for joint in joints] == list(range(29))
    for joint in joints:
        assert joint["q_rad"] is not None
        assert joint["dq_rad_s"] is not None
        assert joint["ddq_rad_s2"] is not None
        assert joint["tau_est_nm"] is not None
        assert joint["temperature_raw"] is not None
        assert joint["voltage_v"] is not None
        assert joint["mode"] is not None
        assert joint["status_code"] is not None

    assert snapshot["robot"]["bms"]["soc_pct"] == 92
    assert snapshot["robot"]["hip_imu"]["rpy"]
    assert snapshot["robot"]["torso_imu"]["rpy"]
    assert set(snapshot["sources"]) == {
        "low_state",
        "bms",
        "hip_imu",
        "torso_imu",
        "fsm",
    }
    assert all(source["age_ms"] is not None for source in snapshot["sources"].values())


def test_fake_low_state_pause_reports_delayed_then_offline() -> None:
    app = create_app()
    adapter = app.state.telemetry_adapter

    with TestClient(app) as client:
        adapter.pause_source("low_state")
        time.sleep(0.4)
        delayed = client.get("/api/v1/status").json()
        time.sleep(1.2)
        offline = client.get("/api/v1/status").json()

    assert delayed["sources"]["low_state"]["state"] == "delayed"
    assert offline["sources"]["low_state"]["state"] == "offline"
    assert offline["connection"]["state"] == "mock"
    assert offline["capabilities"]["controls"]["available"] is False


def test_non_finite_source_is_invalid_and_not_presented_as_valid_joint_data() -> None:
    hub = TelemetryHub()
    hub.ingest("low_state", {"joints": [{"sdk_slot": 0, "q_rad": float("nan")}]})

    snapshot = hub.snapshot("real", "eth0", None)

    assert snapshot["sources"]["low_state"]["state"] == "invalid"
    assert snapshot["robot"]["joints"][0]["q_rad"] is None
    assert snapshot["robot"]["joints"][1]["q_rad"] is None
    assert snapshot["sources"]["low_state"]["error"] == (
        "telemetry_contains_non_finite_value"
    )


def test_unitree_low_state_adapter_uses_profile_slots_without_sdk_install() -> None:
    adapter = UnitreeSdkTelemetryAdapter(
        interface="eth0",
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


def test_real_adapter_startup_failure_is_degraded_without_mock_fallback() -> None:
    app = create_app(
        RuntimeSettings(mode="real", dds_interface="eth0"),
        telemetry_adapter=FailingTelemetryAdapter(),
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health").json()
        snapshot = client.get("/api/v1/status").json()

    assert health == {"status": "degraded", "mode": "real"}
    assert snapshot["mode"] == "real"
    assert snapshot["connection"]["state"] == "error"
    assert snapshot["connection"]["error"] == "test adapter startup failure"
    assert snapshot["sources"]["low_state"]["state"] == "unknown"
    assert snapshot["capabilities"]["controls"]["available"] is False


def test_runtime_config_requires_interface_for_real_and_caps_push_rate() -> None:
    try:
        create_app(RuntimeSettings(mode="real"))
    except ValueError as error:
        assert "ROBOT_GUI_DDS_INTERFACE is required" in str(error)
    else:
        raise AssertionError("Real mode must require a configured DDS interface.")

    try:
        create_app(RuntimeSettings(controls_enabled=True))
    except ValueError as error:
        assert "telemetry-only" in str(error)
    else:
        raise AssertionError("Telemetry-only release must keep controls disabled.")

    try:
        create_app(RuntimeSettings(telemetry_push_hz=20.1))
    except ValueError as error:
        assert "between 1 and 20" in str(error)
    else:
        raise AssertionError("WebSocket push rate must have a maximum.")
