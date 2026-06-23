# All-Source Observation Fanout — Unification Scope-Lock

> **상태**: SCOPE-LOCK (plan-only). **구현은 별도 세션** — 본 문서는 "무엇을 공통화하고 무엇을
> 절대 공통화하지 않는가"를 잠그는 것이 목적이다. 코드 변경 0.
> **작성**: 2026-06-23. **근거**: 3-path 코드 매핑(병렬 Workflow `wf_28a9a877` + bank/investing 복구
> Explore + codex 합의). file:line은 분석 시점 기준 — **구현 시 재확인**(문서 strict mode).
> **계약 본문은 재정의하지 않음**: 4-destination fanout / observation type / freshness 용어는
> [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md)이 단일 진실. 본 문서는 그
> 계약에 대한 **scope-lock(통합 경계 + 단계)** 만 추가한다(anchor-drift 금지, cross-ref만).

## 0. 한 줄 요약

가격 observation이 도달해야 할 **4 destination**(Redis latest / DB / Alert / Topic trigger)을 모든
소스(Bank 9 + Investing / USDT 5 / KRX)가 **하나의 공통 observation 계약**으로 흘리되, **fetch와 write
메커니즘은 소스별로 둔다**. 통합 대상은 *계약과 dispatch 배관*이지 *write 메커니즘(atomic vs v1)이나
fetch*가 아니다. 나이브하게 "전부 한 writer로" 합치면 이번 분기 사고들(decouple/Bithumb/KRX gap)의
원인인 source 비대칭을 거대한 분기문으로 되살린다 — 그것을 막는 것이 본 scope-lock의 존재 이유.

**범위 밖(out of scope)**: DXY(`latest:dxy:current` / `latest:index`)는 source observation이 아니라
보조 index — 별도 key/schema + mirror가 별도 처리(P1 §387 "DXY는 v1 유지"). 본 fanout 통합 대상 아님.

## 1. 근본 불변식 — 왜 비대칭이 존재하는가 (절대 깨면 안 됨)

| 축 | FX (Bank+Investing) | USDT 5소스 / KRX |
| --- | --- | --- |
| writer 프로세스 | **다중 프로세스** — Selenium 은행 4종(shinhan/ibk/nh/sc)이 `runner.py` **자식 프로세스**에서 `insert_bank_rates_into_db` → Redis latest 직접 write (runner.py:61-63) + 3초 mirror + startup warmup | **단일 프로세스** — WS는 in-process asyncio task (Dockerfile `--workers 1`), runner subprocess 미사용 |
| 그래서 필요한 것 | **server-side atomic**(Lua compare_write, revision `skipped_newer` 가드) — cross-process race는 in-memory 가드로 못 막음 (C6 cutover, 2026-06-21 live) | **in-memory `<` 가드로 충분**(USDT `SKIPPED_REGRESSION`; KRX는 accepted gap). server-side atomic 불요 |
| keyspace | `latest:bank:*` / `latest:investing:*` (atomic loader/mirror가 read/write) | `latest:source:*` (atomic loader/mirror가 **allowlist-skip** = disjoint) |

**이 표가 본 문서에서 가장 중요하다.** `de4fe41` decouple이 USDT/KRX를 전역 FX write-mode에서 떼어낸
이유가 바로 이 비대칭이다. **통합 작업은 USDT/KRX를 전역 mode에 다시 묶으면 안 된다**(테더 탭 freeze
재발). 단일 프로세스 전제(`--workers 1`)는 in-memory 가드의 load-bearing 조건 —
[memory: project_atomic_scope_usdt_krx] / [P1_COMMON_BASE_DESIGN.md §401-405](P1_COMMON_BASE_DESIGN.md).

## 2. 공통화 대상 (common layer)

1. **Observation 계약 타입** — 정의는 [REALTIME §4.1.2](REALTIME_ARCHITECTURE_PLAN.md)가 anchor(여기서 재정의 X). 현재 가장 근접한 코드 realization = `AlertObservation`(app/notifications/alert_evaluator.py, `{source, asset, rate, timestamp_ms, kind}`). 통합 목표 = 모든 소스가 fanout 전에 §4.1.2 observation type으로 정규화하는 것.
2. **Alert 평가(destination 3) = 이미 가장 통합됨 + 나머지의 모델**. `KrxAlertEvaluator(UsdtAlertEvaluator)` thin subclass — 둘 다 `AlertObservation` + `CachedAlertSetting` + `condition_matches_observation` + `PriceAlertCoalescer`(5s grain) + `AlertSettingsCache` 공유. **FX alert(현재 inline `crud.process_rate_alerts`)을 이 source-neutral evaluator로 올리는 것이 최고가치·최저위험 슬라이스**.
3. **DB writer 인프라(destination 2)** — `insert_source_rate_if_changed(...)` rail은 이미 **공유 인프라**(optional `timestamp=` param + `timestamp DESC` 조회). 단 **event-ts 채택은 현재 USDT 5소스만**(§12.9.8 ③, stale write가 과거 ts로 history에 남되 latest 안 됨). **KRX general tick은 save-time default**(krx_kis.py general writer가 timestamp 미전달, models.py 주석; KRX tick event-ts는 §12.9.8 ③에서 defer), **FX bank/investing도 save-ts**(`get_utc_now`). 즉 *rail은 공통, event-ts 정책 통일은 open decision*(§5-6) — KRX close/REST boundary path만 명시 ts 전달.
4. **Topic-trigger dispatch 배관(destination 4)** — `request_*` → per-topic coalesce window(configurable, **500ms default** `*_TOPIC_TRIGGER_COALESCE_MS`) → `safe_publish_*_snapshot`. `fx_topic_trigger.py`는 `tether_topic_trigger.py`의 명시적 clone. bridge + coalescer + telemetry(`trigger_*` Redis hash) + "SET-success만 trigger" 규칙은 공통. publish_func + reason 상수만 다름.
5. **Redis-write OUTCOME enum + SET-only-trigger 규칙** — `UsdtLatestWriteOutcome` / `KrxLatestWriteOutcome`가 이미 평행(FAILED/SKIPPED/SET[/SKIPPED_REGRESSION]/BLOCKED). "SET만 trigger, SKIPPED/FAILED는 silent" 규칙 공통. enum 타입은 합칠 수 있음(setter·serializer는 분리 유지).
6. **Freshness 용어**(저장 shape 아님) — `rate_changed_at`/`seen_at`/`mirrored_at`은 §4.1.3 vocabulary로만 공통. **on-wire JSON shape는 아직 공통 아님**(§4 참조).

## 3. 소스별 유지 (per-source fetch + policy)

- **fetch/ingest는 소스 소유**: USDT 5 WS client + REST helper, KRX `KisFuturesClient` WS, FX bank/investing 크롤러 + `insert_bank_rates_into_db`. 프레임 파싱·필드명·timestamp 단위가 전부 다름.
- **policy 값(= 의도된 분기점)**: liveness 임계(Gopax 300/600, Coinone 60/300, Korbit 30/120, KRX 60s, USDT 30s), REST-fallback 조건, DB debounce window, 5s coalesce grain, quantize 정밀도(KRX 0.1 KRW Decimal vs USDT raw).
- **Bithumb REST event-ts 재구성**(`_bithumb_rest_event_ms` + future-skew fail-closed) — 소스별 결함 완화, Bithumb fetch 층에 유지. OUTPUT(clean timestamp_ms)만 공통 계약으로 넘어감.
- **Gopax dual-endpoint**(write-path `/trading-pairs` vs ticker-dead `/tickers`) — Gopax-local.
- **topic publish_func + reason + cardinality**: FX=3 per-asset topic(fx:usd-krw/jpy-krw/eur-krw) vs tether=1 aggregate(usdt:krw). publish 입도·payload builder 다름.

## 4. 절대 통합 금지 (never-unify) — 사유 포함

1. **FX Redis write 경로 ↔ USDT/KRX Redis write 경로를 한 writer로 합치지 말 것.** FX는 writer 2개(legacy v1 `set_latest_bank_rate_from_sync_job` + atomic v2 `atomic_compare_write_v2`); USDT/KRX는 mode-independent v1 + disjoint keyspace. 합치면 USDT/KRX가 전역 FX write-mode gate에 재결합 = `de4fe41`이 고친 바로 그 버그(테더 freeze).
2. **Freshness JSON 저장 shape**: FX 3-field `serialize_value` vs USDT/KRX-tick 5-field `serialize_usdt_value`. 5-field는 FX legacy가 구현 안 한 in-memory coalesce+regression 의미 + client wire-compat(legacy `timestamp` alias) 운반. shape 통합은 **open decision**(§5)이지 기본값 아님.
3. **Regression/out-of-order 가드 메커니즘**: USDT seen_at-compare(`SKIPPED_REGRESSION`) / FX atomic revision-compare(`skipped_newer`) / **KRX-tick은 coalesce+state(`_last_written_krx_state`)는 있으나 regression `<` 가드 없음(accepted gap)** / FX-legacy·KRX-DB 없음 — **4가지 다른 ordering key**. 하나로 강제하면 FX atomic invariant를 깨거나 KRX의 단순 모델을 회귀시킴.
4. **단일 프로세스 in-memory state**(`_last_written_usdt_state`/`_last_written_krx_state`/coalescer buckets) — multi-process를 암묵 가정하는 무언가로 "통합"하면 안 됨. `--workers 1`에서만 correct. multi-worker는 별도 큰 결정(DB/Redis-CAS 가드 필요).
5. **KRX 세션 lifecycle**: close finalizer(CF 15:45/CM 06:00 unconditional INSERT + close-captured flag + CF daily-append), REST fallback gating, contract rollover(만기 기반 + 5분 reconcile + 07:00 swap), close-grace DB-skip. bank/investing/USDT에 등가 0. close_snapshot은 *observation TYPE*으로 공통 계약을 타지만, 스케줄링/플래그/daily-canonical 기계는 환원불가 KRX. → [KRX_FANOUT_REFACTOR_PLAN.md §5.2](KRX_FANOUT_REFACTOR_PLAN.md).
6. **Topic cardinality + payload builder**: FX 3 per-asset(per-topic flush) vs tether 1 aggregate. bridge/coalescer는 공통, topic graph + payload builder는 아님.
7. **legacy FX broadcast hook**(main.py `broadcast_rates_once` is_changed → `safe_publish_all_fx_snapshots`)은 **FX-own topic(fx:*)의 recovery publisher** + test-locked. FX C1 source-level trigger는 이미 direct 활성(§6 참조)이지만 이 broadcast hook은 PR D/E까지 공존. 제거는 **fanout 트랙의 끝**(§6 마지막), 통합 대상 아님.
8. **Alert-input 정책 ≠ Redis SET-only skip 정책** — Redis write는 5s coalesce/SKIPPED로 same-rate를 silent skip하지만, **alert는 모든 tick을 평가**(KRX `KrxAlertTickHandler`는 close-grace skip 없음, "생략 금지 조건" [USDT_WS_DESIGN_PLAN §13.10](USDT_WS_DESIGN_PLAN.md)). Redis coalesce/SKIPPED를 alert 입력에 전파하면 알림 누락. destination 1(Redis)의 skip 정책을 destination 3(alert) 입력에 묶지 말 것.
9. **Telemetry stats namespace는 소스별 유지** — §2-5의 OUTCOME *enum*은 통합 가능하나 stats는 아님: KRX writer는 `usdt_redis_stats`를 **의도적으로 미호출**(latest_rates_cache.py), `bank_investing_redis_stats`는 "USDT와 측정 대상 다름 — 통계 의미 혼선 회피" 명시. enum 통합 ≠ stats 통합.

## 5. Open decisions (구현 전/중 결정)

1. **Freshness shape 통합?** — FX가 seen_at/rate_changed_at를 가질 필요가 있나, 아니면 §4.1.3 vocabulary로 충분(shape는 per-source 유지)? **잠정: 구체 consumer가 FX seen_at을 요구할 때까지 분리 유지**. 진짜 forcing function은 mirror-retirement(§4.1.5).
2. **PR D/E hook 격하의 의존성?** — FX C1 trigger는 이미 direct_coalesced 활성(기존 publisher 기반, REALTIME §4.1.5)이라 "direct flip"은 이미 끝남. 남은 질문 = **legacy FX broadcast hook 격하(§6 step 8)가 (a) topic-protocol-v1 완성에 의존하나, 아니면 (b) C2/C3 freshness/dedup + recovery 경로 확보만으로 가능한가?** (USDT도 v1 완성 전 direct_coalesced 했으니 hook 격하의 v1 의존성은 낮을 가능성 — 확인 필요.)
3. **단일 프로세스 가정 명문화** — 공통 계약이 multi-worker 안전을 약속하나? 약속 안 함이 기본. multi-worker 원하면 USDT/KRX는 DB-revision/Redis-CAS 가드 필요(별도 큰 결정).
4. **FX가 atomic-v2 revision을 THE 공통 regression 메커니즘으로 채택? vs 2개 유지?** — 강제는 위험. topology별 정당한 선택으로 **문서화** 권장(통합 아님).
5. **KRX tick 가드 gap을 통합 단계에서 닫나?** — `_last_written_krx_state` state dict는 이미 존재해 seen_at `<` 가드 추가는 cheap. accepted gap 유지 vs 닫기를 **명시 결정**. (단 event-ts midnight helper 선행 필요 — [memory: project_atomic_scope_usdt_krx].)
6. **FX DB event-ts?** — USDT 5소스는 event-ts 저장(§12.9.8 ③), **KRX general tick + FX는 save-ts**(`get_utc_now`). 은행 크롤러가 신뢰할 거래소 event ts를 노출 안 할 수 있어 **환원불가일 수 있음 — 소스별 확인 후 약속**(§2-3).
7. **✅ RESOLVED (2026-06-23, codex 019ef2fa 확인): (a) ADAPTER — 테이블 분리 유지.** `NotificationSetting`(bank/currency)·`SourceNotificationSetting`(source/asset)은 **키 컬럼만 다르고 나머지 필드 동일**(condition/threshold/enabled/triggered/last_notified_*) → bank→source/currency→asset 매핑 trivial·lossless. 두 테이블 = **정당한 도메인 경계 + 별도 API**(`/api/source-notification-settings`는 exchange/derivative만 허용·reference/FX 거부, FX는 `/api/notification-settings` 전용). (b) migrate는 **live user alert 데이터 이전 + dual-write 정합 + API 계약 변경 = 고위험·무이득**(통합 가치=evaluator 로직은 (a)로 이미 달성). migrate가 정당한 유일 시나리오 = 장기 "FX를 source registry의 reference source로 완전 편입 + `/api/notification-settings` deprecate" 제품 결정 — shadow 범위 밖. ⚠️ **adapter = "loader override"가 아니라 alert storage backend 추상화**: codex 확인 — evaluator가 **load**(`_load_settings_from_db` 550) + **refetch**(`get_source_notification_setting_by_id` 770) + **persist**(`_persist_result` 793 = mark_triggered+log) 3곳에서 source 테이블에 결합. FX는 load/refetch/persist/payload를 backend로 분리해야 함. 구현 제약은 §6 step 4.

## 6. 단계 분해 (별도 세션, 작은 슬라이스 — atomic A1→A2 정신)

| # | step | scope | size |
| --- | --- | --- | --- |
| 1 | **본 scope-lock 문서**(이 artifact) + REALTIME §4.1.6 cross-ref 등록. 코드 0. | docs | S |
| 2 | **✅ LAND (2026-06-23)** — Redis-write OUTCOME enum 통합: `Usdt`+`Krx`LatestWriteOutcome → `SourceLatestWriteOutcome`(union 멤버, 동일 .value) + 후방 호환 alias. setter per-source 유지, return type만 공유. behavior-change-0(3205 passed, codex GO). | latest_rates_cache 타입 | S |
| 3 | **✅ LAND (2026-06-23)** — source-neutral `observation_from_tick(tick, kind)` adapter(alert_evaluator.py): **tick dict→AlertObservation 매핑만** 통합(USDT 11 site + KRX 1). ⚠️ **timestamp_ms 도출은 caller 유지**(USDT=exchange-ts passthrough / KRX=received_at 변환 — helper에 안 둠, never-unify §4-3; 구 문구의 "timestamp 변환까지 helper"는 부정확이라 정정). evaluator subclass 유지. behavior-change-0(3210 passed, -49 net lines, codex GO). | alert_evaluator.py + 6 crawler 파일 | S |
| 4 | **FX alert SHADOW(dual-run)** — `crud.process_rate_alerts` authoritative 유지 + default-OFF flag 병렬 평가(telemetry-only). **open decision 7 = (a) adapter 확정.** 제약(codex 019ef2fa): ① adapter = **storage backend 추상화**(load+refetch+persist+payload, "loader만" ❌) — `FxNotificationBackend`(notification_settings, bank=source/currency=asset) 주입, USDT/KRX default backend 불변, KRX subclass `pass` 유지(test 계약) ② **sync→async 브리지**(FX는 sync inline `process_rate_alerts`[crud.py:519/589], `evaluator.schedule()`은 event-loop 필요 — bank/investing topic bridge 패턴 재사용) ③ **shadow = pure eval**(FCM/mark_triggered/log/cache mutate 전부 no-op — 아니면 2× 발사 / legacy mutate 후 refetch-skip) + **legacy mutate 전 read-only snapshot**서 평가 ④ cache invalidation: FX endpoint 추가 or shadow TTL-0(현 FX API는 evaluator cache invalidate 안 함). | alert_evaluator backend 추상화 + crud FX fanout | M |
| 5 | **unified freshness shape 결정(emit은 나중)** — open decision 1로 FX가 seen_at/rate_changed_at 받을지 결정. yes면 reader-tolerant deserialize 뒤 schema-additive로 land(reader 동작 변화 0 먼저). | latest_rates_cache + docs | M |
| 6 | **PREFLIGHT (구현 세션 시작 시 필수)** — 현재 FX topic trigger 상태 확인: FX **C1 source-level trigger**(`BANK_INVESTING_TOPIC_TRIGGER_MODE`, SET-success → fx:* + 조건부 usdt:krw cross-route)는 **code default `legacy_piggyback`이나 prod는 .env로 `direct_coalesced` 활성**(REALTIME §4.1.5, 2026-06-15) + legacy hook 공존. **PREFLIGHT는 code default 아닌 live prod/admin mode 확인 + backward-flip 금지.** 남은 β는 piggyback→direct flip이 **아니라** C2/C3/hook 격하. | audit only | S |
| 7 | **β C2(freshness) / C3(dedup) 반영** — Bank/Investing observation의 freshness metadata(decision 1) + dedup 정책 정의·land(USDT 5b-bis/5d-a 등가). [USDT_TOPIC_MIGRATION_PLAN §6.6](USDT_TOPIC_MIGRATION_PLAN.md). | crud FX fanout | M |
| 8 | **(C1+C2+C3 coverage 입증 + PR D/E recovery 경로 확보 후에만) legacy FX broadcast hook 격하/제거** — `safe_publish_all_fx_snapshots`(main.py). 끝 단계. recovery test 갱신. tether hook(main.py)도 독립 audit. | main.py + tests | M |

### §6.1 step 4 (FX alert shadow) 상세 설계 — codex-agreed (019ef2fa, 2026-06-23, plan-only, 코드 0)

> plan-first Workflow(`wf_f45a7380`) 3 조사 + codex design GO. **구현은 별 세션**(M, design-sensitive).

**6 coupling points** (evaluator → backend/sender 추상화):
1. `load_settings` (구 `_load_settings_from_db` 562) 2. `refetch_snapshot` (구 `_refetch_setting_snapshot` 770) 3. `persist_success` (mark_triggered+log, `_persist_result` 807) 4. `persist_failure` (log fail 836) 5. `build_payload` (구 `_build_fcm_payload` 879, `data["type"]` 소유) 6. **🔴 `sender` (delivery)** — codex blocker: `_send_fcm_multicast`(785)는 ABC 5점 밖 → persist no-op만으론 **2× FCM 발사**. **constructor-injected sender**(default `send_fcm_multicast_sync`, FX shadow=no-op)로 추상화. global flag ❌.

**backends**: `SourceAlertBackend`(default, 현 동작 verbatim, USDT/KRX runtime byte-identical, **KRX subclass `pass` 유지**) / `FxNotificationBackend`(notification_settings read — **value pass-through**: source→bank·asset→currency identity[확인 `_changes_to_fcm` crud.py:275], persist=shadow no-op, payload type=`rate_alert`).

**shadow wiring**: crud.py 4 site(522/593/717/783, `process_rate_alerts` 직전) + `schedule_on_loop`(worker→main loop, crud.py:350 검증 패턴). double-fire = **committed changed_rates batch당 1회**(atomic gate 561/752로 atomic OR legacy 한 경로만 — codex 확인). **bounded sequential batch pure-eval**(TTL-0 per-observation load-task spam ❌ = threadpool/DB/pending backlog[356/517/546] 위험) + **dedicated cache**(singleton ❌ = cross-pollution + FX API invalidation[295] 부재). `timestamp_ms`는 changed_rates에 없어 ingest-time 합성 + coalescer bypass. default-OFF `FX_ALERT_SHADOW_ENABLED`.

**sub-slices** (codex GO):
- **S1** (M): AlertStorageBackend ABC + SourceAlertBackend **+ sender seam** 추출. ⚠️ gate = backend **characterization test**(empty-token 제외/refetch stale/persist 순서/failed-token cleanup) + static method를 patch하던 기존 test는 **backend patch로 갱신**("zero-edit green"은 compat-wrapper 잔존 신호라 gate 부적절 — behavior-change-0 기준 = runtime alert 출력 identical).
- **S2** (M): FxNotificationBackend (dead code, 단위 test만).
- **S3** (M): FX shadow evaluator singleton + bounded batch wrapper + **delivery no-op + state/log no-op + telemetry-only** + dedicated cache.
- **S4** (S): crud 4 site 주입 behind flag(default off).
- **S5** (ops): flag 활성 + parity 관찰(would_fire vs legacy sent_count per source/asset).

**S1 착수 준비 (pre-impl checklist, 2026-06-23 read-only 확인)**:
- **추출 대상 6**(alert_evaluator.py): `_load_settings_from_db`(def 551 / call 546) → `load_settings` · `_refetch_setting_snapshot`(762 / call 653·710) → `refetch_snapshot` · `_persist_result`(794 / call 758) → `persist_success`+`persist_failure` 분리(⚠️ **failed-token cleanup[UserDevice delete]은 evaluator 잔류** — fcm_result 기반, schema 무관) · `_build_fcm_payload`(865 / call 752) → `build_payload` · **`_send_fcm_multicast`(786 / call 754) → constructor 주입 `sender`**.
- **constructor**: `__init__`에 `backend=SourceAlertBackend()` + `sender=send_fcm_multicast_sync` default → 6 instantiation(USDT 5 + KRX) 불변. **`KrxAlertEvaluator`(910) `pass` 유지**.
- **test-update scope (bounded)**: `tests/test_usdt_ws_upbit_skeleton.py`만 기존 static seam **5개** 참조(50 refs: load 17·refetch 11·persist 11·send 11; **`_build_fcm_payload` direct ref 0** = characterization 신규 대상; ≥3 `patch.object`) — patch를 backend/sender 인스턴스로 재타겟 + characterization test 추가. **나머지 4 USDT + KRX test는 미참조(영향 0)**.
- **gate(정정)**: runtime alert 출력 identical = behavior-change-0("zero-edit green" ❌). characterization lock = empty-token 제외 / refetch stale 차단 / persist success·failure 순서 / failed-token cleanup 위치 / payload `data["type"]`.
- **risk**: session 경계(get_db_context per call 보존) / persist 분리 시 순서 / sender @staticmethod→injected(default real).

**open** (구현 시): parity tolerance 정의 / cutover 시 persist no-op→real 전환 + FX endpoint invalidation(constraint ④ option a) — shadow 범위 밖.

## 7. Sequencing 가드레일

- **legacy FX broadcast hook을 먼저 제거하지 말 것** — fx:* topic의 recovery publisher + test-locked (C1 source-level trigger는 이미 direct 활성이나 hook은 PR D/E까지 공존). 제거는 §6 마지막(C1+C2+C3 coverage 입증 + PR D/E recovery 경로 확보 후). tether hook도 동일 주의.
- **USDT를 reference로 보존** — `_last_written_usdt_state`/5s coalescer/`SKIPPED_REGRESSION`은 통합 중 **건드리지 말 것**. 가장 고충실도 경로 = 템플릿이지 수정 대상 아님.
- **enum + ObservationToAlert(behavior-change-0)를 FX alert/trigger cutover 전에 land** — 후속 diff 축소 + 독립 revert 가능.
- **FX C1 source-level trigger는 이미 direct 활성**(REALTIME §4.1.5, 2026-06-15) — USDT의 legacy_piggyback→dual_shadow→direct_coalesced 시퀀스 중 FX는 *direct까지 이미 옴*. 남은 FX β = C2(freshness)/C3(dedup) + PR D/E hook 격하. **backward-flip 금지** — 구현 세션 시작 시 현재 mode를 PREFLIGHT(§6 step 6).
- **atomic-FX 트랙과 직교** — C6 atomic v2는 막 깨지기 쉬운 prod cutover(incident+rollback, 2026-06-21)를 끝냄. `de4fe41` decouple이 USDT/KRX를 전역 mode 밖에 둠. **통합이 USDT/KRX에 전역 mode 결합을 재도입하면 안 됨**(§4-1).
- **mirror-cycle 은퇴(ADR-026 3s, §4.1.5)는 별도 후속 트랙** — measure-first/replace-before-remove. fanout 통합의 일부로 mirror 제거 금지(FX legacy v1이 safety net으로 의존).
- **KRX close-finalizer/rollover/REST-fallback는 fanout 작업이 건드리지 말 것**(직교, §4-5). close_snapshot observation만 공통 계약을 타고, 스케줄링은 이동 안 함.

## 8. Cross-references (계약 중복 금지 — 여기서 재정의 X)

- [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md) — 4-destination fanout 계약 / observation type / freshness 용어 / contract-common vs source-specific 경계 (단일 진실).
- [USDT_TOPIC_MIGRATION_PLAN.md §6.6](USDT_TOPIC_MIGRATION_PLAN.md) — Bank/Investing β 트랙의 named home (topic trigger 시퀀스).
- [KRX_FANOUT_REFACTOR_PLAN.md §5.2](KRX_FANOUT_REFACTOR_PLAN.md) — KRX-specific never-unify(close finalizer/rollover/REST fallback).
- [P1_COMMON_BASE_DESIGN.md §401-405](P1_COMMON_BASE_DESIGN.md) + memory `project_atomic_scope_usdt_krx` — FX atomic vs USDT/KRX decouple 근거(§1 불변식).
