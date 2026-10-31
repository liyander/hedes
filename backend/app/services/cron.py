"""Minimal 5-field cron (minute hour day-of-month month day-of-week) with *, lists, ranges and steps. UTC."""
from __future__ import annotations

from datetime import datetime, timedelta

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> set[int]:
    dow = hi == 6  # day of week also accepts 7 for Sunday
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step in {text!r}")
            step = int(s)
        if part == "*":
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
        if a < lo or b > (7 if dow else hi) or a > b:
            raise CronError(f"{text!r} is outside {lo}-{hi}")
        out.update(v % 7 if dow else v for v in range(a, b + 1, step))
    return out


def parse(expr: str) -> list[set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise CronError("A cron expression has 5 fields: minute hour day-of-month month day-of-week")
    fields = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    _, _, dom, month, dow = fields
    if dow == set(range(7)) and not any(d <= _MONTH_DAYS[m] for m in month for d in dom):
        raise CronError(f"{expr!r} never matches: no month in it has that day")
    return fields


def next_after(expr: str, after: datetime) -> datetime:
    """First minute strictly after ``after`` that matches (searches up to ~5 years). As in standard cron, when both
    day-of-month and day-of-week are restricted, a day matching either one counts."""
    minute, hour, dom, month, dow = parse(expr)
    either = dom != set(range(1, 32)) and dow != set(range(7))

    def day_ok(t: datetime) -> bool:
        d, w = t.day in dom, (t.weekday() + 1) % 7 in dow
        return (d or w) if either else (d and w)

    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = t + timedelta(days=366 * 5)
    while t < end:  # skip whole days and hours that cannot match, so rare expressions are found quickly
        if t.month not in month or not day_ok(t):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        elif t.hour not in hour:
            t = (t + timedelta(hours=1)).replace(minute=0)
        elif t.minute not in minute:
            t += timedelta(minutes=1)
        else:
            return t
    raise CronError("expression never matches")
