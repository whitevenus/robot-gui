"""只播讲解语音的会话。

机器人在**回放**时会接管控制权；只播语音时不会，两者不能混为一谈。但页面
上只该有一套「暂停 / 继续 / 停止」，所以语音会话独立成一个类，状态并进
`/api/v1/control/state`，由后端把暂停/继续/停止分派给当前真正在跑的那一个。

暂停的语义和回放一致：**停在这里，接着播从下一段开始**。区别是回放会先把
机器人动态带回接管姿态再交权，只播语音不用动机器人，停在哪就是哪。

音频走课程包原声的 PCM（和回放时同一条推流路径），不做 TTS 兜底。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from loguru import logger

from .audio import G1AudioPort, pcm_duration_s

#: 语音会话的阶段：idle / playing / paused。
IDLE = "idle"
PLAYING = "playing"
PAUSED = "paused"


class NarrationError(RuntimeError):
    """语音会话操作失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NarrationSession:
    """逐段播讲解语音的会话。"""

    def __init__(self, audio_port: G1AudioPort | None) -> None:
        self._audio = audio_port
        self._lock = threading.Lock()
        self._schedule: tuple[tuple[str, float], ...] = ()
        self._clips: dict[str, bytes] = {}
        self._index = 0
        self._phase = IDLE
        self._current = ""
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 状态 ----

    def state(self) -> dict[str, Any]:
        with self._lock:
            names = [name for name, _ in self._schedule]
            index = self._index
            return {
                "phase": self._phase,
                "current": self._current,
                "index": index,
                "total": len(names),
                "remaining": names[index:],
            }

    @property
    def active(self) -> bool:
        with self._lock:
            return self._phase in (PLAYING, PAUSED)

    # ---- 控制 ----

    def start(
        self, schedule: Sequence[tuple[str, float]], clips: dict[str, bytes]
    ) -> dict[str, Any]:
        """开始播。schedule 是 (动作名, 该段在时间线上的起始秒数)。"""
        if self._audio is None:
            raise NarrationError("audio_disabled", "语音推流未开启。")
        with self._lock:
            if self._phase == PLAYING:
                raise NarrationError("narration_busy", "已经在播语音了。")
            self._schedule = tuple(schedule)
            self._clips = dict(clips)
            self._index = 0
            self._current = ""
            self._phase = PLAYING
            self._cancel = threading.Event()
        self._spawn()
        return self.state()

    def pause(self) -> dict[str, Any]:
        """停在这一段的边界：正在播的那段不重播，接着从下一段继续。"""
        with self._lock:
            if self._phase != PLAYING:
                raise NarrationError("not_playing", "当前没有在播语音。")
            self._phase = PAUSED
            self._index = min(self._index + 1, len(self._schedule))
        if self._audio is not None:
            self._audio.stop()
        return self.state()

    def resume(self) -> dict[str, Any]:
        """从下一段接着播。"""
        with self._lock:
            if self._phase != PAUSED:
                raise NarrationError("not_paused", "当前没有可继续的语音。")
            self._phase = PLAYING
            self._cancel = threading.Event()
        self._spawn()
        return self.state()

    def stop(self) -> dict[str, Any]:
        """立刻停掉，不再续播。"""
        with self._lock:
            self._phase = IDLE
            self._current = ""
            self._cancel.set()
        if self._audio is not None:
            self._audio.stop()
        return self.state()

    # ---- 内部 ----

    def _spawn(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="robot-gui-narration", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._phase != PLAYING:
                    return
                index = self._index
                schedule = self._schedule
            if index >= len(schedule):
                break
            name, starts_at = schedule[index]
            pcm = self._clips.get(name)
            with self._lock:
                self._current = name
            if pcm is not None and self._audio is not None:
                error = self._audio.play([(0.0, pcm)], self._cancel)
                with self._lock:
                    interrupted = self._phase != PLAYING
                if error and not interrupted:
                    logger.warning("讲解语音推流失败（{}）：{}", error, name)
            # 段与段之间按时间线补静音：下一段的起始时刻减去这一段实际播完的时刻。
            with self._lock:
                if self._phase != PLAYING:
                    return
                self._index = index + 1
                next_start = (
                    schedule[index + 1][1] if index + 1 < len(schedule) else None
                )
            if next_start is not None and pcm is not None:
                gap = next_start - starts_at - pcm_duration_s(pcm)
                if gap > 0:
                    if self._cancel.wait(gap):
                        return
        with self._lock:
            if self._phase == PLAYING:
                self._phase = IDLE
                self._current = ""
        logger.info("讲解语音播放完成。")
