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
KRX_BROADCAST_INCLUDE=false   # broadcast 노출 X (Stage 2에서 true)

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

---

## 5/18 만기일 특별 관찰 (현재 자동 rollover 미구현)

**컨텍스트:** A75605 = 2026-05-18 만기. 정규세션 11:30 종료 후 client는 만료 contract를 그대로 가진 채 reconnect 시도하거나 silent close 상태로 머물게 됨 — 자동 rollover/재구독 코드 없음.

**관찰 시나리오 (수동 데이터 수집):**

1. **5/18 11:30 직전 (예: 11:25~11:30)**:
   - tick 들어오는지 확인 (정상 영업)
   - `SELECT max(timestamp) FROM source_rates WHERE source='krx'` 분 단위 polling

2. **5/18 11:30 직후 (예: 11:30~12:00)**:
   - tick 끊김 패턴 — 즉시 끊김 / 일정 시간 후 끊김 / 혼합
   - WebSocket 상태: `[kis_ws]` 로그에서 disconnect / reconnect 시도 / 무한 backoff 등 어떤 패턴인지
   - client status 간접 확인: `[kis_ws] status %s → %s` 전이 로그로만 추적 가능 (`_status` 직접 expose하는 admin endpoint/shell 없음). 기대 전이 예: `normal → reconnecting → stale`

3. **만기일 후 (예: 5/19 영업 시작 시)**:
   - 만료 contract A75605에 더 이상 tick 없음 (KIS 측 종목 제거)
   - 수동 `docker compose restart fastapi` 실행
   - restart 후 `select_active_usd_futures_contract()`가 다음 월물(예: 6월물 단축코드)로 잡히는지 확인:

     ```bash
     docker compose logs fastapi --since 1m | grep -E '\[kis_ws\] subscribed'
     # 기대: subscribed tr_id=H0CFCNT0 key=<6월물 단축코드>
     ```

   - SQL로 신규 월물 데이터 적재 확인

4. **수집된 데이터로 PR6c-2d / PR6d 설계 결정**:
   - 자동 rollover trigger를 cron으로 할지 vs WebSocket 상태 감지로 할지
   - REST snapshot fallback 임계값 (staleness N초)
   - rollover 시점 (만기일 11:30:00 정확히 vs 다음 영업일 시작 시)

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

> Stage 2 = `KRX_BROADCAST_INCLUDE=true` 추가. broadcast `rates` 배열에 `source="krx", asset="usd-krw-futures"` 등장.

진입 **전** 모두 충족되어야 함:

- [ ] Stage 1을 24h+ 운영, 위 baseline 지표 수집 완료
- [ ] 5/18 만기 관찰 데이터 확보 (위 시나리오 1~3)
- [ ] PR6d (REST snapshot/fallback) 구현 완료 — broadcast 노출 시 stale 방지 안전장치 필수
- [ ] PR6c-2d (자동 rollover) 또는 동등 운영 절차(수동 restart cron) 결정 완료
- [ ] iOS/Android 클라이언트가 `source="krx", asset="usd-krw-futures"` 처리 검증 완료 (모르는 source 무시 또는 표시)
- [ ] ADR-027 (REST fallback 정책) 작성 완료

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
