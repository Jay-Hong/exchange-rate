# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

### 📚 주요 문서 가이드

**핵심 가이드:**
> 💡 **아키텍처 의사결정:** [DECISIONS.md](DECISIONS.md) - 주요 기술 선택과 그 근거 (ADR)
> 🕷️ **크롤러 구현:** [CRAWLERS.md](CRAWLERS.md) - 각 은행별 크롤링 방식과 특수 로직
> 📝 **변경 이력:** [CHANGELOG.md](CHANGELOG.md) - 버전별 변경사항 및 마이그레이션 가이드

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

> 💡 **MCP 설정 가이드**: `~/.claude/CLAUDE.md` 참고 (Scope 3가지, 명령어, 트러블슈팅 등)
> ⚠️ **주의**: project scope (`.claude/mcp.json`) 대신 **local scope** 사용 (안정성)

### 이 프로젝트의 MCP 구성

**글로벌 MCP** (`~/.claude.json`, user scope):
- **context7**: 라이브러리 문서 조회 (모든 프로젝트 공통)
- **github**: GitHub 저장소 관리 (Docker 기반, 설치 방법은 `~/.claude/CLAUDE.md` 참고)

**프로젝트별 MCP** (`~/.claude.json`, local scope):
- **sqlite**: `data/exchange_rates.db` 접근 (DB 쿼리 실행)
- **filesystem**: 프로젝트 디렉토리 접근 (파일 탐색)

## 목표 사용자

- 초기: 200-500명
- 주 사용층: iOS/Android 네이티브 앱 사용자
- 웹: 관리자/디버깅 전용

## 핵심 아키텍처

```
크롤러(10개) → Scheduler → SQLite → WebSocket(10초) → 클라이언트
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
- **DB**: SQLite (초기) → PostgreSQL (500명 이상 시)
- **ORM**: SQLAlchemy
- **스케줄러**: APScheduler (BackgroundScheduler)
- **크롤링**: requests + BeautifulSoup4, Selenium

### 프론트엔드

- **iOS**: Swift (네이티브)
- **Android**: Kotlin + Jetpack Compose
- **웹**: Vanilla JS + WebSocket (관리자용)

### 실시간 통신

- WebSocket (10초 주기 브로드캐스트)
- Ping/Pong 하트비트 (30초)

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

## 데이터 관리 정책

- **은행 데이터**: 10일분만 유지 (단기 비교용)
- **인베스팅 데이터**: 장기 보관 (그래프/분석용)
- **저장 조건**: 변경사항 있을 때만 INSERT (중복 방지)

## 스케줄링 시스템

> 💡 **최근 개선:** Queue 압력 완화 + 실시간성 강화 (2025-11-10)
> 📖 **상세 기록:** [ADR-008](DECISIONS.md#adr-008-queue-압력-완화-전략-실시간성-vs-완전성)
> - 타임아웃 50% 감축 (실시간성 우선)
> - Queue 크기 최적화 (50 → 25)
> - 80% 압력 완화 정책 (20/25 초과 시 작업 거부)
> - Worker 헬스체크 시스템 (180초 stuck 자동 재시작)
> - 크롤러 스케줄 재조정 (느린 크롤러 빈도 감소)

> 🔖 **이전 개선:**
> - Queue Blocking 방지 (2025-11-09) - [MAINTENANCE_2025-11-09.md](MAINTENANCE_2025-11-09.md)
> - Priority Queue + Timeout (2025-11-08) - [MAINTENANCE_2025-11-08.md](MAINTENANCE_2025-11-08.md), [ADR-007](DECISIONS.md#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)
> - AsyncIO Queue 순차 실행 (2025-11-06) - [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md), [ADR-006](DECISIONS.md#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue)

### 영업시간 자동 감지

- **IN 모드**: 월요일 04:00 ~ 토요일 07:59
  - 크롤링 주기: 4.9-33.3초 (소스별 상이)
- **OUT 모드**: 그 외 시간
  - 크롤링 주기: 49-333초 (IN 모드의 10배)

### 크롤러 아키텍처 (2025-11-10 최종)

**스케줄러:** APScheduler (AsyncIOScheduler)

**실행 방식:**
- **Group A (Request 기반)**: 동시 실행 (경량, 메모리 부담 적음)
  - investing, kb, woori, bs, citi
  - APScheduler 직접 실행
- **Group B (Selenium 기반)**: AsyncIO PriorityQueue 순차 실행 (메모리 집약)
  - hana, shinhan, ibk, nh, sc
  - **우선순위 기반 실행**: 빠른 크롤러 우선 (hana → ibk → nh → sc → shinhan)
  - **개별 타임아웃**: 크롤러별 맞춤형 타임아웃 (30-60초, 실시간성 우선)
  - **자동 재시도**: 실패 시 우선순위 +1000으로 재실행
  - **Non-blocking Queue**: put_nowait()으로 APScheduler 멈춤 방지
  - **Queue 크기**: 25 (메모리 최적화)
  - **압력 완화**: 80% (20/25) 초과 시 새 작업 거부
  - **헬스체크**: 60초마다 Worker 상태 점검, 180초 stuck 시 자동 재시작

### 크롤러 실행 주기 (IN 모드 기준)

```python
# Group A: Request 기반 (동시 실행)
REQUEST_BASED_TASKS = [
    ("investing", 4.9초),
    ("kb", 7.9초),
    ("woori", 23.3초),
    ("bs", 27.7초),
    ("citi", 28.5초),
]

# Group B: Selenium 기반 (Queue 순차 처리)
# [2025-11-10] Queue 압력 완화를 위한 주기 재조정
SELENIUM_BASED_TASKS = [
    ("hana", 20초),      # 빠른 크롤러: 높은 빈도
    ("shinhan", 60초),   # 느린 크롤러: 중간 빈도
    ("ibk", 60초),       # 느린 크롤러: 중간 빈도
    ("nh", 90초),        # 가장 느린 크롤러: 낮은 빈도
    ("sc", 120초),       # 가장 불안정: 최소 빈도
]

# 크롤러별 타임아웃 (실시간성 강화)
SELENIUM_TIMEOUT_MAP = {
    "hana": 30초,
    "shinhan": 45초,
    "nh": 45초,
    "ibk": 45초,
    "sc": 45초,
}
```

**설계 원칙:**
- **실시간성 > 완전성**: 타임아웃 엄격화로 빠른 실패 → Queue 정체 방지
- **압력 완화**: Queue 80% 초과 시 새 작업 거부 → 시스템 안정성 유지
- **자동 복구**: Worker stuck 시 자동 재시작, 실패 작업 자동 재시도

## 크롤러 구조

**공통 패턴:**
- 2-3개 폴백 URL, CSS Selector 기반, 변경 시에만 DB 저장

**상세 가이드:** [CRAWLERS.md](CRAWLERS.md) (각 은행별 특수 로직, 트러블슈팅)

## API 엔드포인트

### WebSocket

- `WS /ws` - 실시간 환율 스트리밍 (10초 주기)

### REST API (폴백용)

- `GET /api/rates` - 전체 환율 (모바일 앱용)
- `GET /api/rates/{currency}` - 특정 통화쌍
- `GET /api/investing/{pair}` - Investing.com 특정 통화
- `GET /api/banks/{pair}` - 모든 은행 특정 통화
- `GET /health` - 헬스체크

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

### 현재 환경

- **AWS 프리티어**: t2.micro (1 vCPU, 1GB RAM) , Ubuntu 24.04.3 LTS , 64비트(x86) = x86_64 = AMD64 아키텍처
- **DB 크기**: 5.4MB (SQLite)
- **동시 접속**: 최대 ~100명 (WebSocket)

### 확장 계획 (사용자 500명 이상 시)

1. **PostgreSQL 전환** (동시성 개선)
2. **Redis 캐시 도입** (최신 환율 캐싱)
3. **변경 감지 시스템** (변경 시에만 WebSocket 전송)
4. **WebSocket 주기 동적 조절** (5-10초)
5. **EC2 인스턴스 업그레이드** (t3.small 이상)

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
│   ├── main.py              # FastAPI 서버, WebSocket
│   ├── scheduler.py         # APScheduler, IN/OUT 모드 전환
│   │
│   ├── crawlers/            # 크롤러 도메인 (2025-10-25 리팩토링)
│   │   ├── __init__.py
│   │   ├── constants.py     # 크롤러 공통 상수 (HEADERS, SELENIUM_OPTIONS, TIMEOUT)
│   │   ├── utils.py         # 크롤러 공통 함수 (parse_rate_text, create_selenium_driver)
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
│   │   └── monitor.py       # 시스템 모니터링 (메모리, CPU, Chrome 프로세스) - 2025-11-05 추가
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
├── MAINTENANCE_2025-11-05.md # 성능 개선 및 모니터링 시스템 구축 작업 기록
└── REBOOT_CHECKLIST.md      # 재부팅 후 검증 절차 가이드
```

> 💡 **구조 설계 원칙**: API-first 서비스 (모바일 앱이 메인, 웹은 관리자 전용)
> **확장성**: React 전환 시 `frontend/` 폴더 추가만 하면 됨 (Backend/Frontend 약한 결합)

## 주요 로직

**중복 방지 INSERT**: 마지막 레코드와 비교, 변경 시에만 저장 (`crud.py`)

**WebSocket 브로드캐스트**: 10초 주기, 통계 수집, 전체 환율 전송 (`main.py`)

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

- [ ] HTTPS 강제 (Let's Encrypt)
- [ ] CORS 설정 (특정 도메인만 허용)
- [ ] API Rate Limiting
- [x] **민감 정보 환경 변수화** (python-dotenv, .env 파일)

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
- **자동 정리 시스템**: 좀비 Chrome 프로세스 1분마다 자동 정리 (3분 이상 실행된 것)
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
- 📋 알림 시스템 고도화 ([ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification))
- 🔜 비동기 크롤링, Docker, 모니터링, CI/CD, 유닛 테스트
