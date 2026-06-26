from sqlalchemy import BigInteger, Column, ForeignKey, Index, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class VirtualExit(Base):
    __tablename__ = "virtual_exits"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    triggered_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    theoretical_exit_price = Column(Numeric(6, 4), nullable=True)
    entry_confidence = Column(Integer, nullable=True)
    exit_confidence = Column(Integer, nullable=True)
    forecast_shift_f = Column(Numeric(6, 2), nullable=True)
    trigger_reason = Column(Text, nullable=True)
    signals_at_exit = Column(JSONB, nullable=True)
    theoretical_pnl = Column(Numeric(8, 4), nullable=True)

    opportunity = relationship("Opportunity", foreign_keys=[opportunity_id])

    __table_args__ = (
        Index("ix_virtual_exits_triggered_at", "triggered_at"),
    )
