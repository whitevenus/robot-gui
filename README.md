# Robot GUI

Robot GUI is a development workstation for Unitree robot telemetry and motion
workflows. The current application release is **Mock-only**: it does not
connect to a robot or initialize the Unitree SDK, and hardware controls are
disabled.

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

## Verify

```powershell
uv run --directory backend pytest
cd frontend
npm test
npm run build
```
