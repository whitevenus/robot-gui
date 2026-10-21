# Robot GUI

Robot GUI 是用于 Unitree 机器人遥测与运动工作流的开发工作站。本应用
**仅连接真实设备**：必须配置 Unitree SDK2/DDS 连接，且不会生成
Mock/Fake 运行时遥测。硬件控制默认关闭，需由配置显式开启；开启后每一次真机动作
都要求操作员逐次确认。

## 本版范围

- G1 实时遥测（29 个关节、BMS、两路 IMU、FSM）与浏览器内的三维姿态显示。
- **`group` + `frames` 动作格式的动作库**，与
  [g1-action-studio](references/g1-action-studio) 兼容 —— ECHO-G 生成的动作可以直接导入。
- **双臂 14 关节的真机回放**，走 `rt/arm_sdk`：
  下发任何控制命令之前先做轨迹规划、关节限位与自碰撞检查；
  从实测姿态平滑进入动作，结束后返回该姿态，并交还控制权。
- 可选的机器人自带扬声器讲解语音（16 kHz 单声道 16 bit PCM），与动作同步起播。

FSM 模式切换**已开放**（页面上直接点，切换前有操作员二次确认）；
键盘行走、手掰示教**不在本版**，对应控件保持禁用，而不是假装可用。
**动作必须是 `group: "arms"`（14 维）**，含腰的 `arms_waist` 动作会被直接拒绝——
腰的三维锁在接管瞬间的姿态，不执行动作里的腰轨迹。
腰的**槽位仍由本工作台下发**：2026-10-21 真机实测，`rt/arm_sdk` 的权重一旦升到 1，
腰的控制权就无条件归我们（per-motor `mode` 写 0 或 1 没有任何区别），
腰槽位留空时腰会被推到 +32.8° 的机械限位，固件的腰部平衡回路退让、躯干前倾 +40°。
腿始终由机器人内置运控负责。

## 连接真实 G1 运行

要求：Python 3.11+、Node.js 20+、Unitree SDK2 Python、CycloneDDS，并且能够通过
所选 DDS 网络接口访问 G1。

启动后端前，请先安装后端依赖并配置 DDS 网络接口：

```powershell
uv sync --directory backend --extra test
$env:ROBOT_GUI_MODE = "real"
$env:ROBOT_GUI_DDS_INTERFACE = "eth0"
$env:ROBOT_GUI_DATA_DIR = "C:\robot-gui-data"
uv run --directory backend uvicorn robot_gui.main:app --host 127.0.0.1
```

`ROBOT_GUI_DATA_DIR` 是动作库目录。它**不会**被静默替换成安装目录下的默认路径：
未配置时动作接口返回 `action_store_not_configured`。

`g1-action-studio` 以**显式 path 依赖**声明在
[`backend/pyproject.toml`](backend/pyproject.toml) 中，参考实现随仓库一起分发。
后端直接调用它的 `planner` / `model` / `robot` 模块，而不是通过它的 CLI 起子进程。

如果运行模式、SDK2 软件包、DDS 网络接口或必要的遥测初始化不可用，后端会明确报错并
启动失败，不会回退到模拟数据。请在另一个终端中启动前端：

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的 URL。页面会显示实际的 SDK2/DDS 连接状态以及接收数据源的新鲜度。

## 真机控制

不设置 `ROBOT_GUI_CONTROLS_ENABLED=1` 就不会接受任何真机控制。开启后，回放请求
只有在以下条件全部满足时才会被接受：操作员已确认、LowState 遥测新鲜、
`mode_machine` 与 29DoF 机型一致、当前 FSM 在 `ROBOT_GUI_FSM_WHITELIST`（默认 `501`）列表内。

轨迹规划与关节限位 / 自碰撞检查都在发布 `rt/arm_sdk` **之前**完成，
因此被拒绝的动作不会产生任何控制命令。

已知出厂状态的 G1 上 `rt/arm_sdk` 存在不止一个发布端点，**只读的控制权调查是前置条件**。
完整的部署步骤、首次上机清单与本版边界见
[`docs/deployment-g1.md`](docs/deployment-g1.md)。

默认使用 G1 SDK 的 LowState 话题 `rt/lowstate`、BMS 话题
`rt/lf/bmsstate` 和次级 IMU 话题 `rt/lf/secondary_imu`。可通过
`ROBOT_GUI_G1_LOWSTATE_TOPIC`、`ROBOT_GUI_G1_BMS_TOPIC` 和
`ROBOT_GUI_G1_SECONDARY_IMU_TOPIC` 覆盖这些设置。只读 FSM ID 通过 G1
LocoClient 轮询；当前公开的 SDK API 未提供 FSM 模式读取接口，因此 UI 会将模式显示为未知，
而不会从 `mode_machine` 推断（后者是模型选择器）。

`ROBOT_GUI_TELEMETRY_HZ` 默认为 10，范围限制为 1–20 Hz。后端根据主机单调时钟记录的
接收时间，报告各数据源的 `fresh`、`delayed`、`offline`、`invalid` 或 `unknown` 状态。
如果缺少 SDK/网络接口或 DDS 初始化失败，后端将无法启动；不会回退到 Fake 数据，也不会启用
控制功能。本版本的控制路径不构成硬件控制安全保障，也不代表已完成现场验收。

3D 查看器内置了 Unitree 官方的 G1 29DoF rev 1.0 URDF，以及以下来源中引用的全部 35 个
STL 网格：
[`unitreerobotics/unitree_ros`](https://github.com/unitreerobotics/unitree_ros/tree/7c40519e02d7dd16c06b25fe3fa3b67fdeb7cd74/robots/g1_description),
版本为 `7c40519e02d7dd16c06b25fe3fa3b67fdeb7cd74`。这些资源由本地提供，运行时
不会请求外部模型。BSD 3-Clause 许可条款和版权归属信息见
[`frontend/public/models/unitree-g1/LICENSE`](frontend/public/models/unitree-g1/LICENSE)。
此模型对应 `mode_machine` 5；依赖其可视化结果前，请确认部署的机器人与该模型一致。

查看器仅显示测得的关节/IMU 遥测数据，支持旋转视角、缩放和重置；不会模拟动力学、证明平衡，
也不提供硬件安全验收。如果模型映射或数据源新鲜度信息不完整，查看器会报告原因，关节表仍可
使用。仅供测试的遥测数据夹具不能作为应用运行模式使用。

## 验证

```powershell
uv run --directory backend pytest
cd frontend
npm test
npm run build
```
