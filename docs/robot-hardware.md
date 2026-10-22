# 机器人侧硬件与算力清单

`robot-gui` 跑在机器人本机的算力板上。这份清单记的是**底座本身**：跑得动什么、
装东西有什么坑、哪些结论是实测查出来的。

> 数据来源：2026-10-22 在机器人上直接查（`unitree@192.168.110.165`）。
> 与功能验收数据不是一回事 —— 那部分在 [`deployment-g1.md`](deployment-g1.md)
> §3.1 / §3.2（轨迹时长、前倾峰值、暂停续播的实测），这里只记硬件与运行环境。

## 1. 算力板

| 项 | 实测值 | 怎么查的 |
|---|---|---|
| 型号 | **NVIDIA Orin NX Developer Kit** | `cat /proc/device-tree/model` |
| 架构 | aarch64（ARMv8；Orin NX = 8×A78AE + 2×A78AE） | `lscpu` |
| 内存 | 15 GiB 可见（标称 16 GB 共享 LPDDR5，**CPU/GPU 共用**） | `free -h` |
| Swap | 7.5 GiB | `free -h` |
| 存储 | NVMe SSD **1.9 T**（已用 54 G，可用 1.8 T） | `df -h /` |
| GPU | Orin NX 集成的 Ampere GPU（1024 核 / 2048 Tensor Core，标称 100 TOPS INT8） | 板卡规格，见下方说明 |
| 相机 | **Intel RealSense Depth Camera 435i**（头部，4 个 `/dev/video*` 节点，走 Realtek USB 3.0 hub） | `lsusb` / `ls -l /dev/video*` |

**`lspci | grep -Ei 'vga|3d|display'` 输出为空 ≠ 没有显卡。**
Jetson 的 GPU 挂在 SoC 上，不是 PCIe 设备，所以不进 lspci（x86 独显才会列出来）。
判断 Jetson 有没有 GPU，要看 `/proc/device-tree/model` 和 `/dev/nvidia*`。

## 2. 驱动与 CUDA：GPU 现在就能用

| 项 | 实测 | 说明 |
|---|---|---|
| JetPack | **R35.3.1**（L4T 35.3.1，GCID 32827747，board t186ref） | `/etc/nv_tegra_release` |
| 内核 | **5.10.104-tegra** | `uname -r` |
| L4T 驱动包 | `nvidia-l4t-core` / `-cuda` / `-3d-core` / `-display-kernel`（5.10.104-tegra-35.3.1）/ `-gstreamer` / `-firmware` / `-bootloader` / `-init` / `-configs` / `-apt-source` / `-gbm` / `-camera` / `-graphics-demos` 全部在位，版本与内核模块包严丝合缝 | `dpkg -l \| grep nvidia` |
| **驱动库** | **`/usr/lib/aarch64-linux-gnu/libcuda.so` 存在** | **所有 CUDA 程序的实际入口 → GPU 可用** |
| 内核态节点 | `/dev/nvidia0`、`/dev/nvidiactl`、`/dev/nvidia-modeset` 存在 | 内核驱动在跑 |
| **CUDA Toolkit** | **11.4**（`nvcc` = V11.4.315，`/usr/local/cuda-11.4` 含 bin/lib64/nvvm/nvml/targets；`libcudart.so.11.4.298` 等在） | `nvcc --version` |
| 电源模式 | **MAXN**（最高性能档；Orin NX 另有 15W/10W/7W） | `nvpmodel -q` |
| JetPack 工具 | `nvpmodel` / `jetson_clocks` / `tegrastats` **都在** | `which` |
| 容器运行时 | `nvidia-container-toolkit` 1.11.0 + `nvidia-docker2` 2.11.0 + `nvidia-container-runtime` 3.9.0 已装 | `dpkg -l \| grep nvidia` |
| `nvidia-smi` | **command not found** | 见 §2.1 |

### 2.1 只有 `nvidia-smi` 这个诊断工具没有

`nvidia-smi` 不在 PATH，而 `libcuda.so` 在、`/dev/nvidia0` 在、CUDA Toolkit 完整、
电源模式 MAXN —— 所以**缺的只是那个看状态的命令行工具，不是驱动，也不是 CUDA 运行时**。

已排除的两种可能：

- **不是 shell 缓存**：`hash -r` 之后仍然 command not found。
- **不是 core 包里被删了**：`dpkg -L nvidia-l4t-core | grep bin/nvidia-smi` **输出为空** ——
  这个包本来就不含该文件。提供 Tegra 版 `nvidia-smi` 的是另一个包
  （JetPack 5.1.1 上是 `nvidia-utils-35.3.1`，"NVIDIA L4T Utils Package"），
  这台机器**没装**（`dpkg -l | grep nvidia-utils` 无输出）。

也就是说这台机器的 JetPack 是**裁剪过的**：驱动、CUDA Toolkit、功耗与监控工具都在，
只少了 `nvidia-utils` 这个包。

后果与补法：

- **对跑 CUDA 没影响** —— 写程序不需要 `nvidia-smi`。
- **想看 GPU 状态**：用 `tegrastats`（已装）；要完整功耗表用 `sudo nvpmodel -q`
  （普通用户读 EMC 参数会报 `Error opening /sys/kernel/nvpmodel_emc_cap/emc_iso_cap: 13`，
  13 是权限不足，**不是故障**）。
- **想补 `nvidia-smi`**：`sudo apt install nvidia-utils-35.3.1`（版本必须对上 R35.3.1，
  见 §4.1）；不补也不影响任何事。

## 3. 对 robot-gui 的影响：当前这条路线是纯 CPU

robot-gui 这套**不需要 GPU**：

| 环节 | 跑在哪 |
|---|---|
| 轨迹规划、关节限位与自碰撞检查 | MuJoCo 几何检测，CPU |
| 样条插值、跟踪误差、姿态采样 | numpy / scipy，CPU |
| 讲解语音 | SDK2 `AudioClient` 推 PCM —— **合成在课程包里**，机器人上只负责推流 |

所以 2026-10-21 那次整套验收（9 段、104.6 秒一次连续接管、腰 14 前倾峰值 3.93°）
是**纯 CPU** 跑出来的，有没有 GPU 都不影响。

GPU 只在这三件事上才有意义：

1. 在机器人上跑**语音合成**（UniRoboGui 的 Kokoro TTS 走的就是这条路）；
2. 跑**视觉 / VLM**（G1 头部的 RealSense D435i 已经在 `lsusb` 上）；
3. 跑 ECHO-G 的**实时推理**。

## 4. Python 与已装包

| 项 | 实测 |
|---|---|
| 系统 Python | **3.8.10**（只此一个，`/usr/bin/python3.8`）—— 这是给 SDK 用的 |
| 系统 numpy | 1.17.4 |
| torch | **未安装**（`ModuleNotFoundError`） |
| robot-gui 自己的环境 | `backend/.venv`（uv 装的 3.11+，见 `deployment-g1.md` §2.1） |

**系统 Python 3.8 跑不了 torch 的现代版本**（torch 2.x 要 ≥ 3.8 但 aarch64 wheel 更要 3.9/3.10+），
而且 `numpy 1.17.4` 太老。要在机器人上装模型，只能另起环境（见 §4.2）。

## 5. 在机器人上装东西的几条硬规矩

### 5.1 NVIDIA 驱动跟内核版本绑死

Jetson 的 `nvidia-l4t-*` 版本必须同时对上 `/etc/nv_tegra_release`（R35.3.1）、
`uname -r`（5.10.104-tegra）和内核模块包
（`nvidia-l4t-display-kernel 5.10.104-tegra-35.3.1-20230319081403`）。
**版本不对的 `apt install nvidia-*` 可能直接让机器开不了机**（黑屏、串口都进不去），
要拆卡刷 JetPack 才能恢复。

现成的两条路：

- **PyTorch 容器**（推荐）：`nvcr.io/nvidia/l4t-pytorch:r35.3.1-multiarch`。
  自带匹配的 CUDA 用户态，不碰宿主机驱动；机器上 `nvidia-container-toolkit`
  与 `nvidia-docker2` 都已就位，容器这条路是通的。
- **宿主机上装 pip 的 aarch64 torch wheel**：它会自带 CUDA 用户态库，靠
  CUDA 11.x 的次版本兼容性跑在 11.4 的驱动上，**纯 PyTorch 够用**；
  但 TensorRT / cuDNN 那条路还是走容器省事。

### 5.2 别往 `backend/.venv` 里装

`uv sync` 会清掉不在 `pyproject.toml` 里的包 —— 2026-10-21 就因为这个把手动装的
`unitree_sdk2py` 冲掉过一次（补装步骤见 [`deployment-g1.md`](deployment-g1.md) §2.5）。
要在机器人上装 torch / 语音模型，**另开一个 venv 或用容器**。

### 5.3 内存是 CPU/GPU 共享的

15 GiB 里 DDS 订阅、控制回路、语音推流都要占，留给模型的大概只有 8~10 GB。
2B 级别的 VLM（fp16 约 4~5 GB）勉强够；再大就得外接算力。

### 5.4 磁盘够，但记录不会自动清

根分区 1.9 T、已用 54 G、可用 1.8 T —— **执行记录涨不满**。但 `logs/` 里的东西
**不入库也不自动清理**：执行记录整套约 200~400 KB，长期跑要自己 `rm`
（见 [`deployment-g1.md`](deployment-g1.md) §6 已知边界）。
2026-10-22 实测：`logs/` 整个才 1.7 MB（7 条记录）。

### 5.5 相机

G1 头部的 **RealSense D435i** 在 `lsusb` 上可见，4 个 `/dev/video*` 节点也在
（`/dev/video0..3`，`plugdev` 组可读写）。要在机器人上做视觉时注意：

- **`v4l2-ctl` 没装**（`apt install v4l-utils` 才有），`lsusb` 只能看到设备在，
  看不到它当前在流哪一路、什么分辨率；
- 想用深度流要装 `librealsense2`（`pyrealsense2` 走 pip），不是普通 V4L2 设备。
