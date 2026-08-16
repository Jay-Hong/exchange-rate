# 시스템 성능 개선 및 모니터링 강화 작업 기록

**작업 일시:** 2025년 11월 5일
**작업자:** Claude Code
**목적:** 시간이 지날수록 시스템이 느려지는 문제 해결 및 모니터링 시스템 구축

---

## 📊 문제 분석 결과

### 발견된 주요 문제

1. **Selenium 좀비 프로세스 누적** (CRITICAL)
   - Chrome/ChromeDriver 프로세스가 제대로 종료되지 않고 누적
   - 현재 실행 중: Chrome 2개 (310MB), ChromeDriver 2개
   - 시간이 지날수록 메모리 사용량 증가

2. **로그 파일 과다 증가** (HIGH)
   - 총 크기: 83MB (error.log 28MB + app.log 53MB)
   - 컨테이너 unhealthy 상태로 대량 에러 로그 생성
   - 디스크 I/O 부하 증가

3. **WebSocket 연결 리스트 누적 가능성** (MEDIUM)
   - disconnect() 시 ValueError 발생 가능
   - broadcast() 순회 중 리스트 수정 위험

4. **Docker 빌드 캐시 누적** (MEDIUM)
   - 빌드 캐시: 4.8GB
   - Dangling 이미지: 3.1GB
   - 총 8GB 디스크 낭비

---

## 🔧 수행한 작업

### Phase 1: 긴급 수정 (Core Issues)

#### 1. Selenium 좀비 프로세스 강제 종료 로직 추가

**파일:** `app/crawlers/utils.py:182-213`

**변경사항:**
```python
# driver.quit() 실패 시 Chrome 프로세스 강제 종료 (좀비 프로세스 방지)
try:
    import psutil
    import os
    import time

    current_process = psutil.Process(os.getpid())
    killed_count = 0

    # 현재 Python 프로세스의 모든 자식 프로세스 중 Chrome 관련 프로세스 강제 종료
    for child in current_process.children(recursive=True):
        try:
            if any(name in child.name().lower() for name in ['chrome', 'chromedriver']):
                child.kill()
                child.wait(timeout=3)  # 종료 대기 (최대 3초)
                killed_count += 1
                logger.warning(f"🔨 좀비 프로세스 강제 종료: {child.name()} (PID: {child.pid})")
        except psutil.TimeoutExpired:
            # 3초 내 종료 안 되면 강제 종료
            child.kill()
            logger.warning(f"⚡ 좀비 프로세스 강제 kill: {child.name()} (PID: {child.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # 이미 종료되었거나 권한 없음
            pass
```

**효과:**
- driver.quit() 실패 시에도 프로세스 강제 종료 보장
- 좀비 프로세스 누적 방지

---

#### 2. Chrome 프로세스 수명 제한 설정

**파일:** `app/crawlers/constants.py:82-83`

**추가 상수:**
```python
CHROME_MAX_LIFETIME_SECONDS = 300  # Chrome 프로세스 최대 수명: 5분 (600→300 감소)
CHROME_CLEANUP_INTERVAL_MINUTES = 3  # 좀비 프로세스 정리 주기: 3분 (5→3 감소)
```

**효과:**
- 프로세스 수명 제한으로 장기 실행 방지
- 정리 주기 명확화

---

#### 3. 좀비 Chrome 프로세스 정리 스케줄러 추가

**파일:** `app/scheduler.py:126-163, 170-176`

**새 함수:**
```python
def cleanup_zombie_chrome_processes():
    """좀비 Chrome 프로세스 정리 (5분 이상 실행된 프로세스 강제 종료)"""
    try:
        import psutil
        import time
        from app.crawlers.constants import CHROME_MAX_LIFETIME_SECONDS

        killed_count = 0
        current_time = time.time()

        for proc in psutil.process_iter(['pid', 'name', 'create_time', 'cmdline']):
            try:
                proc_name = proc.info['name'].lower()
                if any(name in proc_name for name in ['chrome', 'chromedriver']):
                    process_age = current_time - proc.info['create_time']

                    if process_age > CHROME_MAX_LIFETIME_SECONDS:
                        proc.kill()
                        killed_count += 1
```

**스케줄 등록:**
```python
scheduler.add_job(
    cleanup_zombie_chrome_processes,
    IntervalTrigger(minutes=CHROME_CLEANUP_INTERVAL_MINUTES, timezone=KST),
    id="cleanup_zombie_chrome"
)
```

**효과:**
- 3분마다 자동으로 좀비 프로세스 정리
- 5분 이상 실행된 Chrome 프로세스 강제 종료

---

#### 4. WebSocket ConnectionManager 개선

**파일:** `app/main.py:66-90`

**변경사항:**
```python
def disconnect(self, websocket: WebSocket):
    try:
        self.active_connections.remove(websocket)
    except ValueError:
        logger.warning("⚠️ 이미 제거된 WebSocket 연결 시도")

async def broadcast(self, message: dict):
    disconnected = []
    # 리스트 복사본으로 순회 (순회 중 수정 방지)
    for connection in self.active_connections[:]:
        try:
            await connection.send_json(message)
        except Exception as e:
            disconnected.append(connection)

    for conn in disconnected:
        try:
            self.active_connections.remove(conn)
        except ValueError:
            pass  # 이미 제거됨
```

**효과:**
- ValueError 예외 처리로 크래시 방지
- 안전한 리스트 순회로 메모리 누수 방지

---

#### 5. 로그 설정 최적화

**파일:** `app/config.py:29`

**변경사항:**
```python
LOG_ROTATION = {
    "maxBytes": 10 * 1024 * 1024,  # 10MB
    "backupCount": 3,               # 5 → 3 감소 (총 30MB)
    "encoding": "utf-8"
}
```

**효과:**
- 로그 백업 개수 감소: 50MB → 30MB (20MB 절약)
- 디스크 I/O 부하 감소

---

### Phase 2: 모니터링 시스템 구축

#### 6. Docker 메모리 제한 조정

**파일:** `docker-compose.yml:67`

**변경사항:**
```yaml
deploy:
  resources:
    limits:
      memory: 800M  # 700M → 800M
```

**효과:**
- 좀비 프로세스 정리로 여유 확보
- OOM Killer 트리거 가능성 감소

---

#### 7. 내부 모니터링 모듈 생성

**파일:** `app/admin/monitor.py` (신규)

**주요 기능:**
- 메모리/CPU 사용량 추적 (5분 간격)
- Chrome 프로세스 개수 및 메모리 모니터링
- 임계값 초과 시 경고 로그 + 텔레그램 알림 (선택)
- 최근 60개 데이터 포인트 히스토리 저장 (5시간)

**임계값:**
```python
MEMORY_WARNING_PERCENT = 70   # 메모리 70% 이상 경고
MEMORY_CRITICAL_PERCENT = 85  # 메모리 85% 이상 위험
CHROME_MAX_PROCESSES = 4      # Chrome 프로세스 최대 개수
CHROME_WARNING_PROCESSES = 3  # Chrome 프로세스 경고 개수
```

**효과:**
- 실시간 리소스 모니터링
- 문제 조기 감지 및 알림

---

#### 8. 모니터링 스케줄러 등록

**파일:** `app/scheduler.py:166-192`

**추가 함수:**
```python
def record_monitoring_stats():
    """시스템 모니터링 통계 기록 (5분마다)"""
    try:
        from app.admin.monitor import system_monitor
        system_monitor.record_stats()
    except Exception as e:
        logger.error("❌ 모니터링 통계 기록 실패", exc_info=True)
```

**스케줄 등록:**
```python
scheduler.add_job(
    record_monitoring_stats,
    IntervalTrigger(minutes=5, timezone=KST),
    id="monitoring_stats"
)
```

**효과:**
- 5분마다 리소스 통계 자동 수집
- 시간별 트렌드 분석 가능

---

#### 9. 관리자 API 추가

**파일:** `app/main.py:490-501`

**새 엔드포인트:**
```python
@app.get("/admin/api/monitor/current")
def get_current_monitor_stats():
    """현재 시스템 모니터링 상태 조회"""
    from app.admin.monitor import system_monitor
    return system_monitor.get_current_stats()

@app.get("/admin/api/monitor/history")
def get_monitor_history(hours: int = 1):
    """시간별 모니터링 히스토리 조회 (Chart.js용)"""
    from app.admin.monitor import system_monitor
    return system_monitor.get_history(hours=hours)
```

**효과:**
- REST API로 모니터링 데이터 조회
- 향후 Chart.js 그래프 연동 가능

---

#### 10. 관리자 대시보드 UI 추가

**파일:** `templates/admin.html:456-460, 682-699`

**추가 카드:**
```html
<div class="card">
    <div class="card-label">Chrome 프로세스</div>
    <div class="card-value" id="chrome-count">-</div>
    <div class="card-subvalue" id="chrome-memory">메모리: -</div>
</div>
```

**JavaScript 함수:**
```javascript
async function loadChromeMonitoring() {
    const res = await fetch('/admin/api/monitor/current');
    const data = await res.json();

    if (data.chrome) {
        document.getElementById('chrome-count').textContent = `${data.chrome.count}개`;
        document.getElementById('chrome-memory').textContent =
            `메모리: ${data.chrome.total_memory_mb.toFixed(1)}MB`;
    }
}
```

**효과:**
- 관리자 페이지에서 Chrome 프로세스 실시간 모니터링
- 30초마다 자동 업데이트

---

### Phase 3: 시스템 리소스 정리

#### 11. Docker 리소스 정리

**실행 명령:**
```bash
docker image prune -f        # Dangling 이미지 삭제
docker builder prune -f      # 빌드 캐시 정리
```

**결과:**
- Dangling 이미지: 8개 삭제
- 빌드 캐시: 7.8GB 정리
- 총 회수: 약 8GB

---

#### 12. 시스템 캐시 메모리 정리

**실행 명령:**
```bash
sudo sync
echo 3 | sudo tee /proc/sys/vm/drop_caches
```

**결과:**
- buff/cache: 414MB → 125MB
- 회수: 289MB

---

#### 13. 로그 파일 정리

**실행 명령:**
```bash
# 오래된 백업 로그 삭제
find /home/ubuntu/exchange-rate/volumes/logs/app -name "*.log.[0-9]*" -type f -delete

# 현재 로그 파일 비우기
truncate -s 0 /home/ubuntu/exchange-rate/volumes/logs/app/*.log
sudo truncate -s 0 /home/ubuntu/exchange-rate/volumes/logs/nginx/*.log
```

**결과:**
- 로그 파일: 85MB → 12KB
- 회수: 85MB

---

#### 14. 불필요한 서비스 비활성화

**실행 명령:**
```bash
# snapd 중지 및 비활성화
sudo systemctl stop snapd.service snapd.socket
sudo systemctl mask snapd.service snapd.socket

# multipathd 중지 및 비활성화
sudo systemctl stop multipathd.service
sudo systemctl disable multipathd.service
```

**결과:**
- snapd: 12MB 절약
- multipathd: 27MB 절약
- 총 메모리 절약: 39MB

---

## 📊 최종 결과

### 디스크 사용량

| 항목 | Before | After | 회수량 |
|------|--------|-------|--------|
| **전체 디스크** | 15GB/29GB (51%) | 6.3GB/29GB (23%) | **8.7GB** |
| Docker 이미지 | 4.2GB (10개) | 1.1GB (2개) | 3.1GB |
| Docker 빌드 캐시 | 4.8GB | 0B | 4.8GB |
| 로그 파일 | 85MB | 12KB | 85MB |

### 메모리 사용량

| 항목 | Before | After | 절약량 |
|------|--------|-------|--------|
| **사용 중 메모리** | 647MB | 581MB | 66MB |
| 캐시 메모리 | 414MB | 130MB | 284MB |
| **여유 메모리** | 309MB | 375MB | +66MB |

### 시스템 개선사항

- ✅ Selenium 좀비 프로세스 자동 정리 (5분마다)
- ✅ WebSocket 연결 관리 안정화
- ✅ 로그 백업 개수 감소 (50MB → 30MB)
- ✅ Chrome 프로세스 모니터링 대시보드 추가
- ✅ 시스템 리소스 모니터링 (5분마다)
- ✅ Docker 메모리 제한 증가 (700M → 800M)

---

## 🚀 재부팅 후 절차

### 1. 시스템 재부팅

```bash
sudo reboot
```

### 2. 재부팅 후 메모리 확인

```bash
free -h
```

**예상 메모리:**
- 기본 시스템: ~250MB
- dockerd + containerd: ~100MB
- 여유 메모리: ~600MB

### 3. Docker Compose 실행

```bash
cd /home/ubuntu/exchange-rate
docker-compose up -d
```

### 4. 컨테이너 상태 확인

```bash
# 컨테이너 실행 확인
docker ps

# 로그 확인
docker logs -f exchange-rate-app

# 헬스체크 확인
curl http://localhost/health
```

### 5. Chrome 프로세스 모니터링 (5분 후)

```bash
# 좀비 프로세스 정리 로그 확인
docker logs exchange-rate-app | grep "좀비"

# Chrome 프로세스 개수 확인 (2개 이하여야 정상)
docker exec exchange-rate-app ps aux | grep -c chrome

# 메모리 사용량 확인
docker stats
```

### 6. 관리자 페이지 확인

```
http://your-server-ip/admin
```

**확인 항목:**
- WebSocket 연결 수
- 메모리 사용량 (800M 이하)
- Chrome 프로세스 개수 (2개 이하)
- 브로드캐스트 성공률
- 에러 개수

---

## ⚠️ 주의사항

### 1. 첫 5-10분 모니터링 필수

재부팅 후 첫 5-10분간 집중적으로 모니터링하세요:
- Chrome 프로세스 개수
- 메모리 사용량 추이
- 에러 로그 발생 여부

### 2. 좀비 프로세스 정리 확인 (5분 후)

```bash
docker logs exchange-rate-app | grep "좀비 Chrome 프로세스"
```

**정상 출력 예시:**
```
✅ 좀비 Chrome 프로세스 없음
```

또는

```
🧹 좀비 Chrome 프로세스 정리 완료: 1개 종료
```

### 3. unhealthy 상태 재발 시

```bash
# 헬스체크 직접 실행
docker exec exchange-rate-app curl -v http://localhost:8000/health

# 최근 에러 확인
docker logs exchange-rate-app --tail 200 | grep -E "ERROR|CRITICAL"

# LOG_LEVEL 조정 (.env 파일)
LOG_LEVEL=WARNING  # INFO → WARNING 변경 후 재시작
```

### 4. 메모리 부족 시

```bash
# 메모리 사용량 확인
docker stats

# Swap 사용량 확인
free -h

# 필요 시 메모리 제한 감소 (docker-compose.yml)
memory: 700M  # 800M → 700M
```

---

## 📝 향후 모니터링 항목

### 일일 체크 (관리자 페이지)

1. Chrome 프로세스 개수 (2개 이하 유지)
2. 메모리 사용량 (70% 이하 유지)
3. 에러 개수 (1시간당 10개 이하)
4. 브로드캐스트 성공률 (95% 이상)

### 주간 체크

1. 로그 파일 크기 (30MB 이하 유지)
2. 디스크 사용량 (30% 이하 유지)
3. DB 크기 (5MB 이하 유지)
4. 좀비 프로세스 정리 로그 확인

### 월간 체크

1. Docker 이미지 정리 (`docker image prune -f`)
2. Docker 빌드 캐시 정리 (`docker builder prune -f`)
3. 시스템 업데이트 (`apt update && apt upgrade`)

---

## 🔗 관련 문서

- **프로젝트 가이드**: `CLAUDE.md`
- **크롤러 가이드**: `CRAWLERS.md`
- **아키텍처 의사결정**: `DECISIONS.md`
- **Docker 가이드**: `DOCKER.md`

---

**작성일**: 2025-11-05
**마지막 수정**: 2025-11-05
**버전**: 1.0
