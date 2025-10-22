# 환율 정보 서비스 (Exchange Rate Comparison Service)

## 프로젝트 개요

실시간 은행 간 환율 차이 비교 서비스 (인베스팅 기준 환율 포함)

> 💡 **아키텍처 의사결정 기록:** 주요 기술 선택과 그 근거는 [DECISIONS.md](DECISIONS.md)를 참고하세요.
> 🐳 **Docker 배포 가이드:** AWS 배포 및 확장 전략은 [DOCKER.md](DOCKER.md)를 참고하세요.
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
  - 크롤링 주기: 5-33초 (소스별 상이)
- **OUT 모드**: 그 외 시간
  - 크롤링 주기: 50-330초 (IN 모드의 10배)

### 크롤러 실행 주기 (IN 모드 기준)

```python
BANK_TASKS = [
    ("investing", 5초),
    ("hana", 7초),
    ("kb", 8초),
    ("woori", 11초),
    ("ibk", 29초),
    ("bs", 27초),
    ("citi", 28초),
    ("shinhan", 31초),
    ("nh", 32초),
    ("sc", 33초),
]
```

## 크롤러 구조

### 공통 패턴

1. 2-3개 폴백 URL 지원 (1차 실패 시 자동 전환)
2. CSS Selector 기반 환율 추출
3. 마지막 레코드와 비교 → 변경 시만 DB 저장
4. 타임아웃: 10초 (requests), 5초 (Selenium WebDriverWait)

### 예시 (bank_kb_crawler.py)

```python
# Primary URL
KB_BANK_URL = 'https://obank.kbstar.com/...'
# Fallback URL 1
SECOND_KB_BANK_URL = 'https://obank.kbstar.com/...'
# Fallback URL 2
MIBANK_KB_URL = 'https://www.mibank.me/...'
```

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
│   ├── main.py              # FastAPI 서버, WebSocket
│   ├── scheduler.py         # APScheduler, IN/OUT 모드 전환
│   ├── crud.py              # DB CRUD 로직
│   ├── models.py            # SQLAlchemy ORM 모델
│   ├── schemas.py           # Pydantic 스키마
│   ├── database.py          # DB 연결 설정
│   ├── config.py            # 환경 설정, 로그 설정
│   ├── logging_config.py    # 구조화된 로깅 시스템 (중앙 설정)
│   ├── investing_crawler.py # Investing.com 크롤러
│   ├── bank_*_crawler.py    # 은행별 크롤러 (9개)
│   └── utils/               # 유틸리티 모듈
│       ├── __init__.py
│       ├── log_reader.py    # 로그 조회 (관리자 페이지용, 백엔드 bank 필터링 지원)
│       ├── log_cleaner.py   # 오래된 로그 파일 자동 삭제
│       ├── broadcast_stats.py  # WebSocket 브로드캐스트 통계 수집
│       └── telegram_handler.py  # 텔레그램 알림 (Phase 2용 - 에러/경고 알림)
├── data/
│   └── exchange_rates.db    # SQLite DB
├── logs/                    # 로그 파일 (자동 생성)
│   ├── app.log              # 모든 운영 로그 (INFO+, 크롤러 포함)
│   └── error.log            # 에러/경고만 (WARNING+)
│   # Option 1 (단순화): crawler.log, debug.log 제거 (중복 제거, 디스크 66% 절약)
├── static/                  # 은행 아이콘
├── templates/
│   └── index.html           # 웹 대시보드 (관리자용)
├── .env                     # 환경 변수 (git 제외)
├── .gitignore               # Git 제외 파일
├── requirements.txt         # Python 패키지
├── CLAUDE.md                # 이 파일 (프로젝트 가이드)
└── DECISIONS.md             # 아키텍처 의사결정 기록 (ADR)
```

## 주요 로직

### 중복 방지 INSERT (crud.py)

```python
last_record = db.query(BankExchangeRate)
    .filter(bank == bank_name, currency == pair)
    .order_by(id.desc())
    .first()

if last_record is None or last_record.rate != current_rate:
    # INSERT
```

### WebSocket 브로드캐스트 (main.py)

```python
async def broadcast_rates():
    while True:
        await asyncio.sleep(10)
        all_rates = crud.get_all_rates_flat(db)

        # 통계 수집
        data_size_bytes = len(json.dumps(message, ensure_ascii=False).encode('utf-8'))
        broadcast_stats.record_success(
            data_size_bytes=data_size_bytes,
            rate_count=len(all_rates)
        )

        await manager.broadcast({"type": "rates", "data": all_rates})
```

### 브로드캐스트 통계 수집 (broadcast_stats.py)

```python
class BroadcastStats:
    def record_success(self, data_size_bytes: int, rate_count: int):
        """성공적인 브로드캐스트 기록"""
        # 히스토리 저장 (그래프용)
        self.broadcast_history.append({
            "timestamp": now.isoformat(),
            "status": "success",
            "interval": interval,
            "data_size_kb": round(data_size_bytes / 1024, 2),
            "rate_count": rate_count
        })
        self.total_success += 1

    def get_stats(self) -> Dict:
        """관리자 페이지용 통계 반환"""
        # 성공률, 평균 주기, 평균 크기, 건강 상태 등 계산
        return {
            "success_rate": round(success_rate, 2),
            "avg_interval": round(avg_interval, 1),
            "broadcasts_per_hour": round(broadcasts_per_hour, 1),
            "status": self._get_health_status()  # healthy/warning/critical
        }
```

## 개발 가이드라인

### 크롤러 추가 시

1. `app/bank_NEW_crawler.py` 생성
2. `BANK_NAME`, `SELECTORS`, `URL` 정의
3. `crawl_and_save_routine()` 재사용
4. `scheduler.py`의 `BANK_TASKS`에 추가

### DB 마이그레이션

- SQLAlchemy `Base.metadata.create_all()` 자동 실행
- 스키마 변경 시 Alembic 사용 권장 (TODO)

### 로깅 시스템

> ✅ **개선 완료** (2025-10): 타임존 일관성, LOG_LEVEL 활용, exception() 강화, 백엔드 bank 필터링

#### 로거 사용법

**모든 파일에서 표준 로거 사용 (print문 사용 금지)**:

```python
import logging

# 일반 모듈
logger = logging.getLogger("exchange_rate.main")  # main.py
logger = logging.getLogger("exchange_rate.scheduler")  # scheduler.py
logger = logging.getLogger("exchange_rate.db")  # crud.py

# 크롤러
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")  # 크롤러
```

**로그 레벨 가이드**:
- `logger.debug()` - 내부 상태 (📼 [유지], DB 저장 성공 등) - 개발 환경에서만 기록
- `logger.info()` - 정상 이벤트 (신규/변경 감지, 브로드캐스트, 모드 전환)
- `logger.warning()` - 복구 가능한 문제 (SELECTOR 오류, 폴백 URL 사용)
- `logger.error()` - 복구 불가능한 오류 (크롤러 1개 완전 실패, API 오류)
- `logger.critical()` - 시스템 중단 수준 장애 (DB 연결 실패, 모든 크롤러 실패)
- `logger.exception()` - **예외 발생 시 필수** (자동으로 스택 트레이스 포함)

**중요**: except 블록에서는 **반드시 logger.exception()** 사용 (logger.error() 대신)

**구조화된 로그 (extra 필드)**:
```python
# 환율 변경
logger.info(f"⚡️ [변경] {pair}: {old} → {new}",
    extra={"pair": pair, "old_rate": old, "new_rate": new, "change": diff})

# 크롤링 실패
logger.exception("URL 크롤링 실패", extra={"url": url, "bank": BANK_NAME})

# WebSocket 연결
logger.info("✅ WebSocket 연결 성공", extra={"connections": count})
```

**중요**: `extra` 필드의 내용은 JSON 로그에서 **최상위 레벨**에 저장됩니다.
```json
{
  "message": "⚡️ [변경] usd-krw: 1340.5 → 1341.0",
  "timestamp": "2025-10-07T14:29:07",
  "level": "INFO",
  "logger": "exchange_rate.db",
  "pair": "usd-krw",
  "old_rate": 1340.5,
  "new_rate": 1341.0,
  "change": 0.5,
  "bank": "kb"
}
```

#### 로그 파일 (Option 1: 단순화)

**🎯 2개 파일만 사용** (crawler.log, debug.log 제거):

- **app.log** - 모든 운영 로그 (INFO 이상), JSON 형식, KST 타임존
  - 포함: 일반 동작, 환율 변경, 브로드캐스트, **크롤러 활동** 등
  - 용도: 전체 시스템 모니터링, 관리자 페이지 메인 로그

- **error.log** - 에러/경고만 (WARNING 이상), JSON 형식, KST 타임존
  - 포함: SELECTOR 오류, URL 오류, 크롤링 실패, 시스템 오류
  - 용도: 문제 발생 시 빠른 확인, 알림 트리거

**로테이션**: 각 파일 10MB, 최대 5개 백업 (총 ~20MB, 기존 50MB의 40%)

**자동 삭제**: 7일 이상 된 백업 파일(.log.1, .log.2 등)은 매일 자정에 자동 삭제 (scheduler.py → log_cleaner.py)

**개선 효과** (2025-10-14):
- ✅ **디스크 사용량 66% 감소** (146MB → ~50MB)
- ✅ **파일 수 75% 감소** (최대 24개 → 6개)
- ✅ **중복 제거**: 크롤러 에러 1건 = 3곳 기록 → 2곳 기록
- ✅ **관리 단순화**: 관리자 페이지 필터링으로 크롤러 로그 확인 가능
- ✅ **AWS 프리티어 친화적**: 디스크 절약 (한 달 로그 ~400MB 예상)

**이전 주요 개선 사항** (2025-10):
- ✅ 타임존 일관성: 모든 로그에 KST 명시
- ✅ LOG_LEVEL 환경 변수 활용
- ✅ exception() 사용 강화 (모든 크롤러 except 블록)
- ✅ 콘솔 출력도 LOG_LEVEL 따름

#### 환경 설정 (.env)

**ENV** - 환경 구분:
- `development`: 개발 환경
  - 콘솔: 컬러 출력 (가독성 좋음)
  - 파일: **app.log, error.log만 생성** (2개)
- `production`: 운영 환경
  - 콘솔: JSON 출력 (로그 수집 도구에 적합)
  - 파일: **app.log, error.log만 생성** (2개, 동일)

**LOG_LEVEL** - 로그 레벨 제어 (**콘솔 + 파일 모두 적용**):
- `DEBUG`: 모든 로그 출력 (개발 중 상세 디버깅 - 권장)
  - 콘솔, app.log에서 DEBUG 로그 확인 가능 (크롤러 포함)
- `INFO`: 일반 정보 이상만 (기본값, 운영 환경 권장)
  - DEBUG 로그 무시, 환율 변경/브로드캐스트 등만 표시
- `WARNING`: 경고 이상만 (운영 환경 최적화)
  - SELECTOR 오류, URL 오류 등만 표시
- `ERROR`: 에러만 (긴급 상황만 추적)
  - 크롤링 실패, 시스템 오류만 표시

**권장 조합**:
```bash
# 로컬 개발 중 (DEBUG 로그 필요)
ENV=development
LOG_LEVEL=DEBUG

# 테스트/스테이징 (INFO만 충분)
ENV=development
LOG_LEVEL=INFO

# 운영 서버 (INFO 또는 WARNING)
ENV=production
LOG_LEVEL=INFO  # 또는 WARNING
```

**확인 방법**:
- **콘솔**: 터미널 출력으로 즉시 확인 (컬러 또는 JSON)
- **파일**: `logs/app.log` (모든 로그), `logs/error.log` (에러/경고만)
- **관리자 페이지**: `http://localhost:8000/admin` → 로그 뷰어 (app/error 선택)

**기타 설정**:
```bash
# 관리자 페이지
ADMIN_PASSWORD=admin1234     # 실제 운영 시 강력한 비밀번호로 변경

# 텔레그램 (Phase 2에서 활성화)
TELEGRAM_ENABLED=false       # Phase 2에서 true로 변경
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

#### 로그 조회 (관리자용)

```python
from app.utils.log_reader import read_logs, get_log_stats

# 최근 24시간 에러 로그 300개 (기본값)
logs = read_logs(log_type="error", level="ERROR", limit=300, hours=24)

# 특정 은행 로그만 조회 (백엔드 필터링 - 효율적)
kb_logs = read_logs(log_type="app", bank="kb", limit=300, hours=1)
shinhan_logs = read_logs(log_type="app", bank="shinhan", limit=300, hours=1)

# 통계 조회
stats = get_log_stats(hours=24)
# {"total": 1500, "by_level": {"INFO": 1200, ...}, "errors_last_hour": 3}
```

**최근 개선사항 (2025-10-15)**:
- ✅ **백엔드 bank 필터링 추가**: 서버에서 은행별 로그 필터링 (메모리/CPU 효율적)
  - 크롤링 주기가 긴 은행(신한, IBK, NH, 부산, 씨티)의 로그도 안정적으로 표시
  - limit=300일 때 빠른 크롤러가 슬롯을 독점하는 문제 해결
- 기본 limit: 100 → **300개** (약 5-10분치 로그)
- 관리자 페이지에서도 300개 표시 (스크롤 가능, 부하 미미)

### 코딩 스타일 가이드

#### Import 순서 (PEP 8 준수)

**기본 원칙**:
1. 모든 import는 **파일 상단**에 작성 (원칙)
2. 3개 그룹으로 분리 (각 그룹 사이 빈 줄 1개)
3. 각 그룹 내에서 알파벳 순 정렬

**표준 순서**:
```python
# 1. 표준 라이브러리
import asyncio
from datetime import datetime
from typing import List, Dict

# 2. 서드파티 라이브러리
from fastapi import FastAPI
from pytz import timezone
from sqlalchemy.orm import Session

# 3. 로컬 애플리케이션
from app import models, crud
from app.database import SessionLocal
```

**함수 내부 import 허용 조건** (다음 경우에만):

1. **순환 참조 방지**
   ```python
   def some_function():
       from app.module_b import something  # A ↔ B 순환 참조 방지
   ```

2. **성능 최적화** (무거운 라이브러리 + 거의 호출되지 않는 함수)
   ```python
   def monthly_report():  # 월 1회만 호출
       import pandas as pd  # pandas는 import에 ~1초 소요
       import matplotlib.pyplot as plt
   ```
   - 조건: import 시간 > 0.5초 AND 호출 빈도 < 주 1회

3. **조건부/선택적 의존성** (특정 환경에서만 필요)
   ```python
   def windows_only_function():
       if os.name == 'nt':
           import winreg  # Windows에서만 사용
   ```

4. **동적 로딩/플러그인** (런타임에 모듈 결정)
   ```python
   def get_crawler(name):
       module = importlib.import_module(f"app.{name}_crawler")
   ```

**중요**: 함수 내부 import 사용 시 **반드시 주석으로 이유 명시**

## 보안 고려사항

- [ ] HTTPS 강제 (Let's Encrypt)
- [ ] CORS 설정 (특정 도메인만 허용)
- [ ] API Rate Limiting
- [x] **민감 정보 환경 변수화** (python-dotenv, .env 파일)

## 관리자 페이지 (/admin)

### Phase 1: 심플 대시보드 ✅ 완료 (2025-10-17 재설계)

> **설계 철학**: AWS 프리티어 친화적, API 호출 최소화, 모바일/데스크탑 모두 지원

**아키텍처 개선** (2025-10-17):
- ✅ **통합 API 도입**: 4개 API를 1개로 통합 (`/admin/api/dashboard`) → API 호출 75% 감소
- ✅ **코드 단순화**: admin.html 1,288줄 → 615줄 (52% 감소), JavaScript 500줄 이하
- ✅ **크롤러 상태 중심 설계**: 에러 개수를 뱃지로 표시, 클릭 시 해당 크롤러 로그만 표시
- ✅ **여백 제거**: 정보 밀도 극대화, 모바일에서도 스크롤 최소화

**핵심 기능**:
- **4개 대시보드 카드**: WebSocket 연결, 메모리, 브로드캐스트, 에러
- **크롤러 상태 뱃지**: 각 크롤러별 에러 개수 표시 (클릭 시 로그 필터링)
- **로그 뷰어 (2개 탭)**:
  - 🖥️ **실시간 모니터링** (app.log): 300개, 1시간 고정, 크롤러 배지 필터링
  - ⚠️ **에러 추적** (error.log): 10,000개, 1시간/24시간/7일, 레벨/은행 필터
- **자동 새로고침**: 30초마다 통합 API 호출 1회

**보안**: HTTP Basic Authentication

### Phase 2: 제어 & 관리 (사용자 500명+)
- **크롤러 제어판**
  - 개별 크롤러 재시작/일시정지
  - 크롤링 주기 임시 조정
  - 강제 재크롤링 (캐시 무시)
- **데이터 관리**
  - 은행 데이터 수동 정리 (10일 → N일 조정)
  - DB VACUUM 수동 실행
  - 데이터베이스 백업 다운로드
- **통계 & 분석**
  - 크롤러별 성공률 그래프 (Chart.js)
  - API 엔드포인트 호출 통계
  - 환율 변동 히트맵
- **설정 관리**
  - 텔레그램 알림 ON/OFF (UI)
  - 로그 레벨 동적 변경 (재시작 없이)
  - WebSocket 브로드캐스트 주기 조정

### Phase 3: 자동화 & 고급 기능 (운영 성숙 단계)
- **실시간 알림 시스템**
  - 조건별 알림 (크롤러 실패 3회 연속 → 텔레그램)
  - 임계치 설정 (메모리 80% → 이메일)
  - 알림 히스토리 관리
- **환율 이상치 감지**
  - 급격한 환율 변동 감지 (5분 내 3% 이상)
  - 은행 간 환율 차이 알림 (5원 이상)
  - 이상치 패턴 학습 (ML)
- **실시간 대시보드**
  - WebSocket 기반 실시간 업데이트
  - 크롤러 상태 실시간 변경 반영
  - 환율 변동 실시간 차트

### 사용법

#### 접속

```
URL: http://localhost:8000/admin
계정: admin / .env의 ADMIN_PASSWORD (기본: admin1234)
```

#### API 엔드포인트 (HTTP Basic Auth 필요)

```
GET  /admin                  - 관리자 대시보드 페이지
GET  /admin/api/dashboard    - 통합 대시보드 API (시스템/브로드캐스트/크롤러 상태)
GET  /admin/api/logs         - 로그 조회 (bank 필터링 지원)
GET  /admin/api/download-logs - 로그 다운로드
```

#### 기술 스택

- **UI**: Vanilla CSS (여백 제거, 정보 밀도 극대화)
- **JS**: Vanilla JavaScript (~400줄, Fetch API)
- **차트**: Chart.js (Phase 2+)
- **인증**: HTTP Basic Auth

#### 자동 새로고침

30초마다 `/admin/api/dashboard` 호출 1회

## 향후 개선 사항

1. **비동기 크롤링** (httpx + asyncio)
2. ~~**구조화된 로깅**~~ ✅ **완료** (JSON 형식, 파일 로테이션, 크롤러별 로거, 자동 삭제)
3. ~~**관리자 페이지**~~ ✅ **Phase 1 완료** (모니터링, 로그 통계, 고급 필터링, 다운로드)
4. **알림 시스템 고도화** (📋 계획 중 - [ADR-003](DECISIONS.md#adr-003-알림-시스템---websocket-vs-push-notification) 참고)
   - Phase 2: 맞춤형 WebSocket 알림 (사용자 200명+)
   - Phase 3: Push Notification (앱 꺼져 있을 때 알림, 사용자 500명+)
   - Phase 4: 고급 알림 (ML 기반 예측, 사용자 1,000명+)
5. **모니터링** (Prometheus + Grafana)
6. **Docker 컨테이너화**
7. **CI/CD** (GitHub Actions)
8. **유닛 테스트** (pytest)
