"""事件链时间因果规则：接入校验与启动审计共用的纯函数规则集。

设计要点
========

* **规范化先行**：事件时间入库前已携带固定 UTC 偏移并转换为 UTC；规则比较前
  再经机场 IANA 时区做一次本地往返（:func:`canonicalize`），保证不同写法的
  同一时刻收敛。所有大小比较都在 UTC 瞬时上进行，夏令时重复/跳过的本地墙钟
  时刻不会让比较产生歧义；错误明细同时给出 UTC 与机场本地时刻以便审计。
* **左闭右开、端点相接**：延长段起点允许恰好等于上一段窗口末端
  （``from == previous_until`` 合法）；恢复点与恢复缓冲结束允许恰好落在关闭
  窗口末端。只有严格跨过（留出开放间隙或越过末端）才算违规。
* **只依赖事件内容**：本模块不访问数据库和时钟，便于接入校验与启动审计复用
  同一判定。``reported_at`` 的未来/迟报时限依赖当前时间，只在接入时检查
  （:func:`timing_violations`），不进入启动审计，以保证容器重建后审计结论
  完全一致。

违规统一返回 ``[{"field": ..., "issue": ..., ...上下文}]``，可直接装入
``ValidationError(details={"errors": ...})``，HTTP 422 能准确指出冲突字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.models import (
    EVENT_CLOSED,
    EVENT_EXTENDED,
    EVENT_REOPENED,
    Airport,
    DisruptionEvent,
)
from app.timeutil import airport_zone, iso_local, to_utc

# --------------------------------------------------------------------------- #
# Auditable timing bounds for reported_at
# --------------------------------------------------------------------------- #

# 客户端时钟偏移容忍窗口：reported_at 最多领先服务时钟 60 秒。
FUTURE_SKEW = timedelta(seconds=60)
# 预报上限：reported_at 早于 effective_from 不得超过 14 天。
MAX_ADVANCE = timedelta(days=14)
# 迟报上限：reported_at 晚于 effective_from 不得超过 24 小时；更迟的事件
# 只能走权威更正通道，避免陈旧事件改写当前窗口。
MAX_LATENESS = timedelta(hours=24)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

def canonicalize(
    dt: datetime, airport: Airport
) -> tuple[datetime, datetime]:
    """返回 (UTC 瞬时, 机场本地墙钟时间)。

    UTC 形式用于所有比较；本地形式仅用于错误明细与跨午夜判定。输入若带
    固定偏移，其绝对时刻与时区无关，因此 DST 重复时刻（如柏林 11 月的
    01:30 出现两次）由偏移本身区分，本地往返不会折叠它们。
    """
    utc = to_utc(dt)
    local = utc.astimezone(airport_zone(airport))
    return utc, local


def _detail_times(value: datetime, airport: Airport, key: str, out: dict) -> None:
    utc, local = canonicalize(value, airport)
    out[key] = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    out[f"{key}_local"] = iso_local(local)


# --------------------------------------------------------------------------- #
# Violation helpers
# --------------------------------------------------------------------------- #

def violation(field: str, issue: str, **context: object) -> dict[str, str]:
    item = {"field": field, "issue": issue}
    for key, value in context.items():
        if isinstance(value, bool):
            item[key] = "true" if value else "false"
        else:
            item[key] = str(value)
    return item


# --------------------------------------------------------------------------- #
# Causal rules for a single chain edge (shared by intake validation and audit)
# --------------------------------------------------------------------------- #

def edge_violations(
    child: DisruptionEvent,
    parent: DisruptionEvent,
    root: DisruptionEvent,
    airport: Airport,
) -> list[dict[str, str]]:
    """检查 child 相对其直接前驱 parent（链根为 root）的时间因果。

    调用方需已确认父子属于同一机场、parent 存在。本函数只做与结构无关的
    纯时间比较，接入校验与启动审计对同一条边必须得到相同结论。
    """
    errors: list[dict[str, str]] = []

    if parent.event_type == EVENT_REOPENED:
        errors.append(
            violation(
                "supersedes_event_id",
                "chain_already_closed",
                event_id=parent.event_id,
            )
        )
        # 已终结链上的时间窗口不再有意义，跳过后续时间比较。
        return errors

    child_from, _ = canonicalize(child.effective_from, airport)
    root_from, _ = canonicalize(root.effective_from, airport)
    parent_until = (
        canonicalize(parent.effective_until, airport)[0]
        if parent.effective_until is not None
        else None
    )

    if child.event_type == EVENT_EXTENDED:
        # 延长段不能早于根关闭：effective_from 写在整条链起点之前会让
        # 并集窗口的起点倒挂（审计抽查到的第二类异常）。
        if child_from < root_from:
            ctx: dict[str, str] = {}
            _detail_times(child.effective_from, airport, "received_effective_from", ctx)
            _detail_times(root.effective_from, airport, "chain_start", ctx)
            errors.append(
                violation(
                    "effective_from",
                    "must_not_precede_root_closure",
                    **ctx,
                )
            )

        if parent_until is not None:
            # 左闭右开：from == parent_until 是合法的端点相接；严格大于
            # 才是“跨过未说明的开放间隙”。
            if child_from > parent_until:
                gap_ctx: dict[str, str] = {}
                _detail_times(child.effective_from, airport, "received_effective_from", gap_ctx)
                _detail_times(parent.effective_until, airport, "previous_effective_until", gap_ctx)
                errors.append(
                    violation(
                        "effective_from",
                        "extension_leaves_uncovered_gap",
                        **gap_ctx,
                    )
                )
            child_until, _ = canonicalize(child.effective_until, airport)
            if child_until <= parent_until:
                end_ctx: dict[str, str] = {}
                _detail_times(child.effective_until, airport, "received_effective_until", end_ctx)
                _detail_times(parent.effective_until, airport, "previous_effective_until", end_ctx)
                errors.append(
                    violation(
                        "effective_until",
                        "must_extend_previous_window",
                        **end_ctx,
                    )
                )
        # 前驱为开放窗口（effective_until=null）时，延长只是补上新获知的
        # 末端，不存在间隙/推后比较。

    elif child.event_type == EVENT_REOPENED:
        reopen_at = child_from
        resume_at = reopen_at + timedelta(minutes=airport.reopen_buffer_minutes)

        # 恢复时点不能早于根关闭（审计抽查到的第一类异常）。
        if reopen_at < root_from:
            ctx = {}
            _detail_times(child.effective_from, airport, "received_effective_from", ctx)
            _detail_times(root.effective_from, airport, "chain_start", ctx)
            errors.append(
                violation("effective_from", "reopen_before_chain_start", **ctx)
            )

        if parent_until is not None:
            if reopen_at > parent_until:
                # 恢复点本身已越过当前有效关闭末端：中间是一段未建模的开放时间。
                ctx = {}
                _detail_times(child.effective_from, airport, "received_effective_from", ctx)
                _detail_times(parent.effective_until, airport, "current_closure_end", ctx)
                errors.append(
                    violation(
                        "effective_from",
                        "reopen_outside_closure_window",
                        **ctx,
                    )
                )
            elif resume_at > parent_until:
                # 恢复点在窗口内，但“机场缓冲结束”越过关闭末端：运营恢复
                # 时刻必须仍落在当前有效关闭范围内（端点相接合法）。
                ctx = {
                    "buffer_minutes": str(airport.reopen_buffer_minutes),
                }
                _detail_times(resume_at, airport, "resume_at", ctx)
                _detail_times(parent.effective_until, airport, "current_closure_end", ctx)
                errors.append(
                    violation(
                        "effective_from",
                        "reopen_buffer_extends_past_closure",
                        **ctx,
                    )
                )

    # reported_at 沿链必须单调不减：后继事件不可能比它接续的事件更早被接报。
    child_reported, _ = canonicalize(child.reported_at, airport)
    parent_reported, _ = canonicalize(parent.reported_at, airport)
    if child_reported < parent_reported:
        ctx = {}
        _detail_times(child.reported_at, airport, "received_reported_at", ctx)
        _detail_times(parent.reported_at, airport, "previous_reported_at", ctx)
        errors.append(
            violation("reported_at", "reported_at_out_of_order", **ctx)
        )

    return errors


# --------------------------------------------------------------------------- #
# Intake-only: clock-dependent reported_at rules
# --------------------------------------------------------------------------- #

def timing_violations(
    event: DisruptionEvent,
    airport: Airport,
    *,
    now: datetime,
) -> list[dict[str, str]]:
    """reported_at 的未来事件 / 迟报事件规则。

    * 领先服务时钟超过 :data:`FUTURE_SKEW` 的接报时刻视为未来事件，拒绝
      （边界 ``now + 60s`` 取左闭，恰好相等放行）。
    * ``reported_at`` 早于 ``effective_from`` 超过 :data:`MAX_ADVANCE` 视为
      超出预报窗口。
    * ``reported_at`` 晚于 ``effective_from`` 超过 :data:`MAX_LATENESS` 视为
      迟报，需走权威更正流程。
    """
    errors: list[dict[str, str]] = []
    now_utc = to_utc(now)
    reported, _ = canonicalize(event.reported_at, airport)
    effective, _ = canonicalize(event.effective_from, airport)

    if reported > now_utc + FUTURE_SKEW:
        ctx: dict[str, str] = {"skew_seconds": str(int(FUTURE_SKEW.total_seconds()))}
        _detail_times(event.reported_at, airport, "received_reported_at", ctx)
        ctx["now"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        errors.append(violation("reported_at", "reported_at_in_future", **ctx))

    if reported < effective - MAX_ADVANCE:
        ctx = {"max_advance_minutes": str(int(MAX_ADVANCE.total_seconds() // 60))}
        _detail_times(event.reported_at, airport, "received_reported_at", ctx)
        _detail_times(event.effective_from, airport, "effective_from", ctx)
        errors.append(
            violation("reported_at", "reported_too_far_in_advance", **ctx)
        )

    if reported > effective + MAX_LATENESS:
        ctx = {"max_lateness_minutes": str(int(MAX_LATENESS.total_seconds() // 60))}
        _detail_times(event.reported_at, airport, "received_reported_at", ctx)
        _detail_times(event.effective_from, airport, "effective_from", ctx)
        errors.append(violation("reported_at", "reported_too_late", **ctx))

    return errors


# --------------------------------------------------------------------------- #
# Startup audit over fully persisted chains
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChainAnomaly:
    """一条被判定为异常的链（或无闭合根的孤立事件组）。"""

    group_id: str
    airport_code: str
    reasons: tuple[str, ...]
    members: frozenset[str]
    violations: tuple[dict[str, str], ...]
    detected_at: datetime  # 取组内最晚 reported_at，保证重建后判定一致


def _walk_root(
    event: DisruptionEvent, events: dict[str, DisruptionEvent]
) -> tuple[DisruptionEvent | None, bool]:
    """沿 supersedes 链找 closed 根。返回 (root, 链是否完整可达)。"""
    current = event
    seen: set[str] = set()
    while True:
        if current.event_type == EVENT_CLOSED:
            return current, True
        ref_id = current.supersedes_event_id
        if ref_id is None:
            return None, False
        if ref_id in seen:  # 防御：环不可能由正常接入产生，但历史数据可能有
            return None, False
        seen.add(ref_id)
        current = events.get(ref_id)
        if current is None:
            return None, False


def _root_id(event: DisruptionEvent, events: dict[str, DisruptionEvent]) -> str | None:
    """沿 supersedes 链找 closed 根的 id；不可达（悬空/环/无 closed 根）返回 None。"""
    root, complete = _walk_root(event, events)
    return root.event_id if (complete and root is not None) else None


def audit_stored(
    events: dict[str, DisruptionEvent],
    airports: dict[str, Airport],
) -> list[ChainAnomaly]:
    """对已持久化事件做只依赖事件内容的因果审计。

    输出是确定性的：同一组事件在任意时间、任意容器重建后得到相同分组、
    相同违规原因与相同成员集合。开放中（尚未恢复）的链不是异常；只有
    违反时间因果、分叉、悬空引用等才会被标记。
    """
    # 1. 分组：可达同一 closed 根的事件归为一组；不可达事件（悬空引用、
    #    环、无 closed 根）按连通关系归簇，簇内找不到 closed 根。
    root_ids = {eid: _root_id(e, events) for eid, e in events.items()}
    groups: dict[str, list[DisruptionEvent]] = {}
    for event in sorted(events.values(), key=lambda e: e.event_id):
        root_id = root_ids[event.event_id]
        if root_id is not None:
            groups.setdefault(root_id, []).append(event)
        else:
            # 以不可达引用的并查形式把相互指向的事件归到同一簇。
            key = _cluster_key(event, events, root_ids)
            groups.setdefault(key, []).append(event)

    anomalies: list[ChainAnomaly] = []
    for group_id in sorted(groups):
        members_events = groups[group_id]
        by_id = {e.event_id: e for e in members_events}
        airport = airports.get(members_events[0].airport_code)
        violations: list[dict[str, str]] = []

        # 组的 closed 根就是 group_id 对应的事件（若它存在且是 closed）。
        root = events.get(group_id)
        if root is None or root.event_type != EVENT_CLOSED:
            root = None

        # 组成员必须同属一个机场。
        codes = {e.airport_code for e in members_events}
        if len(codes) > 1:
            violations.append(
                violation(
                    "airport_code",
                    "chain_airport_mismatch",
                    airports=", ".join(sorted(codes)),
                )
            )

        # 分叉：同一个事件被两个及以上后继接续。
        successor_counts: dict[str, int] = {}
        for e in members_events:
            if e.supersedes_event_id:
                successor_counts[e.supersedes_event_id] = (
                    successor_counts.get(e.supersedes_event_id, 0) + 1
                )
        for parent_id, count in sorted(successor_counts.items()):
            if count > 1:
                violations.append(
                    violation(
                        "supersedes_event_id",
                        "chain_branched",
                        event_id=parent_id,
                        successor_count=str(count),
                    )
                )

        # 逐边检查（按版本、id 排序保证结论顺序稳定）。
        for child in sorted(members_events, key=lambda e: (e.event_version, e.event_id)):
            ref_id = child.supersedes_event_id
            if ref_id is None:
                if child.event_type != EVENT_CLOSED:
                    violations.append(
                        violation(
                            "supersedes_event_id",
                            "chain_without_closed_root",
                            event_id=child.event_id,
                        )
                    )
                continue

            parent = events.get(ref_id)
            if parent is None:
                violations.append(
                    violation(
                        "supersedes_event_id",
                        "unknown_event",
                        event_id=ref_id,
                    )
                )
                continue
            if parent.event_id not in by_id:
                violations.append(
                    violation(
                        "supersedes_event_id",
                        "chain_cross_reference",
                        event_id=ref_id,
                    )
                )
                continue

            if root is None:
                violations.append(
                    violation(
                        "supersedes_event_id",
                        "chain_without_closed_root",
                        event_id=child.event_id,
                    )
                )
            elif airport is not None:
                violations.extend(edge_violations(child, parent, root, airport))

            if child.event_version <= parent.event_version:
                violations.append(
                    violation(
                        "event_version",
                        "version_must_increase",
                        stored_version=str(parent.event_version),
                        received_version=str(child.event_version),
                    )
                )

        if not violations:
            continue

        reasons = tuple(sorted({v["issue"] for v in violations}))
        detected_at = max(to_utc(e.reported_at) for e in members_events)
        anomalies.append(
            ChainAnomaly(
                group_id=group_id,
                airport_code=members_events[0].airport_code,
                reasons=reasons,
                members=frozenset(by_id),
                violations=tuple(violations),
                detected_at=detected_at,
            )
        )

    return anomalies


def _cluster_key(
    event: DisruptionEvent,
    events: dict[str, DisruptionEvent],
    root_ids: dict[str, str | None],
) -> str:
    """不可达事件的簇键：沿 supersedes 向上走到最早的已知祖先 id。"""
    current = event
    seen: set[str] = set()
    while current.supersedes_event_id is not None:
        ref_id = current.supersedes_event_id
        if ref_id in seen:
            break
        seen.add(ref_id)
        parent = events.get(ref_id)
        if parent is None or root_ids.get(parent.event_id) is not None:
            break
        current = parent
    return f"orphan:{current.event_id}"


def quarantine_members(anomalies: Iterable[ChainAnomaly]) -> frozenset[str]:
    """所有异常组成员事件 id 的并集——这些链不参与当前汇总。"""
    members: set[str] = set()
    for anomaly in anomalies:
        members.update(anomaly.members)
    return frozenset(members)
