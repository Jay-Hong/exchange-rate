# Source-Health Plan (무료 스냅샷 소스별 건강 관측)

> **상태: Proposed / Step 0(인벤토리 + shadow 계약 초안) / 구현 없음**
> 검증: 5-agent Workflow 코드 인벤토리 + codex 다라운드 리뷰(인벤토리→계약 정정 반복) + Claude 코드 재검증 (2026-07-22).
> 범위: ADR-039 무료(비구독) 매시간 스냅샷([FREE_TIER_ACCESS_MODEL_PLAN.md](FREE_TIER_ACCESS_MODEL_PLAN.md))이 노출하는 소스의 **개별 stall** 관측.
> [free_snapshot.py:59-64](app/free_snapshot.py) 주석이 명시하듯 S6(24h whole-snapshot cutoff)는 스냅샷 전체 정지만 잡고 **개별 소스 stall은 못 잡는 층** — 이 문서가 그 후속 계약.
> ⚠️ **구현 착수 전 §6 게이트 결정 필수.** 아래 설계는 여러 열린 결정을 포함하며, 코드는 shadow 관측(§5) 결과 검토 후 별도로 결정한다.

---

## 1. 소스 유니버스 — (source, asset/instrument, consumer) 단위

"소스 N개"는 아직 계약 단위가 아니다. **서버 payload 포함 ≠ 사용자 표시**를 분리해야 한다 (codex Medium 2):
- 서버 FX rate 리더([free_snapshot.py:119](app/free_snapshot.py))는 `BankExchangeRate`를 bank로 partition해 **전 은행 행(Citi 포함)** 을 조회한다.
- iOS는 표시에서 **Citi를 제외**한다 ([Constants.swift:178](../ios/FXi/Utils/Constants.swift)).
- DXY는 **spot(`instrument='dxy'`, dxy_spot 크롤러)과 futures(`instrument='dxy_futures'`, investing 크롤러 동반추출)** 가 수집 경로가 다르다.

| source | asset/instrument | 수집 경로 | 서버 payload | iOS 표시 | collection 신호 후보 |
|---|---|---|---|---|---|
| kb·hana·shinhan·woori·ibk·nh·sc·bs | usd/jpy/eur-krw | 크롤러(Request/Selenium) | ✅ (전 은행) | ✅ | crawler_stats(불신, §2) + DB ts |
| **citi** | usd/jpy/eur-krw | 크롤러 | ✅ | ❌ (표시 제외) | 동일 — **표시 안 되나 수집됨** |
| investing | usd/jpy/eur-krw | 크롤러(curl_cffi) | ✅ | ✅ | crawler_stats(불신) + DB ts |
| dxy(spot) | instrument=dxy | dxy_spot 크롤러 | ✅ (graph, usd/tether) | ✅ | crawler_stats(불신) + market_index ts |
| dxy_futures | instrument=dxy_futures | investing 동반추출 | ✅ (graph, tether 1d) | ✅ | investing 크롤러 성공에 종속 |
| upbit·bithumb·coinone·korbit·gopax | usdt-krw | WS collector(lifespan) | ✅ (tether) | ✅ | **crawler_stats 부재** — Redis/DB ts + in-process liveness(§2) |
| ~~krx~~ | usd-krw-futures | — | **2중 제외** | ❌ | 스코프 밖([free_snapshot.py:9-10,163](app/free_snapshot.py)) |

**결론**: 인벤토리 단위 = (source, asset/instrument, consumer-visibility). source-health는 "수집되는 것"을 관측하되, 사용자 배지는 "표시되는 것"에만. Citi는 수집 관측 대상이나 표시 배지 대상 아님.

---

## 2. 신호 인벤토리 (코드 검증)

무료 스냅샷/Redis/DB에서 접근 가능한 신호 중 **per-(source,asset) 유효-수집 시각을 그대로 주는 단일 신호는 없다.**

| 신호 | kind | 커버 | 영속 | tz | 핵심 한계 |
|---|---|---|---|---|---|
| `crawler_stats.last_success_at` | collection_success (명목) | 9은행+investing+dxy_spot | in-mem 휘발 | naive **local** | **전 collector가 총실패를 삼킴 → 장애 중에도 success**(§2.1). bank단위·재시작 소실 |
| DB row `timestamp`(bank/investing/source_rates/market_index) | value_changed | **전 소스** | 영속 | naive **UTC** | 정적 시장↔collector 사망 구분 X. write-gate skip 시 미전진 |
| USDT `seen_at` | last_observed | 거래소5 | Redis 휘발 | KST | 거래소 event ts(5s floor). 정적↔사망 구분 X |
| USDT `rate_changed_at` | value_changed | 거래소5 | Redis 휘발 | KST | 값 변경 시각 |
| USDT `mirrored_at` | collection_success proxy | 거래소5 | Redis 휘발 | KST | coalesce·무-tick freeze, 미소비 |
| bank/investing `mirrored_at` | (mirror liveness) | 은행+investing | Redis | KST | **FALSE-NEG**: 크롤러 다 죽어도 mirror가 3s/60s마다 stale DB latest 재복사 → 영원히 fresh |
| in-process liveness signals — `is_stale`(연결 heartbeat 위주)·Gopax ticker-dead(데이터 진행성)·supervisor `task.done()`(task 사망) | liveness(층별 상이) | USDT only(ticker-dead는 Gopax만) | 비영속·**미노출** | — | **is_stale은 heartbeat fresh+ticker dead를 못 잡음**(연결 liveness≠데이터 진행성). 스냅샷/Redis/DB 미노출 |

**tz 3종 혼재**: crawler_stats=naive local / DB=naive UTC / Redis=KST-aware → 섞으면 컨테이너 offset(운영 KST ~9h) 오차. heartbeat 저장은 timezone-aware로.

### 2.1 crawler_stats가 신뢰 불가한 이유 (codex High 1, 코드 검증됨)

`crawler_stats.record_success`는 크롤러 wrapper가 **예외 없이 반환**하면 호출된다([scheduler.py:269](app/scheduler.py#L269) Request / [scheduler.py:319](app/scheduler.py#L319) subprocess exit 0). 그러나 **scheduled collector의 일반적인 총 source 실패 경로는 로그만 남기고 정상 반환**한다:
- Request: KB([kb.py:93-96](app/crawlers/kb.py)), Investing([investing.py:186-206](app/crawlers/investing.py)) — 전 URL 실패/403도 None 정상 반환.
- **Selenium**([runner.py:74](app/crawlers/runner.py)가 `crawler_func()` 후 `sys.exit(0)` — 함수가 raise해야만 exit 1): shinhan([shinhan.py:118-122](app/crawlers/shinhan.py)) / nh([nh.py:104-107](app/crawlers/nh.py)) / sc([sc.py:120-123](app/crawlers/sc.py)) / ibk([ibk.py:135-213](app/crawlers/ibk.py)) — IBK는 00:00~00:05 공식 날짜 POST 실패 뒤 Selenium을 억제하고 정상 return하며, 이후 Selenium/MIBANK 총실패도 마지막 경로에서 raise 없이 return한다. 둘 다 subprocess exit 0 → false success가 될 수 있다.
- DXY([dxy_spot.py:451](app/crawlers/dxy_spot.py#L451)) — fallback chain이 모두 실패해도 warning 후 정상 반환. 단 정책 상태 불일치는 전용 예외로 전파한다.

→ **subprocess returncode 0 = "top-level 함수가 raise 안 함"이지 "크롤 성공"이 아니다.** crawler_stats.last_success_at은 전 소스에서 장애 중에도 갱신될 수 있어 그대로는 collection-success 신호로 부적합. (초기 오해: "Selenium은 returncode 기반이라 신뢰"는 틀림 — 함수가 삼키면 returncode도 0.)

---

## 3. 핵심 설계 제약

1. per-(source,asset) 유효-수집 단일 신호 **없음**.
2. durable 신호 2개가 **상보적·각 불완전**:
   - **collection_success**(수집 성공 = 정적 시장↔사망 구분 O) — 단 crawler_stats는 §2.1로 신뢰 불가 + USDT/KRX 미커버 + 휘발.
   - **value_changed_at**(전 소스·영속) — 정적 시장↔사망 구분 **X**.
3. USDT in-process liveness signals([§12.9.8](USDT_WS_DESIGN_PLAN.md))는 **in-process만 → Redis/DB export가 신규 필요**(단순 소비 불가). **연결 liveness(`is_stale`, heartbeat 기반)와 데이터 진행성(Gopax ticker-dead, REST 교차검증)은 다른 층** — is_stale은 heartbeat만 살아있고 ticker 정지한 장애를 **놓친다**. 데이터 진행성 확인은 Gopax만 보유, 나머지 4곳 미커버.
4. [source_registry.py:25](app/source_registry.py)에 이미 `observed_at / last_collection_success_at` 별도 추적 TODO 주석 존재 — 설계 의도 정합.
5. **cadence 마스킹이 오탐의 핵심.** 한국 공휴일 캘린더([kr_holidays.py](app/calendars/kr_holidays.py), observed=True)는 daily-append cron 전용이라 **realtime 경로 미연결**(휴일 대량 오탐 창).

---

## 4. Cadence — `collection_expected` ⊥ `market_expected` (2 별개 축)

**두 의미를 분리해야 한다** (codex Medium): `collection_expected`(스케줄러상 수집이 실행돼야 하는가 — **collection_success 판정을 gate**)와 `market_expected`(시장·고시 값이 움직일 수 있는가 — **value_changed 진단만** 마스킹). **시장 휴장만으로 collection 판정을 skip하면 주말에도 도는 crawler의 장애를 은폐**한다 — investing과 hana/shinhan/bs/nh는 OUT에도 등록된다([scheduler.py:983-1097](app/scheduler.py#L983)).

| source군 | collection_expected (수집 실행 스케줄) | market_expected (값 변화 가능) |
|---|---|---|
| 은행 9종 | 4-모드가 소스별 등록/제거 + **은행별 cutoff**(shinhan ~02:59:18 / woori ~05:04:53 / ibk BREAK1 ~05:59:34 + terminal 2회 / sc ~18:59:58) — ⚠️ **`get_market_mode()`만으로 `collection_expected`를 계산하면 false stall이 난다**(BREAK1 안이어도 해당 은행 job이 없는 구간이 있음). cutoff를 함께 봐야 한다 | 고시 window (kb 08:30~익일05:00 / hana ~06:00 / shinhan ~02:45 / woori ~05:00 / ibk ~06:00 / nh ~24:00 / sc 09:00~약17:50 / bs 08:10~24:00 / citi 09:00~익일06:00 — **2026-08-28 갱신**(우리/IBK 연장 고시 반영)), 주말·**공휴일** off |
| investing | 상시(전 모드 등록, OUT 1min) | 월06:00~토06:00(글로벌 FX), FX 휴일 off |
| 거래소5 | 24/7(lifespan collector) | 24/7(단 소스별 sparse 상이, gopax 평상 ~180s) |
| dxy | dxy_spot 상시 / dxy_futures=investing 종속 | investing과 동조 |

- **collection_success 판정은 `collection_expected`일 때만** 수행(휴장이라도 crawler가 도는 창이면 stall=실장애). `market_expected=false`는 **value_changed 진단만** 마스킹(값 안 변함이 정상).
- **collection_expected는 eligibility + interval-aware** (codex): eligibility(`scheduled_off`/`admin_disabled`)이며 stall 임계는 모드별 주기(현재 등록 소스는 IN/OUT 모두 10~60s)에 맞춘 **`expected_interval`/`next_due_at + grace`**. dispatch·run 결과(queue_full=dispatch health / timeout=run failure / transition=run skip)는 collection_expected가 아니라 **§6-2 3계층 계약**에서 분리(사건 계층이 다름).
- **재사용**: [market_mode.get_market_mode](app/market_mode.py)와 source별 실제 등록 trigger/cutoff를 함께 사용해 collection_expected를 파생한다. 모드만 단독 사용하면 BREAK1 안의 신한·우리 종료 뒤를 false stall로 오판한다. [kr_holidays](app/calendars/kr_holidays.py)는 realtime 연결 시 market_expected 공휴일 마스크로 사용한다.

---

## 5. Shadow 계약안 (log-only, 코드 전 결정)

1. **상태 축 분리 (health ⊥ collection_expected ⊥ market_expected)**:
   - health: `healthy` / `degraded` / `unknown`
   - `collection_expected` (스케줄 파생 — §4): collection_success 판정을 **gate**. false면 판정 skip(healthy로 칠하지 않음).
   - `market_expected` (시장 파생 — §4): value_changed **진단**만 마스킹. **collection 판정엔 무관**(주말에도 도는 crawler의 장애 은폐 방지).
2. **collection_success(또는 유효 observation)를 주 신호, value_changed_at은 진단 정보로만** (codex High 2). 값이 오래 안 변해도 수집 성공 중이면 정상 — value_changed age를 health gate로 AND하면 정상 정적 시장을 오탐. (초기 오해: "collection_success ∩ value_changed" 교집합 판정은 틀림.)
3. **신뢰 가능한 성공 신호 부재 → `unknown`**(≠ degraded/dead):
   - 재시작 직후(crawler_stats 휘발) / crawler_stats 미커버 소스(USDT/KRX) / §2.1로 신뢰 불가한 collector 모두 `unknown`.
4. **as_of(HH:30:00) ≠ eval(HH:30:19)** — precompute cron은 :30:19 발화([free_snapshot.py:35](app/free_snapshot.py)), as_of는 :30:00 floor. liveness는 eval-now(:30:19) 상태로 읽되 basis 라벨 병기, 계약에 어느 기준인지 명시.
5. **USDT는 초기엔 관대 판정 or unknown** — value 신호(seen_at age)만으론 저유동 오탐 → liveness export(§6-4, 특히 데이터 진행성 신호) 전까진 무리한 stall 판정 금지.
6. **API/UI 노출 0** → **≥7일 log-only 관측**(주간 모드 사이클 + deterministic 휴일 테스트, §6-5) → 오탐률 확인 → 그 후 영속 heartbeat(Redis) 필요성 + API/UI 계약 결정. **단일-worker in-memory 직접참조를 정식 계약으로 즉시 승격 금지**(codex ③ — [Dockerfile:118](Dockerfile) `--workers 1` 의존, multi-worker 시 깨짐. 리포 전반 USDT/KRX in-memory coalesce와 동일 load-bearing 가정).

---

## 6. 구현 순서 게이트 (codex 수정안, 결정 후 착수)

1. **per-asset 유효-수집 성공 기준 정의 (선행)** — "예외 없이 반환"이 아니라 "해당 (source,asset)의 **유효 데이터**를 실제로 받았나"(통화별 부분 실패 은닉 해소). **이 정의 없이는 아래 success/partial/failure를 분류할 수 없다**(codex — validity가 결과 계약보다 먼저).
2. **실행 결과를 3계층으로 분리하는 계약 설계**(codex — 사건 발생 위치가 달라 collector 결과 하나로 못 묶음):
   - **eligibility**(스케줄러 등록 전): `enabled` / `scheduled_off`(mode) / `admin_disabled`(`crawler_config`) — collector 안 돎.
   - **dispatch**(스케줄러→executor, started 안 됨): `dispatched` / `queue_full`([scheduler.py:485](app/scheduler.py#L485) 80% 거부) / `misfire`(grace 초과) / `backpressure`. **반복 시 `degraded`**(계획 아니라 시스템 압력 누락).
   - **run**(collector 실행): `success` / `partial` / `failure`(`failure_reason`=timeout 등) / `skipped`. **timeout은 skip 아니라 run failure**(계획 skip과 재혼입 금지). IBK는 00:00~00:05에도 collection을 시도한다. 날짜 지정 Request로 USD·JPY·EUR를 유효 관측하면 `changed_count=0`이어도 success이고, 이 Request 실패 뒤 자정 창 때문에 Selenium만 억제되면 `observed_assets=∅`, `failure_reason=request_failed`, `fallback_suppressed=midnight_transition`인 failure여야 한다([ibk.py:120-159](app/crawlers/ibk.py)). 완전한 DB snapshot의 공식 무고시는 `no_observation_preserved`, 과거 서비스일 회귀 차단은 `stale_regression_preserved`(부분 안전 통화 관측이 있으면 `partial`)로 분리해 exit 0을 무조건 success로 오인하지 않는다.
   collector 실행결과는 **run 계층만 반환**(eligibility·dispatch는 안 돌았으니 스케줄러 계층에서 별도 관측). run 계층에서 §6-1 유효성으로 `attempted_assets`/`observed_assets`/`failed_assets` 판정. 전 collector가 총실패를 삼킴(§2.1)이라 이 계약 선행 없이 shadow 무의미. **저장소(§6-4/D1/D4)는 observed_assets 반환한 다음**(crawler_stats는 source 단위라 per-asset 단독 미충족).
3. **collection_expected(eligibility + interval-aware) + market_expected + 공휴일 정책 정의** — collection_expected = eligibility(`scheduled_off`/`admin_disabled`)이며 timing은 Bool 아닌 **`expected_interval`/`next_due_at + grace`**(현재 등록 소스는 IN/OUT 모두 10~60s). **dispatch(queue_full/misfire)·run(timeout/transition) 결과는 collection_expected가 아니라 §6-2 3계층** — queue 포화는 억제가 아니라 health 영향(§4·§6-2 정합). market_mode(eligibility 파생) + kr_holidays(market_expected) realtime 연결.
4. **timezone-aware heartbeat 저장 설계** — collection_success_at을 per-(source,asset) 영속(Redis/DB), tz-aware. USDT는 in-process liveness(is_stale/ticker-dead/task.done) export 포함.
5. **≥7일 shadow 관측 + deterministic 휴일 테스트** (log-only) — 1~2일은 평일만 보고 끝나 BREAK/OUT·주말 전환 오탐을 검증 못 함(codex). 주간 모드 사이클(평일 IN/BREAK + 주말 OUT) 전체 + **주입식 공휴일 테스트**(실휴일 대기 불요)로 cadence 마스크 검증.
6. **오탐률 확인 후 API/UI 계약 검토** (additive health 필드 → iOS 최소 배지, 확실한 장애만).

---

## 열린 결정 (미해결)

- (D1) collection_success 저장: crawler_stats export vs source_registry TODO 기반 신규 필드 — **단, §6-2 실행결과(observed_assets 포함)가 정해진 뒤 결정**(crawler_stats는 source 단위라 per-asset 목표 단독 미충족).
- (D2) §6-2 실행결과 계약을 크롤러 리팩터로 할지 wrapper 레벨로 우회할지. **성공 기준은 `observed_assets`/`valid_observation_count`(유효 관측 asset)이며 `changed_rows`(new_records_count)는 진단값** — new_records_count는 변경-시-INSERT라 정상 수집도 0건이라 **성공 판정에 사용 불가**(codex High). §6-1(유효성 정의)이 이 기준의 선행.
- (D3) USDT in-process liveness export 대상/형식(is_stale만 vs ticker-dead/task.done 포함), 5거래소 ticker-dead 공통화([§12.9.8①](USDT_WS_DESIGN_PLAN.md)) 선행 여부.
- (D4) 영속 heartbeat 저장소(Redis vs DB) 및 multi-worker 대비 필요성 — shadow 관측 후.
- (D5) Citi(수집O·표시X) 및 미표시 소스의 health를 관측만 할지 무시할지.
- (D6) source-health가 무료 트랙 출시 blocker인지 fast-follow인지 (제품/타임라인 결정).
