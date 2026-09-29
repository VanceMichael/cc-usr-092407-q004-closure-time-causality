"""SQLite 持久化层。

单一数据库文件同时保存事件和计算结果，因此一次提交可以原子写入事件及其
全部影响，失败时也不会留下部分数据。数据库位于挂载卷时，WAL 模式可在
容器重启后继续保留数据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id             TEXT PRIMARY KEY,
    event_version        INTEGER NOT NULL,
    event_type           TEXT NOT NULL,
    airport_code         TEXT NOT NULL,
    effective_from       TEXT NOT NULL,
    effective_until      TEXT,
    reported_at          TEXT NOT NULL,
    supersedes_event_id  TEXT,
    reason               TEXT,
    payload_json         TEXT NOT NULL,
    replay_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    quarantined          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS impacts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT NOT NULL REFERENCES events(event_id),
    root_event_id      TEXT NOT NULL,
    airport_code       TEXT NOT NULL,
    flight_id          TEXT NOT NULL,
    flight_number      TEXT NOT NULL,
    affected_endpoint  TEXT NOT NULL,
    impact_status      TEXT NOT NULL,
    overlap_minutes    INTEGER,
    delay_minutes      INTEGER,
    proposed_departure TEXT,
    proposed_arrival   TEXT,
    passenger_count    INTEGER NOT NULL,
    crosses_midnight   INTEGER NOT NULL,
    UNIQUE(event_id, flight_id, airport_code)
);

CREATE TABLE IF NOT EXISTS chain_anomalies (
    root_event_id  TEXT PRIMARY KEY,
    airport_code   TEXT NOT NULL,
    reasons_json   TEXT NOT NULL,
    detected_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_impacts_root    ON impacts(root_event_id);
CREATE INDEX IF NOT EXISTS idx_impacts_airport ON impacts(airport_code, impact_status);
CREATE INDEX IF NOT EXISTS idx_impacts_flight  ON impacts(flight_id);
CREATE INDEX IF NOT EXISTS idx_events_airport  ON events(airport_code, event_version);
"""

# 旧版数据库（v1 建表时没有 quarantined 列）的增量迁移。SQLite 支持
# ALTER TABLE ADD COLUMN，列默认 0，存量行随后由启动审计重新打标。
_MIGRATIONS = (
    (
        "events",
        "quarantined",
        "ALTER TABLE events ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0",
    ),
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repository:
    """对单一 SQLite 连接提供线程安全封装。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, column, ddl in _MIGRATIONS:
            columns = {
                r["name"]
                for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1").fetchone()
            return row is not None and row[0] == 1

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def get_event_row(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()

    def get_impacts(self, event_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM impacts WHERE event_id = ? ORDER BY flight_id",
                    (event_id,),
                )
            )

    def events_for_airport(
        self, airport_code: str, *, include_quarantined: bool = False
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM events WHERE airport_code = ? "
            "ORDER BY event_version, effective_from"
        )
        params: list[Any] = [airport_code]
        if not include_quarantined:
            sql = sql.replace("WHERE", "WHERE quarantined = 0 AND", 1)
        with self._lock:
            return list(self._conn.execute(sql, params))

    def all_events(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM events ORDER BY event_version, event_id"
                )
            )

    def is_quarantined(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT quarantined FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row is not None and bool(row["quarantined"])

    def list_anomalies(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM chain_anomalies ORDER BY root_event_id"
                )
            )

    def anomaly_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM chain_anomalies").fetchone()
            return int(row["n"])

    def count_replays(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT replay_count FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return int(row["replay_count"]) if row else 0

    def latest_impacts(
        self,
        *,
        airport: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回每个航班与机场组合的最新影响。

        同一机场内，每条事件链采用最新事件的快照；航班同时出现在多条链时，
        采用最后生成的快照。`resolved` 墓碑参与排序，使恢复开放后释放的航班
        不再出现在结果中。启动审计隔离的异常链不参与当前汇总。最终结果按
        flight_id 稳定排序。
        """
        where = ["i.airport_code = ?", "e.quarantined = 0"] if airport else ["e.quarantined = 0"]
        params: list[Any] = [airport] if airport else []
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        outer = ["rn = 1", "impact_status != 'resolved'"]
        outer_params: list[Any] = []
        if status:
            outer.append("impact_status = ?")
            outer_params.append(status)

        sql = f"""
        WITH ranked AS (
            SELECT i.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.flight_id, i.airport_code
                       ORDER BY e.created_at DESC,
                                i.id DESC
                   ) AS rn
            FROM impacts i
            JOIN events e ON e.event_id = i.event_id
            {where_sql}
        )
        SELECT * FROM ranked
        WHERE {' AND '.join(outer)}
        ORDER BY flight_id, airport_code
        """
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params + outer_params)]

    def prior_chain_impact_ids(self, conn, root_event_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT DISTINCT flight_id FROM impacts WHERE root_event_id = ? "
            "AND impact_status != 'resolved'",
            (root_event_id,),
        ).fetchall()
        return {r["flight_id"] for r in rows}

    # ------------------------------------------------------------------ #
    # Writes (all callers run inside ``transaction``)
    # ------------------------------------------------------------------ #

    def transaction(self):
        return _Transaction(self._conn, self._lock)

    def insert_event(self, conn: sqlite3.Connection, event_dict: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO events (event_id, event_version, event_type, airport_code,
                                effective_from, effective_until, reported_at,
                                supersedes_event_id, reason, payload_json,
                                replay_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                event_dict["event_id"],
                event_dict["event_version"],
                event_dict["event_type"],
                event_dict["airport_code"],
                event_dict["effective_from"],
                event_dict["effective_until"],
                event_dict["reported_at"],
                event_dict["supersedes_event_id"],
                event_dict["reason"],
                json.dumps(event_dict, sort_keys=True, ensure_ascii=False),
                utcnow_iso(),
            ),
        )

    def insert_impacts(
        self, conn: sqlite3.Connection, impacts: Iterable[dict[str, Any]]
    ) -> None:
        conn.executemany(
            """
            INSERT INTO impacts (event_id, root_event_id, airport_code, flight_id,
                                 flight_number, affected_endpoint, impact_status,
                                 overlap_minutes, delay_minutes, proposed_departure,
                                 proposed_arrival, passenger_count, crosses_midnight)
            VALUES (:event_id, :root_event_id, :airport_code, :flight_id,
                    :flight_number, :affected_endpoint, :impact_status,
                    :overlap_minutes, :delay_minutes, :proposed_departure,
                    :proposed_arrival, :passenger_count, :crosses_midnight)
            """,
            list(impacts),
        )

    def increment_replay(self, conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "UPDATE events SET replay_count = replay_count + 1 WHERE event_id = ?",
            (event_id,),
        )

    def mark_quarantine(
        self, conn: sqlite3.Connection, event_ids: Iterable[str], flag: int
    ) -> int:
        ids = list(event_ids)
        if not ids:
            return 0
        conn.executemany(
            "UPDATE events SET quarantined = ? WHERE event_id = ?",
            [(flag, event_id) for event_id in ids],
        )
        return len(ids)

    def replace_chain_anomalies(
        self, conn: sqlite3.Connection, records: Iterable[dict[str, Any]]
    ) -> None:
        """以本次审计结果为准重写异常台账（纯数据派生，可重复执行）。"""
        conn.execute("DELETE FROM chain_anomalies")
        conn.executemany(
            """
            INSERT INTO chain_anomalies (root_event_id, airport_code,
                                         reasons_json, detected_at)
            VALUES (:root_event_id, :airport_code, :reasons_json, :detected_at)
            """,
            list(records),
        )


class _Transaction:
    """管理 BEGIN IMMEDIATE、COMMIT 与 ROLLBACK 的事务上下文。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
