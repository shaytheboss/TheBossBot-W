from datetime import date, datetime, timezone
from typing import Optional

_SOURCE_LABELS = {
    "gfs": "GFS",
    "ecmwf": "ECMWF",
    "nws": "NWS",
    "wunderground": "WU",
}


def fmt_opportunity(
    city_name, market_question, bucket_label, market_price, true_prob, edge,
    confidence, signals, side="YES", event_date=None,
    resolution_time=None, market_url=None, station_icao=None
) -> str:
    """Format an opportunity alert.

    side: "YES" or "NO" — the recommended trade direction.
    market_price: always the YES price (0..1). We display the relevant side price.
    true_prob: always the YES probability. We display the relevant side estimate.
    confidence: prediction certainty for the chosen side (0..100).
    event_date: the market's resolution date (shown in header, not today's date).
    """
    is_no = (side == "NO")

    # Prices and estimates for the chosen side
    side_price_cents = round((1 - market_price if is_no else market_price) * 100)
    side_prob_pct = round((1 - true_prob if is_no else true_prob) * 100)
    edge_pct = round(edge * 100)

    # Market event date (not today)
    if event_date:
        if isinstance(event_date, date):
            date_str = event_date.strftime("%b %d, %Y")
        else:
            date_str = str(event_date)
    else:
        date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    station_line = f" `{station_icao}`" if station_icao else ""

    # Signals section
    key_signals = []

    ref = signals.get("reference_metar") or {}
    if ref.get("wind_direction") and ref.get("wind_speed_kt"):
        key_signals.append(f"• Ref station wind {ref['wind_direction']:03d}°/{ref['wind_speed_kt']}kt")

    trend = signals.get("metar_trend") or {}
    primary = signals.get("primary_metar") or {}
    if trend.get("dew_rate_per_hour") and abs(trend["dew_rate_per_hour"]) > 0.3:
        direction = "rising" if trend["dew_rate_per_hour"] > 0 else "falling"
        dp = primary.get("dew_point_f")
        key_signals.append(f"• Dew point {direction} ({dp}°F)")

    pireps = signals.get("pireps") or []
    low_pireps = [p for p in pireps if (p.get("flight_level_ft") or 99999) <= 5000 and p.get("temperature_c") is not None]
    if low_pireps:
        avg_c = sum(p["temperature_c"] for p in low_pireps) / len(low_pireps)
        avg_f = round(avg_c * 9 / 5 + 32)
        key_signals.append(f"• PIREP: {avg_f}°F avg at low altitude")

    # Forecast sources — distinguish high vs low market
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    fc_label_suffix = " low" if is_low_market else " high"

    for source in ("gfs", "ecmwf", "nws", "wunderground"):
        fc = signals.get(f"{source}_forecast") or {}
        val = fc.get(fc_key)
        if val:
            label = _SOURCE_LABELS.get(source, source.upper())
            key_signals.append(f"• {label} forecast{fc_label_suffix}: {val}°F")

    # Open-Meteo ensemble probability (most reliable source)
    ensemble = signals.get("gfs_ensemble") or {}
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    ensemble_vals = ensemble.get(ensemble_key) or []
    if ensemble_vals:
        n = len(ensemble_vals)
        bucket_min = signals.get("_bucket_min")
        bucket_max = signals.get("_bucket_max")
        if bucket_min is not None or bucket_max is not None:
            def in_bucket(v):
                if bucket_min is not None and v < bucket_min:
                    return False
                if bucket_max is not None and v > bucket_max:
                    return False
                return True
            pct_in = round(100 * sum(1 for h in ensemble_vals if in_bucket(h)) / n)
            p50_key = "p50_low_f" if is_low_market else "p50_high_f"
            p50 = ensemble.get(p50_key)
            p50_str = f", median {p50}°F" if p50 else ""
            key_signals.append(f"• Open-Meteo ensemble ({n} members): {pct_in}% in bucket{p50_str}")

    signals_text = "\n".join(key_signals) if key_signals else "• No key signals available"

    hours_left = ""
    if resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Closes in ~{h}h ({event_date.strftime('%b %d') if event_date else ''})"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    return (
        f"🎯 *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"📍 {city_name}{station_line} | {date_str}\n"
        f"📊 Market: {market_question}\n"
        f"🏢 Bucket: {bucket_label} (*{side}*)\n\n"
        f"💰 Market {side} price: {side_price_cents}¢\n"
        f"🧠 Our {side} estimate: {side_prob_pct}%\n"
        f"📈 Edge: +{edge_pct}pp\n\n"
        f"🔍 Key signals:\n{signals_text}\n\n"
        f"⚠️ Certainty: {confidence}%{hours_left}{link_line}"
    )


def fmt_status(city_signals: list) -> str:
    if not city_signals:
        return "No cities currently being monitored."

    lines = ["📡 *Current Status*\n"]
    for cs in city_signals:
        temp = f"{cs['temp_f']}°F" if cs.get("temp_f") is not None else "--"
        forecasts: dict = cs.get("forecasts") or {}

        fc_parts = []
        for source in ("gfs", "ecmwf", "nws", "wunderground"):
            if source in forecasts:
                label = _SOURCE_LABELS[source]
                fc_parts.append(f"{label}:{forecasts[source]}°F")
        fc_str = " | ".join(fc_parts) if fc_parts else "no forecast"

        lines.append(
            f"📍 *{cs['city']}* `{cs.get('icao', '?')}`: now {temp} — {fc_str}"
        )
    return "\n".join(lines)
