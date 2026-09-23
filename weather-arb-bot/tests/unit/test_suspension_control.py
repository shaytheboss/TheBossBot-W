"""Auto-suspension: a master switch, tunable thresholds, and the maths that
explains why the defaults fire so often.

The complaint driving this: cities go into suspension constantly. The cause is
not a bug, it is the window size. Over 10 trades the standard error on a win
rate near 0.78 is about 13 percentage points, so a 0.65 minimum sits roughly
ONE standard error below a city performing exactly as expected — and the rule
is re-evaluated every time a trade settles.

`_chronic_false_alarm_pct` puts a number on that (15.9% per check at the
defaults) so the choice of window is made from arithmetic rather than feel.
"""
from __future__ import annotations

import pytest

from app.analyzers.opportunity_detector import _suspension_verdict
from app.api.admin import _chronic_false_alarm_pct as false_alarm
from app.utils.settings_store import PERSISTABLE_KEYS


# ── Why it fires so often ─────────────────────────────────────────────────

class TestFalseAlarmMaths:
    def test_the_defaults_fire_on_a_healthy_city_about_one_check_in_six(self):
        """78% is the fleet's realised rate at >=90% stated confidence — the
        bot is chronically overconfident. A city performing exactly at that
        average still trips a 65%-over-10 rule ~16% of the time."""
        assert false_alarm(0.78, 10, 0.65) == pytest.approx(15.9, abs=0.3)

    def test_widening_the_window_is_what_fixes_it(self):
        """The fix is more evidence, not a looser bar. Same 65% threshold."""
        assert false_alarm(0.78, 20, 0.65) < 6.0
        assert false_alarm(0.78, 30, 0.65) < 6.0

    def test_loosening_the_rate_also_works_but_lets_real_losers_through(self):
        assert false_alarm(0.78, 10, 0.50) < 2.0

    def test_a_genuinely_good_city_is_rarely_touched(self):
        assert false_alarm(0.90, 10, 0.65) < 2.0

    def test_a_genuinely_bad_city_is_caught(self):
        """The rule must still work. At a true 50% win rate it should fire on
        most checks — that city loses money at any realistic entry price."""
        assert false_alarm(0.50, 10, 0.65) > 60.0

    def test_it_declines_to_answer_without_inputs(self):
        assert false_alarm(None, 10, 0.65) is None
        assert false_alarm(0.78, 0, 0.65) is None
        assert false_alarm(0.78, 10, 0.0) is None

    def test_an_exact_boundary_is_not_below_the_minimum(self):
        """`rate < min_win_rate` is strict: 13/20 = 65% must NOT trip, so the
        probability must exclude it."""
        assert _suspension_verdict(["win"] * 13 + ["loss"] * 7, 0, 20, 0.65) is None
        strict = false_alarm(0.65, 20, 0.65)
        assert strict < 50.0, "at exactly the threshold, under half of draws trip"


# ── The master switch ─────────────────────────────────────────────────────

class _City:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "Austin")
        self.suspended_until = kw.get("suspended_until")
        self.suspension_reason = kw.get("suspension_reason")


class _DB:
    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.commits = 0
        self.queried = False

    async def execute(self, *a, **kw):
        self.queried = True
        rows = [type("R", (), {"virtual_status": s})() for s in self.statuses]
        return type("Res", (), {"all": lambda _self: rows})()

    async def commit(self):
        self.commits += 1


class TestMasterSwitch:
    @pytest.mark.asyncio
    async def test_off_means_no_new_suspension(self, monkeypatch):
        import app.analyzers.opportunity_detector as od
        monkeypatch.setattr(od.settings, "suspension_enabled", False)

        city = _City()
        db = _DB(["loss"] * 10)
        await od._auto_suspend_check(db, city)

        assert city.suspended_until is None
        assert db.queried is False, "must not even run the lookback query"

    @pytest.mark.asyncio
    async def test_on_still_suspends(self, monkeypatch):
        import app.analyzers.opportunity_detector as od
        monkeypatch.setattr(od.settings, "suspension_enabled", True)
        monkeypatch.setattr(od.settings, "suspension_consecutive_losses", 3)

        city = _City()
        await od._auto_suspend_check(_DB(["loss"] * 10), city)
        assert city.suspended_until is not None
        assert "consecutive" in city.suspension_reason

    @pytest.mark.asyncio
    async def test_switching_off_does_not_strand_a_suspended_city(self, monkeypatch):
        """The trap: if the expiry path sat behind the enabled check, turning
        the feature off would freeze every city that was suspended at that
        moment, with nothing left to release it."""
        from datetime import datetime, timedelta, timezone
        import app.analyzers.opportunity_detector as od
        monkeypatch.setattr(od.settings, "suspension_enabled", False)

        city = _City(
            suspended_until=datetime.now(timezone.utc) - timedelta(days=1),
            suspension_reason="Auto-suspended: 3 consecutive high-confidence losses",
        )
        db = _DB()
        await od._auto_suspend_check(db, city)

        assert city.suspended_until is None and city.suspension_reason is None
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_an_unexpired_suspension_is_left_alone_when_off(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        import app.analyzers.opportunity_detector as od
        monkeypatch.setattr(od.settings, "suspension_enabled", False)

        until = datetime.now(timezone.utc) + timedelta(days=3)
        city = _City(suspended_until=until)
        await od._auto_suspend_check(_DB(), city)
        assert city.suspended_until == until

    def test_it_defaults_to_on(self):
        """This change must not silently disable a live safety mechanism."""
        from app.config import settings
        assert type(settings).model_fields["suspension_enabled"].default is True


# ── Runtime tunability ────────────────────────────────────────────────────

class TestSettingsAreTunable:
    def test_every_suspension_knob_persists(self):
        for key in (
            "suspension_enabled",
            "suspension_consecutive_losses",
            "suspension_window_trades",
            "suspension_min_win_rate",
            "suspension_days",
        ):
            assert key in PERSISTABLE_KEYS, f"{key} must survive a restart"

    def test_secrets_are_still_refused(self):
        """Widening the whitelist must not widen it too far."""
        for key in ("database_url", "telegram_bot_token", "admin_password"):
            assert key not in PERSISTABLE_KEYS

    def test_the_api_accepts_and_reports_them(self):
        from app.api.admin import SettingsIn
        fields = SettingsIn.model_fields
        for key in ("suspension_enabled", "suspension_consecutive_losses",
                    "suspension_window_trades", "suspension_min_win_rate",
                    "suspension_days"):
            assert key in fields
            assert fields[key].default is None, "omitted fields must not overwrite"


# ── Validation ────────────────────────────────────────────────────────────

class TestValidation:
    @staticmethod
    async def _patch(**kw):
        from fastapi import HTTPException
        from app.api.admin import SettingsIn, admin_set_settings

        class _DB2:
            async def get(self, *a, **k): return None
            def add(self, *a): pass
            async def commit(self): pass

        try:
            return await admin_set_settings(SettingsIn(**kw), "tok", _DB2()), None
        except HTTPException as e:
            return None, e

    @pytest.mark.asyncio
    async def test_a_window_of_three_is_rejected(self):
        """1-4 trades is noise dressed up as a rule — the standard error is
        larger than any threshold you could set."""
        ok, err = await self._patch(suspension_window_trades=3)
        assert ok is None and err.status_code == 400

    @pytest.mark.asyncio
    async def test_zero_is_allowed_because_it_means_off(self):
        ok, err = await self._patch(suspension_window_trades=0)
        assert err is None

    @pytest.mark.asyncio
    async def test_a_sane_window_is_accepted(self, monkeypatch):
        ok, err = await self._patch(suspension_window_trades=30)
        assert err is None

    @pytest.mark.asyncio
    async def test_a_win_rate_above_one_is_rejected(self):
        ok, err = await self._patch(suspension_min_win_rate=1.4)
        assert ok is None and err.status_code == 400

    @pytest.mark.asyncio
    async def test_a_zero_day_suspension_is_rejected(self):
        ok, err = await self._patch(suspension_days=0)
        assert ok is None and err.status_code == 400

    @pytest.mark.asyncio
    async def test_a_negative_streak_is_rejected(self):
        ok, err = await self._patch(suspension_consecutive_losses=-1)
        assert ok is None and err.status_code == 400
