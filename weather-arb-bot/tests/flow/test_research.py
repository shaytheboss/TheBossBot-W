"""Research views: per-city results since a date, and models vs actual."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time, timedelta, timezone

import pytest

import app.peaks.models  # noqa: F401
from app.models.forecast import Forecast
from app.models.intraday import IntradayOpportunity
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity
from app.peaks.models import DailyPeak
from app.research.performance import MIN_TRADES, city_performance, verdict
from tests.fixtures import cities as city_fixtures
from tests.mocks import sqlite_compat

SINCE = date(2026, 10, 4)


def _ts(d: date, h=12):
    return datetime.combine(d, time(h), tzinfo=timezone.utc)


class TestVerdict:
    def test_too_few_trades(self):
        assert verdict(MIN_TRADES - 1, 19, 0.7)[1].startswith("too few")

    def test_a_clear_edge(self):
        z, label = verdict(100, 92, 0.74)
        assert z > 3 and label == "beats the price"

    def test_a_good_looking_but_unclear_city(self):
        """Paris-like: 85% at 74c over 40 trades is z≈2 — not called."""
        z, label = verdict(40, 34, 0.74)
        assert 1.5 < z < 3 and label == "not yet distinguishable from luck"

    def test_a_clear_loser(self):
        assert verdict(100, 50, 0.74)[1] == "loses to the price"


class TestCityPerformance:
    @pytest.mark.asyncio
    async def test_only_trades_since_the_date_count(self, sqlite_db):
        austin, ny = city_fixtures.get("Austin"), city_fixtures.get("New York")
        for c in (austin, ny):
            sqlite_db.add(c.as_row())
        sqlite_db.add_all([Market(id=1, city_id=austin.id, external_id="a", question="q", event_date=SINCE),
                           Market(id=2, city_id=ny.id, external_id="b", question="q", event_date=SINCE),
                           MarketOutcome(id=1, market_id=1, bucket_label="x", bucket_unit="F"),
                           MarketOutcome(id=2, market_id=2, bucket_label="y", bucket_unit="F")])

        def opp(i, oid, d, status, entry, pnl, model=Opportunity):
            return model(id=i, outcome_id=oid, detected_at=_ts(d), side="NO", market_price=0.3,
                         estimated_true_prob=0.1, edge=0.1, confidence_score=93, signals={},
                         virtual_status=status, virtual_entry_price=entry, virtual_pnl=pnl)
        sqlite_db.add_all([
            opp(1, 1, SINCE, "win", 0.75, 1.25), opp(2, 1, SINCE, "loss", 0.75, -3.75),
            opp(3, 1, SINCE - timedelta(days=1), "win", 0.75, 1.25),     # before the date
            opp(4, 1, SINCE, "open", 0.75, None),                        # not settled
            opp(5, 2, SINCE, "win", 0.70, 1.5),
            opp(6, 1, SINCE, "win", 0.80, 1.0, model=IntradayOpportunity),
        ])
        await sqlite_db.commit()
        out = await city_performance(sqlite_db, SINCE)
        a = out["daily"]["Austin"]
        assert (a["trades"], a["win_rate"], a["avg_entry"], a["pnl"]) == (2, 0.5, 0.75, -2.5)
        assert out["daily"]["New York"]["trades"] == 1
        assert list(out["daily"]) == ["New York", "Austin"], "sorted by P&L"
        assert out["intraday"]["Austin"]["trades"] == 1


async def _seed_research(db):
    city = city_fixtures.get("Austin")
    db.add(city.as_row())
    d = date(2026, 9, 20)
    db.add(Market(id=1, city_id=city.id, external_id="m", question="q", event_date=d, resolved=True))
    db.add(MarketOutcome(id=1, market_id=1, bucket_label="90-91°F", bucket_min=90, bucket_max=91,
                         bucket_unit="F", won=True))
    db.add(MarketOutcome(id=2, market_id=1, bucket_label="92-93°F", bucket_min=92, bucket_max=93,
                         bucket_unit="F", won=False))
    db.add(DailyPeak(city_id=city.id, local_date=d, icao="KAUS", max_f=90.4, peak_hour=15,
                     peak_last_hour=15, n_obs=24, first_obs_hour=0, last_obs_hour=23, max_gap_h=1,
                     spike_dropped=0, computed_at=_ts(d)))
    rows = [("gfs", d, 0, 9, 89.0), ("gfs", d, 0, 15, 90.0),     # later one wins
            ("ecmwf", d, 1, 12, 92.0), ("om_ukmo_seamless", d, 0, 12, 91.0),
            ("gfs", d, 5, 12, 80.0)]                              # lead 5 — out of range
    for src, ev, lead, hour, high in rows:
        db.add(Forecast(city_id=city.id, source=src, forecast_for_date=ev, predicted_high_f=high,
                        retrieved_at=_ts(ev - timedelta(days=lead), hour)))
    await db.commit()
    return city, d


class TestModelsVsActual:
    @pytest.mark.asyncio
    async def test_one_row_per_lead_with_each_models_latest_forecast(self, sqlite_db):
        from app.research.models_vs_actual import rows
        city, d = await _seed_research(sqlite_db)
        out = [r async for r in rows(sqlite_db, days=30, today=date(2026, 9, 25))]
        by_lead = {r["lead"]: r for r in out}
        assert sorted(by_lead) == [0, 1], "no row for a lead with no forecast; lead 5 dropped"
        r0 = by_lead[0]
        assert (r0["winner"], r0["winner_lo"], r0["winner_hi"]) == ("90-91°F", 90, 91)
        assert r0["metar_max_f"] == pytest.approx(90.4) and r0["metar_icao"] == "KAUS"
        assert r0["gfs"] == 90.0, "the latest forecast of that lead"
        assert r0["om_ukmo_seamless"] == 91.0 and r0["ecmwf"] == ""
        assert by_lead[1]["ecmwf"] == 92.0

    @pytest.mark.asyncio
    async def test_unresolved_and_future_markets_are_left_out(self, sqlite_db):
        from app.research.models_vs_actual import rows
        await _seed_research(sqlite_db)
        out = [r async for r in rows(sqlite_db, days=30, today=date(2026, 9, 20))]
        assert out == [], "the event day itself is not over yet"

    @pytest.mark.asyncio
    async def test_the_zipped_endpoint(self, monkeypatch):
        from app.database import Base
        import app.models  # noqa: F401
        from app.api.admin import admin_models_vs_actual_csv
        engine = sqlite_compat.make_engine()
        await sqlite_compat.create_schema(engine, Base.metadata)
        maker = sqlite_compat.make_sessionmaker(engine)
        monkeypatch.setattr("app.utils.csv_stream.AsyncSessionLocal", maker)
        async with maker() as db:
            await _seed_research(db)
        try:
            resp = await admin_models_vs_actual_csv("t", days=400, zipped=True)
            raw = b"".join([c async for c in resp.body_iterator])
        finally:
            await engine.dispose()
        text = zipfile.ZipFile(io.BytesIO(raw)).read("models_vs_actual.csv").decode()
        header = text.splitlines()[0].split(",")
        assert header[:9] == ["city", "event_date", "lead", "unit", "winner", "winner_lo",
                              "winner_hi", "metar_max_f", "metar_icao"]
        assert "gfs" in header and "om_ukmo_seamless" in header
        assert "Austin,2026-09-20,0,F,90-91°F" in text


def test_trading_code_never_imports_research():
    from pathlib import Path
    app_dir = Path(__file__).resolve().parents[2] / "app"
    importers = {str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
                 if "app.research" in p.read_text(encoding="utf-8")
                 and not str(p.relative_to(app_dir)).startswith("research")}
    assert importers <= {"api/admin.py"}, importers
