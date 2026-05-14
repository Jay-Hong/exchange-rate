# USDT WebSocket (Upbit Canary) Runbook

Phase B.1 (PR1~PR7) 완료 후 운영 진입 절차. KRX canary 패턴 mirror.

설계 문서: [USDT_WS_DESIGN_PLAN.md](USDT_WS_DESIGN_PLAN.md) (§12 PR 분할, §13 long-term roadmap)

---

## 핵심 원칙 (반드시 인지)

- **`USDT_WS_UPBIT_ENABLED=false` default**: env 변경 없이 코드 배포 시 lifecycle 비활성, 운영 영향 0.
- **기존 REST polling 유지** (`usdt_sources.collect_usdt_rates` 10s): WS 활성화해도 polling은 그대로. additive 원칙 (§12.1 guardrail 2).
- **Dual-writer 의도적**: WS는 Redis writer + DB writer + Alert evaluator로 fanout. polling도 동일 fanout 호출 → 일시적으로 dual-write 발생. `insert_source_rate_if_changed`가 대부분 dedup, 잔존 race는 baseline 측정 항목.
- **격리 원칙**: WS connection 장애 / Redis 장애 / DB 장애 / FCM 장애 모두 WS session에 전파 X. fallback probe 실패도 log only.
- **Rollback 1차 수단**: `USDT_WS_UPBIT_ENABLED=false` env 변경 + process 재생성. 어느 stage에서든 즉시 격리 가능.

---

## Stage 0 — Dev Smoke (백엔드 서버에서 실행)

목적: **백엔드 서버 (EC2)** 에서 짧은 시간 `USDT_WS_UPBIT_ENABLED=true` 토글해
connect/subscribe/fanout 정상 동작 확인. PR4~PR7 fanout이 단일 토글로 한
번에 활성됨 (Stage 1 = 모든 fanout 동시).

**실행 환경**: AWS EC2 (`fxi.kr` 도메인). 모든 명령은 SSH 후 EC2 작업
디렉토리에서 실행.

### Stage 0 진입 전 — 로컬 사전 sanity (1~5분)

운영 데이터 검증 아님. 코드/test 통과만 확인:

```bash
# 로컬 작업 디렉토리에서 (Mac/Linux 개발 머신)
cd /path/to/exchange-rate
git status                       # 변경/커밋 안 한 코드 없음 확인
git log --oneline -3             # PR7 commit (17471f7) origin에 있음
python -m pytest -q              # 736 passed, 1 skipped 기대
```

import/runtime error 없는지 짧게 확인하려면 (선택):

```bash
# 로컬에서 short run (production DB 영향 방지 위해 별도 .env 또는 sqlite override)
DATABASE_URL=sqlite:////tmp/usdt_smoke.db \
USDT_WS_UPBIT_ENABLED=true \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
# 30초~1분 관찰: lifecycle log + first tick
# Ctrl+C로 종료. production 영향 없음.
```

이 단계 통과 후 EC2 진입.

### Stage 0 본 절차 (EC2)

```bash
# ── EC2 SSH ──
ssh ubuntu@fxi.kr   # (또는 EC2 IP)
cd ~/exchange-rate  # EC2 작업 디렉토리 (실제 경로는 사용자 설정에 따름)

# 1. 최신 코드 pull
git pull origin master
# 기대: PR1~PR7 + follow-ups + CANARY runbook 반영

# 2. 빌드 (Python deps 변경 없으면 생략 가능, 안전을 위해 한 번 빌드 권장)
docker compose build

# 3. .env에 토글 추가
# (vi/nano 등으로) .env 편집 → 다음 라인 추가:
USDT_WS_UPBIT_ENABLED=true

# 4. process 재생성 (env_file은 import 시 1회 읽음 — restart는 변경 미반영)
docker compose up -d fastapi

# 5. 활성화 확인 (즉시)
docker compose logs --tail=30 fastapi | grep "usdt_ws.upbit"
# 기대: "[usdt_ws.upbit] UpbitWsClient skeleton 시작 (PR1)" 또는
#       "[usdt_ws.upbit] start (PR3 reconnect loop)" 등.
#       "USDT_WS_UPBIT_ENABLED=false, skip start"는 없어야 함.
```

### 확인 명령 (EC2)

```bash
# (1) Lifecycle + first tick (WS connect/subscribe 정상)
docker compose logs -f fastapi | grep "usdt_ws.upbit"
# 기대 로그 (시작 30초 이내):
#   [usdt_ws.upbit] start (PR3 reconnect loop)
#   [usdt_ws.upbit] connected url=wss://api.upbit.com/websocket/v1
#   [usdt_ws.upbit] subscribed code=KRW-USDT ticket=fxi-usdt-upbit
#   [usdt_ws.upbit] first tick (rate=..., timestamp_ms=...)

# (2) Redis latest 갱신 (mirror_age < 5s)
# Redis 컨테이너에 password 필요 (.env REDIS_PASSWORD).
docker compose exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" GET latest:source:upbit:usdt-krw'
# 기대: JSON {"rate": ..., "timestamp": "...+09:00", "mirrored_at": "..."}
#       mirrored_at이 현재 시간 대비 5초 이내

# (3) DB insert-if-changed 동작 (RDS Postgres)
# fastapi 컨테이너 안에서 SQLAlchemy로 쿼리 (host psql 불필요, .env export 불필요,
# 앱이 실제 쓰는 DB 연결 경로 동일 검증).
docker compose exec fastapi python - <<'PY'
from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT source, asset, rate, timestamp
        FROM source_rates
        WHERE source='upbit' AND asset='usdt-krw'
        ORDER BY timestamp DESC
        LIMIT 10
    """)).fetchall()

for row in rows:
    print(dict(row._mapping))
PY
# 기대: 최근 10건이 모두 다른 rate (insert-if-changed 작동),
#       timestamp는 1초 window 간격 또는 가격 변동 간격

# (4) Alert evaluator no-crash
docker compose logs --since=5m fastapi | grep "alert_evaluator"
# 기대: error/exception 없음. settings 있을 시 평가 로그.
#       settings 없으면 cache miss → empty populate → 조용.

# (5) Reconnect/fallback warning 없음
docker compose logs --since=5m fastapi | grep -E "usdt_ws.upbit.*(reconnect|fallback|crashed)"
# 기대: 정상 시 warning 없음. 일시적 reconnect는 허용.

# (6) 기존 REST polling 생존 확인 (additive 유지 검증)
# scheduler job은 10초 주기지만 로그는 가격 변경 시("⚡️ USDT 환율 변경")만
# 출력 — 저거래량 시 로그 없을 수 있음. 따라서 생존은 아래 3가지로 종합 판단:
#
# (6a) 변경 로그 — 가격 변동 있는 시간대에만 출력
docker compose logs --since=5m fastapi | grep "USDT 환율 변경" | head -5
# 기대: 5분 안에 가격 변동 있으면 출력. 변동 없으면 0건도 정상.
#
# (6b) 실패 로그 부재 — polling 자체 실패는 항상 logged
docker compose logs --since=5m fastapi | grep -E "USDT 저장 실패|source.*timeout|source.*request failed"
# 기대: 0건. 1건 이상이면 polling 부분 실패 (특정 source).
#
# (6c) DB row 시점 확인 — 어떤 source든 최근 30초 안에 INSERT
docker compose exec fastapi python - <<'PY'
from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT source, asset, MAX(timestamp) AS latest
        FROM source_rates
        WHERE source IN ('upbit','bithumb','coinone','korbit','gopax')
        GROUP BY source, asset
        ORDER BY source
    """)).fetchall()

for row in rows:
    print(dict(row._mapping))
PY
# 기대: upbit는 WS로 매우 fresh (< 5s). 나머지 4개 (bithumb/coinone/korbit/gopax)는
#       polling 주기(10s)에 따라 대부분 30초 이내 latest 갱신 (가격 변동 있을 때만,
#       없으면 더 오래된 timestamp 정상).
```

### Stage 0 성공 기준 (Codex 권장 6항목)

- [ ] WS 연결/subscribe 성공 (log)
- [ ] first tick INFO log 발생
- [ ] Redis `latest:source:upbit:usdt-krw` 갱신 (mirror_age <5s)
- [ ] DB `source_rates` insert-if-changed 정상 (rate 변경 시만 INSERT)
- [ ] Alert evaluator no-crash (cache miss → DB query → empty 처리 OK)
- [ ] Reconnect/fallback task warning 없음 (강제 disconnect 시나리오 외)
- [ ] **기존 REST polling 정상 동작 유지** (확인 방법은 Stage 0 명령 §(6) 참조)

### Stage 0 실패 시 rollback (EC2)

```bash
# ── EC2에서 실행 ──

# 1. .env 편집 → 토글 false로 변경 (or 해당 라인 삭제)
USDT_WS_UPBIT_ENABLED=false

# 2. process 재생성 (env_file은 import 시 1회 읽기 — restart는 변경 미반영)
docker compose up -d fastapi

# 3. 격리 확인
docker compose logs --tail=20 fastapi | grep "usdt_ws.upbit"
# 기대: "[usdt_ws.upbit] USDT_WS_UPBIT_ENABLED=false, skip start" 1줄 (시작 직후만)
# 추가 로그 없음 = lifecycle 완전 격리.
```

문제 진단 (rollback 후 별도 분석):
- WS connect 실패 → 네트워크/방화벽 → curl/wscat으로 직접 확인
- Redis write 실패 → `set_latest_usdt_rate_from_sync_job` log + Redis 연결
- DB write 실패 → `_sync_db_write` log + DB 연결
- Alert evaluator crash → `app/notifications/alert_evaluator.py` 로그 + setting/device 데이터 확인

---

## 24h Soak (EC2, Stage 1 진입 전 필수)

§12.1 guardrail 3: PR2 24h soak는 운영 활성화 진입 전 leak/race 검증 목적.
Stage 0 짧은 활성화 통과 후, `USDT_WS_UPBIT_ENABLED=true` 그대로 두고 24h
운영 → 메모리/race/reconnect 빈도 등 baseline 확보.

### 목적

- 메모리 leak 없음 (RSS 안정)
- File descriptor leak 없음 (WS task lifecycle)
- Asyncio pending task 누적 없음 (Redis writer / Alert evaluator)
- Reconnect 빈도 합리적 (네트워크 일시 끊김 정도, repeated 폭주 X)
- DB row 증가율 baseline (insert-if-changed 효과)
- FCM 발송 (test alert 존재 시) 성공률

### 진입 조건

- Stage 0 성공 기준 7항목 모두 ✓
- EC2 24h 안정 가용 (실제 Upbit 트래픽 받는 상태로 24h 유지)

### 24h 모니터링 명령 (EC2, 1시간마다 check 권장)

```bash
# ── EC2에서 실행 ──

# 메모리/프로세스
docker stats --no-stream fastapi

# Asyncio task 누수 — Redis writer / Alert evaluator pending
# (psutil/asyncio inspection이 직접 어렵다면 log warning 카운트로 대체)
docker compose logs --since=1h fastapi | grep -c "alert_evaluator.*backlog"
# 기대: 0 또는 매우 적음 (>100 threshold 도달 시 warning)

# Redis writer saturation
docker compose logs --since=1h fastapi | grep -c "Redis write queue saturated"
# 기대: 0 (정상이면 절대 발생 X)

# Reconnect 빈도
docker compose logs --since=1h fastapi | grep -c "connection closed (attempt"
# 기대: 시간당 한 자릿수 이내

# DB row 증가 — 1시간 차이 (RDS Postgres, 컨테이너 내부 SQLAlchemy)
docker compose exec fastapi python - <<'PY'
from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    count = conn.execute(text("""
        SELECT COUNT(*) FROM source_rates
        WHERE source='upbit' AND asset='usdt-krw'
          AND timestamp > NOW() - INTERVAL '1 hour'
    """)).scalar()

print(f"upbit usdt-krw rows in last 1h: {count}")
PY
# 기대: 가격 변동 빈도에 비례 (insert-if-changed로 동일 rate skip)
```

### 24h 통과 기준

- [ ] 메모리 RSS 안정 (시작 대비 ±20% 이내)
- [ ] Asyncio backlog/saturation warning 0
- [ ] Reconnect 시간당 평균 < 10회
- [ ] DB row 증가가 가격 변동과 합리적 비례 (REST polling 대비 큰 차이 X)
- [ ] FCM 발송 (test alert 있을 시) 성공률 > 95%

---

## Stage 1 — 운영 활성화 (모든 fanout 동시 active)

**중요**: 현재 구현은 `USDT_WS_UPBIT_ENABLED=true` **단일 토글**로
PR4 Redis writer + PR5 DB writer + PR6 Alert evaluator + PR7 REST fallback
controller가 **모두 동시에 활성**됩니다. Stage 2/3는 *별도 활성화 단계가
아니라* Stage 1 활성화 후 시간 두고 진행하는 **관찰/검증 checklist**입니다.

### 진입 조건

- Stage 0 Dev Smoke 7항목 통과 (EC2 짧은 활성화)
- 24h Soak 5항목 통과 (EC2 24h 유지)

### Stage 0 → 24h Soak → Stage 1 흐름

Stage 0 통과 후 토글을 끄지 않고 그대로 24h 유지하면 자연스럽게 24h Soak
단계 진입. 24h 통과 후 별도 활성화 명령 없이 그대로 Stage 1 (1주 baseline)
관찰 단계로 진입. 토글은 한 번 켜지면 유지.

문제 발생 시: 어느 단계에서든 즉시 [Rollback 절차 공통](#rollback-절차-공통)
참조 → fix → Stage 0부터 재시작.

```bash
# 24h Soak 시작 시점 기록 (EC2):
date  # 활성화 시점
docker compose logs --tail=5 fastapi | grep "usdt_ws.upbit"
# 24h 후 다시 실행해 baseline 비교.
```

### Stage 1 활성화 시 동시 켜지는 fanout

| 컴포넌트 | 동작 |
|---|---|
| WS connection (PR2) | Upbit `wss://api.upbit.com/websocket/v1` 연결 |
| LivenessMonitor + reconnect (PR3) | frame/heartbeat 추적, reconnect backoff |
| **Redis writer (PR4)** | 매 valid tick → `latest:source:upbit:usdt-krw` write |
| **DB writer (PR5)** | 1초 window debounce + insert_source_rate_if_changed |
| **Alert evaluator (PR6)** | tick → AlertObservation → FCM (settings 있을 시) |
| **REST fallback (PR7)** | stale 감지 시 자동 probe (강제 trigger 없음) |

### Stage 2/3 관찰 checklist (별도 활성화 X)

Stage 2/3는 시간 두고 진행하는 관찰 단계. Stage 1 활성화 시점부터 모두 동작
중이며, 본 checklist는 단계별 **검증 포커스**를 정의.

---

## Stage 2 — Alert evaluator 발화 검증 (관찰)

**Stage 1 활성화 1주 baseline 통과 후 본 checklist 진행.**

### Stage 2 관찰 checklist

- [ ] **중복 FCM 없음**: 같은 setting_id에 대해 REST polling alert과 WS evaluator
  alert이 모두 발화하지 않는지. `mark_source_setting_triggered`가 atomic이라
  triggered=False → True 전환 후 다른 path는 dedup됨. log로 발화 횟수 확인:

  ```bash
  docker compose logs --since=1h fastapi | grep "alert_fcm_sent" | wc -l
  ```

- [ ] **`triggered/last_notified_*` 일관성**: 발화된 setting의 DB 상태 확인:

  ```sql
  SELECT id, source, asset, condition, threshold, triggered, last_notified_at, last_notified_rate
  FROM source_notification_settings
  WHERE triggered = TRUE
  ORDER BY last_notified_at DESC LIMIT 10;
  ```

  기대: triggered=True인 모든 row가 last_notified_at + last_notified_rate 채워짐.

- [ ] **Cache stale 빈도**: CRUD invalidation이 정상 동작하면 stale 발화 거의 없음.

  ```bash
  docker compose logs --since=1h fastapi | grep -c "stale cache detected"
  ```

  기대: 시간당 < 10 (사용자 settings 수정 빈도에 비례).

- [ ] **Empty device_tokens 케이스**: device 없는 사용자가 setting만 등록한 경우.

  ```bash
  docker compose logs --since=1h fastapi | grep "alert_evaluator.*empty"
  ```

  기대: warning 0 (cache populate 시점에 제외 — PR6 guardrail).

---

## Stage 3 — Fallback probe 검증 (관찰)

**Stage 2 통과 후 본 checklist 진행. 강제 disconnect 시나리오 포함.**

### Stage 3 관찰 checklist

- [ ] **자연 발화 관찰**: 운영 중 Upbit WS 일시 끊김 발생 시 probe 발화 확인.

  ```bash
  docker compose logs --since=24h fastapi | grep "fallback.*probe start"
  ```

  기대: 0 또는 적음 (Upbit WS 안정적이면 발화 거의 없음).

- [ ] **강제 disconnect 검증** (선택, dev 환경 권장):
  네트워크 차단 등으로 강제 disconnect → stale 감지 → probe 발화 확인.

  ```bash
  # stale → probe 발화 log
  docker compose logs fastapi | grep -E "status normal → stale|fallback.*probe (start|success)"
  ```

  기대 순서:
  1. `status normal → stale` (frame/heartbeat silence 30s+)
  2. `fallback probe start (reason=stale_transition)`
  3. `fallback probe success (rate=..., ts_ms=..., reason=stale_transition)`
  4. cooldown 30s 동안 추가 probe 발화 없음
  5. WS 복구 시 `status stale → normal` → `cooldown reset`

- [ ] **Probe 실패 격리**: REST 실패 시 WS session 영향 없음.

  ```bash
  # Probe 실패 log
  docker compose logs --since=24h fastapi | grep -E "fallback.*(timeout|error|None)"
  # WS session 영향 확인 (재시작 / status 갱신 등)
  docker compose logs --since=24h fastapi | grep "usdt_ws.upbit] start"
  ```

  기대: probe 실패 후 WS lifecycle log (start/restart 등) 추가 발생 없음.

- [ ] **Cooldown reset on normal recovery**: stale → reconnecting → normal 경로에서도
  cooldown reset 확인 (PR7 Codex Finding 1 회귀 검증).

  ```bash
  docker compose logs --since=24h fastapi | grep "cooldown reset"
  ```

---

## Rollback 절차 공통

```bash
# ── EC2에서 실행 ──

# 1차 (모든 stage 공통): env toggle
# .env 편집 → USDT_WS_UPBIT_ENABLED=false (또는 라인 삭제)
docker compose up -d fastapi
# (env_file은 import 시 1회 읽기 — restart는 변경 미반영, up -d 필수)

# 격리 확인
docker compose logs --tail=20 fastapi | grep "usdt_ws.upbit"
# 기대: "USDT_WS_UPBIT_ENABLED=false, skip start" 1줄. 추가 로그 없음.

# 2차 (env toggle로도 격리 안 되거나 코드 자체 문제 시): 코드 revert
git revert <commit-sha>
docker compose build
docker compose up -d fastapi

# 데이터 보존: source_rates / source_notification_logs schema/data 유지.
# (DB writer / alert evaluator rollback 시 누락된 tick은 기존 REST polling이
#  baseline 처리 — additive 원칙으로 polling은 계속 동작.)
```

---

## 공통 관찰 지표

| 항목 | 명령 | baseline |
|---|---|---|
| WS 연결 상태 | `grep "status .* → "` 최근 24h | 시간당 reconnect <10 |
| Redis latest 갱신 | `redis-cli GET latest:source:upbit:usdt-krw` → mirrored_at | <5s ago |
| DB row 증가 | source_rates COUNT 1h | 가격 변동에 비례 |
| Alert FCM 발송 | `grep "alert_fcm_sent"` | test alert 있을 시 성공 ≥95% |
| Cache stale 빈도 | `grep "stale cache detected"` | <10/h (CRUD invalidation 정상 시) |
| Fallback probe 빈도 | `grep "fallback.*probe start"` | stale transition 시 1회만 |

---

## 운영 이력

각 stage 진행 시 결과/시각/특이사항 누적 기록. 24h Soak / Stage 1/2/3 진입 시
동일 형식으로 추가.

### 2026-05-15 Stage 0 Dev Smoke

- 실행 환경: EC2 production backend (3.36.30.32, `~/exchange-rate`)
- 활성화 시각: 2026-05-15 01:01 KST (= 2026-05-14 16:01 UTC)
- Commit: `e8ee834` + PR7 `17471f7` 포함 (Phase B.1 PR1~PR7 + follow-ups)
- 결과: **PASS** (7/7)
- WS connect/subscribed/first tick: PASS (01:01:23 first tick)
- Redis latest mirror_age: **~4s** (<5s 기준 통과)
- DB insert-if-changed: PASS, rate 1479.0 ↔ 1480.0 alternating, sub-second 간격 INSERT
- Alert evaluator: no-crash, active untriggered upbit usdt-krw alerts = **0**
- Reconnect/fallback warning: none
- Existing REST polling: PASS (변경 로그 5건 01:01:26~01:01:46, 실패 0건)
- Note: gopax latest 30분 stale (15:33 vs 16:03), failure log 0건 → 가격 미변동
  추정. Upbit WS scope 외 별도 추적.
- Next: **24h Soak 시작** (토글 유지). First 1h checkpoint at **2026-05-15 02:01 KST**
  — reconnect count / Redis saturation / alert backlog / DB row count 확인.

### 2026-05-15 02:01 KST — 24h Soak 1h Checkpoint (PASS)

- 측정 시각: 17:01:29 UTC (1h 0min 33sec after activation)
- Memory/CPU: **326.7MiB / 40.84%** (limit 800M), CPU **10.58%** — 안정 (leak 징후 없음)
- Alert evaluator backlog warning: **0** (>100 threshold 미도달)
- Redis writer saturation: **0** (정상)
- Reconnect count: **0** (1h 동안 WS disconnect 없음)
- Fallback probe count: **0** (stale transition 미발생 → probe 미발화, WS 매우 안정)
- Status 전이: **0** (normal 유지)
- DB row 1h (asset=usdt-krw):
  - upbit: **288 rows** (~13s/INSERT, WS 가격 감지 빈도)
  - bithumb: 128 rows (~28s/INSERT, REST 10s polling)
  - coinone: 63 / korbit: 17 / gopax: 21
- 분석: Upbit 288 > 다른 REST source ~2배 — WS fresh tick 효과 검증. dual-writer
  (WS + REST polling) additive 정상 동작.
- Next: **2h 또는 6h checkpoint** (사용자 트리거). 24h 종료 시각 = **2026-05-16 01:01 KST**.

## 참조

- [USDT_WS_DESIGN_PLAN.md](USDT_WS_DESIGN_PLAN.md) — 설계/PR 분할/long-term roadmap
- [USDT_EXCHANGE_WEBSOCKET_GUIDE.md](USDT_EXCHANGE_WEBSOCKET_GUIDE.md) — Upbit WS spec
- [KRX_CANARY.md](KRX_CANARY.md) — 동일 패턴 runbook 참고

Phase B.1 commits:
- PR1 `9cc5079` feature flag + lifecycle skeleton
- PR2 `7ea7670` connect/subscribe/parse + log only
- PR3 `c4d3771` LivenessMonitor + reconnect + heartbeat
- PR4 `4c67330` Redis latest writer
- PR5 `f2921f5` DB writer 1초 window debounce
- PR6 `8504100` AlertObservation + UsdtAlertEvaluator B1
- Follow-up 1 `46d2e9a` Settings CRUD cache invalidation
- Follow-up 2 `3a782e2` Long-term scaling roadmap doc
- PR7 `17471f7` REST fallback controller (Phase B.1 마지막)
