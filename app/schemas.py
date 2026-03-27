# app/schemas.py

# 표준 라이브러리
from datetime import datetime
from enum import Enum
from typing import Optional, List, Literal

# 서드파티 라이브러리
from pydantic import BaseModel, Field


# ============================================================
# Phase 2: Enums for validation
# ============================================================

class BankEnum(str, Enum):
    """허용된 은행 코드"""
    INVESTING = "investing"
    KB = "kb"
    HANA = "hana"
    SHINHAN = "shinhan"
    WOORI = "woori"
    IBK = "ibk"
    NH = "nh"
    SC = "sc"
    BS = "bs"
    CITI = "citi"


class CurrencyEnum(str, Enum):
    """허용된 통화쌍"""
    USD_KRW = "usd-krw"
    JPY_KRW = "jpy-krw"
    EUR_KRW = "eur-krw"


class ConditionEnum(str, Enum):
    """알림 조건"""
    ABOVE = "above"  # 이상
    BELOW = "below"  # 이하


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
    bank: BankEnum = Field(..., description="은행 코드")
    currency: CurrencyEnum = Field(..., description="통화쌍")
    condition: ConditionEnum = Field(..., description="조건 (above: 이상, below: 이하)")
    threshold: float = Field(..., gt=0, description="목표 환율")
    is_enabled: bool = Field(default=True, description="활성화 여부 (기본: True)")


class NotificationSettingResponse(BaseModel):
    """알림 설정 응답"""
    id: int
    user_id: str
    bank: str
    currency: str
    condition: str  # "above" or "below"
    threshold: float
    is_enabled: bool  # 활성화 여부
    triggered: bool
    created_at: str  # ISO 8601
    updated_at: Optional[str] = None  # ISO 8601
    triggered_at: Optional[str] = None  # ISO 8601, 발송 시간

    class Config:
        from_attributes = True


class NotificationSettingsListResponse(BaseModel):
    """알림 설정 목록 응답"""
    settings: List[NotificationSettingResponse]
    total_count: int


class NotificationSettingUpdateRequest(BaseModel):
    """알림 설정 수정 요청 (PUT - 부분 업데이트 지원)"""
    bank: Optional[BankEnum] = None  # 은행 변경
    condition: Optional[ConditionEnum] = None  # 조건 변경
    threshold: Optional[float] = Field(None, gt=0)  # 임계값 변경
    is_enabled: Optional[bool] = None  # 활성화 여부 (토글)


class DeleteResponse(BaseModel):
    """삭제 응답"""
    success: bool
    message: str


# ============================================================
# News: 뉴스 피드 스키마
# ============================================================

class NewsItem(BaseModel):
    """개별 뉴스 아이템"""
    id: str
    title: str
    link: Optional[str] = None       # 원문 URL (external_link, report_pdf일 때)
    source: str                      # einfomax, fxi, ...
    content_type: str = "external_link"  # "external_link" | "flash" | "report_pdf" | "direct_text"
    published_at: str                # ISO 8601
    body: Optional[str] = None       # 뉴스 본문 (direct_text일 때)


class NewsMetadata(BaseModel):
    """뉴스 API 메타데이터"""
    returned_count: int  # 이번 응답에 포함된 기사 수
    window_hours: float
    responded_at: str    # ISO 8601


class NewsResponse(BaseModel):
    """뉴스 API 응답"""
    news: List[NewsItem]
    metadata: NewsMetadata
