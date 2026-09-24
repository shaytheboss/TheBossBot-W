"""Shadow study: the per-city clock and the summary logic. Pure functions.

The clock is the reason the study records hourly instead of once a day: a
fixed UTC hour sits at a different point of every city's day, so the rows are
tagged with the city's own position instead — and these tests pin that the
tags mean the same thing everywhere.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.shadow.clock import hour_floor, hours_to_close, local_close_utc, local_hour
from app.shadow.report import (
    MAX_TABLE_ROWS, TELEGRAM_LIMIT, Row, build_hourly_digest, build_summary,
    by_hour, knew_from,
)
from app.shadow.snapshot import DEAD_MARKET_P, DEAD_MODEL_P, is_dead

UTC = timezone.utc


# ── The city clock ────────────────────────────────────────────────────────

class TestCityClock:
    def test_the_same_utc_moment_is_a_different_point_in_each_citys_day(self):
        """The whole reason for tagging rows: at one UTC instant Tokyo's day is
        almost over while New York's has barely begun."""
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        tokyo = hours_to_close(now, date(2026, 9, 24), "Asia/Tokyo")
        nyc = hours_to_close(now, date(2026, 9, 24), "America/New_York")
        assert tokyo == pytest.approx(3.0)      # closes 15:00 UTC
        assert nyc == pytest.approx(16.0)       # closes 04:00 UTC next day
        assert local_hour(now, "Asia/Tokyo") == 21
        assert local_hour(now, "America/New_York") == 8

    def test_close_is_the_end_of_the_local_day(self):
        # Chicago is UTC-5 in September (CDT).
        assert local_close_utc(date(2026, 9, 24), "America/Chicago") == \
            datetime(2026, 9, 25, 5, 0, tzinfo=UTC)

    def test_dst_ending_on_the_event_day_is_handled(self):
        """1 Nov 2026 is 25 hours long in New York. Arithmetic on offsets gets
        this wrong; building from the local wall clock does not."""
        assert local_close_utc(date(2026, 11, 1), "America/New_York") == \
            datetime(2026, 11, 2, 5, 0, tzinfo=UTC)

    def test_dst_starting_on_the_event_day_is_handled(self):
        assert local_close_utc(date(2026, 3, 8), "America/New_York") == \
            datetime(2026, 3, 9, 4, 0, tzinfo=UTC)

    def test_a_bad_timezone_falls_back_to_utc_instead_of_failing_the_run(self):
        assert local_close_utc(date(2026, 9, 24), "Not/AZone") == \
            datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
        assert local_hour(datetime(2026, 9, 24, 7, tzinfo=UTC), None) == 7

    def test_hours_to_close_goes_negative_after_the_day_ends(self):
        now = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
        assert hours_to_close(now, date(2026, 9, 24), "UTC") == pytest.approx(-24.0)

    def test_hour_floor_makes_a_run_idempotent_within_the_hour(self):
        a = hour_floor(datetime(2026, 9, 24, 14, 5, 33, 12, tzinfo=UTC))
        b = hour_floor(datetime(2026, 9, 24, 14, 59, 59, tzinfo=UTC))
        assert a == b == datetime(2026, 9, 24, 14, 0, tzinfo=UTC)


# ── What is worth storing ─────────────────────────────────────────────────

class TestDeadBuckets:
    def test_both_sides_calling_it_dead_is_skipped(self):
        assert is_dead(0.001, 0.01)

    def test_the_model_alone_calling_it_dead_is_kept(self):
        """If the market still prices it, the disagreement is exactly the data."""
        assert not is_dead(0.001, 0.20)

    def test_the_market_alone_calling_it_dead_is_kept(self):
        assert not is_dead(0.30, 0.01)

    def test_an_unpriced_bucket_the_model_dismisses_is_skipped(self):
        assert is_dead(0.001, None)

    def test_thresholds_stay_conservative(self):
        """Loosening these would drop buckets that one side takes seriously."""
        assert DEAD_MODEL_P <= 0.02 and DEAD_MARKET_P <= 0.03


# ── The summary's logic ───────────────────────────────────────────────────

T0 = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
A, B, C = 1, 2, 3
LABELS = {A: "91-92°F", B: "93-94°F", C: "95-96°F"}


def _hour(i, probs):
    """probs: {outcome: (model_p, market_p)} at hour i, 24 hours before close."""
    return [Row(T0 + timedelta(hours=i), 24.0 - i, i % 24, oid, m, k)
            for oid, (m, k) in probs.items()]


class TestWhoKnewFirst:
    def test_the_side_that_settled_earlier_is_credited(self):
        rows = (
            _hour(0, {A: (.5, .6), C: (.3, .2)})
            + _hour(1, {A: (.2, .6), C: (.7, .2)})     # model moves to C
            + _hour(2, {A: (.1, .3), C: (.8, .6)})     # market follows
            + _hour(3, {A: (.1, .1), C: (.9, .9)})
        )
        hours = by_hour(rows, C)
        assert knew_from(hours, C, "model_pick") == pytest.approx(23.0)
        assert knew_from(hours, C, "market_pick") == pytest.approx(22.0)

    def test_a_flicker_onto_the_winner_does_not_count(self):
        """Being right two days out and then leaving it is not knowing."""
        rows = (_hour(0, {A: (.2, .5), C: (.8, .5)})
                + _hour(1, {A: (.8, .5), C: (.2, .5)})
                + _hour(2, {A: (.1, .5), C: (.9, .5)}))
        hours = by_hour(rows, C)
        assert knew_from(hours, C, "model_pick") == pytest.approx(22.0)

    def test_never_settling_on_the_winner_is_none(self):
        rows = _hour(0, {A: (.9, .9), C: (.1, .1)})
        assert knew_from(by_hour(rows, C), C, "model_pick") is None


class TestScoring:
    def test_a_winner_both_sides_dismissed_is_charged_in_full(self):
        """A missing row means both called it dead. If it won, that is a total
        miss for both, not a free pass."""
        hours = by_hour(_hour(0, {A: (.9, .8)}), C)
        assert hours[0].model_win == 0.0
        assert hours[0].market_win == 0.0
        assert hours[0].model_brier == pytest.approx(0.9 ** 2 + 1.0)

    def test_a_perfect_forecast_scores_zero(self):
        hours = by_hour(_hour(0, {A: (0.0, 0.0), C: (1.0, 1.0)}), C)
        assert hours[0].model_brier == pytest.approx(0.0)
        assert hours[0].market_brier == pytest.approx(0.0)

    def test_an_unpriced_hour_gives_no_market_score(self):
        """Scoring the market on buckets it never priced would invent a
        number for it."""
        hours = by_hour(_hour(0, {A: (.4, None), C: (.6, .5)}), C)
        assert hours[0].market_brier is None


class TestSummaryMessage:
    def _rows(self, n):
        out = []
        for i in range(n):
            out += _hour(i, {A: (.3, .4), C: (.6 + i * 0.001, .5)})
        return out

    def test_it_names_the_city_the_winner_and_the_verdicts(self):
        text = build_summary(city="Austin", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=self._rows(5))
        assert "Austin" in text and "95-96°F" in text
        assert "Who knew first" in text and "Brier" in text

    def test_nothing_to_report_is_none(self):
        assert build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=[]) is None

    def test_long_histories_are_sampled_but_keep_the_final_hours(self):
        text = build_summary(city="Austin", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=self._rows(72))
        table = text.split("<pre>")[1].split("</pre>")[0].strip().splitlines()[2:]
        assert len(table) <= MAX_TABLE_ROWS
        # 72 hourly rows from 24h-to-close down to -47h. The sample must keep
        # the very first hour and the very last — the decisive end is what
        # the summary is for.
        assert table[0].split()[0] == "24.0"
        assert table[-1].split()[0] == "-47.0"

    def test_user_text_is_escaped(self):
        """Bucket labels come from Polymarket. A stray '<' would make Telegram
        reject the whole HTML message."""
        labels = {**LABELS, C: "<95 & up"}
        text = build_summary(city="A<B", event_date=date(2026, 9, 24),
                             labels=labels, winner_id=C, rows=self._rows(3))
        assert "&lt;95 &amp; up" in text and "A&lt;B" in text

    def test_it_never_exceeds_telegrams_limit_and_never_leaves_pre_open(self):
        labels = {A: "x" * 300, C: "y" * 300}
        rows = []
        for i in range(200):
            rows += _hour(i, {A: (.3, .4), C: (.6, .5)})
        text = build_summary(city="Z" * 200, event_date=date(2026, 9, 24),
                             labels=labels, winner_id=C, rows=rows)
        assert len(text) <= TELEGRAM_LIMIT
        assert text.count("<pre>") == text.count("</pre>")


class TestHourlyDigest:
    def test_it_is_one_message_ranked_by_the_size_of_the_gap(self):
        gaps = [("Austin", "95-96°F", 20, .60, .40),
                ("Paris", "70-71°F", 30, .50, .48),
                ("Tokyo", "80-81°F", 5, .10, .70)]
        text = build_hourly_digest(taken_at=T0, markets_tracked=3, gaps=gaps)
        assert text.index("Tokyo") < text.index("Austin") < text.index("Paris")

    def test_it_caps_the_list(self):
        gaps = [(f"C{i}", "1-2°F", 10, .5, .1) for i in range(30)]
        text = build_hourly_digest(taken_at=T0, markets_tracked=30, gaps=gaps, top=8)
        assert sum(1 for i in range(30) if f"C{i} " in text) == 8

    def test_an_empty_hour_still_says_so(self):
        assert "No priced buckets" in build_hourly_digest(
            taken_at=T0, markets_tracked=0, gaps=[])


def test_a_single_snapshot_reads_naturally():
    rows = _hour(0, {A: (.3, .4), C: (.6, .5)})
    text = build_summary(city="Austin", event_date=date(2026, 9, 24),
                         labels=LABELS, winner_id=C, rows=rows)
    assert "(1 snapshot)" in text and "1 snapshots" not in text
