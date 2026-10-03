"""Every model's forecast next to what happened, per city and day.

The question it serves: what makes a city like Paris profitable — one model
far more accurate there than the rest, a combination, agreement between
models? That needs each model's own forecast beside the truth, for every
day, not only for the days the bot traded.

One row per (city, event day, lead): the bucket Polymarket settled, the
METAR high of that local day (daily_peaks) and, for each source, its latest
forecast made `lead` days before (lead as model_skill counts it, from the UTC
date the forecast was retrieved). Built city by city — three small reads per
city — so memory stays flat. Admin export only; run it when needed.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import AsyncIterator

from sqlalchemy import select

from app.models.city import City
from app.models.forecast import Forecast
from app.models.market import Market, MarketOutcome
from app.peaks.models import DailyPeak

MAX_LEAD = 3
BASE_SOURCES = ("gfs", "ecmwf", "icon", "hrrr", "nws", "meteosource", "tomorrowio",
                "gfs_ensemble", "ecmwf_ensemble")


def source_columns() -> list[str]:
    from app.workers.open_meteo_job import extra_models, extra_source
    return list(BASE_SOURCES) + [extra_source(m) for m in extra_models()]


def fieldnames() -> list[str]:
    return (["city", "event_date", "lead", "unit", "winner", "winner_lo", "winner_hi",
             "metar_max_f", "metar_icao"] + source_columns())


async def rows(db, days: int, today: date | None = None) -> AsyncIterator[dict]:
    today = today or date.today()
    start = today - timedelta(days=days)
    sources = source_columns()
    cities = (await db.execute(select(City.id, City.name).order_by(City.name))).all()
    for city_id, city_name in cities:
        winners = {}
        for ev, label, lo, hi, unit in (await db.execute(
            select(Market.event_date, MarketOutcome.bucket_label, MarketOutcome.bucket_min,
                   MarketOutcome.bucket_max, MarketOutcome.bucket_unit)
            .join(MarketOutcome, MarketOutcome.market_id == Market.id)
            .where(Market.city_id == city_id, Market.resolved == True,  # noqa: E712
                   Market.event_date >= start, Market.event_date < today,
                   MarketOutcome.won == True)  # noqa: E712
        )).all():
            winners[ev] = (label, lo, hi, unit)
        if not winners:
            continue
        actual = {d: (mx, icao) for d, mx, icao in (await db.execute(
            select(DailyPeak.local_date, DailyPeak.max_f, DailyPeak.icao)
            .where(DailyPeak.city_id == city_id, DailyPeak.local_date >= start)
        )).all()}
        latest: dict[tuple, tuple] = {}     # (event_date, lead, source) → (retrieved_at, high)
        for source, ev, at, high in (await db.execute(
            select(Forecast.source, Forecast.forecast_for_date, Forecast.retrieved_at,
                   Forecast.predicted_high_f)
            .where(Forecast.city_id == city_id, Forecast.source.in_(sources),
                   Forecast.forecast_for_date.in_(list(winners)),
                   Forecast.predicted_high_f.isnot(None))
        )).all():
            lead = (ev - at.date()).days
            if 0 <= lead <= MAX_LEAD:
                key = (ev, lead, source)
                if key not in latest or at > latest[key][0]:
                    latest[key] = (at, float(high))
        for ev in sorted(winners):
            label, lo, hi, unit = winners[ev]
            mx, icao = actual.get(ev, (None, None))
            for lead in range(MAX_LEAD + 1):
                fc = {s: latest.get((ev, lead, s), (None, None))[1] for s in sources}
                if not any(v is not None for v in fc.values()):
                    continue
                yield {"city": city_name, "event_date": ev.isoformat(), "lead": lead,
                       "unit": unit, "winner": label, "winner_lo": lo, "winner_hi": hi,
                       "metar_max_f": mx, "metar_icao": icao,
                       **{s: ("" if v is None else round(v, 2)) for s, v in fc.items()}}
