from __future__ import annotations

import hashlib
import hmac
import json
import re
from functools import partial
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .dashboard import ensure_dashboard_state, get_dashboard_document
from .database import database_session, initialize_database
from .models import IngestBatch
from .realtime import DashboardRealtimeBroker, event_response
from .service import (
    IdempotencyConflict,
    apply_ingest_batch,
    get_live_rooms,
    get_match,
    get_match_summary,
    list_matches,
    reconcile_match_fingerprints,
)
from .settings import ApiSettings

_IDEMPOTENCY_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')


def _authenticate_ingest(authorization: Optional[str], settings: ApiSettings) -> None:
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
            detail='invalid ingest credentials',
            headers={'WWW-Authenticate': 'Bearer'},
        )


def _hero_filters(value: str) -> tuple[str, ...]:
    heroes = tuple(
        dict.fromkeys(hero.strip() for hero in value.split(',') if hero.strip())
    )
    if len(heroes) > 10 or any(len(hero) > 80 for hero in heroes):
        raise HTTPException(status_code=422, detail='invalid hero filters')
    return heroes


def create_app(settings: Optional[ApiSettings] = None) -> FastAPI:
    active_settings = settings or ApiSettings.from_environment()
    database_target = active_settings.database_target
    initialize_database(database_target)
    reconcile_match_fingerprints(database_target)
    ensure_dashboard_state(database_target)
    app = FastAPI(
        title='BLREC Vainglory Dashboard API',
        version='1.0.0',
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=False,
        allow_methods=['GET'],
        allow_headers=['Accept', 'Content-Type'],
        max_age=86400,
    )
    realtime_broker = DashboardRealtimeBroker()
    app.state.realtime_broker = realtime_broker

    @app.get('/v1/health')
    def health() -> dict:
        with database_session(database_target) as connection:
            connection.execute('SELECT 1').fetchone()
        return {'status': 'ok'}

    @app.post('/v1/ingest/batches')
    async def ingest_batch(
        batch: IngestBatch,
        authorization: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(
            default=None, alias='X-Idempotency-Key'
        ),
    ) -> dict:
        _authenticate_ingest(authorization, active_settings)
        if idempotency_key is None or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise HTTPException(status_code=422, detail='invalid idempotency key')
        try:
            result = await run_in_threadpool(
                partial(
                    apply_ingest_batch,
                    database_target,
                    idempotency_key=idempotency_key,
                    batch=batch,
                )
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail='idempotency key was already used for another payload',
            ) from error
        current = await run_in_threadpool(get_dashboard_document, database_target)
        revision = '' if current is None else current[1]
        await realtime_broker.publish('dashboard', {'revision': revision})
        await realtime_broker.publish('live_rooms', {'revision': revision})
        return result

    @app.get('/v1/events')
    async def events(request: Request) -> StreamingResponse:
        return event_response(request, realtime_broker)

    @app.get('/v1/dashboard')
    def dashboard(
        if_none_match: Optional[str] = Header(default=None, alias='If-None-Match')
    ) -> Response:
        current = get_dashboard_document(database_target)
        if current is None:
            raise HTTPException(
                status_code=503, detail='dashboard is waiting for its first publication'
            )
        document, revision = current
        etag = '"{}"'.format(revision)
        headers = {
            'Cache-Control': 'public, max-age=60, stale-while-revalidate=300',
            'ETag': etag,
        }
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=document, headers=headers)

    @app.get('/v1/live-rooms')
    def live_rooms(
        if_none_match: Optional[str] = Header(default=None, alias='If-None-Match')
    ) -> Response:
        document = get_live_rooms(database_target)
        if document is None:
            raise HTTPException(
                status_code=503,
                detail='live rooms are waiting for their first publication',
            )
        revision = hashlib.sha256(
            json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(',', ':')
            ).encode('utf-8')
        ).hexdigest()
        etag = '"{}"'.format(revision)
        headers = {
            'Cache-Control': 'public, max-age=15, stale-while-revalidate=30',
            'ETag': etag,
        }
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=document, headers=headers)

    @app.get('/v1/matches')
    def matches(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, alias='pageSize', ge=1, le=50),
        season: Optional[str] = Query(
            default=None, regex=r'^(all-time|\d{4}-(spring|summer|autumn))$'
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
            regex=r'^(all-time|\d{4}-(spring|summer|autumn))$',
        ),
    ) -> dict:
        with database_session(database_target) as connection:
            initialized = connection.execute(
                'SELECT 1 FROM ingestion_batches LIMIT 1'
            ).fetchone()
        if initialized is None:
            raise HTTPException(
                status_code=503,
                detail='match archive is waiting for its first publication',
            )
        return list_matches(
            database_target,
            page=page,
            page_size=page_size,
            season=None if season == 'all-time' else season,
            mode=mode,
            player_id=player_id,
            query=query,
            heroes=_hero_filters(heroes),
            rating_scope=rating_scope,
            rating_season=rating_season,
        )

    @app.get('/v1/matches/summary')
    def match_summary(
        season: Optional[str] = Query(
            default=None, regex=r'^(all-time|\d{4}-(spring|summer|autumn))$'
        ),
        mode: Optional[Literal['3v3', 'brawl', '5v5']] = None,
        player_id: Optional[int] = Query(default=None, alias='playerId', gt=0),
    ) -> dict:
        with database_session(database_target) as connection:
            initialized = connection.execute(
                'SELECT 1 FROM ingestion_batches LIMIT 1'
            ).fetchone()
        if initialized is None:
            raise HTTPException(
                status_code=503,
                detail='match archive is waiting for its first publication',
            )
        return get_match_summary(
            database_target,
            season=None if season == 'all-time' else season,
            mode=mode,
            player_id=player_id,
        )

    @app.get('/v1/matches/{match_id}')
    def match_detail(
        match_id: int,
        rating_scope: Literal['all', '3v3', 'brawl', '5v5'] = Query(
            default='all', alias='ratingScope'
        ),
        rating_season: Optional[str] = Query(
            default=None,
            alias='ratingSeason',
            regex=r'^(all-time|\d{4}-(spring|summer|autumn))$',
        ),
    ) -> dict:
        try:
            return get_match(
                database_target,
                match_id,
                rating_scope=rating_scope,
                rating_season=rating_season,
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
