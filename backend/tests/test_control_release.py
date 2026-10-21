"""动作库、真机控制前置检查与音频端口的测试。

真机路径不接硬件：所有拒绝分支都在下发任何控制命令之前返回，
因此可以完整覆盖。成功路径用替身替换 RobotSession 与 AudioClient。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from robot_gui.actions import ActionStore, ActionStoreError
from robot_gui.audio import G1AudioPort, WorkerAudioPusher
from robot_gui.control import ControlError, ControlService, ControlSettings
from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter


def sample_action(frames: int = 4) -> dict[str, Any]:
    """arms 组 = 14 维动作（腰锁在接管瞬间的姿态，不执行腰轨迹）。"""
    return {
        "group": "arms",
        "frames": [
            {"t": round(index * 0.04, 4), "q": [0.0] * 14} for index in range(frames)
        ],
    }


def sample_pcm(milliseconds: int = 200) -> bytes:
    return b"\x00\x00" * int(16000 * milliseconds / 1000)


class FakeTelemetry:
    def __init__(
        self, *, fresh: bool = True, fsm_id: int | None = 501, mode_machine: int = 5
    ):
        self._fresh = fresh
        self._fsm_id = fsm_id
        self._mode_machine = mode_machine

    def snapshot(self, mode: str, interface: str | None, adapter_error: str | None):
        state = "fresh" if self._fresh else "offline"
        return {
            "sources": {"low_state": {"state": state}},
            "robot": {
                "mode_machine": self._mode_machine,
                "fsm": None
                if self._fsm_id is None
                else {"id": self._fsm_id, "mode": None},
            },
        }


# ---------------------------------------------------------------- 动作库


def test_action_store_import_list_export_delete(tmp_path: Path) -> None:
    store = ActionStore(tmp_path)
    body = json.dumps(sample_action()).encode("utf-8")

    summary = store.import_action("demo", body)
    assert summary.group == "arms"
    assert summary.frames == 4
    assert summary.duration_s == pytest.approx(0.12)

    assert [item.name for item in store.list()] == ["demo"]
    assert store.export_action("demo") == body
    assert store.load("demo").group == "arms"

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
        store.import_action("bad", json.dumps({"group": "arms", "frames": []}).encode())
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


class FakeAudioPusher:
    """替身：记录推流调用，模拟子进程的结果。"""

    def __init__(self, error: str = "") -> None:
        self.error = error
        self.calls: list[tuple[str, bytes]] = []
        self.stopped = 0

    def push(self, stream_id: str, pcm: bytes, stop: threading.Event) -> str:
        self.calls.append((stream_id, pcm))
        return self.error

    def stop(self) -> None:
        self.stopped += 1


def test_audio_port_pushes_the_whole_clip_once() -> None:
    """整段一次推完：分块只会把"第 N 块被拒 → 只播开头几个字"的故障面放大。"""
    pusher = FakeAudioPusher()
    port = G1AudioPort("robot-gui", pusher=pusher)
    pcm = sample_pcm(3200)  # 3.2 秒

    assert port.play(pcm, threading.Event()) == ""
    assert len(pusher.calls) == 1
    assert pusher.calls[0][1] == pcm
    assert port.playing is False


def test_audio_port_has_no_tts_fallback() -> None:
    """只走 PCM：音频端口不再提供 TTS 播报，讲解出声只有课程包原声一路。"""
    assert not hasattr(G1AudioPort, "speak")


def test_audio_port_rejects_odd_length() -> None:
    pusher = FakeAudioPusher()
    port = G1AudioPort("robot-gui", pusher=pusher)

    assert port.play(b"\x00", threading.Event()) == "audio.invalid_pcm_length"
    assert pusher.calls == []


def test_audio_port_refuses_a_second_playback_while_playing() -> None:
    """同一时刻只允许一段讲解在放。"""

    class BlockingPusher(FakeAudioPusher):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def push(self, stream_id: str, pcm: bytes, stop: threading.Event) -> str:
            self.calls.append((stream_id, pcm))
            self.entered.set()
            self.release.wait(5.0)
            return self.error

    pusher = BlockingPusher()
    port = G1AudioPort("robot-gui", pusher=pusher)
    result: list[str] = []

    def play() -> None:
        result.append(port.play(sample_pcm(200), threading.Event()))

    thread = threading.Thread(target=play)
    thread.start()
    assert pusher.entered.wait(5.0)

    assert port.play(sample_pcm(200), threading.Event()) == "audio.already_playing"

    pusher.release.set()
    thread.join(timeout=5.0)
    assert result == [""]


def test_audio_port_uses_a_unique_stream_id_per_playback() -> None:
    """stream_id 必须全局唯一，否则进程重启后会撞上机器人端残留的旧流。"""
    pusher = FakeAudioPusher()
    port = G1AudioPort("robot-gui", pusher=pusher)
    port.play(sample_pcm(200), threading.Event())
    port.play(sample_pcm(200), threading.Event())

    stream_ids = [stream_id for stream_id, _ in pusher.calls]
    assert len(set(stream_ids)) == len(stream_ids)


def test_audio_port_does_not_retry_a_rejected_push() -> None:
    """被拒不能重推：同 stream_id 重推会让机器人端把已播内容再播一遍。"""
    pusher = FakeAudioPusher(error="audio.stream_rejected")
    port = G1AudioPort("robot-gui", pusher=pusher)

    assert port.play(sample_pcm(200), threading.Event()) == "audio.stream_rejected"
    assert len(pusher.calls) == 1
    # 失败之后仍然能再播：闩锁只记账，不封锁。
    assert port.play(sample_pcm(200), threading.Event()) == "audio.stream_rejected"
    assert len(pusher.calls) == 2


def test_audio_port_forwards_cancellation() -> None:
    pusher = FakeAudioPusher(error="audio.cancelled")
    port = G1AudioPort("robot-gui", pusher=pusher)

    assert port.play(sample_pcm(200), threading.Event()) == "audio.cancelled"
    assert port.playing is False


def test_worker_pusher_runs_the_audio_worker_module() -> None:
    """推流必须在独立进程里跑：回放期间同进程内的 RPC 回包收不到。"""
    pusher = WorkerAudioPusher("robot-gui", python_executable="/usr/bin/python3")
    command = pusher.build_command("robot-gui-123")

    assert command[:3] == ["/usr/bin/python3", "-m", "robot_gui.audio_worker"]
    assert "--app-name" in command
    assert "robot-gui" in command
    assert "--stream-id" in command
    assert "robot-gui-123" in command


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
        assert created.json()["group"] == "arms"

        listing = client.get("/api/v1/actions").json()
        assert [item["name"] for item in listing["actions"]] == ["demo"]

        exported = client.get("/api/v1/actions/demo")
        assert exported.status_code == 200
        assert exported.json()["group"] == "arms"

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
        actions = {
            item["name"]: item
            for item in client.get("/api/v1/actions").json()["actions"]
        }

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
