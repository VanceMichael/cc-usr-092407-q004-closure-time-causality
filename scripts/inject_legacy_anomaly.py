#!/usr/bin/env python3
"""向运行中服务的 SQLite 卷直接注入一条修复前版本才会接受的异常遗留链。

仅用于容器自检：绕过服务层校验，用仓储层原语写入
``airport.closed`` + 恢复点早于根关闭的 ``airport.reopened``。重启后启动
审计必须把整条链标记为隔离（quarantined），排除出当前汇总，同时保留只读
追溯。脚本不经过 HTTP，也不连接任何外部服务。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, load_airports, load_flights
from app.engine import compute_impacts
from app.models import DisruptionEvent
from app.repository import Repository

LEGACY_ROOT = "volc-legacy-close1"
LEGACY_REOPEN = "volc-legacy-reopn1"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def main(db_path: str = "/data/disruptions.db") -> int:
    config = Config.from_env()
    airports = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airports)
    repo = Repository(Path(sys.argv[1] if len(sys.argv) > 1 else db_path))

    existing = repo.get_event_row(LEGACY_ROOT)
    if existing is not None:
        print(f"legacy anomaly chain {LEGACY_ROOT} already present; skipping")
        repo.close()
        return 0

    root = DisruptionEvent(
        event_id=LEGACY_ROOT,
        event_version=1,
        event_type="airport.closed",
        airport_code="KTA",
        effective_from=_ts("2026-09-07T16:30:00+00:00"),
        effective_until=_ts("2026-09-07T18:00:00+00:00"),
        reported_at=_ts("2026-09-07T16:00:00+00:00"),
        supersedes_event_id=None,
        reason="legacy pre-fix closure",
    )
    # 因果倒置：恢复点（09-07 15:00Z）早于根关闭起点（16:30Z）。
    bad_reopen = DisruptionEvent(
        event_id=LEGACY_REOPEN,
        event_version=2,
        event_type="airport.reopened",
        airport_code="KTA",
        effective_from=_ts("2026-09-07T15:00:00+00:00"),
        effective_until=None,
        reported_at=_ts("2026-09-07T14:55:00+00:00"),
        supersedes_event_id=LEGACY_ROOT,
        reason=None,
    )

    impacts = compute_impacts(root, root, airports["KTA"], flights)
    with repo.transaction() as conn:
        repo.insert_event(conn, root.to_dict())
        if impacts:
            repo.insert_impacts(conn, impacts)
        repo.insert_event(conn, bad_reopen.to_dict())

    print(
        f"injected anomalous legacy chain {LEGACY_ROOT} -> {LEGACY_REOPEN} "
        f"({len(impacts)} impact rows)"
    )
    repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
