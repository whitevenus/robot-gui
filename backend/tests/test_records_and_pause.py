"""执行记录与暂停/续播的测试。

两条线都不碰硬件：
- 记录：往记录目录里写一份 JSON，走接口读回来（列表 / 详情 / 下载）；
- 暂停：只测"暂停后接着播哪几段"这段纯逻辑，���不动真机。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robot_gui.actions import ActionStore
from robot_gui.control import ControlError, ControlService, ControlSettings
from robot_gui.main import RuntimeSettings, create_app
from robot_gui.records import (
    OUTCOME_COMPLETED,
    ExecutionRecorder,
    RecordStore,
    RecordStoreError,
)
from test_real_telemetry import TelemetryTestAdapter


def sample_record(store: RecordStore, record_id: str = "20261021T091240Z-p1_s2"):
    record = {
        "id": record_id,
        "kind": "action",
        "label": "p1_s2",
        "actions": ["p1_s2"],
        "speed": 0.8,
        "started_at": "2026-10-21T09:12:40+00:00",
        "finished_at": "2026-10-21T09:12:52+00:00",
        "elapsed_s": 12.3,
        "trajectory_duration_s": 8.9,
        "outcome": OUTCOME_COMPLETED,
        "error_code": "",
        "error_message": "",
        "segments": [{"name": "p1_s2", "start_s": 2.0}],
        "tracking": {
            "samples": 100,
            "joints": {
                "left_shoulder_pitch": {
                    "max_error_rad": 0.146,
                    "mean_error_rad": 0.02,
                    "target_rad": 0.1,
                    "measured_rad": 0.05,
                }
            },
        },
        "lower_body": {
            "threshold_deg": 5.0,
            "max_drift_deg": 0.31,
            "max_drift_joint": "left_hip_pitch",
            "waist_pitch_lean_deg": 4.02,
            "waist_pitch_drift_deg": 5.43,
            "violated": False,
        },
        "samples": [
            {
                "t": 0.0,
                "waist_pitch": -0.0219,
                "waist_modes": [1, 1, 1],
                "torso_pitch": 0.0102,
                "hip_pitch": -0.0715,
            }
        ],
    }
    store.write(record)
    return record


# ---------------------------------------------------------------- 记录存储


def test_record_store_writes_lists_and_reads_back(tmp_path: Path) -> None:
    store = RecordStore(tmp_path / "records")
    record = sample_record(store)

    assert (tmp_path / "records" / "20261021T091240Z-p1_s2.json").is_file()
    assert store.read(record["id"]) == record
    summaries = store.list()
    assert len(summaries) == 1
    assert summaries[0]["id"] == record["id"]
    assert summaries[0]["waist_pitch_lean_deg"] == 4.02
    assert summaries[0]["sample_count"] == 1


def test_record_store_skips_broken_files_and_rejects_bad_ids(tmp_path: Path) -> None:
    store = RecordStore(tmp_path / "records")
    (tmp_path / "records").mkdir(parents=True)
    (tmp_path / "records" / "broken.json").write_text("{ not json", encoding="utf-8")

    assert store.list() == []
    with pytest.raises(RecordStoreError) as error:
        store.read("../escape")
    assert error.value.code == "record_id_invalid"
    with pytest.raises(RecordStoreError) as missing:
        store.read("nope")
    assert missing.value.code == "record_not_found"


def test_execution_recorder_samples_at_a_fixed_rate_and_builds_the_record() -> None:
    recorder = ExecutionRecorder(
        kind="course",
        label="整套 2 段",
        actions=["p1_s0", "p2_s1"],
        speed=0.8,
        slug="course",
    )
    for step in range(10):
        recorder.sample(
            step * 0.02,
            waist_pitch=0.01,
            waist_modes=[1, 1, 1],
            torso_pitch=0.02,
            hip_pitch=0.03,
        )

    record = recorder.build(
        outcome=OUTCOME_COMPLETED,
        elapsed_s=12.3,
        trajectory_duration_s=8.9,
        tracking={"samples": 1},
        lower_body={"violated": False},
        segments=[{"name": "p1_s0", "start_s": 2.0}],
    )

    # 20 Hz 采样：0.2 秒里留 4 条（0 / 0.06 / 0.12 / 0.18），不是每步一条。
    assert len(record["samples"]) == 4
    assert [sample["t"] for sample in record["samples"]] == [0.0, 0.06, 0.12, 0.18]
    assert record["samples"][0]["waist_modes"] == [1, 1, 1]
    assert record["id"].startswith("20")
    assert record["id"].endswith("-course")
    assert record["outcome"] == OUTCOME_COMPLETED


# ---------------------------------------------------------------- 记录接口


def test_record_endpoints_list_detail_and_download(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    store = RecordStore(records_dir)
    record = sample_record(store)
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=records_dir,
    )
    app = create_app(settings, telemetry_adapter=TelemetryTestAdapter())

    with TestClient(app) as client:
        listing = client.get("/api/v1/records").json()
        assert [item["id"] for item in listing["records"]] == [record["id"]]

        detail = client.get(f"/api/v1/records/{record['id']}").json()
        assert detail["label"] == "p1_s2"
        assert detail["speed"] == 0.8
        assert detail["samples"][0]["waist_modes"] == [1, 1, 1]

        download = client.get(f"/api/v1/records/{record['id']}/download")
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]
        assert json.loads(download.text)["id"] == record["id"]

        assert client.get("/api/v1/records/nope").status_code == 404


# ---------------------------------------------------------------- 暂停续播


class FakeTelemetry:
    def snapshot(self, mode: str, interface: str | None, adapter_error: str | None):
        return {
            "sources": {"low_state": {"state": "fresh"}},
            "robot": {"mode_machine": 5, "fsm": {"id": 501, "mode": None}},
        }


def build_service(tmp_path: Path) -> ControlService:
    store = ActionStore(tmp_path)
    return ControlService(
        settings=ControlSettings(interface="test0", enabled=True, fsm_whitelist=(501,)),
        telemetry=FakeTelemetry(),
        action_store=store,
    )


def test_remaining_actions_start_from_the_next_segment(tmp_path: Path) -> None:
    """整课暂停后从**下一段**开始：被打断的那段不重播。"""
    service = build_service(tmp_path)
    service._run_names = ["p1_s0", "p1_s1", "p2_s0"]  # noqa: SLF001
    service._run_marks = [2.0, 12.0, 22.0]  # noqa: SLF001

    # 播到 15 秒：p1_s0、p1_s1 已经起播，下一段是 p2_s0。
    assert service._remaining_after(15.0) == ["p2_s0"]  # noqa: SLF001
    # 还在进入衔接段里：谁都还没起播，整套都要接着播。
    assert service._remaining_after(1.0) == ["p1_s0", "p1_s1", "p2_s0"]  # noqa: SLF001


def test_single_action_replays_itself_after_a_pause(tmp_path: Path) -> None:
    """单个动作还没播完：续播就是重播这一段（没有"下一段"可播）。"""
    service = build_service(tmp_path)
    service._run_names = ["p1_s2"]  # noqa: SLF001
    service._run_marks = [2.0]  # noqa: SLF001

    assert service._remaining_after(5.0) == ["p1_s2"]  # noqa: SLF001


def test_pause_without_a_running_replay_is_refused(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(ControlError) as error:
        service.pause()
    assert error.value.code == "control_idle"


def test_resume_without_a_paused_session_is_refused(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(ControlError) as error:
        service.resume()
    assert error.value.code == "not_paused"


# ---------------------------------------------------------------- 暂停的动作截短


def sample_trajectory():
    """一条 1 秒的 arms 轨迹：手臂从 0 走到 0.1 rad，腰不动。"""
    import numpy as np
    from g1_action_studio.motion import Action, action_curve

    action = Action("arms", np.array([0.0, 1.0]), np.zeros((2, 14)))
    return action_curve(action, np.zeros(17), 1.0)


def test_truncate_stops_the_motion_at_the_current_moment() -> None:
    """截短后的轨迹：studio 下一帧就认为这段播完了，目标角停在当前位置。"""
    import numpy as np

    from robot_gui.control import _truncate

    trajectory = sample_trajectory()
    truncated = _truncate(trajectory, 0.5)

    assert truncated is not None
    assert truncated.duration == pytest.approx(0.5)
    # 目标角连续：截短处的位置就是原轨迹在该时刻的位置。
    expected = np.asarray(trajectory.polynomial(0.5), dtype=float)
    assert np.allclose(truncated.last_q, expected)
    # 截短后的曲线在 [0, 0.5] 上与原曲线一致（换基没算错）。
    for moment in (0.0, 0.13, 0.27, 0.41, 0.5):
        assert np.allclose(
            truncated.polynomial(moment),
            trajectory.polynomial(moment),
            atol=1e-9,
        ), f"t={moment} 处截短后的曲线与原曲线不一致"
    # studio 在采样时刻越过时长后取 last_q —— 动作就停在这里。
    assert np.allclose(truncated.sample(0.5)[0], expected)
    assert np.allclose(truncated.sample(0.52)[0], expected)


def test_truncate_is_refused_outside_the_played_range() -> None:
    """还没开始播或已经播完时不用截短：直接按"没有动作在播"处理。"""
    from robot_gui.control import _truncate

    trajectory = sample_trajectory()
    assert _truncate(trajectory, 0.0) is None
    assert _truncate(trajectory, 1.0) is None
    assert _truncate(trajectory, 5.0) is None
    assert _truncate(None, 0.5) is None


def test_return_connection_brings_the_arms_back_to_the_baseline() -> None:
    """回基线的那一段：从停住的位置一路连回接管姿态（腰也一起回）。"""
    import threading

    import numpy as np
    from g1_action_studio.model import ArmModel
    from g1_action_studio.planner import connect

    model = ArmModel(np.zeros(12))
    baseline = np.zeros(17)
    source = baseline.copy()
    source[0] = 0.3  # 左肩 pitch 偏了 0.3 rad

    returning = connect(model, source, baseline, {"arms_waist"}, 2.0, threading.Event())

    assert returning is not None
    assert np.allclose(returning.first_q, source)
    assert np.allclose(returning.last_q, baseline)
    # 终点就是基线：机器人回到接管瞬间的姿态才交还控制权。
    final, _ = returning.sample(returning.duration)
    assert np.allclose(final, baseline)
