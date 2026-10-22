/** 课程详情：逐段看讲解与动作，单段回放或整套回放。 */
import { useCallback, useEffect, useState } from "react";

import { readError } from "./api";
import { useConfirm } from "./ConfirmDialog";
import {
  SessionButtons,
  SessionState,
  SpeedPicker,
  useControlSession,
  useReplaySpeed,
  type Notice,
} from "./CourseControls";
import type { Course } from "./CourseList";

interface CourseDetailProps {
  courseId: string;
  onBack: () => void;
}

export function CourseDetail({ courseId, onBack }: CourseDetailProps) {
  const [course, setCourse] = useState<Course | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();
  const session = useControlSession();
  const { speed, setSpeed } = useReplaySpeed(session.state?.default_speed ?? null);
  const controlsAvailable = session.state?.available === true;

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`/api/v1/courses/${encodeURIComponent(courseId)}`);
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        setCourse(null);
        return;
      }
      setCourse(await response.json());
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    }
  }, [courseId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function replaySegment(name: string) {
    const accepted = await confirm({
      title: "回放这一段",
      message:
        `即将在真机上回放 ${name}（速度 ${speed}×）。\n\n` +
        "请确认现场安全、机器人处于允许的 FSM 模式。",
    });
    if (!accepted) return;
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch("/api/v1/control/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: name, confirmed: true, speed }),
      });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      setNotice({ tone: "info", text: `已开始回放 ${name}。` });
      await session.refresh();
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    } finally {
      setBusy(false);
    }
  }

  /** 只播这一段的讲解语音：机器人不动，用来单独检查声音。 */
  async function playNarration(name: string) {
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch("/api/v1/control/audio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      const body = await response.json();
      setNotice({
        tone: "info",
        text: `已开始播放 ${name} 的讲解语音（约 ${body.duration_s} 秒），机器人没有动。`,
      });
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    } finally {
      setBusy(false);
    }
  }

  async function replayWhole() {
    if (!course) return;
    const accepted = await confirm({
      title: "回放整套课程",
      message:
        `即将在真机上连续回放整套课程「${course.name}」（${course.actions.length} 段，速度 ${speed}×）。\n` +
        "整套是一次连续接管，中途需要停止请按停止按钮或遥控器。\n\n" +
        "请确认现场安全、机器人处于允许的 FSM 模式。",
    });
    if (!accepted) return;
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch("/api/v1/control/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ course: course.id, confirmed: true, speed }),
      });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      setNotice({ tone: "info", text: `已开始回放「${course.name}」。` });
      await session.refresh();
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
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
          {notice ? (
            <p className={`notice notice-${notice.tone}`}>{notice.text}</p>
          ) : (
            <p className="empty-cell">正在读取课程…</p>
          )}
        </div>
      </section>
    );
  }

  const paused = session.state?.phase === "paused";
  const remaining = session.state?.resume_actions?.length ?? 0;

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
          <SpeedPicker value={speed} onChange={setSpeed} disabled={session.busy} />
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            返回课程列表
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !controlsAvailable}
            title={controlsAvailable ? undefined : "真机控制未开启"}
            onClick={() => void replayWhole()}
          >
            回放整套
          </button>
        </div>
      </div>
      <div className="card-toolbar">
        <SessionState state={session.state} />
        <SessionButtons session={session} onError={(text) => setNotice({ tone: "bad", text })} />
      </div>
      {paused ? (
        <p className="notice notice-info">
          已暂停：机器人已动态回到接管瞬间的姿态，控制权已交还。继续播放会从当前实测姿态
          重新规划，
          {remaining > 0 ? `接着播 ${session.state?.resume_actions.join("、")}` : "重播这一段"}。
        </p>
      ) : notice ? (
        <p className={`notice notice-${notice.tone}`}>{notice.text}</p>
      ) : null}
      <div className="card-body">
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
                    disabled={busy || !controlsAvailable}
                    title={controlsAvailable ? undefined : "真机控制未开启"}
                    onClick={() => void replaySegment(segment.name)}
                  >
                    回放
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={busy || !segment.has_audio}
                    title={segment.has_audio ? undefined : "这一段没有语音"}
                    onClick={() => void playNarration(segment.name)}
                  >
                    只播放语音
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
