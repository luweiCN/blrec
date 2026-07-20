from __future__ import annotations

import time

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blrec.logging.audit import audit
from blrec.web.request_metrics import request_metrics_scope

_OCTET_STREAM_MEDIA_ROUTES = frozenset(
    {'/api/v1/recording-sessions/parts/{part_id}/media'}
)


class RequestPerformanceMiddleware:
    def __init__(self, app: ASGIApp, slow_request_seconds: float = 0.25) -> None:
        self._app = app
        self._slow_ms = slow_request_seconds * 1000.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self._app(scope, receive, send)
            return

        status_code = 0
        content_type = ''
        response_bytes = 0
        logged = False

        with request_metrics_scope() as metrics:
            started = time.perf_counter()

            def emit(event_status: int, *, exception: bool = False) -> None:
                nonlocal logged
                if logged:
                    return
                logged = True
                normalized_content_type = content_type.partition(';')[0].lower()
                route = scope.get('route')
                normalized_route = getattr(route, 'path', None) or '<unmatched>'
                if 200 <= event_status < 400 and (
                    normalized_content_type == 'text/event-stream'
                    or normalized_content_type.startswith('audio/')
                    or normalized_content_type.startswith('video/')
                    or (
                        normalized_content_type == 'application/octet-stream'
                        and normalized_route in _OCTET_STREAM_MEDIA_ROUTES
                    )
                ):
                    return
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if exception or event_status >= 500 or elapsed_ms >= self._slow_ms:
                    level = 'WARNING'
                elif event_status >= 400:
                    level = 'INFO'
                else:
                    level = 'DEBUG'
                audit(
                    'http_request_performance',
                    level=level,
                    method=scope.get('method', ''),
                    route=normalized_route,
                    status=event_status,
                    elapsed_ms=round(elapsed_ms, 3),
                    response_bytes=response_bytes,
                    database_calls=metrics.database_calls,
                    database_ms=round(metrics.database_ms, 3),
                )

            async def measured_send(message: Message) -> None:
                nonlocal content_type, response_bytes, status_code
                message_type = message['type']
                final_body = False
                if message_type == 'http.response.start':
                    status_code = message['status']
                    content_type = Headers(raw=message['headers']).get(
                        'content-type', ''
                    )
                elif message_type == 'http.response.body':
                    response_bytes += len(message.get('body', b''))
                    final_body = not message.get('more_body', False)

                await send(message)

                if final_body and not logged:
                    emit(status_code)

            try:
                await self._app(scope, receive, measured_send)
            except Exception:
                emit(500, exception=True)
                raise
