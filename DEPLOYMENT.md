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

> 📌 **이 절이 배포 절차의 기준 문서다.** `CLAUDE.md`·`DOCKER.md` 의 배포 언급은 여기를
> 가리키며, 충돌하면 이 절이 우선한다.

**B v1 보관기가 호스트에 설치된 뒤부터**(설치 여부는 [LOG_RETENTION.md](LOG_RETENTION.md#설치-기록) 설치 기록으로 확인 — 2026-09-23 설치),
컨테이너 switch 명령 바로 직전에 [Docker 로그 보존 절차](LOG_RETENTION.md#switch-직전과-복원-검증)의
`pre-switch`를 실행해 옛 ID 수집·복원·ID 재확인을 마치고 종료 0·`ready=true`를 확인한다.
실패하면 교체를 멈춘다. B의 14일 보존소와 Investing 관측 증거 보존소는 별개이며, 관측 기간의
추출은 별도로 유지한다. 교체 뒤 새 ID 수집과 미보존 꼬리를 확인하고 무손실로 단정하지 않는다.

### 8.0 배포 전에 반드시 아는 것 (2026-09-10 실측)

**① 예약 작업 5개가 앱과 같은 이미지를 쓴다.** 호스트 crontab 의
`docker compose run --rm … fastapi …` 5건(KST 매일 00:01, 매시 :05 :07 :09 :11)은
`exchange-rate-fastapi:latest` 를 쓴다. 빌드가 끝나 그 태그가 새 이미지에 붙는 순간부터,
**앱 컨테이너를 교체하기 전이라도 다음 예약 실행은 새 코드를 쓴다.** 그래서 태그 전환과
컨테이너 교체는 같은 배포 작업에서 조율한다. 두 명령을 연달아 실행하거나 `&&`로 이어도
원자적 전환은 아니다. 새·구 코드가 함께 도는 중간 상태와 실패 시 복구를 별도로 판단한다.
검증 전 예약 작업이 후보 이미지를 쓰면 안 되는 배포는 **별도 후보 태그로 빌드하고,
검증 뒤 `latest`를 전환**한다. 이 분리는 일반 Dockerfile 빌드에도 적용할 수 있으며,
8.1a의 기존 실행 환경 재사용과는 별개의 선택이다.

**② `--force-recreate` 와 `--build` 는 다르다.** 2026-09-10 `--dry-run` 실측: 쓸 이미지가
이미 있으면 `--force-recreate` 단독은 `Recreate → Started` 만 계획하고 빌드 단계가 없다.
`--build` 를 붙이면 `writing image → naming to exchange-rate-fastapi → Built` 가 붙는다.
⚠️ 이것을 "`--build` 없이는 절대 빌드하지 않는다" 로 일반화하지 말 것 — 이미지가 없거나
빌드 정책이 지정되면 달라진다. 그리고 `--build` 가 캐시를 재사용할지도 보장이 아니다.
관련 레이어가 무효화되면 Python·Chrome 까지 새로 설치될 수 있다.

**③ `docker compose down` 은 redis·nginx 까지 내린다.** 앱만 바꾸는 배포에 쓰지 않는다.

**④ `.env` 설정은 코드 기본값과 다르다.** 예: `IBK_RESULT_PATH_ENABLED` 는 코드 기본값이
`false` 인데 운영은 2026-09-10 부터 `.env` 로 `true` 다. 재빌드가 이 설정을 끄지는 않지만,
`.env` 를 덮어쓰거나 `docker compose restart` 를 쓰면(=env 재읽기 안 함) 어긋난다.
**설정 변경 반영에는 `--force-recreate` 가 필요하다.**

**⑤ `app/`·`scripts/` 는 bind-mount 가 아니다.** 호스트 `git pull` 만으로는 실행 컨테이너에
반영되지 않는다. 심볼 검색은 보조 확인일 뿐이다. 승인 SHA와 이미지 안 파일의 경로·해시를
대조하고, 실제 실행 이미지가 그 검증된 이미지와 같은지 확인한다(8.1 성공 조건 참조).

**⑥ 배포 창.** 서버 기준(`TZ=Asia/Seoul date`)으로 위 cron 분과 KRX CF close 종가
15:35~15:50 을 피한다. 시작 분만 피하는 것이 아니라 빌드·교체·복구에 필요한 시간과
이미 실행 중인 one-shot 컨테이너도 확인한다. 창을 확보하지 못하면 후보 태그 방식을 쓰거나
배포를 미룬다. 그리고 **검증하려는 크롤러가 실제로 도는 시간대**를 고른다 —
예로 IBK 는 06:00~07:59(BREAK2)에 정규 회차가 없어 그 창에서는 판정할 수 없다.

### 8.1 일반 Dockerfile로 앱을 빌드·교체하는 배포 (GitHub 사용)

⛔ **명령을 줄 단위로 나열하지 않는다.** 한 줄이 실패해도 다음 줄이 실행되면, 롤백 앵커
없이 빌드가 돌거나 `latest` 만 옮겨간 split 상태가 된다. 아래처럼 **실패 시 실제로 멈추는
한 덩어리**로 실행한다. ⚠️ 그래도 태그 전환과 컨테이너 교체가 원자적이 되지는 않는다 —
중간에 죽으면 수동 복구가 필요하므로, 실행 전에 롤백 경로를 먼저 확인한다.
아래는 **배포 승인을 받은 뒤 실행하는 예시**이지 실행 승인이나 자동 합격 판정이 아니다.
시작 전에 서버 상태·`.env`·해석된 Compose 모델이 승인한 범위와 일치하고 다른 배포가
동시에 실행되지 않는지 확인한다.
비밀값이 들어 있는 전체 설정은 출력하지 않는다. 이 예시는 `.env`나 호스트에 즉시 반영되는
nginx·Compose·static·ops 파일을 바꾸지 않는 범위다. 그 변경이 있으면 별도 절차를 정한다.
승인 SHA를 채우고 **독립된 Bash 프로세스 전체**로 실행한다(`if`/`!` 안에서 호출하지 않는다).

```bash
bash <<'DEPLOY_BASH'
set -euo pipefail
trap 'rc=$?; printf "배포 중단: runtime/latest/checkout을 확인한 뒤 복구 판단 필요\n" >&2; exit "$rc"' ERR
cd /home/ubuntu/exchange-rate

DEPLOY_SHA='승인된_40자리_SHA로_교체'
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
DEPLOY_STATUS=$(git status --porcelain)
[[ -z "$DEPLOY_STATUS" ]] || exit 1
DEPLOY_BRANCH=$(git branch --show-current)
[[ "$DEPLOY_BRANCH" = master ]] || exit 1
[[ -z "${COMPOSE_FILE:-}" && -z "${COMPOSE_PROFILES:-}" ]] || exit 1
docker info >/dev/null

df -P / | awk 'NR==2 {if ($5+0 >= 70) print "디스크 70% 이상 — 여유 확인 필요"; if ($5+0 >= 80) exit 1} END {if (NR < 2) exit 1}'
DEPLOY_BASE_SHA=$(git rev-parse HEAD)
DEPLOY_OLD_IMAGE=$(docker inspect exchange-rate-app --format '{{.Image}}')
[[ "$(docker image inspect exchange-rate-fastapi:latest --format '{{.Id}}')" = "$DEPLOY_OLD_IMAGE" ]] || exit 1

# `! inspect`는 set -e의 중단 대상이 아니다. 존재 여부를 명시적으로 분기한다.
DEPLOY_ROLLBACK_TAG="exchange-rate-fastapi:rollback-$(date +%Y%m%d-%H%M%S)-$$"
DEPLOY_EXISTING_TAG=$(docker image ls --format '{{.Repository}}:{{.Tag}}' "$DEPLOY_ROLLBACK_TAG")
if [[ -n "$DEPLOY_EXISTING_TAG" ]]; then
    printf '롤백 태그가 이미 존재하므로 중단합니다.\n' >&2
    exit 1
fi
docker tag "$DEPLOY_OLD_IMAGE" "$DEPLOY_ROLLBACK_TAG"
[[ "$(docker image inspect "$DEPLOY_ROLLBACK_TAG" --format '{{.Id}}')" = "$DEPLOY_OLD_IMAGE" ]] || exit 1
# 실패 후 새 터미널에서도 복구할 수 있도록 이 비밀 없는 세 값을 배포 기록에 보존한다.
printf 'BASE=%s\nOLD_IMAGE=%s\nROLLBACK_TAG=%s\n' "$DEPLOY_BASE_SHA" "$DEPLOY_OLD_IMAGE" "$DEPLOY_ROLLBACK_TAG"

git fetch origin master
git merge-base --is-ancestor "$DEPLOY_SHA" origin/master
DEPLOY_HOST_DIFF=$(git diff --name-only "$DEPLOY_BASE_SHA" "$DEPLOY_SHA" -- docker-compose.yml nginx static ops)
[[ -z "$DEPLOY_HOST_DIFF" ]] || exit 1
git merge --ff-only "$DEPLOY_SHA"
[[ "$(git rev-parse HEAD)" = "$DEPLOY_SHA" ]] || exit 1

docker compose build fastapi    # 완료 시점에 latest가 이동한다. 이후 실패해도 자동 복구되지 않는다.
DEPLOY_NEW_IMAGE=$(docker image inspect exchange-rate-fastapi:latest --format '{{.Id}}')
# 반드시 방금 빌드한 이미지 검사. Compose env·volume·네트워크 없이 구문만 확인한다.
docker run --rm --network none --entrypoint python "$DEPLOY_NEW_IMAGE" -m compileall -q /app/app /app/scripts
[[ "$(docker image inspect exchange-rate-fastapi:latest --format '{{.Id}}')" = "$DEPLOY_NEW_IMAGE" ]] || exit 1

docker compose up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 60 fastapi
[[ "$(docker inspect exchange-rate-app --format '{{.Image}}')" = "$DEPLOY_NEW_IMAGE" ]] || exit 1
docker exec exchange-rate-nginx nginx -t
docker exec exchange-rate-nginx nginx -s reload
printf '교체 명령 완료: NEW_IMAGE=%s. 아래 성공 조건을 별도로 확인하세요.\n' "$DEPLOY_NEW_IMAGE"
DEPLOY_BASH
```

**⑦ ⛔ `python -c "import app.main"` 을 사전 점검으로 쓰지 않는다.**
`app/main.py:222` 의 `create_all_app_tables(engine)` 이 **모듈 최상위**라, import 만으로
운영 DB 에 `Base.metadata.create_all()` 이 실행된다. idempotent 하지만 없는 테이블은
생성한다 — 배포 전 점검이 스키마를 건드리면 안 된다. 구문 확인은 **새 이미지의**
`compileall`을 쓴다. 이것은 의존성 로드·앱 기동·DB 정상 동작을 증명하는 시험이 아니다.

**성공 조건 — 넷을 모두 본다.**

1. 실행 이미지 ID: `docker inspect exchange-rate-app --format '{{.Image}}'` 가 의도한
   이미지와 같은가. (health 만 보면 교체 미적용을 성공으로 오판한다)
2. **승인 SHA 와 실제 코드의 결속**: `grep -c <심볼>` 은 문자열 존재일 뿐 승인한 코드가
   통째로 들어갔다는 증거가 아니다. `app/`·`scripts/`·`static/`·`templates/`의 파일 목록과 파일별
   SHA-256을 승인 SHA의 `git archive`와 대조한다. 추가·누락도 실패이며 `__pycache__`와
   `.pyc`·`.pyo` 같은 생성물만 같은 규칙으로 제외한다. 8.1a 실행기도 이 검사를 한다.
3. 앱 내부: `docker compose exec -T fastapi curl --fail --silent --show-error --max-time 10 http://localhost:8000/health`
   (fastapi는 host로 포트를 열지 않는다). HTTP 성공뿐 아니라 응답의 healthy 상태도 본다.
4. **nginx 경유·공개 경로**: 내부 health 만 보면 upstream 이 옛 컨테이너 IP 를 붙들고
   있는 장애를 놓친다. 위 교체 절차는 `nginx -t` 성공 후 reload한다. 공개 경로는
   `curl --fail --silent --show-error --max-time 10 https://fxi.kr/health`와 실제 데이터
   경로 하나를 함께 확인한다(TLS 검사를 생략하지 않는다). 502가 남으면 upstream과 앱을
   조사하고 성공 처리하지 않는다. nginx의 단일 파일 bind mount가 호스트 파일 교체로 옛
   inode를 붙들고 있으면 reload만으로 새 설정을 읽지 못한다. 그 경우 재생성을 포함한 별도
   nginx 배포 절차를 따른다. 모든 파일 수정이 반드시 inode를 바꾸는 것은 아니다.

**실패·되돌리기**: 중단 자체는 원복이 아니다. 특히 빌드 뒤 구문 검사에 실패해도 `latest`는
새 이미지일 수 있다. runtime·latest·checkout과 진행 중인 cron을 먼저 확인하고 복구 판단을
한다. 위에 기록한 rollback 태그의 전체 이미지 ID가 `OLD_IMAGE`와 같은지 확인한 뒤,
그 이미지를 `latest`로 붙이고 **추가 빌드·pull 없이 fastapi만 재생성**한다. nginx 검사·reload와
위 성공 조건도 다시 확인한다. checkout과 설정이 이전 이미지와 호환되는지 별도로 확인하며,
이미 DB에 기록한 값은 되돌리지 않는다. tag와 재생성을 한 셸에서 실행해도 원자적이지 않다.

### 8.1a 실행 환경을 고정한 채 코드만 바꾸는 배포 (선택)

Python·Chrome·패키지까지 바뀌는 변수를 배제하고 싶을 때는, 구 이미지를 `FROM` 으로
`app/`·`scripts/`·`static/`·`templates/`만 교체한 후보 이미지를 만들고 검증이 끝난 뒤에야 `latest`
를 옮기는 방식을 쓴다(2026-09-08·2026-09-10 배포가 이 방식). 검증 전에는 `latest` 가
구 이미지를 가리키므로 **예약 작업이 새 코드를 쓰지 않는다**.

⚠️ 이 방식의 실행기는 `BASE`/`TARGET` 이 하드코딩된 **전환 1회용**이며 서버 홈에 배포별
파일로 남아 있다. **예전 파일을 그대로 다시 실행하지 않는다.** 또 이 방식으로 교체한
컨테이너는 compose 라벨이 그 실행 디렉터리의 동결 모델을 가리키므로, 다음 배포 도구가
그 상태를 받아들이도록 준비돼 있어야 한다.

⚠️ 실행 환경(Chrome·Python·보안 패치) 갱신은 이 방식으로 할 수 없다. 그때는 일반
Dockerfile 빌드에서 갱신할 버전·캐시·pull 정책을 명시하고 격리된 브라우저 시험으로 확인한다.
`--build`만 붙인다고 모든 패키지가 갱신되는 것은 아니다. 운영 HTTP를 일부러 실패시켜
Selenium 표본을 만들지는 않는다.

### 8.1b 현재 운영 기준선 (2026-09-22 — 배포 직전 서버와 반드시 대조)

⛔ 이 표는 **참고**다. 권위는 서버에 있고, 다음 배포는 **서버 실측값에서 출발**한다.

| 항목 | 값 |
| --- | --- |
| 배포된 코드 | `34ef52c5df2c909c75b45d52f99f25d1c283b662` (Investing D9·D10, C1a 추출 분리, 관리자 토글·정기 전환 직렬화) |
| 실행 이미지 = `latest` | `sha256:6efc3f973676679d77b35aa3e7d9622f326c78537a0fff7d95f35badf6c3ae4e` |
| 코드 롤백 앵커 | `exchange-rate-fastapi:rollback-1790025334830310672` (= 직전 이미지 `d18b5d680c2a…3365b`) |
| IBK 설정 | `IBK_RESULT_PATH_ENABLED=true`, `TELEGRAM_ENABLED=true` (**둘 다 코드 기본값은 `false`**) |
| 현재 Compose 라벨 | `/home/ubuntu/fxi-release-34ef52c-20260922-01/new.json` |
| 동결 모델 SHA-256 | `3f38e15b18fefdf1ba6afb6c5e8c5c8f0902c5a3b3335a2ea1b00d210ff3fae6` |
| 같은 실행의 구 모델 SHA-256 | `05264d9580a7abd2fbe0fbbfc41f1be69e514444394e1daf226a038164c52e8a` (= 직전 배포의 `new.json`) |
| 배포 도구 | `/home/ubuntu/fxi-release-34ef52c-v8.py` SHA-256 `cc774ef6…17507` (**전환 1회용**) |

⚠️ **롤백 앵커 태그 이름을 세대와 혼동하지 말 것.** `721a323` 배포 시점에 `rollback-1789082062562934420`
(구 세대 `453fb87f…`)과 `d80ea73-1789082062562934420`(= `f6738338…`)이 **둘 다 존재**했고,
직전 운영 이미지는 후자였다. `rollback-` 접두사가 "직전 세대"를 뜻하지 않는다 — 태그를
이름으로 고르지 말고 **전체 이미지 ID 로 해석해 대조**한다.

⚠️ **구 기록 정정**: 2026-09-10 판 표는 Compose 라벨을 `fxi-ibk-activation-9d4e1c4-20260910-01/on.json`
으로 적었으나, `d80ea73` 배포가 라벨을 `fxi-release-d80ea73-v4-20260911-01/new.json` 으로 옮긴 뒤에도
문서가 따라가지 않았다. **라벨은 배포마다 이동하므로 경로 이름이 아니라 해시로 대조한다.**

**설정만 되돌리기(코드·이미지 유지)** — 활성화 도구의 rollback 이 `.env` 와 실행 모델을
함께 OFF 로 돌린다:

```bash
python3 /home/ubuntu/activate-ibk-9d4e1c4.py rollback \
        /home/ubuntu/fxi-ibk-activation-9d4e1c4-20260910-01
```

이 명령은 **2026-09-10 활성화 실행의 복구 전용**이며, 그 뒤로 코드가 다섯 번(`d80ea73`,
`721a323`, `13bc1a3`, `f94d04a`, `34ef52c`) 바뀌었으므로 **그대로 실행하지 않는다.** 당시 도구의 사전 조건과 현재 상태를
대조한 뒤에만 쓴다. 되돌리면 legacy의 MIBANK 저장 정책도 함께 복원되고, **이미 저장된 DB
데이터는 되돌아가지 않는다.**

⛔ **다음 배포가 알아야 할 것 셋.**
(1) 현재 컨테이너는 canonical `docker-compose.yml` 이 아니라 위 표의 동결 모델
(`fxi-release-34ef52c-20260922-01/new.json`)로 만들어져 있다. 배포 도구가 그 상태를
받아들이도록 준비돼 있어야 한다 — 경로 이름이 아니라 **파일 해시·직전 실행 기록·실행
이미지·전체 모델 동등성**을 대조하는 방식이어야 한다.
(2) 서버의 동결 모델(`new.json`/`old.json`)과 `dotenv.*` 는 **해석된 비밀값을 포함**하므로
0700 실행 폴더 밖으로 복사하지 않는다. 이 문서에도 해시만 적는다.
(3) §8.1a 실행기는 `BASE`/`TARGET` 하드코딩 **전환 1회용**이다. 직전 실행기를 재실행하지 말고
파생본을 새로 만들되, 상수만 바꾸지 말고 **직전 실행 기록의 스키마가 바뀌었는지 먼저 확인한다** —
`721a323` 배포에서 직전 실행이 활성화 기록에서 배포 기록으로 바뀌어 검증 함수를 새로 써야 했다.
`13bc1a3` 배포(v6)와 `f94d04a` 배포(v7)에서는 직전 기록(`state.json`·`switch-verify.json`·`operator-promoted.json`)이
직전 실행기가 검증하던 필드를 모두 가진 **같은 스키마**여서 상수만 바꿨다 — 다음에도 먼저 서버에서 대조한다.
`34ef52c` 배포(v8)도 같은 스키마였고, v7↔v8 은 전환 상수·머리말 주석·usage 문자열만 다르며 함수 정의는 모두 같다(Codex 의 AST 대조).
⚠️ v6 에 남아 있던 낡은 문구 두 가지("구 이미지에 추출기가 없다", "cron 5건" — 주석과 출력 문자열
양쪽)는 v7 에서 정정했다. v6↔v7 은 27줄 추가·27줄 삭제이고 전환 상수·머리말 주석·그 두 문구·usage
문자열뿐이라 **제어 흐름과 조건은 그대로다**.

`CLAUDE.md` 는 이 절을 가리키기만 한다(2026-09-18 정정 — 그전에는 `9d4e1c4` 를 "현재 운영 기준" 으로
적은 채 세 배포(`d80ea73`·`721a323`·`13bc1a3`) 동안 낡아 있었다). 배포 SHA·설정값의 정본은 이 절 하나다.

### 8.2 로컬에서 파일 전송 시

운영 checkout에 `scp -r app/`로 덮어쓰는 방법은 승인 SHA와 파일의 결속을 흐리고 삭제된
파일을 남길 수 있다. 필요한 경우 승인 SHA의 `git archive`를 별도 작업 폴더에 옮기고,
파일 목록·해시 검증, 이미지 생성, 전환·복구를 포함한 배포 후보로 검토한다. GitHub 배포가
가능하면 8.1을 사용한다. 파일 전송만으로 배포 완료로 판정하지 않는다.

### 8.3 .env 파일만 수정

⛔ **`docker compose restart` 는 `.env` 변경을 반영하지 않는다.** 기존 컨테이너를
stop/start 할 뿐이라 env_file 을 다시 읽지 않는다. 반영에는 **재생성**이 필요하다.
⛔ `down`은 쓰지 않는다(다른 서비스까지 내려간다). Compose의 변수 보간에 셸의 같은 이름
변수가 쓰이면 파일의 의도와 달라질 수 있다. `.env`만 보지 말고 실제 적용 모델을 대조한다.
편집 전 원본은 **기존 파일을 덮어쓰지 않는 고유한 백업**으로 보존한다(폴더 0700·파일 0600).
백업과 해석된 Compose 모델에는 비밀값이 있으므로 출력하거나 커밋하지 않는다.
승인한 설정만 편집·대조한 뒤, 아래 재생성 부분을 별도 Bash로 실행한다.

```bash
bash <<'ENV_BASH'
set -euo pipefail
cd /home/ubuntu/exchange-rate
ENV_CHANGE_IMAGE=$(docker inspect exchange-rate-app --format '{{.Image}}')
[[ "$(docker image inspect exchange-rate-fastapi:latest --format '{{.Id}}')" = "$ENV_CHANGE_IMAGE" ]] || exit 1
docker compose up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 60 fastapi
[[ "$(docker inspect exchange-rate-app --format '{{.Image}}')" = "$ENV_CHANGE_IMAGE" ]] || exit 1
docker exec exchange-rate-nginx nginx -t
docker exec exchange-rate-nginx nginx -s reload
ENV_BASH
```

8.1의 내부·공개 health와 실제 데이터 확인도 수행한다. `docker exec`로 설정을 조사할 때는
비밀이 아닌 특정 플래그만 확인한다. startup에서만 만들어지는 값(lifespan 전역 등)은 별개
Python 프로세스에서 import해도 미초기화다. 그런 값은 **운영 프로세스의 상태 API**로 확인한다.

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
- [ ] **DB 마이그레이션 필요 시**: 새 이미지 빌드 후 마이그레이션 실행 (아래 참조)
- [ ] 컨테이너 중지 (`sudo docker compose down`)
- [ ] 이미지 재빌드 (`sudo docker compose build`)
- [ ] 서비스 재시작 (`sudo docker compose up -d`)
- [ ] 로그 확인 (`sudo docker compose logs -f fastapi`)

### DB 마이그레이션 (v1.12.0+: DXY granularity)

> ⚠️ **순서: 코드 pull → 이미지 빌드 → 마이그레이션 → 서비스 시작 → 백필**
> 새 스크립트는 새 이미지에만 존재하므로, 반드시 빌드 후 `docker compose run`으로 실행합니다.

```bash
# 1. DB 백업 (RDS PostgreSQL)
pg_dump -h <RDS-HOST> -U <USER> -d <DB> > backup_$(date +%Y%m%d).sql

# 2. 코드 가져오기 + 새 이미지 빌드 (서비스는 아직 시작하지 않음)
git pull origin master
sudo docker compose build

# 3. granularity 컬럼 추가 (dry-run으로 먼저 확인)
sudo docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py --dry-run

# 4. 실제 마이그레이션 실행
sudo docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py

# 5. 서비스 시작
sudo docker compose up -d

# 6. 히스토리 백필 (서비스 가동 후 실행)
sudo docker compose exec fastapi python scripts/backfill_history.py
```

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
