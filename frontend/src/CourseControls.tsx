/** 课程页共用的真机控制：会话状态、暂停/继续/停止、回放速度。

控制入口原来在右侧的「动作库与真机回放」栏里，现在课程页是唯一的操作面，
所以这几个控件被抽出来给课程列表和课程详情共用。
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

import { readError } from "./api";

export interface ControlState {
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

/** 回放速度档位：只允许慢放和原速，不提供快放。 */
export const SPEED_OPTIONS = [0.5, 0.8, 1];

export const PHASE_LABELS: Record<string, string> = {
  idle: "空闲",
  planning: "规划中",
  acquiring: "接管中",
  playing: "回放中",
  paused: "已暂停",
  releasing: "交还控制权",
  fault: "故障",
};

const RUNNING_PHASES = ["planning", "acquiring", "playing", "releasing"];

/** 页面上给用户看的一条提示：成功/说明是 info，出错是 bad。 */
export interface Notice {
  tone: "info" | "bad";
  text: string;
}

export interface ControlSession {
  state: ControlState | null;
  busy: boolean;
  refresh: () => Promise<void>;
  send: (path: string) => Promise<string>;
}

/** 轮询后端的控制会话状态，并提供暂停/继续/停止用的发送口。 */
export function useControlSession(): ControlSession {
  const [state, setState] = useState<ControlState | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/control/state");
      if (response.ok) {
        setState((await response.json()) as ControlState);
      }
    } catch {
      // 控制状态不可达时保留上一次快照，不伪造空闲。
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  /** 调一个控制接口；返回空串表示成功，否则是给用户看的错误。 */
  const send = useCallback(async (path: string): Promise<string> => {
    setBusy(true);
    try {
      const response = await fetch(path, { method: "POST" });
      if (!response.ok) return await readError(response);
      setState((await response.json()) as ControlState);
      return "";
    } catch (cause) {
      return String(cause);
    } finally {
      setBusy(false);
    }
  }, []);

  return { state, busy, refresh, send };
}

/** 最近的可用档位：后端默认值比 1 还快时只能落到 1。 */
function nearestSpeed(value: number): number {
  const allowed = SPEED_OPTIONS.filter((option) => option <= value + 1e-9);
  return allowed.length > 0 ? allowed[allowed.length - 1] : SPEED_OPTIONS[0];
}

/** 回放速度：初始取后端的默认值，操作员改过之后就不再被默认值覆盖。 */
export function useReplaySpeed(defaultSpeed: number | null): {
  speed: number;
  setSpeed: (value: number) => void;
} {
  const [speed, setSpeed] = useState(1);
  const touched = useRef(false);

  useEffect(() => {
    if (touched.current || defaultSpeed === null) return;
    setSpeed(nearestSpeed(defaultSpeed));
  }, [defaultSpeed]);

  return {
    speed,
    setSpeed: (value: number) => {
      touched.current = true;
      setSpeed(value);
    },
  };
}

interface SpeedPickerProps {
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}

/**
 * 回放速度下拉：只有 0.5 / 0.8 / 1.0 三档。
 *
 * 自己画，不用浏览器原生的 `<select>`：原生下拉的展开列表是系统样式，在深色
 * 页面里是一块白板。方向键 / 回车 / ESC 都能操作，展开层点外面自动收起。
 */
export function SpeedPicker({ value, onChange, disabled }: SpeedPickerProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const optionRefs = useRef<(HTMLDivElement | null)[]>([]);
  const selectedIndex = Math.max(0, SPEED_OPTIONS.indexOf(value));

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  useEffect(() => {
    if (open) optionRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  function openMenu(index: number) {
    setActiveIndex(index);
    setOpen(true);
  }

  function choose(option: number) {
    onChange(option);
    setOpen(false);
    triggerRef.current?.focus();
  }

  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openMenu(selectedIndex);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openMenu(SPEED_OPTIONS.length - 1);
    }
  }

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => Math.min(index + 1, SPEED_OPTIONS.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose(SPEED_OPTIONS[activeIndex]);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div className="speed-picker" ref={rootRef}>
      <span className="speed-picker-label">回放速度</span>
      <button
        ref={triggerRef}
        type="button"
        className="speed-picker-button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`回放速度 ${value.toFixed(1)}×`}
        onClick={() => (open ? setOpen(false) : openMenu(selectedIndex))}
        onKeyDown={onTriggerKeyDown}
      >
        {value.toFixed(1)}×
        <span className="speed-picker-caret" aria-hidden="true" />
      </button>
      {open && (
        <div
          className="speed-picker-menu"
          role="listbox"
          aria-label="回放速度"
          onKeyDown={onMenuKeyDown}
        >
          {SPEED_OPTIONS.map((option, index) => (
            <div
              key={option}
              ref={(node) => {
                optionRefs.current[index] = node;
              }}
              role="option"
              aria-selected={option === value}
              className={`speed-picker-option${option === value ? " selected" : ""}`}
              tabIndex={-1}
              onClick={() => choose(option)}
            >
              {option.toFixed(1)}×
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface SessionButtonsProps {
  session: ControlSession;
  onError: (text: string) => void;
}

/** 暂停 / 继续 / 停止：只对正在跑的会话有意义，其余时候保持禁用。 */
export function SessionButtons({ session, onError }: SessionButtonsProps) {
  const { state, busy, send } = session;
  const running = state !== null && RUNNING_PHASES.includes(state.phase);
  const paused = state?.phase === "paused";
  const remaining = state?.resume_actions?.length ?? 0;

  async function run(path: string) {
    const error = await send(path);
    if (error) onError(error);
  }

  return (
    <div className="session-buttons">
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!running || busy}
        onClick={() => void run("/api/v1/control/pause")}
      >
        暂停
      </button>
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!paused || busy}
        onClick={() => void run("/api/v1/control/resume")}
      >
        {paused && remaining > 0 ? `继续播放剩余 ${remaining} 段` : "继续播放"}
      </button>
      <button
        type="button"
        className="btn btn-ghost"
        disabled={!running || busy}
        onClick={() => void run("/api/v1/control/release")}
      >
        停止
      </button>
    </div>
  );
}

interface SessionStateProps {
  state: ControlState | null;
}

/** 当前会话的读数：阶段、在播哪一段、这一把用的速度。 */
export function SessionState({ state }: SessionStateProps) {
  if (state === null) {
    return <span className="session-state meta">正在读取控制状态…</span>;
  }
  const phase = PHASE_LABELS[state.phase] ?? state.phase;
  return (
    <span className="session-state">
      <span className={`session-dot phase-${state.phase}`} aria-hidden="true" />
      <strong>{phase}</strong>
      {state.action ? <span className="meta">· {state.action}</span> : null}
      <span className="chip teal">双臂 14 维</span>
      <span className="chip">{state.gravity_compensation ? "重力补偿 开" : "重力补偿 关"}</span>
      <span className="meta">本把速度 {state.current_speed}×</span>
    </span>
  );
}
