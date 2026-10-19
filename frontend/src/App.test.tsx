import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";

import { App } from "./App";

vi.mock("./G1Viewer", () => ({
  G1Viewer: ({
    joints,
    hipImuAgeMs,
    hipImuState,
    lowStateAgeMs,
    lowStateState,
    modeMachine,
  }: {
    joints: unknown[];
    hipImuAgeMs: number | null;
    hipImuState: string;
    lowStateAgeMs: number | null;
    lowStateState: string;
    modeMachine: number | null;
  }) => (
    <section
      aria-label="G1 三维姿态模型"
      data-mode-machine={modeMachine ?? "unknown"}
    >
      Pose: {lowStateState}/{hipImuState} · {lowStateAgeMs ?? "—"}/
      {hipImuAgeMs ?? "—"} ms · {joints.length} joints
    </section>
  ),
}));

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  close = vi.fn();

  constructor() {
    MockWebSocket.instances.push(this);
  }
}

afterEach(() => {
  cleanup();
  MockWebSocket.instances = [];
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Real telemetry workstation", () => {
  it("requires real telemetry and keeps hardware controls disabled", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);

    render(<App />);

    expect(screen.getByText("REAL · 等待连接")).toBeInTheDocument();
    expect(screen.getByText(/仅接受真实 Unitree SDK2\/DDS 遥测/)).toBeInTheDocument();
    expect(
      screen.getByText("此版本仅提供只读遥测，尚未开放任何真机控制能力。"),
    ).toBeInTheDocument();

    for (const label of ["FSM 模式", "键盘行走", "手掰示教", "动作回放"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeDisabled();
    }
  });

  it("shows the real SDK2/DDS source and connection state", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            schema_version: "1.0",
            mode: "real",
            connection: {
              state: "connected",
              interface: "eth0",
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "已收到 G1 LowState 遥测。",
            },
            sources: {
              low_state: {
                state: "fresh",
                age_ms: 12,
                fresh_for_ms: 300,
                offline_after_ms: 1500,
              },
            },
            capabilities: {
              controls: {
                available: false,
                reason: "telemetry_release_is_read_only",
              },
            },
          }),
        }),
      );
    });

    expect(screen.getByText("已连接")).toBeInTheDocument();
    expect(
      screen.getByText("Unitree SDK2 / DDS（eth0）"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "连接状态" })).getByText(
        /\s12 ms/,
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("已收到 G1 LowState 遥测。")).toHaveLength(2);
  });

  it("rejects snapshots from a non-real runtime", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            schema_version: "1.0",
            mode: "mock",
            connection: { state: "mock" },
            sources: { low_state: { state: "fresh" } },
          }),
        }),
      );
    });

    expect(screen.getByText("REAL · 等待连接")).toBeInTheDocument();
    expect(
      screen.getByText("遥测 schema、运行模式或必需字段不受支持。"),
    ).toBeInTheDocument();
  });

  it("continues aging the last sample after the telemetry socket disconnects", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            schema_version: "1.0",
            mode: "real",
            connection: {
              state: "connected",
              interface: "eth0",
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "已收到 G1 LowState 遥测。",
            },
            sources: {
              low_state: {
                state: "fresh",
                age_ms: 12,
                fresh_for_ms: 300,
                offline_after_ms: 1500,
              },
            },
            capabilities: {
              controls: {
                available: false,
                reason: "telemetry_release_is_read_only",
              },
            },
          }),
        }),
      );
    });

    act(() => {
      socket.onclose?.();
      vi.advanceTimersByTime(500);
    });

    expect(screen.getByText("WebSocket 未连接")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "连接状态" })).getByText(
        /\s512 ms/,
      ),
    ).toBeInTheDocument();
    const sourceHealth = screen.getByRole("region", {
      name: "数据来源状态",
    });
    expect(within(sourceHealth).getByText("延迟")).toBeInTheDocument();
    expect(within(sourceHealth).getByText("512 ms")).toBeInTheDocument();
    expect(screen.getByText("数据延迟")).toBeInTheDocument();
    expect(
      screen.getByText("Pose: delayed/unknown · 512/— ms · 0 joints"),
    ).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(within(sourceHealth).getByText("离线")).toBeInTheDocument();
    expect(screen.getByText("未连接")).toBeInTheDocument();
    expect(
      screen.getByText("Pose: offline/unknown · 1512/— ms · 0 joints"),
    ).toBeInTheDocument();
  });

  it("renders all semantic joint rows and auxiliary telemetry from the schema", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];
    const joints = Array.from({ length: 29 }, (_, sdk_slot) => ({
      name: `joint_${sdk_slot}`,
      urdf_joint_name: `joint_${sdk_slot}_joint`,
      sdk_slot,
      group: "test_group",
      q_rad: 0.25,
      dq_rad_s: 0.5,
      ddq_rad_s2: 0.75,
      tau_est_nm: 1,
      temperature_raw: [32, 33],
      voltage_v: 48,
      mode: 1,
      status_code: 0,
    }));

    act(() => {
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            schema_version: "1.0",
            mode: "real",
            connection: {
              state: "connected",
              interface: "eth0",
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "Real telemetry.",
            },
            sources: Object.fromEntries(
              ["low_state", "bms", "hip_imu", "torso_imu", "fsm"].map(
                (source) => [
                  source,
                  {
                    state: "fresh",
                    age_ms: 5,
                    fresh_for_ms: 300,
                    offline_after_ms: 1500,
                    error: null,
                  },
                ],
              ),
            ),
            robot: {
              product_id: "g1",
              variant: "29dof",
              joint_count: 29,
              mode_machine: 2,
              joints,
              bms: {
                soc_pct: 92,
                soh_pct: 100,
                current_raw: 0,
                voltage_raw: [50000, 50000, 50000],
                temperature_raw: [25, 25],
                cycle_count: 12,
              },
              hip_imu: { rpy: [0, 0, 0] },
              torso_imu: { rpy: [0, 0, 0] },
              fsm: { id: 500, mode: null },
            },
            capabilities: {
              controls: { available: false, reason: "read_only" },
            },
          }),
        }),
      );
    });

    expect(screen.getAllByRole("row")).toHaveLength(30);
    expect(screen.getByText("joint_0")).toBeInTheDocument();
    expect(screen.getByText("joint_28")).toBeInTheDocument();
    expect(screen.getByText("92")).toBeInTheDocument();
    expect(screen.getByText("SOH 100%")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.getAllByText("0.000, 0.000, 0.000")).toHaveLength(2);
    expect(screen.getByText("模式：unknown")).toBeInTheDocument();
    expect(
      await screen.findByText("Pose: fresh/fresh · 5/5 ms · 29 joints"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "G1 三维姿态模型" }),
    ).toHaveAttribute("data-mode-machine", "2");
  });
});
