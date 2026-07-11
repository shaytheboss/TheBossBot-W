"""Tests for the July-11 bot fixes:

1. Tomorrow.io budget-aware city selection (rate-limit fix)
2. Chronic-loser auto-suspend verdict (KL archetype)
3. Settings persistence whitelist
4. Config defaults: alert_near raised to 0.80, beta buy gate exists
"""
import pytest

from app.workers.tomorrowio_job import select_cities_for_budget, TOMORROWIO_FORECAST_DAYS
from app.analyzers.opportunity_detector import _suspension_verdict
from app.utils.settings_store import PERSISTABLE_KEYS, save_setting_override
from app.config import settings


# ── 1. Tomorrow.io budget selection ────────────────────────────────────────────

class TestTomorrowioBudget:
    def test_market_cities_come_first(self):
        picked, _ = select_cities_for_budget(
            market_city_ids=[7, 3],
            all_city_ids=[1, 2, 3, 4, 5, 6, 7],
            cursor=0,
            budget_requests=12,       # 12 // 3 = 4 cities
            requests_per_city=3,
        )
        assert picked[:2] == [7, 3], "open-market cities must be fetched first"
        assert len(picked) == 4

    def test_budget_respected(self):
        picked, _ = select_cities_for_budget(
            market_city_ids=list(range(100, 150)),
            all_city_ids=list(range(100, 150)),
            cursor=0,
            budget_requests=20,
            requests_per_city=3,
        )
        # 20 // 3 = 6 cities max → 18 requests ≤ 20
        assert len(picked) == 6
        assert len(picked) * 3 <= 20

    def test_rotation_advances(self):
        """Non-market cities rotate across runs so everyone gets refreshed."""
        all_ids = [1, 2, 3, 4, 5, 6]
        cursor = 0
        seen = set()
        for _ in range(3):
            picked, cursor = select_cities_for_budget(
                market_city_ids=[],
                all_city_ids=all_ids,
                cursor=cursor,
                budget_requests=6,   # 2 cities per run
                requests_per_city=3,
            )
            seen.update(picked)
        assert seen == set(all_ids), f"rotation must cover all cities, got {seen}"

    def test_no_duplicates_when_market_city_in_all(self):
        picked, _ = select_cities_for_budget(
            market_city_ids=[1, 2],
            all_city_ids=[1, 2, 3],
            cursor=0,
            budget_requests=30,
            requests_per_city=3,
        )
        assert sorted(picked) == [1, 2, 3]
        assert len(picked) == len(set(picked))

    def test_zero_budget_returns_empty(self):
        picked, cur = select_cities_for_budget([1], [1, 2], 0, 0, 3)
        assert picked == [] and cur == 0

    def test_zero_requests_per_city_returns_empty(self):
        picked, _ = select_cities_for_budget([1], [1, 2], 0, 20, 0)
        assert picked == []

    def test_budget_smaller_than_one_city(self):
        picked, _ = select_cities_for_budget([1], [1, 2], 0, 2, 3)
        assert picked == []

    def test_forecast_days_constant(self):
        """Budget math in main.py assumes 3 requests per city."""
        assert TOMORROWIO_FORECAST_DAYS == 3

    def test_daily_budget_inside_free_tier(self):
        """Default config must stay inside 25 req/h and 500 req/day."""
        per_run = settings.tomorrowio_requests_per_run
        interval = settings.tomorrowio_fetch_interval
        runs_per_day = 86400 // interval
        assert per_run <= 25, "hourly cap"
        assert per_run * runs_per_day <= 500, "daily cap"


# ── 2. Chronic-loser suspension verdict ────────────────────────────────────────

class TestSuspensionVerdict:
    STREAK = 3
    WINDOW = 10
    MIN_RATE = 0.65

    def _v(self, statuses):
        return _suspension_verdict(statuses, self.STREAK, self.WINDOW, self.MIN_RATE)

    def test_streak_fires(self):
        assert self._v(["loss", "loss", "loss", "win"]) is not None

    def test_streak_not_fired_on_two(self):
        assert self._v(["loss", "loss", "win", "loss"]) is None

    def test_chronic_loser_kl_archetype(self):
        """56% win rate alternating W/L — streak never fires, chronic must."""
        statuses = ["win", "loss", "win", "loss", "loss", "win", "loss",
                    "win", "loss", "win"]  # 5/10 = 50% < 65%
        reason = self._v(statuses)
        assert reason is not None
        assert "chronic" in reason.lower()

    def test_healthy_city_not_suspended(self):
        statuses = ["win"] * 8 + ["loss"] * 2   # 80% > 65%
        assert self._v(statuses) is None

    def test_exactly_at_min_rate_not_suspended(self):
        """7/10 = 70% >= 65% → no suspension (rule is strictly below)."""
        statuses = (["win"] * 7 + ["loss"] * 3)
        assert self._v(statuses) is None

    def test_window_needs_full_sample(self):
        """Fewer than `window` settled trades → chronic rule stays quiet."""
        statuses = ["loss", "win", "loss", "win", "loss"]  # only 5 < 10
        assert self._v(statuses) is None

    def test_disabled_window_only_streak_applies(self):
        statuses = ["win", "loss"] * 5   # 50%
        assert _suspension_verdict(statuses, 3, 0, 0.65) is None

    def test_disabled_streak_chronic_still_fires(self):
        statuses = ["win", "loss"] * 5   # 50% < 65%
        assert _suspension_verdict(statuses, 0, 10, 0.65) is not None

    def test_both_disabled(self):
        assert _suspension_verdict(["loss"] * 20, 0, 0, 0.0) is None

    def test_empty_history(self):
        assert self._v([]) is None

    def test_streak_takes_priority_in_reason(self):
        reason = self._v(["loss"] * 10)
        assert "consecutive" in reason


# ── 3. Settings persistence whitelist ──────────────────────────────────────────

class TestSettingsStore:
    @pytest.mark.asyncio
    async def test_non_whitelisted_key_refused_without_db(self):
        """Refusal must short-circuit before any DB access (db=None proves it)."""
        ok = await save_setting_override(None, "database_url", "hacked")
        assert ok is False

    @pytest.mark.asyncio
    async def test_api_keys_not_persistable(self):
        for key in ("telegram_bot_token", "admin_password", "tomorrowio_api_key"):
            assert key not in PERSISTABLE_KEYS
            assert await save_setting_override(None, key, "x") is False

    def test_all_threshold_keys_whitelisted(self):
        for key in (
            "min_confidence_for_alert", "min_edge_for_alert",
            "max_days_ahead_for_alert",
            "min_confidence_alert_near", "min_confidence_alert_far",
            "min_confidence_buy_near", "min_confidence_buy_far",
            "min_confidence_beta_alert", "min_confidence_beta_buy",
        ):
            assert key in PERSISTABLE_KEYS, f"{key} must be persistable"


# ── 4. Config defaults ─────────────────────────────────────────────────────────

class TestConfigDefaults:
    def test_alert_near_default_is_080(self):
        """The 0.75 default caused sub-80% alerts the user never asked for."""
        assert type(settings).model_fields["min_confidence_alert_near"].default == pytest.approx(0.80)

    def test_beta_buy_gate_exists_and_below_alpha_buy(self):
        beta_buy = type(settings).model_fields["min_confidence_beta_buy"].default
        alpha_buy = type(settings).model_fields["min_confidence_buy_near"].default
        assert 0.75 <= beta_buy < alpha_buy, (
            "beta virtual-buy gate must sit below alpha's so beta keeps trading"
        )

    def test_chronic_suspend_defaults_enabled(self):
        assert type(settings).model_fields["suspension_window_trades"].default == 10
        assert type(settings).model_fields["suspension_min_win_rate"].default == pytest.approx(0.65)
