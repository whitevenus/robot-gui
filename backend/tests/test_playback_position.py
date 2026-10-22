"""播放位置的测试：机器人现在播到哪一段了。

外部（真机课堂页）要靠这个把画面跟到机器人的实际位置：机器人轨迹里段与段
之间有 2 秒衔接段，暂停续播还会重新规划，自己算时间一定对不上。

真机路径不接硬件：RobotSession 与规划器都用替身，只跑纯逻辑。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from robot_gui import control as control_module
from robot_gui.actions import ActionStore
from robot_gui.control import ControlService, ControlSettings
from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter


class FakeTelemetry:
    """够用就好：回放路径只要快照、关节角、IMU 三样。"""

    def snapshot(self, mode: str, interface: str | None, adapter_error: str | None):
        return {
            "sources": {"low_state": {"state": "fresh"}},
            "robot": {"mode_machine": 5, "fsm": {"id": 501, "mode": None}},
        }

    def latest_joint_angles(self) -> dict[str, float]:
        return {}

    def latest_joint_modes(self) -> dict[str, int]:
        return {}

    def latest_imu_rpy(self):
        return ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])


def build_service(tmp_path: Path) -> ControlService:
    return ControlService(
        settings=ControlSettings(
            interface="test0",
            enabled=True,
            fsm_whitelist=(501,),
            hold_soak_s=0.0,
        ),
        telemetry=FakeTelemetry(),
        action_store=ActionStore(tmp_path),
    )


# ---------------------------------------------------------------- 位置算法


def test_nothing_is_playing_before_the_first_replay(tmp_path: Path) -> None:
    """没在播就没有当前位置：下标 -1、动作名空、已播 0 秒。"""
    service = build_service(tmp_path)

    assert service._playback_position() == (-1, "", 0.0)  # noqa: SLF001


def test_the_current_segment_is_the_last_one_that_started(tmp_path: Path) -> None:
    """marks 是各段动作本体的起始时刻，已经起播的最后一段就是正在播的那段。"""
    service = build_service(tmp_path)
    service._run_names = ["p1_s0", "p1_s1", "p2_s0"]  # noqa: SLF001
    service._run_marks = [2.0, 12.0, 22.0]  # noqa: SLF001

    # 还在进入衔接段里：谁都还没起播，但当前段算第 0 段。
    service._play_started = time.monotonic() - 1.0  # noqa: SLF001
    assert service._playback_position()[:2] == (0, "p1_s0")  # noqa: SLF001

    # 播到 15 秒：p1_s0、p1_s1 已经起播，当前段是 p1_s1。
    service._play_started = time.monotonic() - 15.0  # noqa: SLF001
    index, action, elapsed = service._playback_position()  # noqa: SLF001
    assert (index, action) == (1, "p1_s1")
    assert elapsed == pytest.approx(15.0, abs=0.5)

    # 播到 25 秒：第三段已经起播。
    service._play_started = time.monotonic() - 25.0  # noqa: SLF001
    assert service._playback_position()[:2] == (2, "p2_s0")  # noqa: SLF001


def test_the_current_segment_agrees_with_the_resume_boundary(tmp_path: Path) -> None:
    """位置与"暂停后从下一段续播"必须是同一个口径，否则画面会跳到错的段。

    不变量有两条：
    - 当前段 = 最后一段**已经起播**的动作（`started - 1`，还没起播时算第 0 段）；
    - 续播的第一段 = 下一段（`started`）；最后一段都播完了就重播最后一段。
    """
    service = build_service(tmp_path)
    names = ["p1_s0", "p1_s1", "p2_s0"]
    service._run_names = names  # noqa: SLF001
    service._run_marks = [2.0, 12.0, 22.0]  # noqa: SLF001

    for elapsed in (1.0, 5.0, 15.0, 25.0):
        service._play_started = time.monotonic() - elapsed  # noqa: SLF001
        index, action, _ = service._playback_position()  # noqa: SLF001
        remaining = service._remaining_after(elapsed)  # noqa: SLF001
        started = sum(1 for mark in [2.0, 12.0, 22.0] if mark <= elapsed)

        assert index == max(started - 1, 0)
        assert action == names[index]
        assert remaining[0] == names[min(started, len(names) - 1)]


# ---------------------------------------------------------------- 回放全程


class FakeSession:
    """替身：接管一下、把轨迹播完，不碰硬件。"""

    observer: Any = None

    def __init__(self, interface, gains, cancel):
        self.interface = interface
        self.gains = gains
        self.cancel = cancel
        self.start_pose = control_module.StartPose(
            leg_q=np.zeros(12), upper_q=np.zeros(17)
        )
        self.acquired = False
        self.played: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def acquire(self) -> None:
        self.acquired = True

    def play(self, trajectory) -> None:
        self.played.append(trajectory)
        if self.observer is not None:
            self.observer(trajectory)


def fake_trajectory():
    from g1_action_studio.motion import Action, action_curve

    action = fake_action()
    return action_curve(action, np.zeros(17), 1.0)


def fake_action():
    from g1_action_studio.motion import Action

    return Action("arms", np.array([0.0, 1.0]), np.zeros((2, 14)))


def install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 RobotSession 与规划器换成替身。"""
    trajectory = fake_trajectory()

    def fake_plan(actions, start, model, speed, transition, cancel):
        return trajectory, [0.0, 0.5]

    monkeypatch.setattr(control_module, "RobotSession", FakeSession)
    monkeypatch.setattr(control_module, "plan_sequence_with_marks", fake_plan)


def test_a_running_replay_reports_the_segment_being_played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """播放途中状态里有当前位置，播完就清掉。"""
    observed: dict[str, dict] = {}
    service = build_service(tmp_path)

    def observer(trajectory) -> None:
        # marks 是 [0.0, 0.5]：播过 0.5 秒之后当前段就是第二段。
        time.sleep(0.6)
        observed["during"] = service.state()

    install_fakes(monkeypatch)
    # staticmethod：挂在类上的观察者不能变成绑定方法（否则第一个参数会被当成 self）。
    monkeypatch.setattr(FakeSession, "observer", staticmethod(observer))

    service._run_names = ["p1_s0", "p1_s1"]  # noqa: SLF001
    service._run_marks = [0.0, 0.5]  # noqa: SLF001
    service._play_started = None  # noqa: SLF001
    service._run(["p1_s0", "p1_s1"], [fake_action()], {}, 1.0, "course")  # noqa: SLF001

    during = observed["during"]
    assert during["phase"] == "playing"
    assert during["current_action"] == "p1_s1"
    assert during["current_index"] == 1
    assert during["elapsed_s"] > 0.5

    # 播完之后位置被复位：已播秒数不会一直涨。
    after = service.state()
    assert after["current_action"] == ""
    assert after["current_index"] == -1
    assert after["elapsed_s"] == 0.0


# ---------------------------------------------------------------- HTTP 接口


def test_the_state_endpoint_exposes_the_playback_position(tmp_path: Path) -> None:
    """接口层也带这三个字段：外部只读这一个接口就知道机器人在哪儿。"""
    settings = RuntimeSettings(
        mode="real", dds_interface="test0", data_dir=tmp_path, controls_enabled=True
    )
    app = create_app(settings, telemetry_adapter=TelemetryTestAdapter())

    with TestClient(app) as client:
        state = client.get("/api/v1/control/state").json()

    assert state["current_action"] == ""
    assert state["current_index"] == -1
    assert state["elapsed_s"] == 0.0
