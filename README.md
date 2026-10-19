# Robot GUI

Robot GUI 是用于 Unitree 机器人遥测与运动工作流的开发工作站。本应用
**仅连接真实设备并提供遥测**：必须配置 Unitree SDK2/DDS 连接，且不会生成
Mock/Fake 运行时遥测。硬件控制功能保持禁用。

## 连接真实 G1 运行

要求：Python 3.11+、Node.js 20+、Unitree SDK2 Python、CycloneDDS，并且能够通过
所选 DDS 网络接口访问 G1。

启动后端前，请先安装后端依赖并配置 DDS 网络接口：

```powershell
uv sync --directory backend --extra test
$env:ROBOT_GUI_MODE = "real"
$env:ROBOT_GUI_DDS_INTERFACE = "eth0"
uv run --directory backend uvicorn robot_gui.main:app --host 127.0.0.1
```

如果运行模式、SDK2 软件包、DDS 网络接口或必要的遥测初始化不可用，后端会明确报错并
启动失败，不会回退到模拟数据。请在另一个终端中启动前端：

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的 URL。页面会显示实际的 SDK2/DDS 连接状态以及接收数据源的新鲜度。
硬件控制仍不可用，后端会拒绝所有控制会话请求。

默认使用 G1 SDK 的 LowState 话题 `rt/lowstate`、BMS 话题
`rt/lf/bmsstate` 和次级 IMU 话题 `rt/lf/secondary_imu`。可通过
`ROBOT_GUI_G1_LOWSTATE_TOPIC`、`ROBOT_GUI_G1_BMS_TOPIC` 和
`ROBOT_GUI_G1_SECONDARY_IMU_TOPIC` 覆盖这些设置。只读 FSM ID 通过 G1
LocoClient 轮询；当前公开的 SDK API 未提供 FSM 模式读取接口，因此 UI 会将模式显示为未知，
而不会从 `mode_machine` 推断（后者是模型选择器）。

`ROBOT_GUI_TELEMETRY_HZ` 默认为 10，范围限制为 1–20 Hz。后端根据主机单调时钟记录的
接收时间，报告各数据源的 `fresh`、`delayed`、`offline`、`invalid` 或 `unknown` 状态。
如果缺少 SDK/网络接口或 DDS 初始化失败，后端将无法启动；不会回退到 Fake 数据，也不会启用
控制功能。本版本为只读版本，不构成硬件控制安全保障，也不代表已完成现场验收。

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
