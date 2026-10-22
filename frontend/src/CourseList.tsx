/** 课程管理：上传课程包、列出各门课、整套回放入口。 */
import { useCallback, useEffect, useRef, useState } from "react";

import { readError } from "./api";

export interface CourseSegment {
  name: string;
  text: string;
  duration_s: number;
  frames: number;
  has_audio: boolean;
}

export interface Course {
  id: string;
  name: string;
  actions: string[];
  segments: CourseSegment[];
  duration_s: number;
  created_at: string;
  source: Record<string, unknown>;
}

const SPEED_OPTIONS = [0.8, 1, 1.2];

interface CourseListProps {
  onOpen: (courseId: string) => void;
}

export function CourseList({ onOpen }: CourseListProps) {
  const [courses, setCourses] = useState<Course[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [speed, setSpeed] = useState(1);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/courses");
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      const body = await response.json();
      setCourses(Array.isArray(body.courses) ? body.courses : []);
    } catch (error) {
      setMessage(String(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function upload(file: File) {
    setBusy(true);
    setMessage("");
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const response = await fetch("/api/v1/courses/import", { method: "POST", body: form });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      const body = await response.json();
      setMessage(`已导入课程「${body.course?.name ?? file.name}」。`);
      await refresh();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function replayWhole(course: Course) {
    if (
      !window.confirm(
        `即将在真机上连续回放整套课程「${course.name}」（${course.actions.length} 段，速度 ${speed}×）。`
          + "整套是一次连续接管，中途需要停止请按停止按钮或遥控器。请确认现场安全、"
          + "机器人处于允许的 FSM 模式。",
      )
    ) {
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/v1/control/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ course: course.id, confirmed: true, speed }),
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      setMessage(`已开始回放「${course.name}」。`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function remove(course: Course) {
    if (!window.confirm(`删除课程「${course.name}」？动作库是共享的，不会被删除。`)) {
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/v1/courses/${encodeURIComponent(course.id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refresh();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card records-card">
      <div className="card-head">
        <div>
          <p className="eyebrow">Course Library</p>
          <h2>课程管理</h2>
        </div>
        <div className="card-head-actions">
          <label className="speed-picker">
            <span>回放速度</span>
            <select value={String(speed)} onChange={(event) => setSpeed(Number(event.target.value))}>
              {SPEED_OPTIONS.map((option) => (
                <option key={option} value={String(option)}>
                  {option}×
                </option>
              ))}
            </select>
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void upload(file);
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
          >
            上传课程包
          </button>
        </div>
      </div>
      <div className="card-body">
        {message ? <p className="rail-error">{message}</p> : null}
        {courses.length === 0 ? (
          <p className="empty-cell">
            还没有课程。上传一个 courseware-v3 课程包（robocourse 导出），导入后就能整套回放。
          </p>
        ) : (
          <table className="course-table">
            <thead>
              <tr>
                <th>课程</th>
                <th>动作数</th>
                <th>总时长</th>
                <th>创建时间</th>
                <th>来源</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {courses.map((course) => (
                <tr key={course.id}>
                  <td>
                    <strong>{course.name}</strong>
                    <div className="meta">{course.id}</div>
                  </td>
                  <td className="num">{course.actions.length}</td>
                  <td className="num">{course.duration_s.toFixed(1)} s</td>
                  <td className="meta">{course.created_at || "—"}</td>
                  <td className="meta">{String(course.source?.kind ?? "—")}</td>
                  <td className="course-actions">
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={busy}
                      onClick={() => void replayWhole(course)}
                    >
                      回放整套
                    </button>
                    <button type="button" className="link-button" onClick={() => onOpen(course.id)}>
                      详情
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      disabled={busy}
                      onClick={() => void remove(course)}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
