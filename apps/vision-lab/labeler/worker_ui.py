"""在 Vision Worker 上提供标注页，并把轻量 API 代理到 NAS 控制面。"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import hero_review

_HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


def create_worker_ui_app(server_url: str) -> FastAPI:
    upstream = server_url.rstrip('/')
    app = FastAPI(title='BLREC Vision Worker UI')

    @app.get('/api/training-review/heroes')
    def local_hero_catalog() -> dict[str, list[dict[str, str]]]:
        try:
            heroes = hero_review.hero_catalog()
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        return {
            'heroes': [
                {
                    **hero,
                    'image_url': '/api/training-review/heroes/{}/image'.format(
                        hero['label']
                    ),
                }
                for hero in heroes
            ]
        }

    @app.get('/api/training-review/heroes/{label}/image')
    def local_hero_image(label: str) -> Response:
        try:
            content = hero_review.hero_image_bytes(label)
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        if content is None:
            raise HTTPException(404, '英雄头像不存在')
        return Response(
            content=content,
            media_type='image/jpeg',
            headers={'Cache-Control': 'private, max-age=86400'},
        )

    @app.api_route(
        '/api/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']
    )
    async def proxy_api(path: str, request: Request) -> StreamingResponse:
        body = await request.body()
        upstream_request = _build_upstream_request(
            upstream,
            path=path,
            query=request.url.query,
            method=request.method,
            headers=dict(request.headers),
            body=body,
        )
        try:
            response = await asyncio.to_thread(_open_upstream, upstream_request)
        except (TimeoutError, urllib.error.URLError) as error:
            raise HTTPException(502, 'NAS 标注控制面暂时无法连接') from error
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        }
        return StreamingResponse(
            _response_chunks(response),
            status_code=int(response.status),
            headers=response_headers,
            media_type=response.headers.get_content_type(),
        )

    static_dir = Path(__file__).resolve().parent / 'static'
    app.mount('/', StaticFiles(directory=static_dir, html=True), name='static')
    return app


def _build_upstream_request(
    upstream: str,
    *,
    path: str,
    query: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
) -> urllib.request.Request:
    target = upstream.rstrip('/') + '/api/' + urllib.parse.quote(path, safe='/')
    if query:
        target += '?' + query
    forwarded_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS | {'host', 'content-length'}
    }
    return urllib.request.Request(
        target, data=body or None, headers=forwarded_headers, method=method
    )


def _open_upstream(request: urllib.request.Request):
    try:
        return urllib.request.urlopen(request, timeout=300)
    except urllib.error.HTTPError as error:
        return error


def _response_chunks(response) -> Iterator[bytes]:
    try:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                return
            yield chunk
    finally:
        response.close()
