"""G1 扬声器 PCM 播放。

robot-gui 部署在机器人本机，直接用 SDK2 的 AudioClient 推流，不需要 SSH 转发
（robocourse 的 g1_audio_worker.py 是给远端控制端用的桥）。

协议：PlayStream(app_name, stream_id, chunk) 分块推流，PlayStop(app_name) 收尾。
PCM 必须是 16 kHz / 单声道 / 16 bit，每块 5120 字节正好 160 毫秒。

一个必须记住的坑：SDK2 的 PlayStop 不校验 RPC 返回码就返回 0，所以主动中断之后
无法确认音频流是否真的停了。这里用 _recovery_unconfirmed 闩锁记住这件事，一旦发生
就不再播放新音频，直到进程重启或运维确认。
"""
from __future__ import annotations

import threading
from time import monotonic
from typing import Any, Protocol

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
CHUNK_BYTES = 5120  # 160 毫秒


class G1AudioClient(Protocol):
    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes) -> Any: ...

    def PlayStop(self, app_name: str) -> Any: ...


class AudioError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def validate_pcm(pcm: bytes) -> None:
    if not pcm:
        raise AudioError("audio.empty", "音频数据为空。")
    if len(pcm) % 2:
        raise AudioError("audio.invalid_pcm_length", "PCM 长度必须是偶数（16 位采样）。")


class G1AudioPort:
    """把 PCM 推送到 G1 扬声器。"""

    def __init__(self, client: G1AudioClient, app_name: str) -> None:
        if not app_name:
            raise ValueError("必须指定 app_name")
        self._client = client
        self._app_name = app_name
        self._lock = threading.Lock()
        self._active_stream_id = ""
        self._recovery_unconfirmed = False
        self._next_stream_id = 1

    @property
    def recovery_unconfirmed(self) -> bool:
        return self._recovery_unconfirmed

    @property
    def playing(self) -> bool:
        return bool(self._active_stream_id)

    def play(self, pcm: bytes, stop: threading.Event) -> str:
        """推流播放；返回空串表示完成，否则返回稳定错误码。"""
        try:
            validate_pcm(pcm)
        except AudioError as error:
            return error.code
        with self._lock:
            if self._recovery_unconfirmed:
                return "audio.stop_unconfirmed"
            if self._active_stream_id:
                return "audio.already_playing"
            stream_id = f"{self._app_name}-{self._next_stream_id}"
            self._next_stream_id += 1
            self._active_stream_id = stream_id
        started = monotonic()
        try:
            for offset in range(0, len(pcm), CHUNK_BYTES):
                if stop.is_set():
                    self.stop()
                    return "audio.cancelled"
                reply = self._client.PlayStream(
                    self._app_name, stream_id, pcm[offset : offset + CHUNK_BYTES]
                )
                code = reply[0] if isinstance(reply, tuple) else reply
                if code != 0:
                    self.stop()
                    return "audio.stream_rejected"
                target = min((offset + CHUNK_BYTES) / BYTES_PER_SECOND, len(pcm) / BYTES_PER_SECOND)
                if stop.wait(max(0.0, started + target - monotonic())):
                    self.stop()
                    return "audio.cancelled"
            return self._finish_stream()
        except Exception:
            self.stop()
            return "audio.sdk_exception"

    def stop(self) -> str:
        """停止播放；返回空串表示已确认停止，否则返回稳定错误码。"""
        with self._lock:
            if self._recovery_unconfirmed:
                return "audio.stop_unconfirmed"
            if not self._active_stream_id:
                return ""
            try:
                result = self._client.PlayStop(self._app_name)
            except Exception:
                self._recovery_unconfirmed = True
                return "audio.stop_exception"
            if result != 0:
                self._recovery_unconfirmed = True
                return "audio.stop_rejected"
            # 该 SDK 实现不校验 RPC 返回码就返回 0，因此不能确认流已安全停止。
            self._active_stream_id = ""
            self._recovery_unconfirmed = True
            return "audio.stop_unconfirmed"

    def _finish_stream(self) -> str:
        with self._lock:
            try:
                result = self._client.PlayStop(self._app_name)
            except Exception:
                return "audio.stop_exception"
            if result != 0:
                return "audio.stop_rejected"
            self._active_stream_id = ""
            return ""
