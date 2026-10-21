"""G1 扬声器 PCM 播放。

robot-gui 部署在机器人本机，直接用 SDK2 的 AudioClient 推流，不需要 SSH 转发
（robocourse 的 g1_audio_worker.py 是给远端控制端用的桥）。

**只有 PCM 一条路**：不做官方 TTS 兜底。兜底播的是云端合成的另一套音色，和课程包
对不上；讲不出原声就不出声，比出个错的音色好。

整段一次推完（2026-10-21 真机实测）：分块只会把"第 N 块被拒 → 只播开头几个字"
的故障面放大，官方 C++ 例程也是一次推整个 wav。

三个真机踩过的坑：

1. **推流必须放在独立进程里。** 后端进程同时跑着控制回路（50 Hz 发
   `rt/arm_sdk`）、遥测订阅（`rt/lowstate` 实测 1050 Hz 的回调）和音频的 RPC，
   三者共用 SDK2 的**同一个 DDS participant**。回放期间，后端进程内的
   `PlayStream` **收不到 RPC 回包**，等满 10 秒超时（3104 =
   `RPC_ERR_CLIENT_API_TIMEOUT`），**而服务端其实已经收到并播了音频**——表现是
   "声音正常但后端报失败"，还白等 20 秒（推流 10 秒 + `PlayStop` 10 秒）。
   同一时刻、同样的调用放到独立进程里推，回包稳定 0.05 秒；回放期间连测 29 次
   全部正常。所以语音一律走 `robot_gui.audio_worker` 子进程：它有自己的 DDS
   participant，不跟控制回路抢通道。
2. **失败不能重推。** 官方《音频灯光服务接口》写明「stream_id 相同为缓存续播，
   不同则打断播放」：用同一个 stream_id 重推，机器人端会把已播内容**再播一遍**
   （真机实测：开头几个字之后，同一句重复了一遍）。换新 stream_id 则会打断当前
   播放、丢掉已播内容。两种都比断在半截更糟，所以如实报错，把选择权交回调用方。
3. **stream_id 必须全局唯一。** 用时间戳，不用进程内自增计数。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import Protocol

from loguru import logger

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
#: 子进程比音频本身多给的余量：进程起停 + DDS 初始化（约 0.7 秒）+ 收尾的 PlayStop。
WORKER_MARGIN_S = 15.0


class AudioPusher(Protocol):
    """把整段 PCM 推给扬声器。返回空串表示成功，否则返回稳定错误码。"""

    def push(self, stream_id: str, pcm: bytes, stop: threading.Event) -> str: ...

    def stop(self) -> None: ...


class WorkerAudioPusher:
    """在独立子进程里推流（为什么必须独立进程见模块说明）。"""

    def __init__(self, app_name: str, python_executable: str | None = None) -> None:
        self._app_name = app_name
        self._python = python_executable or sys.executable
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def build_command(self, stream_id: str) -> list[str]:
        return [
            self._python,
            "-m",
            "robot_gui.audio_worker",
            "--app-name",
            self._app_name,
            "--stream-id",
            stream_id,
        ]

    def push(self, stream_id: str, pcm: bytes, stop: threading.Event) -> str:
        try:
            process = subprocess.Popen(  # noqa: S603 - 命令是固定拼装的，没有外部输入
                self.build_command(stream_id),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            logger.warning("拉起语音推流子进程失败：{}", error)
            return "audio.worker_spawn_failed"
        with self._lock:
            self._process = process
        # PCM 从 stdin 灌进去：不在命令行里传（太长），也不落临时文件。
        threading.Thread(target=_feed, args=(process, pcm), daemon=True).start()

        deadline = time.monotonic() + pcm_duration_s(pcm) + WORKER_MARGIN_S
        while process.poll() is None:
            if stop.is_set():
                process.terminate()
                _reap(process)
                return "audio.cancelled"
            if time.monotonic() > deadline:
                process.kill()
                _reap(process)
                return "audio.worker_timeout"
            time.sleep(0.05)
        with self._lock:
            self._process = None
        return _read_result(process)

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            _reap(process)


class AudioError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def build_timeline(clips: Sequence[tuple[float, bytes]]) -> bytes:
    """把若干段 PCM 按各自的起始时刻拼成**一条**连续的 PCM。

    `clips` 是 (相对起点的秒数, PCM) 列表。段与段之间补静音，这样每段讲解都
    落在动作自己的时刻上——动作之间还插着衔接段，按序号硬拼会越走越偏。

    拼成一条而不是分段推，是因为官方接口「stream_id 不同则打断播放」：
    分段推的话，后一段会把还没播完的前一段掐断。
    """
    out = bytearray()
    cursor = 0.0
    for offset, pcm in sorted(clips, key=lambda item: item[0]):
        if offset < cursor - 1e-6:
            # 上一段还没结束这一段就该开始了：语音会整体往后拖，如实记一笔。
            logger.warning(
                "语音片段 {} 秒处与上一段重叠 {:.2f} 秒，后续讲解会整体延后。",
                offset,
                cursor - offset,
            )
        gap = int(round((offset - cursor) * BYTES_PER_SECOND))
        if gap > 0:
            out.extend(b"\x00" * gap)
        out.extend(pcm)
        cursor = offset + len(pcm) / BYTES_PER_SECOND
    return bytes(out)


def validate_pcm(pcm: bytes) -> None:
    if not pcm:
        raise AudioError("audio.empty", "音频数据为空。")
    if len(pcm) % 2:
        raise AudioError(
            "audio.invalid_pcm_length", "PCM 长度必须是偶数（16 位采样）。"
        )


class G1AudioPort:
    """把 PCM 推送到 G1 扬声器。"""

    def __init__(self, app_name: str, pusher: AudioPusher | None = None) -> None:
        if not app_name:
            raise ValueError("必须指定 app_name")
        self._app_name = app_name
        self._pusher: AudioPusher = (
            pusher if pusher is not None else WorkerAudioPusher(app_name)
        )
        self._lock = threading.Lock()
        self._playing = False
        self._sequence = 0

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self, pcm: bytes, stop: threading.Event) -> str:
        """整段推流播放；返回空串表示完成，否则返回稳定错误码。"""
        try:
            validate_pcm(pcm)
        except AudioError as error:
            return error.code
        with self._lock:
            if self._playing:
                return "audio.already_playing"
            # 全局唯一的流 ID：时间戳保证跨进程不撞上机器人端残留的旧流，
            # 序号保证同一进程内连续两次播放也不撞（毫秒级时间戳会撞）。
            self._sequence += 1
            stream_id = f"{self._app_name}-{time.time_ns()}-{self._sequence}"
            self._playing = True
        try:
            error = self._pusher.push(stream_id, pcm, stop)
        except Exception:  # noqa: BLE001 - 推流异常统一收敛成错误码
            logger.exception("语音推流异常。")
            error = "audio.sdk_exception"
        finally:
            with self._lock:
                self._playing = False
        if error:
            logger.warning("语音推流失败（{}）", error)
        return error

    def stop(self) -> str:
        """主动停止播放。返回空串表示已发出停止请求。"""
        self._pusher.stop()
        with self._lock:
            self._playing = False
        return ""


def _feed(process: subprocess.Popen[bytes], pcm: bytes) -> None:
    """把 PCM 写进子进程的 stdin。子进程可能已经退出，写入失败不算错误。"""
    try:
        if process.stdin is not None:
            process.stdin.write(pcm)
            process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def _reap(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _read_result(process: subprocess.Popen[bytes]) -> str:
    """读子进程的结果。退出码 0 = 服务端已接受；非 0 = 失败。"""
    stdout = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.returncode == 0:
        return ""
    logger.warning(
        "语音推流子进程失败（退出码 {}）：{} {}",
        process.returncode,
        stdout.strip(),
        stderr.strip(),
    )
    return "audio.stream_rejected"
