# KRX 미국달러선물 (KIS Open API) Canary Runbook

> 목적: KRX optional source(PR6 시리즈)의 단계적 운영 진입 절차. 운영자가 그대로 따라할 수 있는 runbook 형태.
>
> 관련 문서: [CLAUDE.md "KRX 미국달러선물" 섹션](CLAUDE.md), [.env.example](.env.example), [DECISIONS.md](DECISIONS.md), [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md)
>
> ⚠️ **Historical note (Z-2d cleanup 2026-05-12)**: 본 runbook의 `KRX_BROADCAST_INCLUDE` 참조는
> historical context. 해당 env는 Z-2d cleanup에서 제거됨 — legacy 노출은
> `app/legacy_policy.should_include_source_in_legacy_rates` allowlist가 단일 진실 소스.
> KRX는 allowlist 미포함이라 자동 차단. Stage 2 topic 노출은 별도 topic protocol(ADR-028).

## 🚀 Close finalizer 2차 작업 배포 status (2026-05-17)

| 항목 | 상태 |
| --- | --- |
| Commit | `c2fb796` (Stage 5) — 누적: 68b8702 ~ c2fb796 (Stage 1-5) |
| EC2 deploy | 2026-05-17 17:07 KST |
| Plan | [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md) §5 (2차 작업) |
| Env toggle | `KRX_CLOSE_FINALIZER_ENABLED=true` (default) — `false` 시 1차 PR (c0855ff) 동작 rollback (Rollback B 참조) |
| 검증 | 250 passed + 1 skipped |
| 첫 실측 결과 | ✅ **2026-05-18 (월) 15:45 KST CF close 통과** + ✅ **2026-05-19 (화) 06:00 KST CM close 통과** (둘 다 WS-first path, REST fallback skip). 상세는 §"2026-05-18~19 만기 첫 실측 결과". |
| Rollback | 본 문서 [Rollback 절차](#rollback-절차) Rollback B (`KRX_CLOSE_FINALIZER_ENABLED=false`) |

---

## 핵심 원칙 (반드시 인지)

1. **KRX optional source**: KRX 수집/노출 어느 단계의 실패도 baseline 서비스(은행 + investing + USDT) 가용성에 영향 0. 실패 격리는 env 토글 + bootstrap try/except로 보장.
2. **Stage 1은 "DB 저장 관찰" 목적**: broadcast 노출 X. 앱/사용자 가시 영향 0.
3. **실행 중 contract 자동 교체 구현됨** (PR6c-2d-1, 2026-05-07). APScheduler 5분 cron `_reconcile_krx_futures_contract`이 KIS 상품 마스터로 active contract resolve 후 현재 client contract와 비교 → 다르면 shutdown + start으로 자연 swap (만기일 07:00 KST 선제 전환 정책 포함). bootstrap 시 1회 resolve는 그대로 유지.
4. **5/18 만기일 관찰 = 자동 rollover 첫 실측**: 07:00 swap 정상 발화 / 08:45 새 월물 정규장 시작 / 11:30 만기 시점 expiring contract WS frame 패턴 / 15:45 CF close finalizer 첫 실측.
5. **두 토글 모두 process 재생성 필요**: `app/config.py`가 import 시 1회 `os.getenv` 읽기 → hot-reload 안 됨. `.env` 변경 후에는 `docker compose up -d --force-recreate fastapi`처럼 컨테이너를 재생성해야 env_file이 다시 반영된다 (`docker compose restart fastapi`는 기존 컨테이너 stop/start라 `.env` 변경을 반영하지 않음).

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
   - 별도 ad-hoc 스크립트 (운영 코드 contamination 0): `scripts/observe_kis_master.py`

   **Runbook (`scripts/observe_kis_master.py`, v1 — PR6c-2d-3 follow-up, 2026-05-08):**

   ```bash
   # 1회 snapshot — 성공 시 표준출력 1줄 JSONL.
   # fetch INFO 로그는 observe_kis_master.py가 suppress (cron append 오염 방지).
   docker compose exec -T fastapi python scripts/observe_kis_master.py
   # → {"timestamp": "2026-05-18T07:30:00+09:00",
   #    "contracts": [{"short_code": "A75605", "name": "미국달러 F 202605",
   #                   "contract_month": "202605", "mmsc_cls_code": "..."}, ...]}

   # 특정 short_code만 필터 (반복 허용)
   docker compose exec -T fastapi python scripts/observe_kis_master.py \
     --short-code A75605 --short-code A75606

   # JSONL append 누적 (manual 또는 cron)
   docker compose exec -T fastapi python scripts/observe_kis_master.py \
     >> /tmp/krx_master_obs.jsonl 2>> /tmp/krx_master_obs.err
   ```

   **5/18 관찰 cron 등록 (선택, host crontab — `crontab -e`):**

   ```cron
   # 5/18(월) 07:05~23:05 매시간 05분 (cron 첫 필드는 분 단위)
   5 7-23 18 5 * cd /home/ubuntu/exchange-rate && docker compose exec -T fastapi python scripts/observe_kis_master.py >> /tmp/krx_master_obs.jsonl 2>> /tmp/krx_master_obs.err
   # 5/19(화)~5/22(금) 매시간 05분 — master batch 갱신 가설 (b)/(c) 추적용
   5 * 19-22 5 * cd /home/ubuntu/exchange-rate && docker compose exec -T fastapi python scripts/observe_kis_master.py >> /tmp/krx_master_obs.jsonl 2>> /tmp/krx_master_obs.err
   ```

   **분석 (jq로 timeline 추출):**

   ```bash
   # A75605/A75606 mmsc_cls_code 변화 timeline
   jq -r '
     .timestamp as $t
     | .contracts[]
     | "\($t)\t\(.short_code)\t\(.contract_month)\t\(.mmsc_cls_code)"
   ' /tmp/krx_master_obs.jsonl | column -t -s $'\t'

   # A75605가 master에서 사라진 첫 timestamp (가설 (a)/(b)/(c) 판정용)
   jq -r '
     select((.contracts | map(.short_code) | index("A75605")) == null)
     | .timestamp
   ' /tmp/krx_master_obs.jsonl | head -1
   ```

   **v1 범위 — 격리 보장:**
   - master file 다운로드 + USD futures 메타데이터 JSONL snapshot만
   - 운영 ContractInfo / 운영 lifecycle / DB 저장 변경 X
   - 성공 path는 stdout 1줄 JSONL. fetch 실패는 exit 1 + stderr 출력
   - `app.sources.kis_master` INFO 로그는 suppress해 cron append JSONL 오염 차단 (콘솔 핸들러는 stdout이지만 fetch 정상 path에서 INFO 이상 로그 미발생)
   - mmsc_cls_code offset은 `app/sources/kis_master.extract_commodity_future_master_observation` 재사용 (드리프트 차단)

6. **REST inquire-price 응답 변화 timing**:
   - A75605에 대한 KIS REST 호출 응답 형식 변화 시점 추적
   - 2026-05-07 사전 검증 (A75604 만기 17일 후 + master 제거 상태): rt_cd=0이어도 `futs_prpr` 부재, `bstp_*` (지수형) keys 응답. helper의 `futs_prpr/prpr 부재` 검사가 None 차단으로 안전.
   - 5/18 11:30 직후 A75605에 대한 응답 변화 시점 측정 (만기 정확한 시점 vs master 제거 시점 분리)

**수집된 데이터로 후속 결정:**

- PR6c-2d-5 (hybrid scheduler 전환): 5/18 swap이 5분 cron으로 안전하게 통과 확인 시 06:01 + boundary 기반으로 축소
- PR6d-2b (REST fallback): stale 임계값, session-end grace, reconnect 동반 조건, cooldown
- 점프 보호 임계값 (현재 45일) 적정성

### Close finalizer 첫 실측 체크리스트 (c2fb796 deploy 후, 2026-05-18 ~ 5/19)

**2026-05-18 (월) 15:45 KST — CF close 첫 실측**:

```bash
# Redis latest 갱신 확인 (KrxCloseWindowWriter → Redis SET)
docker exec exchange-rate-app python -c "
from app import latest_rates_cache
print(latest_rates_cache.get_latest_krx_rate_from_sync_job('usd-krw-futures'))
"
# 기대: timestamp이 2026-05-18T15:45:0X+09:00 (boundary 직후)

# Redis captured flag SET 확인
docker compose exec redis redis-cli GET 'close_captured:krx:CF:2026-05-18'
# 기대: "1" (DB+Redis 모두 성공 시 SET)

# DB row 확인 (unconditional INSERT)
docker exec exchange-rate-app python -c "
from app.database import SessionLocal
from sqlalchemy import text
with SessionLocal() as s:
    rows = s.execute(text(\"\"\"
        SELECT timestamp, rate FROM source_rates
        WHERE source='krx' AND timestamp >= '2026-05-18 06:45:00'
          AND timestamp < '2026-05-18 06:46:00'
    \"\"\")).fetchall()
    print(rows)
"
# 기대: 1+ row, timestamp 15:45:0X KST (UTC 06:45:0X)

# 로그 확인
docker compose logs fastapi --since "2026-05-18T15:44+09:00" --until "2026-05-18T15:47+09:00" \
  | grep -E '\[krx_close_window\]'
# 기대: "close saved session=CF rate=... ts=2026-05-18T15:45:0X+09:00 ..."
```

**2026-05-19 (화) 06:00 KST — CM close 첫 실측**: 동일 패턴, `CF` → `CM`, date `2026-05-19`.

**Telemetry counter (5종) — `KrxCloseFinalizerStats` in-memory singleton**:

`get_krx_close_finalizer_stats()`는 uvicorn 메인 프로세스의 module-level singleton이라
`docker exec ... python -c` 같은 **새 Python 프로세스에서는 조회 불가** (별 import → 초기값 0만 보여 운영자 오도).

> 📌 **2026-05-25 추가 (`6a43785` 정책 PR)**: 신규 `rest_write_blocked` counter는 본 5종과 **다른 owner** — `KrxCloseSnapshotController.counters` dict (controller instance attribute). 운영 dashboard / 조회 스크립트 작성 시 두 owner 분리 인식 필수. 상세: [KRX_CLOSE_SNAPSHOT_PLAN.md §5.5](KRX_CLOSE_SNAPSHOT_PLAN.md).

현재 외부 조회 path 없음 — 1차 관찰은 다음 indirect signal로:

- **logs**: `[krx_close_window] close saved session=... rate=... ts=...` 발화 횟수 (case A signal)
- **logs**: `[krx_close_snapshot] WS captured ... → REST skip` vs close snapshot REST fallback 실제 호출 (stale fallback `KRX_REST_FALLBACK_ENABLED`와 별도 toggle — close snapshot은 `KRX_CLOSE_FINALIZER_ENABLED=true` 시 captured flag 부재일 때만 최대 1회 REST 호출, false 시 1차 PR 3 retry) (case B signal)
- **logs**: `[krx_close_window] close grace window 안 N frames detected` (close_duplicate_count alert)
- **DB**: 영업일 close window 시간(15:45:00~15:45:59 / 06:00:00~06:00:59) `source_rates` row 존재 여부
- **Redis**: 영업일 `close_captured:krx:{CF|CM}:{kst_date}` flag SET 여부

**후속 polish (3차 PR scope 후보)**:

- admin API endpoint (`/admin/api/krx/close-finalizer-stats`) — 외부 조회 가능 path
- 또는 Redis hash telemetry (`topic:krx:close_finalizer:stats`) — process restart 시 누적 유지

**Case A/B/C 분포 분석** (7일 telemetry 측정 후, 2026-05-26 무렵):

- case A (우리 cutoff fix): 영업일 close 시점 직후 `close saved` log 발화 + DB row + Redis flag 정상
- **case B (KIS WS 미송신, 2026-05-25 정책 PR `6a43785` 이후 갱신)**: close saved log 없음 + REST fallback 발화 (`close_rest_fallback_used` +1) → **DB/Redis write 차단** (`KRX_CLOSE_REST_WRITE_ENABLED=false` default, `rest_write_blocked` +1) → Redis latest는 직전 정상 영업일 종가 유지. REST 응답은 diagnostic only, write source 아님. **[2026-06-10 amendment: flag=true 활성화(6/15 rollover 후) 이후엔 gate 통과 시 실제 write — §5.7.8]**
- case C (dedup skip): 2차 작업 정책상 0 예상

case B 비율 + `rest_write_blocked` 분포 결과로 3차 PR scope 결정 (REST close fallback 코드 완전 제거 vs 검증 가능성 재검토). 상세 정책 근거는 [DECISIONS.md ADR-027 follow-up](DECISIONS.md) + [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md) 참조.

### 2026-05-18~19 만기 첫 실측 결과 (CF/CM 통과, master 가설 b 확정)

> 📅 **실측일**: 2026-05-18 (월) ~ 2026-05-19 (화)
> 🏷️ **상태**: CF close finalizer + CM close finalizer + 만기일 07:00 자동 rollover + post-close A75605 관찰 + KIS master batch 익일 갱신 모두 통과/확정. close finalizer 양 session 첫 실측 완료.

#### 확정 결론 — 운영 정책 input

1. **07:00 자체 rollover (PR6c-2d-1) 정책 타당** — 07:01:47 KST `[krx] rollover A75605/202605 → A75606/202606 reason=scheduled` 발화. A75606 (만기 2026-06-15) 운영 client 자동 재시작. 08:30 CF 정규세션 진입 시 H0CFCNT0/H0CFASP0 SUBSCRIBE SUCCESS.
2. **KIS master batch 갱신 = 가설 (b) 확정 — 익일 5/19 07:00~08:00 KST window**:
   - 5/18 17:05까지: A75605 `mmsc_cls_code=1` 유지
   - 5/19 07:00: A75605 `cls=1` / A75606 `cls=2` (만기 다음날 새벽까지 옛 월물 잔존)
   - **5/19 08:00 snapshot부터**: A75605 제거, A75606 `cls=1` 승급, A75607 `cls=2` 신규 부각
   - 즉시 제거 가설 (a) 부정 / 익일 갱신 가설 (b) 확정 (5/22까지 추가 sample은 선택).
3. **KIS REST는 만기 후에도 A75605에 대해 `rt_cd=0, futs_prpr=1505.800` stale 반환** (관찰 구간 11:31~12:10 동안 가격 + `acml_vol=7846` 고정. 12:10 이후 REST 미관찰). 응답 형식 자체는 정상 → 단순 `rt_cd == "0"` 분기로는 stale 인지 불가.
4. **active contract 판단은 KIS master/REST 응답에 의존하면 안 됨** — 자체 calendar/expiry 정책 필요. PR6c-2d-1 만기일 07:00 KST swap이 이 위험을 정확히 회피.
5. **CF 15:45 close finalizer (c2fb796 Stage 5) WS-first path 성공, REST fallback skip** — 첫 실측 통과. log: `[krx_close_window] close saved session=CF rate=1496.5 ts=2026-05-18T15:45:00+09:00` + `[krx_close_snapshot] WS captured at entry → REST skip`.
6. **CM 06:00 close finalizer (c2fb796 Stage 5) WS-first path 성공, REST fallback skip** — 5/19 06:00:00 KST 첫 실측 통과. log: `[krx_close_window] close saved session=CM rate=1490.0 ts=2026-05-19T06:00:00+09:00` (06:01:00.151) + `[krx_close_snapshot] WS captured at entry → REST skip` (06:01:03.736). DB row: `id=293030 rate=1490.0 timestamp=2026-05-18 21:00:00 UTC` (= 2026-05-19 06:00:00 KST). F1/F2/F3 race 위험은 본 케이스에서 노출되지 않음 (CF/CM 양 session 단발 성공으로 일반 해소 단정 X — 7일 telemetry 누적 후 확정).
7. **frame 수신 시각 추정 근거** — `[kis_ws] metrics` 06:00:50.119 시점 `trade_age=49 quote_age=49` → 마지막 trade/quote frame ~06:00:01 KST 수신 추정 (sec 단위). DB/Redis에는 received_at 미저장 — `event_at_kst = market_time payload (060000)` 우선 → boundary 06:00:00 KST 저장. WS-first path와 REST fallback이 동일 boundary timestamp semantics.
8. **만기월 A75605 WS post-close 관찰** (15:48~16:02 별도 observer):
   - SUBSCRIBE SUCCESS (H0CFCNT0/H0CFASP0 `rt_cd=0 OPSP0000`)
   - 14분간 trade/quote frame 0건
   - 16:02:01 ConnectionClosedError (KIS가 idle close)
   - → 만기/휴장 후 subscribe 자체는 허용, frame은 미송신
9. **운영 중 만기월 WS subscribe는 `OPSP8996 ALREADY IN USE appkey`로 거부** — KIS는 같은 appkey 동시 connection 1개만 허용 (운영 client가 A75606 사용 중). 다음 만기 옛 월물 실시간 관찰을 원하면 별도 appkey 또는 단일 connection multi-contract subscribe 구조 필요.

#### KRX 단일가 메커니즘 — 5/18 데이터로 검증

세 시점 모두 동일 패턴 (10분 단일가 호가 접수 → 단일가 체결):

| 시점 | 메커니즘 | 호가 접수 구간 | 단일가 체결 |
|---|---|---|---|
| **만기 CF 종가** (A75605) | 옛 월물 만기 처리 | 11:20:06~11:29:06 (10분, `acml_vol=7816` 고정) | **11:30:0X — 1505.800원 × 30계약** (`acml_vol` 7816 → 7846, +30) |
| **정규 CF 종가** (A75606) | 새 월물 정규장 종료 | 15:35:49~15:44:49 (10분, `trade_age` 50→590s 증가) | **15:45:01 — 1496.5원** (운영 close finalizer capture) |
| **야간 CM 시가** (A75606) | 새 월물 야간장 시작 | 17:50:01~17:59:49 (10분, `trade_age=None`) | **18:00:01 — 1494.7원** (DB row 18:00:01 KST `rate=1494.7`, `frames/min` 53→444 폭증) |
| **야간 CM 종가** (A75606) | 새 월물 야간장 종료 (5/19 새벽) | 05:50:0X~05:59:5X (~10분) | **06:00:00 — 1490.0원** (WS-first path, REST fallback skip, DB row id=293030, timestamp=2026-05-18 21:00:00 UTC (= KST 2026-05-19 06:00:00), Redis "2026-05-19T06:00:00+09:00") |

→ 사용자 도메인 지식 (10분 호가 접수 + 단일가 체결) 실측과 4시점 모두 일치 (만기 CF 종가 / 정규 CF 종가 / 야간 CM 시가 / 야간 CM 종가). c2fb796 Stage 5 close finalizer는 CF/CM 양 session 정각 boundary timestamp 저장 정책 (WS path는 KIS payload `market_time` 우선, REST path는 `boundary_at_kst` 고정) 검증 완료.

#### 운영 교훈 — observer 도구 작업 중 발견 (재사용 가치)

1. **EC2 host cron은 UTC 기준**: KST 시각을 cron에 그대로 넣으면 9시간 밀림. KST → UTC 변환 필수 (예: KST 11:00 → UTC 02:00 → `0 2 18 5 *`).
2. **docker cp로 넣은 observer script는 컨테이너 recreate 시 사라짐**: docker-compose volume mount에 `./scripts` 미포함. 임시 관찰 도구는 docker cp 적절하나 영구 도구는 image rebuild + commit 필요.
3. **host `/tmp/`와 container `/tmp/` 결과 파일 분리**: cron의 `>>` redirect는 host context, 스크립트 내부 `Path("/tmp/...")`는 container context. 결과 수집 시 둘 다 확인 필수 (`docker compose exec -T fastapi cat ...`).
4. **KIS WS appkey 동시 connection 제한** (`OPSP8996 ALREADY IN USE appkey`): 같은 appkey로 2 connection 시도 → 두 번째 거부. 다음 만기에서 옛 월물 실시간 관찰 시 별도 appkey 또는 단일 connection multi-contract subscribe 구조 필요.
5. **Approval cache (`.cache/kis_ws_approval.json`)는 24h 유효, contract와 무관**: 운영 client가 한 번 발급한 cache를 observer가 read-only로 재사용 가능 (해당 session 활성 동안).
6. **KIS REST access_token은 `KRX_REST_FALLBACK_ENABLED=false`에서 운영이 발급 안 함**: observer가 자체 발급해 별도 cache (`/tmp/krx_expiry_obs/.observer_token.json`)로 격리 필요.
7. **Secret 마스킹**: `env | grep KIS`류 명령은 secret 노출 위험. CLAUDE.md global "운영 안전성 / 보안 원칙"의 `sed` 마스킹 명령 사용 필수.

#### Observer 스크립트 — 진단 도구

```text
scripts/observe_kis_master.py  # KIS 상품 마스터 fetch (secret 무관, public URL) — 운영 진단 도구로 영구 유지
```

`observe_kis_rest.py` / `observe_kis_ws_old.py`는 5/18 만기 1회 실측 후 정리 (2026-05-19 commit). 필요 시 git history (`b7c8b6a`)에서 복구 가능하나, 다음 만기에는 다음 조건이 충족되기 전엔 재사용 가치 낮음:

- REST observer: 다음 만기에서 stale 응답 패턴 재확인 가치는 2~3회 sample만 필요 (정책 판단은 ADR-027에서 완료) → ad-hoc 1회 호출로 충분
- WS old observer: same appkey `OPSP8996` 제한으로 그대로 재사용 무가치. 별도 appkey 또는 단일 connection multi-contract subscribe 구조 만들기 전엔 재실행해도 같은 결과

`observe_kis_master.py`는 만기 관찰이 아니라 KIS master public endpoint 진단 도구로 보고 유지. 운영 중 master 이상 (URL 변경 / 새 contract 추가 / `mmsc_cls_code` 형식 변경) 시 활용.

#### Pending — 추가 관찰 항목

- **5/19~5/26 close finalizer 7일 telemetry** — case A (WS-first 성공) / case B (WS-first 실패, REST diagnostic + `rest_write_blocked` write 차단 — 2026-05-25 정책 PR `6a43785` 이후) / case C (둘 다 실패) 분포 측정. CF/CM 첫 실측 모두 case A. 상세 status는 [KRX_CLOSE_SNAPSHOT_PLAN.md §0](KRX_CLOSE_SNAPSHOT_PLAN.md) 추적.

> **다음 만기 (6/18) 별도 observer는 불필요로 결정 (2026-05-19)**. 5/18 첫 실측으로 운영 정책 input 모두 확정 — 만기일 rollover (PR6c-2d-1) 정상 / KIS master 익일 batch 갱신 / REST stale 정상 응답 가능 / 자체 calendar 기반 active contract 판단 / CF/CM close finalizer WS-first 성공. 다음 만기는 운영 로그/DB row만 사후 확인 ([KRX_CLOSE_SNAPSHOT_PLAN.md §0](KRX_CLOSE_SNAPSHOT_PLAN.md) telemetry). master batch 재확인이 꼭 필요하면 `observe_kis_master.py` ad-hoc 1회 호출로 충분.

### 2026-05-25 휴장일 사고 + 대응 (4단계 완료)

> 📅 **실측일**: 2026-05-25 (월) — 부처님오신날(5/24 일요일) 대체공휴일
> 🏷️ **상태**: 4단계 대응 land 완료 (`170380f` hotfix + `6a43785` 정책 PR)
> 📋 **상세 정책 근거**: [DECISIONS.md ADR-027 follow-up](DECISIONS.md) + [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md)

#### 사고 요약

2026-05-25 (월)은 한국 부처님오신날 대체공휴일로 KRX 휴장. 그러나 `KRX_2026_KNOWN_HOLIDAYS`에 2026-05-25 미등록 → `is_krx_business_day(2026-05-25)=True` → `is_close_snapshot_eligible("CF", 2026-05-25)=True` → 15:45 KST CF close snapshot REST fallback 발동.

KIS REST가 5/22 (금) stale 종가(rate=1516.8)를 `rt_cd=0` 정상 응답으로 반환 → DB row id=429785 + Redis latest를 `2026-05-25T15:45:00+09:00` timestamp로 잘못 기록 → 단말 노출.

**근본 진단**: KIS REST는 휴장/만기 후에도 stale 응답을 정상 형식으로 반환 (5/19 만기 + 5/25 휴장 두 번 확인). 자동 코드로 stale 인지 불가 → REST 응답만으로 "오늘 종가"임을 증명할 수 없음 → write source로 부적합.

#### 4단계 대응 (순서 기반, 시각은 commit 시각만 명시)

**1. Production read-only 식별**

```bash
docker exec exchange-rate-app python -c '
from app.database import get_db_context
from app import models
with get_db_context() as db:
    rows = db.query(models.SourceRate).filter(
        models.SourceRate.source == "krx",
        models.SourceRate.timestamp.between("2026-05-25 06:00", "2026-05-25 07:30"),
    ).all()
    for r in rows:
        print(r.id, r.rate, r.timestamp)
'
```

식별 결과:

- **잘못된 row**: `id=429785, rate=1516.8, ts_utc=2026-05-25 06:45:00` (= KST 15:45)
- **직전 정상 row**: `id=390427, rate=1519.9, ts_utc=2026-05-22 21:00:00` (= KST 2026-05-23 06:00, 5/22 야간 CM close)
- **Redis latest**: `{"rate": 1516.8, "timestamp": "2026-05-25T15:45:00+09:00", "mirrored_at": "2026-05-25T15:46:04+09:00"}` (잘못된 상태)
- **close_captured flag**: CF/CM 모두 미존재 (ttl=-2)

**2. 운영 데이터 보정**

```bash
docker exec exchange-rate-app python -c '
from app.database import get_db_context
from app import models
from app.latest_rates_cache import _get_sync_client, latest_key_source
from datetime import datetime, timezone, timedelta
import json
KST = timezone(timedelta(hours=9))
now_kst = datetime.now(KST).isoformat()

with get_db_context() as db:
    row = db.query(models.SourceRate).filter(models.SourceRate.id == 429785).first()
    db.delete(row); db.commit()

client = _get_sync_client()
client.set(
    latest_key_source("krx", "usd-krw-futures"),
    json.dumps({
        "rate": 1519.9,
        "timestamp": "2026-05-23T06:00:00+09:00",
        "mirrored_at": now_kst,
    }, ensure_ascii=False),
)
'
```

결과 — DB row id=429785 삭제 + Redis latest를 직전 정상값으로 복구 (mirrored_at = 보정 시각).

**3. Hotfix (`170380f`)**

- `app/sources/kis_futures.py` — `KRX_2026_KNOWN_HOLIDAYS`에 `date(2026, 5, 25)` 추가 (kwatch.kr/markets/kr/trading-days cross-check)
- 5/25 CF skip + 5/26 06:00 CM skip 회귀 잠금 테스트 (mock 없이 실제 캘린더 entry 기반)
- 32 tests passed → push → EC2 build + force-recreate → production sanity 검증

**5/26 06:00 KST CM 재발 차단 확정**: production container에서 `is_close_snapshot_eligible("CM", date(2026, 5, 26))=False` (today-1=2026-05-25 business day check).

**4. 정책 PR (`6a43785`) — KRX_CLOSE_REST_WRITE_ENABLED=false default**

- 새 env `KRX_CLOSE_REST_WRITE_ENABLED` (default `false`)
- `KrxCloseSnapshotController._sync_write` 안 sanity check 통과 후 DB/Redis write 직전 분기 + retry short-circuit (같은 stale 값 3회 확인 무의미)
- `KRX_CLOSE_FINALIZER_ENABLED` 값과 무관 — finalizer=true의 fallback 1회와 finalizer=false rollback의 retry 3회 모두 단일 분기로 차단
- 새 counter `rest_write_blocked` 추가 (case B 의 write 차단 signal)
- `KrxCloseWindowWriter` (WS close frame, 신뢰 source): 변경 없음
- 173 tests passed → push → EC2 deploy + production verification

#### 정책 anchor — 5 핵심 결정

> ⚠️ **Amendment 2026-06-10**: (1)(2)(3)은 [KRX_CLOSE_SNAPSHOT_PLAN §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md)로
> 부분 supersede — 6/9 WS silent-stall 종가 누락(차단이 정확한 종가 1514.7을 버림) 후
> "무조건 차단" → **gate-checked write** 재설계 (gate: calendar / contract identity /
> session evidence[tick recency, env `KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS`=3h] + sanity).
> flag=false = shadow 평가 + 차단 (gate verdict telemetry — WS-miss 날에만 샘플) /
> flag=true = gate-checked write (구 "무가드 복원" 폐기). (4)(5)는 유지.
> env 활성화: **2026-06-15 16:12 KST 완료** (A75606→A75607 rollover + CF close case A 후 GO → prod `KRX_CLOSE_REST_WRITE_ENABLED=true`). 첫 gate-checked write 실측 후보 6/16 06:00 CM~.

1. ~~KIS REST close snapshot은 더 이상 authoritative write source가 아니다.~~ (supersede — gate 전부 통과 시 gated authoritative fallback, WS-first 불변)
2. ~~REST 호출은 diagnostic으로 유지될 수 있지만 DB/Redis write는 default off다.~~ (supersede — default false 유지하되 true 의미가 gate-checked write로 변경)
3. ~~Case B는 REST 성공 write가 아니라 `rest_write_blocked` diagnostic signal로 해석한다.~~ (supersede — flag=true + gate 통과 시 실제 write + CF daily append tail)
4. **`KrxCloseWindowWriter`의 WS close frame write는 신뢰 경로로 유지한다.**
5. **Stage E는 close finalizer 정책과 직교 작업이다.**

#### 운영 검증 명령 (정책 PR 배포 후)

```bash
ssh -i ~/fxi-server-key-pair.pem ubuntu@<EC2> "docker exec exchange-rate-app python -c '
from app import config
from app.crawlers.krx_kis import KrxCloseSnapshotController
print(f\"KRX_CLOSE_REST_WRITE_ENABLED: {config.KRX_CLOSE_REST_WRITE_ENABLED}\")  # True (2026-06-15 활성, code default=false)
print(f\"KRX_CLOSE_FINALIZER_ENABLED:  {config.KRX_CLOSE_FINALIZER_ENABLED}\")   # True
controller = KrxCloseSnapshotController(token_manager=None)
print(f\"rest_write_blocked counter: {controller.counters.get(\\\"rest_write_blocked\\\")}\")  # 0 (initial)
'"
```

#### Rollback 경로 (6/15 활성 → shadow-only 복귀)

prod이 현재 `KRX_CLOSE_REST_WRITE_ENABLED=true`(2026-06-15 활성)이므로 **REST flag rollback = false 복귀**:

```bash
# REST flag만 원복(direct mode 유지) — .env true→false 실제 변경
sed -i 's/^KRX_CLOSE_REST_WRITE_ENABLED=true$/KRX_CLOSE_REST_WRITE_ENABLED=false/' .env
grep -qx 'KRX_CLOSE_REST_WRITE_ENABLED=false' .env || exit 1   # 미적용 시 중단(fail-closed)
docker compose up -d --force-recreate fastapi
```

→ gate-checked write 중단, shadow 평가만(5/25형 stale 차단 복귀). [D] 전체(direct mode 포함) 일괄 원복은 `.env.bak.predirect-20260615` 복원.

~~flag=true 시 기존 1차/2차 PR write 동작 복원. 단 KIS REST stale 위험 동반.~~
**2026-06-10 이후**: flag=true는 gate-checked write (gate 통과 시에만 write — 5/25형
stale은 gate가 차단). 완전 차단 복귀는 flag=false. 상세:
[KRX_CLOSE_SNAPSHOT_PLAN §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md).

#### 다음 자연 검증 시점

| 시점 | 검증 항목 |
| --- | --- |
| **2026-05-26 (화) 06:00 KST** | CM close — 5/25 휴일 차단으로 진입 안 함 (hotfix 효과) |
| **2026-05-26 (화) 08:30 KST** | CF 정규 세션 자동 진입 (정상 영업일) |
| **2026-05-26 (화) 15:45 KST** | 정상 영업일 CF close — WS captured 케이스(case A) 정상 + WS 미captured 시 `rest_write_blocked` 로그 발화 확인 |
| **5/19~5/26 7일 telemetry 분석** | case A/B/C 분포 + 신규 `rest_write_blocked` counter 결합 분석 → ADR-027 Stage C + 3차 PR scope 결정 |

#### 다음 잠재 위험 (캘린더 보강 PR 후보)

2026 한국 공휴일 중 본 코드의 사고 재발 위험:

- 자연 회피 (주말 겹침): 현충일 6/6 토 / 광복절 8/15 토 / 개천절 10/3 토
- 위험 (평일/금요일): 추석 9/24 목 / 9/25 금 / 한글날 10/9 금 / 크리스마스 12/25 금

→ 별도 캘린더 보강 PR로 검증 + cross-check 후 추가 (본 5/25 hotfix scope 외).

### 2026-06-13 금요일밤 CM close-write calendar gap 발견 + fix (`78b02be`)

> 📅 **발견**: 2026-06-13(토) 06:00 KST CM close (금요일밤 세션 종료). 🏷️ **상태**: fix land(shadow-only). **`KRX_CLOSE_REST_WRITE_ENABLED=true` prod 활성 2026-06-15 16:12 KST** (A75606→A75607 rollover[07:04] + CF close case A[15:46] 후 별도 GO, `.env.bak.predirect-20260615`). 활성 전까지 shadow-only(flag=false). **첫 gate-checked write 실측 후보 = 6/16 06:00 CM~** (WS-miss 시 gate 전부 통과하면 write, WS captured면 inert). code default는 여전히 false.

**관측 (6/13, 이미 발생 — flag=false)**:

- 마지막 체결 **1518.10 @ 05:49:30 KST**, 이후 06:00까지 무거래(thin night close, trade_age 636s)
- close trade frame 0건 → WS close-window write 미발생 (captured flag 미SET)
- REST는 **동일가 1518.1 반환** (production 로그 `rate=1518.1` 실증 — `futs_prpr`=마지막 체결가)
- **구 gate가 토요일 boundary를 calendar reject**: `[krx_close_snapshot] REST write gate REJECTED reason=calendar ... boundary=2026-06-13T06:00:00+09:00 (flag=False)`
- Redis latest는 **05:49에 잔존** (06:00 미갱신)

**root cause**: gate 1이 `is_krx_business_day(boundary date=토)` 판정 → 금요일밤 CM의 REST fallback 평가(WS 종가 미capture 시)에서 토요일 boundary를 휴장 오판 reject. scheduling `is_close_snapshot_eligible`(CM: boundary−1=금)과 불일치.

**fix (`78b02be`)**: gate 1 → `is_close_snapshot_eligible(session, boundary_date)` (CM: 야간장 시작일 boundary−1). shadow-only(flag=false → write 변화 0). gate 3(session evidence) 5/25 보호 유지. 상세: [KRX_CLOSE_SNAPSHOT_PLAN §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md).

**예측 (flag=true 활성 2026-06-15 완료, 첫 write 미실측)**: 금요일밤 CM(다음 6/20 토 06:00)이 gate 통과 시 **Redis를 REST 반환 최근가(그날 마지막 체결가) @06:00 overwrite / DB insert-if-changed(동일가 skip)**. 첫 write 실측 후보 = 6/16 06:00 CM 또는 6/20. 토글 매트릭스는 6/15 활성 반영 완료([line 987 이하]), CF/CM 첫 write canary 측정은 다음 WS-miss close 대기.

---

## Rollback 절차

### Rollback A — KRX 전체 격리 (`KRX_FUTURES_ENABLED=false`)

**즉시 격리 (KRX 장애 / KIS API 점검 / 데이터 이상):**

```bash
# 운영 .env에서 KRX_FUTURES_ENABLED=false로 변경
# (.env.example 참고)
docker compose up -d --force-recreate fastapi
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

### Rollback B — Close finalizer만 격하 (`KRX_CLOSE_FINALIZER_ENABLED=false`)

**대상 시나리오 (2026-05-17 c2fb796 배포 이후)**:
KRX 자체는 유지하되 close finalizer (Stage 1-5 2차 작업)만 1차 PR (c0855ff) 동작으로 rollback.
KRX_CLOSE_SNAPSHOT_PLAN §5 정책 비활성 — KrxDbWriter close-grace skip 미진입 + KrxCloseWindowWriter early return + REST 3 retry 그대로.

```bash
cd ~/exchange-rate
# .env 변경 (idempotent — 중복 키 차단 sed 교체 + 없으면 추가)
grep -q '^KRX_CLOSE_FINALIZER_ENABLED=' .env \
  && sed -i 's/^KRX_CLOSE_FINALIZER_ENABLED=.*/KRX_CLOSE_FINALIZER_ENABLED=false/' .env \
  || echo 'KRX_CLOSE_FINALIZER_ENABLED=false' >> .env

# Container 재생성 — restart는 env_file 변경 반영 X (핵심 원칙 5 참조)
docker compose up -d --force-recreate fastapi
```

**검증:**

```bash
docker exec exchange-rate-app python -c "from app import config; print('KRX_CLOSE_FINALIZER_ENABLED =', config.KRX_CLOSE_FINALIZER_ENABLED)"
# 기대: "KRX_CLOSE_FINALIZER_ENABLED = False"
```

**효과 (process 재생성 후 기준)**:

- KrxDbWriter `_flush_after_window` close-grace skip 분기 미진입 → 일반 path로 close grace tick 처리 (insert-if-changed)
- KrxCloseWindowWriter `__call__` early return → close grace window 안 frame 무시
- KrxCloseSnapshotController `_retry_sequence` 3 retry 유지 (1차 PR c0855ff 동작)
- 효과적으로 5/15 ~ 5/16 deploy 상태로 복귀

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

---

## F-3 KRX 가격알림 운영 활성 (2026-05-26)

> 🟢 **상태**: Deployed and observing
> 📌 **ADR**: [ADR-032](DECISIONS.md#adr-032-krx-가격알림-evaluator--source-neutral-재사용--krx-adapter--validate-helper-분리)
> 📌 **Plan**: [KRX_FANOUT_REFACTOR_PLAN §5.2 F](KRX_FANOUT_REFACTOR_PLAN.md)

### F-1/F-2/F-3 trilogy 요약

| 단계 | Commit | Default | 사용자 영향 | 의미 |
|---|---|---|---|---|
| F-1 | `e5ef42e` | `KRX_ALERT_EVALUATOR_ENABLED=false` | 0 (evaluator 미등록) | KrxAlertEvaluator thin subclass + KrxAlertTickHandler adapter + boundary drain helper + 24 tests |
| F-2 | `9cbd7ae` | API 허용 + helper 분리 | 0 (KRX 발송 여전히 닫힘) | `validate_alert_source_asset` source_registry 분리 + category derivative 허용 + 11 tests |
| F-3 | env override | `KRX_ALERT_EVALUATOR_ENABLED=true` 활성 (2026-05-26 13:40 KST) | 테스트 iOS canary만 영향 | 코드 변경 0, .env 토글 + force-recreate |

### F-3 활성 절차 (운영 runbook)

```bash
# 1) .env에 토글 추가 (sed 안전 패턴)
ssh ubuntu@<ec2> '
if grep -q "^KRX_ALERT_EVALUATOR_ENABLED=" ~/exchange-rate/.env; then
  sed -i "s/^KRX_ALERT_EVALUATOR_ENABLED=.*/KRX_ALERT_EVALUATOR_ENABLED=true/" ~/exchange-rate/.env
else
  echo "KRX_ALERT_EVALUATOR_ENABLED=true" >> ~/exchange-rate/.env
fi
'

# 2) git pull (F-1/F-2 코드가 EC2에 없으면 필수)
ssh ubuntu@<ec2> 'cd ~/exchange-rate && git pull'

# 3) build + force-recreate (코드 변경 포함 시 build 필수, env-only 변경이면 force-recreate만)
ssh ubuntu@<ec2> 'cd ~/exchange-rate && docker compose build fastapi && docker compose up -d --force-recreate fastapi'

# 4) 컨테이너 안 import + flag 활성 확인
ssh ubuntu@<ec2> 'docker exec exchange-rate-app python -c "
from app import config, source_registry
from app.crawlers.krx_kis import KrxAlertTickHandler
from app.notifications.alert_evaluator import KrxAlertEvaluator, UsdtAlertEvaluator
print(\"KRX_ALERT_EVALUATOR_ENABLED:\", config.KRX_ALERT_EVALUATOR_ENABLED)
print(\"krx validation:\", source_registry.validate_alert_source_asset(\"krx\", \"usd-krw-futures\"))
print(\"subclass OK:\", issubclass(KrxAlertEvaluator, UsdtAlertEvaluator))
"'
```

기대 출력: `KRX_ALERT_EVALUATOR_ENABLED: True`, `krx validation: None` (통과), `subclass OK: True`.

### iOS canary 검증 절차 (Google Sign-In 우회 — 옵션 D)

테스트 iOS 앱이 KRX UI 미보유 + Google Sign-In 사용 시 Firebase REST `signInWithPassword` 불가. Admin SDK custom token + `signInWithCustomToken` 우회 패턴:

```python
# /tmp/krx_canary.py — EC2 컨테이너에서 단일 process 실행 (docker exec stdout pipe long-line 잘림 회피)
from firebase_admin import auth
from app.notifications.fcm import init_firebase
from app.database import get_db_context
from app import models
from app.latest_rates_cache import _get_sync_client
import os, json, urllib.request

# 1. test user UID 조회 (USDT 알림 등록자 = 테스트 사용자 거의 확정)
with get_db_context() as db:
    row = db.query(models.SourceNotificationSetting.user_id).filter(
        models.SourceNotificationSetting.user_id.like('<prefix>%')
    ).first()
    uid = row[0]

# 2. custom token + ID token 교환
init_firebase()
custom_token = auth.create_custom_token(uid).decode()
req = urllib.request.Request(
    f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={os.environ['WEB_API_KEY']}",
    data=json.dumps({"token": custom_token, "returnSecureToken": True}).encode(),
    headers={"Content-Type": "application/json"}, method="POST",
)
id_token = json.loads(urllib.request.urlopen(req).read())['idToken']

# 3. KRX latest sampling
v = _get_sync_client().get('latest:source:krx:usd-krw-futures')
krx_rate = json.loads(v.decode())['rate']
threshold = round(krx_rate - 0.5, 1)

# 4. API POST — F-2 validation chain 검증 포함
post_req = urllib.request.Request(
    "https://fxi.kr/api/source-notification-settings",
    data=json.dumps({"source":"krx","asset":"usd-krw-futures","condition":"above","threshold":threshold,"is_enabled":True}).encode(),
    headers={"Authorization": f"Bearer {id_token}","Content-Type":"application/json"}, method="POST",
)
print(urllib.request.urlopen(post_req).read().decode())
```

실행: `docker cp /tmp/krx_canary.py exchange-rate-app:/app/ && docker exec -w /app -e PYTHONPATH=/app -e WEB_API_KEY="<from GoogleService-Info.plist>" exchange-rate-app python krx_canary.py`.

### 5/26 13:40 KST iOS canary 실측 결과

| 항목 | 결과 |
|---|---|
| F-2 API validation (`validate_alert_source_asset("krx", "usd-krw-futures") -> None`) | ✅ POST 200 (setting id=6) |
| KRX latest rate | 1505.2 @ 2026-05-26T13:40:00 KST |
| Threshold (`above`) | 1504.7 (즉시 매치 조건) |
| F-1 KrxAlertTickHandler payload→AlertObservation | ✅ source=krx asset=usd-krw-futures rate=1505.3 kind=tick |
| F-1 coalescer 5초 grain flush + evaluate_price_input_async | ✅ 13:40:06 POST → 13:40:08 FCM (2초) |
| F-1 cache invalidate immediate | ✅ POST 직후 다음 tick에서 cache miss → DB query → 1건 평가 |
| FCM multicast → iOS APNS 도착 | ✅ LG U+ iPhone 잠금화면 "📈 미국달러F USD-KRW-FUTURES [1504.7↑이상 도달] 1505.30" |
| DB persist (triggered) | ✅ id=6, triggered=True, enabled=False, last_notified_at=2026-05-26T04:40:08.951948 UTC, last_notified_rate=1505.3 |
| DB persist (log) | ✅ id=95, success=True, triggered_rate=1505.3, error=None |
| 중복 발송 차단 (stale cache skip) | ✅ 13:40:11 / 13:40:15 `stale cache (enabled/triggered) skip` log 2회 |
| 서버 alert_evaluator 예외 | ✅ 0건 |

### Rollback 절차

```bash
# .env에서 토글 false
ssh ubuntu@<ec2> 'sed -i "s/^KRX_ALERT_EVALUATOR_ENABLED=.*/KRX_ALERT_EVALUATOR_ENABLED=false/" ~/exchange-rate/.env'

# force-recreate (env 변경은 restart로는 반영 안 됨 — env_file 재로드 필요)
ssh ubuntu@<ec2> 'cd ~/exchange-rate && docker compose up -d --force-recreate fastapi'
```

`docker compose restart`로는 env 변경 미반영 (env_file은 컨테이너 재생성 시에만 다시 읽음). `--force-recreate` 필수.

### KRX 토글 매트릭스 (F-3 활성 후)

| Env | Default | 운영값 (2026-06-15 기준) | 의미 |
|---|---|---|---|
| `KRX_FUTURES_ENABLED` | false | true | WS 연결 + 신규 DB 저장 |
| `KRX_CLOSE_FINALIZER_ENABLED` | true | true | KrxCloseWindowWriter + KrxCloseSnapshotController |
| `KRX_CLOSE_REST_WRITE_ENABLED` | false | **true** (2026-06-15 16:12 활성) | gate-checked write (6/10 §5.7.8 재설계 + 6/15 rollover 후 GO; 첫 write 실측 6/16 CM~) |
| `KRX_REDIS_TICK_WRITE_ENABLED` | false | true | Stage E E-2 tick-level Redis mirror |
| `KRX_ALERT_EVALUATOR_ENABLED` | false | true | **F-3 신규** KRX 가격알림 활성 |

> **E-2 활성 anchor (2026-06-07 정리)**: E-1 `c3cb1c1` land 이후 2026-05-26 운영에서 `KRX_REDIS_TICK_WRITE_ENABLED=true` 확인. 5/26에는 약 01:30 KST 컨테이너 시작 후보와 13:03 KST 수동 컨테이너 교체(journald 확인)가 있었으나, env 값은 journald/docker 로그에 남지 않아 어느 recreate에서 flip이 적용됐는지는 특정 불가. Stage E 활성 증거는 본 토글 매트릭스와 5/26 15:45 CF close Stage E non-interference 실측이며, 13:40 iOS canary는 F-3 alert 활성 증거(별개 토글)다. 정확한 flip 시각은 `.env` mtime 덮임(6/5)·이후 컨테이너 재생성·bash history timestamp 부재·journald env 미기록으로 복구 불가.

### 5/26 15:45 CF close 첫 실측 결과 (sampler 검증)

5/26 13:40 iOS canary 직후 15:45 close 자연 도래 검증. sampler `/tmp/krx_close_1545_sample.log` 5초 grain 36 sample.

| 시점 | Phase | 핵심 데이터 |
|---|---|---|
| 15:44:30~15:45:54 | pre-close (close grace window 안) | latest stagnant (rate=1504.2 ts=15:34:55+09:00, mirrored=15:34:56) — Stage E tick writer가 close grace tick skip하므로 정상. captured_flag NONE. DB recent 3min count=0 |
| 15:46:00.110 | KrxCloseWindowWriter close 발사 | LOG `[krx_close_window] close saved session=CF rate=1502.8 ts=2026-05-26T15:45:00+09:00` |
| 15:46:00 | captured_flag SET | value="1" TTL=3594s (1h) |
| 15:46:00 | DB row 1건 unconditional INSERT | id=451680 rate=1502.8 ts_kst=15:45:00.000 |
| 15:46:00 | Redis latest 갱신 (KrxCloseWindowWriter 단독) | rate=1502.8 ts=15:45:00+09:00 mirrored=15:46:00.110 |
| 15:46:01 | KrxCloseSnapshotController retry sequence entry | LOG `[krx_close_snapshot] WS captured at entry → REST skip (session=CF date=2026-05-26)` |

**Invariant 검증 (3축 모두 통과, sampler 직접 실측)**:

1. ✅ captured_flag SET (WS 단독 처리 성공 신호) — Case A 진입 조건
2. ✅ DB close row 1건 (KrxCloseWindowWriter unconditional INSERT)
3. ✅ REST `WS captured at entry → REST skip` (Case A — WS frame이 신뢰 source, `rest_write_blocked` 증가 0, `KRX_CLOSE_REST_WRITE_ENABLED=false`라 차단 분기도 미진입 — REST 호출 자체가 entry flag check에서 skip)

**Stage E non-interference 실측**: pre-close 1분 동안 Redis latest stagnant (rate=1504.2 ts=15:34:55) — Stage E tick writer가 close grace tick skip + KrxCloseWindowWriter 단독 처리 정상.

**F-1 alert handler 격리 실측**: alert_evaluator 예외 0건 (sampler 시간대 docker logs 검증). **단 close grace tick에서 alert evaluation/FCM 발사 path 자체는 직접 실측되지 않음** — sampler 시간대에 활성 KRX 알림 0건(setting id=6은 13:40에 이미 triggered=true)이라 candidates empty path를 silent하게 거침. ADR-032 invariant 2 (close grace skip ❌)는 *코드 구조상 보장* (`KrxAlertTickHandler.__call__`이 close grace check 없음 — `KrxRedisLatestWriter`와 다르게)이고, 운영 실측은 별도 close grace 시점 활성 알림 등록 후 검증 필요.

= 5/26 CF close = **Case A 완전 정상 동작** (close finalizer / Stage E / REST fallback / alert handler 예외 격리 4 layer 실측 통과). close grace tick FCM 발사 path 실측은 후속 관찰 항목 → 5/27 canary로 닫힘 (다음 섹션).

### 5/27 15:45 CF close grace + FCM canary 실측 결과 (invariant 2 closed)

5/26 caveat (close grace tick FCM 발사 path 직접 실측 X)을 닫기 위해 5/27 15:45 CF close 직전 활성 KRX 알림을 등록하고 end-to-end 5축 검증.

**Canary 설계**:

- canary script `/tmp/krx_canary_close_grace.py`: 15:43:15 KST 시작 → custom token + ID token 발급 → 114s wait → 15:45:10 POST
- threshold = current - 10.0 (Stage E close grace skip으로 Redis latest stale 마진 확보 — 5/26 lesson)
- condition = above (1499.6 close rate → 1490.3 threshold 초과 = 발사 조건)
- sampler `/tmp/krx_close_1545_sample.sh` 병행 (5초 grain Redis latest + DB recent 관찰)

**Timeline (5축 evidence)**:

| 시점 | 축 | Evidence |
| --- | --- | --- |
| 15:43:15 | canary start | Step 1 UID resolved + Step 2 custom_token len=872 segments=3 + Step 3 ID token len=1118 |
| 15:45:10 | canary POST | setting id=7 / threshold=1490.3 / condition=above / triggered=false (응답 시점) |
| 15:46:00 | close finalizer | LOG `[krx_close_window] close saved session=CF rate=1499.6 ts=2026-05-27T15:45:00+09:00` + captured_flag SET |
| 15:46:00 | Stage E | pre-close 1분 latest stagnant (rate=15:34:55 stale) → close save 후 15:45:00/1499.6 반영. KrxRedisLatestWriter close grace tick skip 정상 |
| 15:46:01 | alert evaluator | `alert_evaluator FCM sent` (close grace tick path 실측) |
| 15:46:01 | DB log | log id=96 success=True, setting id=7 triggered=True enabled=False last_notified_rate=1499.6 |
| 15:46:01 | REST skip | LOG `[krx_close_snapshot] WS captured at entry → REST skip` (rest_write_blocked 증가 0) |
| 15:46:01+ | structured event | `/admin/api/krx-finalizer-stats?days=1` → `ws_close_saved=1`, `rest_skipped_ws_captured=1`, `case_summary={"A": 1}` |
| ~15:46:10 | iOS APNS | iPad + iPhone 동시 도착 (사용자 캡처 15:46:10) — 서버 sent_at 대비 **사용자 확인 기준 ~9s 이내 (실제 도착은 그보다 빠름)**, 문구 `📈 미국달러F USD-KRW-FUTURES [1490.3 ↑이상 도달] 1499.60` |

**5축 판정 = 모두 통과**:

1. ✅ **close finalizer**: 15:46:00 CF close 1499.6 DB save + captured_flag SET (Case A 진입)
2. ✅ **Stage E**: close grace tick에서 Redis mirror SKIP 정상 (KrxRedisLatestWriter non-interference) + KrxCloseWindowWriter 단독 처리
3. ✅ **alert evaluator**: close grace tick에서 evaluator 평가 진행 + FCM 발사 (15:46:01) + DB log success
4. ✅ **structured event persist**: commit `18b06f5` (2026-05-26 land) 후 **첫 운영 검증** — case=A aggregation 정상 동작
5. ✅ **iOS APNS**: 사용자 화면 캡처로 도착 확인, APNs 지연 ~9s 이내

**ADR-032 invariant 2 (close grace skip ❌) closed by 2026-05-27 close grace canary**.

**별도 항목**: invariant 1 (SET-only ❌) + invariant 3 (Session boundary drain) — 본 canary로 직접 검증되지 않음. 1번은 alert path가 mirror SET/SKIPPED/FAILED outcome과 직교 (코드 구조 보장 유지), 3번은 CF→CM session boundary 시점 활성 알림 별도 등록 후 검증 필요 (Phase 외 후속 작업).

**2일 연속 case=A 안정 확인 (5/26 → 5/27)**: WS path 단독 처리 정상 영업일 안정. 5/25 휴장일 사고 같은 stale REST write 재발 X — `KRX_CLOSE_REST_WRITE_ENABLED=false` default 정책 효과 운영 검증.

### 후속 관찰 / TODO

1. **24h+ alert_evaluator FCM sent 누적 + 예외 0 유지** (ADR-032 Stable observed 조건) — 5/27 canary로 1차 충족 (FCM sent 누적 시작 + 예외 0), 24h+ 누적은 5/28 14:00경 도달.
2. ✅ **5/26 15:45 CF close 첫 실측** — close finalizer / Stage E / REST fallback / alert handler 예외 격리 4 layer 통과. close grace tick FCM 발사 path 직접 실측은 다음 항목으로 닫힘.
3. ✅ **5/27 15:45 CF close grace + FCM canary 통과 (2026-05-27)** — ADR-032 invariant 2 closed by 2026-05-27 close grace canary (위 5/27 섹션 5축 evidence). structured event persist (commit `18b06f5`) 첫 운영 검증 동시 통과.
4. **`USDT_PHASE1_CLIENT_GUIDE.md` line 571 stale fix** — 본 PR에 포함 (F-2 후속 자연 적용).
5. ✅ **`_validate_phase1_source_asset` → `_validate_alert_source_asset_or_400` rename** — F-3 직후 별도 cleanup commit으로 완료 (2026-05-26).
6. **CF→CM session boundary drain 실측** (ADR-032 invariant 3) — 본 canary로 직접 검증되지 않음. CF→CM boundary 시점 활성 알림 별도 등록 후 검증 필요 (Phase 외 후속).

### 5/19~5/26 7일 telemetry 분석 결과 (2026-05-26 요약)

F-3 활성 후 수행한 5/19~5/26 close finalizer 7일 운영 평가 (정책: `KRX_CLOSE_REST_WRITE_ENABLED=false` diagnostic-only, 5/25 정책 PR `6a43785`):

**Evidence level 분리** (정량 telemetry / 운영 관찰 / 한계 명시):

| Evidence | source | 결과 |
| --- | --- | --- |
| Hard evidence | sampler 직접 실측 | 5/26 CF close = Case A 확인 (captured_flag SET, DB row 1건 INSERT, REST `WS captured at entry → REST skip`, `rest_write_blocked` 증가 0) |
| Persistent evidence | RDS `source_rates` table | 5/19~5/26 KRX close boundary row 9건 존재 — CF 5/19~22 + 5/26 + CM 5/20~23, 모두 timestamp 정확히 15:45:00 / 06:00:00 KST |
| Operator observation | 운영 중 수시 확인 | 정상 영업일 close는 WS path (KrxCloseWindowWriter)로 처리됨을 직접 확인 (정량 분포 X, 단발성 확인 누적) |
| Unavailable | docker logs / container file logs / fastapi process counter | 5/26 13:03 KST 재배포 시점 이전 모두 유실. 7일 Case A/B/C 정량 분포 복원 불가 |

**정책 결론 (3차 PR scope)**: 정상 영업일 close는 운영 중 수시 확인상 WebSocket close path로 처리됐고, 5/26 CF close는 sampler로 Case A를 직접 재확인했다. 다만 container 재배포로 과거 logs/process counters가 유실되어 7일 Case A/B/C 정량 분포는 복원할 수 없다. 따라서 close REST fallback은 완전 제거하지 않고 `KRX_CLOSE_REST_WRITE_ENABLED=false` diagnostic-only 상태를 유지한다. **[Amendment 2026-06-15: 이 diagnostic-only/false 결론은 6/10 gate-checked write 재설계(§5.7.8) + 6/15 16:12 prod `=true` 활성으로 supersede — code default만 false 유지. 상세: 본 문서 토글 매트릭스 + DECISIONS ADR-027 follow-up amendment.]**

**별도 개선 후보** (이번 PR scope 외 — logs/telemetry 보존 인프라):
- CloudWatch log stream (container 재배포 영향 X)
- `/app/logs/` host volume mount (container 재생성 시 file 보존)
- `KrxCloseFinalizerStats` Redis/DB persist (process restart 시에도 누적 유지)
- 위 3개는 향후 telemetry 보강 필요 시점에 별도 PR로 진입. 현재 운영 안정성 측면에서는 우선순위 낮음 (정상 영업일 WS path 운영 관찰 기반 신뢰).
