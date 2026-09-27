"""Daily peak times: the pure part — a day's readings → its peak; solar noon."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.peaks.compute import (
    COVER_FROM_HOUR, COVER_TO_HOUR, MIN_OBS, day_peak, group_by_local_day, solar_noon_hour,
)

D = date(2026, 7, 15)
TZ = ZoneInfo("Europe/Madrid")


def _day(temps_by_hour: dict[float, float], d=D, tz=TZ):
    return [(datetime(d.year, d.month, d.day, int(h), int((h % 1) * 60), tzinfo=tz), f)
            for h, f in sorted(temps_by_hour.items())]


def _hm(h: float) -> str:
    m = round(h * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


class TestDayPeak:
    def test_the_first_reading_at_the_max_is_the_peak(self):
        p = day_peak(D, _day({6: 70, 12: 85, 15: 90, 16: 90, 17: 88, 22: 75}))
        assert p.max_f == 90 and p.peak_hour == 15.0 and p.peak_last_hour == 16.0

    def test_a_spike_is_not_the_high(self):
        """Same rule as the aggregator: a top reading more than 5°F above the
        second-highest is a bad observation."""
        p = day_peak(D, _day({10: 80, 13: 97, 15: 88, 16: 87}))
        assert p.max_f == 88 and p.peak_hour == 15.0 and p.spike_dropped == 1

    def test_half_hourly_readings_keep_their_minutes(self):
        p = day_peak(D, _day({14.0: 88, 14.5: 90, 15.0: 89}))
        assert p.peak_hour == pytest.approx(14.5)

    def test_coverage_is_recorded(self):
        p = day_peak(D, _day({h: 70 + h for h in range(5, 22, 2)}))
        assert (p.first_obs_hour, p.last_obs_hour, p.max_gap_h, p.n_obs) == (5.0, 21.0, 2.0, 9)

    def test_a_full_day_is_complete(self):
        assert day_peak(D, _day({h: 70 for h in range(24)})).complete

    @pytest.mark.parametrize("hours,why", [
        (range(12, 24), "starts after the morning"),
        (range(0, 18), "ends before the evening"),
        ([h for h in range(24) if not 13 <= h <= 16], "a hole across the afternoon"),
        (range(0, 24, 3), "too few readings"),
    ])
    def test_a_day_with_holes_is_not_complete(self, hours, why):
        assert not day_peak(D, _day({h: 70 for h in hours})).complete, why

    def test_the_thresholds_are_the_documented_ones(self):
        assert (MIN_OBS, COVER_FROM_HOUR, COVER_TO_HOUR) == (12, 8.0, 20.0)

    def test_no_readings_is_none(self):
        assert day_peak(D, []) is None


class TestLocalDays:
    def test_readings_are_grouped_by_the_citys_own_date(self):
        """16:00 UTC on 1 July is 01:00 on 2 July in Tokyo."""
        utc = timezone.utc
        rows = [(datetime(2026, 7, 1, 5, tzinfo=utc), 80.0),
                (datetime(2026, 7, 1, 16, tzinfo=utc), 75.0)]
        days = group_by_local_day(rows, "Asia/Tokyo")
        assert sorted(days) == [date(2026, 7, 1), date(2026, 7, 2)]
        assert days[date(2026, 7, 2)][0][0].hour == 1

    def test_missing_temperatures_are_skipped(self):
        rows = [(datetime(2026, 7, 1, 12, tzinfo=timezone.utc), None)]
        assert group_by_local_day(rows, "UTC") == {}


class TestSolarNoon:
    """Reference values from the NOAA equation-of-time formula, worked by hand."""

    @pytest.mark.parametrize("lat,lon,tz,day,expected", [
        (51.51, 0.03, "Europe/London", date(2026, 11, 3), "11:44"),     # sun 16 min fast
        (40.45, -3.58, "Europe/Madrid", date(2026, 7, 15), "14:20"),    # CEST, west of the zone
        (40.45, -3.58, "Europe/Madrid", date(2026, 1, 15), "13:24"),    # CET in winter
        (35.55, 139.78, "Asia/Tokyo", date(2026, 2, 11), "11:55"),
    ])
    def test_reference_values(self, lat, lon, tz, day, expected):
        got = solar_noon_hour(lat, lon, tz, day)
        want_h, want_m = map(int, expected.split(":"))
        assert abs(got * 60 - (want_h * 60 + want_m)) <= 2, _hm(got)

    def test_the_spread_between_cities_that_motivated_this(self):
        """In summer, solar noon is ~2.5 h later on Madrid's clock than on Tokyo's."""
        d = date(2026, 7, 15)
        madrid = solar_noon_hour(40.45, -3.58, "Europe/Madrid", d)
        tokyo = solar_noon_hour(35.55, 139.78, "Asia/Tokyo", d)
        assert 2.3 <= madrid - tokyo <= 2.7

    def test_dst_moves_it_by_an_hour(self):
        a = solar_noon_hour(40.7, -73.9, "America/New_York", date(2026, 3, 7))
        b = solar_noon_hour(40.7, -73.9, "America/New_York", date(2026, 3, 9))
        assert b - a == pytest.approx(1.0, abs=0.02)

    def test_southern_hemisphere(self):
        """Wellington: NZDT from late September — solar noon ~13:05 in late October."""
        got = solar_noon_hour(-41.33, 174.8, "Pacific/Auckland", date(2026, 10, 25))
        assert 12.9 < got < 13.5, _hm(got)
