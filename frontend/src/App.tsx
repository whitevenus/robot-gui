import { lazy, Suspense, useEffect, useState } from "react";

const G1Viewer = lazy(() =>
  import("./G1Viewer").then((module) => ({ default: module.G1Viewer })),
);

interface TelemetrySnapshot {
  schema_version: string;
  mode: "real";
  connection: {
    state: "connected" | "delayed" | "disconnected" | "error" | "unknown";
    interface: string | null;
    last_data_at: string | null;
    error: string | null;
    message: string;
  };
  sources: {
    [source: string]: {
      state: "fresh" | "delayed" | "offline" | "invalid" | "unknown";
      age_ms: number | null;
      fresh_for_ms: number;
      offline_after_ms: number;
      error?: string | null;
    };
  };
  robot?: {
    product_id: string;
    variant: string;
    joint_count: number;
    mode_machine: number | null;
    joints: {
      name: string;
      urdf_joint_name: string;
      sdk_slot: number;
      group: string;
      q_rad: number | null;
      dq_rad_s: number | null;
      ddq_rad_s2: number | null;
      tau_est_nm: number | null;
      temperature_raw: number[] | null;
      voltage_v: number | null;
      mode: number | null;
      status_code: number | null;
    }[];
    bms: {
      soc_pct: number;
      soh_pct: number;
      current_raw: number;
      voltage_raw: number[];
      temperature_raw: number[];
      cycle_count: number;
    } | null;
    hip_imu: { rpy: number[] } | null;
    torso_imu: { rpy: number[] } | null;
    fsm: { id: number | null; mode: number | null } | null;
  };
  capabilities: {
    controls: { available: boolean; reason: string };
  };
}

const controlEntries = ["FSM 模式", "键盘行走", "手掰示教", "动作回放"];
const telemetrySources = [
  ["low_state", "LowState"],
  ["bms", "BMS 电池"],
  ["hip_imu", "LowState IMU"],
  ["torso_imu", "Secondary IMU"],
  ["fsm", "FSM"],
] as const;

function connectionLabel(
  snapshot: TelemetrySnapshot | null,
  lowStateState: TelemetrySnapshot["sources"][string]["state"],
): string {
  if (!snapshot) return "等待状态数据";
  if (
    snapshot.connection.state === "connected" &&
    lowStateState === "offline"
  ) {
    return "未连接";
  }
  if (
    snapshot.connection.state === "connected" &&
    (lowStateState === "delayed" || lowStateState === "invalid")
  ) {
    return "数据延迟";
  }
  if (snapshot.connection.state === "connected") return "已连接";
  if (snapshot.connection.state === "disconnected") return "未连接";
  if (snapshot.connection.state === "delayed") return "数据延迟";
  if (snapshot.connection.state === "error") return "连接错误";
  return "状态未知";
}

function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(3);
}

function sourceStateLabel(state: TelemetrySnapshot["sources"][string]["state"]): string {
  if (state === "fresh") return "最新";
  if (state === "delayed") return "延迟";
  if (state === "offline") return "离线";
  if (state === "invalid") return "无效";
  return "等待";
}

function liveSourceAge(
  source: TelemetrySnapshot["sources"][string],
  elapsedMs: number,
): number | null {
  return source.age_ms === null ? null : Math.round(source.age_ms + elapsedMs);
}

function liveSourceState(
  source: TelemetrySnapshot["sources"][string],
  elapsedMs: number,
): TelemetrySnapshot["sources"][string]["state"] {
  const ageMs = liveSourceAge(source, elapsedMs);
  if (ageMs === null) return "unknown";
  if (source.state === "invalid" && ageMs <= source.offline_after_ms) {
    return "invalid";
  }
  if (ageMs <= source.fresh_for_ms) return "fresh";
  if (ageMs <= source.offline_after_ms) return "delayed";
  return "offline";
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
            next.mode !== "real" ||
            !next.connection ||
            !next.sources?.low_state
          ) {
            throw new Error("遥测 schema、运行模式或必需字段不受支持。");
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

  const elapsedSinceSnapshot =
    sampleReceivedAt === null ? 0 : Math.max(0, ageTick - sampleReceivedAt);
  const lowStateSource = snapshot?.sources.low_state;
  const hipImuSource = snapshot?.sources.hip_imu;
  const sourceAge = lowStateSource
    ? liveSourceAge(lowStateSource, elapsedSinceSnapshot) ?? undefined
    : undefined;
  const lowStateState = lowStateSource
    ? liveSourceState(lowStateSource, elapsedSinceSnapshot)
    : "unknown";
  const hipImuAge = hipImuSource
    ? liveSourceAge(hipImuSource, elapsedSinceSnapshot)
    : null;
  const hipImuState = hipImuSource
    ? liveSourceState(hipImuSource, elapsedSinceSnapshot)
    : "unknown";

  return (
    <main className="workstation">
      <header className="topbar">
        <div>
          <p className="eyebrow">ROBOT GUI · DEVELOPMENT WORKSTATION</p>
          <h1>机器人状态工作台</h1>
        </div>
        <span className="mode-badge" role="status">
          {snapshot ? "REAL · 只读遥测" : "REAL · 等待连接"}
        </span>
      </header>

      <section className="notice" aria-label="运行模式提示">
        <span className="notice-icon" aria-hidden="true">
          i
        </span>
        <p>
          可运行模式仅接受真实 Unitree SDK2/DDS 遥测；无法取得真实数据时不会生成替代状态。此版本不提供任何真机控制能力。
        </p>
      </section>

      <section className="status-grid" aria-label="连接状态">
        <article className="status-card">
          <span className="card-label">运行模式</span>
          <strong>
            真实遥测（只读）
          </strong>
          <span className="subtle">
            SDK2/DDS 必须成功初始化
          </span>
        </article>
        <article className="status-card">
          <span className="card-label">DDS 连接</span>
          <strong>{connectionLabel(snapshot, lowStateState)}</strong>
          <span className="subtle">
            网卡：{snapshot?.connection.interface ?? "等待启动配置"}
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
            数据年龄：
            {sourceAge === undefined ? "—" : ` ${sourceAge} ms`}
          </span>
        </article>
        <article className="status-card">
          <span className="card-label">页面遥测通道</span>
          <strong>{connected ? "WebSocket 已连接" : "WebSocket 未连接"}</strong>
          <span className="subtle">
            {sourceStateLabel(lowStateState)}
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
            <dd>
              {`Unitree SDK2 / DDS（${snapshot?.connection.interface ?? "网卡未知"}）`}
            </dd>
          </div>
          <div>
            <dt>DDS / Unitree SDK</dt>
            <dd>
              {snapshot?.connection.message ?? "等待真实 SDK2/DDS 连接"}
            </dd>
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

      <section className="source-panel" aria-label="实时遥测数据">
        <div className="section-heading">
          <div>
            <p className="eyebrow">LIVE TELEMETRY</p>
            <h2>实时数据</h2>
          </div>
          <span>
            {snapshot?.robot?.product_id ?? "g1"}{" "}
            {snapshot?.robot?.variant ?? "29dof"} ·{" "}
            {snapshot?.robot?.joint_count ?? 29} DoF
          </span>
        </div>
        <div className="auxiliary-grid">
          <article>
            <h3>BMS 电池</h3>
            <p className="telemetry-primary">
              {snapshot?.robot?.bms?.soc_pct ?? "—"}<small>% SOC</small>
            </p>
            <p>SOH {snapshot?.robot?.bms?.soh_pct ?? "—"}%</p>
            <small>
              电压 raw：
              {snapshot?.robot?.bms?.voltage_raw?.join(", ") ?? "—"}
            </small>
            <small>
              电流 raw：{snapshot?.robot?.bms?.current_raw ?? "—"} · 温度 raw：
              {snapshot?.robot?.bms?.temperature_raw?.join(", ") ?? "—"}
            </small>
            <small>循环次数：{snapshot?.robot?.bms?.cycle_count ?? "—"}</small>
          </article>
          <article>
            <h3>LowState IMU</h3>
            <p className="telemetry-primary telemetry-vector">
              {snapshot?.robot?.hip_imu?.rpy?.map(formatNumber).join(", ") ?? "—"}
            </p>
            <small>RPY</small>
          </article>
          <article>
            <h3>Secondary IMU</h3>
            <p className="telemetry-primary telemetry-vector">
              {snapshot?.robot?.torso_imu?.rpy?.map(formatNumber).join(", ") ??
                "—"}
            </p>
            <small>RPY</small>
          </article>
          <article>
            <h3>FSM</h3>
            <p className="telemetry-primary">
              {snapshot?.robot?.fsm?.id ?? "—"}
              <small>FSM ID</small>
            </p>
            <p>模式：{snapshot?.robot?.fsm?.mode ?? "unknown"}</p>
            <small>
              Mode machine（URDF selector）：
              {snapshot?.robot?.mode_machine ?? "—"}
            </small>
          </article>
        </div>
        <section className="source-health" aria-label="数据来源状态">
          {telemetrySources.map(([key, label]) => {
            const source = snapshot?.sources[key];
            const ageMs = source
              ? liveSourceAge(source, elapsedSinceSnapshot)
              : null;
            const state = source
              ? liveSourceState(source, elapsedSinceSnapshot)
              : "unknown";
            return (
              <div className={`source-health-item state-${state}`} key={key}>
                <span>{label}</span>
                <strong>{sourceStateLabel(state)}</strong>
                <small>{ageMs === null ? "—" : `${ageMs} ms`}</small>
                {source?.error && (
                  <small className="source-error">{source.error}</small>
                )}
              </div>
            );
          })}
        </section>
      </section>

      <Suspense
        fallback={
          <section className="pose-panel" aria-label="G1 三维姿态模型">
            <p role="status">正在加载三维姿态工作台…</p>
          </section>
        }
      >
        <G1Viewer
          joints={snapshot?.robot?.joints ?? []}
          hipImuRpy={snapshot?.robot?.hip_imu?.rpy ?? null}
          hipImuAgeMs={hipImuAge}
          hipImuState={hipImuState}
          lowStateAgeMs={sourceAge ?? null}
          lowStateState={lowStateState}
          modeMachine={snapshot?.robot?.mode_machine ?? null}
        />
      </Suspense>

      <section className="joint-panel" aria-label="G1 29DoF 关节遥测">
        <div className="section-heading">
          <div>
            <p className="eyebrow">JOINT TELEMETRY</p>
            <h2>G1 29DoF 关节状态</h2>
          </div>
          <span>关节名称与 SDK 槽位由后端产品 schema 提供</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>关节</th>
                <th>组</th>
                <th>q (rad)</th>
                <th>dq (rad/s)</th>
                <th>ddq (rad/s²)</th>
                <th>tau_est (Nm)</th>
                <th>温度 raw</th>
                <th>电压 (V)</th>
                <th>模式</th>
                <th>状态码</th>
              </tr>
            </thead>
            <tbody>
              {(snapshot?.robot?.joints ?? []).map((joint) => (
                <tr key={joint.name}>
                  <th scope="row">{joint.name}</th>
                  <td>{joint.group}</td>
                  <td>{formatNumber(joint.q_rad)}</td>
                  <td>{formatNumber(joint.dq_rad_s)}</td>
                  <td>{formatNumber(joint.ddq_rad_s2)}</td>
                  <td>{formatNumber(joint.tau_est_nm)}</td>
                  <td>{joint.temperature_raw?.join(", ") ?? "—"}</td>
                  <td>{formatNumber(joint.voltage_v)}</td>
                  <td>{joint.mode ?? "—"}</td>
                  <td>{joint.status_code ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!snapshot?.robot?.joints?.length && (
            <p className="empty-state">
              尚未收到可用 LowState；关节数据不可用，不以默认数值代替。
            </p>
          )}
        </div>
      </section>

      <section className="control-panel" aria-label="真机控制能力">
        <div>
          <p className="eyebrow">HARDWARE CONTROL</p>
          <h2>真机控制</h2>
        </div>
        <p className="disabled-explanation">
          此版本仅提供只读遥测，尚未开放任何真机控制能力。
        </p>
        <div className="control-list">
          {controlEntries.map((label) => (
            <button
              key={label}
              type="button"
              disabled
              title="当前版本未开放真机控制"
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
