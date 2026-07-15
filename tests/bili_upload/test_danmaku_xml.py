from pathlib import Path

import pytest

import blrec.setting  # noqa: F401  # Initialize settings before its core import.
from blrec.core.models import DanmuMsg
from blrec.danmaku.io import DanmakuReader, DanmakuWriter
from blrec.danmaku.models import Danmu


@pytest.mark.asyncio
async def test_old_xml_without_optional_attributes_is_kept(tmp_path: Path) -> None:
    path = tmp_path / 'old.xml'
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<i><d p="1.250,1,25,16777215,1000,0,hash,9" '
        'uid="42" user="用户">旧弹幕</d></i>',
        encoding='utf8',
    )

    async with DanmakuReader(str(path)) as reader:
        rows = [row async for row in reader.read_danmus()]

    assert len(rows) == 1
    assert rows[0].text == '旧弹幕'
    assert rows[0].source_event_id is None
    assert rows[0].is_system is None
    assert rows[0].is_lottery is None
    assert rows[0].user_level is None
    assert rows[0].fan_medal_name is None
    assert rows[0].fan_medal_level is None


@pytest.mark.asyncio
async def test_optional_filter_metadata_round_trips(tmp_path: Path) -> None:
    path = tmp_path / 'new.xml'
    expected = Danmu(
        stime=2.5,
        mode=1,
        size=25,
        color=16777215,
        date=1000,
        pool=0,
        uid_hash='hash',
        uid=42,
        uname='用户',
        dmid=10,
        text='新弹幕',
        source_event_id='event-1',
        is_system=False,
        is_lottery=True,
        user_level=6,
        fan_medal_name='粉丝牌',
        fan_medal_level=12,
    )

    async with DanmakuWriter(str(path)) as writer:
        await writer.write_danmu(expected)
    async with DanmakuReader(str(path)) as reader:
        actual = [row async for row in reader.read_danmus()][0]

    assert actual == expected


def test_receiver_extracts_optional_filter_metadata() -> None:
    info_head = [0, 1, 25, 16777215, 1000, 9, 0, 'hash', 0, 1]
    info = [info_head, '抽奖弹幕', [42, '用户'], [12, '粉丝牌'], [6]]

    message = DanmuMsg.from_danmu(
        {'cmd': 'DANMU_MSG', 'msg_id': 'event-1', 'info': info}
    )

    assert message.source_event_id == 'event-1'
    assert message.is_system is False
    assert message.is_lottery is True
    assert message.user_level == 6
    assert message.fan_medal_name == '粉丝牌'
    assert message.fan_medal_level == 12
