from datetime import date, datetime, timezone
from typing import Optional

from app.utils.units import (
    is_celsius_bucket,
    f_to_c,
    fmt_temp_dual,
    fmt_bucket_range_f,
    fmt_bucket_range_c,
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

    bucket_min = signals.get("_bucket_min")
    bucket_max = signals.get("_bucket_max")

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

    # ── Header / location ─────────────────────────────────────────────────
    lat = signals.get("city_lat")
    lon = signals.get("city_lon")
    coord_str = _coord_str(lat, lon)
    station_part = f" ({station_icao})" if station_icao else ""
    coord_part = f" | {coord_str}" if coord_str else ""
    loc_line = f"\U0001f4cd {city_name}{station_part}{coord_part} | {date_str}"

    # ── Bucket display (dual unit when Celsius market) ──────────────────────────
    bucket_display = bucket_label
    f_range = fmt_bucket_range_f(bucket_min, bucket_max)
    if is_c and f_range:
        bucket_display = f"{bucket_label} (= {f_range})"
    elif (not is_c) and f_range and f_range.replace("–", "-") not in bucket_label.replace("–", "-"):
        bucket_display = f"{bucket_label} ({f_range})"

    # ── Realistic pricing line ───────────────────────────────────────────────
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

    # ── Per-source forecast breakdown ────────────────────────────────────────
    blend = signals.get("_blend") or {}
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

    # ── Source spread ───────────────────────────────────────────────────────
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

    breakdown_text = "\n".join(breakdown_lines)
    if spread_line:
        breakdown_text += "\n" + spread_line

    # ── Ensemble section ──────────────────────────────────────────────────────
    ens = blend.get("ensemble")
    ens_section = ""
    if ens:
        hits = ens["hits"]
        n = ens["n"]
        med_f = ens.get("median_f")
        smoothed_pct = ens["smoothed_pct"]
        side_smoothed = (100 - smoothed_pct) if is_no else smoothed_pct
        med_str = fmt_temp_dual(med_f, is_c) if med_f is not None else "—"
        ens_section = (
            f"\n\n\U0001f3b2 *GFS Ensemble ({n} members)*\n"
            f"• {hits}/{n} members forecast a {fc_kind.lower()} inside the bucket\n"
            f"• Median {fc_kind.lower()}: {med_str}\n"
            f"• Laplace-smoothed P(in bucket) = ({hits}+0.5)/({n}+1) = "
            f"{round(smoothed_pct, 1)}%\n"
            f"• ⇒ P({side}) ≈ {round(side_smoothed, 1)}%"
        )

    # ── Math walkthrough ──────────────────────────────────────────────────────
    det_avg = blend.get("det_avg")
    ens_p = blend.get("ens_p")
    wg_p = blend.get("wg_p")
    blend_pre = blend.get("blend_before_adjustments")
    final_p = blend.get("final")
    adjustments = blend.get("adjustments") or []

    def _to_side(p: Optional[float]) -> Optional[float]:
        if p is None:
            return None
        return 1 - p if is_no else p

    math_lines = ["⚗️ *How we got to this estimate*"]
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
    for adj in adjustments:
        delta_side = -adj["delta"] if is_no else adj["delta"]
        sign = "+" if delta_side >= 0 else "−"
        math_lines.append(
            f"• {adj['name']}: {sign}{round(abs(delta_side) * 100, 1)}pp"
        )
    if final_p is not None:
        math_lines.append(
            f"• *Final P({side}) = {round(_to_side(final_p) * 100, 1)}%* "
            f"(clipped to [3%, 97%])"
        )
    math_section = "\n".join(math_lines)

    # ── Atmospheric signals ──────────────────────────────────────────────────
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
    atm_section = (
        "\n\n\U0001f321️ *Atmospheric signals*\n" + "\n".join(atm_lines)
        if atm_lines else ""
    )

    # ── Footer ───────────────────────────────────────────────────────────────
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
        f"\U0001f4c8 Edge vs ask: +{edge_pct}pp\n\n"
        f"\U0001f4d0 *Forecast breakdown* (forecasts for {fc_kind})\n"
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
