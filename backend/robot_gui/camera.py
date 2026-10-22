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


def _frame_channels(frame: Any) -> int:
    """帧的通道数：1=灰度，2=YUYV（有色），3=BGR。"""
    return 1 if frame.ndim == 2 else int(frame.shape[2])


def _to_bgr(frame: Any) -> Any:
    """把原始帧统一成 BGR，JPEG 编码才认。

    关掉 OpenCV 自动转色之后，YUYV 设备给的是两通道原始缓冲，得自己转。
    """
    import cv2  # noqa: PLC0415 - 按需导入

    channels = _frame_channels(frame)
    if channels == 2:
        return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_YUY2)
    if channels == 1:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame


class V4l2Grabber:
    """从 V4L2 设备取帧（G1 头部的相机在机器上就是 /dev/video*）。

    2026-10-22 真机实测（`tools/probe_depth_camera.py`）：**videohub RPC 不通**
    （返回 3102），`pyrealsense2` / `pyorbbec` 都没装，能用的是 V4L2。
    相机是 **Intel RealSense D435**，彩色流在 V4L2 下是 **YUYV422**。

    **第一版抓到的是灰度画面，根因不是挑错了节点，而是解码方式**：OpenCV 默认的
    FFmpeg 后端解不了 yuyv422，会把它当成 8UC1（单通道灰度）。所以要拿到彩色
    必须三件事一起做：

    1. 用 `CAP_V4L2` 打开（别用默认后端）；
    2. `CAP_PROP_CONVERT_RGB=0` 拿**原始**两通道 YUYV 缓冲（开着转色的话
       FFmpeg 那一路已经把它压成灰度了）；
    3. 自己 `cvtColor(..., COLOR_YUV2BGR_YUY2)` 转成 BGR 再编码 JPEG。

    挑设备按**通道数**判断有没有颜色（1=灰度，2=YUYV 有色，3=BGR 有色）：
    优先选有颜色的，其次分辨率最高。这台机器上选出来的是 `/dev/video4`
    （640×480，YUYV422），就是头部相机的彩色流。
    """

    def __init__(self, device: str) -> None:
        self._device = device
        self._capture: Any = None
        self._width = 0
        self._height = 0
        self._channels = 0

    @property
    def device(self) -> str:
        return self._device

    @property
    def size(self) -> tuple[int, int]:
        return self._width, self._height

    @property
    def color(self) -> bool:
        """这条流有没有颜色（红外/深度流是灰度的）。"""
        return self._channels >= 2

    def describe(self) -> str:
        kind = "彩色" if self.color else "灰度"
        return f"{self._device}（{self._width}×{self._height}·{kind}）"

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
        # 关掉自动转色：这样拿到的通道数才说明这条流本身是不是彩色的。
        capture.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.release()
            raise CameraError("camera.grab_failed", f"{self._device} 打开后读不出帧。")
        self._capture = capture
        self._channels = _frame_channels(frame)
        self._width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or int(frame.shape[1])
        self._height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or int(
            frame.shape[0]
        )

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
        encoded, buffer = cv2.imencode(".jpg", _to_bgr(frame))
        if not encoded:
            raise CameraError("camera.encode_failed", "这一帧编码成 JPEG 失败。")
        return bytes(buffer)

    @staticmethod
    def pick() -> str:
        """挑一个能用的设备：**优先有颜色的**，其次分辨率最高的。"""
        try:
            import cv2  # noqa: PLC0415 - 按需导入
        except ImportError:
            return "/dev/video0"
        best_device = ""
        best_pixels = 0
        best_color = False
        for path in sorted(Path("/dev").glob("video*")):
            capture = cv2.VideoCapture(str(path), cv2.CAP_V4L2)
            if not capture.isOpened():
                capture.release()
                continue
            capture.set(cv2.CAP_PROP_CONVERT_RGB, 0)
            ok, frame = capture.read()
            capture.release()
            if not ok or frame is None:
                continue
            color = _frame_channels(frame) >= 2
            pixels = int(frame.shape[1]) * int(frame.shape[0])
            if color and not best_color:
                best_device, best_pixels, best_color = str(path), pixels, True
            elif color == best_color and pixels > best_pixels:
                best_device, best_pixels = str(path), pixels
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
        self._color = True
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
                "color": self._color,
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
        """记下画面来自哪儿（设备号、分辨率、有没有颜色），页面要显示。"""
        describe = getattr(self._grabber, "describe", None)
        if describe is None:
            return
        with self._lock:
            self._source = str(describe())
            self._color = bool(getattr(self._grabber, "color", True))
