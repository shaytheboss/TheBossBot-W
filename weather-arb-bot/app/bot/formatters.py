from datetime import datetime, timezone
from typing import Optional

_SOURCE_LABELS = {
    "gfs": "GFS",
    "ecmwf": "ECMWF",
    "nws": "NWS",
    "wunderground": "WU",
}


def fmt_opportunity(
    city_name, market_question, bucket_label, market_price, true_prob, edge,
    confidence, signals, resolution_time=None, market_url=None, station_icao=None
) -> str:
    edge_pct = round(edge * 100)
    price_cents = round(market_price * 100)
    prob_pct = round(true_prob * 100)
    date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    station_line = f" `{station_icao}`" if station_icao else ""

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
    low_pireps = [p for p in pireps if (p.get("flight_level_ft") or 99999) <= 5000]
    if low_pireps:
        avg_c = sum(p["temperature_c"] for p in low_pireps if p.get("temperature_c")) / len(low_pireps)
        avg_f = round(avg_c * 9 / 5 + 32)
        key_signals.append(f"• PIREP: {avg_f}°F avg at low altitude")

    # Show all available model forecasts
    for source in ("gfs", "ecmwf", "nws", "wunderground"):
        key = f"{source}_forecast" if source == "wunderground" else f"{source}_forecast"
        fc = signals.get(key) or signals.get(source) or {}
        if fc.get("predicted_high_f"):
            label = _SOURCE_LABELS.get(source, source.upper())
            key_signals.append(f"• {label} forecast: {fc['predicted_high_f']}°F")

    signals_text = "\n".join(key_signals) if key_signals else "• No key signals available"

    hours_left = ""
    if resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Time to resolution: ~{h}h"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    return (
        f"\U0001f3af *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"\U0001f4cd {city_name}{station_line} | {date_str}\n"
        f"\U0001f4ca Market: {market_question}\n"
        f"\U0001f3e2 Bucket: {bucket_label} (YES)\n\n"
        f"\U0001f4b0 Market price: {price_cents}¢\n"
        f"\U0001f9e0 Model estimate: {prob_pct}%\n"
        f"\U0001f4c8 Edge: +{edge_pct}pp\n\n"
        f"\U0001f50d Key signals:\n{signals_text}\n\n"
        f"⚠️  Confidence: {confidence}/100{hours_left}{link_line}"
    )


def fmt_status(city_signals: list) -> str:
    if not city_signals:
        return "No cities currently being monitored."

    lines = ["\U0001f4e1 *Current Status*\n"]
    for cs in city_signals:
        temp = f"{cs['temp_f']}°F" if cs.get("temp_f") is not None else "--"
        forecasts: dict = cs.get("forecasts") or {}

        # Build forecast string from all available sources
        fc_parts = []
        for source in ("gfs", "ecmwf", "nws", "wunderground"):
            if source in forecasts:
                label = _SOURCE_LABELS[source]
                fc_parts.append(f"{label}:{forecasts[source]}°F")
        fc_str = " | ".join(fc_parts) if fc_parts else "no forecast"

        lines.append(
            f"\U0001f4cd *{cs['city']}* `{cs.get('icao', '?')}`: now {temp} — {fc_str}"
        )
    return "\n".join(lines)
