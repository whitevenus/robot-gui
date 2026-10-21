"""执行记录：每次真机回放落一份 JSON 到项目目录，不上数据库。

记录目录默认在项目内的 `logs/records/`（`logs/` 不入库），可用
`ROBOT_GUI_RECORDS_DIR` 改到别处。文件是纯 JSON：一个文件就是一次执行，
文件名即记录 ID，前端直接列表 / 看详情 / 下载，不用数据库也不用后台任务。

记录里放的是**这次执行自己采到的数据**（腰 14 与两路 IMU 的时间序列、
逐关节跟踪误差、下盘守卫结论），不是事后从别处拼的：采样跟着回放一起跑，
采样窗口由实际轨迹时长决定，所以慢放（speed < 1）时窗口自动变长。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORD_SUFFIX = ".json"
#: 记录 ID 同时是文件名，必须是安全的 ASCII 名字。
SAFE_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
#: 采样周期（秒）。20 Hz 足够看清前倾的发展过程，采样量也压得住。
SAMPLE_PERIOD_S = 0.05

#: 执行结果。
OUTCOME_COMPLETED = "completed"
OUTCOME_FAULT = "fault"
OUTCOME_PAUSED = "paused"
OUTCOME_CANCELLED = "cancelled"


class RecordStoreError(RuntimeError):
    """记录读写失败，code 用于稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", text).strip("-")
    return cleaned or "run"


def new_record_id(started_at: datetime, slug: str) -> str:
    """记录 ID = UTC 时间戳 + 名字。时间戳在前，字典序就是时间序。"""
    return f"{started_at:%Y%m%dT%H%M%SZ}-{_slug(slug)}"


class RecordStore:
    """执行记录的读写：一个文件一条记录，文件名即记录 ID。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, record_id: str) -> Path:
        if not SAFE_ID.fullmatch(record_id):
            raise RecordStoreError(
                "record_id_invalid", f"记录 ID 不合法：{record_id!r}"
            )
        return self._directory / f"{record_id}{RECORD_SUFFIX}"

    def write(self, record: dict[str, Any]) -> Path:
        """写一条记录。先写临时文件再改名，避免读到半截 JSON。"""
        record_id = str(record.get("id") or "")
        path = self._path(record_id)
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(payload, encoding="utf-8")
            temp.replace(path)
        return path

    def list(self) -> list[dict[str, Any]]:
        """列出全部记录的摘要，新的在前。坏文件跳过，不让整个列表不可用。"""
        if not self._directory.is_dir():
            return []
        summaries: list[dict[str, Any]] = []
        for path in sorted(self._directory.glob(f"*{RECORD_SUFFIX}")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                summaries.append(self._summary(record))
        summaries.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return summaries

    def read(self, record_id: str) -> dict[str, Any]:
        path = self._path(record_id)
        if not path.is_file():
            raise RecordStoreError("record_not_found", f"执行记录 {record_id} 不存在。")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RecordStoreError(
                "record_unreadable", f"执行记录 {record_id} 读不出来：{error}"
            ) from error
        if not isinstance(record, dict):
            raise RecordStoreError("record_unreadable", "执行记录必须是 JSON 对象。")
        return record

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        lower = record.get("lower_body") or {}
        return {
            "id": record.get("id"),
            "kind": record.get("kind"),
            "label": record.get("label"),
            "actions": record.get("actions") or [],
            "speed": record.get("speed"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "elapsed_s": record.get("elapsed_s"),
            "trajectory_duration_s": record.get("trajectory_duration_s"),
            "outcome": record.get("outcome"),
            "error_code": record.get("error_code") or "",
            "waist_pitch_lean_deg": lower.get("waist_pitch_lean_deg"),
            "max_drift_deg": lower.get("max_drift_deg"),
            "violated": bool(lower.get("violated")),
            "sample_count": len(record.get("samples") or []),
        }


class ExecutionRecorder:
    """收集一次回放的全过程采样，最后落成一条执行记录。

    采样跟着回放一起跑：控制器每 20 ms 问一次要不要记一条，够密又不至于
    把记录撑大。采样窗口不是写死的——回放多久就采多久。
    """

    def __init__(
        self,
        *,
        kind: str,
        label: str,
        actions: Sequence[str],
        speed: float,
        slug: str,
    ) -> None:
        self.kind = kind
        self.label = label
        self.actions = list(actions)
        self.speed = speed
        self.started_at = datetime.now(UTC)
        self.id = new_record_id(self.started_at, slug)
        self._samples: list[dict[str, Any]] = []
        self._last_sample = -1e9

    def sample(
        self,
        elapsed: float,
        *,
        waist_pitch: float | None,
        waist_modes: Sequence[int] | None,
        torso_pitch: float | None,
        hip_pitch: float | None,
    ) -> None:
        if elapsed - self._last_sample < SAMPLE_PERIOD_S:
            return
        self._last_sample = elapsed
        self._samples.append(
            {
                "t": round(elapsed, 3),
                "waist_pitch": waist_pitch,
                "waist_modes": list(waist_modes) if waist_modes else None,
                "torso_pitch": torso_pitch,
                "hip_pitch": hip_pitch,
            }
        )

    def build(
        self,
        *,
        outcome: str,
        elapsed_s: float,
        trajectory_duration_s: float,
        tracking: dict[str, Any],
        lower_body: dict[str, Any],
        segments: list[dict[str, Any]],
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        finished_at = datetime.now(UTC)
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "actions": self.actions,
            "speed": self.speed,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "elapsed_s": round(elapsed_s, 3),
            "trajectory_duration_s": round(trajectory_duration_s, 3),
            "outcome": outcome,
            "error_code": error_code,
            "error_message": error_message,
            "segments": segments,
            "tracking": tracking,
            "lower_body": lower_body,
            "samples": self._samples,
        }
