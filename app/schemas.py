# app/schemas.py

# 표준 라이브러리
from datetime import datetime
from typing import Optional, List

# 서드파티 라이브러리
from pydantic import BaseModel


class BankExchangeRateResponse(BaseModel):
    currency: str
    bank: str
    rate: Optional[float]
    timestamp: Optional[str]  # ISO 8601 문자열로 변경

    
    # ORM 객체(= SQLAlchemy 객체)를 Pydantic이 읽을 수 있도록 설정
    class Config:
        # orm_mode = True (Pydantic v1)
        from_attributes = True

# 모바일/AJAX용 주요 스키마
class ExchangeRateItem(BaseModel):
    """
    개별 환율 아이템 (플랫 구조) - ISO 8601 타임스탬프 사용
    """
    currency: str
    bank: str
    rate: float
    timestamp: str  # ISO 8601 형식 (예: "2025-09-27T14:54:21+09:00")

class ExchangeRateMetadata(BaseModel):
    """
    환율 API 메타데이터
    """
    updated_at: str  # ISO 8601 형식
    currencies: List[str]
    banks: List[str]
    total_count: int
    error: Optional[str] = None

class ExchangeRatesResponse(BaseModel):
    """
    모바일/AJAX용 환율 API 응답
    """
    rates: List[ExchangeRateItem]
    metadata: ExchangeRateMetadata
