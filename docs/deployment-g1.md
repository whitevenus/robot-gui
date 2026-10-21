# 真机部署与验收清单

本文是 Robot GUI 在 Unitree G1 上的部署步骤与首次上机前的检查清单。
面向现场操作员，不替代受监督的人工验收。

> 当前控制能力范围：**只有双臂 14 关节**（`rt/arm_sdk` 的 slot 15-28）的动作回放。
> 腰（slot 12-14）与腿（slot 0-11）**都不接管**，由机器人内置运控负责。
> 键盘行走、手掰示教尚未开放，页面上的对应按钮保持禁用。
> FSM 运动模式**可以切换**，但默认关闭：要用得同时满足 `ROBOT_GUI_CONTROLS_ENABLED=1`
> 和 `ROBOT_GUI_FSM_MODES` 列出了要放开的模式 ID，详见 §2.3。

---

## 1. 部署位置

| 组件 | 位置 | 说明 |
|---|---|---|
| 后端 | 机器人本机（或同网段电脑） | 必须能访问 `rt/lowstate` 与 `rt/arm_sdk` |
| 前端 | 任意浏览器的开发电脑 | 通过 HTTP/WS 连到后端 |

后端跑在机器人本机时不需要 SSH 转发：音频直接用 SDK2 的 `AudioClient` 推流。

---

## 2. 后端环境

### 2.1 Python 版本

机器人的 `~/venvs/g1-sdk` 是 **Python 3.8.10**（unitree-sdk2py 1.0.1）。
本工作台要求 **Python 3.11+**（g1-action-studio 的 `requires-python`）。

两者必须分开：SDK 用 3.8 的系统环境，工作台另建 3.11+ 的虚拟环境。
`unitree_sdk2py` 本身兼容 3.8，在 3.11 上也能装（原生扩展 `crc_aarch64.so` 随包提供）。

### 2.2 安装

```bash
cd robot-gui
uv sync --directory backend --extra test
# Unitree SDK2 不是本仓库的依赖，是机器人上的环境级安装。
# uv sync 会清掉不在 pyproject 里的包，所以要在 sync 之后补装（或用 uv sync --inexact）。
uv pip install --python backend/.venv/bin/python /usr/local/src/unitree_sdk2_python
```

`g1-action-studio` 通过 `backend/pyproject.toml` 的 `tool.uv.sources` 以**显式 path 依赖**
指向 `references/g1-action-studio`，随仓库分发，不需要单独安装。
`planning` extra 会装 MuJoCo（几何检测）与 OMPL（衔接段绕行，仅 Linux）。

> **部署时容易被裁掉的一步**：`references/g1-action-studio` 必须在目标机上真实存在。
> 2026-10-20 实测这台机器人上该目录是空的（部署时被裁剪），导致 `uv sync` 找不到包。
> 修复方式是把已部署的一份源码拷回去：
>
> ```bash
> mkdir -p references/g1-action-studio
> cp -r /usr/local/src/g1-action-studio-rebuild-26e4f4c/src \
>       /usr/local/src/g1-action-studio-rebuild-26e4f4c/examples \
>       /usr/local/src/g1-action-studio-rebuild-26e4f4c/pyproject.toml \
>       references/g1-action-studio/
> ```

`uv` 通常不在 PATH 里，用绝对路径：

```bash
export PATH=$HOME/.local/bin:$PATH
uv --version    # 0.13.1 aarch64
```

### 2.3 配置

```bash
export ROBOT_GUI_MODE=real
export ROBOT_GUI_DDS_INTERFACE=eth0          # 机器人内部 DDS 网卡
export ROBOT_GUI_DATA_DIR=/opt/robot-gui-data # 动作库目录，必须显式配置
export ROBOT_GUI_CONTROLS_ENABLED=1           # 开启真机控制；不设置则只读
export ROBOT_GUI_FSM_WHITELIST=501            # 允许回放的 FSM（501=三自由度腰部常规运控）
export ROBOT_GUI_TRANSITION_SECONDS=2.0       # 进入/返回的过渡段时长
export ROBOT_GUI_REPLAY_SPEED=1.0
export ROBOT_GUI_HOLD_SOAK_S=3.0               # 动作结束后的沉降观察窗口
```

| 变量 | 默认 | 含义 |
|---|---|---|
| `ROBOT_GUI_MODE` | `real` | 只接受 `real`；其他值启动即失败 |
| `ROBOT_GUI_DDS_INTERFACE` | 无 | **必填**，DDS 网卡 |
| `ROBOT_GUI_DATA_DIR` | 无 | **动作库目录**。未配置时动作接口返回 `action_store_not_configured`，不会静默回退到安装目录 |
| `ROBOT_GUI_CONTROLS_ENABLED` | `false` | 开启后才接受回放请求 |
| `ROBOT_GUI_FSM_WHITELIST` | `501` | 逗号分隔；不在列表内的 FSM 一律拒绝下发 |
| `ROBOT_GUI_FSM_MODES` | 无 | 允许切换的 FSM 模式 ID，逗号分隔，如 `0,1,4,501`。**留空 = 模式切换整体关闭**，页面上按钮显示原因而不是假可用 |
| `ROBOT_GUI_GAINS` | 内置 60/1.5 | 增益 JSON 路径。只需 `arms` 一组；`waist` 组是遗留，接管范围收窄后不生效 |
| `ROBOT_GUI_TRANSITION_SECONDS` | `2.0` | 进入与返回的衔接段时长 |
| `ROBOT_GUI_REPLAY_SPEED` | `1.0` | 动作速度倍率 |
| `ROBOT_GUI_HOLD_SOAK_S` | `3.0` | 动作结束后的沉降观察窗口，见 [§6.1](#61-沉降窗口与前倾峰值) |
| `ROBOT_GUI_LOWER_BODY_DRIFT_DEG` | `5` | 下盘（腿 + 腰）漂移告警阈值 |
| `ROBOT_GUI_WAIST_SLOT_MODE` | `0` | 腰槽位的 `motor_cmd.mode`。`0`=不接管腰；`1`=**诊断档**，见 §6.3 |
| `ROBOT_GUI_AUDIO_APP` | `robot-gui` | 音频流的应用标识 |

### 2.4 启动

```bash
cd robot-gui
uv run --directory backend uvicorn robot_gui.main:app --host 0.0.0.0 --port 8000
```

前端：

```bash
cd robot-gui/frontend
npm install
npm run dev          # 开发
npm run build        # 产物在 frontend/dist，可交给任意静态服务器
```

**开发机连机器人**（后端在机器人、只在本机开前端）：

```bash
export ROBOT_GUI_BACKEND=http://192.168.110.165:8000
npm run dev -- --host 0.0.0.0 --port 5173
```

不设 `ROBOT_GUI_BACKEND` 时代理仍指向本机 8000（机器人本机部署的行为不变）。

机器人上的封装脚本 `~/robot-gui.sh`：

```bash
bash ~/robot-gui.sh start            # 只读
bash ~/robot-gui.sh start-control    # 开启真机控制 + 机器人扬声器语音
bash ~/robot-gui.sh restart-control  # 同上并重启
bash ~/robot-gui.sh status
bash ~/robot-gui.sh stop
```

`start-control` 等价于设置 `ROBOT_GUI_CONTROLS_ENABLED=1`、`ROBOT_GUI_FSM_WHITELIST=501`、
`ROBOT_GUI_AUDIO_ENABLED=1` 后再启动。脚本用 `setsid` 启动，SSH 断开或终端关闭都不会带走进程。

**部署安全**：HTTP 服务面向局域网且不实现登录（FR-20）。只放在受信任网络，
禁止直接暴露到公网。页面上的"确认"是误触防护，**不是身份认证**。

### 2.5 Unitree SDK2 的 CRC 动态库
SDK2 的 `setup.py` 不把 `utils/lib/*.so` 打进 wheel，`pip`/`uv` 安装完成后
`crc_aarch64.so` 会缺失。SDK 的 `utils/crc.py` 用
`ctypes.CDLL(<utils>/lib/crc_<arch>.so)` 加载它，缺文件时报的是
`cannot open shared object file`，指向共享库而不是安装不完整，很难排查。

更隐蔽的是**只读遥测不经过 CRC**：后端能连上机器人、页面能显示数据，
只有真正下发 LowCmd 的那一刻才失败——也就是接管动作做到一半才炸。

`robot_gui/sdk_runtime.py` 在导入 SDK 之前把 `.so` 从机器人上的已知 SDK 位置
（`/usr/local/src/unitree_sdk2_python`、`/home/unitree/Program/unitree_sdk2_python`）
补进虚拟环境；补不齐时启动日志会给出明确说明。也可以用
`UNITREE_SDK_LIB_DIR` 指定来源目录。**每次 `uv sync` 之后需要重新补一次**
（后端下次启动时会自动补上）。

### 2.6 FSM 运动模式切换

官方《高层运动服务接口》把整机控制（含模式切换）放在 `unitree::robot::g1::LocoClient`
这条 RPC 通道上，模式用**数字 ID** 指定：

```python
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
client = LocoClient(); client.SetTimeout(3.0); client.Init()
client.GetFsmId()        # 读当前模式 ID
client.SetFsmId(501)     # 切模式
```

官方「模式 ID 说明」：

| ID | 模式 | 备注 |
|---|---|---|
| 0 | 零力矩 | 无平衡控制 |
| 1 | 阻尼 | 无平衡控制 |
| 2 | 位控下蹲 | 无平衡控制 |
| 3 | 位控落座 | 无平衡控制 |
| 4 | 锁定站立 | 无平衡控制 |
| 500 | 常规运控 | 两自由度腰 |
| 501 | 常规运控-3Dof-waist | **本机是这个**（29DoF 三自由度腰） |
| 801 | 走跑运控 | 新版固件为 802 |

Python SDK 另有语义化的糖：`ZeroTorque()` = SetFsmId(0)、`Damp()` = SetFsmId(1)、
`Start()` = SetFsmId(500)。**注意 `Start()` 走的是 500（两自由度腰）**，本机要用 501。

因此：

1. **默认关闭。** `ROBOT_GUI_FSM_MODES` 不设置时，页面上的模式按钮全部禁用并显示原因。
2. **双重门控。** 需要同时满足 `ROBOT_GUI_CONTROLS_ENABLED=1` 和模式 ID 在
   `ROBOT_GUI_FSM_MODES` 里，两个都满足才放行。
3. **只放目录里的 ID。** 配置写的是官方 ID，例如 `ROBOT_GUI_FSM_MODES="0,1,4,501"`；
   写错或写了目录外的 ID 会被忽略并打日志，不会静默生效。
4. **切换后回读。** 官方对每个切换接口都写着「**最终状态切换是否成功取决于内置运控的
   状态切换逻辑**」——RPC 返回 0 不代表模式真切过去了。所以后端切完立刻再读一次
   `GetFsmId()` 核对，没变就如实记警告，不假装成功。
5. **拒绝时机的保护。** LowState 遥测不新鲜、或有控制会话在跑时拒绝切换。
6. **和调试模式互斥。** 官方明确：高层运动服务依赖内置运控，**进入调试模式后内置运控
   完全退出、高层运动服务失效**。所以将来接管腿（`rt/lowcmd` 全 29 关节）那条路
   和模式切换不能同时用。

**零力矩与阻尼会让站立中的机器人失去支撑并瘫倒。** 页面上这两个按钮标为高风险，
点了要再确认一次；现场仍需遥控器值守。**软件按钮不等于物理急停。**

启用前建议先只读确认一次当前模式（不切换）：

```bash
python3 -c "
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
c = LocoClient(); c.SetTimeout(3.0); c.Init()
print('当前 FSM:', c.GetFsmId())
"
```

---

## 3. 动作库准备

动作库目录结构：

```
$ROBOT_GUI_DATA_DIR/
├── p1_s0.json          # g1-action-studio 兼容的 group+frames 动作
├── p2_s3.json
└── audio/
    ├── p1_s0.pcm       # 16 kHz / 单声道 / 16 bit 裸 PCM，与动作同名
    └── p2_s3.pcm
```

把 ECHO-G 转出的动作拷进去即可（`echo-g/tools/echo_to_g1_action.py` 的产物）。
同名 `.pcm` 存在时，回放会在进入段结束后起播语音，与手势对齐。

**动作必须是 `group: "arms"`（14 维）。** 含腰的 `arms_waist`（17 维）动作会被直接拒绝
（错误码 `action_group_unsupported`），因为腰不由我们驱动。转换器默认就是 `--group arms`。

也可以直接在页面上粘贴 JSON 导入。

---

## 4. 首次上机前：控制权只读调查（**必做**）

`rt/arm_sdk` 上已知存在**两个发布端点**（`unitree-g1` 服务 + 本工作台）。
在弄清仲裁机制之前启动自制发布者，有重复下发关节指令的风险。

在机器人上执行（全部只读，不创建发布者）：

```bash
python3 inspect_g1_dds_publications.py eth0
python3 observe_g1_arm_sdk.py eth0 --seconds 15
python3 inspect_g1_loco_fsm.py eth0
systemctl show -p MainPID -p ExecStart unitree-g1.service robot-g1.service
```

要拿到的结论：

1. `rt/arm_sdk` 上另一个发布端点是谁，它与 `motor_cmd[29].q`（0-1 权重）的关系是什么；
2. 本工作台发布时，内置运控会不会同时下发同一批关节；
3. 结论不明确就不要开 `ROBOT_GUI_CONTROLS_ENABLED=1`。

### 4.1 现场实测结果（2026-10-20，`.165`）

只读调查已经在这台机器人上跑过，结论如下：

| 观测项 | 结果 |
|---|---|
| `unitree-g1.service` | `unitree_g1 eth0 29`，**开机自启**，一直在跑 |
| `rt/arm_sdk` 实际流量（15 秒被动订阅） | **0 条 LowCmd**（同期 `rt/lowstate` 15740 条） |
| FSM（开机后） | 先读到 **4**，随后机器人自行切到 **501** |
| `mode_machine` | **5**（29DoF，符合本工作台要求） |
| 躯干 IMU rpy | [0.016, 0.009, -0.004]，直立 |
| 机器人当前姿态下 9 段动作的规划与避碰 | **9/9 通过** |

**关键结论**：DDS 上 `rt/arm_sdk` 有多个发布端点，但**实际流量为 0** ——
`unitree_g1` 服务在 FSM 501 下并不占用这条通道，它只在自己的状态需要驱动腰/臂时才发。
端点数不等于流量，必须以被动观测为准。

FSM 4 与 501 之间的切换是机器人自己完成的，本工作台的遥测适配器只用
`LocoClient.GetFsmId()` 只读查询，**不会改变机器人模式**。模式切换要走
`LocoClient.SetFsmId()`，见 §2.6。

因此在 FSM 501 下启动本工作台的上肢回放，与内置运控不存在通道冲突。
仍需现场用遥控器值守，因为后端目前不做整机失衡监测。

---

## 5. 首次上机操作清单

现场必须有人只负责观察与人工停机（遥控器），学生不在场。

| # | 步骤 | 判据 |
|---|---|---|
| 1 | 机器人正常站立，确认电量、场地净空、官方 App 可正常停机 | — |
| 2 | 只读模式启动后端与前端 | 页面显示 LowState `fresh`、FSM `501`、数据年龄 < 300 ms |
| 3 | 确认动作库里已导入要试的动作 | 列表里能看到名称、帧数、时长 |
| 4 | **先开 `ROBOT_GUI_CONTROLS_ENABLED=1` 但不点回放** | 页面显示"真机控制已开启" |
| 5 | 选**最短**的一段（建议 `p1_s2`，4.9 秒）回放 | 不跌倒、躯干不晃、双臂不撞腿 |
| 6 | **接管期间盯住腰 12/13/14 的「模式」读数** | 保持 1（若变成 0 说明腰被断力矩，立刻停机，见 §6.3） |
| 7 | 观察 `rt/arm_sdk` 上的权重 | 接管时 0→1，结束时 1→0 |
| 8 | 停止测试 | 机器人回到回放前姿态，控制权交还内置运控 |

**软件停止按钮不等于物理急停。** 任何异常立即用遥控器停机。

### 回放被拒绝时的含义

| 错误码 | 含义 |
|---|---|
| `capability_disabled` | 未开 `ROBOT_GUI_CONTROLS_ENABLED` |
| `confirmation_required` | 前端没有传 `confirmed` |
| `telemetry_stale` | LowState 不新鲜，拒绝下发 |
| `fsm_not_allowed` | 当前 FSM 不在白名单 |
| `fsm_unknown` | 读不到 FSM |
| `mode_machine_unsupported` | 不是 29DoF 机型 |
| `control_busy` | 已有控制会话在跑 |
| `action_invalid` / `action_exists` | 动作文件不合法或重名 |
| `action_group_unsupported` | 动作不是 `arms` 组（含腰的动作不接管） |

---

## 6. 已知边界

1. **没有整机监测。** 接管期间不读 IMU、骨盆高度或躯干倾斜；上肢大幅动作时的失衡
   不会被后端主动发现。依赖现场观察与遥控器停机。
2. **重力前馈是模型近似。** 上肢力矩由官方 G1 29DoF MJCF 算出的重力项给出，
   真实手臂质量分布与模型有偏差时会留下残余下垂（偏小，不会更糟）。
   机器人若装了 Dex3 灵巧手，腕关节的残余会略大。
3. **动作幅度受 ECHO-G 限速约束。** 转换阶段把速度峰值压到 0.8 rad/s，幅度保留
   62–100%；这是刻意的安全取舍，不是模型输出被改坏。
4. **词级时间戳是近似值。** 课程包的 TTS 不回字幕，时间轴由静音检测 + 按字分配
   重建。ECHO-G 的时间偏置是高斯先验（sigma 0.25–2.0 s），这个精度在其容差内，
   但不应当当作精确对齐。
5. **没有并发控制。** 同一时刻只允许一个动作执行。
6. **腰由固件管，规划时按"不动"建模。** 避碰与限位检查把腰当成静止在接管瞬间的姿态，
   固件的平衡回路实际会让腰有小幅摆动，所以规划结果对腰来说是偏乐观的。

### 6.1 沉降窗口与前倾峰值

躯干前倾是**延迟发展**的：2026-10-20 真机实测，轨迹 8.9 秒，腰 14 从 −1.49° 起
以 +2.7°/s 前倾，**动作结束后仍以 +0.6°/s 继续恶化**，到 t=10.86s 才见峰值
（+10.01°），t=12.7s 才恢复。

所以沉降观察窗口 `ROBOT_GUI_HOLD_SOAK_S` 从 1.5 秒提到 **3 秒**：峰值在动作结束后
约 2 秒才到，1.5 秒的窗口必然漏掉。判定漂移**只看腿（slot 0-11）与腰（slot 12-14）**，
手臂在动作里本来就要大幅运动，算进去只会让守卫每次都误报
（实测：把手臂算进去后最大漂移 77.11° @right_wrist_roll，而真实腰 14 漂移只有 0.17°）。

---

## 6.2 为什么只接管双臂

2026-10-20 的真机实测把躯干前倾指向了一个具体机制：**`rt/arm_sdk` 一旦接管腰，
固件内置的腰部平衡回路就退让**，躯干姿态只剩我们下发的 PD 在撑。

| 姿态 | 腰 14 前倾峰值 |
|---|---|
| 完全不锁腰 | +28.5°（膝偏移 46°，腿来回摆） |
| 锁"实测角"（软锁，位置误差恒 0） | ~12° |
| 锁"定值" + kp=120 | +10.01°（动作结束后仍在恶化） |

把 kp 调大只会让弹簧更硬，**补不回缺失的平衡闭环**——延迟发展的特征不会变。
所以接管范围收窄为**只有双臂**：腰的三个槽位写成 `mode=0`（不由 arm_sdk 驱动），
固件继续用自己的平衡回路管腰与躯干姿态。

代价是失去了腰的动作表达（ECHO-G 转出的 17 维轨迹里腰的部分不再执行），
这与官方 `rt/arm_sdk` 例程只填臂关节、不填腰槽位的做法一致。

### 6.3 真机判别：`mode=0` 到底是"不接管"还是"断电机关"

腰槽位写 `mode=0`（官方注释「电机控制模式 0:Disable, 1:Enable」），**这一路到底
是"arm_sdk 不接管、固件继续管"还是"这路电机断力矩"，官方没有写清楚**。

官方 `rt/arm_sdk` 例程压根不设 per-motor `mode`（保持默认 0）只填臂关节，所以
"臂关节 mode=0 也能驱动手臂"——这说明 `mode` 在 arm_sdk 通道上的语义与
`rt/lowcmd` 全身通道不同（后者官方例程每个电机都写 `mode=1`）。

**判别设计**：接管时我们给双臂写 `mode=1`、给腰写 `mode=0`，其余字段同构，
所以「臂」和「腰」之间唯一的变量就是 mode。接管期间盯住腰 12/13/14 的「模式」读数：

| 腰的「模式」读数 | 腰 14 表现 | 含义 | 处置 |
|---|---|---|---|
| 1 → 0 | 腰瘫、躯干 pitch 涨 | `mode=0` = 断力矩 | **立刻停**（遥控器 L2+B 进阻尼），本方案不成立 |
| 1 → 0 | 腰稳、躯干 pitch 不涨 | `mode=0` = 交回固件 | 方案成立 |
| 保持 1 | 腰稳 | arm_sdk 忽略 per-motor mode，腰由固件管 | 方案成立，但机制要另记 |

**对照实验用 `ROBOT_GUI_WAIST_SLOT_MODE=1`**：腰槽位的 `mode` 改成 1，**其余字段
完全不变**（增益与目标仍是 0，双臂仍是 mode=1）——臂和腰之间唯一的变量就是 mode。

```bash
export ROBOT_GUI_WAIST_SLOT_MODE=1     # 默认 0；诊断档
bash ~/robot-gui.sh restart-control
bash ~/robot-gui-track.sh p1_s2
```

> ⚠ 这一档**不是可用的控制模式**：腰被 arm_sdk 名义上接管却给零增益，若固件真的把腰
> 交出来，腰会瘫。现场必须有人握着遥控器（随时 L2+B 进阻尼），且机器人要**站在地面
> 上**——挂在保护架上时腰瘫了也看不出来。先跑默认档（mode=0），只有它显示腰瘫才需要
> 这一档做对照。

> 「模式」列读的是 `LowState.motor_state[i].mode`，官方注释「电机当前模式」——
> 是**当前生效的控制模式回读**，和我们下发的 `LowCmd.motor_cmd[i].mode`（指令侧）
> 是两个字段。它是**信号不是证明**：SDK 仓库里的 IDL 没有注释，官方文档也没定义
> mode=0 时这一路由谁驱动。判读以腰 14 的实际角度为准。

---

## 7. 上肢下垂：重力前馈

`rt/arm_sdk` 报文里 `motor_cmd[i].tau` 是**前馈关节力矩**，接收端把它加在 PD 项之外：

```
总力矩 = kp*(q_target - q) + kd*(dq_target - dq) + tau
```

`tau` 恒为 0 时全靠 PD 扛重力，上肢前伸就会明显下垂，动作结束时手臂还要"追"回
目标位姿，这个突然的纠偏力矩加上质心突变，正是机载运控"原地晃了晃"的来源。

官方 `xr_teleoperate` 用 Pinocchio 的 `pin.rnea(model, data, q, 0, 0)` 算这个值
（逆运动学，静止时即维持姿态所需的力矩）。本工作台用 MuJoCo 的等价做法：

```
tau = -qfrc_bias[upper_dof]      # qfrc_bias 在 v=0 时只剩重力项
```

两个实现细节：

- **按实测姿态算，不用目标角。** 官方用 IK 解算出的目标角，快速运动时前馈会偏离
  实际重力；这里改成每帧用 LowState 的实测角，PD 的跟踪误差与重力补偿互相正交。
- **腿部姿态必须用实测值。** 腰关节的重力力矩对腿姿极敏感：模型默认直腿下
  `waist_pitch` 约 +4.9 Nm，实测蹲姿约 +1.4 Nm，差 3.5 Nm。腿部取会话开始时
  的实测值（上肢动作的几秒内腿基本不动）。

`robot_gui/gravity.py` 负责这件事，通过一个显式补丁替换 `g1-action-studio` 的命令
构造函数（参考实现按项目约束是只读的）。自检方式很硬：**施加前馈后上肢关节的
`qacc` 必须为 0**，五个姿态实测都是 0.000e+00。

关掉它：`ROBOT_GUI_GRAVITY_COMPENSATION=0`。前馈力矩按执行器限位裁剪（肩/肘
±25 Nm，腕 pitch ±5 Nm），真机实测需要的量级是 0.4–4.4 Nm。前馈**只应用在双臂上**，
腰不由我们驱动；腰角仍然参与重力模型（手臂的重力力矩依赖躯干姿态）。

**如果真机上前馈符号反了**，表现是"手臂垂得更厉害"而不是剧烈动作——PD 仍在
按 kp=60 往回拉，纠偏力矩有界。观察这一点就能立刻判断。

---

## 8. 语音

动作库目录下的 `audio/<动作名>.pcm`（16 kHz 单声道 16 bit）与动作同名时，回放会
在**进入段结束后**起播讲解，与手势对齐。回放请求不显式指定语音时后端自动按同名
发现；找不到就安静地不播语音，不影响动作回放。

**只有 PCM 一条路，没有官方 TTS 兜底。** 兜底播的是云端合成的另一套音色，和课程包
对不上；讲不出原声就不出声，比出个错的音色好。

`ROBOT_GUI_AUDIO_ENABLED=1` 才会建立 AudioClient。机器人上默认已有
`g1_audio.service` 在跑官方 WAV socket 播放服务，所以这个开关默认关闭，
确认不冲突后再开。

> SDK2 的 `PlayStop` 不校验 RPC 返回码就返回 0，所以主动中断之后无法确认音频流
> 是否真的停了。后端用闩锁记住"停止未确认"，一旦发生就不再播放新音频，
> 直到进程重启或运维确认。看到 `PCM 推流失败（audio.stop_unconfirmed）` 时，
> 那是**兜底闩锁生效**的正常提示，不是故障——但也意味着这次讲解没播出去。

---

## 9. 验证记录

| 项目 | 范围 | 状态 |
|---|---|---|
| 后端单元与接口测试 | 动作库、控制前置检查、音频端口、重力前馈、接管范围、HTTP 接口 | `pytest` 47 项通过（1 项需 SDK，机器人上跑） |
| 前端测试 | 遥测展示、动作列表、禁用态 | `vitest` 11 项通过 |
| 离线规划与避碰 | 课程包 9 段动作整条样条限位 + 自碰撞 | 9/9 通过 |
| 真机部署 | `.165` 上后端启动、遥测连通、动作库加载 | 已完成，5 个遥测源全 fresh |
| 真机姿态下的规划预演 | 以机器人实时姿态为基线 | 9/9 通过 |
| 控制权只读调查 | `rt/arm_sdk` 实际流量、FSM、模式 | 已完成，FSM 501、arm_sdk 流量 0 |
| arm_sdk 发布链路 | weight=0 发布 97 帧 LowCmd（含 CRC） | 通过，机器人未动 |
| 重力前馈自检 | 五个姿态施加前馈后的上肢 `qacc` | 全部 0.000e+00 |
| 音频链路 | AudioClient 播放 1 秒 PCM | 通过 |
| 接管范围 | 发布的报文里腰槽位 mode=0、增益全 0；双臂槽位 mode=1 | 单测通过，**真机待复测** |
| 实机验收 | G1 真机接管、播放、返回、交还 | 首轮已跑通（发现下垂与语音两个问题），修复后待复测 |
