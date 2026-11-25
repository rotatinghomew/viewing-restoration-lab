"""Core evidence aggregation and compensation decisions.

The engine is deliberately deterministic and side-effect free.  It consumes the
same dictionary rows validated by :mod:`intake` / :mod:`loss_service.store`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .intervals import (
    Interval,
    clip,
    contains_all,
    intersect,
    intersection_of_all,
    length,
    merge,
    subtract,
    subtract_merged,
    union,
)

INFINITE = 10**30


def split_multi(value: Any) -> list[str]:
    if value is None or value == "-":
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def material_time(tables: dict[str, list[dict[str, Any]]], explicit: int | None = None) -> int:
    if explicit is not None:
        return int(explicit)
    values: list[int] = []
    for table, field in (
        ("playback", "known_at"),
        ("outages", "known_at"),
        ("calibration", "known_at"),
        ("receipts", "utc"),
        ("rewatched", "utc"),
    ):
        values.extend(int(r[field]) for r in tables.get(table, []))
    return max(values) if values else 0


def _sequence(row: dict[str, Any], fallback: int) -> int:
    value = row.get("_seq")
    return int(value) if value is not None else fallback


def _delivered(rows: Iterable[dict[str, Any]], field: str, as_of: int) -> list[dict[str, Any]]:
    return [r for r in rows if int(r[field]) <= as_of]


@dataclass
class CalibrationDomain:
    session: str
    status: str
    offset_min: int | None = None
    offset_max: int | None = None
    limit_min: int | None = None
    limit_max: int | None = None
    anchors: list[dict[str, Any]] | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "status": self.status,
            "offset_min": self.offset_min,
            "offset_max": self.offset_max,
            "inclusive": True,
            "limit_min": self.limit_min,
            "limit_max": self.limit_max,
            "anchors": self.anchors or [],
            "reason": self.reason,
        }


def calibration_domains(
    tables: dict[str, list[dict[str, Any]]], as_of: int
) -> dict[str, CalibrationDomain]:
    limits = {r["session"]: r for r in tables.get("session_limits", [])}
    latest_anchor: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(_delivered(tables.get("calibration", []), "known_at", as_of)):
        key = (row["session"], row["anchor"])
        order = _sequence(row, index)
        old = latest_anchor.get(key)
        if old is None or (int(row["known_at"]), order) >= (
            int(old["known_at"]),
            int(old.get("_choice_order", -1)),
        ):
            enriched = dict(row)
            enriched["_choice_order"] = order
            latest_anchor[key] = enriched

    sessions = set(limits)
    sessions.update(s for s, _ in latest_anchor)
    sessions.update(r["session"] for r in tables.get("playback", []) if r["session"] != "-")

    result: dict[str, CalibrationDomain] = {}
    for session in sorted(sessions):
        limit = limits.get(session)
        lo = int(limit["offset_min"]) if limit else -INFINITE
        hi = int(limit["offset_max"]) if limit else INFINITE
        anchors = [
            {
                "anchor": r["anchor"],
                "offset_min": int(r["offset_min"]),
                "offset_max": int(r["offset_max"]),
                "known_at": int(r["known_at"]),
            }
            for (s, _anchor), r in sorted(latest_anchor.items())
            if s == session
        ]
        for anchor in anchors:
            lo = max(lo, int(anchor["offset_min"]))
            hi = min(hi, int(anchor["offset_max"]))

        if lo > hi:
            result[session] = CalibrationDomain(
                session=session,
                status="incompatible",
                offset_min=None,
                offset_max=None,
                limit_min=int(limit["offset_min"]) if limit else None,
                limit_max=int(limit["offset_max"]) if limit else None,
                anchors=anchors,
                reason="最新锚点范围与初始位移范围没有共同整数位移",
            )
        elif not limit and (lo <= -INFINITE // 2 or hi >= INFINITE // 2):
            result[session] = CalibrationDomain(
                session=session,
                status="unbounded",
                offset_min=lo if lo > -INFINITE // 2 else None,
                offset_max=hi if hi < INFINITE // 2 else None,
                anchors=anchors,
                reason="缺少 session_limits 且校准没有给出有限范围",
            )
        else:
            result[session] = CalibrationDomain(
                session=session,
                status="ok",
                offset_min=lo,
                offset_max=hi,
                limit_min=int(limit["offset_min"]) if limit else None,
                limit_max=int(limit["offset_max"]) if limit else None,
                anchors=anchors,
            )
    return result


def _program(tables: dict[str, list[dict[str, Any]]], program_id: str) -> dict[str, Any] | None:
    return next((p for p in tables.get("programs", []) if p["id"] == program_id), None)


def _outage_map(
    tables: dict[str, list[dict[str, Any]]], program_id: str, as_of: int, duration: int
) -> dict[str, list[Interval]]:
    result: dict[str, list[Interval]] = {}
    for outage in _delivered(tables.get("outages", []), "known_at", as_of):
        if outage["program"] != program_id:
            continue
        interval = clip([(int(outage["from_pos"]), int(outage["to_pos"]))], 0, duration)
        if not interval:
            continue
        result.setdefault(outage["source"], []).extend(interval)
    return {source: merge(intervals) for source, intervals in result.items()}


def aggregate_receipts(
    tables: dict[str, list[dict[str, Any]]], order: str, program_id: str, as_of: int
) -> list[dict[str, Any]]:
    rows = [
        r
        for r in tables.get("receipts", [])
        if r["order"] == order and r["program"] == program_id and int(r["utc"]) <= as_of
    ]
    events: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        item = dict(row)
        item["_order"] = _sequence(row, index)
        events.setdefault(row["event"], []).append(item)

    result: list[dict[str, Any]] = []
    for event, status_rows in sorted(events.items()):
        status_rows.sort(key=lambda r: (int(r["utc"]), int(r["_order"])))
        first = status_rows[0]
        latest = status_rows[-1]
        posted_rows = [r for r in status_rows if r["state"] == "posted"]
        effective = posted_rows[-1] if posted_rows else latest
        current_state = latest["state"]
        if posted_rows and current_state != "posted":
            current_state = "posted_with_later_status"
        result.append(
            {
                "event": event,
                "order": order,
                "program": program_id,
                "kind": first["kind"],
                "state": current_state,
                "posted": bool(posted_rows),
                "first_utc": int(first["utc"]),
                "latest_utc": int(latest["utc"]),
                "from_pos": int(first["from_pos"]),
                "to_pos": int(first["to_pos"]),
                "interval": [int(first["from_pos"]), int(first["to_pos"])],
                "status_history": [
                    {"state": r["state"], "utc": int(r["utc"])} for r in status_rows
                ],
            }
        )
    return result


def legal_rewatched(
    tables: dict[str, list[dict[str, Any]]],
    person: str,
    program_id: str,
    region: str,
    as_of: int,
    duration: int,
) -> tuple[list[Interval], list[dict[str, Any]]]:
    replays = {r["id"]: r for r in tables.get("replays", [])}
    valid: list[Interval] = []
    problems: list[dict[str, Any]] = []
    for row in tables.get("rewatched", []):
        if row["person"] != person or row["program"] != program_id or int(row["utc"]) > as_of:
            continue
        piece = clip([(int(row["from_pos"]), int(row["to_pos"]))], 0, duration)
        replay = replays.get(row["replay"])
        ok = bool(piece)
        reason = None
        if replay is None:
            ok = False
            reason = "回放记录不存在"
        else:
            if replay["region"] != region or program_id not in split_multi(replay["programs"]):
                ok = False
                reason = "回放地区或节目不符"
            elif not (int(replay["start"]) <= int(row["utc"]) <= int(replay["until"])):
                ok = False
                reason = "回看时间不在回放有效期"
            else:
                covered = clip(
                    [(int(replay["covered_from"]), int(replay["covered_to"]))], 0, duration
                )
                if not piece or any(
                    not contains_all(covered, p) for p in piece
                ):
                    ok = False
                    reason = "回看片段不在回放覆盖范围"
        if ok and piece:
            valid.extend(piece)
        else:
            problems.append(
                {
                    "replay": row["replay"],
                    "interval": [row["from_pos"], row["to_pos"]],
                    "utc": int(row["utc"]),
                    "reason": reason or "片段超出节目范围",
                }
            )
    return merge(valid), problems


def _summarize_cut(
    confirmed_worlds: list[list[Interval]],
    possible_worlds: list[list[Interval]],
    cutter: list[Interval],
) -> dict[str, Any]:
    cut_conf = [subtract(world, cutter) for world in confirmed_worlds]
    cut_poss = [subtract(world, cutter) for world in possible_worlds]
    definite = intersection_of_all(cut_conf) if cut_conf else []
    possible: list[Interval] = []
    for world in cut_poss:
        possible = union(possible, world)
    confirmed_seconds = [length(x) for x in cut_conf]
    upper_seconds = [length(x) for x in cut_poss]
    return {
        "lower_seconds": min(confirmed_seconds, default=0),
        "upper_seconds": max(upper_seconds, default=0),
        "definite": [[a, b] for a, b in definite],
        "possible": [[a, b] for a, b in possible],
        "definite_named_seconds": length(definite),
        "world_count": len(cut_conf),
    }


def _new_accumulator() -> dict[str, Any]:
    return {
        "lower": INFINITE,
        "upper": -1,
        "definite": None,
        "possible_parts": [],
        "worlds": 0,
    }


def _update_accumulator(
    acc: dict[str, Any], confirmed: list[Interval], possible: list[Interval]
) -> None:
    acc["lower"] = min(acc["lower"], length(confirmed))
    acc["upper"] = max(acc["upper"], length(possible))
    if acc["definite"] is None:
        acc["definite"] = confirmed
    elif acc["definite"]:
        acc["definite"] = intersect_intervals(acc["definite"], confirmed)
    acc["possible_parts"].extend(possible)
    acc["worlds"] += 1


def _finish_accumulator(acc: dict[str, Any]) -> dict[str, Any]:
    definite = acc["definite"] or []
    possible = merge(acc["possible_parts"])
    return {
        "lower_seconds": 0 if acc["lower"] == INFINITE else acc["lower"],
        "upper_seconds": max(0, acc["upper"]),
        "definite": [[a, b] for a, b in definite],
        "possible": [[a, b] for a, b in possible],
        "definite_named_seconds": length(definite),
        "world_count": acc["worlds"],
    }


def intersect_intervals(a: list[Interval], b: list[Interval]) -> list[Interval]:
    from .intervals import intersect

    return intersect(a, b)


def replay_fulfillment(
    tables: dict[str, list[dict[str, Any]]],
    region: str,
    program_id: str,
    segment: Interval,
    as_of: int,
) -> list[dict[str, Any]]:
    """Return replay rows which geometrically cover segment and leave enough time."""
    a, b = segment
    needed = b - a
    answers: list[dict[str, Any]] = []
    for replay in tables.get("replays", []):
        if replay["region"] != region or program_id not in split_multi(replay["programs"]):
            continue
        covered = merge(
            clip([(int(replay["covered_from"]), int(replay["covered_to"]))], 0, INFINITE)
        )
        geometric = intersect_segment_with_coverage(segment, covered)
        if not geometric:
            continue
        start = int(replay["start"])
        until = int(replay["until"])
        available_seconds = until - max(as_of, start)
        for ga, gb in geometric:
            seg_len = gb - ga
            answers.append(
                {
                    "replay": replay["id"],
                    "segment": [ga, gb],
                    "seconds": seg_len,
                    "window_start": start,
                    "until": until,
                    "available_watch_seconds": max(0, available_seconds),
                    "fulfillable": available_seconds >= seg_len,
                    "reason": None
                    if available_seconds >= seg_len
                    else "有效期内剩余时间不足",
                }
            )
    return answers


def replay_interval_audit(
    tables: dict[str, list[dict[str, Any]]],
    region: str,
    program_id: str,
    interval: Interval,
    as_of: int,
) -> dict[str, Any]:
    """Check whether one recorded receipt interval can be fulfilled as written."""
    options = replay_fulfillment(tables, region, program_id, interval, as_of)
    geometric = merge([(tuple(o["segment"])) for o in options])  # type: ignore[arg-type]
    gaps = subtract([interval], geometric)
    full_by_one = any(
        tuple(o["segment"]) == tuple(interval) and o["fulfillable"] for o in options
    )
    return {
        "interval": [interval[0], interval[1]],
        "seconds": interval[1] - interval[0],
        "geometric_coverage": [[a, b] for a, b in geometric],
        "coverage_gaps": [[a, b] for a, b in gaps],
        "fully_covered_geometrically": not gaps,
        "fulfillable_as_recorded": full_by_one,
        "options": options,
    }


def intersect_segment_with_coverage(
    segment: Interval, coverage: list[Interval]
) -> list[Interval]:
    from .intervals import intersect_one

    return intersect_one(coverage, segment)


def assess_entitlement(
    tables: dict[str, list[dict[str, Any]]],
    entitlement: dict[str, Any],
    as_of: int | None = None,
    *,
    audit_receipts: bool = True,
    _cache: dict[Any, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    as_of = material_time(tables, as_of)
    program_id = entitlement["program"]
    core_key = (
        id(tables),
        entitlement.get("order"),
        entitlement.get("program"),
        as_of,
        False,
    )
    if audit_receipts:
        core = assess_entitlement(
            tables,
            entitlement,
            as_of,
            audit_receipts=False,
            _cache=_cache,
        )
        if not core.get("_receipt_audit_done"):
            for event in core["receipts"]:
                snapshot_at = event["first_utc"]
                if snapshot_at == as_of:
                    snapshot = core
                else:
                    snapshot = assess_entitlement(
                        tables,
                        entitlement,
                        snapshot_at,
                        audit_receipts=False,
                        _cache=_cache,
                    )
                event["decision_snapshot"] = {
                    "as_of": snapshot_at,
                    "decision": snapshot["decision"],
                    "calibration": snapshot["calibration"],
                    "loss": snapshot["loss"],
                    "receipt_interval_supported": interval_support(
                        event["interval"], snapshot["loss"]
                    ),
                    "replay_audit": replay_interval_audit(
                        tables,
                        entitlement["region"],
                        program_id,
                        tuple(event["interval"]),  # type: ignore[arg-type]
                        snapshot_at,
                    )
                    if event["kind"] == "replay"
                    else None,
                    "replay_check": replay_fulfillment(
                        tables,
                        entitlement["region"],
                        program_id,
                        tuple(event["interval"]),  # type: ignore[arg-type]
                        snapshot_at,
                    )
                    if event["kind"] == "replay"
                    else None,
                }
            core["_receipt_audit_done"] = True
        return core

    if _cache is not None and core_key in _cache:
        return _cache[core_key]
    program_id = entitlement["program"]
    program = _program(tables, program_id)
    if program is None:
        raise ValueError(f"缺少节目表：{program_id}")

    start_utc = int(program["start_utc"])
    duration = int(program["end_utc"]) - start_utc
    valid_from = max(0, int(entitlement["buy_utc"]) - start_utc)
    valid_to = duration
    entitlement_active = int(entitlement["buy_utc"]) <= as_of and valid_from < valid_to

    domains = calibration_domains(tables, as_of)
    outages = _outage_map(tables, program_id, as_of, duration)
    scope = set(split_multi(entitlement.get("source_scope")))

    playback = [
        r
        for r in _delivered(tables.get("playback", []), "known_at", as_of)
        if r["person"] == entitlement["person"]
        and r["program"] == program_id
        and (not scope or r["source"] in scope)
    ]

    bad_sessions: list[dict[str, Any]] = []
    variables: list[tuple[str, int, int]] = []
    for session, domain in domains.items():
        if not any(r["session"] == session for r in playback):
            continue
        if domain.status == "ok" and domain.offset_min is not None and domain.offset_max is not None:
            variables.append((session, domain.offset_min, domain.offset_max))
        else:
            rows = [r["id"] for r in playback if r["session"] == session]
            bad_sessions.append(
                {
                    **domain.as_dict(),
                    "playback_ids": rows,
                    "impact": "该会话片段不参与确定结论；可能覆盖关系按初始范围保守排除",
                }
            )

    # A contradictory session may still have trustworthy initial limits.  Use
    # their envelope only as a coverage block: if an unalignable playing
    # fragment might cover a point, that point cannot be called net-missed.
    blocked_coverage: list[Interval] = []
    fallback_possible: list[Interval] = []
    unaligned_envelopes: list[dict[str, Any]] = []
    for bad in bad_sessions:
        session = bad["session"]
        limit_lo = bad.get("limit_min")
        limit_hi = bad.get("limit_max")
        for row in playback:
            if row["session"] != session:
                continue
            if limit_lo is None or limit_hi is None:
                envelope = (valid_from, valid_to)
            else:
                from .intervals import offset_envelope

                envelope = offset_envelope(
                    int(row["from_pos"]), int(row["to_pos"]), int(limit_lo), int(limit_hi)
                )
                envelope = clip([envelope], valid_from, valid_to)[0] if envelope else None
            if envelope:
                unaligned_envelopes.append(
                    {"id": row["id"], "state": row["state"], "envelope": list(envelope)}
                )
                if row["state"] == "playing":
                    blocked_coverage.append(envelope)
                else:
                    fallback_possible.extend(
                        intersect([envelope], outages.get(row["source"], []))
                    )
            if envelope and row["state"] != "playing":
                # The initial limits still give a conservative outage ceiling
                # even when later calibration anchors are mutually contradictory.
                lo = bad.get("limit_min")
                start = int(row["from_pos"]) + (int(lo) if lo is not None else -INFINITE)
                fallback_possible.extend(
                    clip(outages.get(row["source"], []), max(valid_from, start), valid_to)
                )
    blocked_coverage = merge(blocked_coverage)
    fallback_possible = merge(fallback_possible)

    receipts = aggregate_receipts(tables, entitlement["order"], program_id, as_of)
    posted_intervals = [
        (r["from_pos"], r["to_pos"]) for r in receipts if r.get("posted")
    ]
    unknown_intervals = [
        (r["from_pos"], r["to_pos"]) for r in receipts if r["state"] == "unknown"
    ]
    rewatched, rewatch_problems = legal_rewatched(
        tables,
        entitlement["person"],
        program_id,
        entitlement["region"],
        as_of,
        duration,
    )
    compensated_cutter = merge([*rewatched, *posted_intervals])
    hold_cutter = merge([*compensated_cutter, *unknown_intervals])

    # Protect the HTTP/API process from pathological Cartesian products.  The
    # shipped synthetic data is small; large real ranges fall back to a clear
    # manual/degraded result rather than hanging customer service.
    world_count_estimate = 1
    too_large = False
    for _s, lo, hi in variables:
        world_count_estimate *= max(1, hi - lo + 1)
        if world_count_estimate > 200_000:
            too_large = True
            break
    if too_large:
        from .intervals import offset_envelope

        for session, lo, hi in variables:
            for row in playback:
                if row["session"] != session:
                    continue
                envelope = offset_envelope(
                    int(row["from_pos"]), int(row["to_pos"]), lo, hi
                )
                if not envelope:
                    continue
                if row["state"] == "playing":
                    blocked_coverage.append(envelope)
                else:
                    clipped = intersect([envelope], outages.get(row["source"], []))
                    fallback_possible.extend(clipped)
                    start = int(row["from_pos"]) + lo
                    fallback_possible.extend(
                        clip(outages.get(row["source"], []), max(valid_from, start), valid_to)
                    )
        blocked_coverage = merge(blocked_coverage)
        fallback_possible = merge(fallback_possible)
        bad_sessions.append(
            {
                "session": ",".join(s for s, _l, _h in variables),
                "status": "range_too_large",
                "reason": "校准位移组合过多，已保守排除可能被覆盖片段并转人工区间核算",
                "playback_ids": [],
            }
        )
        variables = []

    live_acc = _new_accumulator()
    observed_acc = _new_accumulator()
    rewatch_acc = _new_accumulator()
    compensated_acc = _new_accumulator()
    available_acc = _new_accumulator()

    def evaluate(assignments: dict[str, int]) -> None:
        playing: list[Interval] = []
        stalled: list[Interval] = []
        observed_unknown: list[Interval] = []
        source_starts: dict[str, int] = {}

        for row in playback:
            session = row["session"]
            if session == "-":
                offset = 0
            elif session in assignments:
                offset = assignments[session]
            else:
                continue  # unalignable/unbounded session
            mapped = clip(
                [(int(row["from_pos"]) + offset, int(row["to_pos"]) + offset)],
                valid_from,
                valid_to,
            )
            if not mapped:
                continue
            source = row["source"]
            source_starts[source] = min(source_starts.get(source, INFINITE), mapped[0][0])
            if row["state"] == "playing":
                playing.extend(mapped)
            elif row["state"] == "stalled":
                stalled.extend(intersect_outages(mapped, outages.get(source, [])))
            elif row["state"] == "missing":
                # The row itself is the player/session attempt record.  The
                # outage intersection is mandatory.
                observed_unknown.extend(intersect_outages(mapped, outages.get(source, [])))

        playing = merge(playing)
        stalled = merge(stalled)
        observed_candidate = union(stalled, observed_unknown)
        observed_possible = subtract_merged(observed_candidate, playing)

        # True upper ceiling: after a source has even one playback attempt,
        # the remainder of a confirmed matching outage is observation-unknown.
        outage_ceiling: list[Interval] = []
        for source, ranges in outages.items():
            if source not in source_starts:
                continue
            start = source_starts[source]
            outage_ceiling.extend(clip(ranges, max(valid_from, start), valid_to))
        upper_candidate = union(stalled, outage_ceiling, fallback_possible)
        conf_net = subtract_merged(stalled, playing)
        conf_safe = subtract_merged(conf_net, blocked_coverage)
        poss_net = subtract_merged(upper_candidate, playing)

        _update_accumulator(live_acc, conf_safe, poss_net)
        _update_accumulator(observed_acc, conf_safe, observed_possible)
        rw_conf = subtract_merged(conf_safe, rewatched)
        rw_poss = subtract_merged(poss_net, rewatched)
        _update_accumulator(rewatch_acc, rw_conf, rw_poss)
        comp_conf = subtract_merged(conf_safe, compensated_cutter)
        comp_poss = subtract_merged(poss_net, compensated_cutter)
        _update_accumulator(compensated_acc, comp_conf, comp_poss)
        avail_conf = subtract_merged(conf_safe, hold_cutter)
        avail_poss = subtract_merged(poss_net, hold_cutter)
        _update_accumulator(available_acc, avail_conf, avail_poss)

    if not variables:
        evaluate({})
    else:
        ranges = [range(lo, hi + 1) for _s, lo, hi in variables]

        def enumerate_worlds(index: int, assigned: dict[str, int]) -> None:
            if index == len(variables):
                evaluate(assigned)
                return
            session = variables[index][0]
            for value in ranges[index]:
                next_assigned = dict(assigned)
                next_assigned[session] = value
                enumerate_worlds(index + 1, next_assigned)

        enumerate_worlds(0, {})

    live = _finish_accumulator(live_acc)
    observed = _finish_accumulator(observed_acc)
    observed_upper = observed["upper_seconds"]
    after_rewatch = _finish_accumulator(rewatch_acc)
    compensated = _finish_accumulator(compensated_acc)
    available = _finish_accumulator(available_acc)

    critical = clip(
        [(int(program["critical_from"]), int(program["critical_to"]))], valid_from, valid_to
    )
    ordinary_available_definite = subtract(
        [tuple(x) for x in available["definite"]], critical
    )
    critical_available_definite = intersection_of_all(
        [[tuple(x) for x in available["definite"]], [tuple(x) for x in critical]]
    )

    threshold = int(program["threshold"])
    credit_value = int(program["credit"])
    upper = live["upper_seconds"]
    lower = live["lower_seconds"]
    review = upper >= threshold
    eligible = lower >= threshold and entitlement_active

    replay_recommendations: list[dict[str, Any]] = []
    rule_clear = eligible and not bad_sessions
    coverable_groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for segment in ordinary_available_definite:
        candidates = replay_fulfillment(
            tables, entitlement["region"], program_id, segment, as_of
        )
        for option in candidates:
            if option["fulfillable"]:
                key = (int(option["segment"][0]), int(option["segment"][1]))
                coverable_groups.setdefault(key, []).append(option)

    for key, options in sorted(coverable_groups.items()):
        chosen = sorted(
            options, key=lambda c: (-c["seconds"], c["available_watch_seconds"])
        )[0]
        replay_recommendations.append(
            {
                "kind": "replay",
                "segment": [key[0], key[1]],
                "seconds": key[1] - key[0],
                "eligible_by_threshold": eligible,
                "coverage_clear": True,
                "can_issue": rule_clear,
                "requires_manual_approval_due_to_posted_receipt": bool(posted_intervals),
                "suggested_replay": chosen["replay"],
                "fulfillment": chosen,
                "all_replay_options": options,
            }
        )
    covered_replay_segments = merge(list(coverable_groups))

    credit_recommendation = None
    if critical_available_definite:
        credit_recommendation = {
            "kind": "credit",
            "amount": credit_value,
            "rule": "本场 critical 片段按 program.credit 发放一次积分",
            "segments": [[a, b] for a, b in critical_available_definite],
            "clear": len(critical_available_definite) == 1,
            "eligible_by_threshold": eligible,
            "can_issue": rule_clear and len(critical_available_definite) == 1,
            "requires_manual_approval_due_to_posted_receipt": bool(posted_intervals),
        }

    manual_items: list[dict[str, Any]] = []
    for bad in bad_sessions:
        manual_items.append(
            {
                "code": "calibration_unaligned",
                "message": f"会话 {bad['session']} 无法用同一校准位移对齐，需人工校准",
                "details": bad,
            }
        )
    for event in receipts:
        if event["state"] == "unknown":
            manual_items.append(
                {
                    "code": "receipt_unknown",
                    "event": event["event"],
                    "interval": event["interval"],
                    "message": "回执发送状态未明；保留该覆盖，核对前不得重发",
                }
            )
        elif event["state"] == "posted_with_later_status":
            manual_items.append(
                {
                    "code": "receipt_state_conflict",
                    "event": event["event"],
                    "interval": event["interval"],
                    "message": "同一发送标识同时存在已发和后续状态，需支付/回执人工核对",
                }
            )

    if available["upper_seconds"] > available["lower_seconds"]:
        manual_items.append(
            {
                "code": "loss_bounds_open",
                "legacy_code": "loss_location_uncertain",
                "lower_seconds": available["lower_seconds"],
                "upper_seconds": available["upper_seconds"],
                "definite": available["definite"],
                "possible": available["possible"],
                "message": "仍有观察范围外或互斥校准解释造成的不确定范围；确定片段可发，可能片段只能人工复核",
            }
        )

    posted_events = [e for e in receipts if e.get("posted")]
    if posted_events and (available["definite"] or available["upper_seconds"] > 0):
        manual_items.append(
            {
                "code": "posted_receipt_correction_gap",
                "posted_events": [e["event"] for e in posted_events],
                "available": available,
                "message": "已发回执不能撤销或移动；更正后若出现新缺口，不得悄悄重发，转人工核对差额",
            }
        )

    clean_unfulfillable = []
    for segment in ordinary_available_definite:
        for leftover in subtract([segment], covered_replay_segments):
            options = replay_fulfillment(
                tables, entitlement["region"], program_id, leftover, as_of
            )
            clean_unfulfillable.append(
                {
                    "segment": [leftover[0], leftover[1]],
                    "seconds": leftover[1] - leftover[0],
                    "options": options,
                }
            )
    if clean_unfulfillable and eligible:
        manual_items.append(
            {
                "code": "clean_gap_without_fulfillable_replay",
                "segments": clean_unfulfillable,
                "message": "确定漏掉的普通片段没有可覆盖且有效期足够的地区回放",
            }
        )

    issuable_replays = [r for r in replay_recommendations if r["can_issue"]]
    issuable_credit = bool(credit_recommendation and credit_recommendation["can_issue"])
    has_unknown_receipt = any(i["code"] == "receipt_unknown" for i in manual_items)
    has_receipt_conflict = any(i["code"] == "receipt_state_conflict" for i in manual_items)
    if not entitlement_active:
        decision = "entitlement_not_effective_at_as_of"
    elif has_unknown_receipt:
        decision = "hold_unknown_receipt"
    elif has_receipt_conflict:
        decision = "receipt_manual_reconciliation"
    elif issuable_replays or issuable_credit:
        decision = "ready_for_agent_issue" if not posted_events else "manual_review_after_posted"
    elif review:
        decision = "manual_review" if manual_items else "review_only_no_clear_grant"
    elif bad_sessions:
        decision = "calibration_manual"
    elif lower == 0 and upper == 0:
        decision = "no_action"
    else:
        decision = "no_action_below_review_threshold"

    result: dict[str, Any] = {
        "as_of": as_of,
        "order": entitlement["order"],
        "person": entitlement["person"],
        "region": entitlement["region"],
        "program": program_id,
        "entitlement_active": entitlement_active,
        "valid_program_interval": [valid_from, valid_to],
        "program_duration": duration,
        "threshold": threshold,
        "credit_value": credit_value,
        "critical_interval": [program["critical_from"], program["critical_to"]],
        "local_day_boundary_utc": int(program["local_day_boundary_utc"]),
        "crosses_local_day_boundary": start_utc
        < int(program["local_day_boundary_utc"])
        < int(program["end_utc"]),
        "calibration": [d.as_dict() for d in domains.values() if any(r["session"] == d.session for r in playback)],
        "outages": {
            source: [[a, b] for a, b in ranges] for source, ranges in sorted(outages.items())
        },
        "unaligned_playback_envelopes": unaligned_envelopes,
        "loss": {
            # lower is the minimum across coherent worlds.  It can exceed the
            # total length of definite_named_seconds.
            "confirmed_seconds": lower,
            "upper_seconds": upper,
            "observed_only_upper_seconds": observed_upper,
            "observed_possible": observed["possible"],
            "definite": live["definite"],
            "possible": live["possible"],
            "definite_named_seconds": live["definite_named_seconds"],
            "review_required": review,
            "threshold_confirmed_met": eligible,
            "world_count": live["world_count"],
            "calibration_complete": not bad_sessions,
            "upper_bound_method": "coherent_worlds"
            if not bad_sessions
            else "conservative_fallback_envelope",
            "upper_bound_complete": True,
        },
        "rewatched": {
            "intervals": [[a, b] for a, b in rewatched],
            "problems": rewatch_problems,
            "after_rewatch": _public_summary(after_rewatch),
        },
        "receipts": receipts,
        "compensation": {
            "receipt_summary": {
                "posted_credit_events": [e["event"] for e in receipts if e.get("posted") and e["kind"] == "credit"],
                "posted_replay_events": [e["event"] for e in receipts if e.get("posted") and e["kind"] == "replay"],
                "unknown_events": [e["event"] for e in receipts if e["state"] == "unknown"],
                "declined_events": [e["event"] for e in receipts if e["state"] == "declined"],
                "posted_credit_value_if_program_rate": credit_value
                * sum(1 for e in receipts if e.get("posted") and e["kind"] == "credit"),
            },
            "posted_or_rewatched": _public_summary(compensated),
            "available_after_holds": _public_summary(available),
            "posted_intervals": [[a, b] for a, b in posted_intervals],
            "unknown_hold_intervals": [[a, b] for a, b in unknown_intervals],
            "ordinary_definite": [list(x) for x in ordinary_available_definite],
            "critical_definite": critical_available_definite,
            "replay_recommendations": replay_recommendations,
            "credit_recommendation": credit_recommendation,
            "clean_unfulfillable": clean_unfulfillable,
        },
        "manual_items": manual_items,
        "decision": decision,
    }

    if _cache is not None:
        _cache[core_key] = result
    return result


def intersect_outages(mapped: list[Interval], outages: list[Interval]) -> list[Interval]:
    from .intervals import intersect

    return intersect(mapped, outages)


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k != "world_count"}


def interval_support(interval: list[int], loss: dict[str, Any]) -> dict[str, Any]:
    piece = (int(interval[0]), int(interval[1]))
    definite = [tuple(x) for x in loss["definite"]]
    possible = [tuple(x) for x in loss["possible"]]
    return {
        "interval": interval,
        "definitely_missed": contains_all(definite, piece),
        "some_possible_part": bool(intersect_segment_with_coverage(piece, possible)),
        "clear_named_coverage": contains_all(definite, piece),
    }


class LossService:
    """Small facade used by the CLI, HTTP adapter, and tests."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables
        self._cache: dict[Any, dict[str, Any]] = {}

    @classmethod
    def from_directory(cls, directory: str) -> "LossService":
        from intake import read_tables

        return cls(read_tables(directory))

    @classmethod
    def from_store(cls, store: Any) -> "LossService":
        return cls(store.tables())

    def order(self, order: str, as_of: int | None = None) -> dict[str, Any]:
        effective_as_of = material_time(self.tables, as_of)
        ents = [e for e in self.tables.get("entitlements", []) if e["order"] == order]
        if not ents:
            raise KeyError(f"找不到订单：{order}")
        assessments = [
            assess_entitlement(self.tables, e, effective_as_of, _cache=self._cache)
            for e in ents
        ]
        person = ents[0]["person"]
        return {
            "order": order,
            "person": person,
            "as_of": effective_as_of,
            "program_count": len(assessments),
            "has_manual_items": any(a["manual_items"] for a in assessments),
            "assessments": assessments,
        }

    def queue(self, as_of: int | None = None) -> dict[str, Any]:
        effective_as_of = material_time(self.tables, as_of)
        items = []
        seen = set()
        for ent in self.tables.get("entitlements", []):
            report = assess_entitlement(
                self.tables, ent, effective_as_of, _cache=self._cache
            )
            key = (ent["order"], ent["program"])
            seen.add(key)
            codes = {item["code"] for item in report["manual_items"]}
            actionable_without_threshold = {
                "receipt_unknown",
                "receipt_state_conflict",
                "calibration_unaligned",
                "posted_receipt_correction_gap",
            }
            if (
                report["loss"]["review_required"]
                or codes & actionable_without_threshold
                or any(r.get("can_issue") for r in report["compensation"]["replay_recommendations"])
                or (
                    report["compensation"]["credit_recommendation"]
                    and report["compensation"]["credit_recommendation"].get("can_issue")
                )
            ):
                items.append(
                    {
                        "order": ent["order"],
                        "person": ent["person"],
                        "program": ent["program"],
                        "decision": report["decision"],
                        "confirmed_seconds": report["loss"]["confirmed_seconds"],
                        "upper_seconds": report["loss"]["upper_seconds"],
                        "manual_codes": [m["code"] for m in report["manual_items"]],
                    }
                )
        return {"as_of": effective_as_of, "items": items}
