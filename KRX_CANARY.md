# KRX 미국달러선물 (KIS Open API) Canary Runbook

> 목적: KRX optional source(PR6 시리즈)의 단계적 운영 진입 절차. 운영자가 그대로 따라할 수 있는 runbook 형태.
>
> 관련 문서: [CLAUDE.md "KRX 미국달러선물" 섹션](CLAUDE.md), [.env.example](.env.example), [DECISIONS.md](DECISIONS.md)

---

## 핵심 원칙 (반드시 인지)

1. **KRX optional source**: KRX 수집/노출 어느 단계의 실패도 baseline 서비스(은행 + investing + USDT) 가용성에 영향 0. 실패 격리는 env 토글 + bootstrap try/except로 보장.
2. **Stage 1은 "DB 저장 관찰" 목적**: broadcast 노출 X. 앱/사용자 가시 영향 0.
3. **실행 중 contract 자동 교체 없음** (현재 미구현). bootstrap 시 `select_active_usd_futures_contract()`로 1회 resolve 후 client lifecycle 동안 불변.
4. **5/18 만기일 관찰 = 자동 rollover 검증 X / 자동 rollover 설계용 데이터 수집**: 11:30 이후 client 상태, tick 끊김 패턴, 수동 restart 시 다음 월물 잡힘 여부 등.
5. **두 토글 모두 process 재생성 필요**: `app/config.py`가 import 시 1회 `os.getenv` 읽기 → hot-reload 안 됨. `.env` 변경 후에는 `docker compose up -d fastapi`처럼 컨테이너를 재생성해야 env_file이 다시 반영된다 (`docker compose restart fastapi`는 기존 컨테이너 stop/start라 `.env` 변경을 반영하지 않음).

---

## Stage 0 — 비활성 baseline (운영 영향 0)

**상태 확인 명령 (변경 X, 사전 검증만):**

```bash
# 운영 .env에 KRX 토글 default false 인지 확인
docker compose exec fastapi env | grep -E '^(KRX_|KIS_|REDIS_LATEST_)'
```

**기대 출력:**

- `KRX_FUTURES_ENABLED=false` (또는 미설정 → default false)
- `KRX_BROADCAST_INCLUDE=false` (또는 미설정 → default false)
- `KIS_APP_KEY` / `KIS_APP_SECRET`: 미설정이어도 무방 (Stage 0에선 lifecycle 비활성)
- `REDIS_LATEST_ENABLED`: 운영 .env의 현재값 그대로 (변경 X)

**로그 확인 (KRX 비활성 정상 동작):**

```bash
docker compose logs fastapi --since 5m | grep -E '\[krx\]'
# 기대: "[krx] KRX_FUTURES_ENABLED=false, skip start" 1줄 (시작 직후만)
```

---

## Stage 1 — DB 저장 관찰 (앱/사용자 영향 0)

### 목적

- KIS approval 안정성 (refresh 주기, 실패 빈도)
- WebSocket reconnect 빈도 / 패턴
- 체결 tick rate (분당, 세션별)
- KrxDbWriter 1초 window debounce 효과 (DB insert / tick ratio)
- `source_rates` 증가량 / 디스크 소비
- 5/18 만기일 client 상태 관찰

### 진입 절차

**1. 운영 `.env`에 추가/변경:**

```bash
# KRX 활성화 (수집 lifecycle만)
KIS_APP_KEY=<발급받은 KIS App Key>
KIS_APP_SECRET=<발급받은 KIS App Secret>
KRX_FUTURES_ENABLED=true
KRX_BROADCAST_INCLUDE=false   # broadcast 노출 X (Stage 2 topic protocol 도입 후 true)

# 기존 운영 토글은 변경 X
# REDIS_LATEST_ENABLED=<운영 현재값 그대로>
```

**2. process 재생성:**

```bash
docker compose up -d fastapi
```

**3. 재생성 직후 검증 (5분 이내):**

> ⚠️ 아래 검증은 **KRX active session 중**일 때만 즉시 성립합니다. KRX active session: 정규 평일 08:30~15:45 (CF) / 야간 평일 17:50~익일 06:00 (CM). 그 외(15:45~17:50 break / 새벽 06:00~08:30 / 주말 / 만기일 11:30 이후)에 restart하면 `[krx] KisFuturesClient 시작` 로그까지는 보이지만 `[kis_ws] connected` / `subscribed` / approval cache 파일 / `source_rates` KRX row는 다음 active session 진입 전까지 부재가 **정상 동작**입니다 (`KisFuturesClient.start()`의 메인 루프가 session=None 시 30초 sleep loop만 돌고 approval/connect/subscribe/DB write 호출 X).

```bash
# bootstrap 성공 + WebSocket 연결 + subscribe 확인
docker compose logs fastapi --since 5m | grep -E '\[krx\]|\[kis_ws\]|\[kis_approval\]'
```

기대 로그 (실제 코드 기준):

```text
[krx] KisFuturesClient 시작 — contract=A75605 expiry=2026-05-18
[kis_ws] connected, session=CF    # 정규세션 (08:30~15:45 KST). 야간은 session=CM
[kis_ws] subscribed tr_id=H0CFCNT0 key=A75605   # 야간이면 H0MFCNT0
[kis_ws] subscribed tr_id=H0CFASP0 key=A75605   # 야간이면 H0MFASP0
# status 전이는 변경 시에만 출력 (`if self._status != new_status`). 첫 정상
# 연결은 기본값 normal 그대로라 status 로그가 없을 수 있음.
# 예 (재연결 발생 시): [kis_ws] status normal → reconnecting
# 예 (재연결 복구 시): [kis_ws] status reconnecting → normal
```

> KisApprovalManager는 정상 발급 시 silent (성공 로그 없음). approval 관련 로그가 보인다면 warning (`[kis_approval] 캐시 읽기 실패` / `chmod 600 실패` 등)이고 즉시 검토 필요.

**경고 신호 (즉시 rollback 검토):**

- `[krx] active contract resolve 실패` → KIS 마스터 fetch/parse 실패
- `[krx] active USD futures contract 없음` → contract 선정 실패 (만기 데이터 이상)
- `[krx] KIS_APP_KEY/SECRET 미설정` → env 적용 안 됨
- `[krx] bootstrap 실패` → 일반 예외
- `[krx] KisFuturesClient task crashed` → client task 비정상 종료
- `[kis_ws] malformed tick frame` 빈발 → KIS payload 포맷 변경 가능성

### 검증 명령 (Stage 1 정합성)

**SQL — `source_rates`에 KRX 데이터 적재 확인:**

```sql
SELECT source, asset,
       COUNT(*) AS cnt,
       MIN(timestamp) AS first_ts,
       MAX(timestamp) AS last_ts
FROM source_rates
WHERE source = 'krx'
GROUP BY source, asset;
```

기대 (KRX active session에서 첫 tick 수신 이후): `source='krx', asset='usd-krw-futures', cnt > 0`. `last_ts`는 active session 중일 때만 `now()`에 근접 (정규 08:30~15:45 / 야간 17:50~익일 06:00 KST). active session 진입 전(첫 tick 미수신)이거나 휴장/break 중에는 cnt=0 또는 stale이 정상.

**KIS approval cache 파일 검증 (정상 발급은 silent — 파일 mtime/만료 epoch로 확인):**

```bash
# 파일 존재 + mtime + 권한
docker compose exec fastapi ls -la .cache/kis_ws_approval.json
# 기대: chmod 600, mtime이 최근 24h 이내 (KIS approval 23.5h 갱신 마진 정책)

# 캐시 내용 확인 — 실제 저장 형식: {"approval_key", "expires_at_epoch", "created_at_epoch"}
docker compose exec fastapi cat .cache/kis_ws_approval.json | python3 -m json.tool
```

**복붙용 만료 잔여시간 검증 (현재 epoch 대비 expires_at_epoch 잔여 초):**

```bash
docker compose exec fastapi python3 - <<'PY'
import json, time
from pathlib import Path
p = Path(".cache/kis_ws_approval.json")
d = json.loads(p.read_text())
print("has_key:", bool(d.get("approval_key")))
print("expires_in_sec:", int(d.get("expires_at_epoch", 0) - time.time()))
print("created_at_age_sec:", int(time.time() - d.get("created_at_epoch", 0)))
PY
```

기대 해석 (KisApprovalManager 정책: 발급 시 expires_in=23.5h, refresh 마진 300초):

- `has_key=True` 필수
- `expires_in_sec > 300` → fresh (정상 운영)
- `0 < expires_in_sec ≤ 300` → 다음 호출에서 자동 갱신 예정 (정상)
- `expires_in_sec ≤ 0` → 만료. 발급 실패 fallback 경로만 의존 중 — 즉시 점검
- `created_at_age_sec ≈ 0 ~ 84600` (≤ 23.5h) 내에서 주기적 갱신이면 정상

**Redis — `latest:index`에 KRX **미포함** 검증 (Stage 1 핵심 invariant):**

```bash
docker compose exec redis redis-cli GET latest:index | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
keys = data.get('keys', [])
krx_keys = [k for k in keys if k.startswith('latest:source:krx:')]
print(f'total keys: {len(keys)}')
print(f'krx keys in index (must be 0): {len(krx_keys)}')
assert len(krx_keys) == 0, 'INVARIANT VIOLATION: KRX in latest:index during Stage 1'
print('OK: latest:index excludes KRX (Stage 1 invariant 유지)')
"
```

**Redis — `latest:source:krx:*` data key 잔존 여부 (참고용, 무관):**

```bash
docker compose exec redis redis-cli KEYS 'latest:source:krx:*'
# Stage 0 → Stage 1 first 진입이라면 아무것도 없음
# Stage 2 → Stage 1 롤백 후라면 잔여 가능 — 무관 (latest:index가 게이트)
```

---

## 24h 관찰 지표 (baseline 수집)

> Stage 1 종료 시점에 PR6d 설계 input으로 사용. 24h 미만 데이터는 weekend/holiday/만기일 등 특수 이벤트 포함 여부 확인 후 해석.

| 지표 | 측정 방법 | 비고 |
| --- | --- | --- |
| insert count / hour | SQL: `source_rates` 시간별 GROUP BY | KRX 영업시간 / 야간세션 / 휴무 차이 baseline |
| last tick age | SQL: `now() - max(timestamp)` 주기적 측정 | 정상 영업시간 중 분 단위 갱신 기대 |
| reconnect count | `[kis_ws] status reconnecting` 로그 카운트 | 시간당 / 세션당 |
| DB write warning count | `[krx_db_writer] DB write failed` 로그 카운트 | 0이 정상, >0이면 원인 분석 |
| KIS approval 이상 | `[kis_approval]` 로그 카운트 (모두 warning) + 캐시 파일 mtime/만료 검증 | 정상 발급은 silent. warning 빈발 = 이상 |
| session boundary 전환 | `[kis_ws] session changed ... → ...` 로그 | 정상 세션 경계에서만 발생해야 함 |

**baseline 수집 쿼리 예 (시간별 insert):**

```sql
SELECT date_trunc('hour', timestamp) AS hour,
       COUNT(*) AS inserts,
       MAX(timestamp) AS last_ts
FROM source_rates
WHERE source = 'krx' AND timestamp >= now() - interval '24 hours'
GROUP BY hour
ORDER BY hour;
```

**`/admin/api/krx-status` 운영 검증 (PR6d-2a, ADR-027):**

PR6d-2a admin endpoint의 분기 동작은 단위 테스트가 `main.py`의 `firebase_admin` + lifespan side effect 회피 어려움으로 skip 처리됨. 운영 배포 후 admin 페이지 직접 호출로 cover:

```bash
# admin auth 적용 (verify_admin dependency 재사용).
# ADMIN_PASSWORD는 운영 .env의 값. 호스트는 운영 도메인 fxi.kr (또는 EC2 직접 접속이면 localhost:8000).
curl -u admin:"$ADMIN_PASSWORD" https://fxi.kr/admin/api/krx-status | jq .

# 분기 검증:
# 1. KRX_FUTURES_ENABLED=false → {"enabled": false, "started": false, "reason": "..."}
# 2. enabled=true + bootstrap 미완 → {"enabled": true, "started": false, "reason": "client 미생성..."}
# 3. enabled=true + client present → {"enabled": true, "started": true, "client": {...metrics}}
```

기대 (active session 중):

- `client.status` = "normal"
- `client.active_session` = "CF" 또는 "CM"
- `client.last_frame_age_sec` < 5 (frame 활발 수신)
- `client.counters.frame_total` 시간에 따라 증가
- `client.counters.status_transitions.stale` = 0 또는 매우 낮음

baseline 수집 명령 (분당 1회 polling 권장 — 24~48h 후 PR6d-2b 진입 데이터):

```bash
# ADMIN_PASSWORD는 .env에서 export 후 사용. 호스트는 운영 도메인 fxi.kr.
while true; do
  curl -s -u admin:"$ADMIN_PASSWORD" https://fxi.kr/admin/api/krx-status \
    | jq '.client | {status, active_session, last_frame_age_sec, frame_count: .counters.frame_total, max_gap: .max_gap_sec.total}'
  sleep 60
done
```

> ⚠️ **active-session gap metric 해석 caveat (2026-05-07 fix)**: `max_*_gap_sec` / `gap_buckets` / `last_*_at`는 **current active session 기준**으로만 의미를 갖는다. 5/7 fix commit 이전 데이터에는 세션 break(2.5h~)가 섞여 9000초대 오염값으로 잡혔다. fix 이후 새 active session subscribe 직후 + 휴장 진입 시 reset되어 깨끗한 baseline. lifetime counter(`frame_count_total`, `status_transition_count`, `reconnect_attempt_count`)는 reset되지 않고 그대로 누적되므로 baseline 해석 시 분리해 본다.

---

## Rollover 정책 (PR6c-2d-1, 2026-05-07)

**User-facing swap point: 만기일 07:00 KST** (거래소 만기 11:30이 아님).

- 만기 직전 영업일 야간장(=만기일 새벽 06:00) 종료 후 swap
- 06:00 야간장 boundary와 분리 (07:00은 휴장 한가운데, KRX 이벤트 없음)
- 08:30 정규장 시작 시 새 contract client 준비 완료 상태
- 사용자 거래 관행 준수 (전문 트레이더는 만기 전 다음 월물로 이동)

**자동 reconcile (5/18 임시 안전모드):**

- APScheduler cron 5분마다 `_reconcile_krx_futures_contract()` 실행
- 동작: resolve → 현재 client contract 비교 → 다르면 shutdown + bootstrap(resolved_override=)
- 안전장치: 만기 차이 > 45일 점프 의심 보류 / resolve 실패 격리 / None 처리
- `KRX_FUTURES_ENABLED=true`일 때만 cron 등록
- **임시 안전모드** — 5/18 통과 후 hybrid (06:01 daily + session boundary)로 축소 검토 (PR6c-2d-5 후보)

**Contract-aware session 판정 (Codex 외부 검토 amend):**

- `get_active_session(now, contract_expiry_date)` — contract.expiry_date를 인자로 받음
- expiring 월물 (today == expiry): 만기일 정규세션 11:30 종료 적용
- next month (today != expiry): 정상 15:45 종료 (rollover 후 11:30~15:45 disconnect 차단)
- legacy 호환: contract_expiry_date=None 시 캘린더 기반 fallback

**예상 timeline (5/18 만기, A75605 → A75606):**

| 시각 | reconcile 동작 | client 상태 |
|---|---|---|
| 5/15 금 17:00 ~ 5/16 토 06:00 | resolve=A75605, no-op | A75605 야간장 운영 |
| 5/16 토 ~ 5/17 일 | resolve=A75605, no-op | client offline (휴장) |
| 5/18 월 06:55 cron | resolve=A75605, no-op | offline |
| **5/18 월 07:00 cron** | **resolve=A75606, rollover 발화** | A75605 → A75606 restart |
| 5/18 월 07:00 ~ 08:30 | A75606 client connect 시도, frame X (휴장) | A75606 idle |
| 5/18 월 08:30 CF 시작 | A75606 정상 | A75606 frames 수신 |

---

## 5/18 만기일 검증 관찰 (자동 rollover 검증용)

**자동 rollover 구현 (PR6c-2d-1) 검증 + 옵션 관찰 항목.**

**핵심 검증 (rollover 동작):**

1. **5/18 월 07:00 ~ 07:05 사이 cron 발화**:
   ```bash
   docker compose logs fastapi --since "2026-05-18T06:55+09:00" | grep -E '\[krx\] (rollover|reconcile)'
   # 기대: [krx] rollover A75605/202605 → A75606/202606 reason=scheduled
   # 기대: [krx] KisFuturesClient 시작 — contract=A75606 expiry=2026-06-15
   ```

2. **5/18 월 08:30 정규장 시작 시 새 contract subscribe 성공**:
   ```bash
   docker compose logs fastapi --since "2026-05-18T08:25+09:00" | grep -E '\[kis_ws\] subscribed'
   # 기대: subscribed tr_id=H0CFCNT0/H0CFASP0 key=A75606
   ```

3. **DB에 새 월물 데이터 적재**:
   ```sql
   SELECT MIN(timestamp), COUNT(*) FROM source_rates 
   WHERE source='krx' AND timestamp >= '2026-05-18 00:00:00';
   -- 기대: 첫 row가 5/18 08:30 직후 + 행 수 정상 증가
   ```

**옵션 관찰 (KIS 측 동작 패턴):**

4. **A75605 (옛 만기) silence 패턴** (수동, 별도 ad-hoc 스크립트 권장):
   - 5/18 07:00 swap 이후에도 별도 client로 A75605 subscribe 유지하면 어떤 frame 패턴인가
   - 11:30 만기 종료 시점의 끊김 / disconnect / reconnect 시도 패턴
   - 본 운영 client에는 영향 없음 (이미 A75606 운영 중)

5. **KIS master file `mmsc_cls_code` 변경 timing**:
   - 5/18 매시간 master 다운로드 → A75605 존재 / A75606 mmsc_cls_code 추적
   - 가설 검증: (a) 11:30 직후 즉시 / (b) 익일 갱신 / (c) 며칠 후 batch
   - 별도 ad-hoc 스크립트 (운영 코드 contamination 0)

6. **REST inquire-price 응답 변화 timing**:
   - A75605에 대한 KIS REST 호출 응답 형식 변화 시점 추적
   - 2026-05-07 사전 검증 (A75604 만기 17일 후 + master 제거 상태): rt_cd=0이어도 `futs_prpr` 부재, `bstp_*` (지수형) keys 응답. helper의 `futs_prpr/prpr 부재` 검사가 None 차단으로 안전.
   - 5/18 11:30 직후 A75605에 대한 응답 변화 시점 측정 (만기 정확한 시점 vs master 제거 시점 분리)

**수집된 데이터로 후속 결정:**

- PR6c-2d-5 (hybrid scheduler 전환): 5/18 swap이 5분 cron으로 안전하게 통과 확인 시 06:01 + boundary 기반으로 축소
- PR6d-2b (REST fallback): stale 임계값, session-end grace, reconnect 동반 조건, cooldown
- 점프 보호 임계값 (현재 45일) 적정성

---

## Rollback 절차

**즉시 격리 (KRX 장애 / KIS API 점검 / 데이터 이상):**

```bash
# 운영 .env에서 KRX_FUTURES_ENABLED=false로 변경
# (.env.example 참고)
docker compose up -d fastapi
```

**검증:**

```bash
docker compose logs fastapi --since 2m | grep -E '\[krx\]'
# 기대: "[krx] KRX_FUTURES_ENABLED=false, skip start" 1줄
```

**효과 (process 재생성 후 기준):**

- 재생성 후 신규 KRX tick 수집 / DB insert 중단 (lifecycle 미시작)
- 재생성 전까지는 기존 client가 계속 동작 — `.env` 변경만으로 즉시 멈추지 않음 (config 1회 read + Docker Compose env_file 재로드 정책)
- 기존 `source_rates` 레코드는 유지 (수동 정리 필요 시 별도)
- broadcast / latest:index는 이미 Stage 1에서 KRX 미포함 → 사용자 가시 영향 0

---

## Stage 2 진입 조건 (체크리스트)

> ⚠️ **Stage 2 의미 재정의 (2026-05-06, [ADR-028](DECISIONS.md))**: Stage 2 = **topic 채널 발사** (legacy `rates` 배열 노출 X). KRX는 새 topic protocol로만 노출. `KRX_BROADCAST_INCLUDE=true`는 legacy `rates` 통합이 아니라 topic 발사 트리거로 재해석 (코드 분리는 Phase Z-2 영역). 단순 토글로 legacy `rates`에 KRX 노출은 새 계약 위반.

진입 **전** 모두 충족되어야 함:

- [ ] Stage 1을 24h+ 운영, 위 baseline 지표 수집 완료
- [ ] 5/18 만기 관찰 데이터 확보 (위 시나리오 1~3)
- [ ] PR6d (REST snapshot/fallback) 구현 완료 — topic 노출 시 stale 방지 안전장치 필수
- [ ] PR6c-2d (자동 rollover) 또는 동등 운영 절차(수동 restart cron) 결정 완료
- [ ] **topic protocol v1 도입 완료 (Phase Z-2 영역)** — WebSocket subscribe + topic delta dispatch 인프라
- [ ] **ADR-028 (Topic-only Tether/KRX + legacy FX dual-emit) 결정 적용 완료**
- [ ] iOS/Android 클라이언트가 새 topic protocol에서 `source="krx", asset="usd-krw-futures"` 처리 검증 완료
- [ ] ADR-027 (REST fallback 정책) 작성 완료

---

## PR6d-2b Stage A/B baseline 기록 (2026-05-08 ~ 5/9)

> ⚠️ **운영 판단 근거 — Stage B 활성화 결정 시점에 반드시 검토.**
> 5/8 단일 일자 baseline으로 임계값 확정하지 말 것 (5/9 데이터로 가설 깨짐).

### 5/8 vs 5/9 비교

| 지표 | 5/8 (금) 새벽 | 5/9 (토) 새벽 | 변동 |
|---|---|---|---|
| stale_transitions | 4 | **14** | 3.5× |
| max_total_gap | 76.1s | **278.4s** | 3.6× |
| max_quote_gap | 53s | **278.4s** | 5.3× (호가 dead) |
| max_trade_gap | 355s | 445.3s | 1.3× |
| fb_grace | (n/a Stage A 미배포) | 3 | grace 안 (-40min) 차단 |
| fb_below_th | (n/a) | 19 | 짧은 stale (<120s) 차단 |
| fb_disabled | (n/a) | **4** | **Stage B 활성화 시 REST 후보** |
| fb_eligible | (n/a) | 0 | env=false라 0 |
| REST 실제 호출 | 0 | 0 | env=false 정상 |
| reconnect | 0 | 0 | 네트워크 장애 X |

5/8 단일 일자에서 stale 4건 모두 -32min 이내 cluster → 40min grace로 cover. 그러나 **5/9는 distinct stale event 14건이 -213min ~ -35min 광범위 분포** (가장 이른 stale 02:26:52). max_quote_gap 278.4s는 호가도 dead (5/8의 53s 대비 5×).

**fb_* counter는 evaluation count (event count 아님)** — Codex 권고 명시:
- fallback evaluation 총 26회 (stale event 14건 + 일부 stale이 60s+ 지속 시 transition + summary cycle 중복 평가)
- counter 분포: `fb_grace=3` / `fb_below_th=19` / `fb_disabled=4`
- distinct event 기준 grace window(-40min ~ 종료) 안 stale은 05:23:29, 05:24:54 **2건** (counter `fb_grace=3`은 그 이벤트들이 transition + summary cycle 평가에 분산된 결과)
- env=false라 실제 REST 호출 0

**핵심 함의** (Codex 분석): "종료 1시간 반 전부터 조용했다"가 아니라 "**종료 3시간 반 전부터 조용했다**". grace 확장(40 → 240min)으로는 전 야간 차단이라 fallback 의미 X. **토요일 새벽 저유동성 전반 패턴** — session 타입별 정책 또는 Stage B 활성화 자체 신중함이 필요.

### fb_* counter 운영 해석 가이드

운영자가 `/admin/api/krx-status` 또는 docker logs에서 grep할 때 의미:

| Counter | 의미 | 운영 해석 |
|---|---|---|
| `fb_eval` | 총 evaluation 횟수 | stale 발생 시점 + summary cycle 60s 1회 |
| `fb_grace` | session-end -40min 안 차단 | **자연 silence 후보** — 정상 거래 끝물 |
| `fb_below_th` | frame_age < 120s 차단 | **짧은 stale** — REST 호출 부적절 |
| `fb_cooldown` | 직전 fallback 후 30s 안 | 폭주 방지 |
| `fb_disabled` | 다른 조건 통과 + env=false | **Stage B 켰다면 REST 호출됐을 진짜 후보** ⭐ |
| `fb_eligible` | env=true + 모든 조건 통과 | Stage B 활성 시 REST 호출 task 생성 후보 |
| `rest_success` | REST 호출 성공 (Stage B+) | |
| `rest_error` | REST 호출 실패 (Stage B+) | |

**판단 예시** (Stage A 운영 중):
- `fb_disabled` 매일 0~2건: env=true 시 noise 적음 → Stage B 활성화 가능
- `fb_disabled` 일별 변동성 큼 (5/9 같이 4건 vs 5/8 0건): 추가 누적 후 결정
- `fb_eligible` (Stage B 활성 시) > rest_success: rest_error 원인 확인 (HTTP / 토큰 / 응답 형식 / 네트워크)

### Stage B 활성화 조건 (체크리스트)

Stage B (`KRX_REST_FALLBACK_ENABLED=true`) 진입 전 모두 충족:

- [ ] **5/12 ~ 5/14 평일 데이터 최소 2-3일 누적** (single-day 결정 금지)
- [ ] `fb_disabled` 일별 평균 < 5건 (운영 noise 부담 작음)
- [ ] `fb_disabled` 발생 시 `quote_age` 동반 (호가도 dead — 진짜 fallback 가치 있는 case)
- [ ] `reconnect_attempts` 0 또는 일관 (네트워크 장애 별도 분기 필요 시 spec 추가)
- [ ] env 변경은 별도 GO (docker compose down/up 필요 — process restart로 env 반영)
- [ ] 활성화 후 24h 관찰 → `rest_success` / `rest_error` 분포 확인 → 임계값 튜닝

### 교훈 (메타 lesson)

1. **single-day baseline 금지**: 5/8만 보면 grace 40min 충분이라 단정 가능. 5/9에서 가설 깨짐. multi-day 누적 필수.
2. **요일/거래일 특성 다름**: 5/9 (토 새벽) = 금 야간장 마지막 + 저거래량. 평일 새벽과 패턴 다를 수 있음.
3. **Codex incremental 접근 정당화**: Stage A (env=false + telemetry only)로 위험 0 검증. Stage B 코드 골격도 env=false 유지로 활성화 timing 별도 GO. 보수적 단계가 5/9 데이터로 입증됨.
4. **fb_disabled 가치**: 단순 "env=false 차단 카운터"가 아니라 **"Stage B 활성화 결정 자료"**. 운영 logs에서 매일 추적해야 함.

---

## PR6d로 넘길 결정 항목 (Stage 1 데이터 기반)

Stage 1 24h+ 데이터로 결정해야 할 정책 파라미터:

1. **REST snapshot 호출 주기**: WebSocket 정상 시 0회 (zero-cost) vs 백업 주기 (예: 분당 1회)
2. **Staleness 임계값**: WebSocket tick 수신 후 N초 무응답 시 REST fallback trigger
3. **REST 호출 실패 시 정책**: latest:index에서 KRX 제외 vs 마지막 값 유지 + stale flag
4. **만기일 자동 rollover trigger**:
   - 옵션 A: cron (예: 만기일 11:30:30 KST resolve + restart client)
   - 옵션 B: WebSocket session boundary 감지 시 resolve 재시도
   - 옵션 C: 수동 restart 절차만 (자동화 X) — 운영 부담 감수
5. **ADR-027 작성**: REST fallback + rollover 정책 의사결정 기록
