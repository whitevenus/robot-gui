import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { readError } from "./api";
import { ActionWorkbench } from "./ActionWorkbench";
import { ExecutionRecords } from "./ExecutionRecords";
import { FsmPanel, fsmModeLabel, type FsmModeInfo, type FsmState } from "./FsmPanel";
import { JointPanel, type JointRow } from "./JointPanel";

const G1Viewer = lazy(() =>
  import("./G1Viewer").then((module) => ({ default: module.G1Viewer })),
);

interface BmsReading {
  soc_pct: number;
  soh_pct: number;
  current_raw: number;
  voltage_raw: number[];
  temperature_raw: number[];
  cycle_count: number;
}

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
    joints: JointRow[];
    bms: BmsReading | null;
    hip_imu: { rpy: number[] } | null;
    torso_imu: { rpy: number[] } | null;
    fsm: { id: number | null; mode: string | null } | null;
  };
  capabilities: {
    controls: { available: boolean; reason: string };
  };
}

const controlEntries = ["键盘行走控制", "手掰示教"];

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

function formatVector(value: number[] | null | undefined): string {
  if (!value) return "—";
  const parts = value.map((item) => item.toFixed(3));
  const labels = ["R", "P", "Y"];
  return parts
    .map((item, index) => `${labels[index] ?? index} ${item}`)
    .join(" / ");
}

/** BMS 的原始计数对操作者没有意义，只留能直接读的量。 */
function validChannels(value: number[] | null | undefined): number[] {
  if (!value) return [];
  return value.filter((item) => item !== 0);
}

type View = "console" | "records";
type SourceState = TelemetrySnapshot["sources"][string]["state"];

interface TelemetryBadgesProps {
  bms: BmsReading | null;
  bmsState: SourceState;
  bmsAge: number | null;
  hipImuRpy: number[] | null | undefined;
  hipImuState: SourceState;
  hipImuAge: number | null;
  torsoImuRpy: number[] | null | undefined;
  torsoImuState: SourceState;
  torsoImuAge: number | null;
}

/**
 * 顶部的遥测小徽标：电池与两路 IMU 的新鲜度和读数。
 *
 * 这些量以前挤在左侧导航里，导航一多就没地方放；收成徽标后导航只留页面切换，
 * 常用的状态读数仍在视线高度。
 */
function TelemetryBadges(props: TelemetryBadgesProps) {
  const {
    bms,
    bmsState,
    bmsAge,
    hipImuRpy,
    hipImuState,
    hipImuAge,
    torsoImuRpy,
    torsoImuState,
    torsoImuAge,
  } = props;
  const bmsTemperature = bms ? validChannels(bms.temperature_raw).join(", ") : "";

  return (
    <div className="telemetry-badges" role="region" aria-label="遥测摘要">
      <span className="tbadge" title={bmsTemperature ? `温度 ${bmsTemperature}` : ""}>
        <span className="tbadge-label">电池</span>
        <span className={`src-state state-${bmsState}`}>
          {sourceStateLabel(bmsState)}
        </span>
        <b>{bms ? `${bms.soc_pct}% SOC · SOH ${bms.soh_pct}%` : "—"}</b>
        {bmsAge !== null && <span className="src-age">{bmsAge} ms</span>}
      </span>
      <span className="tbadge">
        <span className="tbadge-label">髋部 IMU</span>
        <span className={`src-state state-${hipImuState}`}>
          {sourceStateLabel(hipImuState)}
        </span>
        <b>{formatVector(hipImuRpy)}</b>
        {hipImuAge !== null && <span className="src-age">{hipImuAge} ms</span>}
      </span>
      <span className="tbadge">
        <span className="tbadge-label">躯干 IMU</span>
        <span className={`src-state state-${torsoImuState}`}>
          {sourceStateLabel(torsoImuState)}
        </span>
        <b>{formatVector(torsoImuRpy)}</b>
        {torsoImuAge !== null && <span className="src-age">{torsoImuAge} ms</span>}
      </span>
    </div>
  );
}

export function App() {
  const [snapshot, setSnapshot] = useState<TelemetrySnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [sampleReceivedAt, setSampleReceivedAt] = useState<number | null>(null);
  const [ageTick, setAgeTick] = useState(0);
  const [fsmState, setFsmState] = useState<FsmState | null>(null);
  const [fsmBusy, setFsmBusy] = useState(false);
  const [fsmMessage, setFsmMessage] = useState("");
  const [view, setView] = useState<View>("console");

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

  const refreshFsm = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/fsm/state");
      if (response.ok) {
        setFsmState((await response.json()) as FsmState);
      }
    } catch {
      // 后端不可达时保留上一次状态，不伪造可用。
    }
  }, []);

  useEffect(() => {
    void refreshFsm();
    const timer = window.setInterval(() => {
      void refreshFsm();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refreshFsm]);

  const switchFsm = useCallback(
    async (mode: FsmModeInfo) => {
      const warning = mode.danger
        ? `即将把机器人切到「${mode.label}」（FSM ${mode.fsm_id}）：${mode.hint}\n\n` +
          "站立中切换会让机器人失去支撑力矩并瘫倒。确认现场已用遥控器接管、" +
          "机器人处于可安全瘫倒的状态后再继续。"
        : `即将把机器人切到「${mode.label}」（FSM ${mode.fsm_id}）：${mode.hint}\n\n` +
          "确认现场安全后继续。";
      if (!window.confirm(warning)) return;
      setFsmBusy(true);
      setFsmMessage("");
      try {
        const response = await fetch("/api/v1/fsm/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fsm_id: mode.fsm_id, confirmed: true }),
        });
        if (!response.ok) {
          setFsmMessage(await readError(response));
          return;
        }
        setFsmState((await response.json()) as FsmState);
        await refreshFsm();
      } catch (cause) {
        setFsmMessage(`模式切换请求失败：${String(cause)}`);
      } finally {
        setFsmBusy(false);
      }
    },
    [refreshFsm],
  );

  const elapsedSinceSnapshot =
    sampleReceivedAt === null ? 0 : Math.max(0, ageTick - sampleReceivedAt);
  const lowStateSource = snapshot?.sources.low_state;
  const sourceAge = lowStateSource
    ? liveSourceAge(lowStateSource, elapsedSinceSnapshot) ?? undefined
    : undefined;
  const lowStateState = lowStateSource
    ? liveSourceState(lowStateSource, elapsedSinceSnapshot)
    : "unknown";
  const hipImuSource = snapshot?.sources.hip_imu;
  const hipImuAge = hipImuSource
    ? liveSourceAge(hipImuSource, elapsedSinceSnapshot)
    : null;
  const hipImuState = hipImuSource
    ? liveSourceState(hipImuSource, elapsedSinceSnapshot)
    : "unknown";
  const bmsSource = snapshot?.sources.bms;
  const bmsState = bmsSource
    ? liveSourceState(bmsSource, elapsedSinceSnapshot)
    : "unknown";
  const bmsAge = bmsSource
    ? liveSourceAge(bmsSource, elapsedSinceSnapshot)
    : null;
  const torsoImuSource = snapshot?.sources.torso_imu;
  const torsoImuState = torsoImuSource
    ? liveSourceState(torsoImuSource, elapsedSinceSnapshot)
    : "unknown";
  const torsoImuAge = torsoImuSource
    ? liveSourceAge(torsoImuSource, elapsedSinceSnapshot)
    : null;

  const fsm = snapshot?.robot?.fsm ?? null;
  const fsmId = fsm?.id ?? null;
  const fsmLabel = fsmModeLabel(fsmState, fsmId);
  const whitelist = fsmState?.whitelist ?? [];
  // 后端还没提供运动模式接口时，白名单是未知的——不能拿空列表当成"不在白名单"。
  const whitelistKnown = fsmState !== null;
  const inWhitelist = fsmId !== null && whitelistKnown && whitelist.includes(fsmId);
  const controlsAvailable = snapshot?.capabilities.controls.available === true;
  const bms = snapshot?.robot?.bms ?? null;

  const alerts: { tone: "warn" | "bad"; text: string }[] = [];
  if (error) {
    alerts.push({ tone: "bad", text: error });
  }
  if (snapshot && !connected) {
    alerts.push({
      tone: "bad",
      text: "页面与后端的遥测通道已断开，正在每秒自动重连。",
    });
  }
  if (snapshot && fsmId === null) {
    alerts.push({
      tone: "warn",
      text: "读不到机器人 FSM 状态，动作回放会被拒绝。",
    });
  } else if (snapshot && controlsAvailable && whitelistKnown && !inWhitelist) {
    alerts.push({
      tone: "warn",
      text: `当前 FSM ${fsmId} 不在允许列表 [${whitelist.join(", ")}] 内，回放会被拒绝。`,
    });
  }

  return (
    <main className="workstation">
      <aside className="side" aria-label="工作区导航">
        <div className="brand">
          <div className="brand-mark">G1</div>
          <div className="brand-copy">
            <strong>Robot GUI</strong>
            <small>29dof workstation</small>
          </div>
        </div>

        <nav className="nav">
          <span className="nav-label">页面</span>
          <button
            type="button"
            className={`nav-item${view === "console" ? " active" : ""}`}
            aria-current={view === "console" ? "page" : undefined}
            onClick={() => setView("console")}
          >
            <span className="nav-pill">WS</span>
            <span>控制台总览</span>
          </button>
          <button
            type="button"
            className={`nav-item${view === "records" ? " active" : ""}`}
            aria-current={view === "records" ? "page" : undefined}
            onClick={() => setView("records")}
          >
            <span className="nav-pill">TR</span>
            <span>执行轨迹记录</span>
          </button>
        </nav>

        <div className="side-note">
          <span
            className="status-dot"
            style={{
              background: controlsAvailable ? "#66d588" : "#ffc868",
              boxShadow: controlsAvailable
                ? "0 0 0 4px rgba(102,213,136,.14)"
                : "0 0 0 4px rgba(255,200,104,.14)",
            }}
            aria-hidden="true"
          />
          <div>
            <strong>
              {controlsAvailable ? "真机控制已开启" : "真机控制未开启"}
            </strong>
            <small>
              {controlsAvailable
                ? "回放需逐次确认 · 先停止再交还控制权"
                : "只读遥测 · 回放按钮保持禁用"}
            </small>
          </div>
        </div>
      </aside>

      <div className={`main${view === "records" ? " main-records" : ""}`} id="overview">
        <header className="main-head">
          <div>
            <p className="eyebrow">
              {view === "console" ? "Live Telemetry" : "Execution Trace"}
            </p>
            <h1>
              {view === "console" ? "机器人状态工作台" : "执行轨迹记录"}
            </h1>
          </div>
          <div className="head-right">
            <TelemetryBadges
              bms={bms}
              bmsState={bmsState}
              bmsAge={bmsAge}
              hipImuRpy={snapshot?.robot?.hip_imu?.rpy}
              hipImuState={hipImuState}
              hipImuAge={hipImuAge}
              torsoImuRpy={snapshot?.robot?.torso_imu?.rpy}
              torsoImuState={torsoImuState}
              torsoImuAge={torsoImuAge}
            />
            <span className="mode-badge" role="status">
              {snapshot ? "REAL · 真实遥测" : "REAL · 等待连接"}
            </span>
          </div>
        </header>

        {view === "records" ? (
          <ExecutionRecords />
        ) : (
          <>
            <section className="status-strip" aria-label="连接状态">
              <div className="strip-item">
                <span className="strip-label">运行模式</span>
                <span className="strip-value">
                  真实遥测
                  {controlsAvailable && <span className="chip teal">真机控制</span>}
                </span>
                <span className="strip-sub">
                  {controlsAvailable ? "回放需逐次确认" : "只读，不下发命令"}
                </span>
              </div>
              <div className="strip-item">
                <span className="strip-label">DDS 连接</span>
                <span
                  className={`strip-value ${
                    connectionLabel(snapshot, lowStateState) === "已连接"
                      ? "ok"
                      : "warn"
                  }`}
                >
                  {connectionLabel(snapshot, lowStateState)}
                </span>
                <span className="strip-sub">
                  {snapshot?.connection.interface
                    ? `Unitree SDK2 / ${snapshot.connection.interface}`
                    : "等待后端上报网卡"}
                </span>
                <span className="strip-sub">
                  页面通道：{connected ? "WebSocket 已连接" : "WebSocket 未连接"}
                </span>
                <span className="strip-sub">
                  {snapshot?.connection.message ?? "等待后端状态"}
                </span>
              </div>
              <div className="strip-item">
                <span className="strip-label">数据年龄</span>
                <span
                  className={`strip-value ${lowStateState === "fresh" ? "ok" : "warn"}`}
                >
                  {sourceAge === undefined ? "—" : `${sourceAge} ms`}
                </span>
                <span className="strip-sub">
                  {snapshot?.connection.last_data_at
                    ? `最近 ${new Date(snapshot.connection.last_data_at).toLocaleTimeString()}`
                    : "尚未收到数据"}
                </span>
              </div>
              <div className="strip-item">
                <span className="strip-label">FSM 模式</span>
                <span className={`strip-value ${inWhitelist ? "ok" : "warn"}`}>
                  {fsmLabel}
                  {fsmId !== null && <span className="chip teal">FSM {fsmId}</span>}
                </span>
                <span className="strip-sub">
                  {fsmId === null
                    ? "读不到 FSM 状态"
                    : !whitelistKnown
                      ? "白名单未知（后端未提供运动模式接口）"
                      : inWhitelist
                        ? "白名单允许回放"
                        : `不在白名单 [${whitelist.join(", ")}] 内`}
                </span>
              </div>
            </section>

            {alerts.map((alert) => (
              <div className={`alert ${alert.tone}`} key={alert.text} role="status">
                <span className="icon" aria-hidden="true">
                  !
                </span>
                <span>{alert.text}</span>
              </div>
            ))}

            <div className="verify">
              <Suspense
                fallback={
                  <section className="card" aria-label="G1 三维姿态模型">
                    <p className="empty-cell" role="status">
                      正在加载三维姿态工作台…
                    </p>
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
              <div id="joint-panel">
                <JointPanel joints={snapshot?.robot?.joints ?? []} />
              </div>
            </div>
          </>
        )}
      </div>

      <aside className="rail" aria-label="控制列">
        <FsmPanel
          state={fsmState}
          currentId={fsmId}
          busy={fsmBusy}
          onSwitch={(mode) => void switchFsm(mode)}
        />
        {fsmMessage && <p className="rail-error">{fsmMessage}</p>}
        <ActionWorkbench />
        <section className="rail-section" aria-label="其他真机控制能力">
          <div className="rail-section-head">
            <h2>其余真机控制能力</h2>
          </div>
          <p className="rail-reason">
            动作回放已在上方提供。以下能力本版仍未开放，按钮保持禁用而不是给出假可用状态。
          </p>
          <div className="control-list">
            {controlEntries.map((label) => (
              <button
                key={label}
                type="button"
                disabled
                title="当前版本未开放该真机控制能力"
              >
                {label}
                <span>不可用</span>
              </button>
            ))}
          </div>
        </section>
      </aside>
    </main>
  );
}
