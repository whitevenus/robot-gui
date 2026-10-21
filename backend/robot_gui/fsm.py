"""FSM 运动模式的读取与切换。

**官方接口**（G1 SDK 开发指南《高层运动服务接口》）：整机控制通过
`unitree::robot::g1::LocoClient` 这条 RPC 通道做，模式用**数字 ID** 指定：

    LocoClient.SetFsmId(int fsm_id)   # 设置运控模式
    LocoClient.GetFsmId()             # 获取运控所在模式

Python SDK 里还有几个语义化的糖：`ZeroTorque()` = SetFsmId(0)、`Damp()` = SetFsmId(1)、
`Start()` = SetFsmId(500)。注意 `Start()` 走的是 **500**（两自由度腰），
三自由度腰的机器人要用 **501**。

官方的模式 ID 表：

| ID  | 模式                | 备注             |
|-----|---------------------|------------------|
| 0   | 零力矩              | 无平衡控制       |
| 1   | 阻尼                | 无平衡控制       |
| 2   | 位控下蹲            | 无平衡控制       |
| 3   | 位控落座            | 无平衡控制       |
| 4   | 锁定站立            | 无平衡控制       |
| 500 | 常规运控            | 两自由度腰       |
| 501 | 常规运控-3Dof-waist | 三自由度腰       |
| 801 | 走跑运控            | 新版固件为 802   |

**官方对每一个模式切换接口都写着同一句备注**：「最终状态切换是否成功取决于内置运控的
状态切换逻辑」。也就是说 RPC 返回 0 **不代表模式真的切过去了**，所以这里切换完一定
回读一次 `GetFsmId()` 核对，模式没变就如实报"未变化"，不假装成功。

另外：**高层运动服务依赖内置运控，进入调试模式后内置运控完全退出、高层运动服务失效。**
所以接管腿（`rt/lowcmd` 全 29 关节）那条路和模式切换是互斥的。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 模式切换的落定等待上限。固件接受 SetFsmId 后不会立刻换状态，实测要好几秒。
#: 2026-10-21 真机实测：等 2.5 秒回读还是旧模式，等 10 秒才切过去。
SWITCH_SETTLE_TIMEOUT_S = 12.0


class FsmError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FsmMode:
    """一个可切换的运动模式。"""

    fsm_id: int
    label: str
    danger: bool
    hint: str


#: 页面上放出来的模式目录。
#:
#: 只放与本工作台相关的几个：零力矩 / 阻尼 / 锁定站立 / 常规运控。
#: 2（位控下蹲）、3（位控落座）、706（蹲起）、702（躺起）是过渡用的中间态，
#: 801/802（走跑运控）不在回放允许范围里，都不进目录。
#:
#: 常规运控只列 501：本机是三自由度腰（mode_machine=5、29DoF），
#: 500 是两自由度腰的变体，对不上这台机器。
FSM_MODES: tuple[FsmMode, ...] = (
    FsmMode(
        fsm_id=0,
        label="零力矩",
        danger=True,
        hint="输出力矩恒为 0，机器人失去支撑，会瘫倒。",
    ),
    FsmMode(
        fsm_id=1,
        label="阻尼",
        danger=True,
        hint="只有速度阻尼，没有支撑力矩，同样会瘫倒。",
    ),
    FsmMode(
        fsm_id=4,
        label="锁定站立",
        danger=False,
        hint="纯弹簧-阻尼姿态保持，没有平衡控制，姿态偏了不会迈步救场。",
    ),
    FsmMode(
        fsm_id=501,
        label="常规运控",
        danger=False,
        hint="内置平衡控制生效。本机是三自由度腰版本，对应 501。",
    ),
)

MODES_BY_ID: dict[int, FsmMode] = {mode.fsm_id: mode for mode in FSM_MODES}


@dataclass(frozen=True)
class FsmSettings:
    """FSM 切换的运行配置。"""

    #: 允许切换的模式 ID。**默认全部允许**——切换本身有操作员二次确认，
    #: 门槛交给人和遥控器，不靠配置项。需要收窄时才设 `ROBOT_GUI_FSM_MODES`。
    allowed: tuple[int, ...] | None = None

    @classmethod
    def from_env(cls, raw: str | None) -> "FsmSettings":
        allowed: list[int] = []
        for part in (raw or "").split(","):
            entry = part.strip()
            if not entry:
                continue
            if not entry.isdigit():
                logger.warning("忽略非数字的 FSM 模式 ID %r。", entry)
                continue
            fsm_id = int(entry)
            if fsm_id not in MODES_BY_ID:
                logger.warning(
                    "忽略不在目录里的 FSM 模式 ID %d，可选：%s",
                    fsm_id,
                    ", ".join(str(item) for item in sorted(MODES_BY_ID)),
                )
                continue
            allowed.append(fsm_id)
        if not (raw or "").strip():
            return cls(allowed=None)  # 未配置 = 全部允许
        return cls(allowed=tuple(allowed))

    @property
    def enabled(self) -> bool:
        """切换能力是否开放（未配置白名单时对目录里的模式全部开放）。"""
        return True

    def permits(self, fsm_id: int) -> bool:
        if self.allowed is None:
            return fsm_id in MODES_BY_ID
        return fsm_id in self.allowed


class FsmService:
    """读取当前运动模式，并在受控条件下请求切换。"""

    def __init__(
        self,
        *,
        settings: FsmSettings,
        adapter: Any,
        telemetry: Any,
        controls_enabled: bool,
        control: Any | None = None,
        fsm_whitelist: tuple[int, ...] = (),
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._telemetry = telemetry
        self._controls_enabled = controls_enabled
        self._control = control
        self._whitelist = fsm_whitelist
        #: 最近一次回读到的 fsm_mode（0=静态、1=动态）。只在切换时刷新，
        #: 不放进高频的遥测轮询里。
        self._fsm_mode: int | None = None

    # ---- 状态 ----

    def _current(self) -> dict[str, Any]:
        snapshot = self._telemetry.snapshot("real", None, None)
        fsm = snapshot.get("robot", {}).get("fsm")
        if not isinstance(fsm, dict):
            return {"id": None}
        return {"id": fsm.get("id")}
    def _reason(self) -> str:
        if not self._controls_enabled:
            return "真机控制未开启（ROBOT_GUI_CONTROLS_ENABLED=1）前不允许切换运动模式。"
        return ""

    def state(self) -> dict[str, Any]:
        current = self._current()
        reason = self._reason()
        modes = [
            {
                "fsm_id": mode.fsm_id,
                "label": mode.label,
                "danger": mode.danger,
                "hint": mode.hint,
                "allowed": self._settings.permits(mode.fsm_id) and not reason,
            }
            for mode in FSM_MODES
        ]
        return {
            "available": not reason,
            "reason": reason,
            "current": {**current, "mode": self._fsm_mode},
            # 允许下发控制命令的 FSM ID 列表，页面据此提示"当前能不能回放"。
            "whitelist": list(self._whitelist),
            "modes": modes,
        }

    # ---- 切换 ----

    def request_mode(self, fsm_id: int, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise FsmError("confirmation_required", "切换运动模式需要操作员明确确认。")
        reason = self._reason()
        if reason:
            raise FsmError("capability_disabled", reason)
        mode = MODES_BY_ID.get(fsm_id)
        if mode is None:
            raise FsmError(
                "fsm_mode_unknown",
                f"未知的运动模式 ID {fsm_id}，可选："
                f"{', '.join(str(item) for item in sorted(MODES_BY_ID))}。",
            )
        if not self._settings.permits(fsm_id):
            raise FsmError(
                "fsm_mode_not_allowed",
                f"模式 {mode.label}（{fsm_id}）不在允许列表 "
                f"[{', '.join(str(item) for item in self._settings.allowed or ())}] 中。",
            )
        snapshot = self._telemetry.snapshot("real", None, None)
        if snapshot["sources"]["low_state"]["state"] != "fresh":
            raise FsmError(
                "telemetry_stale",
                "LowState 遥测不是最新，拒绝切换运动模式。",
            )
        if self._control is not None:
            phase = self._control.state().get("phase")
            if phase not in (None, "idle", "fault"):
                raise FsmError(
                    "control_busy",
                    f"控制会话正在进行（{phase}），先停止并交还控制权再切换运动模式。",
                )
        before = self._current()
        code, detail = self._adapter.switch_mode(fsm_id)
        if code != 0:
            raise FsmError(
                "fsm_switch_failed",
                f"切换到 {mode.label}（{fsm_id}）被机器人拒绝，错误码 {code}。{detail}",
            )
        # 固件的模式切换有落定时间：请求被接受后可能要好几秒才真的切过去。
        # 2026-10-21 真机实测：同一个 SetFsmId(501)，等 2.5 秒回读还是旧模式、
        # 被误判成"没切成"，等 10 秒才切过去。所以这里轮询等它落定，而不是
        # 立刻回读一次就下结论。
        after = self._wait_for_mode(fsm_id)
        # 把回读结果写回遥测：否则页面在下一次轮询（最多 1 秒）之前还会显示旧模式。
        self._telemetry.ingest(
            "fsm",
            {"id": after.get("id"), "mode": after.get("mode")},
        )
        if after.get("id") == fsm_id:
            logger.info("FSM 模式已切换：%s → %s", before.get("id"), fsm_id)
            return self.state()

        # 官方对每个切换接口都写了"最终状态切换是否成功取决于内置运控的状态切换逻辑"，
        # 所以 RPC 成功不等于模式真的切过去了。这里**如实报错**，不返回 200 假装成功。
        current = after.get("id")
        fsm_mode = after.get("mode")
        hint = ""
        if fsm_mode == 1:
            hint = (
                "机器人当前处于**运动状态**（fsm_mode=1），固件会拒绝这种切换："
                "先让机器人静立，或用遥控器 L2+B 进阻尼模式再切。"
            )
        raise FsmError(
            "fsm_switch_rejected",
            f"模式切换没有被固件执行：请求切到 {mode.label}（{fsm_id}）已被接受，"
            f"但 {SWITCH_SETTLE_TIMEOUT_S:.0f} 秒内回读到的仍是 {current}。"
            f"{hint}"
            "也可以直接用遥控器切换运动模式。",
        )

    def _wait_for_mode(self, fsm_id: int) -> dict[str, Any]:
        """轮询回读，直到模式真的切过去（或超时）。"""
        deadline = time.monotonic() + SWITCH_SETTLE_TIMEOUT_S
        state = self._adapter.refresh_mode()
        while state.get("id") != fsm_id and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self._adapter.refresh_mode()
        self._fsm_mode = state.get("mode")
        return state
