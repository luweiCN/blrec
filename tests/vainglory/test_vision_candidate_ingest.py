from __future__ import annotations

import json

import pytest

from blrec.vainglory.vision_candidate_ingest import VisionCandidateIngestClient


def test_ingest_rejects_non_loopback_destination() -> None:
    with pytest.raises(ValueError, match='本机 HTTP'):
        VisionCandidateIngestClient(
            'https://example.com/api/training-candidates/ingest', 'x' * 32
        )


@pytest.mark.asyncio
async def test_ingest_posts_bounded_authenticated_batches(monkeypatch) -> None:
    responses = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{}'

    def open_request(request, *, timeout):
        responses.append((request, timeout))
        return Response()

    monkeypatch.setattr('urllib.request.urlopen', open_request)
    client = VisionCandidateIngestClient(
        'http://127.0.0.1:8800/api/training-candidates/ingest', 'x' * 32
    )

    await client.ingest([{'source_id': str(index)} for index in range(205)])

    assert len(responses) == 3
    assert [
        len(json.loads(request.data)['candidates']) for request, _timeout in responses
    ] == [100, 100, 5]
    assert all(
        request.get_header('Authorization') == 'Bearer ' + 'x' * 32
        for request, _timeout in responses
    )
