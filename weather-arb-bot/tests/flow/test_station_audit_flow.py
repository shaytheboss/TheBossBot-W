"""Station audit end to end: stored rules, the Gamma fallback, and applying.

Runs on the pipeline harness, with Gamma served by the mock router.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.models.city import City
from app.models.market import Market
from app.utils.station_audit import audit_cities, apply_fix, VERDICT_BOTH, VERDICT_OK
from tests.mocks import polymarket_payloads as pm

RULES = ("highest temperature recorded at the Austin Camp Mabry Station ... "
         "https://www.wunderground.com/history/daily/us/tx/austin/KATT")


async def _set_rules(pipeline, text):
    async with pipeline.session() as db:
        m = (await db.execute(select(Market))).scalars().one()
        m.resolution_source = text
        await db.commit()


async def _audit(pipeline):
    async with pipeline.session() as db:
        async with httpx.AsyncClient() as client:
            return (await audit_cities(db, client))[0]


class TestAuditFlow:
    @pytest.mark.asyncio
    async def test_stored_rules_are_enough_and_cost_no_request(self, pipeline):
        await _set_rules(pipeline, RULES)
        before = len(pipeline.http.requests)
        a = await _audit(pipeline)
        assert a.resolution_icao == "KATT" and a.verdict == VERDICT_BOTH
        assert len(pipeline.http.requests) == before, "no Gamma call when the stored text has the link"

    @pytest.mark.asyncio
    async def test_truncated_rules_fall_back_to_gamma(self, pipeline):
        """Descriptions are stored cut at 500 characters, which can drop the
        link. Then — and only then — the event is fetched."""
        await _set_rules(pipeline, "x" * 500)
        event = pm.event(markets=[{**pm.sub_market(), "resolutionSource":
                                   "https://www.wunderground.com/history/daily/us/tx/austin/KATT"}])
        pipeline.http.prepend(pm.GAMMA_EVENTS, [event])
        a = await _audit(pipeline)
        assert a.resolution_icao == "KATT"
        assert pipeline.http.calls_to(pm.GAMMA_EVENTS)

    @pytest.mark.asyncio
    async def test_a_gamma_outage_yields_unknown_not_an_error(self, pipeline, no_backoff):
        await _set_rules(pipeline, "no link here")
        pipeline.http.prepend(pm.GAMMA_EVENTS, {"error": "down"}, status=503)
        a = await _audit(pipeline)
        assert a.verdict == "unknown"

    @pytest.mark.asyncio
    async def test_apply_then_reaudit_is_ok(self, pipeline):
        await _set_rules(pipeline, RULES)
        async with pipeline.session() as db:
            city = (await db.execute(select(City))).scalars().one()
            async with httpx.AsyncClient() as client:
                audit = (await audit_cities(db, client, [city]))[0]
            apply_fix(city, audit)
            await db.commit()

        async with pipeline.session() as db:
            city = (await db.execute(select(City))).scalars().one()
            assert city.primary_icao == "KATT"
            assert city.reference_icao == "KEDC", "the seeded reference is kept"
            assert city.wunderground_url.endswith("/KATT")
        assert (await _audit(pipeline)).verdict == VERDICT_OK


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_the_apply_endpoint_recomputes_rather_than_trusting_input(self, pipeline):
        """It takes only a city id: the station always comes from the market
        rules the server read itself."""
        import inspect
        from app.api.admin import admin_stations_apply
        params = set(inspect.signature(admin_stations_apply).parameters)
        assert params == {"city_id", "_", "db"}

    @pytest.mark.asyncio
    async def test_apply_refuses_when_there_is_nothing_to_fix(self, pipeline):
        from fastapi import HTTPException
        from app.api.admin import admin_stations_apply
        await _set_rules(pipeline, "Resolves on https://www.weather.gov.hk/en/cis/climat.htm")
        async with pipeline.session() as db:
            with pytest.raises(HTTPException) as e:
                await admin_stations_apply(pipeline.city.id, "tok", db)
        assert e.value.status_code == 409

    @pytest.mark.asyncio
    async def test_apply_changes_the_city(self, pipeline):
        from app.api.admin import admin_stations_apply
        await _set_rules(pipeline, RULES)
        async with pipeline.session() as db:
            out = await admin_stations_apply(pipeline.city.id, "tok", db)
        assert out["changes"]["primary_icao"] == {"from": "KAUS", "to": "KATT"}
