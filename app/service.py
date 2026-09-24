"""处理事件接入语义、持久化协调与业务查询的应用服务。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from app.chain_rules import (
    audit_stored,
    edge_violations,
    timing_violations,
)
from app.errors import EventConflictError, NotFoundError, ValidationError
from app.engine import compute_impacts
from app.models import (
    EVENT_CLOSED,
    EVENT_REOPENED,
    Airport,
    DisruptionEvent,
    Flight,
    iso_utc,
)
from app.repository import Repository
from app.validation import validate_event


class DisruptionService:
    def __init__(
        self,
        repo: Repository,
        airports: dict[str, Airport],
        flights: dict[str, Flight],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._repo = repo
        self._airports = airports
        self._flights = flights
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def healthy(self) -> bool:
        return self._repo.ping()

    # ------------------------------------------------------------------ #
    # Startup audit
    # ------------------------------------------------------------------ #

    def audit_chains(self) -> list[dict[str, Any]]:
        """重新审计全部已持久化事件链并落定异常标记。

        判定只依赖事件内容，结论确定性：同一数据库在任意时间、容器重建后
        重新审计得到完全相同的分组、原因和成员。异常链保留原始事件/影响
        供只读追溯，但从当前汇总中隔离。
        """
        rows = self._repo.all_events()
        events = {row["event_id"]: _row_to_event(row) for row in rows}
        anomalies = audit_stored(events, self._airports)
        payload = [
            {
                "group_id": anomaly.group_id,
                "airport_code": anomaly.airport_code,
                "reasons": list(anomaly.reasons),
                "members": sorted(anomaly.members),
                "violations": [dict(v) for v in anomaly.violations],
                "detected_at": iso_utc(anomaly.detected_at),
            }
            for anomaly in anomalies
        ]
        with self._repo.transaction() as conn:
            self._repo.replace_anomalies(conn, payload)
        self._repo.refresh_quarantine_set()
        return payload

    # ------------------------------------------------------------------ #
    # Event intake
    # ------------------------------------------------------------------ #

    def submit_event(self, payload: Any) -> dict[str, Any]:
        # Structural + basic semantic validation happens before any DB write.
        event = validate_event(payload, self._airports)

        with self._repo.transaction() as conn:
            existing = conn.execute(
                "SELECT event_id, event_version, payload_json FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()

            if existing is not None:
                return self._handle_duplicate(conn, event, existing)

            self._validate_chain(conn, event)
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
        stored_version = existing["event_version"]
        stored_payload = json.loads(existing["payload_json"])

        same_body = stored_payload == event.to_dict()
        if same_body:
            # Idempotent retry: return the original result. Quarantined chains
            # are frozen read-only traces, so their replay_count is immutable;
            # the original result is still returned unchanged.
            impacts = self._repo.get_impacts(event.event_id)
            if not self._repo.is_quarantined(event.event_id):
                self._repo.increment_replay(conn, event.event_id)
            return self._result(event, [dict(r) for r in impacts], replayed=True)

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
            },
        )

    def _validate_chain(self, conn, event: DisruptionEvent) -> None:
        """校验必须结合数据库现状判断的事件链规则。

        所有违规在一个事务内收集后抛出，异常会回滚整个事务，因此校验失败
        不会写入事件、影响，也不会推进任何重放计数。
        """
        errors: list[dict[str, str]] = []
        airport = self._airports[event.airport_code]

        # Clock-dependent reporting rules apply to every intake event.
        errors.extend(timing_violations(event, airport, now=self._clock()))

        if event.event_type == EVENT_CLOSED:
            row = conn.execute(
                "SELECT MAX(event_version) AS max_version FROM events "
                "WHERE airport_code = ?",
                (event.airport_code,),
            ).fetchone()
            max_version = row["max_version"]
            if max_version is not None and event.event_version <= max_version:
                errors.append(
                    {
                        "field": "event_version",
                        "issue": "must_extend_airport_history",
                        "stored_version": str(max_version),
                        "received_version": str(event.event_version),
                    }
                )
            if errors:
                raise ValidationError("Event failed chain validation", {"errors": errors})
            return

        # extended / reopened must reference a prior event
        ref_id = event.supersedes_event_id
        if ref_id is None:
            errors.append(
                {
                    "field": "supersedes_event_id",
                    "issue": "required_for_chain_event",
                }
            )
            raise ValidationError("Event failed chain validation", {"errors": errors})

        ref = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ref_id,)
        ).fetchone()
        if ref is None:
            errors.append(
                {
                    "field": "supersedes_event_id",
                    "issue": "unknown_event",
                    "event_id": ref_id,
                }
            )
        elif self._repo.is_quarantined(ref_id):
            # Anomalous chains are frozen: no new events may attach to them.
            errors.append(
                {
                    "field": "supersedes_event_id",
                    "issue": "chain_quarantined",
                    "event_id": ref_id,
                }
            )
        elif ref["airport_code"] != event.airport_code:
            errors.append(
                {
                    "field": "supersedes_event_id",
                    "issue": "airport_mismatch",
                    "referenced_airport": ref["airport_code"],
                    "received_airport": event.airport_code,
                }
            )
        else:
            if event.event_version <= ref["event_version"]:
                errors.append(
                    {
                        "field": "event_version",
                        "issue": "version_must_increase",
                        "stored_version": str(ref["event_version"]),
                        "received_version": str(event.event_version),
                    }
                )
            # No branching: a referenced event may have at most one successor.
            sibling = conn.execute(
                "SELECT event_id FROM events WHERE supersedes_event_id = ?",
                (ref_id,),
            ).fetchone()
            if sibling is not None:
                errors.append(
                    {
                        "field": "supersedes_event_id",
                        "issue": "chain_already_extended",
                        "event_id": ref_id,
                        "existing_successor": sibling["event_id"],
                    }
                )
            root = self._resolve_root_from(conn, ref)
            if root is not None:
                errors.extend(
                    edge_violations(event, _row_to_event(ref), root, airport)
                )

        if errors:
            raise ValidationError("Event failed chain validation", {"errors": errors})

    def _resolve_root(self, conn, event: DisruptionEvent) -> DisruptionEvent:
        if event.event_type == EVENT_CLOSED:
            return event
        if event.supersedes_event_id is None:
            raise ValidationError(
                "Event failed chain validation",
                {"errors": [
                    {"field": "supersedes_event_id", "issue": "required_for_chain_event"}
                ]},
            )
        ref = conn.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event.supersedes_event_id,),
        ).fetchone()
        if ref is None:  # validated earlier; defensive
            raise ValidationError("unknown superseded event")
        root = self._resolve_root_from(conn, ref)
        if root is None:
            raise ValidationError("extended/reopened event chain has no closed root")
        return root

    def _resolve_root_from(self, conn, row) -> DisruptionEvent | None:
        """从某条已存储事件行出发，沿 supersedes 链找到 closed 根。"""
        seen: set[str] = set()
        current = _row_to_event(row)
        while current.event_type != EVENT_CLOSED:
            ref_id = current.supersedes_event_id
            if ref_id is None:
                return None
            if ref_id in seen:  # defensive; cycles are structurally prevented
                return None
            seen.add(ref_id)
            parent = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (ref_id,)
            ).fetchone()
            if parent is None:
                return None
            current = _row_to_event(parent)
        return current

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def chain_anomalies(self) -> dict[str, Any]:
        anomalies = self._repo.get_anomalies()
        return {
            "quarantined_chain_count": len(anomalies),
            "quarantined_event_count": len(self._repo.quarantined_event_ids),
            "chains": anomalies,
        }

    def event_status(self, event_id: str) -> dict[str, Any]:
        row = self._repo.get_event_row(event_id)
        if row is None:
            raise NotFoundError(
                f"Event '{event_id}' was not found", {"event_id": event_id}
            )
        impact_rows = self._repo.get_impacts(event_id)
        impacts = [self._impact_dict(r) for r in impact_rows]
        active = [i for i in impacts if i["impact_status"] != "resolved"]
        statuses: dict[str, int] = {}
        passengers = 0
        for imp in active:
            statuses[imp["impact_status"]] = statuses.get(imp["impact_status"], 0) + 1
            passengers += imp["passenger_count"]

        anomaly = self._repo.anomaly_for_event(event_id)
        if anomaly is not None:
            chain_state = "quarantined"
        elif row["event_type"] == EVENT_REOPENED:
            chain_state = "closed"
        else:
            chain_state = "active"

        result = {
            "event": json.loads(row["payload_json"]),
            "processing": {
                "state": "processed",
                "replay_count": row["replay_count"],
                "created_at": row["created_at"],
                "impact_count": len(active),
                "resolved_count": len(impacts) - len(active),
                "affected_passengers": passengers,
                "status_breakdown": statuses,
            },
            "chain_state": chain_state,
            "impacts": active,
        }
        if anomaly is not None:
            # Read-only trace marker: the record is preserved verbatim but does
            # not participate in current summaries.
            result["chain_anomaly"] = {
                "group_id": anomaly["group_id"],
                "reasons": anomaly["reasons"],
                "detected_at": anomaly["detected_at"],
            }
        return result

    def airport_summary(self, airport_code: str) -> dict[str, Any]:
        if airport_code not in self._airports:
            raise NotFoundError(
                f"Unknown airport code '{airport_code}'",
                {"field": "airport_code", "received": airport_code},
            )
        rows = self._repo.events_for_airport(airport_code)
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
        reopen_count = sum(1 for r in rows if r["event_type"] == EVENT_REOPENED)
        quarantined = sum(
            1
            for anomaly in self._repo.get_anomalies()
            if anomaly["airport_code"] == airport_code
        )
        return {
            "airport_code": airport_code,
            "airport_name": self._airports[airport_code].name,
            "event_count": len(rows),
            "active_chains": len(chain_roots) - reopen_count,
            "quarantined_chains": quarantined,
            "affected_flights": len(latest),
            "affected_passengers": total_passengers,
            "by_status": by_status,
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
        self, event: DisruptionEvent, impacts: list[dict[str, Any]], *, replayed: bool
    ) -> dict[str, Any]:
        active_impacts = [i for i in impacts if i["impact_status"] != "resolved"]
        serialized = [self._impact_dict(i) for i in active_impacts]
        statuses: dict[str, int] = {}
        passengers = 0
        for imp in serialized:
            statuses[imp["impact_status"]] = statuses.get(imp["impact_status"], 0) + 1
            passengers += imp["passenger_count"]
        return {
            "event_id": event.event_id,
            "event_version": event.event_version,
            "processing_state": "replayed" if replayed else "processed",
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
