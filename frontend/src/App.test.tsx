import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";

import { App } from "./App";

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

describe("Mock workstation", () => {
  it("clearly identifies Mock mode and keeps real controls disabled", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);

    render(<App />);

    expect(screen.getByText("MOCK · 不连接真实机器人")).toBeInTheDocument();
    expect(screen.getByText(/当前为开发 Mock 模式/)).toBeInTheDocument();
    expect(
      screen.getByText("此版本仅提供只读遥测，尚未开放任何真机控制能力。"),
    ).toBeInTheDocument();

    for (const label of ["FSM 模式", "键盘行走", "手掰示教", "动作回放"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeDisabled();
    }
  });

  it("shows the received Mock source and its non-real connection state", () => {
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
            connection: {
              state: "mock",
              interface: null,
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "Mock 数据源已启动；当前未连接真实机器人或 DDS。",
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
                reason: "mock_mode_control_disabled",
              },
            },
          }),
        }),
      );
    });

    expect(screen.getByText("Mock 数据源")).toBeInTheDocument();
    expect(screen.getByText(/\s12 ms/)).toBeInTheDocument();
    expect(
      screen.getByText("Mock 模式未连接真实机器人"),
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
                reason: "mock_mode_control_disabled",
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
    expect(screen.getByText(/\s512 ms/)).toBeInTheDocument();
    const freshnessRegion = screen.getByRole("region", {
      name: "遥测源新鲜度",
    });
    expect(within(freshnessRegion).getByText("delayed")).toBeInTheDocument();
    expect(within(freshnessRegion).getByText("年龄：512 ms")).toBeInTheDocument();
    expect(screen.getByText("数据延迟")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(within(freshnessRegion).getByText("offline")).toBeInTheDocument();
    expect(screen.getByText("未连接")).toBeInTheDocument();
  });

  it("renders all semantic joint rows and auxiliary telemetry from the schema", () => {
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
            mode: "mock",
            connection: {
              state: "mock",
              interface: null,
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "Mock telemetry.",
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
    expect(screen.getByText(/SOC\s*92\s*%.*SOH\s*100\s*%/)).toBeInTheDocument();
    expect(screen.getByText("ID 500 · Mode unknown")).toBeInTheDocument();
  });
});
