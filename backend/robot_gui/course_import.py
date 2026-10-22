"""从 courseware-v3 课程包导入一门课。

包结构（robocourse 产出）：

    program.json    {schema_version: "courseware-v3", target_profile, slides[], performances[]}
    manifest.json   {format, course_id, target_profile, program_sha256, files[]}
    assets/<id>.pcm   讲解语音（16 kHz 单声道 16 bit 裸 PCM）
    assets/<id>.json  ECHO-G 生成的动作（{"group": "arms", "frames": [...]}）

导入是事务性的：动作、语音、课程清单要么都写成功，要么都不写。

两个约定：

- **动作名用动作资产的 UUID**。动作库是共享的一份、按文件名索引，名字必须全局
  唯一；用片段 ID 的话，同一门课重新导出（内容变了、资产 UUID 变了）会撞名。
- **语音改名成动作名**。回放时按「同名」找语音（`data/audio/<动作名>.pcm`），
  而包里语音和动作是两个不同的资产 UUID，不改名就找不到讲解。
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .actions import ActionStore, ActionStoreError
from .courses import CourseRecord, CourseSegment, CourseStore

SCHEMA_VERSION = "courseware-v3"
PACKAGE_FORMAT = "robocourse-gateway-package-v1"
PCM_TYPE = "audio/L16;rate=16000;channels=1"
JSON_TYPE = "application/json"
MAX_PACKAGE_BYTES = 512 << 20
MAX_AUDIO_SECONDS = 20.0
PCM_BYTES_PER_SECOND = 16000 * 2
MOTION_GROUP = "arms"
MOTION_DIMENSIONS = 14
ACTION_FPS = 30.0


class CourseImportError(RuntimeError):
    """课程包不合法，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _SegmentPlan:
    """一段讲解要写进动作库的东西。"""

    action_name: str
    action_payload: bytes
    audio_payload: bytes | None
    text: str
    duration_s: float
    frames: int


def import_course(
    package: Path,
    actions: ActionStore,
    courses: CourseStore,
    *,
    name: str | None = None,
) -> CourseRecord:
    """导入一门课；任何一步失败都不留下半截文件。"""
    try:
        archive = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as error:
        raise CourseImportError(
            "package_unreadable", f"课程包打不开：{error}"
        ) from error
    with archive:
        entries = _read_entries(archive)
        program = _read_json(entries, "program.json")
        manifest = _read_json(entries, "manifest.json")
        _validate_manifest(manifest, program)
        # 清单自己记着 program.json 的摘要，对不上说明包被改过或拼错了。
        _verify_digest(
            entries["program.json"], manifest.get("program_sha256"), "program.json"
        )
        plan = _plan(program, manifest, entries)
        for item in plan:
            _validate_motion(item)
            _validate_audio(item)
        record = _write(plan, manifest, actions, courses, name=name)
    return record


# ---------------------------------------------------------------------- 读包


def _read_entries(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """把 ZIP 里的条目读成 {路径: 字节}，同时做总量与路径安全检查。"""
    infos = archive.infolist()
    if len(infos) > 1000:
        raise CourseImportError("package_too_many_files", "课程包条目过多。")
    total = sum(item.file_size for item in infos)
    if total > MAX_PACKAGE_BYTES:
        raise CourseImportError(
            "package_too_large",
            f"课程包解压后 {total} 字节，超过 {MAX_PACKAGE_BYTES} 的上限。",
        )
    entries: dict[str, bytes] = {}
    for item in infos:
        if item.is_dir():
            continue
        name = item.filename
        if name.startswith("/") or ".." in Path(name).parts:
            raise CourseImportError(
                "package_path_unsafe", f"课程包里有不安全的路径：{name}"
            )
        entries[name] = archive.read(name)
    return entries


def _read_json(entries: dict[str, bytes], name: str) -> dict[str, Any]:
    raw = entries.get(name)
    if raw is None:
        raise CourseImportError("package_entry_missing", f"课程包缺少 {name}。")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CourseImportError(
            "package_entry_invalid", f"{name} 不是合法 JSON：{error}"
        ) from error
    if not isinstance(parsed, dict):
        raise CourseImportError("package_entry_invalid", f"{name} 必须是 JSON 对象。")
    return parsed


def _verify_digest(raw: bytes, expected: Any, label: str) -> None:
    """清单里记的 SHA-256 必须与内容对上。"""
    if not isinstance(expected, str) or len(expected) != 64:
        raise CourseImportError("package_digest_invalid", f"{label} 的摘要字段不合法。")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise CourseImportError(
            "package_digest_mismatch", f"{label} 的摘要与内容不符。"
        )


def _validate_manifest(manifest: dict[str, Any], program: dict[str, Any]) -> None:
    if program.get("schema_version") != SCHEMA_VERSION:
        raise CourseImportError(
            "package_schema_unsupported",
            f"课程包版本是 {program.get('schema_version')!r}，只接受 {SCHEMA_VERSION}。",
        )
    if manifest.get("format") != PACKAGE_FORMAT:
        raise CourseImportError(
            "package_format_unsupported",
            f"课程包格式是 {manifest.get('format')!r}，只接受 {PACKAGE_FORMAT}。",
        )
    if (
        not isinstance(program.get("target_profile"), str)
        or not program["target_profile"]
    ):
        raise CourseImportError(
            "package_target_profile_missing", "课程包缺少目标机型。"
        )
    performances = program.get("performances")
    if not isinstance(performances, list) or not performances:
        raise CourseImportError("package_performances_missing", "课程包没有演播编排。")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise CourseImportError("package_files_missing", "课程包清单里没有资源文件。")


# ---------------------------------------------------------------------- 规划


def _plan(
    program: dict[str, Any],
    manifest: dict[str, Any],
    entries: dict[str, bytes],
) -> list[_SegmentPlan]:
    """把包里的演播编排翻译成「要写进动作库的动作与语音」。"""
    files: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise CourseImportError(
                "package_files_invalid", "课程包清单里的资源记录不合法。"
            )
        files[item["id"]] = item

    plans: list[_SegmentPlan] = []
    for performance in program.get("performances", []):
        if not isinstance(performance, dict):
            raise CourseImportError("package_performance_invalid", "演播编排项不合法。")
        for segment in performance.get("segments", []):
            if not isinstance(segment, dict):
                raise CourseImportError("package_segment_invalid", "讲稿片段不合法。")
            label = segment.get("id")
            motion = segment.get("motion")
            if not isinstance(motion, dict):
                raise CourseImportError(
                    "segment_motion_missing", f"片段 {label} 没有动作绑定。"
                )
            action_id = motion.get("asset_id")
            action_file = files.get(action_id) if isinstance(action_id, str) else None
            if action_file is None:
                raise CourseImportError(
                    "motion_asset_missing", f"片段 {label} 的动作资产不在包里。"
                )
            payload = _asset_bytes(entries, action_file, label, "动作")
            frames = _motion_frames(payload, label)

            audio_payload = None
            audio = segment.get("audio")
            if isinstance(audio, dict):
                audio_id = audio.get("asset_id")
                audio_file = files.get(audio_id) if isinstance(audio_id, str) else None
                if audio_file is not None:
                    if audio_file.get("content_type") != PCM_TYPE:
                        raise CourseImportError(
                            "audio_type_invalid",
                            f"片段 {label} 的语音格式是 "
                            f"{audio_file.get('content_type')!r}，只接受 {PCM_TYPE}。",
                        )
                    audio_payload = _asset_bytes(entries, audio_file, label, "语音")

            plans.append(
                _SegmentPlan(
                    action_name=action_id,
                    action_payload=payload,
                    audio_payload=audio_payload,
                    text=segment.get("text")
                    if isinstance(segment.get("text"), str)
                    else "",
                    duration_s=_motion_duration(payload, label),
                    frames=frames,
                )
            )
    if not plans:
        raise CourseImportError(
            "package_segments_missing", "课程包里没有可导入的片段。"
        )
    return plans


def _asset_bytes(
    entries: dict[str, bytes],
    file: dict[str, Any],
    label: Any,
    kind: str,
) -> bytes:
    path = file.get("path")
    if not isinstance(path, str) or path not in entries:
        raise CourseImportError(
            "package_asset_missing", f"片段 {label} 的{kind}文件不在包里：{path!r}"
        )
    payload = entries[path]
    expected = file.get("byte_size")
    if not isinstance(expected, int) or expected != len(payload):
        raise CourseImportError(
            "package_asset_size_mismatch", f"片段 {label} 的{kind}大小与清单不符。"
        )
    _verify_digest(payload, file.get("sha256"), f"片段 {label} 的{kind}")
    return payload


# ---------------------------------------------------------------------- 校验


def _motion_frames(payload: bytes, label: Any) -> int:
    action = _parse_motion(payload, label)
    return len(action["frames"])


def _motion_duration(payload: bytes, label: Any) -> float:
    action = _parse_motion(payload, label)
    times = [frame["t"] for frame in action["frames"]]
    return round(float(times[-1] - times[0]), 3)


def _parse_motion(payload: bytes, label: Any) -> dict[str, Any]:
    """校验动作 JSON 的结构：组、维度、有限数、帧时间递增、首帧从 0 开始。"""
    try:
        action = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CourseImportError(
            "motion_invalid", f"片段 {label} 的动作不是合法 JSON：{error}"
        ) from error
    if not isinstance(action, dict) or action.get("group") != MOTION_GROUP:
        raise CourseImportError(
            "motion_group_invalid",
            f"片段 {label} 的动作组是 {action.get('group')!r}，只接受 {MOTION_GROUP}。",
        )
    frames = action.get("frames")
    if not isinstance(frames, list) or not frames:
        raise CourseImportError("motion_frames_invalid", f"片段 {label} 的动作没有帧。")
    previous = -1.0
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise CourseImportError(
                "motion_frame_invalid", f"片段 {label} 第 {index} 帧不合法。"
            )
        time_value = frame.get("t")
        joint = frame.get("q")
        if (
            not isinstance(time_value, (int, float))
            or isinstance(time_value, bool)
            or not isinstance(joint, list)
            or len(joint) != MOTION_DIMENSIONS
        ):
            raise CourseImportError(
                "motion_frame_invalid",
                f"片段 {label} 第 {index} 帧的时间或 {MOTION_DIMENSIONS} 维关节角不合法。",
            )
        for value in joint:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise CourseImportError(
                    "motion_joint_invalid",
                    f"片段 {label} 第 {index} 帧的关节角不是有限数。",
                )
        if time_value <= previous:
            raise CourseImportError(
                "motion_time_not_increasing",
                f"片段 {label} 第 {index} 帧的时间 {time_value} 没有递增。",
            )
        previous = time_value
    if frames[0].get("t") != 0:
        raise CourseImportError(
            "motion_start_not_zero", f"片段 {label} 的首帧时间不是 0。"
        )
    return action


def _validate_motion(item: _SegmentPlan) -> None:
    """再用 g1-action-studio 的加载器做权威校验——它要求文件路径，这里走临时目录。

    不能用 NamedTemporaryFile：Windows 上它是独占打开的，加载器再打开同一个
    文件会 PermissionError。
    """
    from g1_action_studio.motion import load_action

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{item.action_name}.json"
        path.write_bytes(item.action_payload)
        try:
            load_action(str(path))
        except ValueError as error:
            raise CourseImportError(
                "motion_invalid", f"动作 {item.action_name} 不合格：{error}"
            ) from error


def _validate_audio(item: _SegmentPlan) -> None:
    """语音必须是 16 kHz 单声道 16 bit 裸 PCM，且不超过 ECHO-G 的 20 秒上限。"""
    if item.audio_payload is None:
        return
    if len(item.audio_payload) % 2:
        raise CourseImportError(
            "audio_invalid",
            f"片段 {item.action_name} 的语音长度不是偶数（16 bit 采样）。",
        )
    duration = len(item.audio_payload) / PCM_BYTES_PER_SECOND
    if duration > MAX_AUDIO_SECONDS:
        raise CourseImportError(
            "audio_too_long",
            f"片段 {item.action_name} 的语音 {duration:.2f}s 超过 {MAX_AUDIO_SECONDS} 秒。",
        )


# ---------------------------------------------------------------------- 写入


def _write(
    plan: list[_SegmentPlan],
    manifest: dict[str, Any],
    actions: ActionStore,
    courses: CourseStore,
    *,
    name: str | None,
) -> CourseRecord:
    """把动作、语音、课程清单写下去；失败时把这次写的东西撤掉。"""
    created: list[Path] = []
    try:
        for item in plan:
            if _write_action(actions, item):
                created.append(_action_path(actions, item.action_name))
            if item.audio_payload is not None and _write_audio(actions, item):
                created.append(_audio_path(actions, item.action_name))
        course_id = _course_id(manifest)
        record = CourseRecord(
            id=course_id,
            name=name or course_id,
            actions=tuple(item.action_name for item in plan),
            segments=tuple(
                CourseSegment(
                    name=item.action_name,
                    text=item.text,
                    duration_s=item.duration_s,
                    frames=item.frames,
                    has_audio=item.audio_payload is not None,
                )
                for item in plan
            ),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source={
                "kind": SCHEMA_VERSION,
                "target_profile": manifest.get("target_profile", ""),
                "program_sha256": manifest.get("program_sha256", ""),
            },
        )
        courses.write(record)
        return record
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _course_id(manifest: dict[str, Any]) -> str:
    course_id = manifest.get("course_id")
    if not isinstance(course_id, str) or not course_id:
        raise CourseImportError(
            "package_course_id_missing", "课程包清单缺少 course_id。"
        )
    return course_id


def _action_path(actions: ActionStore, name: str) -> Path:
    directory = actions.data_dir
    if directory is None:
        raise ActionStoreError(
            "action_store_not_configured", "未配置 ROBOT_GUI_DATA_DIR，无法保存动作。"
        )
    return directory / f"{name}.json"


def _audio_path(actions: ActionStore, name: str) -> Path:
    directory = actions.data_dir
    if directory is None:
        raise ActionStoreError(
            "action_store_not_configured", "未配置 ROBOT_GUI_DATA_DIR，无法保存语音。"
        )
    return directory / "audio" / f"{name}.pcm"


def _write_action(actions: ActionStore, item: _SegmentPlan) -> bool:
    """写动作；同名已存在时内容相同就跳过（幂等），不同就拒绝。

    返回是否真的写了新文件——只有这次写的才需要在失败时撤掉。
    """
    try:
        actions.import_action(item.action_name, item.action_payload)
        return True
    except ActionStoreError as error:
        if error.code != "action_exists":
            raise
        existing = actions.export_action(item.action_name)
        if existing != item.action_payload:
            raise CourseImportError(
                "action_conflict",
                f"动作 {item.action_name} 已存在且内容不同，不覆盖。",
            ) from error
        return False


def _write_audio(actions: ActionStore, item: _SegmentPlan) -> bool:
    """写语音，改名成动作名；同名已存在时内容相同就跳过。"""
    if item.audio_payload is None:
        return False
    existing = actions.read_audio(item.action_name)
    if existing is not None:
        if existing != item.audio_payload:
            raise CourseImportError(
                "audio_conflict",
                f"语音 {item.action_name} 已存在且内容不同，不覆盖。",
            )
        return False
    path = _audio_path(actions, item.action_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(item.audio_payload)
    return True
