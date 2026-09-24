"""Open-Meteo: one request per (city, model) for every day, under a daily budget.

Why this exists. The legacy jobs (`job_fetch_models`, `job_fetch_icon`) loop
cities x 7 dates x models and send one request per date — each asking for up
to 9 days and keeping one of them. With 48 cities that is ~1,700 requests an
hour, ~41,000 a day, against a free tier of 10,000 a day. Being blocked would
take GFS, ECMWF, ICON, HRRR and both ensembles down together.

What changes. The same response already holds every day we need, so one
request per (city, model) replaces seven, and the parsing writes exactly the
rows the per-date collectors wrote (tests/flow/test_open_meteo_batch.py holds
the two paths side by side).

Freshness. A model's forecast only changes when a new run is published —
GFS, ECMWF and ICON four times a day, the ensembles every 6 hours, HRRR
hourly. Polling more often than that buys nothing; polling right after a run
is what keeps data fresh. The core tier (the models in the blend) still runs
every hour, so the lag after a new run is at most an hour, as before.

Budget. Every request is counted, weighted conservatively (see
`Spec.weight`), against `open_meteo_daily_budget` — our own ceiling below
Open-Meteo's. Lower tiers may only spend what is left after reserving the
core tier's remaining runs for the day. A 429 stops the run and pauses
fetching instead of retrying into the limit.

Extra models are record-only: written as source "om_<model>", which no
estimator reads (they read a fixed list of sources). They exist to be scored
per city — /admin/models/compare.

Update times. Every fetch is compared with the same city's previous one; when
the numbers moved, a row goes to `model_update_events`. The slow tiers are
also probed for one city in the hours they are not due, so the hour a new run
appears is known to the hour. /admin/open-meteo/updates shows it; the slow
tiers' fetch hours are tuned against it.

Rollback: set `model_fetch_mode` to "legacy" on the admin screen. The old
jobs resume on their next run; this one stops.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import delete, select

from app.collectors.base import BaseCollector
from app.collectors.gfs_collector import (
    OPEN_METEO_ENSEMBLE_URL, OPEN_METEO_URL, ensemble_summary,
)
from app.collectors.hrrr_collector import HRRR_MAX_DAYS_AHEAD, _is_conus
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.forecast import Forecast
from app.models.model_update_event import ModelUpdateEvent

logger = logging.getLogger(__name__)

# Same horizon as jobs.FORECAST_DAYS_AHEAD and icon_job.ICON_FORECAST_DAYS_AHEAD.
FORECAST_DAYS = 7
# Pause between requests: caps a run at ~400/minute, under the 600/minute limit.
PACE_SECONDS = 0.15
EXTRA_PREFIX = "om_"
TIER_ORDER = ("core", "hrrr", "probe", "ensemble", "extra")
# Change events older than this are pruned by the job itself.
UPDATE_EVENT_RETENTION_DAYS = 30
MODES = ("batched", "legacy")

# Models that only cover part of the globe. Asking outside the domain spends
# a request on a response full of nulls.
REGIONAL = {"ncep_nbm_conus": _is_conus}


@dataclass(frozen=True)
class Spec:
    tier: str
    source: str        # Forecast.source written
    model: str         # Open-Meteo `models=` parameter
    label: str         # raw_data["model"], as the legacy collectors wrote it
    kind: str = "daily"          # "daily" | "ensemble" | "hrrr"
    # Open-Meteo counts a request with more than 10 variables as several.
    # Whether ensemble members count as variables is not documented anywhere
    # this bot can read, so they are charged as if they do: ceil(members/10).
    weight: float = 1.0
    max_days_ahead: int = FORECAST_DAYS - 1

    @property
    def request_days(self) -> int:
        # The legacy collectors asked for days_ahead + 2 — a city west of UTC
        # starts its local calendar a day behind the server's.
        return self.max_days_ahead + 2


def extra_models() -> list[str]:
    raw = getattr(settings, "open_meteo_extra_models", "") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def extra_source(model: str) -> str:
    return (EXTRA_PREFIX + model)[:30]   # forecasts.source is VARCHAR(30)


def specs_for(tier: str) -> list[Spec]:
    if tier == "core":
        out = [Spec("core", "gfs", "gfs_seamless", "gfs"),
               Spec("core", "ecmwf", "ecmwf_ifs025", "ecmwf")]
        if getattr(settings, "icon_enabled", True):
            out.append(Spec("core", "icon", "icon_seamless", "icon"))
        return out
    if tier == "hrrr":
        return [Spec("hrrr", "hrrr", "gfs_hrrr", "hrrr", kind="hrrr",
                     max_days_ahead=HRRR_MAX_DAYS_AHEAD)]
    if tier == "ensemble":
        return [Spec("ensemble", "gfs_ensemble", "gfs_seamless", "gfs_ensemble",
                     kind="ensemble", weight=4.0),      # 31 members
                Spec("ensemble", "ecmwf_ensemble", "ecmwf_ifs025", "ecmwf_ensemble",
                     kind="ensemble", weight=6.0)]      # 51 members
    if tier == "extra":
        return [Spec("extra", extra_source(m), m, m) for m in extra_models()]
    if tier == "probe":
        # The slow tiers, fetched for one city in the hours they are not due,
        # so the hour a new run appears is seen to the hour rather than to
        # the six. Model runs are global: one city is enough to see them.
        return [replace(s, tier="probe") for t in ("ensemble", "extra") for s in specs_for(t)]
    raise ValueError(tier)


def probe_city(cities, spec: Spec):
    """The lowest-id city the model covers — stable across runs, so each
    fetch is compared with the same place's previous one."""
    for c in sorted(cities, key=lambda c: c.id):
        if c.nws_lat is not None and c.nws_lon is not None \
                and applies_to(spec, float(c.nws_lat), float(c.nws_lon)):
            return c
    return None


def applies_to(spec: Spec, lat: float, lon: float) -> bool:
    if spec.kind == "hrrr":
        return _is_conus(lat, lon)
    region = REGIONAL.get(spec.model)
    return region(lat, lon) if region else True


def every_h(tier: str) -> int:
    if tier == "probe":
        return 1
    key = {"core": "open_meteo_core_every_h", "hrrr": "open_meteo_core_every_h",
           "ensemble": "open_meteo_ensemble_every_h",
           "extra": "open_meteo_extra_every_h"}[tier]
    return max(1, int(getattr(settings, key, 1)))


# Offsets spread the slower tiers over different hours of the day.
_OFFSET = {"core": 0, "hrrr": 0, "probe": 0, "ensemble": 2, "extra": 3}


def due_tiers(hour_utc: int) -> list[str]:
    """Tiers due at this UTC hour. Keyed on the clock, not on "time since the
    last run", so a redeploy cannot trigger an extra round of requests."""
    due = [t for t in TIER_ORDER if (hour_utc - _OFFSET[t]) % every_h(t) == 0]
    if not getattr(settings, "open_meteo_probe_enabled", True):
        due.remove("probe")
    return due


def half_hour_tiers() -> list[str]:
    """What the :50 run fetches. HRRR is the one model that publishes hourly,
    and the one intraday weighs most."""
    return ["hrrr"] if getattr(settings, "open_meteo_hrrr_half_hourly", True) else []


def runs_left_today(tier: str, hour_utc: int) -> int:
    return sum(1 for h in range(hour_utc + 1, 24) if tier in due_tiers(h))


# ── Budget ──────────────────────────────────────────────────────────────────

@dataclass
class Budget:
    day: Optional[date] = None
    used: float = 0.0
    requests: int = 0
    by_tier: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    paused_until: Optional[datetime] = None
    pause_reason: Optional[str] = None
    rejected: dict = field(default_factory=dict)   # model → reason, for the day
    last_run: Optional[dict] = None

    def roll(self, now: datetime) -> None:
        if self.day != now.date():
            last = self.last_run
            self.__init__()
            self.day = now.date()
            self.last_run = last

    def spend(self, tier: str, weight: float) -> None:
        self.used += weight
        self.requests += 1
        self.by_tier[tier] = round(self.by_tier.get(tier, 0.0) + weight, 1)


BUDGET = Budget()


def reset_budget() -> None:
    """For tests: the budget is module state, like the other caches."""
    global BUDGET
    _LAST_FETCH.clear()
    BUDGET = Budget()


class RateLimited(Exception):
    pass


class ModelRejected(Exception):
    pass


def _pause_until(now: datetime, reason: str) -> datetime:
    if "daily" in reason.lower():
        return datetime.combine(now.date() + timedelta(days=1),
                                datetime.min.time(), tzinfo=timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


# ── HTTP ────────────────────────────────────────────────────────────────────

class OpenMeteoBatchCollector(BaseCollector):
    """One GET, one retry on a network error or 5xx. Unlike `BaseCollector._get`
    a 429 is never retried: retrying into a rate limit is how a client gets
    blocked."""

    name = "open_meteo_batch"

    async def collect(self, spec: Spec, lat: float, lon: float) -> dict:
        return await self.fetch(spec, lat, lon)

    @staticmethod
    def params(spec: Spec, lat: float, lon: float) -> tuple[str, dict]:
        if spec.kind == "ensemble":
            return OPEN_METEO_ENSEMBLE_URL, {
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m", "models": spec.model,
                "temperature_unit": "fahrenheit",
                "forecast_days": spec.request_days, "timezone": "auto",
            }
        if spec.kind == "hrrr":
            return OPEN_METEO_URL, {
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "forecast_days": spec.request_days, "models": spec.model,
                "timezone": "auto",
            }
        return OPEN_METEO_URL, {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max",
            "temperature_unit": "fahrenheit", "windspeed_unit": "kn",
            "forecast_days": spec.request_days, "models": spec.model,
            "timezone": "auto",
        }

    async def fetch(self, spec: Spec, lat: float, lon: float) -> dict:
        url, params = self.params(spec, lat, lon)
        client = await self._get_client()
        for attempt in range(2):
            BUDGET.spend(spec.tier, spec.weight)   # every request sent counts
            try:
                resp = await client.get(url, params=params)
            except httpx.RequestError:
                if attempt == 0:
                    await asyncio.sleep(2.0)
                    continue
                raise
            if resp.status_code == 429:
                raise RateLimited(_reason(resp))
            if resp.status_code == 400:
                raise ModelRejected(_reason(resp))
            if resp.status_code >= 500 and attempt == 0:
                await asyncio.sleep(2.0)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover


def _reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("reason"):
            return str(body["reason"])[:200]
    except ValueError:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:160]}"


# ── Parsing: the rows the legacy per-date collectors wrote ──────────────────

def _at(values, i):
    return values[i] if isinstance(values, list) and i < len(values) else None


def parse(spec: Spec, data: dict, dates: list[date]) -> dict[date, dict]:
    if spec.kind == "ensemble":
        hourly = data.get("hourly") or {}
        if not hourly.get("time"):
            return {}
        out = {}
        for d in dates:
            s = ensemble_summary(hourly, str(d))
            if s:
                out[d] = s
        return out

    daily = data.get("daily") or {}
    index = {t: i for i, t in enumerate(daily.get("time") or [])}
    out = {}
    for d in dates:
        i = index.get(str(d))
        if i is None:
            continue
        high = _at(daily.get("temperature_2m_max"), i)
        low = _at(daily.get("temperature_2m_min"), i)
        if high is None or low is None:
            continue   # beyond the model's horizon, or outside its domain
        if spec.kind == "hrrr":
            out[d] = {"predicted_high_f": round(high), "predicted_low_f": round(low),
                      "model": spec.label, "forecast_date": str(d)}
        else:
            out[d] = {"predicted_high_f": round(high), "predicted_low_f": round(low),
                      "wind_max_kt": _at(daily.get("windspeed_10m_max"), i),
                      "model": spec.label, "forecast_date": str(d),
                      "used_lat": data.get("latitude"), "used_lon": data.get("longitude")}
    return out


def to_forecast(spec: Spec, city_id: int, d: date, parsed: dict) -> Forecast:
    if spec.kind == "ensemble":
        high, low = parsed.get("mean_high_f"), parsed.get("mean_low_f")
    else:
        high, low = parsed.get("predicted_high_f"), parsed.get("predicted_low_f")
    return Forecast(city_id=city_id, source=spec.source, forecast_for_date=d,
                    predicted_high_f=high, predicted_low_f=low, raw_data=parsed)


# ── The run ─────────────────────────────────────────────────────────────────

def _cap() -> float:
    return float(getattr(settings, "open_meteo_daily_budget", 8000))


def _tier_cost(tier: str, cities) -> float:
    return sum(s.weight for s in specs_for(tier) for c in cities
               if c.nws_lat is not None and c.nws_lon is not None
               and applies_to(s, float(c.nws_lat), float(c.nws_lon)))


async def run_open_meteo(db, cities, *, now: Optional[datetime] = None,
                         tiers: Optional[list[str]] = None,
                         collector: Optional[OpenMeteoBatchCollector] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    BUDGET.roll(now)
    today = date.today()   # server date, exactly as the legacy jobs used it
    col = collector or _collector
    due = list(tiers) if tiers is not None else due_tiers(now.hour)
    summary = {"at": now.isoformat(timespec="minutes"), "tiers": due, "stored": 0,
               "requests": 0, "errors": 0, "skipped_budget": 0, "stopped": None}
    if BUDGET.paused_until and now < BUDGET.paused_until:
        summary["stopped"] = f"paused until {BUDGET.paused_until:%H:%M} UTC: {BUDGET.pause_reason}"
        BUDGET.last_run = summary
        return summary

    # What the core tier still needs today; lower tiers may not touch it.
    reserve = _tier_cost("core", cities) * runs_left_today("core", now.hour)
    start_requests = BUDGET.requests

    changes: dict[str, list] = {}   # source → [changed, compared, latest prev fetch]

    for tier in TIER_ORDER:
        if tier not in due:
            continue
        for spec in specs_for(tier):
            if spec.model in BUDGET.rejected:
                continue
            targets = cities
            if tier == "probe":
                if _base_tier(spec) in due_tiers(now.hour):
                    continue          # the full tier is scheduled this hour anyway
                one = probe_city(cities, spec)
                targets = [one] if one is not None else []
            dates = [today + timedelta(days=i) for i in range(spec.max_days_ahead + 1)]
            for city in targets:
                if city.nws_lat is None or city.nws_lon is None:
                    continue
                lat, lon = float(city.nws_lat), float(city.nws_lon)
                if not applies_to(spec, lat, lon):
                    continue
                need = spec.weight + (0.0 if tier == "core" else reserve)
                if BUDGET.used + need > _cap():
                    summary["skipped_budget"] += 1
                    BUDGET.skipped[tier] = BUDGET.skipped.get(tier, 0) + 1
                    continue
                try:
                    data = await col.fetch(spec, lat, lon)
                except RateLimited as e:
                    BUDGET.paused_until = _pause_until(now, str(e))
                    BUDGET.pause_reason = str(e)
                    summary["stopped"] = f"429 from Open-Meteo: {e}"
                    logger.error("Open-Meteo rate limit — pausing until %s: %s",
                                 BUDGET.paused_until, e)
                    summary["requests"] = BUDGET.requests - start_requests
                    BUDGET.last_run = summary
                    await _record_changes(db, changes, now)
                    return summary
                except ModelRejected as e:
                    if spec.source.startswith(EXTRA_PREFIX):
                        BUDGET.rejected[spec.model] = str(e)
                        logger.warning("Open-Meteo rejected model %s: %s", spec.model, e)
                        break
                    summary["errors"] += 1
                    logger.error("Open-Meteo rejected %s for %s: %s", spec.model, city.name, e)
                    continue
                except Exception as e:
                    summary["errors"] += 1
                    logger.error("Open-Meteo %s failed for %s: %s", spec.source, city.name, e)
                    continue
                finally:
                    if PACE_SECONDS:
                        await asyncio.sleep(PACE_SECONDS)

                rows = parse(spec, data, dates)
                _note_change(changes, spec, city.id, rows, data, now)
                for d, parsed in rows.items():
                    db.add(to_forecast(spec, city.id, d, parsed))
                if rows:
                    await db.commit()
                    summary["stored"] += len(rows)

    summary["requests"] = BUDGET.requests - start_requests
    summary["changed"] = {k: v[0] for k, v in changes.items() if v[0]}
    BUDGET.last_run = summary
    await _record_changes(db, changes, now)
    logger.info("Open-Meteo run: %s", summary)
    return summary


def _base_tier(spec: Spec) -> str:
    return "extra" if spec.source.startswith(EXTRA_PREFIX) else "ensemble"


# ── When did a model's numbers change? ─────────────────────────────────────

# (source, city_id) → (fetched_at, {date: hash of the unrounded values}).
# Memory only: after a restart the first fetch has nothing to compare with.
_LAST_FETCH: dict[tuple, tuple] = {}


def _fingerprint(spec: Spec, rows: dict, data: dict) -> dict:
    """Per date, a hash of the values BEFORE rounding — a new run that moves
    a high by 0.3°F must count as a change even when the stored integer
    does not move."""
    if spec.kind == "ensemble":
        return {d: hash(tuple(r["ensemble_highs"]) + tuple(r["ensemble_lows"]))
                for d, r in rows.items()}
    daily = data.get("daily") or {}
    index = {t: i for i, t in enumerate(daily.get("time") or [])}
    return {d: hash((_at(daily.get("temperature_2m_max"), index[str(d)]),
                     _at(daily.get("temperature_2m_min"), index[str(d)])))
            for d in rows}


def _note_change(changes: dict, spec: Spec, city_id: int, rows: dict, data: dict,
                 now: datetime) -> None:
    if not rows:
        return
    fp = _fingerprint(spec, rows, data)
    key = (spec.source, city_id)
    prev = _LAST_FETCH.get(key)
    _LAST_FETCH[key] = (now, fp)
    if prev is None:
        return
    prev_at, prev_fp = prev
    overlap = set(prev_fp) & set(fp)
    if not overlap or prev_at >= now:
        return
    entry = changes.setdefault(spec.source, [0, 0, None])
    entry[1] += 1
    if any(prev_fp[d] != fp[d] for d in overlap):
        entry[0] += 1
        entry[2] = prev_at if entry[2] is None else max(entry[2], prev_at)


async def _record_changes(db, changes: dict, now: datetime) -> None:
    try:
        wrote = False
        for source, (changed, compared, prev_at) in changes.items():
            if changed:
                db.add(ModelUpdateEvent(source=source, detected_at=now, prev_fetch_at=prev_at,
                                        cities_changed=changed, cities_compared=compared))
                wrote = True
        if now.hour == 0 and now.minute < 30:
            await db.execute(delete(ModelUpdateEvent).where(
                ModelUpdateEvent.detected_at
                < now - timedelta(days=UPDATE_EVENT_RETENTION_DAYS)))
            wrote = True
        if wrote:
            await db.commit()
    except Exception as e:   # a measurement must never cost a forecast run
        logger.warning("model_update_events write failed: %s", e)
        await db.rollback()


async def update_report(db, days: int = 3, now: Optional[datetime] = None) -> dict:
    """Per source: when its numbers changed over the last `days`, and the UTC
    hours those changes landed in. The slow tiers' fetch hours are tuned
    against this."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(
        select(ModelUpdateEvent.source, ModelUpdateEvent.detected_at,
               ModelUpdateEvent.prev_fetch_at, ModelUpdateEvent.cities_changed,
               ModelUpdateEvent.cities_compared)
        .where(ModelUpdateEvent.detected_at >= now - timedelta(days=days))
        .order_by(ModelUpdateEvent.detected_at)
    )).all()
    out: dict = {}
    for source, at, prev, changed, compared in rows:
        s = out.setdefault(source, {"changes": [], "hours_utc": {}})
        s["changes"].append({"between": f"{prev:%m-%d %H:%M}", "and": f"{at:%m-%d %H:%M}",
                             "cities": f"{changed}/{compared}"})
        s["hours_utc"][at.hour] = s["hours_utc"].get(at.hour, 0) + 1
    for s in out.values():
        s["hours_utc"] = dict(sorted(s["hours_utc"].items()))
    return {"days": days, "sources": dict(sorted(out.items())),
            "note": ("A change seen at a fetch means the new run appeared between "
                     "'between' and 'and'. Probing is hourly, HRRR every 30 min.")}


_collector = OpenMeteoBatchCollector()


async def job_fetch_open_meteo() -> None:
    if getattr(settings, "model_fetch_mode", "batched") != "batched":
        return
    async with AsyncSessionLocal() as db:
        cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
        await run_open_meteo(db, cities)


async def job_fetch_open_meteo_half_hour() -> None:
    """The :50 run — HRRR only, when open_meteo_hrrr_half_hourly is on."""
    tiers = half_hour_tiers()
    if getattr(settings, "model_fetch_mode", "batched") != "batched" or not tiers:
        return
    async with AsyncSessionLocal() as db:
        cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
        await run_open_meteo(db, cities, tiers=tiers)


# ── Status for the admin screen ─────────────────────────────────────────────

def plan(cities) -> dict:
    """Projected weighted requests per day at the current settings."""
    tiers = {}
    for tier in TIER_ORDER:
        hours = [h for h in range(24) if tier in due_tiers(h)]
        runs = len(hours) + (24 if tier in half_hour_tiers() else 0)
        if tier == "probe":
            per_day = sum(_probe_cost(cities, h) for h in hours)
            per_run = max((_probe_cost(cities, h) for h in hours), default=0.0)
        else:
            per_run = _tier_cost(tier, cities)
            per_day = per_run * runs
        tiers[tier] = {"per_run": per_run, "runs_per_day": runs,
                       "per_day": per_day, "every_h": every_h(tier)}
    total = sum(t["per_day"] for t in tiers.values())
    legacy = _legacy_per_day(cities)
    return {"tiers": tiers, "projected_per_day": math.ceil(total),
            "budget": _cap(), "legacy_per_day": legacy}


def _probe_cost(cities, hour: int) -> float:
    due = due_tiers(hour)
    return sum(s.weight for s in specs_for("probe")
               if _base_tier(s) not in due and probe_city(cities, s) is not None)


def _legacy_per_day(cities) -> int:
    """Requests/day the legacy jobs send (unweighted): per city, 7 dates x
    (gfs, ecmwf, icon, 2 ensembles) + HRRR's 3 dates in CONUS, every hour."""
    n = 0
    for c in cities:
        if c.nws_lat is None or c.nws_lon is None:
            continue
        n += FORECAST_DAYS * 5
        if _is_conus(float(c.nws_lat), float(c.nws_lon)):
            n += HRRR_MAX_DAYS_AHEAD + 1
    return n * 24


def status(cities) -> dict:
    BUDGET.roll(datetime.now(timezone.utc))
    return {
        "mode": getattr(settings, "model_fetch_mode", "batched"),
        "today": {"used": round(BUDGET.used, 1), "requests": BUDGET.requests,
                  "by_tier": BUDGET.by_tier, "skipped_for_budget": BUDGET.skipped},
        "paused_until": BUDGET.paused_until.isoformat() if BUDGET.paused_until else None,
        "pause_reason": BUDGET.pause_reason,
        "rejected_models": BUDGET.rejected,
        "extra_models": extra_models(),
        "last_run": BUDGET.last_run,
        "plan": plan(cities),
        "note": ("The counter lives in memory and restarts at zero after a deploy; "
                 "the schedule is keyed on the clock, so a deploy adds no requests."),
    }
