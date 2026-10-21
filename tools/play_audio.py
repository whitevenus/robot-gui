#!/usr/bin/env python3
"""单独播放课程包里的讲解语音（不驱动任何关节）。

走的是后端生产代码的同一条推流路径（G1AudioPort），所以听到的结果就是
回放时讲解的真实效果。用来在不动机器人的前提下验证语音是否完整。

用法（机器人上）：
    python3 tools/play_audio.py p1_s2
    python3 tools/play_audio.py p1_s2 --app-name robot-gui
    python3 tools/play_audio.py p1_s2 --interface eth0

只播声音：不发任何控制消息。
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"


def _bootstrap() -> None:
    """把后端目录加进 sys.path，才能 import 到生产代码里的 robot_gui。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    try:
        import unitree_sdk2py  # noqa: F401
    except ImportError:
        print(
            "找不到 unitree_sdk2py，请用后端的虚拟环境跑："
            f"{BACKEND_DIR}/.venv/bin/python tools/play_audio.py <名字>",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="播放课程包里的讲解语音")
    parser.add_argument("name", help="动作名，语音取 data/audio/<名字>.pcm")
    parser.add_argument("--app-name", default="robot-gui", help="推流用的应用名")
    parser.add_argument("--interface", default="eth0", help="网卡")
    args = parser.parse_args()

    _bootstrap()

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize

    from robot_gui.audio import AudioClientFactory, G1AudioPort, pcm_duration_s

    path = PROJECT_ROOT / "data" / "audio" / f"{args.name}.pcm"
    if not path.is_file():
        print(f"找不到语音文件 {path}", file=sys.stderr)
        return 1
    pcm = path.read_bytes()

    ChannelFactoryInitialize(0, args.interface)
    port = G1AudioPort(AudioClientFactory(), args.app_name)
    print(f"播放 {path.name}，{pcm_duration_s(pcm):.2f} 秒", flush=True)
    error = port.play(pcm, threading.Event())
    if error:
        print(f"播放失败：{error}", file=sys.stderr)
        return 1
    print("播放完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
