# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **BREAK1/BREAK2 수집 주기 확대 canary** (2026-09-03, 로컬 구현·배포 전): 2026-09-01 IN canary에서 검증한 레인을 야간에도 재사용한다. KB와 Hana는 BREAK1/BREAK2에서 각각 20초→10초(`KB :09/:19/:29/:39/:49/:59`, `Hana :02/:12/:22/:32/:42/:52`), Woori는 BREAK1에서 60초→30초(`:14/:44`)로 상향한다. Woori는 단일 `OrTrigger`/`max_instances=1`을 유지하고 05:04에는 `:14/:44`를 실행하지 않은 채 기존 최종 수집 기회 `05:04:53`만 남긴다. BREAK2/OUT의 Woori 제외와 OUT의 KB/Hana 1분 주기는 불변이다. 회귀 테스트는 모드별 정확한 발화초, 대상 은행 및 다른 크롤러와의 같은 시작초 부재, Woori 단일 job·1,209회 발화·최종 시각을 잠근다. 야간 canary에서는 은행별 실행/skip/마지막 성공, Hana·Woori Selenium 폴백과 동시 Chrome, `05:04:53` 실제 실행, IBK terminal 처리, OOM·memory pressure·scheduler event를 함께 본다.

- **IBK 08:00~첫 고시 전 불필요한 Selenium 억제** (2026-09-03, 로컬 구현·배포 전): GET→날짜 지정 POST→Selenium 순서는 유지한다. 정확한 당일 날짜 readback은 있지만 환율표와 공식 무고시 코드가 모두 없는 응답을 전용 상태로 분리하고, `08:00 <= t < 08:35`에만 이전 서비스일 POST로 계속한다. 주말은 요청 없이 건너뛰고 평일 공식 무고시는 최대 10일/12초 soft budget 안에서 계속 조회해 월요일·공휴일 연휴 뒤의 마지막 서비스일을 찾도록 한다. 이전 후보는 기존 완료시각-vs-DB 회귀 방지를 거쳐 같은 값은 유지하고, 더 늦은 공식 최종값은 보충하며, 과거값 덮어쓰기는 차단한다. 전송 오류, 날짜 불일치, header/value 계약, 코드가 명시적으로 판별하는 오류 문구와 과거 후보의 표 누락은 계속 즉시 Selenium 안전망으로 간다. 날짜 readback까지 같은 미지의 오류 HTML은 개장 전 화면과 완전히 구분할 수 없으므로 제한 창 안에서 Selenium 진단이 최대 35분 늦어질 수 있다는 한계가 남는다. 당일 GET도 계속 실패하고 그 응답이 이미 게시된 당일 고시를 가린 경우에는, 이 제한 분류 때문에 진단과 당일 값의 freshness 회복이 08:35 이후 첫 유효한 :34 실행까지 미뤄질 수 있다. 실제 복구 완료 시각은 이후 GET·POST·Selenium 결과에 달려 있으며 08:35가 상한은 아니다. 회귀 방지는 과거값 덮어쓰기만 막을 뿐 신선도를 보장하지 않는다. 그 판별 문구 자체도 실제 응답 캡처가 아니라 기존 테스트 fixture에서 유래한 방어적 heuristic이다. 스케줄·DB schema·08:00 전 야간 POST 경로는 불변이다.

- **배포 2 본단계 — KB·하나 10초 / 우리 30초 IN-only canary** (2026-09-01 당시 배포 전 기록; 이후 운영 배포 및 canary 검증 완료): IN 모드에서 KB 20초→10초(`:09/:19/:29/:39/:49/:59`), Hana 20초→10초(`:02/:12/:22/:32/:42/:52`), Woori 60초→30초(`:14/:44`)로 함께 상향한다. BREAK1/BREAK2의 KB·Hana 20초, BREAK1 Woori 단일 `OrTrigger`·`:53`·05:04:53 종료, OUT 주기와 제외 집합은 불변이다. 레인은 고정 cron·루프 등록 cleanup/health·호스트 cron·운영 평균 실행시간을 함께 비교해 선택했다. 매초 WebSocket broadcast를 제외한 고정 작업과 같은 시작은 Hana/graph precompute(`HH:{00,10,20,30,40,50}:12`) 시간당 6회와 KB/free snapshot(`HH:30:19`) 시간당 1회이며, 더 비싼 Selenium 시작과 대상끼리의 동일 시작초는 0이다. 재기동마다 위상이 달라지는 IntervalTrigger는 고정 레인으로 회피할 수 없으므로 배포 smoke의 `scheduler_event`·은행별 실패/연속 실패·응답시간·큐·자원 델타로 판정한다. 두 co-start에서 특정 은행만 응답시간 또는 이벤트 이상을 보이면 해당 레인을 옮기고, 스로틀·메모리·health 같은 집계 자원 트리거가 발화하면 세 은행을 함께 롤백한 뒤 단계 분리로 재시도한다. 은행 폴백, IBK, DB schema는 변경하지 않는다.

- **배포 2 선행 최소 계측 수리** (2026-09-01, 운영 배포 완료 — `ba54a80`, 이미지 `cdd9ce01`): KB·Hana·Woori의 모든 폴백 실패와 MIBANK `hard_fail` 저장 보류가 정상 반환되어 request wrapper의 성공으로 집계되던 경로를 실패 전파로 교정했다. 유효한 unchanged와 중간 폴백 성공은 기존처럼 성공이다. `/admin/api/crawler/stats`에 `consecutive_failures`를 추가하고 성공 시 0으로 초기화한다. wrapper 실행 전 발생하는 APScheduler `EVENT_JOB_MAX_INSTANCES`·`EVENT_JOB_MISSED`는 `scheduler_event=max_instances|misfire`, `job_id`, 예약시각, 발생 수를 구조화 로그로 남긴다. 실제 APScheduler 장기 실행·과거시각 job 양성 대조로 두 이벤트가 0이 아닌 신호를 내는 것을 검증한다. 주기·레인·IBK·DB schema는 변경하지 않는다. 배포 후 15분 IN smoke에서 KB 52·Hana 52·Woori 18회 성공, 실패·연속 실패·ERROR/WARNING·스케줄 누락 이벤트·memory.events max/oom/oom_kill 모두 0, 스로틀 period 23.0%(기준선 22.0~22.5%)였다.

- **IBK 공식 날짜 지정 Request fast path** (2026-08-29 작성 당시 미배포; 이후 운영 반영 및 야간 동작 확인): 매분 수집 주기는 유지한다. 08:00 이후에는 조회 당일 GET 뒤 공식 `inDate` POST를 사용하고, 08:00 전에는 비어 있는 당일 GET을 생략해 전 조회기준일 POST로 바로 시작한다. 공식 상세 화면의 주간 1회차가 08:26:29에도 관측되어 08:30 경계의 잠재 누락을 피하도록 조회기준일 rollover를 안전한 공백인 08:00으로 잡았다. 주말 조회기준일 후보는 요청 없이 건너뛰며, 실제 조회한 평일 후보는 날짜 readback이 일치한 정확한 무고시 응답일 때만 더 과거로 lookback한다. 응답은 날짜 readback·3개 통화 완전성/범위·고시완료시각을 엄격히 검증하고 미래 완료시각은 120초까지만 허용한다. 12초는 **새 후보 요청 시작**을 제한하는 soft budget이며, 이미 시작한 요청의 hard wall-clock deadline은 아니다. 무고시 뒤 과거 후보는 공식 완료시각이 DB 저장시각보다 뒤이면 catch-up을 허용한다. 통화별 DB 저장시각이 공식 완료시각보다 120초 넘게 최근이고 값도 다르면 그 통화만 보존하고, 누락되거나 안전한 통화는 채워 부분 DB도 완성한다. 모든 후보가 무고시일 때는 DB 3개 통화 snapshot이 완전한 경우에만 유지하고, 빈/부분 DB는 bootstrap fallback한다. 00:00~00:05에도 날짜 POST는 실행하되 실패할 때만 Selenium을 억제하고, 그 이후 이상은 기존 Selenium→조건부 MIBANK 안전망으로 넘긴다. 공식 일자별 표본(2026-08-18~27)에서 야간 꼬리 고시가 있었던 8개 세션 모두 05시 이후 USD 실제 변경이 확인되어 2분 감속은 적용하지 않았다. scheduler(매분 `:34` + terminal 2회), Docker 800M 상한, DB schema는 변경하지 않았다. 정상 야간 Chrome 제거가 목적이며 메모리·큐 개선은 배포 후 canary로 판정한다. 회귀 테스트는 공식 POST/strict parser, 무고시·주말·공휴일 lookback, stale 회귀 차단/catch-up, 빈·부분 DB bootstrap, soft admission budget, fallback/자정·08:00 경계를 잠근다.

- **배포 2A — OUT(주말) 6 소스 1분 주기 + 초 분산** (2026-08-28): `investing`·`kb`·`hana`·`bs`·`nh`·`shinhan`을 10분/60분 → **1분**으로 상향하고 초를 분산(`investing :08 / nh :10 / dxy :21 / kb :28 / shinhan :30 / hana :38 / bs :51`). `dxy`는 주기와 기존 catch-up grace 120초를 유지하고 초만 `:15`→`:21` — **KB 뉴스(*/5분 :15)와 고정 시작초 분리**. 구 `kb`·`hana`의 `:45`도 RSS 뉴스(*/5분 :45)와 충돌하던 것이 분산으로 해소. Selenium enqueue 2개(nh·shinhan)는 20초 간격. 주기가 바뀐 6개의 `misfire_grace_time`은 구 300/1800→30초로 맞춰 오래 지연된 실행보다 다음 정상 슬롯을 우선한다. ⚠️ **IN/BREAK1/BREAK2 코드 블록은 diff 대조상 불변** — 같은 배포 밤의 BREAK1 관측과 격리된다. ⚠️ 주말 실측(최근 30일 OUT 변경행)은 hana 18 / shinhan 6 / investing 1 / kb·bs·nh 0이었고 bs·nh·shinhan은 60분 폴링이라 표본이 성겼다 — **이번 주말이 그 판정 실험**이며 다음 주에 재측정해 유지/축소를 정한다. 회귀 잠금: `TestOutModeSchedule` 6종(등록 집합·1분 발화·고정 시작초/예약 초 회피·Selenium 간격·misfire/max-instance·제외 소스).

- **야간 고시 연장 대응 — 모드 경계 재조정 + DXY 정책 시계 분리** (2026-08-28, ADR-042): 한국 외환시장 24시간 전환으로 우리 **익일 05:00** / IBK **익일 06:00**까지 고시가 연장된 것을 사용자가 은행 페이지에서 직접 확인 → 구 BREAK1이 03:00에 끝나 **03:00~06:00 고시를 수집하지 못하던 공백**을 메움. 은행 모드 `IN 08:00~18:59 / BREAK1 19:00~06:00 / BREAK2 06:00~08:00`. 은행별 종료는 전역 모드가 아니라 **cron 시간 범위**로 표현 — shinhan `hour='19-23,0-2'`(마지막 02:59:18, 현행 고정) / woori는 두 CronTrigger를 **OrTrigger로 묶은 단일 job**(`hour='19-23,0-4'` + `hour='5', minute='0-4'` → 마지막 **05:04:53**. 별 job으로 나누면 `max_instances=1`이 배타가 아니라 지연 시 Chrome 2개 위험) / 신규 `task_ibk_terminal`(`hour=6, minute='0,1', second='34', **day_of_week='tue-sat'**` → IBK 최종 고시(**05:59:55** 관측) 대비 **추가 수집 시도 2회**(포착 보장 아님). 화~토인 이유는 IBK 평일 세션이 익일 06:00에 화~토 아침에만 종료되기 때문). BREAK1 **시작**을 21:00→19:00으로 당긴 근거 = IN과 BREAK1의 등록 job이 `task_sc` 하나만 다르고, SC는 30일 22영업일 실측에서 **18:00 이후 포착된 변경 0건**(최종 17:49) → 평일 SC subprocess 120회/일 감소. **DXY 외부 fallback 정책은 이 경계를 따라가지 않는다** — `market_mode.get_dxy_policy_state()`(`ACTIVE`/`QUIET`/`WEEKEND_PRESERVE`)로 분리해 구 경계를 동결(따라가면 19:00~21:00 장애 복구가 20초 또는 최대 15분 지연). WEEKEND_PRESERVE는 기존 정책 계약과 silent-stale 호출 경로를 보존하지만, ICE 휴장 가드가 외부 chain보다 먼저이고 direct-latest flag가 기본 off라 현재 운영의 72h/heartbeat write 효과를 뜻하지 않는다. 알 수 없는 상태는 전용 예외로 fail-fast해 CSS fallback 저장으로 둔갑하지 않는다. 회귀 잠금은 구 로직 **동결 사본**과 일주일 10,080분 1:1 대조, 실제 job 등록/배선, 15분·30분 경계, 가격 가드, CNBC→Yahoo chain을 검증한다. DB 스키마 변경 없음 → 이전 이미지 즉시 롤백 가능.

- **admin `/admin/api/bank-investing-redis-stats` + process-local SET-outcome telemetry (item 4)** (2026-06-15, `7ab51ec` — prod deploy 2026-06-15 verified): 신규 `app/bank_investing_redis_stats.py`가 `set_latest_bank/investing_rate_from_sync_job`의 Redis direct-SET 시도/성공/실패를 원인 3종(`client_unavailable`/`writer_exception`/`set_exception`)+`unknown`으로 process-local 집계 (derived aggregate + `consecutive_failures` + `last_*`). writer는 3-zone 배선 + `_safe_record_bank_investing_set_stat` hot-path 격리(telemetry 예외 비전파) + `_get_sync_client` 보호로 **bool 계약 불변**. Redis 미저장 이유 = Redis 장애 순간의 실패까지 기록해야 해 Redis를 sink로 못 씀(process 재시작 시 reset, `started_at` 기준). admin endpoint는 read-only(reset route 없음). **`trigger_*`(SET-success만 카운트)가 못 재는 SET-fail 빈도·원인 계측** = PR D precondition 계측 축. tests +25(모듈 12 + 배선 11 + endpoint 2). ⚠️ SET-failure 복구 발행 경로(correctness)는 별도 후속(failure-injection characterization test → recovery 설계).
- **admin `/admin/api/topic-status/fx`에 C1 trigger_* telemetry 노출** (2026-06-15, `69357ed` — repo land + CI green, **prod deploy 2026-06-15 verified**): `get_fx_topic_telemetry`가 Redis-backed trigger counter 10 + `trigger_last_*` 6을 `trigger_` prefix로 추가 노출(publisher 필드와 분리). `trigger_no_loop`은 loop 부재 시만 발생→Redis 미기록(in-process `no_loop_skipped`만)이라 미노출. **publish/trigger hot path 동작 불변 + admin 응답 필드 additive 변경**(SSH HGETALL 대신 admin endpoint 사용 가능). tests +4.

### Fixed

- **계정 삭제의 사용자 소유 데이터 범위 완성** (2026-08-19): `DELETE /api/user/me`가 기존 일반·source 알림 데이터와 기기 토큰뿐 아니라 `comparison_alerts`, `comparison_notification_logs`, `user_entitlements`도 같은 트랜잭션에서 삭제. 대상 사용자 8테이블 삭제, 타 사용자 격리, 실패 시 전체 rollback 회귀 추가.
- **KRX close write gate 1 calendar — CM 야간장 시작일 기준** (2026-06-13, `78b02be`, [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md)): `_evaluate_close_write_gates` gate 1이 `is_krx_business_day(boundary date)`로 판정해 금요일밤 CM(토요일 06:00 boundary)의 REST fallback 평가(WS 종가 미capture 시 — capture되면 REST 자체 skip)에서 토요일을 휴장 오판 reject (scheduling `is_close_snapshot_eligible`와 불일치). → `is_close_snapshot_eligible(session, boundary_date)` 재사용 (CF: boundary 당일 / CM: 야간장 시작일 boundary−1). 5/25형 미등록 휴장 보호는 gate 3(session evidence) 유지. **shadow-only** — `KRX_CLOSE_REST_WRITE_ENABLED=false` 동안 verdict telemetry만 변경(실제 write 변화 0); flag=true는 §5.7.8 **gate-checked write**(구 "무가드 복원" 폐기), 활성화는 6/15 rollover 후 별도 GO → **2026-06-15 16:12 prod 활성 완료**. gate-level test +2 (금요일밤 CM 통과 / negative-control 금요일 휴장 reject).
- **KRX close REST write source 격리** (2026-05-25 정책 PR, `6a43785`, [DECISIONS.md ADR-027 follow-up](DECISIONS.md) / [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md)):
  - **계기**: 2026-05-25 휴장일 사고 (`170380f` hotfix 후속) + 5/19 만기 stale 실측. KIS REST가 휴장/만기 후에도 stale 응답을 `rt_cd=0` 정상 형식으로 반환 → 자동 코드로 stale 인지 불가 → REST 응답만으로 "오늘 종가"임을 증명할 수 없음.
  - **결정**: `KRX_CLOSE_REST_WRITE_ENABLED` env 신규 (default `false`). `KrxCloseSnapshotController._sync_write` 안 sanity check 통과 후 DB/Redis write 직전 분기 + retry short-circuit (같은 stale 값 3회 확인 무의미). `KRX_CLOSE_FINALIZER_ENABLED` 값과 무관하게 KrxCloseSnapshotController의 REST write path 전체 차단 — finalizer=true 경로의 fallback 1회와 finalizer=false rollback 경로의 retry 3회 모두 단일 분기로 차단.
  - **유지**: `KrxCloseWindowWriter` (WS close frame, 신뢰 source). REST fetch + sanity check는 diagnostic으로 유지 (case A/B/C telemetry 보존).
  - **새 counter** `rest_write_blocked`: case B 의 write 차단 signal. `KrxCloseSnapshotController` 내부 `success` counter는 retry short-circuit 위해 `return True` 사용 → 의미상 "attempt sequence completed", 운영 해석은 `rest_write_blocked` counter와 함께.
  - **Case B 의미 변경**: 이전 "REST → Redis 갱신" → 이후 "REST diagnostic 호출 → `rest_write_blocked` +1, Redis latest는 직전 정상 영업일 종가 유지".
  - **Rollback** (5/25 당시 기준): env `KRX_CLOSE_REST_WRITE_ENABLED=true` + force-recreate fastapi (기존 1차/2차 PR write 동작 복원 — 단 KIS REST stale 위험 동반). **[6/10 §5.7.8 gate-checked 재설계 + 6/15 prod true 활성 이후엔 rollback 방향 = false — 위 [Unreleased] KRX gate 1 항목 / KRX_CANARY §Rollback 참조]**
  - 신규 3 tests (`TestKrxCloseSnapshotControllerRestWriteBlocked`): finalizer=true + captured=false / finalizer=false rollback / flag=true legacy. 기존 `TestKrxCloseSnapshotController.setUp`에 `KRX_CLOSE_REST_WRITE_ENABLED=True` patch 추가 (legacy 회귀 잠금). 173 tests passed.
  - Stage E ([KRX_FANOUT_REFACTOR_PLAN.md §5.2 E](KRX_FANOUT_REFACTOR_PLAN.md))는 close finalizer 정책과 직교 작업 — 별도 진입.

- **KRX 2026-05-25 부처님오신날 대체공휴일 추가 + 5/26 06:00 CM skip 회귀 잠금** (hotfix, `170380f`, [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md) / [KRX_CANARY.md "2026-05-25 휴장일 사고 + 대응"](KRX_CANARY.md)):
  - **운영 사고 (2026-05-25 15:45 KST)**: 한국 부처님오신날(5/24 일요일) 대체공휴일로 KRX 휴장이었으나 `KRX_2026_KNOWN_HOLIDAYS`에 2026-05-25 미등록 → `is_close_snapshot_eligible("CF", 2026-05-25)=True` → close snapshot REST fallback 발동 → KIS REST가 5/22 (금) stale 종가(rate=1516.8)를 `rt_cd=0` 정상 응답으로 반환 → DB row id=429785 + Redis latest를 `2026-05-25T15:45:00+09:00` timestamp로 잘못 기록 → 단말 노출.
  - **운영 데이터 보정 (commit 외)**: DB row id=429785 DELETE + Redis latest를 직전 정상값(5/22 야간 CM close, rate=1519.9, timestamp=`2026-05-23T06:00:00+09:00`)으로 복구. close_captured flag는 미존재(ttl=-2)라 별도 작업 불필요.
  - **Hotfix**: `app/sources/kis_futures.py`의 `KRX_2026_KNOWN_HOLIDAYS`에 `date(2026, 5, 25)` 추가 (kwatch.kr/markets/kr/trading-days cross-check). 5/26 06:00 KST CM은 today-1=2026-05-25 business day check를 거치므로 본 entry로 자연 차단.
  - 신규 4 tests: 5/25 휴일 잠금(2 tests in `TestKrxBusinessDay`) + 5/25 CF skip + 5/26 CM skip (mock 없이 실제 캘린더 entry 기반 in `TestBoundaryHelpers` — 캘린더에서 5/25 빠지면 즉시 fail). 32 tests passed.
  - **Production sanity 검증**: 배포 후 `is_close_snapshot_eligible("CM", date(2026, 5, 26))=False` (5/26 06:00 CM 재발 차단 확정).
  - 다른 2026 한국 공휴일 (9/24, 9/25, 10/9, 12/25 등)은 별도 캘린더 보강 PR로 KRX 운영 정책 cross-check 후 추가.

### Added

- **USDT legacy REST polling 비활성** (2026-05-25, `ed0885c`):
  - `USDT_LEGACY_REST_POLLING_ENABLED` env flag 신규 ([app/config.py](app/config.py)) — default `false`로 상시 REST polling(`collect_usdt_rates` 매분 06,16,26,36,46,56초) cron 등록 차단.
  - 5b/5d-a series로 WS fanout이 Redis(tick path + 5s grain coalescing) / DB(1초 window writer) / Alert(coalescer) 책임 처리 + source-specific REST fallback probe가 stale 시 동일 fanout 재사용 — 상시 polling 중복. WS 도입 전 과도기 잔재 제거.
  - `_register_usdt_legacy_polling_job(target_scheduler) -> bool` helper 추출 ([app/scheduler.py](app/scheduler.py)) — flag=false 시 add_job 미호출 + "USDT legacy polling disabled (WS + fallback REST active)" 로그.
  - `app/crawlers/usdt_sources.py` (collect_usdt_rates, `fetch_*_usdt_tick` helper)는 손대지 않음 — WS fallback controller가 `fetch_*_usdt_tick`을 재사용.
  - alert path 흡수 확인: WS `UsdtAlertEvaluator._load_settings_from_db`가 legacy `process_source_rate_alerts → get_triggered_source_settings_for_rate`와 **같은** `SourceNotificationSetting` 테이블 + 같은 filter + 같은 FCM dispatch — alert 누락 없음.
  - 신규 tests 2개 (`tests/test_usdt_legacy_polling_scheduler.py`) — flag false 미등록 + flag true 기존 cron(06,16,26,36,46,56) 등록.
  - 운영 검증: 배포 직후 EC2에서 `USDT_LEGACY_REST_POLLING_ENABLED=False`, `_register_usdt_legacy_polling_job` helper 노출, `usdt_sources` APScheduler job 미등록, disabled 로그 발화, 5 source Redis latest 정상 적재 (upbit/bithumb/coinone/korbit/gopax) 모두 확인.
  - Rollback: env `USDT_LEGACY_REST_POLLING_ENABLED=true` + `docker compose up -d --force-recreate fastapi` (1분 안 cron 복원).

- **(5d-a) UsdtLatestWriteOutcome enum + topic trigger SET-only gating** (2026-05-25, `59276af`, [USDT_WS_DESIGN_PLAN.md §12.8.3](USDT_WS_DESIGN_PLAN.md)):
  - `set_latest_usdt_rate_from_sync_job` 시그니처 `bool` → `UsdtLatestWriteOutcome` enum 변경 (`{FAILED, SKIPPED, SET}` — bool truthiness 모호성 제거).
  - 5 USDT WS source writer (upbit/bithumb/coinone/korbit/gopax) trigger 분기: `outcome is SET` → `request_tether_topic_trigger` 발사 (change notification), `outcome is SKIPPED` → silent (5s grain coalesce는 동일 값/동일 bucket이라 topic publish 불필요. Topic = change notification 역할이지 heartbeat 아님), `outcome is FAILED` → warning + 차단.
  - REST polling `_mirror_changed_source_to_redis`는 외부 `bool` interface 유지 — SKIPPED를 success로 처리 (false-positive warning 회피).
  - TetherTrigger 회귀 테스트: SKIPPED → trigger 미호출 명시 검증 (Upbit 대표, 5 source 동일 패턴 + 누군가 'is not FAILED'로 바꾸면 즉시 fail 가드).
  - 13 files changed, +226/-118. test_source_direct_write.py + 5 source skeleton tests `return_value=True/False` + `return True/False` (fake_to_thread) 모두 SET/FAILED enum으로 일관 변환.

- **KRX close snapshot 1차 PR** (2026-05-15, `c0855ff`, [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md)):
  - CF 15:45 / CM 06:00 단일가 종가 누락 보강. 운영 EC2 로그 + DB 7~14일치 실측 기반 (CF 5/7 누락, CM 1/8 누락) — Codex/Claude 8라운드 합의.
  - `KrxCloseSnapshotController` 신규 ([app/crawlers/krx_kis.py](app/crawlers/krx_kis.py)): `KrxRestFallbackController` (stale-based)와 책임 분리, session boundary trigger.
  - REST close snapshot 1~3회 bounded 호출 (CF: 15:46:00/15:46:30/15:47:00, CM: 06:01:00/06:01:30/06:02:00, 첫 성공 시 short circuit).
  - boundary timestamp 분리 명시: DB는 UTC naive (`SourceRate.timestamp` 호환), Redis는 KST ISO string.
  - Primary correctness target = Redis latest (unconditional overwrite). DB는 best-effort history — `ORDER BY timestamp DESC`가 official close를 보장하지 않음 명시 (별도 PR scope).
  - `crud.insert_source_rate_if_changed(..., timestamp: Optional[datetime] = None)` 시그니처 추가 (backward compat).
  - boundary 명시 생성 helper (`compute_close_boundary_kst` / `is_close_snapshot_eligible`) — CM의 calendar day(today)와 business day check(today-1) 분리.
  - Sanity check ±2% — REST 응답이 직전 latest 대비 비합리적 차이 시 abort + 다음 retry.
  - 만기일 11:30 expiring CF: 1차 PR scope 제외 (07:00 swap 정책으로 자연 처리). 만기일 CM 06:00은 정상 (07:00 swap 전).
  - 휴장일 skip / contract boundary capture (retry 재resolve 금지) / explicit session 파라미터.
  - 23 신규 tests + 전체 784 tests OK.
  - 배포 전 운영 영향 0. 배포 후 `KRX_FUTURES_ENABLED=true` 환경에서는 CF/CM boundary 직후 KIS REST close snapshot이 활성화되며, 호출량은 일 최대 6회.

- **Phase B.2 — Tether topic publish trigger 분리** (2026-05-15, [USDT_WS_DESIGN_PLAN.md §14](USDT_WS_DESIGN_PLAN.md)):
  - 기존 `main.py broadcast_rates_once is_changed` piggyback 임시 hook → `TetherTopicTriggerController`로 분리 (Phase B.2 PR1~PR3 land, PR4 Step A `direct_coalesced` 운영 활성화).
  - **PR1** (`f16d908`, 2026-05-15): controller skeleton + env 2개 (`TETHER_TOPIC_TRIGGER_MODE`, `TETHER_TOPIC_TRIGGER_COALESCE_MS`) + lifespan close hook. mode default `legacy_piggyback` (단말 publish 영향 0, strict noop).
  - **PR2** (`2d6cece`, 2026-05-15): `UpbitRedisWriter._write_async` lock 밖에서 trigger 호출 + Redis-backed telemetry (`topic:tether:stats` hash에 `trigger_*` prefix counter/last fields). `TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS` 상수. GC strong reference (`_telemetry_tasks` set + `add_done_callback`).
  - **PR3** (`7c67e67`, 2026-05-15): `KrxDbWriter._sync_db_write` → `bool` return (inserted=True AND redis_write_success=True 합성). `_flush_after_window` finally 밖에서 trigger 호출 (PR2 lock 밖 패턴 mirror). `TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS` 상수. `KrxRedisLatestWriter.write_after_db_insert` bool propagate.
  - default `legacy_piggyback` 유지 → 단말 publish 영향 0. **trigger telemetry Redis HSET은 발생** (USDT tick-level ~수만/일, KRX 1초+변경 ~수천/일). best-effort 격리 (`circuit.record_failure` 미호출).
  - **PR4 Step A** (2026-05-16): `dual_shadow`를 건너뛰고 `direct_coalesced` 직접 활성화. 운영 App Store 단말에는 테더 탭이 없고, iOS dev 단말로 직접 검증 가능하며, direct 모드에서도 baseline counter 측정 가능하다는 사용자/Codex/Claude 합의.
  - 비구독 path 검증: `trigger_publish_called == hook_called == skipped_no_subscribers` (15:18→15:29 delta `511 == 511 == 511`). 구독 path 검증: `trigger_publish_success`, `built`, `publish_called`, `publish_sent_total` 동시 증가. iOS dev 단말에서 Upbit 가격 갱신이 거의 실시간으로 체감됨.
  - 장기 방향: 모든 테더 탭 표시 자산이 Redis latest write-through 성공 지점 기반 trigger를 갖춘 뒤 `main.py` legacy hook 완전 제거. 신규 단말은 topic 구독 모델만 사용하고, broadcast cycle은 구버전 호환 후 deprecate.

- **KRX 미국달러선물 Stage 1 canary** (2026-05-06, PR6 시리즈, ADR-027 초안):
  - KIS Open API WebSocket 기반 KRX 미국달러선물 수집 경로 추가 (`source="krx"`, `asset="usd-krw-futures"`)
  - 주간 CF TR: `H0CFCNT0` / `H0CFASP0`, 야간 CM TR: `H0MFCNT0` / `H0MFASP0`
  - KIS approval_key cache + 만료 5분 마진 + asyncio.Lock 기반 중복 발급 방지
  - KIS 상품선물 master (`fo_com_code.mst`) 기반 active contract resolve + 만기일 11:30:00 inclusive intraday rollover helper
  - `KrxDbWriter`: 1초 window debounce + `insert_source_rate_if_changed` + `asyncio.to_thread` DB write 격리
  - KRX optional source 토글: `KRX_FUTURES_ENABLED`(수집 lifecycle), `KRX_BROADCAST_INCLUDE`(Redis `latest:index` / broadcast 노출)
  - Stage 1 운영: DB(`source_rates`) 저장만 활성화, `KRX_BROADCAST_INCLUDE=false`로 broadcast/app 노출 차단
  - `KRX_CANARY.md`: Stage 0/1/2 runbook, SQL/Redis 검증 명령, 24h baseline 지표, 5/18 만기 관찰 시나리오
  - `ADR-027` 초안: Stage 2 진입 전 REST snapshot/fallback + stale 정책 결정 항목 정리

- **Redis-first broadcast hot path** (2026-05-04, ADR-026, PR3-PR5):
  - 신규 모듈 `app/latest_rates_cache.py` — Redis latest mirror layer (cache.py·crud.py 변경 0)
  - mirror keys: `latest:bank:{bank}:{currency}`, `latest:source:{source}:{asset}`, `latest:investing:{currency}`, `latest:dxy:current`
  - 제어 key: `latest:index` (atomic snapshot 일관성 보장)
  - mirror cycle: APScheduler IntervalTrigger 3초
  - DXY mirror 추가 (PR5) — broadcast hot path에서 DXY DB SELECT 제거
  - DXY-only fallback 패턴 (rates Redis 성공 + DXY Redis 실패 시 DXY만 DB)
  - 신규 env: `REDIS_LATEST_ENABLED`, `LATEST_MIRROR_INTERVAL_SECONDS`
  - 신규 메트릭: `latest_source` / `mirror_age_ms` / `latest_index_get_ms` / `latest_data_get_ms` / `latest_decode_ms` / `latest_dxy_get_ms` / `dxy_path` / `latest_dxy_fallback_reason` / `payload_assemble_ms` / `payload_assemble_without_dxy_ms` / `payload_build_unmeasured_ms`
  - `fetch_rates_from_redis()` 35 sequential GET → MGET 1회 통합 (PR4)
  - `scripts/analyze_broadcast_metrics.py` PR3-PR5 메트릭 집계 추가

- **DXY 현물 CNBC 외부 fallback 추가** (2026-04-28, ADR-025):
  - 외부 fallback chain: CNBC `.DXY` (1순위) → Yahoo Finance (최후 보루)
  - `app/crawlers/dxy.py`에 `fetch_dxy_from_cnbc()` 추가 (ICE U.S. Dollar Index 공개 quote endpoint)
  - 운영 측정상 Yahoo의 10분 stale 한계를 줄이는 효과 (CNBC는 Investing와 차이 median 0.006)
  - 같은 시간 가드 + fresh-age diff guard 적용 (ADR-022 정책 그대로)
  - chain 내 한 source가 diff guard로 차단되면 다음 source(Yahoo)도 시도하지 않음 (외부값 자체 outlier 신호로 간주)
  - chain 내 fetch 실패는 다음 source로 진행 (네트워크 일시 장애 vs outlier 구분)

- **미국달러지수 선물 분리 저장** (2026-04-27, ADR-024):
  - `instrument='dxy_futures'`로 별도 저장 (`market_index_rates` 테이블)
  - exchange-rates-table `#sb_last_8827`을 환율과 동시 추출 (추가 HTTP 요청 0)
  - 폴백: `/currencies/us-dollar-index` (60초 쿨다운)
  - 향후 테더 탭의 KRX USD 선물 비교 그래프 준비
  - 현재는 raw 수집/저장만 완료 — 조회/그래프/rollup/테더 탭 연결은 후속 작업
  - 활성화/비활성화는 `crawler_config.investing` 토글에 종속 (별도 등록 없음)

- **USDT Phase 1 data feed + source 알림 API** (2026-04-23):
  - **거래소 5종 USDT/KRW 수집** (업비트, 빗썸, 코인원, 고팍스, 코빗)
    - 단일 scheduler job `usdt_sources` (매분 06,16,26,36,46,56초, 24/7 상시)
    - ThreadPoolExecutor fan-out (5개 REST API 병렬, per-source 2s timeout)
    - 변경 시에만 INSERT 정책 (insert-if-changed)
  - **새 테이블 3개**:
    - `source_rates` (source + asset + rate + timestamp, 30일 보관)
    - `source_notification_settings` (source/asset 기반 알림 설정, 기존 notification_settings와 분리)
    - `source_notification_logs` (알림 발송 히스토리, success/error_message 포함)
  - **신규 모듈**:
    - `app/source_registry.py` (SourceDefinition 데이터클래스, 9개 소스 메타데이터)
    - `app/crawlers/usdt_sources.py` (USDT 크롤러)
  - **기존 API 확장 (신규 endpoint 없음)**:
    - `/api/rates`: `rates` 배열에 USDT 엔트리 포함 (`currency=usdt-krw, bank=upbit/bithumb/...`)
    - `/api/rates/{currency}`: `/api/rates/usdt-krw` 지원
    - WebSocket `rates` 배열 자동 확장 (`build_rates_payload()` 경유)
    - `get_source_rates_as_legacy_format()` 어댑터로 source/asset → bank/currency 변환
    - registry sort_order 기준 정렬 (업비트 → 빗썸 → 코인원 → 고팍스 → 코빗)
  - **신규 API** (`/api/source-notification-settings`):
    - POST/GET/PUT/DELETE 4개 엔드포인트
    - `is_phase1_source()` + `category=="exchange"` 서버측 검증 (dead alert 방지)
    - reference 소스(investing/kb/hana)는 기존 `/api/notification-settings` 사용 안내
  - **알림 발송 루프**: `process_source_rate_alerts()` (usdt_sources에서 호출)
    - FCM payload type: `source_rate_alert` (기존 `rate_alert`와 구분)
    - 1회성 발송 (triggered=True + enabled=False)
    - 성공/실패 모두 `source_notification_logs` 기록
    - 표시명은 `source_registry.display_name` 사용
  - **계정 삭제 확장** (App Store 5.1.1(v) 컴플라이언스):
    - `DELETE /api/user/me`에 `source_notification_settings`, `source_notification_logs` 삭제 추가
  - **cleanup job**: `cleanup_old_source_rates` 매일 03:31 (기존 bank cleanup 패턴 재사용)
  - **설계 문서**: `USDT_TAB_PROPOSAL.md`, `USDT_PHASE1_DESIGN.md`
  - **하위 호환성**: 기존 iOS/Android 앱은 usdt-krw 엔트리를 currency 필터링으로 자동 제외, Codable non-optional 필드(banks/currencies)는 유지

- **WebSocket DXY live tick** (`data.indices.dxy`):
  - `crud.get_latest_dxy_rate()` 기반 (realtime granularity, investing > yahoo 우선순위)
  - 초기 연결 메시지 + 후속 broadcast 모두 포함 (`build_rates_payload()` 경유 Redis 캐시 기록)
  - 10초 해상도 live — 기존 `graph_buckets.dxy`(1분 bucket 집계)와 경로 분리
  - broadcast 트리거 조건 확장: rates 또는 DXY 변화 어느 쪽이든 발화 (`build_rates_payload()` 전체 JSON 비교)
  - `insert_dxy_rate_into_db()`의 rate/source dedup 덕에 timestamp-only broadcast 폭증 없음
  - REST `/api/rates`는 별도 응답 로직이라 스키마 무변
  - 하위 호환: 구 iOS 앱은 Codable이 unknown `indices` 필드를 자동 무시

- **환율 뉴스 피드 API** (Phase 1B v2, `GET /api/news`):
  - 연합인포맥스 RSS 4개 소스 + KB API (외환+경제탭) 병행 수집
  - KB API는 RSS 대비 ~2시간 빠른 속보 소스, nsid 기반 자동 병합
  - noise_only 일원화 (잡음 제외만: 인사/부고/정치)
  - content_type: `external_link`(일반), `report_pdf`(은행 보고서 PDF 직링크)
  - `*` 기사: 제목에 "(본문없음)" 추가 + link 유지
  - `[전문]` 기사: KB 상세에서 PDF URL 추출, report_pdf로 분류
  - 초보/상보 near-duplicate collapse (시간 클러스터 방식)
  - Redis-only 저장 (24시간 윈도우, ZSET+HASH)
  - 순수 시간순 정렬, 기본 limit=100, hours=24
  - 스케줄: KB 5분마다 :15초, RSS 5분마다 :45초 (모드 무관)
  - 새 모듈: `app/news/` (sources, filters, fetcher, kb_fetcher, upsert)
  - `app/cache.py` ZSET/HASH 메서드 확장 (bytes→str 디코딩 포함)
  - `app/schemas.py` NewsItem, NewsMetadata, NewsResponse 추가

- **DXY rollup 스케줄** (`app/admin/dxy_rollup.py`): realtime → hourly/daily 자동 집계
  - hourly: 매시 :05분, 직전 완료 시간의 realtime close 집계
  - daily: 매일 00:05 KST, 직전 완료일의 hourly(또는 realtime) close 집계
  - source 보존: 원본 realtime의 실제 source를 그대로 사용
  - idempotent: INSERT ON CONFLICT UPDATE
  - 백필(1회성) 종료 이후 구간을 연속 커버
- **수동 gap 복구 함수** (`app/admin/dxy_rollup.py`):
  - `backfill_hourly_range()`: UTC 구간 realtime → hourly 일괄 생성
  - `backfill_daily_range()`: KST 날짜 구간 hourly/realtime → daily 일괄 생성

### Removed

- **`KRX_BROADCAST_INCLUDE` env/config 변수** (2026-05-12, Z-2d cleanup):
  - Z-2d (legacy exposure policy 통일)에서 `legacy_policy.should_include_source_in_legacy_rates` allowlist가 단일 진실 소스가 되며 KRX는 allowlist 미포함이라 토글 자체가 무의미해짐
  - `app/config.py` 변수 삭제 + `.env.example` 라인 제거 + 테스트 patch 제거
  - 운영 영향 0 — Z-2d Step 1-5(`fa978b0`~`b35da43`) 적용 이후 코드 미참조
  - ADR-027 / KRX_CANARY.md / USDT_TOPIC_MIGRATION_PLAN.md 등 historical 문서는 의사결정 기록 보존 + "removed in Z-2d cleanup" 표시

### Performance

- **broadcast `payload_build_ms` p99 195ms → 30.25ms** (PR5 24h 측정, n=31495, 6.4× 가속)
  - PR3.5 baseline 30분: p99 195ms / max 611ms
  - PR4 30분: p99 86.39ms / max 229.47ms
  - PR5 30분: p99 35.77ms / max 109.61ms
  - **PR5 24h 누적**: p99 30.25ms / max 415.14ms / ≥300ms outlier 0.029%
  - PR5 IN mode 30분 (영업시간 09:00-09:30): p99 75.97ms / max 835.45ms / ≥300ms 0.111% (영업시간 Redis read jitter ~2.5×)
- **DB hot path 100% → 0%** (rates + DXY 모두 Redis-first hit 100%, n=31495+1801)
- **`dxy_query_ms` count: 1827 → 0** (broadcast hot path DB 조회 제거)
- 운영 한계: 영업시간 Redis read wall-clock jitter (DB 경합/DXY fallback은 주요 원인으로 보기 어려움, 후속 진단 영역)

### Fixed

- **KRX Stage 1 운영 보강 (PR6e)**:
  - 새 WebSocket subscribe 직후 `_last_tick_at`을 reset해 세션 경계 통과 후 이전 세션 tick timestamp가 즉시 stale 전이를 유발하는 문제 차단
  - KIS WebSocket raw price의 미세 십진 잔차를 저장 전 0.1 KRW tick으로 정규화 (`Decimal(...).quantize(Decimal("0.1"))`)
  - 운영 배포 후 신규 KRX row는 0.1 단위로 저장되고, `latest:index` KRX 미포함 Stage 1 invariant 유지

- **Yahoo DXY fallback yfinance fast_info NaN 회귀 대응** (2026-04-28):
  - yfinance 0.2.66의 `fast_info.regularMarketPreviousClose`가 NaN 반환하여 fallback이 항상 실패하던 문제 수정
  - `or` 연산자가 NaN을 truthy로 취급하던 latent bug 노출 (1ec2c8d 이후 잠재)
  - 다단계 fallback 도입: fast_info 4개 필드 → `ticker.info` → `history(1d, 1m)` 최후 보루
  - `_coerce_valid_dxy_price()` 헬퍼로 None/NaN/숫자변환실패/범위초과 모두 단일 검증
  - ADR-025로 Yahoo는 이미 최후 보루(2순위)로 격하되어 운영 영향은 격리된 상태였음

- **DXY Yahoo fallback 가드 강화** (2026-04-27, ADR-022):
  - ICE DX 주간 세션 OFF (토 06:00~월 07:00 KST DST) Yahoo 저장 차단 (`_is_dxy_weekly_session_open`)
  - Yahoo 값과 마지막 Investing 값 차이 `0.07` 초과 시 저장 보류
  - 단, latest_investing이 fresh일 때만 가격 가드 적용 (mode별 grace: IN 15분 / BREAK 30분)
  - 4/27 06:00 KST 케이스(시장 개장 전 Yahoo 끼어듦) 및 weekend Friday close 점프 모두 차단됨
  - DST/표준시는 `ZoneInfo("America/New_York")`이 자동 처리

- **MIBANK URL/DOM 변경 대응** (2026-04-27):
  - 구 URL `https://www.mibank.me/exchange/bank/index.php?search_code=...`가 `https://exchange.mibank.me/bank`로 리다이렉트되며 은행 코드가 유실되는 문제 수정
  - 9개 MIBANK 사용 크롤러 URL을 `https://exchange.mibank.me/bank?bank_cd=...` 형식으로 변경
  - 새 DOM(`table.main_table.content`) 지원: `flag_<code>_*.png`에서 통화 코드 추출, `기준환율(원)` 헤더 컬럼 기반 환율 추출
  - 신한/NH/SC의 MIBANK 1차 실패 → Selenium fallback 반복 부하 완화
  - Created [MAINTENANCE_2026-04-27.md](MAINTENANCE_2026-04-27.md): 장애 원인, 검증 절차, 복구 플레이북
- **USDT scheduler job 제거 버그** (2026-04-23, `6a86c01`):
  - 앱 시작 시 `start_scheduler`에서 `task_usdt_sources` job 등록 → 직후 `switch_jobs()`가 `task_` prefix 전체 제거
  - mode-agnostic 상시 실행 의도였으나 첫 모드 전환 시점에 즉시 삭제됨
  - 수정: job id `task_usdt_sources` → `usdt_sources` (prefix 분리)
  - Docker/EC2 런타임 검증에서 발견 (로컬 유닛 테스트로는 잡히지 않음)
- **USDT source 알림 dead alert 버그** (2026-04-23, `bc42bb4`):
  - `_validate_phase1_source_asset()`이 `is_phase1_source()`만 검사하여 reference 소스(investing/kb/hana)도 허용
  - 하지만 `process_source_rate_alerts`는 `usdt_sources` 크롤러에서만 호출되므로 reference 알림은 영원히 발동 안 됨 (dead alert)
  - 수정: `category == "exchange"` 검증 추가, reference는 400 + 기존 API 안내
- **1w DXY carry-forward 버그**: 2-part merge 전략에서 hourly gap 구간의 realtime이 누락되는 문제
  - 원인: hourly 마지막 timestamp 이후부터만 realtime을 조회하여, gap 구간의 realtime이 스킵됨
  - 수정: 1w를 **full-window 전략**으로 전환 (hourly + realtime 전체 7일 단일 쿼리, hourly > realtime dedup)
  - hourly gap backfill 58건 실행 (2026-03-10 16:00 ~ 2026-03-13 04:00 UTC)
- **3m/1y DXY carry-forward 버그**: 동일 취약점이 daily gap에서도 발생
  - 수정: **daily + recent realtime tail 7일 overlap** 전략으로 전환 (`_DXY_DAILY_REALTIME_TAIL_DAYS = 7`)
  - daily gap backfill 실행 (2026-03-11 ~ 2026-03-13 KST)

### Changed

- **USDT/KRX 서비스 출시 계약 재정의** (2026-05-06, [ADR-028](DECISIONS.md#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit)):
  - **이전 가정 (USDT Phase 1, 2026-04-23)**: USDT 5거래소 + (Stage 2 시) KRX 미국달러선물을 legacy `/api/rates` + WebSocket `rates` 배열에 어댑터 변환 통합 (Decision E)
  - **재정의 (2026-05-06)**: USDT 거래소 + KRX = **topic-only** (legacy `rates` 미포함). legacy `rates` 채널 = USD/JPY/EUR + Investing/은행 9개 한정. dual-emit은 환율 탭 데이터에만 적용
  - **재정의 사유**: 운영 앱(iOS 2026-01-21 / Android 2026-03-13)에 테더 탭 없음 → 현재 USDT의 legacy `rates` 포함은 iOS dev/test 단계의 임시 모델. [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) v0.8 합의에 정합
  - **현재 백엔드 구현은 보존**: USDT legacy 통합 경로(`/api/rates/usdt-krw`, WebSocket `rates`에 USDT 포함)는 그대로 운영 중. 코드 분리는 Phase Z-2 (별도 PR, 5/8 baseline + 5/18 만기 관찰 후 시작) 영역
  - **PR6 KRX `KRX_BROADCAST_INCLUDE` 토글 의미 재해석**: 기존 "legacy `rates` 통합 트리거" → "topic 발사 트리거". Stage 2 진입은 topic protocol v1 도입 후
  - 영향 받은 문서: [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md) (superseded), [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md) (부분 superseded), [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md) (데이터 수신 방식 superseded), [CLAUDE.md](CLAUDE.md) USDT/KRX 섹션, [KRX_CANARY.md](KRX_CANARY.md) Stage 2 조건, [DECISIONS.md ADR-027](DECISIONS.md#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안) Stage 2 채널

- **TradingView TVC:DXY 운영 통합 보류** (2026-04-29):
  - 4-28~29 약 24h 측정 (1,236 unique samples) 결과 production fallback 부적합 결론
  - TV `delay_sec`: p50=338s, p95=2009s, max 약 1시간 43분 (시간대별 변동 극심)
  - 시간대 패턴: KST 14~18시 신선(8s), 07~08시 stale(1500~4300s) — 운영에서 예측 불가
  - TV ≈ CNBC (median 0.00 차이, max 0.05) — 같은 origin 의심, sanity check 가치도 제한적
  - 결정: Investing → CNBC → Yahoo chain 그대로 유지 (ADR-025 그대로)
  - 원본 측정 데이터는 운영 서버 정리 완료 — 핵심 통계와 결론만 CHANGELOG에 보존
  - 별도 ADR 작성 안 함 — ADR-025의 부산물 결정으로 CHANGELOG에 기록

- **DXY fallback 관측성 개선** (2026-04-29):
  - `_try_external_fallback()`의 6개 logger 메시지에 핵심 메타 인라인 (reason / mode / fresh_age / failures / source / diff / threshold)
  - 운영 grep 한 줄로 chain 분기/카운터/age 즉시 분석 가능
  - extra dict는 그대로 유지 (JSON 분석 도구 호환)
  - fallback 정책/threshold는 변경 없음 — 관측성만 강화
  - 4-28 운영 분석에서 39회 transient failure의 정확한 분기 추적이 어려웠던 문제 해소

- **데이터 보관 정책 30일 통일** (2026-04-27, ADR-023):
  - 은행 환율(`bank_exchange_rates`): 10일 → 30일
  - USDT 거래소 가격(`source_rates`): 10일 → 30일
  - DXY 현물/선물 realtime(`market_index_rates`): 30일 (신규 cleanup)
  - DXY hourly/daily rollup은 삭제 안 함 — 3m/1y 그래프 보존
  - `crud.delete_old_market_index_rates(days=30, granularities=None)` 신규 (default `["realtime"]`)
  - 매일 03:30~03:32 KST 순차 cleanup

- **DXY_MODE 환경변수 제거** (2026-04-27, ADR-024):
  - 현물(`dxy`)과 선물(`dxy_futures`) 둘 다 별도 저장하는 구조로 toggle 의미 상실
  - `app/config.py`에서 상수 제거, `app/scheduler.py`에서 import + 4곳 분기 제거
  - `task_dxy`는 항상 등록 — `crawler_config.dxy` 토글만 적용
  - 운영 EC2 `.env`의 `DXY_MODE` 라인은 무해하지만 정리 권장

- **DXY 스케줄 조정**: 실시간 비교 품질 개선
  - IN/BREAK1/BREAK2: 10초마다 (`04, 14, 24, 34, 44, 54초`)
  - OUT: 10분마다 (`4, 14, 24, 34, 44, 54분 44초`)
  - 브로드캐스트 정각(`00, 10, 20, 30, 40, 50초`) 유지 기준으로 재배치
  - 주말 OUT 모드에서는 다른 크롤러 분/초 슬롯과 겹치지 않도록 분산

## [1.12.0] - 2026-03-10

### Added - DXY (Dollar Index) Graph Indicator

- **DXY 크롤러** (`app/crawlers/dxy.py`): 달러지수 실시간 수집
  - Primary: Investing.com (`kr.investing.com/indices/usdollar`, curl_cffi TLS 지문 위장)
  - Fallback: Yahoo Finance (`yfinance`, ticker `DX-Y.NYB`)
  - 전환 조건: 연속 5회 실패 OR 5분 stale → Yahoo 자동 전환
  - Circuit Breaker: DXY 전용 (investing.py와 독립, 403 쿨다운 동일 정책)
  - 복구: 쿨다운 해제 후 Investing 재시도, 성공 시 즉시 원소스 복귀
- **market_index_rates 테이블**: 범용 시장 지수 데이터 모델
  - `granularity` 컬럼: `realtime` | `hourly` | `daily` (백필과 실시간 구분)
  - UNIQUE 제약: `(instrument, source, timestamp, granularity)`
  - `source` 우선순위: `investing > yahoo` (ROW_NUMBER CASE WHEN)
- **그래프 API 확장**: `/api/graph/{currency}?range=1d|1w|3m|1y`
  - USD/KRW에만 DXY 보조지표 포함 (API key=`dxy`, UI 표시명=달러지수)
  - 1d: 10분 버킷 (realtime only)
  - 1w: 1시간 버킷 (realtime + hourly, lazy cache 10분 TTL)
  - 3m/1y: 1일 버킷 (realtime + daily, lazy cache 1시간 TTL)
- **2-part merge 전략**: 과거(daily/hourly) + 오늘(realtime) 분리 쿼리
  - 1w 오늘: timestamp 단위 `realtime > hourly` 선택 (공존 허용)
  - 3m/1y 오늘: realtime 있으면 daily 전체 제외 (날짜 단위 배타적)
- **히스토리 백필 스크립트** (`scripts/backfill_history.py`)
  - yfinance로 daily(최대 1년) + hourly(최근 7일) 사전 적재
  - `granularity='daily'|'hourly'`로 구분 저장
- **DB 마이그레이션 스크립트** (`scripts/migrate_market_index_granularity.py`)
  - `granularity` 컬럼 추가, UNIQUE 인덱스 재생성
  - PostgreSQL/SQLite 양쪽 지원, `--dry-run` 옵션
- **스케줄러 통합**: 4단계 모드 모두 DXY 등록
  - IN/BREAK1/BREAK2: 매분 42초
  - OUT: 10분마다 (2분 42초)

### Dependencies

- Added: `yfinance` (Yahoo Finance DXY 폴백)

### Migration

**배포 순서** (운영 환경, 상세: [DEPLOYMENT.md](DEPLOYMENT.md#db-마이그레이션-v1120-dxy-granularity)):
1. `git pull` → `docker compose build` (새 이미지 빌드)
2. `docker compose run --rm fastapi python scripts/migrate_market_index_granularity.py`
3. `docker compose up -d` (서비스 시작)
4. `docker compose exec fastapi python scripts/backfill_history.py`

### Documentation

- Added [ADR-019](DECISIONS.md#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략): DXY granularity 기반 2-part merge 전략
- Updated [CLAUDE.md](CLAUDE.md): DB 스키마, API, 기술 스택, 파일 구조, Phase 1A
- Updated [CRAWLERS.md](CRAWLERS.md): Group D (시장 지수) DXY 크롤러 섹션 추가
- Updated [DEPLOYMENT.md](DEPLOYMENT.md): granularity 마이그레이션 선행 절차

---

## [1.11.1] - 2026-01-29

### Fixed - Investing Cloudflare 403 Block

- **curl_cffi TLS 지문 위장**: `requests` → `curl_cffi` (Investing 크롤러 전용)
  - Cloudflare JA3/JA4 TLS fingerprint 탐지 우회
  - `impersonate="safari17_0"` (Safari 17.0 TLS 핸드셰이크 모방)
  - `chrome131` 시도 → 403 차단, `safari17_0` → 200 OK 성공
  - Graceful 폴백: `_USE_CFFI` 플래그로 curl_cffi 미설치 시 requests 자동 사용
- **Circuit Breaker**: 연속 403 시 점진적 쿨다운 (5회→1분, 10회→5분, 20회→15분)
- **UA 로테이션**: Safari/Chrome UA 풀에서 impersonate에 맞는 UA 선택
- **Jitter**: 0~2초 랜덤 딜레이 (요청 패턴 분산, Broadcasting 타이밍 고려)
- **로그 억제**: 상태 전이 기반 로깅 (차단시작 ERROR 1회, 지속 WARNING 5분마다, 해제 WARNING 1회)

### Dependencies

- Added: `curl_cffi>=0.7.4,<1.0.0` (Investing 크롤러 전용)

### Documentation

- Added [ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장): curl_cffi TLS 지문 위장 선택 근거
- Created [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md): 장애 타임라인 및 복구 플레이북
- Updated [CRAWLERS.md](CRAWLERS.md): Investing 크롤러 Cloudflare 대응 상세
- Updated [CLAUDE.md](CLAUDE.md): 기술 스택 및 리스크 대응 현행화

---

## [1.11.0] - 2026-01-21

### Released - iOS App Store

- **iOS 앱 출시**: App Store에 "환율알림 - 대한민국 주요 은행" 앱 게시
- **심사 전략**: "환율 알림" 중심 포지셔닝으로 Guideline 5.1.1 통과
- **거절→통과**: 초기 거절 사유(로그인 강제) → 알림 기능 중심 설명으로 해결

### Infrastructure - Production Ready

- **RDS PostgreSQL 전환**: SQLite → AWS RDS PostgreSQL (db.t4g.micro)
  - SQLAlchemy dialect 자동 감지 (`psycopg2` 의존성 추가)
  - 환경변수 `DATABASE_URL`로 DB 전환
- **도메인 fxi.kr 전환**: Let's Encrypt SSL 인증서 적용 (HTTPS/WSS)
  - TLS 1.2 & 1.3, HSTS 헤더 적용
  - Certbot 자동 갱신 (Systemd Timer)
- **DB 타임스탬프 UTC 통일**: 저장은 UTC, API 응답은 KST(+09:00) 변환
- **Admin 페이지 보안 강화**: 모니터링 카드 확장 (CPU, 메모리 여유도, 브로드캐스트 건강 상태)
- **Shinhan 주말 크롤링**: OUT 모드에서 shinhan 크롤러 유지 (주말 중 가끔 변동)

---

## [1.10.0] - 2026-01-10

### Changed - MIBANK Parsing Logic Overhaul (Currency-Code Based)

- **Currency-code based parsing**: Extract currency from `href="...?currency=USD"` instead of position-based `tr:nth-child(N)`
  - Immune to table row order changes
  - Uses last cell (매매기준율) for rate extraction
  - See [ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반)

- **3-layer validation system** for data integrity:
  1. **Completeness check**: USD/JPY/EUR all required (`require_all=True`)
  2. **Absolute range check**: USD 1,000~2,000, JPY 600~1,400, EUR 1,100~2,200
  3. **Deviation check**: Dynamic thresholds based on time gap (soft_fail/hard_fail)

- **New utility functions** (`app/crawlers/utils.py`):
  - `crawl_mibank_rates()`: Currency-code based MIBANK crawling
  - `validate_rate_ranges()`: Absolute range validation
  - `evaluate_rate_deviation()`: Dynamic deviation check with soft/hard fail
  - `get_dynamic_thresholds()`: Time-gap based threshold calculator

- **Bank-specific wrapper pattern** (`_crawl_mibank_{bank}()`):
  - All 9 crawlers now use standardized MIBANK wrapper functions
  - Encapsulates: crawling → range validation → deviation check

- **New DB utility** (`app/crud.py`):
  - `get_last_bank_rates_with_ts()`: Fetch last rate + timestamp for deviation check

- **Common constants** (`app/crawlers/constants.py`):
  - `MIBANK_REQUIRED_CODES`: ("USD", "JPY", "EUR")
  - `MIBANK_REQUIRED_PAIRS`: ("usd-krw", "jpy-krw", "eur-krw")
  - `MIBANK_RATE_RANGES`: Absolute range limits per currency

### Fixed

- **Timezone-naive timestamp bug**: DB timestamps without timezone info caused incorrect deviation calculations
  - Fix: KST localize before comparison (`kst.localize(prev_ts)`)

### Removed

- **MIBANK_SELECTORS**: Deleted from all 9 crawlers (dead code after currency-code parsing)

### Deprecated

- **`crawl_and_save_routine()`** in 4 crawlers marked with `[DEPRECATED]` comment:
  - `sc.py`: Selenium-only crawler, requests not applicable
  - `nh.py`: Selenium-only crawler, requires page click navigation
  - `shinhan.py`: SPA page, requires JavaScript rendering
  - `ibk.py`: Replaced by `try_crawl_with_requests()` with enhanced validation

### Documentation

- Added [ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반): MIBANK parsing architecture decision
- Updated [CRAWLERS.md](CRAWLERS.md): New utility functions and wrapper pattern documentation

---

## [1.9.0] - 2026-01-08

### Added - Account Deletion API (Apple App Store 5.1.1(v) Compliance)

- **Account deletion endpoint**: `DELETE /api/user/me`
  - Deletes all user data: `notification_logs`, `notification_settings`, `user_devices`
  - Response: `204 No Content` on success
  - Security: `check_revoked=True` for destructive operations
- **Enhanced Firebase token verification**
  - New parameter: `check_revoked` for revoked token detection
  - New error handling: `RevokedIdTokenError` → 401, `CertificateFetchError` → 503
  - Network error handling: `TransportError`, `RequestException` → 503

### Planned
- Dynamic priority adjustment based on crawler success rate (Phase 2)
- Multiple Queue system for crawler groups (Phase 3)

---

## [1.8.0] - 2025-12-02

### Added - 24-Hour Graph Feature with WebSocket Integration

- **24-hour graph API** with 10-minute bucket aggregation
  - Endpoint: `GET /api/graph/{currency}` (usd-krw, jpy-krw, eur-krw)
  - Data format: `[timestamp, max, min, close]` (candlestick structure)
  - Carry-forward mechanism: Empty buckets filled with previous close value for data continuity
  - Redis cache: `graph:{currency}` keys (~3.6KB per currency, 120s TTL)
  - Backend: `app/admin/graph_cache.py` - Scheduler runs every minute at :03 seconds
- **WebSocket graph integration** for real-time updates
  - `graph_buckets` field: Last bucket for all 3 currencies (~600 bytes)
  - Broadcast trigger: Only when rates change (change detection)
  - Mobile optimization: Reuse existing WebSocket connection (no additional radio activations)
  - Server efficiency: 1 broadcast/10s vs 100+ HTTP req/min (99% CPU reduction)
- **Band Chart implementation** (frontend)
  - Single source: Close line (main) + High/Low translucent bands
  - Multi-source: Close lines only for comparison
  - Tooltip filtering: Range display for single-source mode
  - Y-axis padding: 5% margin for better visualization
- **Lazy loading with cache strategy**
  - Initial load: Selected currency only (3.6KB)
  - Currency switch: Cache reuse (0 bytes) or on-demand load
  - Gap detection: 15-minute threshold → full refresh
  - Frontend cache: In-memory storage for all 3 currencies
- **Toggle controls** for graph customization
  - Show/Hide individual sources (INVESTING, KB, HANA)
  - Prevent empty graphs: At least 1 source required
  - Persistent selection across currency switches

### Performance
- **Network efficiency**: WebSocket integration reduces mobile data usage by 95%+
- **Initial load**: 3.6KB per currency (145 buckets × 3 sources × 4 values)
- **WebSocket overhead**: +600 bytes per broadcast (0.6KB / 10s)
- **Cache hit rate**: ~100% for currency switches (no repeated API calls)
- **Mobile battery**: Zero additional impact (reuses existing WebSocket)

### Documentation
- Created [GRAPH_FEATURE.md](GRAPH_FEATURE.md) v3.0 - Complete implementation guide
- Added [ADR-015](DECISIONS.md#adr-015-websocket-graph-integration-vs-incremental-api) - Graph update strategy decision

---

## [1.7.0] - 2025-11-27

### Added - Redis Broadcast Cache & Change Detection

- **Redis broadcast cache** for WebSocket initial connection optimization
  - Cache key: `broadcast:latest` (~3.6KB JSON payload)
  - Initial connection: Redis cache → instant data delivery
  - Memory usage: ~1MB stable (~1% of 100MB maxmemory)
  - Circuit Breaker: 5 failures → 30s timeout → auto-recovery
  - Admin API endpoint: `GET /admin/api/redis-status`
- **Change detection system** for broadcast efficiency
  - JSON comparison: `new_json != cached_json`
  - Smart logging: "⏸️ 변경사항 없음" when no change, "📡 브로드캐스트 완료" when changed
  - Bandwidth optimization: Only broadcast when data actually changes
- **Redis monitoring card** in admin dashboard
  - Real-time memory usage and key count display
  - Circuit breaker status visualization
  - Color-coded health indicators (connected/circuit-open/disconnected)

### Fixed
- **Broadcasting bug**: `updated_at` timestamp issue causing "always different" comparisons
  - Root cause: Used `datetime.now()` instead of DB's actual latest timestamp
  - Impact: Change detection never triggered, wasted bandwidth
  - Fix: Use `max(rate["timestamp"])` from DB data for accurate comparison
  - Result: Change detection accuracy improved to 100%

### Performance
- **WebSocket initial connection**: Instant data delivery via Redis cache
- **Bandwidth optimization**: Significant reduction in unnecessary broadcasts during low-volatility periods

### Documentation
- Updated [REDIS_IMPACT_ANALYSIS.md](REDIS_IMPACT_ANALYSIS.md) with actual measurements
- Updated [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md): Corrected ElastiCache free tier info

---

## [1.6.0] - 2025-11-10

### Added - 3-Tier Scheduling Architecture
- **Crawler statistics system** for monitoring success rate and performance
  - Real-time tracking: success/fail counts, avg duration, last execution time
  - Thread-safe collector with singleton pattern
  - Admin API endpoint: `GET /admin/api/crawler/stats`
  - Integrated with both Request and Selenium crawlers
- **3-Tier scheduling architecture** optimized for t3.small/medium:
  - **Tier A (investing)**: Most critical, highest frequency
  - **Tier B (kb, hana, woori, bs, citi)**: Important, moderate frequency
  - **Tier C (shinhan, ibk, nh, sc)**: Selenium-based, lowest frequency
- **IN mode: cron-based absolute timing** (Broadcasting synchronization)
  - A Group: 10s interval (5s before Broadcasting @ 00, 10, 20s)
  - B Group: 20-60s interval (3-7s before Broadcasting, staggered)
  - C Group: 33.3-150s interval (Broadcasting independent)
- **OUT mode: cron-based hourly distribution** (Zero concurrent execution)
  - A Group: Every 10 minutes (05, 15, 25, 35, 45, 55 min)
  - B Group: Every 10-60 minutes (fully distributed across the hour)
  - C Group: Once per hour (fully distributed: 07, 17, 27, 36, 47, 57 min)

### Changed
- **Worker health check optimization**: 180s → 90s stuck detection
  - Rationale: Timeout 45s × 2 = 90s is sufficient (heartbeat updates at job start)
  - Faster problem detection and auto-restart
- **Selenium timeout adjustment**: shinhan 60s → 45s (consistency with other crawlers)
- **Scheduling strategy**:
  - IN mode: cron with second precision (e.g., `second='5,15,25,35,45,55'`)
  - OUT mode: cron with minute precision (e.g., `minute='5,15,25,35,45,55'`)
  - Request crawlers: Direct execution with stats wrapper
  - Selenium crawlers: Queue-based execution (unchanged)
- **Statistics wrapper**: All crawlers now tracked via `make_request_crawler_wrapper()`

### Performance
- **OUT mode resource distribution**: 10 crawlers spread across 60 minutes
  - Peak concurrent crawlers: 1 (down from potential 6-8)
  - CPU spike elimination: No simultaneous execution
- **Faster failure detection**: Worker restart in 90s vs 180s
- **Better monitoring**: Real-time success rate and duration tracking per crawler

### Documentation
- Added [ADR-009](DECISIONS.md) documenting 3-Tier scheduling architecture
- Updated scheduler.py with comprehensive inline documentation
- Added `app/admin/crawler_stats.py` with usage examples

---

## [1.5.0] - 2025-11-10

### Added
- **Worker health check system** for detecting and auto-restarting stuck workers
  - Heartbeat tracking: Updates every job completion
  - Stuck detection: >180 seconds on same job → automatic restart
  - Health check interval: Every 60 seconds
- **Queue pressure relief policy** (80% threshold)
  - Rejects new jobs when queue >80% full (20/25)
  - Prevents queue overflow and APScheduler blocking
  - Logs rejected jobs for monitoring
- Worker status tracking: `selenium_worker_last_heartbeat`, `selenium_worker_current_job`

### Changed
- **Timeout reduction (50% cut)** for real-time performance:
  - hana: 60s → 30s
  - ibk: 90s → 45s
  - nh: 90s → 45s
  - sc: 90s → 45s
  - shinhan: 120s → 60s
- **Queue size optimization**: 50 → 25
  - Rationale: 80% pressure relief (20/25) + memory savings
  - Prevents excessive job accumulation
- **Crawler schedule adjustment** (IN mode):
  - hana: 20s (unchanged, fastest crawler)
  - shinhan: 60s (adjusted for queue balance)
  - ibk: 60s (reduced frequency for slow crawler)
  - nh: 90s (further reduced for slowest crawler)
  - sc: 120s (minimal frequency for reliability)

### Fixed
- **Queue saturation (100%)** → Reduced to ~80% with pressure relief
- **Worker stuck issue** → Auto-restart when heartbeat >180s
- **Real-time performance degradation** → NH crawler timeout enforced at 45s
- **APScheduler blocking** → Non-blocking queue operations prevent scheduler freeze

### Performance
- Queue utilization: 100% (50/50) → 80% (20/25) stable
- Timeout enforcement: Slow crawlers (66s) now properly timeout at 45s
- Memory savings: Queue size reduction contributes to overall stability
- Worker reliability: Auto-restart ensures continuous operation

### Tradeoffs
- ⚠️ **Real-time vs Completeness**: Timeouts may reject slow crawlers during high Swap usage
- ⚠️ **Queue pressure**: 80% rejection may skip some scheduled jobs (retry on next cycle)
- ✅ **System stability**: Prioritized over 100% data collection rate

### Documentation
- Added [ADR-008](DECISIONS.md) documenting Queue pressure relief strategy
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with new queue size and policies

---

## [1.4.0] - 2025-11-08

### Added
- **Priority Queue system** for Selenium crawlers (AsyncIO PriorityQueue)
- Crawler priority mapping based on execution speed:
  - hana (0): Fastest → 1st priority
  - ibk (1): Medium → 2nd priority
  - nh, sc (2-3): Slow → 3rd-4th priority
  - shinhan (4): Slowest → 5th priority
- **Individual timeout settings** per crawler (60-120 seconds)
- **Automatic retry mechanism** with lower priority (+1000) on failure
- `execute_with_timeout()` wrapper function for timeout enforcement
- Chrome memory optimization:
  - `--window-size=400,300` (reduced from 800x600)
  - `--disable-javascript` for JS engine memory saving
  - `--disable-webgl` for WebGL memory release
- Monitoring logging enabled (DEBUG → INFO level)

### Changed
- `asyncio.Queue` → `asyncio.PriorityQueue` in `app/scheduler.py`
- Selenium timeout values increased to handle Swap I/O delays:
  - hana: 30 → 60 seconds
  - ibk/nh/sc: 60 → 90 seconds
  - shinhan: 90 → 120 seconds
- Queue enqueue format: simple data → priority tuple `(priority, timestamp, job_func, bank_name, is_retry)`
- Worker execution: added timeout wrapper and retry logic
- PriorityQueue maxsize: 10 → 20 (increased capacity)

### Fixed
- **Selenium crawler timeout issues** caused by memory pressure (100% resolution)
- Swap memory usage causing 2-3x slower Chrome process creation
- False timeout failures when Swap I/O delays exceeded fixed timeout limits
- Slow crawlers blocking fast crawlers in simple FIFO Queue

### Performance
- **Timeout occurrence**: 80% failure rate → 0% (60-second monitoring)
- Crawler execution times: All within normal range (0.36-17.50 seconds)
- Priority Queue ordering: Guaranteed execution order (hana → ibk → nh → sc → shinhan)
- Chrome memory per instance: Estimated 50-80MB additional savings
- System stability: RAM 71.9%, Swap 28.2% with no timeouts

### Documentation
- Added [ADR-007](DECISIONS.md) documenting Priority Queue + Timeout strategy
- Created [MAINTENANCE_2025-11-08.md](MAINTENANCE_2025-11-08.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with Priority Queue info
- Updated Docker deployment section with 3-tier approach (daily/cache/reset)

---

## [1.3.0] - 2025-11-06

### Added
- AsyncIO Queue system for Selenium crawler sequential execution
- Queue Worker for processing Selenium tasks one at a time
- `init_selenium_queue()` and `shutdown_selenium_queue()` lifecycle management
- Crawler grouping: `REQUEST_BASED_TASKS` and `SELENIUM_BASED_TASKS`

### Changed
- **Breaking:** Replaced `BackgroundScheduler` with `AsyncIOScheduler`
- Split crawler tasks into Request-based (concurrent) and Selenium-based (sequential)
- Modified `switch_jobs()` to handle two different execution patterns
- Updated FastAPI `lifespan()` to initialize/shutdown Queue system

### Removed
- Threading Semaphore from `app/crawlers/utils.py`
- Semaphore acquire/release logic from all Selenium crawlers

### Fixed
- Selenium crawler Semaphore contention causing false MIBANK fallbacks (30-40% reduction)
- Memory instability from multiple Chrome instances (50% reduction: 650MB → 320MB)
- Unnecessary fallback to MIBANK when normal collection was possible

### Performance
- Memory usage: 650MB → 320MB (50% improvement)
- Fallback occurrence: 30-40% → 0%
- Sequential execution guarantee for Selenium crawlers

### Documentation
- Added [ADR-006](DECISIONS.md) documenting AsyncIO Queue decision
- Created [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section

---

## [1.2.0] - 2025-11-05

### Added
- System monitoring module (`app/admin/monitor.py`)
- Chrome process monitoring with count and memory tracking
- Zombie Chrome process cleanup scheduler (runs every 3 minutes)
- Force kill logic for Chrome processes older than 5 minutes
- Monitoring statistics collection (every 5 minutes)
- Admin dashboard API endpoints:
  - `GET /admin/api/monitor/current` - Current system status
  - `GET /admin/api/monitor/history?hours=N` - Historical data
- Chrome process card in admin dashboard UI

### Changed
- Chrome max lifetime: 10 minutes → 5 minutes (`CHROME_MAX_LIFETIME_SECONDS`)
- Chrome cleanup interval: 5 minutes → 3 minutes (`CHROME_CLEANUP_INTERVAL_MINUTES`)
- Log backup count: 5 → 3 (50MB → 30MB total)
- Docker memory limit: 700M → 800M
- Disabled unnecessary system services (snapd, multipathd)

### Fixed
- Selenium zombie process accumulation causing memory leaks
- WebSocket ConnectionManager ValueError on disconnect
- Unsafe list iteration in WebSocket broadcast causing potential crashes
- Chrome processes not properly terminated after `driver.quit()` failure

### Performance
- Memory usage: 647MB → 581MB (66MB improvement)
- Free memory: 309MB → 375MB
- Disk usage: 15GB → 6.3GB (8.7GB reclaimed)
- Chrome zombie process automatic cleanup every 3 minutes

### Documentation
- Created [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md)
- Added [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md)

---

## [1.1.0] - 2025-10-26

### Changed
- SC bank crawler: Switched from Selenium to Request-based (cost reduction)
- WOORI bank crawler: Applied SC approach (simplified logic)
- IBK bank crawler: Added Selenium retry logic with MIBANK fallback

### Fixed
- SC bank Alert handling consolidated to single pattern
- Selector fallback improvements for multiple banks

---

## [1.0.1] - 2025-10-25

### Added
- Domain-based project structure:
  - `app/crawlers/` - Crawler domain
  - `app/admin/` - Admin domain
  - `app/notifications/` - Notification domain
- Centralized constants: `app/crawlers/constants.py`
- Common crawler utilities: `app/crawlers/utils.py`
- Log cleaner module: `app/admin/log_cleaner.py`
- WebSocket broadcast statistics: `app/admin/stats.py`

### Changed
- Refactored 10 crawler files to use centralized constants and utilities
- Moved admin-related logic to dedicated domain
- Improved code organization and maintainability

### Documentation
- Added [CRAWLERS.md](CRAWLERS.md) - Detailed crawler implementation guide
- Added [DECISIONS.md](DECISIONS.md) - Architecture Decision Records (ADR)

---

## [1.0.0] - 2025-10-17

### Added
- Initial release of Exchange Rate Comparison Service
- 10 crawler sources:
  - Investing.com (reference rate)
  - 9 Korean banks: KB, Hana, Shinhan, Woori, IBK, NH, SC, Busan, Citi
- Support for 3 currency pairs: USD-KRW, JPY-KRW, EUR-KRW
- FastAPI backend with WebSocket support
- SQLite database with automatic cleanup (10-day retention)
- APScheduler with IN/OUT mode (business hours detection)
- Admin dashboard (`/admin`) with:
  - Real-time crawler status
  - Log viewer (2 tabs: all logs, errors only)
  - WebSocket connection monitoring
  - Memory usage tracking
  - Broadcast statistics
- REST API endpoints:
  - `GET /api/rates` - All exchange rates
  - `GET /api/rates/{currency}` - Specific currency
  - `GET /api/investing/{pair}` - Investing.com rates
  - `GET /api/banks/{pair}` - All bank rates
  - `GET /health` - Health check
- Docker deployment with:
  - Multi-stage build
  - Google Chrome (AMD64 optimized)
  - 700MB memory limit
- Structured logging system:
  - JSON format in production
  - Colored output in development
  - Log rotation (10MB, 5 backups)
  - Auto cleanup (10 days)

### Documentation
- [CLAUDE.md](CLAUDE.md) - Project guide
- [DOCKER.md](DOCKER.md) - Docker deployment guide

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| 1.12.0 | 2026-03-10 | DXY 달러지수 보조지표 (Investing + Yahoo 이중 소스, granularity 2-part merge) |
| 1.11.1 | 2026-01-29 | Investing Cloudflare 403 대응 (curl_cffi TLS 지문 위장) |
| 1.11.0 | 2026-01-21 | iOS 출시 + RDS PostgreSQL + 도메인 fxi.kr + Admin 보안 강화 |
| 1.10.0 | 2026-01-10 | MIBANK Currency-Code parsing + 3-layer validation |
| 1.9.0 | 2026-01-08 | Account Deletion API (Apple App Store 5.1.1(v) Compliance) |
| 1.8.0 | 2025-12-02 | 24-hour graph + WebSocket integration + Band Chart |
| 1.7.0 | 2025-11-27 | Redis broadcast cache + Change detection |
| 1.6.0 | 2025-11-10 | 3-Tier scheduling + Crawler statistics |
| 1.5.0 | 2025-11-10 | Queue pressure relief + Health check + Timeout optimization |
| 1.4.0 | 2025-11-08 | Priority Queue + Timeout strategy |
| 1.3.0 | 2025-11-06 | AsyncIO Queue for Selenium crawlers |
| 1.2.0 | 2025-11-05 | System monitoring & zombie process cleanup |
| 1.1.0 | 2025-10-26 | Crawler optimizations (SC, WOORI, IBK) |
| 1.0.1 | 2025-10-25 | Domain-based refactoring |
| 1.0.0 | 2025-10-17 | Initial release |

---

## Migration Guide

### Upgrading from 1.4.x to 1.5.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.4.x

**Steps:**
1. Pull latest code
2. Review changes in `DECISIONS.md` (ADR-008: Queue pressure relief strategy)
3. Rebuild Docker: `docker compose up -d --build`
4. Monitor queue pressure: `docker logs -f exchange-rate-app | grep "Queue 압력"`
5. Verify health check: `docker logs -f exchange-rate-app | grep "헬스체크\|멈춤 감지"`

**Expected Improvements:**
- Queue saturation reduced from 100% to ~80%
- Real-time performance improved (NH crawler now timeout at 45s)
- Automatic worker recovery on stuck (>180s)
- Memory savings from smaller queue (50 → 25)

**Monitoring:**
- Watch for "Queue 압력 초과로 skip" warnings (acceptable, system working as designed)
- Verify timeout enforcement: Slow crawlers should timeout within new limits
- Check health check logs: Workers should auto-restart if stuck >180s

**Rollback:**
- Revert `app/crawlers/constants.py` SELENIUM_TIMEOUT_MAP (×2 values)
- Revert `app/scheduler.py` queue size (25 → 50) and remove pressure relief logic
- Comment out health check function and job

---

### Upgrading from 1.3.x to 1.4.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.3.x

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-08.md` for detailed changes
3. Rebuild Docker: `docker compose up -d --build` (or `--no-cache` if issues)
4. Verify Priority Queue in logs: `docker logs -f exchange-rate-app | grep "우선순위\|Priority"`

**Expected Improvements:**
- 100% resolution of Selenium timeout issues
- Guaranteed execution order (fast crawlers first)
- Additional 50-80MB memory savings per Chrome instance
- Automatic retry for failed crawlers

**Rollback:**
- All changes preserved as comments in code
- Simply uncomment old code and recomment new code in:
  - `app/crawlers/constants.py`
  - `app/admin/monitor.py`

---

### Upgrading from 1.2.x to 1.3.x

**Breaking Changes:**
- `BackgroundScheduler` → `AsyncIOScheduler`
- If you have custom scheduler code, update to use async-compatible patterns

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-06.md` for detailed changes
3. Rebuild Docker: `docker compose build --no-cache`
4. Restart: `docker compose up -d`
5. Verify Queue Worker in logs: `docker logs -f exchange-rate-app | grep "Queue"`

**Expected Improvements:**
- 50% memory reduction (650MB → 320MB)
- 100% elimination of false MIBANK fallbacks
- Smoother Selenium crawler execution

---

## Contributing

Please update this CHANGELOG when making significant changes following these guidelines:

- **Added** for new features
- **Changed** for changes in existing functionality
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes
- **Performance** for performance improvements
- **Documentation** for documentation updates

---

**Last Updated**: 2026-03-10
