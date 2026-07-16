from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Dict, List, Literal, Optional, Tuple

TrafficDirection = Literal['up', 'down']


@dataclass(frozen=True)
class TrafficSnapshot:
    interface_name: Optional[str]
    purpose: str
    upload_bps: float
    download_bps: float
    upload_total: int
    download_total: int


@dataclass
class _Counters:
    upload: int = 0
    download: int = 0


@dataclass
class _Sample:
    at: float
    upload: int
    download: int
    upload_bps: float = 0.0
    download_bps: float = 0.0


class TrafficMeter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._counters: Dict[Tuple[Optional[str], str], _Counters] = {}
        self._samples: Dict[Tuple[Optional[str], str], _Sample] = {}
        self._lock = RLock()

    def record(
        self,
        interface_name: Optional[str],
        purpose: str,
        direction: TrafficDirection,
        byte_count: int,
    ) -> None:
        if byte_count < 0:
            raise ValueError('byte count must not be negative')
        if byte_count == 0:
            return
        key = (interface_name, purpose)
        with self._lock:
            counters = self._counters.setdefault(key, _Counters())
            if direction == 'up':
                counters.upload += byte_count
            else:
                counters.download += byte_count

    def snapshot(self) -> List[TrafficSnapshot]:
        now = self._clock()
        result: List[TrafficSnapshot] = []
        with self._lock:
            for key, counters in sorted(
                self._counters.items(), key=lambda item: str(item[0])
            ):
                previous = self._samples.get(key)
                if previous is None:
                    sample = _Sample(now, counters.upload, counters.download)
                else:
                    elapsed = now - previous.at
                    if elapsed > 0:
                        sample = _Sample(
                            now,
                            counters.upload,
                            counters.download,
                            max(0.0, (counters.upload - previous.upload) / elapsed),
                            max(0.0, (counters.download - previous.download) / elapsed),
                        )
                    else:
                        sample = _Sample(
                            previous.at,
                            counters.upload,
                            counters.download,
                            previous.upload_bps,
                            previous.download_bps,
                        )
                self._samples[key] = sample
                result.append(
                    TrafficSnapshot(
                        interface_name=key[0],
                        purpose=key[1],
                        upload_bps=sample.upload_bps,
                        download_bps=sample.download_bps,
                        upload_total=counters.upload,
                        download_total=counters.download,
                    )
                )
        return result
