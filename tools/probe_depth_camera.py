#!/usr/bin/env python3
"""探一探 G1 上的深度相机到底能怎么取到画面。

后端里的 `DepthCamera` 默认走 SDK2 的 videohub RPC（`GetImageSample`），但这台
机器上到底哪条路通，得在机器人上实测。这个脚本把常见的几条路都试一遍并打印
结果，方便据此定死取流方式。

用法（机器人上）：
    cd /usr/local/src/robot-gui
    backend/.venv/bin/python tools/probe_depth_camera.py

**只读**：不接管控制权、不发任何关节指令。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
OUT_DIR = Path("/tmp/depth_camera_probe")


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def bootstrap() -> None:
    """把后端目录加进 sys.path，才能 import 到生产代码里的 robot_gui。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def probe_video_hub() -> None:
    """SDK2 videohub RPC 取一帧（后端当前用的就是这条）。"""
    section("videohub RPC（GetImageSample）")
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.video.video_client import VideoClient
    except ImportError as error:
        print(f"拿不到 SDK2：{error}")
        return
    interface = os.getenv("ROBOT_GUI_DDS_INTERFACE", "eth0")
    try:
        ChannelFactoryInitialize(0, interface)
    except Exception as error:  # noqa: BLE001 - 探针要把失败原因打出来
        print(f"DDS 通道初始化失败（网卡 {interface}）：{error}")
        return
    try:
        client = VideoClient()
        client.SetTimeout(3.0)
        client.Init()
    except Exception as error:  # noqa: BLE001
        print(f"videohub 客户端起不来：{error}")
        return
    try:
        code, data = client.GetImageSample()
    except Exception as error:  # noqa: BLE001
        print(f"GetImageSample 抛异常：{error}")
        return
    print(f"返回码 {code}，数据长度 {0 if data is None else len(data)}")
    if code == 0 and data:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / "videohub.jpg"
        path.write_bytes(bytes(data))
        print(f"已保存 {path}（{path.stat().st_size} 字节）")
    else:
        print("这条路没通：videohub 服务没有给出画面。")


def probe_video_devices() -> None:
    """V4L2 设备：相机有没有被当成普通 USB 摄像头接上。"""
    section("V4L2 设备（/dev/video*）")
    devices = sorted(Path("/dev").glob("video*"))
    if not devices:
        print("没有 /dev/video* 设备。")
        return
    for device in devices:
        print(f"发现 {device}")
    try:
        import cv2  # noqa: F401 - 探针里按需导入
    except ImportError:
        print("没有 OpenCV，跳过实际取帧。")
        return
    for device in devices:
        capture = cv2.VideoCapture(str(device))
        if not capture.isOpened():
            print(f"{device}：打不开。")
            continue
        ok, frame = capture.read()
        if ok and frame is not None:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            path = OUT_DIR / f"{device.name}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"{device}：取到 {frame.shape[1]}×{frame.shape[0]}，已保存 {path}")
        else:
            print(f"{device}：打开了但读不出帧。")
        capture.release()


def probe_depth_libraries() -> None:
    """相机厂商的 SDK 有没有装。"""
    section("相机 SDK")
    for module in ("pyrealsense2", "pyorbbec", "pyorbbec_sdk"):
        try:
            __import__(module)
            print(f"{module}：已安装")
        except ImportError:
            print(f"{module}：没装")
    if shutil.which("v4l2-ctl"):
        print("\nv4l2-ctl --list-devices：")
        subprocess.run(["v4l2-ctl", "--list-devices"], check=False)
    else:
        print("没有 v4l2-ctl。")


def probe_local_ports() -> None:
    """相机服务常见的本地端口：有的固件会自己开一个推流口。"""
    section("本地端口")
    for port in (8554, 554, 8080, 8000, 5000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"{port}：有东西在听")
            else:
                print(f"{port}：没人听")


def main() -> int:
    print("深度相机取流探针（只读，不动机器人）")
    print(f"网卡：{os.getenv('ROBOT_GUI_DDS_INTERFACE', 'eth0')}")
    bootstrap()
    probe_video_hub()
    probe_video_devices()
    probe_depth_libraries()
    probe_local_ports()
    section("小结")
    print(f"取到的文件都在 {OUT_DIR}（存在的话）。")
    print("把这份输出发回来，就能定死后端该走哪条取流路径。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
