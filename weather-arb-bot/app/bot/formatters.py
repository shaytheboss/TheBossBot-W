from datetime import datetime, timezone
from typing import Optional


def fmt_opportunity(city_name, market_question, bucket_label, market_price, true_prob, edge, confidence, signals, resolution_time=None, market_url=None) -> str:
    edge_pct = round(edge * 100)
    price_cents = round(market_price * 100)
    prob_pct = round(true_prob * 100)
    date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
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
    wg = signals.get("wunderground_forecast") or {}
    if wg.get("predicted_high_f"):
        key_signals.append(f"• Wunderground forecast: {wg['predicted_high_f']}°F")
    signals_text = "\n".join(key_signals) if key_signals else "• No key signals available"
    hours_left = ""
    if resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Time to resolution: ~{h}h"
    link_line = f"\n[Polymarket]({market_url})" if market_url else ""
    return (f"🎯 *HIGH CONFIDENCE OPPORTUNITY*\n\n📍 {city_name} | {date_str}\n📊 Market: {market_question}\n🎢 Bucket: {bucket_label} (YES)\n\n💰 Market price: {price_cents}¢\n🧠 Model estimate: {prob_pct}%\n📈 Edge: +{edge_pct}pp\n\n🔍 Key signals:\n{signals_text}\n\n⚠️  Confidence: {confidence}/100{hours_left}{link_line}")


def fmt_status(city_signals: list) -> str:
    if not city_signals:
        return "No cities currently being monitored."
    lines = ["📡 *Current Status*\n"]
    for cs in city_signals:
        lines.append(f"📍 *{cs['city']}*: {cs.get('temp_f', '?')}°F now, forecast {cs.get('forecast_high', '?')}°F")
    return "\n".join(lines)
