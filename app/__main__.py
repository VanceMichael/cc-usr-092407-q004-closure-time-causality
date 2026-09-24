"""应用入口：加载夹具、打开数据库并启动 HTTP 服务。"""

from __future__ import annotations

import sys

from app.config import Config, load_airports, load_flights
from app.repository import Repository
from app.server import build_server
from app.service import DisruptionService


def main() -> int:
    config = Config.from_env()
    airports = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airports)
    repo = Repository(config.db_path)
    service = DisruptionService(repo, airports, flights)

    # 启动检查：审计全部已持久化事件链。判定只依赖事件内容，因此容器重建后
    # 得到相同结论；异常链被标记并从当前汇总隔离，原始记录保留只读追溯。
    anomalies = service.audit_chains()
    if anomalies:
        print(
            f"startup audit: {len(anomalies)} anomalous chain(s) quarantined "
            "(preserved for read-only trace, excluded from current summaries):",
            flush=True,
        )
        for anomaly in anomalies:
            print(
                f"  - group={anomaly['group_id']} airport={anomaly['airport_code']} "
                f"reasons={','.join(anomaly['reasons'])} "
                f"members={','.join(anomaly['members'])}",
                flush=True,
            )
    else:
        print("startup audit: no anomalous event chains", flush=True)

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
