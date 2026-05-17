# KRX Close Snapshot PR plan

> KRX 미국달러선물의 CF 15:45 / CM 06:00 단일가 종가 누락 보강.
> Phase B.2와 독립 트랙 (ADR-027 REST fallback 계열).
> Codex/Claude 합의 (2026-05-15, 7라운드).

## 0. Status (2026-05-17)

| 단계 | 상태 | Commit | 비고 |
| --- | --- | --- | --- |
| 1차 PR — REST snapshot only | ✅ 완료 + 배포 (2026-05-16 EC2 11:23 KST) | `c0855ff` | §4 명세 + §4.12 11일 baseline 분석 |
| **2차 작업 — WS-first close finalizer + REST fallback** | ✅ **구현 완료 + 배포 (2026-05-17 EC2 17:07 KST)** | **`68b8702..c2fb796`** (Stage 1-5) | §5 명세 + 외부 검토 Codex 10+ round |
| 첫 실측 대기 | 2026-05-18 (월) 15:45 KST CF close + 2026-05-19 (화) 06:00 KST CM close | — | [KRX_CANARY.md](KRX_CANARY.md) checklist 참조 |
| 7일 telemetry 측정 | 2026-05-19 ~ 2026-05-26 (case A/B/C 분포) | — | 결과로 3차 PR scope 결정 |

**2차 작업 Stage 1-5 요약** (분할 + 외부 검토 통과):

| Stage | Commit | files / +lines | 변경 핵심 |
| --- | --- | --- | --- |
| Stage 1 — window helpers | `68b8702` | 2 / +289 | `is_in_close_grace_window` / `is_in_single_price_window` / `compute_close_grace_end_kst` |
| Stage 2 — Redis flag + crud unconditional | `dc678c3` | 4 / +319 | `set/get_krx_close_captured_flag` (best-effort) + `insert_source_rate_unconditional` (예외 전파, 항상 True) |
| Stage 3 — KrxCloseWindowWriter + KrxDbWriter skip | `2ea3cde` | 3 / +957 | F1 fix (DB row 1 보장) + market_time 정책 + setUp 5중 patch (F3 fix) |
| Stage 4 — _retry_sequence env-gated 단순화 | `e141e6a` | 3 / +356 | 3 retry → 1 + 2-step captured flag GET + F2 fix (db_ok AND redis_ok flag SET) + Codex counter invariant |
| Stage 5 — WS grace drain + scheduler 등록 | `c2fb796` | 3 / +22 | `_connect_and_listen` +59s grace + `KrxCloseWindowWriter` handler 등록 |
| **누적** | — | **15 files / +1943** | F1/F2/F3 모두 구조적 해결 |

**검증** (관련 회귀 + 신규 tests):

```bash
python -m pytest tests/test_kis_futures_close_window.py tests/test_krx_close_captured_flag.py \
  tests/test_crud_insert_source_rate_unconditional.py tests/test_krx_close_window_writer.py \
  tests/test_krx_close_snapshot.py tests/test_krx_close_snapshot_controller_fallback.py \
  tests/test_krx_kis.py tests/test_krx_scheduler.py tests/test_tether_topic_trigger.py \
  tests/test_tether_topic_publisher.py -q
# 250 passed, 1 skipped
```

**Rollback (Close finalizer만 격하, KRX 자체는 유지)**:

```bash
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32
cd ~/exchange-rate
grep -q '^KRX_CLOSE_FINALIZER_ENABLED=' .env \
  && sed -i 's/^KRX_CLOSE_FINALIZER_ENABLED=.*/KRX_CLOSE_FINALIZER_ENABLED=false/' .env \
  || echo 'KRX_CLOSE_FINALIZER_ENABLED=false' >> .env
docker compose up -d --force-recreate fastapi
```

⚠️ `restart`는 env_file 변경을 반영하지 않음. **반드시 `up -d --force-recreate fastapi`** ([KRX_CANARY.md](KRX_CANARY.md) 핵심 원칙 5 참조). 효과: KrxDbWriter close-grace skip 미진입 + KrxCloseWindowWriter `__call__` early return + REST 3 retry 그대로 (1차 PR c0855ff 동작 복귀).

---

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

**1차 PR — REST close snapshot only** (deployed c0855ff, 2026-05-15):

- 단일가 마감 + 60초 안전 margin 후 KIS REST 1~3회 호출
- 응답을 종가로 confirm + DB/Redis 갱신
- Primary correctness target: Redis latest unconditional overwrite. DB best-effort history
- 운영 측정 결과 (§4.12): CF capture **2/8 = 25%** / CM capture **8/9 = 88.9%**. CF 75% 누락 + CM 11% 누락의 근본 원인이 우리 cutoff(15:45:00 / 06:00:00 정각 boundary)일 가능성이 11일 baseline 데이터로 강하게 지지됨

**2차 PR — WS-first close finalizer + REST fallback** (정책 전환, 2026-05-17 합의):

- WS listen +59초 grace drain (CF 15:45:59 / CM 06:00:59까지)
- Close grace window 안 frame은 **실제 WS timestamp 그대로**, 가격 동일도 DB + Redis 양쪽 unconditional INSERT/SET (close window writer)
- REST snapshot은 fallback으로 격하 (grace 종료 후 1회 호출, WS captured 시 skip)
- Race 방지: Redis TTL flag `close_captured:krx:{session}:{kst_date}`
- **DB도 종가 확정**: 1차 PR에서 best-effort였던 DB를 종가만큼은 unconditional 보장 (Redis 실패 fallback path + graph daily source)
- Env toggle `KRX_CLOSE_FINALIZER_ENABLED` (default true, 회귀 시 false로 1차 PR 동작 rollback)

**3차 PR 후보** (2차 PR 7일 telemetry 측정 후):

- DB unique constraint, 종가 read API, open auction 보강, REST fallback 제거 검토, Stage E (KRX Redis-first)
- §7.3 참조

**1차 PR 시점 caveat (2026-05-15 작성, 2차 PR로 해소 예정)**:

- "DB까지 authoritative하게 만드는 작업은 본 PR scope 밖" → **2차 PR에서 close 영역만 authoritative로 격상**. 일반 tick path는 여전히 1초 window + insert-if-changed (close 외 영역까지 authoritative는 schema/ordering 정책 정리가 별도 필요 — 3차 PR 영역)

## 4. 1차 PR 명세 (REST close snapshot only)

### 4.0 구현 진입점 / Controller 구조

**신규 클래스**: `KrxCloseSnapshotController` ([app/crawlers/krx_kis.py](app/crawlers/krx_kis.py) 또는 별도 모듈).

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
- DB까지 official close ordering을 보장하려면 별도 PR (schema/provenance/kind column + ordering 정책 변경) 필요. 본 PR scope 밖.

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

### 4.12 1차 PR 설계 근거 — 운영 DB baseline (2026-05-04 ~ 2026-05-15, c0855ff 배포 전 데이터)

c0855ff (commit 2026-05-15 20:05 KST, EC2 deploy 2026-05-16 11:23 KST) **배포 전** 기존 WS-only path 운영 DB 저장 결과 분석. 1차 PR 설계 근거 + 2차 PR scope 정당화 데이터. 모든 수치는 `source_rates` **DB saved row 기준** (insert-if-changed + 1초 debounce 거친 결과, raw WS frame 관측 아님).

**(a) 종가 결정 시점 DB saved row capture rate (decision-point ±60s window)**:

| 결정 시점 | 영업일 capture | rate | denominator |
|---|---|---|---|
| CF_open (08:45) | 8/8 | **100%** (row 11–28건 풍부) | 5/6, 5/7, 5/8, 5/11, 5/12, 5/13, 5/14, 5/15 |
| CM_open (18:00) | 8/8 | **100%** (row 3–18건) | 동일 8 영업일 (야간장 시작 기준) |
| CM_close (06:00) | **8/9** | **88.9%** (정확히 1 row, 모두 06:00:01) | 5/5, 5/7, 5/8, 5/9, 5/12, 5/13, 5/14, 5/15, 5/16 (9 영업 익일). **5/12 누락** (§1 1/8 누락과 정합, §1은 5/15 작성이라 5/16 미포함) |
| **CF_close (15:45)** | **2/8** | **25%** | 5/6, 5/7, 5/8, 5/11, 5/12, 5/13, 5/14, 5/15. capture: 5/12, 5/14만 (모두 15:45:01) / 미캡처: 5/6, 5/7, 5/8, 5/11, 5/13, 5/15 |

→ **CF 종가 75% 누락 (6/8)**. CM 종가 11% 누락 (1/9, 5/12). §1 문제 정의(CF 5/7~5/15 5/7 누락, CM 1/8 누락)와 정합 — denominator 차이는 §4.12가 5/6 + 5/16 추가 포함이라 발생.

**(b) 단일가 매매 시간 DB saved row 패턴 (11일 누적)**:

| Window | DB saved row 수 |
|---|---|
| CF 단일가 진행 (15:35:00 ~ 15:44:59) | 2건 (5/6 15:35:00, 5/13 15:35:01 — 직전 거래 잔여 echo) |
| CF 종가 결정 직후 (15:45:00 ~ 15:45:59) | 2건 (5/12 15:45:01, 5/14 15:45:01) |
| CM 단일가 진행 (05:50:00 ~ 05:59:59) | **0건** |
| CM 종가 결정 직후 (06:00:00 ~ 06:00:59) | 8건 (8 영업 익일 매번 06:00:01) |
| 단일가 진행 시간 2 row 이상 day | **0건** (11일 전체) |

→ KIS WS feed는 단일가 매매 시간 동안 체결 frame 송출 거의 없음 (KRX 규정상 체결 0건). DB 종가 후보 row는 **모두 boundary 정각 또는 +1초**. ※ insert-if-changed + 1초 debounce 적용된 결과라 raw WS frame은 더 많을 가능성 — 2차 PR `close_grace_tick_count` telemetry로 raw 측정 예정.

**(c) CF 누락 원인 가설 데이터 지지**:

- 단일가 시간 DB row 0 패턴이 11일 일관 → 종가 row는 `15:45:00` 정각 또는 그 직후에만 생성
- 정상 capture된 5/12, 5/14는 모두 `15:45:01` → KIS는 종가 결정 직후 1초 안에 frame 송출
- CM 종가 88.9% vs CF 종가 25%의 차이:
  - CM은 종가 결정(06:00) = 야간장 자연 종료 → boundary 직후 frame(실측 모두 06:00:01)이 마지막 frame으로 보존. 종가 결정 후 거래 0이라 cutoff timing과 무관하게 그 1건이 살아남음 (1건 미캡처는 5/11 야간장 종가 case, §1 명시)
  - CF는 종가 결정(15:45) 후 야간장 18:00 시작까지 비활성 → KIS WS session change lifecycle (CF → 휴식 → CM)이 15:45:00에 발생, 그 직후 들어올 `15:45:0X` frame이 우리 listen loop cutoff 시점에 잡히지 않음

→ **사용자 cutoff 가설 ("우리가 15:45:00에 너무 타이트하게 끊어서 15:45:01 frame 못 받음") 강력 지지**. KIS WS 미송신 가설(코덱스 1차 제기)은 단일가 매매 시간 DB row 0 패턴이 KRX 규정 자연 결과라 입증 안 됨. 다만 raw WS frame 측정이 없으므로 case B(KIS 미송신) 0% 단정은 보류 — 2차 PR telemetry로 정량 검증 예정.

**(d) 2차 PR 진입 근거**:

- CF 75% 누락 + CM 11% 누락이 운영 데이터로 입증
- 사용자 cutoff 가설이 데이터로 강하게 지지
- WS grace drain (CF 15:45:59 / CM 06:00:59까지 listen 연장) + close grace window unconditional INSERT 정책이 CF capture rate 대폭 개선 후보
- REST snapshot은 catastrophic backup (WS 자체 단절 + KIS 미송신 시)으로 fallback 격하

### 4.13 에러 격리

- REST 실패 (network / KIS API error) → silent skip + log warning
- 다음 retry에서 자연 복구
- 모든 retry 실패 → warning + 격리 (시스템 영향 0, 다음 영업일에 자연 복구)
- circuit_breaker 오염 금지 (best-effort 패턴)

## 5. 2차 PR 명세 (WS-first close finalizer + REST fallback)

§4.12 11일 baseline 측정 결과 기반 합의안. 4 round 외부 검토(Codex × 4 + Claude 보강) 수렴 (2026-05-17).

**핵심 정책 전환**:

1차 PR이 "REST snapshot primary + DB best-effort"였다면, 2차 PR은 **"WS close grace primary + REST fallback + DB와 Redis 양쪽 종가 확정"**. 사용자 통찰("우리 cutoff 때문") + Codex 우려("KIS WS 미송신 가능성 catastrophic backup")를 통합.

### 5.1 WS close grace drain

| 영역 | 정책 |
|---|---|
| WS listen 연장 | CF: 15:45:59 KST까지, CM: 06:00:59 KST까지 (boundary +59초) |
| `get_active_session` | **변경 없음** (코덱스 강조 — session 의미 보존, 다른 기능 영향 차단) |
| WS listen loop만 grace 적용 | session 판정과 listen loop grace 분리 필수 |

### 5.2 Close grace tick 처리 (close window writer)

| 영역 | 정책 |
|---|---|
| Close candidate window | **CF 15:45:00~15:45:59 / CM 06:00:00~06:00:59** (§4.12 (b) 데이터 기반) |
| 일반 dedup 분리 | `KrxDbWriter._flush_after_window`에서 close window 진입 시 별도 path 분기 |
| **저장 시점 + 빈도** | **Window 종료 시점 (grace 마감)에 last close candidate 1건만** unconditional INSERT/SET. window 안 raw frame N건 들어와도 DB row 1 + Redis SET 1 보장. `close_grace_tick_count`로 raw count 별도 추적, 2건 이상 시 `close_duplicate_count` ++ alert (이상 신호) |
| DB 저장 | **가격 동일해도 INSERT** (insert-if-changed 우회) |
| Redis latest 갱신 | **가격 동일해도 SET** (1차 PR 정책 그대로 유지) |
| 단일가 진행 구간 | **close 확정 제외** — CF 15:35:00~15:44:59 / CM 05:50:00~05:59:59 (§4.12 (b)에서 row 0 패턴 입증). 일반 dedup 유지 + telemetry 카운트만 |

**Timestamp 정의 (close grace tick)** — DB/Redis 표현 형식 명시:

```text
event_at_kst = KIS payload 체결시각 (있으면 그 값, KIS H0CFCNT0/H0MFCNT0 STCK_CNTG_HOUR 등 — payload 검증 후 정확 field 결정)
             | else: WS frame receive 시각 (KrxDbWriter __call__ 진입 시각, fallback)

DB.timestamp  = event_at_kst.astimezone(UTC).replace(tzinfo=None)   # SourceRate.timestamp는 UTC naive
Redis.timestamp = event_at_kst.isoformat()                          # KST ISO string (1차 PR 표현 형식과 동일)
```

→ 사용자 통찰("인위적 boundary 정각 생성 X")과 정합. boundary 정각(15:45:00)은 REST fallback path에만 적용 (§5.3) — WS close grace path는 실제 체결시각 보존. 단 KIS payload 체결시각 field 존재 + 형식은 구현 시점에 `kis_futures.py` payload parser로 검증 (없으면 fallback path로 안전).

### 5.3 REST snapshot fallback 격하

| 영역 | 정책 |
|---|---|
| 호출 횟수 | **3 retry → 1회** (boundary + 60s 단발, grace listen 종료 직후) |
| Skip 조건 | WS close grace가 종가 frame capture 성공 시 REST 호출 자체 skip |
| Race 방지 mechanism | **Redis TTL flag** `close_captured:krx:{session}:{kst_date}` (TTL 1h) — WS path가 SET, REST는 호출 전 GET 후 captured 시 skip |
| 1차 PR 코드 처리 | revert 아닌 fallback path로 격하 — `KrxCloseSnapshotController._retry_sequence` 단순화 (60/90/120s retry 제거) |

### 5.4 Env toggle

| Env | Default | 의미 |
|---|---|---|
| `KRX_CLOSE_FINALIZER_ENABLED` | `true` | 2차 PR 정책 전체 (close grace + close window writer + REST fallback 단순화) 활성. `false` 시 1차 PR (c0855ff) 동작 유지 — 회귀 시 즉시 rollback path |

### 5.5 Telemetry (5 counters)

운영 dashboard에 추가 — case A/B/C 분포 실측 가능 (A: 우리 cutoff / B: KIS 미송신 / C: dedup skip).

| Counter | 의미 |
|---|---|
| `close_grace_tick_count` | Close window 안 WS frame 수신 (raw count, dedup 전) |
| `close_grace_saved` | Close window 안 frame이 DB + Redis INSERT 성공 |
| `close_rest_fallback_used` | WS grace 종료 시점 captured flag 부재 → REST 호출 |
| `close_duplicate_count` | Close window 안 2 frame 이상 발생 (현재 baseline 0건, **이상 신호 alert**) |
| `single_price_window_frame_count` | 단일가 진행 구간(15:35~15:44 / 05:50~05:59) frame 수신 (현재 baseline 11일 누적 2건 — 잔여 echo. 미래 KIS 정책 변경 대응 안전망) |

### 5.6 2차 PR 제외 항목 (3차 PR / 별도 PR 미룸)

| 항목 | 미루는 이유 |
|---|---|
| DB unique constraint `(source, asset, timestamp)` | `source_rates`는 9개 source 공통 schema (USDT/은행/investing 어댑터 path) — KRX 한정 race 위한 변경으로는 blast radius 큼. 사전 검증 + migration + 다른 호출처 conflict handling 검토 필요. 3차 PR 또는 schema 정리 PR로 미룸. Race는 Redis TTL flag로 충분 |
| Schema metadata 추가 (`kind` / `session` / `event_timestamp` / `observed_at`) | over-engineering 의심. KRX 단일 source 위해 공통 schema 확장 비례성 깨짐. Read API에서 boundary timestamp range query(`WHERE timestamp BETWEEN ... AND source='krx'`)로 식별 가능 |
| Open auction (CF 08:30~08:45 / CM 17:50~18:00) | §4.12 (a) baseline에서 CF_open / CM_open 100% capture rate라 우선순위 낮음. close finalizer 정착 + 운영 측정 후 별도 PR 검토 |
| 종가 read API `get_close_rate(source, asset, kst_date, session)` | 3차 PR 영역. close finalizer 정착 + 종가 데이터 quality 확정 후 API 분리 |
| REST fallback 완전 제거 | 7일 telemetry 측정 후 case B(KIS WS 미송신) 비율 0% 입증 시 제거 검토. 현 단계는 catastrophic backup 가치 유지 |
| KRX Redis-first 전체 전환 (Stage E) | [KRX_FANOUT_REFACTOR_PLAN.md §5.2 E](KRX_FANOUT_REFACTOR_PLAN.md) 영역. tick-level Redis write는 close finalizer와 직교 layer라 별도 PR. 5/18 만기 baseline + ADR-027 Stage C 결정 후 진입 |

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

### 2차 PR 핵심 테스트 (close finalizer)

1. **WS close tick captured**: CF 15:45:01 frame 수신 → close window writer가 DB + Redis 양쪽 unconditional INSERT (실제 WS timestamp), `close_grace_saved` counter ++, Redis flag SET
2. **Same-price close saved**: WS close frame 가격이 직전 정규장 마지막 거래가와 동일 → DB INSERT 발생 (insert-if-changed 우회 검증), Redis SET 발생
3. **REST fallback skipped when captured**: WS close frame이 grace window 안에 1건이라도 잡힘 → grace 종료 시점에 Redis flag GET → captured 확인 → REST 호출 자체 skip, `close_rest_fallback_used` counter unchanged
4. **REST fallback used when not captured**: WS grace 동안 frame 0건 → grace 종료 직후 REST 1회 호출 → 성공 시 Redis + DB INSERT + flag SET, `close_rest_fallback_used` counter ++
5. **Env toggle off**: `KRX_CLOSE_FINALIZER_ENABLED=false` → 2차 PR 정책 비활성 + 1차 PR (c0855ff) 동작 그대로 (close window writer 진입 X, REST 3 retry sequence 그대로)
6. **Single-price window frame counted but not close-saved**: 15:35:00 ~ 15:44:59 frame 수신 (예: 단일가 시작 직후 잔여 echo) → `single_price_window_frame_count` counter ++ but close window writer 미진입 (일반 dedup 유지)

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

### 운영 smoke 기준 (2차 PR 배포 후, 7일 telemetry)

**Primary correctness target — 종가 양쪽 확정**:

- Redis `latest:source:krx:usd-krw-futures` timestamp가 매 영업일 boundary 도달 (CF 15:45 / CM 06:00 KST)
- DB `source_rates` `(source='krx', asset='usd-krw-futures')`에 매 영업일 close window row 1건 이상 (가격 동일 무관)
- Redis flag `close_captured:krx:CF:{kst_date}` / `close_captured:krx:CM:{kst_date}` 매 영업일 SET 확인

**Case A/B/C 분포 측정** (2차 PR 도입 핵심 검증):

- **case A (우리 cutoff)** 비율: `close_grace_saved` > 0 AND `close_rest_fallback_used` == 0 in window
  - 사용자 가설 데이터 검증 — §4.12 (c) cutoff 가설이 옳다면 CF capture rate **2/8 (25%) → ~8/8 (100%) 개선** (CM도 8/9 → 9/9 동시 검증)
- **case B (KIS WS 미송신)** 비율: `close_grace_saved` == 0 AND `close_rest_fallback_used` > 0
  - 0% 입증 시 3차 PR에서 REST fallback 제거 검토 진입 가능 (사용자 원래 의도)
  - >0% 발견 시 REST fallback 유지 (catastrophic backup 가치)
- **case C (dedup skip)** 비율: 2차 PR 정책 적용으로 0건 예상 (close window writer가 insert-if-changed 우회)

**이상 신호 alert**:

- `close_duplicate_count` > 0: close window 안 2 frame 이상 발생 — KIS feed 정책 변경 또는 listen loop race 의심
- `single_price_window_frame_count` 일별 ≥ 5: 단일가 진행 시간 frame 폭증 — 운영자 분석 필요

**시스템 검증**:

- 1차 PR 기능 회귀 0 (close snapshot REST 호출은 fallback path로만 발생)
- `KRX_CLOSE_FINALIZER_ENABLED=false` rollback path 정상 동작 확인 (배포 직후 1회 toggle 검증)
- KIS REST API 호출 빈도 감소 확인 (1차 PR 매일 6회 → 2차 PR 운영 안정 시 0~2회/day)

## 7. PR scope / 우선순위 + Deploy timing

### 7.1 PR 분할

- **Phase B.2와 독립 트랙**: trigger 분리 (PR4)와 운영 보강 (close snapshot)은 다른 목표
- **우선순위**: 사용자 체감 직접 영향 → Phase B.2 PR4보다 위로 (Codex/Claude 합의)
- **PR 분할**: 1차 (REST close snapshot only, c0855ff deployed) → **2차 (WS-first close finalizer + REST fallback)** → 3차 (DB unique + 종가 read API + open auction 검토)

### 7.2 2차 PR Deploy timing (5/18 vs 5/19)

**5/18 KRX 만기 rollover와 직접 의존성 없음** (사용자 + Codex 합의 2026-05-17):

- 5/18 rollover 관찰 영역: 07:00 contract swap / 08:45 새 월물 가격 반영 / 11:30 만기 WS frame format
- 2차 PR close finalizer 영역: 15:45 CF close / 06:00 CM close
- 시간대 + 코드 책임 + telemetry counter prefix 모두 분리 → 회귀 isolate 가능

**결정 기준**:

| 옵션 | 조건 | 평가 |
|---|---|---|
| 5/18 09:00 전 deploy | 구현(9-13h) + tests + 외부 검토 1 round 24h 안 완료 | 기술적 가능. 5/18 CF close 즉시 검증 (1 영업일 이득) |
| 5/19 09:00 전 deploy | 검토 round 2회 + 여유 확보 | 안전. 5/19 CF close부터 측정 시작 (1일 차이) |

**기다린다면 이유는 rollover가 아니라 구현/검토 시간 확보 차원** (Codex 정확 표현). 구현 진입 후 검토 round 진행 보며 결정.

### 7.3 3차 PR 후보 (2차 PR 7일 telemetry 측정 후)

- **DB unique constraint** `(source, asset, timestamp)` + `ON CONFLICT DO NOTHING`: 사전 검증 + migration + 다른 source 호출처 conflict handling 검토 후 도입
- **종가 read API** `get_close_rate(source, asset, kst_date, session)`: boundary timestamp range query 기반. 관리자 페이지 / 그래프 daily candle 활용
- **Open auction 보강** (CF 08:30~08:45 / CM 17:50~18:00): close finalizer 패턴 재사용. 현재 baseline 100% capture라 우선순위는 낮음
- **REST fallback 제거 검토**: telemetry case B == 0 입증 시 (사용자 원래 의도 실현 가능 지점)
- **Stage E (KRX Redis-first)** 진입 trigger: [KRX_FANOUT_REFACTOR_PLAN.md §5.2 E](KRX_FANOUT_REFACTOR_PLAN.md) + [ADR-027](DECISIONS.md) Stage C 결정 후

## 8. 참조

- [ADR-027](DECISIONS.md): KRX REST/stale 정책 (stale-based fallback, 본 plan은 boundary-based 별도 trigger)
- [KRX_CANARY.md](KRX_CANARY.md): 운영 runbook (PR 완료 후 확인 절차 추가)
- [krx_kis.py](app/crawlers/krx_kis.py): KrxDbWriter / KrxRedisLatestWriter / fetch_kis_futures_quote
- [kis_futures.py](app/sources/kis_futures.py): get_active_session / is_in_session_end_grace
- [crud.py](app/crud.py): insert_source_rate_if_changed (시그니처 변경 대상)
- [latest_rates_cache.py](app/latest_rates_cache.py): set_latest_krx_rate_from_sync_job (기존 helper 재사용)
