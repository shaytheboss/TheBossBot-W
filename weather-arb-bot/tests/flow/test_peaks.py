"""Daily peak times end to end: METAR rows in a temporary database → the real
job → daily_peaks → the per-city, per-month climatology and the admin view."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.peaks.models  # noqa: F401  — registers the table
from app.models.city import City
from app.models.metar import MetarObservation
from app.peaks.job import RECOMPUTE_DAYS, record_peaks
from app.peaks.models import DailyPeak
from app.peaks.report import MIN_DAYS_MEASURED, climatology
from tests.fixtures import cities as city_fixtures

AUSTIN = city_fixtures.get("Austin")
TZ = ZoneInfo(AUSTIN.tz)
TODAY = date(2026, 9, 20)                                  # the city's local today
NOW = datetime.combine(TODAY, time(12), tzinfo=TZ).astimezone(timezone.utc)


def _curve(peak_hour: float, base=70.0, amp=20.0):
    return lambda h: round(base + amp * math.exp(-((h - peak_hour) / 4.0) ** 2), 1)


async def _add_days(db, first: date, n: int, peak_hour=15, icao=None, hours=range(24)):
    icao = icao or AUSTIN.primary_icao
    f = _curve(peak_hour)
    for i in range(n):
        d = first + timedelta(days=i)
        for h in hours:
            t = datetime.combine(d, time(h), tzinfo=TZ).astimezone(timezone.utc)
            db.add(MetarObservation(icao=icao, observed_at=t, temperature_f=f(h)))
    await db.commit()


@pytest.fixture
async def db(sqlite_db):
    sqlite_db.add(AUSTIN.as_row())
    await sqlite_db.commit()
    return sqlite_db


async def _peaks(db):
    return (await db.execute(select(DailyPeak).order_by(DailyPeak.local_date))).scalars().all()


class TestRecording:
    @pytest.mark.asyncio
    async def test_the_first_run_backfills_every_closed_day(self, db):
        await _add_days(db, TODAY - timedelta(days=12), 13)        # … through today
        stats = await record_peaks(db, now=NOW)
        rows = await _peaks(db)
        assert [r.local_date for r in rows] == [TODAY - timedelta(days=12 - i) for i in range(12)]
        assert stats["days_written"] == 12, "today is still going — not recorded"
        assert all(r.peak_hour == 15.0 and r.icao == AUSTIN.primary_icao for r in rows)

    @pytest.mark.asyncio
    async def test_a_second_run_writes_no_duplicates(self, db):
        await _add_days(db, TODAY - timedelta(days=5), 5)
        await record_peaks(db, now=NOW)
        await record_peaks(db, now=NOW + timedelta(hours=3))
        assert len(await _peaks(db)) == 5

    @pytest.mark.asyncio
    async def test_recent_days_are_recomputed_older_ones_are_not_reread(self, db):
        """A late observation for yesterday updates it; the job does not reread
        the whole history every run, so a change to an old day is not seen."""
        await _add_days(db, TODAY - timedelta(days=6), 6)
        await record_peaks(db, now=NOW)
        yesterday, old = TODAY - timedelta(days=1), TODAY - timedelta(days=RECOMPUTE_DAYS + 3)
        for d in (yesterday, old):
            t = datetime.combine(d, time(17, 30), tzinfo=TZ).astimezone(timezone.utc)
            db.add(MetarObservation(icao=AUSTIN.primary_icao, observed_at=t, temperature_f=95.0))
        await db.commit()
        await record_peaks(db, now=NOW + timedelta(hours=3))
        by_day = {r.local_date: r for r in await _peaks(db)}
        assert by_day[yesterday].max_f == 95.0 and by_day[yesterday].peak_hour == 17.5
        assert by_day[old].max_f == 90.0

    @pytest.mark.asyncio
    async def test_a_placeholder_station_is_skipped(self, db):
        city = (await db.execute(select(City))).scalars().one()
        city.primary_icao = "ICAO"
        await db.commit()
        await _add_days(db, TODAY - timedelta(days=3), 3, icao="ICAO")
        assert (await record_peaks(db, now=NOW))["days_written"] == 0

    @pytest.mark.asyncio
    async def test_one_failing_city_does_not_stop_the_others(self, db, monkeypatch):
        import app.peaks.job as job
        real = job.record_city
        calls = []

        async def flaky(db_, city, now):
            calls.append(city.name)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return await real(db_, city, now)
        db.add(city_fixtures.get("New York").as_row())
        await db.commit()
        monkeypatch.setattr(job, "record_city", flaky)
        stats = await record_peaks(db, now=NOW)
        assert stats["errors"] == 1 and stats["cities"] == 1


class TestClimatology:
    @pytest.mark.asyncio
    async def test_a_measured_month(self, db):
        first = date(2026, 7, 1)
        await _add_days(db, first, 20, peak_hour=15)
        await record_peaks(db, now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc))
        out = await climatology(db, year=2026)
        july = next(m for m in out["cities"][0]["months"] if m["month"] == 7)
        assert july["source"] == "measured" and july["days"] == 20
        assert july["median"] == "15:00"
        assert july["passed_by"]["14:00"] == 0.0 and july["passed_by"]["15:00"] == 1.0

    @pytest.mark.asyncio
    async def test_passed_by_is_a_share_of_days(self, db):
        await _add_days(db, date(2026, 7, 1), 10, peak_hour=14)
        await _add_days(db, date(2026, 7, 11), 10, peak_hour=17)
        await record_peaks(db, now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc))
        july = next(m for m in (await climatology(db, year=2026))["cities"][0]["months"]
                    if m["month"] == 7)
        assert july["passed_by"]["15:00"] == 0.5 and july["passed_by"]["17:00"] == 1.0
        assert july["range_80pct"] == ["14:00", "17:00"]

    @pytest.mark.asyncio
    async def test_a_month_without_enough_days_is_an_estimate_from_the_sun(self, db):
        """July is measured (peaks 3.0 h after solar noon); August has too few
        days, so it is solar noon plus that measured offset — labelled as such."""
        await _add_days(db, date(2026, 7, 1), 20, peak_hour=16)
        await _add_days(db, date(2026, 8, 1), MIN_DAYS_MEASURED - 1, peak_hour=16)
        await record_peaks(db, now=datetime(2026, 9, 5, 18, tzinfo=timezone.utc))
        out = await climatology(db, year=2026)
        aug = next(m for m in out["cities"][0]["months"] if m["month"] == 8)
        assert aug["source"] == "estimate" and aug["days"] == MIN_DAYS_MEASURED - 1
        noon_h, noon_m = map(int, aug["solar_noon"].split(":"))
        med_h, med_m = map(int, aug["median"].split(":"))
        offset = (med_h * 60 + med_m) - (noon_h * 60 + noon_m)
        assert abs(offset - out["hours_after_solar_noon"] * 60) <= 1

    @pytest.mark.asyncio
    async def test_incomplete_days_are_left_out_and_counted(self, db):
        """A day read only in the afternoon would look like a late peak."""
        await _add_days(db, date(2026, 7, 1), 12, peak_hour=15)
        await _add_days(db, date(2026, 7, 13), 3, peak_hour=15, hours=range(15, 24))
        await record_peaks(db, now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc))
        out = await climatology(db, year=2026)
        july = next(m for m in out["cities"][0]["months"] if m["month"] == 7)
        assert july["days"] == 12 and out["incomplete_days_left_out"] == 3

    @pytest.mark.asyncio
    async def test_the_admin_view(self, db):
        from app.api.admin import admin_peaks
        import app.peaks.job as job
        await _add_days(db, TODAY - timedelta(days=3), 3)
        job.LAST_RUN.update(await record_peaks(db, now=NOW))
        out = await admin_peaks("t", db, city="austin")
        assert [c["city"] for c in out["cities"]] == ["Austin"]
        assert out["last_run"]["days_written"] == 3


class TestBoundary:
    def test_trading_code_depends_on_it_only_through_the_guard(self):
        """The intraday detector reads app.peaks.guard (the peak guard) and
        nothing else; no other trading code imports the package."""
        from pathlib import Path
        app_dir = Path(__file__).resolve().parents[2] / "app"
        importers = {
            str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
            if "app.peaks" in p.read_text(encoding="utf-8")
            and not str(p.relative_to(app_dir)).startswith("peaks")
        }
        # app/research/ is read-only and itself never imported by trading
        # code (tests/flow/test_research.py), so it may read the peaks table.
        importers = {p for p in importers if not p.startswith("research/")}
        assert importers <= {"main.py", "api/admin.py", "intraday/detector.py"}, importers
        det = (app_dir / "intraday" / "detector.py").read_text(encoding="utf-8")
        imports = [ln.strip() for ln in det.splitlines() if "app.peaks" in ln and "import" in ln]
        assert imports == ["from app.peaks.guard import share_passed"], imports
