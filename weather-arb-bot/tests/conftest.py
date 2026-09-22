"""Shared test harness.

Three guarantees are enforced for every non-integration test:

  1. No network.   Blocked at the socket layer, below httpx and below any
                   library that might bypass it. A test that reaches for an
                   unmocked endpoint fails loudly instead of burning a
                   Tomorrow.io call from the 500/day free tier — or, worse,
                   passing in CI only when the internet happens to be up.
  2. No Postgres.  `DATABASE_URL` is pointed at an address that cannot
                   resolve, so an accidental real connection surfaces as an
                   error rather than silently hitting a developer's local DB.
  3. No Telegram.  Covered by the `fake_telegram` fixture; the socket guard
                   backstops anything that forgets to use it.

Tests marked `@pytest.mark.integration` are exempt from the network guard —
they exist to hit the real APIs — and are deselected by default via
`-m "not integration"`.
"""
from __future__ import annotations

import os
import socket
from datetime import date, datetime, timezone

import pytest

# Must precede any app import: app.config reads the environment at import
# time, and app.database builds its engine from it.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@db.invalid:5432/test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("APP_ENV", "test")

from tests.fixtures import cities as city_fixtures          # noqa: E402
from tests.fixtures import scenarios as scenario_fixtures    # noqa: E402
from tests.mocks import db as db_mocks                       # noqa: E402
from tests.mocks import http_router as http_mocks            # noqa: E402
from tests.mocks import polymarket_payloads as pm_payloads   # noqa: E402
from tests.mocks import sqlite_compat                        # noqa: E402
from tests.mocks import telegram_fake                        # noqa: E402
from tests.mocks import weather_payloads as wx_payloads      # noqa: E402


# ── Guard 1: no network ───────────────────────────────────────────────────

class NetworkAccessDenied(RuntimeError):
    """A test tried to open a real socket."""


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_local(address) -> bool:
    """Loopback stays open: pytest plugins and debuggers use it, and nothing
    in this app talks to a local service during a unit test."""
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX and friends — not an outbound call
    host = str(address[0])
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "")


def _denied(address):
    return NetworkAccessDenied(
        f"Blocked outbound connection to {address!r}.\n"
        "Unit tests must not touch the network. Register the endpoint on the "
        "`http` fixture (tests/mocks/http_router.py), or mark the test "
        "@pytest.mark.integration if it is meant to hit the real API."
    )


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        yield
        return

    def blocked_connect(self, address, *a, **kw):
        if _is_local(address):
            return _REAL_CONNECT(self, address, *a, **kw)
        raise _denied(address)

    def blocked_connect_ex(self, address, *a, **kw):
        if _is_local(address):
            return _REAL_CONNECT_EX(self, address, *a, **kw)
        raise _denied(address)

    def blocked_create_connection(address, *a, **kw):
        if _is_local(address):
            return _REAL_CREATE_CONNECTION(address, *a, **kw)
        raise _denied(address)

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    yield


# ── Guard 2 + 3: isolation is asserted, not assumed ───────────────────────

@pytest.fixture(autouse=True)
def reset_module_caches():
    """Clear the analyzer's process-global state between tests.

    Several modules memoise at import scope — the calibration table (30-min
    TTL), per-city model skill and weights, and the side-alert dedup sets,
    which only self-clear when the calendar date changes and therefore never
    clear inside one test run. Left alone, whichever test ran first decides
    what every later test sees.
    """
    import app.analyzers.calibrator as calibrator
    import app.analyzers.model_skill as model_skill
    import app.analyzers.model_weights as model_weights
    import app.analyzers.opportunity_detector as detector
    import app.analyzers.beta_opportunity_detector as beta_detector

    def _clear():
        calibrator._cache = {}
        calibrator._cache_ts = None
        model_skill._cache.clear()
        model_weights._cache.clear()
        detector._side_alert_date = None
        detector._open_position_last_sent.clear()
        detector._bucket_switch_alerts_sent.clear()
        beta_detector._beta_dedup_date = None
        beta_detector._beta_open_pos_last_sent.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def isolated_settings(request, monkeypatch):
    """Keep every test on a non-resolvable DB and a silent bot by default.

    `fake_telegram` overrides the token afterwards for the tests that want the
    send path to run.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    monkeypatch.setattr("app.config.settings.telegram_bot_token", "", raising=False)
    monkeypatch.setattr(
        "app.config.settings.database_url",
        "postgresql://test:test@db.invalid:5432/test",
        raising=False,
    )
    yield


# ── HTTP ──────────────────────────────────────────────────────────────────

@pytest.fixture
def http(monkeypatch) -> http_mocks.HttpRouter:
    """Router every `httpx.AsyncClient` is forced onto. Register routes, then
    assert on `http.calls_to(...)` / `http.count(...)`."""
    return http_mocks.install(monkeypatch, http_mocks.HttpRouter())


@pytest.fixture
def weather_api(http) -> http_mocks.HttpRouter:
    """`http` with every weather endpoint pre-wired to a plausible answer.

    Austin, 95°F/74°F on 2026-07-28, all sources agreeing. Override any single
    endpoint by registering a more specific route first, or by re-registering
    on top — later routes lose to earlier ones, so add overrides before using
    the fixture's defaults.
    """
    target = scenario_fixtures.TODAY
    http.add(wx_payloads.OPEN_METEO_ENSEMBLE,
             wx_payloads.open_meteo_ensemble(target, [93.0, 94.0, 95.0, 96.0, 97.0]))
    http.add(wx_payloads.OPEN_METEO, wx_payloads.open_meteo_daily(target, 95, 74))
    http.add(wx_payloads.TOMORROWIO, wx_payloads.tomorrowio_daily(target, 95, 74))
    http.add(wx_payloads.METEOSOURCE, wx_payloads.meteosource_daily(target, 95, 74))
    http.add(wx_payloads.NWS_GRIDPOINT, wx_payloads.nws_forecast(target, 95, 74))
    http.add(wx_payloads.NWS_POINTS, wx_payloads.nws_points())
    http.add(wx_payloads.METAR, wx_payloads.metar_records())
    http.add(wx_payloads.PIREP, wx_payloads.pirep_records())
    http.add(wx_payloads.BUOY, text=wx_payloads.buoy_text())
    return http


@pytest.fixture
def polymarket_api(http) -> http_mocks.HttpRouter:
    """`http` with the Polymarket endpoints pre-wired: a 62¢ two-sided market."""
    http.add(pm_payloads.CLOB_MIDPOINT, pm_payloads.midpoint(0.62))
    http.add(pm_payloads.CLOB_PRICE, pm_payloads.price(0.62))
    http.add(pm_payloads.CLOB_BOOK, pm_payloads.book(0.61, 0.63))
    http.add(pm_payloads.CLOB_MARKETS, pm_payloads.clob_markets_page())
    http.add(pm_payloads.GAMMA_EVENTS, pm_payloads.events_response())
    http.add(pm_payloads.GAMMA_MARKETS, pm_payloads.gamma_markets_page())
    return http


# ── Telegram ──────────────────────────────────────────────────────────────

@pytest.fixture
def fake_telegram(isolated_settings, monkeypatch) -> type[telegram_fake.FakeBot]:
    """Records outbound Telegram messages AND supplies a token.

    Without the token every send function returns at its first line and the
    test asserts nothing while looking green — see tests/mocks/telegram_fake.py.

    Depends on `isolated_settings` so the ordering is explicit: that fixture
    blanks the token, this one sets it. Relying on autouse ordering instead
    would leave the token empty if pytest ever resolved them the other way.
    """
    bot = telegram_fake.install(monkeypatch)
    yield bot
    bot.reset()


# ── Database ──────────────────────────────────────────────────────────────

@pytest.fixture
def fake_db() -> db_mocks.FakeSession:
    """An `AsyncSession` stand-in to pass as the `db` argument."""
    return db_mocks.FakeSession()


@pytest.fixture
def db_factory(monkeypatch):
    """Redirect `AsyncSessionLocal` for code that opens its own session.

    Call it with the modules to patch:
        session = db_factory("app.workers.jobs", "app.api.admin")
    """
    def _install(*targets: str, session=None) -> db_mocks.FakeSession:
        return db_mocks.install(monkeypatch, session, *targets)
    return _install


@pytest.fixture
async def sqlite_db():
    """A real in-memory SQLite session, for tests where the SQL is the point.

    Postgres-only constructs (`ON CONFLICT`, `DISTINCT ON`) do not compile
    here by design — see tests/mocks/sqlite_compat.py.
    """
    from app.database import Base
    import app.models  # noqa: F401  — registers every table on Base.metadata

    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    async with maker() as session:
        yield session
    await engine.dispose()


# ── Data ──────────────────────────────────────────────────────────────────

@pytest.fixture
def cities():
    """The seeded city list, with real ids, ICAOs and coordinates."""
    return city_fixtures


@pytest.fixture
def austin() -> city_fixtures.CityFixture:
    return city_fixtures.DEFAULT


@pytest.fixture
def scenarios():
    """Named signal/market/exit situations — see tests/fixtures/scenarios.py."""
    return scenario_fixtures


@pytest.fixture
def today() -> date:
    """Frozen date shared by the payload builders, so a fixture built on one
    line and asserted on another cannot straddle midnight."""
    return scenario_fixtures.TODAY


@pytest.fixture
def now() -> datetime:
    return scenario_fixtures.NOW


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(timezone.utc)
