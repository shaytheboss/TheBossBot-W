"""Which model is right most often, per city — including the record-only ones.

Read-only. Uses the same scoring as model_skill (did the forecast land in the
bucket Polymarket settled as the winner, and by how far it missed), over the
blend's models plus the extra Open-Meteo models, and writes nothing: the
trading weights in `model_skill` are computed from the blend's models only.

A week gives each (city, model) about seven settled markets — enough to see a
direction, not to act on one city at a time. The all-cities row pools them
(~48x more samples), which is where a first conclusion can come from.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.model_skill import SKILL_SOURCES, compute_city_skill
from app.models.city import City
from app.workers.open_meteo_job import extra_models, extra_source

# Below this, a city's ranking is shown but labelled as a direction only.
MIN_SAMPLES_FOR_CITY = 20


def compare_sources() -> tuple[str, ...]:
    return SKILL_SOURCES + tuple(extra_source(m) for m in extra_models())


def _row(source: str, st: dict) -> dict:
    n = st["samples"]
    return {"source": source, "samples": n, "hits": st["hits"],
            "hit_rate": round(st["hits"] / n, 3) if n else None,
            "mae_f": round(st["dist_sum"] / n, 2) if n else None,
            "bias_f": round(st["signed_sum"] / n, 2) if n else None,
            "record_only": source not in SKILL_SOURCES}


def _rank(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-(r["hit_rate"] or 0), r["mae_f"] or 99, -r["samples"]))


async def compare_models(db: AsyncSession, days_ahead: int = 1, min_samples: int = 3) -> dict:
    sources = compare_sources()
    cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
    pooled: dict[str, dict] = {}
    per_city = []
    for city in cities:
        stats = await compute_city_skill(db, city.id, sources=sources)
        rows = []
        for (source, da), st in stats.items():
            if da != days_ahead:
                continue
            rows.append(_row(source, st))
            p = pooled.setdefault(source, {"samples": 0, "hits": 0, "dist_sum": 0.0, "signed_sum": 0.0})
            for k in p:
                p[k] += st[k]
        ranked = _rank([r for r in rows if r["samples"] >= min_samples])
        best = ranked[0] if ranked else None
        per_city.append({
            "city": city.name,
            "best": best["source"] if best else None,
            "direction_only": (best is None) or best["samples"] < MIN_SAMPLES_FOR_CITY,
            "models": _rank(rows),
        })
    return {"days_ahead": days_ahead, "sources": list(sources),
            "all_cities": _rank([_row(s, st) for s, st in pooled.items()]),
            "cities": per_city}
