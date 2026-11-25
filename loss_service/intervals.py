"""Half-open integer interval helpers.

All public intervals are ``[start, end)``.  Endpoints are normally integer
program seconds.  Functions never mutate their inputs.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Interval = Tuple[int, int]


def interval(start: float, end: float) -> Interval | None:
    """Create a normalized interval, returning ``None`` when empty."""
    start = int(start)
    end = int(end)
    if end <= start:
        return None
    return (start, end)


def merge(intervals: Iterable[Interval | None]) -> List[Interval]:
    """Merge overlapping or touching half-open intervals."""
    ordered = sorted((int(a), int(b)) for a, b in intervals if a is not None and b > a)
    result: list[Interval] = []
    for a, b in ordered:
        if not result or a > result[-1][1]:
            result.append((a, b))
        elif b > result[-1][1]:
            result[-1] = (result[-1][0], b)
    return result


def length(intervals: Iterable[Interval]) -> int:
    return sum(b - a for a, b in intervals)


def clip(intervals: Iterable[Interval], lo: int | None = None, hi: int | None = None) -> List[Interval]:
    result = []
    for a, b in intervals:
        if lo is not None:
            a = max(a, lo)
        if hi is not None:
            b = min(b, hi)
        if a < b:
            result.append((a, b))
    return merge(result)


def intersect(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """Intersect two merged interval collections."""
    if not a or not b:
        return []
    i = j = 0
    result: list[Interval] = []
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return merge(result)


def intersect_one(intervals: Sequence[Interval], other: Interval) -> List[Interval]:
    return intersect(intervals, [other])


def subtract(source: Sequence[Interval], cutter: Sequence[Interval]) -> List[Interval]:
    """Return ``source - cutter``; inputs need not already be merged."""
    return subtract_merged(merge(source), merge(cutter))


def subtract_merged(source: Sequence[Interval], cutter: Sequence[Interval]) -> List[Interval]:
    """Subtract when both inputs are already sorted/merged."""
    if not source:
        return []
    result: list[Interval] = []
    j = 0
    for a, b in source:
        cur = a
        while j < len(cutter) and cutter[j][1] <= a:
            j += 1
        k = j
        while k < len(cutter) and cutter[k][0] < b:
            ca, cb = cutter[k]
            if ca > cur:
                result.append((cur, min(ca, b)))
            cur = max(cur, cb)
            if cur >= b:
                break
            k += 1
        if cur < b:
            result.append((cur, b))
    return result


def union(*collections: Iterable[Interval | None]) -> List[Interval]:
    items: list[Interval] = []
    for collection in collections:
        items.extend(collection)
    return merge(items)


def intersection_of_all(collections: Sequence[Sequence[Interval]]) -> List[Interval]:
    """Intersect many interval collections.

    An empty input list means the universal interval is not known, so callers
    should handle that case.  With one collection it is returned merged.
    """
    if not collections:
        return []
    result = merge(collections[0])
    for collection in collections[1:]:
        result = intersect(result, collection)
        if not result:
            break
    return result


def contains_all(container: Sequence[Interval], piece: Interval) -> bool:
    """Whether merged ``container`` contains the whole half-open piece."""
    a, b = piece
    for ca, cb in container:
        if ca <= a and b <= cb:
            return True
        if ca > a:
            return False
    return False


def offset_envelope(base_from: int, base_to: int, lo_offset: int, hi_offset: int) -> Interval | None:
    """Union of ``[f+o, t+o)`` over every integer offset in the inclusive range."""
    return interval(base_from + lo_offset, base_to + hi_offset)
