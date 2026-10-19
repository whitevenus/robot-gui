from fastapi.testclient import TestClient

from robot_gui.main import RuntimeSettings, create_app


class UnexpectedSdkFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        raise AssertionError("Mock mode must not construct the real SDK adapter.")


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
    assert snapshot["sources"]["low_state"]["state"] == "mock"
    assert snapshot["capabilities"]["controls"]["available"] is False
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
    app = create_app(RuntimeSettings(websocket_interval_seconds=0.1))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as websocket:
            snapshot = websocket.receive_json()

    assert snapshot["schema_version"] == "1.0"
    assert snapshot["mode"] == "mock"
    assert snapshot["connection"]["state"] == "mock"


def test_real_mode_and_enabled_mock_controls_fail_fast() -> None:
    try:
        create_app(RuntimeSettings(mode="real"))
    except ValueError as error:
        assert "real SDK mode is not implemented" in str(error)
    else:
        raise AssertionError("Real mode must not silently start as mock.")

    try:
        create_app(RuntimeSettings(controls_enabled=True))
    except ValueError as error:
        assert "Controls cannot be enabled" in str(error)
    else:
        raise AssertionError("Mock mode must reject enabled controls.")
