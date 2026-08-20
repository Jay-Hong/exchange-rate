# app/schemas.py

# 표준 라이브러리
from datetime import datetime
from enum import Enum
from typing import Optional, List, Literal

# 서드파티 라이브러리
from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    
    # ORM 객체(= SQLAlchemy 객체)를 Pydantic이 읽을 수 있도록 설정 (Pydantic V2 ConfigDict)
    model_config = ConfigDict(from_attributes=True)

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


# B2 (ADR-036): 반복 발송 간격(초) 허용 enum. null=한번만(once). bank + source 알림 공유.
# iOS picker enum과 값 일치 필요(불일치 시 422 reject). 0/음수/비-enum reject.
REPEAT_INTERVAL_SEC_ALLOWED = frozenset(
    {60, 300, 600, 1800, 3600, 7200, 14400, 21600, 43200, 86400}
)


def _validate_repeat_interval_sec(value: Optional[int]) -> Optional[int]:
    """null(once) 또는 허용 enum 값만 통과. 그 외 ValueError → FastAPI 422."""
    if value is None:
        return value
    if value not in REPEAT_INTERVAL_SEC_ALLOWED:
        raise ValueError(
            "repeat_interval_sec must be null (once) or one of "
            f"{sorted(REPEAT_INTERVAL_SEC_ALLOWED)} seconds"
        )
    return value


class NotificationSettingRequest(BaseModel):
    """알림 설정 요청"""
    bank: BankEnum = Field(..., description="은행 코드")
    currency: CurrencyEnum = Field(..., description="통화쌍")
    condition: ConditionEnum = Field(..., description="조건 (above: 이상, below: 이하)")
    threshold: float = Field(..., gt=0, description="목표 환율")
    is_enabled: bool = Field(default=True, description="활성화 여부 (기본: True)")
    repeat_interval_sec: Optional[int] = Field(
        default=None,
        description="반복 발송 간격(초). null=한번만(once). 허용: 60/300/600/1800/3600/7200/14400/21600/43200/86400",
    )

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)


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
    repeat_interval_sec: Optional[int] = None  # B2 (ADR-036): null=once / 정수=초 간격
    created_at: str  # ISO 8601
    updated_at: Optional[str] = None  # ISO 8601
    triggered_at: Optional[str] = None  # ISO 8601, 발송 시간

    model_config = ConfigDict(from_attributes=True)


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
    # B2 (ADR-036): null=once / 정수=repeat. PUT에서 "미제공"과 "명시적 null(=once)" 구분은
    # main.py가 model_fields_set으로 판단해 crud sentinel(_UNSET)에 매핑.
    repeat_interval_sec: Optional[int] = Field(default=None)

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)


class DeleteResponse(BaseModel):
    """삭제 응답"""
    success: bool
    message: str


# ============================================================
# Source 기반 알림 스키마 (USDT exchange + KRX derivative)
# ============================================================
# (B2 repeat 간격 helper REPEAT_INTERVAL_SEC_ALLOWED / _validate_repeat_interval_sec는
#  bank 알림 스키마와 공유하므로 파일 상단 NotificationSettingRequest 앞에서 정의.)


class SourceNotificationSettingRequest(BaseModel):
    """Source 기반 알림 설정 생성 요청.

    허용 대상: category in {"exchange", "derivative"}.
    - exchange (USDT 5 source + usdt-krw): upbit/bithumb/coinone/korbit/gopax
    - derivative (KRX 미국달러선물 + usd-krw-futures): krx
      F-2 (2026-05-26): KRX 알림 등록 허용 추가. 실제 발송은 별 축인
      `KRX_ALERT_EVALUATOR_ENABLED` env(F-3)가 열려야 발화 — F-2 land ~
      F-3 활성 사이는 의도된 canary staging gap (API 등록 가능하나 발송 안 됨).

    investing/kb/hana 같은 reference 소스는 기존 `/api/notification-settings`를 사용해야 한다.
    여기에 등록해도 발송 루프가 연결되어 있지 않아 발동되지 않는다.
    서버에서 category not in {"exchange","derivative"} 기준으로 400 응답.
    """
    source: str = Field(
        ..., min_length=1,
        description="source 식별자 (USDT: upbit/bithumb/coinone/korbit/gopax, KRX: krx)",
    )
    asset: str = Field(
        ..., min_length=1,
        description="자산 식별자 (USDT: usdt-krw, KRX: usd-krw-futures)",
    )
    condition: ConditionEnum = Field(..., description="조건 (above/below)")
    threshold: float = Field(..., gt=0, description="목표 환율")
    is_enabled: bool = Field(default=True, description="활성화 여부 (기본: True)")
    repeat_interval_sec: Optional[int] = Field(
        default=None,
        description="반복 발송 간격(초). null=한번만(once). 허용: 60/300/600/1800/3600/7200/14400/21600/43200/86400",
    )

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)


class SourceNotificationSettingUpdateRequest(BaseModel):
    """Source 기반 알림 설정 수정 요청 (PUT - 부분 업데이트).

    source/asset 변경 시에도 category in {"exchange","derivative"} 정책으로
    재검증된다 (F-2 2026-05-26: derivative=KRX 허용 추가).
    """
    source: Optional[str] = Field(None, min_length=1)
    asset: Optional[str] = Field(None, min_length=1)
    condition: Optional[ConditionEnum] = None
    threshold: Optional[float] = Field(None, gt=0)
    is_enabled: Optional[bool] = None
    # B2 (ADR-036): null=once / 정수=repeat. PUT에서 "미제공"과 "명시적 null(=once)" 구분은
    # main.py가 model_fields_set으로 판단해 crud sentinel(_UNSET)에 매핑.
    repeat_interval_sec: Optional[int] = Field(default=None)

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)


class SourceNotificationSettingResponse(BaseModel):
    """Source 기반 알림 설정 응답."""
    id: int
    user_id: str
    source: str
    asset: str
    condition: str  # "above" or "below"
    threshold: float
    is_enabled: bool
    triggered: bool
    repeat_interval_sec: Optional[int] = None  # B2 (ADR-036): null=once / 정수=초 간격
    created_at: str  # ISO 8601
    updated_at: Optional[str] = None
    triggered_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SourceNotificationSettingsListResponse(BaseModel):
    """Source 기반 알림 설정 목록 응답."""
    settings: List[SourceNotificationSettingResponse]
    total_count: int


class NotificationLogResponse(BaseModel):
    """FX 은행 가격알림 발송 히스토리 1건 (사용자용).

    SourceNotificationLogResponse 미러. condition/threshold는 히스토리 완전판 보강(2026-07-15)
    이후 발송 row에만 존재 → old row 하위호환 Optional. sent_at은 main.py builder에서
    crud.to_kst_isoformat() KST ISO8601로 채움 (naive UTC 직렬화로 iOS 디코더와 어긋나는 것 회피).
    """
    id: int
    setting_id: Optional[int] = None
    bank: str
    currency: str
    condition: Optional[str] = None  # "above" | "below" (보강 이전 old row는 None)
    threshold: Optional[float] = None
    rate: float                       # 발화 시점 환율 (triggered rate)
    sent_at: str  # KST ISO 8601


class NotificationLogsListResponse(BaseModel):
    """FX 은행 가격알림 발송 히스토리 목록 응답 (최신순)."""
    logs: List[NotificationLogResponse]
    total_count: int  # 반환된 페이지 길이 (cap된 '최근 N건'; 전체 카운트 아님)


class SourceNotificationLogResponse(BaseModel):
    """Source 기반 알림 발송 히스토리 1건 (사용자용).

    source log는 condition/threshold/triggered_rate를 inline 보존 → 설정 삭제 후에도
    안전. `type` 필드 없음(source-scoped endpoint라 중복; 향후 comparison은 별 record).
    sent_at은 main.py builder에서 crud.to_kst_isoformat() KST ISO8601로 채움
    (from_attributes 미사용 — naive UTC 직렬화로 iOS 디코더와 어긋나는 것 회피).
    """
    id: int
    setting_id: Optional[int] = None
    source: str
    asset: str
    condition: str  # "above" or "below"
    threshold: float
    triggered_rate: float
    sent_at: str  # KST ISO 8601


class SourceNotificationLogsListResponse(BaseModel):
    """Source 기반 알림 발송 히스토리 목록 응답 (최신순)."""
    logs: List[SourceNotificationLogResponse]
    total_count: int  # 반환된 페이지 길이 (cap된 '최근 N건'; 전체 카운트 아님)


# ============================================================
# Comparison Alerts: 비교 알림 스키마 (ADR-037 S3)
# ============================================================

class ComparisonAlertRequest(BaseModel):
    """비교 알림 생성 요청. tab-scope/left≠right 검증은 source_registry.validate_comparison_alert
    (400) — 여기선 형식만. threshold는 KRW 원 단위(Decision D), diff_type/operator enum 422."""
    tab: str = Field(min_length=1)
    left_source: str = Field(min_length=1)
    left_asset: str = Field(min_length=1)
    right_source: str = Field(min_length=1)
    right_asset: str = Field(min_length=1)
    diff_type: str
    operator: str
    threshold: float
    is_enabled: bool = True
    repeat_interval_sec: Optional[int] = Field(default=None)   # B2 (ADR-036): null=once

    @field_validator("diff_type")
    @classmethod
    def _validate_diff_type(cls, v: str) -> str:
        if v not in ("signed", "absolute"):
            raise ValueError("diff_type must be 'signed' or 'absolute'")
        return v

    @field_validator("operator")
    @classmethod
    def _validate_operator(cls, v: str) -> str:
        if v not in ("gte", "lte"):
            raise ValueError("operator must be 'gte' or 'lte'")
        return v

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)


class ComparisonAlertUpdateRequest(BaseModel):
    """비교 알림 수정 (PUT — A4 편집: is_enabled + repeat_interval_sec + threshold + operator.

    pair(소스 조합)/diff_type 변경은 여전히 삭제+재생성 (dedup·canonical·signed 방향성·히스토리
    해석 복잡도 회피 — ADR-037 A4). threshold/operator(방향)만 편집 → 재조정 워크플로 지원.
    repeat_interval_sec '미제공' vs '명시적 null(=once)' 구분은 main.py가 model_fields_set으로
    판단. threshold/operator는 미제공=None(값으로서 None 없음)이라 is-not-None으로 구분."""
    is_enabled: Optional[bool] = None
    repeat_interval_sec: Optional[int] = Field(default=None)
    threshold: Optional[float] = None       # A4: 미제공=None / 값 변경 시 §7 리셋 (signed는 음수 허용)
    operator: Optional[str] = None          # A4: 미제공=None / 'gte'|'lte' (방향 편집)

    @field_validator("repeat_interval_sec")
    @classmethod
    def _validate_repeat_interval(cls, v: Optional[int]) -> Optional[int]:
        return _validate_repeat_interval_sec(v)

    @field_validator("operator")
    @classmethod
    def _validate_operator(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("gte", "lte"):
            raise ValueError("operator must be 'gte' or 'lte'")
        return v


class EntitlementsResponse(BaseModel):
    """GET /api/entitlements — krx_visible 단일 신호 (ADR-038 Decision 1, Open 1 확정: 신규 endpoint).

    premium PENDING이면 503 대신 200 + premium_pending=true (read API — fail-closed
    krx_visible=false, 클라는 retry_after_seconds 후 재요청. codex Q2 합의 2026-07-08)."""
    krx_visible: bool
    premium_active: bool = False
    premium_pending: bool = False
    retry_after_seconds: Optional[int] = None


class ComparisonAlertResponse(BaseModel):
    """비교 알림 설정 응답."""
    id: int
    user_id: str
    tab: str
    left_source: str
    left_asset: str
    right_source: str
    right_asset: str
    diff_type: str
    operator: str
    threshold: float
    is_enabled: bool
    triggered: bool
    repeat_interval_sec: Optional[int] = None
    last_notified_spread: Optional[float] = None
    created_at: str  # KST ISO 8601
    updated_at: Optional[str] = None


class ComparisonAlertsListResponse(BaseModel):
    alerts: List[ComparisonAlertResponse]
    total_count: int


class ComparisonNotificationLogResponse(BaseModel):
    """비교 알림 발송 히스토리 1건 — 발화 시점 스냅샷 (left/right rate + spread + observed_at)."""
    id: int
    setting_id: Optional[int] = None
    tab: str
    left_source: str
    left_asset: str
    right_source: str
    right_asset: str
    diff_type: str
    operator: str
    threshold: float
    left_rate: float
    right_rate: float
    spread: float
    left_observed_at: Optional[str] = None   # KST ISO 8601 (stale 설명 — ADR-037 codex B3)
    right_observed_at: Optional[str] = None
    is_repeat: bool
    sent_at: str  # KST ISO 8601


class ComparisonNotificationLogsListResponse(BaseModel):
    logs: List[ComparisonNotificationLogResponse]
    total_count: int  # 반환된 페이지 길이 (cap된 '최근 N건')


# ============================================================
# News: 뉴스 피드 스키마
# ============================================================

class NewsItem(BaseModel):
    """개별 뉴스 아이템"""
    id: str
    title: str
    link: Optional[str] = None       # 원문 URL (정상 경로에서 non-null 기대)
    source: str                      # einfomax
    content_type: str = "external_link"  # "external_link" | "report_pdf"
    published_at: str                # ISO 8601


class NewsMetadata(BaseModel):
    """뉴스 API 메타데이터"""
    returned_count: int  # 이번 응답에 포함된 기사 수
    window_hours: float
    responded_at: str    # ISO 8601


class NewsResponse(BaseModel):
    """뉴스 API 응답"""
    news: List[NewsItem]
    metadata: NewsMetadata
