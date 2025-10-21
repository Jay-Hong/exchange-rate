# app/models.py

# 표준 라이브러리
from datetime import datetime

# 서드파티 라이브러리
from pytz import timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Index

# 로컬 애플리케이션
from app.database import Base


def get_kst_now():
    kst = timezone('Asia/Seoul')
    return datetime.now(kst)


class InvestingExchangeRate(Base):
    __tablename__ = 'investing_exchange_rates'
    
    id = Column(Integer, primary_key=True, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_kst_now)


class BankExchangeRate(Base):
    __tablename__ = 'bank_exchange_rates'
    
    id = Column(Integer, primary_key=True, index=True)
    bank = Column(String, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_kst_now)
    
    __table_args__ = (
        Index('ix_bank_currency_timestamp', 'bank', 'currency', 'timestamp'),
    )
