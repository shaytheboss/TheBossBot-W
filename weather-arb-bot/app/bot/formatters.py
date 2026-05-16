from datetime import date, datetime, timezone
from typing import Optional

from app.analyzers.probability_estimator import _gaussian_bucket_prob


def _coord_str(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return ""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.3f}°{ns}, {abs(lon):.3f}°{ew}"


def _ensemble_bucket_stats(ensemble_vals, bucket_min, bucket_max):
    """Return (raw_hits, n_members, raw_pct, laplace_pct) or None."""
    if not ensemble_vals:
        return None
    if bucket_min is None and bucket_max is None:
        return None
    n = len(ensemble_vals)

    def in_bucket(v):
        if bucket_min is not None and v < bucket_min:
            return False
        if bucket_max is not None and v > bucket_max:
            return False
        return True

    hits = sum(1 for v in ensemble_vals if in_bucket(v))
    raw_pct = round(100 * hits / n)
    laplace_pct = round(100 * (hits + 0.5) / (n + 1))
    return hits, n, raw_pct, laplace_pct


def fmt_opportunity(
    city_name, market_question, bucket_label, market_price, true_prob, edge,
    confidence, signals, side="YES", event_date=None,
    resolution_time=None, market_url=None, station_icao=None
) -> str:
    """Format an opportunity alert with per-source forecast breakdown and station coordinates."""
    is_no = (side == "NO")

    side_price_cents = round((1 - market_price if is_no else market_price) * 100)
    side_prob_pct = round((1 - true_prob if is_no else true_prob) * 100)
    edge_pct = round(edge * 100)

    if event_date:
        date_str = event_date.strftime("%b %d, %Y") if isinstance(event_date, date) else str(event_date)
    else:
        date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")

    # Location line with coordinates
    lat = signals.get("city_lat")
    lon = signals.get("city_lon")
    coord_str = _coord_str(lat, lon)
    station_part = f" ({station_icao})" if station_icao else ""
    coord_part = f" | {coord_str}" if coord_str else ""
    loc_line = f"📍 {city_name}{station_part}{coord_part} | {date_str}"

    # Context
    is_low_market = signals.get("is_low_market", False)
    ensemble = signals.get("gfs_ensemble") or {}
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    p50_key = "p50_low_f" if is_low_market else "p50_high_f"
    ensemble_vals = ensemble.get(ensemble_key) or []
    bucket_min = signals.get("_bucket_min")
    bucket_max = signals.get("_bucket_max")
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"

    # Per-source forecast breakdown
    det_sources = [
        ("gfs_forecast",         "GFS (global)"),
        ("ecmwf_forecast",       "ECMWF"),
        ("hrrr_forecast",        "HRRR (3km CONUS)"),
        ("nws_forecast",         "NWS (official)"),
        ("tomorrowio_forecast",  "Tomorrow.io"),
        ("meteosource_forecast", "Meteosource"),
        ("wunderground_forecast","WunderGround"),
    ]

    breakdown_lines = []
    if bucket_min is not None or bucket_max is not None:
        for sig_key, label in det_sources:
            fc = signals.get(sig_key) or {}
            val = fc.get(fc_key)
            if val is None:
                continue
            p = _gaussian_bucket_prob(val, bucket_min, bucket_max)
            if p is None:
                continue
            side_p_pct = round((1 - p if is_no else p) * 100)
            breakdown_lines.append(f"• {label}: {val}°F → p({side}): {side_p_pct}%")

    # Ensemble line
    stats = _ensemble_bucket_stats(ensemble_vals, bucket_min, bucket_max)
    p50 = ensemble.get(p50_key)
    if stats is not None:
        hits, n_members, _raw, laplace_pct = stats
        side_ens_pct = (100 - laplace_pct) if is_no else laplace_pct
        p50_str = f", median {p50}°F" if p50 else ""
        breakdown_lines.append(
            f"• GFS Ensemble: {hits}/{n_members} in bucket → p({side}): {side_ens_pct}%"
            f" (smoothed{p50_str})"
        )

    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "• No forecast data available"

    # Atmospheric signals (METAR / PIREP context)
    atm_lines = []
    ref = signals.get("reference_metar") or {}
    if ref.get("wind_direction") and ref.get("wind_speed_kt"):
        atm_lines.append(f"• Ref station wind {ref['wind_direction']:03d}°/{ref['wind_speed_kt']}kt")

    trend = signals.get("metar_trend") or {}
    primary = signals.get("primary_metar") or {}
    if trend.get("dew_rate_per_hour") and abs(trend["dew_rate_per_hour"]) > 0.3:
        direction = "rising" if trend["dew_rate_per_hour"] > 0 else "falling"
        dp = primary.get("dew_point_f")
        atm_lines.append(f"• Dew point {direction} ({dp}°F)")

    pireps = signals.get("pireps") or []
    low_pireps = [
        p for p in pireps
        if (p.get("flight_level_ft") or 99999) <= 5000 and p.get("temperature_c") is not None
    ]
    if low_pireps:
        avg_c = sum(p["temperature_c"] for p in low_pireps) / len(low_pireps)
        avg_f = round(avg_c * 9 / 5 + 32)
        atm_lines.append(f"• PIREP: {avg_f}°F avg at low altitude")

    atm_section = ""
    if atm_lines:
        atm_section = "\n\n🌡️ Atmospheric signals:\n" + "\n".join(atm_lines)

    hours_left = ""
    if resolution_time:
        delta = resolution_time - datetime.now(timezone.utc)
        h = int(delta.total_seconds() // 3600)
        if h > 0:
            hours_left = f"\n⏰ Closes in ~{h}h"

    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    return (
        f"🎯 *HIGH CONFIDENCE OPPORTUNITY*\n\n"
        f"{loc_line}\n"
        f"📊 Market: {market_question}\n"
        f"🏢 Bucket: {bucket_label} (*{side}*)\n\n"
        f"💰 Market {side} price: {side_price_cents}¢\n"
        f"🧠 Our {side} estimate: {side_prob_pct}%\n"
        f"📈 Edge: +{edge_pct}pp\n\n"
        f"📐 Forecast breakdown:\n{breakdown_text}"
        f"{atm_section}\n\n"
        f"⚠️ Certainty: {confidence}%{hours_left}{link_line}"
    )


def fmt_status(city_signals: list) -> str:
    _SOURCE_LABELS = {
        "gfs": "GFS", "ecmwf": "ECMWF", "nws": "NWS", "wunderground": "WU",
        "hrrr": "HRRR", "tomorrowio": "T.io", "meteosource": "MSrc",
    }
    if not city_signals:
        return "No cities currently being monitored."

    lines = ["📡 *Current Status*\n"]
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
            f"📍 *{cs['city']}* `{cs.get('icao', '?')}`: now {temp} — {fc_str}"
        )
    return "\n".join(lines)
