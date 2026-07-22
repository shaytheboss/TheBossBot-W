from pydantic_settings import BaseSettings
from typing import List, Optional
import os


class Settings(BaseSettings):
    database_url: str = "postgresql://weatherarb:weatherarb@localhost:5432/weatherarb"
    redis_url: Optional[str] = None
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    wunderground_api_key: str = ""
    openweather_api_key: str = ""
    polymarket_api_key: str = ""
    tomorrowio_api_key: str = ""
    meteosource_api_key: str = ""
    app_env: str = "development"
    secret_key: str = "changeme"
    admin_password: str = ""
    cors_origins: str = "http://localhost:3000"
    metar_fetch_interval: int = 300       # 5 min
    polymarket_fetch_interval: int = 300  # 5 min
    wunderground_fetch_interval: int = 1800
    analyzer_run_interval: int = 300      # 5 min
    external_forecast_fetch_interval: int = 14400  # 4h — rate-limited external APIs
    # Tomorrow.io budget-aware job (free tier: 25 req/h, 500/day).
    # 20 req/run x hourly = 480/day, inside both caps.
    tomorrowio_fetch_interval: int = 3600
    tomorrowio_requests_per_run: int = 20
    # DWD ICON (via Open-Meteo). Written to forecasts table only — NOT yet
    # mixed into the deterministic blend. Safe to enable/disable freely.
    icon_enabled: bool = True
    icon_fetch_interval: int = 3600       # 1h, matches GFS/ECMWF cadence
    # Minimum directional certainty (in %) required to alert.
    # certainty = max(true_prob, 1 - true_prob). 80 means we need to be
    # at least 80% sure the bucket will (YES) or will not (NO) be the answer.
    # NOTE: kept as backward-compat fallback. Prefer the 4 split thresholds below.
    min_confidence_for_alert: int = 80
    min_edge_for_alert: float = 0.15
    # Upper bound on edge. Edge above this is treated as a model-error signal
    # rather than a genuine opportunity (the market knows something we don't).
    # 0.45 = 45pp; e.g. our P=80% vs market P=30% (edge=50pp) would be blocked.
    max_edge_for_alert: float = 0.45

    # ── Split alert / virtual-buy thresholds (0.0–1.0) ──────────────────────
    # "near" = market resolves within 1 day (days_ahead <= 1)
    # "far"  = market resolves 2+ days out (days_ahead >= 2)
    # Alert thresholds control whether an opportunity becomes a Telegram alert.
    # NOTE: near was 0.75 which caused "why am I getting 7x% alerts when my
    # threshold says 80" — these split values silently override
    # min_confidence_for_alert. Defaults now aligned at 0.80; admin-set values
    # persist in the app_settings table across restarts.
    min_confidence_alert_near: float = 0.80
    min_confidence_alert_far: float = 0.80
    # Virtual-buy thresholds control whether a simulated 5-share position is
    # opened at alert time. Buy implies alert, so these must be >= alert thresholds.
    min_confidence_buy_near: float = 0.90
    min_confidence_buy_far: float = 0.90
    # Beta-only Telegram throttle (0.0–1.0). Beta still RECORDS opportunities and
    # virtual buys at the standard thresholds above (for learning), but only sends
    # a Telegram alert when its certainty is at least this high. Reduces the flood
    # of beta alerts without losing any data. Does not affect alpha.
    # Note: the market-blend step (60/40 raw/market) structurally reduces blended
    # confidence by ~5–7pp vs raw. Setting this to 0.80 means a raw 90% signal at
    # a typical 72¢ NO market price → 82.8% blended → clears the threshold.
    # Previously 0.85 silenced virtually all 90–94% raw beta signals post-blend.
    min_confidence_beta_alert: float = 0.80
    # Beta-only VIRTUAL-BUY threshold (0.0–1.0). Beta positions are virtual —
    # they cost nothing and are the ONLY calibration data beta gets. The alpha
    # buy thresholds (0.90) became unreachable for beta after the market-blend
    # step (60/40) structurally caps blended certainty around ~88%, which
    # starved beta to ~1 virtual buy/week and froze its learning. 0.85 keeps
    # the data flowing without touching alpha's real-alert thresholds.
    min_confidence_beta_buy: float = 0.85
    alert_dedup_minutes: int = 30
    # Only alert for markets resolving within this many days.
    # 0 = same-day only, 1 = today+tomorrow, 3 = default.
    # Markets further out have less reliable forecasts and wider spreads.
    max_days_ahead_for_alert: int = 3
    # ── Intraday subsystem (same-day, hours-scale; see INTRADAY.md) ─────────
    intraday_enabled: bool = True
    intraday_run_interval: int = 300          # seconds
    intraday_start_hour_local: float = 10.0   # don't run before this local hour
    intraday_peak_start_hour: float = 14.0    # climatological peak window
    intraday_peak_end_hour: float = 17.0
    intraday_min_certainty_alert: float = 0.90
    intraday_min_certainty_buy: float = 0.94
    intraday_min_edge: float = 0.05           # near-certainty makes 5c worthwhile
    intraday_max_edge: float = 0.40
    intraday_max_book_spread: float = 0.10
    intraday_shares_per_buy: int = 5
    # Maximum entry price for a virtual intraday buy (¢). Entry >88¢ means
    # we're risking $4.40 to win ~40¢ — even at 99% win rate the EV is only
    # 3¢ but one lock-failure loss destroys 10 wins. Alerts still fire.
    intraday_max_entry_cost: float = 0.88

    # מאגר דיוק-המודלים הפר-עירוני (model_skill): מרווח העדכון התקופתי
    # בשניות. בנוסף לכך העדכון רץ מיד אחרי כל settlement של פולימרקט.
    model_skill_update_interval: int = 3600

    # Auto-suspend a city after this many consecutive high-conf (≥90%) losses.
    # Set to 0 to disable the streak rule.
    suspension_consecutive_losses: int = 3
    # Chronic-loser rule: suspend when win rate over the last N settled
    # high-conf trades falls below the minimum. Catches steady bleeders the
    # streak rule never sees (a 55%-win city alternates W/L and never loses
    # 3 in a row, yet loses money at every realistic entry price — breakeven
    # at a 75¢ NO entry is 75% win rate). window=0 or rate=0 disables.
    suspension_window_trades: int = 10
    suspension_min_win_rate: float = 0.65
    # How many days to suspend. City resumes automatically when the timer expires.
    suspension_days: int = 7
    # ── Data retention (cost control) ───────────────────────────────────────
    # A daily job de-dups forecasts (keeps the latest per city/source/target/
    # made-day — exactly what the analyzer and model_skill read) and prunes old
    # rows. Windows exceed every computation window so no feature loses data:
    # model_skill reads 90d of forecasts, bias_estimator 14d of METAR.
    retention_enabled: bool = True
    retention_run_interval: int = 86400   # daily
    forecast_retention_days: int = 120
    metar_retention_days: int = 45
    market_price_retention_days: int = 45
    pirep_retention_days: int = 21
    collector_miss_retention_days: int = 90
    sentry_dsn: str = ""

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
