# KRX Close Snapshot PR plan

> KRX 미국달러선물의 CF 15:45 / CM 06:00 단일가 종가 누락 보강.
> Phase B.2와 독립 트랙 (ADR-027 REST fallback 계열).
> Codex/Claude 합의 (2026-05-15, 7라운드).

## 1. 문제 정의

운영 EC2 로그 + DB 7~14일치 집계 실측:

**CF 정규세션 종가 (15:45)** — 7일 중 5일 누락 (71%):

| 날짜 | 정규장 마지막 KRX 저장 |
|---|---|
| 2026-05-15 | 15:34:51, 1500.9 ❌ 누락 |
| 2026-05-14 | 15:45:01, 1490.6 ✓ |
| 2026-05-13 | 15:35:01, 1490.6 ❌ |
| 2026-05-12 | 15:45:01, 1488.4 ✓ |
| 2026-05-11 | 15:34:58, 1472.2 ❌ |
| 2026-05-08 | 15:34:59, 1470.4 ❌ |
| 2026-05-07 | 15:34:49, 1452.3 ❌ |

**Cross-check (오늘 5/15)**: DB 마지막 15:34:51 1500.9 vs KIS REST CF 18:26 KST 응답 1500.200 — 종가 누락 확정.

**CM 야간세션 종가 (06:00)** — 8일 중 1일 누락 (12.5%):

| 날짜 | 05:45~06:05 저장 패턴 |
|---|---|
| 2026-05-15 | 06:00:01 1492.4 ✓ |
| 2026-05-14 | 06:00:01 1489.7 ✓ |
| 2026-05-13 | 06:00:01 1492.2 ✓ |
| 2026-05-12 | 05:49:41 1474.0 ❌ |
| 2026-05-09 | 06:00:01 1463.4 ✓ |
| 2026-05-08 | 06:00:01 1457.8 ✓ |
| 2026-05-07 | 06:00:01 1445.6 ✓ |
| 2026-05-05 | 06:00:01 1474.6 ✓ |

CM이 CF보다 빈도 낮지만 zero가 아니며, 다음 정규장까지 2.5시간 stale 영향 큼.

## 2. 원인 분석

KIS WebSocket 운영 로그 패턴 (15:35~15:45 / 05:50~06:00 단일가 입찰 구간):

1. **체결 tick 멈춤**: trade_age가 25초 → 565초까지 증가. 단일가 입찰 마감 전까지 KIS가 체결 frame 거의 미발화.
2. **Quote tick은 계속**: 호가 frame은 정상 수신. 그러나 `KrxDbWriter`는 호가를 의도적으로 무시 (호가가 체결보다 19× 빈도 + 매도호가1을 대표값으로 잘못 저장 위험).
3. **15:45:00 / 06:00:00 boundary**: `get_active_session` 결과 None → `_run_session` 즉시 disconnect.
4. **결정론적 누락 조건**: KIS가 종가 체결 tick을 15:45:00~15:45:00.X 사이에 보내야 잡히는데, 단일가 마감 후 발송이 KIS 정책상 누락되거나 지연되는 case가 다수.

추가 효과 — **insert-if-changed dedup**: 종가가 마지막 정규 체결가와 동일하면 DB row 미생성 → Redis latest도 갱신 안 됨 → 사용자 체감 "종가 row 없음".

WS grace drain만으로는 부족: KIS server-side 미발화 case가 다수라 시간 연장 무효.

## 3. 해결책 개요

**1차 PR — REST close snapshot only** (primary 해결책):
- 단일가 마감 + 60초 안전 margin 후 KIS REST 1~3회 호출
- 응답을 종가로 confirm + DB/Redis 갱신
- 단순/안전/검증 가능. WS 미발화 case를 보정하는 primary path. REST 응답 실패/이상값은 retry + sanity check로 격리.
- KIS REST 종가성 응답은 운영 측정으로 검증됨 (CF 18:26 KST 1500.200 / CM 18:39 KST 1500.10), 단 sample size 작음 → 운영 단계 추가 검증.

**Primary correctness target — Redis latest**:

- 본 PR의 핵심 성공 기준은 Redis `latest:source:krx:usd-krw-futures` 갱신.
- DB `source_rates`는 close snapshot history를 best-effort로 남기는 보조 저장소.
- 기존 WS path는 매 영업일 15:45:01 / 06:00:01에 wall-clock row를 만들 수 있어, REST row(boundary 15:45:00 / 06:00:00)와 timestamp 차이 1초가 존재 → DB `ORDER BY timestamp DESC` 결과가 official close를 보장하지 않음.
- 사용자 체감 / read path / topic publish는 모두 Redis-first ([ADR-026](DECISIONS.md)) → Redis가 보정되면 사용자 시점 해결.
- DB까지 authoritative하게 만드는 작업 (schema/provenance/ordering 정책)은 본 PR scope 밖.

**2차 PR — WS grace drain** (보조 안전장치):
- WS listen을 boundary 후 +59초 연장 (15:45:59 / 06:00:59까지)
- KIS가 15:45:00.X (X = 5~50초) 지연 발송하는 드문 case 추가 커버
- **반드시 grace tick timestamp도 boundary로 normalize** (DB ORDER BY 일관성)
- 1차 PR 안정 운영 후 효과 측정 + 진입

## 4. 1차 PR 명세 (REST close snapshot only)

### 4.0 구현 진입점 / Controller 구조

**신규 클래스**: `KrxCloseSnapshotController` ([app/crawlers/krx_kis.py](app/crawlers/krx_kis.py) 또는 별 모듈).

**책임 분리** (기존 `KrxRestFallbackController`와 분리):

| Controller | Trigger | 목적 |
|---|---|---|
| `KrxRestFallbackController` (기존) | stale gating (장중 60s+ silence) | 장중 장애 보정 |
| `KrxCloseSnapshotController` (신규) | session boundary 도달 직후 (CF 15:45 / CM 06:00) | 세션 종료 확정 보정 |

**진입 흐름**:

1. `KisFuturesClient._run_session` 또는 `_connect_and_listen`에서 boundary 감지 시점:
   - `get_active_session(now, contract.expiry_date) != session` (15:45/06:00 직후)
   - 현재 disconnect 직전 시점
2. controller에 schedule 요청 — boundary 시점에 capture:
   - `contract` (ContractInfo)
   - `session` ("CF" / "CM")
   - `boundary_at_kst` (datetime)
   - `boundary_at_utc_naive` (datetime)
3. controller가 asyncio task로 retry sequence 시작 (controller가 task lifecycle 관리)
4. retry 내부:
   - REST 호출 (`fetch_kis_futures_quote` with explicit session)
   - Sanity check (±2%)
   - Pass 시 DB insert-if-changed (boundary timestamp, 가격 동일 시 skip) + Redis SET (boundary timestamp KST ISO) + telemetry
   - 첫 성공 후 남은 retry skip
5. controller가 shutdown 시 task cancel/drain (KisFuturesClient.stop()과 연계)

**API skeleton**:

```python
class KrxCloseSnapshotController:
    def __init__(self, ...) -> None:
        self._tasks: set[asyncio.Task] = set()  # KrxRestFallbackController 패턴 mirror

    def schedule_close_snapshot(
        self,
        *,
        contract: ContractInfo,
        session: str,             # "CF" | "CM"
        boundary_at_kst: datetime,
        boundary_at_utc_naive: datetime,
    ) -> None:
        """boundary 감지 시점에 KisFuturesClient가 호출. retry task 시작."""

    async def _retry_sequence(self, ...) -> None:
        """3회 retry — 15:46:00 / 15:46:30 / 15:47:00 (CF) 등.
        첫 성공 시 break.
        """

    async def close(self, timeout: float = 1.0) -> None:
        """shutdown drain — KrxDbWriter 패턴 mirror."""
```

**이점**:

- stale fallback 로직과 책임 분리 (코드 가독성 + 유지보수)
- 단위 테스트 격리 가능 (controller만 mock으로 검증)
- 향후 2차 PR (WS grace drain) 추가 시에도 controller 안에 추가 trigger 분기로 흡수 가능

### 4.1 Trigger timing

| 세션 | 1st call | 2nd retry | 3rd retry |
|---|---|---|---|
| CF | 15:46:00 | 15:46:30 | 15:47:00 |
| CM | 06:01:00 | 06:01:30 | 06:02:00 |

- 첫 성공 시 다음 retry skip
- 모두 실패 시 warning log + 격리 (다음 영업일에 자연 복구)
- KIS API 호출 최대 1~3/day per session — throttling 부담 미미

### 4.2 Saved timestamp (boundary 고정) — DB/Redis 표현 형식 명시

retry 실행 시각(15:46:00~15:47:00 등)이 아닌 **세션 종료 boundary**가 의미적 정확성.

**boundary 명시 생성** — `now.replace(...)` 금지. fixed time을 `datetime(...)`으로 명시 생성:

```python
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# CF: today 15:45:00 KST
boundary_at_kst = datetime(
    today.year, today.month, today.day,
    15, 45, 0, 0,  # hour, minute, second, microsecond — 모두 명시
    tzinfo=KST,
)
# CM: today 06:00:00 KST (calendar day = today)
boundary_at_kst = datetime(
    today.year, today.month, today.day,
    6, 0, 0, 0,
    tzinfo=KST,
)

boundary_at_utc_naive = boundary_at_kst.astimezone(timezone.utc).replace(tzinfo=None)
```

**CM의 calendar day vs business day check 분리** (중요):

| 항목 | 기준 |
|---|---|
| CM close timestamp day | `today` (보통 06:00:00 KST가 찍히는 calendar day) |
| CM business day check | `is_krx_business_day(today - timedelta(days=1))` (야간장 시작일 기준) |

- 예: 토요일 06:00 = 금요일 야간장 종료
- timestamp day는 토요일, 그러나 business day check는 금요일 기준 (정상 snapshot 실행)
- 두 의미를 같은 변수로 합치면 토요일 06:00 case 오판 위험 → 분리 변수로 처리

**DB write** (`SourceRate.timestamp` = `Column(DateTime, default=get_utc_now)`, UTC naive):

```python
crud.insert_source_rate_if_changed(
    db=db, source="krx", asset="usd-krw-futures",
    rate=rest_rate,
    timestamp=boundary_at_utc_naive,  # UTC naive datetime
)
```

**Redis write** (`set_latest_krx_rate_from_sync_job(asset, rate, timestamp)`, KST ISO string):

```python
latest_rates_cache.set_latest_krx_rate_from_sync_job(
    asset="usd-krw-futures",
    rate=rest_rate,
    timestamp=boundary_at_kst.isoformat(),  # "2026-05-15T15:45:00+09:00"
)
```

**Boundary 시각 정의**:

| 세션 | boundary KST | boundary UTC naive (예: 2026-05-15) |
|---|---|---|
| CF | 15:45:00 KST | 06:45:00 (UTC naive) |
| CM | 06:00:00 KST | 전일 21:00:00 (UTC naive) |

**주의**:

- KST aware datetime을 DB에 직접 INSERT 금지 (`SourceRate.timestamp`는 naive). UTC naive로 변환 후 전달.
- `now.replace(hour=15, minute=45, second=0)` 패턴은 microsecond/잔여 field 누락 위험. `datetime(...)`으로 명시 생성 권장.

### 4.3 REST authoritative — Redis unconditional overwrite

- REST close snapshot 성공 시 Redis `latest:source:krx:usd-krw-futures`를 unconditional 덮어쓰기
- 기존 WS row가 단일가 입찰 중간가일 가능성 → REST가 공식 마감 가격이므로 우선
- `set_latest_krx_rate_from_sync_job(asset, rate, timestamp)` 호출 — 기존 helper에 timestamp 비교 로직 없음 (그대로 사용 가능)

### 4.4 DB 정책 (best-effort history)

- 가격이 직전 latest와 **다름** → `insert_source_rate_if_changed` INSERT (timestamp = boundary)
- 가격 **동일** → DB skip + Redis timestamp만 갱신 (사용자에게 "종가 확정 시점까지 최신화" 신호)

**DB latest ordering 한계 (명시)**:

- 기존 WS path가 매 영업일 15:45:01 / 06:00:01에 wall-clock row를 만들 수 있음 (정상 수신 case).
- REST close snapshot row는 boundary timestamp(15:45:00 / 06:00:00)로 1초 더 이른 시각.
- 따라서 DB `ORDER BY timestamp DESC` 결과가 official close를 보장하지 않음 (WS row가 최신으로 식별 가능).
- 본 PR은 Redis latest를 authoritative target으로 보정 — DB ORDER BY는 보조 지표.
- DB까지 official close ordering을 보장하려면 별 PR (schema/provenance/kind column + ordering 정책 변경) 필요. 본 PR scope 밖.

### 4.5 시그니처 변경

```python
def insert_source_rate_if_changed(
    db: Session,
    source: str,
    asset: str,
    rate: float,
    timestamp: Optional[datetime] = None,
) -> bool:
    ...
    record = models.SourceRate(
        source=source, asset=asset, rate=rate,
        timestamp=timestamp or get_utc_now(),
    )
```

- `timestamp=None` 기본값 → 기존 호출자 동작 변화 0 (DB DEFAULT)
- close snapshot만 boundary 시각 전달

### 4.6 Contract resolve — boundary 시점 1회 캡처

- session boundary 감지/스케줄 시점에 contract / session / boundary_at 캡처
- 모든 retry는 **같은 contract** 사용 (재resolve 금지)
- 만기일 06:00 → 07:00 rollover race 방지 (06:01~06:02 retry 시 next month로 swap된 contract 사용하지 않도록)

### 4.7 REST 호출 — explicit session 파라미터

- 15:46 / 06:01 시점에 `get_active_session()` = None
- `fetch_kis_futures_quote(contract=..., session="CF" | "CM", ...)` explicit hardcode
- 자동 판정 의존 금지

### 4.8 Sanity check (±2% threshold)

- REST 응답 가격이 직전 latest 대비 ±2% 이상 차이 시:
  - 해당 retry abort (저장하지 않음)
  - 다음 retry로 넘김
- KRX 미국달러선물 일일 변동성 통상 ±1% 이내 → 2% 차이는 비합리적 신호 (KIS API 응답 이상 또는 stale data 가능성)

### 4.9 휴장일 판정

- **CF snapshot**: `is_krx_business_day(today)` — 일반적 기준
- **CM snapshot**: `is_krx_business_day(today - timedelta(days=1))` — 야간장 시작일 기준 (토요일 06:00 = 금요일 야간 종료 case)
- False면 snapshot skip

### 4.10 만기일 처리

- **1차 PR scope 제외**: 만기일 11:30 expiring CF close snapshot
- 이유: 현재 운영은 만기일 07:00 KST에 next month로 rollover (PR6c-2d-1) → 11:30 시점에 expiring contract 구독 안 됨 → 자연 처리
- 만기일 다음달 CF 15:45 (정규장)는 일반 CF snapshot 적용 (next month contract)
- 만기일 CM 06:00 (전날 야간)은 일반 CM snapshot 적용 (expiring contract, 07:00 swap 전)
- 필요시 별도 관찰 후 만기일 11:30 case 추가 PR

### 4.11 메타 정보 (로그 + Redis telemetry)

DB 스키마 변경 없이 메타 별도 기록:

- `received_at`: 실제 REST 호출 응답 시각
- `attempt`: retry 차수 (1/2/3)
- `reason`: "krx_close_snapshot"
- `session`: "CF" / "CM"

위치:
- logs (`logger.info`): 매 호출 시 1줄
- Redis hash (예: `topic:krx_close_snapshot:stats` 또는 기존 telemetry 활용): counter + last_*

### 4.12 에러 격리

- REST 실패 (network / KIS API error) → silent skip + log warning
- 다음 retry에서 자연 복구
- 모든 retry 실패 → warning + 격리 (시스템 영향 0, 다음 영업일에 자연 복구)
- circuit_breaker 오염 금지 (best-effort 패턴)

## 5. 2차 PR 명세 (WS grace drain)

1차 PR 안정 운영 후 효과 측정 + 진입.

| 영역 | 정책 |
|---|---|
| WS listen 연장 | CF: 15:45:59 KST까지, CM: 06:00:59 KST까지 (boundary +59초) |
| `get_active_session` | **변경 없음** (15:45/06:00 boundary 유지) |
| WS listen loop만 grace 적용 | session 의미는 시스템 전체에 영향 — 분리 필수 |
| Grace tick timestamp | **boundary로 normalize** (CF: 15:45:00, CM: 06:00:00) — DB ORDER BY 일관성 |
| Grace tick DB | 가격 변경 시 INSERT (timestamp = boundary) |
| Grace tick Redis | timestamp = boundary 갱신 |
| REST snapshot 충돌 | REST가 unconditional overwrite (1차 PR 정책 그대로) — grace tick과 REST가 같은 boundary timestamp 공유 |

## 6. 테스트 / 운영 smoke 기준

### 1차 PR 테스트

1. **CF/CM business day 정상 case**: 첫 retry 성공 → DB INSERT (가격 다름) 또는 DB skip (동일) + Redis timestamp 갱신
2. **첫 retry 실패, 2nd 성공**: skip → 2nd 시점 성공 + 같은 contract 사용
3. **모든 retry 실패**: warning log + 시스템 영향 X
4. **Sanity check abort**: ±2% 이상 응답 → 저장 X, 다음 retry 진행
5. **휴장일 skip**: CF today=토요일 → skip / CM today=토요일 (start=금요일) → snapshot 실행
6. **만기일 07:00 swap 직후**: 06:01:00 retry는 boundary 시점 캡처 contract 사용 (next month로 swap된 contract 아님)
7. **explicit session 검증**: `fetch_kis_futures_quote` 호출 시 session="CF"/"CM" 전달 확인
8. **Redis overwrite 정확성**: 기존 latest와 다른 가격 응답 → Redis 갱신 + timestamp = boundary
9. **DB skip 시 Redis timestamp 갱신**: 가격 동일 → DB row 0, Redis timestamp만 boundary로 갱신
10. **에러 격리**: REST 호출 exception → KrxDbWriter / scheduler 영향 X

### 운영 smoke 기준 (1차 PR 배포 후)

**필수 (Primary correctness target — Redis latest)**:

- Redis `latest:source:krx:usd-krw-futures` timestamp가 매 영업일:
  - CF 15:45:00 KST 도달
  - CM 06:00:00 KST 도달
- Redis latest rate가 KIS REST close snapshot 응답 rate와 일치

**보조 (DB history — best-effort)**:

- 가격 변경 case: DB boundary row (timestamp = 15:45:00 / 06:00:00) 생성 확인
- 가격 동일 case: DB row 없음 + Redis timestamp만 갱신 (정상)
- DB `ORDER BY timestamp DESC` 결과는 공식 종가 판정 기준이 아님 (WS row가 최신으로 남을 수 있음 — 정상 동작)

**시스템 검증**:

- KIS API 호출 빈도: 일 최대 6회 (CF 3 + CM 3) — throttling 영향 0
- KrxDbWriter / Redis writer 정상 동작 (기존 기능 회귀 0)
- close snapshot 실패 시 시스템 영향 격리 (logger.warning만, 다음 영업일에 자연 복구)
- 7일 운영 후 영업일 기준 누락률: CF ≤ 1/7, CM ≤ 1/8 baseline 대비 개선 측정

## 7. PR scope / 우선순위

- **Phase B.2와 독립 트랙**: trigger 분리 (PR4)와 운영 보강 (close snapshot)은 다른 목표
- **우선순위**: 사용자 체감 직접 영향 → Phase B.2 PR4보다 위로 (Codex/Claude 합의)
- **PR 분할**: 1차 (REST close snapshot only) → 2차 (WS grace drain) 단계 진입

## 8. 참조

- [ADR-027](DECISIONS.md): KRX REST/stale 정책 (stale-based fallback, 본 plan은 boundary-based 별 trigger)
- [KRX_CANARY.md](KRX_CANARY.md): 운영 runbook (PR 완료 후 확인 절차 추가)
- [krx_kis.py](app/crawlers/krx_kis.py): KrxDbWriter / KrxRedisLatestWriter / fetch_kis_futures_quote
- [kis_futures.py](app/sources/kis_futures.py): get_active_session / is_in_session_end_grace
- [crud.py](app/crud.py): insert_source_rate_if_changed (시그니처 변경 대상)
- [latest_rates_cache.py](app/latest_rates_cache.py): set_latest_krx_rate_from_sync_job (기존 helper 재사용)
