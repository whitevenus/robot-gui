"""课程库：数据目录里的多门课。

一门课 = 一整套动作的顺序 + 每段的讲解信息。默认课仍然是 `data/course.json`
（机器人上正在播的那门 9 段课），其余课程放在 `data/courses/<id>.json`。

课程与动作库是分开的两层：动作库是共享的一份（按文件名索引），课程只记录
引用与顺序。这样删掉一门课不会动到别的课的动作。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import ActionStore

SAFE_COURSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COURSES_DIR_NAME = "courses"
DEFAULT_COURSE_ID = "default"
DEFAULT_COURSE_FILE_NAME = "course.json"


class CourseStoreError(RuntimeError):
    """课程库操作失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CourseSegment:
    """课程里的一段讲解。"""

    name: str
    text: str = ""
    duration_s: float = 0.0
    frames: int = 0
    has_audio: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "text": self.text,
            "duration_s": self.duration_s,
            "frames": self.frames,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True)
class CourseRecord:
    """一整套课程。"""

    id: str
    name: str
    actions: tuple[str, ...]
    segments: tuple[CourseSegment, ...] = ()
    created_at: str = ""
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return round(sum(segment.duration_s for segment in self.segments), 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "actions": list(self.actions),
            "segments": [segment.as_dict() for segment in self.segments],
            "duration_s": self.duration_s,
            "created_at": self.created_at,
            "source": self.source,
        }


class CourseStore:
    """把课程以 JSON 文件存放在配置的数据目录里。"""

    def __init__(
        self, data_dir: Path | None, actions: ActionStore | None = None
    ) -> None:
        self._data_dir = data_dir
        self._actions = actions

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    def _require_dir(self) -> Path:
        if self._data_dir is None:
            raise CourseStoreError(
                "course_store_not_configured",
                "未配置 ROBOT_GUI_DATA_DIR，无法保存课程。",
            )
        return self._data_dir

    def _courses_dir(self) -> Path:
        return self._require_dir() / COURSES_DIR_NAME

    def list(self) -> list[CourseRecord]:
        """默认课 + `data/courses/*.json`。

        默认课排在最前：它是机器人上正在用的那门课。
        """
        records: list[CourseRecord] = []
        default = self._read_default()
        if default is not None:
            records.append(default)
        directory = self._courses_dir()
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    records.append(self._read_file(path))
                except (CourseStoreError, ValueError, json.JSONDecodeError):
                    # 单个坏文件不应让整个列表不可用，但也不能被当成有效课程。
                    continue
        return records

    def read(self, course_id: str) -> CourseRecord:
        if course_id == DEFAULT_COURSE_ID:
            record = self._read_default()
            if record is None:
                raise CourseStoreError(
                    "course_not_found",
                    f"没有默认课程 {DEFAULT_COURSE_FILE_NAME}。",
                )
            return record
        if not SAFE_COURSE_ID.fullmatch(course_id):
            raise CourseStoreError(
                "course_id_invalid", f"课程 ID 不合法：{course_id!r}"
            )
        path = self._courses_dir() / f"{course_id}.json"
        if not path.is_file():
            raise CourseStoreError("course_not_found", f"课程 {course_id} 不存在。")
        return self._read_file(path)

    def write(self, record: CourseRecord) -> CourseRecord:
        if not SAFE_COURSE_ID.fullmatch(record.id):
            raise CourseStoreError(
                "course_id_invalid", f"课程 ID 不合法：{record.id!r}"
            )
        if record.id == DEFAULT_COURSE_ID:
            raise CourseStoreError(
                "course_id_reserved",
                "默认课由 data/course.json 提供，不能被覆盖。",
            )
        directory = self._courses_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record.id}.json"
        path.write_text(
            json.dumps(record.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return record

    def delete(self, course_id: str) -> None:
        """删课程记录；动作库是共享的，不动。"""
        if course_id == DEFAULT_COURSE_ID:
            raise CourseStoreError("course_protected", "默认课不能删除。")
        if not SAFE_COURSE_ID.fullmatch(course_id):
            raise CourseStoreError(
                "course_id_invalid", f"课程 ID 不合法：{course_id!r}"
            )
        path = self._courses_dir() / f"{course_id}.json"
        if not path.is_file():
            raise CourseStoreError("course_not_found", f"课程 {course_id} 不存在。")
        path.unlink()

    def resolve_actions(self, course_id: str) -> tuple[str, ...]:
        """回放用：拿到这门课的动作顺序。"""
        return self.read(course_id).actions

    # ------------------------------------------------------------------ 内部

    def _read_default(self) -> CourseRecord | None:
        path = self._require_dir() / DEFAULT_COURSE_FILE_NAME
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CourseStoreError(
                "course_invalid", f"默认课程清单读不出来：{error}"
            ) from error
        if not isinstance(raw, dict):
            raise CourseStoreError("course_invalid", "默认课程清单必须是 JSON 对象。")
        actions = raw.get("actions")
        if not isinstance(actions, list) or not actions:
            raise CourseStoreError(
                "course_invalid", "默认课程清单的 actions 必须是非空的动作名列表。"
            )
        for name in actions:
            if not isinstance(name, str) or not name:
                raise CourseStoreError(
                    "course_invalid", "默认课程清单里的动作名不合法。"
                )
        name = raw.get("name")
        return CourseRecord(
            id=DEFAULT_COURSE_ID,
            name=name if isinstance(name, str) and name else path.stem,
            actions=tuple(actions),
            segments=self._segments_for(tuple(actions)),
            source={"kind": "default"},
        )

    def _read_file(self, path: Path) -> CourseRecord:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CourseStoreError(
                "course_invalid", f"课程读不出来：{error}"
            ) from error
        if not isinstance(raw, dict):
            raise CourseStoreError("course_invalid", "课程必须是 JSON 对象。")
        course_id = raw.get("id")
        name = raw.get("name")
        actions = raw.get("actions")
        segments = raw.get("segments")
        if not isinstance(course_id, str) or not SAFE_COURSE_ID.fullmatch(course_id):
            raise CourseStoreError("course_invalid", "课程的 id 不合法。")
        if not isinstance(name, str) or not name:
            raise CourseStoreError("course_invalid", "课程缺少名称。")
        if not isinstance(actions, list) or not actions:
            raise CourseStoreError(
                "course_invalid", "课程的 actions 必须是非空的动作名列表。"
            )
        if not isinstance(segments, list):
            raise CourseStoreError("course_invalid", "课程的 segments 必须是列表。")
        parsed: list[CourseSegment] = []
        for item in segments:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise CourseStoreError("course_invalid", "课程的段落信息不合法。")
            parsed.append(
                CourseSegment(
                    name=item["name"],
                    text=item.get("text") if isinstance(item.get("text"), str) else "",
                    duration_s=float(item.get("duration_s") or 0.0),
                    frames=int(item.get("frames") or 0),
                    has_audio=bool(item.get("has_audio")),
                )
            )
        source = raw.get("source")
        return CourseRecord(
            id=course_id,
            name=name,
            actions=tuple(actions),
            segments=tuple(parsed),
            created_at=raw.get("created_at")
            if isinstance(raw.get("created_at"), str)
            else "",
            source=source if isinstance(source, dict) else {},
        )

    def _segments_for(self, actions: tuple[str, ...]) -> tuple[CourseSegment, ...]:
        """默认课没有逐段讲解信息，段详情从动作库补齐。"""
        summaries = {}
        if self._actions is not None:
            summaries = {item.name: item for item in self._actions.list()}
        return tuple(
            CourseSegment(
                name=name,
                duration_s=summaries[name].duration_s if name in summaries else 0.0,
                frames=summaries[name].frames if name in summaries else 0,
                has_audio=summaries[name].has_audio if name in summaries else False,
            )
            for name in actions
        )
