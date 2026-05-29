from datetime import datetime
from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint
from app.database import Base


class ForecastAccuracy(Base):
    """Per-source, per-lead-time, per-month forecast error statistics.

    Populated by a background resolution job that compares forecast rows against
    resolved METAR actuals. Provides infrastructure for B2 (learned per-source sigma)
    and B3 (adaptive blend weights by accuracy).
    """
    __tablename__ = "forecast_accuracy"

    id = Column(Integer, primary_key=True, index=True)
    city_id = Column(Integer, nullable=False, index=True)
    source = Column(String(50), nullable=False)
    lead_time_days = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)  # 1-12
    mae = Column(Float, nullable=True)          # mean absolute error in deg F
    bias = Column(Float, nullable=True)         # mean bias in deg F (+ = model runs warm)
    sigma_estimate = Column(Float, nullable=True)  # learned sigma for this source/lead/month
    sample_count = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "city_id", "source", "lead_time_days", "month",
            name="uq_forecast_accuracy_key",
        ),
    )
