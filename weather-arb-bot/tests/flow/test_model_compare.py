"""Per-city model comparison: scores record-only models, changes no weight."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.analyzers.model_compare import compare_models
from app.analyzers.model_skill import update_model_skill
from app.models.forecast import Forecast
from app.models.market import Market, MarketOutcome
from app.models.model_skill import ModelSkill
from tests.fixtures import cities as city_fixtures

EXTRA = "om_ukmo_seamless"


async def _seed(db, days: int = 6):
    """Austin, `days` settled markets whose winning bucket is 90-91°F.
    GFS says 95 every day (a miss), UKMO says 90 (a hit), and ECMWF says 91
    on even days only."""
    city = city_fixtures.rows("Austin")[0]
    db.add(city)
    today = date.today()
    for i in range(days):
        ev = today - timedelta(days=i + 1)
        m = Market(id=i + 1, city_id=city.id, external_id=f"m{i}", question="q",
                   event_date=ev, resolved=True)
        db.add(m)
        db.add(MarketOutcome(market_id=i + 1, bucket_label="90-91", bucket_min=90,
                             bucket_max=91, bucket_unit="F", won=True))
        made = datetime.combine(ev - timedelta(days=1), time(12), tzinfo=timezone.utc)
        for source, high in (("gfs", 95), (EXTRA, 90), ("ecmwf", 91 if i % 2 == 0 else 97)):
            db.add(Forecast(city_id=city.id, source=source, forecast_for_date=ev,
                            predicted_high_f=high, predicted_low_f=70, retrieved_at=made))
    await db.commit()
    return city


class TestCompare:
    @pytest.mark.asyncio
    async def test_the_record_only_model_is_ranked_per_city(self, sqlite_db):
        await _seed(sqlite_db)
        out = await compare_models(sqlite_db, days_ahead=1)
        austin = out["cities"][0]
        assert austin["best"] == EXTRA
        ranking = [m["source"] for m in austin["models"]]
        assert ranking == [EXTRA, "ecmwf", "gfs"]
        ukmo = austin["models"][0]
        assert ukmo["record_only"] and ukmo["samples"] == 6 and ukmo["hit_rate"] == 1.0
        assert austin["direction_only"], "six markets is a direction, not a verdict"

    @pytest.mark.asyncio
    async def test_the_all_cities_row_pools_the_samples(self, sqlite_db):
        await _seed(sqlite_db)
        out = await compare_models(sqlite_db, days_ahead=1)
        gfs = next(r for r in out["all_cities"] if r["source"] == "gfs")
        assert gfs["samples"] == 6 and gfs["hits"] == 0 and gfs["mae_f"] == 3.0

    @pytest.mark.asyncio
    async def test_a_different_lead_time_is_scored_separately(self, sqlite_db):
        await _seed(sqlite_db)
        out = await compare_models(sqlite_db, days_ahead=0)
        assert out["cities"][0]["models"] == []

    @pytest.mark.asyncio
    async def test_the_trading_weights_never_include_a_record_only_model(self, sqlite_db):
        """update_model_skill feeds the blend. It must keep scoring only the
        blend's own models, however well an extra one does."""
        await _seed(sqlite_db)
        await update_model_skill(sqlite_db)
        sources = {r.source for r in (await sqlite_db.execute(select(ModelSkill))).scalars()}
        assert sources == {"gfs", "ecmwf"}
