# app/schemas.py

# 표준 라이브러리
from datetime import datetime
from typing import Optional, List, Literal

# 서드파티 라이브러리
from pydantic import BaseModel, Field


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


# ============================================================
# Phase 2: Firebase Auth + FCM 알림 스키마
# ============================================================

class RegisterDeviceRequest(BaseModel):
    """디바이스 등록 요청"""
    device_token: str = Field(..., min_length=10, description="FCM Device Token")
    platform: Literal["ios", "android"] = Field(..., description="플랫폼")


class RegisterDeviceResponse(BaseModel):
    """디바이스 등록 응답"""
    success: bool
    message: str
    device_id: Optional[int] = None


class NotificationSettingRequest(BaseModel):
    """알림 설정 요청"""
    bank: str = Field(..., min_length=1, description="은행 코드 (예: hana, kb)")
    currency: str = Field(..., min_length=1, description="통화쌍 (예: usd-krw)")
    condition: Literal["greater_than", "less_than"] = Field(
        ..., description="조건 (greater_than: 이상, less_than: 이하)"
    )
    threshold: float = Field(..., gt=0, description="임계값")


class NotificationSettingResponse(BaseModel):
    """알림 설정 응답"""
    id: int
    bank: str
    currency: str
    condition: str
    threshold: float
    enabled: bool
    triggered: bool
    created_at: str

    class Config:
        from_attributes = True


class NotificationSettingsListResponse(BaseModel):
    """알림 설정 목록 응답"""
    settings: List[NotificationSettingResponse]
    total_count: int


class NotificationSettingUpdateRequest(BaseModel):
    """알림 설정 수정 요청"""
    enabled: Optional[bool] = None
    condition: Optional[Literal["greater_than", "less_than"]] = None
    threshold: Optional[float] = Field(None, gt=0)


class DeleteResponse(BaseModel):
    """삭제 응답"""
    success: bool
    message: str
