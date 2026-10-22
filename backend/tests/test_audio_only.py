"""只播讲解语音的接口测试。

全程不碰硬件：注入一个假的推流器，检查 PCM 按什么时刻表推给扬声器，
以及机器人的控制状态一点没被碰过（不接管、不回放）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter


class RecordingPusher:
    """记录推流内容的假推流器。"""

    def __init__(self) -> None:
        self.clips: list[tuple[float, bytes]] = []
        self.pushed = threading.Event()

    def push(self, clips: Sequence[tuple[float, bytes]], stop: threading.Event) -> str:
        self.clips = list(clips)
        self.pushed.set()
        return ""

    def stop(self) -> None:
        return


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


def build_client(tmp_path: Path, pusher: RecordingPusher) -> TestClient:
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=tmp_path / "records",
        transition_seconds=2.0,
    )
    app = create_app(
        settings,
        telemetry_adapter=TelemetryTestAdapter(),
        audio_pusher=pusher,
    )
    return TestClient(app)


def test_play_one_segment_audio_leaves_the_robot_alone(tmp_path: Path) -> None:
    write_action(tmp_path, "p1_s0")
    write_audio(tmp_path, "p1_s0", seconds=1.0)
    pusher = RecordingPusher()
    with build_client(tmp_path, pusher) as client:
        response = client.post("/api/v1/control/audio", json={"name": "p1_s0"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["clips"] == 1
        assert body["duration_s"] == 1.0

        assert pusher.pushed.wait(timeout=5)
        assert [at for at, _ in pusher.clips] == [0.0]

        # 只播语音不接管控制权：状态还是空闲，没有回放在跑。
        state = client.get("/api/v1/control/state").json()
        assert state["phase"] == "idle"
        assert state["trajectory_duration_s"] == 0


def test_play_whole_course_audio_keeps_the_replay_timeline(tmp_path: Path) -> None:
    for name in ("p1_s0", "p1_s1"):
        write_action(tmp_path, name, duration_s=1.0)
        write_audio(tmp_path, name, seconds=1.0)
    (tmp_path / "course.json").write_text(
        json.dumps({"name": "stage_c", "actions": ["p1_s0", "p1_s1"]}),
        encoding="utf-8",
    )
    pusher = RecordingPusher()
    with build_client(tmp_path, pusher) as client:
        response = client.post("/api/v1/control/audio", json={"course": "default"})
        assert response.status_code == 200, response.text
        assert response.json()["clips"] == 2

        assert pusher.pushed.wait(timeout=5)
        # 每段之间留一段衔接的静音：2 秒起步，第二段在 2 + 1 + 2 秒。
        assert [round(at, 3) for at, _ in pusher.clips] == [2.0, 5.0]


def test_play_default_course_audio_with_a_boolean_course_flag(tmp_path: Path) -> None:
    """和回放接口一样，course=true 指默认课。"""
    write_action(tmp_path, "p1_s0")
    write_audio(tmp_path, "p1_s0", seconds=1.0)
    (tmp_path / "course.json").write_text(
        json.dumps({"name": "stage_c", "actions": ["p1_s0"]}), encoding="utf-8"
    )
    pusher = RecordingPusher()
    with build_client(tmp_path, pusher) as client:
        response = client.post("/api/v1/control/audio", json={"course": True})
        assert response.status_code == 200, response.text
        assert pusher.pushed.wait(timeout=5)
        assert len(pusher.clips) == 1


def test_play_audio_without_a_target_is_rejected(tmp_path: Path) -> None:
    pusher = RecordingPusher()
    with build_client(tmp_path, pusher) as client:
        response = client.post("/api/v1/control/audio", json={})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "audio_target_required"


def test_play_audio_with_unknown_segment_is_rejected(tmp_path: Path) -> None:
    pusher = RecordingPusher()
    with build_client(tmp_path, pusher) as client:
        response = client.post("/api/v1/control/audio", json={"name": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "audio_not_found"


def test_play_audio_without_audio_enabled_is_rejected(tmp_path: Path) -> None:
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
