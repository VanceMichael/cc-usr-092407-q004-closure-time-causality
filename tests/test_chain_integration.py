"""时间因果校验的服务级集成测试。

覆盖接入拒绝（不写事件/影响/重放计数）、合法端点相接、启动审计对历史异常
链的隔离与只读追溯、容器重建后判定一致，以及异常链不污染同机场正常链汇总。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.errors import EventConflictError, ValidationError
from app.models import DisruptionEvent
from app.validation import validate_event
from tests.support import ServiceTestCase, base_event


def close(event_id="evt-close0000001", airport="APS", **kw) -> dict:
    payload = base_event(event_id=event_id, airport_code=airport)
    payload.update(kw)
    return payload


def chain_event(event_id, event_type, supersedes, version, airport="APS", **kw) -> dict:
    payload = {
        "event_id": event_id,
        "event_version": version,
        "event_type": event_type,
        "airport_code": airport,
        "effective_from": kw.pop("effective_from", "2026-09-07T15:50:00Z"),
        "effective_until": kw.pop("effective_until", None),
        "reported_at": kw.pop("reported_at", "2026-09-07T15:00:00Z"),
        "supersedes_event_id": supersedes,
    }
    payload.update(kw)
    return payload


def extend(event_id, supersedes, version=2, airport="APS", **kw) -> dict:
    kw.setdefault("effective_until", "2026-09-07T20:00:00Z")
    return chain_event(event_id, "airport.extended", supersedes, version, airport, **kw)


def reopen(event_id, supersedes, version=2, airport="APS", **kw) -> dict:
    return chain_event(event_id, "airport.reopened", supersedes, version, airport, **kw)


class CausalRejectionTest(ServiceTestCase):
    def _issues(self, ctx):
        return {e["issue"]: e for e in ctx.exception.details["errors"]}

    def test_reopen_before_root_closure_rejected_and_points_at_field(self) -> None:
        self.service.submit_event(close())
        bad = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T14:30:00Z",  # 早于根关闭 15:00Z
            reported_at="2026-09-07T14:31:00Z",
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(bad)
        issues = self._issues(ctx)
        self.assertIn("reopen_before_chain_start", issues)
        self.assertEqual(issues["reopen_before_chain_start"]["field"], "effective_from")

    def test_extension_before_root_rejected(self) -> None:
        self.service.submit_event(close())
        bad = extend(
            "evt-extend000001",
            "evt-close0000001",
            effective_from="2026-09-07T13:00:00Z",
            reported_at="2026-09-07T14:30:00Z",
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(bad)
        self.assertIn("must_not_precede_root_closure", self._issues(ctx))

    def test_reopen_buffer_overrun_rejected(self) -> None:
        # APS buffer 20min；窗口 15:00-16:00Z，reopen 15:50 -> resume 16:10 越界。
        self.service.submit_event(
            close(effective_until="2026-09-07T16:00:00Z")
        )
        bad = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T15:50:00Z",
            reported_at="2026-09-07T15:51:00Z",
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(bad)
        issues = self._issues(ctx)
        self.assertIn("reopen_buffer_extends_past_closure", issues)
        self.assertEqual(issues["reopen_buffer_extends_past_closure"]["field"], "effective_from")

    def test_reopen_point_after_window_end_rejected(self) -> None:
        self.service.submit_event(
            close(effective_until="2026-09-07T16:00:00Z")
        )
        bad = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T16:30:00Z",
            reported_at="2026-09-07T16:31:00Z",
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(bad)
        self.assertIn("reopen_outside_closure_window", self._issues(ctx))

    def test_rejected_event_writes_nothing_and_keeps_replay_count(self) -> None:
        self.service.submit_event(close())
        # 一次合法重放把计数推到 1。
        self.service.submit_event(close())
        before = self.service.event_status("evt-close0000001")
        self.assertEqual(before["processing"]["replay_count"], 1)

        bad = extend(
            "evt-extend000001",
            "evt-close0000001",
            effective_from="2026-09-07T13:00:00Z",
            reported_at="2026-09-07T14:30:00Z",
        )
        with self.assertRaises(ValidationError):
            self.service.submit_event(bad)

        # 失败事件没有任何行。
        self.assertNotIn("evt-extend000001", self._all_event_ids())
        # 根的影响与重放计数不被失败事务影响。
        after = self.service.event_status("evt-close0000001")
        self.assertEqual(after["impacts"], before["impacts"])
        self.assertEqual(after["processing"]["replay_count"], 1)

    def test_branch_rejected(self) -> None:
        self.service.submit_event(close())
        first = extend("evt-extend000001", "evt-close0000001",
                       effective_from="2026-09-07T18:00:00Z",
                       reported_at="2026-09-07T17:00:00Z")
        self.service.submit_event(first)
        second = extend("evt-extend000002", "evt-close0000001", version=3,
                        effective_from="2026-09-07T18:30:00Z",
                        effective_until="2026-09-07T21:00:00Z",
                        reported_at="2026-09-07T17:30:00Z")
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(second)
        self.assertIn("chain_already_extended", self._issues(ctx))

    def _all_event_ids(self) -> set[str]:
        return {r["event_id"] for r in self.repo.all_events()}


class EndpointAbutmentTest(ServiceTestCase):
    def test_extension_starting_exactly_at_prior_end_accepted(self) -> None:
        self.service.submit_event(
            close(effective_until="2026-09-07T16:00:00Z")
        )
        ok = extend(
            "evt-extend000001",
            "evt-close0000001",
            effective_from="2026-09-07T16:00:00Z",  # 与上一窗口末端相接
            effective_until="2026-09-07T18:00:00Z",
            reported_at="2026-09-07T15:30:00Z",
        )
        result = self.service.submit_event(ok)
        self.assertEqual(result["processing_state"], "processed")

    def test_reopen_resume_exactly_at_window_end_accepted(self) -> None:
        # APS buffer 20；reopen 15:40 -> resume 16:00 恰好等于窗口末，合法。
        self.service.submit_event(
            close(effective_until="2026-09-07T16:00:00Z")
        )
        ok = reopen(
            "evt-reopen000001",
            "evt-close0000001",
            effective_from="2026-09-07T15:40:00Z",
            reported_at="2026-09-07T15:41:00Z",
        )
        result = self.service.submit_event(ok)
        self.assertEqual(result["processing_state"], "processed")


class DaylightSavingIntakeTest(ServiceTestCase):
    """通过真实接入路径验证机场时区 + DST 重复时刻的规范化。"""

    def setUp(self) -> None:
        super().setUp()
        # 固定服务时钟到事件发生当时，避免未来/迟报规则干扰因果判定。
        self.service = type(self.service)(
            self.repo,
            self.airports,
            self.flights,
            clock=lambda: datetime(2026, 10, 25, 2, 0, 0, tzinfo=timezone.utc),
        )

    def _post(self, **overrides) -> dict:
        payload = {
            "event_id": "evt-ber-close0001",
            "event_version": 1,
            "event_type": "airport.closed",
            "airport_code": "BER",
            "effective_from": "2026-10-25T00:00:00+02:00",
            "effective_until": "2026-10-25T02:30:00+02:00",  # 第一次 02:30 == 00:30Z
            "reported_at": "2026-10-24T23:30:00Z",
        }
        payload.update(overrides)
        return self.service.submit_event(payload)

    def test_repeated_local_hour_gap_vs_abut_through_intake(self) -> None:
        self._post()
        # 延长段从第二次 02:30（+01:00 == 01:30Z）起：距末端 00:30Z 有 60 分钟间隙。
        with self.assertRaises(ValidationError) as ctx:
            self._post(
                event_id="evt-ber-ext00001",
                event_version=2,
                event_type="airport.extended",
                effective_from="2026-10-25T02:30:00+01:00",
                effective_until="2026-10-25T03:30:00+01:00",
                reported_at="2026-10-25T01:31:00Z",
                supersedes_event_id="evt-ber-close0001",
            )
        issues = {e["issue"] for e in ctx.exception.details["errors"]}
        self.assertIn("extension_leaves_uncovered_gap", issues)

        # 同一本地墙钟字符串但带 +02:00（第一次 02:30 == 00:30Z），端点相接，合法。
        ok = self._post(
            event_id="evt-ber-ext00002",
            event_version=2,
            event_type="airport.extended",
            effective_from="2026-10-25T02:30:00+02:00",
            effective_until="2026-10-25T03:30:00+01:00",
            reported_at="2026-10-25T00:31:00Z",
            supersedes_event_id="evt-ber-close0001",
        )
        self.assertEqual(ok["processing_state"], "processed")


class ClockRuleServiceTest(ServiceTestCase):
    def _fixed_clock_service(self, now: datetime):
        self.service = type(self.service)(
            self.repo, self.airports, self.flights, clock=lambda: now
        )
        return self.service

    def test_future_reported_at_rejected(self) -> None:
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        svc = self._fixed_clock_service(now)
        bad = close(reported_at="2026-09-07T14:05:00Z")  # 领先 5 分钟
        with self.assertRaises(ValidationError) as ctx:
            svc.submit_event(bad)
        issues = {e["issue"]: e for e in ctx.exception.details["errors"]}
        self.assertIn("reported_at_in_future", issues)
        self.assertEqual(issues["reported_at_in_future"]["field"], "reported_at")

    def test_late_reported_at_rejected(self) -> None:
        now = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)
        svc = self._fixed_clock_service(now)
        bad = close(
            effective_from="2026-09-07T15:00:00Z",
            effective_until="2026-09-07T19:00:00Z",
            reported_at="2026-09-08T16:00:00Z",  # 晚于生效 25 小时
        )
        with self.assertRaises(ValidationError) as ctx:
            svc.submit_event(bad)
        self.assertIn("reported_too_late",
                      {e["issue"] for e in ctx.exception.details["errors"]})

    def test_reported_within_skew_accepted(self) -> None:
        now = datetime(2026, 9, 7, 14, 0, 30, tzinfo=timezone.utc)
        svc = self._fixed_clock_service(now)
        ok = close(reported_at="2026-09-07T14:01:00Z")  # 领先 30 秒，在 60 秒内
        self.assertEqual(svc.submit_event(ok)["processing_state"], "processed")


def _raw_insert(service, payload) -> DisruptionEvent:
    """绕过链式校验直接写入事件及其影响，模拟修复前已落库的历史异常数据。

    历史数据当时通过了（有缺陷的）旧校验，因此带着互相矛盾的影响快照一并
    存在库中；启动审计需要只凭事件内容重新判定并隔离它们。
    """
    from app.engine import compute_impacts
    from app.service import _row_to_event

    event = validate_event(payload, service._airports)
    with service._repo.transaction() as conn:
        root = event
        if event.supersedes_event_id:
            ref = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event.supersedes_event_id,),
            ).fetchone()
            root = service._resolve_root_from(conn, ref)
        impacts = compute_impacts(
            event,
            root,
            service._airports[event.airport_code],
            service._flights,
        )
        service._repo.insert_event(conn, event.to_dict())
        if impacts:
            service._repo.insert_impacts(conn, impacts)
    return event


class StartupAuditTest(ServiceTestCase):
    def _seed_anomalous_chain(self) -> None:
        # 根关闭 15:00-19:00Z（影响 3 个 APS 航班）；历史异常：恢复点 14:30Z
        # 早于根关闭——审计抽查到的第一类记录。
        _raw_insert(self.service, close())
        _raw_insert(
            self.service,
            reopen(
                "evt-reopen000001",
                "evt-close0000001",
                effective_from="2026-09-07T14:30:00Z",
                reported_at="2026-09-07T14:31:00Z",
            ),
        )

    def test_anomalous_chain_flagged_on_startup_and_excluded(self) -> None:
        self._seed_anomalous_chain()
        # 另一机场一条独立的正常链，必须仍然参与汇总（BSR 15:00-16:00Z 影响 BY205）。
        self.service.submit_event(
            close(
                event_id="evt-close-bsr0001",
                airport="BSR",
                effective_from="2026-09-07T15:00:00Z",
                effective_until="2026-09-07T16:00:00Z",
                reported_at="2026-09-07T14:00:00Z",
            )
        )

        # 启动检查（__main__ 在容器启动时执行同一调用）。
        anomalies = self.service.audit_chains()
        flagged = [a for a in anomalies if a["group_id"] == "evt-close0000001"]
        self.assertEqual(len(flagged), 1)
        self.assertIn("reopen_before_chain_start", flagged[0]["reasons"])
        self.assertEqual(
            set(flagged[0]["members"]),
            {"evt-close0000001", "evt-reopen000001"},
        )
        self.assertNotIn("evt-close-bsr0001",
                         {m for a in anomalies for m in a["members"]})

        # 异常链的矛盾影响不参与当前汇总。
        aps = self.service.affected_flights(
            airport="APS", status=None, limit=100, offset=0
        )
        self.assertEqual(aps["pagination"]["total"], 0)
        summary = self.service.airport_summary("APS")
        self.assertEqual(summary["quarantined_chains"], 1)
        self.assertEqual(summary["affected_flights"], 0)
        self.assertEqual(summary["event_count"], 0)

        # 正常链（BSR）不受影响，照常进入汇总。
        bsr = self.service.affected_flights(
            airport="BSR", status=None, limit=100, offset=0
        )
        self.assertEqual([f["flight_id"] for f in bsr["flights"]], ["BY-205-20260908"])
        self.assertEqual(self.service.airport_summary("BSR")["quarantined_chains"], 0)

        # 只读追溯仍可查到异常事件及其影响，并带隔离标记。
        status = self.service.event_status("evt-close0000001")
        self.assertEqual(status["chain_state"], "quarantined")
        self.assertEqual(status["chain_anomaly"]["group_id"], "evt-close0000001")
        self.assertEqual(status["processing"]["impact_count"], 3)

        # 隔离链冻结：不能再向其接入新事件。
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_event(
                extend(
                    "evt-extend000001",
                    "evt-close0000001",
                    effective_from="2026-09-07T18:00:00Z",
                    reported_at="2026-09-07T17:00:00Z",
                )
            )
        self.assertIn(
            "chain_quarantined",
            {e["issue"] for e in ctx.exception.details["errors"]},
        )
        # 被冻结的接入不得写入。
        self.assertNotIn("evt-extend000001",
                         {r["event_id"] for r in self.repo.all_events()})

        self.assertEqual(self.service.chain_anomalies()["quarantined_chain_count"], 1)

    def test_quarantined_event_replay_is_read_only(self) -> None:
        self._seed_anomalous_chain()
        self.service.audit_chains()
        before = self.service.event_status("evt-close0000001")

        # Identical retry returns the preserved result but must not mutate the
        # frozen chain's replay_count.
        result = self.service.submit_event(close())
        self.assertEqual(result["processing_state"], "replayed")
        after = self.service.event_status("evt-close0000001")
        self.assertEqual(
            after["processing"]["replay_count"], before["processing"]["replay_count"]
        )

    def test_audit_verdict_identical_after_container_rebuild(self) -> None:
        self._seed_anomalous_chain()
        first = self.service.audit_chains()

        # 模拟容器重建：关闭并以同一数据库文件重新打开（__main__ 会再审计）。
        self.restart_service()
        second = self.service.audit_chains()

        self.assertEqual(
            [(a["group_id"], a["reasons"], sorted(a["members"]), a["detected_at"])
             for a in first],
            [(a["group_id"], a["reasons"], sorted(a["members"]), a["detected_at"])
             for a in second],
        )
        # 隔离集合在重建后仍生效。
        self.assertTrue(self.repo.is_quarantined("evt-reopen000001"))
        summary = self.service.airport_summary("APS")
        self.assertEqual(summary["quarantined_chains"], 1)
        self.assertEqual(summary["affected_flights"], 0)
        # 只读追溯在重建后仍保留。
        self.assertEqual(
            self.service.event_status("evt-close0000001")["chain_state"],
            "quarantined",
        )

    def test_clean_database_audits_without_anomalies(self) -> None:
        self.service.submit_event(close())
        self.service.submit_event(
            reopen(
                "evt-reopen000001",
                "evt-close0000001",
                effective_from="2026-09-07T15:10:00Z",
                reported_at="2026-09-07T15:12:00Z",
            )
        )
        self.assertEqual(self.service.audit_chains(), [])
        self.assertEqual(self.service.chain_anomalies()["quarantined_chain_count"], 0)


if __name__ == "__main__":
    unittest.main()
