from sqlalchemy import BigInteger, Column, Date, ForeignKey, Integer, String, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import Index
from app.database import Base


class Forecast(Base):
    __tablename__ = "forecasts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False)
    source = Column(String(30), nullable=False)
    forecast_for_date = Column(Date, nullable=False)
    predicted_high_f = Column(Integer)
    predicted_low_f = Column(Integer)
    conditions = Column(Text)
    raw_data = Column(JSONB)
    retrieved_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_forecast_city_date", "city_id", "forecast_for_date", "source"),
    )
