import { useMemo, useState } from "react";

export interface JointRow {
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
}

/** 组名 → 页面上的中文标签。键与后端 profile 的 group 取值一致。 */
const GROUP_LABELS: Record<string, string> = {
  waist: "腰部",
  left_arm: "左臂",
  right_arm: "右臂",
  left_leg: "左腿",
  right_leg: "右腿",
};

/** 展示顺序：先把要接管的腰与双臂放在前面，腿放最后（默认折叠）。 */
const GROUP_ORDER = ["waist", "left_arm", "right_arm", "left_leg", "right_leg"];

const UPPER_GROUPS = new Set(["waist", "left_arm", "right_arm"]);

function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(3);
}

/** 温度是 [t0, t1] 两个通道，0 表示该通道无效，过滤掉再显示。 */
function formatTemperature(value: number[] | null | undefined): string {
  if (!value) return "—";
  const valid = value.filter((item) => item > 0);
  return valid.length ? valid.join(", ") : "—";
}

interface JointPanelProps {
  joints: JointRow[];
}

/**
 * 关节遥测表。
 *
 * 按组分段（腰 / 左臂 / 右臂 / 腿），默认只看上肢——本工作台只接管双臂，
 * 腿与腰由机器人内置运控负责，29 行连成一条既难扫读也没有重点。
 *
 * 恒定的列（ddq、模式、状态码、电压）不再显示：真机上它们整列是常量，纯噪声。
 */
export function JointPanel({ joints }: JointPanelProps) {
  const [upperOnly, setUpperOnly] = useState(true);

  const grouped = useMemo(() => {
    const buckets = new Map<string, JointRow[]>();
    for (const joint of joints) {
      const list = buckets.get(joint.group) ?? [];
      list.push(joint);
      buckets.set(joint.group, list);
    }
    return GROUP_ORDER.filter((group) => buckets.has(group)).map((group) => ({
      group,
      label: GROUP_LABELS[group] ?? group,
      rows: buckets.get(group) ?? [],
    }));
  }, [joints]);

  const visible = grouped.filter(
    (entry) => !upperOnly || UPPER_GROUPS.has(entry.group),
  );
  const hidden = grouped.filter(
    (entry) => upperOnly && !UPPER_GROUPS.has(entry.group),
  );
  const hiddenCount = hidden.reduce((total, entry) => total + entry.rows.length, 0);
  const hiddenLabel = hidden.map((entry) => entry.label).join(" / ");

  return (
    <section className="card joint-card" aria-label="G1 29DoF 关节遥测">
      <div className="card-head">
        <h2>关节状态</h2>
        <button
          className="toggle"
          type="button"
          aria-pressed={upperOnly}
          onClick={() => setUpperOnly((value) => !value)}
        >
          <span className="sw" aria-hidden="true" />
          只看上肢
        </button>
      </div>
      <div className="jt">
        <table>
          <thead>
            <tr>
              <th>关节</th>
              <th>q (rad)</th>
              <th>dq (rad/s)</th>
              <th>tau (Nm)</th>
              <th>温度 raw</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((entry) => (
              <GroupRows key={entry.group} label={entry.label} rows={entry.rows} />
            ))}
            {hidden.length > 0 && (
              <tr className="group-row collapsed">
                <th colSpan={5}>
                  {hiddenLabel} · {hiddenCount} 关节已折叠
                </th>
              </tr>
            )}
            {joints.length === 0 && (
              <tr>
                <td colSpan={5} className="empty-cell">
                  尚未收到可用 LowState；关节数据不可用，不以默认数值代替。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {upperOnly && hiddenCount > 0 && (
        <p className="card-foot">
          另有 {hiddenCount} 个腿部关节已折叠，点「只看上肢」展开。
        </p>
      )}
    </section>
  );
}

function GroupRows({ label, rows }: { label: string; rows: JointRow[] }) {
  return (
    <>
      <tr className="group-row">
        <th colSpan={5}>{label}</th>
      </tr>
      {rows.map((joint) => (
        <tr key={joint.name}>
          <th scope="row">{joint.name}</th>
          <td className="num">{formatNumber(joint.q_rad)}</td>
          <td className="num">{formatNumber(joint.dq_rad_s)}</td>
          <td className="num">{formatNumber(joint.tau_est_nm)}</td>
          <td className="num">{formatTemperature(joint.temperature_raw)}</td>
        </tr>
      ))}
    </>
  );
}
