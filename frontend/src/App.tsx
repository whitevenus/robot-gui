import { useEffect, useState } from "react";

interface TelemetrySnapshot {
  schema_version: string;
  mode: "mock" | "real" | "unknown";
  connection: {
    state: "mock" | "connected" | "disconnected" | "unknown";
    interface: string | null;
    last_data_at: string | null;
    error: string | null;
    message: string;
  };
  sources: {
    low_state: {
      state: "mock" | "fresh" | "delayed" | "offline" | "unknown";
      age_ms: number;
    };
  };
  capabilities: {
    controls: { available: boolean; reason: string };
  };
}

const controlEntries = ["FSM 模式", "键盘行走", "手掰示教", "动作回放"];

function connectionLabel(snapshot: TelemetrySnapshot | null): string {
  if (!snapshot) return "等待状态数据";
  if (snapshot.connection.state === "mock") return "Mock 数据源";
  if (snapshot.connection.state === "connected") return "已连接";
  if (snapshot.connection.state === "disconnected") return "未连接";
  return "状态未知";
}

export function App() {
  const [snapshot, setSnapshot] = useState<TelemetrySnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [sampleReceivedAt, setSampleReceivedAt] = useState<number | null>(null);
  const [ageTick, setAgeTick] = useState(0);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: number | undefined;
    let stopped = false;

    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/telemetry`);
      socket.onopen = () => {
        setConnected(true);
        setError(null);
      };
      socket.onmessage = (event) => {
        try {
          const next = JSON.parse(event.data) as TelemetrySnapshot;
          if (
            next.schema_version !== "1.0" ||
            !next.connection ||
            !next.sources?.low_state
          ) {
            throw new Error("遥测数据格式版本或必需字段不受支持。");
          }
          setSnapshot(next);
          setSampleReceivedAt(performance.now());
          setError(null);
        } catch (cause) {
          setError(
            cause instanceof Error ? cause.message : "无法解析遥测状态。",
          );
        }
      };
      socket.onerror = () => {
        setError("无法连接后端遥测服务。");
      };
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) retryTimer = window.setTimeout(connect, 1000);
      };
    };

    connect();
    const clockTimer = window.setInterval(
      () => setAgeTick(performance.now()),
      250,
    );
    return () => {
      stopped = true;
      window.clearInterval(clockTimer);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, []);

  const sourceAge =
    snapshot && sampleReceivedAt !== null
      ? Math.round(
          snapshot.sources.low_state.age_ms +
            Math.max(0, ageTick - sampleReceivedAt),
        )
      : snapshot?.sources.low_state.age_ms;

  return (
    <main className="workstation">
      <header className="topbar">
        <div>
          <p className="eyebrow">ROBOT GUI · DEVELOPMENT WORKSTATION</p>
          <h1>机器人状态工作台</h1>
        </div>
        <span className="mode-badge" role="status">
          MOCK · 不连接真实机器人
        </span>
      </header>

      <section className="notice" aria-label="Mock 模式提示">
        <span className="notice-icon" aria-hidden="true">
          i
        </span>
        <p>
          当前为开发 Mock 模式。这里显示的连接状态不是 Unitree DDS
          实机连接；所有真机控制能力均已禁用。
        </p>
      </section>

      <section className="status-grid" aria-label="连接状态">
        <article className="status-card">
          <span className="card-label">运行模式</span>
          <strong>Mock 模拟数据</strong>
          <span className="subtle">真实 SDK 未初始化</span>
        </article>
        <article className="status-card">
          <span className="card-label">DDS 连接</span>
          <strong>{connectionLabel(snapshot)}</strong>
          <span className="subtle">
            网卡：{snapshot?.connection.interface ?? "未配置（Mock）"}
          </span>
        </article>
        <article className="status-card">
          <span className="card-label">最近数据</span>
          <strong>
            {snapshot?.connection.last_data_at
              ? new Date(snapshot.connection.last_data_at).toLocaleTimeString()
              : "尚未收到"}
          </strong>
          <span className="subtle">
            Mock 样本年龄：
            {sourceAge === undefined ? "—" : ` ${sourceAge} ms`}
          </span>
        </article>
        <article className="status-card">
          <span className="card-label">页面遥测通道</span>
          <strong>{connected ? "WebSocket 已连接" : "WebSocket 未连接"}</strong>
          <span className="subtle">
            {snapshot?.sources.low_state.state ?? "等待数据"}
          </span>
        </article>
      </section>

      <section className="connection-panel">
        <div>
          <p className="eyebrow">CONNECTION DETAILS</p>
          <h2>连接诊断</h2>
        </div>
        <dl>
          <div>
            <dt>数据来源</dt>
            <dd>本地 Mock 状态服务</dd>
          </div>
          <div>
            <dt>DDS / Unitree SDK</dt>
            <dd>未连接；此版本不会初始化真实 SDK</dd>
          </div>
          <div>
            <dt>连接错误</dt>
            <dd>{error ?? snapshot?.connection.error ?? "无"}</dd>
          </div>
          <div>
            <dt>状态说明</dt>
            <dd>{snapshot?.connection.message ?? "尚未收到后端状态。"}</dd>
          </div>
        </dl>
      </section>

      <section className="control-panel" aria-label="真机控制能力">
        <div>
          <p className="eyebrow">HARDWARE CONTROL</p>
          <h2>真机控制</h2>
        </div>
        <p className="disabled-explanation">
          Mock 模式下不会获得控制权，也不能发送真机命令。
        </p>
        <div className="control-list">
          {controlEntries.map((label) => (
            <button
              key={label}
              type="button"
              disabled
              title="Mock 模式下真机控制不可用"
            >
              {label}
              <span>不可用</span>
            </button>
          ))}
        </div>
      </section>
      <footer>状态 schema {snapshot?.schema_version ?? "—"} · 仅用于开发验证</footer>
    </main>
  );
}
