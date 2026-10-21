"""FSM 运动模式：读取、切换门控与接口行为。

覆盖的关键点：
- 允许列表按**官方数字 ID** 解析，非数字与不在目录里的 ID 被忽略；
- 切换能力默认关闭，关闭时给出原因；
- 切换需要显式确认；
- 真机控制没开时拒绝切换；
- 不在允许列表里的模式拒绝切换；
- 控制会话进行中拒绝切换；
- 机器人拒绝（错误码非 0）时如实报错，不假装成功。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from robot_gui.fsm import FsmError, FsmService, FsmSettings
from robot_gui.main import RuntimeSettings, create_app
from robot_gui.profile import G1_29DOF_JOINTS
from robot_gui.telemetry import TelemetryHub


class FakeAdapter:
    """带 FSM 切换能力的假适配器（接口与 UnitreeSdkTelemetryAdapter 一致）。"""

    def __init__(self, switch_code: int = 0, fsm_id: int = 501) -> None:
        self.switch_code = switch_code
        self.fsm_id = fsm_id
        self.switched: list[int] = []

    def start(self, publish, publish_error) -> None:
        publish(
            "low_state",
            {
                "joints": [
                    {
                        "sdk_slot": joint.sdk_slot,
                        "q_rad": 0.0,
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
                "mode_machine": 5,
            },
        )
        publish("fsm", {"id": self.fsm_id, "mode": None})

    def stop(self) -> None:
        return

    def switch_mode(self, fsm_id: int) -> tuple[int, str]:
        self.switched.append(fsm_id)
        if self.switch_code == 0:
            self.fsm_id = fsm_id
        return self.switch_code, "" if self.switch_code == 0 else "机器人拒绝"

    def refresh_mode(self) -> dict[str, Any]:
        return {"id": self.fsm_id}


class FakeControl:
    def __init__(self, phase: str = "idle") -> None:
        self.phase = phase

    def state(self) -> dict[str, Any]:
        return {"phase": self.phase}


def build_service(
    *,
    modes: str = "501,4",
    controls_enabled: bool = True,
    adapter: FakeAdapter | None = None,
    control: FakeControl | None = None,
) -> tuple[FsmService, FakeAdapter]:
    fake_adapter = adapter or FakeAdapter()
    hub = TelemetryHub()
    fake_adapter.start(hub.ingest, hub.mark_error)
    service = FsmService(
        settings=FsmSettings.from_env(modes),
        adapter=fake_adapter,
        telemetry=hub,
        controls_enabled=controls_enabled,
        control=control,
        fsm_whitelist=(501,),
    )
    return service, fake_adapter


# ---- 配置解析 ----


def test_settings_parse_mode_ids() -> None:
    settings = FsmSettings.from_env("501, 4")

    assert settings.enabled is True
    assert settings.permits(501) is True
    assert settings.permits(4) is True
    assert settings.permits(0) is False


def test_settings_ignore_unknown_and_non_numeric_ids() -> None:
    settings = FsmSettings.from_env("501, teleport, zero")

    assert settings.permits(501) is True
    assert settings.permits(0) is False


def test_settings_without_modes_allow_every_catalogued_mode() -> None:
    """未配置 ROBOT_GUI_FSM_MODES = 目录里的模式全部允许。

    切换本身有操作员二次确认（前端 window.confirm），门槛交给人和遥控器，
    不靠配置项。需要收窄时才设 ROBOT_GUI_FSM_MODES。
    """
    for settings in (FsmSettings.from_env(""), FsmSettings.from_env(None)):
        assert settings.enabled is True
        assert all(settings.permits(fsm_id) for fsm_id in (0, 1, 4, 501))


# ---- 服务门控 ----


def test_state_allows_switching_when_no_allowlist_is_configured() -> None:
    """没配 ROBOT_GUI_FSM_MODES 时，目录里的模式全部可点。"""
    service, _ = build_service(modes="")

    state = service.state()

    assert state["available"] is True
    assert state["reason"] == ""
    assert state["whitelist"] == [501]
    assert {mode["fsm_id"] for mode in state["modes"]} == {0, 1, 4, 501}
    assert all(mode["allowed"] is True for mode in state["modes"])


def test_state_reports_reason_when_controls_are_off() -> None:
    service, _ = build_service(controls_enabled=False)

    state = service.state()

    assert state["available"] is False
    assert "ROBOT_GUI_CONTROLS_ENABLED" in state["reason"]


def test_state_marks_the_dangerous_modes() -> None:
    service, _ = build_service(modes="0,1,4,501")

    modes = {mode["fsm_id"]: mode for mode in service.state()["modes"]}

    assert modes[0]["label"] == "零力矩"
    assert modes[0]["danger"] is True
    assert modes[1]["label"] == "阻尼"
    assert modes[1]["danger"] is True
    assert modes[4]["label"] == "锁定站立"
    assert modes[4]["danger"] is False
    assert modes[501]["label"] == "常规运控"
    assert modes[501]["danger"] is False


def test_request_requires_explicit_confirmation() -> None:
    service, adapter = build_service()

    with pytest.raises(FsmError) as error:
        service.request_mode(501, confirmed=False)

    assert error.value.code == "confirmation_required"
    assert adapter.switched == []


def test_request_refuses_when_controls_are_disabled() -> None:
    service, adapter = build_service(controls_enabled=False)

    with pytest.raises(FsmError) as error:
        service.request_mode(501, confirmed=True)

    assert error.value.code == "capability_disabled"
    assert adapter.switched == []


def test_request_refuses_modes_outside_the_allowlist() -> None:
    service, adapter = build_service()

    with pytest.raises(FsmError) as error:
        service.request_mode(0, confirmed=True)

    assert error.value.code == "fsm_mode_not_allowed"
    assert adapter.switched == []


def test_request_refuses_unknown_mode() -> None:
    service, adapter = build_service()

    with pytest.raises(FsmError) as error:
        service.request_mode(999, confirmed=True)

    assert error.value.code == "fsm_mode_unknown"
    assert adapter.switched == []


def test_request_refuses_while_a_control_session_runs() -> None:
    service, adapter = build_service(control=FakeControl(phase="playing"))

    with pytest.raises(FsmError) as error:
        service.request_mode(501, confirmed=True)

    assert error.value.code == "control_busy"
    assert adapter.switched == []


def test_request_switches_to_the_requested_mode_id() -> None:
    service, adapter = build_service()

    state = service.request_mode(4, confirmed=True)

    assert adapter.switched == [4]
    assert state["current"]["id"] == 4


def test_request_reports_the_robot_rejecting_the_switch() -> None:
    service, adapter = build_service(adapter=FakeAdapter(switch_code=7))

    with pytest.raises(FsmError) as error:
        service.request_mode(501, confirmed=True)

    assert error.value.code == "fsm_switch_failed"
    assert "7" in str(error.value)


# ---- HTTP 接口 ----


def test_http_fsm_state_and_mode_endpoints(tmp_path) -> None:
    adapter = FakeAdapter()
    app = create_app(
        RuntimeSettings(
            mode="real",
            dds_interface="test0",
            data_dir=tmp_path,
            controls_enabled=True,
            fsm_modes="501",
        ),
        telemetry_adapter=adapter,
    )

    with TestClient(app) as client:
        state = client.get("/api/v1/fsm/state").json()
        assert state["available"] is True
        assert state["whitelist"] == [501]

        rejected = client.post("/api/v1/fsm/mode", json={"fsm_id": 501})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "confirmation_required"

        missing = client.post("/api/v1/fsm/mode", json={})
        assert missing.status_code == 400
        assert missing.json()["detail"]["code"] == "fsm_mode_required"

        accepted = client.post(
            "/api/v1/fsm/mode", json={"fsm_id": 501, "confirmed": True}
        )
        assert accepted.status_code == 200

    assert adapter.switched == [501]


def test_http_fsm_switching_is_open_without_configured_modes(tmp_path) -> None:
    """没配 ROBOT_GUI_FSM_MODES 时，HTTP 层的模式切换直接可用。"""
    adapter = FakeAdapter()
    app = create_app(
        RuntimeSettings(
            mode="real",
            dds_interface="test0",
            data_dir=tmp_path,
            controls_enabled=True,
        ),
        telemetry_adapter=adapter,
    )

    with TestClient(app) as client:
        state = client.get("/api/v1/fsm/state").json()
        accepted = client.post(
            "/api/v1/fsm/mode", json={"fsm_id": 501, "confirmed": True}
        )

    assert state["available"] is True
    assert state["reason"] == ""
    assert all(mode["allowed"] is True for mode in state["modes"])
    assert accepted.status_code == 200
    assert adapter.switched == [501]


def test_snapshot_publishes_the_fsm_id(tmp_path) -> None:
    app = create_app(
        RuntimeSettings(mode="real", dds_interface="test0", data_dir=tmp_path),
        telemetry_adapter=FakeAdapter(fsm_id=0),
    )

    with TestClient(app) as client:
        snapshot = client.get("/api/v1/status").json()

    assert snapshot["robot"]["fsm"]["id"] == 0
