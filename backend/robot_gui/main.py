from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .actions import ActionStore, ActionStoreError
from .adapters import TelemetryAdapter, UnitreeSdkTelemetryAdapter
from .audio import AudioClientFactory, G1AudioPort
from .control import (
    ControlError,
    ControlService,
    ControlSettings,
    load_gains,
    measured_pose_provider,
)
from .fsm import FsmError, FsmService, FsmSettings
from .gravity import GravityFeedforward, default_model_path
from .sdk_runtime import describe_crc_status, ensure_crc_libraries
from .telemetry import TelemetryHub

logger = logging.getLogger(__name__)

# Unitree SDK2 的 CRC 动态库不会被 wheel 带上，缺了它只有下发命令的路径会失败。
# 在任何 SDK 导入之前补齐，缺失时记一条明确的日志而不是等到接管动作中途才炸。
_crc_ready, _crc_detail = ensure_crc_libraries()
if not _crc_ready:
    logger.warning("Unitree SDK2 CRC 运行时未就绪：%s", _crc_detail)
else:
    logger.info("%s", describe_crc_status())

AUDIO_DIR_NAME = "audio"


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
    replay_speed: float = 1.0
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
        data_dir = os.getenv("ROBOT_GUI_DATA_DIR") or None
        gains = os.getenv("ROBOT_GUI_GAINS") or None
        return cls(
            mode=os.getenv("ROBOT_GUI_MODE", "real").strip().lower(),
            controls_enabled=os.getenv(
                "ROBOT_GUI_CONTROLS_ENABLED", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            telemetry_push_hz=float(os.getenv("ROBOT_GUI_TELEMETRY_HZ", "10")),
            dds_interface=os.getenv("ROBOT_GUI_DDS_INTERFACE") or None,
            low_state_topic=os.getenv(
                "ROBOT_GUI_G1_LOWSTATE_TOPIC", "rt/lowstate"
            ),
            bms_topic=os.getenv("ROBOT_GUI_G1_BMS_TOPIC", "rt/lf/bmsstate"),
            secondary_imu_topic=os.getenv(
                "ROBOT_GUI_G1_SECONDARY_IMU_TOPIC", "rt/lf/secondary_imu"
            ),
            data_dir=Path(data_dir) if data_dir else None,
            fsm_whitelist=whitelist or (501,),
            gains_path=Path(gains) if gains else None,
            transition_seconds=float(os.getenv("ROBOT_GUI_TRANSITION_SECONDS", "2.0")),
            replay_speed=float(os.getenv("ROBOT_GUI_REPLAY_SPEED", "1.0")),
            audio_app_name=os.getenv("ROBOT_GUI_AUDIO_APP", "robot-gui"),
            audio_enabled=os.getenv(
                "ROBOT_GUI_AUDIO_ENABLED", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            gravity_compensation=os.getenv(
                "ROBOT_GUI_GRAVITY_COMPENSATION", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            lower_body_drift_deg=float(os.getenv("ROBOT_GUI_LOWER_BODY_DRIFT_DEG", "5")),
            hold_soak_s=float(os.getenv("ROBOT_GUI_HOLD_SOAK_S", "3.0")),
            fsm_modes=os.getenv("ROBOT_GUI_FSM_MODES", "").strip(),
        )

    def validate(self) -> None:
        if self.mode != "real":
            raise ValueError("This application requires ROBOT_GUI_MODE=real.")
        if not 1 <= self.telemetry_push_hz <= 20:
            raise ValueError(
                "ROBOT_GUI_TELEMETRY_HZ must be between 1 and 20."
            )
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
    audio_client: Any | None = None,
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
    if audio_client is not None:
        audio_source: Any = audio_client
    elif runtime_settings.audio_enabled:
        audio_source = AudioClientFactory()
    else:
        audio_source = None
    audio_port = (
        G1AudioPort(audio_source, runtime_settings.audio_app_name)
        if audio_source is not None
        else None
    )
    feedforward = (
        GravityFeedforward(default_model_path(), measured_pose_provider(hub))
        if runtime_settings.gravity_compensation
        else None
    )
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
        except Exception:
            logger.exception("Required Unitree SDK2/DDS telemetry failed to start.")
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:
                logger.exception("Failed to clean up the Unitree telemetry adapter.")
            raise
        try:
            yield
        finally:
            control.release()
            if isinstance(audio_source, AudioClientFactory):
                audio_source.close()
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:
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
    app.state.control = control

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
                await asyncio.sleep(
                    1.0 / runtime_settings.telemetry_push_hz
                )
        except WebSocketDisconnect:
            return

    # ---- 动作库 ----

    @app.get("/api/v1/actions")
    async def list_actions() -> dict[str, Any]:
        try:
            summaries = store.list()
        except ActionStoreError as error:
            raise _error(error.code, str(error), status=503) from error
        return {"actions": [item.as_dict() for item in summaries]}

    @app.post("/api/v1/actions/import", status_code=201)
    async def import_action(payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        action = payload.get("action")
        if not isinstance(name, str) or not name:
            raise _error("action_name_required", "缺少动作名 name。", status=400)
        if not isinstance(action, dict):
            raise _error("action_body_required", "缺少动作内容 action。", status=400)
        try:
            body = json.dumps(action, ensure_ascii=False, allow_nan=False).encode("utf-8")
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

    # ---- 真机控制 ----

    @app.get("/api/v1/control/state")
    async def control_state() -> dict[str, Any]:
        return control.state()

    @app.post("/api/v1/control/replay")
    async def replay(payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            raise _error("action_required", "缺少要回放的动作名 action。", status=400)
        confirmed = payload.get("confirmed") is True
        requested = payload.get("audio")
        explicit = isinstance(requested, str) and bool(requested)
        # 未显式指定语音时，自动用与动作同名的 PCM，让讲解和手势一起播；
        # 找不到就安静地不播语音（不做 TTS 兜底）。
        audio_name = requested if explicit else action
        audio_pcm = _read_audio(store, audio_name, required=explicit)
        try:
            return control.request_replay(
                action,
                confirmed=confirmed,
                audio_pcm=audio_pcm,
            )
        except ControlError as error:
            raise _error(error.code, str(error)) from error

    @app.post("/api/v1/control/release")
    async def release() -> dict[str, Any]:
        return control.release()

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


def _read_audio(store: ActionStore, name: str, required: bool = False) -> bytes | None:
    """从动作库目录的 audio/ 子目录取同名 PCM（16 kHz 单声道 16 bit）。

    未显式指定语音时是「尽力而为」：找不到就安静地不播语音，而不是让回放失败。
    显式指定了却找不到才算错误。
    """
    directory = store.data_dir
    if directory is None:
        if required:
            raise _error("action_store_not_configured", "未配置 ROBOT_GUI_DATA_DIR。", status=503)
        return None
    path = directory / AUDIO_DIR_NAME / f"{name}.pcm"
    if not path.is_file():
        if required:
            raise _error("audio_not_found", f"找不到音频 {name}.pcm。", status=404)
        return None
    return path.read_bytes()


app = create_app()
