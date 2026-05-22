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
    # Minimum directional certainty (in %) required to alert.
    # certainty = max(true_prob, 1 - true_prob). 80 means we need to be
    # at least 80% sure the bucket will (YES) or will not (NO) be the answer.
    min_confidence_for_alert: int = 80
    min_edge_for_alert: float = 0.15
    alert_dedup_minutes: int = 30
    # Only alert for markets resolving within this many days.
    # 0 = same-day only, 1 = today+tomorrow, 3 = default.
    # Markets further out have less reliable forecasts and wider spreads.
    max_days_ahead_for_alert: int = 3
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
