"""动作库：本地数据目录里的 group+frames 动作文件。

动作格式与 g1-action-studio 一致：{"group": ..., "frames": [{"t": ..., "q": [...]}]}，
q 的顺序按 group 决定（arms = 左臂 7 + 右臂 7）。
ECHO-G 转出的上肢轨迹就是这个格式，可以直接导入再回放到真机。

数据目录由 ROBOT_GUI_DATA_DIR 指定，未配置时写操作明确失败，不静默回退到安装目录。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from g1_action_studio.motion import Action, load_action

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SOURCE_EXTRA = "robot_gui"
AUDIO_DIR_NAME = "audio"
COURSE_FILE_NAME = "course.json"


class ActionStoreError(RuntimeError):
    """动作库操作失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Course:
    """一整套课程：按讲解顺序排好的动作清单。"""

    name: str
    actions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "actions": list(self.actions)}


@dataclass(frozen=True)
class ActionSummary:
    name: str
    group: str
    frames: int
    duration_s: float
    source: str
    has_audio: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "frames": self.frames,
            "duration_s": self.duration_s,
            "source": self.source,
            "has_audio": self.has_audio,
        }


class ActionStore:
    """把动作以 JSON 文件存放在配置的数据目录里。"""

    def __init__(self, data_dir: Path | None) -> None:
        self._data_dir = data_dir

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    def _require_dir(self) -> Path:
        if self._data_dir is None:
            raise ActionStoreError(
                "action_store_not_configured",
                "未配置 ROBOT_GUI_DATA_DIR，无法保存动作。",
            )
        return self._data_dir

    def _path(self, name: str) -> Path:
        if not SAFE_NAME.fullmatch(name):
            raise ActionStoreError("action_name_invalid", f"动作名不合法：{name!r}")
        return self._require_dir() / f"{name}.json"

    def list(self) -> list[ActionSummary]:
        directory = self._require_dir()
        if not directory.is_dir():
            return []
        summaries: list[ActionSummary] = []
        for path in sorted(directory.glob("*.json")):
            if path.name == COURSE_FILE_NAME:
                continue
            try:
                summaries.append(self._summarize(path))
            except (ActionStoreError, ValueError, json.JSONDecodeError):
                # 单个坏文件不应让整个列表不可用，但也不能被当成有效动作。
                continue
        return summaries

    def load_course(self) -> Course:
        """读取课程清单 `course.json`，拿到"一整套"的顺序。

        顺序由清单给出，不靠文件名猜：ECHO-G 的课程包是按讲解顺序生成动作的，
        而动作名里的 p1/p2 只是生成批次，不承载播放顺序。
        """
        path = self._require_dir() / COURSE_FILE_NAME
        if not path.is_file():
            raise ActionStoreError(
                "course_not_found",
                f"没有课程清单 {path}：整套播放需要它给出动作顺序。",
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ActionStoreError(
                "course_invalid", f"课程清单读不出来：{error}"
            ) from error
        if not isinstance(raw, dict):
            raise ActionStoreError("course_invalid", "课程清单必须是 JSON 对象。")
        actions = raw.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ActionStoreError(
                "course_invalid", "课程清单的 actions 必须是非空的动作名列表。"
            )
        for name in actions:
            if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
                raise ActionStoreError(
                    "course_invalid", f"课程清单里的动作名不合法：{name!r}"
                )
        name = raw.get("name")
        return Course(
            name=name if isinstance(name, str) and name else path.stem,
            actions=tuple(actions),
        )

    def _summarize(self, path: Path) -> ActionSummary:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("动作文件必须是 JSON 对象")
        group = raw.get("group")
        frames = raw.get("frames")
        if (
            not isinstance(group, str)
            or not isinstance(frames, list)
            or len(frames) < 2
        ):
            raise ValueError("动作文件缺少 group 或 frames")
        times = [frame.get("t") for frame in frames if isinstance(frame, dict)]
        if len(times) != len(frames) or not all(
            isinstance(value, (int, float)) for value in times
        ):
            raise ValueError("动作帧缺少时间轴")
        source = raw.get("source")
        return ActionSummary(
            name=path.stem,
            group=group,
            frames=len(frames),
            duration_s=round(float(times[-1] - times[0]), 3),
            source=source if isinstance(source, str) else SOURCE_EXTRA,
            has_audio=self.has_audio(path.stem),
        )

    def has_audio(self, name: str) -> bool:
        """动作库里是否配有同名语音（<name>.pcm，16 kHz 单声道 16 bit）。"""
        if self._data_dir is None:
            return False
        return (self._data_dir / AUDIO_DIR_NAME / f"{name}.pcm").is_file()

    def read_audio(self, name: str) -> bytes | None:
        """读同名语音；没有就返回 None（安静地不播，而不是让回放失败）。"""
        if self._data_dir is None:
            return None
        path = self._data_dir / AUDIO_DIR_NAME / f"{name}.pcm"
        if not path.is_file():
            return None
        return path.read_bytes()

    def _validate(self, name: str, payload: bytes) -> Action:
        """用 g1-action-studio 的加载器做权威校验；它要求文件路径，这里走临时文件。"""
        directory = self._require_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f".validate_{name}.json"
        temp.write_bytes(payload)
        try:
            return load_action(temp)
        finally:
            temp.unlink(missing_ok=True)

    def import_action(self, name: str, payload: bytes) -> ActionSummary:
        """校验并保存一个动作；同名已存在时要求 overwrite。"""
        if not payload:
            raise ActionStoreError("action_empty", "动作文件为空。")
        target = self._path(name)
        if target.exists():
            raise ActionStoreError("action_exists", f"动作 {name} 已存在。")
        try:
            action = self._validate(name, payload)
        except ValueError as error:
            raise ActionStoreError("action_invalid", str(error)) from error
        target.write_bytes(payload)
        return ActionSummary(
            name=name,
            group=action.group,
            frames=len(action.t),
            duration_s=round(float(action.t[-1] - action.t[0]), 3),
            source=SOURCE_EXTRA,
            has_audio=self.has_audio(name),
        )

    def export_action(self, name: str) -> bytes:
        path = self._path(name)
        if not path.is_file():
            raise ActionStoreError("action_not_found", f"动作 {name} 不存在。")
        return path.read_bytes()

    def load(self, name: str) -> Action:
        path = self._path(name)
        if not path.is_file():
            raise ActionStoreError("action_not_found", f"动作 {name} 不存在。")
        try:
            return load_action(path)
        except ValueError as error:
            raise ActionStoreError("action_invalid", str(error)) from error

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.is_file():
            raise ActionStoreError("action_not_found", f"动作 {name} 不存在。")
        path.unlink()
