import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

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
            sources: { low_state: { state: "mock", age_ms: 12 } },
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
      screen.getByText("未连接；此版本不会初始化真实 SDK"),
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
            mode: "mock",
            connection: {
              state: "mock",
              interface: null,
              last_data_at: "2026-10-19T07:00:00+00:00",
              error: null,
              message: "Mock 数据源已启动；当前未连接真实机器人或 DDS。",
            },
            sources: { low_state: { state: "mock", age_ms: 12 } },
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
  });
});
