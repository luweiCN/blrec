from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit


def _secret(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if value:
        return value
    filename = os.environ.get(f'{name}_FILE', '').strip()
    return '' if not filename else Path(filename).read_text(encoding='utf8').strip()


class VisionCandidateIngestClient:
    """把新候选写入同机 NAS Vision Media；只允许 loopback，禁止外传素材。"""

    def __init__(self, url: str, token: str, *, timeout_seconds: float = 30) -> None:
        parsed = urlsplit(url.strip())
        if parsed.scheme != 'http' or parsed.hostname not in {
            '127.0.0.1',
            'localhost',
            '::1',
        }:
            raise ValueError('Vision candidate ingest URL 必须是本机 HTTP 地址')
        if parsed.path != '/api/training-candidates/ingest':
            raise ValueError('Vision candidate ingest URL 路径无效')
        if len(token) < 32:
            raise ValueError('Vision candidate ingest token 缺失或长度不足 32 位')
        self._url = url.strip()
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_environment(cls) -> Optional['VisionCandidateIngestClient']:
        url = os.environ.get('BLREC_VISION_LAB_INGEST_URL', '').strip()
        if not url:
            return None
        return cls(url, _secret('BLREC_VISION_LAB_INGEST_TOKEN'))

    async def ingest(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        for offset in range(0, len(candidates), 100):
            batch = candidates[offset : offset + 100]
            await asyncio.to_thread(self._post, batch)

    def _post(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        payload = json.dumps(
            {'schema_version': 1, 'candidates': list(candidates)},
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf8')
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            response.read()
