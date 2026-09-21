"""Response payloads shaped like the real weather APIs.

Every builder here was written against the parsing code in `app/collectors/`,
so the field names, nesting and units match what the bot actually reads. If a
provider changes its schema, the corresponding builder should fail a test
rather than quietly keep returning a payload the bot can no longer parse.

Temperatures are Fahrenheit everywhere except METAR, which reports Celsius
exactly as aviationweather.gov does.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

# Open-Meteo drives four collectors: GFS, ECMWF, HRRR and ICON. They differ
# only in the `models` query parameter, so one builder serves all four.
OPEN_METEO = "api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE = "ensemble-api.open-meteo.com/v1/ensemble"
TOMORROWIO = "api.tomorrow.io/v4/weather/forecast"
METEOSOURCE = "meteosource.com/api/v1/free/point"
NWS_POINTS = "api.weather.gov/points"
NWS_GRIDPOINT = "api.weather.gov/gridpoints"
METAR = "aviationweather.gov/api/data/metar"
PIREP = "aviationweather.gov/api/data/pirep"
BUOY = "ndbc.noaa.gov"


def _days(target: date, before: int = 1, after: int = 1) -> list[date]:
    return [target + timedelta(days=d) for d in range(-before, after + 1)]


# ── Open-Meteo: GFS / ECMWF / HRRR / ICON ─────────────────────────────────

def open_meteo_daily(
    target: date,
    high: float,
    low: float,
    *,
    wind_kt: float = 8.0,
    lat: float = 30.1975,
    lon: float = -97.6664,
) -> dict:
    """`/v1/forecast` with daily max/min. The collector finds `target` by
    matching the ISO date string in `daily.time`, so neighbouring days are
    included to prove the lookup indexes correctly rather than taking [0]."""
    dates = _days(target)
    offset = {d: i - 1 for i, d in enumerate(dates)}
    return {
        "latitude": lat,
        "longitude": lon,
        "daily": {
            "time": [str(d) for d in dates],
            # Neighbouring days are deliberately different so a test that
            # accidentally reads the wrong index produces a wrong number.
            "temperature_2m_max": [high + offset[d] * 4 for d in dates],
            "temperature_2m_min": [low + offset[d] * 3 for d in dates],
            "windspeed_10m_max": [wind_kt for _ in dates],
        },
    }


def open_meteo_missing_date(target: date) -> dict:
    """A well-formed response that simply does not contain the target date.

    This is the real-world 'collector miss' shape — the request succeeded, so
    no exception fires, but there is nothing usable in it.
    """
    other = target + timedelta(days=30)
    return {
        "latitude": 30.0,
        "longitude": -97.0,
        "daily": {
            "time": [str(other)],
            "temperature_2m_max": [70.0],
            "temperature_2m_min": [50.0],
            "windspeed_10m_max": [5.0],
        },
    }


def open_meteo_empty() -> dict:
    """Response with no `daily` block at all — collector must return None."""
    return {"latitude": 30.0, "longitude": -97.0}


def open_meteo_ensemble(
    target: date,
    member_highs: Sequence[float],
    *,
    member_lows: Optional[Sequence[float]] = None,
) -> dict:
    """`/v1/ensemble` hourly members.

    The collector derives each member's daily high as the max over the target
    day's hours, so we emit a flat day whose peak equals the requested value.
    `member_highs` is the whole point of this builder: ensemble SPREAD is what
    feeds sigma, so tests can hand it a tight or a wide distribution.
    """
    lows = list(member_lows) if member_lows is not None else [h - 20 for h in member_highs]
    if len(lows) != len(member_highs):
        raise ValueError("member_lows must be the same length as member_highs")

    hours = [f"{target}T{h:02d}:00" for h in range(24)]
    hourly: dict = {"time": hours}
    for i, (hi, lo) in enumerate(zip(member_highs, lows)):
        # Hour 15 carries the daily max, hour 5 the daily min; the rest sit
        # between them so max()/min() pick out exactly the requested values.
        series = [(hi + lo) / 2] * 24
        series[15] = hi
        series[5] = lo
        hourly[f"temperature_2m_member{i:02d}"] = series
    return {"latitude": 30.0, "longitude": -97.0, "hourly": hourly}


# ── Tomorrow.io ───────────────────────────────────────────────────────────

def tomorrowio_daily(
    target: date, high: float, low: float, *, lat: float = 30.1975, lon: float = -97.6664
) -> dict:
    return {
        "location": {"lat": lat, "lon": lon},
        "timelines": {
            "daily": [
                {
                    "time": f"{d}T06:00:00Z",
                    "values": {
                        "temperatureMax": high + (i - 1) * 4,
                        "temperatureMin": low + (i - 1) * 3,
                    },
                }
                for i, d in enumerate(_days(target))
            ]
        },
    }


def tomorrowio_rate_limited() -> dict:
    """Body Tomorrow.io returns with HTTP 429 on the free tier."""
    return {"code": 429001, "type": "Too Many Calls", "message": "rate limit exceeded"}


# ── Meteosource ───────────────────────────────────────────────────────────

def meteosource_daily(
    target: date, high: float, low: float, *, lat: float = 30.1975, lon: float = -97.6664
) -> dict:
    return {
        "lat": lat,
        "lon": lon,
        "daily": {
            "data": [
                {
                    "day": str(d),
                    "all_day": {
                        "temperature_max": high + (i - 1) * 4,
                        "temperature_min": low + (i - 1) * 3,
                    },
                }
                for i, d in enumerate(_days(target))
            ]
        },
    }


# ── NWS (two hops: /points then the gridpoint URL it hands back) ──────────

def nws_points(
    *, grid_id: str = "EWX", grid_x: int = 156, grid_y: int = 91,
    lat: float = 30.1975, lon: float = -97.6664,
) -> dict:
    return {
        "properties": {
            "gridId": grid_id,
            "gridX": grid_x,
            "gridY": grid_y,
            "forecast": (
                f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast"
            ),
            "relativeLocation": {"geometry": {"coordinates": [lon, lat]}},
        }
    }


def nws_forecast(
    target: date, high: float, low: float, *, conditions: str = "Sunny"
) -> dict:
    """Gridpoint forecast. NWS splits day and night into separate periods and
    reports a single `temperature` per period — daytime is the high, nighttime
    the low. The collector must pair them by date, so both are emitted."""
    periods = []
    for i, d in enumerate(_days(target)):
        periods.append({
            "number": i * 2 + 1,
            "name": "Today",
            "startTime": f"{d}T06:00:00-05:00",
            "isDaytime": True,
            "temperature": high + (i - 1) * 4,
            "temperatureUnit": "F",
            "shortForecast": conditions,
        })
        periods.append({
            "number": i * 2 + 2,
            "name": "Tonight",
            "startTime": f"{d}T18:00:00-05:00",
            "isDaytime": False,
            "temperature": low + (i - 1) * 3,
            "temperatureUnit": "F",
            "shortForecast": "Clear",
        })
    return {"properties": {"periods": periods}}


# ── METAR / PIREP / buoy ──────────────────────────────────────────────────

def metar_records(
    icao: str = "KAUS",
    *,
    temp_c: float = 31.1,
    dew_c: float = 18.3,
    observed: Optional[datetime] = None,
    wind_dir: int = 170,
    wind_kt: int = 9,
    gust_kt: Optional[int] = None,
) -> list[dict]:
    """aviationweather.gov `/api/data/metar?format=json` returns a LIST."""
    ts = observed or datetime.now(timezone.utc).replace(microsecond=0)
    return [{
        "icaoId": icao,
        "obsTime": int(ts.timestamp()),
        "temp": temp_c,
        "dewp": dew_c,
        "wdir": wind_dir,
        "wspd": wind_kt,
        "wgst": gust_kt,
        "altim": 1013.2,
        "visib": 10.0,
        "wxString": "",
        "clouds": [{"cover": "FEW", "base": 4000}],
        "rawOb": (
            f"{icao} {ts:%d%H%M}Z {wind_dir:03d}{wind_kt:02d}KT 10SM FEW040 "
            f"{int(temp_c):02d}/{int(dew_c):02d} A2993"
        ),
    }]


def metar_empty() -> list:
    """Station reported nothing — a real and frequent condition."""
    return []


def pirep_records(icao: str = "KAUS") -> list[dict]:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    return [{
        "receiptTime": ts.isoformat(),
        "obsTime": int(ts.timestamp()),
        "rawOb": f"{icao} UA /OV {icao}180010/TM 1810/FL050/TP C172/TA 24/WV 18010KT",
    }]


def buoy_text(*, water_temp_c: float = 27.4, air_temp_c: float = 29.1) -> str:
    """NDBC realtime2 is a fixed-width text file, not JSON. `MM` means missing."""
    return (
        "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE\n"
        "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft\n"
        f"2026 07 28 18 00  160  5.0  6.0    MM    MM    MM  MM 1014.5  {air_temp_c:.1f}  {water_temp_c:.1f}  22.0   MM   MM    MM\n"
        f"2026 07 28 17 30  155  4.8  5.8    MM    MM    MM  MM 1014.7  {air_temp_c - 0.3:.1f}  {water_temp_c:.1f}  22.1   MM   MM    MM\n"
    )
