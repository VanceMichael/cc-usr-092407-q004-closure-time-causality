"""处理事件接入语义、持久化协调与业务查询的应用服务。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from app import chains
from app.chains import candidate_violations, report_timing
from app.errors import EventConflictError, NotFoundError, ValidationError
from app.engine import compute_impacts
from app.models import (
    EVENT_CLOSED,
    EVENT_REOPENED,
    Airport,
    DisruptionEvent,
    Flight,
)
from app.repository import Repository, utcnow_iso
from app.validation import validate_event


class DisruptionService:
    def __init__(
        self,
        repo: Repository,
        airports: dict[str, Airport],
        flights: dict[str, Flight],
        *,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self._repo = repo
        self._airports = airports
        self._flights = flights
        self._now_provider = now_provider or chains.utc_now

    @property
    def now(self) -> datetime:
        return self._now_provider()

    def healthy(self) -> bool:
        return self._repo.ping()

    def health_detail(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.healthy() else "degraded",
            "quarantined_chains": self._repo.anomaly_count(),
        }

    def audit_stored_chains(self) -> dict[str, Any]:
        """启动审计：按纯数据规则重算存量链的因果异常与隔离标记。

        判定只依赖事件本身（不依赖提交时刻的历史快照），因此容器重建后对
        同一数据库必然得到相同的隔离集合。审计结果幂等重写：异常台账与
        ``quarantined`` 标记都以本次审计为准，修复（纠正数据）后再次启动会
        自动解除隔离。被隔离事件只读保留，不参与当前汇总。
        """
        with self._repo.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY event_version, event_id"
            ).fetchall()
            events = [_row_to_event(row) for row in rows]
            violations = chains.audit_events(
                events,
                self._airports,
                now=self.now,
                check_reported_at=False,
            )
            flagged = chains.quarantined_event_ids(violations, events)
            currently = {row["event_id"] for row in rows if row["quarantined"]}
            self._repo.mark_quarantine(conn, sorted(flagged - currently), 1)
            self._repo.mark_quarantine(conn, sorted(currently - flagged), 0)

            groups = chains.chain_members(events)
            records: list[dict[str, Any]] = []
            summaries: list[dict[str, Any]] = []
            detected_at = utcnow_iso()
            for root_id in sorted(groups):
                members = groups[root_id]
                chain_violations: list[dict[str, str]] = []
                for member_id in sorted(members):
                    chain_violations.extend(
                        v.to_error() for v in violations.get(member_id, [])
                    )
                if not chain_violations:
                    continue
                root_row = conn.execute(
                    "SELECT airport_code FROM events WHERE event_id = ?",
                    (root_id,),
                ).fetchone()
                airport_code = root_row["airport_code"] if root_row else ""
                records.append(
                    {
                        "root_event_id": root_id,
                        "airport_code": airport_code,
                        "reasons_json": json.dumps(
                            chain_violations, sort_keys=True, ensure_ascii=False
                        ),
                        "detected_at": detected_at,
                    }
                )
                summaries.append(
                    {"root_event_id": root_id, "violations": chain_violations}
                )
            self._repo.replace_chain_anomalies(conn, records)

        return {
            "quarantined_chains": len(records),
            "quarantined_events": len(flagged),
            "chains": summaries,
        }

    # ------------------------------------------------------------------ #
    # Event intake
    # ------------------------------------------------------------------ #

    def submit_event(self, payload: Any) -> dict[str, Any]:
        # Structural + basic semantic validation happens before any DB write.
        event = validate_event(payload, self._airports)

        with self._repo.transaction() as conn:
            existing = conn.execute(
                "SELECT event_id, payload_json FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()

            if existing is not None:
                return self._handle_duplicate(conn, event, existing)

            prior_events = self._load_events(conn)
            violations = candidate_violations(
                event, prior_events, self._airports, now=self.now
            )
            if violations:
                raise ValidationError(
                    "Event failed chain causality validation",
                    {"errors": [v.to_error() for v in violations]},
                )

            root = self._resolve_root(conn, event)
            impacts = compute_impacts(
                event,
                root,
                self._airports[event.airport_code],
                self._flights,
            )
            impacts.extend(self._resolved_tombstones(conn, event, root, impacts))
            self._repo.insert_event(conn, event.to_dict())
            if impacts:
                self._repo.insert_impacts(conn, impacts)
            return self._result(event, impacts, replayed=False)

    @staticmethod
    def _load_events(conn) -> list[DisruptionEvent]:
        rows = conn.execute("SELECT * FROM events").fetchall()
        return [_row_to_event(row) for row in rows]

    def _resolved_tombstones(
        self, conn, event: DisruptionEvent, root: DisruptionEvent, impacts: list[dict]
    ) -> list[dict[str, Any]]:
        """返回本事件生效后不再受影响、但曾出现在同链中的航班。"""
        if event.event_type == EVENT_CLOSED:
            return []
        still_affected = {r["flight_id"] for r in impacts}
        tombstones: list[dict[str, Any]] = []
        prior_ids = self._repo.prior_chain_impact_ids(conn, root.event_id)
        for flight_id in sorted(prior_ids - still_affected):
            flight = self._flights.get(flight_id)
            if flight is None:
                continue
            tombstones.append(
                {
                    "event_id": event.event_id,
                    "root_event_id": root.event_id,
                    "airport_code": event.airport_code,
                    "flight_id": flight.flight_id,
                    "flight_number": flight.flight_number,
                    "affected_endpoint": "none",
                    "impact_status": "resolved",
                    "overlap_minutes": 0,
                    "delay_minutes": None,
                    "proposed_departure": None,
                    "proposed_arrival": None,
                    "passenger_count": flight.passenger_count,
                    "crosses_midnight": 0,
                }
            )
        return tombstones

    def _handle_duplicate(
        self, conn, event: DisruptionEvent, existing
    ) -> dict[str, Any]:
        stored_version_row = conn.execute(
            "SELECT event_version, payload_json, quarantined FROM events "
            "WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        stored_version = stored_version_row["event_version"]
        stored_payload = json.loads(stored_version_row["payload_json"])

        same_body = stored_payload == event.to_dict()
        if same_body:
            # Idempotent retry. Quarantined events are read-only historical
            # evidence: return the stored result without touching the counter.
            if not stored_version_row["quarantined"]:
                self._repo.increment_replay(conn, event.event_id)
            impacts = self._repo.get_impacts(event.event_id)
            return self._result(
                event,
                [dict(r) for r in impacts],
                replayed=True,
                quarantined=bool(stored_version_row["quarantined"]),
            )

        # Same identity, different content.
        if event.event_version == stored_version:
            raise EventConflictError(
                f"Event '{event.event_id}' version {stored_version} already exists "
                "with a different payload",
                {
                    "event_id": event.event_id,
                    "stored_version": stored_version,
                    "received_version": event.event_version,
                    "issue": "payload_mismatch",
                    "quarantined": bool(stored_version_row["quarantined"]),
                },
            )
        raise EventConflictError(
            f"Event '{event.event_id}' already exists at version {stored_version}; "
            "new versions must use a new event_id and reference the previous one "
            "via supersedes_event_id",
            {
                "event_id": event.event_id,
                "stored_version": stored_version,
                "received_version": event.event_version,
                "issue": "event_id_reuse",
                "quarantined": bool(stored_version_row["quarantined"]),
            },
        )

    def _resolve_root(self, conn, event: DisruptionEvent) -> DisruptionEvent:
        if event.event_type == EVENT_CLOSED:
            return event
        # Walk the supersedes chain to the originating closed event.
        seen: set[str] = set()
        current = event
        while current.supersedes_event_id is not None:
            ref_id = current.supersedes_event_id
            if ref_id in seen:  # defensive; cycles are structurally prevented
                raise ValidationError("supersedes chain contains a cycle")
            seen.add(ref_id)
            row = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (ref_id,)
            ).fetchone()
            if row is None:  # validated earlier; defensive
                raise ValidationError(f"unknown superseded event '{ref_id}'")
            current = _row_to_event(row)
            if current.event_type == EVENT_CLOSED:
                return current
        raise ValidationError("extended/reopened event chain has no closed root")

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def event_status(self, event_id: str) -> dict[str, Any]:
        row = self._repo.get_event_row(event_id)
        if row is None:
            raise NotFoundError(
                f"Event '{event_id}' was not found", {"event_id": event_id}
            )
        event = _row_to_event(row)
        impact_rows = self._repo.get_impacts(event_id)
        impacts = [self._impact_dict(r) for r in impact_rows]
        active = [i for i in impacts if i["impact_status"] != "resolved"]
        statuses: dict[str, int] = {}
        passengers = 0
        for imp in active:
            statuses[imp["impact_status"]] = statuses.get(imp["impact_status"], 0) + 1
            passengers += imp["passenger_count"]

        quarantined = bool(row["quarantined"])
        anomaly = self._anomaly_for_event(event_id) if quarantined else None
        response: dict[str, Any] = {
            "event": json.loads(row["payload_json"]),
            "processing": {
                # Quarantined history is retained read-only and never replayed
                # into current aggregates.
                "state": "quarantined" if quarantined else "processed",
                "replay_count": row["replay_count"],
                "created_at": row["created_at"],
                "impact_count": len(active),
                "resolved_count": len(impacts) - len(active),
                "affected_passengers": passengers,
                "status_breakdown": statuses,
                "reporting": report_timing(event, self.now),
            },
            "impacts": active,
        }
        if anomaly is not None:
            response["anomaly"] = anomaly
        return response

    def _anomaly_for_event(self, event_id: str) -> dict[str, Any] | None:
        """返回事件所属异常链的只读追溯信息（不属于该链时为 None）。"""
        root_id = self._root_id(event_id)
        if root_id is None:
            return None
        for row in self._repo.list_anomalies():
            if row["root_event_id"] == root_id:
                return {
                    "root_event_id": row["root_event_id"],
                    "detected_at": row["detected_at"],
                    "violations": json.loads(row["reasons_json"]),
                }
        return None

    def _root_id(self, event_id: str) -> str | None:
        seen: set[str] = set()
        current_id = event_id
        while current_id is not None:
            if current_id in seen:
                return None
            seen.add(current_id)
            row = self._repo.get_event_row(current_id)
            if row is None:
                return None
            if row["event_type"] == EVENT_CLOSED:
                return row["event_id"]
            current_id = row["supersedes_event_id"]
        return None

    def airport_summary(self, airport_code: str) -> dict[str, Any]:
        if airport_code not in self._airports:
            raise NotFoundError(
                f"Unknown airport code '{airport_code}'",
                {"field": "airport_code", "received": airport_code},
            )
        rows = self._repo.events_for_airport(airport_code)
        all_rows = self._repo.events_for_airport(
            airport_code, include_quarantined=True
        )
        latest = self._repo.latest_impacts(airport=airport_code)
        by_status: dict[str, dict[str, Any]] = {}
        total_passengers = 0
        for r in latest:
            bucket = by_status.setdefault(
                r["impact_status"],
                {"flight_count": 0, "passenger_count": 0, "flights": []},
            )
            bucket["flight_count"] += 1
            bucket["passenger_count"] += r["passenger_count"]
            total_passengers += r["passenger_count"]
            bucket["flights"].append(r["flight_id"])
        chain_roots = [r["event_id"] for r in rows if r["event_type"] == EVENT_CLOSED]
        quarantined_root_ids = {
            r["root_event_id"] for r in self._repo.list_anomalies()
            if r["airport_code"] == airport_code
        }
        return {
            "airport_code": airport_code,
            "airport_name": self._airports[airport_code].name,
            "event_count": len(rows),
            "active_chains": len(chain_roots)
            - sum(1 for r in rows if r["event_type"] == EVENT_REOPENED),
            "affected_flights": len(latest),
            "affected_passengers": total_passengers,
            "by_status": by_status,
            "quarantined_event_count": len(all_rows) - len(rows),
            "quarantined_chains": sorted(quarantined_root_ids),
        }

    def affected_flights(
        self,
        *,
        airport: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if airport is not None and airport not in self._airports:
            raise NotFoundError(
                f"Unknown airport code '{airport}'",
                {"field": "airport", "received": airport},
            )
        allowed = {"cancelled", "delayed", "pending_confirmation"}
        if status is not None and status not in allowed:
            raise ValidationError(
                "Unsupported impact status filter",
                {"field": "status", "allowed": sorted(allowed)},
            )
        rows = self._repo.latest_impacts(airport=airport, status=status)
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
            },
            "flights": [self._impact_dict(r) for r in page],
        }

    # ------------------------------------------------------------------ #
    # Serialization helpers
    # ------------------------------------------------------------------ #

    def _result(
        self,
        event: DisruptionEvent,
        impacts: list[dict[str, Any]],
        *,
        replayed: bool,
        quarantined: bool = False,
    ) -> dict[str, Any]:
        active_impacts = [i for i in impacts if i["impact_status"] != "resolved"]
        serialized = [self._impact_dict(i) for i in active_impacts]
        statuses: dict[str, int] = {}
        passengers = 0
        for imp in serialized:
            statuses[imp["impact_status"]] = statuses.get(imp["impact_status"], 0) + 1
            passengers += imp["passenger_count"]
        state = (
            "quarantined"
            if quarantined
            else ("replayed" if replayed else "processed")
        )
        return {
            "event_id": event.event_id,
            "event_version": event.event_id,
            "processing_state": state,
            "impact_count": len(serialized),
            "resolved_count": len(impacts) - len(active_impacts),
            "affected_passengers": passengers,
            "status_breakdown": statuses,
            "impacts": serialized,
        }

    @staticmethod
    def _impact_dict(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            row = dict(row)
        return {
            "event_id": row["event_id"],
            "root_event_id": row["root_event_id"],
            "airport_code": row["airport_code"],
            "flight_id": row["flight_id"],
            "flight_number": row["flight_number"],
            "affected_endpoint": row["affected_endpoint"],
            "impact_status": row["impact_status"],
            "overlap_minutes": row["overlap_minutes"],
            "delay_minutes": row["delay_minutes"],
            "proposed_departure": row["proposed_departure"],
            "proposed_arrival": row["proposed_arrival"],
            "passenger_count": row["passenger_count"],
            "crosses_midnight": bool(row["crosses_midnight"]),
        }


def parse_ts(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _row_to_event(row) -> DisruptionEvent:
    return DisruptionEvent(
        event_id=row["event_id"],
        event_version=row["event_version"],
        event_type=row["event_type"],
        airport_code=row["airport_code"],
        effective_from=parse_ts(row["effective_from"]),
        effective_until=parse_ts(row["effective_until"]) if row["effective_until"] else None,
        reported_at=parse_ts(row["reported_at"]),
        supersedes_event_id=row["supersedes_event_id"],
        reason=row["reason"],
    )
