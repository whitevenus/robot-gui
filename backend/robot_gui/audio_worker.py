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

- `PlayStream(app_name, stream_id, pcm)` **每一段语音整段一次推完**。实测一次推
  3.2 MB（整套课程 100 秒的讲解）返回码 0、往返 0.37 秒。
- **每一段语音用自己的 stream_id**。官方接口写明「stream_id 相同为缓存续播，
  不同则打断播放」：同一段语音切成多块续播，真机实测播出来是沙沙声和重复内容
  （2026-10-21）；不同的语音就配不同的 id，段与段之间互不干扰。
- 失败时**不能**重推：同 stream_id 重推会让机器人端把已播内容再播一遍；
  换新 stream_id 则会打断当前播放。两种都比断在半截更糟，所以如实报错。
- 服务端的播放是异步的：推完不等于播完，要等整段时长过去再 `PlayStop`，
  立刻停会掐掉尾巴。
- PCM 必须是 16 kHz / 单声道 / 16 bit。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence

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


def _wait_until(deadline: float, stopped: threading.Event) -> None:
    """等到 deadline 或被取消。分片睡，信号能及时打断。"""
    while not stopped.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _push_stream(
    client: object,
    app_name: str,
    session_id: str,
    blob: bytes,
    specs: Sequence[tuple[int, int, int]],
    stopped: threading.Event,
    started: float,
) -> int:
    """按各自的时刻**分段推**。

    每一段是独立的语音，**各自用自己的 stream_id**：官方接口「stream_id 不同则
    打断播放」，所以段与段之间不能共用一个 id（共用会让服务器按"缓存续播"处理，
    真机实测播出来是沙沙声和重复内容）。
    """
    for index, (at_ms, offset, length) in enumerate(specs):
        _wait_until(started + at_ms / 1000.0, stopped)
        if stopped.is_set():
            return 0
        stream_id = f"{session_id}-{index}"
        reply = client.PlayStream(  # type: ignore[attr-defined]
            app_name, stream_id, blob[offset : offset + length]
        )
        code = reply[0] if isinstance(reply, tuple) else reply
        if code != 0:
            return code
    return 0


def main() -> int:
    # 起点取进程一开始的时刻：子进程初始化 DDS 要 0.7 秒左右，各段的推流时刻是
    # 相对动作轨迹的 t=0 的，那段时间要被初始化吃掉。
    started = time.monotonic()
    parser = argparse.ArgumentParser(description="把若干段 PCM 按时刻推给 G1 扬声器")
    parser.add_argument("--app-name", default="robot-gui")
    parser.add_argument("--stream-id", required=True, help="本次播放的会话 ID")
    parser.add_argument(
        "--clips",
        required=True,
        help="各段描述：起始毫秒:字节偏移:字节长度，逗号分隔",
    )
    parser.add_argument(
        "--interface", default=os.getenv("ROBOT_GUI_DDS_INTERFACE", "eth0")
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    specs: list[tuple[int, int, int]] = []
    for item in args.clips.split(","):
        parts = item.split(":")
        if len(parts) != 3:
            _report({"code": -1, "error": f"clip 描述不合法：{item!r}"})
            return 1
        specs.append((int(parts[0]), int(parts[1]), int(parts[2])))
    if not specs:
        _report({"code": -1, "error": "没有要播的片段"})
        return 1

    blob = sys.stdin.buffer.read()
    if not blob:
        _report({"code": -1, "error": "空的 PCM"})
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
        code = _push_stream(
            client, args.app_name, args.stream_id, blob, specs, stopped, started
        )
    except Exception as error:  # noqa: BLE001 - 推流异常要如实报出去
        _report({"code": -1, "error": repr(error)})
        return 1
    if code != 0:
        _report({"code": code})
        _stop_audio(client, args.app_name)
        return 1

    # 推完不等于播完：服务端的播放是异步的，等最后一段播完再收尾。
    last_end_ms = max(at + length * 1000 // BYTES_PER_SECOND for at, _, length in specs)
    _wait_until(started + (last_end_ms + TAIL_SINK_S * 1000) / 1000.0, stopped)
    _stop_audio(client, args.app_name)
    _report({"code": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
