"""Tests for _beta_source_sigma residual-noise decomposition.

Root cause fixed: when mae ≈ |bias| (pure systematic error, e.g. Ankara ECMWF
always cold by 4°F), using raw MAE to set sigma double-counts the bias already
corrected in _beta_source_bias. Sigma is now based on the unpredictable residual
sqrt(max(0, mae² − b_corrected²)), keeping London-like variance cities unchanged
while tightening sigma for clean-bias cities where the correction actually works.
"""
import math
import pytest
from dataclasses import dataclass
from typing import Optional

from app.analyzers.beta_estimator import (
    _beta_source_sigma,
    BETA_SIGMA_FLOOR,
    BETA_SIGMA_CAP,
    BETA_BIAS_SHRINK_K,
    BETA_MAE_SIGMA_SCALE,
)


@dataclass
class _FakeSkill:
    samples: int
    mae_f: Optional[float]
    bias_f: Optional[float]
    hits: int = 0
    hit_rate: Optional[float] = None
    weight: float = 1.0


SIGMA_GLOBAL = 4.0   # typical value from _lead_sigma(da=0)


# ── Archetype 1: Ankara — mae ≈ |bias| (pure systematic, correctable) ─────────

class TestPureSystematicBias:
    """When mae ≈ |bias| the residual noise is near-zero.
    High-n: sigma should tighten vs old formula (bias was double-counted).
    Low-n:  sigma should stay wide (shrink is small, most bias uncorrected).
    """

    def test_high_n_sigma_tighter_than_naive(self):
        """n=37 (aggregated Ankara ECMWF): corrected 65% of bias → small residual."""
        skill = _FakeSkill(samples=37, mae_f=4.0, bias_f=-4.0)
        sigma = _beta_source_sigma(skill, SIGMA_GLOBAL)

        # Old naive formula: mae_sigma = 4.0*1.2 = 4.8
        #   → base = max(4.0, 4.8) = 4.8
        #   → bias_unc = 4.0/sqrt(37) ≈ 0.66
        #   → old_sigma ≈ sqrt(4.8²+0.66²) ≈ 4.85
        old_sigma_approx = math.sqrt((4.0 * BETA_MAE_SIGMA_SCALE) ** 2
                                     + (4.0 / math.sqrt(37)) ** 2)
        assert sigma < old_sigma_approx, (
            f"high-n pure-systematic city should have tighter sigma "
            f"({sigma:.3f} >= old {old_sigma_approx:.3f})"
        )

    def test_high_n_above_floor(self):
        skill = _FakeSkill(samples=37, mae_f=4.0, bias_f=-4.0)
        sigma = _beta_source_sigma(skill, SIGMA_GLOBAL)
        assert sigma >= BETA_SIGMA_FLOOR

    def test_low_n_stays_wide(self):
        """n=10 (single slot, KL ECMWF): shrink≈0.33, most bias uncorrected → wide."""
        skill_low_n  = _FakeSkill(samples=10,  mae_f=3.0, bias_f=-3.0)
        skill_high_n = _FakeSkill(samples=60,  mae_f=3.0, bias_f=-3.0)
        sigma_low  = _beta_source_sigma(skill_low_n,  SIGMA_GLOBAL)
        sigma_high = _beta_source_sigma(skill_high_n, SIGMA_GLOBAL)
        assert sigma_low > sigma_high, (
            "fewer samples → less bias corrected → wider sigma"
        )

    def test_positive_and_negative_bias_symmetric(self):
        """Sign of bias_f must not matter."""
        skill_neg = _FakeSkill(samples=30, mae_f=4.0, bias_f=-4.0)
        skill_pos = _FakeSkill(samples=30, mae_f=4.0, bias_f=+4.0)
        assert _beta_source_sigma(skill_neg, SIGMA_GLOBAL) == pytest.approx(
            _beta_source_sigma(skill_pos, SIGMA_GLOBAL), abs=1e-9
        )


# ── Archetype 2: London — mae >> |bias| (pure variance, no systematic error) ──

class TestPureVariance:
    """When bias ≈ 0, residual ≈ mae. Sigma must be essentially unchanged
    vs the old formula — London must stay conservative."""

    def test_near_zero_bias_sigma_unchanged(self):
        """London ECMWF: n=85, mae=4.11, bias=+0.36 → residual ≈ mae."""
        skill = _FakeSkill(samples=85, mae_f=4.11, bias_f=0.36)
        sigma_new = _beta_source_sigma(skill, SIGMA_GLOBAL)

        # Reconstruct old formula for comparison
        mae = 4.11
        old_base = max(SIGMA_GLOBAL, mae * BETA_MAE_SIGMA_SCALE)
        old_sigma = math.sqrt(old_base ** 2 + (mae / math.sqrt(85)) ** 2)
        old_sigma = max(BETA_SIGMA_FLOOR, min(BETA_SIGMA_CAP, old_sigma))

        assert sigma_new == pytest.approx(old_sigma, abs=0.15), (
            f"London archetype sigma changed too much: new={sigma_new:.3f} old={old_sigma:.3f}"
        )

    def test_zero_bias_exactly_unchanged(self):
        """bias_f = 0.0 → b_corrected = 0 → sigma_resid = mae exactly."""
        skill = _FakeSkill(samples=50, mae_f=3.5, bias_f=0.0)
        sigma_new = _beta_source_sigma(skill, SIGMA_GLOBAL)

        mae = 3.5
        old_base = max(SIGMA_GLOBAL, mae * BETA_MAE_SIGMA_SCALE)
        old_sigma = math.sqrt(old_base ** 2 + (mae / math.sqrt(50)) ** 2)
        old_sigma = max(BETA_SIGMA_FLOOR, min(BETA_SIGMA_CAP, old_sigma))

        assert sigma_new == pytest.approx(old_sigma, abs=1e-6)


# ── Archetype 3: No skill data → fallback unchanged ────────────────────────────

class TestNoSkillFallback:
    def test_none_skill_returns_sigma_global(self):
        assert _beta_source_sigma(None, SIGMA_GLOBAL) == SIGMA_GLOBAL

    def test_no_mae_returns_sigma_global(self):
        skill = _FakeSkill(samples=20, mae_f=None, bias_f=-2.0)
        assert _beta_source_sigma(skill, SIGMA_GLOBAL) == SIGMA_GLOBAL

    def test_no_bias_uses_mae_directly(self):
        """bias_f=None → falls back to full mae for sigma (no decomposition)."""
        skill = _FakeSkill(samples=20, mae_f=3.0, bias_f=None)
        sigma = _beta_source_sigma(skill, SIGMA_GLOBAL)
        # Without bias, code path: sigma_resid=mae, b_uncorrected=0
        # mae_sigma = 3.0 * 1.2 = 3.6 < SIGMA_GLOBAL=4.0 → base=4.0
        # extra = sqrt(0 + (3.0/sqrt(20))²) = 3.0/4.47 ≈ 0.67
        # result = sqrt(4.0²+0.67²) ≈ 4.06
        assert sigma >= BETA_SIGMA_FLOOR
        assert sigma <= BETA_SIGMA_CAP


# ── Boundary: floor and cap always respected ────────────────────────────────────

class TestFloorCap:
    def test_floor_never_breached(self):
        """Even with mae=0.1 and large n, result must be >= floor."""
        skill = _FakeSkill(samples=1000, mae_f=0.1, bias_f=0.1)
        assert _beta_source_sigma(skill, SIGMA_GLOBAL) >= BETA_SIGMA_FLOOR

    def test_cap_never_exceeded(self):
        """Even with mae=20 and bias=0, result must be <= cap."""
        skill = _FakeSkill(samples=5, mae_f=20.0, bias_f=0.0)
        assert _beta_source_sigma(skill, SIGMA_GLOBAL) <= BETA_SIGMA_CAP

    def test_monotone_in_mae_for_zero_bias(self):
        """Higher MAE → wider sigma (when bias=0, residual scales with MAE)."""
        s_low  = _FakeSkill(samples=30, mae_f=1.0, bias_f=0.0)
        s_mid  = _FakeSkill(samples=30, mae_f=3.0, bias_f=0.0)
        s_high = _FakeSkill(samples=30, mae_f=5.0, bias_f=0.0)
        assert (
            _beta_source_sigma(s_low, SIGMA_GLOBAL)
            <= _beta_source_sigma(s_mid, SIGMA_GLOBAL)
            <= _beta_source_sigma(s_high, SIGMA_GLOBAL)
        )
