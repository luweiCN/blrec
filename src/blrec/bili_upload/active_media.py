from __future__ import annotations

import asyncio
import copy
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Dict, Hashable, Mapping, Optional, Set, Tuple

import attr

from .recording_content import FlvMediaSnapshot, RecordingContentUnavailable

__all__ = ('ActiveMediaBusy', 'ActiveMediaMetadata', 'ActiveMediaService')


class ActiveMediaBusy(RuntimeError):
    def __init__(self, retry_after: int = 1) -> None:
        super().__init__('active media work capacity is exhausted')
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class ActiveMediaMetadata:
    recording_path: str
    value: object


RevisionKey = Tuple[int, str, int, Hashable, Hashable]
SourceKey = Tuple[int, str]
InFlightValue = Tuple[RevisionKey, Future[FlvMediaSnapshot]]


class ActiveMediaService:
    def __init__(self, *, max_workers: int = 2, max_waiting: int = 8) -> None:
        if max_workers <= 0 or max_waiting < 0:
            raise ValueError('active media capacity must be non-negative')
        self._max_admitted = max_workers + max_waiting
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='blrec-active-media'
        )
        self._lock = threading.RLock()
        self._futures: Set[Future[FlvMediaSnapshot]] = set()
        self._latest_by_source: Dict[SourceKey, InFlightValue] = {}
        self._closed = False

    @property
    def admitted_count(self) -> int:
        with self._lock:
            return len(self._futures)

    @property
    def in_flight_source_count(self) -> int:
        with self._lock:
            return len(self._latest_by_source)

    @property
    def completed_cache_entries(self) -> int:
        return 0

    @property
    def completed_cache_prefix_bytes(self) -> int:
        return 0

    async def snapshot(
        self, part_id: int, path: str, source_size: int, metadata: object
    ) -> FlvMediaSnapshot:
        absolute_path = os.path.abspath(path)
        source_key = (int(part_id), absolute_path)
        metadata_value, recording_path = self._unwrap_metadata(metadata)
        revision_key = (
            int(part_id),
            absolute_path,
            int(source_size),
            self._revision_value(metadata_value, 'lastkeyframelocation'),
            self._revision_value(metadata_value, 'lastkeyframetimestamp'),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError('active media service is closed')
            current = self._latest_by_source.get(source_key)
            if current is not None and current[0] == revision_key:
                future = current[1]
            else:
                if len(self._futures) >= self._max_admitted:
                    raise ActiveMediaBusy(retry_after=1)
                future = self._executor.submit(
                    self._build_snapshot,
                    absolute_path,
                    int(source_size),
                    metadata_value,
                    recording_path,
                )
                self._futures.add(future)
                self._latest_by_source[source_key] = (revision_key, future)
                future.add_done_callback(partial(self._release, source_key))
        return await asyncio.shield(asyncio.wrap_future(future))

    def close_admission(self) -> None:
        with self._lock:
            self._closed = True

    async def shutdown(self) -> None:
        self.close_admission()
        with self._lock:
            futures = tuple(self._futures)
        if futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in futures),
                return_exceptions=True,
            )
        self._executor.shutdown(wait=True)

    @classmethod
    def _build_snapshot(
        cls,
        absolute_path: str,
        source_size: int,
        metadata: object,
        recording_path: Optional[str],
    ) -> FlvMediaSnapshot:
        resolved_path = os.path.realpath(absolute_path)
        if recording_path is not None:
            resolved_recording_path = os.path.realpath(os.path.abspath(recording_path))
            if resolved_path != resolved_recording_path:
                raise RecordingContentUnavailable('录制中的视频与当前录制文件不一致')
        snapshot = FlvMediaSnapshot.create(
            resolved_path, source_size, cls._metadata_mapping(metadata)
        )
        return replace(snapshot, path=absolute_path)

    @staticmethod
    def _metadata_mapping(metadata: object) -> Mapping[str, Any]:
        if isinstance(metadata, Mapping):
            return copy.deepcopy(dict(metadata))
        if attr.has(type(metadata)):
            return attr.asdict(metadata)
        raise RecordingContentUnavailable('录制中的视频索引暂时不可用')

    @staticmethod
    def _unwrap_metadata(metadata: object) -> Tuple[object, Optional[str]]:
        if isinstance(metadata, ActiveMediaMetadata):
            return metadata.value, metadata.recording_path
        return metadata, None

    @classmethod
    def _revision_value(cls, metadata: object, key: str) -> Hashable:
        if isinstance(metadata, Mapping):
            value = metadata.get(key)
        else:
            value = getattr(metadata, key, None)
        if isinstance(value, (str, bytes, int, float, bool, type(None))):
            return value
        return ('unhashable', id(value))

    def _release(self, source_key: SourceKey, future: Future[FlvMediaSnapshot]) -> None:
        with self._lock:
            self._futures.discard(future)
            current = self._latest_by_source.get(source_key)
            if current is not None and current[1] is future:
                del self._latest_by_source[source_key]
