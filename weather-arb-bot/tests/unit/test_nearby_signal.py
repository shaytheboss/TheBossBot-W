"""The nearby-station signal's pure parts: station state, the rule, the bucket."""
from datetime import datetime, timedelta, timezone

import pytest

from app.nearby import signal as sig
from app.nearby.paper import book_summary

NOW = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)


def _reads(*temps, end=NOW, step=30):
    return [(end - timedelta(minutes=step * (len(temps) - 1 - i)), t) for i, t in enumerate(temps)]


class TestStationState:
    def test_high_and_now(self):
        s = sig.station_state("KX", _reads(60, 64, 66, 65), NOW)
        assert (s.high_f, s.now_f, s.below_f) == (66, 65, 1)

    def test_too_few_or_stale_is_none(self):
        assert sig.station_state("KX", _reads(60, 61), NOW) is None
        assert sig.station_state("KX", _reads(60, 61, 62, end=NOW - timedelta(minutes=81)), NOW) is None

    def test_future_and_missing_readings_are_ignored(self):
        r = _reads(60, None, 62, 63) + [(NOW + timedelta(minutes=5), 99.0)]
        assert sig.station_state("KX", r, NOW).high_f == 63


class TestRule:
    T = sig.StationState("KT", 70.0, 71.0, NOW)

    def _nb(self, *belows):
        return [sig.StationState(f"N{i}", 70.0 - b, 70.0, NOW) for i, b in enumerate(belows)]

    def test_fires_at_sixty_percent_falling(self):
        v = sig.evaluate(self.T, self._nb(1.0, 1.5, 2.0, 0.0, 0.5))
        assert len(v.falling) == 3 and v.share == pytest.approx(0.6) and v.fires

    def test_below_the_share_it_does_not(self):
        assert not sig.evaluate(self.T, self._nb(1.0, 0.9, 0.0)).fires

    def test_one_neighbour_is_never_enough(self):
        assert not sig.evaluate(self.T, self._nb(3.0)).fires

    def test_missing_neighbours_do_not_count(self):
        v = sig.evaluate(self.T, self._nb(2.0, 2.0) + [None, None])
        assert len(v.nearby) == 2 and v.fires

    def test_no_target_no_verdict(self):
        assert sig.evaluate(None, self._nb(2, 2, 2)) is None


class TestBucket:
    @pytest.mark.parametrize("temp_f,lo,hi,unit,ok", [
        (70.4, 70, 71, "F", True), (71.6, 70, 71, "F", False), (69.6, 70, 71, "F", True),
        (62.6, 17, 17, "C", True),      # 17°C exactly
        (63.5, 17, 17, "C", False),     # 17.5°C rounds up to 18: not the 17°C bucket
        (89.6, 32, None, "C", True),    # "32°C or higher": 32.0°C
        (50.0, None, 10, "C", True),    # "10°C or below": 10.0°C
    ])
    def test_holds_rounds_the_reading(self, temp_f, lo, hi, unit, ok):
        assert sig.holds(lo, hi, unit, temp_f) is ok

    def test_mid_band(self):
        assert sig.mid_in_band(0.30) and sig.mid_in_band(0.85)
        assert not sig.mid_in_band(0.29) and not sig.mid_in_band(0.86) and not sig.mid_in_band(None)


class TestBook:
    def test_best_prices_and_size_at_the_ask(self):
        b = book_summary({"bids": [{"price": "0.58", "size": "10"}, {"price": "0.55", "size": "5"}],
                          "asks": [{"price": "0.62", "size": "40"}, {"price": "0.62", "size": "10"},
                                   {"price": "0.65", "size": "99"}]})
        assert b == {"bid": 0.58, "ask": 0.62, "mid": pytest.approx(0.60), "ask_size": 50}

    @pytest.mark.parametrize("book", [None, {}, {"bids": [], "asks": [{"price": "0.6", "size": "1"}]},
                                      {"bids": [{"price": "0.7", "size": "1"}], "asks": [{"price": "0.6", "size": "1"}]},
                                      {"bids": [{"price": "x"}], "asks": [{"price": "0.6"}]}])
    def test_one_sided_crossed_or_malformed_is_none(self, book):
        assert book_summary(book) is None
