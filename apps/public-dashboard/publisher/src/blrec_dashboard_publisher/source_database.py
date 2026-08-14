from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, Union

DatabaseTarget = Union[Path, str]


class PostgresRow(Mapping[str, Any]):
    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = tuple(names)
        self._values = tuple(values)
        self._mapping = dict(zip(self._names, self._values))

    def __getitem__(self, key: Union[str, int]) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def _row_factory(cursor: Any) -> Any:
    names = tuple(column.name for column in (cursor.description or ()))
    return lambda values: PostgresRow(names, values)


class PostgresSourceConnection:
    dialect = 'postgresql'
    row_factory: Any = None

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self._connection.info.transaction_status != TransactionStatus.IDLE

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        sql = statement.replace('?', '%s')
        if parameters:
            return self._connection.execute(sql, tuple(parameters))
        return self._connection.execute(sql)

    def close(self) -> None:
        self._connection.close()


def is_postgres(target: DatabaseTarget) -> bool:
    return isinstance(target, str) and target.startswith(
        ('postgresql://', 'postgresql+psycopg://')
    )


def connect_source_database(target: DatabaseTarget) -> Any:
    if is_postgres(target):
        import psycopg

        database_url = str(target).replace('postgresql+psycopg://', 'postgresql://', 1)
        return PostgresSourceConnection(
            psycopg.connect(database_url, autocommit=True, row_factory=_row_factory)
        )
    path = Path(target).expanduser().resolve(strict=True)
    connection = sqlite3.connect('{}?mode=ro'.format(path.as_uri()), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    return connection
