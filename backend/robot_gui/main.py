from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Protocol

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .adapters import (
    FakeTelemetryAdapter,
    TelemetryAdapter,
    UnitreeSdkTelemetryAdapter,
)
from .telemetry import TelemetryHub

logger = logging.getLogger(__name__)


class SdkAdapter(Protocol):
    """Boundary for a future real SDK adapter."""


SdkFactory = Callable[[], SdkAdapter]


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str = "mock"
    controls_enabled: bool = False
    telemetry_push_hz: float = 10.0
    dds_interface: str | None = None
    low_state_topic: str = "rt/lowstate"
    bms_topic: str = "rt/lf/bmsstate"
    secondary_imu_topic: str = "rt/lf/secondary_imu"

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        return cls(
            mode=os.getenv("ROBOT_GUI_MODE", "mock").strip().lower(),
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
        )

    def validate(self) -> None:
        if self.mode not in {"mock", "real"}:
            raise ValueError("ROBOT_GUI_MODE must be 'mock' or 'real'.")
        if self.controls_enabled:
            raise ValueError("This release is telemetry-only; controls must stay disabled.")
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


def create_app(
    settings: RuntimeSettings | None = None,
    sdk_factory: SdkFactory | None = None,
    telemetry_adapter: TelemetryAdapter | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_env()
    runtime_settings.validate()

    hub = TelemetryHub()
    adapter = telemetry_adapter
    if adapter is None and runtime_settings.mode == "mock":
        adapter = FakeTelemetryAdapter(1.0 / runtime_settings.telemetry_push_hz)
    if adapter is None:
        adapter = UnitreeSdkTelemetryAdapter(
            interface=runtime_settings.dds_interface or "",
            low_state_topic=runtime_settings.low_state_topic,
            bms_topic=runtime_settings.bms_topic,
            secondary_imu_topic=runtime_settings.secondary_imu_topic,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.adapter_error = None
        try:
            await asyncio.to_thread(adapter.start, hub.ingest, hub.mark_error)
        except Exception as error:
            logger.exception("Failed to start telemetry adapter.")
            app.state.adapter_error = str(error)
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:
                logger.exception("Failed to clean up the telemetry adapter.")
        try:
            yield
        finally:
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception:
                logger.exception("Failed to stop the telemetry adapter cleanly.")

    app = FastAPI(title="Robot GUI API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.state.runtime_settings = runtime_settings
    app.state.telemetry_hub = hub
    app.state.telemetry_adapter = adapter
    app.state.sdk_adapter = None
    app.state.sdk_factory_requested = sdk_factory is not None

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok" if app.state.adapter_error is None else "degraded",
            "mode": runtime_settings.mode,
        }

    @app.get("/api/v1/status")
    async def status() -> dict[str, object]:
        return hub.snapshot(
            runtime_settings.mode,
            runtime_settings.dds_interface,
            app.state.adapter_error,
        )

    @app.post("/api/v1/control/sessions", status_code=503)
    async def create_control_session() -> None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "capability_disabled",
                "reason": "telemetry_release_is_read_only",
            },
        )

    @app.websocket("/ws/telemetry")
    async def telemetry(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(
                    hub.snapshot(
                        runtime_settings.mode,
                        runtime_settings.dds_interface,
                        app.state.adapter_error,
                    )
                )
                await asyncio.sleep(
                    1.0 / runtime_settings.telemetry_push_hz
                )
        except WebSocketDisconnect:
            return

    return app


app = create_app()
