# 재부팅 후 체크리스트

**목적**: 시스템 재부팅 후 Docker 서비스를 안전하게 시작하고 검증

---

## ✅ 1단계: 시스템 상태 확인

### 메모리 확인
```bash
free -h
```

**예상 결과:**
```
               total        used        free      shared  buff/cache   available
Mem:           957Mi       ~300Mi      ~500Mi        0Ki       ~150Mi       ~600Mi
Swap:          2.0Gi         0Mi       2.0Gi
```

**확인 사항:**
- ✅ 사용 중 메모리: 350MB 이하
- ✅ 여유 메모리: 500MB 이상
- ✅ Swap 사용: 0MB (깨끗한 시작)

---

### 디스크 확인
```bash
df -h /
```

**예상 결과:**
```
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        29G  6.3G   22G  23% /
```

**확인 사항:**
- ✅ 디스크 사용량: 30% 이하
- ✅ 여유 공간: 20GB 이상

---

### Chrome 프로세스 확인
```bash
ps aux | grep -E "chrome|chromedriver" | grep -v grep
```

**예상 결과:**
```
(출력 없음)
```

**확인 사항:**
- ✅ Chrome 프로세스 없음 (깨끗한 시작)

---

### 비활성화된 서비스 확인
```bash
systemctl is-enabled snapd
systemctl is-enabled multipathd
```

**예상 결과:**
```
masked
disabled
```

**확인 사항:**
- ✅ snapd: masked
- ✅ multipathd: disabled

---

## ✅ 2단계: Docker 시작

### Docker 이미지 확인
```bash
docker images
```

**예상 결과:**
```
REPOSITORY              TAG       IMAGE ID       CREATED          SIZE
exchange-rate-fastapi   latest    9a3bd66f893c   XX minutes ago   1.07GB
exchange-rate-nginx     latest    8074e97665bb   XX minutes ago   53MB
```

**확인 사항:**
- ✅ fastapi 이미지 있음
- ✅ nginx 이미지 있음

---

### Docker Compose 실행
```bash
cd /home/ubuntu/exchange-rate
docker-compose up -d
```

**예상 출력:**
```
Creating network "exchange-rate_default" with the default driver
Creating exchange-rate-app   ... done
Creating exchange-rate-nginx ... done
```

---

### 컨테이너 상태 확인
```bash
docker ps
```

**예상 결과:**
```
CONTAINER ID   IMAGE                     STATUS                    PORTS
xxxxx          exchange-rate-nginx       Up XX seconds (healthy)   0.0.0.0:80->80/tcp
xxxxx          exchange-rate-fastapi     Up XX seconds (healthy)   8000/tcp
```

**확인 사항:**
- ✅ 2개 컨테이너 실행 중
- ✅ STATUS: healthy (1-2분 후)

---

## ✅ 3단계: 서비스 검증 (2-3분 대기 후)

### 헬스체크 확인
```bash
curl http://localhost/health
```

**예상 결과:**
```json
{
  "status": "healthy",
  "message": "환율 서비스가 정상적으로 작동 중입니다.",
  "websocket_connections": 0
}
```

---

### 로그 확인
```bash
docker logs exchange-rate-app --tail 50
```

**확인 사항:**
- ✅ `🚀 FastAPI 서버 시작` 로그 확인
- ✅ `=== IN 모드로 전환됨 ===` 또는 `=== OUT 모드로 전환됨 ===`
- ❌ ERROR 로그 없음

---

### API 응답 확인
```bash
curl http://localhost/api/rates | jq '.metadata'
```

**예상 결과:**
```json
{
  "updated_at": "2025-11-05T...",
  "currencies": ["eur-krw", "jpy-krw", "usd-krw"],
  "banks": [...],
  "total_count": 30
}
```

**확인 사항:**
- ✅ HTTP 200 응답
- ✅ total_count: 20개 이상

---

## ✅ 4단계: Chrome 프로세스 모니터링 (5분 후)

### 좀비 프로세스 정리 로그 확인
```bash
docker logs exchange-rate-app | grep "좀비"
```

**정상 출력 예시:**
```
✅ 좀비 Chrome 프로세스 없음
```

또는

```
🧹 좀비 Chrome 프로세스 정리 완료: 1개 종료
```

---

### Chrome 프로세스 개수 확인
```bash
docker exec exchange-rate-app ps aux | grep chrome | grep -v grep
```

**예상 결과:**
```
(2줄 이하 출력, Chrome 2개 이하)
```

**확인 사항:**
- ✅ Chrome/ChromeDriver 총 4개 이하
- ✅ 각 프로세스 실행 시간 10분 이하

---

### 메모리 사용량 확인
```bash
docker stats --no-stream
```

**예상 결과:**
```
CONTAINER           MEM USAGE / LIMIT   MEM %
exchange-rate-app   400MiB / 800MiB     50%
exchange-rate-nginx 30MiB / 32MiB       93%
```

**확인 사항:**
- ✅ FastAPI: 600MB 이하 (초기 400MB 정상)
- ✅ Nginx: 32MB 이하

---

## ✅ 5단계: 관리자 페이지 확인

### 웹 브라우저 접속
```
http://your-server-ip/admin
```

**로그인:**
- Username: `admin`
- Password: `.env` 파일의 `ADMIN_PASSWORD`

---

### 대시보드 카드 확인

**확인 항목:**
1. **WebSocket 연결**: 0 (아직 연결 없음)
2. **메모리 사용**: ~400MB (60% 이하)
3. **Chrome 프로세스**: 2개 이하, 메모리 ~150MB
4. **브로드캐스트**: 성공률 95% 이상
5. **에러+경고 (1시간)**: 10개 이하

---

### 크롤러 상태 뱃지 확인

**확인 사항:**
- ✅ 10개 뱃지 표시 (investing, kb, hana, ...)
- ✅ 에러 개수 대부분 0 (일부 1-2개 허용)
- ⚠️ 특정 크롤러 에러 5개 이상: 클릭해서 로그 확인

---

## ✅ 6단계: 24시간 모니터링 계획

### 첫 1시간 (집중 모니터링)

**10분마다 확인:**
```bash
# 메모리 사용량
docker stats --no-stream

# Chrome 프로세스 개수
docker exec exchange-rate-app ps aux | grep -c chrome
```

**확인 사항:**
- 메모리 사용량이 계속 증가하지 않는지
- Chrome 프로세스가 누적되지 않는지

---

### 6시간 후 확인

```bash
# 로그 파일 크기
du -sh /home/ubuntu/exchange-rate/volumes/logs

# 에러 로그 확인
docker logs exchange-rate-app | grep ERROR | tail -20

# 좀비 프로세스 정리 횟수 (약 72회 실행되어야 함)
docker logs exchange-rate-app | grep "좀비 Chrome 프로세스" | wc -l
```

**확인 사항:**
- ✅ 로그 파일: 50MB 이하
- ✅ 좀비 프로세스 정리: 60회 이상 실행됨
- ❌ CRITICAL/ERROR 로그 반복 패턴 없음

---

### 24시간 후 확인

```bash
# 시스템 전체 메모리
free -h

# Docker 컨테이너 상태
docker ps

# 컨테이너 재시작 횟수 (0이어야 함)
docker ps --format "{{.Names}}\t{{.Status}}"
```

**확인 사항:**
- ✅ 컨테이너 STATUS: healthy
- ✅ 재시작 횟수: 0회
- ✅ 메모리 사용량: 초기 대비 +100MB 이하
- ✅ Swap 사용량: 300MB 이하

---

## ⚠️ 문제 발생 시 대응

### 1. 메모리가 계속 증가한다면

```bash
# Chrome 프로세스 강제 종료
docker exec exchange-rate-app pkill -9 -f chrome

# 좀비 프로세스 정리 로그 확인
docker logs exchange-rate-app | grep "좀비"

# 스케줄러 작동 확인
docker logs exchange-rate-app | grep "cleanup_zombie_chrome"
```

---

### 2. 컨테이너가 unhealthy 상태라면

```bash
# 헬스체크 직접 실행
docker exec exchange-rate-app curl -v http://localhost:8000/health

# 최근 에러 확인
docker logs exchange-rate-app --tail 200 | grep -E "ERROR|CRITICAL"

# 필요 시 LOG_LEVEL 조정 (.env 파일)
LOG_LEVEL=WARNING  # INFO → WARNING
docker-compose restart fastapi
```

---

### 3. 에러 로그가 계속 발생한다면

**특정 크롤러 에러:**
- 관리자 페이지에서 크롤러 뱃지 클릭
- 에러 로그 확인
- 해당 은행 웹사이트 구조 변경 가능성 → `CRAWLERS.md` 참고

**시스템 에러:**
```bash
# 전체 에러 로그 다운로드 (관리자 페이지)
http://your-server-ip/admin → 로그 다운로드 버튼

# 또는 직접 확인
docker logs exchange-rate-app --tail 500 > errors.log
```

---

### 4. Chrome 프로세스가 계속 증가한다면

```bash
# 즉시 강제 종료
docker exec exchange-rate-app pkill -9 -f chrome

# 코드 확인: driver.quit() 호출 확인
docker logs exchange-rate-app | grep "드라이버 종료"

# 필요 시 메모리 제한 감소
# docker-compose.yml 수정: memory: 700M
docker-compose down
docker-compose up -d
```

---

## 📊 정상 상태 기준

### 메모리
- **초기 (1시간)**: 400-500MB
- **안정화 (6시간)**: 500-600MB
- **장기 (24시간)**: 600-700MB
- **⚠️ 경고**: 700MB 이상
- **🚨 위험**: 750MB 이상

### Chrome 프로세스
- **정상**: 0-2개
- **⚠️ 경고**: 3-4개
- **🚨 위험**: 5개 이상

### 로그 파일
- **1시간**: 5-10MB
- **6시간**: 20-30MB
- **24시간**: 50-80MB
- **⚠️ 경고**: 100MB 이상

### 에러 개수 (1시간 기준)
- **정상**: 0-5개
- **⚠️ 경고**: 6-15개
- **🚨 위험**: 16개 이상

---

## 🎯 체크리스트 완료 확인

- [ ] 1단계: 시스템 상태 확인 완료
- [ ] 2단계: Docker 시작 완료
- [ ] 3단계: 서비스 검증 완료 (2-3분 대기)
- [ ] 4단계: Chrome 프로세스 모니터링 완료 (5분 대기)
- [ ] 5단계: 관리자 페이지 확인 완료
- [ ] 6단계: 24시간 모니터링 계획 수립

---

**작성일**: 2025-11-05
**참고 문서**: `MAINTENANCE_2025-11-05.md`
