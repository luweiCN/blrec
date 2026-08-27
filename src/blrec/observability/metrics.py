from __future__ import annotations

import time
from typing import Any, Iterable, Optional, Sequence, Tuple, cast

from loguru import logger
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from blrec import __version__

_TASK_STATES = ('stopped', 'waiting', 'recording', 'remuxing', 'injecting')
_RECORDING_STATES = ('open', 'closed', 'cancelled', 'manual_review', 'skipped')
_RECORDING_SOURCE_KINDS = ('live', 'highlight')
_PART_STATES = (
    'recording',
    'postprocessing',
    'ready',
    'failed',
    'missing',
    'manual_review',
)
_MEDIA_INDEX_STATES = ('pending', 'indexing', 'ready', 'failed')
_UPLOAD_STATES = (
    'waiting_artifacts',
    'ready',
    'uploading',
    'submitting',
    'waiting_review',
    'approved',
    'rejected',
    'paused',
    'completed',
)
_ACCOUNT_STATES = ('active', 'paused', 'refresh_unknown', 'archived')
_ANALYSIS_STATES = ('pending', 'analyzing', 'ready', 'failed')
_ARCHIVE_IMPORT_STATES = (
    'queued',
    'downloading',
    'analyzing',
    'ready',
    'failed',
    'skipped',
)
_ARCHIVE_PART_STATES = ('queued', 'downloading', 'analyzing', 'ready', 'failed')
_VIDEO_SOURCE_STATES = ('missing', 'pending', 'downloading', 'ready', 'failed')
_GAME_MODES = ('3v3', '5v5', 'aram', 'other', 'unknown')
_BUSINESS_EVENTS = (
    'recording_sessions_started',
    'recording_parts_ready',
    'videos_downloaded',
    'videos_processed',
    'videos_analyzed',
    'matches_created',
    'uploads_completed',
)


def _status_class(status: Optional[int]) -> str:
    if status is None:
        return 'transport_error'
    if status < 100 or status >= 600:
        return 'unknown'
    return '{}xx'.format(status // 100)


def _normalized_status(status: object) -> Optional[int]:
    if status is None:
        return None
    try:
        return int(cast(Any, status))
    except (TypeError, ValueError):
        return None


def _request_result(status: Optional[int]) -> str:
    if status is None:
        return 'transport_error'
    return 'success' if 200 <= status < 400 else 'http_error'


def _label_value(value: object) -> str:
    return 'unknown' if value is None else str(value)


def _local_day_start(now: int) -> int:
    local = time.localtime(now)
    return int(
        time.mktime(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                0,
                0,
                0,
                local.tm_wday,
                local.tm_yday,
                local.tm_isdst,
            )
        )
    )


def _set_grouped_counts(
    metric: Gauge,
    rows: Iterable[Any],
    label_names: Sequence[str],
    defaults: Iterable[Tuple[str, ...]] = (),
) -> None:
    metric.clear()
    for values in defaults:
        metric.labels(*values).set(0)
    for row in rows:
        values = tuple(_label_value(row[name]) for name in label_names)
        metric.labels(*values).set(float(row['sample_count'] or 0))


def _set_count(metric: Gauge, value: object) -> None:
    metric.set(float(cast(Any, value) or 0))


class BlrecMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.http_requests = Counter(
            'blrec_http_requests_total',
            'HTTP requests handled by the BLREC web application.',
            ('method', 'route', 'status_class'),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            'blrec_http_request_duration_seconds',
            'BLREC HTTP request duration in seconds.',
            ('method', 'route'),
            registry=self.registry,
        )
        self.http_response_bytes = Counter(
            'blrec_http_response_bytes_total',
            'HTTP response bytes emitted by the BLREC web application.',
            ('method', 'route'),
            registry=self.registry,
        )
        self.outbound_requests = Counter(
            'blrec_outbound_requests_total',
            'Outbound requests issued by BLREC.',
            ('purpose', 'method', 'status_class', 'result'),
            registry=self.registry,
        )
        self.outbound_request_duration = Histogram(
            'blrec_outbound_request_duration_seconds',
            'BLREC outbound request duration in seconds.',
            ('purpose', 'method'),
            registry=self.registry,
        )
        self.metrics_refresh_failures = Counter(
            'blrec_metrics_refresh_failures_total',
            'Failures while refreshing database-backed BLREC metrics.',
            registry=self.registry,
        )

        self.application_info = Gauge(
            'blrec_application_info',
            'BLREC application information. The value is always one.',
            ('version',),
            registry=self.registry,
        )
        self.application_ready = Gauge(
            'blrec_application_ready',
            'Whether the BLREC application has completed startup.',
            registry=self.registry,
        )
        self.metrics_refresh_success = Gauge(
            'blrec_metrics_refresh_success',
            'Whether the latest BLREC metrics refresh succeeded.',
            registry=self.registry,
        )
        self.database_refresh_success = Gauge(
            'blrec_database_metrics_refresh_success',
            'Whether the latest database metrics refresh succeeded.',
            registry=self.registry,
        )
        self.tasks = Gauge(
            'blrec_tasks',
            'Configured BLREC recording tasks by current runtime state.',
            ('state',),
            registry=self.registry,
        )
        self.active_recordings = Gauge(
            'blrec_active_recordings',
            'Number of recording tasks currently recording.',
            registry=self.registry,
        )
        self.task_bytes_per_second = Gauge(
            'blrec_task_bytes_per_second',
            'Current aggregate task byte rate.',
            ('direction',),
            registry=self.registry,
        )
        self.task_danmaku_per_minute = Gauge(
            'blrec_task_danmaku_per_minute',
            'Current aggregate danmaku rate.',
            registry=self.registry,
        )
        self.recording_sessions = Gauge(
            'blrec_recording_sessions',
            'Recording sessions in the BLREC database.',
            ('source_kind', 'state'),
            registry=self.registry,
        )
        self.recording_parts = Gauge(
            'blrec_recording_parts',
            'Recording parts in the BLREC database by artifact state.',
            ('artifact_state',),
            registry=self.registry,
        )
        self.media_index_parts = Gauge(
            'blrec_media_index_parts',
            'Recording parts in the media index pipeline by state.',
            ('state',),
            registry=self.registry,
        )
        self.upload_jobs = Gauge(
            'blrec_upload_jobs',
            'Upload jobs in the BLREC database by state.',
            ('state',),
            registry=self.registry,
        )
        self.accounts = Gauge(
            'blrec_accounts',
            'Bilibili accounts in the BLREC database by state.',
            ('state',),
            registry=self.registry,
        )
        self.analysis_jobs = Gauge(
            'blrec_analysis_jobs',
            'Vainglory analysis jobs in the BLREC database.',
            ('kind', 'state'),
            registry=self.registry,
        )
        self.matches = Gauge(
            'blrec_matches',
            'Vainglory matches in the BLREC database.',
            ('analysis_state', 'game_mode'),
            registry=self.registry,
        )
        self.archive_imports = Gauge(
            'blrec_archive_imports',
            'Archive imports in the BLREC database by state.',
            ('state',),
            registry=self.registry,
        )
        self.archive_parts = Gauge(
            'blrec_archive_parts',
            'Archive parts in the BLREC database by state.',
            ('state',),
            registry=self.registry,
        )
        self.video_sources = Gauge(
            'blrec_video_sources',
            'Cached video sources in the BLREC database.',
            ('origin', 'state'),
            registry=self.registry,
        )
        self.business_totals = Gauge(
            'blrec_business_total',
            'Current cumulative counts for BLREC business entities.',
            ('entity',),
            registry=self.registry,
        )
        self.business_today = Gauge(
            'blrec_business_today',
            'Counts of BLREC business events since local midnight.',
            ('event',),
            registry=self.registry,
        )
        self.network_bytes = Gauge(
            'blrec_network_bytes',
            'Bytes observed by BLREC network routing.',
            ('interface', 'purpose', 'direction'),
            registry=self.registry,
        )
        self.network_bytes_per_second = Gauge(
            'blrec_network_bytes_per_second',
            'Current bytes per second observed by BLREC network routing.',
            ('interface', 'purpose', 'direction'),
            registry=self.registry,
        )
        self.network_interface_enabled = Gauge(
            'blrec_network_interface_enabled',
            'Whether a discovered network interface is enabled for BLREC.',
            ('interface',),
            registry=self.registry,
        )
        self.network_probe_reachable = Gauge(
            'blrec_network_probe_reachable',
            'Whether the last network probe reached its destination.',
            ('interface',),
            registry=self.registry,
        )
        self.network_probe_latency_seconds = Gauge(
            'blrec_network_probe_latency_seconds',
            'Latency from the last network probe.',
            ('interface',),
            registry=self.registry,
        )

    def record_http_request(
        self,
        method: str,
        route: str,
        status: int,
        elapsed_seconds: float,
        response_bytes: int,
    ) -> None:
        normalized_method = str(method or 'UNKNOWN').upper()
        normalized_route = str(route or '<unmatched>')
        self.http_requests.labels(
            normalized_method, normalized_route, _status_class(status)
        ).inc()
        self.http_request_duration.labels(normalized_method, normalized_route).observe(
            max(0.0, elapsed_seconds)
        )
        if response_bytes > 0:
            self.http_response_bytes.labels(normalized_method, normalized_route).inc(
                response_bytes
            )

    def record_outbound_request(
        self, purpose: str, method: str, status: Optional[int], elapsed_seconds: float
    ) -> None:
        normalized_method = str(method or 'UNKNOWN').upper()
        normalized_purpose = str(purpose or 'unknown')
        normalized_status = _normalized_status(status)
        self.outbound_requests.labels(
            normalized_purpose,
            normalized_method,
            _status_class(normalized_status),
            _request_result(normalized_status),
        ).inc()
        self.outbound_request_duration.labels(
            normalized_purpose, normalized_method
        ).observe(max(0.0, elapsed_seconds))

    async def refresh(
        self,
        *,
        application: Optional[Any],
        database: Optional[Any],
        network_manager: Optional[Any],
        application_ready: bool,
    ) -> None:
        self.application_ready.set(1 if application_ready else 0)
        try:
            self._refresh_application(application, application_ready)
            self._refresh_network(network_manager)
        except Exception as error:
            self.metrics_refresh_success.set(0)
            self.metrics_refresh_failures.inc()
            logger.warning(
                'Failed to refresh Prometheus runtime metrics: {}', type(error).__name__
            )
            return
        if database is None or not application_ready:
            self.database_refresh_success.set(0)
            self.metrics_refresh_success.set(0)
            return

        try:
            await self._refresh_database(database, _local_day_start(int(time.time())))
        except Exception as error:
            self.database_refresh_success.set(0)
            self.metrics_refresh_success.set(0)
            self.metrics_refresh_failures.inc()
            logger.warning(
                'Failed to refresh Prometheus business metrics: {}',
                type(error).__name__,
            )
            return
        self.database_refresh_success.set(1)
        self.metrics_refresh_success.set(1)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def _refresh_application(self, application: Optional[Any], ready: bool) -> None:
        self.application_info.clear()
        version = __version__
        if application is not None:
            try:
                version = str(application.info.version)
            except (AttributeError, RuntimeError):
                pass
        self.application_info.labels(version).set(1)

        self.tasks.clear()
        for state in _TASK_STATES:
            self.tasks.labels(state).set(0)
        self.active_recordings.set(0)
        self.task_bytes_per_second.labels('download').set(0)
        self.task_bytes_per_second.labels('recording').set(0)
        self.task_danmaku_per_minute.set(0)
        if not ready or application is None:
            return
        try:
            task_data = tuple(application.get_all_task_data())
        except (AttributeError, RuntimeError):
            return
        counts = dict((state, 0) for state in _TASK_STATES)
        active = 0
        download_rate = 0.0
        recording_rate = 0.0
        danmaku_rate = 0.0
        for data in task_data:
            status = data.task_status
            state = getattr(status.running_status, 'value', status.running_status)
            normalized_state = _label_value(state)
            counts[normalized_state] = counts.get(normalized_state, 0) + 1
            if normalized_state == 'recording':
                active += 1
            download_rate += max(0.0, float(status.dl_rate))
            recording_rate += max(0.0, float(status.rec_rate))
            danmaku_rate += max(0.0, float(status.danmu_rate))
        for state, count in counts.items():
            self.tasks.labels(state).set(count)
        self.active_recordings.set(active)
        self.task_bytes_per_second.labels('download').set(download_rate)
        self.task_bytes_per_second.labels('recording').set(recording_rate)
        self.task_danmaku_per_minute.set(danmaku_rate)

    def _refresh_network(self, network_manager: Optional[Any]) -> None:
        self.network_bytes.clear()
        self.network_bytes_per_second.clear()
        self.network_interface_enabled.clear()
        self.network_probe_reachable.clear()
        self.network_probe_latency_seconds.clear()
        if network_manager is None:
            return
        try:
            for snapshot in network_manager.traffic_meter.snapshot():
                interface = snapshot.interface_name or 'system'
                self.network_bytes.labels(interface, snapshot.purpose, 'up').set(
                    snapshot.upload_total
                )
                self.network_bytes.labels(interface, snapshot.purpose, 'down').set(
                    snapshot.download_total
                )
                self.network_bytes_per_second.labels(
                    interface, snapshot.purpose, 'up'
                ).set(snapshot.upload_bps)
                self.network_bytes_per_second.labels(
                    interface, snapshot.purpose, 'down'
                ).set(snapshot.download_bps)
            for name, interface in network_manager.interfaces().items():
                self.network_interface_enabled.labels(name).set(
                    1 if interface.enabled else 0
                )
            for name, probe in network_manager.cached_probes().items():
                self.network_probe_reachable.labels(name).set(
                    1 if probe.reachable else 0
                )
                self.network_probe_latency_seconds.labels(name).set(
                    0 if probe.latency_ms is None else probe.latency_ms / 1000.0
                )
        except (AttributeError, RuntimeError):
            return

    async def _refresh_database(self, database: Any, day_start: int) -> None:
        rows = await database.fetchall(
            'SELECT COALESCE(source_kind,\'unknown\') AS source_kind,state,'
            'COUNT(*) AS sample_count FROM recording_sessions '
            'GROUP BY source_kind,state'
        )
        _set_grouped_counts(
            self.recording_sessions,
            rows,
            ('source_kind', 'state'),
            (
                (source_kind, state)
                for source_kind in _RECORDING_SOURCE_KINDS
                for state in _RECORDING_STATES
            ),
        )

        rows = await database.fetchall(
            'SELECT artifact_state,COUNT(*) AS sample_count '
            'FROM recording_parts GROUP BY artifact_state'
        )
        _set_grouped_counts(
            self.recording_parts,
            rows,
            ('artifact_state',),
            ((state,) for state in _PART_STATES),
        )

        rows = await database.fetchall(
            'SELECT media_index_state AS state,COUNT(*) AS sample_count '
            'FROM recording_parts GROUP BY media_index_state'
        )
        _set_grouped_counts(
            self.media_index_parts,
            rows,
            ('state',),
            ((state,) for state in _MEDIA_INDEX_STATES),
        )

        rows = await database.fetchall(
            'SELECT state,COUNT(*) AS sample_count FROM upload_jobs GROUP BY state'
        )
        _set_grouped_counts(
            self.upload_jobs, rows, ('state',), ((state,) for state in _UPLOAD_STATES)
        )

        rows = await database.fetchall(
            'SELECT state,COUNT(*) AS sample_count FROM bili_accounts GROUP BY state'
        )
        _set_grouped_counts(
            self.accounts, rows, ('state',), ((state,) for state in _ACCOUNT_STATES)
        )

        rows = await database.fetchall(
            "SELECT 'session' AS kind,state,COUNT(*) AS sample_count "
            'FROM vainglory_scan_jobs GROUP BY state '
            'UNION ALL '
            "SELECT 'part' AS kind,state,COUNT(*) AS sample_count "
            'FROM vainglory_part_jobs GROUP BY state'
        )
        _set_grouped_counts(
            self.analysis_jobs,
            rows,
            ('kind', 'state'),
            (
                (kind, state)
                for kind in ('session', 'part')
                for state in _ANALYSIS_STATES
            ),
        )

        rows = await database.fetchall(
            'SELECT COALESCE(analysis_state,\'final\') AS analysis_state,'
            'COALESCE(game_mode,\'unknown\') AS game_mode,COUNT(*) AS sample_count '
            'FROM vainglory_matches GROUP BY analysis_state,game_mode'
        )
        _set_grouped_counts(
            self.matches,
            rows,
            ('analysis_state', 'game_mode'),
            (
                (analysis_state, game_mode)
                for analysis_state in ('provisional', 'final')
                for game_mode in _GAME_MODES
            ),
        )

        rows = await database.fetchall(
            'SELECT state,COUNT(*) AS sample_count '
            'FROM vainglory_archive_imports GROUP BY state'
        )
        _set_grouped_counts(
            self.archive_imports,
            rows,
            ('state',),
            ((state,) for state in _ARCHIVE_IMPORT_STATES),
        )

        rows = await database.fetchall(
            'SELECT state,COUNT(*) AS sample_count '
            'FROM vainglory_archive_parts GROUP BY state'
        )
        _set_grouped_counts(
            self.archive_parts,
            rows,
            ('state',),
            ((state,) for state in _ARCHIVE_PART_STATES),
        )

        rows = await database.fetchall(
            'SELECT origin,state,COUNT(*) AS sample_count '
            'FROM vainglory_video_sources GROUP BY origin,state'
        )
        _set_grouped_counts(
            self.video_sources,
            rows,
            ('origin', 'state'),
            (
                (origin, state)
                for origin in ('upload', 'archive')
                for state in _VIDEO_SOURCE_STATES
            ),
        )

        total_queries = (
            ('recording_sessions', 'SELECT COUNT(*) FROM recording_sessions'),
            ('recording_parts', 'SELECT COUNT(*) FROM recording_parts'),
            ('upload_jobs', 'SELECT COUNT(*) FROM upload_jobs'),
            ('archive_imports', 'SELECT COUNT(*) FROM vainglory_archive_imports'),
            ('archive_video_sources', 'SELECT COUNT(*) FROM vainglory_video_sources'),
            (
                'analyzed_videos',
                "SELECT COUNT(*) FROM vainglory_part_jobs WHERE state='ready'",
            ),
            ('matches', 'SELECT COUNT(*) FROM vainglory_matches'),
            (
                'published_uploads',
                "SELECT COUNT(*) FROM upload_jobs WHERE state='completed'",
            ),
        )
        self.business_totals.clear()
        for entity, sql in total_queries:
            self.business_totals.labels(entity).set(
                float(await database.scalar(sql) or 0)
            )

        today_queries = (
            (
                'recording_sessions_started',
                'SELECT COUNT(*) FROM recording_sessions WHERE started_at>=?',
            ),
            (
                'recording_parts_ready',
                'SELECT COUNT(*) FROM recording_parts '
                "WHERE artifact_state='ready' AND "
                'COALESCE(postprocessed_at,updated_at)>=?',
            ),
            (
                'videos_downloaded',
                'SELECT COUNT(*) FROM vainglory_video_sources '
                "WHERE origin='archive' AND state='ready' AND "
                'COALESCE(cached_at,updated_at)>=?',
            ),
            (
                'videos_processed',
                'SELECT COUNT(*) FROM recording_parts '
                "WHERE media_index_state='ready' AND "
                'COALESCE(media_index_updated_at,updated_at)>=?',
            ),
            (
                'videos_analyzed',
                'SELECT COUNT(*) FROM vainglory_part_jobs '
                "WHERE state='ready' AND completed_at>=?",
            ),
            (
                'matches_created',
                'SELECT COUNT(*) FROM vainglory_matches WHERE created_at>=?',
            ),
            (
                'uploads_completed',
                'SELECT COUNT(*) FROM upload_jobs '
                "WHERE state='completed' AND "
                'COALESCE(upload_completed_at,updated_at)>=?',
            ),
        )
        self.business_today.clear()
        today_event_names = {name for name, _sql in today_queries}
        for event, sql in today_queries:
            self.business_today.labels(event).set(
                float(await database.scalar(sql, (day_start,)) or 0)
            )
        for event in _BUSINESS_EVENTS:
            if event not in today_event_names:
                self.business_today.labels(event).set(0)


metrics = BlrecMetrics()


def record_http_request(
    method: str, route: str, status: int, elapsed_seconds: float, response_bytes: int
) -> None:
    metrics.record_http_request(method, route, status, elapsed_seconds, response_bytes)


def record_outbound_request(
    purpose: str, method: str, status: Optional[int], elapsed_seconds: float
) -> None:
    metrics.record_outbound_request(purpose, method, status, elapsed_seconds)
