"""The helpers the intraday detector and the shadow study share.

They were lifted out of _evaluate_intraday_outcome unchanged. Two of their
rules had no test at all before — a city must never boost itself, and a
running max counts as confirmed only when Wunderground set it — so a change
to either would have passed the whole suite.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import app.intraday.detector as idet
from app.intraday.detector import apply_cluster_boost, cluster_boost_for, observed_readings

NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)


def _signals(metar_max, wu_high=None, wu_age_min=10, temp=None):
    s = {"metar_today_max_f": metar_max, "primary_metar": {"temperature_f": temp}}
    if wu_high is not None:
        s["wunderground_forecast"] = {
            "predicted_high_f": wu_high,
            "retrieved_at": (NOW - timedelta(minutes=wu_age_min)).isoformat(),
        }
    return s


class TestObservedReadings:
    def test_no_metar_max_means_nothing_to_estimate_from(self):
        assert observed_readings({"metar_today_max_f": None}, NOW) is None

    def test_a_wunderground_max_above_metar_is_confirmed(self):
        r = observed_readings(_signals(95.9, 96.0), NOW)
        assert r["running_max"] == 96.0 and r["wu_confirmed"] is True

    def test_a_metar_max_above_wunderground_is_not_confirmed(self):
        """Polymarket resolves on WU; a METAR reading above it can be a
        station difference, so it must not produce a hard lock."""
        r = observed_readings(_signals(96.1, 96.0), NOW)
        assert r["running_max"] == 96.1 and r["wu_confirmed"] is False

    def test_a_stale_wunderground_reading_is_ignored(self):
        r = observed_readings(_signals(95.0, 97.0, wu_age_min=24 * 60), NOW)
        assert r["running_max"] == 95.0 and r["wu_confirmed"] is False

    def test_the_age_is_measured_from_the_callers_clock(self):
        """The shadow study passes its own `now`; the detector passes the wall
        clock. Same signals, different clocks, different freshness."""
        fresh = observed_readings(_signals(95.0, 97.0, wu_age_min=10), NOW)
        later = observed_readings(_signals(95.0, 97.0, wu_age_min=10), NOW + timedelta(days=1))
        assert fresh["wu_confirmed"] is True and later["wu_confirmed"] is False

    def test_current_temperature_is_passed_through(self):
        assert observed_readings(_signals(95.0, temp="91.4"), NOW)["current_temp"] == 91.4


class TestClusterBoost:
    @pytest.fixture(autouse=True)
    def _clean(self):
        idet._cluster_warmth_today.clear()
        yield
        idet._cluster_warmth_today.clear()

    def test_a_sister_city_running_warm_boosts_the_others(self):
        idet._cluster_warmth_today["europe"] = (date.today(), 4.0, "Paris")
        boost, note = cluster_boost_for("London")
        assert boost > 0 and "Paris" in note

    def test_a_city_never_boosts_itself(self):
        idet._cluster_warmth_today["europe"] = (date.today(), 4.0, "Paris")
        assert cluster_boost_for("Paris") == (0.0, "")

    def test_yesterdays_warm_up_has_expired(self):
        idet._cluster_warmth_today["europe"] = (date.today() - timedelta(days=1), 4.0, "Paris")
        assert cluster_boost_for("London") == (0.0, "")

    def test_a_city_in_no_cluster_gets_nothing(self):
        idet._cluster_warmth_today["europe"] = (date.today(), 4.0, "Paris")
        assert cluster_boost_for("Wellington") == (0.0, "")

    def test_reading_the_boost_changes_no_state(self):
        idet._cluster_warmth_today["europe"] = (date.today(), 4.0, "Paris")
        before = dict(idet._cluster_warmth_today)
        cluster_boost_for("London")
        assert idet._cluster_warmth_today == before

    def test_applying_it_leaves_the_original_bias_dict_alone(self):
        """The study boosts a shallow copy of shared signals; the dict it
        copied from must not change."""
        bias = {"bias_f": 1.5, "notes": "learned"}
        base = {"station_bias": bias}
        copy = dict(base)
        apply_cluster_boost(copy, 2.0, "test")
        assert copy["station_bias"]["bias_f"] == 3.5
        assert base["station_bias"] is bias and bias["bias_f"] == 1.5
