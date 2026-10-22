/** 课程详情：逐段看讲解与动作，单段回放或整套回放。 */
import { useCallback, useEffect, useState } from "react";

import { readError } from "./api";
import type { Course } from "./CourseList";

const SPEED_OPTIONS = [0.8, 1, 1.2];

interface CourseDetailProps {
  courseId: string;
  onBack: () => void;
}

export function CourseDetail({ courseId, onBack }: CourseDetailProps) {
  const [course, setCourse] = useState<Course | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [speed, setSpeed] = useState(1);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`/api/v1/courses/${encodeURIComponent(courseId)}`);
      if (!response.ok) {
        setMessage(await readError(response));
        setCourse(null);
        return;
      }
      setCourse(await response.json());
    } catch (error) {
      setMessage(String(error));
    }
  }, [courseId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function replaySegment(name: string) {
    if (
      !window.confirm(
        `即将在真机上回放 ${name}（速度 ${speed}×）。请确认现场安全、机器人处于允许的 FSM 模式。`,
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
        body: JSON.stringify({ action: name, confirmed: true, speed }),
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      setMessage(`已开始回放 ${name}。`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function replayWhole() {
    if (!course) return;
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

  if (!course) {
    return (
      <section className="card records-card">
        <div className="card-head">
          <div>
            <p className="eyebrow">Course Detail</p>
            <h2>课程详情</h2>
          </div>
          <div className="card-head-actions">
            <button type="button" className="btn btn-ghost" onClick={onBack}>
              返回课程列表
            </button>
          </div>
        </div>
        <div className="card-body">
          {message ? <p className="rail-error">{message}</p> : <p className="empty-cell">正在读取课程…</p>}
        </div>
      </section>
    );
  }

  return (
    <section className="card records-card">
      <div className="card-head">
        <div>
          <p className="eyebrow">Course Detail</p>
          <h2>{course.name}</h2>
          <p className="meta">
            {course.id} · {course.actions.length} 段 · {course.duration_s.toFixed(1)} s
            {course.created_at ? ` · ${course.created_at}` : ""}
          </p>
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
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            返回课程列表
          </button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void replayWhole()}>
            回放整套
          </button>
        </div>
      </div>
      <div className="card-body">
        {message ? <p className="rail-error">{message}</p> : null}
        <table className="course-table">
          <thead>
            <tr>
              <th>段</th>
              <th>动作名</th>
              <th>讲解</th>
              <th>时长</th>
              <th>帧数</th>
              <th>语音</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {course.segments.map((segment, index) => (
              <tr key={segment.name}>
                <td className="num">{index + 1}</td>
                <td className="meta">{segment.name}</td>
                <td>{segment.text || "—"}</td>
                <td className="num">{segment.duration_s.toFixed(1)} s</td>
                <td className="num">{segment.frames}</td>
                <td>
                  <span className={`tag${segment.has_audio ? " teal" : ""}`}>
                    {segment.has_audio ? "有" : "无"}
                  </span>
                </td>
                <td className="course-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={busy}
                    onClick={() => void replaySegment(segment.name)}
                  >
                    回放
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
