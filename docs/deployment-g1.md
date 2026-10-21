# 真机部署与验收清单

本文是 Robot GUI 在 Unitree G1 上的部署步骤与首次上机前的检查清单。
面向现场操作员，不替代受监督的人工验收。

> 当前控制能力范围：**双臂 14 关节的动作回放 + 腰 3 关节的姿态保持**。
> 动作本身是 14 维（`arms` 组），腰的目标角锁在接管瞬间的姿态、不执行动作里的腰轨迹；
> 腰的槽位仍由本工作台下发指令（理由见 §6.2）。腿（slot 0-11）由机器人内置运控负责。
> 键盘行走、手掰示教尚未开放，页面上的对应按钮保持禁用。
> FSM 运动模式**可以直接切换**：目录里的模式（零力矩 / 阻尼 / 锁定站立 / 常规运控）
> 默认可点，切换有操作员二次确认，门槛交给人和遥控器。

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
指向 `references/g1-action-studio`，不需要单独安装。
`planning` extra 会装 MuJoCo（几何检测）与 OMPL（衔接段绕行，仅 Linux）。

> **`references/g1-action-studio` 是 git 子模块**，但父仓库里没有 `.gitmodules`，
> 仓库中只有一个指针（`references/` 下三个目录都是这样）。所以**`git pull` 不会更新
> 它的内容**：机器人上那份是源码拷贝，不同步的话后端会缺新函数而起不来
> （2026-10-21 实测：规划层加了 `plan_sequence_with_marks`，机器人上还是旧版）。
>
> 同步方式（机器人上直接 clone 到指针记录的提交）：
>
> ```bash
> cd /usr/local/src/robot-gui
> mv references/g1-action-studio /tmp/g1-action-studio.bak
> git clone --quiet https://github.com/whitevenus/g1-action-studio.git references/g1-action-studio
> cd references/g1-action-studio && git checkout --quiet 5f9c1ae && cd ../..
> ```
>
> 兜底是把一份源码拷回去（机器人上没有 GitHub 访问时）：
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

配置在项目的 `config/env.sh` 里，由 `tools/robot-gui.sh` 自动 source：

```bash
export ROBOT_GUI_MODE=real
export ROBOT_GUI_DDS_INTERFACE=eth0          # 机器人内部 DDS 网卡
export ROBOT_GUI_DATA_DIR=<项目根>/data       # 动作库目录
export ROBOT_GUI_GAINS=<项目根>/config/gains.json
export ROBOT_GUI_TELEMETRY_HZ=10
```

`start-control` 另外会设上 `ROBOT_GUI_CONTROLS_ENABLED=1`、
`ROBOT_GUI_FSM_WHITELIST=501`、`ROBOT_GUI_AUDIO_ENABLED=1`。
其余可调项（`ROBOT_GUI_TRANSITION_SECONDS`、`ROBOT_GUI_REPLAY_SPEED`、
`ROBOT_GUI_HOLD_SOAK_S`、`ROBOT_GUI_RECORDS_DIR`）在 `config/env.sh` 里加。
**速度不在这里调**：页面上有下拉，选的值会随回放请求一起发过去。

| 变量 | 默认 | 含义 |
|---|---|---|
| `ROBOT_GUI_MODE` | `real` | 只接受 `real`；其他值启动即失败 |
| `ROBOT_GUI_DDS_INTERFACE` | 无 | **必填**，DDS 网卡 |
| `ROBOT_GUI_DATA_DIR` | `<项目根>/data` | **动作库目录**。目录不存在时动作接口返回 `action_store_not_configured` |
| `ROBOT_GUI_GAINS` | `<项目根>/config/gains.json` | 增益 JSON 路径。**需要 arms 与 waist 两组** |
| `ROBOT_GUI_CONTROLS_ENABLED` | `false` | 开启后才接受回放请求 |
| `ROBOT_GUI_FSM_WHITELIST` | `501` | 逗号分隔；不在列表内的 FSM 一律拒绝下发 |
| `ROBOT_GUI_FSM_MODES` | 无 | 收窄允许切换的 FSM 模式 ID，逗号分隔，如 `0,1,4,501`。**留空 = 目录里的模式全部可点**；只有想收窄时才设 |
| `ROBOT_GUI_TRANSITION_SECONDS` | `2.0` | 进入与返回的衔接段时长 |
| `ROBOT_GUI_REPLAY_SPEED` | `1.0` | 速度下拉的**初始项**。实际每一把的速度由页面上的下拉给出（回放接口的 `speed` 参数），改这个变量不用重启也只影响下拉的默认值 |
| `ROBOT_GUI_HOLD_SOAK_S` | `3.0` | 动作结束后的沉降观察窗口，见 [§6.1](#61-沉降窗口与前倾峰值) |
| `ROBOT_GUI_RECORDS_DIR` | `<项目根>/logs/records` | 执行记录目录：每次真机回放落一份 JSON，不上数据库 |
| `ROBOT_GUI_LOWER_BODY_DRIFT_DEG` | `5` | 下盘告警阈值：腿/腰 yaw-roll 看相对接管姿态的偏离，腰 14 看绝对前倾角峰值，见 [§6.1.1](#611-守卫口径腰-14-看绝对前倾腿看相对偏离) |
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

## 2. 机器人侧的目录布局

**所有东西都在项目目录里**，家目录不留副本、不留软链：

```
/usr/local/src/robot-gui/
├── config/            # 配置（跟代码一起走）
│   ├── env.sh         # 环境变量，由 tools/robot-gui.sh 自动 source
│   └── gains.json     # arms / waist 增益
├── data/              # 动作库：导入的 group+frames JSON 与 audio/*.pcm
├── logs/              # 后端与前端日志、采样数据
├── tools/             # 现场脚本
├── backend/  frontend/  docs/
```

`logs/` 与 `data/` 是运行期产物，已在 `.gitignore` 里，不入库。

迁移自旧布局（家目录）：

```bash
cd /usr/local/src/robot-gui
mkdir -p config data logs
cp -a ~/.config/robot-gui/gains.json config/    # 若已存在 config/gains.json 则跳过
cp -a ~/robot-gui-data/. data/                 # 动作库
rm -rf ~/.config/robot-gui ~/robot-gui-data ~/robot-gui-track.sh
```

后端默认就找项目内的 `config/gains.json` 与 `data/`（`ROBOT_GUI_GAINS` /
`ROBOT_GUI_DATA_DIR` 只是覆盖手段）。

---

## 3. 机器人侧的启停与观测

启停脚本在仓库的 `tools/robot-gui.sh`，**直接用仓库里的那份**：

```bash
cd /usr/local/src/robot-gui
bash tools/robot-gui.sh start            # 只读
bash tools/robot-gui.sh start-control    # 开启真机控制 + 机器人扬声器语音
bash tools/robot-gui.sh restart-control  # 同上并重启
bash tools/robot-gui.sh status
bash tools/robot-gui.sh stop
```

观测脚本同样都在 `tools/` 下：

```bash
chmod +x tools/*.sh tools/*.py        # 一次性
./tools/robot-gui-track.sh p1_s2      # 回放 + 全程采样 + 判读摘要
./tools/robot-gui-track.sh course 0.8 # 整套按 0.8 倍速播（速度与页面上那个下拉同口径）
python3 tools/inspect_fsm.py eth0    # 读当前运动模式（fsm_id + fsm_mode）
python3 tools/check_waist_locked.py eth0 --seconds 60   # 物理锁腰判别（要人掰）
```

| 脚本 | 用途 |
|---|---|
| `tools/robot-gui.sh` | 启停后端与前端，日志写 `logs/` |
| `tools/robot-gui-track.sh` | 回放一次动作（或 `course` 播整套），打印跟踪误差、下盘守卫、腰 14 全程曲线 |
| `tools/pose_sampler.py` | 全程姿态采样器（由 track.sh 调用，输出 `logs/pose_samples.json`；回放结束会提前收尾） |
| `tools/inspect_fsm.py` | 读 `fsm_id` 与 `fsm_mode`（静态/运动） |
| `tools/check_waist_locked.py` | 物理锁腰判别：腰掰不动 + 膝盖能掰动 = 装了固定件 |
| `tools/play_audio.py` | 只放讲解语音，不动机器人（`p1_s2` 放一段，`--course` 放整套） |

看后端日志：`tail -f logs/backend.log`。

`start-control` 等价于设置 `ROBOT_GUI_CONTROLS_ENABLED=1`、`ROBOT_GUI_FSM_WHITELIST=501`、
`ROBOT_GUI_AUDIO_ENABLED=1` 后再启动。脚本用 `setsid` 启动，SSH 断开或终端关闭都不会带走进程。

**部署安全**：HTTP 服务面向局域网且不实现登录（FR-20）。只放在受信任网络，
禁止直接暴露到公网。页面上的"确认"是误触防护，**不是身份认证**。

### 3.1 一整套连续播放

课程包里的一整套 = `data/course.json` 给出的动作顺序（ECHO-G 按讲解顺序生成动作，
动作名里的 p1/p2 只是生成批次，不承载播放顺序）。整套**一次连续接管**播完，
不是各播各的再回基线：

- 动作之间是 `plan_sequence` 规划出来的衔接段（避碰 + 自碰撞检查都在里面）。
- 讲解语音按每个动作在轨迹里的起始时刻拼成**一条**连续的 PCM，段间补静音。
  官方接口「stream_id 不同则打断播放」，所以不能分段推。
- 语音推流在**独立子进程**里做（`robot_gui.audio_worker`）：同进程里推，
  回放期间会收不到 RPC 回包（见 §6.3）。

```bash
./tools/robot-gui-track.sh course      # 整套 + 全程采样（窗口自动按整套时长与速度拉长）
backend/.venv/bin/python tools/play_audio.py --course   # 只放整套语音，不动机器人
```

页面上也有「确认并回放整套」按钮（课程清单存在时才出现），速度在右侧下拉里选。

**2026-10-21 首次整套验收（`.165`，9 段，FSM 501）**：

| 指标 | 单动作（9 次，各自播） | 整套（一次连续接管） |
|---|---|---|
| 轨迹时长 | 9～17 秒 | **104.6 秒** |
| 腰 14 前倾峰值 | 2.10°～3.88° | **3.91°**（阈值 5°，未越限） |
| 腰 14 回位偏差 | 0.01°～0.05° | **0.02°** |
| 腿最大偏离 | 3.5°～4.5° | 3.18° @right_hip_pitch |
| 手臂跟踪最大偏差 | 0.18～0.19 rad | 0.20 rad（left_wrist_roll） |
| 讲解语音 | 1 段 | **9 段全部推成功**，无 3104 |

**105 秒的连续接管里前倾没有累积**（3.91° 与单动作的峰值同量级），
这是当初选"一次连续接管"时最担心的风险，实测排除了。
段间 2 秒衔接既是动作的平滑过渡，也刚好盖住上一段讲解的时长，
所以各段讲解互不打断。

### 3.2 速度、暂停续播与执行记录（2026-10-21 验收）

这一版把三件事搬到了页面上：**速度下拉**、**暂停/继续**、**执行轨迹记录**。
本节是同一天的二次验收数据（同一套 9 段动作库，FSM 501）。

#### 3.2.1 整套 1.0× 复测

| 指标 | 首次验收（当天早） | 二次验收（1.0×） |
|---|---|---|
| 轨迹时长 | 104.6 秒 | **104.6 秒**（一致） |
| 腰 14 前倾峰值 | 3.91° | **3.93°**（阈值 5°，未越限） |
| 腰 14 回位偏差 | 0.02° | **0.09°** |
| 腿最大偏离 | 3.18° @right_hip_pitch | **3.32° @right_hip_pitch** |
| 手臂跟踪最大偏差 | 0.20 rad（left_wrist_roll） | **0.2493 rad（left_wrist_roll）**，均值 0.0635 rad |
| 腰 14 mode 回读 | 全程 1 | **全程 1**（7473 帧） |
| 实耗 | — | 125.2 秒（规划 + 接管斜坡 + 104.6 + 沉降 + 交权斜坡） |

**两个独立采样器给出同一个前倾峰值 3.93°**：`tools/robot-gui-track.sh` 自己订阅 DDS
采的那一路，和后端执行记录里采的那一路，是两条互不相干的通路 —— 说明执行记录里的
时间序列可以直接拿来判读，不用再单独跑一遍采样脚本。

> **两个时间基准不一样，换算时注意**：`track.sh` 的 t=0 是**采样器启动**的时刻
> （在触发回放之前，含 9 段动作的规划避碰，本次约 13.4 秒）；执行记录里的 t=0 是
> **轨迹开始**的时刻。同一个前倾峰值，`track.sh` 在 t=81.1 秒、记录里在 t=67.7 秒。

轨迹时长的构成：**只有动作本体按速度缩放，衔接段不缩放**（每个 2 秒）：

| 场景 | 动作本体 | 衔接段 | 轨迹时长（实测） |
|---|---|---|---|
| 整套 9 段 @1.0× | 84.6 s | 10 × 2 s = 20 s | **104.6 s** |
| 整套 9 段 @1.2× | 70.5 s | 20 s | **90.5 s** |
| 单动作 p1_s2 @0.8× | 6.1 s | 2 × 2 s = 4 s | **10.1 s** |

#### 3.2.2 暂停与继续（真机验收）

暂停不是 SDK 原语（`rt/arm_sdk` 只认"每个控制周期发什么目标角和增益"），
是工作台自己拼的三步：**截短轨迹停在当前位置 → 同一会话里播一段回基线连接段 →
权重 2 秒斜坡降到 0 交还控制权**（见 `backend/robot_gui/control.py` 的 `pause()`）。

| 场景 | 实测行为 |
|---|---|
| 单个动作播到一半暂停 | 手臂平滑减速停住，然后**自己走回接管瞬间的姿态**，站定后才交权；回基线的避碰规划没失败（日志无「回基线的连接段规划失败」） |
| 单个动作继续 | **重播这一段**（它还没播完），语音从这一段开头重新起播 |
| 9 段课程播到第 4 段暂停 | 继续后**只播剩余 5 段**，被打断的那段不重播 |
| 执行记录 | 暂停与继续是**两条记录**：前一条 `outcome=paused`，后一条 `outcome=completed`，且后一条的 `actions` 从下一段开始 |

整套 1.2× 那次实测：暂停时轨迹 90.5 秒；续播那条记录是「整套 5 段」、轨迹 51.9 秒
（= 剩余动作本体 47.9 ÷ 1.2 + 6 × 2 秒衔接），与「从下一段开始」完全对得上。

#### 3.2.3 执行轨迹记录

每次真机回放落一份 JSON 到 `logs/records/`（**不上数据库**）：腰 14 与两路 IMU 的
20 Hz 全程时间序列、逐关节跟踪误差、下盘守卫结论、分段起始时刻、速度、结果。
页面左侧「执行轨迹记录」是列表页，一行一条执行记录，可点开看详情（腰 14 前倾曲线带
守卫阈值线与峰值标注）也能下载 JSON。

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

1. **默认可用。** 不设 `ROBOT_GUI_FSM_MODES` 时，目录里的模式（0/1/4/501）全部可点。
2. **门槛是操作员确认。** 前端每次切换都弹 `window.confirm`，零力矩与阻尼还会额外
   警告"站立中切换会让机器人瘫倒"。软件按钮不是物理急停，异常请用遥控器停机。
3. **需要收窄时才配置。** 设 `ROBOT_GUI_FSM_MODES="0,1,4,501"` 就能限定可切的模式；
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
| 9 | （选做）播到一半点**暂停** | 机器人**自己走回接管瞬间的姿态**再交权，不是停在半个手势里；见 §3.2.2 |

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
7. **暂停不是"冻结在原地"。** 暂停会让机器人**自己动起来**走回接管瞬间的姿态
   （约 2~4 秒）才交还控制权，见 §3.2.2。现场按暂停时人要在旁边看着。
8. **执行记录只增不减。** 每次回放落一份 JSON 到 `logs/records/`（整套约 200~400 KB），
   没有自动清理，也没有上限；长期跑要自己 `rm`。

### 6.1 沉降窗口与前倾峰值

躯干前倾是**延迟发展**的：2026-10-20 真机实测，轨迹 8.9 秒，腰 14 从 −1.49° 起
以 +2.7°/s 前倾，**动作结束后仍以 +0.6°/s 继续恶化**，到 t=10.86s 才见峰值
（+10.01°），t=12.7s 才恢复。

所以沉降观察窗口 `ROBOT_GUI_HOLD_SOAK_S` 从 1.5 秒提到 **3 秒**：峰值在动作结束后
约 2 秒才到，1.5 秒的窗口必然漏掉。判定漂移**只看腿（slot 0-11）与腰（slot 12-14）**，
手臂在动作里本来就要大幅运动，算进去只会让守卫每次都误报
（实测：把手臂算进去后最大漂移 77.11° @right_wrist_roll，而真实腰 14 漂移只有 0.17°）。

### 6.1.1 守卫口径：腰 14 看绝对前倾，腿看相对偏离

2026-10-21 实测 9 个动作后统一了口径，守卫和判读脚本说的是同一件事：

| 部位 | 判据 | 为什么 |
|---|---|---|
| 腰 14（waist_pitch） | **绝对前倾角的峰值** | 危险是前倾（机械限位 +29.8°，失控实测瘫到 +32.8°），判据必须是绝对角 |
| 腿（slot 0-11）、腰 yaw/roll | 相对接管瞬间姿态的偏离 | 危险是底盘歪掉，偏离量才是对的量 |

早先腰 14 也按"相对接管瞬间的偏离"判，等于把**接管瞬间本身就后仰的那 1.4°**
也算了进去：前倾 3.85° + 初始 −1.4° = 5.26° > 5°，于是 9 个动作里有 2 个误报越限；
同一批数据按"前倾峰值"判，峰值是 2.10°～3.88°，全部在 5° 内。两个口径不是一回事，
守卫必须跟判读口径一致，否则告警就是在制造噪声。

`ROBOT_GUI_LOWER_BODY_DRIFT_DEG`（默认 5°）对这两个量同时生效。

---

## 6.2 腰必须由我们接管

**结论：`rt/arm_sdk` 的 per-motor `mode` 对腰完全无效，"不接管腰"在 arm_sdk 通道上
做不到。** 2026-10-21 真机实测给了铁证：

| 腰槽位写的 mode | 腰 14 前倾峰值 | 曲线形状 |
|---|---|---|
| 0（声称不接管） | +34.01° | 与下一行一致 |
| 1（正常接管） | +34.15° | 与上一行一致 |

两档几乎逐点重合。时序也钉死了因果：

```
t=1.36s   arm_sdk 权重开始爬升
t=3.374s  权重达到 1.0
t=4.2s    腰 14 连续三跳 +6.7° / +7.7° / +8.4°   ← 腰失去支撑
t=6~13s   平台 +32.80°（waist_pitch 的机械限位 ±29.8°）
t=17.6s   权重降到 0.85，腰立刻往回走（-7°/0.5s）
t≥21s     稳定 -1.79°（固件的平衡回路重新接管）
```

**腰的前倾与恢复精确挂在接管权重的升降上**。权重一到 1，腰的控制权就无条件归我们；
腰槽位留空（增益全 0）时，躯干前倾的重力把腰推到机械限位，固件的腰部平衡回路
退让（躯干 pitch +40°，髋部只 +7°——前倾完全发生在腰关节上，腿没被迫配平）。

所以腰必须由我们给出**足够的支撑力矩**：PD 项 + 重力前馈。

### 6.2.1 增益怎么定：按力矩上限反推

官方 G1 29DoF MJCF 里的执行器上限：`waist_pitch` / `waist_roll` ±50 Nm、
`waist_yaw` ±88 Nm、手臂肩肘 ±25 Nm、腕 ±5 Nm。腰 pitch 的行程只有
±0.52 rad = ±29.8°。

| 姿态 | 腰 14 前倾峰值 | 说明 |
|---|---|---|
| 腰槽位全 0（无支撑） | +32.8°（限位） | 2026-10-21 实测 |
| 锁"实测角"（软锁，位置误差恒 0） | ~12° | 参考实现 2026-09-29 实测 |
| 锁"定值" + kp=120 | +10.01° | 2026-10-20 实测 |
| 锁"定值" + kp=300 + 重力前馈 | 待实测 | 当前默认 |

- **腰 kp=300**：饱和误差 = 50/300 = 0.167 rad = 9.5°，明显小于 ±29.8° 的行程，
  前倾会停在饱和界以内。kd 取 5，与官方 unitree_mujoco 的 G1 FixStand 腰增益
  （200/5）同量级。
- **手臂 kp=120**：肩 pitch 的饱和误差 = 25/120 = 0.208 rad = 11.9°，而 2026-10-21
  实测最大跟踪偏差只有 0.146 rad = 8.4°，还没到饱和。kd 按 √kp 比例取 3.5，保持
  阻尼比不变。

**kp 太大为什么不安全**：τ = kp·Δq 会被执行器上限裁剪。超过上限之后刚度就丢了，
腰会继续倒——所以 kp 不是越大越好，而是"够撑住"且"饱和误差小于行程"。

**kd 为什么也要动**：kd 是阻尼系数，只抑制振荡、不提供支撑。kp 提上去之后如果 kd 不动，
阻尼比 ζ = kd/(2√(kp·J)) 会下降，系统更容易振荡。经验做法是 **kd 按 √kp 比例微调**
（kp 120→300 是 2.5 倍，kd 2.0→3.2，取 5 是更保守的官方量级）。kd 太大会让关节发黏、
动作迟钝、跟踪误差变大，所以也不宜大。

### 6.2.2 手臂够不到目标

2026-10-21 实测的跟踪误差是**尖峰型**（肩 pitch 最大 0.146 rad / 平均 0.034 rad），
不是稳态下垂：说明是**动态跟踪刚度不足**，不是重力没补偿对。提高 kp 同时压峰值和均值，
这也是把手臂 kp 从 80 提到 120 的依据。

### 6.3 语音推流必须在独立进程里

2026-10-21 真机实测（`.165`），同一个 `PlayStream` 调用：

| 在哪推 | 回放期间的表现 |
|---|---|
| 后端进程内 | **收不到 RPC 回包**，等满 10 秒超时（3104 = `RPC_ERR_CLIENT_API_TIMEOUT`） |
| 独立进程 | 回包稳定 0.05 秒，连测 29 次全通 |

而**服务端其实已经收到并播了音频**——现场表现是"声音正常，但后端报失败"，
还白等 20 秒（推流 10 秒 + `PlayStop` 10 秒）。

原因在 SDK 的结构：`ChannelFactory` 是单例，**整个进程共用一个 DDS participant**。
后端进程里同时跑着控制回路（50 Hz 发 `rt/arm_sdk`）、遥测订阅（`rt/lowstate`
实测 1050 Hz 的回调）和音频 RPC，三者挤在同一个 participant 上，回包就收不到了。

⇒ 语音一律走 `robot_gui.audio_worker` 子进程（它有自己的 participant）。
另外两条实测结论也写进了代码：

- **每段语音整段一次推**：分块只会把"第 N 块被拒 → 只播开头几个字"的故障面放大。
- **每段语音用自己的 stream_id**：官方接口「stream_id 相同为缓存续播，不同则打断
  播放」。同一段语音切成多块续播，真机实测播出来是**沙沙声和重复内容**
  （2026-10-21）；不同的语音就配不同的 id，段与段之间互不干扰。
- **失败不能重推**：同 stream_id 重推会让机器人端把已播内容**再播一遍**
  （真机实测：开头几个字之后同一句重复了一遍）。

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
| 后端单元与接口测试 | 动作库、控制前置检查、音频端口、重力前馈、接管范围、执行记录、暂停续播、HTTP 接口 | `pytest` 87 项通过（1 项需 SDK，机器人上跑） |
| 前端测试 | 遥测展示、动作列表、速度下拉、执行记录页、禁用态 | `vitest` 18 项通过 |
| 离线规划与避碰 | 课程包 9 段动作整条样条限位 + 自碰撞 | 9/9 通过 |
| 真机部署 | `.165` 上后端启动、遥测连通、动作库加载 | 已完成，5 个遥测源全 fresh |
| 真机姿态下的规划预演 | 以机器人实时姿态为基线 | 9/9 通过 |
| 控制权只读调查 | `rt/arm_sdk` 实际流量、FSM、模式 | 已完成，FSM 501、arm_sdk 流量 0 |
| arm_sdk 发布链路 | weight=0 发布 97 帧 LowCmd（含 CRC） | 通过，机器人未动 |
| 重力前馈自检 | 五个姿态施加前馈后的上肢 `qacc` | 全部 0.000e+00 |
| 音频链路 | AudioClient 播放 1 秒 PCM | 通过 |
| 接管范围 | 发布的报文里腰槽位也带增益与重力前馈；per-motor mode 对腰无效 | 真机已复测：mode 写 0 / 1 两档前倾峰值 34.01° vs 34.15°，无区别 |
| 腰的支撑 | 锁"定值" + kp=300 + 腰的重力前馈 | 真机已复测：整套 1.0× 连播 104.6 秒，腰 14 前倾峰值 3.93°（阈值 5°），回位 0.09° |
| 实机验收 | G1 真机接管、播放、返回、交还 | 整套 1.0× / 1.2× 全跑通；105 秒连续接管里前倾不累积 |
| 速度下拉 | 页面上选速度，随回放请求传给后端 | 真机已跑 0.8× / 1.0× / 1.2×，轨迹时长按「本体÷速度 + 衔接段」变化 |
| 暂停与继续 | 截短停在当前位置 → 回基线 → 交权；整课从下一段续播 | 真机已跑通：机器人自己走回接管姿态再交权，9 段课程第 4 段暂停后续播 5 段 |
| 执行轨迹记录 | 每次回放落一份 JSON，页面列表/详情/下载 | 真机已跑通：与采样脚本的腰 14 峰值一致（3.93°） |
