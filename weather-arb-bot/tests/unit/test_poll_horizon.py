"""Tiered price polling: full resolution near the trading horizon, throttled beyond it.

The analyzer only trades markets within max_days_ahead_for_alert (3 days), but
the price job polled EVERY unresolved market every 5 minutes — including ones a
week out. Each poll is an HTTP call (CPU + egress) and a potential row.

Contract: markets inside the horizon keep 5-minute resolution (nothing about
trading changes); markets beyond it refresh at most every 30 minutes, so history
still exists by the time they become tradeable.
"""
from datetime import date, datetime, timedelta, timezone

from app.workers.jobs import should_poll_now, FAR_MARKET_POLL_SECONDS

TODAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
HORIZON = 3


def _poll(event_date, last_ts):
    return should_poll_now(event_date, TODAY, HORIZON, last_ts, NOW)


# ── Inside the trading horizon: never throttled ───────────────────────────────

def test_same_day_always_polls():
    assert _poll(TODAY, NOW - timedelta(seconds=10)) is True


def test_within_horizon_always_polls_even_if_just_polled():
    """A market we may trade today must keep full 5-minute resolution."""
    for d in range(0, HORIZON + 1):
        assert _poll(TODAY + timedelta(days=d), NOW - timedelta(seconds=5)) is True, (
            f"day +{d} is inside the horizon and must not be throttled"
        )


def test_past_event_date_still_polls():
    """Negative days-ahead (event today/earlier, market not yet resolved)."""
    assert _poll(TODAY - timedelta(days=1), NOW - timedelta(seconds=5)) is True


# ── Beyond the horizon: throttled, but never starved ──────────────────────────

def test_far_market_skipped_when_recently_polled():
    assert _poll(TODAY + timedelta(days=7), NOW - timedelta(minutes=5)) is False


def test_far_market_polls_once_interval_elapsed():
    stale = NOW - timedelta(seconds=FAR_MARKET_POLL_SECONDS + 1)
    assert _poll(TODAY + timedelta(days=7), stale) is True


def test_far_market_with_no_history_is_seeded():
    """A market we have never priced must be polled regardless of distance."""
    assert _poll(TODAY + timedelta(days=10), None) is True


def test_just_outside_horizon_is_throttled():
    assert _poll(TODAY + timedelta(days=HORIZON + 1), NOW - timedelta(minutes=1)) is False


# ── Safety ────────────────────────────────────────────────────────────────────

def test_unknown_event_date_never_skipped():
    """If we cannot tell how far out a market is, poll it — never guess."""
    assert _poll(None, NOW - timedelta(seconds=1)) is True


def test_far_interval_is_sane():
    assert 300 <= FAR_MARKET_POLL_SECONDS <= 7200
