"""G1 深度相机取流。

G1 头部的相机不通过遥测话题过来，官方给的是**取一帧图片**的 RPC：videohub
服务的 `GetImageSample`（Go2 / B2 的相机例程用的就是它）。这里在后台线程里
按固定间隔取帧，把最新的 JPEG 缓存起来，供网页端做 MJPEG 推流。

**取不到就如实说取不到**：机器上服务没起、没权限、或者这台机器没装相机，
`state()` 里会写明原因，页面照实显示，不用别的画面凑数。

取流通道的取舍：**读** RPC 留在本进程（和遥测的 FSM 回读一样，能拿到回包）；
之前音频推流之所以要挪到子进程，是因为推流时控制回路把 DDS 通道占满了，
那是写方向的问题，跟这里不一样。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

#: 取一帧的实现：成功返回 JPEG 字节，失败抛 CameraError。
FrameGrabber = Callable[[], bytes]


class CameraError(RuntimeError):
    """取帧失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VideoHubGrabber:
    """SDK2 videohub RPC 取帧。

    `VideoClient` 走的是 SDK2 的通用 RPC 通道（服务名 `videohub`，接口 1001
    `GetImageSample`），返回的就是一帧 JPEG。
    """

    def __init__(self, timeout_s: float = 2.0) -> None:
        self._timeout_s = timeout_s
        self._client: Any = None

    def open(self) -> None:
        from unitree_sdk2py.go2.video.video_client import VideoClient

        client = VideoClient()
        client.SetTimeout(self._timeout_s)
        client.Init()
        self._client = client

    def close(self) -> None:
        self._client = None

    def grab(self) -> bytes:
        if self._client is None:
            raise CameraError("camera.client_missing", "videohub 通道没有建立。")
        code, data = self._client.GetImageSample()
        if code != 0:
            raise CameraError(
                "camera.grab_failed", f"GetImageSample 返回错误码 {code}。"
            )
        if not data:
            raise CameraError("camera.empty_frame", "取到的这一帧是空的。")
        return bytes(data)


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
        self._grabber_factory = grabber_factory or VideoHubGrabber
        self._poll_interval_s = poll_interval_s
        self._grabber: Any = None
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._error_code = ""
        self._error_message = ""
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
                "frames": self._frames,
                "frame_age_s": round(age, 3) if age is not None else None,
            }

    # ---- 内部 ----

    def _run(self) -> None:
        try:
            self._grabber = self._grabber_factory()
            self._grabber.open()
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
