import asyncio
import logging
import signal

from telegram import Bot
from telegram.ext import Application, CommandHandler

from app.config import settings
from app.bot.handlers import (
    cmd_start, cmd_status, cmd_scan,
    cmd_watch, cmd_unwatch, cmd_settings, cmd_dashboard,
)
from app.bot.formatters import fmt_opportunity

logger = logging.getLogger(__name__)

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = Application.builder().token(settings.telegram_bot_token).build()
        _app.add_handler(CommandHandler("start", cmd_start))
        _app.add_handler(CommandHandler("status", cmd_status))
        _app.add_handler(CommandHandler("scan", cmd_scan))
        _app.add_handler(CommandHandler("watch", cmd_watch))
        _app.add_handler(CommandHandler("unwatch", cmd_unwatch))
        _app.add_handler(CommandHandler("settings", cmd_settings))
        _app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    return _app


async def send_opportunity_alert(opportunity, db) -> None:
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser, Alert
    from app.models.market import MarketOutcome, Market
    from app.models.city import City
    from app.models.opportunity import Opportunity as Opp  # noqa: F401 (used below)

    outcome_result = await db.execute(select(MarketOutcome).where(MarketOutcome.id == opportunity.outcome_id))
    outcome = outcome_result.scalar_one_or_none()
    if not outcome:
        return
    market_result = await db.execute(select(Market).where(Market.id == outcome.market_id))
    market = market_result.scalar_one_or_none()
    if not market:
        return
    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city = city_result.scalar_one_or_none()

    # Use the actual event slug stored in external_id — guaranteed to match Polymarket's URL
    market_url = f"https://polymarket.com/event/{market.external_id}" if market.external_id else None

    # Look up the prior opportunity for UPDATE detection (stored in signals by detector)
    signals = opportunity.signals or {}
    prior_opportunity = None
    prior_opp_id = signals.get("_prior_opportunity_id")
    if prior_opp_id is not None:
        prior_result = await db.execute(select(Opp).where(Opp.id == prior_opp_id))
        prior_opportunity = prior_result.scalar_one_or_none()

    text = fmt_opportunity(
        city_name=city.name if city else "Unknown",
        market_question=market.question,
        bucket_label=outcome.bucket_label,
        market_price=float(opportunity.market_price),
        true_prob=float(opportunity.estimated_true_prob),
        edge=float(opportunity.edge),
        confidence=opportunity.confidence_score,
        signals=signals,
        side=opportunity.side or "YES",
        event_date=market.event_date,
        resolution_time=market.resolution_time,
        market_url=market_url,
        station_icao=city.primary_icao if city else None,
        city_timezone=city.timezone if city else None,
        prior_opportunity=prior_opportunity,
    )

    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= opportunity.confidence_score)
    )
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city and city.id not in user.cities_watched:
            continue
        try:
            msg = await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            alert = Alert(
                alert_type="OPPORTUNITY_DETECTED",
                city_id=city.id if city else None,
                market_id=market.id,
                opportunity_id=opportunity.id,
                priority="HIGH",
                message_text=text,
                telegram_message_id=msg.message_id,
            )
            db.add(alert)
        except Exception as e:
            logger.error(f"Failed to send alert to {user.chat_id}: {e}")
    opportunity.alert_sent = True
    await db.commit()


def _fmt_open_position_alert(alert: dict) -> str:
    conf = int(round(alert["certainty"] * 100))
    cal_conf = int(round(alert["calibrated_certainty"] * 100))
    edge_cents = int(round(alert["edge"] * 100))
    entry_cents = int(round(alert["entry_cost"] * 100))
    calib_note = f" (calibrated: {cal_conf}%)" if cal_conf != conf else ""
    change_note = alert.get("change_note")
    if change_note:
        headline = "🔁 *SIGNAL UPDATE — BUY ALREADY OPEN*"
        change_line = f"\n📊 *What changed:* {change_note}\n"
        footer = (
            "_The numbers moved materially since the last alert on this outcome. "
            "Not entering a new trade — a virtual buy is already open. "
            "Review if manually trading._"
        )
    else:
        headline = "🔁 *SIGNAL STILL FIRING — BUY ALREADY OPEN*"
        change_line = ""
        footer = (
            "_The signal has not changed direction. "
            "Monitor or consider scaling if manually trading._"
        )
    return (
        f"{headline}\n\n"
        f"📍 *{alert['city_name']}* — {alert['side']} on _{alert['bucket_label']}_\n"
        f"📅 {alert['event_date'].strftime('%b %d')}\n"
        f"{change_line}\n"
        f"Confidence: *{conf}%*{calib_note}  |  Edge: +{edge_cents}¢  |  Entry: {entry_cents}¢\n\n"
        f"A virtual buy is already open on this outcome today — no new position was created.\n"
        f"{footer}"
    )


def _fmt_bucket_switch_alert(alert: dict) -> str:
    conf = alert["new_confidence"]
    edge_cents = int(round(alert["new_edge"] * 100))
    new_entry = int(round(alert["new_entry_cost"] * 100))
    old_parts = []
    for label, entry in zip(alert["old_buckets"], alert["old_entry_prices"]):
        entry_cents = int(round(entry * 100))
        old_parts.append(f"• _{label}_ (opened at {entry_cents}¢)")
    old_str = "\n".join(old_parts) if old_parts else "• (unknown)"
    return (
        f"🔄 *BUCKET SWITCH SIGNAL*\n\n"
        f"📍 *{alert['city_name']}*\n"
        f"📅 {alert['event_date'].strftime('%b %d')}\n\n"
        f"*New signal:* {alert['new_side']} on _{alert['new_bucket_label']}_\n"
        f"Confidence: *{conf}%*  |  Edge: +{edge_cents}¢  |  Entry: {new_entry}¢\n\n"
        f"*Currently open position(s) on same market:*\n{old_str}\n\n"
        f"_Consider: exit current bucket(s) and enter the new one — or hold both if the "
        f"forecast is genuinely ambiguous between them._"
    )


async def send_side_alert(alert: dict, db) -> None:
    """Send a lightweight Telegram notification for open-position or bucket-switch signals."""
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser

    alert_type = alert.get("type")
    if alert_type == "open_position":
        text = _fmt_open_position_alert(alert)
        min_conf = int(round(alert["certainty"] * 100))
    elif alert_type == "bucket_switch":
        text = _fmt_bucket_switch_alert(alert)
        min_conf = alert["new_confidence"]
    else:
        logger.warning(f"send_side_alert: unknown type {alert_type!r}")
        return

    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= min_conf)
    )
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send side alert to {user.chat_id}: {e}")
    await db.commit()


# ── Intraday (⚡) alerts — separate format so the two strategies are
#    instantly distinguishable in Telegram. See INTRADAY.md. ──────────────────

def _sigma_context(hours_left, peak_passed: bool) -> str:
    if peak_passed:
        return "post-peak — temperature falling"
    if hours_left is None:
        return "schedule-based"
    h = float(hours_left)
    if h >= 6.0:
        return f"{h:.1f}h to peak — high early-day uncertainty"
    if h >= 4.0:
        return f"{h:.1f}h to peak — moderate uncertainty"
    if h >= 2.0:
        return f"{h:.1f}h to peak — uncertainty shrinking"
    if h >= 1.0:
        return f"{h:.1f}h to peak — near-locked"
    return "<1h to peak — almost certain"


def _fmt_intraday_alert(
    opp,
    city_name: str,
    bucket_label: str,
    market_question: str,
    station_icao: str = "",
    event_date=None,
    market_url: str = "",
) -> str:
    sig = opp.signals or {}
    bd = sig.get("_intraday") or {}
    book = sig.get("_book") or {}
    conf = opp.confidence_score
    side = opp.side
    is_no = (side == "NO")

    edge_c = int(round(float(opp.edge) * 100))
    entry_c = int(round(float(sig.get("_entry_cost") or 0) * 100))

    # ── Intraday model fields ─────────────────────────────────────────────
    running = bd.get("running_max_f")
    current = bd.get("current_temp_f")
    expected = bd.get("expected_final_max_f")
    forecast_high = bd.get("forecast_high_f")
    hours_left = bd.get("hours_to_peak_end")
    gain_w = bd.get("gain_weight")
    sigma = bd.get("sigma_used")
    lock = bd.get("lock_state")
    peak_passed = bd.get("peak_passed", False)
    local_hour = bd.get("local_hour")
    f_lo = bd.get("f_lo")
    f_hi = bd.get("f_hi")
    prob_yes = bd.get("probability")

    # ── Local time string ─────────────────────────────────────────────────
    if local_hour is not None:
        h_int = int(local_hour)
        m_int = int(round((local_hour - h_int) * 60))
        local_time_str = f"{h_int:02d}:{m_int:02d}"
    else:
        local_time_str = "--:--"

    # ── Location / date line ──────────────────────────────────────────────
    from datetime import date as _date
    icao_part = f" ({station_icao})" if station_icao else ""
    date_str = ""
    if event_date and isinstance(event_date, _date):
        date_str = f" | {event_date.strftime('%b %d, %Y')}"
    loc_line = f"📍 {city_name}{icao_part}{date_str}"

    # ── Headline ──────────────────────────────────────────────────────────
    if sig.get("_create_virtual_buy"):
        headline = f"⚡ *INTRADAY BUY — {side} {bucket_label}* ({city_name})\n#INTRADAY"
    else:
        headline = f"⚡ *INTRADAY — {side} {bucket_label}* ({city_name})\n#INTRADAY"

    # ── Lock / peak status ────────────────────────────────────────────────
    hours_left_str = f"{float(hours_left):.1f}h" if hours_left is not None else "?h"
    time_line = f"⏳ Local time *{local_time_str}*  |  *{hours_left_str}* to peak window end"
    if lock == "yes_impossible":
        status_block = (
            f"{time_line}\n"
            "🔒 *LOCK — YES IMPOSSIBLE*\n"
            f"Running max *{running}°F* already exceeds this bucket's ceiling *{f_hi}°F*. "
            "The daily max can only increase, so this bucket can never be the final answer.\n"
            "→ NO is mathematically guaranteed."
        )
    elif lock == "yes_locked":
        status_block = (
            f"{time_line}\n"
            "🔒 *LOCK — YES SECURED*\n"
            f"Running max *{running}°F* has already crossed the open-ended bucket floor *{f_lo}°F*. "
            "→ YES is mathematically guaranteed regardless of remaining heating."
        )
    elif peak_passed:
        sigma_str = f"±{sigma:.1f}°F" if sigma is not None else "?"
        status_block = (
            f"{time_line}\n"
            f"🌡 *Peak passed* — temperature is now falling.\n"
            f"Today's max is almost certainly set at *{running}°F*. "
            f"Residual σ = {sigma_str}."
        )
    else:
        heating_pct = int(round((gain_w or 0) * 100))
        gain_w_str = f"{float(gain_w):.2f}" if gain_w is not None else "?"
        status_block = (
            f"{time_line}\n"
            f"Remaining heating: *{heating_pct}%* of today's potential still ahead "
            f"(gain weight = {gain_w_str})"
        )

    # ── Current conditions ────────────────────────────────────────────────
    current_str = f"*{current:.1f}°F*" if current is not None else "N/A"
    running_str = f"*{running}°F*" if running is not None else "N/A"
    expected_str = f"*{expected}°F*" if expected is not None else "N/A"
    cond_lines = [
        f"  Now: {current_str}  |  Running max today: {running_str}  |  Expected final: {expected_str}"
    ]
    if forecast_high is not None and running is not None:
        gap = round(float(forecast_high) - float(running), 1)
        gap_str = f"+{gap}°F" if gap >= 0 else f"{gap}°F"
        cond_lines.append(f"  Forecast high – running max gap: *{gap_str}* (what remains to be gained)")
    conditions_block = "\n".join(cond_lines)

    # ── Per-source forecast table ─────────────────────────────────────────
    fc_sources = sig.get("_forecast_sources") or {}
    fc_lines = []
    for label, val in fc_sources.items():
        fc_lines.append(f"  • {label}: *{val:.1f}°F*")
    if forecast_high is not None:
        if fc_lines:
            fc_vals = list(fc_sources.values())
            lo_f = min(fc_vals)
            hi_f = max(fc_vals)
            spread = round(hi_f - lo_f, 1)
            if spread <= 2:
                agree = "✅ strong agreement"
            elif spread <= 4:
                agree = "⚠️ moderate spread"
            else:
                agree = "🚨 HIGH spread — models disagree"
            fc_lines.append(f"  ↳ Blended high: *{forecast_high:.1f}°F*  |  Range: {lo_f:.1f}–{hi_f:.1f}°F (Δ{spread:.1f}°F) — {agree}")
        else:
            fc_lines.append(f"  ↳ Blended high: *{forecast_high:.1f}°F* (single source)")
    else:
        fc_lines = ["  • No forecast data available"]
    bias_f = sig.get("_forecast_bias_f")
    if fc_sources and bias_f is not None and abs(float(bias_f)) >= 0.1:
        bias_src = (
            "default prior" if sig.get("_forecast_bias_is_default", True)
            else "learned from history"
        )
        sign = "+" if float(bias_f) >= 0 else ""
        fc_lines.append(
            f"  ↳ 🌡️ Airport bias {sign}{float(bias_f):.1f}°F included "
            f"({bias_src}) — METAR highs run warmer than gridded models"
        )
    forecasts_block = "\n".join(fc_lines)

    # ── Probability model math ────────────────────────────────────────────
    sigma_str = f"±{sigma:.1f}°F" if sigma is not None else "?"
    bucket_lo_str = f"{f_lo:.1f}°F" if f_lo is not None else "−∞"
    bucket_hi_str = f"{f_hi:.1f}°F" if f_hi is not None else "+∞"
    yes_pct = round((prob_yes or 0) * 100, 1)
    no_pct = round(100 - yes_pct, 1)
    sigma_ctx = _sigma_context(hours_left, bool(peak_passed))
    math_lines = [
        f"_max(M, X) model: final max = max(running max, X),  X ~ N(μ, σ)_",
        f"  • Bucket bounds: [{bucket_lo_str}, {bucket_hi_str})",
        f"  • M (running max) = {running_str}  |  μ (expected final max) = {expected_str}",
        f"  • σ = {sigma_str}  ({sigma_ctx})",
        f"  • P(YES) = {yes_pct}%  ⇒  P(NO) = {no_pct}%  →  *{side}*",
    ]
    math_block = "\n".join(math_lines)

    # ── Pricing / book ────────────────────────────────────────────────────
    if book.get("bid") is not None and book.get("ask") is not None:
        if is_no:
            side_bid_c = round((1.0 - book["ask"]) * 100)
            side_ask_c = round((1.0 - book["bid"]) * 100)
        else:
            side_bid_c = round(book["bid"] * 100)
            side_ask_c = round(book["ask"] * 100)
        spread_c = round(book.get("spread", 0) * 100)
        price_line = (
            f"💰 Buy *{side}* at ≈{entry_c}¢ "
            f"(bid {side_bid_c}¢ / ask {side_ask_c}¢, spread {spread_c}¢)"
        )
    else:
        price_line = f"💰 Market *{side}* price: {entry_c}¢"

    # ── Virtual buy ───────────────────────────────────────────────────────
    buy_line = ""
    if sig.get("_create_virtual_buy"):
        shares = opp.virtual_shares or 0
        cost = float(opp.virtual_cost or 0)
        win_pnl = shares * 1.0 - cost
        buy_line = (
            f"\n🛒 *Virtual buy*: {shares} × {entry_c}¢ = ${cost:.2f}\n"
            f"   If WIN: +${win_pnl:.2f}  |  If LOSS: −${cost:.2f}\n"
            f"#INTRADAY_BUY"
        )
    else:
        buy_thresh = sig.get("_buy_threshold")
        if buy_thresh is not None:
            buy_line = (
                f"\n_No virtual buy — certainty {conf}% below buy threshold "
                f"{int(round(float(buy_thresh) * 100))}%._"
            )

    # ── Polymarket link ───────────────────────────────────────────────────
    link_line = f"\n[Polymarket]({market_url})" if market_url else ""

    return (
        f"{headline}\n\n"
        f"{loc_line}\n"
        f"📊 {market_question}\n\n"
        f"{status_block}\n\n"
        f"🌡 *Current conditions*\n{conditions_block}\n\n"
        f"📡 *Forecast highs (same-day models)*\n{forecasts_block}\n\n"
        f"🎯 *Probability model*\n{math_block}\n\n"
        f"{price_line}\n"
        f"📈 Edge: +{edge_c}¢  |  Certainty: *{conf}%*"
        f"{buy_line}"
        f"{link_line}"
    )


async def send_intraday_alert(opportunity, db) -> None:
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser, Alert
    from app.models.market import MarketOutcome, Market
    from app.models.city import City

    outcome_result = await db.execute(
        select(MarketOutcome).where(MarketOutcome.id == opportunity.outcome_id))
    outcome = outcome_result.scalar_one_or_none()
    if not outcome:
        return
    market_result = await db.execute(select(Market).where(Market.id == outcome.market_id))
    market = market_result.scalar_one_or_none()
    if not market:
        return
    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city = city_result.scalar_one_or_none()

    market_url = (
        f"https://polymarket.com/event/{market.external_id}" if market.external_id else ""
    )
    text = _fmt_intraday_alert(
        opportunity,
        city_name=city.name if city else "Unknown",
        bucket_label=outcome.bucket_label,
        market_question=market.question or "",
        station_icao=city.primary_icao if city else "",
        event_date=market.event_date,
        market_url=market_url,
    )

    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= opportunity.confidence_score)
    )
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city and city.id not in user.cities_watched:
            continue
        try:
            msg = await bot.send_message(
                chat_id=user.chat_id, text=text, parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            alert = Alert(
                alert_type="INTRADAY_OPPORTUNITY",
                city_id=city.id if city else None,
                market_id=market.id,
                priority="HIGH",
                message_text=text,
                telegram_message_id=msg.message_id,
            )
            db.add(alert)
        except Exception as e:
            logger.error(f"Failed to send intraday alert to {user.chat_id}: {e}")
    opportunity.alert_sent = True
    await db.commit()


def _fmt_intraday_realert(ra: dict) -> str:
    conf = int(round(ra["certainty"] * 100))
    edge_c = int(round(ra["edge"] * 100))
    entry_c = int(round(ra["entry_cost"] * 100))
    bd = ra.get("breakdown") or {}
    running = bd.get("running_max_f")
    expected = bd.get("expected_final_max_f")
    current = bd.get("current_temp_f")
    lock = bd.get("lock_state")
    peak_passed = bd.get("peak_passed", False)
    hours_left = bd.get("hours_to_peak_end")
    sigma = bd.get("sigma_used")

    if lock == "yes_impossible":
        state_line = f"🔒 LOCKED — YES IMPOSSIBLE (running max {running}°F above bucket ceiling)"
    elif lock == "yes_locked":
        state_line = f"🔒 LOCKED — YES SECURED (running max {running}°F crossed bucket floor)"
    elif peak_passed:
        state_line = f"🌡 Peak passed — temp now falling, max set at {running}°F"
    else:
        sigma_str = f"±{sigma:.1f}°F" if sigma is not None else "?"
        hours_str = f"{float(hours_left):.1f}h" if hours_left is not None else "?h"
        state_line = f"⏳ {hours_str} to peak end | running max {running}°F → expected {expected}°F | σ={sigma_str}"

    current_str = f" | now {current:.1f}°F" if current is not None else ""
    return (
        f"⚡ *INTRADAY UPDATE* — {ra['side']} _{ra['bucket_label']}_ ({ra['city_name']})\n"
        f"📊 {ra['change_note']}\n\n"
        f"{state_line}{current_str}\n\n"
        f"💰 Entry: {entry_c}¢  |  Edge: +{edge_c}¢  |  Certainty: *{conf}%*\n"
        f"_Already recorded today — tracking continues on the original position._"
    )


async def send_intraday_realert(ra: dict, db) -> None:
    """Lightweight ⚡ update — certainty moved >=1pp on an already-recorded signal."""
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser

    conf = int(round(ra["certainty"] * 100))
    text = _fmt_intraday_realert(ra)
    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= conf)
    )
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send intraday realert to {user.chat_id}: {e}")
