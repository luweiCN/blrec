from __future__ import annotations

import hashlib
import hmac
import re
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import database_session, initialize_database
from .models import IngestBatch
from .service import IdempotencyConflict, apply_ingest_batch, get_match, list_matches
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
    initialize_database(active_settings.database_path)
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

    @app.get('/v1/health')
    def health() -> dict:
        with database_session(active_settings.database_path) as connection:
            connection.execute('SELECT 1').fetchone()
        return {'status': 'ok'}

    @app.post('/v1/ingest/batches')
    def ingest_batch(
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
            return apply_ingest_batch(
                active_settings.database_path,
                idempotency_key=idempotency_key,
                batch=batch,
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail='idempotency key was already used for another payload',
            ) from error

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
        with database_session(active_settings.database_path) as connection:
            initialized = connection.execute(
                'SELECT 1 FROM ingestion_batches LIMIT 1'
            ).fetchone()
        if initialized is None:
            raise HTTPException(
                status_code=503,
                detail='match archive is waiting for its first publication',
            )
        return list_matches(
            active_settings.database_path,
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
                active_settings.database_path,
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
