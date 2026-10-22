"""G1 深度相机取流。

G1 头部的相机在机器上就是一个 V4L2 设备（`/dev/video*`），不经过遥测话题，
也没有装厂商 SDK。后台线程按固定间隔取帧、缓存最新的 JPEG，供网页端做 MJPEG
推流。

**取不到就如实说取不到**：设备打不开、读不出帧、或者这台机器没装相机，
`state()` 里会写明原因，页面照实显示，不用别的画面凑数。

取流通道的取舍：读帧留在本进程（和遥测的 FSM 回读一样）。之前音频推流要挪到
子进程，是因为推流时控制回路把 DDS 通道占满了，那是写方向的问题，跟这里
不一样。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

#: 取一帧的实现：成功返回 JPEG 字节，失败抛 CameraError。
FrameGrabber = Callable[[], bytes]


class CameraError(RuntimeError):
    """取帧失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class V4l2Grabber:
    """从 V4L2 设备取帧（G1 头部的相机在机器上就是 /dev/video*）。

    2026-10-22 真机实测（`tools/probe_depth_camera.py`）：**videohub RPC 不通**
    （返回 3102），`pyrealsense2` / `pyorbbec` 都没装，但 `/dev/video0`、`/dev/video2`、
    `/dev/video4` 能读到 16:9 的画面（256×144 / 424×240 / 320×180）——这三个是同一个
    头部相机在不同分辨率下的输出节点，偶数号能读，奇数号是配对的元数据节点。

    所以默认走 V4L2 + OpenCV：**分辨率最高的那个设备**优先。哪一路真是头部相机，
    看一眼画面就知道；要指定设备就设 `ROBOT_GUI_DEPTH_CAMERA_DEVICE`。
    """

    def __init__(self, device: str) -> None:
        self._device = device
        self._capture: Any = None
        self._width = 0
        self._height = 0

    @property
    def device(self) -> str:
        return self._device

    @property
    def size(self) -> tuple[int, int]:
        return self._width, self._height

    def describe(self) -> str:
        return f"{self._device}（{self._width}×{self._height}）"

    def open(self) -> None:
        try:
            import cv2  # noqa: PLC0415 - 按需导入，没装就报清楚
        except ImportError as error:
            raise CameraError(
                "camera.opencv_missing",
                f"没有 OpenCV，取不了相机画面：{error}",
            ) from error
        capture = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            raise CameraError(
                "camera.device_unavailable",
                f"打不开相机设备 {self._device}。",
            )
        self._capture = capture
        self._width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def grab(self) -> bytes:
        if self._capture is None:
            raise CameraError("camera.client_missing", "相机设备没有打开。")
        import cv2  # noqa: PLC0415 - 同上

        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise CameraError("camera.grab_failed", f"{self._device} 读不出帧。")
        encoded, buffer = cv2.imencode(".jpg", frame)
        if not encoded:
            raise CameraError("camera.encode_failed", "这一帧编码成 JPEG 失败。")
        return bytes(buffer)

    @staticmethod
    def pick() -> str:
        """挑一个能用的设备：能读到画面、且分辨率最高的那个。"""
        try:
            import cv2  # noqa: PLC0415 - 按需导入
        except ImportError:
            return "/dev/video0"
        best_device = ""
        best_pixels = 0
        for path in sorted(Path("/dev").glob("video*")):
            capture = cv2.VideoCapture(str(path), cv2.CAP_V4L2)
            if not capture.isOpened():
                capture.release()
                continue
            ok, frame = capture.read()
            capture.release()
            if not ok or frame is None:
                continue
            pixels = int(frame.shape[1]) * int(frame.shape[0])
            if pixels > best_pixels:
                best_pixels = pixels
                best_device = str(path)
        if not best_device:
            raise CameraError(
                "camera.no_device",
                "机器上没有一个能读出画面的 /dev/video* 设备。",
            )
        return best_device


def default_grabber_factory() -> Any:
    """默认取帧器：配置指定的设备优先，否则自动挑一个分辨率最高的。"""
    device = os.getenv("ROBOT_GUI_DEPTH_CAMERA_DEVICE", "").strip()
    if device:
        return V4l2Grabber(device)
    return V4l2Grabber(V4l2Grabber.pick())


class DepthCamera:
    """后台取帧 + 缓存最新一帧。

    取帧是**尽力而为**的：任何一次失败都只记下原因并继续重试，不会把整个
    后端带崩；页面通过 `state()` 看到当前到底能不能用。
    """

    def __init__(
        self,
        grabber_factory: Callable[[], Any] | None = None,
        poll_interval_s: float = 0.2,
    ) -> None:
        self._grabber_factory = grabber_factory or default_grabber_factory
        self._poll_interval_s = poll_interval_s
        self._grabber: Any = None
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._error_code = ""
        self._error_message = ""
        self._source = ""
        self._frames = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="robot-gui-depth-camera", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._grabber is not None:
            try:
                self._grabber.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.opt(exception=True).debug("深度相机通道关闭失败。")
            self._grabber = None

    # ---- 读状态 ----

    def latest_frame(self) -> bytes | None:
        with self._lock:
            return self._frame

    def state(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._frame_at is None else time.monotonic() - self._frame_at
            return {
                "available": self._frame is not None,
                "error_code": self._error_code,
                "error_message": self._error_message,
                "source": self._source,
                "frames": self._frames,
                "frame_age_s": round(age, 3) if age is not None else None,
            }

    # ---- 内部 ----

    def _run(self) -> None:
        try:
            self._grabber = self._grabber_factory()
            self._grabber.open()
            self._set_source()
        except Exception as error:  # noqa: BLE001 - 起不来就记原因，等下一轮
            self._record_error(
                "camera.unavailable",
                f"深度相机通道不可用：{error}",
            )
            self._grabber = None

        while not self._stop.is_set():
            if self._grabber is None:
                # 通道没起来，隔一会儿再试一次。
                if self._stop.wait(2.0):
                    return
                try:
                    self._grabber = self._grabber_factory()
                    self._grabber.open()
                    self._set_source()
                    self._clear_error()
                except Exception as error:  # noqa: BLE001 - 继续等
                    self._record_error(
                        "camera.unavailable",
                        f"深度相机通道不可用：{error}",
                    )
                    self._grabber = None
                    continue
            try:
                frame = self._grabber.grab()
            except CameraError as error:
                self._record_error(error.code, str(error))
                self._stop.wait(self._poll_interval_s)
                continue
            except Exception as error:  # noqa: BLE001 - 未知异常也要收敛
                self._record_error("camera.grab_failed", f"取帧异常：{error}")
                self._stop.wait(self._poll_interval_s)
                continue
            with self._lock:
                self._frame = frame
                self._frame_at = time.monotonic()
                self._frames += 1
                self._error_code = ""
                self._error_message = ""
            self._stop.wait(self._poll_interval_s)

    def _record_error(self, code: str, message: str) -> None:
        with self._lock:
            if self._error_code == code and self._error_message == message:
                return
            self._error_code = code
            self._error_message = message
        logger.warning("深度相机取帧失败（{}）：{}", code, message)

    def _clear_error(self) -> None:
        with self._lock:
            self._error_code = ""
            self._error_message = ""

    def _set_source(self) -> None:
        """记下画面来自哪儿（设备号和分辨率），页面要显示。"""
        describe = getattr(self._grabber, "describe", None)
        if describe is None:
            return
        with self._lock:
            self._source = str(describe())
