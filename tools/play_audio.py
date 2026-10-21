#!/usr/bin/env python3
"""播放课程包里的讲解语音（不驱动任何关节）。

走的是后端生产代码的同一条推流路径（G1AudioPort → robot_gui.audio_worker 子进程），
所以听到的结果就是回放时讲解的真实效果。用来在不动机器人的前提下验证语音是否完整。

用法（机器人上）：
    python3 tools/play_audio.py p1_s2          放一段
    python3 tools/play_audio.py --course        放整套（按 data/course.json 的顺序）

整套走的是**和回放时一样的拼法**：各段讲解按动作在轨迹里的起始时刻拼成一条
连续的 PCM，段间补静音。段间衔接时长取 ROBOT_GUI_TRANSITION_SECONDS（默认 2 秒）。

只播声音：不发任何控制消息。
"""

from __future__ import annotations

import argparse
import json
import os
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


def _course_clips(data_dir: Path) -> list[tuple[float, bytes]]:
    """按课程清单算出各段讲解的**推流时刻**：(起始秒数, PCM)。

    时刻跟回放时一致：先等一段进入衔接段，然后每段动作的讲解落在它自己的
    起始时刻上，段间留出一段衔接的静音。
    """
    course = json.loads((data_dir / "course.json").read_text(encoding="utf-8"))
    transition = float(os.getenv("ROBOT_GUI_TRANSITION_SECONDS", "2.0"))
    clips: list[tuple[float, bytes]] = []
    at = transition
    for name in course["actions"]:
        body = json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))
        duration = float(body["frames"][-1]["t"])
        pcm_path = data_dir / "audio" / f"{name}.pcm"
        if pcm_path.is_file():
            clips.append((at, pcm_path.read_bytes()))
        at += duration + transition
    return clips


def main() -> int:
    parser = argparse.ArgumentParser(description="播放课程包里的讲解语音")
    parser.add_argument("name", nargs="?", help="动作名，语音取 data/audio/<名字>.pcm")
    parser.add_argument(
        "--course", action="store_true", help="放整套（按 data/course.json 的顺序）"
    )
    parser.add_argument("--app-name", default="robot-gui", help="推流用的应用名")
    parser.add_argument("--interface", default="eth0", help="网卡")
    args = parser.parse_args()

    if not args.course and not args.name:
        parser.error("要给出动作名，或者用 --course")

    _bootstrap()

    from robot_gui.audio import G1AudioPort

    data_dir = Path(os.getenv("ROBOT_GUI_DATA_DIR", str(PROJECT_ROOT / "data")))
    if args.course:
        clips = _course_clips(data_dir)
        total = max(at + len(pcm) / 32000 for at, pcm in clips)
        print(f"整套语音 {len(clips)} 段，共 {total:.2f} 秒", flush=True)
    else:
        path = data_dir / "audio" / f"{args.name}.pcm"
        if not path.is_file():
            print(f"找不到语音文件 {path}", file=sys.stderr)
            return 1
        pcm = path.read_bytes()
        print(f"播放 {path.name}，{len(pcm) / 32000:.2f} 秒", flush=True)
        clips = [(0.0, pcm)]

    # 推流在独立子进程里做（它自己建 DDS 通道），这里只把网卡传给它。
    os.environ["ROBOT_GUI_DDS_INTERFACE"] = args.interface
    port = G1AudioPort(args.app_name)
    error = port.play(clips, threading.Event())
    if error:
        print(f"播放失败：{error}", file=sys.stderr)
        return 1
    print("播放完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
