"""深度相机取流的后端测试。

全程不碰硬件：注入一个假的取帧器，验证「有帧就推流、没帧就说清原因」。
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from robot_gui.camera import CameraError, DepthCamera
from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter

JPEG = b"\xff\xd8\xff\xe0 fake jpeg \xff\xd9"


class FakeGrabber:
    """按脚本返回帧或抛错的假取帧器。"""

    def __init__(self, behaviour: list[object]) -> None:
        self._behaviour = behaviour
        self.index = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def grab(self) -> bytes:
        item = self._behaviour[min(self.index, len(self._behaviour) - 1)]
        self.index += 1
        if isinstance(item, Exception):
            raise item
        return bytes(item)  # type: ignore[arg-type]


def build_client(tmp_path: Path, camera: DepthCamera) -> TestClient:
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=tmp_path / "records",
    )
    app = create_app(
        settings,
        telemetry_adapter=TelemetryTestAdapter(),
        camera=camera,
    )
    return TestClient(app)


def wait_for_frame(camera: DepthCamera, timeout: float = 5.0) -> bool:
    deadline = threading.Event()
    waited = 0.0
    while waited < timeout:
        if camera.latest_frame() is not None:
            return True
        deadline.wait(0.05)
        waited += 0.05
    return False


def test_camera_state_reports_the_failure_reason(tmp_path: Path) -> None:
    """取不到帧时要说清原因，不能给一个假可用状态。"""
    camera = DepthCamera(
        grabber_factory=lambda: FakeGrabber(
            [CameraError("camera.grab_failed", "取不到")]
        ),
        poll_interval_s=0.05,
    )
    with build_client(tmp_path, camera) as client:
        state = client.get("/api/v1/camera/state").json()
        assert state["available"] is False
        assert state["error_code"] == "camera.grab_failed"
        assert "取不到" in state["error_message"]

        frame = client.get("/api/v1/camera/frame")
        assert frame.status_code == 404
        assert frame.json()["detail"]["code"] == "camera.grab_failed"


def test_camera_frame_endpoint_serves_the_latest_jpeg(tmp_path: Path) -> None:
    camera = DepthCamera(
        grabber_factory=lambda: FakeGrabber([JPEG]),
        poll_interval_s=0.05,
    )
    with build_client(tmp_path, camera) as client:
        assert wait_for_frame(camera)
        response = client.get("/api/v1/camera/frame")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == JPEG

        state = client.get("/api/v1/camera/state").json()
        assert state["available"] is True
        assert state["frames"] >= 1


def test_camera_grabber_that_cannot_open_is_reported(tmp_path: Path) -> None:
    class BrokenGrabber(FakeGrabber):
        def open(self) -> None:
            raise RuntimeError("服务没起")

    camera = DepthCamera(
        grabber_factory=lambda: BrokenGrabber([]),
        poll_interval_s=0.05,
    )
    with build_client(tmp_path, camera) as client:
        state = client.get("/api/v1/camera/state").json()
        assert state["available"] is False
        assert state["error_code"] == "camera.unavailable"
        assert "服务没起" in state["error_message"]
