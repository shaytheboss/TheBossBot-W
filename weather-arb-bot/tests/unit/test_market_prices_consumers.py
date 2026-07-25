"""Safety audit of every market_prices consumer, locked in as tests.

The user's concern is legitimate: efficiency changes must not silently remove a
capability. These tests encode what the audit of the repository actually found,
so a future change that breaks one of these assumptions fails here instead of in
production.

Audit result — the COMPLETE set of market_prices readers:

  1. signal_aggregator._latest_price()  → newest row only (LIMIT 1)
  2. signal_aggregator._price_trend()   → last 60 minutes … but its result is
                                          never read by anything (see below)
  3. api/markets.py /{id}/prices        → last `hours` (default 24)
  4. workers/jobs.job_fetch_polymarket  → newest row per outcome (write path)

Nothing reads price rows older than 24 hours, and nothing reads the stored
timestamp to judge staleness.
"""
import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def _sources():
    for p in APP.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        yield p, p.read_text()


# ── The write-on-change optimisation is only safe if these hold ───────────────

def test_nothing_consumes_price_trend():
    """price_trend is produced but never read — that is what makes it safe to
    stop computing it. If someone starts using it, this test fails and the
    60-minute range scan must be reinstated (or served another way)."""
    consumers = []
    for path, src in _sources():
        for m in re.finditer(r'[\[\(]\s*["\']price_trend["\']|\.price_trend\b', src):
            line = src[: m.start()].count("\n") + 1
            # The producer line in signal_aggregator is the assignment itself.
            if path.name == "signal_aggregator.py":
                continue
            consumers.append(f"{path.name}:{line}")
    assert not consumers, f"price_trend is now consumed by {consumers}"


def test_nothing_reads_market_price_timestamp_for_staleness():
    """Write-on-change leaves the stored timestamp older while the PRICE stays
    correct. That is only safe because no consumer judges freshness from it."""
    # Excluded: producers/writers, and retention_job (which names the column in
    # its prune configuration — a deletion window, not a freshness check).
    skip = {"signal_aggregator.py", "polymarket_collector.py", "jobs.py",
            "retention_job.py", "dbdiag.py"}
    offenders = []
    for path, src in _sources():
        if path.name in skip:
            continue
        if re.search(r'market_price[^\n]*\btimestamp\b', src):
            offenders.append(path.name)
    assert not offenders, f"market_price timestamp used for logic in {offenders}"


def test_latest_price_reads_only_the_newest_row():
    """The analyzer needs the newest price, not history — so skipping identical
    writes cannot change what it sees (an unchanged price means the stored row
    already holds the current value)."""
    src = (APP / "analyzers" / "signal_aggregator.py").read_text()
    body = src[src.index("async def _latest_price"): src.index("async def _price_trend")]
    assert "limit(1)" in body.replace(" ", "")
    assert "desc(MarketPrice.timestamp)" in body


# ── No deletion: the retention prune must stay off ────────────────────────────

def test_price_retention_prune_is_disabled_by_default():
    """The user requires that NO price history is deleted. The hard prune must
    remain opt-in; only the lossless de-dup runs on its own."""
    from app.config import settings
    assert settings.retention_prune_enabled is False
    assert settings.retention_dedup_enabled is True


def test_dedup_never_touches_market_prices():
    """The automatic de-dup collapses duplicate FORECAST rows only. It must
    never delete price rows."""
    src = (APP / "workers" / "retention_job.py").read_text()
    dedup = src[src.index("if dedup_on:"): src.index("# ── 2.")]
    assert "market_prices" not in dedup
    assert "forecasts" in dedup
