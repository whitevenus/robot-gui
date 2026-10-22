import { useCallback, useEffect, useState } from "react";

import { readError } from "./api";

interface RecordSummary {
  id: string;
  kind: string;
  label: string;
  actions: string[];
  speed: number | null;
  started_at: string;
  finished_at: string | null;
  elapsed_s: number | null;
  trajectory_duration_s: number | null;
  outcome: string;
  error_code: string;
  waist_pitch_lean_deg: number | null;
  max_drift_deg: number | null;
  violated: boolean;
  sample_count: number;
}

interface RecordSample {
  t: number;
  waist_pitch: number | null;
  waist_modes: number[] | null;
  torso_pitch: number | null;
  hip_pitch: number | null;
}

interface JointError {
  max_error_rad: number;
  mean_error_rad: number;
  target_rad: number;
  measured_rad: number;
}

interface RecordDetail {
  id: string;
  kind: string;
  label: string;
  actions: string[];
  speed: number;
  started_at: string;
  finished_at: string;
  elapsed_s: number;
  trajectory_duration_s: number;
  outcome: string;
  error_code: string;
  error_message: string;
  segments: { name: string; start_s: number }[];
  tracking: { samples?: number; joints?: Record<string, JointError> };
  lower_body: {
    threshold_deg: number;
    max_drift_deg: number;
    max_drift_joint: string;
    waist_pitch_lean_deg: number;
    waist_pitch_drift_deg: number;
    violated: boolean;
  };
  samples: RecordSample[];
}

const OUTCOME_LABELS: Record<string, string> = {
  completed: "完成",
  fault: "故障",
  paused: "已暂停",
  cancelled: "已停止",
};

const OUTCOME_TONES: Record<string, string> = {
  completed: "ok",
  fault: "bad",
  paused: "warn",
  cancelled: "warn",
};

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function degrees(rad: number | null | undefined): string {
  if (rad === null || rad === undefined) return "—";
  return `${((rad * 180) / Math.PI).toFixed(2)}°`;
}

/**
 * 腰 14 的前倾曲线。
 *
 * 只画腰 14：它是躯干前倾的判据（IMU 装在骨盆上，腰倒 23° 时 IMU pitch 只变 2.4°，
 * 反映不出来）。虚线是守卫阈值，越线就说明腰的支撑力矩不够。
 */
function WaistChart({
  samples,
  thresholdDeg,
}: {
  samples: RecordSample[];
  thresholdDeg: number;
}) {
  const width = 1200;
  const height = 200;
  const padLeft = 52;
  const padRight = 16;
  const padTop = 14;
  const padBottom = 28;
  const points = samples.filter((sample) => sample.waist_pitch !== null);
  if (points.length < 2) {
    return <p className="empty-cell">这条记录里没有腰 14 的姿态采样。</p>;
  }
  const tMax = Math.max(...points.map((sample) => sample.t));
  const values = points.map((sample) => ((sample.waist_pitch as number) * 180) / Math.PI);
  const peak = Math.max(...values);
  const min = Math.min(0, ...values);
  const max = Math.max(thresholdDeg * 1.2, peak * 1.15, 1);
  const x = (t: number) => padLeft + (t / Math.max(tMax, 0.001)) * (width - padLeft - padRight);
  const y = (value: number) =>
    padTop + (1 - (value - min) / (max - min)) * (height - padTop - padBottom);
  const path = points
    .map((sample, index) => {
      const value = ((sample.waist_pitch as number) * 180) / Math.PI;
      return `${index === 0 ? "M" : "L"}${x(sample.t).toFixed(1)},${y(value).toFixed(1)}`;
    })
    .join(" ");
  const peakIndex = values.indexOf(peak);

  return (
    <figure className="trace-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="腰 14 前倾曲线">
        <line
          className="chart-threshold"
          x1={padLeft}
          x2={width - padRight}
          y1={y(thresholdDeg)}
          y2={y(thresholdDeg)}
        />
        <text className="chart-axis" x={4} y={y(max) + 4}>
          {max.toFixed(1)}°
        </text>
        <text className="chart-axis" x={4} y={y(min) + 4}>
          {min.toFixed(1)}°
        </text>
        <text className="chart-axis" x={padLeft} y={height - 6}>
          0 s
        </text>
        <text className="chart-axis" x={width - padRight} y={height - 6} textAnchor="end">
          {tMax.toFixed(1)} s
        </text>
        <path className="chart-line" d={path} fill="none" />
        <circle
          className="chart-peak"
          cx={x(points[peakIndex].t)}
          cy={y(peak)}
          r={3.5}
        />
      </svg>
      <figcaption>
        腰 14 前倾峰值 {peak.toFixed(2)}°（t={points[peakIndex].t.toFixed(1)} s）·
        守卫阈值 {thresholdDeg.toFixed(1)}°
      </figcaption>
    </figure>
  );
}

export function ExecutionRecords() {
  const [records, setRecords] = useState<RecordSummary[]>([]);
  const [message, setMessage] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RecordDetail | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/records");
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      const body = await response.json();
      setRecords(body.records ?? []);
      setMessage("");
    } catch (error) {
      setMessage(`执行记录不可达：${String(error)}`);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openDetail = useCallback(async (id: string) => {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/v1/records/${encodeURIComponent(id)}`);
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      setDetail(await response.json());
      setSelected(id);
    } catch (error) {
      setMessage(`执行记录详情读不出来：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  if (detail) {
    const joints = Object.entries(detail.tracking?.joints ?? {}).sort(
      ([, first], [, second]) => second.max_error_rad - first.max_error_rad,
    );
    return (
      <section className="card record-detail" aria-label="执行记录详情">
        <div className="card-head">
          <h2>{detail.label}</h2>
          <div className="card-head-actions">
            <a
              className="btn btn-ghost"
              href={`/api/v1/records/${encodeURIComponent(detail.id)}/download`}
              download
            >
              下载 JSON
            </a>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setDetail(null);
                setSelected(null);
              }}
            >
              返回列表
            </button>
          </div>
        </div>
        <div className="record-body">
          <dl className="record-facts">
            <div>
              <dt>记录 ID</dt>
              <dd>{detail.id}</dd>
            </div>
            <div>
              <dt>开始时间</dt>
              <dd>{formatTime(detail.started_at)}</dd>
            </div>
            <div>
              <dt>类型 / 速度</dt>
              <dd>
                {detail.kind === "course" ? "整套课程" : "单个动作"} · {detail.speed}×
              </dd>
            </div>
            <div>
              <dt>时长</dt>
              <dd>
                轨迹 {detail.trajectory_duration_s.toFixed(1)} s · 实耗{" "}
                {detail.elapsed_s.toFixed(1)} s
              </dd>
            </div>
            <div>
              <dt>结果</dt>
              <dd>
                <span className={`chip ${OUTCOME_TONES[detail.outcome] ?? ""}`}>
                  {OUTCOME_LABELS[detail.outcome] ?? detail.outcome}
                </span>
                {detail.error_message && ` ${detail.error_message}`}
              </dd>
            </div>
            <div>
              <dt>下盘守卫</dt>
              <dd>
                腿最大偏离 {detail.lower_body.max_drift_deg.toFixed(2)}° ·
                腰 14 前倾 {detail.lower_body.waist_pitch_lean_deg.toFixed(2)}° ·
                {detail.lower_body.violated ? " 越限" : " 在阈值内"}
              </dd>
            </div>
          </dl>

          {detail.segments.length > 0 && (
            <p className="rail-note">
              分段起始时刻：
              {detail.segments
                .map((segment) => `${segment.name} @${segment.start_s.toFixed(1)}s`)
                .join(" · ")}
            </p>
          )}

          <WaistChart
            samples={detail.samples}
            thresholdDeg={detail.lower_body.threshold_deg}
          />

          <h3 className="record-subhead">逐关节跟踪误差</h3>
          {joints.length === 0 ? (
            <p className="rail-note">这条记录没有采到跟踪数据。</p>
          ) : (
            <div className="jt">
              <table>
                <thead>
                  <tr>
                    <th>关节</th>
                    <th>最大偏差</th>
                    <th>平均偏差</th>
                    <th>目标角</th>
                    <th>实测角</th>
                  </tr>
                </thead>
                <tbody>
                  {joints.map(([name, value]) => (
                    <tr key={name}>
                      <th>{name}</th>
                      <td className="num">{degrees(value.max_error_rad)}</td>
                      <td className="num">{degrees(value.mean_error_rad)}</td>
                      <td className="num">{value.target_rad.toFixed(3)}</td>
                      <td className="num">{value.measured_rad.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="card records-card" aria-label="执行轨迹记录">
      <div className="card-head">
        <h2>执行轨迹记录</h2>
        <div className="card-head-actions">
          <span className="meta">{records.length} 条</span>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => void refresh()}
          >
            {busy ? "读取中…" : "刷新"}
          </button>
        </div>
      </div>
      {message && <p className="rail-error">{message}</p>}
      {records.length === 0 ? (
        <p className="empty-cell">
          还没有执行记录。在「课程管理」页回放一个动作或整套课程，这里就会出现一条。
        </p>
      ) : (
        <div className="jt">
          <table className="records-table">
            <thead>
              <tr>
                <th>开始时间</th>
                <th>类型</th>
                <th className="col-name">名称</th>
                <th>速度</th>
                <th>轨迹时长</th>
                <th>腰14前倾</th>
                <th>结果</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {records.map((record) => (
                <tr
                  key={record.id}
                  className={selected === record.id ? "selected" : ""}
                  onClick={() => void openDetail(record.id)}
                >
                  <th>{formatTime(record.started_at)}</th>
                  <td>{record.kind === "course" ? "整套" : "单动作"}</td>
                  <td className="col-name">{record.label}</td>
                  <td className="num">{record.speed ?? "—"}×</td>
                  <td className="num">
                    {record.trajectory_duration_s?.toFixed(1) ?? "—"} s
                  </td>
                  <td className="num">
                    {record.waist_pitch_lean_deg === null
                      ? "—"
                      : `${record.waist_pitch_lean_deg.toFixed(2)}°`}
                  </td>
                  <td>
                    <span className={`chip ${OUTCOME_TONES[record.outcome] ?? ""}`}>
                      {OUTCOME_LABELS[record.outcome] ?? record.outcome}
                    </span>
                  </td>
                  <td className="col-action">
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={(event) => {
                        event.stopPropagation();
                        void openDetail(record.id);
                      }}
                    >
                      详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
