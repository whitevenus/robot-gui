"""课程库与课程包导入的测试。

全程不碰硬件：造一个 courseware-v3 课程包，走接口导入，再看动作库、语音和
课程清单是否都落地；坏包必须被拒，而且不留半截文件。

重点验证两件事：
- 语音在导入时被**改名成动作名**（回放按同名找语音）；
- 导入是事务性的：写到一半失败要把这次写的都撤掉。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from robot_gui.main import RuntimeSettings, create_app
from test_real_telemetry import TelemetryTestAdapter

SCHEMA_VERSION = "courseware-v3"
PACKAGE_FORMAT = "robocourse-gateway-package-v1"
PCM_TYPE = "audio/L16;rate=16000;channels=1"
JSON_TYPE = "application/json"
COURSE_ID = "11111111-1111-4111-8111-111111111111"


def build_package(
    directory: Path,
    *,
    course_id: str = COURSE_ID,
    segments: int = 2,
    schema_version: str = SCHEMA_VERSION,
    group: str = "arms",
    dimensions: int = 14,
    frames: int = 3,
    audio_seconds: float = 1.0,
    audio_content_type: str = PCM_TYPE,
    corrupt_asset_digest: bool = False,
    motion_by_index: dict[int, dict] | None = None,
) -> Path:
    """造一个 courseware-v3 课程包，返回 zip 路径。"""
    files: list[dict] = []
    assets: dict[str, bytes] = {}
    program_segments: list[dict] = []
    timeline: list[dict] = []
    for index in range(segments):
        action_id = f"aaaaaaaa-0000-4000-8000-00000000000{index}"
        audio_id = f"bbbbbbbb-0000-4000-8000-00000000000{index}"
        action = {
            "group": group,
            "frames": [{"t": k / 30, "q": [0.0] * dimensions} for k in range(frames)],
        }
        if motion_by_index and index in motion_by_index:
            action = motion_by_index[index]
        action_bytes = json.dumps(action).encode()
        audio_bytes = b"\x01\x00" * int(16000 * audio_seconds)
        assets[f"assets/{action_id}.json"] = action_bytes
        assets[f"assets/{audio_id}.pcm"] = audio_bytes
        files.append(
            {
                "id": action_id,
                "path": f"assets/{action_id}.json",
                "kind": "other",
                "content_type": JSON_TYPE,
                "byte_size": len(action_bytes),
                "sha256": (
                    "0" * 64
                    if corrupt_asset_digest and index == 0
                    else hashlib.sha256(action_bytes).hexdigest()
                ),
            }
        )
        files.append(
            {
                "id": audio_id,
                "path": f"assets/{audio_id}.pcm",
                "kind": "audio",
                "content_type": audio_content_type,
                "byte_size": len(audio_bytes),
                "sha256": hashlib.sha256(audio_bytes).hexdigest(),
            }
        )
        program_segments.append(
            {
                "id": f"seg-{index}",
                "text": f"第 {index} 段讲解",
                "audio": {"asset_id": audio_id, "input_hash": "0" * 64},
                "motion": {
                    "asset_id": action_id,
                    "group": "arms",
                    "frames": frames,
                    "duration_s": round(frames / 30, 3),
                },
            }
        )
        timeline.append(
            {"id": f"step-{index}", "kind": "narration", "segment_id": f"seg-{index}"}
        )

    program = {
        "schema_version": schema_version,
        "target_profile": "g1",
        "slides": [
            {
                "id": "slide-1",
                "viewportSize": 1920,
                "viewportRatio": 0.5625,
                "theme": {},
                "elements": [],
            }
        ],
        "performances": [
            {
                "slide_id": "slide-1",
                "segments": program_segments,
                "focus_regions": [],
                "timeline": timeline,
            }
        ],
    }
    program_bytes = json.dumps(program).encode()
    manifest = {
        "format": PACKAGE_FORMAT,
        "course_id": course_id,
        "target_profile": "g1",
        "program_sha256": hashlib.sha256(program_bytes).hexdigest(),
        "files": files,
    }

    path = directory / "courseware.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("program.json", program_bytes)
        archive.writestr("manifest.json", json.dumps(manifest).encode())
        for name, blob in assets.items():
            archive.writestr(name, blob)
    return path


def build_client(tmp_path: Path) -> TestClient:
    settings = RuntimeSettings(
        mode="real",
        dds_interface="test0",
        data_dir=tmp_path,
        records_dir=tmp_path / "records",
    )
    app = create_app(settings, telemetry_adapter=TelemetryTestAdapter())
    return TestClient(app)


def action_files(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("*.json") if p.name != "course.json")


# ---------------------------------------------------------------- 导入


def test_import_writes_actions_audio_and_course(tmp_path: Path) -> None:
    package = build_package(tmp_path)
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
            data={"name": "测试课程"},
        )
        assert response.status_code == 201, response.text
        course = response.json()["course"]
        assert course["id"] == COURSE_ID
        assert course["name"] == "测试课程"
        assert len(course["actions"]) == 2
        assert course["segments"][0]["text"] == "第 0 段讲解"
        assert course["segments"][0]["has_audio"] is True

    # 动作按资产 UUID 落到动作库里。
    stored = action_files(tmp_path)
    assert len(stored) == 2
    # 语音改名为动作名：回放按同名找语音。
    for action in stored:
        assert (tmp_path / "audio" / f"{action.stem}.pcm").is_file()
    assert (tmp_path / "courses" / f"{COURSE_ID}.json").is_file()


def test_import_uses_voice_renamed_after_action(tmp_path: Path) -> None:
    """语音资产本身是另一个 UUID，导入后必须落在动作名下。"""
    package = build_package(tmp_path, segments=1)
    with build_client(tmp_path) as client:
        client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    audio_files = sorted((tmp_path / "audio").glob("*.pcm"))
    assert len(audio_files) == 1
    assert audio_files[0].stem.startswith("aaaaaaaa-")


def test_import_is_idempotent(tmp_path: Path) -> None:
    package = build_package(tmp_path)
    with build_client(tmp_path) as client:
        first = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
        assert second.status_code == 201, second.text
        listing = client.get("/api/v1/courses").json()["courses"]
    # 同一个包重复导入不产生重复动作，也不产生第二门课。
    assert len(action_files(tmp_path)) == 2
    assert len([c for c in listing if c["id"] == COURSE_ID]) == 1


def test_import_rolls_back_when_a_later_segment_conflicts(tmp_path: Path) -> None:
    """写到一半失败要把这次写的都撤掉，不能留下半截。"""
    # 先手工放一个同名但内容不同的动作，制造冲突。
    existing = "aaaaaaaa-0000-4000-8000-000000000001"
    (tmp_path / f"{existing}.json").write_text(
        json.dumps(
            {
                "group": "arms",
                "frames": [{"t": 0.0, "q": [0.5] * 14}, {"t": 0.1, "q": [0.5] * 14}],
            }
        ),
        encoding="utf-8",
    )
    package = build_package(tmp_path, segments=2)
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "action_conflict"

    # 第一段的动作被撤掉了；原有的那个没被动过。
    assert action_files(tmp_path) == [tmp_path / f"{existing}.json"]
    assert not (tmp_path / "courses" / f"{COURSE_ID}.json").exists()


# ---------------------------------------------------------------- 坏包


def test_import_rejects_wrong_schema_version(tmp_path: Path) -> None:
    package = build_package(tmp_path, schema_version="courseware-v2")
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "package_schema_unsupported"
    assert action_files(tmp_path) == []


def test_import_rejects_motion_with_wrong_group(tmp_path: Path) -> None:
    package = build_package(tmp_path, group="arms_waist")
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "motion_group_invalid"
    assert action_files(tmp_path) == []


def test_import_rejects_motion_with_wrong_dimension(tmp_path: Path) -> None:
    package = build_package(tmp_path, dimensions=13)
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] in {
        "motion_frame_invalid",
        "motion_invalid",
    }
    assert action_files(tmp_path) == []


def test_import_rejects_audio_longer_than_twenty_seconds(tmp_path: Path) -> None:
    package = build_package(tmp_path, audio_seconds=20.5)
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "audio_too_long"
    assert action_files(tmp_path) == []


def test_import_rejects_audio_with_wrong_content_type(tmp_path: Path) -> None:
    package = build_package(tmp_path, audio_content_type="audio/mpeg")
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "audio_type_invalid"
    assert action_files(tmp_path) == []


def test_import_rejects_asset_with_wrong_digest(tmp_path: Path) -> None:
    """清单里的 SHA-256 必须与内容对上。"""
    package = build_package(tmp_path, corrupt_asset_digest=True)
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "package_digest_mismatch"
    assert action_files(tmp_path) == []


def test_import_rejects_not_a_zip(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", b"not a zip", "application/zip")},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "package_unreadable"


# ---------------------------------------------------------------- 课程接口


def test_course_endpoints_list_read_and_delete(tmp_path: Path) -> None:
    package = build_package(tmp_path)
    with build_client(tmp_path) as client:
        client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", package.read_bytes(), "application/zip")},
            data={"name": "测试课程"},
        )
        listing = client.get("/api/v1/courses").json()["courses"]
        assert [item["id"] for item in listing] == [COURSE_ID]
        detail = client.get(f"/api/v1/courses/{COURSE_ID}").json()
        assert detail["name"] == "测试课程"
        assert len(detail["segments"]) == 2

        deleted = client.delete(f"/api/v1/courses/{COURSE_ID}")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/courses/{COURSE_ID}").status_code == 404
        assert client.get("/api/v1/courses").json()["courses"] == []

    # 删课程不动共享的动作库。
    assert len(action_files(tmp_path)) == 2


def test_default_course_stays_available(tmp_path: Path) -> None:
    """默认课（data/course.json）继续有效，不能被新课程功能弄坏。"""
    (tmp_path / "p1_s0.json").write_text(
        json.dumps(
            {
                "group": "arms",
                "frames": [{"t": 0.0, "q": [0.0] * 14}, {"t": 0.1, "q": [0.0] * 14}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "course.json").write_text(
        json.dumps({"name": "stage_c", "actions": ["p1_s0"]}), encoding="utf-8"
    )
    with build_client(tmp_path) as client:
        listing = client.get("/api/v1/courses").json()["courses"]
        assert listing[0]["id"] == "default"
        assert listing[0]["name"] == "stage_c"
        assert listing[0]["actions"] == ["p1_s0"]
        # 动作库接口照样返回默认课。
        actions = client.get("/api/v1/actions").json()
        assert actions["course"]["name"] == "stage_c"
        # 默认课不能被删。
        assert client.delete("/api/v1/courses/default").status_code == 409


def test_replay_with_unknown_course_id_is_rejected(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post("/api/v1/control/replay", json={"course": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "course_not_found"
