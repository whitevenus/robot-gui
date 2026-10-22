"""删动作时连带删掉同名讲解语音。

场景是「改稿后重新同步」：平台上重做一段动作或重生成一次语音，资产 UUID 不变、
内容变了，同步到机器人就会被 action_conflict / audio_conflict 挡下。

解法是删动作时把同名语音一起删掉——语音是按动作名存的（<动作名>.pcm），
动作没了语音留着就是孤儿，重新导入同名动作时一定撞上 audio_conflict。
"""

from __future__ import annotations

from pathlib import Path

from test_courses import build_client, build_package

from robot_gui.actions import ActionStore

ACTION_ID = "aaaaaaaa-0000-4000-8000-000000000000"


def test_deleting_an_action_also_deletes_its_audio(tmp_path: Path) -> None:
    """动作与讲解语音是一对：动作没了，语音也不能留着。"""
    store = ActionStore(tmp_path)
    (tmp_path / "audio").mkdir()
    (tmp_path / f"{ACTION_ID}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "audio" / f"{ACTION_ID}.pcm").write_bytes(b"\x00\x00" * 16)

    store.delete(ACTION_ID)

    assert not (tmp_path / f"{ACTION_ID}.json").exists()
    assert not (tmp_path / "audio" / f"{ACTION_ID}.pcm").exists()


def test_deleting_an_action_without_audio_is_fine(tmp_path: Path) -> None:
    """没有同名语音的动作也能删：语音本来就是可选的。"""
    store = ActionStore(tmp_path)
    (tmp_path / f"{ACTION_ID}.json").write_text("{}", encoding="utf-8")

    store.delete(ACTION_ID)

    assert not (tmp_path / f"{ACTION_ID}.json").exists()


def test_a_changed_action_can_be_reimported_after_deleting_it(
    tmp_path: Path,
) -> None:
    """改稿后重新同步：先撞 action_conflict，删掉旧动作再导就能过。"""
    # build_package 每次都写同一个文件名：读完字节再建下一个包。
    original = build_package(tmp_path).read_bytes()
    changed = build_package(
        tmp_path,
        motion_by_index={
            0: {
                "group": "arms",
                "frames": [{"t": k / 30, "q": [0.1] * 14} for k in range(3)],
            }
        },
    ).read_bytes()

    with build_client(tmp_path) as client:
        first = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", original, "application/zip")},
        )
        assert first.status_code == 201, first.text

        conflict = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", changed, "application/zip")},
        )
        assert conflict.status_code == 400
        assert conflict.json()["detail"]["code"] == "action_conflict"

        # 删掉这一段的动作（连带同名语音），再导一次就通了。
        assert client.delete(f"/api/v1/actions/{ACTION_ID}").status_code == 200
        assert not (tmp_path / "audio" / f"{ACTION_ID}.pcm").exists()

        again = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", changed, "application/zip")},
        )
        assert again.status_code == 201, again.text


def test_a_changed_voice_can_be_reimported_after_deleting_the_action(
    tmp_path: Path,
) -> None:
    """重新生成了讲解语音：撞的是 audio_conflict，删动作连带删语音后能重导。"""
    # 只做一段：语音一改就整段变，删掉这一段的动作与语音就能重导。
    original = build_package(tmp_path, segments=1).read_bytes()
    changed = build_package(tmp_path, segments=1, audio_seconds=2.0).read_bytes()

    with build_client(tmp_path) as client:
        first = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", original, "application/zip")},
        )
        assert first.status_code == 201, first.text

        conflict = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", changed, "application/zip")},
        )
        assert conflict.status_code == 400
        assert conflict.json()["detail"]["code"] == "audio_conflict"

        assert client.delete(f"/api/v1/actions/{ACTION_ID}").status_code == 200
        assert not (tmp_path / "audio" / f"{ACTION_ID}.pcm").exists()

        again = client.post(
            "/api/v1/courses/import",
            files={"file": ("courseware.zip", changed, "application/zip")},
        )
        assert again.status_code == 201, again.text
