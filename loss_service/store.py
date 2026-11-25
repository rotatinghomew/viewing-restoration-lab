"""Append-only SQLite storage for slowly arriving viewing evidence.

The store deliberately keeps every imported business row.  Duplicate imports
of the same CSV/JSON row are ignored through a content hash.  Temporal queries
are handled by :mod:`loss_service.engine` using the rows' own ``known_at`` or
``utc`` columns, not the import sequence.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from intake import CONVERT, ROOT

SCHEMA_PATH = ROOT / "table-format.json"
SCHEMA: dict[str, dict[str, str]] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _value_for_db(value: Any, kind: str) -> Any:
    if kind == "bool":
        return 1 if value else 0
    return value


def read_csv_subset(directory: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Read any ``*.csv`` tables present, validating headers and scalar types."""
    directory = Path(directory)
    tables: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.csv")):
        table = path.stem
        if table not in SCHEMA:
            raise ValueError(f"{path.name}：未知资料表")
        columns = SCHEMA[table]
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(columns):
                raise ValueError(f"{path.name}：表头和格式约定不一致")
            rows: list[dict[str, Any]] = []
            for line_no, record in enumerate(reader, start=2):
                if None in record or None in record.values():
                    raise ValueError(f"{path.name}:{line_no}：列数不符")
                try:
                    rows.append(
                        {name: CONVERT[kind](record[name]) for name, kind in columns.items()}
                    )
                except ValueError as error:
                    raise ValueError(f"{path.name}:{line_no}：字段值无效") from error
        tables[table] = rows
    return tables


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for table, columns in SCHEMA.items():
                cols = [
                    "_seq INTEGER PRIMARY KEY AUTOINCREMENT",
                    "_hash TEXT NOT NULL UNIQUE",
                    "_batch TEXT",
                ]
                for name, kind in columns.items():
                    sql_kind = "INTEGER" if kind in {"int", "bool"} else "REAL" if kind == "float" else "TEXT"
                    cols.append(f'"{name}" {sql_kind}')
                self.db.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(cols)})')

    @staticmethod
    def _hash_row(table: str, row: dict[str, Any]) -> str:
        payload = json.dumps([table, row], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ingest_tables(
        self, tables: dict[str, list[dict[str, Any]]], batch: str | None = None
    ) -> dict[str, int]:
        inserted: dict[str, int] = {}
        with self.db:
            for table, rows in tables.items():
                if table not in SCHEMA:
                    raise ValueError(f"未知资料表：{table}")
                columns = list(SCHEMA[table])
                marks = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{c}"' for c in columns)
                sql = (
                    f'INSERT OR IGNORE INTO "{table}" (_hash, _batch, {quoted}) '
                    f"VALUES (?, ?, {marks})"
                )
                count = 0
                for row in rows:
                    clean = {c: _value_for_db(row[c], SCHEMA[table][c]) for c in columns}
                    digest = self._hash_row(table, clean)
                    cursor = self.db.execute(
                        sql, [digest, batch, *[clean[c] for c in columns]]
                    )
                    count += cursor.rowcount
                inserted[table] = count
        return inserted

    def ingest_directory(self, directory: str | Path, batch: str | None = None) -> dict[str, int]:
        return self.ingest_tables(read_csv_subset(directory), batch=batch or str(directory))

    def ingest_json_records(self, table: str, records: Iterable[dict[str, Any]]) -> int:
        if table not in SCHEMA:
            raise ValueError(f"未知资料表：{table}")
        kind_map = SCHEMA[table]
        rows = []
        for record in records:
            row = {}
            for name, kind in kind_map.items():
                if name not in record:
                    raise ValueError(f"{table} 记录缺少字段 {name}")
                value = record[name]
                if kind == "bool" and not isinstance(value, bool):
                    value = CONVERT["bool"](str(value))
                elif kind == "int":
                    value = int(value)
                elif kind == "float":
                    value = float(value)
                row[name] = value
            rows.append(row)
        return self.ingest_tables({table: rows})[table]

    def table(self, name: str) -> list[dict[str, Any]]:
        if name not in SCHEMA:
            raise ValueError(f"未知资料表：{name}")
        cursor = self.db.execute(f'SELECT * FROM "{name}" ORDER BY _seq')
        rows = []
        for record in cursor.fetchall():
            row = {k: record[k] for k in record.keys() if k == "_seq" or not k.startswith("_")}
            rows.append(row)
        return rows

    def tables(self) -> dict[str, list[dict[str, Any]]]:
        return {name: self.table(name) for name in SCHEMA}

    def latest_material_time(self) -> int | None:
        values: list[int] = []
        for table, field in (
            ("playback", "known_at"),
            ("outages", "known_at"),
            ("calibration", "known_at"),
            ("receipts", "utc"),
            ("rewatched", "utc"),
        ):
            for row in self.table(table):
                values.append(int(row[field]))
        return max(values) if values else None


def memory_store_from_directory(directory: str | Path) -> Store:
    store = Store(":memory:")
    store.ingest_directory(directory)
    return store
