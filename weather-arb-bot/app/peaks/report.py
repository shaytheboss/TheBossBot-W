"""Per city and month: when the high usually arrives, and how often it has
already passed by each hour. Reads daily_peaks only (a small table)."""
from __future__ import annotations

import statistics
from datetime import date
from typing import Optional

from sqlalchemy import select

from app.models.city import City
from app.peaks.compute import is_complete, mid_month, solar_noon_hour
from app.peaks.models import DailyPeak

#: Below this many complete days a month is shown as an estimate from the sun.
MIN_DAYS_MEASURED = 10
#: Local hours at which "share of days already past their peak" is reported.
HOURS = tuple(range(11, 20))
#: Used when no city has a measured month yet: how far after solar noon the
#: high typically comes. Replaced by the measured offset as soon as one exists.
DEFAULT_OFFSET_H = 2.5


def _q(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _hhmm(h: Optional[float]) -> Optional[str]:
    if h is None:
        return None
    m = int(round(h * 60))
    return f"{m // 60:02d}:{m % 60:02d}"


def summarise_month(peak_hours: list[float]) -> dict:
    n = len(peak_hours)
    return {
        "days": n,
        "median": _hhmm(statistics.median(peak_hours)),
        "range_80pct": [_hhmm(_q(peak_hours, 0.1)), _hhmm(_q(peak_hours, 0.9))],
        # The number an entry decision needs: by HH:00, on what share of days
        # had the high already come? (A peak at 14:30 has not passed at 14:00.)
        "passed_by": {f"{h:02d}:00": round(sum(1 for x in peak_hours if x <= h) / n, 2)
                      for h in HOURS},
    }


async def climatology(db, city_name: Optional[str] = None, year: Optional[int] = None) -> dict:
    year = year or date.today().year
    cities = (await db.execute(select(City).where(City.active == True))).scalars().all()  # noqa: E712
    if city_name:
        cities = [c for c in cities if c.name.lower() == city_name.lower()]
    rows = (await db.execute(select(
        DailyPeak.city_id, DailyPeak.local_date, DailyPeak.peak_hour, DailyPeak.n_obs,
        DailyPeak.first_obs_hour, DailyPeak.last_obs_hour, DailyPeak.max_gap_h,
    ))).all()
    by_city = {c.id: c for c in cities}

    per: dict[tuple, list[float]] = {}
    offsets: dict[int, list[float]] = {}          # month → peak minus solar noon
    skipped = 0
    for cid, d, ph, n, first, last, gap in rows:
        c = by_city.get(cid)
        if c is None:
            continue
        if not is_complete(n, first, last, gap):
            skipped += 1
            continue
        per.setdefault((cid, d.month), []).append(ph)
        if c.nws_lat is not None and c.nws_lon is not None:
            noon = solar_noon_hour(float(c.nws_lat), float(c.nws_lon), c.timezone, d)
            offsets.setdefault(d.month, []).append(ph - noon)

    all_offsets = [o for v in offsets.values() for o in v]
    out = []
    for c in sorted(cities, key=lambda c: c.name):
        months = []
        for m in range(1, 13):
            noon = (solar_noon_hour(float(c.nws_lat), float(c.nws_lon), c.timezone, mid_month(year, m))
                    if c.nws_lat is not None and c.nws_lon is not None else None)
            hours = per.get((c.id, m), [])
            if len(hours) >= MIN_DAYS_MEASURED:
                months.append({"month": m, "source": "measured", "solar_noon": _hhmm(noon),
                               **summarise_month(hours)})
            elif noon is not None:
                month_off = offsets.get(m) or []
                off = (statistics.median(month_off) if len(month_off) >= MIN_DAYS_MEASURED
                       else statistics.median(all_offsets) if all_offsets else DEFAULT_OFFSET_H)
                months.append({"month": m, "source": "estimate", "days": len(hours),
                               "solar_noon": _hhmm(noon), "median": _hhmm(noon + off)})
        out.append({"city": c.name, "months": months})
    return {
        "note": ("median = local time the day's high usually arrives; passed_by = share "
                 "of days whose high had already come by that hour. 'estimate' months "
                 "have fewer than %d complete days and use solar noon + the measured "
                 "offset." % MIN_DAYS_MEASURED),
        "incomplete_days_left_out": skipped,
        "hours_after_solar_noon": round(statistics.median(all_offsets), 2) if all_offsets else None,
        "cities": out,
    }
