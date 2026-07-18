import json
from typing import Any, Literal, Mapping, Optional, Sequence

import attr

from blrec.bili.typing import Danmaku


@attr.s(auto_attribs=True, frozen=True, slots=True)
class DanmuMsg:
    mode: int
    size: int  # font size
    color: int
    date: int  # a timestamp in miliseconds
    dmid: int
    pool: int
    uid_hash: str
    uid: int
    uname: str  # sender name
    text: str
    source_event_id: Optional[str] = None
    is_system: Optional[bool] = None
    is_lottery: Optional[bool] = None
    user_level: Optional[int] = None
    fan_medal_name: Optional[str] = None
    fan_medal_level: Optional[int] = None

    @staticmethod
    def from_danmu(danmu: Danmaku) -> 'DanmuMsg':
        info = danmu['info']
        head = info[0]
        sender = info[2]
        uid = int(sender[0])
        source_event_id = _nonempty_text(danmu.get('msg_id'))
        if source_event_id is None and len(head) > 15:
            source_event_id = _event_id_from_extra(head[15])
        return DanmuMsg(
            mode=int(head[1]),
            size=int(head[2]),
            color=int(head[3]),
            date=int(head[4]),
            dmid=int(head[5]),
            pool=int(head[6]),
            uid_hash=head[7],
            uid=uid,
            uname=sender[1],
            text=info[1],
            source_event_id=source_event_id,
            # Bilibili now masks many ordinary senders as uid=0. The command is
            # still DANMU_MSG, so uid alone cannot identify a system message.
            is_system=False,
            is_lottery=_optional_bool(head, 9),
            user_level=_optional_int(info[4] if len(info) > 4 else (), 0),
            fan_medal_name=_optional_text(info[3] if len(info) > 3 else (), 1),
            fan_medal_level=_optional_int(info[3] if len(info) > 3 else (), 0),
        )


def _optional_int(values: Any, index: int) -> Optional[int]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    if len(values) <= index:
        return None
    value = values[index]
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional_bool(values: Any, index: int) -> Optional[bool]:
    value = _optional_int(values, index)
    return None if value is None else value != 0


def _optional_text(values: Any, index: int) -> Optional[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    if len(values) <= index:
        return None
    return _nonempty_text(values[index])


def _nonempty_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _event_id_from_extra(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    extra = value.get('extra')
    if not isinstance(extra, str):
        return None
    try:
        parsed = json.loads(extra)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    return _nonempty_text(parsed.get('id_str'))


@attr.s(auto_attribs=True, slots=True, frozen=True)
class UserToastMsg:
    start_time: int  # timestamp in seconds
    uid: int
    username: str
    unit: str
    num: int
    price: int
    role_name: str
    guard_level: str
    toast_msg: str

    @staticmethod
    def from_danmu(danmu: Danmaku) -> 'UserToastMsg':
        data = danmu['data']
        return UserToastMsg(
            start_time=data['start_time'],
            uid=data['uid'],
            username=data['username'],
            unit=data['unit'],
            num=data['num'],
            price=data['price'],
            role_name=data['role_name'],
            guard_level=data['guard_level'],
            toast_msg=data['toast_msg'].replace('<%', '').replace('%>', ''),
        )


@attr.s(auto_attribs=True, frozen=True, slots=True)
class GiftSendMsg:
    gift_name: str
    count: int
    coin_type: Literal['sliver', 'gold']
    price: int
    uid: int
    uname: str
    timestamp: int  # timestamp in seconds

    @staticmethod
    def from_danmu(danmu: Danmaku) -> 'GiftSendMsg':
        data = danmu['data']
        return GiftSendMsg(
            gift_name=data['giftName'],
            count=int(data['num']),
            coin_type=data['coin_type'],
            price=int(data['price']),
            uid=int(data['uid']),
            uname=data['uname'],
            timestamp=int(data['timestamp']),
        )


@attr.s(auto_attribs=True, frozen=True, slots=True)
class GuardBuyMsg:
    gift_name: str
    count: int
    price: int
    uid: int
    uname: str
    guard_level: int  # 1 总督, 2 提督, 3 舰长
    timestamp: int  # timestamp in seconds

    @staticmethod
    def from_danmu(danmu: Danmaku) -> 'GuardBuyMsg':
        data = danmu['data']
        return GuardBuyMsg(
            gift_name=data['gift_name'],
            count=int(data['num']),
            price=int(data['price']),
            uid=int(data['uid']),
            uname=data['username'],
            guard_level=int(data['guard_level']),
            timestamp=int(data['start_time']),
        )


@attr.s(auto_attribs=True, frozen=True, slots=True)
class SuperChatMsg:
    gift_name: str
    count: int
    price: int
    rate: int
    time: int  # duration in seconds
    message: str
    uid: int
    uname: str
    timestamp: int  # timestamp in seconds

    @staticmethod
    def from_danmu(danmu: Danmaku) -> 'SuperChatMsg':
        data = danmu['data']
        return SuperChatMsg(
            gift_name=data['gift']['gift_name'],
            count=int(data['gift']['num']),
            price=int(data['price']),
            rate=int(data['rate']),
            time=int(data['time']),
            message=data['message'],
            uid=int(data['uid']),
            uname=data['user_info']['uname'],
            timestamp=int(data['ts']),
        )
