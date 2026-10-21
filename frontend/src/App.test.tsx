import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

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
  vi.unstubAllGlobals();
});

const FSM_MODES = [
  {
    fsm_id: 0,
    label: "零力矩",
    danger: true,
    hint: "输出力矩恒为 0，机器人失去支撑，会瘫倒。",
    allowed: true,
  },
  {
    fsm_id: 1,
    label: "阻尼",
    danger: true,
    hint: "只有速度阻尼，没有支撑力矩，同样会瘫倒。",
    allowed: true,
  },
  {
    fsm_id: 4,
    label: "锁定站立",
    danger: false,
    hint: "纯弹簧-阻尼姿态保持，没有平衡控制，姿态偏了不会迈步救场。",
    allowed: true,
  },
  {
    fsm_id: 501,
    label: "常规运控",
    danger: false,
    hint: "内置平衡控制生效。",
    allowed: true,
  },
];

function stubFetch(routes: Record<string, unknown>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    const key = Object.keys(routes).find((route) => url.endsWith(route));
    if (key === undefined) {
      return new Response("not found", { status: 404 });
    }
    return new Response(JSON.stringify(routes[key]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  // 动作工作台与运动模式面板在挂载时就会拉取状态，默认给一个空实现，
  // 避免每个用例都要重复准备 fetch 替身。
  stubFetch({
    "/api/v1/actions": { actions: [] },
    "/api/v1/control/state": {
      phase: "idle",
      message: "",
      action: "",
      error_code: "",
      available: false,
      trajectory_duration_s: 0,
      audio_playing: false,
      gravity_compensation: false,
      default_speed: 1,
      current_speed: 1,
      resume_actions: [],
    },
    "/api/v1/fsm/state": {
      available: true,
      reason: "",
      current: { id: null, name: "" },
      whitelist: [501],
      modes: FSM_MODES,
    },
  });
});

function telemetrySnapshot(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "1.0",
    mode: "real",
    connection: {
      state: "connected",
      interface: "eth0",
      last_data_at: "2026-10-19T07:00:00+00:00",
      error: null,
      message: "已收到 G1 LowState 遥测。",
    },
    sources: Object.fromEntries(
      ["low_state", "bms", "hip_imu", "torso_imu", "fsm"].map((source) => [
        source,
        {
          state: "fresh",
          age_ms: 12,
          fresh_for_ms: 300,
          offline_after_ms: 1500,
          error: null,
        },
      ]),
    ),
    robot: {
      product_id: "g1",
      variant: "29dof",
      joint_count: 29,
      mode_machine: 5,
      joints: [],
      bms: {
        soc_pct: 92,
        soh_pct: 100,
        current_raw: 0,
        voltage_raw: [50000, 50000, 50000],
        temperature_raw: [25, 25],
        cycle_count: 12,
      },
      hip_imu: { rpy: [0.001, 0.002, 0.003] },
      torso_imu: { rpy: [0.004, 0.005, 0.006] },
      fsm: { id: 501, mode: null },
    },
    capabilities: {
      controls: { available: false, reason: "read_only" },
    },
    ...overrides,
  };
}

describe("Real telemetry workstation", () => {
  it("requires real telemetry and keeps unopened hardware controls disabled", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    stubFetch({
      "/api/v1/actions": { actions: [] },
      "/api/v1/fsm/state": {
        available: true,
        reason: "",
        current: { id: null, name: "" },
        whitelist: [501],
        modes: FSM_MODES,
      },
    });

    render(<App />);

    expect(screen.getByText("REAL · 等待连接")).toBeInTheDocument();
    expect(
      screen.getByText(/以下能力本版仍未开放，按钮保持禁用/),
    ).toBeInTheDocument();

    for (const label of ["键盘行走控制", "手掰示教"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeDisabled();
    }
  });

  it("lists stored actions and refuses replay while controls are disabled", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    stubFetch({
      "/api/v1/actions": {
        actions: [
          {
            name: "p1_s0",
            group: "arms",
            frames: 98,
            duration_s: 4.9,
            source: "echo-g",
            has_audio: true,
          },
        ],
      },
      "/api/v1/control/state": {
        phase: "idle",
        message: "",
        action: "",
        error_code: "",
        available: false,
        trajectory_duration_s: 0,
        audio_playing: false,
        gravity_compensation: false,
      },
    });

    render(<App />);

    expect(await screen.findByText("p1_s0")).toBeInTheDocument();
    expect(screen.getByText("arms")).toBeInTheDocument();
    expect(
      screen.getByText(/真机控制未开启。设置 ROBOT_GUI_CONTROLS_ENABLED=1/),
    ).toBeInTheDocument();
    const replay = screen.getByRole("button", { name: /确认并回放/ });
    expect(replay).toBeDisabled();
  });

  it("offers playing the whole course and asks the backend for it", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    const fetchMock = stubFetch({
      "/api/v1/actions": {
        actions: [],
        course: { name: "stage_c", actions: ["p1_s0", "p2_s1"] },
      },
      "/api/v1/control/state": {
        phase: "idle",
        message: "",
        action: "",
        error_code: "",
        available: true,
        trajectory_duration_s: 0,
        audio_playing: false,
        gravity_compensation: false,
      },
      "/api/v1/control/replay": { phase: "planning", action: "整套 2 段" },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);

    const courseButton = await screen.findByRole("button", { name: /回放整套/ });
    expect(courseButton).toBeEnabled();

    await act(async () => {
      courseButton.click();
    });

    const call = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/v1/control/replay"),
    );
    expect(call).toBeDefined();
    const init = call?.[1] as RequestInit | undefined;
    // 速度由页面上的下拉给出，不再靠环境变量。
    expect(JSON.parse(String(init?.body))).toEqual({
      course: true,
      confirmed: true,
      speed: 1,
    });
  });

  it("sends the speed picked in the dropdown with the replay request", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    const fetchMock = stubFetch({
      "/api/v1/actions": {
        actions: [
          {
            name: "p1_s0",
            group: "arms",
            frames: 98,
            duration_s: 4.9,
            source: "echo-g",
            has_audio: true,
          },
        ],
        course: { name: "stage_c", actions: ["p1_s0", "p2_s1"] },
      },
      "/api/v1/control/state": {
        phase: "idle",
        message: "",
        action: "",
        error_code: "",
        available: true,
        trajectory_duration_s: 0,
        audio_playing: false,
        gravity_compensation: false,
        default_speed: 1,
        current_speed: 1,
        resume_actions: [],
      },
      "/api/v1/control/replay": { phase: "planning", action: "整套 2 段" },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);

    const speedSelect = await screen.findByLabelText("回放速度");
    await act(async () => {
      fireEvent.change(speedSelect, { target: { value: "0.8" } });
    });

    const courseButton = screen.getByRole("button", { name: /回放整套/ });
    await act(async () => {
      courseButton.click();
    });

    const call = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/v1/control/replay"),
    );
    const init = call?.[1] as RequestInit | undefined;
    expect(JSON.parse(String(init?.body)).speed).toBe(0.8);
  });

  it("lists execution records and opens one on click", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    stubFetch({
      "/api/v1/actions": { actions: [] },
      "/api/v1/records": {
        records: [
          {
            id: "20261021T091240Z-p1_s2",
            kind: "action",
            label: "p1_s2",
            actions: ["p1_s2"],
            speed: 0.8,
            started_at: "2026-10-21T09:12:40+00:00",
            finished_at: "2026-10-21T09:12:52+00:00",
            elapsed_s: 12.3,
            trajectory_duration_s: 8.9,
            outcome: "completed",
            error_code: "",
            waist_pitch_lean_deg: 4.02,
            max_drift_deg: 0.31,
            violated: false,
            sample_count: 232,
          },
        ],
      },
      "/api/v1/records/20261021T091240Z-p1_s2": {
        id: "20261021T091240Z-p1_s2",
        kind: "action",
        label: "p1_s2",
        actions: ["p1_s2"],
        speed: 0.8,
        started_at: "2026-10-21T09:12:40+00:00",
        finished_at: "2026-10-21T09:12:52+00:00",
        elapsed_s: 12.3,
        trajectory_duration_s: 8.9,
        outcome: "completed",
        error_code: "",
        error_message: "",
        segments: [{ name: "p1_s2", start_s: 2 }],
        tracking: {
          samples: 100,
          joints: {
            left_shoulder_pitch: {
              max_error_rad: 0.146,
              mean_error_rad: 0.02,
              target_rad: 0.1,
              measured_rad: 0.05,
            },
          },
        },
        lower_body: {
          threshold_deg: 5,
          max_drift_deg: 0.31,
          max_drift_joint: "left_hip_pitch",
          waist_pitch_lean_deg: 4.02,
          waist_pitch_drift_deg: 5.43,
          violated: false,
        },
        samples: [
          { t: 0, waist_pitch: -0.02, waist_modes: [1, 1, 1], torso_pitch: 0.01, hip_pitch: 0.02 },
          { t: 0.5, waist_pitch: 0.01, waist_modes: [1, 1, 1], torso_pitch: 0.02, hip_pitch: 0.02 },
        ],
      },
    });

    render(<App />);

    await act(async () => {
      screen.getByRole("button", { name: /执行轨迹记录/ }).click();
    });

    const row = await screen.findByText("p1_s2");
    expect(row).toBeInTheDocument();

    await act(async () => {
      row.click();
    });

    const detail = await screen.findByRole("region", { name: "执行记录详情" });
    expect(within(detail).getByText("20261021T091240Z-p1_s2")).toBeInTheDocument();
    expect(within(detail).getByText(/腰 14 前倾峰值/)).toBeInTheDocument();
    expect(
      within(detail).getByRole("link", { name: /下载 JSON/ }),
    ).toHaveAttribute(
      "href",
      "/api/v1/records/20261021T091240Z-p1_s2/download",
    );
  });

  it("shows the real SDK2/DDS source and connection state", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify(telemetrySnapshot()),
        }),
      );
    });

    expect(screen.getByText("已连接")).toBeInTheDocument();
    expect(screen.getByText("Unitree SDK2 / eth0")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "连接状态" })).getByText(/12 ms/),
    ).toBeInTheDocument();
    expect(screen.getByText("已收到 G1 LowState 遥测。")).toBeInTheDocument();
    expect(screen.getByText(/WebSocket 已连接/)).toBeInTheDocument();
  });

  it("translates the FSM id and mode name into a readable label", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify(telemetrySnapshot()),
        }),
      );
    });

    // 机器人自报模式名 "ai" → 页面显示「常规运控」并带上 FSM ID。
    const strip = screen.getByRole("region", { name: "连接状态" });
    expect(await within(strip).findByText("常规运控")).toBeInTheDocument();
    expect(within(strip).getByText("FSM 501")).toBeInTheDocument();
    expect(within(strip).getByText("白名单允许回放")).toBeInTheDocument();
  });

  it("offers every catalogued mode without extra configuration", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);

    // 未配置 ROBOT_GUI_FSM_MODES 时，目录里的模式全部可点。
    for (const label of ["零力矩", "阻尼", "锁定站立", "常规运控"]) {
      expect(
        await screen.findByRole("button", { name: new RegExp(`^${label}`) }),
      ).toBeEnabled();
    }
  });

  it("disables mode switching and says why when hardware controls are off", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    stubFetch({
      "/api/v1/actions": { actions: [] },
      "/api/v1/fsm/state": {
        available: false,
        reason: "真机控制未开启（ROBOT_GUI_CONTROLS_ENABLED=1）前不允许切换运动模式。",
        current: { id: null, name: "" },
        whitelist: [501],
        modes: FSM_MODES.map((mode) => ({ ...mode, allowed: false })),
      },
    });

    render(<App />);

    expect(
      await screen.findByText(/前不允许切换运动模式/),
    ).toBeInTheDocument();
    for (const label of ["零力矩", "阻尼", "锁定站立", "常规运控"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`^${label}`) }),
      ).toBeDisabled();
    }
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
          data: JSON.stringify(telemetrySnapshot()),
        }),
      );
    });

    act(() => {
      socket.onclose?.();
      vi.advanceTimersByTime(500);
    });

    expect(screen.getByText(/WebSocket 未连接/)).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "连接状态" })).getByText(
        /512 ms/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("数据延迟")).toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "遥测摘要" });
    expect(within(summary).getAllByText("延迟").length).toBeGreaterThan(0);
    expect(within(summary).getAllByText("512 ms").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Pose: delayed/delayed · 512/512 ms · 0 joints"),
    ).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(within(summary).getAllByText("离线").length).toBeGreaterThan(0);
    expect(screen.getByText("未连接")).toBeInTheDocument();
    expect(
      screen.getByText("Pose: offline/offline · 1512/1512 ms · 0 joints"),
    ).toBeInTheDocument();
  });

  it("renders joint rows grouped by body part and only shows the upper body by default", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];
    const groups = [
      ...Array.from({ length: 12 }, () => "leg"),
      ...Array.from({ length: 3 }, () => "waist"),
      ...Array.from({ length: 14 }, () => "arm"),
    ];
    const joints = groups.map((group, sdk_slot) => ({
      name: `joint_${sdk_slot}`,
      urdf_joint_name: `joint_${sdk_slot}_joint`,
      sdk_slot,
      group:
        group === "leg"
          ? sdk_slot < 6
            ? "left_leg"
            : "right_leg"
          : group === "waist"
            ? "waist"
            : sdk_slot < 18
              ? "left_arm"
              : "right_arm",
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
          data: JSON.stringify(
            telemetrySnapshot({
              robot: {
                ...telemetrySnapshot().robot,
                joints,
              },
            }),
          ),
        }),
      );
    });

    const jointRegion = screen.getByRole("region", {
      name: "G1 29DoF 关节遥测",
    });
    // 默认只看上肢：腰 3 + 双臂 14 = 17 行，加上 3 个分组表头和 1 行折叠的腿。
    expect(within(jointRegion).getAllByRole("row")).toHaveLength(1 + 3 + 17 + 1);
    expect(within(jointRegion).getByText("腰部")).toBeInTheDocument();
    expect(within(jointRegion).getByText("左臂")).toBeInTheDocument();
    expect(within(jointRegion).getByText("右臂")).toBeInTheDocument();
    expect(within(jointRegion).queryByText("joint_0")).not.toBeInTheDocument();
    expect(
      within(jointRegion).getByText(/左腿 \/ 右腿 · 12 关节已折叠/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("另有 12 个腿部关节已折叠，点「只看上肢」展开。"),
    ).toBeInTheDocument();

    // 切到「全部」后腿部行出现。
    act(() => {
      screen.getByRole("button", { name: /只看上肢/ }).click();
    });
    expect(within(jointRegion).getByText("joint_0")).toBeInTheDocument();
    expect(within(jointRegion).getAllByRole("row")).toHaveLength(1 + 5 + 29);
  });

  it("renders auxiliary telemetry with readable vectors and drops raw noise", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    render(<App />);
    const socket = MockWebSocket.instances[0];

    act(() => {
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify(telemetrySnapshot()),
        }),
      );
    });

    const summary = screen.getByRole("region", { name: "遥测摘要" });
    expect(within(summary).getByText(/92% SOC · SOH 100%/)).toBeInTheDocument();
    expect(within(summary).getByText("R 0.001 / P 0.002 / Y 0.003")).toBeInTheDocument();
    expect(within(summary).getByText("R 0.004 / P 0.005 / Y 0.006")).toBeInTheDocument();
    // 原始计数（电压 / 电流）不再直接露出。
    expect(screen.queryByText(/50000/)).not.toBeInTheDocument();
    expect(
      await screen.findByText("Pose: fresh/fresh · 12/12 ms · 0 joints"),
    ).toBeInTheDocument();
  });
});
