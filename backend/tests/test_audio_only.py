"""只播讲解语音的接口测试。

全程不碰硬件：注入一个假的推流器，检查语音会话的启停、暂停续播，以及
「机器人不动」这件事（控制会话始终是空闲）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter


class BlockingPusher:
    """会一直播下去的假推流器：直到被 stop() 打断。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.clips: list[tuple[float, bytes]] = []

    def push(self, clips: Sequence[tuple[float, bytes]], stop: threading.Event) -> str:
        self.clips = list(clips)
        self.started.set()
        while not stop.is_set() and not self.stopped.is_set():
            stop.wait(0.05)
        return "audio.cancelled"

    def stop(self) -> None:
        self.stopped.set()


def write_action(directory: Path, name: str, duration_s: float = 1.0) -> None:
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "group": "arms",
                "frames": [
                    {"t": 0.0, "q": [0.0] * 14},
                    {"t": duration_s, "q": [0.0] * 14},
                ],
            }
        ),
        encoding="utf-8",
    )


def write_audio(directory: Path, name: str, seconds: float = 1.0) -> None:
    audio = directory / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / f"{name}.pcm").write_bytes(b"\x00\x01" * int(16000 * seconds))


def build_client(tmp_path: Path) -> tuple[TestClient, BlockingPusher]:
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=tmp_path / "records",
        transition_seconds=0.05,
    )
    pusher = BlockingPusher()
    app = create_app(
        settings,
        telemetry_adapter=TelemetryTestAdapter(),
        audio_pusher=pusher,
    )
    return TestClient(app), pusher


def test_play_one_segment_starts_a_narration_session(tmp_path: Path) -> None:
    write_action(tmp_path, "p1_s0")
    write_audio(tmp_path, "p1_s0", seconds=1.0)
    client, pusher = build_client(tmp_path)
    with client:
        response = client.post("/api/v1/control/audio", json={"name": "p1_s0"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["phase"] == "playing"
        assert body["total"] == 1
        assert pusher.started.wait(timeout=5)

        # 控制会话始终是空闲的：只播语音不接管控制权。
        state = client.get("/api/v1/control/state").json()
        assert state["phase"] == "idle"
        assert state["narration"]["phase"] == "playing"


def test_pause_stops_the_voice_and_resume_continues(tmp_path: Path) -> None:
    for name in ("p1_s0", "p1_s1"):
        write_action(tmp_path, name)
        write_audio(tmp_path, name, seconds=1.0)
    (tmp_path / "course.json").write_text(
        json.dumps({"name": "stage_c", "actions": ["p1_s0", "p1_s1"]}),
        encoding="utf-8",
    )
    client, pusher = build_client(tmp_path)
    with client:
        client.post("/api/v1/control/audio", json={"course": "default"})
        assert pusher.started.wait(timeout=5)

        paused = client.post("/api/v1/control/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["phase"] == "paused"
        # 打断的那一段不重播，接着从下一段开始。
        assert paused.json()["remaining"] == ["p1_s1"]

        state = client.get("/api/v1/control/state").json()
        assert state["narration"]["phase"] == "paused"
        assert state["phase"] == "idle"

        resumed = client.post("/api/v1/control/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["phase"] == "playing"


def test_release_stops_the_voice(tmp_path: Path) -> None:
    write_action(tmp_path, "p1_s0")
    write_audio(tmp_path, "p1_s0", seconds=1.0)
    client, pusher = build_client(tmp_path)
    with client:
        client.post("/api/v1/control/audio", json={"name": "p1_s0"})
        assert pusher.started.wait(timeout=5)

        released = client.post("/api/v1/control/release")
        assert released.status_code == 200, released.text
        assert released.json()["phase"] == "idle"
        assert (
            client.get("/api/v1/control/state").json()["narration"]["phase"] == "idle"
        )


def test_audio_without_a_target_is_rejected(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    with client:
        response = client.post("/api/v1/control/audio", json={})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "audio_target_required"


def test_audio_with_unknown_segment_is_rejected(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    with client:
        response = client.post("/api/v1/control/audio", json={"name": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "audio_not_found"


def test_audio_without_audio_enabled_is_rejected(tmp_path: Path) -> None:
    """语音推流没开时要说清楚原因，而不是静默地什么都不发生。"""
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=tmp_path / "records",
        audio_enabled=False,
    )
    app = create_app(settings, telemetry_adapter=TelemetryTestAdapter())
    with TestClient(app) as client:
        response = client.post("/api/v1/control/audio", json={"name": "p1_s0"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "audio_disabled"
