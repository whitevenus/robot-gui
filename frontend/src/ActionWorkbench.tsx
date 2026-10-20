import { useCallback, useEffect, useMemo, useState } from "react";

interface ActionSummary {
  name: string;
  group: string;
  frames: number;
  duration_s: number;
  source: string;
  has_audio: boolean;
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
}

const PHASE_LABELS: Record<string, string> = {
  idle: "空闲",
  planning: "规划中",
  acquiring: "接管中",
  playing: "回放中",
  releasing: "交还控制权",
  fault: "故障",
};

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      return `${detail.code ?? "error"}：${detail.reason ?? ""}`.trim();
    }
    return JSON.stringify(detail ?? body);
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}

export function ActionWorkbench() {
  const [actions, setActions] = useState<ActionSummary[]>([]);
  const [state, setState] = useState<ControlState | null>(null);
  const [message, setMessage] = useState("");
  const [importName, setImportName] = useState("");
  const [importBody, setImportBody] = useState("");
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);

  const refreshActions = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/actions");
      if (!response.ok) {
        setMessage(await readError(response));
        return;
      }
      const body = await response.json();
      setActions(body.actions ?? []);
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

  const busyControl = useMemo(
    () => state !== null && state.phase !== "idle" && state.phase !== "fault",
    [state],
  );

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
      await refreshActions();
    } finally {
      setBusy(false);
    }
  }

  async function runReplay(name: string, withAudio: boolean) {
    if (
      !window.confirm(
        `即将在真机上回放 ${name}。请确认现场安全、机器人处于允许的 FSM 模式，且随时可以按停止。`,
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

  return (
    <section className="action-panel" aria-label="动作库与真机回放">
      <div className="section-heading">
        <div>
          <p className="eyebrow">ACTION LIBRARY</p>
          <h2>动作库与真机回放</h2>
        </div>
        <span>
          {state ? PHASE_LABELS[state.phase] ?? state.phase : "读取控制状态"}
          {state?.action ? ` · ${state.action}` : ""}
        </span>
      </div>

      {!available && (
        <p className="disabled-explanation">
          真机控制未开启。设置 ROBOT_GUI_CONTROLS_ENABLED=1 并确认现场安全后，才会接受回放请求。
        </p>
      )}
      {state?.phase === "fault" && state.error_code && (
        <p className="source-error">控制会话失败：{state.error_code} {state.message}</p>
      )}
      {state && (
        <p className="action-flags">
          重力补偿：{state.gravity_compensation ? "开" : "关"}
          {state.gravity_compensation
            ? "（上肢力矩按实测姿态前馈，抵消 PD 扛重力造成的下垂）"
            : "（未开启，上肢前伸会明显下垂）"}
        </p>
      )}
      {message && <p className="action-message">{message}</p>}

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>动作</th>
              <th>关节组</th>
              <th>帧数</th>
              <th>时长 (s)</th>
              <th>来源</th>
              <th>语音</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {actions.map((action) => (
              <tr key={action.name}>
                <th scope="row">{action.name}</th>
                <td>{action.group}</td>
                <td>{action.frames}</td>
                <td>{action.duration_s.toFixed(2)}</td>
                <td>{action.source}</td>
                <td>{action.has_audio ? "有" : "无"}</td>
                <td className="action-cell">
                  <button
                    type="button"
                    disabled={!available || busyControl || busy}
                    onClick={() => void runReplay(action.name, action.has_audio)}
                  >
                    回放
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void runDelete(action.name)}
                  >
                    删除
                  </button>
                  <a href={`/api/v1/actions/${encodeURIComponent(action.name)}`}>导出</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {actions.length === 0 && (
          <p className="empty-state">
            动作库为空。导入 g1-action-studio 兼容的 group+frames JSON，或用 ECHO-G
            转出的上肢轨迹。
          </p>
        )}
      </div>

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
          rows={5}
          placeholder='{"group": "arms_waist", "frames": [{"t": 0, "q": [ ... 17 项 ... ]}]}'
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

      <div className="action-footer">
        <label htmlFor="action-select">选择要回放的动作</label>
        <select
          id="action-select"
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
        >
          <option value="">— 请选择 —</option>
          {actions.map((action) => (
            <option key={action.name} value={action.name}>
              {action.name}（{action.group} · {action.duration_s.toFixed(1)}s）
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!available || busyControl || busy || !selected}
          onClick={() => {
            const target = actions.find((item) => item.name === selected);
            void runReplay(selected, target?.has_audio ?? false);
          }}
        >
          确认并回放
        </button>
        <button
          type="button"
          disabled={!busyControl}
          onClick={() => void runRelease()}
        >
          停止并交还控制权
        </button>
      </div>
    </section>
  );
}
