from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.responses import StreamingResponse

from .actions import ActionStore, ActionStoreError
from .adapters import TelemetryAdapter, UnitreeSdkTelemetryAdapter
from .audio import AudioPusher, G1AudioPort
from .camera import DepthCamera
from .control import (
    ControlError,
    ControlService,
    ControlSettings,
    load_gains,
    measured_pose_provider,
)
from .course_import import MAX_PACKAGE_BYTES, CourseImportError, import_course
from .courses import DEFAULT_COURSE_ID, CourseStore, CourseStoreError
from .fsm import FsmError, FsmService, FsmSettings
from .gravity import GravityFeedforward, default_model_path
from .narration import NarrationError, NarrationSession
from .records import RecordStore, RecordStoreError
from .sdk_runtime import describe_crc_status, ensure_crc_libraries
from .telemetry import TelemetryHub

#: 项目根目录（backend/robot_gui/main.py → 上两级）。配置、动作库、日志都在它下面，
#: 不往家目录散落。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configure_logging() -> None:
    """换掉 loguru 的默认 handler：输出到 stderr，带模块名，级别可用环境变量调。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("ROBOT_GUI_LOG_LEVEL", "INFO").upper(),
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )


_configure_logging()

# Unitree SDK2 的 CRC 动态库不会被 wheel 带上，缺了它只有下发命令的路径会失败。
# 在任何 SDK 导入之前补齐，缺失时记一条明确的日志而不是等到接管动作中途才炸。
_crc_ready, _crc_detail = ensure_crc_libraries()
if not _crc_ready:
    logger.warning("Unitree SDK2 CRC 运行时未就绪：{}", _crc_detail)
else:
    logger.info("{}", describe_crc_status())


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str = "real"
    controls_enabled: bool = False
    telemetry_push_hz: float = 10.0
    dds_interface: str | None = None
    low_state_topic: str = "rt/lowstate"
    bms_topic: str = "rt/lf/bmsstate"
    secondary_imu_topic: str = "rt/lf/secondary_imu"
    data_dir: Path | None = None
    fsm_whitelist: tuple[int, ...] = (501,)
    gains_path: Path | None = None
    transition_seconds: float = 2.0
    #: 速度的**默认值**。实际每一把的速度由页面上的下拉给出（回放接口的 speed 参数），
    #: 这个值只是下拉的初始项。
    replay_speed: float = 1.0
    #: 执行记录目录：每次真机回放落一份 JSON 在这里，不上数据库。
    records_dir: Path | None = None
    audio_app_name: str = "robot-gui"
    audio_enabled: bool = False
    gravity_compensation: bool = True
    lower_body_drift_deg: float = 5.0
    #: 沉降观察窗口。躯干前倾是延迟发展的，峰值出现在动作结束后约 2 秒，
    #: 1.5 秒的窗口会漏掉峰值，所以默认 3 秒。
    hold_soak_s: float = 3.0
    #: 允许切换的 FSM 运动模式，形如 "0,1,4,501"。留空表示**全部模式都允许**——
    #: 切换本身有操作员二次确认，门槛交给人和遥控器，不靠配置项。
    fsm_modes: str = ""

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        whitelist_raw = os.getenv("ROBOT_GUI_FSM_WHITELIST", "501").strip()
        whitelist = tuple(
            int(part) for part in whitelist_raw.split(",") if part.strip().isdigit()
        )
        # 配置和动作库都在项目目录里，不往家目录散落；环境变量仍可覆盖。
        data_dir = os.getenv("ROBOT_GUI_DATA_DIR") or str(PROJECT_ROOT / "data")
        gains = os.getenv("ROBOT_GUI_GAINS") or str(
            PROJECT_ROOT / "config" / "gains.json"
        )
        return cls(
            mode=os.getenv("ROBOT_GUI_MODE", "real").strip().lower(),
            controls_enabled=os.getenv("ROBOT_GUI_CONTROLS_ENABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            telemetry_push_hz=float(os.getenv("ROBOT_GUI_TELEMETRY_HZ", "10")),
            dds_interface=os.getenv("ROBOT_GUI_DDS_INTERFACE") or None,
            low_state_topic=os.getenv("ROBOT_GUI_G1_LOWSTATE_TOPIC", "rt/lowstate"),
            bms_topic=os.getenv("ROBOT_GUI_G1_BMS_TOPIC", "rt/lf/bmsstate"),
            secondary_imu_topic=os.getenv(
                "ROBOT_GUI_G1_SECONDARY_IMU_TOPIC", "rt/lf/secondary_imu"
            ),
            data_dir=Path(data_dir) if data_dir else None,
            fsm_whitelist=whitelist or (501,),
            gains_path=Path(gains) if gains else None,
            transition_seconds=float(os.getenv("ROBOT_GUI_TRANSITION_SECONDS", "2.0")),
            replay_speed=float(os.getenv("ROBOT_GUI_REPLAY_SPEED", "1.0")),
            records_dir=Path(
                os.getenv("ROBOT_GUI_RECORDS_DIR")
                or str(PROJECT_ROOT / "logs" / "records")
            ),
            audio_app_name=os.getenv("ROBOT_GUI_AUDIO_APP", "robot-gui"),
            audio_enabled=os.getenv("ROBOT_GUI_AUDIO_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            gravity_compensation=os.getenv("ROBOT_GUI_GRAVITY_COMPENSATION", "true")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            lower_body_drift_deg=float(
                os.getenv("ROBOT_GUI_LOWER_BODY_DRIFT_DEG", "5")
            ),
            hold_soak_s=float(os.getenv("ROBOT_GUI_HOLD_SOAK_S", "3.0")),
            fsm_modes=os.getenv("ROBOT_GUI_FSM_MODES", "").strip(),
        )

    def validate(self) -> None:
        if self.mode != "real":
            raise ValueError("This application requires ROBOT_GUI_MODE=real.")
        if not 1 <= self.telemetry_push_hz <= 20:
            raise ValueError("ROBOT_GUI_TELEMETRY_HZ must be between 1 and 20.")
        if self.mode == "real" and not self.dds_interface:
            raise ValueError(
                "ROBOT_GUI_DDS_INTERFACE is required when ROBOT_GUI_MODE=real."
            )
        for key, topic in (
            ("ROBOT_GUI_G1_LOWSTATE_TOPIC", self.low_state_topic),
            ("ROBOT_GUI_G1_BMS_TOPIC", self.bms_topic),
            ("ROBOT_GUI_G1_SECONDARY_IMU_TOPIC", self.secondary_imu_topic),
        ):
            if not topic.startswith("rt/"):
                raise ValueError(f"{key} must be an SDK2 rt/ topic.")
        if self.controls_enabled:
            if not self.fsm_whitelist:
                raise ValueError(
                    "ROBOT_GUI_FSM_WHITELIST must list at least one FSM id when controls are enabled."
                )
            if self.transition_seconds <= 0:
                raise ValueError("ROBOT_GUI_TRANSITION_SECONDS must be positive.")
            if not 0 < self.replay_speed <= 2.0:
                raise ValueError("ROBOT_GUI_REPLAY_SPEED must be in (0, 2].")


def _error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "reason": message})


def create_app(
    settings: RuntimeSettings | None = None,
    telemetry_adapter: TelemetryAdapter | None = None,
    audio_pusher: AudioPusher | None = None,
    camera: DepthCamera | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_env()

    hub = TelemetryHub()
    adapter = telemetry_adapter or UnitreeSdkTelemetryAdapter(
        interface=runtime_settings.dds_interface or "",
        low_state_topic=runtime_settings.low_state_topic,
        bms_topic=runtime_settings.bms_topic,
        secondary_imu_topic=runtime_settings.secondary_imu_topic,
    )
    store = ActionStore(runtime_settings.data_dir)
    courses = CourseStore(runtime_settings.data_dir, actions=store)
    audio_port = (
        G1AudioPort(runtime_settings.audio_app_name, pusher=audio_pusher)
        if audio_pusher is not None or runtime_settings.audio_enabled
        else None
    )
    feedforward = (
        GravityFeedforward(default_model_path(), measured_pose_provider(hub))
        if runtime_settings.gravity_compensation
        else None
    )
    records = RecordStore(
        runtime_settings.records_dir or (PROJECT_ROOT / "logs" / "records")
    )
    depth_camera = camera or DepthCamera()
    narration = NarrationSession(audio_port)
    control = ControlService(
        settings=ControlSettings(
            interface=runtime_settings.dds_interface or "",
            enabled=runtime_settings.controls_enabled,
            fsm_whitelist=runtime_settings.fsm_whitelist,
            transition_seconds=runtime_settings.transition_seconds,
            replay_speed=runtime_settings.replay_speed,
            lower_body_drift_deg=runtime_settings.lower_body_drift_deg,
            hold_soak_s=runtime_settings.hold_soak_s,
            gains=load_gains(runtime_settings.gains_path),
        ),
        telemetry=hub,
        action_store=store,
        audio_port=audio_port,
        feedforward=feedforward,
        records=records,
    )
    fsm = FsmService(
        settings=FsmSettings.from_env(runtime_settings.fsm_modes),
        adapter=adapter,
        telemetry=hub,
        controls_enabled=runtime_settings.controls_enabled,
        control=control,
        fsm_whitelist=runtime_settings.fsm_whitelist,
    )

    def snapshot() -> dict[str, Any]:
        state = control.state()
        return hub.snapshot(
            runtime_settings.mode,
            runtime_settings.dds_interface,
            None,
            controls_available=state["available"],
            controls_reason=(
                "controls_enabled" if state["available"] else "controls_disabled"
            ),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_settings.validate()
        try:
            await asyncio.to_thread(adapter.start, hub.ingest, hub.mark_error)
        except Exception:  # noqa: BLE001 - 启动失败必须记录后原样抛出
            logger.exception("Required Unitree SDK2/DDS telemetry failed to start.")
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:  # noqa: BLE001 - 清理失败不能盖掉原始异常
                logger.exception("Failed to clean up the Unitree telemetry adapter.")
            raise
        # 深度相机取流是尽力而为的：起不来只记原因，不影响后端启动。
        depth_camera.start()
        try:
            yield
        finally:
            control.release()
            narration.stop()
            if audio_port is not None:
                audio_port.stop()
            depth_camera.stop()
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:  # noqa: BLE001 - 收尾失败只记日志，不阻断退出
                logger.exception("Failed to stop the telemetry adapter cleanly.")

    app = FastAPI(title="Robot GUI API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    app.state.runtime_settings = runtime_settings
    app.state.telemetry_hub = hub
    app.state.telemetry_adapter = adapter
    app.state.action_store = store
    app.state.course_store = courses
    app.state.control = control
    app.state.records = records

    # ---- 只读遥测 ----

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": runtime_settings.mode}

    @app.get("/api/v1/status")
    async def status() -> dict[str, object]:
        return snapshot()

    @app.websocket("/ws/telemetry")
    async def telemetry(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(snapshot())
                await asyncio.sleep(1.0 / runtime_settings.telemetry_push_hz)
        except WebSocketDisconnect:
            return

    # ---- 动作库 ----

    @app.get("/api/v1/actions")
    async def list_actions() -> dict[str, Any]:
        try:
            summaries = store.list()
        except ActionStoreError as error:
            raise _error(error.code, str(error), status=503) from error
        # 课程清单（如果有）一起给前端：有它才有"播放整套"这个入口。
        try:
            course = store.load_course().as_dict()
        except ActionStoreError:
            course = None
        return {"actions": [item.as_dict() for item in summaries], "course": course}

    @app.post("/api/v1/actions/import", status_code=201)
    async def import_action(payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        action = payload.get("action")
        if not isinstance(name, str) or not name:
            raise _error("action_name_required", "缺少动作名 name。", status=400)
        if not isinstance(action, dict):
            raise _error("action_body_required", "缺少动作内容 action。", status=400)
        try:
            body = json.dumps(action, ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
            summary = store.import_action(name, body)
        except ActionStoreError as error:
            raise _error(error.code, str(error)) from error
        return summary.as_dict()

    @app.get("/api/v1/actions/{name}")
    async def export_action(name: str) -> Response:
        try:
            body = store.export_action(name)
        except ActionStoreError as error:
            raise _error(error.code, str(error), status=404) from error
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
        )

    @app.delete("/api/v1/actions/{name}")
    async def delete_action(name: str) -> dict[str, Any]:
        try:
            store.delete(name)
        except ActionStoreError as error:
            raise _error(error.code, str(error), status=404) from error
        return {"deleted": name}

    # ---- 课程库 ----

    @app.get("/api/v1/courses")
    async def list_courses() -> dict[str, Any]:
        """课程列表：默认课（data/course.json）在最前，其余是导入的课。"""
        try:
            records = courses.list()
        except CourseStoreError as error:
            raise _error(error.code, str(error)) from error
        return {"courses": [record.as_dict() for record in records]}

    @app.get("/api/v1/courses/{course_id}")
    async def read_course(course_id: str) -> dict[str, Any]:
        try:
            return courses.read(course_id).as_dict()
        except CourseStoreError as error:
            raise _error(error.code, str(error), status=404) from error

    @app.delete("/api/v1/courses/{course_id}")
    async def delete_course(course_id: str) -> dict[str, Any]:
        """删课程记录；动作库是共享的，不动。"""
        try:
            courses.delete(course_id)
        except CourseStoreError as error:
            status = 409 if error.code == "course_protected" else 404
            raise _error(error.code, str(error), status=status) from error
        return {"course_id": course_id, "deleted": True}

    @app.post("/api/v1/courses/import", status_code=201)
    async def import_course_package(
        # File()/Form() 放在默认值里是 FastAPI 声明上传接口的标准写法。
        file: UploadFile = File(...),  # noqa: B008
        name: str | None = Form(default=None),  # noqa: B008
    ) -> dict[str, Any]:
        """导入一个 courseware-v3 课程包。

        导入是事务性的：动作、语音、课程清单要么都写成功，要么都不写。
        """
        data_dir = runtime_settings.data_dir
        if data_dir is None:
            raise _error(
                "action_store_not_configured", "未配置 ROBOT_GUI_DATA_DIR。", status=503
            )
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            temp_path = Path(handle.name)
            written = 0
            try:
                # 上传内容边收边写，不整个读进内存。
                while chunk := await file.read(1 << 20):
                    written += len(chunk)
                    if written > MAX_PACKAGE_BYTES:
                        raise _error(
                            "package_too_large",
                            f"课程包超过 {MAX_PACKAGE_BYTES} 字节的上限。",
                            status=413,
                        )
                    handle.write(chunk)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        try:
            course_name = name or Path(file.filename or "course").stem
            record = import_course(temp_path, store, courses, name=course_name)
        except CourseImportError as error:
            raise _error(error.code, str(error), status=400) from error
        except CourseStoreError as error:
            raise _error(error.code, str(error)) from error
        except ActionStoreError as error:
            raise _error(error.code, str(error)) from error
        finally:
            temp_path.unlink(missing_ok=True)
        return {"course": record.as_dict()}

    # ---- 真机控制 ----

    @app.get("/api/v1/control/state")
    async def control_state() -> dict[str, Any]:
        """控制会话状态。

        `narration` 是**只播讲解语音**那一类会话（机器人不动）：页面上只有一套
        暂停/继续/停止，谁在跑就归谁管。
        """
        state = control.state()
        state["narration"] = narration.state()
        return state

    @app.post("/api/v1/control/replay")
    async def replay(payload: dict[str, Any]) -> dict[str, Any]:
        """回放一个动作，或课程清单里的**一整套**。

        - `{"action": "p1_s2"}`：单个动作（调试用）
        - `{"course": true}`：按 `data/course.json` 的顺序播整套（默认课）
        - `{"course": "a1b2c3"}`：按指定课程 id 播整套
        - `{"speed": 0.8}`：这一把的速度倍率（页面上的下拉），可选

        整套是一条连续轨迹：动作之间是规划出来的衔接段，讲解语音按每个动作在
        轨迹里的起始时刻拼成一条连续的 PCM。
        """
        confirmed = payload.get("confirmed") is True
        requested = payload.get("audio")
        explicit = isinstance(requested, str) and bool(requested)
        speed = payload.get("speed")
        if speed is not None and (
            isinstance(speed, bool) or not isinstance(speed, (int, float))
        ):
            raise _error("replay_speed_invalid", "speed 必须是数字。", status=400)
        course = payload.get("course")
        if course is True:
            try:
                names = list(store.load_course().actions)
            except ActionStoreError as error:
                raise _error(error.code, str(error), status=404) from error
            # 整套播放时每段用同名语音；显式指定的 audio 只对单动作有意义。
            explicit = False
            kind = "course"
        elif isinstance(course, str) and course:
            try:
                names = list(courses.resolve_actions(course))
            except CourseStoreError as error:
                raise _error(error.code, str(error), status=404) from error
            explicit = False
            kind = "course"
        else:
            action = payload.get("action")
            if not isinstance(action, str) or not action:
                raise _error(
                    "action_required", "缺少要回放的动作名 action。", status=400
                )
            names = [action]
            kind = "action"
        # 未显式指定语音时，自动用与动作同名的 PCM，让讲解和手势一起播；
        # 找不到就安静地不播语音（不做 TTS 兜底）。
        clips: dict[str, bytes] = {}
        for name in names:
            audio_name = requested if explicit else name
            pcm = _read_audio(store, audio_name, required=explicit)
            if pcm is not None:
                clips[name] = pcm
        try:
            return control.request_replay(
                names,
                confirmed=confirmed,
                audio_clips=clips,
                speed=speed,
                kind=kind,
            )
        except ControlError as error:
            raise _error(error.code, str(error)) from error

    @app.post("/api/v1/control/pause")
    async def pause() -> dict[str, Any]:
        """暂停当前会话：回放是停语音并交还控制权，只播语音是停在段边界。"""
        if narration.active:
            try:
                return narration.pause()
            except NarrationError as error:
                raise _error(error.code, str(error)) from error
        try:
            return await asyncio.to_thread(control.pause)
        except ControlError as error:
            raise _error(error.code, str(error)) from error

    @app.post("/api/v1/control/resume")
    async def resume() -> dict[str, Any]:
        """从暂停处继续：回放按当前实测姿态重新规划，只播语音接着播下一段。"""
        if narration.active:
            try:
                return narration.resume()
            except NarrationError as error:
                raise _error(error.code, str(error)) from error
        try:
            return await asyncio.to_thread(control.resume)
        except ControlError as error:
            raise _error(error.code, str(error)) from error

    @app.post("/api/v1/control/release")
    async def release() -> dict[str, Any]:
        """停止当前会话：回放交还控制权，只播语音就只是停声。"""
        if narration.active:
            return narration.stop()
        return control.release()

    @app.post("/api/v1/control/audio")
    async def play_audio(payload: dict[str, Any]) -> dict[str, Any]:
        """只播放讲解语音：机器人不动，也不接管控制权。

        - `{"name": "p1_s2"}`：放一段
        - `{"course": "<课程 id>"}`：按课程顺序放整套讲解

        语音走的是回放时同一条推流路径（PCM → 独立子进程 → 扬声器），所以听到的
        就是回放时的效果，只是这次机器人一动不动。段与段之间按同一条时间线补
        静音，听感和整套回放一致。
        """
        if audio_port is None:
            raise _error(
                "audio_disabled",
                "语音推流未开启：设 ROBOT_GUI_AUDIO_ENABLED=1 后重启后端。",
                status=409,
            )
        if control.state()["phase"] not in ("idle", "fault"):
            raise _error("control_busy", "回放还在进行，先停掉再播语音。", status=409)
        course_id = payload.get("course")
        name = payload.get("name")
        if course_id is True:
            course_id = DEFAULT_COURSE_ID
        if isinstance(course_id, str) and course_id:
            try:
                record = courses.read(course_id)
            except CourseStoreError as error:
                raise _error(error.code, str(error), status=404) from error
            schedule = _narration_schedule(
                [(item.name, item.duration_s) for item in record.segments],
                runtime_settings.transition_seconds,
            )
        elif isinstance(name, str) and name:
            pcm = _read_audio(store, name, required=True)
            schedule = [(name, 0.0)] if pcm is not None else []
        else:
            raise _error(
                "audio_target_required",
                "要给出动作名 name 或课程 id course。",
                status=400,
            )
        clips = {item: store.read_audio(item) for item, _ in schedule}
        clips = {item: pcm for item, pcm in clips.items() if pcm is not None}
        if not clips:
            raise _error("audio_not_found", "找不到对应的讲解语音。", status=404)
        try:
            return narration.start(schedule, clips)
        except NarrationError as error:
            raise _error(error.code, str(error)) from error

    # ---- 深度相机 ----

    @app.get("/api/v1/camera/state")
    async def camera_state() -> dict[str, Any]:
        """深度相机当前能不能用，以及不能用时的原因。"""
        return depth_camera.state()

    @app.get("/api/v1/camera/frame")
    async def camera_frame() -> Response:
        """最新一帧（JPEG）。取不到就 404，并把原因写进 detail。"""
        frame = depth_camera.latest_frame()
        if frame is None:
            state = depth_camera.state()
            raise _error(
                state["error_code"] or "camera.unavailable",
                state["error_message"] or "深度相机暂时没有画面。",
                status=404,
            )
        return Response(content=frame, media_type="image/jpeg")

    @app.get("/api/v1/camera/stream")
    async def camera_stream() -> StreamingResponse:
        """MJPEG 推流：浏览器直接用 <img src> 就能显示，不用前端轮询。"""
        boundary = "frame"

        async def frames():
            while True:
                frame = await asyncio.to_thread(depth_camera.latest_frame)
                if frame is None:
                    await asyncio.sleep(0.2)
                    continue
                yield (
                    (
                        f"--{boundary}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii")
                    + frame
                    + b"\r\n"
                )

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={"Cache-Control": "no-store"},
        )

    # ---- 执行记录 ----

    @app.get("/api/v1/records")
    async def list_records() -> dict[str, Any]:
        return {"records": records.list()}

    @app.get("/api/v1/records/{record_id}")
    async def read_record(record_id: str) -> dict[str, Any]:
        try:
            return records.read(record_id)
        except RecordStoreError as error:
            raise _error(error.code, str(error), status=404) from error

    @app.get("/api/v1/records/{record_id}/download")
    async def download_record(record_id: str) -> Response:
        try:
            record = records.read(record_id)
        except RecordStoreError as error:
            raise _error(error.code, str(error), status=404) from error
        body = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
        return Response(
            content=body.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{record_id}.json"'},
        )

    # ---- FSM 运动模式 ----

    @app.get("/api/v1/fsm/state")
    async def fsm_state() -> dict[str, Any]:
        return fsm.state()

    @app.post("/api/v1/fsm/mode")
    async def fsm_mode(payload: dict[str, Any]) -> dict[str, Any]:
        fsm_id = payload.get("fsm_id")
        if not isinstance(fsm_id, int):
            raise _error(
                "fsm_mode_required",
                "缺少要切换的模式 ID fsm_id。",
                status=400,
            )
        confirmed = payload.get("confirmed") is True
        try:
            return fsm.request_mode(fsm_id, confirmed=confirmed)
        except FsmError as error:
            raise _error(error.code, str(error)) from error

    @app.websocket("/ws/control")
    async def control_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(control.state())
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            return

    return app


def _narration_schedule(
    segments: Sequence[tuple[str, float]],
    transition: float,
) -> list[tuple[str, float]]:
    """只播讲解时各段的起始时刻。

    和 tools/play_audio.py、整套回放同一套拼法：每段之间留一段衔接的静音，
    听起来和整套回放时一致。返回 (动作名, 起始秒数)。
    """
    schedule: list[tuple[str, float]] = []
    at = transition
    for name, duration in segments:
        schedule.append((name, at))
        at += duration + transition
    return schedule


def _read_audio(store: ActionStore, name: str, required: bool = False) -> bytes | None:
    """从动作库目录的 audio/ 子目录取同名 PCM（16 kHz 单声道 16 bit）。

    未显式指定语音时是「尽力而为」：找不到就安静地不播语音，而不是让回放失败。
    显式指定了却找不到才算错误。
    """
    if store.data_dir is None:
        if required:
            raise _error(
                "action_store_not_configured", "未配置 ROBOT_GUI_DATA_DIR。", status=503
            )
        return None
    pcm = store.read_audio(name)
    if pcm is None and required:
        raise _error("audio_not_found", f"找不到音频 {name}.pcm。", status=404)
    return pcm


app = create_app()
