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
    CHECKPOINTS, TELEGRAM_LIMIT, Row, build_hourly_digest, build_summary,
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
        assert "Settled on the winner" in text and "Brier" in text

    def test_nothing_to_report_is_none(self):
        assert build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=[]) is None

    def test_a_long_history_is_a_short_grid(self):
        """72 hourly snapshots become one row per side, six checkpoints wide —
        the old one-row-per-snapshot table could not be read."""
        rows = [Row(r.taken_at, r.hours_to_close + 48, r.local_hour, r.outcome_id,
                    r.model_p, r.market_p) for r in self._rows(72)]
        text = build_summary(city="Austin", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=rows)
        grid = text.split("<pre>")[1].split("</pre>")[0].strip().splitlines()
        assert [ln.split()[0] for ln in grid] == ["hours", "daily", "market"]
        assert grid[0].split()[2:] == [f"{c}h" for c in CHECKPOINTS]

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


class TestOnlyTheHoursBeforeTheClose:
    """Guangzhou, 24 Sep: the model picked the winner (35C) from 11h before
    the close, the market from 8h. Seven snapshots taken after the close —
    the model already looking at the next day, the market pinned at 100% —
    turned that into "model never settled, market led by 8h"."""

    W, L = 1, 2                      # 35C (won), 34C
    LABELS = {1: "35°C", 2: "34°C", 3: "36°C"}

    def _rows(self):
        rows = []
        for i in range(11):                        # 10.9h … 0.9h before close
            htc = 10.9 - i
            market_on_winner = i >= 3              # market switches with 7.9h left
            rows += [Row(T0 + timedelta(hours=i), htc, (13 + i) % 24, self.W, .19,
                         .96 if market_on_winner else .30),
                     Row(T0 + timedelta(hours=i), htc, (13 + i) % 24, self.L, .15,
                         .04 if market_on_winner else .60)]
        for j in range(7):                         # after the close
            t = T0 + timedelta(hours=11 + j)
            rows += [Row(t, -0.1 - j, j, self.W, .16, 1.0),
                     Row(t, -0.1 - j, j, self.L, .30, 0.0)]
        return rows

    def _text(self, rows):
        return build_summary(city="Guangzhou", event_date=date(2026, 9, 24),
                             labels=self.LABELS, winner_id=self.W, rows=rows)

    def test_who_knew_first_is_decided_before_the_close(self):
        text = self._text(self._rows())
        assert "daily 11h before close · market 8h before close" in text
        assert "never" not in text

    def test_the_after_close_rows_are_left_out_of_the_table_and_the_scores(self):
        with_after = self._text(self._rows())
        before_only = self._text([r for r in self._rows() if r.hours_to_close > 0])
        assert with_after.replace(" · 7 after close left out", "") == before_only
        assert "7 after close left out" in with_after
        assert "100%" not in with_after.split("<pre>")[1], "post-close prices are not shown"

    def test_a_market_seen_only_after_its_close_has_nothing_to_report(self):
        rows = [r for r in self._rows() if r.hours_to_close <= 0]
        assert self._text(rows) is None


class TestIntradayInTheSummary:
    """Guangzhou again, now with the intraday model from 13:00 local (7.9h
    before close): it locks onto the winner as soon as the thermometer does."""

    W, L = 1, 2
    LABELS = {1: "35°C", 2: "34°C", 3: "36°C"}

    def _rows(self, with_intraday=True):
        rows = []
        for i in range(11):                         # 10.9h … 0.9h before close
            htc = 10.9 - i
            t = T0 + timedelta(hours=i)
            intra_on = with_intraday and i >= 3     # intraday hours begin
            iw = (.97 if i >= 4 else .40) if intra_on else None
            il = (.02 if i >= 4 else .55) if intra_on else None
            mkt_w = .96 if i >= 5 else .30
            rows += [Row(t, htc, (13 + i) % 24, self.W, .19, mkt_w, iw),
                     Row(t, htc, (13 + i) % 24, self.L, .15, 1 - mkt_w, il)]
        return rows

    def _text(self, rows):
        return build_summary(city="Guangzhou", event_date=date(2026, 9, 24),
                             labels=self.LABELS, winner_id=self.W, rows=rows)

    def test_the_grid_has_an_intraday_row(self):
        text = self._text(self._rows())
        grid = text.split("<pre>")[1].split("</pre>")[0].strip().splitlines()
        intra = next(ln for ln in grid if ln.startswith("intraday")).split()[1:]
        # checkpoints 48 24 12 6 3 1 → tracked from 10.9h; intraday from 7.9h
        assert intra == ["-", "-", "-", "97%", "97%", "97%"]

    def test_who_knew_first_includes_the_intraday_model(self):
        text = self._text(self._rows())
        assert "intraday 7h before close" in text      # 6.9h, rounded
        assert "market 6h before close" in text        # 5.9h

    def test_the_intraday_model_is_scored_on_its_own_hours_only(self):
        rows = self._rows()
        text = self._text(rows)
        assert "intraday hours only (8h)" in text
        hours = [h for h in by_hour(rows, self.W) if h.intraday_pick is not None]
        mkt = sum(h.market_brier for h in hours) / len(hours)
        assert f"market {mkt:.3f}" in text.split("intraday hours only")[1]

    def test_without_intraday_rows_the_summary_reads_as_before(self):
        text = self._text(self._rows(with_intraday=False))
        assert "intraday" not in text


class TestGridLayout:
    """The first table drifted out of line in Telegram and ran to 24 rows.
    The grid has fixed-width cells, nothing variable-length inside it, and
    one row per side."""

    def _grid(self, rows, labels=LABELS):
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=labels, winner_id=C, rows=rows)
        return text.split("<pre>")[1].split("</pre>")[0].strip("\n").splitlines()

    @staticmethod
    def _ends(line):
        import re
        return [m.end() for m in re.finditer(r"\S+", line)]

    def _rows(self):
        rows = []
        for i in range(48):
            htc = 47.9 - i
            intra = (0.97, 0.02) if htc < 10 else (None, None)
            rows += [Row(T0 + timedelta(hours=i), htc, i % 24, C, .31, .63, intra[0]),
                     Row(T0 + timedelta(hours=i), htc, i % 24, A, .2, .3, intra[1])]
        return rows

    def test_every_value_ends_where_its_checkpoint_ends(self):
        grid = self._grid(self._rows())
        header = self._ends(grid[0])[2:]          # skip "hours left" (two words)
        assert len(header) == len(CHECKPOINTS)
        for line in grid[1:]:
            assert self._ends(line)[1:] == header, line

    def test_long_bucket_labels_never_enter_the_grid(self):
        labels = {**LABELS, C: "76°F or below", A: "a very long bucket name"}
        grid = self._grid(self._rows(), labels)
        assert not any("below" in ln or "long" in ln for ln in grid)

    def test_only_ascii_in_the_grid(self):
        assert all(ch.isascii() for line in self._grid(self._rows()) for ch in line)

    def test_it_fits_a_phone(self):
        assert max(len(line) for line in self._grid(self._rows())) <= 40

    def test_a_checkpoint_with_no_snapshot_is_a_dash(self):
        """Tracked from 11.9h only: 48h and 24h have nothing to show; 12h is
        taken from the 11.9h snapshot (within an hour)."""
        rows = [r for r in self._rows() if r.hours_to_close < 12.5]
        daily = next(ln for ln in self._grid(rows) if ln.startswith("daily")).split()[1:]
        assert daily[:2] == ["-", "-"] and daily[2] == "31%"


class TestBrokenRecordsAreFlagged:
    """A real summary showed a market with one bucket in the database and no
    price ever recorded — and printed it like a result. It must say so."""

    def _rows(self, market_p=None, oid=C):
        return [Row(T0 + timedelta(hours=i), 30.9 - i, i % 24, oid, .04, market_p)
                for i in range(30)]

    def test_no_market_price_is_flagged(self):
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=self._rows())
        assert "no market price was recorded" in text

    def test_a_one_bucket_market_is_flagged_and_not_scored(self):
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels={C: "76°F or below"}, winner_id=C,
                             rows=self._rows(market_p=.5))
        assert "only 1 bucket(s)" in text
        assert "Settled on the winner" not in text and "Brier" not in text

    def test_most_buckets_never_recorded_is_flagged(self):
        labels = {i: f"{i}°F" for i in range(1, 11)}
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=labels, winner_id=1, rows=self._rows(.5, oid=1))
        assert "only 1 of 10 buckets were ever recorded" in text

    def test_a_healthy_record_has_no_warning(self):
        rows = []
        for i in range(10):
            rows += _hour(i, {A: (.3, .4), B: (.1, .1), C: (.6, .5)})
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=rows)
        assert "⚠️" not in text


class TestCheckpointsAndOrder:
    def test_a_snapshot_two_hours_off_is_not_a_checkpoint(self):
        """A cell shows the snapshot within an hour of its checkpoint, or
        nothing — never a reading from a different part of the day."""
        rows = _hour(0, {A: (.3, .4), B: (.1, .1), C: (.6, .5)})
        rows = [Row(r.taken_at, 22.0, r.local_hour, r.outcome_id, r.model_p, r.market_p)
                for r in rows]
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=rows)
        grid = text.split("<pre>")[1].split("</pre>")[0].strip().splitlines()
        daily = next(ln for ln in grid if ln.startswith("daily")).split()[1:]
        assert daily == ["-"] * len(CHECKPOINTS)

    def test_whoever_settled_first_is_named_first(self):
        rows = []
        for i in range(12):                 # 11.5h … 0.5h before close
            htc = 11.5 - i
            mkt_on = htc <= 10
            model_on = htc <= 3
            rows += [Row(T0 + timedelta(hours=i), htc, i, C, .6 if model_on else .2,
                         .7 if mkt_on else .2),
                     Row(T0 + timedelta(hours=i), htc, i, A, .4 if model_on else .7,
                         .2 if mkt_on else .7)]
        text = build_summary(city="X", event_date=date(2026, 9, 24),
                             labels=LABELS, winner_id=C, rows=rows)
        assert "market 10h before close · daily 2h before close" in text
