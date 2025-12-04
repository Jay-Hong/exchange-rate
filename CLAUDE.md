# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

### 📚 주요 문서 가이드

**핵심 가이드:**
> 💡 **아키텍처 의사결정:** [DECISIONS.md](DECISIONS.md) - 주요 기술 선택과 그 근거 (ADR)
> 🕷️ **크롤러 구현:** [CRAWLERS.md](CRAWLERS.md) - 각 은행별 크롤링 방식과 특수 로직
> 📝 **변경 이력:** [CHANGELOG.md](CHANGELOG.md) - 버전별 변경사항 및 마이그레이션 가이드
> 🔔 **알림 & 구독:** [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md) - 푸시 알림, 인증, 구독 관리 가이드

**배포 및 운영:**
> 🐳 **Docker 아키텍처:** [DOCKER.md](DOCKER.md) - Docker Compose 구조 및 확장 전략
> 🚀 **AWS 배포:** [DEPLOYMENT.md](DEPLOYMENT.md) - EC2 인스턴스 설정 및 실전 배포 절차
> 🔄 **재부팅 절차:** [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md) - 재부팅 후 검증 체크리스트

**유지보수 기록:**
> 🔧 **2025-11-06:** [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) - AsyncIO Queue 도입 (Semaphore 경합 제거)
> 🔧 **2025-11-05:** [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md) - 성능 개선 및 모니터링 시스템 구축

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
크롤러(10개) → Scheduler → SQLite → WebSocket(10초) → 클라이언트

[서비스 환경]
크롤러(10개) → Scheduler → RDS PostgreSQL → WebSocket(10초) → 클라이언트
                                    ↓
                          Firebase Auth + FCM → 푸시 알림
```

### 데이터 소스

- Investing.com (기준 환율)
- 9개 은행: KB, 하나, 신한, 우리, IBK, NH, SC제일, 부산, 씨티

### 지원 통화

- USD-KRW, JPY-KRW, EUR-KRW

## 기술 스택

### 백엔드

- **언어**: Python 3.x
- **웹 프레임워크**: FastAPI
- **DB**: SQLite (개발/테스트) → AWS RDS PostgreSQL (서비스 시작 시)
- **ORM**: SQLAlchemy
- **스케줄러**: APScheduler (AsyncIOScheduler)
- **크롤링**: requests + BeautifulSoup4, Selenium
- **SSL/TLS**: Let's Encrypt (Certbot 자동 갱신, 90일 주기)
- **리버스 프록시**: Nginx (HTTPS, HTTP/2, wss://)

### 프론트엔드

- **iOS**: Swift (네이티브)
- **Android**: Kotlin + Jetpack Compose
- **웹**: Vanilla JS + WebSocket (관리자용)

### 실시간 통신

- **프로토콜**: WebSocket Secure (wss://)
- **도메인**: https://fxi.n-e.kr
- **브로드캐스트**: 매분 00, 10, 20, 30, 40, 50초 (정확한 시간)
- **하트비트**: Ping/Pong (30초)
- **암호화**: TLS 1.2 & 1.3

## 데이터베이스 스키마

### investing_exchange_rates

```sql
id         INTEGER PRIMARY KEY
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (KST)
```

### bank_exchange_rates

```sql
id         INTEGER PRIMARY KEY
bank       TEXT (INDEX)
currency   TEXT (INDEX)
rate       REAL
timestamp  DATETIME (KST)

-- 복합 인덱스: (bank, currency, timestamp)
```

### crawler_config (Phase 1.8)

```sql
id            INTEGER PRIMARY KEY
crawler_name  TEXT (UNIQUE, INDEX)
enabled       BOOLEAN (DEFAULT: TRUE)
updated_at    DATETIME (KST)

-- 크롤러 활성화/비활성화 설정 (관리자 페이지에서 제어)
```

### 알림 관련 테이블 (서비스 환경, 예정)

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#필요한-db-테이블-rds-postgresql)

- `user_devices` - FCM Device Token 저장 (user_id, device_token, platform)
- `notification_settings` - 알림 조건 설정 (bank, currency, condition, threshold)
- `notification_logs` - 알림 발송 히스토리 (중복 방지)

## 데이터 관리 정책

- **은행 데이터**: 10일분만 유지 (단기 비교용)
- **인베스팅 데이터**: 장기 보관 (그래프/분석용)
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
  - 10개 은행 전체 크롤링

- **BREAK1 모드**: 월~금 21:00 ~ 익일 02:59 (심야, 9개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: sc
  - 유지: investing, kb, hana, woori, shinhan, bs, citi, ibk, nh
  - ibk는 00:00부터 Selenium만 사용 (날짜 변경 필요, Request 불가)

- **BREAK2 모드**: 월 06:00~07:59, 화~금 03:00~07:59, 토 03:00~06:59 (고시 마무리, 6개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori (02:45 종료), ibk (02:05 종료), shinhan (02:30 종료), sc (21:00 종료)
  - 유지: investing, kb, hana, bs, citi, nh

- **OUT 모드**: 토 07:00 ~ 월 06:00 전 (주말, 5개 크롤러)
  - Broadcasting: 매분 00, 10, 20, 30, 40, 50초 (동일)
  - 제외: woori, ibk, shinhan, sc, citi (주말 고시 없음)
  - 유지: investing, kb, hana, bs, nh (주말 가끔 변동)
  - 크롤러: cron 시간 단위 (완전 분산, 동시 실행 0개)

### 은행별 환율 고시 스케줄

| 은행 | 고시 시작 | 고시 종료 | 특이사항 |
|------|----------|----------|---------|
| **investing** | 월 06:00 | 토 06:00 | 외환 시장 글로벌 운영 |
| **kb** | 평일 08:30 | 익일 05:00 | - |
| **hana** | 평일 08:30 | 익일 06:00 | 주말 중 가끔 변동 |
| **shinhan** | 평일 08:19 | 익일 02:30 | - |
| **woori** | 평일 08:30 | 익일 02:45 | - |
| **ibk** | 평일 08:30 | 익일 02:05 | 00:00부터 Selenium만 사용 (날짜 변경 필요), 03:00까지 크롤링 (55분 여유) |
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
- 변경사항 있을 때만 실제 전송

#### 2. 3-Tier 크롤러 아키텍처 (4단계 모드)

**Tier A (investing):** 기준 환율, 최우선
- **특징**: 가장 중요한 데이터, 빠른 응답
- **실행 방식**: Request 기반 (requests 라이브러리)
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
  - ibk: IN 모드(08:30~20:59) Request 우선, 00:00~02:59 Selenium만 사용 (날짜 변경 필요)
- **IN**: 매분 cron (shinhan: 18초, ibk: 34초, nh: 54초, sc: 58초)
- **BREAK1**: ibk(34초), nh(54초), shinhan(18초) 유지 (sc는 21:00에 크롤링 중단)
- **BREAK2**: nh(54초)만 유지 (ibk는 02:05 종료, shinhan은 02:30 종료, sc는 21:00 종료)
- **OUT**: nh(3분 45초)만 유지 (자정 이후/주말 가끔 고시)

**Selenium Queue 관리:**
- **Request 우선 전략**: Queue 압력 대폭 감소 (대부분 Request 성공)
- **우선순위 기반 실행**: 빠른 크롤러 우선 (hana → ibk → nh → sc → shinhan)
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
07초: investing
10초: Broadcasting
13초: citi
15초: kb
17초: investing
18초: shinhan (Selenium Queue)
20초: Broadcasting
25초: hana
27초: investing
30초: Broadcasting
33초: bs
34초: ibk (Selenium Queue)
35초: kb
37초: investing
40초: Broadcasting
45초: hana
47초: investing
50초: Broadcasting
53초: woori (우선순위 높음)
54초: nh (Selenium Queue)
55초: kb
57초: investing
58초: sc (Selenium Queue)
```

**특징:**
- Broadcasting 직전(3-7초)에 Request 크롤러 실행 → 최신 데이터 반영
- woori는 citi보다 우선순위 높음: 53초 실행 → 사용자 표시 시간이 실제 변경 시간과 유사
- Selenium 크롤러는 Queue 순차 처리 (Request 먼저 시도)
- 최대 동시 실행: 1-2개 (Request 기반 크롤러만)

### 설계 원칙

- **환율 고시 스케줄 기반**: 은행별 실제 운영 시간에 맞춰 크롤러 활성화/비활성화
- **mibank 딜레이 고려**: shinhan/sc 자정 종료지만 01:00까지 크롤링 (마지막 고시 누락 방지)
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

### REST API (폴백용)

- `GET /api/rates` - 전체 환율 (모바일 앱용)
- `GET /api/rates/{currency}` - 특정 통화쌍
- `GET /api/investing/{pair}` - Investing.com 특정 통화
- `GET /api/banks/{pair}` - 모든 은행 특정 통화
- `GET /health` - 헬스체크

### 알림 API (예정)

> 상세 스키마: [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md#-개인화-알림-동작-흐름)

- `POST /api/register-device` - FCM Device Token 등록 (Firebase ID Token 검증 필수)
- `POST /api/notification-settings` - 알림 설정 저장/수정
- `GET /api/notification-settings` - 사용자 알림 설정 조회
- `DELETE /api/notification-settings/{id}` - 알림 설정 삭제

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

## 인프라 제약사항

### 현재 환경 (개발/테스트)

- **EC2**: AWS 프리티어 t2.micro (1 vCPU, 1GB RAM)
- **DB**: SQLite (로컬 파일)
- **OS**: Ubuntu 24.04.3 LTS, 64비트(x86) = x86_64 = AMD64 아키텍처
- **DB 크기**: 5.4MB (SQLite)
- **동시 접속**: 최대 ~100명 (WebSocket)

### 서비스 환경 (예정)

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

**AWS RDS PostgreSQL 프리티어:**

- **인스턴스**: db.t4g.micro (2 vCPU, 1GB RAM, ARM64)
- **스토리지**: 20GB SSD (gp2)
- **기간**: 12개월 무료 (AWS 첫 가입 후)
- **네트워크**: 같은 VPC 내 통신 (지연 1-3ms, 비용 $0)
- **12개월 후 비용**: ~$15/월

**비용 예상:**

| 항목 | Phase 1 (12개월) | Phase 2 (12개월 후) |
|------|-----------------|-------------------|
| EC2 t3.small | $15/월 | $15/월 |
| RDS db.t4g.micro | $0 (프리티어) | ~$15/월 |
| Firebase Auth/FCM | $0 | $0 |
| **합계** | **$15/월** | **$30/월** |

### 확장 계획 (사용자 1,000명 이상 시)

1. ✅ **PostgreSQL 전환** → RDS 프리티어로 해결
2. ✅ **Redis 캐시 도입** → Phase 1.7 완료 (EC2 Docker)
3. ✅ **변경 감지 시스템** → Phase 1.7 완료
4. **EC2 업그레이드** (t3.small → t3.medium, 필요 시)
5. **RDS 업그레이드** (db.t4g.micro → db.t4g.small, 필요 시)

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
4. **User-Agent 로테이션** (TODO)

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
│   ├── main.py              # FastAPI 서버, WebSocket
│   ├── scheduler.py         # APScheduler, 4단계 모드 전환
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
│   │   └── citi.py          # 씨티은행 크롤러
│   │
│   ├── admin/               # 관리자 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── log_reader.py    # 로그 조회 (관리자 페이지용, 백엔드 bank 필터링)
│   │   ├── log_cleaner.py   # 오래된 로그 파일 자동 삭제
│   │   ├── stats.py         # WebSocket 브로드캐스트 통계 수집
│   │   ├── crawler_stats.py # 크롤러 통계 수집 (성공률, 실행시간)
│   │   ├── monitor.py       # 시스템 모니터링 (메모리, CPU, Chrome) - Phase 1.5
│   │   └── graph_cache.py   # 그래프 데이터 Redis 캐시 - Phase 1A
│   │
│   └── notifications/       # 알림 도메인 (2025-10-25 리팩토링)
│       ├── __init__.py
│       └── telegram.py      # 텔레그램 알림 (Phase 2용)
│
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

**관리**: 로테이션 10MB, 백업 5개, 10일 자동 삭제

**상세 가이드**: `app/logging.py`, `app/admin/log_reader.py`

## 보안 고려사항

- [x] **HTTPS/SSL 적용** (Let's Encrypt, 2025-11-17)
  - 도메인: `fxi.n-e.kr`
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

### Phase 2-3: 고급 기능 (사용자 500명+)
- 크롤러 제어 (재시작, 주기 조정)
- 통계 & 분석 (Chart.js, 성공률 그래프)
- 실시간 알림, 환율 이상치 감지 (ML)


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
- 📋 알림 시스템 고도화 ([ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification))
- 🔜 비동기 크롤링, Docker, 모니터링, CI/CD, 유닛 테스트
