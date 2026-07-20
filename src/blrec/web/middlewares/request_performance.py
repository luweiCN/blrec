from __future__ import annotations

import time

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blrec.logging.audit import audit
from blrec.web.request_metrics import request_metrics_scope


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

            async def measured_send(message: Message) -> None:
                nonlocal content_type, logged, response_bytes, status_code
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
                    logged = True
                    normalized_content_type = content_type.partition(';')[0].lower()
                    if normalized_content_type == 'text/event-stream' or (
                        normalized_content_type.startswith('audio/')
                        or normalized_content_type.startswith('video/')
                    ):
                        return
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    route = scope.get('route')
                    normalized_route = getattr(route, 'path', None) or scope.get(
                        'path', ''
                    )
                    audit(
                        'http_request_performance',
                        level=('WARNING' if elapsed_ms >= self._slow_ms else 'DEBUG'),
                        method=scope.get('method', ''),
                        route=normalized_route,
                        status=status_code,
                        elapsed_ms=round(elapsed_ms, 3),
                        response_bytes=response_bytes,
                        database_calls=metrics.database_calls,
                        database_ms=round(metrics.database_ms, 3),
                    )

            await self._app(scope, receive, measured_send)
