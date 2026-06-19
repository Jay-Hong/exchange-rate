# app/models.py

# 표준 라이브러리
from datetime import datetime, timezone as dt_timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Boolean, Index, Numeric, JSON, Text, func, CheckConstraint

# 로컬 애플리케이션
from app.database import Base


def get_utc_now():
    """UTC 기준 현재 시각 (naive datetime)."""
    return datetime.now(dt_timezone.utc).replace(tzinfo=None)


class InvestingExchangeRate(Base):
    __tablename__ = 'investing_exchange_rates'

    id = Column(Integer, primary_key=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_utc_now)


class BankExchangeRate(Base):
    __tablename__ = 'bank_exchange_rates'

    id = Column(Integer, primary_key=True)
    bank = Column(String, index=True)
    currency = Column(String, index=True)
    rate = Column(Float)
    timestamp = Column(DateTime, default=get_utc_now)

    __table_args__ = (
        Index('ix_bank_currency_timestamp', 'bank', 'currency', 'timestamp'),
    )


class MarketIndexRate(Base):
    """시장 지수 데이터 (DXY 등)"""
    __tablename__ = "market_index_rates"

    id = Column(Integer, primary_key=True)
    instrument = Column(String, nullable=False)  # 'dxy' (향후 다른 지수 확장 가능)
    source = Column(String, nullable=False)  # 'investing' | 'yahoo'
    rate = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=get_utc_now)
    granularity = Column(String, nullable=False, default="realtime")  # 'realtime' | 'hourly' | 'daily'

    __table_args__ = (
        Index('ix_market_index_instrument_ts', 'instrument', 'timestamp'),
        Index('ix_market_index_granularity', 'instrument', 'granularity', 'timestamp'),
        Index('uq_market_index', 'instrument', 'source', 'timestamp', 'granularity', unique=True),
    )


class CrawlerConfig(Base):
    """크롤러 활성화/비활성화 설정 (Phase 1.8)"""
    __tablename__ = "crawler_config"

    id = Column(Integer, primary_key=True)
    crawler_name = Column(String, unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)


# ============================================================
# Phase 2: Firebase Auth + FCM 알림 테이블
# ============================================================

class UserDevice(Base):
    """사용자 기기 정보 (FCM Device Token)"""
    __tablename__ = "user_devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)  # Firebase Auth user_id
    device_token = Column(String, nullable=False, unique=True)  # FCM Device Token (전역 유니크)
    platform = Column(String, nullable=False)  # 'ios' or 'android'
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

    # Note: device_token이 전역 유니크이므로 복합 인덱스는 조회 최적화용으로만 유지


class NotificationSetting(Base):
    """알림 설정"""
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    bank = Column(String, nullable=False)  # 'hana', 'kb', etc.
    currency = Column(String, nullable=False)  # 'usd-krw', etc.
    condition = Column(String, nullable=False)  # 'above', 'below'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True)
    # 멱등성 필드 (중복 알림 방지)
    triggered = Column(Boolean, default=False)
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

    __table_args__ = (
        Index('ix_notification_settings_bank_currency', 'bank', 'currency'),
    )


class NotificationLog(Base):
    """알림 발송 히스토리"""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    setting_id = Column(Integer, nullable=True)  # NotificationSetting.id 참조
    bank = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    rate = Column(Float, nullable=False)
    success = Column(Boolean, default=True)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, default=get_utc_now)


# ============================================================
# USDT Phase 1: Source 기반 새 데이터 모델
# ============================================================
# - 기존 bank/investing 세계와 분리
# - DB 내부: source + asset
# - 레거시 API 응답 시에만 bank + currency로 변환 (어댑터 레이어)

class SourceRate(Base):
    """Source 기반 실시간 시세 (크립토 거래소 등).

    Phase 1: 업비트/빗썸/코인원/고팍스/코빗 USDT/KRW
    Phase 2 예정: KRX 미국달러선물
    """
    __tablename__ = "source_rates"

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False)   # 'upbit', 'bithumb', etc.
    asset = Column(String, nullable=False)    # 'usdt-krw', 'usd-krw-futures'
    rate = Column(Float, nullable=False)
    # §12.9.8 ③ super-lite (2026-06-11): USDT WS row는 exchange event ts(naive UTC)를
    # writer가 명시 전달. default(get_utc_now=save-time)는 미전달 경로(KRX 일부/legacy)만.
    # 혼재: 구 row(super-lite 전)=save-time / 신 USDT WS row=exchange-time — 정상 tick은
    # ≈동일, out-of-order stale만 (의도적으로) 과거 ts로 latest 회피.
    timestamp = Column(DateTime, nullable=False, default=get_utc_now)

    __table_args__ = (
        Index('ix_source_rates_source_asset_ts', 'source', 'asset', 'timestamp'),
    )


class SourceNotificationSetting(Base):
    """Source 기반 단일 소스 알림 설정.

    기존 NotificationSetting(bank + currency)과 별도로 유지한다.
    """
    __tablename__ = "source_notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    asset = Column(String, nullable=False)
    condition = Column(String, nullable=False)   # 'above', 'below'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    triggered = Column(Boolean, default=False, nullable=False)
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=get_utc_now, nullable=False)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    __table_args__ = (
        Index('ix_source_notification_settings_source_asset', 'source', 'asset'),
    )


class SourceNotificationLog(Base):
    """Source 기반 알림 발송 히스토리 (운영 추적용)."""
    __tablename__ = "source_notification_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    setting_id = Column(Integer, nullable=True)   # SourceNotificationSetting.id (설정 삭제 후에도 로그 유지)
    source = Column(String, nullable=False)
    asset = Column(String, nullable=False)
    condition = Column(String, nullable=False)
    threshold = Column(Float, nullable=False)
    triggered_rate = Column(Float, nullable=False)
    success = Column(Boolean, default=True, nullable=False)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, default=get_utc_now, nullable=False)


class SourceDailyRate(Base):
    """v2 장기 그래프 (3m/1y) hot path가 읽는 daily canonical row.

    상세 설계: ADR-034 §3 schema.

    invariant: rate == close (app-level enforce, ADR-034 §10 + §13 monitoring).
    DB CHECK constraint는 보류 — Phase 2d 안정화 후 추가 검토.

    Source별 정책:
    - Bithumb: 공식 24h candle API (backfill + daily refresh append 동일 방법, source_method=bithumb_candlestick_api)
    - Hana: official_historical backfill (mixed) + bank_exchange_rates observed_eod append
    - KRX: KIS daily + A75YMM chain backfill + CF 15:45 close finalizer append
    - Investing: investing_exchange_rates(장기 raw) observed_eod daily rollup (ADR-035 D1, Proposed)

    Numeric(14, 6): 환율/선물/USDT/DXY index 모두 충분 (정수부 8자리 / 소수부 6자리).
    Read path는 Decimal 반환 — helper에서 float() 변환 정책 적용 (app/source_daily_rates.py).
    """
    __tablename__ = "source_daily_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)              # "hana" / "krx" / "bithumb" / "investing"
    asset = Column(String, nullable=False)               # "usd-krw" / "usdt-krw" / "usd-krw-futures" 등
    date_kst = Column(Date, nullable=False)              # canonical KST date (ADR-034 §12)
    rate = Column(Numeric(14, 6), nullable=False)        # invariant: rate == close
    high = Column(Numeric(14, 6), nullable=True)
    low = Column(Numeric(14, 6), nullable=True)
    close = Column(Numeric(14, 6), nullable=False)
    ohlc_quality = Column(String, nullable=False)        # source_ohlc / observed_rollup / close_only (ADR-034 §6)
    close_basis = Column(String, nullable=False)         # 5 values (ADR-034 §6 + ADR-035 investing_observed_eod)
    source_method = Column(String, nullable=False)       # 6 values (ADR-034 §6 + Step 4B krx_openapi_daily)
    contract_code = Column(String, nullable=True)        # KRX 전용 (예: A75606)
    basis_date = Column(Date, nullable=True)             # Hana official backfill 응답 기준일
    published_at = Column(DateTime(timezone=True), nullable=True)  # Hana official 발표 timestamp (다음날 새벽)
    captured_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    metadata_json = Column(JSON, nullable=True)          # pbldSqn / raw response 일부 / diagnostics

    __table_args__ = (
        Index('uq_source_daily_rates', 'source', 'asset', 'date_kst', unique=True),
    )


class SourceHourlyRate(Base):
    """v2 1w 그래프 hot path가 읽는 hourly canonical row (ADR-035 D3).

    source_daily_rates의 1w 대응 — schema mirror, date_kst(Date) → bucket_ts_kst(DateTime).
    bucket_ts_kst = KST 시 정각 floor (예: 2026-06-07 14:00 = 14:00~14:59 KST 관측 rollup).

    invariant: rate == close (app-level enforce, source_daily_rates 동형).
    retention ~14일 (config — 1w(7일) 노출 + 주말/배포지연/boundary 버퍼). source별 raw table retention과 분리.

    Source별 rollup provider (raw → hour bucket): KRX·Bithumb=source_rates / Hana=bank_exchange_rates /
    Investing=investing_exchange_rates (DXY는 market_index_rates.hourly 유지 — D2 동형 별 path).

    Numeric(14, 6) / read path Decimal → helper float() 변환 (app/source_hourly_rates.py).
    """
    __tablename__ = "source_hourly_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)              # "krx" / "hana" / "bithumb" / "investing"
    asset = Column(String, nullable=False)               # "usd-krw" / "usdt-krw" / "usd-krw-futures" 등
    bucket_ts_kst = Column(DateTime, nullable=False)     # KST 시 정각 floor (naive, KST 해석 — date_kst 동형)
    rate = Column(Numeric(14, 6), nullable=False)        # invariant: rate == close
    high = Column(Numeric(14, 6), nullable=True)
    low = Column(Numeric(14, 6), nullable=True)
    close = Column(Numeric(14, 6), nullable=False)
    ohlc_quality = Column(String, nullable=False)        # source_ohlc / observed_rollup / close_only
    close_basis = Column(String, nullable=False)         # source_daily_rates와 동일 enum
    source_method = Column(String, nullable=False)       # source_daily_rates와 동일 enum
    contract_code = Column(String, nullable=True)        # KRX 전용 (예: A75606)
    basis_date = Column(Date, nullable=True)             # provenance (daily schema parity — hourly observed는 보통 None)
    published_at = Column(DateTime(timezone=True), nullable=True)  # provenance (daily schema parity)
    captured_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    metadata_json = Column(JSON, nullable=True)          # point_count / first_ts / last_ts / diagnostics

    __table_args__ = (
        Index('uq_source_hourly_rates', 'source', 'asset', 'bucket_ts_kst', unique=True),
    )


# ============================================================
# P1 atomic-write control plane (PR D / P1b — A1)
# ============================================================

class AtomicWriteControl(Base):
    """P1 atomic writer 3-state control plane (싱글톤 row id=1).

    P1_COMMON_BASE_DESIGN.md §4 control table. legacy/atomic/halt 3-state mode를
    DB 단일 진실 소스로 관리. **A1에서는 read-only infra만** — writer hot path
    미연결(순수 legacy), behavior-change-0. writer가 effective-mode를 consume하는
    enforcement는 A2+. 읽기/계산 helper는 app/atomic_write_control.py.

    이름 'atomic_write_control': 기존 broadcast hot path 개념과 구분 — 이 table은
    broadcast가 아니라 atomic write mode control plane.

    DB CHECK 제약 (control-plane safety constraint):
        리포 첫 DB CHECK. ADR-034 §10 'DB CHECK 보류'(rate==close 같은 data-quality
        case)와 **의도적 divergence** — 싱글톤/enum/non-negative는 data quality가
        아니라 구조적 무결성이고, 깨지면 wrong-mode read → halt 대상이라 DB-level
        fail-closed가 타당. §4가 ADR-034 이후 명시적으로 이 CHECK를 mandate하므로
        governs. (future reader가 ADR-034 선례로 app-level 회귀시키지 말 것.)

        §4 line 43-45 명시 3개만 구현: CHECK(id=1) / requested_mode enum /
        activation_epoch non-negative. monotonicity(activation_epoch/mode_generation)는
        intra-row CHECK로 불가(§4 line 48) → conditional-update
        (UPDATE ... WHERE id=1 AND col < :new + affected==1)로 A2/C6에서 enforce.

    A1→A2 handoff precondition: table-exists ≠ row-exists. 운영 진입점(main.py/backfill)은
        create_all_app_tables로 이 table을 만들지 않음 — migration script만 생성+seed.
        **A2 enforcement (§19 A2 상세)**: activation 전(activation_latched=false) phase는
        row 부재/corruption = **legacy passthrough**(배포≠halt), activation(activation_epoch>0)
        후에만 halt 차단. compute_effective_mode None→HALT는 **진단 라벨 불변**이고 writer
        enforcement 강도만 phase-gated. A2/bootstrap은 row 존재를 가정하지 말 것.
    """
    __tablename__ = "atomic_write_control"

    # 싱글톤 PK. autoincrement=False → PostgreSQL이 SERIAL 아닌 plain INTEGER emit
    # → 명시 id=1 seed가 CHECK(id=1)와 공존.
    id = Column(Integer, primary_key=True, autoincrement=False)
    control_row_format_version = Column(Integer, nullable=False, default=1)   # §5 control row 형식
    target_write_schema_version = Column(Integer, nullable=False, default=1)  # §5 새 write가 emit할 Redis value schema (1=legacy, 2=atomic)
    required_writer_protocol = Column(Integer, nullable=False, default=1)     # §5/§6 preflight 비교 대상
    activation_epoch = Column(Integer, nullable=False, default=0)             # §4 atomic 최초 활성화 이력·schema floor (0=미활성화)
    mode_generation = Column(Integer, nullable=False, default=0)              # §4 fencing token (legacy/halt/atomic 전환마다 증가, activation_epoch와 별개)
    requested_mode = Column(String, nullable=False, default="legacy")         # 'legacy'|'atomic'|'halt' (CHECK enforce)
    activated_at = Column(DateTime, nullable=True)                            # atomic activation 시각 (활성화 전 None — §8 one-shot에서 set; activation_epoch=0과 정합)
    updated_at = Column(DateTime, nullable=False, default=get_utc_now, onupdate=get_utc_now)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_atomic_write_control_singleton"),
        CheckConstraint(
            "requested_mode IN ('legacy', 'atomic', 'halt')",
            name="ck_atomic_write_control_requested_mode",
        ),
        CheckConstraint("activation_epoch >= 0", name="ck_atomic_write_control_activation_epoch"),
    )


class AtomicCutoverControl(Base):
    """P1b C6-1 — cutover/activation 제어 singleton (§15 global bootstrap + §18 lease). dormant.

    A1 `AtomicWriteControl`(writer mode)와 **분리된** cutover-state table — A1 row는 live-read
    (scheduler refresh + admin status + compute_effective_mode)라 컬럼 추가 시 migration-before-model
    위험이 있어 무접촉. C6-1은 schema + idempotent seed만, read/CAS/transition은 C6-2+ (zero live logic).
    monotonicity(bootstrap_generation)는 intra-row CHECK 불가(§4 line 48) → conditional-update로 C6-2 enforce.
    `scripts/migrate_atomic_cutover.py`가 운영(non-test) 유일 생성 경로 (create_all 제외, A1 패턴).
    """
    __tablename__ = "atomic_cutover_control"

    id = Column(Integer, primary_key=True, autoincrement=False)            # 싱글톤 (CHECK id=1)
    cutover_row_format_version = Column(Integer, nullable=False, default=1)
    bootstrap_session_id = Column(String, nullable=True)                   # §18 active session (None pre-bootstrap)
    bootstrap_generation = Column(Integer, nullable=False, default=0)      # §18 generation (monotonic via conditional-update, C6-2)
    bootstrap_status = Column(String, nullable=False, default="idle")      # §15-4 idle|running|failed|verified|completed
    lease_owner = Column(String, nullable=True)                           # §18 migration lease holder (None when unleased)
    lease_expiry = Column(DateTime, nullable=True)                        # lease TTL (None when unleased)
    updated_at = Column(DateTime, nullable=False, default=get_utc_now, onupdate=get_utc_now)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_atomic_cutover_control_singleton"),
        CheckConstraint(
            "bootstrap_status IN ('idle', 'running', 'failed', 'verified', 'completed')",
            name="ck_atomic_cutover_control_status",
        ),
        CheckConstraint("bootstrap_generation >= 0", name="ck_atomic_cutover_control_generation"),
        CheckConstraint("cutover_row_format_version >= 1", name="ck_atomic_cutover_control_format_version"),
        # lease는 owner+expiry 쌍 — 둘 다 NULL(unleased) 또는 둘 다 NOT NULL(leased)
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expiry IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expiry IS NOT NULL)",
            name="ck_atomic_cutover_control_lease_paired",
        ),
    )


class AtomicCutoverAsset(Base):
    """P1b C6-1 — per-asset cutover publish gate state (§15 publish_state + readiness). dormant.

    publish_state=blocked(기본)/ready. ready면 ready_revision_vector(JSON Text)+membership_version 필수
    (payload consistency CHECK — ready인데 readiness 데이터 부재 차단). seed=3 asset blocked
    (insert-if-missing — readiness 후 migration rerun이 state 리셋 못 함). read/transition은 C6-2+.
    """
    __tablename__ = "atomic_cutover_asset"

    asset = Column(String, primary_key=True)                              # usd-krw|jpy-krw|eur-krw (CHECK enum)
    publish_state = Column(String, nullable=False, default="blocked")     # §15 gate: blocked|ready
    ready_revision_vector = Column(Text, nullable=True)                   # readiness 시 effective vector JSON 직렬화 (blocked면 None)
    membership_version = Column(Integer, nullable=True)                   # readiness 시 FX membership version (blocked면 None)
    updated_at = Column(DateTime, nullable=False, default=get_utc_now, onupdate=get_utc_now)

    __table_args__ = (
        CheckConstraint(
            "publish_state IN ('blocked', 'ready')", name="ck_atomic_cutover_asset_publish_state"
        ),
        CheckConstraint(
            "asset IN ('usd-krw', 'jpy-krw', 'eur-krw')", name="ck_atomic_cutover_asset_asset"
        ),
        CheckConstraint(
            "membership_version IS NULL OR membership_version > 0",
            name="ck_atomic_cutover_asset_membership",
        ),
        # publish_state ⟺ readiness payload 일관성
        CheckConstraint(
            "(publish_state = 'blocked' AND ready_revision_vector IS NULL AND membership_version IS NULL) "
            "OR (publish_state = 'ready' AND ready_revision_vector IS NOT NULL "
            "AND ready_revision_vector <> '' AND membership_version IS NOT NULL)",
            name="ck_atomic_cutover_asset_payload_consistency",
        ),
    )
