from typing import Any, Dict, List, Tuple

import pytest
from brotli_asgi import BrotliMiddleware
from fastapi import FastAPI
from fastapi.responses import Response
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


@pytest.mark.parametrize('media_type', ('text/event-stream', 'video/mp4'))
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


def test_main_registers_performance_inside_security_and_compression() -> None:
    middleware_classes = [middleware.cls for middleware in api.user_middleware]

    assert RequestPerformanceMiddleware in middleware_classes
    performance_index = middleware_classes.index(RequestPerformanceMiddleware)
    assert middleware_classes.index(SecurityHeadersMiddleware) < performance_index
    assert middleware_classes.index(BrotliMiddleware) < performance_index
