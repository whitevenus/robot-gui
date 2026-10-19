# Robot GUI

Robot GUI is a development workstation for Unitree robot telemetry and motion
workflows. The application is **real-only and telemetry-only**: it requires a
configured Unitree SDK2/DDS connection and never generates Mock/Fake runtime
telemetry. Hardware controls remain disabled.

## Run against a real G1

Requirements: Python 3.11+, Node.js 20+, Unitree SDK2 Python, CycloneDDS, and
access to a G1 on the selected DDS network interface.

Install backend dependencies and configure the DDS interface before starting
the backend:

```powershell
uv sync --directory backend --extra test
$env:ROBOT_GUI_MODE = "real"
$env:ROBOT_GUI_DDS_INTERFACE = "eth0"
uv run --directory backend uvicorn robot_gui.main:app --host 127.0.0.1
```

If the mode, SDK2 package, DDS interface, or required telemetry initialization
is unavailable, backend startup fails with an explicit error; it does not fall
back to simulated data. In a second terminal, start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite. The page reports the actual SDK2/DDS connection
and received source freshness. Hardware control remains unavailable and the
backend rejects all control-session requests.

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
DDS initialization prevents startup; it does not fall back to Fake or enable
controls. This release is read-only and does not
establish hardware control safety or perform a field acceptance.

The 3D viewer bundles Unitree's official G1 29DoF rev 1.0 URDF and all 35
referenced STL meshes from
[`unitreerobotics/unitree_ros`](https://github.com/unitreerobotics/unitree_ros/tree/7c40519e02d7dd16c06b25fe3fa3b67fdeb7cd74/robots/g1_description),
at revision `7c40519e02d7dd16c06b25fe3fa3b67fdeb7cd74`. The assets are served
locally with no external runtime model requests. Their BSD 3-Clause terms and
copyright attribution are included in
[`frontend/public/models/unitree-g1/LICENSE`](frontend/public/models/unitree-g1/LICENSE).
This model corresponds to `mode_machine` 5; confirm that the deployed robot
matches the model before relying on its visual representation.

The viewer shows measured joint/IMU telemetry only, supports camera rotation,
zoom, and reset, and does not simulate dynamics, prove balance, or provide a
hardware safety acceptance. If model mapping or source freshness is
incomplete, the viewer reports the reason while the joint table remains
available. Test-only telemetry fixtures are not available as an application
runtime mode.

## Verify

```powershell
uv run --directory backend pytest
cd frontend
npm test
npm run build
```
