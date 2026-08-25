import asyncio
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from loguru import logger

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.remote_media import RemoteMediaCache
from blrec.vainglory.analyzer import (
    AnalysisCancelled,
    AnalysisStatus,
    ScannedPart,
    VideoPart,
)
from blrec.vainglory.repository import (
    AnalysisQueueCompletion,
    AnalysisQueueStatus,
    AnalysisWorkerRecord,
    ScanClaim,
    VaingloryRepository,
)
from blrec.vainglory.service import VaingloryIndexService


class Repository:
    def __init__(self, path: str = '/unused') -> None:
        self.path = path
        self.realtime_pending = False
        self.historical_paused = False
        self.requeued = []
        self.failed = []

    async def claim_next(self) -> ScanClaim:
        return ScanClaim(
            session_id=1, part=VideoPart(id=1, index=1, path=self.path), realtime=False
        )

    async def next_hero_rematch(self):
        return None

    async def discover_ready_parts(self) -> int:
        return 0

    async def has_realtime_pending(self) -> bool:
        return self.realtime_pending

    async def historical_part_paused(self, _part_id: int) -> bool:
        return self.historical_paused

    async def requeue(self, part_id: int) -> None:
        self.requeued.append(part_id)

    async def fail(self, part_id: int, error: str) -> None:
        self.failed.append((part_id, error))

    async def update_progress(self, _part_id: int, _progress: float) -> None:
        return None

    async def complete_part(self, _part_id: int, _matches: object) -> None:
        raise AssertionError('preempted analysis must not complete')


class Analyzer:
    def __init__(self) -> None:
        self.started = Event()

    def analyze_part(self, _part: VideoPart, *, progress: object, cancelled: object):
        del progress
        self.started.set()
        while not cancelled():
            time.sleep(0.002)
        raise AnalysisCancelled('preempted')


class UnusedRemoteDownloader:
    async def download(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError('the test only queues the remote download')


async def async_bundle(_account_id: int) -> object:
    return object()


def enabled_worker(worker_id: str, concurrency: int = 1) -> AnalysisWorkerRecord:
    return AnalysisWorkerRecord(
        worker_id=worker_id,
        display_name='',
        enabled=True,
        model_package_id='',
        pipeline_version='',
        concurrency=concurrency,
        first_seen_at=1,
        last_seen_at=1,
        completed_task_count=0,
        failed_task_count=0,
        total_processing_seconds=0,
        profiled_task_count=0,
        profiled_video_seconds=0,
        total_decode_analysis_seconds=0,
        total_profiled_task_seconds=0,
        last_task_finished_at=None,
    )


def test_runtime_log_deduplicates_repeated_stage_messages() -> None:
    service = VaingloryIndexService(
        Repository(),  # type: ignore[arg-type]
        analyzer=Analyzer(),  # type: ignore[arg-type]
    )

    service._record_runtime_status(
        1,
        AnalysisStatus(
            stage='coarse_scan',
            detail='分类粗扫 10% · 已采样 2 帧',
            elapsed_seconds=1,
            coarse_frames=2,
            model_package_id='vision-package-v1',
            keyframe_frames=2,
        ),
    )
    service._record_runtime_status(
        1,
        AnalysisStatus(
            stage='coarse_scan',
            detail='分类粗扫 10% · 已采样 2 帧',
            elapsed_seconds=2,
            coarse_frames=3,
        ),
    )

    assert len(service._runtime_events[1]) == 1
    assert service._runtime_status[1].coarse_frames == 3
    assert service._runtime_status[1].model_package_id == 'vision-package-v1'
    assert service._runtime_status[1].keyframe_frames == 2


@pytest.mark.asyncio
async def test_player_visibility_update_delegates_to_repository() -> None:
    player = object()

    class VisibilityRepository:
        def __init__(self) -> None:
            self.updates = []

        async def set_player_public_visibility(
            self, player_id: int, public_visible: bool
        ) -> object:
            self.updates.append((player_id, public_visible))
            return player

    repository = VisibilityRepository()
    service = VaingloryIndexService(repository)  # type: ignore[arg-type]

    assert await service.set_player_public_visibility(7, False) is player
    assert repository.updates == [(7, False)]


@pytest.mark.asyncio
async def test_remote_worker_status_tracks_multiple_nodes_and_task_owner() -> None:
    class IdleRemoteRepository:
        def __init__(self) -> None:
            self.claims = [
                ScanClaim(
                    session_id=1,
                    part=VideoPart(id=7, index=1, path='/unused'),
                    realtime=True,
                )
            ]

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            return 0

        async def discover_ready_parts(self) -> int:
            return 0

        async def register_analysis_worker(
            self,
            worker_id: str,
            *,
            model_package_id: str = '',
            pipeline_version: str = '',
            concurrency: int = 0,
        ) -> AnalysisWorkerRecord:
            return AnalysisWorkerRecord(
                worker_id=worker_id,
                display_name='',
                enabled=True,
                model_package_id=model_package_id,
                pipeline_version=pipeline_version,
                concurrency=concurrency,
                first_seen_at=1,
                last_seen_at=1,
                completed_task_count=0,
                failed_task_count=0,
                total_processing_seconds=0,
                profiled_task_count=0,
                profiled_video_seconds=0,
                total_decode_analysis_seconds=0,
                total_profiled_task_seconds=0,
                last_task_finished_at=None,
            )

        async def claim_next_match_rerun(self):
            return None

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self, *, discover: bool = True):
            del discover
            return self.claims.pop(0) if self.claims else None

        async def update_progress(self, _part_id: int, _progress: float) -> None:
            return None

    service = VaingloryIndexService(
        IdleRemoteRepository(), remote_worker_enabled=True  # type: ignore[arg-type]
    )

    first_claim = await service.claim_remote_work(
        worker_id='macbook-pro',
        model_package_id='vg-vision-v2',
        pipeline_version='timeline-v2',
    )
    second_claim = await service.claim_remote_work(
        worker_id='mac-studio',
        model_package_id='vg-vision-v2',
        pipeline_version='timeline-v2',
    )
    await service.heartbeat_remote_work(
        'part',
        7,
        0.25,
        worker_id='macbook-pro',
        model_package_id='vg-vision-v2',
        pipeline_version='timeline-v2',
    )

    assert first_claim is not None
    assert first_claim.item_id == 7
    assert second_claim is None
    status = service.analysis_worker_status
    assert status.state == 'running'
    assert status.remote_enabled is True
    assert status.worker_id == 'macbook-pro'
    assert status.model_package_id == 'vg-vision-v2'
    assert status.pipeline_version == 'timeline-v2'
    assert status.last_seen_at is not None
    assert {worker.worker_id for worker in status.workers} == {
        'macbook-pro',
        'mac-studio',
    }
    macbook = next(
        worker for worker in status.workers if worker.worker_id == 'macbook-pro'
    )
    assert macbook.active_task_count == 1
    assert macbook.active_part_ids == (7,)
    assert service.remote_worker_for('part', 7) == 'macbook-pro'


@pytest.mark.asyncio
async def test_remote_worker_registry_preserves_pause_and_records_work(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    repository = VaingloryRepository(database, clock=lambda: 1_000)
    try:
        created = await repository.add_analysis_worker(
            'mac-studio', 'Mac Studio 夜间节点'
        )
        paused = await repository.update_analysis_worker(
            created.worker_id, enabled=False
        )
        registered = await repository.register_analysis_worker(
            created.worker_id,
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
            concurrency=3,
        )

        assert paused.enabled is False
        assert registered.enabled is False
        assert registered.display_name == 'Mac Studio 夜间节点'
        assert registered.concurrency == 3
        configured = await repository.update_analysis_worker(
            created.worker_id, desired_concurrency=4
        )
        assert configured.desired_concurrency == 4

        service = VaingloryIndexService(repository, remote_worker_enabled=True)
        assert (
            await service.analysis_worker_configuration(
                worker_id=created.worker_id,
                model_package_id='vg-vision-v2',
                pipeline_version='timeline-v2',
                concurrency=3,
            )
            == 4
        )
        assert (
            await service.claim_remote_work(
                worker_id=created.worker_id,
                model_package_id='vg-vision-v2',
                pipeline_version='timeline-v2',
                concurrency=3,
            )
            is None
        )

        await repository.record_analysis_worker_task(
            created.worker_id,
            succeeded=True,
            processing_seconds=180.0,
            video_duration_seconds=3_600.0,
            decode_analysis_seconds=120.0,
        )
        worker = (await repository.list_analysis_workers())[0]
        assert worker.desired_concurrency == 4
        assert worker.completed_task_count == 1
        assert worker.failed_task_count == 0
        assert worker.total_processing_seconds == 180.0
        assert worker.profiled_task_count == 1
        assert worker.profiled_video_seconds == 3_600.0
        assert worker.total_decode_analysis_seconds == 120.0
        assert worker.total_profiled_task_seconds == 180.0
        assert worker.last_task_finished_at == 1_000
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_analysis_worker_registry_uses_bound_parameters(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    repository = VaingloryRepository(database)
    try:
        malicious_name = "'; DROP TABLE vainglory_analysis_workers; --"
        created = await repository.add_analysis_worker('safe-worker', malicious_name)

        assert created.display_name == malicious_name
        assert 'vainglory_analysis_workers' in await database.table_names()
        assert len(await repository.list_analysis_workers()) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_pause_waits_for_inflight_claim_before_returning() -> None:
    class BlockingRepository:
        def __init__(self) -> None:
            self.enabled = True
            self.claim_entered = asyncio.Event()
            self.release_claim = asyncio.Event()

        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return AnalysisWorkerRecord(
                worker_id=worker_id,
                display_name='',
                enabled=self.enabled,
                model_package_id='',
                pipeline_version='',
                concurrency=1,
                first_seen_at=1,
                last_seen_at=1,
                completed_task_count=0,
                failed_task_count=0,
                total_processing_seconds=0,
                profiled_task_count=0,
                profiled_video_seconds=0,
                total_decode_analysis_seconds=0,
                total_profiled_task_seconds=0,
                last_task_finished_at=None,
            )

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            return 0

        async def discover_ready_parts(self) -> int:
            return 0

        async def claim_next_match_rerun(self):
            self.claim_entered.set()
            await self.release_claim.wait()
            return None

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self, *, discover: bool = True):
            del discover
            return None

        async def update_analysis_worker(
            self, worker_id: str, **update: object
        ) -> AnalysisWorkerRecord:
            self.enabled = bool(update['enabled'])
            return await self.register_analysis_worker(worker_id)

        async def list_analysis_workers(self):
            return (await self.register_analysis_worker('mac-studio'),)

    repository = BlockingRepository()
    service = VaingloryIndexService(
        repository, remote_worker_enabled=True  # type: ignore[arg-type]
    )
    claim = asyncio.create_task(service.claim_remote_work(worker_id='mac-studio'))
    await repository.claim_entered.wait()
    pause = asyncio.create_task(
        service.update_analysis_worker('mac-studio', enabled=False)
    )
    await asyncio.sleep(0)

    assert pause.done() is False
    repository.release_claim.set()
    assert await claim is None
    paused = await pause
    assert paused.enabled is False
    assert await service.claim_remote_work(worker_id='mac-studio') is None


@pytest.mark.asyncio
async def test_remote_claim_stops_waiting_before_another_request_can_claim() -> None:
    class BlockingRepository:
        def __init__(self) -> None:
            self.claim_entered = asyncio.Event()
            self.release_claim = asyncio.Event()
            self.claim_calls = 0

        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return enabled_worker(worker_id)

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            return 0

        async def discover_ready_parts(self) -> int:
            return 0

        async def claim_next_match_rerun(self):
            self.claim_calls += 1
            if self.claim_calls == 1:
                self.claim_entered.set()
                await self.release_claim.wait()
            return None

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self, *, discover: bool = True):
            del discover
            return None

    repository = BlockingRepository()
    service = VaingloryIndexService(
        repository, remote_worker_enabled=True  # type: ignore[arg-type]
    )
    service._remote_claim_wait_seconds = 0.01
    first = asyncio.create_task(service.claim_remote_work(worker_id='mac-studio'))
    try:
        await repository.claim_entered.wait()

        second = asyncio.create_task(service.claim_remote_work(worker_id='macbook-pro'))
        second_result = await asyncio.wait_for(asyncio.shield(second), timeout=0.1)

        assert second_result is None
        assert repository.claim_calls == 1
    finally:
        repository.release_claim.set()
        await first


@pytest.mark.asyncio
async def test_remote_claim_throttles_queue_maintenance() -> None:
    class IdleRepository:
        def __init__(self) -> None:
            self.recovery_calls = 0
            self.discovery_calls = 0
            self.claim_discovery_flags = []

        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return enabled_worker(worker_id, concurrency=3)

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            self.recovery_calls += 1
            return 0

        async def discover_ready_parts(self) -> int:
            self.discovery_calls += 1
            return 0

        async def claim_next_match_rerun(self):
            return None

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self, *, discover: bool = True):
            self.claim_discovery_flags.append(discover)
            return None

    repository = IdleRepository()
    service = VaingloryIndexService(
        repository, remote_worker_enabled=True  # type: ignore[arg-type]
    )

    assert await service.claim_remote_work(worker_id='mac-studio') is None
    assert await service.claim_remote_work(worker_id='macbook-pro') is None

    assert repository.recovery_calls == 1
    assert repository.discovery_calls == 1
    assert repository.claim_discovery_flags == [False, False]


@pytest.mark.asyncio
async def test_video_claim_does_not_consume_image_backfill() -> None:
    class SplitRepository:
        def __init__(self) -> None:
            self.afk_claim_calls = 0
            self.part_claims = [
                ScanClaim(
                    session_id=1,
                    part=VideoPart(id=7, index=1, path='/unused'),
                    realtime=False,
                )
            ]

        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return enabled_worker(worker_id)

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            return 0

        async def discover_ready_parts(self) -> int:
            return 0

        async def claim_next_match_rerun(self):
            return None

        async def claim_next(self, *, discover: bool = True):
            assert discover is False
            return self.part_claims.pop(0) if self.part_claims else None

        async def claim_next_afk_status_backfill(self):
            self.afk_claim_calls += 1
            raise AssertionError('视频领取通道不得读取图片回填队列')

    repository = SplitRepository()
    service = VaingloryIndexService(
        repository, remote_worker_enabled=True  # type: ignore[arg-type]
    )

    claim = await service.claim_remote_work(worker_id='mac-studio', queue='video')

    assert claim is not None
    assert claim.kind == 'part'
    assert repository.afk_claim_calls == 0


@pytest.mark.asyncio
async def test_image_claim_processes_afk_while_video_queue_is_pending(
    tmp_path: Path,
) -> None:
    frame = tmp_path / 'result.png'
    frame.write_bytes(b'result-frame')

    class SplitRepository:
        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return enabled_worker(worker_id)

        async def recover_stale_remote_work(self, _timeout: int) -> int:
            return 0

        async def discover_ready_parts(self) -> int:
            return 0

        async def claim_next_afk_status_backfill(self):
            return SimpleNamespace(match_id=8, team_size=3)

        async def result_frame_path(self, _match_id: int) -> Path:
            return frame

    service = VaingloryIndexService(
        SplitRepository(), remote_worker_enabled=True  # type: ignore[arg-type]
    )

    claim = await service.claim_remote_work(worker_id='mac-studio', queue='image')

    assert claim is not None
    assert claim.kind == 'afk_status_backfill'
    assert claim.item_id == 8
    assert claim.frame_png == b'result-frame'


@pytest.mark.asyncio
async def test_remote_claim_requeues_part_when_database_exceeds_deadline() -> None:
    class SlowRepository:
        def __init__(self) -> None:
            self.requeued = []

        async def register_analysis_worker(
            self, worker_id: str, **_metadata: object
        ) -> AnalysisWorkerRecord:
            return enabled_worker(worker_id)

        async def claim_next_match_rerun(self):
            return None

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self, *, discover: bool = True):
            del discover
            await asyncio.sleep(0.02)
            return ScanClaim(
                session_id=1,
                part=VideoPart(id=7, index=1, path='/unused'),
                realtime=True,
            )

        async def requeue(self, part_id: int) -> None:
            self.requeued.append(part_id)

    repository = SlowRepository()
    service = VaingloryIndexService(
        repository, remote_worker_enabled=True  # type: ignore[arg-type]
    )
    service._remote_claim_deadline_seconds = 0.01
    service._remote_maintenance_due = float('inf')

    assert await service.claim_remote_work(worker_id='mac-studio') is None
    assert repository.requeued == [7]


@pytest.mark.asyncio
async def test_reanalysis_recovers_deleted_video_from_published_upload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        deleted_video = tmp_path / 'deleted.mp4'
        local_video = tmp_path / 'local.mp4'
        empty_video = tmp_path / 'empty.mp4'
        local_video.write_bytes(b'video')
        empty_video.write_bytes(b'')
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
            "VALUES(1,100,'session:1','closed',1,'历史稿件','主播')"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('run:1',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,record_start_time,'
            'artifact_state,video_deleted_at,created_at,updated_at) '
            "VALUES(1,1,'run:1',1,?,1,'missing',50,1,50)",
            (str(deleted_video),),
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,record_start_time,'
            'artifact_state,created_at,updated_at) '
            "VALUES(2,1,'run:1',2,?,1,'ready',1,1)",
            (str(local_video),),
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,record_start_time,'
            'artifact_state,created_at,updated_at) '
            "VALUES(3,1,'run:1',3,?,1,'ready',1,1)",
            (str(empty_video),),
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'aid,bvid,created_at,updated_at) '
            "VALUES(1,1,1,'{}','approved','confirmed',303,'BV1abcdefgh',1,1)"
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'job_id,part_index,source_path,artifact_state,upload_state,'
            'remote_filename,cid) '
            "VALUES(1,1,?,'ready','confirmed','remote-p1',401)",
            (str(deleted_video),),
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'job_id,part_index,source_path,artifact_state,upload_state,'
            'remote_filename,cid) '
            "VALUES(1,2,?,'ready','confirmed','remote-p2',402)",
            (str(local_video),),
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'job_id,part_index,source_path,artifact_state,upload_state,'
            'remote_filename,cid) '
            "VALUES(1,3,?,'ready','confirmed','remote-p3',403)",
            (str(empty_video),),
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        remote_media = RemoteMediaCache(
            database,
            tmp_path / 'recordings',
            bundle_loader=async_bundle,
            downloader=UnusedRemoteDownloader(),
            clock=lambda: 100,
        )
        service = VaingloryIndexService(repository, remote_media_cache=remote_media)

        job = await service.request_scan(1)

        assert job.state == 'pending'
        source = await database.fetchone(
            'SELECT state,bvid,cid,page FROM vainglory_video_sources ' 'WHERE part_id=1'
        )
        assert source is not None
        assert (
            str(source['state']),
            str(source['bvid']),
            int(source['cid']),
            int(source['page']),
        ) == ('pending', 'BV1abcdefgh', 401, 1)
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM vainglory_video_sources WHERE part_id=2'
            )
            == 0
        )
        empty_source = await database.fetchone(
            'SELECT state,bvid,cid,page FROM vainglory_video_sources WHERE part_id=3'
        )
        assert empty_source is not None
        assert (
            str(empty_source['state']),
            str(empty_source['bvid']),
            int(empty_source['cid']),
            int(empty_source['page']),
        ) == ('pending', 'BV1abcdefgh', 403, 3)
        part_job = await database.fetchone(
            'SELECT state,request_kind,algorithm_version FROM vainglory_part_jobs '
            'WHERE part_id=1'
        )
        assert part_job is not None
        assert (
            str(part_job['state']),
            str(part_job['request_kind']),
            int(part_job['algorithm_version']),
        ) == ('pending', 'manual', repository.ALGORITHM_VERSION)
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM vainglory_part_jobs WHERE session_id=1 "
                "AND state='pending' AND request_kind='manual' "
                'AND algorithm_version=?',
                (repository.ALGORITHM_VERSION,),
            )
            == 3
        )
        part_states = await database.fetchall(
            'SELECT id,artifact_state FROM recording_parts ORDER BY id'
        )
        assert [
            (int(row['id']), str(row['artifact_state'])) for row in part_states
        ] == [(1, 'missing'), (2, 'ready'), (3, 'missing')]
        claim = await repository.claim_next()
        assert claim is not None
        assert claim.part.id == 2
        await repository.complete_part(2, ())
        assert await repository.claim_next() is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_background_analysis_yields_when_a_live_part_arrives(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    repository = Repository(str(video))
    analyzer = Analyzer()
    service = VaingloryIndexService(
        repository,  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        realtime_poll_seconds=0.01,
    )

    running = asyncio.create_task(service.run_once())
    while not analyzer.started.is_set():
        await asyncio.sleep(0)
    repository.realtime_pending = True
    assert await asyncio.wait_for(running, timeout=1) is True

    assert repository.requeued == [1]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_historical_analysis_yields_when_its_sync_is_paused(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    repository = Repository(str(video))
    analyzer = Analyzer()
    service = VaingloryIndexService(
        repository,  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        realtime_poll_seconds=0.01,
    )

    running = asyncio.create_task(service.run_once())
    while not analyzer.started.is_set():
        await asyncio.sleep(0)
    repository.historical_paused = True
    assert await asyncio.wait_for(running, timeout=1) is True

    assert repository.requeued == [1]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_scan_task_log_includes_part_and_recording_durations(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')

    class ScanRepository:
        def __init__(self) -> None:
            self.completed = []
            self.candidate_count = 0

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self) -> ScanClaim:
            return ScanClaim(
                session_id=7,
                part=VideoPart(id=11, index=2, path=str(video)),
                realtime=True,
                part_duration_seconds=7_200,
                recording_duration_seconds=10_800,
            )

        async def update_progress(self, _part_id: int, _progress: float) -> None:
            return None

        async def complete_part(
            self, part_id: int, matches: object, *, candidate_count: int = 0
        ) -> None:
            self.candidate_count = candidate_count
            self.completed.append((part_id, matches))

        async def analysis_queue_status(
            self, *, limit: int = 8, offset: int = 0
        ) -> AnalysisQueueStatus:
            assert (limit, offset) == (8, 0)
            recent_completions = (
                ()
                if not self.completed
                else (
                    AnalysisQueueCompletion(
                        completed_at=int(time.time()),
                        session_id=7,
                        part_id=11,
                        part_index=2,
                        title='测试直播',
                        part_duration_seconds=7_200,
                        recording_duration_seconds=10_800,
                        part_match_duration_seconds=1_800,
                        session_match_duration_seconds=2_700,
                        candidate_count=self.candidate_count,
                        match_count=0,
                        elapsed_seconds=0,
                    ),
                )
            )
            return AnalysisQueueStatus(
                active=(),
                queued=(),
                pending_count=0,
                manual_pending=0,
                realtime_pending=0,
                archive_pending=0,
                migration_pending=0,
                backlog_pending=0,
                recent_completions=recent_completions,
            )

    class ScanAnalyzer:
        def scan_part(
            self,
            _part: VideoPart,
            *,
            progress: object,
            status_callback: object,
            cancelled: object,
        ):
            del status_callback, cancelled
            progress(1.0)
            return ScannedPart(7_200_000, ())

    repository = ScanRepository()
    service = VaingloryIndexService(
        repository, analyzer=ScanAnalyzer()  # type: ignore[arg-type]
    )
    messages = []
    sink = logger.add(messages.append, format='{message}')
    try:
        assert await service._scan_once() is True
    finally:
        logger.remove(sink)

    message = ''.join(str(item) for item in messages)
    queue = await service.analysis_queue_status()
    assert repository.completed == [(11, ())]
    assert len(queue.recent_completions) == 1
    assert queue.recent_completions[0].part_id == 11
    assert queue.recent_completions[0].part_duration_seconds == 7_200
    assert queue.recent_completions[0].recording_duration_seconds == 10_800
    assert queue.recent_completions[0].part_match_duration_seconds == 1_800
    assert queue.recent_completions[0].session_match_duration_seconds == 2_700
    assert queue.recent_completions[0].candidate_count == 0
    assert queue.recent_completions[0].match_count == 0
    assert queue.recent_completions[0].elapsed_seconds >= 0
    assert 'Vainglory part analysis task started: session_id=7 part_id=11' in message
    assert 'part_duration_seconds=7200' in message
    assert 'recording_duration_seconds=10800' in message
    assert 'Vainglory part analysis task completed: session_id=7 part_id=11' in message
    assert 'matches=0' in message
