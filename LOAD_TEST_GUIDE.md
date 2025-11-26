# Phase 1.7 부하 테스트 가이드

> 📅 작성일: 2025-11-27
> 🎯 목적: 동시 접속 50명 WebSocket 부하 테스트로 Redis 캐시 효과 검증

---

## 📋 테스트 목표

1. **Redis 캐시 효과 측정** - WebSocket 초기 접속 시간 (DB vs Redis)
2. **Broadcasting 안정성 검증** - 수신 누락 여부, 메시지 지연 시간
3. **서버 자원 사용률 확인** - CPU, 메모리, Redis 메모리 변화
4. **연결 안정성 확인** - 재연결 횟수, 에러 발생 여부

---

## 🛠️ 사전 준비

### 1. 필수 패키지 설치

**로컬 머신 (테스트 클라이언트):**
```bash
# websockets 라이브러리 설치
pip install websockets

# load_test.py 실행 권한
chmod +x load_test.py
```

**AWS 서버 (모니터링):**
```bash
# monitor_server.sh 실행 권한
chmod +x monitor_server.sh

# Redis 비밀번호 환경 변수 설정 (선택)
export REDIS_PASSWORD=$(grep REDIS_PASSWORD .env | cut -d= -f2)
```

### 2. 서버 상태 확인

```bash
# SSH 접속
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32

# 컨테이너 상태
cd ~/exchange-rate
docker ps | grep exchange-rate

# Redis 상태
docker exec exchange-rate-redis redis-cli -a RH7535753p* ping 2>/dev/null
# 예상 출력: PONG
```

---

## 🚀 테스트 실행

### Scenario 1: 기본 부하 테스트 (3분, 50명)

**목적:** 전반적인 성능 확인

#### Step 1: 서버 모니터링 시작 (AWS 서버)

```bash
# SSH 접속 (터미널 1)
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32
cd ~/exchange-rate

# 모니터링 시작 (3분)
./monitor_server.sh 180
```

#### Step 2: 부하 테스트 실행 (로컬 머신)

**새 터미널 열기 (터미널 2):**
```bash
cd /Users/jay/Downloads/Projects/FXi/exchange-rate

# 부하 테스트 실행
python load_test.py \
    --url wss://fxi.n-e.kr/ws \
    --clients 50 \
    --duration 180
```

#### Step 3: 실시간 모니터링 (선택)

**새 터미널 열기 (터미널 3):**
```bash
# SSH 접속
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32

# 실시간 Docker stats
docker stats --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

# 또는 htop (Ctrl+C로 종료)
htop
```

#### Step 4: 결과 확인

**클라이언트 결과 (터미널 2):**
```
📊 부하 테스트 결과
============================================================

1️⃣  초기 접속 시간 (Redis 캐시 효과)
   평균: XX.Xms
   첫 번째 접속: XX.Xms (DB 폴백 예상)
   2-50번째 평균: XX.Xms (Redis 캐시 예상)
   ✅ Redis 캐시 효과: XX.Xms 빠름

2️⃣  메시지 지연 시간 (서버 전송 → 클라이언트 수신)
   평균: XX.Xms
   중앙값: XX.Xms

3️⃣  Broadcasting 수신 (예상: 18회)
   총 수신: XXX개
   평균: XX.X개/클라이언트
   ✅ 누락 없음: 모든 클라이언트 18개 수신

4️⃣  재연결 (연결 안정성)
   ✅ 재연결 없음: 모든 연결 안정적

5️⃣  에러: 없음 ✅
```

**서버 결과 (터미널 1):**
```
📁 결과 파일:
   - docker_stats_start_YYYYMMDD_HHMMSS.txt
   - docker_stats_end_YYYYMMDD_HHMMSS.txt
   - redis_start_YYYYMMDD_HHMMSS.txt
   - redis_end_YYYYMMDD_HHMMSS.txt
   - continuous_YYYYMMDD_HHMMSS.txt
   - broadcast_YYYYMMDD_HHMMSS.log

📋 간단 요약:
Docker Stats 변화:
  시작:
    exchange-rate-app: CPU=X.XX% MEM=XX.X%
    exchange-rate-redis: CPU=X.XX% MEM=XX.X%
  종료:
    exchange-rate-app: CPU=X.XX% MEM=XX.X%
    exchange-rate-redis: CPU=X.XX% MEM=XX.X%

Redis 메모리 변화:
  시작: 1.XXM
  종료: 1.XXM

Broadcasting 통계 (180초 = 예상 18회):
  총 로그: XX줄
  변경사항 없음: XX회
  Redis 업데이트만: XX회
  브로드캐스트 완료: XX회
```

---

### Scenario 2: 연결/재연결 스트레스 테스트 (15분, 5회 반복)

**목적:** 메모리 릭 확인, 연결 풀 안정성

```bash
# 서버 모니터링 (15분)
./monitor_server.sh 900 &

# 테스트 반복 실행 (5회)
for i in {1..5}; do
    echo "=== Round $i/5 ==="
    python load_test.py --url wss://fxi.n-e.kr/ws --clients 50 --duration 30
    echo "10초 대기..."
    sleep 10
done
```

**확인 사항:**
- 메모리 증가 추세 (없어야 함)
- Redis 메모리 1.13MB 유지
- 재연결 횟수 (0이어야 함)

---

### Scenario 3: 극한 부하 테스트 (100명, 선택)

**⚠️ 주의:** t2.micro 인스턴스 부하 높음, 신중히 실행

```bash
# 서버 모니터링
./monitor_server.sh 180 &

# 100명 접속 (단계적)
python load_test.py --url wss://fxi.n-e.kr/ws --clients 100 --duration 180
```

**예상 결과:**
- CPU 사용률 증가 (70-90%)
- 일부 재연결 발생 가능
- Redis 메모리는 변화 없음 (1.13MB)

---

## 📊 결과 분석

### 1. Redis 캐시 효과 확인

**기준값 (REDIS_STRATEGY.md):**
- DB 폴백: 10-20ms
- Redis 캐시: 0.5-1.5ms
- 예상 개선: 7-40배

**실측값 확인:**
```
첫 번째 접속 (DB): XX.Xms
2-50번째 평균 (Redis): XX.Xms
개선: XX.X배 빠름
```

### 2. Broadcasting 안정성

**기준:**
- 예상 수신: `duration / 10` 회
- 누락: 0개
- 지연: 100ms 이내

**실측값:**
```
예상: 18회 (180초 / 10)
실제: XX회
누락: X개
평균 지연: XX.Xms
```

### 3. 서버 자원

**기준값 (t2.micro):**
- CPU: 30-50% (정상), 70-90% (경고)
- 메모리: 40-60% (정상)
- Redis 메모리: 1.13MB 유지

**실측값:**
```
Docker Stats:
  exchange-rate-app: CPU=XX.X%, MEM=XX.X%
  exchange-rate-redis: CPU=XX.X%, MEM=XX.X%

Redis:
  시작: 1.XXM
  종료: 1.XXM (증가 없음 ✅)
```

### 4. 변경 감지 로그 패턴

**기준:**
- 환율 변경 없을 때: "⏸️ 변경사항 없음"
- 변경 있고 접속자 없음: "🅾️ Redis 업데이트만"
- 변경 있고 접속자 있음: "📡 브로드캐스트 완료"

**실측값:**
```
변경사항 없음: XX회 (XX%)
Redis 업데이트만: XX회 (XX%)
브로드캐스트 완료: XX회 (XX%)
```

---

## ✅ 성공 기준

### 필수 (Must-Have)

| 항목 | 기준 | Pass/Fail |
|------|------|-----------|
| 초기 접속 시간 (Redis) | 5ms 이하 | [ ] |
| Broadcasting 수신 누락 | 0개 | [ ] |
| 재연결 | 0회 (안정적 연결) | [ ] |
| Redis 메모리 증가 | 없음 (1.13MB 유지) | [ ] |
| 치명적 에러 | 없음 | [ ] |

### 권장 (Should-Have)

| 항목 | 기준 | Pass/Fail |
|------|------|-----------|
| 메시지 지연 시간 | 100ms 이하 | [ ] |
| CPU 사용률 (50명) | 50% 이하 | [ ] |
| 변경 감지 정확성 | 90% 이상 | [ ] |

---

## 🐛 문제 해결

### 문제 1: "Connection refused"

**원인:** WebSocket 경로 또는 서버 미실행

**해결:**
```bash
# 서버 상태 확인
docker ps | grep exchange-rate-app

# 로그 확인
docker compose logs --tail 50 fastapi

# 재시작
docker compose restart fastapi
```

### 문제 2: 초기 접속 시간 매우 느림 (> 100ms)

**원인:** Redis 미작동 또는 Circuit Breaker 열림

**해결:**
```bash
# Redis 상태 확인
docker exec exchange-rate-redis redis-cli -a RH7535753p* ping 2>/dev/null

# Circuit Breaker 확인
docker compose logs fastapi | grep "회로차단기"

# Redis 재시작
docker compose restart redis
```

### 문제 3: 수신 누락 발생

**원인:** 서버 과부하 또는 네트워크 불안정

**해결:**
```bash
# CPU/메모리 확인
docker stats

# Broadcasting 로그 확인
docker compose logs fastapi | grep "브로드캐스트"

# 클라이언트 수 줄이기
python load_test.py --clients 20 --duration 60
```

### 문제 4: Redis 메모리 증가

**원인:** 메모리 릭 또는 캐시 정책 오류

**해결:**
```bash
# Redis 키 확인
docker exec exchange-rate-redis redis-cli -a RH7535753p* KEYS '*' 2>/dev/null

# 키 개수 (2개여야 함)
docker exec exchange-rate-redis redis-cli -a RH7535753p* DBSIZE 2>/dev/null

# maxmemory 확인
docker exec exchange-rate-redis redis-cli -a RH7535753p* INFO memory 2>/dev/null | grep maxmemory
```

---

## 📝 테스트 리포트 템플릿

```markdown
# Phase 1.7 부하 테스트 리포트

**테스트 일시:** YYYY-MM-DD HH:MM:SS
**테스트 환경:**
- 서버: AWS EC2 t2.micro (1 vCPU, 1GB RAM)
- 클라이언트: 50명 동시 접속
- 테스트 시간: 3분 (180초)

## 1. Redis 캐시 효과

| 항목 | 측정값 | 기준값 | Pass |
|------|--------|--------|------|
| 첫 번째 접속 (DB) | XX.Xms | 10-20ms | ✅ |
| 2-50번째 평균 (Redis) | XX.Xms | 0.5-1.5ms | ✅ |
| 개선 효과 | XX.X배 | 7-40배 | ✅ |

## 2. Broadcasting 안정성

| 항목 | 측정값 | 기준값 | Pass |
|------|--------|--------|------|
| 예상 수신 | 18회 | 18회 | ✅ |
| 실제 수신 (평균) | XX.X회 | 18회 | ✅ |
| 누락 | X개 | 0개 | ✅ |
| 평균 지연 | XX.Xms | < 100ms | ✅ |

## 3. 서버 자원

| 항목 | 시작 | 종료 | 증가 | Pass |
|------|------|------|------|------|
| CPU (FastAPI) | XX.X% | XX.X% | XX.X% | ✅ |
| 메모리 (FastAPI) | XX.X% | XX.X% | XX.X% | ✅ |
| Redis 메모리 | 1.XXM | 1.XXM | 0M | ✅ |

## 4. 연결 안정성

| 항목 | 측정값 | 기준값 | Pass |
|------|--------|--------|------|
| 총 접속 | 50명 | 50명 | ✅ |
| 재연결 | X회 | 0회 | ✅ |
| 에러 | X개 | 0개 | ✅ |

## 5. 결론

- [ ] 모든 필수 기준 통과
- [ ] 권장 기준 통과
- [ ] 개선 필요 사항: (있다면 기술)

## 6. 첨부 파일

- load_test_results/docker_stats_start_YYYYMMDD_HHMMSS.txt
- load_test_results/redis_start_YYYYMMDD_HHMMSS.txt
- (기타 모니터링 파일)
```

---

**작성:** Claude Code 🤖
**업데이트:** 필요 시 테스트 결과 반영
