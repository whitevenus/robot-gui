"""语音推流子进程（由后端拉起，不要手工运行）。

**为什么必须独立进程**（2026-10-21 真机实测）：

后端进程里同时跑着控制回路（50 Hz 发 `rt/arm_sdk`）、遥测订阅（`rt/lowstate`
实测 1050 Hz 的回调）和语音的 `PlayStream` RPC，三者共用 SDK2 的**同一个 DDS
participant**。回放期间：

- 后端进程内的 `PlayStream` **收不到 RPC 回包**，等满 10 秒超时，返回 3104
  （`RPC_ERR_CLIENT_API_TIMEOUT`）；**而服务端其实已经收到并播了音频**——现场
  表现是"声音正常，但后端报失败"，还白等 20 秒（推流 10 秒 + `PlayStop` 10 秒）。
- 同一时刻、同一台机器人、同样的调用，放到**独立进程**里推，回包稳定 0.05 秒；
  回放期间连测 29 次全部正常。

所以语音一律走这个子进程：它有自己的 DDS participant，不跟控制回路抢通道。

协议（由 `robot_gui.audio` 调用）：

    python -m robot_gui.audio_worker --app-name robot-gui --stream-id <id> < clip.pcm

PCM 从 stdin 读（不在命令行里传，也不落临时文件）。stdout 输出一行 JSON，
`code` 是机器人端的返回码（0 = 服务端已接受）；退出码 0 = 成功，1 = 失败。

协议要点（官方《音频灯光服务接口》）：

- `PlayStream(app_name, stream_id, pcm)` **整段一次推完**。PCM 必须是
  16 kHz / 单声道 / 16 bit。
- `stream_id` **相同为缓存续播，不同则打断播放**。所以失败时**不能**用同一个
  stream_id 重推（机器人端会把已播内容再播一遍），换新的则会打断当前播放。
- 服务端的播放是异步的：推完不等于播完，要等整段时长过去再 `PlayStop`，
  立刻停会掐掉尾巴。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
#: 推完之后再多等这么久才 PlayStop：服务端的播放是异步的，立刻停会掐掉尾巴。
TAIL_SINK_S = 0.3


def _report(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _stop_audio(client: object, app_name: str) -> None:
    try:
        client.PlayStop(app_name)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 收尾失败只记日志，不影响退出码
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="把一段 PCM 推给 G1 扬声器")
    parser.add_argument("--app-name", default="robot-gui")
    parser.add_argument("--stream-id", required=True)
    parser.add_argument(
        "--interface", default=os.getenv("ROBOT_GUI_DDS_INTERFACE", "eth0")
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    pcm = sys.stdin.buffer.read()
    if not pcm or len(pcm) % 2:
        _report({"code": -1, "error": "invalid_pcm"})
        return 1

    # 被后端取消时（SIGTERM）要收尾：停掉正在播的音频再退出。
    stopped = threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    ChannelFactoryInitialize(0, args.interface)
    client = AudioClient()
    # 超时给足：整段 PCM 要一次传过去，官方例程用的就是 10 秒。
    client.SetTimeout(args.timeout)
    client.Init()

    try:
        reply = client.PlayStream(args.app_name, args.stream_id, pcm)
    except Exception as error:  # noqa: BLE001 - 推流异常要如实报出去
        _report({"code": -1, "error": repr(error)})
        return 1
    code = reply[0] if isinstance(reply, tuple) else reply
    if code != 0:
        _report({"code": code})
        _stop_audio(client, args.app_name)
        return 1

    # 推完不等于播完：服务端的播放是异步的，等整段时长过去再收尾。
    deadline = time.monotonic() + len(pcm) / BYTES_PER_SECOND + TAIL_SINK_S
    while time.monotonic() < deadline and not stopped.is_set():
        time.sleep(0.05)
    _stop_audio(client, args.app_name)
    _report({"code": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
