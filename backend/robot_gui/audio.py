"""G1 扬声器 PCM 播放。

robot-gui 部署在机器人本机，直接用 SDK2 的 AudioClient 推流，不需要 SSH 转发
（robocourse 的 g1_audio_worker.py 是给远端控制端用的桥）。

协议：PlayStream(app_name, stream_id, chunk) 分块推流，PlayStop(app_name) 收尾。
PCM 必须是 16 kHz / 单声道 / 16 bit。

**只有 PCM 一条路**：不做官方 TTS 兜底。兜底播的是云端合成的另一套音色，和课程包
对不上；讲不出原声就不出声，比出个错的音色好。

三个真机踩过的坑（2026-10-21）：

1. **等待必须从"推送完成"起算，不能从整段开始算。** 官方 `play_pcm_stream` 是
   `PlayStream` 之后 `sleep(该块时长)`；早期版本按"整段播放位置"等待，把第一块
   的 RPC 往返吃掉了，后面每一块都提前到达，机器人端会拒。
2. **失败收尾不能置闩锁。** 闩锁原本是"PlayStop 不校验返回码、无法确认流真的停了"
   的保险，但旧实现把它当成了"永久封锁"：任何一次失败（甚至一次用户取消）之后，
   后续所有播放都直接返回 `audio.stop_unconfirmed`，一个字节都不推，机器人上
   彻底没声音。现在闩锁只**记账**，不再封锁——`stream_id` 每次都用新时间戳，
   残留的旧流不会和新流撞上。
3. **stream_id 必须全局唯一。** 用时间戳，不用进程内自增计数。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
#: 官方播放服务（g1_audio_client_play_wav_socket.py）是"攒一批字节一次性 PlayStream
#: 出去"，实测按 1 秒（32000 字节）一批最接近官方行为。太小会频繁打断，太大则
#: 服务端一次要吃下太多数据。
CHUNK_BYTES = 32000
#: 推完最后一批后还要等这么久才 PlayStop：服务端的播放是异步的，立刻停会掐掉尾巴。
TAIL_SINK_S = 1.5
#: 被拒时重试一次的等待时间。机器人端在上一块还没播完时收到新块会拒，
#: 等半秒通常就够了。
RETRY_DELAY_S = 0.5


class G1AudioClient(Protocol):
    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes) -> Any: ...

    def PlayStop(self, app_name: str) -> Any: ...


class AudioClientFactory:
    """按需创建 SDK2 的 AudioClient。

    DDS 通道由后端统一管理，AudioClient 需要在 ChannelFactory 初始化之后使用。
    这里做成懒加载：只有真的要播语音时才建立客户端，避免只读模式下多占一条 DDS 通道。
    """

    def __init__(self) -> None:
        self._client: G1AudioClient | None = None

    def get(self) -> G1AudioClient:
        if self._client is None:
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

            client = AudioClient()
            # 超时给足：PlayStream 要把整块 PCM 传过去，2 秒在 DDS 带宽被占时不够用
            # （官方例程用的是 10 秒）。
            client.SetTimeout(10.0)
            client.Init()
            self._client = client
        return self._client

    def close(self) -> None:
        self._client = None


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
        raise AudioError(
            "audio.invalid_pcm_length", "PCM 长度必须是偶数（16 位采样）。"
        )


class G1AudioPort:
    """把 PCM 推送到 G1 扬声器。"""

    def __init__(
        self,
        client: G1AudioClient | AudioClientFactory,
        app_name: str,
    ) -> None:
        if not app_name:
            raise ValueError("必须指定 app_name")
        self._client_source = client
        self._app_name = app_name
        self._lock = threading.Lock()
        self._active_stream_id = ""
        self._recovery_unconfirmed = False

    @property
    def _client(self) -> G1AudioClient:
        if isinstance(self._client_source, AudioClientFactory):
            return self._client_source.get()
        return self._client_source

    @property
    def recovery_unconfirmed(self) -> bool:
        """上一次停止是否没收到确认。只用于诊断，不再封锁后续播放。"""
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
            if self._active_stream_id:
                return "audio.already_playing"
            # 全局唯一的流 ID：官方用时间戳，进程内自增计数在重启后会撞上
            # 机器人端可能还留着的旧流。
            stream_id = f"{self._app_name}-{int(time.time() * 1000)}"
            self._active_stream_id = stream_id
        try:
            for offset in range(0, len(pcm), CHUNK_BYTES):
                if stop.is_set():
                    self._abort_stream()
                    return "audio.cancelled"
                chunk = pcm[offset : offset + CHUNK_BYTES]
                code = self._push_chunk(stream_id, chunk)
                if code != 0 and not stop.wait(RETRY_DELAY_S):
                    # 机器人端在上一块还没播完时收到新块会拒，等一下再试一次。
                    code = self._push_chunk(stream_id, chunk)
                if code != 0:
                    self._abort_stream()
                    return "audio.stream_rejected"
                # 等这一块播完再推下一块。**从推送完成起算**——按整段播放位置等待会把
                # 第一块的 RPC 往返吃掉，后面每一块都提前到达，机器人端会拒。
                if stop.wait(len(chunk) / BYTES_PER_SECOND):
                    self._abort_stream()
                    return "audio.cancelled"
            # 推完不等于播完：服务端的播放是异步的，立刻 PlayStop 会掐掉尾巴。
            if stop.wait(TAIL_SINK_S):
                self._abort_stream()
                return "audio.cancelled"
            return self._finish_stream()
        except Exception:  # noqa: BLE001 - SDK 推流异常统一收敛成错误码
            self._abort_stream()
            return "audio.sdk_exception"

    def _push_chunk(self, stream_id: str, chunk: bytes) -> int:
        """推一块，返回机器人端的返回码（0 = 接受）。"""
        reply = self._client.PlayStream(self._app_name, stream_id, chunk)
        return reply[0] if isinstance(reply, tuple) else reply

    def stop(self) -> str:
        """主动停止播放。返回空串表示已发出停止请求。"""
        with self._lock:
            if not self._active_stream_id:
                return ""
            try:
                self._client.PlayStop(self._app_name)
            except Exception:  # noqa: BLE001 - 停止失败要收敛成错误码，不能往上抛
                self._recovery_unconfirmed = True
                return "audio.stop_exception"
            # 该 SDK 实现不校验 RPC 返回码就返回 0，因此无法确认流已安全停止。
            # 这里只记账，不封锁后续播放——stream_id 每次都是新时间戳，残留的旧流
            # 不会和新流撞上。
            self._active_stream_id = ""
            self._recovery_unconfirmed = True
            return ""

    def _abort_stream(self) -> None:
        """推流失败或被取消时的收尾：只发停止请求，不记账也不封锁后续播放。"""
        with self._lock:
            if not self._active_stream_id:
                return
            try:
                self._client.PlayStop(self._app_name)
            except Exception:  # noqa: BLE001 - 收尾路径不再抛错，只保证不残留流
                pass
            self._active_stream_id = ""

    def _finish_stream(self) -> str:
        with self._lock:
            try:
                result = self._client.PlayStop(self._app_name)
            except Exception:  # noqa: BLE001 - 停止失败要收敛成错误码，不能往上抛
                return "audio.stop_exception"
            if result != 0:
                return "audio.stop_rejected"
            self._active_stream_id = ""
            return ""
