# Robot GUI

Robot GUI is a development workstation for Unitree robot telemetry and motion
workflows. The current application is **telemetry-only**: Mock mode is the default and
does not connect to a robot; real mode subscribes to read-only G1 SDK2/DDS
telemetry when the Unitree SDK is installed and a DDS interface is configured.
Hardware controls remain disabled in both modes.

## Run the Mock workstation

Requirements: Python 3.11+ and Node.js 20+.

In one terminal, install and run the backend:

```powershell
uv sync --directory backend --extra test
uv run --directory backend uvicorn robot_gui.main:app --reload
```

In a second terminal, install and run the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite. The page must show the Mock banner and explain
that no real robot or DDS connection exists. FSM, locomotion, teaching, and
playback controls are disabled. The backend rejects control-session requests
in Mock mode.

## Read-only G1 telemetry mode

Install Unitree SDK2 Python and its CycloneDDS runtime in the target robot/PC2
environment. Configure the DDS interface and start the backend with:

```powershell
$env:ROBOT_GUI_MODE = "real"
$env:ROBOT_GUI_DDS_INTERFACE = "eth0"
uv run --directory backend uvicorn robot_gui.main:app --host 127.0.0.1
```

The defaults use the G1 SDK LowState topic `rt/lowstate`, BMS topic
`rt/lf/bmsstate`, and secondary IMU topic `rt/lf/secondary_imu`. These can be
overridden with `ROBOT_GUI_G1_LOWSTATE_TOPIC`, `ROBOT_GUI_G1_BMS_TOPIC`, and
`ROBOT_GUI_G1_SECONDARY_IMU_TOPIC`. Read-only FSM ID is polled from the G1
LocoClient; the currently exposed SDK API does not provide an FSM mode getter,
so the UI reports mode as unknown rather than deriving it from `mode_machine`
(the latter is a model selector).

`ROBOT_GUI_TELEMETRY_HZ` defaults to 10 and is limited to 1–20 Hz. The backend
reports source-specific `fresh`, `delayed`, `offline`, `invalid`, or `unknown`
states using host monotonic receive times. A missing SDK/interface or failed
DDS initialization is reported as a degraded/disconnected status; it does not
fall back to Mock or enable controls. This release is read-only and does not
establish hardware control safety or perform a field acceptance.

## Verify

```powershell
uv run --directory backend pytest
cd frontend
npm test
npm run build
```
