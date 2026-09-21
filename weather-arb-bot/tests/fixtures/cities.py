"""City fixtures derived from the application's own seed list.

These are generated from `app.utils.seed.CITIES` rather than retyped, so a
city added, renamed or re-pointed to a different ICAO shows up here
automatically. `test_harness_selfcheck` asserts that link holds.

ID assignment mirrors seeding order (1-based), which is what the seeder
produces on a fresh database — so `CITY_IDS["Austin"] == 1` matches a real
deployment rather than being an arbitrary test number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.utils.seed import CITIES as _SEED


@dataclass(frozen=True)
class CityFixture:
    id: int
    name: str
    slug: str
    primary_icao: str
    reference_icao: str
    lat: float
    lon: float
    tz: str
    wunderground_url: str

    def as_row(self, **overrides):
        """Build a real `City` ORM instance for tests that need one.

        Column names follow the model: coordinates live in `nws_lat`/`nws_lon`,
        not `latitude`/`longitude`. `**overrides` sets anything else a test
        cares about, e.g. `blacklisted=True` or `suspended_until=...`.
        """
        from app.models.city import City
        fields = dict(
            id=self.id,
            name=self.name,
            polymarket_slug=self.slug,
            primary_icao=self.primary_icao,
            reference_icao=self.reference_icao,
            wunderground_url=self.wunderground_url,
            nws_lat=self.lat,
            nws_lon=self.lon,
            timezone=self.tz,
            active=True,
            blacklisted=False,
            intraday_enabled=True,
        )
        fields.update(overrides)
        return City(**fields)


CITIES: list[CityFixture] = [
    CityFixture(
        id=i + 1, name=name, slug=slug, primary_icao=primary,
        reference_icao=reference, lat=lat, lon=lon, tz=tz, wunderground_url=wu,
    )
    for i, (name, slug, primary, reference, wu, lat, lon, tz) in enumerate(_SEED)
]

BY_NAME: dict[str, CityFixture] = {c.name: c for c in CITIES}
BY_SLUG: dict[str, CityFixture] = {c.slug: c for c in CITIES}
CITY_IDS: dict[str, int] = {c.name: c.id for c in CITIES}


def get(name_or_slug: str) -> CityFixture:
    """Look up by display name or Polymarket slug; raises if unknown."""
    if name_or_slug in BY_NAME:
        return BY_NAME[name_or_slug]
    if name_or_slug in BY_SLUG:
        return BY_SLUG[name_or_slug]
    raise KeyError(
        f"No such city fixture: {name_or_slug!r}. "
        f"Known: {sorted(BY_NAME)} / {sorted(BY_SLUG)}"
    )


def rows(*names: str) -> list:
    """`City` ORM rows for the named cities, or all of them if none given."""
    picked = [get(n) for n in names] if names else CITIES
    return [c.as_row() for c in picked]


# Austin is the default subject across the harness: it is the first seeded
# city, it is in a single timezone, and its market slug is the simplest.
DEFAULT = BY_NAME["Austin"]

# NYC earns a named constant because its resolution station is a documented
# trap — Polymarket settles NYC on KNYC (Central Park), not the airport KLGA.
# Any test about station choice should use this rather than a literal.
NYC = BY_NAME["New York"]

MULTI_TZ: list[str] = sorted({c.tz for c in CITIES})


def coords(name_or_slug: str) -> tuple[float, float]:
    c = get(name_or_slug)
    return c.lat, c.lon


def icao(name_or_slug: str, reference: bool = False) -> str:
    c = get(name_or_slug)
    return c.reference_icao if reference else c.primary_icao


def find_by_icao(code: str) -> Optional[CityFixture]:
    code = code.upper()
    for c in CITIES:
        if code in (c.primary_icao.upper(), c.reference_icao.upper()):
            return c
    return None
