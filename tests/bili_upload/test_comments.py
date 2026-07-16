from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

import pytest
import pytest_asyncio

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.comments import (
    CommentPlanner,
    CommentPublisher,
    CommentRecord,
    PartXml,
)
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    RemoteOutcomeUnknown,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


def write_xml(path: Path, body: str) -> Path:
    path.write_text('<i>{}</i>'.format(body), encoding='utf8')
    return path


def test_comment_contains_only_sc_and_guard_sorted_by_part_and_time(
    tmp_path: Path,
) -> None:
    first = write_xml(
        tmp_path / 'first.xml',
        '<d p="1,1,25,1,1,0,hash,1">普通弹幕</d>'
        '<guard ts="200" user="用户B" giftname="舰长" count="3" />'
        '<sc ts="185" user="用户A" price="30000">留言内容</sc>',
    )
    second = write_xml(
        tmp_path / 'second.xml',
        '<sc ts="754" user="用户C" price="50000">第二段留言</sc>',
    )
    planner = CommentPlanner(max_chars=1000)

    segments = planner.render((PartXml(2, second), PartXml(1, first)))

    rendered = '\n'.join(segments)
    assert segments[0].startswith('SC 和上舰列表\n')
    assert rendered.index('1#00:03:05') < rendered.index('1#00:03:20')
    assert rendered.index('1#00:03:20') < rendered.index('2#00:12:34')
    assert '用户A发送了30元留言：留言内容' in rendered
    assert '用户B开通了3个月舰长' in rendered
    assert '普通弹幕' not in rendered
    assert all(len(segment) <= 1000 for segment in segments)


def test_no_sc_or_guard_returns_explicit_skip(tmp_path: Path) -> None:
    ordinary = write_xml(
        tmp_path / 'ordinary.xml', '<d p="1,1,25,1,1,0,hash,1">普通弹幕</d>'
    )

    assert CommentPlanner().render((PartXml(1, ordinary),)) == []


def test_comment_items_split_without_cutting_a_fitting_line() -> None:
    records = (
        CommentRecord('sc', 1, 1, 0, '甲', 1, '一' * 25, ''),
        CommentRecord('sc', 1, 2, 1, '乙', 2, '二' * 25, ''),
        CommentRecord('guard', 2, 3, 0, '丙', 0, '', '提督' * 12),
        CommentRecord('sc', 2, 4, 1, '丁', 3, '很长' * 100, ''),
    )
    planner = CommentPlanner(max_chars=80)
    fitting_line = planner.render_record(records[2])

    items = planner.create_items(records, account_uid=42, aid=303)

    assert items[0].ordinal == 0 and items[0].kind == 'root'
    assert all(item.kind == 'reply' for item in items[1:-1])
    assert items[-1].kind == 'pin'
    assert all(len(item.content) <= 80 for item in items[:-1])
    assert fitting_line in '\n'.join(item.content for item in items[:-1])
    assert any(item.content.endswith('……（内容过长已截断）') for item in items[:-1])
    assert items[-1].content == items[0].request_fingerprint
    assert all(len(item.request_fingerprint) == 64 for item in items)


async def seed_approved_job(
    database: BiliUploadDatabase,
    xml_paths: List[Optional[Path]],
    *,
    comment_state: str = 'pending',
) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        "VALUES(1,100,'100:1','closed',1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,aid,bvid,created_at,updated_at) '
        "VALUES(1,1,1,'{}','approved','confirmed',?,'disabled',303,'BVtest',1,1)",
        (comment_state,),
    )
    for index, path in enumerate(xml_paths, start=1):
        await database.execute(
            'INSERT INTO upload_parts('
            'job_id,part_index,source_path,final_path,xml_path,artifact_state,'
            'upload_state,danmaku_import_state,remote_filename,cid) '
            "VALUES(1,?,?,?,?,'ready','confirmed','disabled',?,?)",
            (
                index,
                '/rec/p{}.flv'.format(index),
                '/rec/p{}.mp4'.format(index),
                None if path is None else str(path),
                'remote-p{}'.format(index),
                1000 + index,
            ),
        )


@pytest.mark.asyncio
async def test_planner_persists_deterministic_comment_items(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(
        tmp_path / 'part.xml',
        '<sc ts="1" user="用户" price="30000">{}</sc>'
        '<guard ts="2" user="用户" giftname="舰长" />'.format('留言' * 30),
    )
    await seed_approved_job(database, [xml])
    planner = CommentPlanner(database, max_chars=70, clock=lambda: 1000)

    await planner.create(1)
    await planner.create(1)

    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'running'
    )
    rows = await database.fetchall(
        'SELECT ordinal,kind,parent_ordinal,content,state '
        'FROM comment_items ORDER BY ordinal'
    )
    assert [row['kind'] for row in rows] == ['root', 'reply', 'pin']
    assert [row['parent_ordinal'] for row in rows] == [None, 0, 0]
    assert all(row['state'] == 'prepared' for row in rows)


@pytest.mark.asyncio
async def test_planner_marks_missing_source_without_remote_work(
    database: BiliUploadDatabase,
) -> None:
    await seed_approved_job(database, [None])

    await CommentPlanner(database).create(1)

    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'skipped_source_missing'
    )
    assert await database.scalar('SELECT COUNT(*) FROM comment_items') == 0


@pytest.mark.asyncio
async def test_planner_marks_no_sc_or_guard_without_remote_work(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(
        tmp_path / 'ordinary.xml', '<d p="1,1,25,1,1,0,hash,1">普通弹幕</d>'
    )
    await seed_approved_job(database, [xml])

    await CommentPlanner(database).create(1)

    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'skipped_no_content'
    )
    assert await database.scalar('SELECT COUNT(*) FROM comment_items') == 0


class FakeProtocol:
    def __init__(self) -> None:
        self.add_reply_calls: List[Mapping[str, Any]] = []
        self.top_reply_calls: List[Mapping[str, Any]] = []
        self.list_replies_calls: List[Mapping[str, Any]] = []
        self.reply_detail_calls: List[Mapping[str, Any]] = []
        self.add_reply_results: List[Any] = []
        self.top_reply_result: Any = {'code': 0}
        self.list_replies_result: Mapping[str, Any] = {
            'code': 0,
            'data': {'replies': []},
        }
        self.reply_detail_result: Mapping[str, Any] = {
            'code': 0,
            'data': {'replies': []},
        }

    async def add_reply(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.add_reply_calls.append(dict(params))
        result = (
            self.add_reply_results.pop(0)
            if self.add_reply_results
            else {'code': 0, 'data': {'rpid': 100 + len(self.add_reply_calls)}}
        )
        if isinstance(result, BaseException):
            raise result
        return result

    async def top_reply(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.top_reply_calls.append(dict(params))
        if isinstance(self.top_reply_result, BaseException):
            raise self.top_reply_result
        return self.top_reply_result

    async def list_replies(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.list_replies_calls.append(dict(params))
        return self.list_replies_result

    async def reply_detail(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.reply_detail_calls.append(dict(params))
        return self.reply_detail_result


async def seed_comment_items(
    database: BiliUploadDatabase, items: List[Dict[str, Any]]
) -> None:
    await seed_approved_job(database, [], comment_state='running')
    for item in items:
        await database.execute(
            'INSERT INTO comment_items('
            'job_id,ordinal,kind,parent_ordinal,content,'
            'request_fingerprint,rpid,state) '
            'VALUES(1,?,?,?,?,?,?,?)',
            (
                item['ordinal'],
                item['kind'],
                item.get('parent_ordinal'),
                item['content'],
                item.get('fingerprint', 'fingerprint-{}'.format(item['ordinal'])),
                item.get('rpid'),
                item.get('state', 'prepared'),
            ),
        )


async def bundle_loader(_account_id: int) -> object:
    return object()


def publisher(database: BiliUploadDatabase, protocol: FakeProtocol) -> CommentPublisher:
    return CommentPublisher(
        database,
        protocol,
        bundle_loader=bundle_loader,
        account_gates=AccountWriteGate(database),
        worker_id='comment-test',
        clock=lambda: 1000,
    )


@pytest.mark.asyncio
async def test_publisher_sends_root_replies_then_pins_once(
    database: BiliUploadDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.comments.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    await seed_comment_items(
        database,
        [
            {'ordinal': 0, 'kind': 'root', 'content': '根评论'},
            {'ordinal': 1, 'kind': 'reply', 'parent_ordinal': 0, 'content': '楼中楼'},
            {
                'ordinal': 2,
                'kind': 'pin',
                'parent_ordinal': 0,
                'content': 'fingerprint-0',
            },
        ],
    )
    protocol = FakeProtocol()
    worker = publisher(database, protocol)

    await worker.run_once()
    await worker.run_once()
    await worker.run_once()

    assert protocol.add_reply_calls == [
        {'type': 1, 'oid': 303, 'message': '根评论', 'plat': 1},
        {
            'type': 1,
            'oid': 303,
            'message': '楼中楼',
            'plat': 1,
            'root': 101,
            'parent': 101,
        },
    ]
    assert protocol.top_reply_calls == [
        {'type': 1, 'oid': 303, 'rpid': 101, 'action': 1}
    ]
    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'completed'
    )
    confirmed = [fields for event, fields in events if event == 'comment_confirmed']
    assert [fields['kind'] for fields in confirmed] == ['root', 'reply', 'pin']
    assert all(fields['job_id'] == 1 for fields in confirmed)


@pytest.mark.asyncio
async def test_reply_timeout_reconciles_before_any_retry(
    database: BiliUploadDatabase,
) -> None:
    content = 'SC 和上舰列表\n1#00:00:01  用户发送了30元留言：内容'
    await seed_comment_items(
        database,
        [
            {'ordinal': 0, 'kind': 'root', 'content': content},
            {
                'ordinal': 1,
                'kind': 'pin',
                'parent_ordinal': 0,
                'content': 'fingerprint-0',
            },
        ],
    )
    protocol = FakeProtocol()
    protocol.add_reply_results = [RemoteOutcomeUnknown('add_reply')]
    worker = publisher(database, protocol)

    await worker.run_once()
    protocol.list_replies_result = {
        'code': 0,
        'data': {
            'replies': [
                {
                    'rpid': 501,
                    'oid': 303,
                    'mid': 42,
                    'root': 0,
                    'parent': 0,
                    'content': {'message': content},
                }
            ]
        },
    }
    await worker.run_once()

    row = await database.fetchone(
        'SELECT state,rpid FROM comment_items WHERE ordinal=0'
    )
    assert dict(row) == {'state': 'confirmed', 'rpid': 501}
    assert len(protocol.add_reply_calls) == 1
    assert len(protocol.list_replies_calls) == 1


@pytest.mark.asyncio
async def test_malformed_reconciliation_response_pauses_without_losing_lease(
    database: BiliUploadDatabase,
) -> None:
    await seed_comment_items(
        database,
        [
            {
                'ordinal': 0,
                'kind': 'root',
                'content': '根评论',
                'state': 'unknown_outcome',
            }
        ],
    )
    protocol = FakeProtocol()
    protocol.list_replies_result = {'code': 0}

    await publisher(database, protocol).run_once()

    row = await database.fetchone(
        'SELECT state,lease_owner,error_message FROM comment_items WHERE ordinal=0'
    )
    assert row['state'] == 'prepared'
    assert row['lease_owner'] is None
    assert row['error_message'] == '评论远端对账响应不符合预期'
    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'paused'
    )


@pytest.mark.asyncio
async def test_pin_failure_does_not_repost_root(database: BiliUploadDatabase) -> None:
    await seed_comment_items(
        database,
        [
            {
                'ordinal': 0,
                'kind': 'root',
                'content': '根评论',
                'state': 'confirmed',
                'rpid': 123,
            },
            {
                'ordinal': 1,
                'kind': 'pin',
                'parent_ordinal': 0,
                'content': 'fingerprint-0',
            },
        ],
    )
    protocol = FakeProtocol()
    protocol.top_reply_result = BiliApiError(code=12015)

    await publisher(database, protocol).run_once()

    assert protocol.add_reply_calls == []
    assert len(protocol.top_reply_calls) == 1
    assert (
        await database.scalar('SELECT comment_branch_state FROM upload_jobs WHERE id=1')
        == 'paused'
    )


@pytest.mark.asyncio
async def test_definitely_not_sent_comment_remains_safe_to_retry(
    database: BiliUploadDatabase,
) -> None:
    await seed_comment_items(
        database, [{'ordinal': 0, 'kind': 'root', 'content': '根评论'}]
    )
    protocol = FakeProtocol()
    protocol.add_reply_results = [DefinitelyNotSent('add_reply')]

    await publisher(database, protocol).run_once()

    row = await database.fetchone(
        'SELECT state,next_attempt_at,lease_owner FROM comment_items WHERE ordinal=0'
    )
    assert row['state'] == 'prepared'
    assert row['next_attempt_at'] > 1000
    assert row['lease_owner'] is None
