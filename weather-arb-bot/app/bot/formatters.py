from datetime import date, datetime, timezone
from typing import Optional

_SOURCE_LABELS = {
    "gfs": "GFS",
    "ecmwf": "ECMWF",
    "nws": "NWS",
    "wunderground": "WU",
}


def _ensemble_bucket_pct(ensemble_vals, bucket_min, bucket_max):
    """Return (pct_in_bucket, n_members) or (None, 0) when not computable."""
    if not ensemble_vals:
        return None, 0
    n = len(ensemble_vals)
    if bucket_min is None and bucket_max is None:
        return None, n
    def in_bucket(v):
        if bucket_min is not None and v < bucket_min:
            return False
        if bucket_max is not None and v > bucket_max:
            return False
        return True
    return round(100 * sum(1 for v in ensemble_vals if in_bucket(v)) / n), n


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

    side_price_cents = round((1 - market_price if is_no else market_price) * 100)
    side_prob_pct = round((1 - true_prob if is_no else true_prob) * 100)
    edge_pct = round(edge * 100)

    if event_date:
        if isinstance(event_date, date):
            date_str = event_date.strftime("%b %d, %Y")
        else:
            date_str = str(event_date)
    else:
        date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    station_line = f" `{station_icao}`" if station_icao else ""

    # Open-Meteo ensemble probability for the chosen side
    is_low_market = signals.get("is_low_market", False)
    ensemble = signals.get("gfs_ensemble") or {}
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    p50_key = "p50_low_f" if is_low_market else "p50_high_f"
    ensemble_vals = ensemble.get(ensemble_key) or []
    bucket_min = signals.get("_bucket_min")
    bucket_max = signals.get("_bucket_max")
    pct_in_bucket, n_members = _ensemble_bucket_pct(ensemble_vals, bucket_min, bucket_max)
    p50 = ensemble.get(p50_key)

    # For the side we're alerting on, the bucket probability for NO is (1 - pct_in_bucket)
    side_ensemble_pct: Optional[int] = None
    if pct_in_bucket is not None:
        side_ensemble_pct = (100 - pct_in_bucket) if is_no else pct_in_bucket

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

    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    fc_label_suffix = " low" if is_low_market else " high"
    for source in ("gfs", "ecmwf", "nws", "wunderground"):
        fc = signals.get(f"{source}_forecast") or {}
        val = fc.get(fc_key)
        if val:
            label = _SOURCE_LABELS.get(source, source.upper())
            key_signals.append(f"• {label} forecast{fc_label_suffix}: {val}°F")

    if pct_in_bucket is not None and n_members:
        p50_str = f", median {p50}°F" if p50 else ""
        key_signals.append(
            f"• Open-Meteo ensemble ({n_members} members): {pct_in_bucket}% in bucket{p50_str}"
        )

    signals_text = "\n".join(key_signals) if key_signals else "• No key signals available"

    hours_left = ""
    if resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Closes in ~{h}h ({event_date.strftime('%b %d') if event_date else ''})"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    # Prominent Open-Meteo probability line for the chosen side
    om_line = ""
    if side_ensemble_pct is not None:
        om_line = f"\n\U0001f310 Open-Meteo p({side}): {side_ensemble_pct}% ({n_members} members)"

    return (
        f"🎯 *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"📍 {city_name}{station_line} | {date_str}\n"
        f"📊 Market: {market_question}\n"
        f"🏢 Bucket: {bucket_label} (*{side}*)\n\n"
        f"💰 Market {side} price: {side_price_cents}¢\n"
        f"🧠 Our {side} estimate: {side_prob_pct}%"
        f"{om_line}\n"
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
