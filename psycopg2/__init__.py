from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

apilevel = "2.0"
threadsafety = 1
paramstyle = "pyformat"

Error = sqlite3.Error
DatabaseError = sqlite3.DatabaseError
OperationalError = sqlite3.OperationalError
ProgrammingError = sqlite3.ProgrammingError
IntegrityError = sqlite3.IntegrityError
InterfaceError = sqlite3.InterfaceError


def _sqlite_path() -> Path:
    from backend.db.database import DB_PATH

    return Path(DB_PATH)


def _normalize_sql(sql: str) -> str:
    return (
        sql.replace("NOW()", "CURRENT_TIMESTAMP")
        .replace("now()", "CURRENT_TIMESTAMP")
        .replace("ILIKE", "LIKE")
    )


def _expand_any_clause(sql: str, params: Sequence[Any]) -> tuple[str, list[Any]]:
    if "ANY(%s)" not in sql:
        return sql, list(params)

    new_sql = sql
    new_params: list[Any] = []
    param_index = 0
    while "ANY(%s)" in new_sql:
        if param_index >= len(params):
            break
        value = params[param_index]
        param_index += 1
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise ProgrammingError("ANY(%s) requires a sequence parameter")
        items = list(value)
        placeholders = ", ".join("?" for _ in items) or "NULL"
        new_sql = re.sub(
            r"=\s*ANY\(\s*%s\s*\)", f"IN ({placeholders})", new_sql, count=1
        )
        new_params.extend(items)

    new_params.extend(list(params[param_index:]))
    return new_sql, new_params


def _translate_sql(sql: str, params: Any) -> tuple[str, Any]:
    sql = _normalize_sql(sql)

    if params is None:
        return sql, None

    if isinstance(params, dict):
        translated = re.sub(r"%\(([^)]+)\)s", r":\1", sql)
        return translated, params

    if isinstance(params, (list, tuple)):
        sql, params_list = _expand_any_clause(sql, list(params))
        sql = re.sub(r"%s", "?", sql)
        return sql, params_list

    return re.sub(r"%s", "?", sql), [params]


class Cursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def _coerce_row(self, row):
        if row is None:
            return None
        if not self.description:
            return row
        coerced = []
        for idx, value in enumerate(row):
            column = (self.description[idx][0] or "").lower()
            if isinstance(value, str) and (
                column.endswith("_at")
                or column.endswith("_time")
                or column.endswith("timestamp")
                or column.endswith("date")
            ):
                try:
                    coerced.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
                    continue
                except ValueError:
                    pass
            coerced.append(value)
        return tuple(coerced)

    def execute(self, sql: str, params: Any = None):
        translated_sql, translated_params = _translate_sql(sql, params)
        if translated_params is None:
            self._cursor.execute(translated_sql)
        else:
            self._cursor.execute(translated_sql, translated_params)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        translated_sql = _normalize_sql(sql)
        translated_sql = re.sub(r"%s", "?", translated_sql)
        self._cursor.executemany(translated_sql, seq_of_params)
        return self

    def fetchone(self):
        return self._coerce_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._coerce_row(row) for row in self._cursor.fetchall()]

    def close(self):
        self._cursor.close()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount


class Connection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def connect(dsn: str | None = None, *args, **kwargs) -> Connection:
    from backend.db.database import init_db

    init_db()

    sqlite_path = _sqlite_path()
    if dsn and dsn.startswith("sqlite:///"):
        sqlite_path = Path(dsn.removeprefix("sqlite:///"))
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return Connection(conn)


__all__ = [
    "connect",
    "Connection",
    "Cursor",
    "Error",
    "DatabaseError",
    "OperationalError",
    "ProgrammingError",
    "IntegrityError",
    "InterfaceError",
]
