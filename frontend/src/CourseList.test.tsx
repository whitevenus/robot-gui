import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("课程管理", () => {
  it("列出课程并显示动作数与总时长", async () => {
    stubFetch({ "/api/v1/courses": { courses: COURSES } });
    render(<CourseList onOpen={() => {}} />);

    expect(await screen.findByText("第一课")).toBeTruthy();
    expect(screen.getByText("11111111-1111-4111-8111-111111111111")).toBeTruthy();
    expect(screen.getByText("19.1 s")).toBeTruthy();
  });

  it("没有课程时给出空态", async () => {
    stubFetch({ "/api/v1/courses": { courses: [] } });
    render(<CourseList onOpen={() => {}} />);

    expect(await screen.findByText(/还没有课程/)).toBeTruthy();
  });

  it("回放整套带上课程 id 与确认标记", async () => {
    const fetchMock = stubFetch({ "/api/v1/courses": { courses: COURSES } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<CourseList onOpen={() => {}} />);
    await screen.findByText("第一课");

    fireEvent.click(screen.getByText("回放整套"));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.course).toBe(COURSES[0].id);
      expect(body.confirmed).toBe(true);
    });
  });

  it("上传课程包走 import 接口", async () => {
    const fetchMock = stubFetch({
      "/api/v1/courses": { courses: [] },
      "/api/v1/courses/import": { course: COURSES[0] },
    });
    render(<CourseList onOpen={() => {}} />);
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
    const fetchMock = stubFetch({ "/api/v1/courses/11111111-1111-4111-8111-111111111111": COURSES[0] });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <CourseDetail
        courseId="11111111-1111-4111-8111-111111111111"
        onBack={() => {}}
      />,
    );

    expect(await screen.findByText("第一段讲解")).toBeTruthy();
    expect(screen.getByText("第二段讲解")).toBeTruthy();

    fireEvent.click(screen.getAllByText("回放")[0]);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/control/replay"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call?.[1] as RequestInit).body));
      expect(body.action).toBe("seg-a");
      expect(body.confirmed).toBe(true);
    });
  });
});
