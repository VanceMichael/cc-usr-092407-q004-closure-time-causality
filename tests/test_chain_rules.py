"""事件链时间因果规则的纯函数测试。

覆盖：延长段早于根关闭、跨过开放间隙、端点相接、恢复早于根、恢复缓冲越过
窗口末端、reported_at 链上单调与未来/迟报边界，以及柏林夏令时重复时刻下
“同一本地墙钟、不同绝对时刻”的判定。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.chain_rules import (
    FUTURE_SKEW,
    MAX_ADVANCE,
    MAX_LATENESS,
    audit_stored,
    edge_violations,
    quarantine_members,
    timing_violations,
)
from app.models import (
    EVENT_CLOSED,
    EVENT_EXTENDED,
    EVENT_REOPENED,
    Airport,
    DisruptionEvent,
)
from app.timeutil import parse_event_datetime

BERLIN = Airport(code="BER", name="Berlin Test", timezone="Europe/Berlin", reopen_buffer_minutes=30)
APS = Airport(code="APS", name="Awan Pura", timezone="Asia/Makassar", reopen_buffer_minutes=20)


def ev(
    event_id: str,
    version: int,
    event_type: str,
    airport: Airport,
    frm: str,
    until: str | None,
    reported: str,
    supersedes: str | None = None,
) -> DisruptionEvent:
    return DisruptionEvent(
        event_id=event_id,
        event_version=version,
        event_type=event_type,
        airport_code=airport.code,
        effective_from=parse_event_datetime(frm, "effective_from"),
        effective_until=parse_event_datetime(until, "effective_until") if until else None,
        reported_at=parse_event_datetime(reported, "reported_at"),
        supersedes_event_id=supersedes,
        reason=None,
    )


def issues(violations: list[dict[str, str]]) -> set[str]:
    return {v["issue"] for v in violations}


class ExtensionRuleTest(unittest.TestCase):
    def _chain(self, airport: Airport, *, root_from, root_until, ext_from, ext_until):
        root = ev("evt-root0000001", 1, EVENT_CLOSED, airport,
                  root_from, root_until, "2026-10-24T20:00:00Z")
        ext = ev("evt-ext00000001", 2, EVENT_EXTENDED, airport,
                 ext_from, ext_until, "2026-10-24T21:00:00Z",
                 supersedes="evt-root0000001")
        return root, ext

    def test_extension_before_root_rejected(self) -> None:
        # 审计抽查的第二类异常：effective_from 写到了整条链起点之前。
        root, ext = self._chain(
            APS,
            root_from="2026-09-07T15:00:00Z",
            root_until="2026-09-07T19:00:00Z",
            ext_from="2026-09-07T14:00:00Z",
            ext_until="2026-09-07T20:00:00Z",
        )
        v = edge_violations(ext, root, root, APS)
        self.assertIn("must_not_precede_root_closure", issues(v))
        # 错误明细必须指出冲突字段与两个冲突时刻。
        by_issue = {x["issue"]: x for x in v}
        self.assertEqual(by_issue["must_not_precede_root_closure"]["field"], "effective_from")
        self.assertIn("chain_start", by_issue["must_not_precede_root_closure"])

    def test_extension_across_gap_rejected(self) -> None:
        root, ext = self._chain(
            APS,
            root_from="2026-09-07T15:00:00Z",
            root_until="2026-09-07T16:00:00Z",
            ext_from="2026-09-07T16:30:00Z",
            ext_until="2026-09-07T18:00:00Z",
        )
        self.assertIn("extension_leaves_uncovered_gap", issues(edge_violations(ext, root, root, APS)))

    def test_extension_abutting_end_accepted(self) -> None:
        # 左闭右开：from == previous_until 是端点相接，不算间隙。
        root, ext = self._chain(
            APS,
            root_from="2026-09-07T15:00:00Z",
            root_until="2026-09-07T16:00:00Z",
            ext_from="2026-09-07T16:00:00Z",
            ext_until="2026-09-07T18:00:00Z",
        )
        self.assertEqual(edge_violations(ext, root, root, APS), [])

    def test_extension_within_window_abutting_end_accepted(self) -> None:
        # 延长段起点在上一窗口内部也是合法的连续覆盖。
        root, ext = self._chain(
            APS,
            root_from="2026-09-07T15:00:00Z",
            root_until="2026-09-07T16:00:00Z",
            ext_from="2026-09-07T15:30:00Z",
            ext_until="2026-09-07T18:00:00Z",
        )
        self.assertEqual(edge_violations(ext, root, root, APS), [])

    def test_extension_must_push_end(self) -> None:
        root, ext = self._chain(
            APS,
            root_from="2026-09-07T15:00:00Z",
            root_until="2026-09-07T19:00:00Z",
            ext_from="2026-09-07T16:00:00Z",
            ext_until="2026-09-07T19:00:00Z",  # 相等也不允许：必须推后
        )
        self.assertIn("must_extend_previous_window", issues(edge_violations(ext, root, root, APS)))

    def test_extend_open_ended_root_only_requires_not_before_root(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", None, "2026-09-07T14:00:00Z")
        ok = ev("evt-ext00000001", 2, EVENT_EXTENDED, APS,
                "2026-09-07T15:30:00Z", "2026-09-07T20:00:00Z",
                "2026-09-07T15:00:00Z", supersedes="evt-root0000001")
        self.assertEqual(edge_violations(ok, root, root, APS), [])
        bad = ev("evt-ext00000002", 2, EVENT_EXTENDED, APS,
                 "2026-09-07T14:30:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T15:00:00Z", supersedes="evt-root0000001")
        self.assertIn(
            "must_not_precede_root_closure", issues(edge_violations(bad, root, root, APS))
        )


class ReopenRuleTest(unittest.TestCase):
    def test_reopen_before_root_rejected(self) -> None:
        # 审计抽查的第一类异常：恢复开放发生在原始关闭之前。
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        reopen = ev("evt-reopen00001", 2, EVENT_REOPENED, APS,
                    "2026-09-07T14:30:00Z", None, "2026-09-07T14:35:00Z",
                    supersedes="evt-root0000001")
        v = edge_violations(reopen, root, root, APS)
        self.assertIn("reopen_before_chain_start", issues(v))
        self.assertEqual(
            {x["issue"]: x["field"] for x in v}["reopen_before_chain_start"],
            "effective_from",
        )

    def test_reopen_point_past_closure_end_rejected(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T16:00:00Z",
                  "2026-09-07T14:00:00Z")
        reopen = ev("evt-reopen00001", 2, EVENT_REOPENED, APS,
                    "2026-09-07T16:30:00Z", None, "2026-09-07T16:31:00Z",
                    supersedes="evt-root0000001")
        self.assertIn(
            "reopen_outside_closure_window",
            issues(edge_violations(reopen, root, root, APS)),
        )

    def test_resume_buffer_past_end_rejected_but_abut_accepted(self) -> None:
        # Berlin buffer=30。reopen 01:00Z、窗口末 01:30Z -> resume 恰好相接，合法。
        root = ev("evt-root0000001", 1, EVENT_CLOSED, BERLIN,
                  "2026-10-25T00:00:00Z", "2026-10-25T01:30:00Z",
                  "2026-10-24T22:00:00Z")
        abut = ev("evt-reopen00001", 2, EVENT_REOPENED, BERLIN,
                  "2026-10-25T01:00:00Z", None, "2026-10-25T01:01:00Z",
                  supersedes="evt-root0000001")
        self.assertEqual(edge_violations(abut, root, root, BERLIN), [])

        # reopen 01:10Z -> resume 01:40Z，越过末端 01:30Z -> 拒绝。
        overrun = ev("evt-reopen00002", 2, EVENT_REOPENED, BERLIN,
                     "2026-10-25T01:10:00Z", None, "2026-10-25T01:11:00Z",
                     supersedes="evt-root0000001")
        self.assertIn(
            "reopen_buffer_extends_past_closure",
            issues(edge_violations(overrun, root, root, BERLIN)),
        )

    def test_cannot_continue_reopened_chain(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        reopened = ev("evt-reopen00001", 2, EVENT_REOPENED, APS,
                      "2026-09-07T18:30:00Z", None, "2026-09-07T18:31:00Z",
                      supersedes="evt-root0000001")
        ext = ev("evt-ext00000001", 3, EVENT_EXTENDED, APS,
                 "2026-09-07T19:00:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T19:05:00Z", supersedes="evt-reopen00001")
        self.assertIn(
            "chain_already_closed",
            issues(edge_violations(ext, reopened, root, APS)),
        )


class ReportedAtRuleTest(unittest.TestCase):
    def _event(self, airport: Airport, *, frm, reported) -> DisruptionEvent:
        return ev("evt-root0000001", 1, EVENT_CLOSED, airport,
                  frm, "2026-09-07T19:00:00Z", reported)

    def test_future_reported_rejected_at_boundary(self) -> None:
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        at_skew = self._event(APS, frm="2026-09-07T15:00:00Z",
                              reported="2026-09-07T14:01:00Z")
        self.assertEqual(timing_violations(at_skew, APS, now=now), [])
        future = self._event(APS, frm="2026-09-07T15:00:00Z",
                             reported="2026-09-07T14:01:01Z")
        v = timing_violations(future, APS, now=now)
        self.assertIn("reported_at_in_future", issues(v))
        self.assertTrue(all(x["field"] == "reported_at" for x in v))

    def test_late_report_rejected_at_boundary(self) -> None:
        # reported 恰好晚于 effective_from 24h 放行，超过则迟报。
        now = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)
        at_limit = self._event(APS, frm="2026-09-07T15:00:00Z",
                               reported="2026-09-08T15:00:00Z")
        self.assertEqual(timing_violations(at_limit, APS, now=now), [])
        late = self._event(APS, frm="2026-09-07T15:00:00Z",
                           reported="2026-09-08T15:00:01Z")
        self.assertIn("reported_too_late", issues(timing_violations(late, APS, now=now)))

    def test_far_advance_rejected(self) -> None:
        now = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
        too_early = self._event(APS, frm="2026-09-07T15:00:00Z",
                                reported="2026-08-24T14:59:00Z")
        self.assertIn(
            "reported_too_far_in_advance",
            issues(timing_violations(too_early, APS, now=now)),
        )
        at_limit = self._event(APS, frm="2026-09-07T15:00:00Z",
                               reported="2026-08-24T15:00:00Z")
        self.assertEqual(timing_violations(at_limit, APS, now=now), [])

    def test_reported_at_must_be_monotonic_along_chain(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T15:00:00Z")
        ext = ev("evt-ext00000001", 2, EVENT_EXTENDED, APS,
                 "2026-09-07T18:00:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T14:00:00Z", supersedes="evt-root0000001")
        self.assertIn(
            "reported_at_out_of_order", issues(edge_violations(ext, root, root, APS))
        )

    def test_bounds_constants_are_coherent(self) -> None:
        self.assertLessEqual(FUTURE_SKEW, timedelta(minutes=5))
        self.assertEqual(MAX_LATENESS, timedelta(hours=24))
        self.assertEqual(MAX_ADVANCE, timedelta(days=14))


class DaylightSavingRuleTest(unittest.TestCase):
    def test_repeated_local_hour_distinguished_by_offset(self) -> None:
        # 柏林 2026-10-25 本地 02:30 出现两次：+02:00 -> 00:30Z（fold 前），
        # +01:00 -> 01:30Z（fold 后）。窗口在第一次 02:30（00:30Z）结束。
        root = ev("evt-root0000001", 1, EVENT_CLOSED, BERLIN,
                  "2026-10-24T23:00:00Z", "2026-10-25T00:30:00Z",
                  "2026-10-24T22:00:00Z")

        # 延长段从第二次 02:30（01:30Z，带 +01:00）起：相隔 60 分钟开放间隙。
        second = ev("evt-ext00000001", 2, EVENT_EXTENDED, BERLIN,
                    "2026-10-25T02:30:00+01:00", "2026-10-25T03:00:00+01:00",
                    "2026-10-25T01:31:00Z", supersedes="evt-root0000001")
        self.assertIn(
            "extension_leaves_uncovered_gap",
            issues(edge_violations(second, root, root, BERLIN)),
        )

        # 同一本地墙钟字符串但带 +02:00，即第一次 02:30（00:30Z），端点相接合法。
        first = ev("evt-ext00000002", 2, EVENT_EXTENDED, BERLIN,
                   "2026-10-25T02:30:00+02:00", "2026-10-25T03:30:00+01:00",
                   "2026-10-25T00:31:00Z", supersedes="evt-root0000001")
        self.assertEqual(edge_violations(first, root, root, BERLIN), [])

    def test_repeated_local_hour_reopen_abut_and_overrun(self) -> None:
        # 窗口末端 01:30Z。本地重复的 02:30 出现两次：
        #   第一次 02:30+02:00 == 00:30Z -> +30min 缓冲后 01:00Z，落在窗口内，合法
        #   第二次 02:30+01:00 == 01:30Z -> 缓冲后 02:00Z，越过末端，拒绝
        # 相同本地墙钟字符串因偏移不同而结论相反。
        root = ev("evt-root0000001", 1, EVENT_CLOSED, BERLIN,
                  "2026-10-24T23:00:00Z", "2026-10-25T01:30:00Z",
                  "2026-10-24T22:00:00Z")
        first = ev("evt-reopen00001", 2, EVENT_REOPENED, BERLIN,
                   "2026-10-25T02:30:00+02:00", None,
                   "2026-10-25T00:31:00Z", supersedes="evt-root0000001")
        second = ev("evt-reopen00002", 3, EVENT_REOPENED, BERLIN,
                    "2026-10-25T02:30:00+01:00", None,
                    "2026-10-25T01:31:00Z", supersedes="evt-root0000001")
        self.assertEqual(edge_violations(first, root, root, BERLIN), [])
        self.assertIn(
            "reopen_buffer_extends_past_closure",
            issues(edge_violations(second, root, root, BERLIN)),
        )


class AuditTest(unittest.TestCase):
    def test_clean_open_and_terminal_chains_are_not_anomalies(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        ext = ev("evt-ext00000001", 2, EVENT_EXTENDED, APS,
                 "2026-09-07T18:00:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T17:00:00Z", supersedes="evt-root0000001")
        reopen = ev("evt-reopen00001", 3, EVENT_REOPENED, APS,
                    "2026-09-07T19:30:00Z", None, "2026-09-07T19:31:00Z",
                    supersedes="evt-ext00000001")
        anomalies = audit_stored(
            {e.event_id: e for e in (root, ext, reopen)}, {"APS": APS}
        )
        self.assertEqual(anomalies, [])

        # 仍开放（无 reopen）的正常链也不是异常。
        self.assertEqual(
            audit_stored({e.event_id: e for e in (root, ext)}, {"APS": APS}), []
        )

    def test_reopen_before_root_chain_quarantined(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        reopen = ev("evt-reopen00001", 2, EVENT_REOPENED, APS,
                    "2026-09-07T14:30:00Z", None, "2026-09-07T14:31:00Z",
                    supersedes="evt-root0000001")
        events = {e.event_id: e for e in (root, reopen)}
        first = audit_stored(events, {"APS": APS})
        self.assertEqual(len(first), 1)
        anomaly = first[0]
        self.assertEqual(anomaly.group_id, "evt-root0000001")
        self.assertIn("reopen_before_chain_start", anomaly.reasons)
        self.assertEqual(quarantine_members(first), frozenset(events))

        # 确定性：同一输入再次审计得到逐字节相同的结论（容器重建一致性）。
        second = audit_stored(dict(events), {"APS": APS})
        self.assertEqual(
            [a.group_id for a in first], [a.group_id for a in second]
        )
        self.assertEqual(
            [a.reasons for a in first], [a.reasons for a in second]
        )
        self.assertEqual(
            [sorted(a.members) for a in first],
            [sorted(a.members) for a in second],
        )
        self.assertEqual(
            [a.violations for a in first], [a.violations for a in second]
        )

    def test_extension_before_root_chain_quarantined(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        ext = ev("evt-ext00000001", 2, EVENT_EXTENDED, APS,
                 "2026-09-07T13:00:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T14:30:00Z", supersedes="evt-root0000001")
        anomalies = audit_stored(
            {e.event_id: e for e in (root, ext)}, {"APS": APS}
        )
        self.assertEqual(len(anomalies), 1)
        self.assertIn("must_not_precede_root_closure", anomalies[0].reasons)

    def test_branching_chain_quarantined(self) -> None:
        root = ev("evt-root0000001", 1, EVENT_CLOSED, APS,
                  "2026-09-07T15:00:00Z", "2026-09-07T19:00:00Z",
                  "2026-09-07T14:00:00Z")
        ext_a = ev("evt-ext0000000a", 2, EVENT_EXTENDED, APS,
                   "2026-09-07T18:00:00Z", "2026-09-07T20:00:00Z",
                   "2026-09-07T17:00:00Z", supersedes="evt-root0000001")
        ext_b = ev("evt-ext0000000b", 3, EVENT_EXTENDED, APS,
                   "2026-09-07T18:30:00Z", "2026-09-07T21:00:00Z",
                   "2026-09-07T17:30:00Z", supersedes="evt-root0000001")
        anomalies = audit_stored(
            {e.event_id: e for e in (root, ext_a, ext_b)}, {"APS": APS}
        )
        self.assertEqual(len(anomalies), 1)
        self.assertIn("chain_branched", anomalies[0].reasons)

    def test_dangling_reference_quarantined(self) -> None:
        ext = ev("evt-ext00000001", 2, EVENT_EXTENDED, APS,
                 "2026-09-07T18:00:00Z", "2026-09-07T20:00:00Z",
                 "2026-09-07T17:00:00Z", supersedes="evt-missing00001")
        anomalies = audit_stored({"evt-ext00000001": ext}, {"APS": APS})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("unknown_event", anomalies[0].reasons)


if __name__ == "__main__":
    unittest.main()
