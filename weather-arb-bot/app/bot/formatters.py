from datetime import date, datetime, timezone
from typing import Optional

import pytz

from app.utils.units import (
    is_celsius_bucket,
    f_to_c,
    fmt_temp_dual,
    fmt_bucket_range_f,
)


def _coord_str(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return ""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.3f}°{ns}, {abs(lon):.3f}°{ew}"


def _api_coord_tag(used_lat: Optional[float], used_lon: Optional[float]) -> str:
    if used_lat is None or used_lon is None:
        return ""
    ns = "N" if used_lat >= 0 else "S"
    ew = "E" if used_lon >= 0 else "W"
    return f" [\U0001f4cd {abs(used_lat):.3f}°{ns}, {abs(used_lon):.3f}°{ew}]"


def _agreement_label(spread_f: float) -> str:
    if spread_f <= 3:
        return "✅ strong agreement"
    if spread_f <= 6:
        return "⚠️ moderate spread"
    return "\U0001f6a8 HIGH spread (models disagree)"


def _lead_label(days_ahead: Optional[int]) -> str:
    if days_ahead is None:
        return "unknown lead"
    if days_ahead < 0:
        return "past market"
    if days_ahead == 0:
        return "same-day"
    if days_ahead == 1:
        return "1-day lead"
    return f"{days_ahead}-day lead"


def _closes_in_text(market_date: date, city_tz_str: Optional[str]) -> str:
    tz_str = city_tz_str or "UTC"
    try:
        tz = pytz.timezone(tz_str)
    except Exception:
        tz = pytz.utc
    local_end = tz.localize(
        datetime(market_date.year, market_date.month, market_date.day, 23, 59, 59)
    )
    utc_now = datetime.now(timezone.utc)
    delta = local_end.astimezone(timezone.utc) - utc_now
    total_seconds = delta.total_seconds()
    if total_seconds < 0:
        return "closed"
    elif total_seconds < 3600:
        return f"~{int(total_seconds / 60)}min"
    else:
        return f"~{total_seconds / 3600:.0f}h"


# ---------------------------------------------------------------------------
# Risk banner
# ---------------------------------------------------------------------------

def _score_row(level: int, label: str, detail: str) -> str:
    """Single row in the decision scorecard. level: 0=green, 1=yellow, 2=red, 3=grey."""
    icons = ["\U0001f7e2", "\U0001f7e1", "\U0001f534", "⚪"]
    return f"  {icons[level]} *{label}:* {detail}"


def _risk_scorecard(
    blend: dict,
    side: str,
    true_prob: float,
    days_ahead: Optional[int],
    ci_pp: Optional[float],
    is_open_ended: bool,
    entry_cost: Optional[float],
    alert_threshold: float,
    buy_threshold: float,
    virtual_buy_opened: Optional[bool] = None,
    no_buy_reason: Optional[str] = None,
) -> str:
    """Build the full decision scorecard that appears at the top of each alert.

    Replaces the single-line risk banner with a per-parameter breakdown so the
    user can see exactly which factors are concerning and make an informed call.

    Each row is independently rated GREEN/YELLOW/RED/GREY and has a plain-language
    explanation. The overall risk level at the top is the worst individual level.

    Parameters
    ----------
    blend        : breakdown dict from probability_estimator
    side         : "YES" or "NO"
    true_prob    : post-normalization P(YES) for this bucket
    days_ahead   : calendar days until market resolution
    ci_pp        : ±half-width of confidence interval in percentage points
    is_open_ended: True if bucket has only one bound (e.g. "90°F or higher")
    entry_cost   : cost per share at entry (0.72 = 72¢)
    alert_threshold : minimum directional certainty required for an alert
    buy_threshold   : minimum certainty required for a virtual buy
    """
    is_no = (side == "NO")
    side_prob = (1 - true_prob) if is_no else true_prob
    side_prob_pct = round(side_prob * 100)

    rows: list[str] = []
    top_level = 0  # overall worst level

    def _track(level: int, label: str, detail: str) -> None:
        nonlocal top_level
        # level 3 = grey (N/A / no data) — does not affect overall risk rating
        if level < 3:
            top_level = max(top_level, level)
        rows.append(_score_row(level, label, detail))

    # ── 1. Confidence ───────────────────────────────────────────────────────
    thresh_pct = round(alert_threshold * 100)
    buy_pct = round(buy_threshold * 100)
    margin_pp = side_prob_pct - thresh_pct
    if side_prob_pct >= buy_pct:
        conf_level = 0
        # Reflect the ACTUAL buy decision, not just the threshold. A confidence
        # above the buy threshold does not guarantee a buy — the city may be
        # blacklisted/suspended. (Calibration is display-only and does NOT block
        # the buy.) Saying "virtual position opened" when none was opened — or
        # vice-versa — is the contradiction users hit ("opened" up top, "no buy"
        # at the bottom). virtual_buy_opened=None keeps the legacy wording for
        # callers that don't pass the real status.
        if virtual_buy_opened is False:
            reason = f" ({no_buy_reason})" if no_buy_reason else ""
            conf_note = f"above buy threshold ({buy_pct}%) — no virtual buy{reason}"
        else:
            conf_note = f"above buy threshold ({buy_pct}%) — virtual position opened"
    elif side_prob_pct >= thresh_pct + 5:
        conf_level = 0
        conf_note = f"+{margin_pp}pp above alert threshold ({thresh_pct}%)"
    elif side_prob_pct >= thresh_pct:
        conf_level = 1
        conf_note = f"barely above threshold ({thresh_pct}%) — slim margin"
    else:
        conf_level = 2
        conf_note = f"below threshold — check normalization"
    _track(conf_level, "Confidence", f"P({side})={side_prob_pct}% — {conf_note}")

    # ── 2. Source coverage ──────────────────────────────────────────────────
    sparse = blend.get("sparse_sources") or {}
    # "X/N global models" counts GLOBAL sources only. Prefer the always-present
    # breakdown field; fall back to the legacy sparse dict; last resort count
    # the deterministic rows but never claim MORE than the baseline (the old
    # fallback counted all 7 det models incl. CONUS-only HRRR/NWS → "7/5").
    baseline = int(blend.get("n_global_baseline") or sparse.get("baseline") or 5)
    n_global = blend.get("n_global_det")
    if n_global is None:
        n_global = sparse.get("n_global_det")
    if n_global is None:
        n_global = min(len(blend.get("deterministic") or []), baseline)
    n_global = int(n_global)
    missing_global = blend.get("missing_sources") or []
    missing_no_key = blend.get("missing_no_key") or []
    missing_conus = blend.get("missing_conus_only") or []
    n_missing_global = len(missing_global) + len(missing_no_key)

    cov_parts = [f"{n_global}/{baseline} global models"]
    if missing_no_key:
        cov_parts.append(f"no API key: {', '.join(missing_no_key)}")
    if missing_global:
        cov_parts.append(f"no data: {', '.join(missing_global)}")
    if missing_conus:
        cov_parts.append(f"CONUS-only (N/A): {', '.join(missing_conus)}")

    if n_missing_global >= 3:
        cov_level = 2
    elif n_missing_global >= 1:
        cov_level = 1
    else:
        cov_level = 0
    _track(cov_level, "Coverage", " — ".join(cov_parts))

    # ── 3. Model agreement ──────────────────────────────────────────────────
    model_dis = blend.get("model_disagreement") or {}
    spread_f = float(model_dis.get("source_spread_f") or 0.0)
    if spread_f > 6:
        agree_level = 2
        agree_note = f"Δ{round(spread_f)}°F — HIGH disagreement"
    elif spread_f > 3:
        agree_level = 1
        agree_note = f"Δ{round(spread_f)}°F — moderate spread"
    elif spread_f > 0:
        agree_level = 0
        agree_note = f"Δ{round(spread_f)}°F — strong agreement"
    else:
        agree_level = 3
        agree_note = "single source — no spread to measure"
    _track(agree_level, "Agreement", agree_note)

    # ── 4. Entry cost / payout ──────────────────────────────────────────────
    if entry_cost is not None:
        risk_c = round(entry_cost * 100)
        win_c = round((1.0 - entry_cost) * 100)
        breakeven_pct = risk_c
        rr_ratio = round(entry_cost / (1.0 - entry_cost), 1) if entry_cost < 1 else 99
        if entry_cost >= 0.80:
            ec_level = 2
            ec_note = (
                f"{risk_c}¢ to win {win_c}¢ — {rr_ratio}:1 adverse risk/reward "
                f"(need ≥{breakeven_pct}% win rate to break even)"
            )
        elif entry_cost >= 0.70:
            ec_level = 1
            ec_note = f"{risk_c}¢ to win {win_c}¢ — {rr_ratio}:1 (need ≥{breakeven_pct}%)"
        else:
            ec_level = 0
            ec_note = f"{risk_c}¢ to win {win_c}¢ — {rr_ratio}:1 (need ≥{breakeven_pct}%)"
        _track(ec_level, "Entry cost", ec_note)

    # ── 5. Boundary proximity ───────────────────────────────────────────────
    br = blend.get("boundary_risk") or {}
    dist = br.get("closest_source_dist_f")
    if dist is not None:
        dist = float(dist)
        if dist < 0.5:
            bnd_level = 2
            bnd_note = f"{dist:.1f}°F from bucket edge — right on the line, resolution is a coin flip"
        elif dist < 1.5:
            bnd_level = 1
            bnd_note = f"{dist:.1f}°F from bucket edge — resolution risk applied"
        else:
            bnd_level = 0
            bnd_note = f"{dist:.1f}°F clearance from nearest edge"
        _track(bnd_level, "Boundary", bnd_note)
    else:
        _track(3, "Boundary", "forecast well clear of all edges")

    # ── 6. Open-ended bucket ────────────────────────────────────────────────
    if is_open_ended:
        _track(2, "Bucket type", "open-ended (e.g. ≥90°F) — tail event, σ×1.5 applied")
    else:
        _track(0, "Bucket type", "bounded range — standard confidence")

    # ── 7. Ensemble signal ──────────────────────────────────────────────────
    ens = blend.get("ensemble")
    if ens:
        hits = int(ens.get("hits") or 0)
        n_ens = int(ens.get("n") or 30)
        inside_pct = round(100 * hits / n_ens) if n_ens else 0
        outside_pct = 100 - inside_pct
        # For NO side: we want few hits (most runs outside = NO is correct)
        # For YES side: we want many hits
        relevant_pct = outside_pct if is_no else inside_pct
        relevant_count = (n_ens - hits) if is_no else hits
        if relevant_pct >= 80:
            ens_level = 0
            ens_strength = "strong"
        elif relevant_pct >= 60:
            ens_level = 1
            ens_strength = "moderate"
        else:
            ens_level = 2
            ens_strength = "weak — mixed signal"
        _track(
            ens_level, "Ensemble",
            f"{relevant_count}/{n_ens} runs agree with {side} — {ens_strength}"
        )
    else:
        _track(3, "Ensemble", "no ensemble data")

    # ── Assemble scorecard ──────────────────────────────────────────────────
    overall_emoji = ["\U0001f7e2", "\U0001f7e1", "\U0001f534"][min(top_level, 2)]
    overall_label = ["LOW", "MODERATE", "HIGH"][min(top_level, 2)]

    header = f"\U0001f6a6 *Risk: {overall_emoji} {overall_label}*"
    scorecard = "\n".join(rows)
    return f"{header}\n{scorecard}\n"


# ---------------------------------------------------------------------------

def _why_now_line(prior_opp, current_blend: dict) -> str:
    if prior_opp is None:
        return ""
    prior_signals = getattr(prior_opp, "signals", None) or {}
    prior_blend = prior_signals.get("_blend") or {}
    prior_det = prior_blend.get("deterministic") or []
    current_det = current_blend.get("deterministic") or []
    if not prior_det or not current_det:
        return ""
    prior_by_src = {d.get("source"): d.get("value_f") for d in prior_det}
    biggest = None
    for d in current_det:
        src = d.get("source")
        cur_val = d.get("value_f")
        prior_val = prior_by_src.get(src)
        if cur_val is None or prior_val is None:
            continue
        delta = float(cur_val) - float(prior_val)
        if biggest is None or abs(delta) > abs(biggest[1]):
            biggest = (src, delta)
    if biggest is None or abs(biggest[1]) < 1.0:
        return ""
    src, delta = biggest
    verb = "rose" if delta > 0 else "dropped"
    return f"   Why now: {src} {verb} {abs(round(delta))}°F since last alert\n"


def _bottom_line(
    blend: dict, bucket_min, bucket_max, side: str, is_c: bool
) -> Optional[str]:
    det = blend.get("deterministic") or []
    ens = blend.get("ensemble")
    if not det and not ens:
        return None
    parts: list[str] = []
    if det:
        vals = [d["value_f"] for d in det]
        n = len(vals)
        avg = sum(vals) / n
        lo_f = min(vals)
        hi_f = max(vals)
        spread = hi_f - lo_f
        if spread <= 3:
            parts.append(
                f"All {n} forecasts cluster around {fmt_temp_dual(avg, is_c)} "
                f"(±{round(spread)}°F)."
            )
        elif spread <= 6:
            parts.append(
                f"Forecasts span {fmt_temp_dual(lo_f, is_c)}–"
                f"{fmt_temp_dual(hi_f, is_c)} (moderate spread, Δ{round(spread)}°F)."
            )
        else:
            parts.append(
                f"Forecasts span {fmt_temp_dual(lo_f, is_c)}–"
                f"{fmt_temp_dual(hi_f, is_c)} (large Δ{round(spread)}°F disagreement)."
            )
        if bucket_min is not None and bucket_max is not None:
            if lo_f > bucket_max + 2:
                parts.append(f"That's well above the {bucket_min}–{bucket_max}°F bucket.")
            elif lo_f >= bucket_max:
                parts.append(
                    f"Forecasts mostly above the {bucket_min}–{bucket_max}°F bucket "
                    f"(lowest source {round(lo_f)}°F is at or above the top)."
                )
            elif avg > bucket_max:
                parts.append(
                    f"Average above the {bucket_min}–{bucket_max}°F bucket, "
                    f"but source range starts at {round(lo_f)}°F (inside the bucket)."
                )
            elif hi_f < bucket_min - 2:
                parts.append(f"That's well below the {bucket_min}–{bucket_max}°F bucket.")
            elif hi_f <= bucket_min:
                parts.append(
                    f"Forecasts mostly below the {bucket_min}–{bucket_max}°F bucket "
                    f"(highest source {round(hi_f)}°F is at or below the bottom)."
                )
            elif avg < bucket_min:
                parts.append(
                    f"Average below the {bucket_min}–{bucket_max}°F bucket, "
                    f"but source range ends at {round(hi_f)}°F (inside the bucket)."
                )
            else:
                parts.append(f"That sits inside the {bucket_min}–{bucket_max}°F bucket.")
        elif bucket_min is not None and bucket_max is None:
            if avg >= bucket_min + 2:
                parts.append(f"That's well above the ≥{bucket_min}°F threshold.")
            elif avg >= bucket_min:
                parts.append(f"That sits just above the ≥{bucket_min}°F threshold.")
            else:
                parts.append(f"That sits below the ≥{bucket_min}°F threshold.")
        elif bucket_min is None and bucket_max is not None:
            if avg <= bucket_max - 2:
                parts.append(f"That's well below the ≤{bucket_max}°F threshold.")
            elif avg <= bucket_max:
                parts.append(f"That sits just below the ≤{bucket_max}°F threshold.")
            else:
                parts.append(f"That sits above the ≤{bucket_max}°F threshold.")
    if ens:
        hits = ens["hits"]
        n_ens = ens["n"]
        if hits == 0:
            parts.append(f"The ensemble fully agrees: all {n_ens} perturbed runs land outside.")
        elif hits == n_ens:
            parts.append(f"The ensemble fully agrees: all {n_ens} perturbed runs land inside.")
        elif hits / n_ens > 0.7:
            parts.append(f"Ensemble leans the same way ({hits}/{n_ens} inside the bucket).")
        else:
            parts.append(f"Ensemble is mixed ({hits}/{n_ens} inside the bucket).")
    # Honesty check: deterministic models and the GFS ensemble can both point to
    # the SAME side (e.g. NO) while disagreeing on direction — one sees a cooler
    # day, the other a hotter one. Surface that instead of implying consensus.
    if det and ens:
        det_avg = sum(d["value_f"] for d in det) / len(det)
        med = ens.get("median_f")
        if (
            med is not None
            and bucket_min is not None
            and bucket_max is not None
        ):
            det_below, det_above = det_avg < bucket_min, det_avg > bucket_max
            med_below, med_above = med < bucket_min, med > bucket_max
            if (det_below and med_above) or (det_above and med_below):
                parts.append(
                    f"⚠️ Direction split: models average "
                    f"{fmt_temp_dual(det_avg, is_c)} ({'below' if det_below else 'above'} "
                    f"the bucket) but the GFS ensemble median is "
                    f"{fmt_temp_dual(med, is_c)} ({'above' if med_above else 'below'} it) "
                    f"— both miss the bucket so {side} wins either way, but they "
                    f"disagree on whether the day runs hot or cool."
                )
    br = blend.get("boundary_risk")
    if br:
        closest_f = br.get("closest_source_f")
        closest_dist = br.get("closest_source_dist_f")
        avg_f = br.get("avg_forecast_f")
        if closest_f is not None and closest_dist is not None:
            if closest_f != avg_f:
                parts.append(
                    f"⚠️ Source {closest_f:.1f}°F sits {closest_dist:.1f}°F from a "
                    f"bucket edge — resolution-risk premium applied."
                )
            else:
                parts.append(
                    f"⚠️ Forecast sits only {closest_dist:.1f}°F from a "
                    f"bucket edge — resolution-risk premium applied."
                )
    parts.append(f"→ *{side}*.")
    return " ".join(parts)


def _ensemble_verdict(
    ens: dict, bucket_min, bucket_max, is_c: bool, fc_kind_lower: str
) -> str:
    hits = ens["hits"]
    n = ens["n"]
    med = ens.get("median_f")
    if hits == 0:
        base = (
            f"Verdict: zero of {n} runs land inside — the actual {fc_kind_lower} "
            f"will almost certainly fall outside the bucket."
        )
    elif hits == n:
        base = (
            f"Verdict: all {n} runs land inside — the actual {fc_kind_lower} "
            f"is almost certain to fall inside the bucket."
        )
    elif hits / n > 0.7:
        base = f"Verdict: most runs ({hits}/{n}) agree on falling inside the bucket."
    elif hits / n < 0.3:
        base = f"Verdict: most runs ({n - hits}/{n}) agree on falling outside the bucket."
    else:
        base = f"Verdict: mixed signal ({hits}/{n} inside) — high uncertainty."
    if med is not None and bucket_min is not None and bucket_max is not None:
        if med > bucket_max:
            base += f" Median run is {round(med - bucket_max)}°F above the bucket top."
        elif med < bucket_min:
            base += f" Median run is {round(bucket_min - med)}°F below the bucket bottom."
    return base


def _build_uncertainty_section(blend: dict, is_no: bool) -> str:
    lines: list[str] = []
    model_dis = blend.get("model_disagreement")
    br = blend.get("boundary_risk")
    straddle = blend.get("straddle_info")
    has_model_dis = model_dis and model_dis.get("source_spread_f", 0) > 3.0
    has_boundary = br and br.get("blend_weight", 0) > 0
    has_straddle = straddle and straddle.get("straddles", False)
    if not has_model_dis and not has_boundary and not has_straddle:
        return ""
    lines.append("⚗️ *Uncertainty breakdown:*")
    if has_model_dis:
        spread_f = model_dis["source_spread_f"]
        ew_used = model_dis["ensemble_weight_used"]
        ew_pct_base = 70
        ew_pct_used = round(ew_used * 100)
        det = blend.get("deterministic") or []
        wg = blend.get("wunderground")
        all_sources_named = [(d["source"], d["value_f"]) for d in det]
        if wg and wg.get("value_f") is not None:
            all_sources_named.append(("Wunderground", wg["value_f"]))
        spread_detail = ""
        if all_sources_named:
            lo_src = min(all_sources_named, key=lambda x: x[1])
            hi_src = max(all_sources_named, key=lambda x: x[1])
            if lo_src[0] != hi_src[0]:
                spread_detail = (
                    f" ({lo_src[0]}={round(lo_src[1])}°F, "
                    f"{hi_src[0]}={round(hi_src[1])}°F)"
                )
        lines.append(
            f"• Model spread: Δ{round(spread_f)}°F{spread_detail} → "
            f"ensemble weight reduced {ew_pct_base}%→{ew_pct_used}%"
        )
        lines.append("  ↳ High spread means GFS ensemble under-represents model disagreement")
    if has_boundary:
        avg_f = br.get("avg_forecast_f")
        closest_f = br.get("closest_source_f")
        closest_dist = br.get("closest_source_dist_f")
        blend_w = br.get("blend_weight", 0)
        b_min = br.get("bucket_true_min")
        b_max = br.get("bucket_true_max")
        edge_desc = ""
        if b_min is not None and closest_f is not None:
            if abs(closest_f - b_min) <= abs(closest_f - (b_max or 1e9)):
                edge_desc = f"bucket low edge ({closest_dist:.1f}°F from boundary)"
            else:
                edge_desc = f"bucket high edge ({closest_dist:.1f}°F from boundary)"
        source_name = ""
        if closest_f is not None and closest_f != avg_f:
            for d in (blend.get("deterministic") or []):
                if abs(d["value_f"] - closest_f) < 0.01:
                    source_name = d["source"]
                    break
            if not source_name:
                wg = blend.get("wunderground")
                if wg and abs((wg.get("value_f") or 0) - closest_f) < 0.01:
                    source_name = "Wunderground"
            src_str = f"{source_name} ({closest_f:.0f}°F)" if source_name else f"{closest_f:.1f}°F"
        elif avg_f is not None:
            src_str = f"avg forecast {avg_f:.1f}°F"
        else:
            src_str = "forecast"
        lines.append(f"• Boundary proximity: {src_str} at {edge_desc}")
        lines.append(f"  ↳ Blend toward 50% applied (weight={round(blend_w * 100)}%)")
    if has_straddle:
        inside = straddle.get("inside_sources", [])
        outside = straddle.get("outside_sources", [])
        br_min = blend.get("boundary_risk", {}).get("bucket_true_min")
        br_max_display = None
        if blend.get("boundary_risk", {}).get("bucket_true_max") is not None:
            br_max_display = round(blend["boundary_risk"]["bucket_true_max"] - 1)
        bucket_str = ""
        if br_min is not None and br_max_display is not None:
            bucket_str = f" [{round(br_min)}–{br_max_display}°F]"
        elif br_min is not None:
            bucket_str = f" [≥{round(br_min)}°F]"
        n_inside = len(inside)
        n_outside = len(outside)
        fraction_inside = n_inside / (n_inside + n_outside) if (n_inside + n_outside) > 0 else 0
        extra_blend_pct = round(0.10 * fraction_inside * 100)
        lines.append(
            f"• ⚠️ Sources straddle bucket: {n_inside} source{'s' if n_inside != 1 else ''} "
            f"inside{bucket_str}, {n_outside} outside"
        )
        lines.append(f"  ↳ Additional {extra_blend_pct}% blend toward 50% applied")
    return "\n".join(lines)


def fmt_opportunity(
    city_name,
    market_question,
    bucket_label,
    market_price,
    true_prob,
    edge,
    confidence,
    signals,
    side="YES",
    event_date=None,
    resolution_time=None,
    market_url=None,
    station_icao=None,
    city_timezone=None,
    prior_opportunity=None,
) -> str:
    """Render a high-confidence opportunity for Telegram (Markdown)."""
    is_no = (side == "NO")
    is_c = is_celsius_bucket(bucket_label)
    is_low_market = signals.get("is_low_market", False)
    fc_kind = "daily LOW" if is_low_market else "daily HIGH"
    fc_kind_lower = fc_kind.lower()

    bucket_min = signals.get("_bucket_min")
    bucket_max = signals.get("_bucket_max")

    blend = signals.get("_blend") or {}
    days_ahead = blend.get("days_ahead")
    sigma_used = blend.get("sigma_used")
    obs_skipped = blend.get("observation_skipped", False)

    side_prob_pct = round((1 - true_prob if is_no else true_prob) * 100)
    edge_pct = round(edge * 100)

    # F2: CI half-width in percentage points
    ci_pp_val = blend.get("ci_pp")
    if ci_pp_val is not None and float(ci_pp_val) >= 0.5:
        ci_suffix = f" ± {round(float(ci_pp_val))}pp"
    else:
        ci_suffix = ""

    if event_date:
        date_str = (
            event_date.strftime("%b %d, %Y")
            if isinstance(event_date, date)
            else str(event_date)
        )
    else:
        date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    # ── UPDATE header ───────────────────────────────────────────────────────
    update_section = ""
    if prior_opportunity is not None:
        prior_dt = prior_opportunity.detected_at
        prior_ts = prior_dt.strftime("%b %d %H:%M UTC") if prior_dt else "unknown time"
        prior_prob = float(prior_opportunity.estimated_true_prob)
        prior_side_prob = round((1 - prior_prob if is_no else prior_prob) * 100)
        prior_edge_pp = round(float(prior_opportunity.edge) * 100)
        prior_price = float(prior_opportunity.market_price)
        prior_ask_c = round((1 - prior_price if is_no else prior_price) * 100)

        current_side_prob = side_prob_pct
        prob_change = current_side_prob - prior_side_prob
        edge_change = edge_pct - prior_edge_pp

        conf_arrow = f"↑{abs(prob_change)}pp" if prob_change >= 0 else f"↓{abs(prob_change)}pp"

        book = signals.get("_book") or {}
        entry_cost = signals.get("_entry_cost")
        if entry_cost is not None:
            current_ask_c = round(entry_cost * 100)
        elif book.get("ask") is not None:
            current_ask_c = round((1 - book["ask"] if is_no else book["ask"]) * 100)
        else:
            current_ask_c = round((1 - market_price if is_no else market_price) * 100)

        price_change = current_ask_c - prior_ask_c
        confidence_decreased = prob_change < 0
        warn_flag = " ⚠️" if confidence_decreased else ""
        update_section = (
            f"\U0001f501 *UPDATE{warn_flag}* (previously alerted {prior_ts})\n"
            f"   Was: P({side})={prior_side_prob}%, ask={prior_ask_c}¢ | Edge=+{prior_edge_pp}pp\n"
            f"   Now: P({side})={current_side_prob}%, ask={current_ask_c}¢ | Edge=+{edge_pct}pp\n"
            f"   Change: confidence {conf_arrow}"
        )
        if confidence_decreased:
            update_section += " — consider exiting position"
        if price_change != 0:
            price_arrow = f"↑{abs(price_change)}¢" if price_change > 0 else f"↓{abs(price_change)}¢"
            update_section += f", price {price_arrow}"
        update_section += "\n"
        update_section += _why_now_line(prior_opportunity, blend)
        update_section += "\n"

    # ── Location header ─────────────────────────────────────────────────
    lat = signals.get("city_lat")
    lon = signals.get("city_lon")
    coord_str = _coord_str(lat, lon)
    station_part = f" ({station_icao})" if station_icao else ""
    coord_part = f" | {coord_str}" if coord_str else ""
    loc_line = f"\U0001f4cd {city_name}{station_part}{coord_part} | {date_str}"

    # ── Bucket display ─────────────────────────────────────────────────────
    bucket_display = bucket_label
    f_range = fmt_bucket_range_f(bucket_min, bucket_max)
    if is_c and f_range:
        bucket_display = f"{bucket_label} (= {f_range})"
    elif (not is_c) and f_range and f_range.replace("–", "-") not in bucket_label.replace("–", "-"):
        bucket_display = f"{bucket_label} ({f_range})"

    # ── Pricing line ───────────────────────────────────────────────────
    book = signals.get("_book") or {}
    entry_cost = signals.get("_entry_cost")
    if entry_cost is not None and book.get("bid") is not None:
        entry_cents = round(entry_cost * 100)
        if is_no:
            side_bid_c = round((1 - book["ask"]) * 100)
            side_ask_c = round((1 - book["bid"]) * 100)
        else:
            side_bid_c = round(book["bid"] * 100)
            side_ask_c = round(book["ask"] * 100)
        spread_c = round(book["spread"] * 100)
        price_line = (
            f"\U0001f4b0 Buy *{side}* at ≈{entry_cents}¢ "
            f"(bid {side_bid_c}¢ / ask {side_ask_c}¢, spread {spread_c}¢)"
        )
    else:
        side_price_cents = round((1 - market_price if is_no else market_price) * 100)
        price_line = f"\U0001f4b0 Market {side} price: {side_price_cents}¢"

    # ── Virtual buy ────────────────────────────────────────────────────────
    virtual_section = ""
    create_buy = signals.get("_create_virtual_buy")
    buy_thresh = signals.get("_buy_threshold")
    is_blacklisted = signals.get("_city_blacklisted", False)
    # Use the share count the detector actually opened the position with; the
    # old hardcoded 5 could silently diverge from the stored virtual_shares.
    SHARES = int(signals.get("_shares_per_buy") or 5)
    if entry_cost is not None and create_buy:
        entry_cents_v = round(float(entry_cost) * 100)
        cost_v = SHARES * float(entry_cost)
        if_win_pnl = SHARES * 1.00 - cost_v
        virtual_section = (
            f"\n\n\U0001f6d2 *Virtual buy*: {SHARES} shares × {entry_cents_v}¢ "
            f"= ${cost_v:.2f} cost\n"
            f"   If WIN: +${if_win_pnl:.2f} P&L  |  If LOSS: -${cost_v:.2f} P&L"
        )
    elif signals.get("_city_suspended"):
        reason = signals.get("_suspension_reason") or "auto-suspended"
        virtual_section = (
            f"\n\n\U000023f8 *No virtual buy* — *{city_name} is temporarily suspended*. "
            f"({reason}. Suspension lifts automatically — alerts continue for tracking.)"
        )
    elif is_blacklisted:
        # City is blacklisted: even if confidence cleared the buy threshold, no
        # money is committed. Make the reason explicit so it isn't mistaken for
        # a low-confidence skip.
        cleared = buy_thresh is not None and confidence >= round(float(buy_thresh) * 100)
        conf_note = (
            f"confidence {confidence}% would normally buy, but "
            if cleared else f"confidence {confidence}% — "
        )
        virtual_section = (
            f"\n\n\U0001f6ab *No virtual buy* — *{city_name} is blacklisted*. "
            f"({conf_note}blacklisted cities are alerted for tracking but never "
            f"funded.)"
        )
    elif create_buy is False and buy_thresh is not None:
        virtual_section = (
            f"\n\n\U0001f6d2 *No virtual buy* (confidence {confidence}% below "
            f"buy threshold {round(float(buy_thresh) * 100)}%)"
        )

    # ── Bottom line ──────────────────────────────────────────────────────
    bottom_line = _bottom_line(blend, bucket_min, bucket_max, side, is_c)
    bottom_line_section = f"\n\n\U0001f4a1 *Bottom line*\n{bottom_line}" if bottom_line else ""

    # ── Risk scorecard ──────────────────────────────────────────────────────
    # Real buy status + reason, so the scorecard matches what actually happened.
    _buy_opened = signals.get("_create_virtual_buy")
    _no_buy_reason = None
    if _buy_opened is False:
        if signals.get("_city_blacklisted"):
            _no_buy_reason = "city blacklisted"
        elif signals.get("_city_suspended"):
            _no_buy_reason = "city suspended"
    risk_line = _risk_scorecard(
        blend=blend,
        side=side,
        true_prob=true_prob,
        days_ahead=days_ahead,
        ci_pp=ci_pp_val,
        is_open_ended=blend.get("is_open_ended", False),
        entry_cost=signals.get("_entry_cost"),
        alert_threshold=float(signals.get("_alert_threshold") or 0.75),
        buy_threshold=float(signals.get("_buy_threshold") or 0.90),
        virtual_buy_opened=(None if _buy_opened is None else bool(_buy_opened)),
        no_buy_reason=_no_buy_reason,
    )

    # ── Per-source forecast breakdown ─────────────────────────────────────────
    sigma_hint = (
        f" · σ=±{sigma_used:.1f}°F ({_lead_label(days_ahead)})"
        if sigma_used is not None else ""
    )
    breakdown_intro = (
        f"_Each row: model's point forecast → probability the actual "
        f"{fc_kind_lower} lands in the bucket. Narrow buckets give small "
        f"P(in bucket) even when the forecast is centred on them._"
    )
    bias_info = blend.get("bias_correction") or {}
    bias_f_val = float(bias_info.get("bias_f") or 0.0)
    bias_is_default = bool(bias_info.get("is_default", True))
    bias_samples = int(bias_info.get("samples") or 0)
    bias_notes = str(bias_info.get("notes") or "")
    show_bias = abs(bias_f_val) >= 0.1

    det_rows = blend.get("deterministic") or []
    forecast_vals_f: list[float] = []
    breakdown_lines: list[str] = []
    for det in det_rows:
        val_f = det["value_f"]          # already bias-corrected
        raw_f = det.get("raw_value_f")  # original model output
        forecast_vals_f.append(val_f)
        p_in = det.get("p_in_bucket") or 0.0
        side_p = 1 - p_in if is_no else p_in
        coord_tag = _api_coord_tag(det.get("used_lat"), det.get("used_lon"))
        if show_bias and raw_f is not None and abs(val_f - raw_f) >= 0.1:
            raw_str = fmt_temp_dual(raw_f, is_c)
            corr_str = fmt_temp_dual(val_f, is_c)
            val_display = f"{raw_str} → +{bias_f_val:.1f}°F bias = {corr_str}"
        else:
            val_display = fmt_temp_dual(val_f, is_c)
        breakdown_lines.append(
            f"• {det['source']}: {val_display} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
            f"{coord_tag}"
        )
    wg = blend.get("wunderground")
    if wg and wg.get("value_f") is not None:
        val_f = wg["value_f"]
        raw_f = wg.get("raw_value_f")
        forecast_vals_f.append(val_f)
        p_in = wg.get("p_in_bucket") or 0.0
        side_p = 1 - p_in if is_no else p_in
        coord_tag = _api_coord_tag(wg.get("used_lat"), wg.get("used_lon"))
        if show_bias and raw_f is not None and abs(val_f - raw_f) >= 0.1:
            raw_str = fmt_temp_dual(raw_f, is_c)
            corr_str = fmt_temp_dual(val_f, is_c)
            val_display = f"{raw_str} → +{bias_f_val:.1f}°F bias = {corr_str}"
        else:
            val_display = fmt_temp_dual(val_f, is_c)
        breakdown_lines.append(
            f"• Wunderground (soft): {val_display} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
            f"{coord_tag}"
        )
    if not breakdown_lines:
        breakdown_lines = ["• No forecast data available"]
    spread_line = ""
    if len(forecast_vals_f) >= 2:
        lo_f = min(forecast_vals_f)
        hi_f = max(forecast_vals_f)
        spread_f_val = hi_f - lo_f
        spread_line = (
            f"  ↳ Source range: {fmt_temp_dual(lo_f, is_c)}–"
            f"{fmt_temp_dual(hi_f, is_c)} "
            f"(Δ{round(spread_f_val)}°F) — {_agreement_label(spread_f_val)}"
        )
    breakdown_text = breakdown_intro + "\n" + "\n".join(breakdown_lines)
    if spread_line:
        breakdown_text += "\n" + spread_line
    # Airport warm-bias correction transparency line
    if show_bias:
        if bias_is_default:
            bias_desc = f"default prior (no historical data yet)"
        else:
            bias_desc = f"learned from {bias_samples} day(s): {bias_notes}"
        breakdown_text += (
            f"\n  ↳ 🌡️ Airport bias: +{bias_f_val:.1f}°F applied to all models "
            f"({bias_desc})"
        )
    # Surface models that did NOT report, categorised by reason so the user
    # knows whether to act (e.g. add an API key) or accept (CONUS-only).
    missing_global = blend.get("missing_sources") or []
    missing_key = blend.get("missing_no_key") or []
    missing_conus = blend.get("missing_conus_only") or []
    if missing_key:
        breakdown_text += (
            f"\n  ↳ 🔑 API key not configured: {', '.join(missing_key)}"
        )
    if missing_global:
        breakdown_text += (
            f"\n  ↳ ⚠️ No data (check collector): {', '.join(missing_global)}"
        )
    if missing_conus:
        breakdown_text += (
            f"\n  ↳ ℹ️ CONUS-only, N/A here: {', '.join(missing_conus)}"
        )

    # ── Ensemble section ────────────────────────────────────────────────
    ens = blend.get("ensemble")
    ens_section = ""
    if ens:
        hits = ens["hits"]
        n = ens["n"]
        med_f = ens.get("median_f")
        smoothed_pct = ens["smoothed_pct"]
        side_smoothed = (100 - smoothed_pct) if is_no else smoothed_pct
        med_str = fmt_temp_dual(med_f, is_c) if med_f is not None else "—"
        ens_intro = (
            f"_The ensemble runs GFS {n} times with slightly perturbed "
            f"initial conditions to estimate forecast uncertainty. Each "
            f"'member' is a complete forecast for the target date._"
        )
        verdict = _ensemble_verdict(ens, bucket_min, bucket_max, is_c, fc_kind_lower)
        ens_section = (
            f"\n\n\U0001f3b2 *GFS Ensemble ({n} members)*\n"
            f"{ens_intro}\n"
            f"• {hits}/{n} members forecast a {fc_kind_lower} inside the bucket\n"
            f"• Median {fc_kind_lower}: {med_str}\n"
            f"• Laplace-smoothed P(in bucket) = ({hits}+0.5)/({n}+1) = "
            f"{round(smoothed_pct, 1)}%\n"
            f"• ⇒ P({side}) ≈ {round(side_smoothed, 1)}%\n"
            f"_{verdict}_"
        )

    # ── Math walkthrough ──────────────────────────────────────────────────
    det_avg = blend.get("det_avg")
    ens_p = blend.get("ens_p")
    wg_p = blend.get("wg_p")
    blend_pre = blend.get("blend_before_adjustments")
    # Prefer the post-normalization probability so this walkthrough's final
    # number matches the headline estimate exactly (normalization runs after
    # the per-bucket estimate, rescaling all outcomes to sum to 1).
    final_p = blend.get("normalized_final")
    if final_p is None:
        final_p = blend.get("final")
    adjustments = blend.get("adjustments") or []
    boundary_risk = blend.get("boundary_risk")
    model_dis = blend.get("model_disagreement")

    def _to_side(p: Optional[float]) -> Optional[float]:
        if p is None:
            return None
        return 1 - p if is_no else p

    is_open_ended_bucket = blend.get("is_open_ended", False)
    open_ended_note = (
        " _(open-ended bucket: 1.5× σ applied for tail-event uncertainty)_"
        if is_open_ended_bucket else ""
    )
    math_intro = (
        f"_Forecast σ = ±{sigma_used:.1f}°F for a {_lead_label(days_ahead)}"
        f"{open_ended_note}; this controls how confident a single point forecast can be._"
        if sigma_used is not None else ""
    )
    math_lines = ["⚗️ *How we got to this estimate*"]
    if math_intro:
        math_lines.append(math_intro)
    if show_bias:
        if bias_is_default:
            bias_math_note = f"default prior, 0 samples"
        else:
            bias_math_note = f"{bias_samples} samples, {bias_notes}"
        math_lines.append(
            f"• Airport warm-bias correction: +{bias_f_val:.1f}°F added to every model "
            f"_({bias_math_note})_ — METAR daily highs run warmer than gridded NWP"
        )
    if det_avg is not None:
        math_lines.append(
            f"• Deterministic average P({side}) = "
            f"{round(_to_side(det_avg) * 100, 1)}% "
            f"(over {len(det_rows)} model{'s' if len(det_rows) != 1 else ''})"
        )
    if ens_p is not None:
        math_lines.append(f"• Ensemble P({side}) = {round(_to_side(ens_p) * 100, 1)}%")
    if det_avg is not None and ens_p is not None:
        if model_dis and model_dis.get("source_spread_f", 0) > 3.0:
            ew_pct = round(model_dis["ensemble_weight_used"] * 100)
            dw_pct = round(model_dis["det_weight_used"] * 100)
            math_lines.append(
                f"• Blend = {ew_pct}% × ensemble + {dw_pct}% × deterministic "
                f"_(spread-adjusted from 70/30)_"
            )
        else:
            math_lines.append("• Blend = 70% × ensemble + 30% × deterministic")
    if wg_p is not None and (det_avg is not None or ens_p is not None):
        math_lines.append(
            f"• Wunderground soft mix: 90% blend + 10% WG "
            f"({round(_to_side(wg_p) * 100, 1)}%)"
        )
    if blend_pre is not None:
        math_lines.append(
            f"• Pre-adjustment P({side}) = {round(_to_side(blend_pre) * 100, 1)}%"
        )
    norm_scale = blend.get("normalization_scale")
    if norm_scale is not None and abs(float(norm_scale) - 1.0) > 0.02:
        math_lines.append(
            f"• Market normalisation: ×{float(norm_scale):.3f} "
            f"_(raw bucket probs renormalized to sum=1 across outcomes)_"
        )
    if obs_skipped:
        math_lines.append(
            "• _Observation-based adjustments (METAR trend, ref wind, PIREP) "
            f"skipped — this market is {_lead_label(days_ahead)}, so today's "
            "surface obs don't predict the target date._"
        )
    for adj in adjustments:
        name = adj["name"]
        delta_side = -adj["delta"] if is_no else adj["delta"]
        sign = "+" if delta_side >= 0 else "−"
        if name == "Boundary proximity" and boundary_risk:
            closest_f = boundary_risk.get("closest_source_f")
            closest_dist = boundary_risk.get("closest_source_dist_f")
            avg_f_br = boundary_risk.get("avg_forecast_f")
            extra = ""
            if closest_dist is not None and closest_f is not None:
                if closest_f != avg_f_br:
                    extra = (
                        f" _(closest source {closest_f:.1f}°F is {closest_dist:.1f}°F from a "
                        "bucket edge — pulls P toward 50% to price resolution risk)_"
                    )
                else:
                    extra = (
                        f" _(forecast {avg_f_br:.1f}°F is {closest_dist:.1f}°F from a "
                        "bucket edge — pulls P toward 50% to price resolution risk)_"
                    )
            math_lines.append(f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp{extra}")
        elif name == "Boundary proximity":
            math_lines.append(f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp")
        elif name == "Straddle blend":
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(sources on both sides of bucket edge)_"
            )
        elif name == "Dew point convergence":
            dew_info = blend.get("dew_convergence") or {}
            spread_note = f" (T−Td={dew_info['spread_f']}°F)" if dew_info.get("spread_f") is not None else ""
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(near-saturated air{spread_note} suppresses daytime max)_"
            )
        elif name == "Station gradient (sea/lake-breeze proxy)":
            grad_info = blend.get("station_gradient") or {}
            grad_note = f" (+{grad_info['gradient_f']}°F primary vs ref)" if grad_info.get("gradient_f") is not None else ""
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(onshore marine air likely{grad_note})_"
            )
        elif name == "Sparse sources":
            sparse_info = blend.get("sparse_sources") or {}
            n_g = sparse_info.get("n_global_det", "?")
            n_miss = sparse_info.get("n_global_missing", "?")
            base = sparse_info.get("baseline", 5)
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_({n_g}/{base} global models present, {n_miss} missing → blend toward 50%)_"
            )
        else:
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(based on today's METAR/PIREP — same-day only)_"
            )
    if not adjustments and not obs_skipped:
        math_lines.append("• _No observation-based or boundary adjustments triggered._")
    if final_p is not None:
        clip_note = (
            "after market normalisation" if blend.get("normalized_final") is not None
            else "clipped to [3%, 92%]"
        )
        math_lines.append(
            f"• *Final P({side}) = {round(_to_side(final_p) * 100, 1)}%* "
            f"({clip_note})"
        )
    math_section = "\n".join(math_lines)

    # ── Uncertainty breakdown ───────────────────────────────────────────────
    uncertainty_text = _build_uncertainty_section(blend, is_no)
    uncertainty_section = f"\n\n{uncertainty_text}" if uncertainty_text else ""

    # ── Atmospheric signals ──────────────────────────────────────────────────
    atm_section = ""
    if obs_skipped:
        atm_section = (
            f"\n\n\U0001f321️ *Atmospheric signals* (today's obs)\n"
            f"_Skipped for this market — {_lead_label(days_ahead)}. "
            "Today's wind/dew don't tell us about the daily high "
            f"on {date_str}._"
        )
    else:
        atm_lines: list[str] = []
        ref = signals.get("reference_metar") or {}
        if ref.get("wind_direction") is not None and ref.get("wind_speed_kt") is not None:
            atm_lines.append(
                f"• Ref station wind {ref['wind_direction']:03d}°/"
                f"{ref['wind_speed_kt']}kt"
            )
        trend = signals.get("metar_trend") or {}
        primary = signals.get("primary_metar") or {}
        if trend.get("dew_rate_per_hour") and abs(trend["dew_rate_per_hour"]) > 0.3:
            direction = "rising" if trend["dew_rate_per_hour"] > 0 else "falling"
            dp = primary.get("dew_point_f")
            atm_lines.append(f"• Dew point {direction} ({fmt_temp_dual(dp, is_c)})")
        pireps = signals.get("pireps") or []
        low_pireps = [
            p for p in pireps
            if (p.get("flight_level_ft") or 99999) <= 5000
            and p.get("temperature_c") is not None
        ]
        if low_pireps:
            avg_c = sum(p["temperature_c"] for p in low_pireps) / len(low_pireps)
            avg_f_p = avg_c * 9 / 5 + 32
            atm_lines.append(f"• PIREP: {fmt_temp_dual(avg_f_p, is_c)} avg at low altitude")
        dew_info = blend.get("dew_convergence")
        if dew_info:
            atm_lines.append(
                f"• \U0001f4a7 Dew convergence: T−Td={dew_info.get('spread_f', '?')}°F "
                f"— near-saturated air, daytime max suppressed (×0.92)"
            )
        grad_info = blend.get("station_gradient")
        if grad_info:
            atm_lines.append(
                f"• \U0001f30a Station gradient: primary {grad_info.get('primary_f', '?')}°F, "
                f"ref {grad_info.get('reference_f', '?')}°F "
                f"(+{grad_info.get('gradient_f', '?')}°F) — sea/lake-breeze likely (×0.91)"
            )
        if atm_lines:
            atm_section = (
                "\n\n\U0001f321️ *Atmospheric signals* (today's obs — used because "
                "market resolves today)\n" + "\n".join(atm_lines)
            )

    # ── Footer ────────────────────────────────────────────────────────────
    hours_left = ""
    if event_date and isinstance(event_date, date):
        closes_txt = _closes_in_text(event_date, city_timezone)
        if closes_txt and closes_txt != "closed":
            hours_left = f"\n⏰ Closes in {closes_txt}"
        elif closes_txt == "closed":
            hours_left = "\n⏰ Market closed"
    elif resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Closes in ~{h}h"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""
    calibrated_conf = signals.get("_calibrated_confidence")
    calib_note = ""
    if calibrated_conf is not None and abs(calibrated_conf - confidence) >= 2:
        direction = "↓" if calibrated_conf < confidence else "↑"
        calib_note = f" → calibrated {direction}{calibrated_conf}%"
    # Calibration is DISPLAY-ONLY: _calibration_gated flags that the empirical
    # win rate for this band is below the buy threshold, but it does NOT block
    # the buy (create_virtual_buy ignores it). The old text claimed "no virtual
    # buy", contradicting the position that was actually opened above. Only call
    # it a real block when the buy truly wasn't created; otherwise word it as a
    # caution.
    calibration_gated = signals.get("_calibration_gated", False)
    buy_opened = signals.get("_create_virtual_buy")
    if calibration_gated and buy_opened is False:
        calib_gate_note = (
            "\n⛔ _Calibration gate: raw confidence cleared the buy threshold but "
            "historical win rate for this band is lower — no virtual buy._"
        )
    elif calibration_gated:
        calib_gate_note = (
            "\n⚠️ _Calibration caution: historical win rate for this confidence "
            "band is below the raw estimate — treat the edge as thinner than it "
            "looks._"
        )
    else:
        calib_gate_note = ""
    certainty_note = (
        f"\n⚠️ Certainty: {confidence}%{calib_note} "
        f"(directional confidence = max(P(YES), P(NO)) of our blend)"
        f"{calib_gate_note}"
    )

    # Near-money warning: the modal bucket (most likely outcome per blended
    # forecast) has the worst risk/reward profile — a 1-2°F error in either
    # direction is a loss. Flag it clearly so the user can decide whether the
    # edge justifies the exposure.
    near_money_section = ""
    if signals.get("_is_near_money"):
        near_money_section = (
            "\n\n⚠️ *NEAR-MONEY BUCKET* — this is the bucket the blended forecast "
            "lands in (or closest to). The model is most confident here, but a "
            "1-2°F error in either direction is a loss. Treat the edge as narrower "
            "than it appears. Consider sizing down or skipping if manually trading."
        )

    # Headline: when a virtual buy is actually opened, make that unmistakable at
    # the very top and add a #BUY hashtag so these messages are trivially
    # searchable in Telegram (tap the tag or search "#BUY"). Non-buy alerts keep
    # the original opportunity headline.
    if create_buy:
        headline = (
            f"\U0001f6d2 *BUY — {side} {bucket_label}* "
            f"({city_name}, {date_str})\n#BUY"
        )
    else:
        headline = "\U0001f3af *HIGH CONFIDENCE OPPORTUNITY*"

    # Probability first — every alert type leads with the model's certainty so
    # it can be read at a glance from the notification preview.
    return (
        f"*{side_prob_pct}%* "
        f"{update_section}"
        f"{headline}\n\n"
        f"{loc_line}\n"
        f"\U0001f4ca Market: {market_question}\n"
        f"\U0001f3e2 Bucket: {bucket_display} (*{side}*)\n\n"
        f"{price_line}\n"
        f"\U0001f9e0 Our P({side}) estimate: {side_prob_pct}%{ci_suffix}\n"
        f"\U0001f4c8 Edge vs ask: +{edge_pct}pp\n"
        f"{risk_line}"
        f"{virtual_section}"
        f"{bottom_line_section}\n\n"
        f"\U0001f4d0 *Forecast breakdown* ({fc_kind}{sigma_hint})\n"
        f"{breakdown_text}"
        f"{ens_section}\n\n"
        f"{math_section}"
        f"{uncertainty_section}"
        f"{atm_section}"
        f"{near_money_section}"
        f"{certainty_note}{hours_left}{link_line}"
    )


def fmt_status(city_signals: list) -> str:
    _SOURCE_LABELS = {
        "gfs": "GFS", "ecmwf": "ECMWF", "nws": "NWS", "wunderground": "WU",
        "hrrr": "HRRR", "tomorrowio": "T.io", "meteosource": "MSrc",
    }
    if not city_signals:
        return "No cities currently being monitored."
    lines = ["\U0001f4e1 *Current Status*\n"]
    for cs in city_signals:
        temp = f"{cs['temp_f']}°F" if cs.get("temp_f") is not None else "--"
        forecasts: dict = cs.get("forecasts") or {}
        fc_parts = []
        for source in ("gfs", "ecmwf", "hrrr", "nws", "tomorrowio", "meteosource", "wunderground"):
            if source in forecasts:
                label = _SOURCE_LABELS.get(source, source.upper())
                fc_parts.append(f"{label}:{forecasts[source]}°F")
        fc_str = " | ".join(fc_parts) if fc_parts else "no forecast"
        lines.append(
            f"\U0001f4cd *{cs['city']}* `{cs.get('icao', '?')}`: now {temp} — {fc_str}"
        )
    return "\n".join(lines)
