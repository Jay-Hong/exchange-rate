# AWS 상용화 체크리스트

> 💡 **목적**: Exchange Rate 서비스를 AWS에서 상용화할 때 필요한 필수 요소 정리
> 📅 **작성일**: 2025-11-15
> 🔗 **관련 문서**: [DEPLOYMENT.md](DEPLOYMENT.md), [DOCKER.md](DOCKER.md)

---

## 📋 체크리스트 요약

| 단계 | 완료 여부 | 우선순위 | 예상 소요 시간 |
|------|-----------|----------|----------------|
| HTTPS/SSL 설정 | ⬜ | 🚨 CRITICAL | 10분 |
| 보안 강화 (CORS, Rate Limiting) | ⬜ | 🚨 CRITICAL | 30분 |
| 데이터 백업 자동화 | ⬜ | 🚨 CRITICAL | 20분 |
| 외부 모니터링 (Uptime, Sentry) | ⬜ | 🚨 CRITICAL | 15분 |
| CloudWatch Logs 통합 | ⬜ | 🚨 CRITICAL | 15분 |
| PostgreSQL 마이그레이션 준비 | ⬜ | 📋 HIGH | 1-2시간 |
| CI/CD 파이프라인 | ⬜ | 📋 HIGH | 30-60분 |
| 무중단 배포 설정 | ⬜ | 📋 HIGH | 30분 |
| Auto Scaling + Load Balancer | ⬜ | 🎯 MEDIUM | 1-2시간 |
| Redis 캐싱 | ⬜ | 🎯 MEDIUM | 1시간 |

---

## 🚨 Tier 1: 출시 전 필수 (Production Blocker)

### 1. HTTPS/SSL 인증서

**현재 상태**: ❌ HTTP only

**위험도**: CRITICAL
- iOS ATS (App Transport Security)로 인해 HTTP 기본 차단
- WebSocket (`ws://`)이 iOS 앱에서 작동 안 함 → `wss://` 필수
- 관리자 페이지 Basic Auth 비밀번호 평문 노출

**해결 방법**:
```bash
# Let's Encrypt (무료)
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com

# 자동 갱신 테스트
sudo certbot renew --dry-run

# Nginx 설정 (자동 생성됨)
# /etc/nginx/sites-available/default
server {
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
    }
}
```

**비용**: 무료
**유지보수**: 자동 갱신 (90일)
**소요 시간**: 10분

**대안 (도메인 없을 경우)**:
- **Cloudflare Tunnel**: 무료, 도메인 자동 제공, HTTPS 자동
- **ngrok**: 개발/테스트용, 유료 ($8/월)

---

### 2. 보안 강화

#### 2.1 AWS Security Group

**현재 상태**: ⚠️ 기본 설정 (추정)

**필요 설정**:
```yaml
# EC2 Security Group 규칙
Inbound Rules:
  - Type: HTTPS (443)
    Source: 0.0.0.0/0  # 전체 허용

  - Type: HTTP (80)
    Source: 0.0.0.0/0  # HTTPS 리다이렉트용

  - Type: SSH (22)
    Source: YOUR_IP_ONLY  # 관리자 IP만

  - Type: Custom TCP (8000)
    Source: 127.0.0.1/32  # localhost만 (Nginx 프록시)

Outbound Rules:
  - All traffic: 0.0.0.0/0  # 크롤링용
```

#### 2.2 CORS 설정

**현재 코드 확인 필요**: `app/main.py`

```python
# app/main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-domain.com",
        "https://admin.your-domain.com",
        # 개발 환경
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

**주의**: `allow_origins=["*"]` 금지 (CSRF 취약)

#### 2.3 Rate Limiting

**목적**: DDoS 방지, 크롤링 봇 차단

**방법 1: FastAPI-Limiter (간단)**
```python
# requirements.txt
fastapi-limiter==0.1.5
redis==4.5.1

# app/main.py
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
import redis.asyncio as redis

@app.on_event("startup")
async def startup():
    redis_conn = await redis.from_url("redis://localhost:6379")
    await FastAPILimiter.init(redis_conn)

@app.get("/api/rates", dependencies=[Depends(RateLimiter(times=60, seconds=60))])
async def get_rates():
    # 1분에 60회 제한
    ...
```

**방법 2: Nginx (프로덕션 권장)**
```nginx
# /etc/nginx/sites-available/default
http {
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;

    server {
        location /api/ {
            limit_req zone=api burst=20 nodelay;
            proxy_pass http://localhost:8000;
        }
    }
}
```

#### 2.4 AWS WAF (선택사항, $5/월)

**보호 대상**:
- SQL Injection
- XSS (Cross-Site Scripting)
- Bot 트래픽

**설정**: AWS Console → WAF → Web ACL → Managed Rules

---

### 3. 데이터 백업 자동화

**현재 상태**: ❌ 백업 없음 (SQLite 파일만)

**위험**: 인스턴스 종료/장애 시 데이터 완전 유실

#### 3.1 S3 자동 백업 (일일)

```bash
# /home/ubuntu/backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
DB_PATH="/home/ubuntu/exchange-rate/data/exchange_rates.db"
S3_BUCKET="s3://your-backup-bucket/db-backups/"

# SQLite 백업 (일관성 보장)
sqlite3 $DB_PATH ".backup /tmp/backup_$DATE.db"

# S3 업로드
aws s3 cp /tmp/backup_$DATE.db $S3_BUCKET

# 로컬 임시 파일 삭제
rm /tmp/backup_$DATE.db

# 30일 이상 오래된 백업 삭제
aws s3 ls $S3_BUCKET | awk '{print $4}' | head -n -30 | xargs -I {} aws s3 rm $S3_BUCKET{}
```

```bash
# crontab -e (매일 새벽 3시)
0 3 * * * /home/ubuntu/backup.sh >> /var/log/backup.log 2>&1
```

#### 3.2 EBS 스냅샷 자동화

**AWS Console**:
1. EC2 → Elastic Block Store → Volumes
2. 볼륨 선택 → Actions → Create Snapshot
3. Data Lifecycle Manager → Create Lifecycle Policy
   - 매일 자동 스냅샷
   - 7일 보관

**비용**: 0.05$/GB-월 (1GB면 $0.05/월)

#### 3.3 백업 복구 테스트

```bash
# 매월 1일 자동 테스트
# test_restore.sh
#!/bin/bash
LATEST_BACKUP=$(aws s3 ls $S3_BUCKET | sort | tail -n 1 | awk '{print $4}')
aws s3 cp $S3_BUCKET$LATEST_BACKUP /tmp/test_restore.db

# 무결성 검증
sqlite3 /tmp/test_restore.db "PRAGMA integrity_check;"

# 레코드 수 확인
RECORD_COUNT=$(sqlite3 /tmp/test_restore.db "SELECT COUNT(*) FROM bank_exchange_rates;")
echo "Backup validated: $RECORD_COUNT records"
```

---

### 4. 외부 모니터링 & 즉각 알림

**현재 상태**: ❌ 관리자 페이지 수동 확인만

#### 4.1 UptimeRobot (무료)

**설정**:
1. [uptimerobot.com](https://uptimerobot.com) 가입
2. New Monitor:
   - Type: HTTPS
   - URL: `https://your-domain.com/health`
   - Interval: 5분
   - Alert Contacts: 이메일, 텔레그램

**알림 조건**:
- 2회 연속 실패 시 알림
- 복구 시 알림

**무료 티어**: 50개 모니터, 5분 간격

#### 4.2 Sentry (에러 추적)

**설정**:
```bash
# requirements.txt
sentry-sdk[fastapi]==1.38.0
```

```python
# app/main.py
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

sentry_sdk.init(
    dsn="https://your-sentry-dsn@sentry.io/project-id",
    traces_sample_rate=0.1,  # 10%만 추적 (비용 절감)
    integrations=[FastApiIntegration()],
    environment="production",
)
```

**장점**:
- Python Exception 자동 캡처
- 스택 트레이스 + 컨텍스트
- 이메일/Slack 알림

**무료 티어**: 5,000 이벤트/월

#### 4.3 텔레그램 봇 알림 활성화

**현재 코드**: `app/notifications/telegram.py` (이미 구현됨)

**활성화 방법**:
```python
# app/scheduler.py
from app.notifications.telegram import send_telegram_alert

async def run_crawler(crawler_name: str):
    try:
        result = await crawler_func()
    except Exception as e:
        logger.exception(f"크롤러 실패: {crawler_name}")

        # 텔레그램 알림 추가
        await send_telegram_alert(
            f"🚨 크롤러 실패\n"
            f"은행: {crawler_name}\n"
            f"에러: {str(e)[:100]}"
        )
```

**알림 조건** (추천):
- 크롤러 3회 연속 실패
- WebSocket 브로드캐스트 실패
- 메모리 사용량 80% 이상
- Chrome 프로세스 5개 이상

---

### 5. CloudWatch Logs 통합

**현재 상태**: ❌ Docker 컨테이너 로그만 (휘발성)

**문제점**:
- 인스턴스 재시작 시 로그 유실
- 검색 불가능
- 알림 설정 불가

#### 5.1 awslogs 드라이버 설정

```yaml
# docker-compose.yml
version: '3.8'

services:
  app:
    image: exchange-rate-app
    logging:
      driver: awslogs
      options:
        awslogs-region: ap-northeast-2
        awslogs-group: /ecs/exchange-rate
        awslogs-stream-prefix: app
        awslogs-create-group: "true"
```

#### 5.2 IAM 권한 추가

```json
// EC2 Instance Role에 추가
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

#### 5.3 CloudWatch Alarms

**AWS Console** → CloudWatch → Alarms:

1. **에러 로그 알림**:
   - Metric: `ERROR` 키워드 개수
   - Threshold: 10개/5분
   - Action: SNS → 이메일/텔레그램

2. **크롤러 실패 알림**:
   - Metric: `크롤링 실패` 로그 개수
   - Threshold: 3개/10분

**비용**: 무료 티어 (10개 알람, 5GB 로그)

---

## 📋 Tier 2: 출시 직후 1개월 내 (Growth Enabler)

### 6. PostgreSQL 마이그레이션 준비

**타이밍**: 다음 조건 중 하나 충족 시
- 동시 접속 50명 이상
- DB 크기 100MB 이상
- SQLite 락 에러 빈번

**현재 상태**: ✅ SQLite 5.4MB (여유 있음)

#### 6.1 RDS PostgreSQL 프리티어

**스펙**:
- db.t3.micro (2 vCPU, 1GB RAM)
- 20GB SSD 스토리지
- 12개월 무료

**비용 (프리티어 이후)**:
- 인스턴스: $13/월
- 스토리지: $2.30/월 (20GB)

#### 6.2 마이그레이션 스크립트 준비

```python
# migrations/sqlite_to_postgres.py
import sqlite3
import psycopg2
from tqdm import tqdm

def migrate():
    # SQLite 연결
    sqlite_conn = sqlite3.connect("data/exchange_rates.db")
    sqlite_cursor = sqlite_conn.cursor()

    # PostgreSQL 연결
    pg_conn = psycopg2.connect(
        host="your-rds-endpoint.rds.amazonaws.com",
        database="exchange_rate",
        user="admin",
        password="password"
    )
    pg_cursor = pg_conn.cursor()

    # 데이터 마이그레이션
    tables = ["investing_exchange_rates", "bank_exchange_rates"]
    for table in tables:
        rows = sqlite_cursor.execute(f"SELECT * FROM {table}").fetchall()
        for row in tqdm(rows, desc=f"Migrating {table}"):
            pg_cursor.execute(
                f"INSERT INTO {table} VALUES ({','.join(['%s']*len(row))})",
                row
            )

    pg_conn.commit()
    print("Migration completed!")

if __name__ == "__main__":
    migrate()
```

#### 6.3 SQLAlchemy 설정 변경

```python
# app/database.py (변경 최소)
import os

# 환경 변수로 DB 전환
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./data/exchange_rates.db"  # 기본값
)

# PostgreSQL 사용 시
# DATABASE_URL = "postgresql://admin:password@rds-endpoint/exchange_rate"

engine = create_engine(
    DATABASE_URL,
    # SQLite용 설정 제거
    # connect_args={"check_same_thread": False}
)
```

**마이그레이션 시 코드 변경**: 1줄 (환경 변수만)

---

### 7. CI/CD 파이프라인

**목적**: 1분 배포, 사람 실수 제거

#### 7.1 GitHub Actions

```yaml
# .github/workflows/deploy.yml
name: Deploy to AWS EC2

on:
  push:
    branches: [master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest

      - name: Run tests
        run: pytest tests/ -v

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Deploy to EC2
        env:
          SSH_PRIVATE_KEY: ${{ secrets.EC2_SSH_KEY }}
          HOST: ${{ secrets.EC2_HOST }}
        run: |
          echo "$SSH_PRIVATE_KEY" > key.pem
          chmod 600 key.pem

          ssh -i key.pem ubuntu@$HOST << 'EOF'
            cd /home/ubuntu/exchange-rate
            git pull origin master
            docker compose up -d --build
            docker compose logs -f --tail 50
          EOF
```

#### 7.2 Secrets 설정

**GitHub Repository** → Settings → Secrets:
- `EC2_SSH_KEY`: EC2 프라이빗 키
- `EC2_HOST`: EC2 퍼블릭 IP

#### 7.3 롤백 스크립트

```bash
# rollback.sh
#!/bin/bash
PREV_COMMIT=$(git log --oneline -2 | tail -n 1 | awk '{print $1}')

echo "Rolling back to $PREV_COMMIT..."
git checkout $PREV_COMMIT
docker compose up -d --build

echo "Rollback completed!"
```

---

### 8. 무중단 배포 (Rolling Update)

**현재**: `docker compose up -d --build` (2-3초 다운타임)

#### 8.1 Docker Compose Rolling Update

```yaml
# docker-compose.yml
version: '3.8'

services:
  app:
    image: exchange-rate-app
    deploy:
      replicas: 2  # 2개 컨테이너 동시 실행
      update_config:
        parallelism: 1  # 한 번에 1개씩 업데이트
        delay: 10s      # 10초 간격
        order: start-first  # 새 컨테이너 먼저 시작
      rollback_config:
        parallelism: 1
        delay: 5s
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
```

#### 8.2 Nginx Load Balancing

```nginx
# /etc/nginx/sites-available/default
upstream backend {
    server 127.0.0.1:8000;
    server 127.0.0.1:8001;

    # Health check
    least_conn;
}

server {
    listen 443 ssl;

    location / {
        proxy_pass http://backend;
        proxy_next_upstream error timeout http_500;
    }
}
```

**결과**: 완전 무중단 배포 (0초 다운타임)

---

## 🎯 Tier 3: 성장 단계 (500명 이상)

### 9. Auto Scaling + Load Balancer

**현재**: 단일 t2.micro 인스턴스

**확장 아키텍처**:
```
                  ┌─────────────┐
                  │     ALB     │ (Application Load Balancer)
                  └──────┬──────┘
                         │
            ┌────────────┼────────────┐
            │            │            │
       ┌────▼────┐  ┌────▼────┐  ┌────▼────┐
       │ EC2 #1  │  │ EC2 #2  │  │ EC2 #3  │
       │ t3.small│  │ t3.small│  │ t3.small│
       └─────────┘  └─────────┘  └─────────┘
```

#### 9.1 Auto Scaling Group 설정

**AWS Console**:
1. EC2 → Auto Scaling Groups → Create
2. Launch Template:
   - AMI: 현재 EC2 스냅샷
   - Instance Type: t3.small
   - User Data: Docker 자동 시작 스크립트
3. Scaling Policy:
   - Target Metric: CPU Utilization 70%
   - Min: 2, Max: 4, Desired: 2

#### 9.2 Application Load Balancer

**설정**:
1. EC2 → Load Balancers → Create ALB
2. Listeners:
   - HTTP:80 → Redirect to HTTPS:443
   - HTTPS:443 → Target Group (EC2 instances)
3. Health Check:
   - Path: `/health`
   - Interval: 30초
   - Healthy Threshold: 2

**비용**:
- ALB: $16/월
- t3.small × 2: $30/월
- **Total**: ~$46/월

---

### 10. Redis 캐싱

**목적**: DB 부하 감소, 응답 속도 개선 (실측 필요)

#### 10.1 Redis 설치 옵션

**옵션 A: EC2에 Redis 설치 (권장)**
- Docker Compose로 설치
- 추가 비용: $0
- 메모리: 100MB (추정)
- 관리: 간단 (docker-compose)

**옵션 B: ElastiCache for Redis (확장 시)**
- **⚠️ 주의**: ElastiCache는 프리티어 대상 아님
- cache.t3.micro (0.5GB RAM)
- 비용: $11/월 (첫 달부터)
- 관리형 서비스 (고가용성)

#### 10.2 캐싱 전략

```python
# app/cache.py
import redis.asyncio as redis
import json
from functools import wraps

redis_client = redis.from_url("redis://elasticache-endpoint:6379")

def cache(ttl: int = 10):
    """TTL 기반 캐싱 데코레이터"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}:{args}:{kwargs}"

            # 캐시 조회
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)

            # DB 조회
            result = await func(*args, **kwargs)

            # 캐시 저장
            await redis_client.setex(
                cache_key,
                ttl,
                json.dumps(result, default=str)
            )

            return result
        return wrapper
    return decorator
```

```python
# app/crud.py
@cache(ttl=10)  # 10초 캐싱
async def get_latest_rates(db: Session):
    """최신 환율 조회 (캐싱)"""
    return db.query(BankExchangeRate).all()
```

**효과**:
- DB 쿼리: 초당 100회 → 초당 10회 (90% 감소)
- 응답 속도: 50ms → 15ms (3배 개선)

---

## 💰 즉시 적용 가능한 무료 도구 (30분 내 설정)

| 도구 | 용도 | 비용 | 무료 티어 | 설정 시간 |
|------|------|------|-----------|----------|
| **Let's Encrypt** | HTTPS 인증서 | 무료 | 영구 무료 | 10분 |
| **UptimeRobot** | Uptime 모니터링 | 무료 | 50개 모니터 | 5분 |
| **Sentry** | 에러 추적 | 무료 | 5k 이벤트/월 | 10분 |
| **CloudWatch Logs** | 로그 중앙화 | 무료 | 5GB/월, 10개 알람 | 15분 |
| **GitHub Actions** | CI/CD | 무료 | 2,000분/월 | 30분 |
| **S3 백업** | 데이터 백업 | $0.023/GB | 5GB/월 (12개월) | 20분 |

---

## 🎬 추천 실행 순서 (4주 계획)

### Week 1: 보안 + 모니터링 (출시 전 필수)

**Day 1-2: HTTPS 설정**
- [ ] 도메인 구매 (Cloudflare, $10/년)
- [ ] Let's Encrypt 인증서 발급
- [ ] Nginx HTTPS 리버스 프록시 설정
- [ ] HTTP → HTTPS 리다이렉트
- [ ] WebSocket `wss://` 테스트

**Day 3-4: 보안 강화**
- [ ] AWS Security Group 재설정
- [ ] CORS 화이트리스트 적용
- [ ] Rate Limiting (Nginx)
- [ ] Basic Auth → JWT 전환 (관리자 페이지)

**Day 5: 모니터링 설정**
- [ ] UptimeRobot 가입 + 모니터 생성
- [ ] Sentry 연동 + 테스트
- [ ] 텔레그램 봇 알림 활성화
- [ ] CloudWatch Logs 통합

**Day 6: 백업 자동화**
- [ ] S3 버킷 생성
- [ ] 백업 스크립트 작성 + cron 등록
- [ ] EBS 스냅샷 자동화
- [ ] 백업 복구 테스트

**Day 7: 점검 및 문서화**
- [ ] 전체 시스템 테스트
- [ ] 롤백 테스트
- [ ] 운영 매뉴얼 작성

---

### Week 2: 안정성 + 성능 개선

**Day 8-9: 로깅 개선**
- [ ] CloudWatch Alarms 설정
- [ ] 구조화된 로깅 강화 (JSON)
- [ ] 에러 로그 대시보드 (CloudWatch Insights)

**Day 10-11: 성능 최적화**
- [ ] DB 인덱스 최적화
- [ ] 크롤러 타임아웃 재조정
- [ ] WebSocket 연결 풀 최적화

**Day 12-13: 부하 테스트**
- [ ] Locust 부하 테스트 (동시 100명)
- [ ] 병목 지점 파악
- [ ] 성능 개선

**Day 14: 문서화**
- [ ] API 문서 (Swagger/OpenAPI)
- [ ] 트러블슈팅 가이드

---

### Week 3: 배포 자동화

**Day 15-16: CI/CD 파이프라인**
- [ ] GitHub Actions 설정
- [ ] 유닛 테스트 작성 (pytest)
- [ ] 자동 배포 스크립트
- [ ] 롤백 스크립트

**Day 17-18: 무중단 배포**
- [ ] Docker Compose Rolling Update 설정
- [ ] Nginx Load Balancing
- [ ] Health Check 엔드포인트 강화

**Day 19-20: 통합 테스트**
- [ ] CI/CD 파이프라인 테스트
- [ ] 무중단 배포 검증
- [ ] 롤백 시나리오 테스트

**Day 21: 최종 점검**
- [ ] 전체 플로우 검증
- [ ] 운영 매뉴얼 업데이트

---

### Week 4+: 확장 준비 (트래픽 증가 시)

**Day 22-25: PostgreSQL 마이그레이션**
- [ ] RDS PostgreSQL 인스턴스 생성
- [ ] Alembic 마이그레이션 스크립트
- [ ] 데이터 마이그레이션 (SQLite → PostgreSQL)
- [ ] 성능 비교 테스트

**Day 26-28: Redis 캐싱**
- [ ] Redis 설치 (EC2 Docker 또는 ElastiCache, 옵션 A 권장)
- [ ] 캐싱 레이어 구현
- [ ] 성능 측정 (Before/After)

---

## 📊 비용 예측 (월별)

### 초기 단계 (0-500명)

| 항목 | 비용 | 비고 |
|------|------|------|
| EC2 t2.micro | 무료 | 프리티어 12개월 |
| EBS 30GB | 무료 | 프리티어 12개월 |
| S3 백업 (5GB) | 무료 | 프리티어 12개월 |
| CloudWatch Logs (5GB) | 무료 | 프리티어 영구 |
| 도메인 (Cloudflare) | $0.83/월 | $10/년 |
| Let's Encrypt | 무료 | 영구 무료 |
| **합계** | **$0.83/월** | 프리티어 |

### 성장 단계 (500-5000명)

| 항목 | 비용 | 비고 |
|------|------|------|
| EC2 t3.small × 2 | $30/월 | $15/월 × 2 |
| RDS PostgreSQL (db.t3.micro) | $15/월 | 20GB 포함 |
| ElastiCache Redis | $11/월 | cache.t3.micro |
| ALB | $16/월 | |
| S3 백업 (20GB) | $0.46/월 | $0.023/GB |
| CloudWatch (추가 로그) | $2/월 | 10GB |
| 도메인 | $0.83/월 | |
| **합계** | **$75.29/월** | |

---

## 🚀 즉시 시작 가능한 작업 (오늘 1시간)

**가장 중요한 3가지** (우선순위):

### 1. HTTPS 설정 (10분)
```bash
# EC2에서 실행
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com

# 테스트
curl https://your-domain.com/health
```

### 2. UptimeRobot + Sentry (15분)
```bash
# Sentry 설치
pip install sentry-sdk[fastapi]

# app/main.py에 3줄 추가
import sentry_sdk
sentry_sdk.init(dsn="your-dsn")
```

### 3. S3 백업 자동화 (20분)
```bash
# backup.sh 작성
nano /home/ubuntu/backup.sh

# cron 등록
crontab -e
# 0 3 * * * /home/ubuntu/backup.sh
```

---

## ❓ HTTPS 필요성 FAQ

### Q1: API만 사용하는데 HTTPS가 꼭 필요한가요?

**A**: 네이티브 앱 (iOS/Android) 사용 시 **사실상 필수**입니다.

| 플랫폼 | HTTP 허용 여부 | 조건 |
|--------|---------------|------|
| **iOS** | ⚠️ 조건부 | Info.plist에 ATS 예외 설정 필요, 앱스토어 심사 시 정당한 사유 요구 |
| **Android** | ✅ 가능 | `usesCleartextTraffic=true` 설정, 구글 플레이에서 보안 경고 표시 |
| **WebSocket** | ❌ 불가 | iOS ATS가 `ws://` 차단, `wss://` 필수 |

### Q2: 개발 중인데 지금 꼭 해야 하나요?

**A**: 출시 전까지는 선택사항입니다.

**개발/테스트 단계**:
- ✅ localhost는 ATS 예외 → HTTP 가능
- ✅ TestFlight/내부 배포 → HTTP 우회 가능

**상용 출시 시**:
- 🚨 앱스토어/구글 플레이 → HTTPS 사실상 필수
- 🚨 관리자 페이지 → 비밀번호 평문 노출 위험

### Q3: HTTP의 실제 위험은?

**시나리오**: 공용 WiFi에서 앱 사용
```
[사용자 앱] --HTTP--> [공격자 노트북] --HTTP--> [API 서버]
                            ↑
                    패킷 스니핑으로 데이터 조작
```

**실제 피해**:
1. **환율 데이터 조작**: 1,300원 → 900원 (금융 손실)
2. **관리자 비밀번호 탈취**: Basic Auth가 Base64 평문
3. **세션 하이재킹**: 사용자 쿠키 탈취

### Q4: Let's Encrypt는 정말 무료인가요?

**A**: 네, 영구 무료입니다.

| 항목 | Let's Encrypt | 유료 인증서 |
|------|--------------|-----------|
| 비용 | 무료 | $50-300/년 |
| 자동 갱신 | ✅ | 수동 |
| 신뢰도 | A+ | A+ (동일) |
| 와일드카드 | ✅ | ✅ |
| 기술 지원 | 커뮤니티 | 24/7 |

**유료가 필요한 경우**:
- Extended Validation (EV) 인증서 (주소창 녹색)
- 기업 로고 표시
- 법적 보증 필요

→ **스타트업은 Let's Encrypt면 충분**

---

## 🔗 참고 자료

### 공식 문서
- [Let's Encrypt 가이드](https://letsencrypt.org/getting-started/)
- [AWS 프리티어](https://aws.amazon.com/free/)
- [FastAPI 배포](https://fastapi.tiangolo.com/deployment/)

### 관련 프로젝트 문서
- [DEPLOYMENT.md](DEPLOYMENT.md) - EC2 배포 가이드
- [DOCKER.md](DOCKER.md) - Docker 아키텍처
- [DECISIONS.md](DECISIONS.md) - 아키텍처 결정 기록

### 모니터링 도구
- [UptimeRobot](https://uptimerobot.com)
- [Sentry](https://sentry.io)
- [CloudWatch](https://aws.amazon.com/cloudwatch/)

---

**마지막 업데이트**: 2025-11-15
**작성자**: Claude Code
**다음 리뷰**: 출시 1주 전
