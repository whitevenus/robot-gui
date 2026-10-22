/** 课程管理：上传课程包、列出各门课、整套回放与只播讲解语音。 */
import { useCallback, useEffect, useRef, useState } from "react";

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

interface CourseListProps {
  onOpen: (courseId: string) => void;
}

export function CourseList({ onOpen }: CourseListProps) {
  const [courses, setCourses] = useState<Course[]>([]);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();
  const session = useControlSession();
  const { speed, setSpeed } = useReplaySpeed(session.state?.default_speed ?? null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const controlsAvailable = session.state?.available === true;

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/courses");
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      const body = await response.json();
      setCourses(Array.isArray(body.courses) ? body.courses : []);
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function upload(file: File) {
    setBusy(true);
    setNotice(null);
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const response = await fetch("/api/v1/courses/import", { method: "POST", body: form });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      const body = await response.json();
      setNotice({ tone: "info", text: `已导入课程「${body.course?.name ?? file.name}」。` });
      await refresh();
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    } finally {
      setBusy(false);
    }
  }

  async function replayWhole(course: Course) {
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

  /** 只播讲解语音：机器人不动，不用二次确认（也不会下发任何关节指令）。 */
  async function playNarration(course: Course) {
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch("/api/v1/control/audio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ course: course.id }),
      });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      const body = await response.json();
      setNotice({
        tone: "info",
        text: `已开始播放「${course.name}」的讲解语音（${body.clips} 段，约 ${body.duration_s} 秒）。`,
      });
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
    } finally {
      setBusy(false);
    }
  }

  async function remove(course: Course) {
    const accepted = await confirm({
      title: "删除课程",
      message: `删除课程「${course.name}」？\n动作库是共享的，不会被删除。`,
      danger: true,
    });
    if (!accepted) return;
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch(`/api/v1/courses/${encodeURIComponent(course.id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        setNotice({ tone: "bad", text: await readError(response) });
        return;
      }
      await refresh();
    } catch (error) {
      setNotice({ tone: "bad", text: String(error) });
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
          <SpeedPicker
            value={speed}
            onChange={setSpeed}
            disabled={session.busy}
          />
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
      <div className="card-toolbar">
        <SessionState state={session.state} />
        <SessionButtons session={session} onError={(text) => setNotice({ tone: "bad", text })} />
      </div>
      {notice ? <p className={`notice notice-${notice.tone}`}>{notice.text}</p> : null}
      <div className="card-body">
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
                      disabled={busy || !controlsAvailable}
                      title={controlsAvailable ? undefined : "真机控制未开启"}
                      onClick={() => void replayWhole(course)}
                    >
                      回放整套
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busy}
                      onClick={() => void playNarration(course)}
                    >
                      只播放语音
                    </button>
                    <button type="button" className="btn btn-ghost" onClick={() => onOpen(course.id)}>
                      详情
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-danger"
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
