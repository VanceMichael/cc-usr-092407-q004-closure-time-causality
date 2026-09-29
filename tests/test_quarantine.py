"""启动审计、异常链隔离、只读追溯与重建确定性测试。"""

from __future__ import annotations

from datetime import datetime

from app.models import DisruptionEvent
from tests.support import ServiceTestCase, base_event


def close(event_id="evt-close0000001", **kw):
    payload = base_event(event_id=event_id)
    payload.update(kw)
    return payload


def reopen(event_id, supersedes, version=2, **kw):
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": "airport.reopened",
        "airport_code": "APS",
        "effective_from": kw.pop("effective_from", "2026-09-07T16:00:00Z"),
        "reported_at": kw.pop("reported_at", "2026-09-07T15:00:00Z"),
        "effective_until": None,
        "supersedes_event_id": supersedes,
    }
    payload.update(kw)
    return payload


def extend(event_id, supersedes, version=2, **kw):
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": "airport.extended",
        "airport_code": "APS",
        "effective_from": kw.pop("effective_from", "2026-09-07T18:55:00Z"),
        "effective_until": kw.pop("effective_until", "2026-09-07T20:00:00Z"),
        "reported_at": kw.pop("reported_at", "2026-09-07T18:00:00Z"),
        "supersedes_event_id": supersedes,
    }
    payload.update(kw)
    return payload


class CausalityRejectionTest(ServiceTestCase):
    """两类被审计抽查到的记录在接入时就必须被拒绝。"""

    def _issues(self, payload):
        from app.errors import ValidationError

        try:
            self.service.submit_event(payload)
        except ValidationError as exc:
            return [(e["field"], e["issue"]) for e in exc.details["errors"]]
        raise AssertionError("payload was accepted")

    def test_reopen_before_original_closure_rejected(self) -> None:
        self.service.submit_event(close())
        bad = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T14:00:00Z",
            reported_at="2026-09-07T13:55:00Z",
        )
        issues = self._issues(bad)
        self.assertIn(("effective_from", "reopen_before_closure_start"), issues)

    def test_reopen_buffer_end_past_window_rejected(self) -> None:
        self.service.submit_event(
            close(effective_until="2026-09-07T19:00:00Z")
        )
        # 18:50 + APS 20 分钟缓冲 => 19:10，越过当前窗口末端 19:00。
        bad = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T18:50:00Z",
            reported_at="2026-09-07T18:40:00Z",
        )
        issues = self._issues(bad)
        self.assertIn(("effective_from", "resume_after_closure_end"), issues)
        detail = [
            e
            for e in self._issues(bad)
            if e[1] == "resume_after_closure_end"
        ]
        self.assertTrue(detail)

    def test_extension_before_chain_start_rejected(self) -> None:
        self.service.submit_event(close())
        bad = extend(
            "evt-extend000001",
            "evt-close0000001",
            effective_from="2026-09-07T14:30:00Z",
            effective_until="2026-09-07T20:00:00Z",
            reported_at="2026-09-07T14:00:00Z",
        )
        issues = self._issues(bad)
        self.assertIn(("effective_from", "must_not_precede_root_closure"), issues)

    def test_future_reported_at_rejected(self) -> None:
        bad = close("evt-close0000009", reported_at="2030-01-01T00:00:00Z")
        issues = self._issues(bad)
        self.assertIn(("reported_at", "reported_at_in_future"), issues)

    def test_rejected_event_writes_nothing(self) -> None:
        self.service.submit_event(close())
        with self.assertRaises(Exception):
            self.service.submit_event(
                reopen(
                    "evt-reopen000001",
                    "evt-close0000001",
                    effective_from="2026-09-07T14:00:00Z",
                    reported_at="2026-09-07T13:55:00Z",
                )
            )
        from app.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.service.event_status("evt-reopen000001")
        # 根事件重放计数不受失败提交影响。
        root = self.service.event_status("evt-close0000001")
        self.assertEqual(root["processing"]["replay_count"], 0)

    def test_valid_touching_endpoints_accepted(self) -> None:
        # 窗口 15:00-19:00，APS 缓冲 20 分钟；18:40 恢复 => 19:00 恰好相接。
        self.service.submit_event(close())
        result = self.service.submit_event(
            reopen(
                "evt-reopen000001",
                "evt-close0000001",
                effective_from="2026-09-07T18:40:00Z",
                reported_at="2026-09-07T18:30:00Z",
            )
        )
        self.assertEqual(result["processing_state"], "processed")


def _insert_raw(repo, event: DisruptionEvent) -> None:
    """绕过服务校验直接写库，模拟修复前版本留下的异常历史数据。"""
    with repo.transaction() as conn:
        repo.insert_event(conn, event.to_dict())


def _event(event_id, event_type, version, frm, until, reported, supersedes, airport="APS"):
    return DisruptionEvent(
        event_id=event_id,
        event_version=version,
        event_type=event_type,
        airport_code=airport,
        effective_from=datetime.fromisoformat(frm),
        effective_until=datetime.fromisoformat(until) if until else None,
        reported_at=datetime.fromisoformat(reported),
        supersedes_event_id=supersedes,
        reason=None,
    )


class StartupQuarantineTest(ServiceTestCase):
    def _seed_valid_and_anomalous_legacy(self) -> None:
        # 一条健康链（恢复点合法）。
        self.service.submit_event(
            close(
                "evt-goodclose001",
                effective_from="2026-09-07T15:00:00Z",
                effective_until="2026-09-07T19:00:00Z",
            )
        )
        # 直接写库的异常遗留链：恢复点早于根关闭。
        _insert_raw(
            self.repo,
            _event(
                "evt-badclose0001",
                "airport.closed",
                1,
                "2026-09-06T15:00:00+00:00",
                "2026-09-06T19:00:00+00:00",
                "2026-09-06T14:00:00+00:00",
                None,
            ),
        )
        _insert_raw(
            self.repo,
            _event(
                "evt-badreopen0001",
                "airport.reopened",
                2,
                "2026-09-06T14:00:00+00:00",
                None,
                "2026-09-06T13:55:00+00:00",
                "evt-badclose0001",
            ),
        )

    def test_startup_audit_flags_and_excludes_anomalous_chain(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        report = self.service.audit_stored_chains()
        self.assertEqual(report["quarantined_chains"], 1)
        self.assertEqual(report["quarantined_events"], 2)
        issues = [
            (v["event_id"], v["issue"])
            for chain in report["chains"]
            for v in chain["violations"]
        ]
        self.assertIn(("evt-badreopen0001", "reopen_before_closure_start"), issues)

        # 当前汇总只含健康链，异常链不参与。
        summary = self.service.airport_summary("APS")
        self.assertEqual(summary["quarantined_event_count"], 2)
        self.assertEqual(summary["quarantined_chains"], ["evt-badclose0001"])
        affected = self.service.affected_flights(
            airport="APS", status=None, limit=50, offset=0
        )
        # 健康链 15:00-19:00 影响 3 个航班；异常链窗口被排除。
        self.assertEqual(affected["pagination"]["total"], 3)

    def test_quarantined_events_remain_readable(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        self.service.audit_stored_chains()
        status = self.service.event_status("evt-badreopen0001")
        self.assertEqual(status["processing"]["state"], "quarantined")
        self.assertIsNotNone(status["anomaly"])
        self.assertEqual(status["anomaly"]["root_event_id"], "evt-badclose0001")
        # 事件本体仍可只读追溯。
        self.assertEqual(status["event"]["event_id"], "evt-badreopen0001")

    def test_replay_of_quarantined_event_does_not_bump_counter(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        self.service.audit_stored_chains()
        payload = self.service.event_status("evt-badclose0001")["event"]
        before = self.service.event_status("evt-badclose0001")["processing"]["replay_count"]
        result = self.service.submit_event(payload)
        after = self.service.event_status("evt-badclose0001")["processing"]["replay_count"]
        self.assertEqual(result["processing_state"], "quarantined")
        self.assertEqual(before, after)

    def test_cannot_build_new_events_on_quarantined_chain(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        self.service.audit_stored_chains()
        bad = extend(
            "evt-extendbad001",
            "evt-badclose0001",
            version=3,
            effective_from="2026-09-06T18:55:00Z",
            effective_until="2026-09-06T21:00:00Z",
            reported_at="2026-09-06T18:00:00Z",
        )
        from app.errors import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(bad)
        issues = [e["issue"] for e in ctx.exception.details["errors"]]
        self.assertIn("cannot_extend_quarantined_chain", issues)

    def test_verdict_stable_across_restart_and_rebuild(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        first = self.service.audit_stored_chains()
        self.restart_service()
        second = self.service.audit_stored_chains()
        self.restart_service()
        third = self.service.audit_stored_chains()
        self.assertEqual(
            (first["quarantined_chains"], first["quarantined_events"]),
            (second["quarantined_chains"], second["quarantined_events"]),
        )
        self.assertEqual(
            (second["quarantined_chains"], second["quarantined_events"]),
            (third["quarantined_chains"], third["quarantined_events"]),
        )
        summary = self.service.airport_summary("APS")
        self.assertEqual(summary["quarantined_event_count"], 2)

    def test_repairing_data_lifts_quarantine(self) -> None:
        self._seed_valid_and_anomalous_legacy()
        self.service.audit_stored_chains()
        # 纠正：把异常恢复点改为合法时刻（模拟权威更正后的数据修复）。
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE events SET effective_from = ?, payload_json = ? "
                "WHERE event_id = ?",
                (
                    "2026-09-06T18:40:00Z",
                    __import__("json").dumps(
                        {
                            **self.service.event_status("evt-badreopen0001")["event"],
                            "effective_from": "2026-09-06T18:40:00Z",
                        },
                        sort_keys=True,
                    ),
                    "evt-badreopen0001",
                ),
            )
        report = self.service.audit_stored_chains()
        self.assertEqual(report["quarantined_chains"], 0)
        status = self.service.event_status("evt-badreopen0001")
        self.assertEqual(status["processing"]["state"], "processed")


if __name__ == "__main__":
    import unittest

    unittest.main()
