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
- case B (KIS WS 미송신): close saved log 없음 + REST fallback 발화 + Redis latest는 REST로 갱신
- case C (dedup skip): 2차 작업 정책상 0 예상

case B 비율 결과로 3차 PR scope 결정 (REST fallback 제거 검토 / 유지).

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

- **5/19~5/26 close finalizer 7일 telemetry** — case A (WS-first 성공) / case B (WS-first 실패, REST 성공) / case C (둘 다 실패) 분포 측정. CF/CM 첫 실측 모두 case A. 상세 status는 [KRX_CLOSE_SNAPSHOT_PLAN.md §0](KRX_CLOSE_SNAPSHOT_PLAN.md) 추적.

> **다음 만기 (6/18) 별도 observer는 불필요로 결정 (2026-05-19)**. 5/18 첫 실측으로 운영 정책 input 모두 확정 — 만기일 rollover (PR6c-2d-1) 정상 / KIS master 익일 batch 갱신 / REST stale 정상 응답 가능 / 자체 calendar 기반 active contract 판단 / CF/CM close finalizer WS-first 성공. 다음 만기는 운영 로그/DB row만 사후 확인 ([KRX_CLOSE_SNAPSHOT_PLAN.md §0](KRX_CLOSE_SNAPSHOT_PLAN.md) telemetry). master batch 재확인이 꼭 필요하면 `observe_kis_master.py` ad-hoc 1회 호출로 충분.

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
