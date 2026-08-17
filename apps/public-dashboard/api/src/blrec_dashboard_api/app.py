from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import suppress
from functools import partial
from threading import Lock
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .assets import IdempotencyConflict, apply_asset_batch
from .database import database_session, initialize_database
from .direct import DirectDashboardRepository
from .models import AssetBatch
from .realtime import DashboardRealtimeBroker, event_response
from .settings import ApiSettings

LOGGER = logging.getLogger(__name__)
_IDEMPOTENCY_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')


class _DashboardResponseCache:
    def __init__(self) -> None:
        self._lock = Lock()
        self._payload: Optional[bytes] = None
        self._revision: Optional[str] = None

    def replace(self, current: Tuple[Mapping[str, Any], str]) -> None:
        document, revision = current
        payload = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(',', ':')
        ).encode('utf-8')
        with self._lock:
            self._payload = payload
            self._revision = revision

    def current(self) -> Tuple[bytes, str]:
        with self._lock:
            if self._payload is None or self._revision is None:
                raise RuntimeError('dashboard response cache is empty')
            return self._payload, self._revision


def _authenticate_write(authorization: Optional[str], settings: ApiSettings) -> None:
    scheme, separator, token = (authorization or '').partition(' ')
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    if (
        separator != ' '
        or scheme.casefold() != 'bearer'
        or not token
        or not hmac.compare_digest(digest, settings.ingest_token_sha256)
    ):
        raise HTTPException(
            status_code=401,
            detail='invalid asset credentials',
            headers={'WWW-Authenticate': 'Bearer'},
        )


def _owner_view(authorization: Optional[str], settings: ApiSettings) -> bool:
    if authorization is None:
        return False
    scheme, separator, token = authorization.partition(' ')
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    if (
        not settings.owner_token_sha256
        or separator != ' '
        or scheme.casefold() != 'bearer'
        or not token
        or not hmac.compare_digest(digest, settings.owner_token_sha256)
    ):
        raise HTTPException(
            status_code=401,
            detail='invalid owner credentials',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    return True


def _set_view_headers(response: Response, owner_view: bool) -> None:
    response.headers['Vary'] = 'Authorization'
    response.headers['Cache-Control'] = (
        'private, no-store' if owner_view else 'public, no-cache'
    )


def _hero_filters(value: str) -> tuple[str, ...]:
    heroes = tuple(
        dict.fromkeys(hero.strip() for hero in value.split(',') if hero.strip())
    )
    if len(heroes) > 10 or any(len(hero) > 80 for hero in heroes):
        raise HTTPException(status_code=422, detail='invalid hero filters')
    return heroes


def _etag_matches(if_none_match: Optional[str], revision: str) -> bool:
    expected = '"{}"'.format(revision)
    for candidate in (if_none_match or '').split(','):
        normalized = candidate.strip()
        if normalized == '*':
            return True
        if normalized.startswith('W/'):
            normalized = normalized[2:].lstrip()
        if normalized == expected:
            return True
    return False


def create_app(
    settings: Optional[ApiSettings] = None,
    *,
    repository: Optional[DirectDashboardRepository] = None,
) -> FastAPI:
    active_settings = settings or ApiSettings.from_environment()
    auxiliary_target = active_settings.database_target
    initialize_database(auxiliary_target)
    active_repository = repository or DirectDashboardRepository(
        source_target=active_settings.source_database_target,
        auxiliary_target=auxiliary_target,
    )
    if repository is None:
        active_repository.refresh(force=True)
    public_dashboard_cache = _DashboardResponseCache()
    owner_dashboard_cache = _DashboardResponseCache()
    public_dashboard_cache.replace(active_repository.dashboard_document())
    owner_dashboard_cache.replace(active_repository.dashboard_document(owner_view=True))
    app = FastAPI(
        title='BLREC Vainglory Dashboard API',
        version='2.0.0',
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=False,
        allow_methods=['GET'],
        allow_headers=['Accept', 'Authorization', 'Content-Type'],
        max_age=86400,
    )
    realtime_broker = DashboardRealtimeBroker()
    app.state.realtime_broker = realtime_broker
    app.state.dashboard_repository = active_repository
    app.state.source_watch_task = None

    @app.on_event('startup')
    async def start_source_watch() -> None:
        async def watch() -> None:
            while True:
                await asyncio.sleep(active_settings.source_watch_seconds)
                try:
                    changed = await run_in_threadpool(active_repository.refresh)
                    if not changed:
                        continue
                    public_dashboard_cache.replace(
                        active_repository.dashboard_document()
                    )
                    owner_dashboard_cache.replace(
                        active_repository.dashboard_document(owner_view=True)
                    )
                    revision = active_repository.dashboard_document()[1]
                    await realtime_broker.publish('dashboard', {'revision': revision})
                    await realtime_broker.publish('live_rooms', {'revision': revision})
                    await realtime_broker.publish('matches', {'revision': revision})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception(
                        'dashboard source refresh failed; keeping last good result'
                    )

        app.state.source_watch_task = asyncio.create_task(watch())

    @app.on_event('shutdown')
    async def stop_source_watch() -> None:
        task = app.state.source_watch_task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @app.get('/v1/health')
    def health() -> Dict[str, str]:
        with database_session(auxiliary_target) as connection:
            connection.execute('SELECT 1').fetchone()
        with database_session(active_settings.source_database_target) as connection:
            connection.execute(
                'SELECT revision FROM dashboard_source_state WHERE singleton_id=1'
            ).fetchone()
        return {'status': 'ok'}

    @app.post('/v1/assets/batches')
    async def asset_batch(
        batch: AssetBatch,
        authorization: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(
            default=None, alias='X-Idempotency-Key'
        ),
    ) -> Dict[str, Any]:
        _authenticate_write(authorization, active_settings)
        if idempotency_key is None or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise HTTPException(status_code=422, detail='invalid idempotency key')
        try:
            result = await run_in_threadpool(
                partial(
                    apply_asset_batch,
                    auxiliary_target,
                    idempotency_key=idempotency_key,
                    batch=batch,
                )
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail='idempotency key was already used for another payload',
            ) from error
        await realtime_broker.publish('matches', {'batchId': str(result['batchId'])})
        return result

    @app.get('/v1/events')
    async def events(request: Request) -> StreamingResponse:
        return event_response(request, realtime_broker)

    @app.get('/v1/owner/session')
    def owner_session(
        response: Response, authorization: Optional[str] = Header(default=None)
    ) -> Mapping[str, bool]:
        if not _owner_view(authorization, active_settings):
            raise HTTPException(
                status_code=401,
                detail='owner credentials are required',
                headers={'WWW-Authenticate': 'Bearer'},
            )
        _set_view_headers(response, True)
        return {'owner': True}

    @app.get('/v1/dashboard')
    def dashboard(
        if_none_match: Optional[str] = Header(default=None, alias='If-None-Match'),
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        owner_view = _owner_view(authorization, active_settings)
        payload, revision = (
            owner_dashboard_cache.current()
            if owner_view
            else public_dashboard_cache.current()
        )
        if owner_view:
            return Response(
                content=payload,
                media_type='application/json',
                headers={'Cache-Control': 'private, no-store', 'Vary': 'Authorization'},
            )
        etag = 'W/"{}"'.format(revision)
        headers = {
            'Cache-Control': 'public, no-cache',
            'ETag': etag,
            'Vary': 'Authorization',
        }
        if _etag_matches(if_none_match, revision):
            return Response(status_code=304, headers=headers)
        return Response(content=payload, media_type='application/json', headers=headers)

    @app.get('/v1/live-rooms')
    def live_rooms(
        if_none_match: Optional[str] = Header(default=None, alias='If-None-Match'),
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        owner_view = _owner_view(authorization, active_settings)
        document, revision = active_repository.live_rooms(owner_view=owner_view)
        payload = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(',', ':')
        ).encode('utf-8')
        if owner_view:
            return Response(
                content=payload,
                media_type='application/json',
                headers={'Cache-Control': 'private, no-store', 'Vary': 'Authorization'},
            )
        etag = '"{}"'.format(revision)
        headers = {
            'Cache-Control': 'public, max-age=15, stale-while-revalidate=30',
            'ETag': etag,
            'Vary': 'Authorization',
        }
        if _etag_matches(if_none_match, revision):
            return Response(status_code=304, headers=headers)
        return Response(content=payload, media_type='application/json', headers=headers)

    @app.get('/v1/matches')
    def matches(
        response: Response,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, alias='pageSize', ge=1, le=50),
        season: Optional[str] = Query(
            default=None, regex=r'^(all-time|\d{4}-(spring|summer|autumn|winter))$'
        ),
        mode: Optional[Literal['3v3', 'brawl', '5v5']] = None,
        player_id: Optional[int] = Query(default=None, alias='playerId', gt=0),
        query: str = Query(default='', alias='q', max_length=120),
        heroes: str = Query(default='', max_length=900),
        rating_scope: Literal['all', '3v3', 'brawl', '5v5'] = Query(
            default='all', alias='ratingScope'
        ),
        rating_season: Optional[str] = Query(
            default=None,
            alias='ratingSeason',
            regex=r'^(all-time|\d{4}-(spring|summer|autumn|winter))$',
        ),
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        owner_view = _owner_view(authorization, active_settings)
        _set_view_headers(response, owner_view)
        return active_repository.list_matches(
            page=page,
            page_size=page_size,
            season=None if season == 'all-time' else season,
            mode=mode,
            player_id=player_id,
            query=query,
            heroes=_hero_filters(heroes),
            rating_scope=rating_scope,
            rating_season=rating_season,
            owner_view=owner_view,
        )

    @app.get('/v1/matches/summary')
    def match_summary(
        response: Response,
        season: Optional[str] = Query(
            default=None, regex=r'^(all-time|\d{4}-(spring|summer|autumn|winter))$'
        ),
        mode: Optional[Literal['3v3', 'brawl', '5v5']] = None,
        player_id: Optional[int] = Query(default=None, alias='playerId', gt=0),
        authorization: Optional[str] = Header(default=None),
    ) -> Mapping[str, int]:
        owner_view = _owner_view(authorization, active_settings)
        _set_view_headers(response, owner_view)
        return active_repository.match_summary(
            season=None if season == 'all-time' else season,
            mode=mode,
            player_id=player_id,
            owner_view=owner_view,
        )

    @app.get('/v1/matches/{match_id}')
    def match_detail(
        response: Response,
        match_id: int,
        rating_scope: Literal['all', '3v3', 'brawl', '5v5'] = Query(
            default='all', alias='ratingScope'
        ),
        rating_season: Optional[str] = Query(
            default=None,
            alias='ratingSeason',
            regex=r'^(all-time|\d{4}-(spring|summer|autumn|winter))$',
        ),
        authorization: Optional[str] = Header(default=None),
    ) -> Mapping[str, Any]:
        owner_view = _owner_view(authorization, active_settings)
        _set_view_headers(response, owner_view)
        try:
            return active_repository.get_match(
                match_id,
                rating_scope=rating_scope,
                rating_season=rating_season,
                owner_view=owner_view,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail='match not found') from error

    return app


def main() -> None:
    settings = ApiSettings.from_environment()
    uvicorn.run(
        create_app(settings),
        host='127.0.0.1',
        port=8787,
        proxy_headers=True,
        forwarded_allow_ips='127.0.0.1',
        access_log=True,
    )


if __name__ == '__main__':
    main()
