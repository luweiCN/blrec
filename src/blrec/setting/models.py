from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from typing import (
    ClassVar,
    Collection,
    Dict,
    Final,
    List,
    Literal,
    Optional,
    Tuple,
    TypeVar,
)

import toml
from pydantic import BaseModel as PydanticBaseModel
from pydantic import BaseSettings, Field, PrivateAttr, root_validator, validator
from pydantic.networks import EmailStr, HttpUrl
from typing_extensions import Annotated

from blrec.bili.typing import QualityNumber, StreamFormat
from blrec.core.cover_downloader import CoverSaveStrategy
from blrec.logging.typing import LOG_LEVEL
from blrec.postprocess import DeleteStrategy
from blrec.utils.string import camel_case

from .typing import (
    BarkMessageType,
    EmailMessageType,
    MessageType,
    PushdeerMessageType,
    PushplusMessageType,
    RecordingMode,
    ServerchanMessageType,
    TelegramMessageType,
)

__all__ = (
    'DEFAULT_SETTINGS_FILE',
    'EnvSettings',
    'Settings',
    'SettingsIn',
    'SettingsOut',
    'BiliApiSettings',
    'BiliUploadSettings',
    'NetworkRouteSettings',
    'NetworkSettings',
    'LiveMonitorSettings',
    'HeaderOptions',
    'HeaderSettings',
    'DanmakuOptions',
    'DanmakuSettings',
    'RecorderOptions',
    'RecorderSettings',
    'PostprocessingSettings',
    'PostprocessingOptions',
    'TaskOptions',
    'TaskSettings',
    'OutputSettings',
    'LoggingSettings',
    'SpaceSettings',
    'EmailSettings',
    'ServerchanSettings',
    'PushdeerSettings',
    'PushplusSettings',
    'TelegramSettings',
    'BarkSettings',
    'NotifierSettings',
    'NotificationSettings',
    'OperationalNotificationTarget',
    'OperationalNotificationRoute',
    'OperationalNotificationSettings',
    'EmailMessageTemplateSettings',
    'ServerchanMessageTemplateSettings',
    'PushdeerMessageTemplateSettings',
    'PushplusMessageTemplateSettings',
    'TelegramMessageTemplateSettings',
    'BarkMessageTemplateSettings',
    'EmailNotificationSettings',
    'ServerchanNotificationSettings',
    'PushdeerNotificationSettings',
    'PushplusNotificationSettings',
    'TelegramNotificationSettings',
    'BarkNotificationSettings',
    'WebHookSettings',
)


DEFAULT_OUT_DIR: Final[str] = os.environ.get('BLREC_DEFAULT_OUT_DIR', '.')
DEFAULT_LOG_DIR: Final[str] = os.environ.get('BLREC_DEFAULT_LOG_DIR', '~/.blrec/logs/')
DEFAULT_SETTINGS_FILE: Final[str] = os.environ.get(
    'BLREC_DEFAULT_SETTINGS_FILE', '~/.blrec/settings.toml'
)


def _decode_credential_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip(), altchars=b'-_', validate=True)
    except (binascii.Error, ValueError):
        key = b''
    if len(key) != 32:
        raise ValueError('credential key must decode to 32 bytes')
    return key


def _load_credential_key_file(path: str) -> bytes:
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise ValueError(f'credential key file cannot be read: {path}') from error
    if stat.S_ISLNK(file_stat.st_mode):
        raise ValueError('credential key file must not be a symlink')
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError('credential key file must be a regular file')
    if file_stat.st_mode & 0o077:
        raise ValueError('credential key file must use 0600 permissions')

    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f'credential key file cannot be read: {path}') from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError('credential key file must be a regular file')
        if opened_stat.st_mode & 0o077:
            raise ValueError('credential key file must use 0600 permissions')
        with os.fdopen(descriptor, 'rt', encoding='ascii') as file:
            descriptor = -1
            value = file.read()
    except (OSError, UnicodeError) as error:
        raise ValueError(f'credential key file cannot be read: {path}') from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _decode_credential_key(value)


def _parse_old_credential_key_files(value: object) -> Dict[str, str]:
    if value is None:
        return {}
    entries: List[Tuple[object, object]]
    if isinstance(value, dict):
        entries = list(value.items())
    elif isinstance(value, str):
        entries = []
        for item in value.split(','):
            if '=' not in item:
                raise ValueError(
                    'old credential keys must use key_id=/absolute/path pairs'
                )
            key_id, path = item.split('=', 1)
            entries.append((key_id, path))
    else:
        raise ValueError('old credential keys must use key_id=/absolute/path pairs')

    result: Dict[str, str] = {}
    for raw_key_id, raw_path in entries:
        key_id = str(raw_key_id).strip()
        path = str(raw_path).strip()
        if not key_id or not os.path.isabs(path):
            raise ValueError('old credential keys must use key_id=/absolute/path pairs')
        if key_id in result:
            raise ValueError(f'duplicate old credential key id: {key_id}')
        result[key_id] = path
    return result


class EnvSettings(BaseSettings):
    settings_file: Annotated[str, Field(env='BLREC_CONFIG')] = DEFAULT_SETTINGS_FILE
    out_dir: Annotated[Optional[str], Field(env='BLREC_OUT_DIR')] = None
    log_dir: Annotated[Optional[str], Field(env='BLREC_LOG_DIR')] = None
    admin_username: Annotated[str, Field(env='BLREC_ADMIN_USERNAME')] = 'admin'
    api_key: Annotated[
        Optional[str],
        Field(
            env='BLREC_API_KEY',
            min_length=8,
            max_length=80,
            regex=r'[a-zA-Z\d\-]{8,80}',
        ),
    ] = None
    credential_key: Annotated[Optional[str], Field(env='BLREC_CREDENTIAL_KEY')] = None
    credential_key_file: Annotated[
        Optional[str], Field(env='BLREC_CREDENTIAL_KEY_FILE')
    ] = None
    credential_old_key_files: Annotated[
        Dict[str, str], Field(env='BLREC_CREDENTIAL_OLD_KEY_FILES')
    ] = {}

    @validator('admin_username')
    def _validate_admin_username(cls, value: str) -> str:
        if (
            not 1 <= len(value) <= 64
            or value != value.strip()
            or any(not char.isprintable() for char in value)
        ):
            raise ValueError(
                'administrator username must contain 1 to 64 visible characters'
            )
        return value

    @root_validator(pre=True)
    def _parse_credential_sources(cls, values: Dict[str, object]) -> Dict[str, object]:
        admin_username = values.get('admin_username')
        if isinstance(admin_username, str) and (
            admin_username != admin_username.strip()
            or any(not char.isprintable() for char in admin_username)
        ):
            raise ValueError(
                'administrator username must contain 1 to 64 visible characters'
            )
        if (
            values.get('credential_key') is not None
            and values.get('credential_key_file') is not None
        ):
            raise ValueError(
                'BLREC_CREDENTIAL_KEY and BLREC_CREDENTIAL_KEY_FILE '
                'must not both be set'
            )
        values['credential_old_key_files'] = _parse_old_credential_key_files(
            values.get('credential_old_key_files')
        )
        return values

    @root_validator
    def _validate_credential_sources(
        cls, values: Dict[str, object]
    ) -> Dict[str, object]:
        credential_key = values.get('credential_key')
        credential_key_file = values.get('credential_key_file')
        current_key: Optional[bytes] = None
        if isinstance(credential_key, str):
            current_key = _decode_credential_key(credential_key)
        elif isinstance(credential_key_file, str):
            current_key = _load_credential_key_file(credential_key_file)

        old_key_files = values.get('credential_old_key_files', {})
        assert isinstance(old_key_files, dict)
        for path in old_key_files.values():
            _load_credential_key_file(path)
        if current_key is not None:
            current_key_id = hashlib.sha256(current_key).hexdigest()
            if current_key_id in old_key_files:
                raise ValueError(
                    'old credential key id duplicates current credential key id'
                )
        return values

    def load_credential_key(self) -> Optional[bytes]:
        if self.credential_key is not None:
            return _decode_credential_key(self.credential_key)
        if self.credential_key_file is not None:
            return _load_credential_key_file(self.credential_key_file)
        return None

    def load_old_credential_keys(self) -> Dict[str, bytes]:
        return {
            key_id: _load_credential_key_file(path)
            for key_id, path in self.credential_old_key_files.items()
        }

    class Config:
        anystr_strip_whitespace = True

        @classmethod
        def parse_env_var(cls, field_name: str, raw_value: str) -> object:
            if field_name == 'credential_old_key_files':
                return raw_value
            return json.loads(raw_value)


_V = TypeVar('_V')


class BaseModel(PydanticBaseModel):
    class Config:
        validate_assignment = True
        anystr_strip_whitespace = True
        allow_population_by_field_name = True

        @classmethod
        def alias_generator(cls, string: str) -> str:
            return camel_case(string)

    @staticmethod
    def _validate_with_collection(value: _V, allowed_values: Collection[_V]) -> None:
        if value not in allowed_values:
            raise ValueError(
                f'the value {value} does not be allowed, '
                f'must be one of {", ".join(map(str, allowed_values))}'
            )


class BiliApiSettings(BaseModel):
    base_api_urls: List[str] = ['https://api.bilibili.com']
    base_live_api_urls: List[str] = ['https://api.live.bilibili.com']
    base_play_info_api_urls: List[str] = ['https://api.live.bilibili.com']


class BiliUploadSettings(BaseModel):
    database_path: str = '/cfg/blrec.sqlite3'
    upload_chunk_size: Annotated[int, Field(ge=1024 * 1024, le=32 * 1024 * 1024)] = (
        4 * 1024 * 1024
    )
    upload_chunk_concurrency: Annotated[int, Field(ge=1, le=3)] = 2
    danmaku_interval_seconds: Annotated[int, Field(ge=25, le=3600)] = 25
    import_high_watermark: Annotated[int, Field(ge=10000)] = 1000000


class LiveMonitorSettings(BaseModel):
    mode: Literal['batch', 'legacy'] = 'batch'
    interval_seconds: Annotated[int, Field(ge=30, le=60)] = 30
    batch_size: Annotated[int, Field(ge=1, le=29)] = 29
    fallback_cooldown_seconds: Annotated[int, Field(ge=600, le=3600)] = 600


class NetworkRouteSettings(BaseModel):
    primary_interface: Optional[str] = None
    fallback_interface: Optional[str] = None
    failover_enabled: bool = True

    @root_validator
    def _validate_distinct_interfaces(
        cls, values: Dict[str, object]
    ) -> Dict[str, object]:
        primary = values.get('primary_interface')
        fallback = values.get('fallback_interface')
        if primary is not None and primary == fallback:
            raise ValueError('primary and fallback interfaces must be different')
        return values


class NetworkSettings(BaseModel):
    room_status: NetworkRouteSettings = NetworkRouteSettings()
    danmaku: NetworkRouteSettings = NetworkRouteSettings()
    recording: NetworkRouteSettings = NetworkRouteSettings()
    upload: NetworkRouteSettings = NetworkRouteSettings()
    bili_api: NetworkRouteSettings = NetworkRouteSettings()


class HeaderOptions(BaseModel):
    user_agent: Optional[str]
    cookie: Optional[str]


class HeaderSettings(HeaderOptions):
    user_agent: str = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'  # noqa
    )
    cookie: str = ''


class DanmakuOptions(BaseModel):
    danmu_uname: Optional[bool]
    record_gift_send: Optional[bool]
    record_free_gifts: Optional[bool]
    record_guard_buy: Optional[bool]
    record_super_chat: Optional[bool]
    save_raw_danmaku: Optional[bool]


class DanmakuSettings(DanmakuOptions):
    danmu_uname: bool = False
    record_gift_send: bool = True
    record_free_gifts: bool = True
    record_guard_buy: bool = True
    record_super_chat: bool = True
    save_raw_danmaku: bool = False


class RecorderOptions(BaseModel):
    stream_format: Optional[StreamFormat]
    recording_mode: Optional[RecordingMode]
    quality_number: Optional[QualityNumber]
    fmp4_stream_timeout: Optional[int]
    read_timeout: Optional[int]  # seconds
    disconnection_timeout: Optional[int]  # seconds
    buffer_size: Annotated[  # bytes
        Optional[int], Field(ge=4096, le=1024**2 * 512, multiple_of=2)
    ]
    save_cover: Optional[bool]
    cover_save_strategy: Optional[CoverSaveStrategy]

    @validator('fmp4_stream_timeout')
    def _validate_fmp4_stream_timeout(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            allowed_values = frozenset((3, 5, 10, 30, 60, 180, 300, 600))
            cls._validate_with_collection(v, allowed_values)
        return v

    @validator('read_timeout')
    def _validate_read_timeout(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            allowed_values = frozenset((3, 5, 10, 30, 60, 180, 300, 600))
            cls._validate_with_collection(value, allowed_values)
        return value

    @validator('disconnection_timeout')
    def _validate_disconnection_timeout(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            allowed_values = frozenset(60 * i for i in (3, 5, 10, 15, 20, 30))
            cls._validate_with_collection(value, allowed_values)
        return value


class RecorderSettings(RecorderOptions):
    stream_format: StreamFormat = 'flv'
    recording_mode: RecordingMode = 'standard'
    quality_number: QualityNumber = 20000  # 4K, the highest quality.
    fmp4_stream_timeout: int = 10
    read_timeout: int = 3
    disconnection_timeout: int = 600
    buffer_size: Annotated[int, Field(ge=4096, le=1024**2 * 512, multiple_of=2)] = 8192
    save_cover: bool = False
    cover_save_strategy: CoverSaveStrategy = CoverSaveStrategy.DEFAULT


class PostprocessingOptions(BaseModel):
    remux_to_mp4: Optional[bool]
    inject_extra_metadata: Optional[bool]
    delete_source: Optional[DeleteStrategy]


class PostprocessingSettings(PostprocessingOptions):
    remux_to_mp4: bool = False
    inject_extra_metadata: bool = True
    delete_source: DeleteStrategy = DeleteStrategy.AUTO


class OutputOptions(BaseModel):
    path_template: Optional[str]
    filesize_limit: Optional[int]  # file size in bytes
    duration_limit: Optional[int]  # duration in seconds

    @validator('path_template')
    def _validate_path_template(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            pattern = r'''^
                (?:
                    [^\\/:*?"<>|\t\n\r\f\v\{\}]*?
                    \{
                    (?:
                        roomid|uname|title|area|parent_area|
                        year|month|day|hour|minute|second
                    )
                    \}
                    [^\\/:*?"<>|\t\n\r\f\v\{\}]*?
                )+?
                (?:
                    /
                    (?:
                        [^\\/:*?"<>|\t\n\r\f\v\{\}]*?
                        \{
                        (?:
                            roomid|uname|title|area|parent_area|
                            year|month|day|hour|minute|second
                        )
                        \}
                        [^\\/:*?"<>|\t\n\r\f\v\{\}]*?
                    )+?
                )*
            $'''
            if not re.fullmatch(pattern, value, re.VERBOSE):
                raise ValueError(f"invalid path template: '{value}'")
        return value

    @validator('filesize_limit')
    def _validate_filesize_limit(cls, value: Optional[int]) -> Optional[int]:
        # file size in bytes, 0 indicates not limit。
        if value is not None:
            if not (0 <= value <= 1073731086581):  # 1073731086581(999.99 GB)
                raise ValueError(
                    'The filesize limit must be in the range of 0 to 1073731086581'
                )
        return value

    @validator('duration_limit')
    def _validate_duration_limit(cls, value: Optional[int]) -> Optional[int]:
        # duration in seconds, 0 indicates not limit。
        if value is not None:
            if not (0 <= value <= 359999):  # 359999(99:59:59)
                raise ValueError(
                    'The duration limit must be in the range of 0 to 359999'
                )
        return value


def out_dir_factory() -> str:
    path = os.path.normpath(os.path.expanduser(DEFAULT_OUT_DIR))
    os.makedirs(path, exist_ok=True)
    return path


class OutputSettings(OutputOptions):
    out_dir: Annotated[str, Field(default_factory=out_dir_factory)]
    path_template: str = (
        '{roomid} - {uname}/'
        'blive_{roomid}_{year}-{month}-{day}-{hour}{minute}{second}'
    )
    filesize_limit: int = 0  # no limit by default
    duration_limit: int = 0  # no limit by default

    @validator('out_dir')
    def _validate_dir(cls, path: str) -> str:
        if not os.path.isdir(os.path.expanduser(path)):
            raise ValueError(f"'{path}' not a directory")
        return path


class TaskOptions(BaseModel):
    output: OutputOptions = OutputOptions()
    header: HeaderOptions = HeaderOptions()
    danmaku: DanmakuOptions = DanmakuOptions()
    recorder: RecorderOptions = RecorderOptions()
    postprocessing: PostprocessingOptions = PostprocessingOptions()

    @classmethod
    def from_settings(cls, settings: TaskSettings) -> TaskOptions:
        return cls(
            **settings.dict(
                include={'output', 'header', 'danmaku', 'recorder', 'postprocessing'}
            )
        )


class TaskSettings(TaskOptions):
    # must use the real room id rather than the short room id!
    room_id: Annotated[int, Field(ge=1, lt=2**100)]
    enable_monitor: bool = True
    enable_recorder: bool = True


def log_dir_factory() -> str:
    path = os.path.normpath(os.path.expanduser(DEFAULT_LOG_DIR))
    os.makedirs(path, exist_ok=True)
    return path


class LoggingSettings(BaseModel):
    log_dir: Annotated[str, Field(default_factory=log_dir_factory)]
    console_log_level: LOG_LEVEL = 'INFO'
    backup_count: Annotated[int, Field(ge=0, le=90)] = 30

    @validator('log_dir')
    def _validate_dir(cls, path: str) -> str:
        if not os.path.isdir(os.path.expanduser(path)):
            raise ValueError(f"'{path}' not a directory")
        return path


class SpaceSettings(BaseModel):
    check_interval: int = 60  # 1 minutes
    space_threshold: int = 1024**3  # 1 GB
    recycle_records: bool = False
    recording_capacity: Annotated[int, Field(ge=0, le=1024**5)] = 0
    capacity_warning_threshold: Annotated[int, Field(ge=0, le=1024**5)] = 20 * 1024**3

    @validator('check_interval')
    def _validate_interval(cls, value: int) -> int:
        allowed_values = frozenset((0, 10, 30, *(60 * i for i in (1, 3, 5, 10))))
        cls._validate_with_collection(value, allowed_values)
        return value

    @validator('space_threshold')
    def _validate_threshold(cls, value: int) -> int:
        allowed_values = frozenset(1024**3 * i for i in (1, 3, 5, 10, 20))
        cls._validate_with_collection(value, allowed_values)
        return value


class EmailSettings(BaseModel):
    src_addr: Annotated[str, EmailStr] = ''
    dst_addr: Annotated[str, EmailStr] = ''
    auth_code: str = ''
    smtp_host: str = 'smtp.163.com'
    smtp_port: int = 465


class ServerchanSettings(BaseModel):
    sendkey: str = ''

    @validator('sendkey')
    def _validate_sendkey(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'[a-zA-Z\d]+', value):
            raise ValueError('sendkey is invalid')
        return value


class PushdeerSettings(BaseModel):
    server: str = ''
    pushkey: str = ''

    @validator('server')
    def _validate_server(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'https?://.+', value):
            raise ValueError('server is invalid')
        return value

    @validator('pushkey')
    def _validate_pushkey(cls, value: str) -> str:
        if value != '' and not re.fullmatch(
            r'PDU\d+T[a-zA-Z\d]{32}(,PDU\d+T[a-zA-Z\d]{32}){0,99}', value
        ):
            raise ValueError('pushkey is invalid')
        return value


class PushplusSettings(BaseModel):
    token: str = ''
    topic: str = ''

    @validator('token')
    def _validate_token(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'[a-z\d]{32}', value):
            raise ValueError('token is invalid')
        return value


class TelegramSettings(BaseModel):
    token: str = ''
    chatid: str = ''
    server: str = ''

    @validator('token')
    def _validate_token(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'[0-9]{8,10}:[a-zA-Z0-9_-]{35}', value):
            raise ValueError('token is invalid')
        return value

    @validator('chatid')
    def _validate_chatid(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'(-|[0-9]){0,}', value):
            raise ValueError('chatid is invalid')
        return value

    @validator('server')
    def _validate_server(cls, value: str) -> str:
        if value != '' and not re.fullmatch(
            r'^https?:\/\/[a-zA-Z0-9-_.]+(:[0-9]+)?', value
        ):
            raise ValueError('server is invalid')
        return value


class BarkSettings(BaseModel):
    server: str = ''
    pushkey: str = ''

    @validator('server')
    def _validate_server(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'https?://.+', value):
            raise ValueError('server is invalid')
        return value

    @validator('pushkey')
    def _validate_pushkey(cls, value: str) -> str:
        if value != '' and not re.fullmatch(r'[a-zA-Z\d]+', value):
            raise ValueError('pushkey is invalid')
        return value


class NotifierSettings(BaseModel):
    enabled: bool = False


class NotificationSettings(BaseModel):
    notify_began: bool = True
    notify_ended: bool = True
    notify_error: bool = True
    notify_space: bool = True


OperationalEventCode = Literal[
    'account_unavailable',
    'network_unavailable',
    'network_failover',
    'recording_failed',
    'upload_failed',
    'review_rejected',
    'collection_failed',
    'comment_failed',
    'danmaku_failed',
    'transcode_repair_failed',
    'capacity_warning',
]
OperationalChannel = Literal[
    'email', 'serverchan', 'pushdeer', 'pushplus', 'telegram', 'bark'
]

_OPERATIONAL_EVENT_CODES: Tuple[OperationalEventCode, ...] = (
    'account_unavailable',
    'network_unavailable',
    'network_failover',
    'recording_failed',
    'upload_failed',
    'review_rejected',
    'collection_failed',
    'comment_failed',
    'danmaku_failed',
    'transcode_repair_failed',
    'capacity_warning',
)
_OPERATIONAL_MESSAGE_TYPES = {
    'email': frozenset(('text', 'html')),
    'serverchan': frozenset(('markdown',)),
    'pushdeer': frozenset(('text', 'markdown')),
    'pushplus': frozenset(('text', 'markdown', 'html')),
    'telegram': frozenset(('markdown', 'html')),
    'bark': frozenset(('text', 'markdown')),
}


class OperationalNotificationTarget(BaseModel):
    channel: OperationalChannel
    message_type: MessageType = 'text'

    @root_validator
    def validate_message_type(cls, values: Dict[str, object]) -> Dict[str, object]:
        channel = values.get('channel')
        message_type = values.get('message_type')
        if (
            isinstance(channel, str)
            and isinstance(message_type, str)
            and message_type not in _OPERATIONAL_MESSAGE_TYPES[channel]
        ):
            raise ValueError(
                "channel '{}' does not support '{}' messages".format(
                    channel, message_type
                )
            )
        return values


class OperationalNotificationRoute(BaseModel):
    event: OperationalEventCode
    targets: Annotated[List[OperationalNotificationTarget], Field(max_items=6)] = []
    notify_recovery: bool = True

    @validator('targets')
    def targets_must_be_unique(
        cls, targets: List[OperationalNotificationTarget]
    ) -> List[OperationalNotificationTarget]:
        channels = [target.channel for target in targets]
        if len(channels) != len(set(channels)):
            raise ValueError('duplicate channel in operational notification route')
        return targets


def _default_operational_routes() -> List[OperationalNotificationRoute]:
    return [
        OperationalNotificationRoute(event=event) for event in _OPERATIONAL_EVENT_CODES
    ]


class OperationalNotificationSettings(BaseModel):
    routes: List[OperationalNotificationRoute] = Field(
        default_factory=_default_operational_routes
    )

    @validator('routes')
    def routes_must_be_unique(
        cls, routes: List[OperationalNotificationRoute]
    ) -> List[OperationalNotificationRoute]:
        events = [route.event for route in routes]
        if len(events) != len(set(events)):
            raise ValueError('duplicate operational notification event route')
        return routes

    def route_for(self, event: OperationalEventCode) -> OperationalNotificationRoute:
        for route in self.routes:
            if route.event == event:
                return route
        return OperationalNotificationRoute(event=event)


class MessageTemplateSettings(BaseModel):
    began_message_type: MessageType
    began_message_title: str
    began_message_content: str
    ended_message_type: MessageType
    ended_message_title: str
    ended_message_content: str
    space_message_type: MessageType
    space_message_title: str
    space_message_content: str
    error_message_type: MessageType
    error_message_title: str
    error_message_content: str


class EmailMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: EmailMessageType = 'html'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: EmailMessageType = 'html'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: EmailMessageType = 'html'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: EmailMessageType = 'html'
    error_message_title: str = ''
    error_message_content: str = ''


class ServerchanMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: ServerchanMessageType = 'markdown'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: ServerchanMessageType = 'markdown'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: ServerchanMessageType = 'markdown'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: ServerchanMessageType = 'markdown'
    error_message_title: str = ''
    error_message_content: str = ''


class PushdeerMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: PushdeerMessageType = 'markdown'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: PushdeerMessageType = 'markdown'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: PushdeerMessageType = 'markdown'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: PushdeerMessageType = 'markdown'
    error_message_title: str = ''
    error_message_content: str = ''


class PushplusMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: PushplusMessageType = 'markdown'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: PushplusMessageType = 'markdown'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: PushplusMessageType = 'markdown'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: PushplusMessageType = 'markdown'
    error_message_title: str = ''
    error_message_content: str = ''


class TelegramMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: TelegramMessageType = 'html'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: TelegramMessageType = 'html'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: TelegramMessageType = 'html'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: TelegramMessageType = 'html'
    error_message_title: str = ''
    error_message_content: str = ''


class BarkMessageTemplateSettings(MessageTemplateSettings):
    began_message_type: BarkMessageType = 'markdown'
    began_message_title: str = ''
    began_message_content: str = ''
    ended_message_type: BarkMessageType = 'markdown'
    ended_message_title: str = ''
    ended_message_content: str = ''
    space_message_type: BarkMessageType = 'markdown'
    space_message_title: str = ''
    space_message_content: str = ''
    error_message_type: BarkMessageType = 'markdown'
    error_message_title: str = ''
    error_message_content: str = ''


class EmailNotificationSettings(
    EmailSettings, NotifierSettings, NotificationSettings, EmailMessageTemplateSettings
):
    pass


class ServerchanNotificationSettings(
    ServerchanSettings,
    NotifierSettings,
    NotificationSettings,
    ServerchanMessageTemplateSettings,
):
    pass


class PushdeerNotificationSettings(
    PushdeerSettings,
    NotifierSettings,
    NotificationSettings,
    PushplusMessageTemplateSettings,
):
    pass


class PushplusNotificationSettings(
    PushplusSettings,
    NotifierSettings,
    NotificationSettings,
    PushplusMessageTemplateSettings,
):
    pass


class TelegramNotificationSettings(
    TelegramSettings,
    NotifierSettings,
    NotificationSettings,
    TelegramMessageTemplateSettings,
):
    pass


class BarkNotificationSettings(
    BarkSettings, NotifierSettings, NotificationSettings, BarkMessageTemplateSettings
):
    pass


class WebHookEventSettings(BaseModel):
    live_began: bool = True
    live_ended: bool = True
    room_change: bool = True
    recording_started: bool = True
    recording_finished: bool = True
    recording_cancelled: bool = True
    video_file_created: bool = True
    video_file_completed: bool = True
    danmaku_file_created: bool = True
    danmaku_file_completed: bool = True
    raw_danmaku_file_created: bool = True
    raw_danmaku_file_completed: bool = True
    cover_image_downloaded: bool = True
    video_postprocessing_completed: bool = True
    postprocessing_completed: bool = True
    space_no_enough: bool = True
    error_occurred: bool = True


class WebHookSettings(WebHookEventSettings):
    url: Annotated[str, HttpUrl]


class Settings(BaseModel):
    _MAX_TASKS: ClassVar[int] = 100
    _MAX_WEBHOOKS: ClassVar[int] = 50

    _path: str = PrivateAttr()
    version: str = '1.0'

    tasks: Annotated[List[TaskSettings], Field(max_items=100)] = []
    output: OutputSettings = OutputSettings()  # type: ignore
    logging: LoggingSettings = LoggingSettings()  # type: ignore
    bili_api: BiliApiSettings = BiliApiSettings()
    bili_upload: BiliUploadSettings = BiliUploadSettings()
    live_monitor: LiveMonitorSettings = LiveMonitorSettings()
    network: NetworkSettings = NetworkSettings()
    header: HeaderSettings = HeaderSettings()
    danmaku: DanmakuSettings = DanmakuSettings()
    recorder: RecorderSettings = RecorderSettings()
    postprocessing: PostprocessingSettings = PostprocessingSettings()
    space: SpaceSettings = SpaceSettings()
    email_notification: EmailNotificationSettings = EmailNotificationSettings()
    serverchan_notification: ServerchanNotificationSettings = (
        ServerchanNotificationSettings()
    )
    pushdeer_notification: PushdeerNotificationSettings = PushdeerNotificationSettings()
    pushplus_notification: PushplusNotificationSettings = PushplusNotificationSettings()
    telegram_notification: TelegramNotificationSettings = TelegramNotificationSettings()
    bark_notification: BarkNotificationSettings = BarkNotificationSettings()
    operational_notifications: OperationalNotificationSettings = (
        OperationalNotificationSettings()
    )
    webhooks: Annotated[List[WebHookSettings], Field(max_items=50)] = []

    @classmethod
    def load(cls, path: str) -> Settings:
        settings = cls.parse_obj(toml.load(path))
        settings._path = path
        return settings

    def update_from_env_settings(self, env_settings: EnvSettings) -> None:
        if (out_dir := env_settings.out_dir) is not None:
            self.output.out_dir = out_dir
        if (log_dir := env_settings.log_dir) is not None:
            self.logging.log_dir = log_dir

    def dump(self) -> None:
        assert self._path
        with open(self._path, 'wt', encoding='utf8') as file:
            toml.dump(self.dict(exclude_none=True), file)

    @validator('tasks')
    def _validate_tasks(cls, tasks: List[TaskSettings]) -> List[TaskSettings]:
        if len(tasks) > cls._MAX_TASKS:
            raise ValueError(f'Out of max tasks limits: {cls._MAX_TASKS}')
        return tasks

    @validator('webhooks')
    def _validate_webhooks(
        cls, webhooks: List[WebHookSettings]
    ) -> List[WebHookSettings]:
        if len(webhooks) >= cls._MAX_WEBHOOKS:
            raise ValueError(f'Out of max webhooks limits: {cls._MAX_WEBHOOKS}')
        return webhooks


class SettingsIn(BaseModel):
    output: Optional[OutputSettings] = None
    logging: Optional[LoggingSettings] = None
    bili_api: Optional[BiliApiSettings] = None
    bili_upload: Optional[BiliUploadSettings] = None
    live_monitor: Optional[LiveMonitorSettings] = None
    network: Optional[NetworkSettings] = None
    header: Optional[HeaderSettings] = None
    danmaku: Optional[DanmakuSettings] = None
    recorder: Optional[RecorderSettings] = None
    postprocessing: Optional[PostprocessingSettings] = None
    space: Optional[SpaceSettings] = None
    email_notification: Optional[EmailNotificationSettings] = None
    serverchan_notification: Optional[ServerchanNotificationSettings] = None
    pushdeer_notification: Optional[PushdeerNotificationSettings] = None
    pushplus_notification: Optional[PushplusNotificationSettings] = None
    telegram_notification: Optional[TelegramNotificationSettings] = None
    bark_notification: Optional[BarkNotificationSettings] = None
    operational_notifications: Optional[OperationalNotificationSettings] = None
    webhooks: Optional[List[WebHookSettings]] = None


class SettingsOut(SettingsIn):
    version: Optional[str] = None
    tasks: Optional[List[TaskSettings]] = None
