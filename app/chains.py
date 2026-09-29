"""事件链时间因果审计（纯函数）。

同一组规则同时服务于两条路径，保证判定口径一致、可重放：

* 接入新事件时，把候选事件追加进既有事件集合做*模拟*审计；所有归因到候选
  事件的违规都以 422 拒绝，事件、影响与重放计数均不落库。
* 服务启动时，对数据库中的既有事件做*存量*审计；违规链被标记为隔离
  （quarantined），只读保留，但不参与当前汇总。

所有时间在进入本模块前都已带时区并归一化到 UTC，因此比较结果与时区、夏令
时（含秋季重复时刻）、跨午夜无关——调用方必须显式携带偏移，机场时区只用于
本地日界判定与展示。区间一律采用左闭右开语义，端点相接合法。

``reported_at`` 规则（以当前时刻 ``now`` 为基准，UTC 比较）：

* 不得晚于 ``now``（拒绝"来自未来的报告"），:data:`FUTURE_REPORT_TOLERANCE`
  用于抹平时钟漂移；
* 不得晚于生效窗口结束（窗口结束后才上报的关闭没有业务意义）；
* 相对生效开始滞后超过 :data:`MAX_LATE_REPORT` 的迟报拒绝；
* 宽限期 :data:`LATE_REPORT_GRACE` 之后、上限之内的迟报允许接收，但标记
  ``late_report`` 供审计（见 :func:`report_timing`），不导致隔离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.models import (
    EVENT_CLOSED,
    EVENT_EXTENDED,
    EVENT_REOPENED,
    Airport,
    DisruptionEvent,
)

# 报告时刻允许领先服务器时钟的最大幅度（时钟漂移容差）。
FUTURE_REPORT_TOLERANCE = timedelta(minutes=1)
# 迟报宽限期：生效开始后这么久之内上报不算迟报。
LATE_REPORT_GRACE = timedelta(minutes=15)
# 迟报上限：超过则作为硬违规拒绝/隔离。
MAX_LATE_REPORT = timedelta(days=30)


@dataclass(frozen=True)
class ChainViolation:
    """一条可审计的因果违规，全部归因到具体事件与字段。"""

    event_id: str
    field: str
    issue: str
    detail: dict[str, str] = field(default_factory=dict)

    def to_error(self) -> dict[str, str]:
        entry: dict[str, str] = {
            "field": self.field,
            "issue": self.issue,
            "event_id": self.event_id,
        }
        entry.update(self.detail)
        return entry


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 报告时点
# --------------------------------------------------------------------------- #

def report_timing(event: DisruptionEvent, now: datetime) -> dict[str, Any]:
    """返回报告时点的可审计判定（纯数据派生，重启后结果一致）。

    ``state`` 取值：``on_time``（含生效前预报与宽限期内上报）、``late``
    （宽限期后但在上限内）。``lag_minutes`` 为相对生效开始的滞后分钟数，
    预报为负值。
    """
    lag = event.reported_at - event.effective_from
    lag_minutes = int(lag.total_seconds() // 60)
    state = "late" if lag > LATE_REPORT_GRACE else "on_time"
    return {
        "state": state,
        "lag_minutes": lag_minutes,
        "reported_at": iso_z(event.reported_at),
        "effective_from": iso_z(event.effective_from),
    }


def hard_report_violations(
    event: DisruptionEvent, now: datetime
) -> list[ChainViolation]:
    violations: list[ChainViolation] = []
    if event.reported_at > now + FUTURE_REPORT_TOLERANCE:
        violations.append(
            ChainViolation(
                event.event_id,
                "reported_at",
                "reported_at_in_future",
                {
                    "reported_at": iso_z(event.reported_at),
                    "server_time": iso_z(now),
                    "tolerance_seconds": str(int(FUTURE_REPORT_TOLERANCE.total_seconds())),
                },
            )
        )
    lag = event.reported_at - event.effective_from
    if lag > MAX_LATE_REPORT:
        violations.append(
            ChainViolation(
                event.event_id,
                "reported_at",
                "report_too_late",
                {
                    "effective_from": iso_z(event.effective_from),
                    "reported_at": iso_z(event.reported_at),
                    "lag_minutes": str(int(lag.total_seconds() // 60)),
                    "max_lag_minutes": str(int(MAX_LATE_REPORT.total_seconds() // 60)),
                },
            )
        )
    if event.effective_until is not None and event.reported_at > event.effective_until:
        violations.append(
            ChainViolation(
                event.event_id,
                "reported_at",
                "reported_after_window_end",
                {
                    "effective_until": iso_z(event.effective_until),
                    "reported_at": iso_z(event.reported_at),
                },
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# 边（事件 → 直接前驱）校验
# --------------------------------------------------------------------------- #

def _index(events: Iterable[DisruptionEvent]) -> dict[str, DisruptionEvent]:
    return {e.event_id: e for e in events}


def _resolve_parent(
    event: DisruptionEvent, by_id: dict[str, DisruptionEvent]
) -> tuple[DisruptionEvent | None, list[ChainViolation]]:
    """解析直接前驱并做结构性检查。"""
    ref_id = event.supersedes_event_id
    if ref_id is None:
        return None, [
            ChainViolation(
                event.event_id, "supersedes_event_id", "required_for_chain_event"
            )
        ]
    parent = by_id.get(ref_id)
    if parent is None:
        return None, [
            ChainViolation(
                event.event_id,
                "supersedes_event_id",
                "unknown_event",
                {"referenced_event_id": ref_id},
            )
        ]
    if parent.airport_code != event.airport_code:
        return None, [
            ChainViolation(
                event.event_id,
                "supersedes_event_id",
                "airport_mismatch",
                {
                    "referenced_airport": parent.airport_code,
                    "received_airport": event.airport_code,
                },
            )
        ]
    return parent, []


def _resolve_root(
    event: DisruptionEvent, by_id: dict[str, DisruptionEvent]
) -> tuple[DisruptionEvent | None, list[ChainViolation]]:
    """沿 supersedes 走到根关闭；沿途检查环与无根链。"""
    seen: set[str] = set()
    current = event
    while current.supersedes_event_id is not None:
        if current.event_id in seen:
            return None, [
                ChainViolation(event.event_id, "supersedes_event_id", "chain_cycle")
            ]
        seen.add(current.event_id)
        nxt = by_id.get(current.supersedes_event_id)
        if nxt is None:  # 悬空引用已由 _resolve_parent 报告
            return None, []
        current = nxt
    if current.event_type != EVENT_CLOSED:
        return None, [
            ChainViolation(
                event.event_id, "supersedes_event_id", "chain_has_no_closed_root"
            )
        ]
    return current, []


def _check_extension(
    event: DisruptionEvent, root: DisruptionEvent, prev: DisruptionEvent
) -> list[ChainViolation]:
    violations: list[ChainViolation] = []
    assert event.effective_until is not None  # 结构校验已保证

    # 延长段不得早于根关闭起点（"effective_from 写到整条链起点之前"）。
    if event.effective_from < root.effective_from:
        violations.append(
            ChainViolation(
                event.event_id,
                "effective_from",
                "must_not_precede_root_closure",
                {
                    "root_event_id": root.event_id,
                    "root_effective_from": iso_z(root.effective_from),
                    "received_effective_from": iso_z(event.effective_from),
                },
            )
        )

    if prev.event_type == EVENT_REOPENED:
        violations.append(
            ChainViolation(
                event.event_id,
                "supersedes_event_id",
                "chain_already_closed",
                {"reopened_event_id": prev.event_id},
            )
        )
        return violations

    # 延长段不得跨过未说明的开放间隙；左闭右开下起点恰好等于上一窗口末端
    # （首尾相接）合法。
    if prev.effective_until is not None:
        if event.effective_from > prev.effective_until:
            violations.append(
                ChainViolation(
                    event.event_id,
                    "effective_from",
                    "extension_leaves_uncovered_gap",
                    {
                        "previous_end": iso_z(prev.effective_until),
                        "received_effective_from": iso_z(event.effective_from),
                    },
                )
            )
        if event.effective_until <= prev.effective_until:
            violations.append(
                ChainViolation(
                    event.event_id,
                    "effective_until",
                    "must_extend_previous_window",
                    {
                        "previous_end": iso_z(prev.effective_until),
                        "received_effective_until": iso_z(event.effective_until),
                    },
                )
            )

    if event.event_version <= prev.event_version:
        violations.append(
            ChainViolation(
                event.event_id,
                "event_version",
                "version_must_increase",
                {
                    "stored_version": str(prev.event_version),
                    "received_version": str(event.event_version),
                },
            )
        )
    return violations


def _check_reopen(
    event: DisruptionEvent,
    root: DisruptionEvent,
    prev: DisruptionEvent,
    airport: Airport,
) -> list[ChainViolation]:
    violations: list[ChainViolation] = []

    if prev.event_type == EVENT_REOPENED:
        violations.append(
            ChainViolation(
                event.event_id,
                "supersedes_event_id",
                "chain_already_reopened",
                {"reopened_event_id": prev.event_id},
            )
        )
        return violations

    if event.event_version <= prev.event_version:
        violations.append(
            ChainViolation(
                event.event_id,
                "event_version",
                "version_must_increase",
                {
                    "stored_version": str(prev.event_version),
                    "received_version": str(event.event_version),
                },
            )
        )

    # 恢复时点不得早于根关闭起点；左闭区间，恰好等于根起点合法。
    if event.effective_from < root.effective_from:
        violations.append(
            ChainViolation(
                event.event_id,
                "effective_from",
                "reopen_before_closure_start",
                {
                    "root_event_id": root.event_id,
                    "root_effective_from": iso_z(root.effective_from),
                    "received_effective_from": iso_z(event.effective_from),
                },
            )
        )

    # 机场缓冲结束必须落在当前有效关闭范围内：不得晚于前驱的已知窗口末端。
    # 左闭右开，缓冲结束恰好等于窗口末端合法（相接航班不受影响）。前驱为
    # 开放结束（effective_until=null）时，恢复事件负责提供结束时点。
    resume_at = event.effective_from + timedelta(
        minutes=airport.reopen_buffer_minutes
    )
    if prev.effective_until is not None and resume_at > prev.effective_until:
        violations.append(
            ChainViolation(
                event.event_id,
                "effective_from",
                "resume_after_closure_end",
                {
                    "reopen_buffer_minutes": str(airport.reopen_buffer_minutes),
                    "resume_at": iso_z(resume_at),
                    "current_closure_end": iso_z(prev.effective_until),
                },
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# 整体审计
# --------------------------------------------------------------------------- #

def audit_events(
    events: Sequence[DisruptionEvent],
    airports: dict[str, Airport],
    *,
    now: datetime | None = None,
    check_reported_at: bool = True,
) -> dict[str, list[ChainViolation]]:
    """对一批事件执行完整因果审计，返回 event_id -> 违规列表。

    ``check_reported_at`` 为 False 时跳过报告时点规则：启动审计只隔离结构性
    与时间因果异常，迟报属于接入时刻判定，不应追溯隔离历史链。
    """
    now = now or utc_now()
    by_id = _index(events)
    violations: dict[str, list[ChainViolation]] = {}

    for event in events:
        if check_reported_at:
            found = hard_report_violations(event, now)
            if found:
                violations.setdefault(event.event_id, []).extend(found)
        if event.event_type == EVENT_CLOSED and event.supersedes_event_id is not None:
            violations.setdefault(event.event_id, []).append(
                ChainViolation(
                    event.event_id,
                    "supersedes_event_id",
                    "not_allowed_for_close_event",
                )
            )

    for event in sorted(events, key=lambda e: (e.event_version, e.event_id)):
        if event.event_type == EVENT_CLOSED:
            continue
        prev, structural = _resolve_parent(event, by_id)
        if structural:
            violations.setdefault(event.event_id, []).extend(structural)
        if prev is None:
            continue
        root, root_errors = _resolve_root(event, by_id)
        if root_errors:
            violations.setdefault(event.event_id, []).extend(root_errors)
        if root is None:
            continue
        airport = airports.get(event.airport_code)
        if airport is None:
            continue
        if event.event_type == EVENT_EXTENDED:
            found = _check_extension(event, root, prev)
        else:  # EVENT_REOPENED
            found = _check_reopen(event, root, prev, airport)
        if found:
            violations.setdefault(event.event_id, []).extend(found)

    return violations


def candidate_violations(
    candidate: DisruptionEvent,
    existing: Sequence[DisruptionEvent],
    airports: dict[str, Airport],
    *,
    now: datetime | None = None,
) -> list[ChainViolation]:
    """返回*应当拒绝候选事件*的违规（归因到候选自身）。

    存量中已经存在的违规（历史隔离数据）不算在候选头上；只有加入候选后
    新出现的违规才拒绝接入。候选是新事件，新增的边必然连接候选，因此新增
    违规通常直接归因于候选本身。
    """
    now = now or utc_now()
    # 存量只做结构与时间因果审计（与启动审计同口径）；报告时点规则只作用于
    # 候选事件本身，不追溯历史。
    before = audit_events(existing, airports, now=now, check_reported_at=False)
    after = audit_events(
        list(existing) + [candidate],
        airports,
        now=now,
        check_reported_at=False,
    )

    already_bad = {event_id for event_id, found in before.items() if found}
    fresh: list[ChainViolation] = []
    for event_id, found in after.items():
        if event_id == candidate.event_id or event_id not in already_bad:
            fresh.extend(found)

    # 候选自身的报告时点（未来/迟报上限/窗口结束后）规则。
    fresh.extend(hard_report_violations(candidate, now))

    # 不允许在已隔离的异常链上继续追加事件——异常历史必须先纠正。
    if candidate.supersedes_event_id is not None:
        quarantined = quarantined_event_ids(before, existing)
        if candidate.supersedes_event_id in quarantined:
            fresh.append(
                ChainViolation(
                    candidate.event_id,
                    "supersedes_event_id",
                    "cannot_extend_quarantined_chain",
                    {"referenced_event_id": candidate.supersedes_event_id},
                )
            )

    # 新根关闭必须延续机场事件历史：版本号严格大于该机场既有最大版本。
    # 该规则依赖"提交时刻的历史"，不属于对存量数据的静态因果审计。
    if candidate.event_type == EVENT_CLOSED:
        airport_versions = [
            e.event_version
            for e in existing
            if e.airport_code == candidate.airport_code
        ]
        if airport_versions and candidate.event_version <= max(airport_versions):
            fresh.append(
                ChainViolation(
                    candidate.event_id,
                    "event_version",
                    "must_extend_airport_history",
                    {
                        "stored_version": str(max(airport_versions)),
                        "received_version": str(candidate.event_version),
                    },
                )
            )

    unique: list[ChainViolation] = []
    seen: set[tuple[str, str, str]] = set()
    for violation in fresh:
        key = (violation.event_id, violation.field, violation.issue)
        if key not in seen:
            seen.add(key)
            unique.append(violation)
    return unique


def chain_members(
    events: Sequence[DisruptionEvent],
) -> dict[str, set[str]]:
    """返回 根事件 id -> 该链全部成员事件 id。无法成根的事件自成一组。"""
    by_id = _index(events)
    groups: dict[str, set[str]] = {}

    def root_of(event: DisruptionEvent) -> str:
        seen: set[str] = set()
        current = event
        while current.supersedes_event_id is not None:
            if current.event_id in seen:
                return event.event_id  # 环：自成一组
            seen.add(current.event_id)
            nxt = by_id.get(current.supersedes_event_id)
            if nxt is None:
                return event.event_id  # 悬空：自成一组
            current = nxt
        return current.event_id if current.event_type == EVENT_CLOSED else event.event_id

    for event in events:
        groups.setdefault(root_of(event), set()).add(event.event_id)
    return groups


def quarantined_event_ids(
    violations: dict[str, list[ChainViolation]],
    events: Sequence[DisruptionEvent],
) -> set[str]:
    """根据违规映射推导需要隔离的*事件* id 集合（整条链一起隔离）。"""
    bad_events = {event_id for event_id, found in violations.items() if found}
    groups = chain_members(events)
    flagged: set[str] = set()
    for root_id, members in groups.items():
        if members & bad_events:
            flagged.update(members)
    return flagged
