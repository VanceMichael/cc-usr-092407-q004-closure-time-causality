"""应用入口：加载夹具、打开数据库、审计既有事件链并启动 HTTP 服务。"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from app.config import Config, load_airports, load_flights
from app.repository import Repository
from app.server import build_server
from app.service import DisruptionService
from app.timeutil import parse_event_datetime


def _now_provider():
    """允许自检通过 DISRUPTION_NOW（带时区 ISO 8601）固定服务器时钟。

    生产环境不设置该变量，使用真实 UTC 时钟。
    """
    override = os.environ.get("DISRUPTION_NOW")
    if override:
        return parse_event_datetime(override, "DISRUPTION_NOW")
    return datetime.now(timezone.utc)


def main() -> int:
    config = Config.from_env()
    airports = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airports)
    repo = Repository(config.db_path)
    service = DisruptionService(
        repo, airports, flights, now_provider=_now_provider
    )

    # 启动检查：用纯数据规则重算既有事件链的时间因果，标记并隔离异常链。
    # 判定不依赖运行时刻状态，容器重建后结果一致；异常数据只读保留可追溯。
    report = service.audit_stored_chains()
    if report["quarantined_chains"]:
        print(
            f"startup audit: {report['quarantined_chains']} anomalous chain(s) "
            f"({report['quarantined_events']} event(s)) quarantined and excluded "
            "from current aggregates; data retained read-only",
            flush=True,
        )
        for chain in report["chains"]:
            for violation in chain["violations"]:
                print(
                    f"  - root={chain['root_event_id']} "
                    f"event={violation['event_id']} field={violation['field']} "
                    f"issue={violation['issue']}",
                    flush=True,
                )

    server = build_server(config.host, config.port, service)
    print(
        f"airport-disruption service listening on {config.host}:{config.port} "
        f"(db={config.db_path}, airports={len(airports)}, flights={len(flights)})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
