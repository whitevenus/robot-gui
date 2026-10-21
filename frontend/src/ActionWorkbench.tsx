import { useCallback, useEffect, useMemo, useState } from "react";

import { readError } from "./api";

interface ActionSummary {
  name: string;
  group: string;
  frames: number;
  duration_s: number;
  source: string;
  has_audio: boolean;
}

interface Course {
  name: string;
  actions: string[];
}

interface ControlState {
  phase: string;
  message: string;
  action: string;
  error_code: string;
  available: boolean;
  trajectory_duration_s: number;
  audio_playing: boolean;
  gravity_compensation: boolean;
  default_speed: number;
  current_speed: number;
  resume_actions: string[];
}

const PHASE_LABELS: Record<string, string> = {
  idle: "空闲",
  planning: "规划中",
  acquiring: "接管中",
  playing: "回放中",
  paused: "已暂停",
  releasing: "交还控制权",
  fault: "故障",
};

/** 速度下拉的档位。1.0 是原速，往下是慢放。 */
const SPEED_OPTIONS = [0.5, 0.6, 0.8, 1.0, 1.2, 1.5];

export function ActionWorkbench() {
  const [actions, setActions] = useState<ActionSummary[]>([]);
  const [course, setCourse] = useState<Course | null>(null);
  const [state, setState] = useState<ControlState | null>(null);
  const [message, setMessage] = useState("");
  const [importName, setImportName] = useState("");
  const [importBody, setImportBody] = useState("");
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [speed, setSpeed] = useState<number | null>(null);

  const refreshActions = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/actions");
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      const body = await response.json();
      setActions(body.actions ?? []);
      setCourse(body.course ?? null);
    } catch (error) {
      setMessage(`动作库不可达：${String(error)}`);
    }
  }, []);

  const refreshState = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/control/state");
      if (response.ok) {
        setState(await response.json());
      }
    } catch {
      // 控制状态不可达时保留上一次快照，不伪造空闲。
    }
  }, []);

  useEffect(() => {
    void refreshActions();
    void refreshState();
    const timer = window.setInterval(() => {
      void refreshState();
    }, 500);
    return () => window.clearInterval(timer);
  }, [refreshActions, refreshState]);

  const running = useMemo(
    () =>
      state !== null &&
      ["planning", "acquiring", "playing", "releasing"].includes(state.phase),
    [state],
  );
  const paused = state?.phase === "paused";
  // 速度默认取后端配置的默认值（ROBOT_GUI_REPLAY_SPEED），页面上随时能改。
  const effectiveSpeed = speed ?? state?.default_speed ?? 1;
  const speedOptions = useMemo(() => {
    const options = new Set(SPEED_OPTIONS);
    options.add(Number((state?.default_speed ?? 1).toFixed(2)));
    return [...options].sort((first, second) => first - second);
  }, [state?.default_speed]);

  async function runImport() {
    setBusy(true);
    setMessage("");
    try {
      const action = JSON.parse(importBody);
      const response = await fetch("/api/v1/actions/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: importName.trim(), action }),
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      setMessage(`已导入动作 ${importName.trim()}`);
      setImportName("");
      setImportBody("");
      await refreshActions();
    } catch (error) {
      setMessage(`动作 JSON 解析失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function runDelete(name: string) {
    if (!window.confirm(`确认删除动作 ${name}？`)) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/v1/actions/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      setMessage(`已删除动作 ${name}`);
      if (selected === name) setSelected("");
      await refreshActions();
    } finally {
      setBusy(false);
    }
  }

  async function runReplay(name: string, withAudio: boolean) {
    if (
      !window.confirm(
        `即将在真机上回放 ${name}（速度 ${effectiveSpeed}×）。请确认现场安全、机器人处于允许的 FSM 模式，且随时可以按停止。`,
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
        body: JSON.stringify({
          action: name,
          confirmed: true,
          speed: effectiveSpeed,
          ...(withAudio ? { audio: name } : {}),
        }),
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  async function runCourse() {
    if (!course) return;
    if (
      !window.confirm(
        `即将在真机上连续回放整套课程「${course.name}」（${course.actions.length} 段，速度 ${effectiveSpeed}×）。`
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
        body: JSON.stringify({
          course: true,
          confirmed: true,
          speed: effectiveSpeed,
        }),
      });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  async function runPause() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/v1/control/pause", { method: "POST" });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  async function runResume() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/v1/control/resume", { method: "POST" });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  async function runRelease() {
    setBusy(true);
    try {
      const response = await fetch("/api/v1/control/release", { method: "POST" });
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  const available = state?.available === true;
  const selectedAction = actions.find((item) => item.name === selected) ?? null;

  return (
    <section className="rail-section" id="action-panel" aria-label="动作库与真机回放">
      <div className="rail-section-head">
        <h2>动作库与真机回放</h2>
        <span className="rail-current">
          {state ? PHASE_LABELS[state.phase] ?? state.phase : "读取控制状态"}
          {state?.action ? ` · ${state.action}` : ""}
        </span>
      </div>

      {!available && (
        <p className="rail-reason">
          真机控制未开启。设置 ROBOT_GUI_CONTROLS_ENABLED=1 并确认现场安全后，才会接受回放请求。
        </p>
      )}
      {state?.phase === "fault" && state.error_code && (
        <p className="rail-error">控制会话失败：{state.error_code} {state.message}</p>
      )}
      {message && <p className="rail-error">{message}</p>}

      <div className="act-list">
        {actions.map((action) => {
          const isSelected = action.name === selected;
          return (
            <div
              key={action.name}
              className={`act-row${isSelected ? " selected" : ""}`}
            >
              <button
                type="button"
                className="act-pick"
                onClick={() => setSelected(action.name)}
                aria-pressed={isSelected}
              >
                <strong>{action.name}</strong>
                <span className="act-meta">
                  <span className="act-group">{action.group}</span>
                  {action.frames} 帧 · {action.duration_s.toFixed(1)} s
                </span>
              </button>
              <span className="act-tags">
                {action.has_audio && <span className="tag teal">语音</span>}
                <button
                  type="button"
                  className="link-button"
                  disabled={busy}
                  onClick={() => void runDelete(action.name)}
                >
                  删除
                </button>
                <a href={`/api/v1/actions/${encodeURIComponent(action.name)}`}>导出</a>
              </span>
            </div>
          );
        })}
        {actions.length === 0 && (
          <p className="rail-reason">
            动作库为空。导入 g1-action-studio 兼容的 group+frames JSON，或用 ECHO-G
            转出的上肢轨迹。
          </p>
        )}
      </div>

      <label className="speed-picker">
        <span>回放速度</span>
        <select
          value={String(effectiveSpeed)}
          onChange={(event) => setSpeed(Number(event.target.value))}
          disabled={running || paused}
        >
          {speedOptions.map((option) => (
            <option key={option} value={String(option)}>
              {option}×
            </option>
          ))}
        </select>
      </label>

      {course && (
        <button
          type="button"
          className="btn btn-primary"
          disabled={!available || running || paused || busy}
          onClick={() => void runCourse()}
        >
          确认并回放整套（{course.name} · {course.actions.length} 段）
        </button>
      )}
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!available || running || paused || busy || !selectedAction}
        onClick={() => {
          if (!selectedAction) return;
          void runReplay(selectedAction.name, selectedAction.has_audio);
        }}
      >
        {selectedAction ? `确认并回放 ${selectedAction.name}` : "确认并回放"}
      </button>
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!running || busy}
        onClick={() => void runPause()}
      >
        暂停（回到接管姿态后交还控制权）
      </button>
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!paused || busy}
        onClick={() => void runResume()}
      >
        {paused && (state?.resume_actions?.length ?? 0) > 0
          ? `继续播放剩余 ${state?.resume_actions.length} 段`
          : "继续播放"}
      </button>
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!running}
        onClick={() => void runRelease()}
      >
        停止并交还控制权
      </button>

      {paused && (
        <p className="rail-note">
          已暂停：机器人已动态回到接管瞬间的姿态，控制权已交还。继续播放会从当前
          实测姿态重新规划，
          {(state?.resume_actions?.length ?? 0) > 0
            ? `接着播 ${state?.resume_actions.join("、")}`
            : "重播这一段"}
          。
        </p>
      )}
      {state && state.phase !== "idle" && state.phase !== "paused" && state.message && (
        <p className="rail-note">{state.message}</p>
      )}

      {state && (
        <div className="rail-flags">
          <div>
            <span>重力补偿</span>
            <b>{state.gravity_compensation ? "开" : "关"}</b>
          </div>
          <div>
            <span>执行范围</span>
            <b>双臂 14 维</b>
          </div>
          <div>
            <span>本把速度</span>
            <b>{state.current_speed}×</b>
          </div>
        </div>
      )}

      <details className="rail-details">
        <summary>导入动作 JSON</summary>
        <div className="action-import">
          <label htmlFor="action-name">动作名</label>
          <input
            id="action-name"
            value={importName}
            placeholder="例如 p1_s0"
            onChange={(event) => setImportName(event.target.value)}
          />
          <label htmlFor="action-body">动作 JSON</label>
          <textarea
            id="action-body"
            value={importBody}
            rows={4}
            placeholder='{"group": "arms", "frames": [{"t": 0, "q": [ ... 14 项 ... ]}]}'
            onChange={(event) => setImportBody(event.target.value)}
          />
          <button
            type="button"
            disabled={busy || !importName.trim() || !importBody.trim()}
            onClick={() => void runImport()}
          >
            导入动作
          </button>
        </div>
      </details>
    </section>
  );
}
