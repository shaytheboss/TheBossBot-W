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
    min_confidence_alert_near: float = 0.75
    min_confidence_alert_far: float = 0.80
    # Virtual-buy thresholds control whether a simulated 5-share position is
    # opened at alert time. Buy implies alert, so these must be >= alert thresholds.
    min_confidence_buy_near: float = 0.90
    min_confidence_buy_far: float = 0.90
    alert_dedup_minutes: int = 30
    # Only alert for markets resolving within this many days.
    # 0 = same-day only, 1 = today+tomorrow, 3 = default.
    # Markets further out have less reliable forecasts and wider spreads.
    max_days_ahead_for_alert: int = 3
    # Auto-suspend a city after this many consecutive high-conf (≥90%) losses.
    # Set to 0 to disable auto-suspension entirely.
    suspension_consecutive_losses: int = 3
    # How many days to suspend. City resumes automatically when the timer expires.
    suspension_days: int = 7
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
