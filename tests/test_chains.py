"""事件链时间因果审计测试：延长/恢复/报告时点规则与夏令时边界。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.chains import (
    FUTURE_REPORT_TOLERANCE,
    LATE_REPORT_GRACE,
    MAX_LATE_REPORT,
    audit_events,
    candidate_violations,
    quarantined_event_ids,
)
from app.models import EVENT_CLOSED, EVENT_EXTENDED, EVENT_REOPENED, Airport, DisruptionEvent
from app.timeutil import parse_event_datetime

NOW = datetime.fromisoformat("2026-10-25T12:00:00+00:00")
LHR = Airport("LHR", "London Test", "Europe/London", 30)
APS = Airport("APS", "Awan Pura", "Asia/Makassar", 20)


def ev(
    event_id: str,
    event_type: str = EVENT_CLOSED,
    *,
    version: int = 1,
    frm: str = "2026-10-25T00:00:00Z",
    until: str | None = "2026-10-25T06:00:00Z",
    reported: str = "2026-10-24T23:00:00Z",
    supersedes: str | None = None,
    airport: Airport = LHR,
) -> DisruptionEvent:
    return DisruptionEvent(
        event_id=event_id,
        event_version=version,
        event_type=event_type,
        airport_code=airport.code,
        effective_from=parse_event_datetime(frm, "effective_from"),
        effective_until=(
            parse_event_datetime(until, "effective_until") if until else None
        ),
        reported_at=parse_event_datetime(reported, "reported_at"),
        supersedes_event_id=supersedes,
        reason=None,
    )


def issues(events, airport=LHR, **kw):
    airports = {airport.code: airport}
    result = audit_events(events, airports, now=NOW, **kw)
    return [(v.event_id, v.field, v.issue) for vs in result.values() for v in vs]


class ExtensionCausalityTest(unittest.TestCase):
    def _chain(self, ext_from, ext_until):
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until="2026-10-25T04:00:00Z")
        ext = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2,
            frm=ext_from, until=ext_until,
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        return [root, ext]

    def test_extension_before_root_rejected(self) -> None:
        found = issues(self._chain("2026-10-25T00:30:00Z", "2026-10-25T05:00:00Z"))
        self.assertIn(
            ("evt-extend00001", "effective_from", "must_not_precede_root_closure"),
            found,
        )

    def test_extension_across_gap_rejected(self) -> None:
        found = issues(self._chain("2026-10-25T04:30:00Z", "2026-10-25T06:00:00Z"))
        self.assertIn(
            ("evt-extend00001", "effective_from", "extension_leaves_uncovered_gap"),
            found,
        )

    def test_extension_touching_prior_end_accepted(self) -> None:
        # 左闭右开：延长起点恰好等于上一窗口末端（首尾相接）合法。
        found = issues(self._chain("2026-10-25T04:00:00Z", "2026-10-25T06:00:00Z"))
        self.assertEqual(found, [])

    def test_extension_must_push_end_out(self) -> None:
        found = issues(self._chain("2026-10-25T03:00:00Z", "2026-10-25T04:00:00Z"))
        self.assertIn(
            ("evt-extend00001", "effective_until", "must_extend_previous_window"),
            found,
        )

    def test_extending_reopened_chain_rejected(self) -> None:
        root = ev("evt-root0000001")
        reopen = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T03:00:00Z", until=None,
            reported="2026-10-25T02:30:00Z", supersedes="evt-root0000001",
        )
        again = ev(
            "evt-extend00001", EVENT_EXTENDED, version=3,
            frm="2026-10-25T03:00:00Z", until="2026-10-25T08:00:00Z",
            reported="2026-10-25T02:45:00Z", supersedes="evt-reopen00001",
        )
        found = issues([root, reopen, again])
        self.assertIn(
            ("evt-extend00001", "supersedes_event_id", "chain_already_closed"),
            found,
        )

    def test_open_ended_root_extension_only_checked_against_root(self) -> None:
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until=None)
        ok = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2,
            frm="2026-10-25T01:30:00Z", until="2026-10-25T05:00:00Z",
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        self.assertEqual(issues([root, ok]), [])
        bad = ev(
            "evt-extend00002", EVENT_EXTENDED, version=2,
            frm="2026-10-25T00:30:00Z", until="2026-10-25T05:00:00Z",
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        found = issues([root, bad])
        self.assertIn(
            ("evt-extend00002", "effective_from", "must_not_precede_root_closure"),
            found,
        )


class ReopenCausalityTest(unittest.TestCase):
    def test_reopen_before_root_start_rejected(self) -> None:
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until="2026-10-25T06:00:00Z")
        bad = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T00:30:00Z", until=None,
            reported="2026-10-25T00:20:00Z", supersedes="evt-root0000001",
        )
        found = issues([root, bad])
        self.assertIn(
            ("evt-reopen00001", "effective_from", "reopen_before_closure_start"),
            found,
        )

    def test_buffer_end_within_window_touching_endpoint_accepted(self) -> None:
        # 窗口 01:00-04:00Z，LHR 缓冲 30 分钟；03:30 恢复 => 04:00 恰好相接。
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until="2026-10-25T04:00:00Z")
        touch = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T03:30:00Z", until=None,
            reported="2026-10-25T03:00:00Z", supersedes="evt-root0000001",
        )
        self.assertEqual(issues([root, touch]), [])

    def test_buffer_end_past_window_rejected(self) -> None:
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until="2026-10-25T04:00:00Z")
        bad = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T03:45:00Z", until=None,  # resume 04:15 > 04:00
            reported="2026-10-25T03:30:00Z", supersedes="evt-root0000001",
        )
        found = issues([root, bad])
        self.assertIn(
            ("evt-reopen00001", "effective_from", "resume_after_closure_end"),
            found,
        )

    def test_reopen_open_ended_closure_supplies_end(self) -> None:
        # 前驱 effective_until 为 null：恢复点+缓冲即为新末端，任何恢复点都合法
        # （只要不早于根）。
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until=None)
        ok = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T05:00:00Z", until=None,
            reported="2026-10-25T04:30:00Z", supersedes="evt-root0000001",
        )
        self.assertEqual(issues([root, ok]), [])

    def test_reopen_at_exactly_root_start_accepted(self) -> None:
        # 左闭：恢复点恰好等于根起点合法。
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z", until=None)
        ok = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-25T01:00:00Z", until=None,
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        self.assertEqual(issues([root, ok]), [])


class StructuralChainTest(unittest.TestCase):
    def test_unknown_parent(self) -> None:
        e = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2,
            supersedes="evt-missing00001",
            reported="2026-10-25T00:30:00Z",
        )
        found = issues([e])
        self.assertIn(("evt-extend00001", "supersedes_event_id", "unknown_event"), found)

    def test_airport_mismatch(self) -> None:
        root = ev("evt-root0000001", airport=LHR)
        ext = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2, airport=APS,
            frm="2026-10-25T03:00:00Z", until="2026-10-25T07:00:00Z",
            supersedes="evt-root0000001",
        )
        found = issues([root, ext], airport=LHR)
        self.assertIn(("evt-extend00001", "supersedes_event_id", "airport_mismatch"), found)

    def test_version_must_increase(self) -> None:
        root = ev("evt-root0000001", version=5)
        ext = ev(
            "evt-extend00001", EVENT_EXTENDED, version=5,
            frm="2026-10-25T03:00:00Z", until="2026-10-25T07:00:00Z",
            supersedes="evt-root0000001",
        )
        found = issues([root, ext])
        self.assertIn(("evt-extend00001", "event_version", "version_must_increase"), found)


class ReportedAtTest(unittest.TestCase):
    def test_future_report_rejected(self) -> None:
        future = ev("evt-root0000001", reported="2026-10-26T00:00:00Z")
        found = issues([future])
        self.assertIn(("evt-root0000001", "reported_at", "reported_at_in_future"), found)

    def test_future_report_within_clock_tolerance_accepted(self) -> None:
        tolerated = ev(
            "evt-root0000001",
            frm="2026-10-25T12:00:30Z",
            until="2026-10-25T13:00:00Z",
            reported=(NOW + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.assertLessEqual(timedelta(seconds=30), FUTURE_REPORT_TOLERANCE)
        self.assertEqual(issues([tolerated]), [])

    def test_report_after_window_end_rejected(self) -> None:
        late = ev(
            "evt-root0000001",
            frm="2026-10-25T01:00:00Z", until="2026-10-25T04:00:00Z",
            reported="2026-10-25T05:00:00Z",
        )
        found = issues([late])
        self.assertIn(("evt-root0000001", "reported_at", "reported_after_window_end"), found)

    def test_report_beyond_max_lag_rejected(self) -> None:
        frm = NOW - MAX_LATE_REPORT - timedelta(hours=1)
        late = ev(
            "evt-root0000001",
            frm=frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
            until=(frm + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reported=(NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        found = issues([late])
        self.assertIn(("evt-root0000001", "reported_at", "report_too_late"), found)

    def test_advance_report_and_grace_period_accepted(self) -> None:
        advance = ev("evt-root0000001", reported="2026-10-24T20:00:00Z")
        self.assertEqual(issues([advance]), [])
        # 宽限期边界：滞后恰好 15 分钟不算硬违规；窗口仍在报告时刻之后。
        in_grace = ev(
            "evt-root0000002",
            frm="2026-10-25T11:00:00Z", until="2026-10-25T13:00:00Z",
            reported=(NOW - LATE_REPORT_GRACE).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.assertEqual(issues([in_grace]), [])


class DaylightSavingRepeatedHourTest(unittest.TestCase):
    """Europe/London 2026-10-25 02:00 本地回拨到 01:00，01:30 出现两次。"""

    def test_same_wall_time_with_offsets_are_distinct_instants(self) -> None:
        first = parse_event_datetime("2026-10-25T01:30:00+01:00", "f")
        second = parse_event_datetime("2026-10-25T01:30:00+00:00", "f")
        self.assertEqual((second - first), timedelta(hours=1))
        self.assertEqual(first.astimezone(ZoneInfo("Europe/London")).hour, 1)
        self.assertEqual(second.astimezone(ZoneInfo("Europe/London")).hour, 1)

    def test_extension_ordering_uses_instant_not_wall_time(self) -> None:
        # 根关闭在重复时刻的第一次（BST，00:30Z）。
        root = ev(
            "evt-root0000001",
            frm="2026-10-25T01:30:00+01:00", until="2026-10-25T03:30:00Z",
            reported="2026-10-24T23:00:00Z",
        )
        # 相同墙上时间但 GMT（01:30Z）晚于根起点，且与末端相接合法。
        ok = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2,
            frm="2026-10-25T01:30:00+00:00", until="2026-10-25T05:00:00Z",
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        self.assertEqual(issues([root, ok]), [])
        # 第一次重复时刻之前（00:00Z）的延长段早于根，必须拒绝。
        bad = ev(
            "evt-extend00002", EVENT_EXTENDED, version=2,
            frm="2026-10-25T01:00:00+01:00", until="2026-10-25T05:00:00Z",
            reported="2026-10-24T23:30:00Z", supersedes="evt-root0000001",
        )
        found = issues([root, bad])
        self.assertIn(
            ("evt-extend00002", "effective_from", "must_not_precede_root_closure"),
            found,
        )

    def test_local_midnight_across_fall_back(self) -> None:
        from app.timeutil import crosses_local_midnight

        tz = ZoneInfo("Europe/London")
        # 22:00Z(Oct24, 本地23:00 BST) -> 02:00Z(Oct25, 本地02:00 GMT) 跨本地日界。
        self.assertTrue(
            crosses_local_midnight(
                datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc),
                tz,
            )
        )
        self.assertFalse(
            crosses_local_midnight(
                datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc),
                tz,
            )
        )


class CandidateSimulationTest(unittest.TestCase):
    def test_candidate_extension_violation_returned(self) -> None:
        root = ev("evt-root0000001", frm="2026-10-25T01:00:00Z")
        candidate = ev(
            "evt-extend00001", EVENT_EXTENDED, version=2,
            frm="2026-10-25T00:30:00Z", until="2026-10-25T08:00:00Z",
            reported="2026-10-25T00:30:00Z", supersedes="evt-root0000001",
        )
        found = candidate_violations(candidate, [root], {"LHR": LHR}, now=NOW)
        self.assertTrue(
            any(v.issue == "must_not_precede_root_closure" for v in found)
        )

    def test_root_close_must_extend_airport_version_history(self) -> None:
        root = ev("evt-root0000001", version=3)
        candidate = ev("evt-root0000002", version=2)
        found = candidate_violations(candidate, [root], {"LHR": LHR}, now=NOW)
        self.assertTrue(
            any(v.issue == "must_extend_airport_history" for v in found)
        )

    def test_quarantine_marks_whole_chain(self) -> None:
        root = ev("evt-root0000001")
        bad = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-24T23:30:00Z", until=None,
            reported="2026-10-24T23:20:00Z", supersedes="evt-root0000001",
        )
        violations = audit_events([root, bad], {"LHR": LHR}, now=NOW)
        flagged = quarantined_event_ids(violations, [root, bad])
        self.assertEqual(flagged, {"evt-root0000001", "evt-reopen00001"})

    def test_cannot_extend_quarantined_chain(self) -> None:
        root = ev("evt-root0000001")
        bad_reopen = ev(
            "evt-reopen00001", EVENT_REOPENED, version=2,
            frm="2026-10-24T23:30:00Z", until=None,
            reported="2026-10-24T23:20:00Z", supersedes="evt-root0000001",
        )
        candidate = ev(
            "evt-extend00009", EVENT_EXTENDED, version=3,
            frm="2026-10-25T03:00:00Z", until="2026-10-25T08:00:00Z",
            reported="2026-10-25T02:30:00Z", supersedes="evt-root0000001",
        )
        found = candidate_violations(
            candidate, [root, bad_reopen], {"LHR": LHR}, now=NOW
        )
        self.assertTrue(
            any(v.issue == "cannot_extend_quarantined_chain" for v in found)
        )


if __name__ == "__main__":
    unittest.main()
