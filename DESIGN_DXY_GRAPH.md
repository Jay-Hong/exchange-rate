# 달러지수(DXY) + 그래프 기간 확장 설계 문서

> **상태**: 최종 확정 (Claude + Codex + Jay)
> **작성일**: 2026-03-10
> **최종 수정**: 2026-03-10 (전원 합의 완료, 미확정 0건)
> **목표**: USD/KRW 그래프에 달러지수 추가 + 그래프 기간을 1일/1주/3달/1년으로 확장
> **설계 기준**: 단기 목표 사용자 200명 (현재 2명은 초기 단계)

---

## 1. 기능 요약

### 1.1 달러지수(DXY) 도입

- **데이터 소스**: Investing.com (Primary) → Yahoo Finance (Fallback, 비상용)
- **표시 위치**: USD/KRW 그래프에만 추가 (JPY/EUR는 불필요 — 전원 합의)
- **향후 확장**: 달러지수 알림 기능 추가 가능하도록 구조 설계

### 1.2 그래프 기간 확장

| 기간 | USD/KRW 표시 소스 | JPY/EUR 표시 소스 | 비고 |
|------|-------------------|-------------------|------|
| **1일** (24시간) | Investing + KB + 하나 + **DXY** | Investing + KB + 하나 | 현재 + DXY 추가 |
| **1주** (7일) | Investing + **DXY** | Investing | 추이 확인용 |
| **3달** (90일) | Investing + **DXY** | Investing | 추이 확인용 |
| **1년** (365일) | Investing + **DXY** | Investing | 추이 확인용 |

**라벨 정책:**

- 1일: 실시간 크롤링 → "investing" (기존 유지)
- 1주/3달/1년: 혼합 소스 가능 → API key는 `reference`, 클라이언트 표시명은 별도 매핑

> **확정**: API 내부 key는 `reference`, 클라이언트 표시명은 **"인베스팅"** 유지 (전원 합의)
> 사용자에게 익숙한 기존 라벨 유지. 내부적으로 Yahoo 백필이 섞여도 서버 모델과 UI 라벨은 분리.

---

## 2. DB 설계 (확정)

### 2.1 `market_index_rates` 테이블 (신규)

```sql
market_index_rates
├── id          INTEGER PRIMARY KEY
├── instrument  TEXT            -- 'dxy' (향후 다른 지수 확장 가능)
├── source      TEXT            -- 'investing' | 'yahoo'
├── rate        FLOAT           -- 104.52
├── timestamp   DATETIME (UTC)  -- 데이터 시점

-- 인덱스
CREATE INDEX ix_market_index_instrument_ts ON market_index_rates (instrument, timestamp);
CREATE UNIQUE INDEX uq_market_index ON market_index_rates (instrument, source, timestamp);
```

**설계 근거 (전원 합의):**

- DXY는 통화쌍이 아님 → `investing_exchange_rates`와 분리
- `instrument` 컬럼으로 향후 다른 지수 추가 가능 (VIX 등)
- `source` 컬럼 필수 → 폴백 시 데이터 출처 추적, 그래프 튐/알림 오차 원인 분석
- 유니크 인덱스 `(instrument, source, timestamp)` → 중복 방지 + upsert 지원
- 클라이언트에는 source 노출하지 않음 (내부 관리용)

### 2.2 source 혼합 시 우선순위 규칙

같은 timestamp 근처에 investing과 yahoo 데이터가 모두 존재할 경우:

- **그래프 조회 시**: `investing` 우선, `yahoo`는 investing 데이터가 없는 구간만 사용
- **구현**: `ORDER BY timestamp ASC` 후 같은 시간대 중복 시 source='investing' 우선 선택

### 2.3 기존 테이블 변경 없음

`investing_exchange_rates`, `bank_exchange_rates` 스키마는 그대로 유지.
단, **쿼리 로직만 수정** (아래 3.1 참조).

---

## 3. 히스토리 데이터 백필 (확정)

### 3.1 전제: ORDER BY 수정 (최우선 작업)

> **전원 합의**: 백필 전에 반드시 `ORDER BY id DESC` → `ORDER BY timestamp DESC`로 수정

현재 `app/crud.py`의 `insert_investing_rates_into_db()`에서 최신 레코드 조회가
`ORDER BY id DESC LIMIT 1`로 되어 있음. 이를 `ORDER BY timestamp DESC LIMIT 1`로 변경.

**원칙**: `id`는 식별자일 뿐, 시계열 의미는 `timestamp`가 담당.

### 3.2 백필 방식: A안 (쿼리 수정 + 기존 데이터 유지)

> **확정: A안** (코덱스 의견 채택, Claude 동의)

| 단계 | 작업 |
|------|------|
| 1 | `ORDER BY id DESC` → `ORDER BY timestamp DESC` 전환 |
| 2 | 그래프 쿼리도 `timestamp` 기준 정렬 확인 (이미 되어 있음) |
| 3 | Yahoo에서 1년치 데이터 다운로드 |
| 4 | 기존 60일 데이터 **유지**, 빈 기간만 upsert/ignore duplicate로 삽입 |

**B안(전체 삭제) 불채택 이유**: 운영 리스크, 소스 혼합 이력 소실, 불필요한 서비스 중단

### 3.3 백필 대상 (Yahoo 티커 검증 완료 ✅)

> 2026-03-10 실제 `yfinance` 호출로 검증됨

| 데이터 | Yahoo 티커 | 1년 일별 | 7일 1시간 | 특이사항 |
|--------|-----------|---------|----------|---------|
| **DXY** | `DX-Y.NYB` | 253행 ✅ | 129행 ✅ | 현재 ~98.76 |
| **USD/KRW** | `KRW=X` | 258행 ✅ | 150행 ✅ | 현재 ~1,472 |
| **JPY/KRW** | `JPYKRW=X` | 258행 ✅ | 152행 ✅ | **⚠️ 1엔 기준 (9.28원)** → ×100 스케일링 필수 |
| **EUR/KRW** | `EURKRW=X` | 258행 ✅ | 152행 ✅ | 현재 ~1,708 |

**JPY/KRW 주의**: Yahoo는 1엔=9.28원 기준. 서비스는 100엔 기준 (`SCALED_CURRENCY_PAIRS`).
백필 시 반드시 `×100` 스케일링 적용.

### 3.4 Yahoo Finance 데이터 주의사항

| 항목 | 내용 | 대응 |
|------|------|------|
| **Granularity** | 일별(daily) OHLCV만 안정적 | 장기 그래프용으로 충분 |
| **Intraday** | 최근 7일만 1시간봉 | 1주 DXY 부트스트랩 가능 (아래 5.2 참조) |
| **Timezone** | 장 마감 기준 (UTC/NY) | 일봉 기준: UTC 00:00으로 정규화 |
| **주말/공휴일** | 데이터 없음 | 그래프에서 자연스럽게 건너뜀 |
| **Rate Limiting** | 과도 요청 시 차단 | 백필 1회성, 실시간은 비상용만 |
| **데이터 차이** | Yahoo vs Investing 미세한 차이 | 장기 추이 목적이므로 허용, source 기록 |

### 3.5 Timezone 절단 규칙

- **일봉(3달/1년)**: UTC 00:00 기준으로 절단 (기존 investing 데이터도 UTC 저장)
- **시간봉(1주)**: UTC 정시 기준 (예: 13:00:00)
- **10분봉(1일)**: 기존 방식 유지 (KST 기준 10분 버켓)

### 3.6 백필 스크립트

```python
# scripts/backfill_history.py
#
# 1. Yahoo Finance에서 1년치 다운로드
#    - DXY: yf.download("DX-Y.NYB", period="1y")
#    - 환율: yf.download("KRW=X", period="1y") 등
#
# 2. Timezone 정규화 (→ UTC naive datetime, 일봉은 00:00:00)
#
# 3. JPY/KRW 스케일링: rate × 100 (1엔 → 100엔 기준)
#
# 4. DXY → market_index_rates 테이블 INSERT
#    - instrument='dxy', source='yahoo'
#    - UNIQUE 제약조건으로 중복 방지
#
# 5. 환율 → investing_exchange_rates 테이블
#    - 기존 데이터와 겹치는 날짜는 SKIP (ON CONFLICT IGNORE)
#    - 기존 60일 이전 데이터만 삽입
#
# 6. 결과 리포트 (삽입 건수, 스킵 건수, 스케일링 적용 건수)
```

---

## 4. 크롤러 설계 (확정)

### 4.1 DXY 크롤러 (`app/crawlers/dxy.py`)

#### Primary: Investing.com

```python
URL = "https://kr.investing.com/indices/usdollar"
# 기존 investing.py 패턴 완전 재활용:
# - curl_cffi + safari17_0 TLS 지문 위장
# - User-Agent 로테이션 (Safari/Chrome 풀)
# - CSS Selector로 현재가 파싱
```

#### Fallback: Yahoo Finance (비상용)

> **확정: 연속 실패 + stale 임계치 조합**

**폴백 전환 조건** (둘 중 하나 충족 시):

1. Investing 연속 5회 실패
2. 마지막 성공 시각이 5분 이상 stale

**복구 정책**:

- 쿨다운 해제 후 Investing 재시도
- Investing 성공 시 즉시 원소스로 복귀
- Yahoo 사용 중에도 `source='yahoo'`로 기록

```python
# 폴백 로직 의사코드
if investing_consecutive_failures >= 5 or last_success_age > 300:
    rate = fetch_from_yahoo()
    save(instrument='dxy', source='yahoo', rate=rate)
else:
    rate = fetch_from_investing()
    save(instrument='dxy', source='investing', rate=rate)
```

#### 라이브러리: yfinance (확정)

> **전원 합의**: `yfinance` 사용 (백필 + 폴백 겸용, 유지보수 편의)

### 4.2 DXY 크롤러 스케줄링

> **확정**: IN/BREAK = 매분 1회, OUT = 10분 1회 (전원 합의)

| 모드 | 주기 | 실행 시각 | 비고 |
|------|------|----------|------|
| **IN** | 매분 1회 | 42초 | Broadcasting 전 DXY 갱신 |
| **BREAK1** | 매분 1회 | 42초 | 동일 |
| **BREAK2** | 매분 1회 | 42초 | 동일 |
| **OUT** | **10분 1회** | 2분 42초 | 주말 DXY 변동 미미 + 기존 investing 크롤러와 일관 |

> **근거**: DXY는 보조 지표. 주말 사용자 가치 대비 Investing 403 리스크가 더 큼.
> 기존 investing 환율 크롤러도 OUT에서 10분 간격이므로 일관성 유지.

---

## 5. 그래프 기간별 설계 (확정)

### 5.1 버켓 크기 (Granularity)

| 기간 | 버켓 크기 | 포인트 수 | 환율 데이터 소스 | DXY 데이터 소스 |
|------|----------|----------|------------------|-----------------|
| **1일** | 10분 | ~144개 | 실시간 크롤링 | 실시간 크롤링 |
| **1주** | 1시간 | ~120개 | 실시간 크롤링 | 실시간 크롤링 |
| **3달** | 1일 | ~65개 | 크롤링 + Yahoo 백필 | 크롤링 + Yahoo 백필 |
| **1년** | 1일 | ~252개 | 크롤링 + Yahoo 백필 | 크롤링 + Yahoo 백필 |

### 5.2 1주 DXY 초기 노출 정책

> **확정: Yahoo 7일 1시간봉 부트스트랩** (전원 합의)

Yahoo Finance `DX-Y.NYB` 7일 1시간봉(129행) 검증 완료.
즉시 출시 가능, 사용자에게 완성된 1주 DXY 그래프 제공.

**전환 규칙:**

- 초기: Yahoo 1시간봉으로 1주 DXY 채움 (`source='yahoo'`)
- 자체 DXY 데이터가 7일 이상 쌓이면 Investing 우선으로 전환
- 같은 시간대 중복 시 `investing > yahoo` (source 우선순위 규칙)

### 5.3 휴장일/주말 표현 정책

- **권장**: 거래일만 표시 (주말 갭은 자연스럽게 건너뜀)
- Chart.js `time` 축에서 실제 데이터 포인트만 연결
- DXY가 주말에 평평하게 이어지는 것 방지

### 5.4 JPY/EUR 그래프 (확정)

> **전원 합의**: 기간 확장 전체 적용, DXY는 USD/KRW만

- JPY/EUR도 1일/1주/3달/1년 제공
- 1주/3달/1년에서는 reference(환율) 단독
- DXY 라인 없음

---

## 6. API 및 데이터 전달 (확정)

### 6.1 전달 방식

| 기간 | 전달 방식 | 이유 |
|------|----------|------|
| **1일** | WebSocket (실시간 버켓) + REST (전체) | 실시간 업데이트 |
| **1주/3달/1년** | REST only | 일별/시간별 데이터, 실시간 불필요 |

### 6.2 REST API 엔드포인트

```
GET /api/graph/{currency}?range=1d|1w|3m|1y
```

> 기존 `/api/graph/{currency}` (range 미지정 시) = 1d (하위 호환)

**응답 구조 (range별 source 구성이 다름):**

```json
// GET /api/graph/usd-krw?range=1d
{
  "pair": "usd-krw",
  "period": "1d",
  "bucket_size": "10m",
  "sources": {
    "investing": [[ts, max, min, close], ...],
    "kb": [[ts, max, min, close], ...],
    "hana": [[ts, max, min, close], ...],
    "dxy": [[ts, max, min, close], ...]
  }
}

// GET /api/graph/usd-krw?range=1y
{
  "pair": "usd-krw",
  "period": "1y",
  "bucket_size": "1d",
  "sources": {
    "reference": [[ts, max, min, close], ...],
    "dxy": [[ts, max, min, close], ...]
  }
}

// GET /api/graph/jpy-krw?range=1y
{
  "pair": "jpy-krw",
  "period": "1y",
  "bucket_size": "1d",
  "sources": {
    "reference": [[ts, max, min, close], ...]
  }
}
```

### 6.3 WebSocket 변경사항

1일 그래프용 마지막 버켓만 전송 (기존 구조 유지 + DXY 추가):

```json
{
  "graph_buckets": {
    "usd-krw": {
      "investing": {"bucket_ts": ..., "max": ..., "min": ..., "close": ...},
      "kb": {...},
      "hana": {...},
      "dxy": {"bucket_ts": ..., "max": ..., "min": ..., "close": ...}
    },
    "jpy-krw": {
      "investing": {...}, "kb": {...}, "hana": {...}
    },
    "eur-krw": {
      "investing": {...}, "kb": {...}, "hana": {...}
    }
  }
}
```

> JPY/EUR의 graph_buckets에는 DXY 미포함
> 장기 그래프 데이터는 WebSocket에 포함하지 않음
> DXY 버켓 추가로 인한 payload 증가: ~50바이트 (200명 규모에서 무시 가능)

### 6.4 캐시 전략

| 기간 | 캐시 키 | TTL | 갱신 방식 |
|------|---------|-----|----------|
| **1일** | `graph:{currency}` | 120초 | 매분 03초 proactive refresh (기존) |
| **1주** | `graph:{currency}:1w` | 10분 | lazy (요청 시 miss면 DB 조회) |
| **3달** | `graph:{currency}:3m` | 1시간 | lazy |
| **1년** | `graph:{currency}:1y` | 1시간 | lazy |

**lazy caching 근거:**

- 장기 그래프는 실시간성 요구 낮음 (일별 데이터)
- 동일 질의 재사용성 높음 (같은 기간 반복 조회)
- 초기에는 lazy로 충분, 트래픽 증가 시 proactive refresh로 전환 가능

---

## 7. 이중 Y축 (Dual Axis) — 확정

DXY(~98-110)와 USD/KRW(~1,300-1,480)는 스케일이 완전히 다름.

```
왼쪽 Y축: USD/KRW (환율)
오른쪽 Y축: DXY (달러지수)
```

- Chart.js `yAxisID: 'y2'`로 두 번째 축 설정
- DXY 라인: 다른 색상 계열 (예: 회색/점선) + 범례 명시
- 모바일 앱에서도 동일 이중 축 적용

**향후 보류 항목:**

- 1년 그래프에서 정규화(%) 보기 옵션 (이번 스코프 외)
- 기준점 대비 변동률 비교 모드 (이번 스코프 외)

---

## 8. 알림 확장성 (향후)

> 이번 스코프에서는 알림 테이블 변경 없음

현재 알림 모델은 `bank + currency` 중심이라 DXY 수용 불가.
향후 확장 시 마이그레이션 방향:

```
현재: bank + currency + condition + threshold
향후: target_type ('currency' | 'index')
      target_code ('usd-krw' | 'dxy')
      source ('investing' | 'yahoo' | null)
```

이번 단계에서는:

- DXY 수집/저장/조회 계층을 별도 모듈로 분리
- API 응답도 source/instrument 중심으로 설계
- 이것만으로도 다음 단계 알림 확장이 자연스러움

---

## 9. 모바일 앱 영향

| 항목 | 영향 | 하위 호환 |
|------|------|----------|
| **WebSocket** | DXY 필드 추가 | O (새 필드 무시 가능) |
| **REST `/api/graph/{currency}`** | range 파라미터 추가 | O (미지정 시 1d) |
| **iOS** | 기간 탭, 이중 Y축, DXY 토글 UI | 앱 업데이트 필요 |

> **구현 순서**: 백엔드 → 웹(관리자/디버깅용) → iOS (전원 합의)
> Android는 현재 미출시, 향후 별도 계획

---

## 10. 데이터 보관 정책 (변경)

| 데이터 | 보관 기간 | 변경 |
|--------|----------|------|
| **DXY** (`market_index_rates`) | 영구 (최소 1년+) | 신규 |
| **Investing 환율** | 영구 (최소 1년+) | "장기 보관" → 명시적 1년+ |
| **은행 환율** | 10일 (기존 유지) | 변경 없음 |

---

## 11. 구현 순서

### Phase A: 기반 작업 (필수 선행)

1. `crud.py`의 `ORDER BY id DESC` → `ORDER BY timestamp DESC` 수정
2. `market_index_rates` DB 테이블 생성 (모델 + 마이그레이션)
3. `yfinance` 의존성 추가 (`requirements.txt`)

### Phase B: DXY 크롤러

4. `app/crawlers/dxy.py` 구현 (Investing primary)
5. Yahoo Finance 폴백 로직 추가 (연속 실패 + stale 조합)
6. `scheduler.py`에 DXY 크롤러 스케줄 추가
7. DXY 저장 서비스에서 source 기록 + stale/failure 정책 적용

### Phase C: 백필

8. `scripts/backfill_history.py` 작성
9. `scripts/migrate_market_index_granularity.py` 작성 (granularity 컬럼 추가)
10. Yahoo Finance 데이터 검증 (로컬 테스트)
11. 운영 DB 배포 (아래 순서 엄수)

**운영 배포 순서 (⚠️ 순서 위반 시 서버 즉시 중단)**:

```
1. python scripts/migrate_market_index_granularity.py --dry-run  # SQL 확인
2. python scripts/migrate_market_index_granularity.py             # 마이그레이션 실행
   ↑ granularity 컬럼이 DB에 존재해야 새 코드가 동작
3. 코드 배포 (models.py, crud.py, dxy.py 등)
4. python scripts/backfill_history.py --dry-run --target dxy      # 백필 확인
5. python scripts/backfill_history.py --target all                 # 백필 실행
```

> **근거**: 새 코드는 `WHERE granularity = 'realtime'` 필터를 사용.
> 마이그레이션 전에 코드를 배포하면 `column does not exist` SQL 에러 발생.

### Phase D: 그래프 API 확장

11. `/api/graph/{currency}?range=` 엔드포인트 구현
12. 기간별 버켓 로직 (1시간/1일 버켓 추가)
13. 1일/1주/3달/1년 캐시 분리
14. WebSocket broadcast에 DXY 버켓 추가

### Phase E: 프론트엔드 (웹)

15. 그래프 기간 탭 UI (1일/1주/3달/1년)
16. Chart.js 이중 Y축 구현
17. DXY 토글 버튼 추가
18. 기간별 소스 구성 자동 전환

### Phase F: iOS 앱

19. 그래프 API 연동 + 기간/DXY UI

---

## 12. 리스크 및 주의사항

| 리스크 | 대응 |
|--------|------|
| Investing DXY 페이지 CSS 변경 | Yahoo 자동 폴백 (source 기록) |
| Yahoo Finance rate limiting | 백필 1회성, 실시간은 비상용만 |
| Yahoo vs Investing 데이터 미세 차이 | 장기 추이 목적이므로 허용, source 기록으로 추적 |
| 이중 Y축 모바일 UX | 범례 + 색상 구분 + 축 라벨 명시 |
| 1주 DXY 데이터 부족 (초기) | Yahoo 7일 1시간봉 부트스트랩 → 자체 데이터 전환 (확정) |
| RDS 저장 용량 증가 | 1년 일별 ≈ 4,000행 (미미) |
| JPY/KRW Yahoo 스케일링 | 백필 시 ×100 필수 (1엔 → 100엔 기준) |
| Investing.com 403 리스크 증가 | DXY 크롤러 별도 Circuit Breaker, OUT 모드 주기 완화 |

---

## 13. 확정된 결정 요약

| # | 항목 | 결정 | 근거 |
|---|------|------|------|
| 1 | DB 테이블 | `market_index_rates` (별도) | 확장성 + 도메인 분리 |
| 2 | `source` 컬럼 | 포함 (필수) | 폴백 추적, 데이터 품질 |
| 3 | `instrument` 컬럼 | 포함 (`'dxy'`) | 향후 다른 지수 확장 |
| 4 | 백필 방식 | A안 (쿼리 수정, 기존 유지) | 운영 리스크 최소화 |
| 5 | 폴백 전환 | 연속 실패 + stale 임계치 조합 | Investing 우선, Yahoo 비상용 |
| 6 | 라이브러리 | yfinance | 백필 + 폴백 겸용 |
| 7 | IN/BREAK 크롤링 | 매분 1회 (42초) | 보조 지표, 1분이면 충분 |
| 8 | OUT 크롤링 | 10분 1회 | 주말 변동 미미, 기존 investing과 일관 |
| 9 | 1주 버켓 | 1시간 | 1일은 너무 거침 |
| 10 | 1주 DXY 초기 노출 | Yahoo 7일 1시간봉 부트스트랩 | 즉시 출시, 7일 후 자체 데이터 전환 |
| 11 | JPY/EUR 기간 | 전체 적용 (DXY 없이) | UI 일관성 |
| 12 | 장기 환율 라벨 | API key=`reference`, 표시명="인베스팅" | 서버/UI 분리, 사용자 익숙한 라벨 |
| 13 | WebSocket | 1일 버켓만 전송 | 장기는 REST only |
| 14 | 구현 순서 | 백엔드 → 웹 → iOS | API 안정화 우선 |
| 15 | source 우선순위 | investing > yahoo | 같은 시간대 중복 시 |
| 16 | 일봉 timezone | UTC 00:00 기준 | 기존 데이터와 일관성 |
| 17 | Yahoo 티커 | 검증 완료 ✅ | 4개 전부 정상, JPY ×100 필수 |
| 18 | 유니크 인덱스 | (instrument, source, timestamp) | 중복 방지 + source별 관리 |

---

---

> **전원 합의 완료. 다음 단계: Phase A 기반 작업 시작.**
