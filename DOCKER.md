# Docker Compose 아키텍처 가이드

> 💡 **작성일**: 2025-10-21 (운영 기준 갱신: 2026-05-16)
> 💡 **상태**: 운영 중 (docker-compose + Nginx/Redis/FastAPI, DB는 외부 RDS)
> 💡 **목적**: AWS EC2 t3.small (2GB RAM) 기반 단계적 확장 가능한 Docker Compose 구조 설계
>
> **현재 컨테이너 메모리 cap** (`deploy.resources.limits.memory`):
>
> - FastAPI: 800M (좀비 Chrome 정리 + Selenium 여유)
> - Redis: 256M (maxmemory 100M + AOF rewrite/fragmentation/buffer 여유, 7.4-alpine pin)
> - Nginx: 32M
>
> 과거 t2.micro / 1GB / FastAPI 600M 표현은 historical 참고용. 현재 운영은 t3.small 기준.

---

## 📋 목차

1. [개요](#개요)
2. [AWS t3.small 기준](#aws-t3small-기준)
3. [단계별 아키텍처](#단계별-아키텍처)
4. [네트워크 및 보안](#네트워크-및-보안)
5. [리소스 최적화](#리소스-최적화)
6. [파일 구조](#파일-구조)
7. [설정 파일 예시](#설정-파일-예시)
8. [실행 방법](#실행-방법)
9. [모니터링 및 관리](#모니터링-및-관리)
10. [트러블슈팅](#트러블슈팅)

---

## 개요

### 현재 운영 구성 (2026-01-15)

- **구성**: Nginx + FastAPI + Redis (docker-compose)
- **DB**: 외부 RDS PostgreSQL (DATABASE_URL로 연결)
- **도메인**: fxi.kr (HTTPS/WSS)

### 목표

- AWS t3.small (2GB RAM) 기준 안정적 운영
- 단계적 확장 가능한 아키텍처 (SQLite → PostgreSQL → 서비스 분리)
- 메모리 효율성 극대화 (2GB 한계 고려)
- 무중단 마이그레이션 지원

### 핵심 설계 원칙

1. **단계적 확장** - 사용자 증가에 따라 점진적으로 복잡도 증가
2. **메모리 우선** - t3.small 2GB RAM 안에서 컨테이너 cap으로 안전 마진 확보 (FastAPI 800M / Redis 256M / Nginx 32M)
3. **보안 강화** - Nginx만 외부 노출, 내부 네트워크 분리
4. **무중단 전환** - 데이터 마이그레이션 스크립트 제공
5. **모니터링** - 헬스체크, 리소스 제한, 로그 수집

---

## AWS t3.small 기준

### 하드웨어 스펙

```
EC2 t3.small
├─ CPU: 2 vCPU
├─ RAM: 2GB (실제 사용 가능 ~1.9GB)
├─ Network: 최대 5Gbps
└─ Storage: EBS (gp3/gp2)
```

### 메모리 소비 예측

| 컨테이너 | Phase 1 | Phase 2 | Phase 3 |
|----------|---------|---------|---------|
| **Nginx** | ~15MB | ~15MB | ~15MB |
| **FastAPI** | ~200MB | ~250MB | ~300MB |
| **SQLite** | 포함 | - | - |
| **PostgreSQL** | - | ~150MB | ~200MB |
| **Redis** | - | ~50MB | ~80MB |
| **크롤러 (분리)** | - | - | ~150MB |
| **시스템 오버헤드** | ~100MB | ~100MB | ~100MB |
| **총 메모리 사용** | **~315MB** | **~565MB** | **~845MB** |
| **여유 메모리** | ✅ ~1.7GB | ✅ ~1.5GB | ✅ ~1.2GB |

**결론:**
- Phase 1~3: t3.small에서 안정적 운영 가능
- Phase 4 이상: t3.medium 이상 권장

---

## 단계별 아키텍처

### Phase 1: 초기 출시 (200-500명, SQLite)

> **운영 메모**: 현재는 SQLite 대신 외부 RDS를 사용 중이며, `DATABASE_URL`로 전환 가능합니다.

```
인터넷
  ↓
[포트 80/443]
  ↓
┌─────────────────────────────────────┐
│  Nginx (Reverse Proxy)              │
│  - HTTPS 종료 (Let's Encrypt)       │
│  - WebSocket 프록시                 │
│  - 정적 파일 서빙 (아이콘)          │
│  - Rate Limiting                    │
│  - 로그 수집                        │
└─────────────────────────────────────┘
  ↓
┌─────────────────────────────────────┐
│  FastAPI Container                  │
│  ┌───────────────────────────────┐  │
│  │  FastAPI App (main.py)        │  │
│  │  - WebSocket 서버              │  │
│  │  - REST API                    │  │
│  │  - 관리자 페이지               │  │
│  ├───────────────────────────────┤  │
│  │  Scheduler (APScheduler)      │  │
│  │  - 10개 크롤러 관리            │  │
│  │  - IN/OUT 모드 전환            │  │
│  ├───────────────────────────────┤  │
│  │  SQLite DB (Volume Mount)     │  │
│  │  - exchange_rates.db           │  │
│  │  - 10일치 데이터               │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

**파일:** `docker-compose.yml`

**장점:**
- 메모리 사용 최소화 (~315MB)
- 단순한 구조, 운영 부담 적음
- SQLite 파일 백업 간단 (cp 명령으로 가능)
- AWS 프리티어 무료

**단점:**
- 동시 쓰기 제한 (크롤러 10개는 문제없음)
- 수평 확장 불가 (단일 컨테이너)

**적용 시기:** 서비스 초기 ~ 사용자 500명

---

### Phase 2: 중기 확장 (500-1,000명, PostgreSQL + Redis) ⭐️ 현재

```
인터넷
  ↓
[포트 80/443]
  ↓
┌─────────────────────────────────────┐
│  Nginx                              │
│  - SSL Termination                  │
│  - WebSocket Sticky Session         │
│  - Gzip Compression                 │
└─────────────────────────────────────┘
  ↓
┌─────────────────────────────────────┐
│  FastAPI Container                  │
│  - 크롤러 + 웹서버 통합             │
│  - Redis 캐시 사용 (최신 환율)      │
│  - PostgreSQL 연결 풀 관리          │
└─────────────────────────────────────┘
  ↓                    ↓
┌──────────────┐  ┌──────────────┐
│ PostgreSQL   │  │   Redis      │
│ - 환율 DB    │  │ - 캐싱       │
│ - WAL 모드   │  │ - 세션 저장  │
│ - 64MB 버퍼  │  │ - 32MB 제한  │
└──────────────┘  └──────────────┘
```

**파일:** `docker-compose.yml` + `docker-compose.phase2.yml`

**마이그레이션 트리거:**
- WebSocket 동시 접속 100명 초과
- SQLite Write Lock 대기 시간 증가
- 사용자 불만 발생 (느린 응답)

**주요 변경사항:**
- SQLite → PostgreSQL (동시성 개선)
- Redis 도입 (최신 환율 캐싱, 10초 TTL)
- 메모리 최적화 설정 (shared_buffers=64MB, maxmemory=32MB)

**마이그레이션 절차:**
```bash
# 1. 데이터 백업
./scripts/backup-db.sh

# 2. PostgreSQL 컨테이너 시작
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d postgres

# 3. 데이터 마이그레이션 (무중단)
./scripts/migrate-to-postgres.sh

# 4. FastAPI 재시작 (새 DB 연결)
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d fastapi

# 5. 검증
./scripts/health-check.sh
```

**적용 시기:** 사용자 500명 ~ 1,000명

---

### Phase 3: 대규모 확장 (1,000명+, 서비스 분리)

```
                  인터넷
                    ↓
        [AWS ELB or CloudFront]
                    ↓
        ┌───────────────────────┐
        │      Nginx             │
        │  - SSL                 │
        │  - Load Balancing      │
        │  - Upstream Health     │
        └───────────────────────┘
                ↓
    ┌──────────┴──────────┐
    ↓                     ↓
┌─────────┐         ┌─────────┐
│FastAPI-1│         │FastAPI-2│
│(WebSocket)│       │(WebSocket)│
│(Read Only)│       │(Read Only)│
└─────────┘         └─────────┘

    ↓                     ↓
┌─────────────────────────────┐
│  크롤러 전용 Container        │
│  - 10개 크롤러만 실행         │
│  - DB Write 전담             │
│  - WebSocket 브로드캐스트    │
└─────────────────────────────┘
    ↓                     ↓
┌──────────┐        ┌──────────┐
│PostgreSQL│        │  Redis   │
│(Master)  │        │(Cluster) │
│(Write)   │        │(64MB)    │
└──────────┘        └──────────┘
```

**파일:** `docker-compose.yml` + `docker-compose.phase3.yml`

**주요 변경사항:**
- FastAPI 수평 확장 (2개 인스턴스)
- 크롤러 컨테이너 분리 (Write 전담)
- Nginx 로드 밸런싱
- Redis 메모리 증가 (64MB)

**요구사항:**
- EC2 t3.small 이상 (2GB RAM, ~$15/월)
- 또는 t3.small 2대 + ELB

**적용 시기:** 사용자 1,000명 이상

---

## 네트워크 및 보안

### Docker 네트워크 전략

**단일 네트워크 사용 (권장)**

```yaml
# networks 섹션 명시 안 함 (자동 생성)
# → exchange_rate_default 네트워크 자동 생성
# → 모든 서비스가 자동 연결
# → 서비스명으로 통신 가능 (예: http://fastapi:8000)
```

**왜 단일 네트워크인가?**
- **단순함**: 설정 복잡도 50% 감소
- **충분한 보안**: 포트 노출 제어로 외부 접근 차단
- **확장성**: 프로덕션 규모(사용자 2000명)까지 문제없음
- **Docker 철학**: "Convention over Configuration"

**네트워크 분리가 필요한 경우:**
- 마이크로서비스 10개 이상
- 컴플라이언스 요구사항 (PCI-DSS, HIPAA)
- Multi-tenant 환경
- 이 프로젝트는 해당 없음 ✅

### 포트 노출 전략 (실제 보안 제어)

| 서비스 | 포트 | 외부 노출 | 설정 방법 | 설명 |
|--------|------|-----------|----------|------|
| Nginx | 80, 443 | ✅ 노출 | `ports: ["80:80", "443:443"]` | HTTPS 엔드포인트 |
| FastAPI | 8000 | ❌ 내부만 | `expose: ["8000"]` | Docker 내부 네트워크만 |
| PostgreSQL | 5432 | ❌ 내부만 | `expose: ["5432"]` | Docker 내부 네트워크만 |
| Redis | 6379 | ❌ 내부만 | `expose: ["6379"]` | Docker 내부 네트워크만 |

**핵심:** `ports` vs `expose`로 보안 제어 (네트워크 분리 불필요)

### 보안 설정

1. **Nginx 보안 헤더**
```nginx
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "no-referrer-when-downgrade" always;
```

2. **Rate Limiting**
```nginx
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=conn_limit:10m;
```

3. **환경 변수 보호**
```bash
# .env 파일 (Git 제외)
chmod 600 .env
```

---

## 리소스 최적화

> ⚠️ **아래 Phase 1/Phase 2 예시는 historical 설계 진화 기록**. 현재 운영 cap은 본 문서 상단 frontmatter + 루트 [docker-compose.yml](docker-compose.yml) 기준 (FastAPI **800M** / Redis **256M** (`7.4-alpine` pin) / Nginx 32M). 예시 값(600M / 96M / `redis:7-alpine` 등)을 운영 compose에 직접 적용 금지.

### 컨테이너별 리소스 할당

#### Phase 1 (SQLite)

```yaml
services:
  nginx:
    deploy:
      resources:
        limits:
          cpus: '0.1'
          memory: 32M
        reservations:
          cpus: '0.05'
          memory: 16M

  fastapi:
    deploy:
      resources:
        limits:
          cpus: '0.9'      # 대부분의 CPU
          memory: 600M
        reservations:
          cpus: '0.5'
          memory: 300M
```

#### Phase 2 (PostgreSQL + Redis)

```yaml
services:
  postgres:
    deploy:
      resources:
        limits:
          cpus: '0.3'
          memory: 200M
        reservations:
          memory: 128M
    command: >
      postgres
      -c shared_buffers=64MB
      -c max_connections=20
      -c work_mem=4MB
      -c maintenance_work_mem=32MB

  redis:
    deploy:
      resources:
        limits:
          cpus: '0.2'
          memory: 96M
    command: >
      redis-server
      --maxmemory 64mb
      --maxmemory-policy allkeys-lru
      --appendonly yes
```

### 메모리 부족 대응 전략

- 메모리 85% 이상: OUT 모드 강제 전환 (크롤링 주기 10배)
- 메모리 75-85%: 크롤링 주기 50% 증가
- 메모리 60% 이하: 정상 주기 복귀
- 구현: `app/scheduler.py`에 `psutil` 기반 모니터링 추가

### OOM Killer 우선순위 조정

```yaml
services:
  fastapi:
    # 중요 서비스 보호 (점수가 낮을수록 보호)
    oom_score_adj: -500

  nginx:
    oom_score_adj: -300

  postgres:
    oom_score_adj: -400

  redis:
    # 필요 시 먼저 종료 가능
    oom_score_adj: 100
```

---

## 파일 구조

```
exchange-rate/
├── docker-compose.yml              # Phase 1 (SQLite)
├── docker-compose.phase2.yml       # Phase 2 오버라이드 (PostgreSQL + Redis)
├── docker-compose.phase3.yml       # Phase 3 오버라이드 (서비스 분리)
├── docker-compose.dev.yml          # 로컬 개발용 (포트 노출)
│
├── .env.example                    # 환경 변수 템플릿
├── .env                            # 실제 환경 변수 (git 제외)
├── .dockerignore                   # Docker 빌드 제외 파일
│
├── Dockerfile                      # FastAPI 이미지
├── Dockerfile.crawler              # 크롤러 전용 (Phase 3)
│
├── nginx/
│   ├── Dockerfile
│   ├── nginx.conf                  # 메인 설정
│   ├── conf.d/
│   │   ├── phase1.conf             # SQLite 버전
│   │   ├── phase2.conf             # PostgreSQL 버전
│   │   └── phase3.conf             # 로드 밸런싱
│   ├── ssl/
│   │   ├── certbot/                # Let's Encrypt
│   │   └── dhparam.pem
│   └── html/
│       └── 50x.html                # 에러 페이지
│
├── postgres/
│   ├── Dockerfile                  # PostgreSQL 커스텀 이미지
│   ├── init/
│   │   ├── 01-create-schema.sql    # 초기 스키마
│   │   └── 02-create-indexes.sql   # 인덱스
│   └── conf/
│       └── postgresql.conf         # 메모리 최적화 설정
│
├── redis/
│   ├── Dockerfile
│   └── redis.conf                  # 메모리 제한 설정
│
├── scripts/
│   ├── deploy.sh                   # 배포 스크립트
│   ├── migrate-to-postgres.sh      # SQLite → PostgreSQL 마이그레이션
│   ├── backup-db.sh                # DB 백업
│   ├── restore-db.sh               # DB 복구
│   ├── health-check.sh             # 헬스체크
│   └── ssl-renew.sh                # SSL 인증서 갱신
│
├── volumes/                        # Docker 볼륨 (git 제외)
│   ├── postgres-data/
│   ├── redis-data/
│   ├── sqlite-data/                # Phase 1
│   ├── logs/                       # 로그 수집
│   │   ├── nginx/
│   │   └── app/
│   └── ssl/                        # SSL 인증서
│
├── app/                            # FastAPI 앱 (기존 코드)
│   ├── main.py
│   ├── scheduler.py
│   └── ...
│
├── monitoring/                     # Phase 2+ (선택)
│   ├── prometheus.yml
│   └── grafana/
│
└── docs/
    ├── DOCKER.md                   # 이 파일
    ├── DEPLOYMENT.md               # 배포 가이드 (작성 예정)
    └── MIGRATION.md                # 마이그레이션 가이드 (작성 예정)
```

---

## 설정 파일 예시

> ⚠️ **아래 예시는 Phase 1/Phase 2 도입 시점 historical 가이드**. 현재 운영 compose는 루트 [docker-compose.yml](docker-compose.yml) 단일 진실 source. 예시 값(`redis:7-alpine`, FastAPI 600M, Redis 96M, postgres 200M 등)을 운영 환경에 그대로 copy-paste 금지 — 운영 cap은 본 문서 상단 frontmatter 참고.

### 1. docker-compose.yml (Phase 1)

```yaml
services:
  # ─────────────────────────────────────
  # Nginx (Reverse Proxy)
  # ─────────────────────────────────────
  nginx:
    build:
      context: ./nginx
      dockerfile: Dockerfile
    container_name: exchange-rate-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf.d/phase1.conf:/etc/nginx/conf.d/default.conf:ro
      - ./static:/var/www/static:ro
      - ./volumes/ssl:/etc/letsencrypt:ro
      - ./volumes/logs/nginx:/var/log/nginx
    depends_on:
      fastapi:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: '0.1'
          memory: 32M
        reservations:
          cpus: '0.05'
          memory: 16M
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "nginx", "-t"]
      interval: 30s
      timeout: 5s
      retries: 3

  # ─────────────────────────────────────
  # FastAPI (App + Crawler + SQLite)
  # ─────────────────────────────────────
  fastapi:
    build:
      context: .
      dockerfile: Dockerfile
      args:
        PYTHON_VERSION: 3.11
    container_name: exchange-rate-app
    env_file:
      - .env
    environment:
      - ENV=production
      - LOG_LEVEL=INFO
      - DATABASE_URL=sqlite:////data/exchange_rates.db
    volumes:
      - ./volumes/sqlite-data:/data
      - ./volumes/logs/app:/app/logs
    expose:
      - "8000"
    deploy:
      resources:
        limits:
          cpus: '0.9'
          memory: 600M
        reservations:
          cpus: '0.5'
          memory: 300M
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    oom_score_adj: -500  # OOM Killer 보호

# ─────────────────────────────────────
# Volumes
# ─────────────────────────────────────
volumes:
  sqlite-data:
  logs:
```

### 2. docker-compose.phase2.yml (PostgreSQL + Redis)

```yaml
services:
  # FastAPI 설정 오버라이드
  fastapi:
    environment:
      - DATABASE_URL=postgresql://exchange_user:${DB_PASSWORD}@postgres:5432/exchange_rate_db
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  # ─────────────────────────────────────
  # PostgreSQL
  # ─────────────────────────────────────
  postgres:
    image: postgres:15-alpine
    container_name: exchange-rate-postgres
    environment:
      - POSTGRES_USER=exchange_user
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=exchange_rate_db
      - POSTGRES_INITDB_ARGS=--encoding=UTF-8 --locale=C
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    command: >
      postgres
      -c shared_buffers=64MB
      -c max_connections=20
      -c work_mem=4MB
      -c maintenance_work_mem=32MB
      -c effective_cache_size=256MB
      -c random_page_cost=1.1
      -c checkpoint_completion_target=0.9
    expose:
      - "5432"
    deploy:
      resources:
        limits:
          cpus: '0.3'
          memory: 200M
        reservations:
          memory: 128M
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U exchange_user -d exchange_rate_db"]
      interval: 10s
      timeout: 5s
      retries: 5
    oom_score_adj: -400

  # ─────────────────────────────────────
  # Redis
  # ─────────────────────────────────────
  redis:
    image: redis:7-alpine
    container_name: exchange-rate-redis
    command: >
      redis-server
      --maxmemory 64mb
      --maxmemory-policy allkeys-lru
      --appendonly yes
      --save 900 1
      --save 300 10
    volumes:
      - redis-data:/data
    expose:
      - "6379"
    deploy:
      resources:
        limits:
          cpus: '0.2'
          memory: 96M
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    oom_score_adj: 100

# ─────────────────────────────────────
# Volumes
# ─────────────────────────────────────
volumes:
  postgres-data:
  redis-data:
```

### 3. .env.example

```bash
# ─────────────────────────────────────
# 환경 설정
# ─────────────────────────────────────
ENV=production
LOG_LEVEL=INFO

# ─────────────────────────────────────
# 관리자 페이지
# ─────────────────────────────────────
ADMIN_PASSWORD=your_strong_password_here

# ─────────────────────────────────────
# 데이터베이스 (Phase 2+)
# ─────────────────────────────────────
DB_PASSWORD=your_db_password_here

# ─────────────────────────────────────
# 텔레그램 알림 (Phase 2+)
# ─────────────────────────────────────
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ─────────────────────────────────────
# SSL/TLS
# ─────────────────────────────────────
DOMAIN=your-domain.com
EMAIL=your-email@example.com
```

### 4. .dockerignore

```bash
# ─────────────────────────────────────
# 개발 도구
# ─────────────────────────────────────
.claude/
.git/
.gitignore
.vscode/
.idea/

# ─────────────────────────────────────
# Python
# ─────────────────────────────────────
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.venv/
venv/
env/

# ─────────────────────────────────────
# 로그 및 데이터 (서버에서 새로 생성)
# ─────────────────────────────────────
logs/
*.log
volumes/
data/*.db
data/*.db-shm
data/*.db-wal

# ─────────────────────────────────────
# Docker 관련
# ─────────────────────────────────────
docker-compose*.yml
Dockerfile*
.dockerignore

# ─────────────────────────────────────
# 문서
# ─────────────────────────────────────
*.md
docs/

# ─────────────────────────────────────
# 기타
# ─────────────────────────────────────
.DS_Store
*.swp
*.swo
node_modules/
```

### 5. Dockerfile (FastAPI)

```dockerfile
# ─────────────────────────────────────
# Stage 1: Builder
# ─────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 빌드 의존성 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ─────────────────────────────────────
# Stage 2: Runtime
# ─────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 런타임 의존성만 설치 (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 빌더에서 패키지 복사
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 앱 코드 복사
COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/

# 데이터 디렉토리 생성 (볼륨 마운트용)
RUN mkdir -p /data /app/logs

# 비루트 사용자 생성 (보안)
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app /data
USER appuser

# 환경 변수
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# 포트 노출
EXPOSE 8000

# 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# 실행
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

### 6. nginx/conf.d/phase1.conf

```nginx
# ─────────────────────────────────────
# Upstream (FastAPI 백엔드)
# ─────────────────────────────────────
upstream fastapi_backend {
    server fastapi:8000 max_fails=3 fail_timeout=30s;
}

# ─────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=conn_limit:10m;

# ─────────────────────────────────────
# HTTP → HTTPS 리다이렉트
# ─────────────────────────────────────
server {
    listen 80;
    server_name _;

    # Let's Encrypt ACME Challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # 나머지는 HTTPS로 리다이렉트
    location / {
        return 301 https://$host$request_uri;
    }
}

# ─────────────────────────────────────
# HTTPS 서버
# ─────────────────────────────────────
server {
    listen 443 ssl http2;
    server_name _;

    # SSL 설정
    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # 보안 헤더
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;

    # Gzip 압축
    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml;

    # ─────────────────────────────────────
    # WebSocket (최대 우선순위)
    # ─────────────────────────────────────
    location /ws {
        proxy_pass http://fastapi_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 타임아웃 (10분)
        proxy_connect_timeout 600s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
    }

    # ─────────────────────────────────────
    # API (Rate Limiting 적용)
    # ─────────────────────────────────────
    location /api/ {
        limit_req zone=api_limit burst=20 nodelay;
        limit_conn conn_limit 10;

        proxy_pass http://fastapi_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 10s;
        proxy_send_timeout 10s;
        proxy_read_timeout 10s;
    }

    # ─────────────────────────────────────
    # 관리자 페이지
    # ─────────────────────────────────────
    location /admin {
        proxy_pass http://fastapi_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ─────────────────────────────────────
    # 정적 파일 (Nginx에서 직접 서빙)
    # ─────────────────────────────────────
    location /static/ {
        alias /var/www/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # ─────────────────────────────────────
    # 루트 및 기타
    # ─────────────────────────────────────
    location / {
        proxy_pass http://fastapi_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ─────────────────────────────────────
    # 에러 페이지
    # ─────────────────────────────────────
    error_page 500 502 503 504 /50x.html;
    location = /50x.html {
        root /usr/share/nginx/html;
    }
}
```

---

## 실행 방법

### Phase 1 배포 (초기)

```bash
# 1. 환경 변수 설정
cp .env.example .env
vim .env  # 비밀번호, 도메인 등 수정

# 2. 디렉토리 생성
mkdir -p volumes/{sqlite-data,logs/{nginx,app},ssl}

# 3. SSL 인증서 발급 (Let's Encrypt)
# Nginx 없이 직접 발급
docker run -it --rm \
  -v $(pwd)/volumes/ssl:/etc/letsencrypt \
  -v $(pwd)/nginx/html:/var/www/html \
  -p 80:80 \
  certbot/certbot certonly \
  --standalone \
  -d your-domain.com \
  --email your-email@example.com \
  --agree-tos

# 4. Docker Compose 빌드 및 실행
docker-compose build
docker-compose up -d

# 5. 로그 확인
docker-compose logs -f fastapi

# 6. 헬스체크
curl https://your-domain.com/health
```

### Phase 2로 마이그레이션

```bash
# 1. 데이터 백업 (안전장치)
./scripts/backup-db.sh

# 2. PostgreSQL 시작 (FastAPI는 아직 SQLite 사용)
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d postgres redis

# 3. PostgreSQL 초기화 대기
docker-compose logs -f postgres
# "database system is ready to accept connections" 확인

# 4. 데이터 마이그레이션 (무중단)
./scripts/migrate-to-postgres.sh

# 5. FastAPI 재시작 (PostgreSQL 연결)
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d fastapi

# 6. Nginx 설정 교체 (phase2.conf 사용)
docker-compose restart nginx

# 7. 검증
./scripts/health-check.sh
curl https://your-domain.com/api/rates
```

### 로컬 개발 환경

```bash
# docker-compose.dev.yml 사용 (포트 노출)
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up

# 접속
# - FastAPI: http://localhost:8000
# - Nginx: http://localhost
# - PostgreSQL: localhost:5432 (Phase 2)
# - Redis: localhost:6379 (Phase 2)
```

---

## 모니터링 및 관리

### 리소스 사용량 확인

```bash
# 컨테이너별 리소스 사용
docker stats

# 예상 출력 (현재 운영 — t3.small / 외부 RDS):
# CONTAINER             CPU %   MEM USAGE / LIMIT    MEM %   NET I/O
# exchange-rate-nginx   0.00%   5MB / 32MB           16%     ...
# exchange-rate-app     3%      220MB / 800MB        28%     ...
# exchange-rate-redis   1%      8MB / 256MB           3%     ...
#
# 참고: PostgreSQL은 외부 AWS RDS이라 컨테이너 미존재. DB 메모리는 RDS 인스턴스
# (db.t4g.micro 1GB) 측에서 별도 모니터링 (CloudWatch).
```

### 로그 관리

```bash
# 모든 로그 확인
docker-compose logs --tail=100 -f

# 특정 서비스
docker-compose logs -f nginx
docker-compose logs -f fastapi

# 에러만 필터링
docker-compose logs fastapi | grep -i error

# 로그 파일 직접 확인
tail -f volumes/logs/app/error.log
tail -f volumes/logs/nginx/error.log
```

### 데이터 백업 및 복구

```bash
# SQLite 백업
./scripts/backup-db.sh
# → volumes/backups/exchange_rates_20251021_143000.db

# PostgreSQL 백업
docker-compose exec postgres pg_dump \
  -U exchange_user -d exchange_rate_db \
  > backup_$(date +%Y%m%d_%H%M%S).sql

# 복구
./scripts/restore-db.sh backup_20251021_143000.sql
```

### SSL 인증서 갱신

```bash
# 자동 갱신 (crontab)
0 3 1 * * /path/to/scripts/ssl-renew.sh

# 수동 갱신
./scripts/ssl-renew.sh

# Nginx 재시작
docker-compose restart nginx
```

### 컨테이너 재시작

```bash
# 전체 재시작
docker-compose restart

# 특정 서비스만
docker-compose restart fastapi

# 설정 변경 후 재빌드
docker-compose up -d --build fastapi
```

---

## 트러블슈팅

### 메모리 부족 (OOM Killer 발동)

**증상:**
```bash
docker-compose logs fastapi
# "Killed" 메시지 또는 컨테이너 갑자기 종료
```

**해결:**
```bash
# 1. 메모리 사용량 확인
docker stats

# 2. OUT 모드 강제 전환 (크롤링 주기 늘림)
docker-compose exec fastapi python -c "
from app.scheduler import switch_to_out_mode
switch_to_out_mode()
"

# 3. Redis 메모리 줄이기 (Phase 2)
# docker-compose.phase2.yml 수정
# maxmemory 64mb → 32mb

# 4. EC2 스왑 메모리 추가 (임시 방편)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### WebSocket 연결 끊김

**증상:**
```
클라이언트에서 "WebSocket connection closed" 에러
```

**해결:**
```bash
# 1. Nginx 타임아웃 확인
docker-compose exec nginx cat /etc/nginx/conf.d/default.conf
# proxy_read_timeout 600s; 확인

# 2. FastAPI 로그 확인
docker-compose logs -f fastapi | grep -i websocket

# 3. Nginx 재시작
docker-compose restart nginx
```

### PostgreSQL 연결 실패 (Phase 2)

**증상:**
```
FATAL: password authentication failed for user "exchange_user"
```

**해결:**
```bash
# 1. 환경 변수 확인
docker-compose exec fastapi env | grep DATABASE_URL

# 2. PostgreSQL 로그 확인
docker-compose logs postgres

# 3. 수동 연결 테스트
docker-compose exec postgres psql -U exchange_user -d exchange_rate_db

# 4. 비밀번호 재설정
docker-compose exec postgres psql -U postgres -c \
  "ALTER USER exchange_user WITH PASSWORD 'new_password';"
```

### SSL 인증서 문제

**증상:**
```
curl: (60) SSL certificate problem: certificate has expired
```

**해결:**
```bash
# 1. 인증서 유효기간 확인
openssl x509 -in volumes/ssl/live/your-domain.com/fullchain.pem -noout -dates

# 2. 갱신
./scripts/ssl-renew.sh

# 3. Nginx 재시작
docker-compose restart nginx
```

### 디스크 용량 부족

**증상:**
```
docker-compose up -d
# Error: No space left on device
```

**해결:**
```bash
# 1. 사용하지 않는 Docker 이미지/컨테이너 제거
docker system prune -a --volumes

# 2. 오래된 로그 삭제
find volumes/logs -name "*.log.*" -mtime +7 -delete

# 3. 오래된 DB 백업 삭제
find volumes/backups -name "*.db" -mtime +30 -delete

# 4. PostgreSQL VACUUM (Phase 2)
docker-compose exec postgres psql -U exchange_user -d exchange_rate_db -c "VACUUM FULL;"
```

---

## 다음 단계

### 즉시 실행 가능한 작업

1. **Phase 1 구현**
   - [ ] `.dockerignore` 생성
   - [ ] `Dockerfile` 작성
   - [ ] `docker-compose.yml` 작성
   - [ ] `nginx/` 디렉토리 및 설정 파일 생성
   - [ ] 로컬에서 테스트

2. **스크립트 작성**
   - [ ] `scripts/backup-db.sh`
   - [ ] `scripts/health-check.sh`
   - [ ] `scripts/ssl-renew.sh`

3. **문서 보완**
   - [ ] `DEPLOYMENT.md` - 상세 배포 가이드
   - [ ] `MIGRATION.md` - 단계별 마이그레이션 절차

### Phase 2 준비 (사용자 500명 도달 시)

1. **PostgreSQL 마이그레이션**
   - [ ] `postgres/` 디렉토리 생성
   - [ ] 스키마 변환 스크립트 작성
   - [ ] `scripts/migrate-to-postgres.sh` 작성
   - [ ] 무중단 전환 테스트

2. **Redis 캐싱 구현**
   - [ ] `app/cache.py` 작성
   - [ ] 최신 환율 캐싱 로직 추가
   - [ ] TTL 10초 설정

3. **모니터링 강화**
   - [ ] Prometheus + Grafana (선택)
   - [ ] 메모리 알림 설정

### Phase 3 준비 (사용자 1,000명 도달 시)

1. **EC2 인스턴스 업그레이드**
   - [ ] t3.medium으로 업그레이드
   - [ ] 또는 t3.small 2대 + ELB

2. **서비스 분리**
   - [ ] `Dockerfile.crawler` 작성
   - [ ] 크롤러 전용 컨테이너
   - [ ] FastAPI 수평 확장 (2개)

3. **로드 밸런싱**
   - [ ] Nginx 업스트림 설정
   - [ ] WebSocket Sticky Session

---

## 참고 자료

- [Docker Compose 공식 문서](https://docs.docker.com/compose/)
- [Nginx WebSocket 프록시 가이드](https://nginx.org/en/docs/http/websocket.html)
- [PostgreSQL 메모리 최적화](https://wiki.postgresql.org/wiki/Tuning_Your_PostgreSQL_Server)
- [Redis 메모리 관리](https://redis.io/docs/management/optimization/memory-optimization/)
- [AWS 프리티어 한계](https://aws.amazon.com/free/)

---

**작성일:** 2025-10-21
**작성자:** Claude Code + Jay
**버전:** 1.0.0
**상태:** 운영 중 (docker-compose + Nginx/Redis/FastAPI, DB는 외부 RDS)
