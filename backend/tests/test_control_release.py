"""动作库、真机控制前置检查与音频端口的测试。

真机路径不接硬件：所有拒绝分支都在下发任何控制命令之前返回，
因此可以完整覆盖。成功路径用替身替换 RobotSession 与 AudioClient。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from robot_gui.actions import ActionStore, ActionStoreError
from robot_gui.adapters import TelemetryCallback, TelemetryErrorCallback
from robot_gui.audio import CHUNK_BYTES, G1AudioPort
from robot_gui.control import ControlError, ControlService, ControlSettings
from robot_gui.main import RuntimeSettings, create_app

from test_real_telemetry import TelemetryTestAdapter


def sample_action(frames: int = 4) -> dict[str, Any]:
    return {
        "group": "arms_waist",
        "frames": [
            {"t": round(index * 0.04, 4), "q": [0.0] * 17}
            for index in range(frames)
        ],
    }


def sample_pcm(milliseconds: int = 200) -> bytes:
    return b"\x00\x00" * int(16000 * milliseconds / 1000)


class FakeTelemetry:
    def __init__(self, *, fresh: bool = True, fsm_id: int | None = 501, mode_machine: int = 5):
        self._fresh = fresh
        self._fsm_id = fsm_id
        self._mode_machine = mode_machine

    def snapshot(self, mode: str, interface: str | None, adapter_error: str | None):
        state = "fresh" if self._fresh else "offline"
        return {
            "sources": {"low_state": {"state": state}},
            "robot": {
                "mode_machine": self._mode_machine,
                "fsm": None if self._fsm_id is None else {"id": self._fsm_id, "mode": None},
            },
        }


# ---------------------------------------------------------------- 动作库


def test_action_store_import_list_export_delete(tmp_path: Path) -> None:
    store = ActionStore(tmp_path)
    body = json.dumps(sample_action()).encode("utf-8")

    summary = store.import_action("demo", body)
    assert summary.group == "arms_waist"
    assert summary.frames == 4
    assert summary.duration_s == pytest.approx(0.12)

    assert [item.name for item in store.list()] == ["demo"]
    assert store.export_action("demo") == body
    assert store.load("demo").group == "arms_waist"

    store.delete("demo")
    assert store.list() == []


def test_action_store_rejects_duplicate_and_invalid_payload(tmp_path: Path) -> None:
    store = ActionStore(tmp_path)
    body = json.dumps(sample_action()).encode("utf-8")
    store.import_action("demo", body)

    with pytest.raises(ActionStoreError) as duplicate:
        store.import_action("demo", body)
    assert duplicate.value.code == "action_exists"

    with pytest.raises(ActionStoreError) as invalid:
        store.import_action("bad", json.dumps({"group": "arms_waist", "frames": []}).encode())
    assert invalid.value.code == "action_invalid"

    with pytest.raises(ActionStoreError) as unsafe:
        store.import_action("../escape", body)
    assert unsafe.value.code == "action_name_invalid"


def test_action_store_without_data_dir_fails_explicitly(tmp_path: Path) -> None:
    store = ActionStore(None)
    with pytest.raises(ActionStoreError) as error:
        store.list()
    assert error.value.code == "action_store_not_configured"


# ---------------------------------------------------------------- 音频端口


class RecordingAudioClient:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.stopped = 0

    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes):
        self.chunks.append(pcm_data)
        return 0

    def PlayStop(self, app_name: str):
        self.stopped += 1
        return 0


def test_audio_port_streams_5120_byte_chunks_and_stops() -> None:
    client = RecordingAudioClient()
    port = G1AudioPort(client, "robot-gui")
    pcm = sample_pcm(400)

    assert port.play(pcm, threading.Event()) == ""
    assert len(client.chunks) == 3
    assert all(len(chunk) <= CHUNK_BYTES for chunk in client.chunks)
    assert b"".join(client.chunks) == pcm
    assert client.stopped == 1
    assert port.playing is False


def test_audio_port_rejects_odd_length_and_double_play() -> None:
    client = RecordingAudioClient()
    port = G1AudioPort(client, "robot-gui")

    assert port.play(b"\x00", threading.Event()) == "audio.invalid_pcm_length"

    blocking = threading.Event()
    blocking.set()
    assert port.play(sample_pcm(200), blocking) == "audio.cancelled"
    # 取消后 SDK 不校验返回码，闩锁必须记住「停止未确认」。
    assert port.recovery_unconfirmed is True
    assert port.play(sample_pcm(200), threading.Event()) == "audio.stop_unconfirmed"


# ---------------------------------------------------------------- 控制前置检查


def build_control(
    tmp_path: Path,
    *,
    enabled: bool = True,
    telemetry: FakeTelemetry | None = None,
    action: str = "demo",
) -> tuple[ControlService, ActionStore]:
    store = ActionStore(tmp_path)
    store.import_action(action, json.dumps(sample_action()).encode("utf-8"))
    service = ControlService(
        settings=ControlSettings(
            interface="test0",
            enabled=enabled,
            fsm_whitelist=(501,),
        ),
        telemetry=telemetry or FakeTelemetry(),
        action_store=store,
    )
    return service, store


def test_replay_requires_controls_enabled(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path, enabled=False)
    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=True)
    assert error.value.code == "capability_disabled"


def test_replay_requires_operator_confirmation(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path)
    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=False)
    assert error.value.code == "confirmation_required"


def test_replay_rejects_stale_telemetry(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path, telemetry=FakeTelemetry(fresh=False))
    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=True)
    assert error.value.code == "telemetry_stale"


def test_replay_rejects_fsm_outside_whitelist(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path, telemetry=FakeTelemetry(fsm_id=505))
    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=True)
    assert error.value.code == "fsm_not_allowed"


def test_replay_rejects_unknown_fsm(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path, telemetry=FakeTelemetry(fsm_id=None))
    with pytest.raises(ControlError) as error:
        service.request_replay("demo", confirmed=True)
    assert error.value.code == "fsm_unknown"


def test_replay_rejects_missing_action(tmp_path: Path) -> None:
    service, _ = build_control(tmp_path)
    with pytest.raises(ControlError) as error:
        service.request_replay("nope", confirmed=True)
    assert error.value.code == "action_not_found"


# ---------------------------------------------------------------- HTTP 接口


def real_test_app(tmp_path: Path, **overrides: Any):
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        **overrides,
    )
    return create_app(
        settings,
        telemetry_adapter=TelemetryTestAdapter(),
    )


def test_action_endpoints_round_trip(tmp_path: Path) -> None:
    with TestClient(real_test_app(tmp_path)) as client:
        created = client.post(
            "/api/v1/actions/import",
            json={"name": "demo", "action": sample_action()},
        )
        assert created.status_code == 201
        assert created.json()["group"] == "arms_waist"

        listing = client.get("/api/v1/actions").json()
        assert [item["name"] for item in listing["actions"]] == ["demo"]

        exported = client.get("/api/v1/actions/demo")
        assert exported.status_code == 200
        assert exported.json()["group"] == "arms_waist"

        assert client.delete("/api/v1/actions/demo").json() == {"deleted": "demo"}
        assert client.get("/api/v1/actions").json()["actions"] == []


def test_control_endpoints_report_refusals(tmp_path: Path) -> None:
    with TestClient(real_test_app(tmp_path)) as client:
        client.post(
            "/api/v1/actions/import",
            json={"name": "demo", "action": sample_action()},
        )
        state = client.get("/api/v1/control/state").json()
        assert state["phase"] == "idle"
        assert state["available"] is False

        refused = client.post(
            "/api/v1/control/replay", json={"action": "demo", "confirmed": False}
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "capability_disabled"


def test_action_summary_reports_matching_audio(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "with_audio.pcm").write_bytes(b"\x00\x00" * 16)

    with TestClient(real_test_app(tmp_path)) as client:
        client.post(
            "/api/v1/actions/import",
            json={"name": "with_audio", "action": sample_action()},
        )
        client.post(
            "/api/v1/actions/import",
            json={"name": "silent", "action": sample_action()},
        )
        actions = {item["name"]: item for item in client.get("/api/v1/actions").json()["actions"]}

    assert actions["with_audio"]["has_audio"] is True
    assert actions["silent"]["has_audio"] is False


def test_replay_auto_picks_matching_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未显式指定语音时，后端应自动用同名 PCM，让讲解和手势一起播。"""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    pcm = b"\x11\x22" * 64
    (audio_dir / "demo.pcm").write_bytes(pcm)

    captured: dict[str, object] = {}

    def recording_request_replay(self, action, *, confirmed, audio_pcm=None):
        captured["action"] = action
        captured["confirmed"] = confirmed
        captured["audio"] = audio_pcm
        return self.state()

    app = real_test_app(tmp_path)
    monkeypatch.setattr(
        type(app.state.control), "request_replay", recording_request_replay
    )

    with TestClient(app) as client:
        client.post(
            "/api/v1/actions/import",
            json={"name": "demo", "action": sample_action()},
        )
        response = client.post(
            "/api/v1/control/replay", json={"action": "demo", "confirmed": True}
        )

    assert response.status_code == 200
    assert captured["action"] == "demo"
    assert captured["audio"] == pcm


def test_telemetry_snapshot_reports_controls_capability(tmp_path: Path) -> None:
    with TestClient(real_test_app(tmp_path, controls_enabled=True)) as client:
        snapshot = client.get("/api/v1/status").json()
        assert snapshot["capabilities"]["controls"]["available"] is True
