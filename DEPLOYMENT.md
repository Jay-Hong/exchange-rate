# AWS EC2 배포 가이드

> 💡 **목적:** 테스트 서버에서 검증된 실전 배포 절차
> 🎯 **대상:** AWS t3.small (2 vCPU, 2GB RAM)
> ✅ **검증일:** 2026-01-13 (Ubuntu 24.04 LTS)

---

## 목차

1. [사전 준비](#1-사전-준비)
2. [EC2 인스턴스 생성](#2-ec2-인스턴스-생성)
3. [서버 초기 설정](#3-서버-초기-설정)
4. [배포 및 실행](#4-배포-및-실행)
5. [서비스 확인](#5-서비스-확인)
6. [트러블슈팅](#6-트러블슈팅)
7. [서비스 관리](#7-서비스-관리)
8. [코드 업데이트](#8-코드-업데이트)
9. [백업 및 복구](#9-백업-및-복구)

---

## 1. 사전 준비

### 1.1 필요한 것

- [ ] AWS 계정
- [ ] EC2 키 페어 생성 (`.pem` 파일)
- [ ] 프로젝트 코드 (GitHub 저장소 또는 로컬 파일)
- [ ] `.env` 파일 (환경 변수 설정)

### 1.2 로컬 환경 확인

```bash
# SSH 키 권한 설정 (Mac/Linux)
chmod 400 ~/path/to/your-key.pem
```

---

## 2. EC2 인스턴스 생성

### 2.1 인스턴스 설정

| 항목 | 설정 |
|------|------|
| **AMI** | Ubuntu 24.04 LTS (64-bit x86) |
| **인스턴스 타입** | t3.small (2 vCPU, 2GB RAM) |
| **스토리지** | 8-10GB gp3 (기본값) |
| **키 페어** | 기존 키 페어 선택 또는 새로 생성 |

### 2.2 보안 그룹 초기 설정

| 유형 | 프로토콜 | 포트 범위 | 소스 | 설명 |
|------|----------|----------|------|------|
| SSH | TCP | 22 | 내 IP | 관리자 접속 |

> ⚠️ **주의:** HTTP(80) 포트는 배포 완료 후 추가합니다.

---

## 3. 서버 초기 설정

### 3.1 SSH 접속

```bash
ssh -i ~/your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

### 3.2 시스템 업데이트 및 필수 패키지 설치

```bash
# 시스템 패키지 업데이트
sudo apt update && sudo apt upgrade -y

# 필수 패키지 설치
sudo apt install -y ca-certificates curl gnupg lsb-release git
```

### 3.3 Docker 설치

```bash
# Docker의 공식 GPG 키 추가
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Docker 저장소 추가
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 패키지 목록 업데이트
sudo apt update

# Docker Engine 설치
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Docker 서비스 시작 및 부팅 시 자동 시작 설정
sudo systemctl start docker
sudo systemctl enable docker

# 현재 사용자를 docker 그룹에 추가 (sudo 없이 docker 명령어 실행 가능)
sudo usermod -aG docker $USER

# Docker 설치 확인
sudo docker --version
sudo docker compose version
```

**예상 출력:**
```
Docker version 24.x.x
Docker Compose version v2.x.x
```

---

## 4. 배포 및 실행

### 4.1 프로젝트 파일 가져오기

**옵션 A: GitHub에서 클론**
```bash
cd ~
git clone https://github.com/your-username/exchange-rate.git
cd exchange-rate
```

**옵션 B: 로컬에서 전송 (scp)**
```bash
# 로컬 터미널에서 실행
cd /path/to/exchange-rate
scp -i ~/your-key.pem -r . ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate
```

### 4.2 환경 변수 설정

**옵션 A: 로컬에서 .env 파일 전송**
```bash
# 로컬 터미널에서 실행
scp -i ~/your-key.pem .env ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate/
```

**옵션 B: EC2에서 직접 생성**
```bash
cd ~/exchange-rate
nano .env
```

**운영 환경 권장 설정:**
```env
ENV=production                    # JSON 로그 출력
LOG_LEVEL=INFO                    # 적절한 로그 레벨
ADMIN_PASSWORD=강력한비밀번호123!   # 보안 강화
TELEGRAM_ENABLED=false            # Phase 2에서 활성화
```

### 4.3 ⚠️ 로그 디렉토리 권한 설정 (중요!)

**이 단계를 빠뜨리면 컨테이너 시작 실패합니다!**

```bash
cd ~/exchange-rate

# 필요한 디렉토리 생성
mkdir -p volumes/logs/app volumes/logs/nginx data

# 권한 설정 (1000:1000 = Dockerfile의 appuser UID:GID)
sudo chown -R 1000:1000 volumes/logs/app data
sudo chown -R 101:101 volumes/logs/nginx  # nginx user

# 권한 확인
ls -la volumes/logs/
```

**예상 출력:**
```
drwxr-xr-x 2 1000 1000 4096 Nov  2 13:00 app
drwxr-xr-x 2  101  101 4096 Nov  2 13:00 nginx
```

### 4.4 Docker 이미지 빌드

```bash
cd ~/exchange-rate

# 이미지 빌드 (첫 실행 시 5-10분 소요)
sudo docker compose build

# 빌드 완료 확인
sudo docker images
```

### 4.5 서비스 시작

```bash
# 백그라운드 실행
sudo docker compose up -d

# 컨테이너 상태 확인
sudo docker compose ps
```

**예상 출력:**
```
NAME                  STATUS
exchange-rate-app     Up (healthy)
exchange-rate-nginx   Up (healthy)
```

---

## 5. 서비스 확인

### 5.1 내부에서 확인 (EC2 터미널)

```bash
# 헬스체크
curl http://localhost:80/health

# 로그 확인
sudo docker compose logs -f fastapi

# 크롤러 동작 확인
sudo docker compose logs fastapi | grep -i "scheduler\|환율"
```

**예상 출력 (헬스체크):**
```json
{
  "status": "healthy",
  "message": "환율 서비스가 정상적으로 작동 중입니다.",
  "websocket_connections": 1
}
```

### 5.2 보안 그룹 포트 개방

**AWS Console에서:**
1. EC2 → 인스턴스 → 보안 탭 → 보안 그룹 클릭
2. 인바운드 규칙 → 인바운드 규칙 편집
3. 규칙 추가:
   - 유형: **HTTP**
   - 포트 범위: **80**
   - 소스: **0.0.0.0/0** (모든 IP) 또는 **내 IP** (본인만)
   - 설명: Exchange Rate Service
4. 규칙 저장

### 5.3 외부에서 확인 (브라우저)

- **메인 페이지:** `http://<EC2-PUBLIC-IP>/`
- **헬스체크:** `http://<EC2-PUBLIC-IP>/health`
- **관리자 페이지:** `http://<EC2-PUBLIC-IP>/admin` (ID: admin)
- **WebSocket:** `ws://<EC2-PUBLIC-IP>/ws`

---

## 6. 트러블슈팅

### 6.1 로그 권한 에러

**증상:**
```
PermissionError: [Errno 13] Permission denied: '/app/logs/app.log'
```

**원인:**
- `docker-compose.yml`의 bind mount (`./volumes/logs/app:/app/logs`)
- Docker가 root 권한으로 디렉토리 자동 생성
- 컨테이너 내부의 appuser(UID 1000)가 쓰기 권한 없음

**해결:**
```bash
cd ~/exchange-rate

# 컨테이너 중지
sudo docker compose down

# 디렉토리 생성 및 권한 설정
mkdir -p volumes/logs/app volumes/logs/nginx data
sudo chown -R 1000:1000 volumes/logs/app data
sudo chown -R 101:101 volumes/logs/nginx

# 다시 시작
sudo docker compose up -d
```

### 6.2 컨테이너가 Unhealthy 상태

**증상:**
```
exchange-rate-app  Exit 1
```

**확인:**
```bash
# 자세한 로그 확인
sudo docker compose logs --tail=100 fastapi

# 헬스체크 상태
sudo docker inspect exchange-rate-app | grep -A 10 Health
```

**일반적인 원인:**
1. 로그 권한 에러 (위 해결법 참고)
2. Python 패키지 설치 실패 → `sudo docker compose build --no-cache` 재빌드
3. Selenium/Chrome 설치 실패 → 메모리 부족 (t3.small에서도 고부하 시 발생)

### 6.3 메모리 부족 (OOM Killed)

**증상:**
```
OOMKilled: true
```

**확인:**
```bash
# 메모리 사용량
free -h
sudo docker stats

# dmesg에서 OOM 확인
dmesg | grep -i "out of memory"
```

**해결:**
- t3.small은 2GB RAM (고부하 시 한계 있음)
- 사용자 1,000명 이상 시 t3.medium 업그레이드 권장
- Swap 메모리 추가 (임시 방편):
  ```bash
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ```

### 6.4 포트 80 접속 안됨

**확인 사항:**
1. 보안 그룹 인바운드 규칙 확인 (HTTP 80 포트 개방)
2. Nginx 컨테이너 상태: `sudo docker compose ps`
3. Nginx 로그: `sudo docker compose logs nginx`

---

## 7. 서비스 관리

### 7.1 컨테이너 관리

```bash
# 상태 확인
sudo docker compose ps

# 로그 확인 (실시간)
sudo docker compose logs -f fastapi
sudo docker compose logs -f nginx

# 특정 패턴만 확인
sudo docker compose logs fastapi | grep -i "error\|exception"
sudo docker compose logs fastapi | grep -i "환율\|kb\|hana"

# 서비스 재시작
sudo docker compose restart

# 서비스 중지
sudo docker compose down

# 서비스 시작
sudo docker compose up -d

# 리소스 사용량 확인
sudo docker stats
```

### 7.2 시스템 모니터링

```bash
# 메모리 사용량
free -h

# 디스크 사용량
df -h

# 실시간 프로세스
htop  # 설치: sudo apt install htop
```

### 7.3 로그 관리

**로그 파일 위치:**
- 앱 로그: `volumes/logs/app/app.log`, `error.log`
- Nginx 로그: `volumes/logs/nginx/access.log`, `error.log`

**로그 확인:**
```bash
# Docker logs (컨테이너 stdout/stderr)
sudo docker compose logs -f fastapi

# 파일 로그 (컨테이너 내부)
sudo docker exec exchange-rate-app cat /app/logs/app.log
sudo docker exec exchange-rate-app tail -f /app/logs/error.log

# 호스트에서 직접 확인
tail -f volumes/logs/app/app.log
```

**로그 다운로드:**
```bash
# 로컬 터미널에서
scp -i ~/your-key.pem ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate/volumes/logs/app/app.log ./
```

---

## 8. 코드 업데이트

### 8.1 GitHub 사용 시

```bash
cd ~/exchange-rate

# 최신 코드 가져오기
git pull

# 컨테이너 중지
sudo docker compose down

# 이미지 재빌드 (코드 변경 시)
sudo docker compose build

# 서비스 재시작
sudo docker compose up -d

# 로그 확인
sudo docker compose logs -f fastapi
```

### 8.2 로컬에서 파일 전송 시

```bash
# 로컬 터미널에서
cd /path/to/exchange-rate
scp -i ~/your-key.pem -r app/ ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate/

# EC2에서
cd ~/exchange-rate
sudo docker compose down
sudo docker compose build
sudo docker compose up -d
```

### 8.3 .env 파일만 수정

```bash
# .env 수정
nano .env

# 재시작 (빌드 불필요)
sudo docker compose restart

# 또는
sudo docker compose down && sudo docker compose up -d
```

---

## 9. 백업 및 복구

### 9.1 데이터베이스 백업

```bash
# SQLite DB 백업 (로컬 터미널에서)
scp -i ~/your-key.pem ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate/data/exchange_rates.db ./backup/

# 백업 파일 압축
tar -czf backup_$(date +%Y%m%d).tar.gz data/ volumes/
```

### 9.2 자동 백업 (크론탭)

```bash
# EC2에서 크론탭 편집
crontab -e

# 매일 새벽 3시 DB 백업
0 3 * * * tar -czf ~/backups/db_$(date +\%Y\%m\%d).tar.gz ~/exchange-rate/data/

# 백업 디렉토리 생성
mkdir -p ~/backups
```

### 9.3 복구

```bash
# 로컬에서 백업 파일 전송
scp -i ~/your-key.pem exchange_rates.db ubuntu@<EC2-PUBLIC-IP>:~/exchange-rate/data/

# 컨테이너 재시작
cd ~/exchange-rate
sudo docker compose restart
```

---

## 10. 추가 개선 사항

### 10.1 HTTPS 설정 (Let's Encrypt)

**준비:**
- 도메인 이름 필요 (예: exchange-rate.yourdomain.com)
- DNS A 레코드를 EC2 IP로 설정

**Certbot 설치 및 SSL 인증서 발급:**
```bash
# Certbot 설치
sudo apt install -y certbot python3-certbot-nginx

# SSL 인증서 발급 (도메인 필요)
sudo certbot --nginx -d exchange-rate.yourdomain.com

# 자동 갱신 확인 (Let's Encrypt는 90일 유효)
sudo certbot renew --dry-run
```

### 10.2 도메인 연결

**AWS Route 53 또는 외부 DNS:**
1. A 레코드 추가: `exchange-rate.yourdomain.com` → EC2 Public IP
2. TTL: 300초
3. 전파 확인 (5-30분 소요)

### 10.3 CloudWatch 알람 설정

**모니터링 항목:**
- CPU 사용률 > 80%
- 메모리 사용률 > 90%
- 디스크 사용률 > 80%
- HTTP 5xx 에러율

### 10.4 텔레그램 알림 활성화

**.env 파일:**
```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

**재시작:**
```bash
sudo docker compose restart
```

---

## 11. 체크리스트 (빠른 참조)

### 초기 배포

- [ ] EC2 인스턴스 생성 (Ubuntu 24.04, t3.small)
- [ ] SSH 접속 확인
- [ ] 시스템 업데이트 (`sudo apt update && sudo apt upgrade -y`)
- [ ] Docker 설치 및 확인 (`sudo docker --version`)
- [ ] 프로젝트 파일 전송 (git clone 또는 scp)
- [ ] .env 파일 생성/전송
- [ ] **로그 디렉토리 권한 설정** (`mkdir -p` → `sudo chown`)
- [ ] Docker 이미지 빌드 (`sudo docker compose build`)
- [ ] 서비스 시작 (`sudo docker compose up -d`)
- [ ] 내부 헬스체크 (`curl http://localhost:80/health`)
- [ ] 보안 그룹 HTTP(80) 포트 개방
- [ ] 외부 접속 확인 (브라우저에서 `http://<EC2-IP>/`)

### 코드 업데이트

- [ ] 코드 가져오기 (git pull 또는 scp)
- [ ] 컨테이너 중지 (`sudo docker compose down`)
- [ ] 이미지 재빌드 (`sudo docker compose build`)
- [ ] 서비스 재시작 (`sudo docker compose up -d`)
- [ ] 로그 확인 (`sudo docker compose logs -f fastapi`)

### 일일 모니터링

- [ ] 컨테이너 상태 (`sudo docker compose ps`)
- [ ] 크롤러 동작 확인 (로그 또는 관리자 페이지)
- [ ] 메모리 사용률 (`free -h`)
- [ ] 디스크 사용률 (`df -h`)

---

## 12. 유용한 참고 자료

**프로젝트 문서:**
- [CLAUDE.md](CLAUDE.md) - 프로젝트 전체 가이드
- [DOCKER.md](DOCKER.md) - Docker 아키텍처 및 최적화 가이드
- [CRAWLERS.md](CRAWLERS.md) - 크롤러 구현 세부사항
- [DECISIONS.md](DECISIONS.md) - 아키텍처 의사결정 기록

**공식 문서:**
- [Docker Compose 문서](https://docs.docker.com/compose/)
- [FastAPI 배포 가이드](https://fastapi.tiangolo.com/deployment/)
- [Nginx 설정 가이드](https://nginx.org/en/docs/)

---

**마지막 업데이트:** 2026-01-13
**검증 환경:** AWS EC2 t3.small, Ubuntu 24.04 LTS, Docker 24.x
**작성자:** Jay (테스트 서버 배포 경험 기반)
