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
    """Return a short coordinate tag for source lines, e.g. [📍 30.162°N, 97.664°W]."""
    if used_lat is None or used_lon is None:
        return ""
    ns = "N" if used_lat >= 0 else "S"
    ew = "E" if used_lon >= 0 else "W"
    return f" [📍 {abs(used_lat):.3f}°{ns}, {abs(used_lon):.3f}°{ew}]"


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
    """Compute time-to-close using the city's local timezone.

    Market resolves at end of market_date in local TZ (23:59:59).
    Returns human-readable string like '~24h', '~45min', or 'closed'.
    """
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
        total_hours = total_seconds / 3600
        return f"~{total_hours:.0f}h"


def _bottom_line(
    blend: dict, bucket_min, bucket_max, side: str, is_c: bool
) -> Optional[str]:
    """One- or two-sentence verbal verdict pulling from the breakdown."""
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
            # Use min/max of sources (not avg) so we don't say "well above"
            # when the lowest source is right at the bucket top.
            if lo_f > bucket_max + 2:
                parts.append(
                    f"That's well above the {bucket_min}–{bucket_max}°F bucket."
                )
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
                parts.append(
                    f"That's well below the {bucket_min}–{bucket_max}°F bucket."
                )
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
                parts.append(
                    f"That sits inside the {bucket_min}–{bucket_max}°F bucket."
                )
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
            parts.append(
                f"The ensemble fully agrees: all {n_ens} perturbed runs land outside."
            )
        elif hits == n_ens:
            parts.append(
                f"The ensemble fully agrees: all {n_ens} perturbed runs land inside."
            )
        elif hits / n_ens > 0.7:
            parts.append(
                f"Ensemble leans the same way ({hits}/{n_ens} inside the bucket)."
            )
        else:
            parts.append(
                f"Ensemble is mixed ({hits}/{n_ens} inside the bucket)."
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
        base = (
            f"Verdict: most runs ({hits}/{n}) agree on falling inside the bucket."
        )
    elif hits / n < 0.3:
        base = (
            f"Verdict: most runs ({n - hits}/{n}) agree on falling outside the bucket."
        )
    else:
        base = (
            f"Verdict: mixed signal ({hits}/{n} inside) — high uncertainty."
        )
    if med is not None and bucket_min is not None and bucket_max is not None:
        if med > bucket_max:
            base += (
                f" Median run is {round(med - bucket_max)}°F above the bucket top."
            )
        elif med < bucket_min:
            base += (
                f" Median run is {round(bucket_min - med)}°F below the bucket bottom."
            )
    return base


def _build_uncertainty_section(blend: dict, is_no: bool) -> str:
    """Build the verbose uncertainty breakdown section for the alert.

    Shows model disagreement weight reduction, boundary proximity using closest
    source, and straddle detection when applicable.
    Returns empty string when no uncertainty adjustments were applied.
    """
    side = "NO" if is_no else "YES"
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
        ew_base = 0.70
        ew_pct_base = round(ew_base * 100)
        ew_pct_used = round(ew_used * 100)

        # Find which sources contributed to the spread
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
        lines.append(
            "  ↳ High spread means GFS ensemble under-represents model disagreement"
        )

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
            det = blend.get("deterministic") or []
            for d in det:
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

        lines.append(
            f"• Boundary proximity: {src_str} at {edge_desc}"
        )
        lines.append(
            f"  ↳ Blend toward 50% applied (weight={round(blend_w * 100)}%)"
        )

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
        lines.append(
            f"  ↳ Additional {extra_blend_pct}% blend toward 50% applied"
        )

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

    if event_date:
        date_str = (
            event_date.strftime("%b %d, %Y")
            if isinstance(event_date, date)
            else str(event_date)
        )
    else:
        date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    # ── UPDATE header (re-alert detection) ─────────────────────────────────────
    update_section = ""
    if prior_opportunity is not None:
        prior_dt = prior_opportunity.detected_at
        if prior_dt:
            prior_ts = prior_dt.strftime("%b %d %H:%M UTC")
        else:
            prior_ts = "unknown time"
        prior_prob = float(prior_opportunity.estimated_true_prob)
        prior_side_prob = round((1 - prior_prob if is_no else prior_prob) * 100)
        prior_edge = float(prior_opportunity.edge)
        prior_edge_pp = round(prior_edge * 100)
        prior_price = float(prior_opportunity.market_price)
        if is_no:
            prior_ask_c = round((1 - prior_price) * 100)
        else:
            prior_ask_c = round(prior_price * 100)

        current_side_prob = side_prob_pct
        prob_change = current_side_prob - prior_side_prob
        edge_change = edge_pct - prior_edge_pp

        if prob_change >= 0:
            conf_arrow = f"↑{abs(prob_change)}pp"
        else:
            conf_arrow = f"↓{abs(prob_change)}pp"

        # Current ask price for comparison
        book = signals.get("_book") or {}
        entry_cost = signals.get("_entry_cost")
        if entry_cost is not None:
            current_ask_c = round(entry_cost * 100)
        elif book.get("ask") is not None:
            if is_no:
                current_ask_c = round((1 - book["ask"]) * 100)
            else:
                current_ask_c = round(book["ask"] * 100)
        else:
            current_ask_c = round((1 - market_price if is_no else market_price) * 100)

        price_change = current_ask_c - prior_ask_c

        confidence_decreased = prob_change < 0
        warn_flag = " ⚠️" if confidence_decreased else ""
        update_section = (
            f"🔁 *UPDATE{warn_flag}* (previously alerted {prior_ts})\n"
            f"   Was: P({side})={prior_side_prob}%, ask={prior_ask_c}¢ | Edge=+{prior_edge_pp}pp\n"
            f"   Now: P({side})={current_side_prob}%, ask={current_ask_c}¢ | Edge=+{edge_pct}pp\n"
            f"   Change: confidence {conf_arrow}"
        )
        if confidence_decreased:
            update_section += " — consider exiting position"
        if price_change != 0:
            price_arrow = f"↑{abs(price_change)}¢" if price_change > 0 else f"↓{abs(price_change)}¢"
            update_section += f", price {price_arrow}"
        update_section += "\n\n"

    # ── Header / location ─────────────────────────────────────────────────────
    lat = signals.get("city_lat")
    lon = signals.get("city_lon")
    coord_str = _coord_str(lat, lon)
    station_part = f" ({station_icao})" if station_icao else ""
    coord_part = f" | {coord_str}" if coord_str else ""
    loc_line = f"\U0001f4cd {city_name}{station_part}{coord_part} | {date_str}"

    # ── Bucket display (dual unit when Celsius market) ──────────────────────────────
    bucket_display = bucket_label
    f_range = fmt_bucket_range_f(bucket_min, bucket_max)
    if is_c and f_range:
        bucket_display = f"{bucket_label} (= {f_range})"
    elif (not is_c) and f_range and f_range.replace("–", "-") not in bucket_label.replace("–", "-"):
        bucket_display = f"{bucket_label} ({f_range})"

    # ── Pricing line ────────────────────────────────────────────────────────────
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

    # ── Virtual buy section ─────────────────────────────────────────────────────
    # Driven by the detector's `_create_virtual_buy` flag (truthful, since the
    # actual Opportunity row may not yet be visible to the formatter caller).
    virtual_section = ""
    create_buy = signals.get("_create_virtual_buy")
    buy_thresh = signals.get("_buy_threshold")
    SHARES = 5
    if entry_cost is not None and create_buy:
        entry_cents_v = round(float(entry_cost) * 100)
        cost_v = SHARES * float(entry_cost)
        if_win_pnl = SHARES * 1.00 - cost_v
        if_loss_pnl = -cost_v
        virtual_section = (
            f"\n\n\U0001f6d2 *Virtual buy*: {SHARES} shares × {entry_cents_v}¢ "
            f"= ${cost_v:.2f} cost\n"
            f"   If WIN: +${if_win_pnl:.2f} P&L  |  "
            f"If LOSS: -${cost_v:.2f} P&L"
        )
    elif create_buy is False and buy_thresh is not None:
        virtual_section = (
            f"\n\n\U0001f6d2 *No virtual buy* (confidence {confidence}% below "
            f"buy threshold {round(float(buy_thresh) * 100)}%)"
        )

    # ── Bottom-line verbal verdict ──────────────────────────────────────────────
    bottom_line = _bottom_line(blend, bucket_min, bucket_max, side, is_c)
    bottom_line_section = f"\n\n\U0001f4a1 *Bottom line*\n{bottom_line}" if bottom_line else ""

    # ── Per-source forecast breakdown ────────────────────────────────────────────
    sigma_hint = (
        f" · σ=±{sigma_used:.1f}°F ({_lead_label(days_ahead)})"
        if sigma_used is not None else ""
    )
    breakdown_intro = (
        f"_Each row: model's point forecast → probability the actual "
        f"{fc_kind_lower} lands in the bucket. Narrow buckets give small "
        f"P(in bucket) even when the forecast is centred on them._"
    )

    det_rows = blend.get("deterministic") or []
    forecast_vals_f: list[float] = []
    breakdown_lines: list[str] = []
    for det in det_rows:
        val_f = det["value_f"]
        forecast_vals_f.append(val_f)
        p_in = det.get("p_in_bucket") or 0.0
        side_p = 1 - p_in if is_no else p_in
        # Include API coordinates if available in the det entry
        coord_tag = _api_coord_tag(det.get("used_lat"), det.get("used_lon"))
        breakdown_lines.append(
            f"• {det['source']}: {fmt_temp_dual(val_f, is_c)} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
            f"{coord_tag}"
        )

    wg = blend.get("wunderground")
    if wg and wg.get("value_f") is not None:
        val_f = wg["value_f"]
        forecast_vals_f.append(val_f)
        p_in = wg.get("p_in_bucket") or 0.0
        side_p = 1 - p_in if is_no else p_in
        coord_tag = _api_coord_tag(wg.get("used_lat"), wg.get("used_lon"))
        breakdown_lines.append(
            f"• Wunderground (soft): {fmt_temp_dual(val_f, is_c)} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
            f"{coord_tag}"
        )

    if not breakdown_lines:
        breakdown_lines = ["• No forecast data available"]

    spread_line = ""
    if len(forecast_vals_f) >= 2:
        lo_f = min(forecast_vals_f)
        hi_f = max(forecast_vals_f)
        spread_f = hi_f - lo_f
        spread_line = (
            f"  ↳ Source range: {fmt_temp_dual(lo_f, is_c)}–"
            f"{fmt_temp_dual(hi_f, is_c)} "
            f"(Δ{round(spread_f)}°F) — {_agreement_label(spread_f)}"
        )

    breakdown_text = breakdown_intro + "\n" + "\n".join(breakdown_lines)
    if spread_line:
        breakdown_text += "\n" + spread_line

    # ── Ensemble section ──────────────────────────────────────────────────
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

    # ── Math walkthrough ────────────────────────────────────────────────────────────
    det_avg = blend.get("det_avg")
    ens_p = blend.get("ens_p")
    wg_p = blend.get("wg_p")
    blend_pre = blend.get("blend_before_adjustments")
    final_p = blend.get("final")
    adjustments = blend.get("adjustments") or []
    boundary_risk = blend.get("boundary_risk")
    model_dis = blend.get("model_disagreement")

    def _to_side(p: Optional[float]) -> Optional[float]:
        if p is None:
            return None
        return 1 - p if is_no else p

    math_intro = (
        f"_Forecast σ = ±{sigma_used:.1f}°F for a {_lead_label(days_ahead)}; "
        f"this controls how confident a single point forecast can be._"
        if sigma_used is not None else ""
    )
    math_lines = ["⚗️ *How we got to this estimate*"]
    if math_intro:
        math_lines.append(math_intro)
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
            avg_f = boundary_risk.get("avg_forecast_f")
            extra = ""
            if closest_dist is not None and closest_f is not None:
                if closest_f != avg_f:
                    extra = (
                        f" _(closest source {closest_f:.1f}°F is {closest_dist:.1f}°F from a "
                        "bucket edge — pulls P toward 50% to price resolution risk)_"
                    )
                else:
                    extra = (
                        f" _(forecast {avg_f:.1f}°F is {closest_dist:.1f}°F from a "
                        "bucket edge — pulls P toward 50% to price resolution risk)_"
                    )
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp{extra}"
            )
        elif name == "Boundary proximity":
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp"
            )
        elif name == "Straddle blend":
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(sources on both sides of bucket edge)_"
            )
        else:
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp "
                f"_(based on today's METAR/PIREP — same-day only)_"
            )
    if not adjustments and not obs_skipped:
        math_lines.append(
            "• _No observation-based or boundary adjustments triggered._"
        )
    if final_p is not None:
        math_lines.append(
            f"• *Final P({side}) = {round(_to_side(final_p) * 100, 1)}%* "
            f"(clipped to [3%, 97%])"
        )
    math_section = "\n".join(math_lines)

    # ── Uncertainty breakdown section (Change 3) ─────────────────────────────────
    uncertainty_text = _build_uncertainty_section(blend, is_no)
    uncertainty_section = (
        f"\n\n{uncertainty_text}" if uncertainty_text else ""
    )

    # ── Atmospheric signals ──────────────────────────────────────────────────────
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
            atm_lines.append(
                f"• Dew point {direction} ({fmt_temp_dual(dp, is_c)})"
            )
        pireps = signals.get("pireps") or []
        low_pireps = [
            p for p in pireps
            if (p.get("flight_level_ft") or 99999) <= 5000
            and p.get("temperature_c") is not None
        ]
        if low_pireps:
            avg_c = sum(p["temperature_c"] for p in low_pireps) / len(low_pireps)
            avg_f = avg_c * 9 / 5 + 32
            atm_lines.append(
                f"• PIREP: {fmt_temp_dual(avg_f, is_c)} avg at low altitude"
            )
        if atm_lines:
            atm_section = (
                "\n\n\U0001f321️ *Atmospheric signals* (today's obs — used because "
                "market resolves today)\n" + "\n".join(atm_lines)
            )

    # ── Footer ───────────────────────────────────────────────────────────────────
    hours_left = ""
    # Change 2: timezone-correct closes-in calculation
    if event_date and isinstance(event_date, date):
        closes_txt = _closes_in_text(event_date, city_timezone)
        if closes_txt and closes_txt != "closed":
            hours_left = f"\n⏰ Closes in {closes_txt}"
        elif closes_txt == "closed":
            hours_left = "\n⏰ Market closed"
    elif resolution_time:
        # Fallback: use raw resolution_time if no event_date
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Closes in ~{h}h"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    certainty_note = (
        f"\n⚠️ Certainty: {confidence}% "
        f"(directional confidence = max(P(YES), P(NO)) of our blend)"
    )

    return (
        f"{update_section}"
        f"\U0001f3af *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"{loc_line}\n"
        f"\U0001f4ca Market: {market_question}\n"
        f"\U0001f3e2 Bucket: {bucket_display} (*{side}*)\n\n"
        f"{price_line}\n"
        f"\U0001f9e0 Our P({side}) estimate: {side_prob_pct}%\n"
        f"\U0001f4c8 Edge vs ask: +{edge_pct}pp"
        f"{virtual_section}"
        f"{bottom_line_section}\n\n"
        f"\U0001f4d0 *Forecast breakdown* ({fc_kind}{sigma_hint})\n"
        f"{breakdown_text}"
        f"{ens_section}\n\n"
        f"{math_section}"
        f"{uncertainty_section}"
        f"{atm_section}"
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
