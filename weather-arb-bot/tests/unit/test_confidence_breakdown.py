"""Unit tests for the Stats confidence-band breakdown.

The key invariant: because the breakdown is computed from the SAME filtered
row set as the headline cards, summing every band must reproduce the panel
totals (positions, wins, losses, invested, net P&L). This is exactly the
property that was broken when the breakdown ignored the Min-conf filter.
"""
from dataclasses import dataclass
from typing import Optional

from app.api.admin import _band_stats, _confidence_band_breakdown


@dataclass
class _Row:
    confidence_score: Optional[int]
    virtual_status: Optional[str]
    virtual_cost: Optional[float]
    virtual_pnl: Optional[float]


def _make_rows():
    # Mirrors the screenshot scenario: positions cluster at 90-95.
    return [
        _Row(91, "win", 3.50, 1.20),
        _Row(92, "win", 4.00, 0.90),
        _Row(93, "loss", 3.85, -3.85),
        _Row(90, "loss", 3.00, -3.00),     # sub-threshold loser
        _Row(94, "win", 2.50, 0.60),
        _Row(96, "win", 3.85, 1.15),
        _Row(95, "open", 4.00, None),       # still open → not settled
    ]


def test_bands_are_per_range_not_cumulative():
    bands = _confidence_band_breakdown(_make_rows())
    labels = [b["band"] for b in bands]
    # Only the two populated bands appear; empty bands are omitted.
    assert labels == ["90–94%", "95–100%"]
    by_label = {b["band"]: b for b in bands}
    assert by_label["90–94%"]["positions"] == 5   # conf 90,91,92,93,94
    assert by_label["95–100%"]["positions"] == 2  # conf 95 (open) + 96


def test_breakdown_reconciles_with_totals():
    rows = _make_rows()
    bands = _confidence_band_breakdown(rows)

    # Panel-equivalent totals over the same row set.
    total = _band_stats(rows)

    assert sum(b["positions"] for b in bands) == total["positions"]
    assert sum(b["wins"] for b in bands) == total["wins"]
    assert sum(b["losses"] for b in bands) == total["losses"]
    assert round(sum(b["invested"] for b in bands), 2) == total["invested"]
    assert round(sum(b["net_pnl"] for b in bands), 2) == total["net_pnl"]


def test_open_positions_count_but_do_not_settle():
    rows = [
        _Row(95, "open", 4.00, None),
        _Row(96, "win", 2.00, 1.00),
    ]
    (band,) = _confidence_band_breakdown(rows)
    assert band["positions"] == 2      # both counted
    assert band["wins"] == 1
    assert band["losses"] == 0
    assert band["invested"] == 2.00    # only the settled one
    assert band["net_pnl"] == 1.00
    assert band["win_rate_pct"] == 100.0


def test_empty_input_yields_no_bands():
    assert _confidence_band_breakdown([]) == []


def test_roi_none_when_nothing_invested():
    rows = [_Row(92, "open", 5.00, None)]   # open only → no settled cost
    (band,) = _confidence_band_breakdown(rows)
    assert band["roi_pct"] is None
    assert band["win_rate_pct"] is None
