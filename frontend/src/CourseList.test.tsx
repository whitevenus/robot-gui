import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactElement } from "react";

import { ConfirmProvider } from "./ConfirmDialog";
import { CourseDetail } from "./CourseDetail";
import { CourseList } from "./CourseList";

const COURSES = [
  {
    id: "11111111-1111-4111-8111-111111111111",
    name: "第一课",
    actions: ["seg-a", "seg-b"],
    segments: [
      {
        name: "seg-a",
        text: "第一段讲解",
        duration_s: 11.9,
        frames: 357,
        has_audio: true,
      },
      {
        name: "seg-b",
        text: "第二段讲解",
        duration_s: 7.2,
        frames: 216,
        has_audio: false,
      },
    ],
    duration_s: 19.1,
    created_at: "2026-10-22T12:00:00+00:00",
    source: { kind: "courseware-v3" },
  },
];

const CONTROL_STATE = {
  phase: "idle",
  message: "",
  action: "",
  error_code: "",
  available: true,
  trajectory_duration_s: 0,
  audio_playing: false,
  gravity_compensation: true,
  default_speed: 1,
  current_speed: 1,
  resume_actions: [],
};

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

function renderWithConfirm(ui: ReactElement) {
  return render(<ConfirmProvider>{ui}</ConfirmProvider>);
}

/** 确认弹窗弹出来后点「确认」。 */
async function acceptDialog() {
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认" }));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("课程管理", () => {
  it("列出课程并显示动作数与总时长", async () => {
    stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);

    expect(await screen.findByText("第一课")).toBeTruthy();
    expect(screen.getByText("11111111-1111-4111-8111-111111111111")).toBeTruthy();
    expect(screen.getByText("19.1 s")).toBeTruthy();
  });

  it("没有课程时给出空态", async () => {
    stubFetch({
      "/api/v1/courses": { courses: [] },
      "/api/v1/control/state": CONTROL_STATE,
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);

    expect(await screen.findByText(/还没有课程/)).toBeTruthy();
  });

  it("回放整套前先弹确认框，确认后带上课程 id 与确认标记", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/replay": { phase: "planning" },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    fireEvent.click(screen.getByText("回放整套"));
    // 弹窗是页面自己的对话框，不是浏览器原生的。
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("回放整套课程")).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.course).toBe(COURSES[0].id);
      expect(body.confirmed).toBe(true);
    });
  });

  it("在确认框上取消就不发回放请求", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/replay": { phase: "planning" },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    fireEvent.click(screen.getByText("回放整套"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(
      fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay")),
    ).toBeFalsy();
  });

  it("回放成功给出一条说明而不是报错", async () => {
    stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/replay": { phase: "planning" },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    fireEvent.click(screen.getByText("回放整套"));
    await acceptDialog();

    const notice = await screen.findByText("已开始回放「第一课」。");
    expect(notice.className).toContain("notice-info");
  });

  it("只播放语音走 audio 接口，机器人不动", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/audio": { clips: 2, duration_s: 21.4 },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    // 只播语音不动机器人，不需要二次确认。
    fireEvent.click(screen.getByText("只播放语音"));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/audio"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.course).toBe(COURSES[0].id);
    });
    // 没有下发回放请求：只播语音不动机器人。
    expect(
      fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay")),
    ).toBeFalsy();
  });

  it("回放速度只有 0.5 / 0.8 / 1.0 三档，选中的速度随请求发出", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/replay": { phase: "planning" },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    const trigger = screen.getByRole("button", { name: /回放速度/ });
    fireEvent.click(trigger);
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "0.5×",
      "0.8×",
      "1.0×",
    ]);

    fireEvent.click(options[0]);
    expect(screen.getByRole("button", { name: "回放速度 0.5×" })).toBeTruthy();

    fireEvent.click(screen.getByText("回放整套"));
    await acceptDialog();

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.speed).toBe(0.5);
    });
  });

  it("暂停、继续、停止按会话状态启用", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": { ...CONTROL_STATE, phase: "playing", action: "seg-a" },
      "/api/v1/control/pause": { ...CONTROL_STATE, phase: "releasing" },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    const pause = screen.getByRole("button", { name: "暂停" });
    const resume = screen.getByRole("button", { name: /继续播放/ });
    const stop = screen.getByRole("button", { name: "停止" });
    expect(pause).toBeEnabled();
    expect(stop).toBeEnabled();
    expect(resume).toBeDisabled();

    fireEvent.click(pause);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/v1/control/pause")),
      ).toBeTruthy();
    });
  });

  it("只播语音时暂停和停止也可用", async () => {
    stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": {
        ...CONTROL_STATE,
        narration: {
          phase: "playing",
          current: "seg-a",
          index: 1,
          total: 2,
          remaining: ["seg-b"],
        },
      },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    // 语音会话也在跑：暂停和停止必须可用，不能是死的。
    expect(screen.getByRole("button", { name: "暂停" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /继续播放/ })).toBeDisabled();
    expect(screen.getByText("播讲解语音")).toBeInTheDocument();
  });

  it("语音暂停后可以接着播剩下的段", async () => {
    stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": {
        ...CONTROL_STATE,
        narration: {
          phase: "paused",
          current: "seg-a",
          index: 1,
          total: 2,
          remaining: ["seg-b"],
        },
      },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    const resume = screen.getByRole("button", { name: "继续播放剩余 1 段" });
    expect(resume).toBeEnabled();
    expect(screen.getByText("语音已暂停")).toBeInTheDocument();
  });

  it("真机控制未开启时回放按钮保持禁用", async () => {
    stubFetch({
      "/api/v1/courses": { courses: COURSES },
      "/api/v1/control/state": { ...CONTROL_STATE, available: false },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    expect(screen.getByRole("button", { name: "回放整套" })).toBeDisabled();
  });

  it("上传课程包走 import 接口", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: [] },
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/courses/import": { course: COURSES[0] },
    });
    renderWithConfirm(<CourseList onOpen={() => {}} />);
    await screen.findByText(/还没有课程/);

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "courseware.zip", {
      type: "application/zip",
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/courses/import"));
      expect(call).toBeTruthy();
      expect((call?.[1] as RequestInit).method).toBe("POST");
    });
  });

  it("详情页显示每段讲解并能单段回放", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses/11111111-1111-4111-8111-111111111111": COURSES[0],
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/replay": { phase: "planning" },
    });
    renderWithConfirm(
      <CourseDetail
        courseId="11111111-1111-4111-8111-111111111111"
        onBack={() => {}}
      />,
    );

    expect(await screen.findByText("第一段讲解")).toBeTruthy();
    expect(screen.getByText("第二段讲解")).toBeTruthy();

    fireEvent.click(screen.getAllByText("回放")[0]);
    await acceptDialog();

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.action).toBe("seg-a");
      expect(body.confirmed).toBe(true);
    });
  });

  it("详情页能只播一段的讲解语音，没有语音的段落不给按钮", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses/11111111-1111-4111-8111-111111111111": COURSES[0],
      "/api/v1/control/state": CONTROL_STATE,
      "/api/v1/control/audio": { clips: 1, duration_s: 11.9 },
    });
    renderWithConfirm(
      <CourseDetail
        courseId="11111111-1111-4111-8111-111111111111"
        onBack={() => {}}
      />,
    );
    await screen.findByText("第一段讲解");

    const buttons = screen.getAllByRole("button", { name: "只播放语音" });
    expect(buttons).toHaveLength(2);
    // 第二段没有语音，按钮是禁用的。
    expect(buttons[0]).toBeEnabled();
    expect(buttons[1]).toBeDisabled();

    fireEvent.click(buttons[0]);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/audio"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.name).toBe("seg-a");
    });
  });
});
