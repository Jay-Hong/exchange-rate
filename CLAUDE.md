# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

### 📚 주요 문서 가이드

**핵심 가이드:**
> 💡 **아키텍처 의사결정:** [DECISIONS.md](DECISIONS.md) - 주요 기술 선택과 그 근거 (ADR)
> 🕷️ **크롤러 구현:** [CRAWLERS.md](CRAWLERS.md) - 각 은행별 크롤링 방식과 특수 로직
> 📝 **변경 이력:** [CHANGELOG.md](CHANGELOG.md) - 버전별 변경사항 및 마이그레이션 가이드
> 🔔 **알림 & 구독:** [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md) - 푸시 알림, 인증, 구독 관리 가이드
> 💳 **RevenueCat 서버 사이드:** [REVENUECAT_SERVER_SIDE.md](REVENUECAT_SERVER_SIDE.md) - 구독 검증, Webhook, 캐시 정책

**배포 및 운영:**
> 🐳 **Docker 아키텍처:** [DOCKER.md](DOCKER.md) - Docker Compose 구조 및 확장 전략
> 🚀 **AWS 배포:** [DEPLOYMENT.md](DEPLOYMENT.md) - EC2 인스턴스 설정 및 실전 배포 절차
> 🔄 **재부팅 절차:** [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md) - 재부팅 후 검증 체크리스트

**유지보수 기록:**
> 🔧 **2026-04-27:** [MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md) - MIBANK URL/DOM 변경 대응 (bank_cd URL, 기준환율 헤더 기반 파싱)
> 🔧 **2026-01-29:** [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md) - Investing Cloudflare 403 차단 대응 (curl_cffi TLS 지문 위장)
> 🔧 **2025-11-06:** [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) - AsyncIO Queue 도입 (Semaphore 경합 제거)
> 🔧 **2025-11-05:** [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md) - 성능 개선 및 모니터링 시스템 구축

**계획/설계 문서:**
> ⚡ **실시간 아키텍처:** [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) - 1초 broadcast, 거래소 WebSocket, 구독 기반 라우팅 전환 계획
> 🔌 **USDT 거래소 WebSocket:** [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) - 업비트/빗썸/코인원/코빗/고팍스 ticker 구독 및 정규화 가이드

**글로벌 가이드:**
> 📚 **공통 개발 규칙:** [~/.claude/CLAUDE.md](file:///Users/jay/.claude/CLAUDE.md) - MCP 설정, 코딩 스타일, Git Convention

## MCP (Model Context Protocol) 설정

> 💡 **MCP 전체 가이드**: `~/.claude/CLAUDE.md` 참고 (설치/삭제 명령어, Context 최적화 전략)
> ⚠️ **Context Window 최적화**: 필요한 MCP만 설치, 작업 완료 후 삭제 권장 (세션 재시작 필수)

### 이 프로젝트에서 사용 가능한 MCP

**기본 유지 권장**:
- **context7**: 라이브러리 문서 조회 (2개 도구, 경량)
- **filesystem**: 프로젝트 파일 탐색 (13개 도구)

**필요시 설치**:
- **sqlite**: DB 쿼리 실행 (`data/exchange_rates.db`)
- **github**: PR/Issue 작업 (62개 도구, 토큰 많음 - 사용 후 삭제 권장)

## 목표 사용자

- 초기: 200-500명
- 주 사용층: iOS/Android 네이티브 앱 사용자
- 웹: 관리자/디버깅 전용

## 핵심 아키텍처

```text
[개발/테스트]
크롤러(11개) → Scheduler → SQLite → WebSocket(10초) → 클라이언트

[서비스 환경]
크롤러(11개) → Scheduler → RDS PostgreSQL → WebSocket(1초) → 클라이언트
                              ↓                ↑
                    Redis latest mirror (3초)  ── broadcast Redis-first read (PR3-PR5, ADR-026)
                                    ↓
                          Firebase Auth + FCM → 푸시 알림
```

> **broadcast 데이터 흐름** (PR5 완료, 2026-05-04):
> 크롤러가 DB에 저장 → mirror cycle 3초마다 DB latest를 Redis로 복사 (`latest:bank:*`, `latest:source:*`, `latest:investing:*`, `latest:dxy:current`) → broadcast 매초 Redis 직접 read → Redis 실패 시 DB fallback. broadcast hot path는 DB-free (rates + DXY 모두). 자세한 내용은 [ADR-026](DECISIONS.md#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성).

### 데이터 소스

- Investing.com (기준 환율)
- DXY (달러지수, USD/KRW 그래프 보조지표) — investing.py에서 환율과 동시 추출 (폴백: /currencies/us-dollar-index, Yahoo Finance)
- 9개 은행: KB, 하나, 신한, 우리, IBK, NH, SC제일, 부산, 씨티
- **가상자산 거래소 5종** (USDT/KRW, Phase 1): 업비트, 빗썸, 코인원, 고팍스, 코빗

### 지원 통화

- USD-KRW, JPY-KRW, EUR-KRW
- **USDT-KRW** (Phase 1, 가상자산 거래소 기반)

## 기술 스택

### 백엔드

- **언어**: Python 3.x
- **웹 프레임워크**: FastAPI
- **DB**: SQLite (개발/테스트), AWS RDS PostgreSQL (운영)
- **ORM**: SQLAlchemy
- **스케줄러**: APScheduler (AsyncIOScheduler)
- **크롤링**: requests + BeautifulSoup4, Selenium, curl_cffi (Investing 전용, TLS 지문 위장), yfinance (DXY Yahoo 폴백)
- **SSL/TLS**: Let's Encrypt (Certbot 자동 갱신, 90일 주기)
- **리버스 프록시**: Nginx (HTTPS, HTTP/2, wss://)

### 프론트엔드

- **iOS**: Swift (네이티브)
- **Android**: Kotlin + Jetpack Compose
- **웹**: Vanilla JS + WebSocket (관리자용)

### 실시간 통신

- **프로토콜**: WebSocket Secure (wss://)
- **도메인**: https://fxi.kr
- **브로드캐스트**: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
- **하트비트**: Ping/Pong (30초)
- **암호화**: TLS 1.2 & 1.3

## 데이터베이스 스키마

### investing_exchange_rates

```sql
id         INTEGER PRIMARY KEY
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (UTC)
```

### bank_exchange_rates

```sql
id         INTEGER PRIMARY KEY
bank       TEXT (INDEX)
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (UTC)

-- 복합 인덱스: (bank, currency, timestamp)
```

### crawler_config (Phase 1.8)

```sql
id            INTEGER PRIMARY KEY
crawler_name  TEXT (UNIQUE, INDEX)
enabled       BOOLEAN (DEFAULT: TRUE)
updated_at    DATETIME (UTC)

-- 크롤러 활성화/비활성화 설정 (관리자 페이지에서 제어)
```

### market_index_rates (Phase 1A)

```sql
id            INTEGER PRIMARY KEY
instrument    TEXT NOT NULL          -- 'dxy' (향후 다른 지수 확장 가능)
source        TEXT NOT NULL          -- 'investing' | 'yahoo'
rate          REAL NOT NULL
timestamp     DATETIME NOT NULL (UTC)
granularity   TEXT NOT NULL          -- 'realtime' | 'hourly' | 'daily'

-- 인덱스: (instrument, timestamp)
-- 인덱스: (instrument, granularity, timestamp)
-- UNIQUE: (instrument, source, timestamp, granularity)
```

**granularity 구분:**
- `realtime`: 실시간 크롤링 데이터 (10초~1분 간격)
- `hourly`: 시간봉 백필 데이터 (yfinance, 최근 7일)
- `daily`: 일봉 백필 데이터 (yfinance, 최대 1년)

### 알림 관련 테이블 (Phase 2, 구현 완료)

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#필요한-db-테이블-rds-postgresql)

- `user_devices` - FCM Device Token 저장 (user_id, device_token, platform)
- `notification_settings` - 알림 조건 설정 (bank, currency, condition, threshold, enabled, triggered)
- `notification_logs` - 알림 발송 히스토리 (중복 방지)

### USDT Phase 1 — Source 기반 테이블 (2026-04-23)

> 💡 **설계 원칙**: 기존 bank/investing 세계와 분리. DB는 `source + asset`, 레거시 API 응답 시에만 `bank + currency`로 변환 (Decision E)
> 📖 **상세 설계**: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md)

#### source_rates (Phase 1)

```sql
id          INTEGER PRIMARY KEY
source      TEXT NOT NULL         -- 'upbit' | 'bithumb' | 'coinone' | 'korbit' | 'gopax'
asset       TEXT NOT NULL         -- 'usdt-krw' (Phase 2: 'usd-krw-futures' 예정)
rate        REAL NOT NULL
timestamp   DATETIME NOT NULL (UTC)

-- 인덱스: (source, asset, timestamp)
```

#### source_notification_settings (Phase 1)

```sql
id                  INTEGER PRIMARY KEY
user_id             TEXT NOT NULL (INDEX)
source              TEXT NOT NULL
asset               TEXT NOT NULL
condition           TEXT NOT NULL         -- 'above' | 'below'
threshold           REAL NOT NULL
enabled             BOOLEAN DEFAULT TRUE
triggered           BOOLEAN DEFAULT FALSE
last_notified_at    DATETIME
last_notified_rate  REAL
created_at          DATETIME
updated_at          DATETIME

-- 인덱스: (source, asset)
```

#### source_notification_logs (Phase 1)

```sql
id              INTEGER PRIMARY KEY
user_id         TEXT NOT NULL (INDEX)
setting_id      INTEGER NULL          -- 설정 삭제 후에도 로그 유지
source          TEXT NOT NULL
asset           TEXT NOT NULL
condition       TEXT NOT NULL
threshold       REAL NOT NULL
triggered_rate  REAL NOT NULL
success         BOOLEAN NOT NULL
error_message   TEXT
sent_at         DATETIME
```

**source_registry**: 9개 소스 메타데이터는 `app/source_registry.py`의 `SourceDefinition` dataclass에서 관리 (canonical key는 DB 저장 안 함, 필요 시 `f"{source}:{asset}"`로 조합)

## 데이터 관리 정책

- **은행 데이터**: 30일분만 유지 (단기 비교용)
- **인베스팅 데이터**: 장기 보관 (그래프/분석용)
- **DXY 데이터**: realtime 원본은 30일분만 유지 (`dxy`, `dxy_futures` 모두 `market_index_rates`에 저장), hourly/daily rollup은 장기 그래프 보존
- **source_rates (USDT)**: 30일분만 유지 (은행 데이터와 동일 정책)
- **저장 조건**: 변경사항 있을 때만 INSERT (중복 방지)

## 스케줄링 시스템

> 💡 **최신 아키텍처:** 4단계 모드 + 환율 고시 스케줄 기반 최적화 (2025-11-16)
> 📖 **상세 기록:**
> - [ADR-011](DECISIONS.md#adr-011-4단계-모드-환율-고시-스케줄-기반-최적화) - 4단계 모드 도입 (IN, BREAK1, BREAK2, OUT)
> - [ADR-010](DECISIONS.md#adr-010-websocket-broadcasting-스케줄링-방식) - Broadcasting APScheduler cron job 전환
> - [ADR-009](DECISIONS.md#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화) - 3-Tier 크롤러 스케줄링
> - [ADR-008](DECISIONS.md#adr-008-queue-압력-완화-전략-실시간성-vs-완전성) - Queue 압력 완화 + 실시간성 강화

> 🔖 **이전 개선:**
> - Priority Queue + Timeout (2025-11-08) - [ADR-007](DECISIONS.md#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)
> - AsyncIO Queue 순차 실행 (2025-11-06) - [ADR-006](DECISIONS.md#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue)

### 4단계 모드 자동 전환

- **IN 모드**: 월~금 08:00~20:59 (영업시간, 전체 크롤러 활성)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
  - 크롤러: cron 절대 시간 동기화 (Broadcasting 기준)
  - 10개 은행 전체 크롤링 (DXY는 investing 크롤러에서 동시 추출)

- **BREAK1 모드**: 월~금 21:00 ~ 익일 02:59 (심야, 9개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: sc
  - 유지: investing, kb, hana, woori, shinhan, bs, citi, ibk, nh
  - ibk는 00:00~00:05 스킵 (자정 전환기), 00:05부터 Selenium만 사용

- **BREAK2 모드**: 월 06:00~07:59, 화~금 03:00~07:59, 토 03:00~06:59 (고시 마무리, 6개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori (02:45 종료), ibk (02:05 종료), shinhan (02:30 종료), sc (20:30 종료)
  - 유지: investing, kb, hana, bs, citi, nh

- **OUT 모드**: 토 07:00 ~ 월 06:00 전 (주말, 6개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori, ibk, sc, citi (주말 고시 없음)
  - 유지: investing, kb, hana, bs, shinhan, nh (주말 중 가끔 변동)
  - 크롤러: cron 시간 단위 (완전 분산, 동시 실행 0개)

### 은행별 환율 고시 스케줄

| 은행 | 고시 시작 | 고시 종료 | 특이사항 |
|------|----------|----------|---------|
| **investing** | 월 06:00 | 토 06:00 | 외환 시장 글로벌 운영 |
| **kb** | 평일 08:30 | 익일 05:00 | - |
| **hana** | 평일 08:30 | 익일 06:00 | 주말 중 가끔 변동 |
| **shinhan** | 평일 08:19 | 익일 02:30 | 주말 중 가끔 변동 |
| **woori** | 평일 08:30 | 익일 02:45 | - |
| **ibk** | 평일 08:30 | 익일 02:05 | 00:00~00:05 스킵 (자정 전환기), 00:05부터 Selenium만 사용 |
| **nh** | 평일 08:40 | 당일 24:00 | 자정 이후/주말 가끔 고시 |
| **sc** | 평일 09:00 | 당일 20:30 | - |
| **bs** | 평일 08:10 | 당일 24:00 | 일요일 넘어갈 때 가끔 고시 |
| **citi** | 평일 09:00 | 익일 06:00 | - |

### 스케줄링 아키텍처 (2025-11-16 최종)

**스케줄러:** APScheduler (AsyncIOScheduler)

#### 1. WebSocket Broadcasting

**실행 시점:** 매분 00, 10, 20, 30, 40, 50초 (정확한 시간, 모든 모드 동일)

```python
# scheduler.py
scheduler.add_job(
    broadcast_rates_once,  # async 함수 직접 등록
    CronTrigger(second='0,10,20,30,40,50', timezone=KST),
    id="websocket_broadcast"
)
```

**설계 원칙:**
- APScheduler cron job으로 정확한 시간 보장
- 크롤러 동기화의 기준 시간
- 변경사항 있을 때만 실제 전송 — trigger 조건: `rates` 또는 `indices.dxy` (DXY live tick) 변화 어느 쪽이든 발화 (`build_rates_payload()`의 JSON 전체 비교)

#### 2. 3-Tier 크롤러 아키텍처 (4단계 모드)

**Tier A (investing):** 기준 환율 + DXY, 최우선
- **특징**: 가장 중요한 데이터, 빠른 응답. DXY(달러지수)도 동시 추출 (`#sb_last_8827`)
- **실행 방식**: Request 기반 (curl_cffi, TLS 지문 위장으로 Cloudflare 우회)
- **DXY 폴백**: 추출 실패 시 dxy.py 폴백 모듈 호출 (60초 쿨다운, /currencies/us-dollar-index → Yahoo Finance)
- **IN/BREAK1/BREAK2**: 10초마다 (Broadcasting 3초 전)
  - `cron(second='7,17,27,37,47,57')`
- **OUT**: 10분마다
  - `cron(minute='7,17,27,37,47,57', second='45')`

**Tier B (kb, hana, woori, bs, citi):** 은행 환율, 중요
- **특징**: 중요도 높음, 빈도 높음
- **실행 방식**: Request 기반 (하이브리드 폴백: Request → Selenium)
- **IN/BREAK1/BREAK2**: 20-60초마다 (Broadcasting 3-7초 전, 엇갈림)
  - kb: `cron(second='15,35,55')`
  - hana: `cron(second='5,25,45')`
  - woori: `cron(minute='*', second='53')` (우선순위 높음, 늦게 크롤링)
  - bs: `cron(minute='*', second='33')`
  - citi: `cron(minute='*', second='13')`
- **BREAK1**: woori, bs, citi 유지 (kb, hana도 유지)
- **BREAK2**: bs, citi만 유지 (woori 02:45 종료)
- **OUT**: bs만 유지 (60분마다, 33분 45초)

**Tier C (shinhan, ibk, nh, sc):** Selenium, 순차 처리
- **특징**: 메모리 집약적, Request→Selenium 폴백으로 부하 감소
- **실행 방식**: AsyncIO PriorityQueue 순차 실행
- **부하 감소 전략**: Request(mibank) 먼저 시도 → 실패 시 Selenium 폴백
  - shinhan, nh, sc: 항상 Request 우선
  - ibk: IN 모드(08:30~20:59) Request 우선, 00:00~00:05 스킵 (자정 전환기), 00:05~02:59 Selenium만 사용
- **IN**: 매분 cron (shinhan: 18초, ibk: 34초, nh: 54초, sc: 58초)
- **BREAK1**: ibk(34초), nh(54초), shinhan(18초) 유지 (sc는 20:30에 크롤링 중단)
- **BREAK2**: nh(54초)만 유지 (ibk는 02:05 종료, shinhan은 02:30 종료, sc는 20:30 종료)
- **OUT**: nh(3분 45초), shinhan(13분 45초) 유지 (주말 중 가끔 변동)

**Selenium Queue 관리:**
- **Request 우선 전략**: Queue 압력 대폭 감소 (대부분 Request 성공)
- **우선순위 기반 실행**: 빠른 크롤러 우선 (hana → shinhan → nh → ibk → sc)
- **개별 타임아웃**: 크롤러별 45초 통일 (실시간성 우선)
- **자동 재시도**: 실패 시 우선순위 +1000으로 재실행
- **중복 작업 방지**: Worker 처리 중 + Queue 대기 중 체크
- **Non-blocking Queue**: put_nowait()으로 APScheduler 멈춤 방지
- **Queue 크기**: 25 (메모리 최적화)
- **압력 완화**: 80% (20/25) 초과 시 새 작업 거부
- **헬스체크**: 매분 03/23/43초 (20초 간격), 60초 stuck 시 자동 재시작

### IN 모드 타임라인 (1분 기준)

```
00초: Broadcasting
05초: hana
07초: investing (+ DXY 동시 추출)
10초: Broadcasting
13초: citi
15초: kb
17초: investing (+ DXY 동시 추출)
18초: shinhan (Selenium Queue)
20초: Broadcasting
25초: hana
27초: investing (+ DXY 동시 추출)
30초: Broadcasting
33초: bs
34초: ibk (Selenium Queue)
35초: kb
37초: investing (+ DXY 동시 추출)
40초: Broadcasting
45초: hana
47초: investing (+ DXY 동시 추출)
50초: Broadcasting
53초: woori (우선순위 높음)
54초: nh (Selenium Queue)
55초: kb
57초: investing (+ DXY 동시 추출)
58초: sc (Selenium Queue)
```

**특징:**
- Broadcasting 직전(3-7초)에 Request 크롤러 실행 → 최신 데이터 반영
- woori는 citi보다 우선순위 높음: 53초 실행 → 사용자 표시 시간이 실제 변경 시간과 유사
- Selenium 크롤러는 Queue 순차 처리 (Request 먼저 시도)
- 최대 동시 실행: 1-2개 (Request 기반 크롤러만)

### 설계 원칙

- **환율 고시 스케줄 기반**: 은행별 실제 운영 시간에 맞춰 크롤러 활성화/비활성화
- **mibank 딜레이 고려**: 은행 고시 종료 후에도 mibank 반영 지연 대비 (마지막 고시 누락 방지)
- **Broadcasting 동기화**: 크롤러가 Broadcasting X초 전에 실행 → 실시간 반영
- **Request 우선 전략**: Selenium 크롤러도 Request(mibank) 먼저 시도 → Queue 압력 감소
- **실시간성 > 완전성**: 타임아웃 엄격화로 빠른 실패 → Queue 정체 방지
- **리소스 분산**: OUT 모드 동시 실행 0개 → CPU 스파이크 제거
- **예측 가능성**: cron 절대 시간 → 매시간 같은 패턴
- **자동 복구**: Worker stuck 시 자동 재시작, 실패 작업 자동 재시도

## 크롤러 구조

**공통 패턴:**
- 2-3개 폴백 URL, CSS Selector 기반, 변경 시에만 DB 저장

**상세 가이드:** [CRAWLERS.md](CRAWLERS.md) (각 은행별 특수 로직, 트러블슈팅)

## API 엔드포인트

### WebSocket

- `WS /ws` - 실시간 환율 스트리밍 (매분 00, 10, 20, 30, 40, 50초)

**Payload 구조** (`build_rates_payload()` 기반):

```json
{
  "type": "rates",
  "data": {
    "rates": [ /* 30개 (10 은행 × 3 통화) */ ],
    "indices": {
      "dxy": {"rate": 99.234, "timestamp": "2026-04-18T09:07:45.189537+09:00", "source": "investing"}
    },
    "metadata": {"updated_at": "...", "currencies": [...], "banks": [...], "total_count": 30}
  },
  "graph_buckets": { /* 후속 broadcast에만 append, 초기 메시지엔 없음 */ }
}
```

- `data.indices.dxy` — **10초 해상도** DXY live tick. `crud.get_latest_dxy_rate()` 기반 (investing > yahoo). 초기 연결 메시지에도 포함됨 (Redis `BROADCAST_CACHE_KEY`가 `build_rates_payload` 결과 저장).
- `graph_buckets.usd-krw.dxy` — **분당 :03초** 집계된 10분 bucket 히스토리. 후속 broadcast에서만 append (Redis 캐시 기록 이후 단계).
- 두 경로는 해상도/갱신 주기/캐시 경로가 달라 **분리 유지**. 상세는 `DESIGN_DXY_GRAPH.md` §6.3 참조.
- 구 iOS 앱은 `indices` 필드 미인지 시 Codable이 자동 무시 (하위 호환).

### REST API (폴백용)

- `GET /api/rates` - 전체 환율 (모바일 앱용)
- `GET /api/rates/{currency}` - 특정 통화쌍
- `GET /api/investing/{pair}` - Investing.com 특정 통화
- `GET /api/banks/{pair}` - 모든 은행 특정 통화
- `GET /api/graph/{currency}` - 그래프 데이터 (파라미터: `range`=1d/1w/3m/1y, USD/KRW에 DXY 포함)
- `GET /api/news` - 환율 관련 뉴스 (파라미터: `limit`, `hours`) — Phase 1B
- `GET /health` - 헬스체크

### Admin API (HTTP Basic Auth 필요)

> 관리자 페이지 전용 API. `admin` / `ADMIN_PASSWORD` 인증 필요.

- `GET /admin/api/dashboard` - 통합 대시보드 데이터 (시스템, 크롤러, 브로드캐스트 상태)
- `GET /admin/api/logs` - 로그 조회 (파라미터: `log_type`, `hours`, `limit`, `bank`)
- `GET /admin/api/crawler-config` - 크롤러 활성화 설정 조회
- `POST /admin/api/crawler-config` - 크롤러 활성화/비활성화 토글
- `GET /admin/api/crawler/stats` - 크롤러별 통계 (성공률, 평균 실행시간)
- `GET /admin/api/queue-status` - Selenium Queue 상태 조회
- `GET /admin/api/redis-status` - Redis 메모리 및 Circuit 상태
- `GET /admin/api/monitor/current` - 현재 시스템 리소스 (메모리, CPU, Chrome)
- `GET /admin/api/monitor/history` - 시간별 리소스 히스토리 (파라미터: `hours`)

### 계정 API (Apple App Store 5.1.1(v) 준수)

- `DELETE /api/user/me` - 사용자 계정 데이터 삭제 (Firebase ID Token 필요)
  - 삭제 대상: `notification_logs`, `notification_settings`, `user_devices`
  - 응답: `204 No Content`
  - 오류: `401`(토큰 만료/무효/철회), `500`(서버 처리 오류), `503`(Firebase 연결 오류)
  - 클라이언트 권장: 401 시 재인증 후 재시도, 서버 삭제 완료 후 Firebase Auth 삭제

### 알림 API

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#-개인화-알림-동작-흐름)

- `POST /api/register-device` - FCM Device Token 등록 (Firebase ID Token 검증 필수)
- `POST /api/notification-settings` - 알림 설정 생성
- `GET /api/notification-settings` - 사용자 알림 설정 조회
- `PUT /api/notification-settings/{id}` - 알림 설정 수정 (토글 ON/OFF)
- `DELETE /api/notification-settings/{id}` - 알림 설정 삭제

**알림 생성 요청 (POST):**

```json
{
  "bank": "hana",
  "currency": "usd-krw",
  "condition": "above",
  "threshold": 1475.0,
  "is_enabled": true
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `is_enabled` | `bool` | 선택 | 활성화 여부 (기본값 `true`, `null` 전송 시 422 에러) |

**중복 처리 정책:**

- 동일 조건 (bank + currency + condition + threshold) 알림 존재 시:
  - 새로 생성하지 않고 기존 설정의 `enabled` 상태만 업데이트
  - `is_enabled=true` → `triggered=false` 초기화 (재알림 가능)
  - `is_enabled=false` → `triggered` 유지 ("발송됨" 상태 보존)

**1회성 알림 동작:**

- 알림 발송 시 자동 비활성화 (`enabled=false, triggered=true`)
- 사용자가 토글 ON (`PUT {is_enabled: true}`) → 자동 초기화 (`enabled=true, triggered=false`)

### 응답 형식

```json
{
  "rates": [
    {
      "currency": "usd-krw",
      "bank": "kb",
      "rate": 1340.50,
      "timestamp": "2025-10-04T14:30:00+09:00"
    }
  ],
  "metadata": {
    "updated_at": "2025-10-04T14:30:00+09:00",
    "currencies": ["usd-krw", "jpy-krw", "eur-krw"],
    "banks": ["investing", "kb", "hana", ...],
    "total_count": 30
  }
}
```

## 인프라 환경

### 현재 서비스 환경

**인프라 구성:**

```text
AWS Cloud (서울 리전)
├── VPC (같은 네트워크)
│   ├── EC2 t3.small (2 vCPU, 2GB RAM)
│   │   └── FastAPI + 크롤러 + Redis (Docker)
│   └── RDS PostgreSQL db.t4g.micro (프리티어)
│       └── 환율 데이터 + 사용자 데이터
│
└── 외부 서비스
    ├── Firebase Auth (로그인, 무료)
    └── Firebase FCM (푸시 알림, 무료)
```

**EC2 인스턴스:**

- **인스턴스**: t3.small (2 vCPU, 2GB RAM)
- **OS**: Ubuntu 24.04 LTS (x86_64)
- **동시 접속**: 최대 ~500명 (WebSocket)

**AWS RDS PostgreSQL:**

- **인스턴스**: db.t4g.micro (2 vCPU, 1GB RAM, ARM64)
- **스토리지**: 20GB SSD (gp2)
- **기간**: 12개월 무료 (AWS 첫 가입 후)
- **네트워크**: 같은 VPC 내 통신 (지연 1-3ms, 비용 $0)
- **12개월 후 비용**: ~$15/월

### CloudWatch 알람

| 알람 이름 | 조건 | 설명 |
|----------|------|------|
| FXi-EC2-CPU-High | CPUUtilization > 80% | EC2 CPU 과부하 |
| FXi-EC2-CPU-Credit-Low | CPUCreditBalance < 50 | EC2 버스트 크레딧 부족 |
| FXi-RDS-CPU-High | CPUUtilization > 80% | RDS CPU 과부하 |
| FXi-RDS-Memory-Low | FreeableMemory < 100MB | RDS 메모리 부족 |
| FXi-RDS-Storage-Low | FreeStorageSpace < 2GB | RDS 스토리지 부족 |

- **SNS 주제**: `fxi-alerts` (이메일 알림)
- **평가 기간**: 5분 내 1개 데이터 포인트

### 비용 예상

| 항목 | Phase 1 (12개월) | Phase 2 (12개월 후) |
|------|-----------------|-------------------|
| EC2 t3.small | $15/월 | $15/월 |
| RDS db.t4g.micro | $0 (프리티어) | ~$15/월 |
| Firebase Auth/FCM | $0 | $0 |
| **합계** | **$15/월** | **$30/월** |

### 확장 계획 (사용자 1,000명 이상 시)

1. ✅ **PostgreSQL 전환** → RDS 프리티어로 해결 (2026-01)
2. ✅ **Redis 캐시 도입** → Phase 1.7 완료 (EC2 Docker)
3. ✅ **변경 감지 시스템** → Phase 1.7 완료
4. ✅ **CloudWatch 알람** → EC2/RDS 모니터링 설정 완료 (2026-01)
5. **EC2 업그레이드** (t3.small → t3.medium, 필요 시)
6. **RDS 업그레이드** (db.t4g.micro → db.t4g.small, 필요 시)

## Docker 배포 및 관리

### 코드 변경 후 Docker 재배포 절차

#### 일상적인 코드 변경 (권장)
```bash
# 변경된 부분만 재빌드하고 컨테이너 재시작
docker compose up -d --build

# 로그 확인
docker compose logs -f
```

#### 캐시 문제 발생 시 클린 빌드
```bash
# 1. 컨테이너 중지 및 제거
docker compose down

# 2. 캐시 없이 완전 재빌드
docker compose build --no-cache

# 3. 컨테이너 시작
docker compose up -d

# 4. 로그 확인
docker compose logs -f
```

#### 완전 초기화 (개발 환경 리셋)
```bash
# 1. 컨테이너 및 볼륨 제거 (주의: 데이터 삭제됨)
docker compose down -v

# 2. 모든 Docker 리소스 정리
docker system prune -a -f

# 3. 재빌드 및 시작
docker compose up -d --build

# 4. 로그 확인
docker compose logs -f
```

### 유용한 Docker 명령어

#### 정리 작업
```bash
# 중지된 컨테이너만 제거
docker container prune -f

# 사용하지 않는 이미지 제거
docker image prune -f

# 빌드 캐시 제거
docker builder prune -f
```

### 배포 시 주의사항

1. **볼륨 유지**: `docker compose down -v`는 데이터베이스 데이터까지 삭제하므로 신중하게 사용
2. **캐시 활용**: `--no-cache`는 빌드 시간이 오래 걸리므로 문제가 있을 때만 사용
3. **로그 확인**: 배포 후 반드시 로그를 확인하여 정상 작동 여부 체크
4. **단계적 접근**: 간단한 명령어부터 시도하고, 문제가 있을 때만 전체 클린 빌드 수행

## 크롤링 리스크 대응

### 문제점

- 은행 웹사이트 구조 변경 시 크롤러 중단
- CSS Selector 무효화
- User-Agent 차단

### 대응 방안

1. **다중 폴백 URL** (현재 구현됨)
2. **정기적인 Selector 검증** (주 1회 권장)
3. **크롤링 실패 알림 시스템** (TODO)
4. ✅ **User-Agent 로테이션** (Investing 크롤러 적용, Safari/Chrome UA 풀)
5. ✅ **TLS 지문 위장** (curl_cffi + safari17_0, Cloudflare 우회) - [ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장)

## 파일 구조

```
exchange-rate/
├── app/
│   ├── __init__.py          # 로거 export
│   ├── config.py            # 환경 설정 (ENV, LOG_LEVEL, 텔레그램 등)
│   ├── logging.py           # 구조화된 로깅 시스템 (중앙 설정)
│   ├── database.py          # DB 연결 설정
│   ├── models.py            # SQLAlchemy ORM 모델
│   ├── schemas.py           # Pydantic 스키마
│   ├── crud.py              # DB CRUD 로직
│   ├── cache.py             # Redis 클라이언트 (Circuit Breaker) - Phase 1.7
│   ├── latest_rates_cache.py # Redis latest mirror layer (PR3-PR5, ADR-026)
│   ├── main.py              # FastAPI 서버, WebSocket
│   ├── scheduler.py         # APScheduler, 4단계 모드 전환
│   ├── source_registry.py   # USDT Phase 1 source 메타데이터 (SourceDefinition)
│   │
│   ├── crawlers/            # 크롤러 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── constants.py     # 크롤러 공통 상수 (HEADERS, SELENIUM_OPTIONS, TIMEOUT)
│   │   ├── utils.py         # 크롤러 공통 함수 (parse_rate_text, create_selenium_driver)
│   │   ├── runner.py        # Selenium 크롤러 subprocess runner
│   │   ├── investing.py     # Investing.com 크롤러
│   │   ├── kb.py            # KB은행 크롤러
│   │   ├── hana.py          # 하나은행 크롤러
│   │   ├── shinhan.py       # 신한은행 크롤러
│   │   ├── woori.py         # 우리은행 크롤러
│   │   ├── ibk.py           # IBK기업은행 크롤러
│   │   ├── nh.py            # NH농협은행 크롤러
│   │   ├── sc.py            # SC제일은행 크롤러
│   │   ├── bs.py            # 부산은행 크롤러
│   │   ├── citi.py          # 씨티은행 크롤러
│   │   ├── dxy.py           # DXY 폴백 모듈 (/currencies/us-dollar-index + Yahoo Finance) - Phase 1A
│   │   └── usdt_sources.py  # USDT/KRW 5개 거래소 통합 크롤러 (Phase 1, fan-out)
│   │
│   ├── admin/               # 관리자 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── log_reader.py    # 로그 조회 (관리자 페이지용, 백엔드 bank 필터링)
│   │   ├── log_cleaner.py   # 오래된 로그 파일 자동 삭제
│   │   ├── stats.py         # WebSocket 브로드캐스트 통계 수집
│   │   ├── crawler_stats.py # 크롤러 통계 수집 (성공률, 실행시간)
│   │   ├── monitor.py       # 시스템 모니터링 (메모리, CPU, Chrome) - Phase 1.5
│   │   ├── graph_cache.py   # 그래프 데이터 Redis 캐시 - Phase 1A
│   │   └── dxy_rollup.py   # DXY realtime → hourly/daily 집계 (rollup)
│   │
│   ├── news/                # 뉴스 피드 도메인 (Phase 1B)
│   │   ├── __init__.py
│   │   ├── sources.py       # RSS 소스 정의 (NewsSource + NEWS_SOURCES)
│   │   ├── filters.py       # 필터 시스템 (잡음/관련도/macro/severity/industry)
│   │   ├── fetcher.py       # RSS 수집 + 파싱 + upsert + cleanup
│   │   ├── kb_fetcher.py    # KB API 수집 (속보, [전문] PDF 추출)
│   │   └── upsert.py        # 공통 Redis upsert (KB↔RSS 병합 규칙)
│   │
│   └── notifications/       # 알림 도메인 (2025-10-25 리팩토링)
│       ├── __init__.py
│       ├── fcm.py           # Firebase Cloud Messaging 푸시 알림 (Phase 2)
│       └── telegram.py      # 텔레그램 알림 (관리자 알림용)
│
├── scripts/
│   ├── backfill_history.py              # DXY 히스토리 백필 (yfinance, daily/hourly)
│   └── migrate_market_index_granularity.py  # granularity 컬럼 마이그레이션
├── data/
│   └── exchange_rates.db    # SQLite DB
├── logs/                    # 로그 파일 (자동 생성)
│   ├── app.log              # 모든 운영 로그 (INFO+, 크롤러 포함)
│   └── error.log            # 에러/경고만 (WARNING+)
├── static/                  # 은행 아이콘
├── templates/               # 관리자 웹 UI (순수 HTML+JS, Jinja2 미사용)
│   ├── index.html           # 메인 대시보드 (API 확인)
│   └── admin.html           # 관리자 페이지
├── .env                     # 환경 변수 (git 제외)
├── .gitignore               # Git 제외 파일
├── requirements.txt         # Python 패키지
├── CLAUDE.md                # 이 파일 (프로젝트 가이드)
├── CRAWLERS.md              # 크롤러 특수 로직 가이드
├── DECISIONS.md             # 아키텍처 의사결정 기록 (ADR)
├── CHANGELOG.md             # 버전별 변경사항 및 마이그레이션 가이드
├── DOCKER.md                # Docker Compose 구조 및 확장 전략
├── DEPLOYMENT.md            # EC2 인스턴스 설정 및 실전 배포 절차
├── REBOOT_CHECKLIST.md      # 재부팅 후 검증 절차 가이드
├── MAINTENANCE_2025-11-05.md # 성능 개선 및 모니터링 시스템 구축
└── MAINTENANCE_2025-11-06.md # AsyncIO Queue 도입 (Semaphore 경합 제거)
```

> 💡 **구조 설계 원칙**: API-first 서비스 (모바일 앱이 메인, 웹은 관리자 전용)
> **확장성**: React 전환 시 `frontend/` 폴더 추가만 하면 됨 (Backend/Frontend 약한 결합)

## 주요 로직

**중복 방지 INSERT**: 마지막 레코드와 비교, 변경 시에만 저장 (`crud.py`)

**WebSocket 브로드캐스트**: 매분 00, 10, 20, 30, 40, 50초 정확한 시간, APScheduler cron job, 변경사항 있을 때만 전송 (`main.py`, `scheduler.py`)

**브로드캐스트 통계**: 성공률, 평균 주기, 건강 상태 추적 (`admin/stats.py`)

## 개발 가이드라인

**크롤러 추가**: [CRAWLERS.md](CRAWLERS.md) "새 은행 추가 가이드" 참고

**DB 마이그레이션**: SQLAlchemy 자동 생성, 변경 시 Alembic 권장

### 로깅 시스템

**구조화된 로깅**: JSON 형식, KST 타임존, exception() 강화

**파일**:
- `app.log` - 모든 운영 로그 (INFO+, 크롤러 포함)
- `error.log` - 에러/경고만 (WARNING+)

**환경 설정** (`.env`):
- `ENV`: development (컬러) / production (JSON)
- `LOG_LEVEL`: DEBUG / INFO / WARNING / ERROR

**로거 사용**:
```python
logger = logging.getLogger("exchange_rate.crawler.kb")
logger.info("⚡️ 환율 변경", extra={"pair": "usd-krw", "rate": 1340.5})
logger.exception("크롤링 실패", extra={"bank": "kb"})  # except 블록
```

**관리**: 로테이션 10MB, 백업 3개, 10일 자동 삭제

**상세 가이드**: `app/logging.py`, `app/admin/log_reader.py`

## 보안 고려사항

- [x] **HTTPS/SSL 적용** (Let's Encrypt, 2025-11-17)
  - 도메인: `fxi.kr`
  - 자동 갱신: Systemd Timer (매일 KST 04:30, 05:30)
  - TLS 1.2 & 1.3, HSTS 헤더 (1년)
- [x] **민감 정보 환경 변수화** (python-dotenv, .env 파일)
- [ ] CORS 설정 (특정 도메인만 허용)
- [ ] API Rate Limiting (현재: Nginx 3 req/s)

## 관리자 페이지 (/admin)

### Phase 1: 심플 대시보드 ✅ 완료 (2025-10-17)

**핵심 기능**:
- 4개 대시보드 카드 (WebSocket, 메모리, 브로드캐스트, 에러)
- 크롤러 상태 뱃지 (에러 개수, 클릭 시 로그 필터링)
- 로그 뷰어 2개 탭 (실시간 모니터링, 에러 추적)
- 자동 새로고침 30초

**접속**: `http://localhost:8000/admin` (admin / .env의 ADMIN_PASSWORD)

**기술**: Vanilla JS, HTTP Basic Auth, 통합 API (`/admin/api/dashboard`)

### Phase 1.5: 모니터링 시스템 강화 ✅ 완료 (2025-11-05)

**배경**: 시간이 지날수록 시스템이 느려지는 문제 해결

**추가된 기능**:
- **Chrome 프로세스 모니터링 카드**: 프로세스 개수 + 메모리 사용량 실시간 표시
- **자동 정리 시스템**: 좀비 Chrome 프로세스 매분 03/23/43초 자동 정리 (60초 이상 실행된 것)
- **리소스 모니터링**: 메모리/CPU 사용량 5분마다 자동 수집 및 히스토리 저장 (5시간)
- **임계값 알림**: 메모리 70% 이상, Chrome 프로세스 3개 이상 시 경고

**모니터링 API**:
- `GET /admin/api/monitor/current` - 현재 시스템 상태
- `GET /admin/api/monitor/history?hours=1` - 시간별 히스토리 (Chart.js용)

**핵심 개선사항**:
- `app/crawlers/utils.py`: driver.quit() 실패 시 강제 종료
- `app/scheduler.py`: 좀비 프로세스 정리 + 모니터링 통계 수집
- `app/admin/monitor.py`: 시스템 모니터링 모듈 (신규)
- `app/main.py`: WebSocket 연결 관리 안정화

**상세 내역**: [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md)

### Phase 1.6: Selenium Queue 모니터링 ✅ 완료 (2025-11-13)

**배경**: Selenium 크롤러 Queue 상태 가시성 확보 및 중복 작업 방지

**추가된 기능**:
- **Queue 모니터링 카드**: 실시간 Queue 크기, 사용률, 현재 처리 작업, 대기 작업 목록 표시
- **중복 작업 방지**: Worker 처리 중 + Queue 대기 중 체크로 중복 작업 제거
- **사용률 시각화**: 60% 미만(녹색), 60-80%(주황), 80% 이상(빨강)
- **대기 작업 뱃지**: 일반/재시도 구분, 대기 시간 표시

**모니터링 API**:
- `GET /admin/api/queue-status` - Selenium Queue 상태 조회 (10초마다 자동 업데이트)

**핵심 개선사항**:
- `app/scheduler.py`: Queue 중복 방지 로직, 상태 캐시, 10초마다 모니터링
- `app/main.py`: Queue 상태 API 엔드포인트 추가
- `templates/admin.html`: Queue 모니터링 카드 및 실시간 업데이트

### Phase 1.7: Redis 브로드캐스트 캐시 ✅ 완료 (2025-11-27)

**배경**: WebSocket 초기 접속 최적화 및 변경 감지 시스템 구축

**추가된 기능**:
- **Redis 브로드캐스트 캐시**: 초기 접속 시 DB 대신 Redis 캐시로 즉시 데이터 전송
  - Cache key: `broadcast:latest` (3.6KB JSON)
  - Memory: ~1MB stable (1% of 100MB)
  - Circuit Breaker: 5 failures → 30s timeout → auto-recovery
- **변경 감지 시스템**: JSON 비교로 실제 변경 시에만 브로드캐스트
  - "⏸️ 변경사항 없음" vs "📡 브로드캐스트 완료" 구분 로깅
  - 대역폭 최적화: 변경 없으면 전송 스킵
- **Redis 모니터링 카드**: 실시간 메모리, 키 개수, Circuit 상태 표시

**버그 수정**:
- Broadcasting `updated_at` 타임스탬프 문제 (항상 다른 값 생성)
  - `datetime.now()` → DB의 실제 최신 `max(rate["timestamp"])` 사용
  - 변경 감지 정확도 100% 달성

**모니터링 API**:
- `GET /admin/api/redis-status` - Redis 메모리, 키, Circuit 상태 조회

**핵심 개선사항**:
- `app/main.py`: Broadcasting 버그 수정, Redis 상태 API, 변경 감지 로직
- `app/cache.py`: Circuit Breaker 기반 Redis 클라이언트
- `templates/admin.html`: Redis 모니터링 카드 추가

### Phase 1.8: 크롤러 제어 ✅ 완료 (2025-12-02)

**배경**: 특정 크롤러의 일시적 비활성화 필요 (은행 사이트 점검, 차단 대응)

**추가된 기능**:
- **크롤러 활성화/비활성화**: 관리자 페이지에서 개별 크롤러 토글
- **crawler_config 테이블**: 크롤러별 enabled 상태 저장
- **실시간 반영**: 토글 즉시 스케줄러에 반영

**관련 스키마**: `crawler_config` 테이블 (CLAUDE.md 데이터베이스 스키마 참조)

### Phase 1.9: Redis-first broadcast hot path ✅ 완료 (2026-05-04)

**배경**: 매초 broadcast가 DB latest SELECT (rates + DXY)에 직접 묶여 정각 spike 발생 ([ADR-026](DECISIONS.md#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성))

**추가된 기능**:
- **신규 모듈** `app/latest_rates_cache.py`: Redis latest mirror layer
- **mirror keys**: `latest:bank:{bank}:{currency}`, `latest:source:{source}:{asset}`, `latest:investing:{currency}`, `latest:dxy:current`
- **제어 key**: `latest:index` (atomic snapshot 일관성)
- **mirror cycle**: APScheduler IntervalTrigger 3초
- **broadcast Redis-first read**: `fetch_rates_from_redis()` (MGET 1회 통합)
- **DXY mirror + DXY-only fallback** (PR5): rates 성공 + DXY 실패 시 DXY만 DB
- **신규 env**: `REDIS_LATEST_ENABLED`, `LATEST_MIRROR_INTERVAL_SECONDS`
- **신규 메트릭**: `latest_source` / `mirror_age_ms` / `latest_index_get_ms` / `latest_data_get_ms` / `latest_decode_ms` / `latest_dxy_get_ms` / `dxy_path` / `latest_dxy_fallback_reason` / `payload_assemble_ms` / `payload_assemble_without_dxy_ms` / `payload_build_unmeasured_ms`

**측정 결과** (PR5 24h 누적, n=31495):
- payload_build_ms p99 **30.25ms** (PR3.5 baseline 195ms → 6.4× 가속)
- DB hot path **100% → 0%** (rates + DXY 모두 Redis-first hit 100%)
- dxy_query_ms count **1827 → 0**
- mirror_age_ms p99 2301ms (3초 ceiling 안정)
- 영업시간 IN mode (n=1801): p99 75.97ms (Redis read jitter ~2.5×, DB 경합 아님)

**핵심 파일**:
- `app/latest_rates_cache.py` (신규, mirror + Redis-first read + DXY mirror)
- `app/main.py` (broadcast hot path Redis-first 분기 + 분해 계측)
- `app/scheduler.py` (mirror job 등록)
- `app/config.py` (env)
- `scripts/analyze_broadcast_metrics.py` (메트릭 집계)

### Phase 1A: DXY 그래프 보조지표 ✅ 완료 (2026-03-10)

**배경**: USD/KRW 환율 그래프에 달러지수(DXY)를 보조지표로 표시 ([ADR-019](DECISIONS.md#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략))

**추가된 기능**:
- **DXY 동시 추출**: investing.py에서 exchange-rates-table의 `#sb_last_8827` 셀렉터로 환율과 함께 추출
  - CDN stale cache 문제 해결: `/indices/usdollar` 대신 exchange-rates-table 사용 (캐시 우회)
  - 독립 cron job 불필요 — Tier A (investing) 크롤러와 동일 타이밍
  - `crawler_config` 테이블에 `dxy` 행 없음 (investing에 종속)
- **DXY 폴백 모듈** (`dxy.py`): 동시 추출 실패 시 순차 폴백
  - 2차: `/currencies/us-dollar-index` (curl_cffi)
  - 3차: Yahoo Finance (yfinance)
  - 60초 쿨다운: 폴백 호출 간격 제한
- **market_index_rates 테이블**: 범용 시장 지수 테이블 (granularity 컬럼)
- **그래프 API 확장**: `/api/graph/{currency}?range=1d|1w|3m|1y`
  - USD/KRW에만 DXY 보조지표 포함 (API key=`dxy`, UI 표시명=달러지수)
  - 1d: 10분 버킷 (realtime only)
  - 1w: 1시간 버킷 — **full-window 전략** (hourly + realtime 전체 7일 조회, hourly > realtime dedup)
  - 3m/1y: 1일 버킷 — **daily + recent realtime tail 전략** (daily 전체 + realtime 최근 7일 overlap)
- **기간별 DXY 쿼리 전략** (graph_cache.py `build_period_dxy_series()`):
  - 1w: 단일 쿼리, `PARTITION BY timestamp`에서 hourly > realtime 우선 → gap 있어도 realtime이 자연 보충
  - 3m/1y: daily 전체 윈도우 조회 + realtime은 `last_bf_ts - 7일`부터만 tail 조회 (`_DXY_DAILY_REALTIME_TAIL_DAYS = 7`)
  - 상수: `_DXY_DAILY_REALTIME_TAIL_DAYS = 7` (graph_cache.py line 31)
- **히스토리 백필 스크립트**: yfinance로 daily(1년)/hourly(7일) 데이터 사전 적재
- **DXY rollup 스케줄**: realtime → hourly(매시 :05) / daily(매일 00:05 KST) 자동 집계
  - 백필 종료 이후 구간을 rollup이 연속 커버 (Yahoo 재실행 불필요)
  - source 보존: 원본 realtime의 실제 source를 그대로 사용
  - idempotent: INSERT ON CONFLICT UPDATE
- **수동 gap 복구 함수** (`app/admin/dxy_rollup.py`):
  - `backfill_hourly_range(start_utc, end_utc)`: UTC 구간의 realtime → hourly 일괄 생성 (배타 상한)
  - `backfill_daily_range(start_kst, end_kst)`: KST 날짜 구간의 hourly/realtime → daily 일괄 생성 (배타 상한)

**배포 순서** (운영 환경, 상세: [DEPLOYMENT.md](DEPLOYMENT.md#db-마이그레이션-v1120-dxy-granularity)):
1. `git pull` → `docker compose build` (새 이미지 빌드)
2. `docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py`
3. `docker compose up -d` (서비스 시작)
4. `docker compose exec fastapi python scripts/backfill_history.py`

**핵심 파일**:
- `app/crawlers/investing.py`: DXY 동시 추출 (exchange-rates-table `#sb_last_8827`)
- `app/crawlers/dxy.py`: DXY 폴백 모듈 (/currencies/us-dollar-index + Yahoo Finance)
- `app/models.py`: MarketIndexRate 모델 (granularity 컬럼)
- `app/admin/graph_cache.py`: 그래프 시계열 구축 (2-part merge, 버킷 집계)
- `app/admin/dxy_rollup.py`: DXY realtime → hourly/daily rollup
- `app/crud.py`: DXY CRUD (insert, get_latest, get_for_period)

### Phase 2: FCM 푸시 알림 ✅ 완료 (2025-12-21)

**배경**: 앱 종료 상태에서도 환율 알림 필요 ([ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification))

**추가된 기능**:
- **Firebase Auth 연동**: ID Token 검증으로 사용자 인증
- **FCM Device Token 관리**: 디바이스별 푸시 토큰 저장
- **알림 설정 API**: 목표 환율 도달 시 푸시 알림 발송
- **1회성 알림**: 발송 후 자동 비활성화, 토글 ON으로 재활성화

**알림 API**:
- `POST /api/register-device` - FCM Device Token 등록
- `POST /api/notification-settings` - 알림 설정 생성
- `GET /api/notification-settings` - 알림 설정 조회
- `PUT /api/notification-settings/{id}` - 알림 설정 수정/토글
- `DELETE /api/notification-settings/{id}` - 알림 설정 삭제

**핵심 파일**:
- `app/notifications/fcm.py`: Firebase Admin SDK 초기화, 푸시 발송
- `app/crud.py`: 알림 설정 CRUD (mark_setting_triggered 등)
- `app/main.py`: 알림 API 엔드포인트

**상세 가이드**: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md)

### Phase 1B: 환율 뉴스 피드 ✅ 완료 (2026-03-28)

**배경**: 환율 변동의 원인을 이해할 수 있는 뉴스 제공

**데이터 소스**:
- **RSS** (연합인포맥스 4개 피드): 채권/외환, 국제뉴스, 해외주식, 증권 — ~2시간 딜레이
- **KB API** (fx.kbstar.com): 동일 인포맥스 뉴스를 딜레이 없이 제공 — 속보 소스

**핵심 기능**:
- **잡음 제외 필터**: 인사/부고/정치 키워드 제외 (noise_only 일원화)
- **KB↔RSS 병합**: nsid 기반 upsert, RSS 유효값 우선, published_at min()
- **content_type 분류**: `external_link`(일반 기사), `report_pdf`(은행 보고서 PDF 직링크)
- **`*` 기사 처리**: 제목에 "(본문없음)" 추가, link 유지
- **`[전문]` 기사 처리**: KB 상세에서 PDF URL 추출, report_pdf로 분류
- **시간순 정렬**: 순수 published_at 내림차순
- **초보/상보 near-duplicate collapse**: 시간 클러스터 방식
- **Redis-only 저장**: 24시간 윈도우, DB 불필요

**스케줄링**:
- KB API: 5분마다 :15초 (외환+경제 탭, 2페이지씩)
- RSS: 5분마다 :45초 (ETag/Last-Modified 조건부 GET)
- 모드 무관 (24시간 동일)

**뉴스 API**: `GET /api/news` (파라미터: `limit`, `hours`)

**핵심 파일**:
- `app/news/sources.py`: RSS 소스 정의
- `app/news/filters.py`: 잡음 제외 필터 + 제목 정규화
- `app/news/fetcher.py`: RSS 수집
- `app/news/kb_fetcher.py`: KB API 수집 ([전문] PDF 추출)
- `app/news/upsert.py`: 공통 Redis upsert (KB↔RSS 병합 규칙)

**구현 참고 문서**: [NEWS_IMPL_SPEC.md](NEWS_IMPL_SPEC.md) (임시, 안정화 후 삭제 예정)

### USDT Phase 1: 테더 탭 백엔드 foundation ✅ 완료 (2026-04-23)

**배경**: 김치프리미엄 전략(KRX 선물 매도 + USDT 매수 헷지) 사용자를 위한 거래소 간 USDT/KRW 비교

**설계 원칙** (Decision E):

- 기존 bank/investing 세계는 유지 (API/앱 호환성)
- 새 source 기반 세계를 별도 도입 (`source + asset`)
- API 표면은 `bank + currency`로 어댑터 변환 (기존 클라이언트 호환)

**추가된 기능**:

- **거래소 5종 수집**: 업비트, 빗썸, 코인원, 고팍스, 코빗 USDT/KRW (REST polling 10초, 24/7)
- **새 데이터 모델**: `source_rates`, `source_notification_settings`, `source_notification_logs`
- **기존 API 확장**: `/api/rates`, `/api/rates/usdt-krw`, WebSocket `rates` 배열에 usdt-krw 엔트리 자동 포함
- **Source 알림 API**: `/api/source-notification-settings` (4종, 거래소 전용, reference는 400 + 기존 API 안내)
- **30일 보관 cleanup**: 매일 03:31

**알림 API**:

- `POST /api/source-notification-settings` - source 기반 알림 생성 (category=exchange만 허용)
- `GET /api/source-notification-settings` - 알림 설정 조회 (asset 필터 지원)
- `PUT /api/source-notification-settings/{id}` - 알림 설정 수정 (source/asset 변경 시 재검증)
- `DELETE /api/source-notification-settings/{id}` - 알림 설정 삭제 (멱등성)

**핵심 파일**:

- `app/source_registry.py`: SourceDefinition 데이터클래스 (9개 소스 메타데이터, canonical key는 DB 저장 안 함)
- `app/crawlers/usdt_sources.py`: 5개 거래소 통합 크롤러 (ThreadPoolExecutor fan-out)
- `app/models.py`: SourceRate, SourceNotificationSetting, SourceNotificationLog
- `app/crud.py`: source CRUD + `get_source_rates_as_legacy_format` 어댑터 + `process_source_rate_alerts`

**FCM payload type**: `source_rate_alert` (기존 `rate_alert`와 구분, 기존 앱은 unknown type 무시)

**알려진 기술 부채**:

- `metadata.banks` / `metadata.currencies` dead fields (iOS/Android에서 파싱만 되고 사용 안 됨, non-optional이라 즉시 제거 불가)
- 상세: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md#known-tech-debt)

**설계/구현 문서**:

- [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md): 제품 방향 + Decision A~F
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): 백엔드 설계 + 구현 순서 + 기술 부채
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): iOS/Android 공통 클라이언트 설계 가이드 (RateSource 모델, SourceRegistry, FCM type 분기)

**향후 Phase**:

- Phase 2: 테더 탭 그래프 (DXY와 동일한 rollup 전략), KRX 달러선물 (재배포 권리 확인 후)
- Phase 3: 비교 알림 (`comparison_alerts` 스키마는 Phase 1 설계 문서에서 잠김)

### KRX 미국달러선물 (KIS Open API) — PR6 사전 골격 ⚙️ 진행 중 (2026-05-04 시점)

**배경**: 김치프리미엄 전략 사용자가 거래소 USDT 외에 KRX 미국달러선물(USDF) 호가/체결도 함께 보고자 함. 기준 만기 종목 단축코드 예: A75605 (2026-05-18 만기, KIS master로 동적 resolve).

**핵심 원칙 — KRX optional source**: 서버/앱의 baseline 서비스(은행 + investing + USDT)는 KRX 없이 항상 정상 동작. KRX는 선택적 데이터 소스로 격리되며, 어느 단계의 실패도 다른 startup/shutdown/broadcast 경로에 영향 없음.

**격리 토글 (env)**:

| Env | Default | Scope |
| --- | --- | --- |
| `KIS_APP_KEY` / `KIS_APP_SECRET` | 미설정 | 미설정 시 bootstrap warning 후 격리 (다른 서비스 영향 X) |
| `KRX_FUTURES_ENABLED` | `false` | WebSocket 연결 / 신규 DB 저장 (수집 lifecycle) |
| `KRX_BROADCAST_INCLUDE` | `false` | mirror `latest:index` 포함 + broadcast `rates` 노출 |

**Canary 단계** (env 토글로 단계적 활성화):

1. **Stage 0 (현재)**: `KRX_FUTURES_ENABLED=false`. 코드는 배포되어 있으나 lifecycle 자체가 비활성. 운영 영향 0.
2. **Stage 1 — DB 저장만**: `KRX_FUTURES_ENABLED=true` + `KRX_BROADCAST_INCLUDE=false`. KIS WebSocket → DB(`source_rates`)까지만. mirror가 broadcast가 읽는 `latest:index`에 KRX key를 포함시키지 않음 → broadcast 노출 X (Redis `latest:source:krx:*` data key 잔존 여부와 무관 — `latest:index`가 게이트). KRX tick 정상성 + DB write throughput을 24h 관찰.
3. **Stage 2 — broadcast 노출**: `KRX_BROADCAST_INCLUDE=true` 추가. mirror cycle이 KRX latest data key를 갱신하고 `latest:index`에 포함시켜 broadcast rates 배열에 `source="krx", asset="usd-krw-futures"` 등장. 클라이언트 호환성 검증 후 활성화.
4. **롤백**: 어느 단계에서든 KRX 장애 / KIS API 점검 / 데이터 이상 발견 시 해당 토글 `false`로 격리. 환경변수 변경 후 process restart 필요 (config는 import 시 1회 읽기). restart 후 visible effect: `KRX_FUTURES_ENABLED=false`는 즉시 lifecycle 미시작 (수집 중단), `KRX_BROADCAST_INCLUDE=false`는 다음 정상 mirror cycle (≤3초)에 `latest:index`에서 KRX key 제외 (broadcast 미노출).

**현재 구현 상태 (PR6 ~ PR6c-2c)**:

- ✅ KIS WebSocket adapter (H0CFCNT0/H0CFASP0 주간, H0MFCNT0/H0MFASP0 야간) + 호가 tick fanout 차단 + PINGPONG 처리
- ✅ KisApprovalManager (approval_key cache + 만료 5분 마진 + asyncio.Lock)
- ✅ 만기 자동 resolve: `select_active_usd_futures_contract` (KIS 상품 마스터 cp949 fixed-width 파싱, 만기일 정규세션 11:30:00 inclusive 기준 intraday rollover)
- ✅ KrxDbWriter (1초 window debounce + insert-if-changed + asyncio.to_thread DB write + race-prevention finally)
- ✅ Latest mirror skip 분기 (`KRX_BROADCAST_INCLUDE=false`일 때 broadcast index에서 제외)
- ✅ Scheduler lifecycle: `start_krx_futures_client` / `_bootstrap_krx_futures_client` / `shutdown_krx_futures_client` (background bootstrap → main.py lifespan blocking 방지, current-task 매칭 finally cleanup)
- ⏸ 세션 boundary 자동 재시작 (cron) — 5/18 만기일 운영 관찰 후 결정 (검증되지 않은 시각 미투입)
- ⏸ REST snapshot/fallback (PR6d) — WebSocket 끊김 시 분당 1회 REST quote로 latest 보전, ADR-027 작성 예정

**핵심 파일**:

- `app/sources/kis_master.py`: 상품 마스터 fetcher + ContractInfo + active contract resolver
- `app/sources/kis_futures.py`: H0CFxxx0/H0MFxxx0 column spec + payload parser + session/holiday helpers
- `app/crawlers/krx_kis.py`: KisApprovalManager + KisFuturesClient + KrxDbWriter
- `app/scheduler.py`: KRX lifecycle helpers (`resolve_active_krx_futures_contract` side-effect free + bootstrap/shutdown)
- `app/latest_rates_cache.py`: `should_include_source_in_latest()` 분기 (KRX broadcast 토글)
- `tests/test_krx_kis.py`, `tests/test_krx_scheduler.py`: 격리/회귀 검증

### Phase 3: 고급 기능 (사용자 500명+, 예정)
- 통계 & 분석 (Chart.js, 성공률 그래프)
- 환율 이상치 감지 (ML)
- 다중 알림 조건 지원


## 문서 관리 가이드라인

### 자동 Reminder (CRITICAL) 🚨

**IMPORTANT:** 다음 조건에 해당하는 코드 변경 후, **반드시** 사용자에게 질문하세요:

> "문서 업데이트 확인을 위해 `/docs-check`를 실행하시겠습니까?"

**트리거 조건:**
- `app/crawlers/*.py` 파일 수정 (크롤러 로직)
- `app/models.py`, `app/schemas.py` 수정 (DB 스키마)
- 새 기능 추가 (feat: 커밋)
- 주요 리팩토링 (refactor: 커밋)
- 새 은행/통화 추가

**사용자 응답:**
- "Yes" / "Okay" → `/docs-check` 실행
- "No" / "나중에" → 건너뜀 (명시적 선택, 기록)

**중요**: 이 Reminder는 **선택 사항이 아닙니다**. 위 조건에 해당하면 항상 물어보세요.

---

### 문서 역할 정의

이 프로젝트는 **3개의 주요 문서**로 구성되어 있으며, 각각 명확한 역할이 있습니다:

| 문서 | 역할 | 업데이트 트리거 | 예시 |
|------|------|----------------|------|
| **CLAUDE.md** | 프로젝트 전체 가이드 | 아키텍처 변경, 기술 스택 추가, DB 스키마 변경 | PostgreSQL 전환, 새 은행 추가 (개수 변경) |
| **CRAWLERS.md** | 크롤러 구현 세부사항 | 크롤링 로직 변경, 특수 로직 추가, 주의사항 발견 | SC 방식 적용, Alert 처리 변경 |
| **DECISIONS.md** | 아키텍처 의사결정 (ADR) | 기술 선택, 트레이드오프 결정 (4가지 기준 충족) | WebSocket vs Node.js, Docker 채택 |

### 문서 업데이트 기준 (간략)

**CRAWLERS.md**: 크롤러 로직 변경, 특수 로직 추가, 트러블슈팅 패턴
**DECISIONS.md**: 아키텍처 결정 (ADR 4가지 기준: 비가역성, 영향 범위, 대안 존재, 장기 유지)
**CLAUDE.md**: 은행/통화 개수 변경, DB 스키마 변경, 기술 스택 변경

**도구**: `/docs-check` - 변경된 파일 분석 후 자동으로 문서 업데이트 체크리스트 제시

---

## 향후 개선 사항

- ✅ 구조화된 로깅, 관리자 페이지, 도메인 기반 구조
- ✅ Redis 브로드캐스트 캐시, 변경 감지 시스템 (Phase 1.7)
- ✅ FCM 푸시 알림, Firebase Auth 연동 (Phase 2)
- ✅ RDS PostgreSQL 전환 (2026-01)
- ✅ iOS 앱스토어 출시 완료 (2026-01-21)
- ✅ DXY 보조지표 그래프 (Phase 1A, 2026-03-10)
- ✅ Android 출시 완료 (2026-03-13)
- ✅ 환율 뉴스 피드 — RSS + KB API 병행 수집, noise_only 24h (Phase 1B, 2026-03-28)
- 🔜 CI/CD, 유닛 테스트
