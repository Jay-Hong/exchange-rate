# KRX Fanout Refactor Plan

> 📅 **작성일**: 2026-05-14
> 🏷️ **상태**: Active plan — A/B/C/A-pre completed, D pending
> 📋 **문서 성격**: KRX 미국달러선물 (`app/crawlers/krx_kis.py`)의 현재 책임/시그널/Stage 상태를 정리하고 장기 fanout 구조와 정합화하기 위한 implementer-focused mapping. ADR-027의 *decision record*와 분리.

## 0. Scope & Status

본 문서는 *신규 skeleton 작성*이 아니라 **이미 들어와 있는 Stage A/B 코드 + ADR-031 Redis 통합을 장기 fanout 목표와 정합화**하기 위한 mapping.

📌 **장기 fanout 목표의 anchor**: [REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md). 본 문서의 §4 목표 구조 및 §5.2 Stage E~G는 §4.1 계약과 정합화되며, freshness metadata terminology(`rate_changed_at` / `seen_at` / `mirrored_at`)는 §4.1.3을 참조하며, 본 문서에서는 용어를 재정의하지 않는다.

**5/18 전 가능 범위**:

- 현재 책임/시그널/Stage 정리
- 목표 fanout 구조 정의 (`KrxLivenessMonitor` / `KrxRestFallbackController` / `KrxRedisLatestWriter` / `KrxDbWindowWriter` / `KrxAlertEvaluator`)
- behavior-change-0 extract (handler/책임 분리만, 호출 순서/조건/timing 보존)

**진행 현황 (2026-05-14 기준 — 모두 behavior-change-0)**:

- ✅ **A-pre**: liveness invariants 회귀 가드 (commit `6faa2dd` — 5 tests)
- ✅ **A**: KrxLivenessMonitor 추출 (commit `eb085c1` — frame/age/gap state ownership, `_set_status`는 client 잔류)
- ✅ **B**: KrxRestFallbackController 추출 (commit `6a5aa91` — getter 패턴 active_session/last_tick_at, wrapper 경유 behavior-change-0)
- ✅ **C**: KrxRedisLatestWriter 객체 분리 (commit `f5ba9bc` — ADR-031 DB-insert-bound timing 보존)
- ⏸ **D**: KrxAlertEvaluator stub — Stage C 영역(ADR-027 결정)과 함께 검토. 단독 stub은 실익 낮음.

각 PR은 5 invariants + 27 fallback eligibility + KRX kis + redis integration suites 모두 unchanged GREEN 입증. 전체 회귀 604 passed 유지.

**미배포 누적 (모두 behavior-change-0)**: `f5ba9bc`, `6faa2dd`, `eb085c1`, `6a5aa91`. 단독 deploy 효익 작아 다음 실제 동작 변경 PR과 묶음 권장.

**5/18 후 결정 영역 (본 doc 범위 외)**:

- REST fallback threshold 확정 ([ADR-027](DECISIONS.md))
- 만기 종목 silence 패턴 + REST 응답 변화 baseline
- Stage C: REST snapshot 결과를 Redis/DB/topic에 반영하는 정책

## 1. krx_kis.py 현재 책임 분해

### 1.1 `KisFuturesClient` (line 513~)

단일 클래스에 다음 책임이 혼재:

| 책임 | 위치 (file:line) | 비고 |
|---|---|---|
| **Session/Connection manager** | `start()` line 760, `_run_session()` line 988, `_connect_and_listen()` line 1031 | active session detection + WebSocket connect/reconnect + backoff sequence |
| **Frame parser/dispatcher** | `_handle_frame()` line 1099, `_dispatch_tick()` line 1127 | raw frame → tick payload + tr_id별 분류 (trade vs quote) |
| **Liveness metrics** | `_dispatch_tick()` line 1134-1166, `get_metrics()` line 670 | `_last_tick_at` / `_last_trade_frame_at` / `_last_quote_frame_at` + gap buckets + max gap |
| **Status transitions** | `_set_status()` line 1015, stale 감지 line 1005 | normal / reconnecting / stale |
| **Fallback eligibility evaluator** | `_evaluate_rest_fallback()` line 853-926 | 4단계 검사 (grace → below_threshold → cooldown → disabled) → counter 갱신 |
| **REST fallback invoker** | `_invoke_rest_fallback()` line 928 | Stage B — eligible 시 REST 호출, 결과는 log/counter only |
| **REST task lifecycle** | `_fallback_tasks` set line 602, task add/discard line 924-925, stop() cleanup line 967 | fire-and-forget task lifecycle |
| **Tick fanout** | `_fanout()` line 1197, `add_tick_handler()` line 756 | trade tick만 fanout (line 1174 quote 제외) |
| **Summary log loop** | `_summary_log_loop()` line 794 | 60초 cycle metric INFO + REST fallback evaluation 트리거 |

### 1.2 `KrxDbWriter` (line 1219~)

| 책임 | 위치 | 비고 |
|---|---|---|
| **DB window writer** | `__call__()` line 1248, `_flush_after_window()` line 1247, `_sync_db_write()` line 1274 | 1초 window debounce + `insert_source_rate_if_changed` |
| **Redis write-through** (ADR-031) | `_sync_db_write()` line 1297-1318 | DB insert 성공 시 `set_latest_krx_rate_from_sync_job` 호출 (best-effort) |
| **Race prevention** | `_flush_after_window()` finally line 1268-1272 | DB write 중 들어온 새 tick → 새 timer 예약 |

**관찰**: `KrxDbWriter`가 *DB window 쓰기* + *Redis write-through* 두 책임 동시 보유. 1차 (ADR-031)에서 의도된 임시 위치.

### 1.3 Quote tick 처리 (line 1171-1176)

```python
if is_quote:
    logger.debug("[kis_ws] quote tick ignored (PR6a fanout): tr_id=%s", tr_id)
    return
```

Quote frame은 liveness metric만 갱신, fanout 대상 X. 이유 (line 1171-1173): "매도호가1을 대표값으로 잘못 저장할 위험 + 체결보다 19× 빈도라 latest를 매도호가로 덮어쓰기".

## 2. 현재 signal 의미

### 2.1 Age signals (liveness)

| Signal | 의미 | 갱신 위치 |
|---|---|---|
| `last_frame_age_sec` | WebSocket frame 전체 liveness (trade + quote 합산) | `_dispatch_tick()` line 1138 |
| `last_trade_frame_age_sec` | 체결 frame 기준 liveness — *가격 움직임* 시그널 | `_dispatch_tick()` line 1158 |
| `last_quote_frame_age_sec` | 호가 frame 기준 liveness — *연결은 살아있는가* 시그널 | `_dispatch_tick()` line 1166 |

**핵심 nuance**: 야간/세션-end 저유동성 구간에서 *체결 frame 멈춤 + 호가 frame 계속*은 정상. 단순 `last_frame_age_sec` 기반 fallback 판단은 false positive. *체결 vs 호가 분리 관찰*이 ADR-027 stale 정책의 전제.

### 2.2 Status state

| State | 진입 조건 | 위치 |
|---|---|---|
| `normal` | active session + tick 정상 수신 / 휴장 진입 / 새 WebSocket 연결 진입 | line 777, 1054, 1169 |
| `reconnecting` | disconnect 발생, backoff 재시도 중 | line 992, 998 |
| `stale` | `last_tick_at > STALE_AFTER_SEC`(60s default) — **active loop 안** OR reconnect 중 양쪽에서 진입 | line 1063 (active loop), line 1005-1006 (reconnect path) |

**관찰**: stale은 *active connection silence + reconnect silence* 양쪽에서 진입. `_connect_and_listen` 안의 5초 idle check loop가 active session 중 silence를 감지 (line 1063-1068). 또한 `_set_status(normal→stale)` 전이는 `_evaluate_rest_fallback()`의 진입점 — 짧은 stale (6~15s) 누락 차단 위해 transition 직후 1회 평가 (line 1026-1027). 따라서 *status transition*과 *fallback eligibility evaluation*은 분리된 두 책임이지만 *normal→stale 전이 시점*에서 연결됨.

### 2.3 Fallback counters (line 589-598)

| Counter | 의미 |
|---|---|
| `evaluated` | 총 evaluation 횟수 (status==stale 시 1회/cycle) |
| `eligible` | 모든 차단 조건 통과 (REST 호출 후보) |
| `suppressed_session_end_grace` | session 종료 직전 grace 구간 차단 |
| `suppressed_below_threshold` | `frame_age < KRX_REST_FALLBACK_STALE_SEC` 차단 |
| `suppressed_cooldown` | 직전 fallback 후 cooldown 미경과 차단 |
| `suppressed_disabled` | env `KRX_REST_FALLBACK_ENABLED=false` 차단 |
| `rest_success` / `rest_error` | Stage B+ 실제 REST 호출 결과 |

**핵심 nuance — Stage 별 의미 변화**:

- **Stage A** (env `KRX_REST_FALLBACK_ENABLED=false` default): counter는 **"현재 정책 후보의 분류/telemetry"** — REST 호출 결정 X. 운영자가 baseline 보고 threshold/policy 결정하기 위한 관찰 도구.
- **Stage B** (env=true): `eligible` 도달 시 `_invoke_rest_fallback()` task 생성 (line 916-925). counter는 *분류 + REST task 생성 decision* 까지 겸함.
- **Stage C** (미결, ADR-027 영역): REST 결과를 Redis/DB/topic에 반영하는 단계. Stage B에서도 결과는 *log/counter only* (broadcast/DB 미반영).

즉 "counter = eligibility 분류"의 framing은 *Stage A 한정*이고, Stage B에서는 호출 decision 의미 포함. Stage C에서야 결과 반영 결정.

### 2.4 Gap buckets

`_gap_buckets_total` / `_gap_buckets_trade` / `_gap_buckets_quote` (line 578-580): `<=1s` / `<=2s` / `<=5s` / `<=10s` / `<=30s` / `<=60s` / `>60s` 7-bucket. baseline 분포 분석용. boundary는 ADR-027 후보 (운영 분포 보고 조정 예정).

## 3. 현재 Stage 정리

| Stage | 상태 | 위치 |
|---|---|---|
| **Stage A** (metrics + eligibility telemetry) | ✅ 완료 + 운영 | line 586-598 counters, line 853-926 evaluator |
| **Stage B skeleton** (REST task + log/counter only) | ✅ 코드 들어옴, env=false default | line 916-925 task creation, line 928-961 invoke |
| **ADR-031** (Redis write-through after DB insert) | ✅ 완료 + 운영 (`0756329`) | `KrxDbWriter._sync_db_write` line 1297-1318 |
| **Stage C** (REST 결과 Redis/DB/topic 반영) | ⏸ 미결, 5/18 후 결정 | [ADR-027](DECISIONS.md) 영역 |

## 4. 목표 fanout 구조

[REALTIME_ARCHITECTURE_PLAN.md §4.1 "All-source observation fanout contract"](REALTIME_ARCHITECTURE_PLAN.md)에 맞춰 KRX 목표 구조를 정렬한다. **"알림은 모든 tick, DB는 window close"** 원칙을 KRX fanout에 적용한다:

```
WebSocket tick (KisFuturesClient)
  → _dispatch_tick (frame parse + liveness metric 갱신)
  → _fanout (trade tick만)
      ├─ KrxRedisLatestWriter      # 매 tick → Redis latest
      ├─ KrxAlertEvaluator (stub)  # 매 tick 평가 (후속)
      └─ KrxDbWindowWriter         # 1초 window의 last → DB

별도 cycle:
  KrxLivenessMonitor      # frame/trade/quote silence + status transitions
  KrxRestFallbackController  # eligibility 평가 + REST task lifecycle
```

### 4.1 책임 분리 매핑

| 미래 컴포넌트 | 현재 위치 | 마이그레이션 비고 |
|---|---|---|
| `KrxLivenessMonitor` | KisFuturesClient line 670-750 (`get_metrics`) + line 1134-1166 (silence/gap 갱신) + line 1015 (`_set_status`) | metric state ownership 이전. behavior-change-0 가능 (state 위치만 이동). |
| `KrxRestFallbackController` | KisFuturesClient line 853-961, 599-602, 967-972 | counter + task lifecycle + invoke. LivenessMonitor의 status/age signal 의존 → interface 정의 필요. |
| `KrxRedisLatestWriter` | `KrxDbWriter._sync_db_write` line 1297-1318 | **현재 ADR-031 = DB insert 성공 후 Redis write**. tick-level write로 옮기면 *behavior change* — 5/18 후 Stage C 결정 영역. 1차에서는 *호출 위치 동일, 책임 객체만 분리*. |
| `KrxDbWindowWriter` | `KrxDbWriter` line 1219-1318 minus Redis write-through 부분 | window debounce + insert-if-changed만 유지. |
| `KrxAlertEvaluator` | 미존재 | tick fanout handler 인터페이스 자리만 마련. 구현 후속 phase. |

## 5. Refactor 순서 제안

### 5.1 5/18 전 가능 (behavior-change-0)

**A. KrxLivenessMonitor 추출** — ✅ 완료 (commit `eb085c1`):

- KisFuturesClient의 metric state를 `KrxLivenessMonitor` 객체에 이전 (`_last_tick_at`, `_last_*_frame_at`, frame counters, gap buckets, max gap).
- **status transition counters는 client에 잔류** (`_set_status` + `_status_transition_count`는 KrxRestFallbackController 결합 영역이라 PR-B와 묶지 않음).
- 외부 API 동일 (`get_metrics()` 출력 shape 100% 보존, `_last_tick_at` property + `_reset_active_session_gap_metrics()` wrapper로 test 호환).
- 호출 순서 변경 X (`_dispatch_tick` 안에서 `self._liveness.observe_tick(tr_id, now)` 위임).
- **`_last_tick_at` multi-purpose 결합 보존 (★ 핵심 회귀 가드)** — 추출 시 다음 모든 연결점에 동일 시맨틱 유지:
  - liveness metric (`get_metrics()` `last_frame_age_sec`)
  - **PR6e grace start**: 새 WebSocket 진입 시 `_last_tick_at = time.time()` (line 1053) — carry-over로 인한 즉시 stale 전이 차단
  - **stale transition**: active loop (line 1063-1068) + reconnect path (line 1005-1006)
  - **fallback frame_age**: `_evaluate_rest_fallback` 안의 `frame_age = now - _last_tick_at` (line 891-893)
  - **session reset**: `_reset_active_session_gap_metrics`가 `_last_tick_at = None`으로 설정 (line 660) 후 호출자가 `_last_tick_at = time.time()` 재설정 책임 (Codex 2026-05-07 외부 검토 경고)
- 검증:
  - 전체 27 tests in `test_krx_fallback_eligibility.py` + 모든 KRX 관련 테스트 통과
  - `get_metrics()` 출력 snapshot diff 0
  - **normal→stale 전이 시 `_evaluate_rest_fallback()` 호출 횟수 보존** (line 1026-1027 trigger 시맨틱)
  - active loop의 stale check (line 1063) 시점/조건 보존
  - reconnect path의 stale check (line 1005) 시점/조건 보존

**B. KrxRestFallbackController 추출** — ✅ 완료 (commit `6a5aa91`):

- `_evaluate_rest_fallback` / `_invoke_rest_fallback` / `_fallback_counters` / `_fallback_tasks` / `_last_fallback_at`을 controller로 이전.
- **Getter 패턴** (Codex 핵심 권고): controller 생성 시 `active_session_getter=lambda: self._active_session`, `last_tick_at_getter=lambda: self._last_tick_at` 주입. `_invoke_rest_fallback()` 내부에서 `self._get_active_session()` 호출 → session은 **invoke 시점에 read** (evaluate 시점 freeze X — 기존 line 1020 동작 보존).
- **Wrapper 경유** (behavior-change-0): `_set_status(normal→stale)`는 client의 `self._evaluate_rest_fallback(time.time())` wrapper를 거쳐 controller.evaluate 호출. invariant test 2/3 (`patch.object(client, "_evaluate_rest_fallback")`) 호환.
- 호출 시점 변경 X (status transition + summary_log_loop 두 진입점 그대로).
- counter 증가 조건 변경 X (검사 순서 4단계 보존).
- REST task 생성 조건 변경 X (`KRX_REST_FALLBACK_ENABLED` env 의미 동일).
- `stop()` cleanup: `await self._fallback_controller.cleanup_tasks()` 위임 (pending cancel/await/clear 동일).
- Backward compat shims (5개): `_fallback_counters` / `_last_fallback_at` (get/set) / `_fallback_tasks` (property), `_evaluate_rest_fallback(ts)` / `_invoke_rest_fallback()` (method wrapper) — test 46건 unchanged.
- 검증: `test_krx_fallback_eligibility.py` 27건 + `test_krx_liveness_invariants.py` 5건 unchanged GREEN.

**C. KrxRedisLatestWriter 객체 분리 (호출 위치 동일)** — ✅ 완료 (commit `f5ba9bc`):

- `KrxDbWriter._sync_db_write` 내부의 *Redis write-through* 부분을 별도 `KrxRedisLatestWriter` 객체로 추출.
- 단, **호출 위치 + timing은 ADR-031 그대로**: DB insert 성공 후, 같은 sync 컨텍스트에서 `redis_writer.write_after_db_insert(asset, rate, ts)` 위임.
- 즉 객체 분리만 (책임 명시), tick-level 갱신은 X.
- 검증: `test_krx_redis_integration.py` 12 tests 통과 + `usdt_redis_stats`에 krx 미등장 유지.

**D. KrxAlertEvaluator stub 자리 마련**:

- 인터페이스 정의만 (Protocol/ABC), 실제 등록은 X.
- `add_tick_handler`로 future 등록 가능한 형태.
- 검증: 코드 변경 거의 없음, smoke 영향 없음.

→ A/B/C 모두 *behavior-change-0* 완료. 실제 PR 분리는 *각 단계 1개씩*으로 진행되어 회귀 surface 최소화:

- PR-A-pre (`6faa2dd`): liveness invariants 회귀 가드 5 tests
- PR-A (`eb085c1`): KrxLivenessMonitor 추출
- PR-B (`6a5aa91`): KrxRestFallbackController 추출
- PR-C (`f5ba9bc`): KrxRedisLatestWriter 객체 분리
- D: Stage C 영역과 함께 검토 (단독 stub 실익 낮음)

### 5.2 5/18 후에만 가능 (Stage C 결정 영역)

**E. KrxRedisLatestWriter tick-level 활성화**:

[REALTIME_ARCHITECTURE_PLAN.md §4.1.5](REALTIME_ARCHITECTURE_PLAN.md)에서 KRX의 "DB-insert-bound" Redis write가 all-source fanout 계약의 주요 deviation으로 명시됨. Stage E가 KRX 측 정합 진입점.

- ADR-031 timing semantic 변경 (`DB-insert-bound` → `tick-level`). 1차 ADR-031에서 의도적으로 제외한 결정.
- 전제: ADR-027 Stage C 결정 (REST snapshot도 Redis에 반영할지) + 5/18 만기 baseline + 운영 stale 패턴 관찰.
- *behavior change* — separate ADR / PR.

**F. KrxAlertEvaluator 구현**:

- "알림은 모든 tick" 원칙 실현. KRX 단일 source 알림 평가.
- 전제: USDT/은행 알림 evaluator의 fanout 패턴 학습 후 일반화 또는 KRX 한정 1차.

**G. REST fallback Stage C 반영**:

- `_invoke_rest_fallback` 결과(현재 log/counter only)를 Redis/DB/topic에 반영.
- 위치 선택지: `KrxRestFallbackController`가 결과 emit → `KrxRedisLatestWriter` / `KrxDbWindowWriter`가 구독. 또는 controller 안에서 직접 반영.
- ADR-027 결정 영역.

## 6. Open Questions

### 6.1 quote frame 통합 여부

현재 quote frame은 liveness만, fanout 대상 X (line 1171-1176). 미래 fanout에서:

- (a) `KrxLivenessMonitor`가 quote frame도 받아 silence 측정 — 유지
- (b) `KrxRedisLatestWriter`에 *호가 정보* 별도 reflection — fanout/구조 변경
- (c) 별도 channel/topic — REALTIME_ARCHITECTURE_PLAN 영역

1차 refactor는 (a)만, (b)/(c)는 별도 phase.

### 6.2 `KrxAlertEvaluator` 위치

- 옵션 1: KRX 단일 source 알림 (현재 source_notification_settings)
- 옵션 2: 비교 알림 (KRX vs USDT 등) — Phase 3 영역, USDT_PHASE1_DESIGN.md 참고

1차에서는 stub만, 구현 시점에 옵션 선택.

### 6.3 LivenessMonitor의 alarm 출력

현재 silence는 *summary log* + *fallback eligibility*에만 영향. 만약 *runtime alarm* (예: Slack/Telegram 알림) 필요해지면 LivenessMonitor에서 별도 emit 인터페이스 필요. 1차 범위 X.

## 7. 참조

- [DECISIONS.md ADR-027](DECISIONS.md): KRX REST snapshot/fallback + stale 정책 (5/18 후 결정)
- [DECISIONS.md ADR-031](DECISIONS.md): KRX Redis 통합 (1차 완료)
- [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md) "All-source observation fanout contract": 본 문서의 anchor. freshness metadata terminology 단일 정의 + "알림 모든 tick, DB window close" 원리.
- [KRX_CANARY.md](KRX_CANARY.md): KRX Stage 0/1/2 운영 + Stage A/B baseline
- [app/crawlers/krx_kis.py](app/crawlers/krx_kis.py): 본 doc 분석 대상
- [tests/test_krx_fallback_eligibility.py](tests/test_krx_fallback_eligibility.py): eligibility 27 tests
- [tests/test_krx_redis_integration.py](tests/test_krx_redis_integration.py): ADR-031 12 tests
