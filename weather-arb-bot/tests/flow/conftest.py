"""A whole bot, wired end to end, with every edge replaced by a mock.

`pipeline` builds a temporary SQLite database, seeds one city and one
temperature market, and exposes the four real stages in order:

    collect_forecasts()  8 collectors  → HTTP mock → forecasts table
    collect_prices()     Polymarket    → HTTP mock → market_prices table
    detect()             the real detector reads those tables
    alert()              the real send functions → FakeBot

Nothing in between is stubbed. The estimator, the normalisation, the
thresholds, the bias correction, the ORM writes and the Telegram formatting
are the production code paths. Only the network and the database engine are
substituted, which is the point: a test here fails when the *logic* breaks,
not when a mock drifts.

Naming note: these are NOT marked `@pytest.mark.integration`. In this repo
that marker means "hits the real APIs" and is deselected by default. These
tests must run on every commit, so they stay in the default suite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import pytest

from tests.fixtures import cities as city_fixtures
from tests.mocks import http_router, sqlite_compat
from tests.mocks import polymarket_payloads as pm
from tests.mocks import weather_payloads as wx

#: Collectors keyed by the `source` string they write to `forecasts.source`.
#: A test names the ones it wants to break.
FORECAST_SOURCES = ("gfs", "ecmwf", "hrrr", "icon", "nws")

#: Endpoints each source depends on, for targeted failure injection. NWS is
#: the interesting one: it needs two hops, so breaking /points and breaking
#: the gridpoint are different failures.
SOURCE_ENDPOINTS = {
    "gfs": wx.OPEN_METEO,
    "ecmwf": wx.OPEN_METEO,
    "hrrr": wx.OPEN_METEO,
    "icon": wx.OPEN_METEO,
    "nws": wx.NWS_GRIDPOINT,
}


def _as_utc(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


@dataclass
class PipelineResult:
    opportunities: list = field(default_factory=list)
    side_alerts: list = field(default_factory=list)
    #: outcome_id → bucket label, resolved eagerly. Reading
    #: `opportunity.outcome_ref` instead would emit a lazy SELECT outside the
    #: async greenlet and raise MissingGreenlet.
    labels: dict = field(default_factory=dict)

    @property
    def best(self):
        """Highest-confidence opportunity, or None."""
        return max(self.opportunities, key=lambda o: o.confidence_score, default=None)

    def label_of(self, opp) -> Optional[str]:
        return self.labels.get(opp.outcome_id)

    def by_bucket(self, label: str):
        for o in self.opportunities:
            if self.labels.get(o.outcome_id) == label:
                return o
        return None

    def bucket_sides(self) -> dict:
        """{bucket label: side} — the readable summary of one detection run."""
        return {self.labels.get(o.outcome_id): o.side for o in self.opportunities}


class Pipeline:
    """Drives the real stages against a temporary database."""

    def __init__(self, maker, router, bot, city, event_date, buckets):
        self._maker = maker
        self.http = router
        self.telegram = bot
        self.city = city
        self.event_date = event_date
        self.buckets = buckets

    def session(self):
        return self._maker()

    # ── stage 1: collectors → forecasts table ─────────────────────────────

    async def collect_forecasts(self, sources: Iterable[str] = FORECAST_SOURCES) -> dict:
        """Run each collector for real. Returns {source: stored?}.

        Failures are swallowed the same way `job_fetch_models` swallows them —
        one dead provider must never stop the others.
        """
        from app.collectors.gfs_collector import GFSCollector
        from app.collectors.hrrr_collector import HRRRCollector
        from app.collectors.icon_collector import IconCollector
        from app.collectors.nws_collector import NWSCollector

        gfs = GFSCollector()
        runners = {
            "gfs": lambda db: gfs.collect_and_store(*self._args(db), "gfs"),
            "ecmwf": lambda db: gfs.collect_and_store(*self._args(db), "ecmwf"),
            "hrrr": lambda db: HRRRCollector().collect_and_store(*self._args(db)),
            "icon": lambda db: IconCollector().collect_and_store(*self._args(db)),
            "nws": lambda db: NWSCollector().collect_and_store(*self._args(db)),
        }
        stored: dict[str, bool] = {}
        async with self.session() as db:
            for name in sources:
                try:
                    stored[name] = await runners[name](db) is not None
                except Exception:
                    stored[name] = False
            await db.commit()
        return stored

    async def collect_ensemble(self) -> bool:
        from app.collectors.gfs_collector import GFSCollector
        async with self.session() as db:
            try:
                got = await GFSCollector().collect_ensemble_and_store(*self._args(db))
            except Exception:
                got = None
            await db.commit()
        return got is not None

    def _args(self, db):
        return (self.city.id, self.city.lat, self.city.lon, self.event_date, db)

    # ── stage 2: Polymarket → market_prices table ─────────────────────────

    async def collect_prices(self) -> dict:
        """Poll every outcome, mirroring `job_fetch_polymarket`.

        The real job loads the last stored price for all outcomes in one
        `DISTINCT ON` query so the collector can skip writing an unchanged
        price. `DISTINCT ON` is Postgres-only; the job already wraps it in a
        try/except that degrades to "treat every price as new", so here the
        same map is built portably. What matters for the test is that the
        collector receives a real `last` — that is the write-on-change
        contract, and it lives in the collector, not the query.

        Returns {"written": n, "skipped": n}.
        """
        from sqlalchemy import select
        from app.collectors.polymarket_collector import PolymarketCollector
        from app.models.market import MarketPrice

        col = PolymarketCollector()
        written = skipped = 0
        async with self.session() as db:
            rows = (await db.execute(
                select(MarketPrice).order_by(MarketPrice.timestamp)
            )).scalars().all()
            # SQLite has no timezone-aware type: a TIMESTAMP(timezone=True)
            # column reads back naive, and the collector's heartbeat check
            # subtracts it from an aware `now`, which raises. Postgres returns
            # it aware, so re-attaching UTC here restores the production shape
            # instead of papering over a bug.
            last_map = {
                r.outcome_id: (r.yes_price, _as_utc(r.timestamp)) for r in rows
            }

            for i in range(len(self.buckets)):
                try:
                    got = await col.collect_and_store(
                        i + 1, f"tok{i}", db, last=last_map.get(i + 1), commit=False
                    )
                except Exception:
                    got = None
                if got is None:
                    continue
                if got.get("skipped"):
                    skipped += 1
                else:
                    written += 1
            await db.commit()
        return {"written": written, "skipped": skipped}

    # ── stage 3: the detector ─────────────────────────────────────────────

    async def detect(self) -> PipelineResult:
        from sqlalchemy import select
        from app.analyzers.opportunity_detector import detect_opportunities
        from app.models.market import MarketOutcome

        async with self.session() as db:
            opps, side = await detect_opportunities(db)
            await db.commit()
            outcomes = (await db.execute(select(MarketOutcome))).scalars().all()
            labels = {o.id: o.bucket_label for o in outcomes}
        return PipelineResult(list(opps), list(side), labels)

    # ── stage 4: Telegram ─────────────────────────────────────────────────

    async def alert(self, result: PipelineResult) -> int:
        from app.bot.telegram_bot import send_opportunity_alert, send_side_alert
        async with self.session() as db:
            for opp in result.opportunities:
                merged = await db.merge(opp)
                try:
                    await send_opportunity_alert(merged, db)
                except Exception:
                    pass          # mirrors job_run_analyzer's own guard
            for sa in result.side_alerts:
                try:
                    await send_side_alert(sa, db)
                except Exception:
                    pass
            await db.commit()
        return len(self.telegram.sent)

    # ── convenience ───────────────────────────────────────────────────────

    async def run(self, sources: Iterable[str] = FORECAST_SOURCES) -> PipelineResult:
        """Every stage, in order — the shape `job_run_analyzer` runs in prod."""
        await self.collect_forecasts(sources)
        await self.collect_ensemble()
        await self.collect_prices()
        result = await self.detect()
        await self.alert(result)
        return result

    async def collect_metar(self) -> bool:
        """METAR observations feed the bias estimator and the intraday path.

        Note this one really does exercise `ON CONFLICT DO NOTHING` — SQLite
        supports the constraint form the collector uses, so the upsert is the
        production statement, not a rewrite.
        """
        from app.collectors.metar_collector import MetarCollector
        async with self.session() as db:
            try:
                got = await MetarCollector().collect_and_store(self.city.primary_icao, db)
            except Exception:
                got = None
            await db.commit()
        return got is not None

    # ── mutating the seeded world ─────────────────────────────────────────

    async def set_market(self, **fields) -> None:
        from sqlalchemy import select
        from app.models.market import Market
        async with self.session() as db:
            m = (await db.execute(select(Market))).scalars().one()
            for k, v in fields.items():
                setattr(m, k, v)
            await db.commit()

    async def set_city(self, **fields) -> None:
        from sqlalchemy import select
        from app.models.city import City
        async with self.session() as db:
            c = (await db.execute(select(City))).scalars().one()
            for k, v in fields.items():
                setattr(c, k, v)
            await db.commit()

    async def clear_subscribers(self) -> None:
        from sqlalchemy import delete
        from app.models.alert import TelegramUser
        async with self.session() as db:
            await db.execute(delete(TelegramUser))
            await db.commit()

    # ── introspection ─────────────────────────────────────────────────────

    async def count(self, model) -> int:
        from sqlalchemy import func, select
        async with self.session() as db:
            return int((await db.execute(select(func.count()).select_from(model))).scalar())

    async def rows(self, model) -> list:
        from sqlalchemy import select
        async with self.session() as db:
            return list((await db.execute(select(model))).scalars().all())


DEFAULT_BUCKETS = [("91-92°F", 91, 92), ("93-94°F", 93, 94),
                   ("95-96°F", 95, 96), ("97-98°F", 97, 98)]


@pytest.fixture
def forecast_temp(request) -> float:
    """The high every weather source reports. Override per test:

        @pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)
    """
    return getattr(request, "param", 95.0)


@pytest.fixture
def market_buckets(request):
    """Bucket ladder for the seeded market, centred on the default forecast."""
    return getattr(request, "param", DEFAULT_BUCKETS)


@pytest.fixture
def source_spread(request) -> float:
    """°F fan between the weather sources. 0 = unanimous."""
    return getattr(request, "param", 0.0)


@pytest.fixture
def days_ahead(request) -> int:
    """How far out the market resolves.

    Default 1: inside `max_days_ahead_for_alert` (3), and not today, so a
    test never depends on what time of day it runs. Parametrize to sit on
    the horizon boundary:

        @pytest.mark.parametrize("days_ahead", [4], indirect=True)
    """
    return getattr(request, "param", 1)


@pytest.fixture
async def pipeline(monkeypatch, forecast_temp, market_buckets, source_spread, days_ahead):
    from app.database import Base
    from app.models.alert import TelegramUser
    from app.models.market import Market, MarketOutcome
    import app.models  # noqa: F401  — registers every table

    city = city_fixtures.DEFAULT
    # Relative to the real today, because the detector computes days_ahead
    # against `date.today()` and a frozen date would drift out of the horizon.
    event_date = date.today() + timedelta(days=days_ahead)

    router = http_router.install(monkeypatch, http_router.HttpRouter())
    _wire_weather(router, event_date, forecast_temp, source_spread)
    _wire_polymarket(router)

    from tests.mocks import telegram_fake
    bot = telegram_fake.install(monkeypatch)

    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)

    async with maker() as db:
        db.add(city.as_row())
        db.add(Market(
            id=1, city_id=city.id, external_id="highest-temperature-in-austin",
            question="Highest temperature in Austin?",
            event_date=event_date, resolved=False,
        ))
        for i, (label, lo, hi) in enumerate(market_buckets):
            db.add(MarketOutcome(
                id=i + 1, market_id=1, bucket_label=label,
                bucket_min=lo, bucket_max=hi, bucket_unit="F", token_id=f"tok{i}",
            ))
        # A subscriber with no confidence floor, so delivery is never the
        # reason a test sees zero messages.
        db.add(TelegramUser(chat_id=999, min_confidence=0,
                            cities_watched=[], alert_types_enabled=[]))
        await db.commit()

    yield Pipeline(maker, router, bot, city, event_date, market_buckets)
    await engine.dispose()


def _wire_weather(router, event_date, high: float, spread: float) -> None:
    """Register every weather endpoint. Sources fan out by `spread` °F.

    ICON and HRRR share Open-Meteo's URL with GFS/ECMWF, so they cannot be
    given different temperatures through routing alone — the handler reads
    the `models` parameter and answers accordingly.
    """
    order = ["gfs_seamless", "ecmwf_ifs025", "hrrr", "icon"]
    low = high - 21

    def open_meteo(request):
        import httpx
        model = request.url.params.get("models", "")
        idx = next((i for i, m in enumerate(order) if m in model), 0)
        offset = 0.0 if spread == 0 else (idx / max(1, len(order) - 1) - 0.5) * spread
        return httpx.Response(
            200, json=wx.open_meteo_daily(event_date, high + offset, low + offset)
        )

    members = [high - 2, high - 1, high, high + 1, high + 2]
    router.add(wx.OPEN_METEO_ENSEMBLE, wx.open_meteo_ensemble(event_date, members))
    router.add_handler(wx.OPEN_METEO, open_meteo)
    router.add(wx.TOMORROWIO, wx.tomorrowio_daily(event_date, high, low))
    router.add(wx.METEOSOURCE, wx.meteosource_daily(event_date, high, low))
    router.add(wx.NWS_GRIDPOINT, wx.nws_forecast(event_date, high, low))
    router.add(wx.NWS_POINTS, wx.nws_points())
    router.add(wx.METAR, wx.metar_records())
    router.add(wx.PIREP, wx.pirep_records())


def _wire_polymarket(router, yes_price: float = 0.62) -> None:
    router.add(pm.CLOB_MIDPOINT, pm.midpoint(yes_price))
    router.add(pm.CLOB_PRICE, pm.price(yes_price))
    router.add(pm.CLOB_BOOK, pm.book(yes_price - 0.01, yes_price + 0.01))
    router.add(pm.GAMMA_EVENTS, pm.events_response())


@pytest.fixture
def break_source():
    """Make one provider fail on an already-wired router.

    The pipeline fixture has registered the happy path by the time a test
    runs, and routes match in registration order — so the override has to go
    in front, which is what `prepend` does.
    """
    def _break(router, endpoint: str, *, status: int = 503, body=None):
        return router.prepend(endpoint, body or {"error": "provider down"},
                              status=status)
    return _break


@pytest.fixture
def no_backoff(monkeypatch):
    """Collapse `BaseCollector`'s 2s/4s/8s retry sleeps.

    Without this a single 5xx test takes 14 seconds; the retry behaviour
    itself is unchanged and still observable through the router's call count.
    """
    import app.collectors.base as base

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(base.asyncio, "sleep", _instant)
