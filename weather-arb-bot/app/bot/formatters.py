from datetime import date, datetime, timezone
from typing import Optional

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
    if br and br.get("min_distance_to_edge_f") is not None:
        parts.append(
            f"⚠️ Forecast sits only {br['min_distance_to_edge_f']:.1f}°F from a "
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
        breakdown_lines.append(
            f"• {det['source']}: {fmt_temp_dual(val_f, is_c)} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
        )

    wg = blend.get("wunderground")
    if wg and wg.get("value_f") is not None:
        val_f = wg["value_f"]
        forecast_vals_f.append(val_f)
        p_in = wg.get("p_in_bucket") or 0.0
        side_p = 1 - p_in if is_no else p_in
        breakdown_lines.append(
            f"• Wunderground (soft): {fmt_temp_dual(val_f, is_c)} "
            f"→ P(in bucket)={round(p_in * 100)}% ⇒ P({side})={round(side_p * 100)}%"
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
            dist = boundary_risk.get("min_distance_to_edge_f")
            fc_avg = boundary_risk.get("forecast_avg_f")
            extra = ""
            if dist is not None and fc_avg is not None:
                extra = (
                    f" _(forecast {fc_avg:.1f}°F is {dist:.1f}°F from a "
                    "bucket edge — pulls P toward 50% to price resolution risk)_"
                )
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp{extra}"
            )
        elif name == "Boundary proximity":
            math_lines.append(
                f"• {name}: {sign}{round(abs(delta_side) * 100, 1)}pp"
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
    if resolution_time:
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
        f"\U0001f3af *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"{loc_line}\n"
        f"\U0001f4ca Market: {market_question}\n"
        f"\U0001f3e2 Bucket: {bucket_display} (*{side}*)\n\n"
        f"{price_line}\n"
        f"\U0001f9e0 Our P({side}) estimate: {side_prob_pct}%\n"
        f"\U0001f4c8 Edge vs ask: +{edge_pct}pp"
        f"{bottom_line_section}\n\n"
        f"\U0001f4d0 *Forecast breakdown* ({fc_kind}{sigma_hint})\n"
        f"{breakdown_text}"
        f"{ens_section}\n\n"
        f"{math_section}"
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
