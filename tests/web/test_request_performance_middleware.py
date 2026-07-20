from typing import Any, Dict, List, Tuple

import pytest
from brotli_asgi import BrotliMiddleware
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from starlette.testclient import TestClient

from blrec.web.main import api
from blrec.web.middlewares.request_performance import RequestPerformanceMiddleware
from blrec.web.middlewares.security_headers import SecurityHeadersMiddleware
from blrec.web.request_metrics import record_database_call, request_metrics_scope


def test_request_metrics_accumulates_database_calls() -> None:
    with request_metrics_scope() as metrics:
        record_database_call(0.012)
        record_database_call(0.003)

    assert metrics.database_calls == 2
    assert metrics.database_ms == pytest.approx(15.0)


def test_request_metrics_ignores_calls_outside_scope_and_negative_elapsed() -> None:
    record_database_call(1.0)

    with request_metrics_scope() as metrics:
        record_database_call(-1.0)

    assert metrics.database_calls == 1
    assert metrics.database_ms == 0.0


def test_middleware_audits_normalized_route_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=0)

    @app.get('/items/{item_id}')
    async def item(item_id: int) -> Dict[str, int]:
        record_database_call(0.004)
        return {'id': item_id}

    response = TestClient(app).get('/items/7?token=must-not-appear')

    assert response.status_code == 200
    assert events[-1][0] == 'http_request_performance'
    assert events[-1][1]['level'] == 'WARNING'
    assert events[-1][1]['method'] == 'GET'
    assert events[-1][1]['route'] == '/items/{item_id}'
    assert events[-1][1]['status'] == 200
    assert events[-1][1]['response_bytes'] == len(response.content)
    assert events[-1][1]['database_calls'] == 1
    assert events[-1][1]['database_ms'] == pytest.approx(4.0)
    assert 'token' not in str(events[-1])
    assert 'must-not-appear' not in str(events[-1])


@pytest.mark.parametrize(
    ('status_code', 'expected_level'), ((404, 'INFO'), (503, 'WARNING'))
)
def test_middleware_promotes_failure_response_level(
    monkeypatch: pytest.MonkeyPatch, status_code: int, expected_level: str
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=60)

    @app.get('/failure')
    async def failure() -> Response:
        return Response(status_code=status_code)

    response = TestClient(app).get('/failure')

    assert response.status_code == status_code
    assert len(events) == 1
    assert events[0][1]['level'] == expected_level


def test_middleware_audits_unhandled_exception_once_and_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=60)
    error = RuntimeError('must-not-be-logged')

    @app.get('/explode')
    async def explode() -> Response:
        record_database_call(0.004)
        raise error

    with pytest.raises(RuntimeError) as raised:
        TestClient(app).get('/explode')

    assert raised.value is error
    assert len(events) == 1
    assert events[0][1]['level'] == 'WARNING'
    assert events[0][1]['route'] == '/explode'
    assert events[0][1]['status'] == 500
    assert events[0][1]['database_calls'] == 1
    assert 'must-not-be-logged' not in str(events[0])


@pytest.mark.parametrize('media_type', ('text/event-stream', 'audio/mpeg', 'video/mp4'))
def test_middleware_skips_streaming_response_completion_audit(
    monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=0)

    @app.get('/stream')
    async def stream() -> Response:
        return Response(b'body', media_type=media_type)

    response = TestClient(app).get('/stream')

    assert response.status_code == 200
    assert events == []


@pytest.mark.parametrize(
    ('status_code', 'expected_level'), ((404, 'INFO'), (503, 'WARNING'))
)
def test_middleware_audits_streaming_content_type_failure(
    monkeypatch: pytest.MonkeyPatch, status_code: int, expected_level: str
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=60)

    @app.get('/failed-stream')
    async def failed_stream() -> Response:
        return Response(status_code=status_code, media_type='video/mp4')

    response = TestClient(app).get('/failed-stream')

    assert response.status_code == status_code
    assert len(events) == 1
    assert events[0][1]['level'] == expected_level


def test_middleware_skips_octet_stream_on_recording_media_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=0)

    @app.get('/api/v1/recording-sessions/parts/{part_id}/media')
    async def media(part_id: int) -> StreamingResponse:
        return StreamingResponse(
            iter((str(part_id).encode(),)), media_type='application/octet-stream'
        )

    response = TestClient(app).get('/api/v1/recording-sessions/parts/7/media')

    assert response.status_code == 200
    assert response.content == b'7'
    assert events == []


def test_middleware_keeps_octet_stream_for_non_media_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=0)

    @app.get('/api/v1/export')
    async def export() -> Response:
        return Response(b'archive', media_type='application/octet-stream')

    response = TestClient(app).get('/api/v1/export')

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0][1]['route'] == '/api/v1/export'


def test_middleware_uses_fixed_route_for_unmatched_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=60)
    client = TestClient(app)

    for path in ('/missing/real-token', '/recordings/private-video.mkv'):
        response = client.get(path)
        assert response.status_code == 404

    assert [event[1]['route'] for event in events] == ['<unmatched>', '<unmatched>']
    assert 'real-token' not in str(events)
    assert 'private-video.mkv' not in str(events)


def test_main_registers_performance_inside_security_and_compression() -> None:
    middleware_classes = [middleware.cls for middleware in api.user_middleware]

    assert RequestPerformanceMiddleware in middleware_classes
    performance_index = middleware_classes.index(RequestPerformanceMiddleware)
    assert middleware_classes.index(SecurityHeadersMiddleware) < performance_index
    assert middleware_classes.index(BrotliMiddleware) < performance_index


def test_main_exposes_media_cache_and_download_headers_to_cors() -> None:
    middleware = next(
        item for item in api.user_middleware if item.cls is CORSMiddleware
    )

    assert set(middleware.options['expose_headers']) >= {
        'Accept-Ranges',
        'Content-Length',
        'Content-Range',
        'ETag',
        'Cache-Control',
        'Content-Disposition',
    }
