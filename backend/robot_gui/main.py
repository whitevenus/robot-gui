from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware


class SdkAdapter(Protocol):
    """Boundary for a future real SDK adapter."""


SdkFactory = Callable[[], SdkAdapter]


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str = "mock"
    controls_enabled: bool = False
    websocket_interval_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        return cls(
            mode=os.getenv("ROBOT_GUI_MODE", "mock").strip().lower(),
            controls_enabled=os.getenv(
                "ROBOT_GUI_CONTROLS_ENABLED", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        )

    def validate(self) -> None:
        if self.mode != "mock":
            raise ValueError(
                "ROBOT_GUI_MODE must be 'mock'; real SDK mode is not "
                "implemented in this release."
            )
        if self.controls_enabled:
            raise ValueError(
                "Controls cannot be enabled in mock mode; "
                "set ROBOT_GUI_CONTROLS_ENABLED=false."
            )
        if not 0.1 <= self.websocket_interval_seconds <= 10:
            raise ValueError(
                "websocket_interval_seconds must be between 0.1 and 10."
            )


class MockStatus:
    def __init__(self) -> None:
        self._started_monotonic = time.monotonic()
        self._started_at = datetime.now(UTC)

    def snapshot(self) -> dict[str, object]:
        age_ms = max(0, round((time.monotonic() - self._started_monotonic) * 1000))
        return {
            "schema_version": "1.0",
            "snapshot_id": age_ms,
            "server_time": datetime.now(UTC).isoformat(),
            "mode": "mock",
            "connection": {
                "state": "mock",
                "interface": None,
                "last_data_at": self._started_at.isoformat(),
                "error": None,
                "message": "Mock 数据源已启动；当前未连接真实机器人或 DDS。",
            },
            "sources": {
                "low_state": {
                    "state": "mock",
                    "age_ms": age_ms,
                    "sample_time": self._started_at.isoformat(),
                }
            },
            "capabilities": {
                "controls": {
                    "available": False,
                    "reason": "mock_mode_control_disabled",
                }
            },
        }


def create_app(
    settings: RuntimeSettings | None = None,
    sdk_factory: SdkFactory | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_env()
    runtime_settings.validate()

    app = FastAPI(title="Robot GUI API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.state.runtime_settings = runtime_settings
    app.state.status_source = MockStatus()
    app.state.sdk_adapter = None
    app.state.sdk_factory_requested = sdk_factory is not None

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "mock"}

    @app.get("/api/v1/status")
    async def status() -> dict[str, object]:
        return app.state.status_source.snapshot()

    @app.post("/api/v1/control/sessions", status_code=503)
    async def create_control_session() -> None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "capability_disabled",
                "reason": "mock_mode_control_disabled",
            },
        )

    @app.websocket("/ws/telemetry")
    async def telemetry(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(app.state.status_source.snapshot())
                await asyncio.sleep(
                    app.state.runtime_settings.websocket_interval_seconds
                )
        except WebSocketDisconnect:
            return

    return app


app = create_app()
