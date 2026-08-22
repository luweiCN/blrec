"""Vision Lab PostgreSQL compatibility layer and SQLite migration support."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import threading
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

POSTGRES_SCHEMA_VERSION = 8
_SCHEMA_NAME = re.compile(r'^[a-z_][a-z0-9_]*$')
_INSERT_TABLE = re.compile(
    r'^\s*INSERT\s+INTO\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))', re.I
)
_CREATE_TABLE = re.compile(
    r'^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)', re.I
)
_REFERENCES = re.compile(r'\bREFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)', re.I)

_pool_lock = threading.Lock()
_pool: Any = None
_pool_key: Optional[Tuple[str, str, int]] = None
_identity_tables: Set[str] = set()


POSTGRES_COMPATIBILITY_SQL = """
CREATE OR REPLACE FUNCTION json_extract(document text, path text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$
    SELECT jsonb_path_query_first(document::jsonb, path::jsonpath) #>> '{}'
$$;

CREATE OR REPLACE FUNCTION json_each(document text)
RETURNS TABLE(key text, value text)
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT item.key, item.value::text
    FROM jsonb_each(COALESCE(document, '{}')::jsonb) AS item
$$;
"""

POSTGRES_SCHEMA_MIGRATIONS = {
    2: (
        """
        CREATE TABLE IF NOT EXISTS service_runtime_states (
            service_key TEXT PRIMARY KEY,
            state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """,
    ),
    3: (
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_sources_type_frame
        ON training_review_sources (source_type, frame_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS training_review_material_index (
            frame_id BIGINT PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
            video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            review_status TEXT NOT NULL CHECK (
                review_status IN ('pending','partial','confirmed','skipped')),
            scene TEXT NOT NULL DEFAULT 'other' CHECK (
                scene IN ('gameplay_hud','scoreboard','result_page',
                          'hero_select','other')),
            match_mode TEXT NOT NULL DEFAULT '' CHECK (
                match_mode IN ('','3v3','aram','5v5')),
            is_new BIGINT NOT NULL DEFAULT 0 CHECK (is_new IN (0,1)),
            is_legacy BIGINT NOT NULL DEFAULT 0 CHECK (is_legacy IN (0,1)),
            has_worker BIGINT NOT NULL DEFAULT 0 CHECK (has_worker IN (0,1)),
            has_result_archive BIGINT NOT NULL DEFAULT 0 CHECK (
                has_result_archive IN (0,1)),
            has_manual_correction BIGINT NOT NULL DEFAULT 0 CHECK (
                has_manual_correction IN (0,1)),
            has_model_prefill BIGINT NOT NULL DEFAULT 0 CHECK (
                has_model_prefill IN (0,1)),
            has_hero_model_prefill BIGINT NOT NULL DEFAULT 0 CHECK (
                has_hero_model_prefill IN (0,1)),
            has_low_confidence BIGINT NOT NULL DEFAULT 0 CHECK (
                has_low_confidence IN (0,1)),
            has_boundary_confidence BIGINT NOT NULL DEFAULT 0 CHECK (
                has_boundary_confidence IN (0,1)),
            has_high_confidence BIGINT NOT NULL DEFAULT 0 CHECK (
                has_high_confidence IN (0,1)),
            selects_aram BIGINT NOT NULL DEFAULT 0 CHECK (selects_aram IN (0,1)),
            suggests_aram BIGINT NOT NULL DEFAULT 0 CHECK (
                suggests_aram IN (0,1)),
            source_created_at BIGINT NOT NULL DEFAULT 0,
            source_offset BIGINT NOT NULL DEFAULT 0,
            result_group_representative_frame_id BIGINT NOT NULL,
            result_group_size BIGINT NOT NULL DEFAULT 1 CHECK (
                result_group_size >= 1),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_queue
        ON training_review_material_index (
            review_status,is_new,scene,match_mode,
            source_created_at DESC,source_offset DESC,frame_id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_legacy_queue
        ON training_review_material_index (
            review_status,is_legacy,scene,match_mode,
            source_created_at DESC,frame_id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_video_mode
        ON training_review_material_index (video_id,review_status,match_mode)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_representative
        ON training_review_material_index (
            result_group_representative_frame_id,frame_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS training_review_material_contributions (
            frame_id BIGINT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('scene_mode','hero_scene')),
            scene TEXT NOT NULL,
            match_mode TEXT NOT NULL DEFAULT '',
            hero_label TEXT NOT NULL DEFAULT '',
            source_scope TEXT NOT NULL CHECK (
                source_scope IN ('all','new','legacy')),
            metric TEXT NOT NULL CHECK (metric IN ('confirmed','candidate')),
            frame_count BIGINT NOT NULL DEFAULT 0 CHECK (frame_count >= 0),
            crop_count BIGINT NOT NULL DEFAULT 0 CHECK (crop_count >= 0),
            PRIMARY KEY (
                frame_id,kind,scene,match_mode,hero_label,source_scope,metric)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS training_review_material_totals (
            kind TEXT NOT NULL CHECK (kind IN ('scene_mode','hero_scene')),
            scene TEXT NOT NULL,
            match_mode TEXT NOT NULL DEFAULT '',
            hero_label TEXT NOT NULL DEFAULT '',
            source_scope TEXT NOT NULL CHECK (
                source_scope IN ('all','new','legacy')),
            metric TEXT NOT NULL CHECK (metric IN ('confirmed','candidate')),
            frame_count BIGINT NOT NULL DEFAULT 0 CHECK (frame_count >= 0),
            crop_count BIGINT NOT NULL DEFAULT 0 CHECK (crop_count >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                kind,scene,match_mode,hero_label,source_scope,metric)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_hero_confirmed_label
        ON training_review_hero_slots (confirmed_label,frame_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_hero_suggested_label
        ON training_review_hero_slots (suggested_label,frame_id)
        """,
    ),
    4: (
        """
        CREATE TABLE IF NOT EXISTS training_review_match_contexts (
            match_id BIGINT PRIMARY KEY,
            session_id BIGINT NOT NULL CHECK (session_id > 0),
            part_id BIGINT NOT NULL CHECK (part_id > 0),
            started_at_ms BIGINT NOT NULL CHECK (started_at_ms >= 0),
            result_at_ms BIGINT NOT NULL CHECK (result_at_ms >= started_at_ms),
            game_mode TEXT NOT NULL DEFAULT '' CHECK (
                game_mode IN ('','3v3','aram','5v5')),
            source_type TEXT NOT NULL CHECK (
                source_type IN ('result_archive','manual_correction')),
            updated_at TEXT NOT NULL
        )
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS session_id BIGINT NOT NULL DEFAULT 0
        CHECK (session_id >= 0)
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS part_id BIGINT NOT NULL DEFAULT 0
        CHECK (part_id >= 0)
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS at_ms BIGINT NOT NULL DEFAULT 0
        CHECK (at_ms >= 0)
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS linked_match_id BIGINT
        REFERENCES training_review_match_contexts(match_id) ON DELETE SET NULL
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS match_link_source TEXT NOT NULL DEFAULT ''
        CHECK (match_link_source IN ('','result_archive','time_window'))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_match_context_window
        ON training_review_match_contexts (
            session_id,part_id,started_at_ms,result_at_ms,match_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_source_time
        ON training_review_material_index (session_id,part_id,at_ms,frame_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_match_scene
        ON training_review_material_index (
            linked_match_id,review_status,scene,frame_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_video_link_scene
        ON training_review_material_index (
            video_id,linked_match_id,review_status,scene,frame_id)
        """,
    ),
    5: (
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (prefill_status IN ('pending','queued','running','ready','failed'))
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_stage TEXT NOT NULL DEFAULT 'core'
        CHECK (prefill_stage IN ('core','hero','complete'))
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_attempts BIGINT NOT NULL DEFAULT 0
        CHECK (prefill_attempts >= 0)
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_error TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_screen_type TEXT NOT NULL DEFAULT ''
        CHECK (prefill_screen_type IN (
            '','gameplay_hud','scoreboard','result_page'))
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_team_size BIGINT
        CHECK (prefill_team_size IN (3,5))
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefill_updated_at TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE training_review_material_index
        ADD COLUMN IF NOT EXISTS prefilled_at TEXT
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_material_prefill_queue
        ON training_review_material_index (
            prefill_status,prefill_stage,prefill_attempts,review_status,is_new,
            source_created_at DESC,frame_id DESC)
        """,
        """
        DELETE FROM training_review_material_contributions
        WHERE metric='candidate'
        """,
        """
        DELETE FROM training_review_material_totals
        WHERE metric='candidate'
        """,
    ),
    6: (
        """
        CREATE TABLE IF NOT EXISTS training_review_candidate_inbox (
            frame_id BIGINT PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
            prefill_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                prefill_status IN (
                    'pending','queued','running','failed','promoted')),
            prefill_stage TEXT NOT NULL DEFAULT 'core' CHECK (
                prefill_stage IN ('core','hero','complete')),
            prefill_attempts BIGINT NOT NULL DEFAULT 0 CHECK (
                prefill_attempts >= 0),
            prefill_error TEXT NOT NULL DEFAULT '',
            prefill_screen_type TEXT NOT NULL DEFAULT '' CHECK (
                prefill_screen_type IN (
                    '','gameplay_hud','scoreboard','result_page')),
            prefill_team_size BIGINT CHECK (prefill_team_size IN (3,5)),
            source_created_at BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            promoted_at TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_candidate_inbox_queue
        ON training_review_candidate_inbox (
            prefill_status,prefill_stage,prefill_attempts,
            source_created_at DESC,frame_id DESC)
        """,
    ),
    7: (
        """
        ALTER TABLE training_review_items
        ADD COLUMN IF NOT EXISTS match_kind_label TEXT CHECK (
            match_kind_label IS NULL OR match_kind_label IN (
                'pvp','bot','practice','unreadable'))
        """,
        """
        ALTER TABLE training_review_items
        ADD COLUMN IF NOT EXISTS view_context_label TEXT CHECK (
            view_context_label IS NULL OR view_context_label IN (
                'played','spectated','replay','unreadable'))
        """,
        """
        ALTER TABLE training_review_items
        DROP CONSTRAINT IF EXISTS training_review_items_match_mode_label_check
        """,
        """
        ALTER TABLE training_review_items
        ADD CONSTRAINT training_review_items_match_mode_label_check CHECK (
            match_mode_label IS NULL OR match_mode_label IN (
                '3v3','aram','5v5','blitz','unreadable'))
        """,
        """
        ALTER TABLE training_review_items
        DROP CONSTRAINT IF EXISTS training_review_items_hero_select_label_check
        """,
        """
        ALTER TABLE training_review_items
        ADD CONSTRAINT training_review_items_hero_select_label_check CHECK (
            hero_select_label IS NULL OR hero_select_label IN (
                'not_select','select_3v3','select_aram','select_5v5',
                'select_blitz','unreadable'))
        """,
        """
        ALTER TABLE training_review_material_index
        DROP CONSTRAINT IF EXISTS training_review_material_index_match_mode_check
        """,
        """
        ALTER TABLE training_review_material_index
        ADD CONSTRAINT training_review_material_index_match_mode_check CHECK (
            match_mode IN ('','3v3','aram','5v5','blitz'))
        """,
        """
        ALTER TABLE training_review_match_contexts
        DROP CONSTRAINT IF EXISTS training_review_match_contexts_game_mode_check
        """,
        """
        ALTER TABLE training_review_match_contexts
        ADD CONSTRAINT training_review_match_contexts_game_mode_check CHECK (
            game_mode IN ('','3v3','aram','5v5','blitz'))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_context
        ON training_review_items (
            match_kind_label,view_context_label,review_status,frame_id)
        """,
    ),
    8: (
        """
        ALTER TABLE training_review_hero_slots
        ADD COLUMN IF NOT EXISTS is_afk BIGINT
        CHECK (is_afk IS NULL OR is_afk IN (0,1))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_training_review_hero_afk
        ON training_review_hero_slots (is_afk,frame_id)
        """,
    ),
}


class PostgresRow(Mapping[str, Any]):
    """Row compatible with both sqlite3.Row string and integer access."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)


class PostgresCursor:
    def __init__(self, cursor: Any, *, lastrowid: Optional[int] = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def _row(self, values: Optional[Sequence[Any]]) -> Optional[PostgresRow]:
        if values is None:
            return None
        columns = tuple(column.name for column in (self._cursor.description or ()))
        return PostgresRow(columns, values)

    def fetchone(self) -> Optional[PostgresRow]:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> List[PostgresRow]:
        result = [self._row(row) for row in self._cursor.fetchall()]
        return [row for row in result if row is not None]

    def __iter__(self) -> Iterator[PostgresRow]:
        for row in self._cursor:
            converted = self._row(row)
            if converted is not None:
                yield converted


class PostgresConnection:
    """Small DB-API adapter preserving Vision Lab's existing query surface."""

    dialect = 'postgresql'

    def __init__(self, connection: Any, pool: Any, identity_tables: Set[str]) -> None:
        self._connection = connection
        self._pool = pool
        self._identity_tables = identity_tables
        self._closed = False
        self.total_changes = 0

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return (
            not self._closed
            and self._connection.info.transaction_status != TransactionStatus.IDLE
        )

    def __enter__(self) -> 'PostgresConnection':
        return self

    def __exit__(self, error_type: Any, _error: Any, _traceback: Any) -> None:
        if error_type is None:
            self.commit()
        else:
            self.rollback()

    def execute(
        self, sql: str, parameters: Union[Sequence[Any], Mapping[str, Any]] = ()
    ) -> PostgresCursor:
        statement = postgres_sql(sql)
        match = _INSERT_TABLE.match(statement)
        table = (
            '' if match is None else next(value for value in match.groups() if value)
        )
        returns_identity = (
            table in self._identity_tables
            and re.search(r'\bRETURNING\b', statement, re.I) is None
        )
        if returns_identity:
            statement = statement.rstrip(';') + ' RETURNING id'
        cursor = self._connection.cursor()
        bound_parameters: Any = (
            dict(parameters) if isinstance(parameters, Mapping) else tuple(parameters)
        )
        try:
            cursor.execute(statement, bound_parameters)
        except Exception as error:
            _raise_compatible_error(error)
        lastrowid: Optional[int] = None
        if returns_identity:
            row = cursor.fetchone()
            if row is not None:
                lastrowid = int(row[0])
        if cursor.rowcount > 0:
            self.total_changes += int(cursor.rowcount)
        return PostgresCursor(cursor, lastrowid=lastrowid)

    def executemany(
        self, sql: str, parameters: Sequence[Union[Sequence[Any], Mapping[str, Any]]]
    ) -> PostgresCursor:
        cursor = self._connection.cursor()
        bound_parameters = tuple(
            dict(row) if isinstance(row, Mapping) else tuple(row) for row in parameters
        )
        try:
            cursor.executemany(postgres_sql(sql), bound_parameters)
        except Exception as error:
            _raise_compatible_error(error)
        if cursor.rowcount > 0:
            self.total_changes += int(cursor.rowcount)
        return PostgresCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self.in_transaction:
                self._connection.rollback()
        finally:
            self._closed = True
            self._pool.putconn(self._connection)


def _raise_compatible_error(error: Exception) -> None:
    import psycopg

    if isinstance(error, psycopg.IntegrityError):
        raise sqlite3.IntegrityError(str(error)) from error
    raise error


def validate_schema_name(schema: str) -> str:
    normalized = schema.strip()
    if not _SCHEMA_NAME.fullmatch(normalized):
        raise ValueError('VISION_LAB_DATABASE_SCHEMA 只能包含小写字母、数字和下划线')
    return normalized


def postgres_placeholders(sql: str) -> str:
    """Translate SQLite qmark/named placeholders without changing SQL literals."""

    translated: List[str] = []
    index = 0
    quote = ''
    while index < len(sql):
        character = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ''
        if quote:
            translated.append('%%' if character == '%' else character)
            if character == quote:
                if following == quote:
                    translated.append(following)
                    index += 1
                else:
                    quote = ''
        elif character in ("'", '"'):
            quote = character
            translated.append(character)
        elif character == '-' and following == '-':
            end = sql.find('\n', index + 2)
            if end < 0:
                translated.append(sql[index:])
                break
            translated.append(sql[index:end])
            index = end - 1
        elif character == '/' and following == '*':
            end = sql.find('*/', index + 2)
            if end < 0:
                translated.append(sql[index:])
                break
            translated.append(sql[index : end + 2])
            index = end + 1
        elif character == '?':
            translated.append('%s')
        elif (
            character == ':'
            and (following.isalpha() or following == '_')
            and (index == 0 or sql[index - 1] != ':')
        ):
            end = index + 2
            while end < len(sql) and (sql[end].isalnum() or sql[end] == '_'):
                end += 1
            name = sql[index + 1 : end]
            translated.append(f'%({name})s')
            index = end - 1
        elif character == '%':
            translated.append('%%')
        else:
            translated.append(character)
        index += 1
    return ''.join(translated)


def postgres_sql(sql: str) -> str:
    statement = sql.strip()
    if re.fullmatch(r'BEGIN\s+IMMEDIATE;?', statement, re.I):
        statement = 'BEGIN'
    if re.match(r'^INSERT\s+OR\s+IGNORE\s+INTO\s+', statement, re.I):
        statement = re.sub(
            r'^INSERT\s+OR\s+IGNORE\s+INTO\s+', 'INSERT INTO ', statement, count=1
        )
        statement = statement.rstrip(';') + ' ON CONFLICT DO NOTHING'
    statement = re.sub(r'\bIS\s+\?', 'IS NOT DISTINCT FROM ?', statement, flags=re.I)
    return postgres_placeholders(statement)


def postgres_schema_sql(sql: str) -> str:
    statement = re.sub(
        r'\bid\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
        'id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY',
        sql,
        flags=re.I,
    )
    statement = re.sub(
        r'\bid\s+INTEGER\s+PRIMARY\s+KEY\b',
        'id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY',
        statement,
        flags=re.I,
    )
    statement = re.sub(r'\bINTEGER\b', 'BIGINT', statement, flags=re.I)
    statement = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', statement, flags=re.I)
    statement = re.sub(r'\bBLOB\b', 'BYTEA', statement, flags=re.I)
    return statement


def _split_statements(script: str) -> List[str]:
    without_comments = re.sub(r'--[^\n]*', '', script)
    statements: List[str] = []
    current: List[str] = []
    quote = ''
    index = 0
    while index < len(without_comments):
        character = without_comments[index]
        following = (
            without_comments[index + 1] if index + 1 < len(without_comments) else ''
        )
        if quote:
            current.append(character)
            if character == quote:
                if following == quote:
                    current.append(following)
                    index += 1
                else:
                    quote = ''
        elif character in ("'", '"'):
            quote = character
            current.append(character)
        elif character == ';':
            value = ''.join(current).strip()
            if value:
                statements.append(value)
            current = []
        else:
            current.append(character)
        index += 1
    value = ''.join(current).strip()
    if value:
        statements.append(value)
    return statements


def ordered_schema_statements(script: str) -> Tuple[List[str], Set[str]]:
    tables: Dict[str, str] = {}
    trailing: List[str] = []
    identities: Set[str] = set()
    for raw in _split_statements(script):
        statement = postgres_schema_sql(raw)
        match = _CREATE_TABLE.match(statement)
        if match is None:
            trailing.append(statement)
            continue
        table = match.group(1)
        tables[table] = statement
        if 'GENERATED BY DEFAULT AS IDENTITY' in statement:
            identities.add(table)
    ordered: List[str] = []
    while tables:
        progress = False
        for table, statement in list(tables.items()):
            dependencies = {
                value
                for value in _REFERENCES.findall(statement)
                if value != table and value in tables
            }
            if dependencies:
                continue
            ordered.append(statement)
            del tables[table]
            progress = True
        if not progress:
            raise RuntimeError('PostgreSQL 表依赖存在环：' + ', '.join(sorted(tables)))
    return ordered + trailing, identities


def _apply_incremental_schema_migrations(cursor: Any, version: int) -> None:
    if not 1 <= version <= POSTGRES_SCHEMA_VERSION:
        raise RuntimeError(f'Vision Lab PostgreSQL schema 版本 {version} 不受支持')
    for target_version in range(version + 1, POSTGRES_SCHEMA_VERSION + 1):
        statements = POSTGRES_SCHEMA_MIGRATIONS.get(target_version)
        if not statements:
            raise RuntimeError(f'缺少 PostgreSQL schema v{target_version} 迁移')
        for statement in statements:
            cursor.execute(statement)
        cursor.execute(
            'INSERT INTO vision_schema_migrations (version, applied_at) '
            "VALUES (%s, to_char(clock_timestamp(), 'YYYY-MM-DD\"T\"HH24:MI:SS'))",
            (target_version,),
        )


def _initialize_schema(
    database_url: str,
    schema: str,
    schema_sql: str,
    default_tasks: Sequence[Sequence[str]],
) -> Set[str]:
    import psycopg
    from psycopg import sql

    statements, identities = ordered_schema_statements(schema_sql)
    connection = psycopg.connect(database_url, autocommit=False)
    try:
        cursor = connection.cursor()
        if not _schema_exists(cursor, schema):
            cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        cursor.execute(
            sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema))
        )
        cursor.execute(POSTGRES_COMPATIBILITY_SQL)
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS vision_schema_migrations ('
            'version BIGINT PRIMARY KEY, applied_at TEXT NOT NULL)'
        )
        row = cursor.execute(
            'SELECT COALESCE(MAX(version), 0) FROM vision_schema_migrations'
        ).fetchone()
        version = 0 if row is None else int(row[0])
        if not 0 <= version <= POSTGRES_SCHEMA_VERSION:
            raise RuntimeError(f'Vision Lab PostgreSQL schema 版本 {version} 不受支持')
        if version == 0:
            for statement in statements:
                cursor.execute(statement)
            cursor.executemany(
                'INSERT INTO annotation_tasks (id, name, description) '
                'VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING',
                tuple(tuple(row) for row in default_tasks),
            )
            cursor.execute(
                'INSERT INTO vision_schema_migrations (version, applied_at) '
                "VALUES (%s, to_char(clock_timestamp(), 'YYYY-MM-DD\"T\"HH24:MI:SS'))",
                (POSTGRES_SCHEMA_VERSION,),
            )
        elif version < POSTGRES_SCHEMA_VERSION:
            _apply_incremental_schema_migrations(cursor, version)
        connection.commit()
        return identities
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _schema_exists(cursor: Any, schema: str) -> bool:
    row = cursor.execute(
        'SELECT EXISTS (' 'SELECT 1 FROM pg_namespace WHERE nspname = %s)', (schema,)
    ).fetchone()
    return row is not None and bool(row[0])


def connect(
    database_url: str,
    *,
    schema: str,
    schema_sql: str,
    default_tasks: Sequence[Sequence[str]],
    pool_size: int = 8,
) -> PostgresConnection:
    global _pool, _pool_key, _identity_tables

    normalized_schema = validate_schema_name(schema)
    key = (database_url, normalized_schema, max(1, int(pool_size)))
    with _pool_lock:
        if _pool_key != key:
            close_pool()
            _identity_tables = _initialize_schema(
                database_url, normalized_schema, schema_sql, default_tasks
            )
            from psycopg_pool import ConnectionPool

            _pool = ConnectionPool(
                conninfo=database_url,
                min_size=1,
                max_size=key[2],
                timeout=10,
                kwargs={
                    'autocommit': False,
                    'options': f'-csearch_path={normalized_schema}',
                    'application_name': 'blrec-vision-lab',
                },
                open=True,
            )
            _pool_key = key
        connection = _pool.getconn()
    return PostgresConnection(connection, _pool, set(_identity_tables))


def close_pool() -> None:
    global _pool, _pool_key, _identity_tables
    if _pool is not None:
        _pool.close()
    _pool = None
    _pool_key = None
    _identity_tables = set()


def _hash_value(digest: Any, value: Any) -> None:
    if value is None:
        payload = b'n'
    elif isinstance(value, bool):
        payload = b'i' + str(int(value)).encode('ascii')
    elif isinstance(value, int):
        payload = b'i' + str(value).encode('ascii')
    elif isinstance(value, float):
        payload = b'f' + struct.pack('!d', value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        payload = b'b' + bytes(value)
    else:
        payload = b's' + str(value).encode('utf-8')
    digest.update(len(payload).to_bytes(8, 'big'))
    digest.update(payload)


def _row_digest(row: Sequence[Any]) -> bytes:
    digest = hashlib.sha256()
    for value in row:
        _hash_value(digest, value)
    return digest.digest()


def _row_hashes_digest(row_hashes: Sequence[bytes]) -> str:
    digest = hashlib.sha256()
    for row_hash in sorted(row_hashes):
        digest.update(row_hash)
    return digest.hexdigest()


def _rows_digest(rows: Iterator[Sequence[Any]]) -> Tuple[int, str]:
    row_hashes = [_row_digest(row) for row in rows]
    count = len(row_hashes)
    return count, _row_hashes_digest(row_hashes)


def migrate_sqlite_database(
    sqlite_path: Path,
    database_url: str,
    *,
    schema: str,
    schema_sql: str,
    default_tasks: Sequence[Sequence[str]],
) -> Dict[str, Any]:
    """Copy a complete SQLite workspace into an otherwise-empty schema."""

    import psycopg
    from psycopg import sql

    normalized_schema = validate_schema_name(schema)
    _initialize_schema(database_url, normalized_schema, schema_sql, default_tasks)
    source = sqlite3.connect(str(sqlite_path))
    source.row_factory = sqlite3.Row
    target = psycopg.connect(database_url, autocommit=False)
    report: Dict[str, Any] = {'schema': normalized_schema, 'tables': {}}
    try:
        quick = source.execute('PRAGMA quick_check').fetchone()
        if quick is None or str(quick[0]) != 'ok':
            raise RuntimeError('SQLite PRAGMA quick_check 未通过')
        target.execute(
            sql.SQL('SET LOCAL search_path TO {}').format(
                sql.Identifier(normalized_schema)
            )
        )
        source_tables = {
            str(row['name'])
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        statements, _identities = ordered_schema_statements(schema_sql)
        ordered_tables = [
            match.group(1)
            for statement in statements
            if (match := _CREATE_TABLE.match(statement)) is not None
            and match.group(1) in source_tables
        ]
        for table in ordered_tables:
            if table == 'annotation_tasks':
                continue
            count = target.execute(
                sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier(table))
            ).fetchone()
            if count is not None and int(count[0]) > 0:
                raise RuntimeError(
                    f'目标 schema 已有数据（{table}={int(count[0])}），拒绝覆盖'
                )
        target.execute('DELETE FROM annotation_tasks')
        for table in ordered_tables:
            info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = [str(row['name']) for row in info]
            primary = [
                str(row['name'])
                for row in sorted(info, key=lambda value: int(value['pk'] or 0))
                if int(row['pk'] or 0) > 0
            ]
            order_columns = primary or columns
            source_sql = 'SELECT {} FROM "{}" ORDER BY {}'.format(
                ', '.join(f'"{name}"' for name in columns),
                table,
                ', '.join(f'"{name}"' for name in order_columns),
            )
            source_rows = source.execute(source_sql)
            source_row_hashes: List[bytes] = []
            source_count = 0
            copy_sql = sql.SQL('COPY {} ({}) FROM STDIN').format(
                sql.Identifier(table),
                sql.SQL(', ').join(sql.Identifier(name) for name in columns),
            )
            with target.cursor().copy(copy_sql) as copy:
                for row in source_rows:
                    values = tuple(row[name] for name in columns)
                    copy.write_row(values)
                    source_count += 1
                    source_row_hashes.append(_row_digest(values))
            target_rows = target.execute(
                sql.SQL('SELECT {} FROM {} ORDER BY {}').format(
                    sql.SQL(', ').join(sql.Identifier(name) for name in columns),
                    sql.Identifier(table),
                    sql.SQL(', ').join(sql.Identifier(name) for name in order_columns),
                )
            )
            target_count, target_hash = _rows_digest(iter(target_rows))
            source_hash = _row_hashes_digest(source_row_hashes)
            if target_count != source_count or target_hash != source_hash:
                raise RuntimeError(
                    f'{table} 迁移校验失败：'
                    f'SQLite {source_count}/{source_hash[:12]}，'
                    f'PostgreSQL {target_count}/{target_hash[:12]}'
                )
            report['tables'][table] = {'rows': source_count, 'sha256': source_hash}
            if 'id' in columns:
                sequence = target.execute(
                    'SELECT pg_get_serial_sequence(%s, %s)',
                    (f'{normalized_schema}.{table}', 'id'),
                ).fetchone()
                if sequence is not None and sequence[0]:
                    maximum = target.execute(
                        sql.SQL('SELECT MAX(id) FROM {}').format(sql.Identifier(table))
                    ).fetchone()
                    value = None if maximum is None else maximum[0]
                    target.execute(
                        'SELECT setval(%s, %s, %s)',
                        (str(sequence[0]), int(value or 1), value is not None),
                    )
        target.commit()
        target.autocommit = True
        for table in ordered_tables:
            target.execute(
                sql.SQL('ANALYZE {}.{}').format(
                    sql.Identifier(normalized_schema), sql.Identifier(table)
                )
            )
        report['verified'] = True
        report['table_count'] = len(report['tables'])
        report['row_count'] = sum(
            int(value['rows']) for value in report['tables'].values()
        )
        return report
    except BaseException:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()
