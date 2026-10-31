"""Minimal 5-field cron (minute hour day-of-month month day-of-week) with *, lists, ranges and steps. UTC."""
from __future__ import annotations

from datetime import datetime, timedelta

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step in {text!r}")
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise CronError(f"bad range in {text!r}")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = int(part)
            b = hi if step > 1 else a
        else:
            raise CronError(f"cannot read {text!r}")
        if hi == 6 and a == 7:
            a = 0
        if a < lo or b > max(hi, 7 if hi == 6 else hi) or a > b:
            raise CronError(f"{text!r} is outside {lo}-{hi}")
        out.update(v % 7 if hi == 6 else v for v in range(a, b + 1, step))
    return out


def parse(expr: str) -> list[set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise CronError("A cron expression has 5 fields: minute hour day-of-month month day-of-week")
    return [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]


def next_after(expr: str, after: datetime) -> datetime:
    """First minute strictly after ``after`` that matches (searches up to ~5 years)."""
    minute, hour, dom, month, dow = parse(expr)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366 * 5):
        if t.month in month and t.day in dom and (t.weekday() + 1) % 7 in dow and t.hour in hour and t.minute in minute:
            return t
        t += timedelta(minutes=1)
    raise CronError("expression never matches")
