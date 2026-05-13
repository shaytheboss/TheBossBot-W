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
    app_env: str = "development"
    secret_key: str = "changeme"
    cors_origins: str = "http://localhost:3000"
    metar_fetch_interval: int = 300       # 5 min
    polymarket_fetch_interval: int = 300  # 5 min (was 30s — too aggressive)
    wunderground_fetch_interval: int = 1800
    analyzer_run_interval: int = 300      # 5 min (was 120s)
    min_confidence_for_alert: int = 60
    min_edge_for_alert: float = 0.15
    alert_dedup_minutes: int = 30
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
