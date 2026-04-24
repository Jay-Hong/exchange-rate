# USDT Phase 1 Design

> Status: design locked, ready for implementation
> Updated: 2026-04-23
> Related: [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md)

## Goal

Phase 1의 목표는 `테더 탭 데이터 피드`를 안정적으로 추가하는 것이다.

이번 단계에서 확정하는 범위:

- 거래소 5종 `USDT/KRW` 수집 (`source_rates` 테이블에 저장)
- 기존 `/api/rates`, `/api/rates/{currency}`, WebSocket `rates` 배열에 `usdt-krw` 엔트리 포함
- source 기반 단일 소스 알림
- source registry 도입
- 알림 히스토리 서버 로그 저장

이번 단계에서 구현하지 않는 범위:

- KRX 미국달러선물 (Phase 2)
- 테더 그래프 API (`/api/graph/usdt-krw`) (Phase 2)
- 비교 알림 구현 (Phase 3, 스키마만 잠금)
- 반복형 알림
- 서버 측 알림 히스토리 조회 API
- 기존 `notification_settings` / `bank_exchange_rates` 세계의 대수술

## Phase 1 Core Decisions

1. 새 source 기반 실시간 데이터는 `source_rates`에 저장한다.
2. 기존 `investing_exchange_rates`, `bank_exchange_rates`, `notification_settings`는 유지한다.
3. **테더 탭 전용 API/WebSocket 블록은 만들지 않는다.** 기존 `/api/rates`, `/api/rates/{currency}`, WebSocket `rates` 배열을 확장한다.
4. 기존 API 필드명 (`bank`, `currency`)은 유지한다. 응답 시 `source → bank`, `asset → currency` 어댑터 변환.
5. 단일 소스 알림은 기존 `notification_settings` 확장 대신 `source_notification_settings`로 분리.
6. 비교 알림은 `left_source/left_asset/right_source/right_asset` 4컬럼 구조 (스키마만, 구현은 Phase 3).
7. 알림 정책은 전체 1회성 (기존 bank 알림과 동일).
8. 알림 히스토리는 서버 로그 저장 + 단말 로컬 저장 이중화, 서버 조회 API는 Phase 1 미구현.

## Recommendation

### Baseline source (클라이언트에서 처리)

테더 탭의 기준 source는 **사용자 정렬 순서의 맨 위 항목**이다. 기존 달러/엔화/유로 탭과 동일한 방식이며, 별도 "기준 소스 변경 UI 토글"은 두지 않는다.

이유:

- iOS `ExchangeRateViewModel.displayState` `filtered.first`, Android `BankPreference` `orderedRates.firstOrNull()` — 기존 두 앱이 이미 "맨 위 = 기준"으로 동작
- 사용자가 원하는 source를 맨 위로 드래그하여 기준을 변경 (`BankCustomizeSheet` 패턴)
- 서버는 원시 rate만 제공, summary/diff 계산은 클라이언트 책임

기본 정렬 순서는 `SourceRegistry.sort_order` (인베스팅 → KB → 하나 → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗). 사용자 재배열 값이 저장되면 다음 세션에도 유지.

### Graph scope

Phase 1에서는 그래프 API를 구현하지 않는다.

이유:

- Phase 1 핵심은 실시간 비교와 단일 알림
- 거래소 데이터는 수집 후 유의미한 그래프가 나올 만큼 쌓이려면 시간 필요
- 그래프는 DXY와 동일한 rollup 전략으로 Phase 2에 추가

## Source Registry

새 source 기반 세계의 단일 truth source는 `app/source_registry.py`로 둔다.

이 모듈은 크롤러 전용이 아니라 아래 모두에서 사용된다.

- 크롤러
- CRUD
- API 어댑터 레이어 (legacy shape 변환)
- 단일 소스 알림
- 향후 비교 알림

### Registry goals

- canonical key를 코드 상수로 관리 (내부 helper only)
- 표시명, 분류, 정렬 순서를 한 곳에서 관리
- 기준 정렬 순서 제공 (앱에서 기본값으로 사용)

### Recommended shape

```python
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceDefinition:
    source: str
    asset: str
    display_name: str
    category: str                        # "exchange" | "reference" | "derivative"
    sort_order: int
    phase1_enabled: bool = True
```

`canonical_key`는 dataclass 필드로 두지 않고 `f"{source}:{asset}"`로 필요 시 생성.

### Phase 1 registry entries

기본 표시 순서는 `인베스팅 → KB → 하나 → (미국달러F) → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗`.

| source | asset | category | display_name | sort_order | phase1_enabled |
| --- | --- | --- | --- | ---: | --- |
| `investing` | `usd-krw` | `reference` | `인베스팅` | 10 | True |
| `kb` | `usd-krw` | `reference` | `국민은행` | 20 | True |
| `hana` | `usd-krw` | `reference` | `하나은행` | 30 | True |
| `krx` | `usd-krw-futures` | `derivative` | `미국달러F` | 40 | **False** (Phase 2) |
| `upbit` | `usdt-krw` | `exchange` | `업비트` | 50 | True |
| `bithumb` | `usdt-krw` | `exchange` | `빗썸` | 60 | True |
| `coinone` | `usdt-krw` | `exchange` | `코인원` | 70 | True |
| `gopax` | `usdt-krw` | `exchange` | `고팍스` | 80 | True |
| `korbit` | `usdt-krw` | `exchange` | `코빗` | 90 | True |

### Recommended helpers

- `get_source_definition(source: str, asset: str) -> SourceDefinition`
- `build_canonical_key(source: str, asset: str) -> str` (내부 helper)
- `get_enabled_sources() -> list[SourceDefinition]`
- `get_usdt_exchange_entries() -> list[SourceDefinition]`
- `is_phase1_source(source: str, asset: str) -> bool`

## Data Model

### 1. `source_rates`

새 source 기반 실시간 시세 저장용 테이블.

이 테이블은 Phase 1에서 거래소 5종 `USDT/KRW`만 저장한다.

`investing`, `kb`, `hana`는 기존 legacy 테이블에서 계속 읽는다.

```sql
CREATE TABLE source_rates (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    asset       TEXT NOT NULL,
    rate        REAL NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_source_rates_source_asset_ts
ON source_rates (source, asset, timestamp);
```

### Notes

- canonical key는 DB에 저장하지 않는다.
- volume, quoteVolume, best bid/ask는 1차에서 저장하지 않는다.
- 저장 정책은 기존 bank/investing와 동일하게 `변경 시에만 INSERT`.

### Retention policy

- raw 데이터는 **10일** 보관 (기존 은행 데이터 보존 정책과 동일)
- 10일 이상 오래된 행은 기존 cleanup job 패턴에 맞춰 자동 삭제
- Phase 2에서 장기 그래프가 필요해지면 raw retention을 늘리지 않고 DXY와 동일한 rollup 전략 (realtime → hourly → daily) 적용

### 2. `source_notification_settings`

Phase 1의 단일 소스 알림용 새 테이블.

기존 `notification_settings`를 건드리지 않는 이유:

- 기존 스키마는 `bank + currency` 중심
- Enum, CRUD, API 계약이 모두 은행 세계에 맞춰져 있음
- 새 source 기반 세계를 억지로 넣으면 Decision E와 충돌

```sql
CREATE TABLE source_notification_settings (
    id                  SERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL,
    source              TEXT NOT NULL,
    asset               TEXT NOT NULL,
    condition           TEXT NOT NULL,    -- above | below
    threshold           REAL NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    triggered           BOOLEAN NOT NULL DEFAULT FALSE,
    last_notified_at    TIMESTAMPTZ NULL,
    last_notified_rate  REAL NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_source_notification_settings_source_asset
ON source_notification_settings (source, asset);
```

### Idempotency rule

기존 `notification_settings`와 동일하게 아래 조합은 중복 생성하지 않는다.

- `user_id`
- `source`
- `asset`
- `condition`
- `threshold`

### 3. `source_notification_logs`

알림 발송 히스토리 (운영 추적 + 향후 조회 API 대비).

```sql
CREATE TABLE source_notification_logs (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    setting_id      INTEGER NULL,       -- 설정 삭제 후에도 로그 유지
    source          TEXT NOT NULL,
    asset           TEXT NOT NULL,
    condition       TEXT NOT NULL,
    threshold       REAL NOT NULL,
    triggered_rate  REAL NOT NULL,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    error_message   TEXT NULL,
    sent_at         TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_source_notification_logs_user
ON source_notification_logs (user_id, sent_at);
```

기존 `notification_logs`와 별도 테이블로 분리 (Decision E 일관성).

### 4. `comparison_alerts` — Phase 3 draft only

Phase 1에서는 구현하지 않지만, source key 체계를 고정하기 위해 스키마 초안을 잡아 둔다.

구조화된 4컬럼 방식 (문자열 canonical key 파싱 대신):

```sql
CREATE TABLE comparison_alerts (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    left_source     TEXT NOT NULL,
    left_asset      TEXT NOT NULL,
    right_source    TEXT NOT NULL,
    right_asset     TEXT NOT NULL,
    diff_type       TEXT NOT NULL,   -- signed | absolute
    operator        TEXT NOT NULL,   -- gte | lte
    threshold       REAL NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    triggered       BOOLEAN NOT NULL DEFAULT FALSE,
    last_notified_at TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);
```

이 설계의 장점:

- 문자열 파싱 불필요 (`"upbit:usdt-krw"` 분해 필요 없음)
- 모든 소스 타입을 동일한 방식으로 참조 (아래 "지원 조합" 참조)
- 쿼리 시 인덱스 활용 용이

### 지원 조합 (모든 소스 타입 간 자유 비교)

`left_source + left_asset`과 `right_source + right_asset`은 DB 스키마상 타입 제약이 없으므로, Phase 1~2에서 수집하는 모든 소스 타입 간 비교가 가능하다. 비교 알림 관점에서는 "bank"와 "source"가 별도 세계가 아니라 **모두 동일한 `source` 값**으로 표현된다 (`kb`, `hana`, `upbit`, `investing`, `krx` 등 모두 source 식별자).

| 조합 타입 | left 예시 | right 예시 | 시나리오 |
| --- | --- | --- | --- |
| exchange ↔ exchange | `upbit:usdt-krw` | `bithumb:usdt-krw` | 거래소 간 스프레드 |
| exchange ↔ reference | `bithumb:usdt-krw` | `investing:usd-krw` | 김프 |
| exchange ↔ bank | `upbit:usdt-krw` | `kb:usd-krw` | 실환전 대비 USDT 가격 |
| **bank ↔ bank** | `kb:usd-krw` | `hana:usd-krw` | 은행 간 환율 차이 |
| **reference ↔ bank** | `investing:usd-krw` | `kb:usd-krw` | 기준환율 대비 은행 스프레드 |
| **exchange ↔ derivative** (Phase 2) | `bithumb:usdt-krw` | `krx:usd-krw-futures` | USDT vs KRX 선물 |
| **bank ↔ derivative** (Phase 2) | `kb:usd-krw` | `krx:usd-krw-futures` | 은행 vs KRX 선물 |
| **reference ↔ derivative** (Phase 2) | `investing:usd-krw` | `krx:usd-krw-futures` | 기준환율 vs 선물 |

### Phase 3 구현 시 필요한 인프라 (스키마 외)

**스키마가 열려 있다고 해서 구현 준비가 끝난 것은 아니다**. 다음 두 인프라가 Phase 3에서 반드시 추가되어야 한다.

#### A. Unified rate lookup

현재 rate 조회 경로는 3개로 분리되어 있다:

- `bank_exchange_rates` → `select_latest_bank_rates_from_db()`
- `investing_exchange_rates` → `select_a_latest_investing_rate_from_db()`
- `source_rates` → `get_latest_source_rate()`

비교 알림 평가는 `left_source+left_asset`과 `right_source+right_asset` 각각에 대해 현재 값을 조회해야 하므로, **세 테이블 모두를 투명하게 조회하는 통합 함수**가 필요하다:

```python
def get_latest_rate_unified(db, source: str, asset: str) -> Optional[float]:
    """source+asset으로 어느 세계든 조회. 조회 순서:
    1. source_rates (새 source 세계, usdt-krw 등)
    2. bank_exchange_rates (source=bank, asset=currency)
    3. investing_exchange_rates (source="investing", asset=currency)
    4. [Phase 2] derivative/KRX 테이블 (추가 예정)
    """
```

Phase 2에서 KRX 달러선물이 도입되면 이 함수에 KRX 조회 경로를 추가한다.

#### B. Unified trigger path

현재 알림 트리거 경로는 세계별로 분리되어 있다:

- `bank_exchange_rates` / `investing_exchange_rates` 변경 → `process_rate_alerts()`
- `source_rates` 변경 → `process_source_rate_alerts()`

비교 알림은 **left/right 중 어느 쪽이 변해도 조건 재평가**가 필요하다. 예: "빗썸 USDT − KB USD ≥ 8원" 알림은 빗썸이 바뀌어도, KB가 바뀌어도 평가되어야 한다. 따라서 두 경로 모두에서 공통 `process_comparison_alerts()`를 호출해야 한다:

```text
bank/investing rate 변경
  ├─ process_rate_alerts()  (기존 단일 bank 알림)
  └─ process_comparison_alerts(changed_source, changed_asset, new_rate)  [신규]

source rate 변경 (usdt_sources 크롤러)
  ├─ process_source_rate_alerts()  (기존 단일 source 알림)
  └─ process_comparison_alerts(changed_source, changed_asset, new_rate)  [신규]
```

`process_comparison_alerts`는 변경된 소스가 포함된 모든 활성 comparison_alert를 찾고, 반대편 소스의 현재 값을 `get_latest_rate_unified`로 조회해 조건을 평가한다.

#### C. KRX (Phase 2) 도입 시 재검토 지점

Phase 2에서 KRX 미국달러선물이 `source_rates` 또는 별도 테이블에 추가되면:

- KRX 저장 위치에 따라 `get_latest_rate_unified` 분기 추가
- KRX 변경 시 트리거 경로 결정 (`source_rates`라면 usdt_sources 크롤러 패턴, 별도 테이블이면 KRX 크롤러에서 직접 호출)
- KRX 재배포 권리 문제로 KRX 참여 비교 알림의 공개 허용 여부 별도 결정 필요할 수 있음

**구조적 권장 방향**: 기존 bank/investing 세계가 동결 상태로 유지되고 (Decision E) 새 소스는 모두 `source_rates`로 수렴하는 경로가 일관적이다. 이 가정이 유지되면 `get_latest_rate_unified`는 장기적으로 **`source_rates` + legacy tables 2갈래**만 커버하면 충분해 복잡도가 제한된다. KRX도 이 방향으로 `source_rates`에 합류하는 것을 1순위로 검토한다.

## API Changes

### 원칙: 기존 API 확장, 새 API 최소화

테더 탭을 위해 **새 엔드포인트를 만들지 않는다.** 기존 API를 확장하여 USDT 데이터를 포함시킨다.

### 1. `/api/rates` 확장

기존:

```python
# crud.py:39
SUPPORTED_CURRENCY_PAIRS = ["eur-krw", "jpy-krw", "usd-krw"]

# crud.py:329
def get_all_rates_flat(db):
    all_rates = []
    for pair in SUPPORTED_CURRENCY_PAIRS:
        investing_data = select_a_latest_investing_rate_from_db(db, pair)
        if investing_data:
            all_rates.append(investing_data)
        bank_data = select_latest_bank_rates_from_db(db, pair)
        all_rates.extend(bank_data)
    return all_rates
```

확장:

```python
def get_all_rates_flat(db):
    all_rates = []
    # 기존 investing + bank 데이터
    for pair in SUPPORTED_CURRENCY_PAIRS:
        investing_data = select_a_latest_investing_rate_from_db(db, pair)
        if investing_data:
            all_rates.append(investing_data)
        bank_data = select_latest_bank_rates_from_db(db, pair)
        all_rates.extend(bank_data)
    # 추가: source_rates를 legacy shape로 변환
    source_data = get_source_rates_as_legacy_format(db)
    all_rates.extend(source_data)
    return all_rates


def get_source_rates_as_legacy_format(db):
    """source_rates를 기존 bank/currency shape로 변환해서 반환."""
    records = ...  # source_rates의 각 (source, asset) 조합별 최신 1건
    return [
        {
            "currency": r.asset,     # asset → currency
            "bank": r.source,        # source → bank
            "rate": r.rate,
            "timestamp": to_kst_isoformat(r.timestamp)
        }
        for r in records
    ]
```

### 2. `/api/rates/{currency}` 확장

`/api/rates/usdt-krw`가 작동하도록 `get_rates_by_currency()`에 같은 패턴 추가.

### 3. WebSocket `rates` 배열 자동 확장

`build_rates_payload()`는 `get_all_rates_flat()`을 호출하므로, CRUD 확장만으로 WebSocket payload에도 USDT 엔트리가 자동 포함된다. 별도 WebSocket 블록 추가 작업 없음.

### 응답 엔트리 형식 (변경 없음, 기존 그대로)

```json
{
  "currency": "usdt-krw",
  "bank": "upbit",
  "rate": 1491.0,
  "timestamp": "2026-04-23T15:00:06+09:00"
}
```

- 기존 클라이언트는 unknown currency(`usdt-krw`)를 무시하거나 필터링하면 됨
- 웹 템플릿 (`templates/index.html`)은 `currencies = ['usd-krw', 'jpy-krw', 'eur-krw']`로 **하드코딩**되어 있어 영향 없음
- 모바일 앱이 USDT를 표시하려면 클라이언트 코드 변경 필요 (의도된 작업)

### 4. 새 API: 단일 소스 알림 전용

기존 `/api/notification-settings`와 별도 endpoint family:

```text
POST   /api/source-notification-settings
GET    /api/source-notification-settings
PUT    /api/source-notification-settings/{setting_id}
DELETE /api/source-notification-settings/{setting_id}
```

Request는 `source` + `asset` 구조 사용 (레거시 호환 불필요, 새 API이므로):

```json
{
  "source": "upbit",
  "asset": "usdt-krw",
  "condition": "below",
  "threshold": 1480.0,
  "is_enabled": true
}
```

## Polling Design

### One job, five sources

거래소 5개를 개별 APScheduler job으로 나누지 않고, 단일 `USDT 수집 job`으로 묶는다.

이유:

- 5개 job보다 운영 포인트가 적다
- 부분 실패를 하나의 로그/알림 단위로 묶기 쉽다

### Recommended crawler module

- `app/crawlers/usdt_sources.py`

### Scheduler job

- job id: `task_usdt_sources`
- trigger: `CronTrigger(second='6,16,26,36,46,56', timezone=KST)`
- 기존 broadcast `0,10,20,30,40,50초`보다 약 4초 앞서 실행
- 운영 모드 무관 (24/7 상시, 크립토는 24시간 거래)

### Fan-out inside one job

하나의 job 안에서 거래소 5개를 병렬 fan-out.

권장 수단:

- `concurrent.futures.ThreadPoolExecutor(max_workers=5)`

이유:

- 현재 스케줄러/크롤러 패턴은 sync 중심
- 별도 async crawler 계층을 지금 도입할 필요 없음
- 5개 HTTP GET은 thread fan-out으로 충분

### Request rules

- per-source timeout: 2.0초
- user agent / headers는 거래소별 기본값
- 실패한 source 하나가 전체 job 실패를 만들지 않음
- 성공한 source만 `insert_source_rate_if_changed`로 저장

## Stale Policy — Phase 1에서는 도입하지 않음

**이전 설계 (freshness_seconds 기반 stale 판정)는 폐기됨.**

### 폐기 사유

저장된 `timestamp` 의미와 stale 판정의 요구사항이 일치하지 않는다:

| 항목 | 현재 저장값 | stale 판정에 필요한 값 |
| --- | --- | --- |
| 의미 | 마지막 **값 변경** 시각 | 마지막 **수집 성공** 시각 |
| 생성 조건 | insert-if-changed (가격 변동 시에만 INSERT) | 크롤링 attempt 성공 시마다 heartbeat |

이 두 값의 괴리는 다음 상황에서 오진을 만든다:

- **은행 주말 정지**: investing/kb/hana는 주말 ~48-72h 고시 없음 → 값 변경 없음 → `now - timestamp > freshness_seconds`로는 전부 stale로 오판
- **USDT 저유동성**: 5개 거래소가 동일 가격($1.000 근처)에서 수 분간 유지 가능 → 동일 문제
- **공통 근본 원인**: insert-if-changed 정책에서 "값이 안 바뀐 것"과 "수집이 실패한 것"을 구분할 수 없음

### Phase 1 규칙

1. 서버는 각 엔트리에 `timestamp`(마지막 값 변경 시각)만 제공
2. 클라이언트는 UI에 타임스탬프를 **정보 표시용**으로만 사용 ("마지막 변동 N초 전")
3. stale 배지, 요약값 계산에서의 stale 제외, diff null 처리 등은 **Phase 1 범위에서 구현하지 않는다**
4. 진짜 수집 장애는 관리자 페이지/Telegram 알림에서 별도 탐지

### 나중에 제대로 도입하려면

다음 두 값을 분리 추적해야 한다:

- `last_rate_change_at`: 현재 있는 값 (가격이 실제 바뀐 시점)
- `last_collection_success_at` 또는 `observed_at`: 크롤링이 성공한 시점 (값이 안 바뀌어도 기록)

이때 stale 판정은 **후자 기준**으로만 유효하다. 이 인프라가 갖춰질 때 stale 규칙과 registry 필드를 재도입한다.

## CRUD Boundary

새 source 기반 데이터 접근도 기존 프로젝트 관례를 따라 `app/crud.py`에 둔다.

별도 repository/service 계층을 만들지 않는다.

### Recommended CRUD functions

#### Registry-adjacent helpers

- `get_source_rates_as_legacy_format(db: Session) -> list[dict]`
  → `{currency, bank, rate, timestamp}` shape로 변환

#### `source_rates`

- `insert_source_rate_if_changed(db, source, asset, rate) -> bool`
- `get_latest_source_rate(db, source, asset) -> dict | None`
- `get_latest_source_rates(db) -> list[dict]`
- `delete_old_source_rates(db, days=10) -> int`

#### `source_notification_settings`

- `create_source_notification_setting(...)`
- `get_source_notification_settings(...)`
- `update_source_notification_setting(...)`
- `delete_source_notification_setting(...)`
- `process_source_rate_alerts(db, changed_rates) -> int`

#### `source_notification_logs`

- `create_source_notification_log(...)`

## Alert Design

### Policy

- **Phase 1은 1회성 알림으로 통일**
- 발송 후 `triggered=True`, `enabled=False` (기존 bank 알림과 동일 동작)
- 사용자가 토글 ON → `triggered=False`로 재무장
- 반복형 알림은 Phase 1 이후 확장 (별도 설계 필요)

### FCM payload type

- `type = "source_rate_alert"`

### History

- **서버**: 발송 시 `source_notification_logs`에 기록 (운영 추적 + 향후 조회 API 대비)
- **단말**: FCM 수신 시 앱 로컬 DB에 저장 (사용자 조회/삭제용)
- **Phase 1에서는 서버 히스토리 조회 API를 만들지 않는다**
- 사용자 히스토리 조회는 단말 로컬에서 처리
- 나중에 멀티디바이스 동기화가 필요하면 서버 조회 API 추가

## Comparison Alerts Draft (Phase 3)

Phase 1에서는 구현하지 않지만 스키마 구조만 잠금.

### Canonical expression

- `signed`: `left_rate - right_rate`
- `absolute`: `abs(left_rate - right_rate)`

### Example

- `{left: {source: "bithumb", asset: "usdt-krw"}, right: {source: "investing", asset: "usd-krw"}, signed, gte, 8.0}`
- `{left: {source: "upbit", asset: "usdt-krw"}, right: {source: "investing", asset: "usd-krw"}, absolute, lte, 1.0}`
- `{left: {source: "kb", asset: "usd-krw"}, right: {source: "hana", asset: "usd-krw"}, signed, gte, 3.0}` (은행 간 비교)

## KRX USD Futures — Phase 2 Skeleton

Phase 1에서는 구현하지 않지만, 개념적 자리만 잠금.

- 저장 위치: `source_rates`
- source: `krx`
- asset: `usd-krw-futures`
- category: `derivative`
- registry `phase1_enabled=False`로 등록 (자리만 확보)

### Phase 2 시작 전 별도 결정 필요

- 시세 재배포 권리 (코스콤 계약 여부)
- 최근월물/차근월물/대표월물 선택 기준
- Rollover 정책 (언제 다음 월물로 전환)
- 수집 API (KIS Developers vs KRX Open API vs 기타)
- 그래프/알림 포함 범위

## Decisions Locked in This Document

- **Alert policy**: Phase 1 전체 1회성 알림으로 통일
- **Alert history**: 서버 로그 저장 + 단말 로컬 조회, 서버 조회 API는 Phase 1 미구현
- **Alert logs**: `source_notification_logs` 별도 테이블 (Decision E 일관성)
- **API 확장 방식**: 전용 endpoint 없이 기존 `/api/rates`, `/api/rates/{currency}`, WebSocket `rates` 배열 확장
- **어댑터 규칙**: DB는 `source`+`asset`, API 표면은 `bank`+`currency`
- **비교 알림 스키마**: 구조화된 4컬럼 (left_source, left_asset, right_source, right_asset)
- **Source registry**: category 필드 유지 (metadata only, DB 컬럼 아님)
- **Graph**: Phase 2로 연기

## Open Decisions

없음. 모든 설계 결정이 잠겼다.

테더 탭 API 공개 여부는 `/api/rates` 확장 방식을 채택했으므로 기존 `/api/rates` 공개 정책을 자동으로 승계한다.

## Known Tech Debt

### 1. `/api/rates` response `metadata.banks` / `metadata.currencies` dead fields

**현상**:

- 서버가 `metadata.banks`, `metadata.currencies`를 계산해서 응답에 포함
- iOS (`ExchangeRate.swift:60-65`), Android (`RatesResult.kt:22-30`) 모두 Codable/Serializable로 파싱만 하고 **실제로 읽는 곳 없음** (`updatedAt`만 사용)
- USDT 추가 후에는 `banks`에 `upbit, bithumb` 등이 섞여 의미 불일치 심화

**왜 지금 제거 못 하나**:

- iOS/Android 양쪽 다 **non-optional** 선언
- 서버에서 필드 제거하면 기존 앱 파싱 실패 → 환율 조회 중단

**정리 경로** (별도 작업):

1. iOS/Android USDT 탭 릴리스 시 `banks`/`currencies` 필드를 optional 또는 default empty로 변경
2. 해당 릴리스 확산 대기 (2~4주)
3. 서버에서 두 필드 제거 (`build_rates_payload`, `get_rates_for_mobile`)
4. 또는 장기적으로 `metadata.sources`처럼 v2 필드로 대체

### 2. Android `sortedByBank()` unknown bank 처리

**현상**:

- [`ExchangeRate.kt:88-89`](../android/app/src/main/java/com/jay/fxi/domain/model/ExchangeRate.kt#L88)의 `sortedByBank()`가 `Bank.sortedEntries.indexOfFirst`로 정렬
- unknown bank(예: `upbit`)는 `-1` 반환 → 리스트 맨 앞으로 정렬되는 부작용 가능
- **현재 호출자 0건**이라 실무 영향 없음

**Android USDT 탭 구현 시 주의**:

- 기존 `Bank` enum에 `UPBIT, BITHUMB, COINONE, GOPAX, KORBIT` 추가 금지 (의미적으로 잘못)
- 대신 별도 `Source` 모델 도입 (서버의 `SourceDefinition` 패턴과 일관)

## Implementation Order

이 문서가 승인되면 구현은 아래 순서로 진행한다.

1. `app/source_registry.py` 추가
2. `models.py`에 `SourceRate`, `SourceNotificationSetting`, `SourceNotificationLog` 추가
3. DB 마이그레이션 (테이블 생성)
4. `crud.py`에 source 기반 CRUD + `get_source_rates_as_legacy_format` + `delete_old_source_rates` + alert 처리 추가
5. `crud.py`의 `get_all_rates_flat()`, `get_rates_by_currency()`에 source_rates 병합 로직 추가
6. `app/crawlers/usdt_sources.py` + 수집 scheduler job 추가 (`task_usdt_sources`)
7. **source_rates cleanup scheduler job 추가** (기존 `cleanup_old_bank_data` 패턴 재사용, 10일 초과 데이터 매일 삭제)
8. `/api/source-notification-settings` 추가
9. WebSocket payload 변경 없음 (기존 `rates` 배열 자동 확장)
10. 통합 테스트: `/api/rates`, `/api/rates/usdt-krw`, WebSocket `rates` 배열에 USDT 포함 확인
11. 운영 검증: cleanup job이 10일 초과 데이터를 실제로 삭제하는지 확인
