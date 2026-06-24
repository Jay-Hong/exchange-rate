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

1. **Freshness shape 통합?** — FX가 seen_at/rate_changed_at를 가질 필요가 있나, 아니면 §4.1.3 vocabulary로 충분(shape는 per-source 유지)? ~~잠정: 구체 consumer가 FX seen_at을 요구할 때까지 분리 유지~~. **✅ RESOLVED (2026-06-24, step 6 PREFLIGHT): 분리 유지 확정.** FX broadcast read path는 3-field만 소비(`deserialize_value`: rate/timestamp/mirrored_at, latest_rates_cache.py:1923-1938), `seen_at`/`rate_changed_at` 실코드 consumer는 USDT 전용(crud.py:179-180은 docstring; rg 확인 = usdt_redis_stats/latest_rates_cache/usdt_sources/usdt_ws만), FX/main.py **0건**. mirror(ADR-026 3s)가 latest:bank:*/investing:*를 3-field로 재기록하는 공동 writer라 FX 5-field 이식은 **mirror ownership 재설계(=mirror-retirement) 선행 필요** → **C2(step 7) freshness는 mirror-retirement 트랙까지 deferred**(forcing consumer 부재 = speculative shape).
2. **PR D/E hook 격하의 의존성?** — FX C1 trigger는 이미 direct_coalesced 활성(기존 publisher 기반, REALTIME §4.1.5)이라 "direct flip"은 이미 끝남. ~~남은 질문 = legacy FX broadcast hook 격하가 (a) topic-protocol-v1 완성에 의존하나, (b) C2/C3 + recovery만으로 가능한가?~~ **✅ RESOLVED (2026-06-24, step 6 PREFLIGHT): hook 격하 = topic-protocol-v1 비의존 확정.** USDT가 v1 완성 전 direct_coalesced(2026-05-16) + hook 공존(USDT_WS_DESIGN_PLAN.md:1560/1728), full v1 handshake(hello/protocol_version/supported_topics)는 코드 미구현(main.py에 handshake handler 없음 — `/ws`가 topic_dispatcher 위임, dispatcher는 ping/subscribe/unsubscribe만), FX C1 direct_coalesced live(6/15). **남은 precondition = PR_D success-watermark reconciliation**(SET-failure 발행 복구): C1 SET-only trigger는 SET 실패분 미발화(pure miss) + 1-source-fail/타-success 시 stale aggregate 가능 + mirror는 값만 무조건 SET 복구하고 trigger 미발사라 hook 제거 시 **발행 복구 공백**. **naive mirror-trigger 금지(무조건 SET이라 storm)** → step 8은 별도 recovery trigger 설계 선결(PR_D_RECOVERY_SPEC §12). recovery latency는 hard SLA 아님(~mirror 3s + broadcast 10s).
3. **단일 프로세스 가정 명문화** — 공통 계약이 multi-worker 안전을 약속하나? 약속 안 함이 기본. multi-worker 원하면 USDT/KRX는 DB-revision/Redis-CAS 가드 필요(별도 큰 결정).
4. **FX가 atomic-v2 revision을 THE 공통 regression 메커니즘으로 채택? vs 2개 유지?** — 강제는 위험. topology별 정당한 선택으로 **문서화** 권장(통합 아님).
5. **KRX tick 가드 gap을 통합 단계에서 닫나?** — `_last_written_krx_state` state dict는 이미 존재해 seen_at `<` 가드 추가는 cheap. accepted gap 유지 vs 닫기를 **명시 결정**. (단 event-ts midnight helper 선행 필요 — [memory: project_atomic_scope_usdt_krx].)
6. **FX DB event-ts?** — USDT 5소스는 event-ts 저장(§12.9.8 ③), **KRX general tick + FX는 save-ts**(`get_utc_now`). ~~은행 크롤러가 신뢰할 거래소 event ts를 노출 안 할 수 있어 환원불가일 수 있음~~ **✅ RESOLVED (2026-06-24, step 6 PREFLIGHT): 현행 crawler contract 기준 FX는 save-ts 유지, event-ts 전환은 현재 범위 밖.** 은행/investing 크롤러 출력 = `current_rates[pair]=bare rate float`(kb.py:120 / hana.py:191,231 / investing.py:242)라 **고신뢰 published timestamp가 균일하게 입력되지 않음** — staging은 `models.get_utc_now()`(crud.py:242/787). (일부 은행 크롤러의 `selected_date` 조작[ibk.py:221 / sc.py:247]은 과거날짜 *조회*용이지 고시 event ts 파싱 아님 — codex 019ef862.) rail은 `insert_source_rate_if_changed(timestamp=)` 지원(KRX close 사용 중)이라 막힌 건 인프라 아닌 **입력 데이터 부재**. **Investing은 미래 live channel(forexpros WS/SSE, REALTIME §8 미구현) 도입 시에만 event-ts 환원 가능** — 그 전까지 FX 전체 save-ts.
7. **✅ RESOLVED (2026-06-23, codex 019ef2fa 확인): (a) ADAPTER — 테이블 분리 유지.** `NotificationSetting`(bank/currency)·`SourceNotificationSetting`(source/asset)은 **키 컬럼만 다르고 나머지 필드 동일**(condition/threshold/enabled/triggered/last_notified_*) → bank→source/currency→asset 매핑 trivial·lossless. 두 테이블 = **정당한 도메인 경계 + 별도 API**(`/api/source-notification-settings`는 exchange/derivative만 허용·reference/FX 거부, FX는 `/api/notification-settings` 전용). (b) migrate는 **live user alert 데이터 이전 + dual-write 정합 + API 계약 변경 = 고위험·무이득**(통합 가치=evaluator 로직은 (a)로 이미 달성). migrate가 정당한 유일 시나리오 = 장기 "FX를 source registry의 reference source로 완전 편입 + `/api/notification-settings` deprecate" 제품 결정 — shadow 범위 밖. ⚠️ **adapter = "loader override"가 아니라 alert storage backend 추상화**: codex 확인 — evaluator가 **load**(`_load_settings_from_db` 550) + **refetch**(`get_source_notification_setting_by_id` 770) + **persist**(`_persist_result` 793 = mark_triggered+log) 3곳에서 source 테이블에 결합. FX는 load/refetch/persist/payload를 backend로 분리해야 함. 구현 제약은 §6 step 4.

## 6. 단계 분해 (별도 세션, 작은 슬라이스 — atomic A1→A2 정신)

| # | step | scope | size |
| --- | --- | --- | --- |
| 1 | **본 scope-lock 문서**(이 artifact) + REALTIME §4.1.6 cross-ref 등록. 코드 0. | docs | S |
| 2 | **✅ LAND (2026-06-23)** — Redis-write OUTCOME enum 통합: `Usdt`+`Krx`LatestWriteOutcome → `SourceLatestWriteOutcome`(union 멤버, 동일 .value) + 후방 호환 alias. setter per-source 유지, return type만 공유. behavior-change-0(3205 passed, codex GO). | latest_rates_cache 타입 | S |
| 3 | **✅ LAND (2026-06-23)** — source-neutral `observation_from_tick(tick, kind)` adapter(alert_evaluator.py): **tick dict→AlertObservation 매핑만** 통합(USDT 11 site + KRX 1). ⚠️ **timestamp_ms 도출은 caller 유지**(USDT=exchange-ts passthrough / KRX=received_at 변환 — helper에 안 둠, never-unify §4-3; 구 문구의 "timestamp 변환까지 helper"는 부정확이라 정정). evaluator subclass 유지. behavior-change-0(3210 passed, -49 net lines, codex GO). | alert_evaluator.py + 6 crawler 파일 | S |
| 4 | **FX alert SHADOW(dual-run)** — `crud.process_rate_alerts` authoritative 유지 + default-OFF flag 병렬 평가(telemetry-only). **open decision 7 = (a) adapter 확정.** 제약(codex 019ef2fa): ① adapter = **storage backend 추상화**(load+refetch+persist+payload, "loader만" ❌) — `FxNotificationBackend`(notification_settings, bank=source/currency=asset) 주입, USDT/KRX default backend 불변, KRX subclass `pass` 유지(test 계약) ② **sync→async 브리지**(FX는 sync inline `process_rate_alerts`[crud.py:519/589], `evaluator.schedule()`은 event-loop 필요 — bank/investing topic bridge 패턴 재사용) ③ **shadow = pure eval**(FCM/mark_triggered/log/cache mutate 전부 no-op — 아니면 2× 발사 / legacy mutate 후 refetch-skip) + **legacy mutate 전 read-only snapshot**서 평가 ④ cache invalidation: FX endpoint 추가 or shadow TTL-0(현 FX API는 evaluator cache invalidate 안 함). | alert_evaluator backend 추상화 + crud FX fanout | M |
| 5 | **unified freshness shape 결정(emit은 나중)** — open decision 1로 FX가 seen_at/rate_changed_at 받을지 결정. yes면 reader-tolerant deserialize 뒤 schema-additive로 land(reader 동작 변화 0 먼저). | latest_rates_cache + docs | M |
| 6 | **✅ LAND (2026-06-24, Understand Workflow 5-reader + spot-verify)** — PREFLIGHT audit 완료. FX C1 trigger code default=`legacy_piggyback`(config.py:476), prod는 .env `direct_coalesced` 추정(6/15 flip; **live 실측은 §6.2 미완 항목 = backward-flip 금지 핵심**). SET-only 규칙 확인(legacy `redis_succeeded` / atomic `applied_for_trigger`=APPLIED만). 4-dest fanout(crud.py): D2 commit→D1 redis→D3 alert→D4 topic, 각 try/except 격리, **observation 단일 정규화 입구 부재**(alert=FCM dict / shadow·canary=AlertObservation / redis·topic=native dict = 3-way). decisions 1/2/6 RESOLVED(§5). 결론·잔여는 §6.2. | audit only | S |
| 7 | **β C2(freshness) / C3(dedup) 반영** — Bank/Investing observation의 freshness metadata(decision 1) + dedup 정책 정의·land(USDT 5b-bis/5d-a 등가). [USDT_TOPIC_MIGRATION_PLAN §6.6](USDT_TOPIC_MIGRATION_PLAN.md). | crud FX fanout | M |
| 8 | **(C1+C2+C3 coverage 입증 + PR D/E recovery 경로 확보 후에만) legacy FX broadcast hook 격하/제거** — `safe_publish_all_fx_snapshots`(main.py). 끝 단계. recovery test 갱신. tether hook(main.py)도 독립 audit. | main.py + tests | M |

### §6.2 step 6 PREFLIGHT 결과 + sequencing 결론 (2026-06-24, Understand Workflow `wf_d7cba03d`, 코드 0)

> 5-reader 병렬 매핑(C1 trigger / legacy hook / crud 4-dest / USDT-KRX ref / open decisions) + Claude spot-verify(decision 1·6 직접 rg). 모든 단정 file:line 근거.

**핵심 결론 — fanout 본체 남은 step은 전부 더 큰 트랙에 gated (deferred-by-design)**:

- **step 7 C2(freshness)** = **mirror-retirement에 gated.** FX 5-field 이식은 mirror(ADR-026 3s 공동 writer)가 3-field로 덮어쓰므로 mirror ownership 재설계 선행 필수. + FX seen_at consumer 코드 0건(decision 1) = forcing function 부재. → mirror-retirement 트랙 없이는 무의미.
- **step 7 C3(dedup)** = 최고위험(change-only INSERT 토대 변경) + 현재 DB-based dedup이 이미 동작. Redis-side dedup은 C2(=mirror world)에서만 의미. → 함께 deferred.
- **step 8 hook 격하** = **PR_D success-watermark reconciliation에 gated**(topic-protocol-v1 아님 — decision 2). C1 SET-only trigger의 SET-실패 miss + stale-aggregate를 mirror가 *값*만 복구하고 *발행*은 미복구 → 별도 recovery trigger 설계 선결(naive mirror-trigger=storm 금지).

→ **fanout 본체엔 지금 작은 저위험 코드 슬라이스가 없다 — 남은 진척은 서로 다른 2개의 더 큰 트랙으로 갈린다** (codex 019ef862 정정: 단일 "다음 레버" over-collapse 회피):
- **(A) mirror-retirement** — C2/C3의 forcing function (measure-first, REALTIME §4.1.5). naive 제거 금지 → measure first → freshness 의미 분리 → targeted reconciliation/audit → cadence 축소·제거 (replace-before-remove).
- **(B) PR_D recovery + C6-7 coordinator wiring** — step 8 hook 격하의 precondition (success-watermark reconciliation). `atomic_fx_live.py` live coordinator adapter는 있으나 caller 0 + C6-7 precondition 잔존 = **high-risk activation/wiring (저위험 아님)**, P1 §19 D-layer 트랙.

두 트랙 모두 fanout 본체의 "작은 슬라이스"가 아니라 별도 큰 결정·트랙이다.

**✅ 확인된 현 실태** (코드): C1 trigger 3-mode(legacy_piggyback/dual_shadow/direct_coalesced) + per-asset 3 topic(fx:usd/jpy/eur) + SET-only(legacy redis_succeeded / atomic applied_for_trigger) + tether cross-route 조건부(usd-krw ∧ {kb,hana,investing}). legacy hook = is_changed(payload 전체 diff, DXY 포함) ∧ active 분기 밖 발화, FX_TOPIC_ENABLED/TOPIC_DISPATCHER_ENABLED/subscriber==0 guard로 default 발행비용 ~0. C1↔hook 공존 안전 = idempotent last-write-wins snapshot(dedup 아님). 4-dest fanout = D2 commit→D1 redis→D3 alert(canary→process_rate_alerts→shadow, B1)→D4 topic, 각 격리.

**⚠️ live prod 실측 미완 (코드만으론 불가, backward-flip 금지 전 필수)**:
- `BANK_INVESTING_TOPIC_TRIGGER_MODE` prod .env 실값(code default=legacy_piggyback, CLAUDE.md 6/15 flip 기록상 direct_coalesced 추정) — EC2 .env grep 또는 admin telemetry endpoint.
- `FX_TOPIC_ENABLED` / `TOPIC_DISPATCHER_ENABLED` / `BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS` prod 값 + fx:* subscriber_count(실구독 단말 유무 = recovery 의미 전제).
- writer mode(legacy v1 vs atomic v2) = `/admin/api/atomic-write-control-status`(C6 6/21 atomic cutover 기록상 atomic 추정) → 어느 D1/D4 경로가 live인지 결정.

### §6.1 step 4 (FX alert shadow) 상세 설계 — codex-agreed (019ef2fa, 2026-06-23, plan-only, 코드 0)

> plan-first Workflow(`wf_f45a7380`) 3 조사 + codex design GO.
>
> **✅ S1 LAND (2026-06-23)**: backend 모듈(`alert_storage_backend.py` = AlertStorageBackend ABC + SourceAlertBackend) + evaluator refactor(backend 주입 + 5 호출처 rewire + 4 static 제거) **byte-identical**(refactor 후 기존 suite 동일 통과 + characterization 8 추가 → 전체 3218 passed, KRX subclass `pass` 상속). **as-built 2 deviation** (codex 019ef2fa 합의): (1) **sender seam은 S3로 defer** — S1은 `_send_fcm_multicast` 직접 호출 유지(early-bind 시 11개 test의 late-binding 깨짐 회피; codex "S3 전 가능" 가이드 정합) → **S1 backend = 4 method**(아래 "5 coupling"의 sender는 S3); (2) load의 unused `crud` import 정리(behaviorally identical, 아래 "verbatim 보존"은 logic-verbatim 의미로 정정). test 35 re-target(load 12 + refetch 11 + persist 11 + direct call 1; `_send_fcm_multicast` patch 11은 kept) + `test_alert_storage_backend` characterization 8(persist_result whole 포함). codex impl 리뷰 GO(blocker 0). **남은 S2~S5는 별 세션.**

**5 coupling** (4 backend method + 1 evaluator-injected sender — codex 019ef2fa: §6.1 초안의 6-coupling[persist 분리]을 **whole-persist로 정정**):
1. `load_settings` (구 `_load_settings_from_db` 562) 2. `refetch_snapshot` (구 `_refetch_setting_snapshot` 770) 3. **`persist_result` WHOLE** (구 `_persist_result` 794 — mark_triggered+log+failed-token cleanup 통째) — ⚠️ **split 안 함**: mark/log/cleanup이 각자 `db.commit()`(crud.py:2886/2909 + alert_evaluator:849)이라 split 시 **session/commit-order 변경 = byte-identity 위협**. whole 이동이 안전. FX shadow = persist_result **전체 no-op**(부분 no-op 시 success_count=0→failure-log 발사). 4. `build_payload` (구 `_build_fcm_payload` 879, `data["type"]` 소유). + **🔴 `sender` (delivery, evaluator constructor-injected, backend ABC 밖)** — `_send_fcm_multicast`(785) 미추상화 시 persist no-op만으론 **2× FCM 발사**. default = **lazy-import wrapper**(현 body, module-import 시점 bind 금지). global flag ❌.

**backends**: `SourceAlertBackend`(default, 현 동작 verbatim, USDT/KRX runtime byte-identical, **KRX subclass `pass` 유지**) / `FxNotificationBackend`(notification_settings read — **value pass-through**: source→bank·asset→currency identity[확인 `_changes_to_fcm` crud.py:275], persist=shadow no-op, payload type=`rate_alert`).

**shadow wiring**: crud.py 4 site(522/593/717/783, `process_rate_alerts` 직전) + `schedule_on_loop`(worker→main loop, crud.py:350 검증 패턴). double-fire = **committed changed_rates batch당 1회**(atomic gate 561/752로 atomic OR legacy 한 경로만 — codex 확인). **bounded sequential batch pure-eval**(TTL-0 per-observation load-task spam ❌ = threadpool/DB/pending backlog[356/517/546] 위험) + **dedicated cache**(singleton ❌ = cross-pollution + FX API invalidation[295] 부재). `timestamp_ms`는 changed_rates에 없어 ingest-time 합성 + coalescer bypass. default-OFF `FX_ALERT_SHADOW_ENABLED`.

**sub-slices** (codex GO):
- **✅ S1 LAND** (2026-06-23, 위 note): AlertStorageBackend ABC(**4 method** — sender seam은 S3 defer) + SourceAlertBackend(logic-verbatim) 추출 + evaluator backend 주입(5 rewire, 4 static 제거, KRX `pass` 상속). gate 통과 = backend characterization(empty-token / refetch stale / **persist_result whole mark+log+cleanup** / payload) + static-patch test backend 재타겟 + **runtime alert 출력·commit/session order identical**(3218 passed; "zero-edit green" ❌ — test 35 재타겟이 정상).
- **✅ S2 LAND** (2026-06-23, `b44da01`): FxNotificationBackend (dead code, 단위 test 4).
- **✅ S3 LAND** (2026-06-23, `b2e787c`): sender seam(alert_evaluator `__init__` sender param + `_do_send_and_persist` `(self._sender or self._send_fcm_multicast)` per-call late-bind = USDT/KRX byte-identical) + `fx_alert_shadow.py` 신규(get_fx_alert_evaluator lazy singleton[FxNotificationBackend + dedicated cache + no-op sender] / evaluate_fx_batch_shadow bounded sequential `_evaluate_async` / `_shadow_noop_sender` would_fire 카운트[FCM 0, `_counter_lock`] / get_fx_would_fire_counts + `_reset_fx_shadow_state`) + test 12. 전체 3234 passed(+12), 회귀 0(11 patch 보존). **dead-until-S4**: app·scripts 0 ref = behavior-change-0. 4-lens Workflow + 합성(GO_WITH_FIXES) + codex design+impl GO(lock snapshot/clear 확대). deploy 보류.
- **✅ S4 LAND** (2026-06-23, `3c794e2`): crud 4 site 주입 behind flag(default off) — config `FX_ALERT_SHADOW_ENABLED` + `_emit_fx_alert_shadow`(flag gate→changes→AlertObservation→bridge) + `_run_fx_alert_shadow`(sync wrapper create_task + `_fx_shadow_tasks` strong-ref) + 4-site 격리 wrapper + test 5. 전체 3239 passed(+5), 회귀 0(flag-off byte-identity), double-fire 0. live-but-inactive(env flip로 S5).
- **✅ S5a LAND** (2026-06-23, `0b96c61`): would_fire 노출 endpoint `/admin/api/fx-shadow-counts`(C7-a read-only).
- **✅ S5b LAND + prod 활성→off** (2026-06-23~24): one-shot deploy+`FX_ALERT_SHADOW_ENABLED=true`. **2026-06-24 prod crossing 실측 → would_fire=0**(legacy 발사했으나 shadow 0) 발견 → flag off.
- **✅ S6 LAND — demote** (2026-06-24, `988ea17`): prod crossing + codex 검증으로 **post-legacy async shadow는 parity 기준 불가** 판명 — legacy가 `mark_setting_triggered`(triggered=True + **enabled=False**) commit 후 shadow가 async load → 발사 setting이 enabled=False라 `load_settings`서 사라짐 + cache TTL(10s)<은행 변경 간격이라 crossing 시점 cache 만료 → would_fire 구조적 신뢰 불가. → would_fire/shadow_stats를 **보조 진단(execution-proof)**으로 demote + **parity 기준선 = crud legacy pre-mutation baseline**(`_fx_legacy_match_counts`, triggered_items 직후 mark_triggered 전) 도입 + endpoint legacy_match/shadow_stats 분리. 전체 3244 passed.
- **❌ S7 검토→폐기 (2026-06-24, tautological)**: inline dual-compute(legacy pre-mutation 지점서 새 condition 동기 비교) 검토했으나 — legacy condition(`crud:2113-2116`)과 `condition_matches_observation`(`alert_evaluator:165-168`)이 **byte-identical**(>=/<=, 동일 4-필터, rate/threshold 동일 출처)이라 **항상 일치 = tautological**(적대적 refute Workflow 3 lens + codex 확정). regression guard 가치뿐(단위 테스트가 더 싸게 커버). 진짜 cutover 위험(real persist / FCM / failed-token cleanup / cache invalidation / 중복발사 방지)은 inline으로 검증 불가(pre-mutation 동기가 그 위험을 정의상 제거). → **fanout parity-measurement 트랙 종료** (매칭이 공유라 측정할 divergence 없음).
- **🔜 cutover (open decision 7, 별도 plan-first 세션)**: legacy `process_rate_alerts` → 새 evaluator authoritative 전환. **위험 = orchestration**(real persist/FCM/cleanup/cache invalidation/중복발사). 검증 근거 = **(a) USDT/KRX 운영 실증된 공유 `UsdtAlertEvaluator` orchestration**(FX shadow가 재사용 = source-agnostic) + **(b) FxNotificationBackend 단위 테스트** + **(c) single-setting canary**(실 FX 설정 1개로 새 path authoritative + legacy 중복 방지 + FCM 수신 + notification_logs + triggered/enabled + cache invalidation 실측, KRX F-3 패턴). high-stakes user-facing이라 fresh focus 별도 세션.

**cutover canary — ✅ 구현 LAND (2026-06-24, codex 019ef7ec design + 019ef7fd impl review)**:

- **접근**: setting_id allowlist 기반 **single-setting canary**부터. shadow(S3-S6) bridge/evaluator 인프라 **재사용**(backend만 FxCanaryBackend로 교체) → S3-S6은 헛수고 아닌 cutover 토대.
- **불변식(partition)**: allowlist setting = canary만 real 발사 / legacy skip. non-allowlist = legacy 그대로 / canary는 load 안 함. ⚠️ **단 canary는 async best-effort라 "no miss 견고" 아님**(B1).
- **변경포인트**:
  1. config: `FX_ALERT_CUTOVER_CANARY_ENABLED`(default false) + `FX_ALERT_CUTOVER_CANARY_SETTING_IDS`(콤마 int set, default 빈) — config:376 패턴.
  2. `FxCanaryBackend(FxNotificationBackend)`: `load_settings`=부모(enabled+!triggered+bank/currency+device) **+ allowlist 필터** / `persist_result`=**legacy mirror**(success→`crud.mark_setting_triggered`+`crud.create_notification_log`, **failure→로그 X** [legacy FX엔 failure-log 분기 없음, `SourceAlertBackend.persist_result`:173 복붙 금지], failed-token cleanup) / `build_payload`·`refetch_snapshot`=부모 상속.
  3. canary evaluator+hook: `get_fx_canary_evaluator()`(=`UsdtAlertEvaluator(backend=FxCanaryBackend(), cache=AlertSettingsCache(), sender=None→real FCM)`, **shadow와 별 singleton/cache**) + `_emit_fx_alert_canary(changes)`(flag gate→obs→schedule_on_loop). `ev._evaluate_async` 직접(real, once-policy=refetch+delivery_allowed).
  4. legacy skip: `process_rate_alerts` for-loop(crud:2301) 최상단 `if CANARY_ENABLED and item["setting"].id ∈ allowlist: continue`.
  5. crud 4-site wiring(`_emit_fx_alert_shadow` 옆, try/except 격리).
- **🔴 codex blocker (반영 필수)**:
  - **B1 enqueue-fail miss**: legacy skip 후 canary `schedule_on_loop` 실패 시 0회 발사(miss). canary phase=best-effort 수용(1 setting 본인 watch, 명시 문서화). **확대 전 필수**: enqueue-confirmed-skip(canary enqueue 성공 시만 legacy skip) or sync fallback.
  - **B2 shutdown drain**: real FCM이라 canary task drain 필요(USDT `close()` 패턴, upbit:1037). main.py lifespan shutdown에 `get_fx_canary_evaluator().close()` 추가.
- **cache invalidation**: FX CRUD엔 invalidate 호출 없음(source CRUD와 달리 — main:2867 류 부재). canary(1 setting)=TTL 10s edge 수용(once-policy는 refetch 보장). **확대 시 hook 필수.**
- **검증**: flag off 배포(dormant, import/startup/health) → 본인 테스트 setting 1개 allowlist+flag on(force-recreate) → 실 crossing 1건: 푸시 1 / notification_logs 1 / enabled=False+triggered=True / **legacy 중복 0** / error·예외 0.
- **확대+rollback**: canary 통과 → **enqueue-confirmed-skip(B1) 추가** → 내 계정 → FX 일부 bank/currency → legacy 완전 교체. rollback=flag off or allowlist 비우기 → 즉시 legacy 복귀.
- **behavior-change-0**: flag off → `_emit_fx_alert_canary` early-return + legacy skip 미발동(allowlist 빈) → prod 동작 불변.

**B1 enqueue-confirmed-skip — ✅ LAND (2026-06-24, `adb4dec`, codex 019ef822 design GO + 019ef834 impl GO[blocker 0], CI green 28078707713)** — canary 확대(real 유저) 전 필수 보강 완료. 변경포인트 3 모두 구현 + 4-site reorder + S6b baseline gate + tests(emit bool 4 + gate 5 + 4-site trip-wire 1, codex non-blocker[wiring 회귀 가드] 반영). 3260 passed. 코드 변경은 crud.py만(canary 인프라 e859030 land). prod 미배포(canary flag off=dormant, behavior-change-0). 아래는 설계 기록:

- **목적**: 현재 canary는 best-effort — legacy skip 후 canary `schedule_on_loop` 실패(loop 미등록/shutdown/race → False) 시 allowlist setting이 legacy·canary 둘 다 미발사=**miss**. single-setting watch는 수용, 확대 전 차단 필수.
- **변경포인트**:
  1. `_emit_fx_alert_canary` → **bool 반환**: flag off/no changes/schedule_on_loop False → False, enqueue 성공 → True. (현재 schedule_on_loop 반환 버림 crud:441 → 보존.)
  2. `process_rate_alerts(db, changed_rates, canary_handled=False)` param 추가: legacy skip을 `if canary_handled and setting.id in allowlist: continue`(flag 대신 **실제 enqueue 결과** gate). + S6b baseline 제외(crud `legacy_fired` 필터)도 `canary_handled and allowlist` 기준(enqueue 실패=legacy 발사=baseline 포함).
  3. **4-site reorder**: `canary_handled = _emit_fx_alert_canary(changes)`(try/except → 예외 시 **False=legacy fallback**) → `process_rate_alerts(db, changed_rates, canary_handled=canary_handled)` → shadow(그대로 after). enqueue 성공해야만 legacy가 allowlist skip.
- **불변식**: enqueue 성공 → legacy skip + canary(1회). enqueue 실패/예외/flag off → legacy fire(fallback, **no miss**). 매칭 tautological이라 matching-divergence miss 없음.
- ⚠️ **B1 정확한 보장 = "bridge enqueue 성공 시에만 legacy skip"**(codex 정정 — "canary fire 보장" 아님; bridge True=`call_soon_threadsafe`까지, callback内 예외는 bridge가 삼킴 topic_trigger_bridge:77/103). **잔여 = enqueue 성공 후 async eval/FCM 실패 miss**(드묾, USDT/KRX 동일 수용, B1 범위 밖).
- ⚠️ **최종 cutover(legacy 완전 제거) 시**: partition miss는 사라지나 `schedule_on_loop=False`가 곧 **authoritative alert miss** → B1보다 강한 **dispatch 보장**(robust enqueue/eval — 재시도/sync fallback 등) 필요(별도, 확대 후반/cutover 직전).
- **test**: enqueue 성공→legacy skip+baseline 제외 / enqueue False→legacy fire+baseline 포함 / 예외→False(legacy fire) / flag off→canary_handled False(legacy 그대로).

**S1 착수 준비 (pre-impl checklist, 2026-06-23 read-only 확인)**:
- **추출 대상 = 4 backend method + sender**(alert_evaluator.py): `_load_settings_from_db`(def 551 / call 546) → `load_settings` · `_refetch_setting_snapshot`(762 / call 653·710) → `refetch_snapshot` · `_persist_result`(794 / call 758) → **`persist_result` WHOLE**(split ❌ — mark/log/cleanup 각자 commit이라 commit-order 보존; cleanup도 backend 잔류) · `_build_fcm_payload`(865 / call 752) → `build_payload` · **`_send_fcm_multicast`(786 / call 754) → constructor 주입 `sender`(lazy wrapper)**.
- **constructor**: `__init__`에 `backend=SourceAlertBackend()` + `sender=lazy FCM wrapper`(현 `_send_fcm_multicast` body, **module-import 시점 bind 금지**) default → 6 instantiation(USDT 5 + KRX) 불변. **`KrxAlertEvaluator`(910) `pass` 유지**.
- **test-update scope (bounded)**: `tests/test_usdt_ws_upbit_skeleton.py`만 기존 static seam **5개** 참조(50 refs: load 17·refetch 11·persist 11·send 11; **`_build_fcm_payload` direct ref 0** = characterization 신규 대상; ≥3 `patch.object`) — patch를 backend/sender 인스턴스로 재타겟 + characterization test 추가. **나머지 4 USDT + KRX test는 미참조(영향 0)**.
- **gate(정정)**: runtime alert 출력 identical + **commit/session order identical** = behavior-change-0("zero-edit green" ❌). characterization lock = empty-token 제외 / refetch stale 차단 / **persist_result whole(mark+log+cleanup commit-order)** / payload `data["type"]` / sender default lazy wrapper.
- **risk**: session 경계(get_db_context per call 보존) / persist_result whole = mark·log·cleanup commit order 보존 / sender @staticmethod→injected(lazy wrapper default).

> **✅ S2 LAND (2026-06-23, `b44da01`)**: `FxNotificationBackend`(notification_settings, **value pass-through** source=bank·asset=currency / persist=shadow no-op / build_payload=legacy `rate_alert` BANK_NAMES_KR) + FX 단위 test 4 → 전체 3222 passed. **dead code**(미주입) = behavior-change-0. codex impl GO(logic 전부 통과, stale docstring 2 catch→정정). flake de-flake 별도 commit `b84d10d`(test_schedule_returns_immediately 0.01→1.0s, [[feedback_flaky_sleep_async_tests]]).

**S3 착수 준비 (pre-impl checklist, 2026-06-23 read-only 확인 — S1서 shifted line 반영)**:

- **R1 — sender seam(S1 defer, THE key risk)**: 현 `alert_evaluator.py` call=716(`self._send_fcm_multicast`) / def=724. S3 = `__init__(sender=None)` + 호출처 `(self._sender or self._send_fcm_multicast)`. ⚠️ **early-bind 금지**(`self._sender = sender or self._send_fcm_multicast` at `__init__` → 11 patch의 late-binding 깨짐, S1서 실증). USDT/KRX(sender=None)→falsy→late-bind `self._send_fcm_multicast`→11 patch 작동. FX shadow→no-op sender 주입→FCM 0. gate: sender seam 후 `UsdtAlertEvaluator,"_send_fcm_multicast"` patch **11개 전수 통과** + USDT/KRX FCM 출력 불변.
- **R2 — dedicated cache = 이미 무료**: `__init__(cache: Optional[AlertSettingsCache]=None)`(344) → `self._cache = cache if cache is not None else get_default_alert_settings_cache()`(350) 존재 → FX shadow = `UsdtAlertEvaluator(backend=FxNotificationBackend(), cache=AlertSettingsCache(), sender=<no-op>)`. singleton(`get_default_alert_settings_cache` 304) 재사용 ❌(USDT/KRX 교차오염 + FX endpoint invalidation[295] 부재). gate: fx instance `_cache is not get_default_alert_settings_cache()`.
- **R3 — pure-eval(side-effect 0)**: persist no-op(✅ S2 검증) + no-op sender(R1) → FCM/DB/mark/log/failed-token cleanup/cache-mutate 전부 0. telemetry-only(`would_fire` counter per source/asset). gate: shadow eval 후 crud mark/log/cleanup + sender 호출 0, counter만 증가.
- **R4 — bounded eval + sync→async bridge(seam만, 실주입 S4)**: `schedule()`(367)은 observation당 `create_task`(tick 396 / non-tick 401 / coalesced 417). FX shadow는 own coalescer instance라 USDT 동급이나, S3는 **bounded sequential batch wrapper**(per-obs N task spam ❌ = threadpool/DB/pending backlog 위험)만 신설. `timestamp_ms`는 changed_rates에 없어 ingest-time 합성 + coalescer bypass(§6.1 line 98). bridge = `schedule_on_loop`(crud.py:350 패턴), crud 4-site 실주입은 **S4**.
- **S3 산출물**: (a) sender seam(R1) (b) FX shadow singleton factory(backend Fx + fresh cache + no-op sender) (c) bounded batch pure-eval wrapper (d) would_fire telemetry. **flag(`FX_ALERT_SHADOW_ENABLED`)/crud 주입은 S4** → S3 자체는 미주입 dead-until-S4 = behavior-change-0.
- **gate**: 11 `_send_fcm_multicast` patch 통과 + USDT/KRX runtime byte-identity + FX shadow factory/wrapper side-effect 0 단위 test(no-op sender·persist no-op·dedicated cache 확인).

> **✅ S3 LAND (2026-06-23, `b2e787c`)**: 위 산출물 전부 구현 — sender seam(byte-identical) + `fx_alert_shadow.py`(lazy singleton + dedicated cache + no-op sender + `_counter_lock` would_fire) + test 12. 전체 3234 passed(+12), 회귀 0. dead-until-S4(app·scripts 0 ref). 4-lens Workflow + codex design+impl GO.

**S4 착수 준비 (pre-impl checklist, 2026-06-23 read-only 확인 — investigation Workflow `w3b13ihzx` + codex grounding)**:

- **injection — 단일 공통 helper** `_emit_fx_alert_shadow(changes)`, crud 4 site(522/593/717/783) process_rate_alerts try/except 직후 1회씩 호출(`_emit_topic_triggers`:336 단일-helper 선례). ⚠️ **`changed_rates` 아닌 `changes: List[ChangedRate]` 전달** — `changes`는 `changed_at: datetime`(crud:185) 보유→`timestamp_ms` 도출, `changed_rates`(`_changes_to_fcm`:272)는 bank/currency/rate 3키라 ts 없음. 4 site 모두 `changes` local 존재(502/575/697/766).
- **double-fire 0 + FX-only 불요**: 3-way write-mode gate(561-569/752-760)로 batch당 atomic XOR legacy 1경로 → process_rate_alerts 1회(`if`-guard 내 단일 call, loop 없음). USDT/KRX는 별 함수 `process_source_rate_alerts`(2942)라 4 site는 구조적 bank/investing-only — **필터 키 불요**.
- **bridge(🔴 BLOCKER 주의)**: `schedule_on_loop`(topic_trigger_bridge:83)은 `call_soon_threadsafe`로 **sync callback만** 마샬링 → async `evaluate_fx_batch_shadow` 직접 전달 시 un-awaited coroutine warning + silent 미실행. → sync wrapper `_run_fx_alert_shadow(obs)`(loop 위 실행)에서 `loop.create_task(evaluate_fx_batch_shadow(obs))` + **module-level `_fx_shadow_tasks: set` strong-ref**(GC 방지, 선례 fx_topic_trigger:142) + `add_done_callback(discard)`. `_run_guarded`는 sync wrapper만 격리 — coroutine 예외는 S3 `_evaluate_async` per-obs try/except(alert_evaluator:468-478)가 처리, **이중 wrap 금지**. wrapper/ref-set 위치(crud vs fx_alert_shadow)는 impl 결정.
- **observation 합성**: `AlertObservation(source=c.source, asset=c.asset, rate=c.rate, timestamp_ms=int(c.changed_at.timestamp()*1000), kind="fx_change")`. timestamp_ms는 history-log-only(`_evaluate_async` 미참조)라 save-time 합성 무방. kind는 비-tick이나 evaluate_fx_batch_shadow가 `_evaluate_async` 직접 호출이라 라우팅 무관(로그 명확성용).
- **flag**: config.py `FX_ALERT_SHADOW_ENABLED = os.getenv("FX_ALERT_SHADOW_ENABLED","false").lower()=="true"`(KRX_ALERT_EVALUATOR_ENABLED:376 동형, 현재 미존재). gate = helper **첫 줄 early-return**(`from app import config as app_config; if not app_config.FX_ALERT_SHADOW_ENABLED: return` — `_emit_topic_triggers` legacy_piggyback:347 선례). flag off=schedule_on_loop 미호출=zero overhead.
- **불변식**: flag default off=prod 동작 불변(**S3 dead-until ≠ S4 flag-gated-off** — S4 후 call site 존재·도달가능하나 flag로 차단) / double-fire 0 / legacy authoritative(process_rate_alerts:2151 + persist no-op:325) 무변경 / placement은 async 마샬링이라 before/after 무관.
- ⚠️ **constraint ③(§6 step 4) 정정**: "legacy mutate 전 read-only snapshot 평가"는 over-stated — shadow input(`changes`)은 immutable DTO + shadow는 async 별 session refetch(`FxNotificationBackend.refetch_snapshot`)라 before-placement이 staleness 제거 못함. 잔여 staleness(shadow refetch가 legacy-commit한 triggered=true 봄 → under-count)는 telemetry-only 허용 divergence(would_fire≠sent_count, fx_alert_shadow:17 기록). placement은 readability상 alert try/except 직후 권장.
- **test**: flag off→schedule_on_loop 미호출(spy) / flag on+register_main_loop→sync wrapper marshal + task 생성(`_fx_shadow_tasks` non-empty) / observation 매핑 / no-loop silent skip / **4-site characterization 회귀(byte-identity, flag off)** / teardown `reset_for_tests` + `_reset_fx_shadow_state`.
- **parity(S5)** ⚠️ **S6에서 supersede**: 이 prep의 "would_fire ≥ sent_count 경향" 전제는 **틀렸음** — post-legacy async shadow는 legacy 발사 후 setting이 enabled=False라 would_fire가 구조적으로 0에 수렴(2026-06-24 prod 실측). → would_fire는 **보조 진단**, **parity 기준선 = legacy pre-mutation baseline**(crud `_fx_legacy_match_counts`). ⚠️ 최종 parity 측정은 **불요**(매칭이 legacy와 byte-identical = tautological → S7 폐기). cutover는 single-setting canary로 검증. (위 sub-slices S6/cutover 참조)
- **S4→S5 경계**: S4=wiring(live-but-inactive, env로 flip 가능, 로직 재배포 불요) / S5=`FX_ALERT_SHADOW_ENABLED=true`(force-recreate)+parity 관찰+cutover(open decision 7) 결정.

**open** (cutover plan-first 영역): cutover 시 persist no-op→real 전환 + FX endpoint invalidation(constraint ④ option a). ⚠️ parity tolerance는 불요 — 매칭이 legacy와 byte-identical(tautological)이라 측정할 divergence 없음(S7 폐기). cutover 검증은 single-setting canary.

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
