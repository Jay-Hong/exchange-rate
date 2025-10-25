# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

> 💡 **아키텍처 의사결정 기록:** 주요 기술 선택과 그 근거는 [DECISIONS.md](DECISIONS.md)를 참고하세요.
> 🐳 **Docker 배포 가이드:** AWS 배포 및 확장 전략은 [DOCKER.md](DOCKER.md)를 참고하세요.
> 🕷️ **크롤러 특수 로직 가이드:** 각 은행별 크롤링 방식과 특수 로직은 [CRAWLERS.md](CRAWLERS.md)를 참고하세요.
> 📚 **공통 개발 가이드:** MCP 설정, 코딩 스타일 등은 [~/.claude/CLAUDE.md](file:///Users/jay/.claude/CLAUDE.md)를 참고하세요.

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
- **크롤링**: requests + BeautifulSoup4

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

### 영업시간 자동 감지

- **IN 모드**: 월요일 04:00 ~ 토요일 07:59
  - 크롤링 주기: 4.4-33.3초 (소스별 상이)
- **OUT 모드**: 그 외 시간
  - 크롤링 주기: 44-333초 (IN 모드의 10배)

### 크롤러 실행 주기 (IN 모드 기준)

```python
BANK_TASKS = [
    ("investing", 4.4초),
    ("kb", 7.9초),
    ("hana", 7.3초),
    ("shinhan", 31초),
    ("woori", 23.3초),
    ("ibk", 29.1초),
    ("nh", 32.7초),
    ("sc", 33.3초),
    ("bs", 27.7초),
    ("citi", 28.5초),
]
```

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

- **AWS 프리티어**: t2.micro (1 vCPU, 1GB RAM)
- **DB 크기**: 5.4MB (SQLite)
- **동시 접속**: 최대 ~100명 (WebSocket)

### 확장 계획 (사용자 500명 이상 시)

1. **PostgreSQL 전환** (동시성 개선)
2. **Redis 캐시 도입** (최신 환율 캐싱)
3. **변경 감지 시스템** (변경 시에만 WebSocket 전송)
4. **WebSocket 주기 동적 조절** (5-10초)
5. **EC2 인스턴스 업그레이드** (t3.small 이상)

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
F06_GitHub/
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
│   │   └── stats.py         # WebSocket 브로드캐스트 통계 수집
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
├── templates/
│   └── index.html           # 웹 대시보드 (관리자용)
├── .env                     # 환경 변수 (git 제외)
├── .gitignore               # Git 제외 파일
├── requirements.txt         # Python 패키지
├── CLAUDE.md                # 이 파일 (프로젝트 가이드)
├── CRAWLERS.md              # 크롤러 특수 로직 가이드
└── DECISIONS.md             # 아키텍처 의사결정 기록 (ADR)
```

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

### Phase 2-3: 고급 기능 (사용자 500명+)
- 크롤러 제어 (재시작, 주기 조정)
- 통계 & 분석 (Chart.js, 성공률 그래프)
- 실시간 알림, 환율 이상치 감지 (ML)


## 향후 개선 사항

- ✅ 구조화된 로깅, 관리자 페이지, 도메인 기반 구조
- 📋 알림 시스템 고도화 ([ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification))
- 🔜 비동기 크롤링, Docker, 모니터링, CI/CD, 유닛 테스트
