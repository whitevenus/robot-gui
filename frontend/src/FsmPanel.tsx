export interface FsmModeInfo {
  fsm_id: number;
  label: string;
  danger: boolean;
  hint: string;
  allowed: boolean;
}

export interface FsmState {
  available: boolean;
  reason: string;
  current: { id: number | null };
  /** 允许下发控制命令的 FSM ID 列表。 */
  whitelist: number[];
  modes: FsmModeInfo[];
}

/**
 * FSM ID → 模式名 的兜底表。
 *
 * 后端（还没升级到带 /api/v1/fsm/state 的版本）只给得出 ID。
 * 这张表抄的是官方《高层运动服务接口》的「模式 ID 说明」，
 * 所以不需要联网、不需要猜，ID 本身就能翻译。
 */
const FALLBACK_BY_ID: Record<number, string> = {
  0: "零力矩",
  1: "阻尼",
  2: "位控下蹲",
  3: "位控落座",
  4: "锁定站立",
  500: "常规运控",
  501: "常规运控",
  801: "走跑运控",
  802: "走跑运控",
};

/**
 * FSM ID → 页面标签。
 *
 * 优先用后端的模式目录（带白名单状态），拿不到就按官方 ID 表翻译；
 * 都没有时如实显示"未知模式"，不猜。
 */
export function fsmModeLabel(
  state: FsmState | null,
  currentId: number | null,
): string {
  if (state) {
    const match = state.modes.find((mode) => mode.fsm_id === currentId);
    if (match) return match.label;
  }
  if (currentId !== null && FALLBACK_BY_ID[currentId]) {
    return FALLBACK_BY_ID[currentId];
  }
  return "未知模式";
}

interface FsmPanelProps {
  state: FsmState | null;
  currentId: number | null;
  busy: boolean;
  onSwitch: (mode: FsmModeInfo) => void;
}

/**
 * 运动模式切换面板。
 *
 * 模式用官方的数字 ID 指定（`LocoClient.SetFsmId`），没有模式名要猜。
 *
 * 零力矩与阻尼会让机器人失去支撑力矩，站立中切换等于让它瘫倒，所以这两个按钮
 * 标成高风险并要求二次确认。切换能力默认关闭：后端没有配置允许的模式时，
 * 按钮显示原因而不是给一个点了没反应的样子。
 */
export function FsmPanel({
  state,
  currentId,
  busy,
  onSwitch,
}: FsmPanelProps) {
  const label = fsmModeLabel(state, currentId);
  const modes = state?.modes ?? [];
  const blocked = state !== null && !state.available;

  return (
    <section className="rail-section" aria-label="运动模式">
      <div className="rail-section-head">
        <h2>运动模式</h2>
        <span className="rail-current">
          {label}
          {currentId !== null && <em>FSM {currentId}</em>}
        </span>
      </div>
      {state === null ? (
        <p className="rail-reason">
          后端没有提供运动模式接口（/api/v1/fsm/state），无法列出可切换的模式。
        </p>
      ) : (
        <>
          <div className="mode-grid">
            {modes.map((mode) => {
              const isCurrent = mode.fsm_id === currentId;
              return (
                <button
                  key={mode.fsm_id}
                  type="button"
                  className={`mode-button${mode.danger ? " danger" : ""}${
                    isCurrent ? " current" : ""
                  }`}
                  disabled={!mode.allowed || busy}
                  title={mode.allowed ? mode.hint : state.reason}
                  onClick={() => onSwitch(mode)}
                >
                  <strong>{mode.label}</strong>
                  <small>{mode.hint}</small>
                </button>
              );
            })}
          </div>
          {blocked && state.reason ? (
            <p className="rail-reason">模式切换未开放：{state.reason}</p>
          ) : null}
        </>
      )}
      <p className="rail-note">
        切换运动模式会立即改变机器人的支撑方式。软件按钮不等于物理急停，
        现场异常请用遥控器停机。
      </p>
    </section>
  );
}
