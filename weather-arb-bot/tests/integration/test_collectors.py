"""
Integration tests for data collectors.
These hit real external APIs and are skipped by default.
Run with: pytest tests/integration/ -m integration
"""
import pytest
import asyncio

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_metar_ksfo():
    from app.collectors.metar_collector import MetarCollector
    col = MetarCollector()
    result = await col.collect("KSFO")
    await col.close()
    assert result is not None
    assert result["temperature_f"] is not None
    assert result["observed_at"] is not None


@pytest.mark.asyncio
async def test_nws_sf():
    from app.collectors.nws_collector import NWSCollector
    col = NWSCollector()
    result = await col.collect(lat=37.6213, lon=-122.3790)
    await col.close()
    assert result is not None


@pytest.mark.asyncio
async def test_gfs_sf():
    from app.collectors.gfs_collector import GFSCollector
    col = GFSCollector()
    result = await col.collect(lat=37.6213, lon=-122.3790, model="gfs")
    await col.close()
    assert result is not None
    assert result["predicted_high_f"] is not None


@pytest.mark.asyncio
async def test_pirep_ksfo():
    from app.collectors.pirep_collector import PirepCollector
    col = PirepCollector()
    results = await col.collect("KSFO")
    await col.close()
    assert isinstance(results, list)
