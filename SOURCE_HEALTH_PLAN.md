# Source-Health Plan (무료 스냅샷 소스별 건강 관측)

> **상태: Investing 슬라이스 1 보고 전용 — 구현 완료(§7.10) / 2026-09-18 운영 배포·promote 완료 / 집계·알림 미구현**
> 검증: 5-agent Workflow 코드 인벤토리 + codex 다라운드 리뷰(인벤토리→계약 정정 반복) + Claude 코드 재검증 (2026-07-22).
> **2026-09-13 개정**: §7 신설 — 9은행 + Investing의 결과 보고·집계 계약, 지속장애 알림, 작업 미실행 감지. §2·§2.1·§4·§6-2의 낡은 현재형 서술 정정. **현재 Investing 보고 전용 구현은 완료(§7.10)했고, 집계·알림은 여전히 미구현이다.**
> **2026-09-18 배포**: `721a323` 를 §8.1a 방식으로 배포·promote 완료(§7.11). 수동 보존·일별 집계 도구는
> 구현되어 운영 표본을 확인했다. **지속적인 source-health 자동 판정·집계·알림은 미구현**이며,
> 정기 점검 담당·주기는 미확정이다.
> 범위: ADR-039 무료(비구독) 매시간 스냅샷([FREE_TIER_ACCESS_MODEL_PLAN.md](FREE_TIER_ACCESS_MODEL_PLAN.md))이 노출하는 소스의 **개별 stall** 관측.
> [free_snapshot.py:59-64](app/free_snapshot.py) 주석이 명시하듯 S6(24h whole-snapshot cutoff)는 스냅샷 전체 정지만 잡고 **개별 소스 stall은 못 잡는 층** — 이 문서가 그 후속 계약.
> ⚠️ **구현 착수 전 §6 게이트 결정 필수.** 아래 설계는 여러 열린 결정을 포함한다.
> ⚠️ **결과 보고·집계(거짓 성공 수리 포함)·판정기와 log-only shadow 구현**은 검토 후 진행한다 — "보냈을 경보"를 관찰하려면 판정기와 log-only 실행이 먼저 있어야 한다. **실제 경보 발송과 새 health API/UI 노출**은 shadow 관측(§5) 결과 검토 후 별도로 승인한다(§6 순서도 결과 계약 ② → shadow ⑤ → API/UI ⑥).

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
| `crawler_stats.last_success_at` | collection_success (명목) | 9은행+investing+dxy_spot | in-mem 휘발 | naive **local** | **다수 collector가 총실패를 삼킴 → 장애 중에도 success**. 예외를 올리는 collector도 부분 통화·저장 결과는 구분 못 함(§2.1, 2026-09-13 재측정). bank단위·재시작 소실 |
| DB row `timestamp`(bank/investing/source_rates/market_index) | value_changed | **전 소스** | 영속 | naive **UTC** | 정적 시장↔collector 사망 구분 X. write-gate skip 시 미전진 |
| USDT `seen_at` | last_observed | 거래소5 | Redis 휘발 | KST | 거래소 event ts(5s floor). 정적↔사망 구분 X |
| USDT `rate_changed_at` | value_changed | 거래소5 | Redis 휘발 | KST | 값 변경 시각 |
| USDT `mirrored_at` | collection_success proxy | 거래소5 | Redis 휘발 | KST | coalesce·무-tick freeze, 미소비 |
| bank/investing `mirrored_at` | (mirror liveness) | 은행+investing | Redis | KST | **FALSE-NEG**: 크롤러 다 죽어도 mirror가 3s/60s마다 stale DB latest 재복사 → 영원히 fresh |
| in-process liveness signals — `is_stale`(연결 heartbeat 위주)·Gopax ticker-dead(데이터 진행성)·supervisor `task.done()`(task 사망) | liveness(층별 상이) | USDT only(ticker-dead는 Gopax만) | 비영속·**미노출** | — | **is_stale은 heartbeat fresh+ticker dead를 못 잡음**(연결 liveness≠데이터 진행성). 스냅샷/Redis/DB 미노출 |

**tz 3종 혼재**: crawler_stats=naive local / DB=naive UTC / Redis=KST-aware → 섞으면 컨테이너 offset(운영 KST ~9h) 오차. heartbeat 저장은 timezone-aware로.

### 2.1 crawler_stats가 신뢰 불가한 이유 (codex High 1, 코드 검증됨)

**부모가 성공을 판정하는 신호가 경로마다 다르다.** 줄 좌표는 편집마다 밀리므로 심볼로 적는다.
- **Request 경로**: `scheduler.make_request_crawler_wrapper` 의 `wrapper` 가 `job_func()` 이 **예외 없이 반환**하면 `record_success`, 예외면 `record_failure`. **재등록 없음.**
- **subprocess 경로**(shinhan·nh·sc): `runner.main` 이 `crawler_func()` 이 raise하지 않으면 `sys.exit(0)`, raise하면 `sys.exit(1)`. 부모 `execute_with_timeout` 은 `proc.returncode == 0` 을 성공으로 본다. 워커는 `should_retry = not success` 로 **부모가 실패로 판정하면 최대 1회 재등록**한다. 타임아웃·비정상 종료는 실패로 처리돼 재등록 조건에 들어간다.
- **IBK 타입 결과 경로**: `IBK_RESULT_PATH_ENABLED`(운영 true) 게이트 뒤 `runner` 가 `ibk.run_ibk_dated_result` 를 바인딩해 `IbkResult`(OBSERVED/PRESERVED/DEGRADED/FAILED + `IbkReason`)를 result frame으로 전달하고, 부모 `IbkParentDecision` 이 **상태와 `should_retry` 를 별도 필드**로 든다. legacy 진입 `crawl_and_save_ibk_bank_exchange_rates` 는 provisional `IbkLegacyResult` 를 **의도적으로 버리고** None을 반환한다(docstring: "부모 wire 계약 아님").

**2026-09-13 재측정 — 10개 collector의 총실패 처리**

| 분류 | collector | 한계 |
|---|---|---|
| ✅ 총실패를 예외로 전달 (3) | kb · hana · woori | 부분 통화·저장 0 은 여전히 구분 못 함 |
| 🔴 총실패를 삼킴 (6) | investing · bs · citi · shinhan · nh · sc | 부모가 성공 집계 |
| 🔷 타입 결과 경로 (1) | ibk | 보존하며 공통 화면·집계에 연결할 대상 |

같은 부류로 묶이지 않는 세부 사실(전부 코드로 확인, 격리 재현 여부는 §7.8):
- **MIBANK `hard_fail` 처리가 갈린다** — kb·hana는 `RuntimeError` 로 올리고, bs·citi·shinhan·nh·sc는 로그 후 반환한다.
- **bs·citi의 신뢰창 밖 종료는 '회차 생략'이 아니다** — 공식 조회를 이미 시도해 **실패한 뒤** `is_mibank_rate_reliable()` 거짓으로 MIBANK만 생략하고 정상 반환한다. woori는 같은 자리에서 `RuntimeError("모든 공식 경로 실패, 신뢰 불가 시간대라 MIBANK 건너뜀")` 를 올린다(선례).
- **investing은 쿨다운(HTTP 0회)과 양쪽 403(HTTP 2회 실패)이 둘 다 성공으로 접힌다.** 쿨다운은 연속 차단 수에 따라 최대 15분까지 늘어난다.
- **shinhan·nh·sc는 MIBANK가 주 경로**이고 Selenium은 `soft_fail` 재검증과 MIBANK 예외 폴백을 겸한다. shinhan은 `soft_fail` + 재검증 실패 + `hard_fail` 아님이면 MIBANK 값을 그대로 저장한다. 성공 `return` 과 포기 `return` 이 같은 모양이다.
- **부분 통화**: 저장 루틴이 통화별 선택자·파싱 실패를 `continue` 로 건너뛰고 얻은 통화만 저장 함수에 넘겨 정상 반환한다. woori·sc의 Selenium 경로는 이 처리를 **하위 현재 날짜 파서**(`woori.crawl_woori_current_date_rates`, `sc.crawl_current_date_rates`)에 위임한다 — 루틴 본문만 보면 패턴이 안 보이므로 위임을 따라가야 한다. 현재 날짜 경로의 부분 통화 전달·정상 반환은 격리 재현으로 확인했고, 과거 날짜 경로·실제 DB 반영·부모 집계는 그 재현 범위 밖이다(§7.8).
- **저장 0 은 세 사실을 뭉갠다** — `insert_bank_rates_into_db` / `insert_investing_rates_into_db` 는 값 불변·쓰기모드 미확정·차단 모두 0을 반환하고 호출자는 구분할 수 없다. 차단은 `crud._write_mode_skip_counts` 근사 계수와 debug 로그로만 남고 **해당 회차 결과·부모 집계와 연결되지 않는다.** ⛔ 값 불변 회차의 성공 집계는 옳으므로 **0 을 실패로 바꾸면 안 된다.**
- **세션 예외는 이미 부모까지 간다** — Request 경로 collector는 `SessionLocal()` 을 `try` 밖에서 만들고 `finally` 에서 닫는다.
- DXY(`dxy_spot`)는 이번 재측정 범위 밖 — 기존 2026-07-22 서술을 그대로 둔다(이후 파일이 바뀌었으나 재확인하지 않음): fallback chain이 모두 실패해도 warning 후 정상 반환하고, 정책 상태 불일치는 전용 예외로 전파한다.

→ **subprocess returncode 0 = "top-level 함수가 raise 안 함"이지 "크롤 성공"이 아니다.** 그리고 **raise 한다고 정확히 보고하는 것도 아니다**(kb·hana·woori도 부분 통화·저장 0을 성공으로 접는다). crawler_stats.last_success_at은 장애 중에도 갱신될 수 있어 그대로는 collection-success 신호로 부적합. (초기 오해: "Selenium은 returncode 기반이라 신뢰"는 틀림 — 함수가 삼키면 returncode도 0.)

### 2.2 운영 로그에서 보이지 않는 사실

운영은 `LOG_LEVEL=INFO` 다. 아래 사실은 **DEBUG 로만 남거나, 자식 프로세스의 출력이 Docker 로그로 오지
않아** 운영 Docker 로그로 판정할 수 없다(2026-09-18 코드 확인, 심볼로 적는다). ADR-044 의 우리은행
마무리 조회 판정과 bs·citi 결과 보고 설계가 이 표에 걸린다.

| 확인하려는 사실 | 현재 증거·레벨 | 로그에 있는 필드 / 회차·시도 ID | 증거로 말할 수 있는 범위 | 추가 관측 후보 |
| --- | --- | --- | --- | --- |
| Selenium 큐 **일반 스케줄 적재** | `scheduler` 의 `📥 … Priority Queue 추가` — **DEBUG**. 재시도 재적재 `🔄 … 재시도 Queue 추가` 는 INFO | 은행 이름·우선순위·대기 수 / **없음** | 운영에서는 **꺼낸** 시각(`selenium_job_executor` 의 `🔄 … Queue 처리 시작`, INFO)과 재시도 재적재만 보인다. 일반 적재를 누가·언제 했는지 모르고, 재시도 로그도 적재→꺼냄을 잇지 못한다(2026-09-18 IBK 06:00:34 는 꺼낸 시각만 확인) | 적재 시각·발화 job ID·예정 시각을 꺼냄과 같은 식별자로 |
| Request 경로 회차의 **정상 완료**(공통 wrapper) | `make_request_crawler_wrapper` 의 `✅ [은행] 완료 (N초)` — **DEBUG** | 은행 이름·소요 시간 / **없음** | 공통 wrapper 로는 시작(`… 시도`, INFO)과 저장(`🎉`, 값이 바뀐 회차만)만 보인다. 값이 같은 정상 회차의 **끝**을 볼 수 없다. Investing 의 별도 INFO 보고 이벤트(§7.10)는 이 행과 무관하다 | 회차 식별자 + 완료 기록 |
| **값 불변** | `crud` 의 `📼 [유지]`·`✋ … 변경사항 없음` — **DEBUG** | `pair`·`rate`·`type`·`bank` / **없음** | 저장 0 이 값 불변인지 알 수 없다(§2.1 "저장 0 은 세 사실을 뭉갠다") | 회차 단위 통화별 결과(신규·변경·불변) |
| **쓰기 모드 차단** | `crud._record_write_mode_skip` — 근사 계수 + **DEBUG** `write-mode skip (staging 전 차단)` | `source`·`enforced` / **없음** | 차단 사실이 그 회차 결과·부모 집계와 연결되지 않는다 | 회차 결과에 차단 사유 |
| MIBANK 로 **얻은 통화 목록** | `utils` 의 `mibank 수집 통화`(found·captured 전체 목록) — **DEBUG** | `bank`·`found`·`captured` / **없음** | found·captured 전체 목록은 운영에서 모른다. 다른 경고·저장 로그에서 일부 통화가 드러날 수는 있다 | 경로·시도 식별자와 함께 |
| Selenium **자식 프로세스의 출력** | 부모가 자식 stdout 을 Docker 로그로 넘기지 않는다. legacy 큐 경로 `execute_with_timeout` 은 PIPE 를 성공 시 읽지 않고 실패 시 stderr 앞 500자만. 우리·하나 Selenium 경로는 `subprocess.run(capture_output=True)` 뒤 **실패 시 자식 stdout 꼬리 1500자와 stderr(둘 다 마스킹 후), 타임아웃 시 자식 stdout 꼬리만** 기록한다(2026-09-21 — 타임아웃 예외에는 stderr 가 None 일 수 있다). 운영 IBK 는 별도 실행기(`ibk_parent.execute`) | — | **확인됨(2026-09-21)**: 자식도 같은 로깅 설정으로 `/app/logs/app.log`·`error.log` 에 쓰고, `error.log` 에는 `exc_info` 까지 남아 6주치가 보존돼 있었다(`app.log` 는 10MB×3 회전이라 몇 시간). 즉 **기록이 없는 것이 아니라 Docker 로그 스트림에 안 온다**. 우리·하나는 위 꼬리로 그 간극을 메웠고, 큐 경로는 아직 stderr 500자뿐이다 | 자식 결과를 구조화된 frame 으로 부모에 전달(IBK result frame 선례) |

⛔ **INFO 로 올리는 것만으로는 풀리지 않는다.** 위 행들은 공통으로 회차·시도를 묶는 식별자가 없다 —
레벨만 올리면 "무언가 완료됐다" 는 줄이 늘 뿐, **어느 예약 발화의 어느 시도가** 끝났는지 잇지 못한다.
마지막 행은 레벨 문제가 아니라 전달 경로 문제다.

⚠️ **코드상 위험 후보, 운영 발생 미확인**: legacy 큐 경로(`execute_with_timeout`)는 stdout·stderr 를 PIPE 로
잡은 채 읽지 않고 `proc.wait()` 를 기다린다. 자식 출력이 파이프 버퍼를 넘으면 자식이 쓰기에서 멈춰
타임아웃처럼 보일 수 있다(파이썬 문서가 경고하는 형태). 실제 자식 출력량은 재지 않았다. `subprocess.run`
경로(우리·하나)는 내부에서 출력을 끝까지 읽으므로 해당하지 않는다.

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

**두 의미를 분리해야 한다** (codex Medium): `collection_expected`(스케줄러상 수집이 실행돼야 하는가 — **collection_success 판정을 gate**)와 `market_expected`(시장·고시 값이 움직일 수 있는가 — **value_changed 진단만** 마스킹). **시장 휴장만으로 collection 판정을 skip하면 주말에도 도는 crawler의 장애를 은폐**한다 — investing과 hana/shinhan/bs/nh는 OUT에도 등록된다([`_switch_jobs_body`](app/scheduler.py) 의 `elif mode == "OUT"` 분기).

| source군 | collection_expected (수집 실행 스케줄) | market_expected (값 변화 가능) |
|---|---|---|
| 은행 9종 | 4-모드가 소스별 등록/제거 + **은행별 cutoff**(shinhan ~02:59:18 / woori BREAK1 ~05:59:44 + 화~토 마무리 06:00~06:03 `:14/:44`·06:04:53 9회 / ibk BREAK1 ~05:59:34 + terminal 2회 / sc ~18:59:58) — ⚠️ **`get_market_mode()`만으로 `collection_expected`를 계산하면 false stall이 난다**(BREAK1 안이어도 해당 은행 job이 없는 구간이 있음). cutoff를 함께 봐야 한다 | 고시 window (kb 08:30~익일05:00 / hana ~06:00 / shinhan ~02:45 / woori **05:55:56 관측(2026-09-05), 종료 시각 미확정** / ibk ~06:00 / nh ~24:00 / sc 09:00~약17:50 / bs 08:10~24:00 / citi 09:00~익일06:00 — **2026-08-28 갱신**(우리/IBK 연장 고시 반영), **2026-09-12 우리 재갱신**(ADR-044: 06:00은 관측이 아니라 우리가 정한 수집 종료 정책)), 주말·**공휴일** off |
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
     ⛔ **정상 제외는 `scheduled_off`·`admin_disabled` 뿐이다.** 수집해야 하는데 등록·시작되지 않은 작업(**등록 누락**)은 정상 제외가 아니라 **탐지 대상**이다. 기대 실행 여부는 실제 등록 목록이 아니라 수집 정책·시간표·관리자 설정으로 **먼저 계산하고**, 그다음 등록·시작 여부와 대조한다(§7.6).
   - **dispatch**(스케줄러→executor, started 안 됨): `dispatched` / `queue_full`([`enqueue_selenium_job`](app/scheduler.py) 의 80% 이상 거부 — `current_size >= 20`, 25칸 기준) / `misfire`(grace 초과) / `backpressure`. **반복 시 `degraded`**(계획 아니라 시스템 압력 누락).
   - **run**(collector 실행): `success` / `partial` / `failure`(`failure_reason`=timeout 등) / `skipped`. **timeout은 skip 아니라 run failure**(계획 skip과 재혼입 금지). (역사적 사례 — **IBK legacy 경로** `crawl_ibk_legacy_result` 기준, 2026-07-22 작성) legacy 경로는 00:00~00:05에도 collection을 시도한다. 날짜 지정 Request로 USD·JPY·EUR를 유효 관측하면 `changed_count=0`이어도 success이고, 이 Request 실패 뒤 자정 창 때문에 Selenium만 억제되면 `observed_assets=∅`, `failure_reason=request_failed`, `fallback_suppressed=midnight_transition`인 failure여야 한다. ⚠️ 운영(게이트 ON)의 IBK 타입 결과 경로 `produce_ibk_dated_result` 는 Selenium 안전망을 **HTTP 기술 실패(`CandidateSearchStop.TECHNICAL_FAILURE`)에서만** 열고 무고시·개장 전·예산 소진에서는 열지 않는다 — 이 문장을 그 경로의 동작으로 읽지 말 것(§2.1). 완전한 DB snapshot의 공식 무고시는 `no_observation_preserved`, 과거 서비스일 회귀 차단은 `stale_regression_preserved`(부분 안전 통화 관측이 있으면 `partial`)로 분리해 exit 0을 무조건 success로 오인하지 않는다.
   collector 실행결과는 **run 계층만 반환**(eligibility·dispatch는 안 돌았으니 스케줄러 계층에서 별도 관측). run 계층에서 §6-1 유효성으로 `attempted_assets`/`observed_assets`/`failed_assets` 판정. 다수 collector가 총실패를 삼키고, 예외를 올리는 collector도 부분 통화·저장 결과를 구분하지 못하므로(§2.1) 이 계약 선행 없이 shadow 무의미. 구체 계약은 §7.2. **저장소(§6-4/D1/D4)는 observed_assets 반환한 다음**(crawler_stats는 source 단위라 per-asset 단독 미충족).
3. **collection_expected(eligibility + interval-aware) + market_expected + 공휴일 정책 정의** — collection_expected = eligibility(`scheduled_off`/`admin_disabled`)이며 timing은 Bool 아닌 **`expected_interval`/`next_due_at + grace`**(현재 등록 소스는 IN/OUT 모두 10~60s). **dispatch(queue_full/misfire)·run(timeout/transition) 결과는 collection_expected가 아니라 §6-2 3계층** — queue 포화는 억제가 아니라 health 영향(§4·§6-2 정합). market_mode(eligibility 파생) + kr_holidays(market_expected) realtime 연결.
4. **timezone-aware heartbeat 저장 설계** — collection_success_at을 per-(source,asset) 영속(Redis/DB), tz-aware. USDT는 in-process liveness(is_stale/ticker-dead/task.done) export 포함.
5. **≥7일 shadow 관측 + deterministic 휴일 테스트** (log-only) — 1~2일은 평일만 보고 끝나 BREAK/OUT·주말 전환 오탐을 검증 못 함(codex). 주간 모드 사이클(평일 IN/BREAK + 주말 OUT) 전체 + **주입식 공휴일 테스트**(실휴일 대기 불요)로 cadence 마스크 검증.
6. **오탐률 확인 후 API/UI 계약 검토** (additive health 필드 → iOS 최소 배지, 확실한 장애만).

---

## 7. 9은행 + Investing 결과 보고·집계 계약 (2026-09-13 개정)

> §1~§6 계획 중 **9은행 + Investing** 부분을 앞당겨 구체화한다. 거래소 5종·DXY·영속 heartbeat·사용자 배지는 §1~§6 범위로 남는다. **Investing 슬라이스 1 보고 전용 구현은 §7.10. 나머지는 계획이며 집계·알림 변경은 미구현.**
> 원칙: **은행별 수집 방법은 유지하고, 무엇을 확인했고 무엇을 모르는지 보고하는 방식과 지속 장애 감지를 통일한다.**

### 7.1 범위

- **이번**: 결과 보고·집계 계약 · 지속장애 알림 · 작업 미실행 최소 감지.
- **후속**: 영속 heartbeat · 사용자 건강 배지 · 정체 감지 확장 · 거래소·DXY.
- §5·§6-5의 ≥7일 shadow는 **알림 활성화의 선행 조건**이다. 거짓 성공 수리의 선행 조건으로 쓰지 않는다.
- 결과 기반 감지만으로는 **작업이 아예 안 도는 상태를 영원히 못 잡는다** → 최소 미실행 감지(§7.6)는 이번 범위다.

### 7.2 공통 결과 계약 — 5축

한 회차가 끝나면 아래 다섯을 **각각** 답한다. 하나로 접지 않는다. 값에는 **상태와 사유를 함께** 적는다.

| 축 | 값 | 사유 예 |
|---|---|---|
| **회차 수집**(통화별) | `유효관측` / `누락` / `미시도` / `확인 불가` | `누락` = **필요한 유효 관측을 확보하지 못함**. 사유 `no_value` / `validation_rejected` |
| **폴백별 상태**(단계별) | `시도·성공` / `시도·실패` / `정책상 미시도` / `앞 단계 성공으로 불필요` / `미도달` | `정책상 미시도`: `mibank_untrusted_window` 등. `미도달`: `budget_exhausted` / `cancelled` |
| **쓰기 동작**(통화별) | `수행` / `변경 불필요` / `정책 차단` / `실패` / `미시도` / `확인 불가` | `정책 차단`: `write_mode_uninitialized` / `write_mode_halt` |
| **최종 DB 확인**(통화별) | 의도값과 `일치` / `불일치` / `확인 불가` | — |
| **실행 결과** | `정상종료` / `시간초과` / `취소` / `비정상종료` | 자손 프로세스 정리 확인 여부는 **별개 필드** |

- ⛔ **쓰기 동작 ⊥ 최종 DB 확인.** "쓰기는 차단됐지만 필요한 값은 이미 DB에 있다"가 동시에 참일 수 있다. IBK `ibk_result_builder` 가 이미 이 차이로 판정을 가른다.
- ⛔ **회차 결과 ⊥ 폴백 생략 사유.** bs·citi가 근거다 — 시도해서 실패한 회차를 "MIBANK 생략"으로 접으면 지속 장애가 정책상 생략으로 숨는다.
- ⛔ **타임아웃은 실행 결과로 기록한다.** 수집·쓰기·DB 확인은 각각 **확보한 증거를 보존**하고 확정하지 못한 항목만 `확인 불가` 로 둔다. 이미 확인한 사실을 타임아웃이 지우지 않는다.
- ⛔ 쓰기·DB 확인을 **회차 단일 값으로 덮지 않는다** — 일부 통화만 반영된 부분 실패가 다시 숨는다.
- ⛔ **저장 0 을 실패로 바꾸지 않는다.** 값 불변 회차의 성공은 옳다.
- ⛔ **이관 전의 약한 성공 신호를 이름만 바꿔 '검증된 성공'으로 승격하지 않는다.**
- `유효관측` 인정 기준은 §6-1 선행 결정이다.
- IBK는 **구조만** 재사용한다. `OBSERVED` 는 "이번에 썼다"가 아니라 기대 서비스일 공식 3통화 관측 + 의도한 최종값과 완전한 DB 일치이고, enum을 그대로 일반화하지 않는다.

### 7.3 통합하지 않는 경계

파싱·날짜/통화/범위 검증 · 수집 시간표와 휴일·개장 전 보존 · HTTP·Selenium·MIBANK 선택과 허용 조건 · **은행별 폴백 순서** · **`soft_fail` 값 채택 정책** · 재시도 간격 · 경보 임계. IBK의 "유효 결과가 있으면 재시도 금지" 불변식(`IbkParentDecision`)도 **IBK 전용**으로 남긴다.

### 7.4 집계·API — 구현 전 결정 항목

`success_rate = 성공/(성공+실패)` 공식을 유지해도 **세는 대상이 바뀌면 지표 의미가 바뀐다.** 관리자 화면이 이 값으로 경고 색을 정하므로 **"API 호환 확보"는 아래 결정 뒤에만 말한다.**

- `partial` / `unknown` / `preserved` 의 집계 방식과 분모 (D7).
- 구 의미 누적 통계와 새 통계를 구분하는 방법.
- **쿨다운 생략은 `last_success_at` 을 갱신하지 않고, 연속 실패·장애 상태를 초기화하지 않는다.**
- 회차·재시도·내부 폴백의 집계 단위와 책임자. **타임아웃 뒤 늦게 도착한 결과를 중복 집계하지 않는다.**
- 재시도 결정은 집계와 분리한다. 지금은 subprocess 경로가 같은 불리언(`should_retry = not success`)을 쓴다.

### 7.5 지속장애 알림

**생명주기**: 일시적 오류는 기록만 → 지속 기준 충족 시 최초 알림 1회 → 지속 중 간격 재알림 → **확인된 복구** 시 1회 알림.

**두 지속 시간을 따로 잰다**:
1. 수집해야 하는 시간에 **필요한 유효 관측을 확보하지 못한 시간**.
2. **반영해야 할 값과 최종 DB가 불일치한 시간** — 은행에서 새 값을 계속 받는데 DB에는 옛 값이 남는 장애.

**평가 보류와 쿨다운을 구분한다**:
- **평가 보류**: 수집 시간대 종료 · 관리자 비활성. 보류는 **복구 확인이 아니다.**
- **쿨다운은 평가 보류가 아니다.** 추가 요청은 억제하되, 수집이 필요한 시간이라면 유효 관측 공백과 기존 장애의 지속 여부를 **계속 평가**한다. 쿨다운 회차를 새 수집 실패로 세지 않고, 복구로도 처리하지 않는다. 근거: investing 쿨다운은 차단이 반복될수록 최대 15분까지 길어진다 — 평가를 멈추면 **차단이 심해질수록 감시가 쉰다.**

**판정 규칙**:
- 연속 실패 **횟수만으로** 장애를 판정하지 않는다(미실행·주기 차이를 놓친다). 시간·실행 주기와 함께 보조 지표로는 쓸 수 있다.
- 은행별·통화별로 본다. USD 성공이 JPY·EUR의 지속 실패를 가리지 않는다.
- 쓰기가 차단됐어도 **DB가 이미 의도값과 같으면** 차단 사실만으로 장애를 선언하지 않는다.
- 공식 무고시·개장 전 보존을 "새 관측 없음" 장애로 만들지 않는다.
- `확인 불가` 가 오래 지속되면 "장애 확정"이 아니라 **"상태 확인 불가 지속"** 으로 알린다.
- DB 일치를 Redis·API·알림 **전달 성공으로 확대하지 않는다.** 하류 확인 범위는 따로 적는다.
- IBK 기존 통지(`ibk_parent_runner` 의 통지 상태 관리)와 공통 알림이 **같은 사건을 중복 발송하지 않도록** 소유권을 정한다 (D8).

**전달**: 접수 ⊥ 발송 성공 · 발송 재시도 ⊥ 크롤러 재실행. `ibk_notice_delivery` 의 구조를 참고한다.
**활성화**: "보냈을 경보" shadow 관찰 + §7.9 격리 시험 통과 후.
리소스 과부하 경보(`admin/monitor.py`)는 **별개 정책이며 이번 범위 밖**이다. 수집 장애를 리소스 수치로 대신 판정하지 않는다.

### 7.6 작업 미실행 감지

- 기대 실행 여부는 **수집 정책·시간표·관리자 설정으로 먼저 계산**하고, 그다음 등록·시작 여부와 대조한다.
- 정상 제외는 `scheduled_off` / `admin_disabled` **뿐**이다. ⛔ **등록 누락은 정상 제외가 아니라 탐지 대상**이다.
- 판정은 Bool이 아니라 **`expected_interval` / `next_due_at` + grace** 로 한다 — 유예 없이 비교하면 정상 대기 중에도 경보가 난다.
- `dispatch` 계층(`queue_full`·`misfire`·`backpressure`)은 반복 시 `degraded`.
- 감시는 크롤러 결과가 돌아올 때가 아니라 **별도 주기 점검**으로 돈다.
- **재시작**: 직후는 `unknown` 으로 시작하고, 첫 기대 실행과 유예시간을 기준으로 판정한다. 이전 장애가 복구됐다고 추정하지 않는다. ⚠️ 재시작을 가로지르는 장애 지속 시간·알림 중복 억제는 이번 단계가 보장하지 못한다(영속 heartbeat는 후속).
- ⚠️ 앱 전체가 멈추면 앱 내부 점검도 멈춘다. 외부 감시는 이번 범위 밖이다.

### 7.7 이관 순서

1. **investing** 첫 적용. **KB는 항목별 대조군** — 정상 수집 보존·전면 실패의 부모 실패 집계는 기준이 되지만, 부분 통화·저장 0은 KB도 공백이다.
2. **bs · citi** — `hard_fail` 삼킴과 신뢰창 밖 종료를 woori 선례로 정렬.
3. **hana · woori** — 내부 Selenium subprocess 폴백까지 한 흐름으로(프로세스를 넘는 증거 계약은 §7.13).
4. **shinhan · nh · sc** — ⚠️ **재시도 부하 검증 필수.** 실패를 전달하도록 고치는 순간 부모 재등록이 붙어 요청·Chrome 기동이 늘 수 있다.
5. **ibk** — 기존 `IbkResult` 를 보존하며 공통 화면·집계에 연결.

⛔ 결과 판정 변경과 subprocess 자손 정리를 **같은 배포에 묶지 않는다.**

### 7.8 증거 상태

출처 표기: `C` Claude 코드 확인 · `X` Codex 격리 재현(HTTP·브라우저·DB·프로세스 생성 대체, **실제 DB·쓰기차단 아님**) · `T` 기존 시험 · `—` 미확인.

**재현된 경계 (`X`, Codex 보고 기준)**
- investing · kb 흐름 — 정상 · 1차 실패 뒤 2차 성공 · 부분 통화 · 저장 함수 0 반환 · 전면 실패 · 세션 예외.
- investing 양쪽 403(HTTP 2회)과 쿨다운(HTTP 0회)이 둘 다 성공 집계.
- bs · citi · woori 의 공식 실패 뒤 신뢰창 밖 종료 — bs·citi 성공 집계, woori 실패 집계.
- shinhan · nh · sc 전면 실패 → 정상 반환 → runner exit 0 → 부모 성공 → **해당 격리 사례에서는 재등록 조건 미충족.**
- IBK 판정기 — 저장 반환 0이어도 DB 대조로 `OBSERVED` / `DEGRADED(WRITE_POLICY_BLOCKED)` / `FAILED(WRITE_POLICY_BLOCKED)` 로 갈림.
- woori · sc Selenium **현재 날짜** 경로 — 정상 3통화 / USD만 유효, 경로당 2건(총 4건). USD만 남으면 그 부분 결과를 저장 함수에 넘기고 정상 반환(가짜 브라우저·파서·저장 함수).

**기존 시험 (`T`)**
- IBK 쓰기차단 — `tests/test_ibk_result_builder.py` 의 `RealDbBuilderTest`(임시 SQLite + 실제 crud writer, `BLOCKED_UNINITIALIZED`). 이번에 읽었고 실행하지 않았다.

**남은 경계 (`—`)**
- **이번 조사에서 새로 수행한** 실제 쓰기차단 재현 0건(기존 시험 부재나 운영 검증 부재와 섞지 않는다).
- hana 흐름 격리 재현 없음.
- `soft_fail` · 신뢰창 밖 종료의 실제 운영 발화 미관측.
- shinhan · nh · sc 중간 `return` 의 성공↔포기 구분.
- woori · sc Selenium 경로의 **과거 날짜** 부분 통화 동작, 그리고 부분 통화의 실제 DB 반영·부모 집계.
- **운영 재등록 횟수 미측정** — 위 격리 사례 결과를 운영 측정으로 옮기지 않는다.

### 7.9 검증 계획

격리 시험 대상: 정상 · 값 불변 · 부분 실패 · 쿨다운 · **등록 누락** · DB 불일치 · 결과 유실 · 알림 발송 실패와 복구.

⛔ **"7일간 조용했다"만으로 알림을 승인하지 않는다.** 격리 시험에서 **울려야 할 때 울리고 조용해야 할 때 조용한지**를 함께 요구한다.

### 7.10 Investing 슬라이스 1 보고 전용 구현

- 보고 로그 보존·일별 집계: [INVESTING_OBSERVE.md](INVESTING_OBSERVE.md) — Git 밖 원문·실행 이력·커서 보존, v2 참조 기반 집계, 충돌·부분 관측 표시. 운영 실행·cron 설치는 별도 승인이다.
- 코드: `app/crawlers/investing.py`(계측 경계), `app/crawlers/investing_report.py`(보고 객체). 회귀 시험: `tests/test_investing_report.py` — HTTP·세션·DB 대역 사용.
- 기존 Investing 로거 → `logs/app.log`의 `message`에 JSON 이벤트: `investing_round_started`(세션 생성 전), `investing_fx_evidence`(FX writer 반환/예외 직후, DXY 저장/폴백 진입 전; 파싱 전패 시 미호출 증거), `investing_round_finished`(세션 생성~close의 최외곽 finally). 로깅 설정은 그대로다.
- `schema_version=3`, 판정 계약 `validity_contract=investing_range_checked/2`, `round_id`, `attempt_id`(URL 시도 1/2, 회차 이벤트는 null). JSON 공백을 제거하고 `format`으로 아래 세 형식을 구분한다. 이벤트 수·발행 위치는 유지한다. (운영 이미지는 2026-09-22 배포 `34ef52c` 부터 schema 3 을 낸다 — 배포 직후 10분 창 60회차 전부 schema 3, 직전 구 이미지 59회차는 전부 schema 2.) 이전 이미지의 이벤트는 `schema_version=2`·계약 필드 없음이며 집계기가 `investing_range_checked/1` 로 명시 매핑한다(D10).
  - `lifecycle`: 시작 이벤트는 식별자와 형식만. 의미는 **세션 생성 전·수집 미시도**이며 성공 증거가 아니다.
  - `compact`: **첫 시도에서 모든 통화 valid, 모든 통화 writer 제출·정수 반환 확인, 재시도·계측 오류 없음**일 때 FX 증거와 정상 종료를 각각 한 줄로 기록한다. 종료는 루틴 정상 반환·세션 closed까지 요구한다. `outcome=all_valid`, `fx_attempt_id=1`, `rates`(통화별 정규화 값), `writer_returned_count`, `writing=per_currency_write_unverified`를 보존한다. rates의 각 항목은 `valid/validated`이며 writer 제출 통화도 같은 키 집합이다. 0 반환도 압축 가능하지만 저장 성공·변경 불필요·정책 차단을 뜻하지 않는다. FX 이벤트 시점의 `execution=running`은 이후 DXY 성공을 보장하지 않는다.
  - `detail`: 부분/전면 누락, 쿨다운, 재시도(2차 성공 포함), 예외, 계측 실패는 상세 스냅샷. `attempts`에 시도별 원본 `collection`을 한 번만 담고, 회차 요약 `collection_attempts[pair]`는 선택된 원본의 attempt_id를 참조한다. 계측 오류가 없을 때만 `not_reached`/`unnecessary` 시도의 빈 collection·writer·execution을 생략하고 id/status/reason을 남긴다(생략된 collection은 `not_attempted/not_started`, writer는 미호출, execution은 `not_attempted/not_started`). 계측 오류가 있으면 상태 표식 자체가 유실됐을 수 있어 이 생략도 하지 않는다. `succeeded`는 **루틴 정상 반환**만 뜻하며 유효 관측·저장 성공으로 승격하지 않는다.
- `collection`은 통화별 `valid`(유효관측) / `missing`(누락) / `not_attempted`(미시도) / `unknown`(확인 불가) + 사유. 판정 순서: `selector_missing` → `empty_or_placeholder`(공백, `-`, `N/A`) → `parse_failed` → JPY ×100 후 `nan_value` → 같은 정규화 값에 `out_of_range`(±inf 포함, 경계 포함 허용). `constants.MIBANK_RATE_RANGES`를 이름 변경·복제 없이 재사용한다.
- **실패한 시도의 미관측(/2)**: 시도가 일반 예외(`Exception` — timeout·`concurrent.futures.CancelledError` 포함)로 끝나면 그 시도에서 `unknown/not_observed` 로 남은 통화만 `missing/attempt_failed`(+`error_type`)로 확정한다. 파싱 루프가 세 통화 모두에 관측을 한 번씩 남기고, 관측 계측 유실은 `unknown/telemetry_error` 로 따로 표시되기 때문이다. `BaseException`(`asyncio.CancelledError`·종료)으로 끝나면 `unknown/attempt_interrupted` — `safely_report` 는 `Exception` 만 격리하므로 관측 훅 안의 중단은 표식 없이 빠져나올 수 있다. 403 은 기존대로 `missing/http_403`, 정상 반환은 전환하지 않는다. ⚠️ 잔여: `telemetry_failed` 자체가 실패하면(첫 `append` 의 메모리 할당 실패 등) 유실된 관측이 `not_observed` 로 남아 누락으로 잘못 확정될 수 있다.
- 회차 수집 요약은 **유효 관측 → 확인 불가 → 누락 판정 → 미시도** 순(은행 보고와 같은 `SUMMARY_ORDER`)으로 고르고, 같은 상태 중 마지막 시도를 가리킨다. 다른 시도의 유효 관측 확보가 미확정이면 회차를 누락으로 확정하지 않는다(D9 해소 — `/1` 은 누락 → 확인 불가 순이었다). 재시도 timeout이나 DXY 실패가 앞 시도 증거를 지우지 않는다. 값은 쓰기 지시가 아니며 통화별 `attempt_id`로 원본을 추적한다.
- `writer`는 시도별 호출 여부·입력 통화·정수 반환값·예외 타입만 기록한다. 상세 형식의 통화별 `writing`은 호출 증거가 있으면 `unknown`, 미전달이 확인되면 `not_attempted`; 압축 형식의 공통 `writing`도 모든 제출 통화가 `unknown/per_currency_write_unverified`라는 뜻이다. 시작 이외 이벤트의 `final_db=not_checked`는 모든 통화가 **`unknown/not_checked`**임을 나타내는 공통 플래그이며 DB 대조는 하지 않는다. 정수 0·`db.add()`·commit 전 로그로 변경 불필요/정책 차단/저장 완료를 추정하지 않는다.
- `execution`: `normal` / `timeout` / `cancelled` / `abnormal`. 마지막 시도의 timeout은 기존 코드가 삼켜도 timeout으로 보고하며, 호출자 전파 여부는 별도 `exception_propagated`에 기록한다. 세션 생성·close 예외는 기록 후 기존처럼 전파한다. `session`은 마지막 도달 단계(creating/open/closing/closed)다.
- 계측 계산·직렬화·로그의 일반 예외는 격리한다. 남길 수 있는 보고에는 `telemetry_errors`를 붙인다. 시도 시작·종료 또는 관측 계측 유실 시 해당 범위의 미확정 수집 항목은 `unknown/telemetry_error`로 남기며, 이미 확보한 `valid`/`missing` 증거는 보존한다. 이후 확인된 403은 미확정 항목을 `missing/http_403`으로 갱신한다. writer 호출 계측 유실도 미시도로 단정하지 않는다. 계측 오류 회차는 압축하지 않는다. 로거/초기화 자체 실패 시 보고 유실 가능; 보고 실패를 이유로 재시도하지 않는다.
- 현재 `observation()`은 등록된 3통화와 파싱된 rate를 전제로 한다. 미등록 통화의 범위 직접 인덱싱은 `KeyError`, reason 없이 text만 있고 rate가 None이면 `TypeError`가 나며, `safely_report`가 `telemetry_errors=[..., "observation"]`를 남기고 해당 관측은 unknown으로 남을 수 있다. 현재 호출 경로에서는 미도달이다. 통화/호출자 확장 시 방어가 필요하며 이번 로그 용량 수정에서는 변경하지 않았다.
- **저장 입력·정책, DXY 예외 뒤 두 번째 URL 재크롤, scheduler의 `record_success`/`record_failure` 매핑은 그대로다.** 파싱 전패는 writer 미호출, 유효성 전패(NaN/범위 밖)는 writer 호출 가능. D7 및 NaN·범위 밖 저장 제외는 후속이다. 운영 배포·검증을 뜻하지 않는다.

#### 로그 용량 및 보존 창 (슬라이스 1 재검토)

**선택: 정상 압축 + 중복 제거. 시작·FX 증거·종료 이벤트는 생략하지 않는다.** FX 증거는 DXY 분기 전에 환율값과 writer 반환까지 독립적으로 남아 프로세스가 그 뒤 중단돼도 확인할 수 있다. 이상 회차의 상세 증거는 샘플링하지 않는다. 로테이션 용량은 변경하지 않으며, 아래의 잔여 보존 창 감소를 명시한다. **6시간 보존 보장은 아니다.**

측정 재현: `pytest -p no:asyncio tests/test_investing_report.py -k log_volume -q -s`.
HTTP·DB 대역으로 실제 crawler 경로를 실행하고, UTF-8 JSON message와 `CustomJsonFormatter()`를 적용한 **실제 파일 형식(외곽 JSON·문자열 이스케이프·개행 포함)**을 각각 센다. 환율 fixture는 USD 1350.0 / JPY 900.0 / EUR 1500.0, 타임스탬프는 소수점 6자리로 고정한다. 로거명·함수명·행 번호는 실제 레코드를 사용한다.

- 정상 3이벤트 message: **[160, 421, 452] = 1,033 B/회 = 7.03 MiB/일**.
- 동일 이벤트의 app.log 기록: **[358, 657, 690] = 1,705 B/회 = 11.61 MiB/일**.
- 수정 전 동일 fixture의 전체 스냅샷 3건은 message **5,987 B/회**, app.log **7,357 B/회 = 50.10 MiB/일**이었다. 검토 보고서의 5,989 B/회·약 41 MiB/일은 message 계층에 가까우며, 보존 창 계산에는 외곽까지 포함해야 한다. 동일 파일 형식 기준 **76.8% 감소**.
- 정상 fixture의 회귀 예산은 **2,000 B/회 이하**이며 3이벤트 존재도 함께 시험한다. 이 값은 런타임 절단/유실 제한이 아니고, 실제 숫자의 자릿수 등에 따라 크기는 달라진다.

하루 환산은 검토와 같은 활성일 가정: 19h/10초 = 6,840회 + 5h/60초 = 300회, 합계 **7,140회/일**. 주말 비활성·추가 대기·실제 장애 비율은 이 가정에 포함하지 않는다. `app/config.py`의 로테이션은 10×1024² B × (활성+백업3) = **40 MiB**다. 검토자가 제공한 기존 약 6시간 보존으로부터 기존 유입을 **160 MiB/일**로 역산하면:

`추가 MiB/일 = 회차 바이트 × 7140 / 1024²`
`예상 보존 시간 = 24 × 40 / (160 + 추가 MiB/일)`

| 회차 시나리오 (하루 전체가 해당 사례라는 가정) | 이벤트 수 | app.log B/회 | 추가 MiB/일 | 예상 보존 시간 |
|---|---:|---:|---:|---:|
| 수정 전 정상 전체 스냅샷 | 3 | 7,357 | 50.10 | 4.57h |
| 수정 후 정상 / writer 0 반환 | 3 | 1,705 | 11.61 | **5.59h** |
| USD만 수집 (JPY/EUR selector 누락) | 3 | 3,297 | 22.45 | 5.26h |
| 쿨다운 | 2 | 2,195 | 14.95 | 5.49h |
| 1차 timeout → 2차 정상 | 3 | 4,188 | 28.52 | 5.09h |
| 두 시도 timeout | 2 | 2,245 | 15.29 | 5.48h |
| 1차 FX 확보·DXY 예외 → 2차 timeout | 3 | 3,004 | 20.45 | 5.32h |
| 양쪽 URL 파싱 전패 | 4 | 5,489 | 37.38 | 4.86h |
| 양쪽 FX writer 예외 | 4 | 6,014 | 40.95 | 4.78h |
| 1차 DXY 예외 → 2차 정상 | 4 | 5,055 | 34.42 | 4.94h |

정상 위주라면 기존 6h 대비 약 **24분(6.8%) 감소**를 예상한다. 혼합 회차는 바이트를 비율로 가중한다. 예를 들어 정상 99% + 양쪽 writer 예외 1%는 **11.90 MiB/일 → 5.58h**, 90%+10%는 **14.54 MiB/일 → 5.50h**다. 표는 이 보고 계층의 추가량만 측정한 것으로 **운영 보존 실측이나 모든 장애의 상한이 아니다**. 장애 시 기존 traceback·경고 로그 유입도 변하므로 실제 보존은 더 짧아질 수 있다. 배포 후 실제 회차 분포·전체 로그 유입·로테이션 시각을 확인해야 한다.

### 7.11 운영 배포 (2026-09-18)

`721a323` 를 DEPLOYMENT.md §8.1a(구 이미지 `FROM` + `app`·`scripts`·`static`·`templates` 만 교체)로
배포했다. 런타임 델타는 4파일 — `investing.py`(수정) · `investing_report.py` · 관측 스크립트 2개.

| 항목 | 값 |
| --- | --- |
| 배포 코드 | `721a3237900961cf59e9a50a98ce20357bc56755` |
| 후보 이미지 = `latest` | `sha256:eec4a8025dcb99531598462d6e8cbcf19a70bdb7e99ccff87f8cd90e637b9b03` |
| 직전 이미지(롤백 앵커) | `exchange-rate-fastapi:rollback-1789661986306132298` (= `sha256:f6738338e173…a3b`) |
| switch | 2026-09-17T16:57:48Z — 새 컨테이너 `23d43b27e310…` |
| promote | 2026-09-17T17:20:37Z — `latest` 이동, checkout `721a323` |

**switch 검증**: `image_matches` · `running` · `tree_matches` 모두 참, `IBK_RESULT_PATH_ENABLED` ·
`TELEGRAM_ENABLED` 둘 다 `true` 유지. 내부·nginx 경유·공개 `/health` 200.

**계측 동작 확인**: 배포 전 추출은 `written=0`(구 이미지에 보고기 없음 = 기준선), 배포 후
`written=12`. 집계기 첫 운영 실행에서 4회차 모두 `start`/`fx_evidence`/`finished` 3종이
이어졌고(`start_only`·`finish_only`·`fx_only` 각 0), 통화 `valid 12`, `conflict_keys 0`.
promote 직전 10분 표본은 종료 60건 전부 `status=normal`·`outcome=all_valid`, 계측 오류 0.

⛔ **여기까지가 "보고가 발생하고, 수동 도구로 집계할 수 있다"이다.** 보존·일별 집계 도구는
구현돼 위 표본을 냈지만, **지속적인 자동 판정·집계·알림은 미구현**이고 정기 점검의 담당·주기도
미확정이다. `final_db` 는 설계대로 항상 `not_checked` 이므로 이 배포는 **저장 성공률을
판정하지 않는다**(§7.10 계약, 위 §3 "저장 0 은 세 사실을 뭉갠다" 참조).

#### Docker 로그 보존 창 — 실측

§7.10 의 로그 용량 표는 **`app.log`**(RotatingFileHandler, 40 MiB 예산) 계층이다. 관측 추출기가 읽는
**Docker json-file 은 별개 계층**이므로 두 수치를 섞어 쓰면 안 된다. 2026-09-17 실측:

| 항목 | 실측 |
| --- | --- |
| 요청 범위 | 24.0h (`--since`) |
| **실제 반환 범위** | **15.6h** (`coverage_since` 2026-09-17T00:55:49Z ~ `until` 16:29:38Z) |
| 보존 바이트 | `stdout.raw` 97 MiB / `stderr.raw` 43 KiB |
| 유입률 | 5.58 MiB/h (`docker logs --since 60m` 실측, 슬라이스 1 적용 전) |
| 측정 컨테이너 | `169480ffa1df…` / run_id `02f73a0259c64e809de58d4ade98af93` |

⚠️ **`--since 24h` 는 Docker 가 그만큼 보유한다는 뜻이 아니다.** 이 15.6h 는 그 시점 한 번의
관측 범위이며 **고정된 최소 보존 시간도, 그 구간의 무유실 보장도 아니다.**
⇒ **15.6h < 24h 이므로 "창 길이로 하루를 복원한다"는 성립하지 않는다.** 하루 분포 복원을
가능하게 하는 것은 창 길이가 아니라 **주기 보존의 연속성**이다. 이 표본은 1시간 주기보다
길지만, 슬라이스 1 이 더하는 유입 이후의 보존 창과 실제 수집 공백은 **추가 확인이 필요하다**.

---

### 7.12 bs·citi 보고 R1a~R1c — 공식 경로·MIBANK·writer 관측 (2026-09-18 구현, 2026-09-21 배포 `f94d04a`)

⛔ 보고 전용. 반환값·폴백 순서·예외 전파(총실패를 로그만 남기고 삼키는 기존 동작 포함, §2.1)·writer 입력·
재시도·crawler_stats 판단은 바꾸지 않는다. 캡처·집계·경보는 범위 밖이다.

- 코드: `app/crawlers/bank_report.py`(보고 객체·`PathObserver`·`record_writer_call`), `bs.py`·`citi.py`(계측 경계).
  회귀 시험: `tests/test_bank_report.py` — HTTP·세션·DB 대역, 합성 HTML(실제 페이지 의미를 주장하지 않는다).
- 이벤트(각 은행 로거 → `message` 에 JSON): `bank_round_started`(`format=lifecycle`), `bank_round_finished`
  (`format=detail`). `schema_version=1`, **판정 계약 `validity_contract=bank_v2_evidence/1`**, `source`, `round_id`.
  ⚠️ `finished` 전에 프로세스가 끝나거나 로그가 실패하면 그 회차의 중간 증거는 남지 않고, `started` 만으로
  진행 상태를 복원할 수 없다.
- **판정은 §7.2 값 그대로**(`valid`/`missing`/`unknown`/`not_attempted`) — 별도 `invalid` 상태는 없다.
  - `valid` = 통화·필드·단위 근거를 **그 회차 응답에서** 확인(판정 계약). R1a 는 근거 **후보 텍스트만** 남기고
    (`label_candidates`: 표 칸이면 행 텍스트·칸 순번·`thead` 헤더 텍스트·헤더 `colspan`/`rowspan` 유무, citi 1차면
    항목·값 묶음 텍스트; 조각 160자·640바이트, 잘림 표시·원래 길이) 판정은 **`unknown/v2_evidence_unconfirmed`**
    — 라벨 표기가 실응답 fixture 로 확인되기 전이다. 파싱 성공으로 `valid` 를 만들지 않는다.
  - 확인된 위반 → `missing/validation_rejected`: 비유한 값(`non_finite` — writer 로는 기존대로 간다), 귀속 충돌
    (`attribution_conflict` — citi 1차의 한 항목이 여러 통화로 귀속되거나, 한 통화가 서로 다른 원천(citi 1차
    항목·MIBANK 행) 둘 이상에서 나타나거나(값을 못 읽은 원천도 원천이다 — 어느 쪽이 그 통화인지 가를 수 없다),
    같은 통화가 두 번 이상 관측·덮어써진 경우. MIBANK는 `currency=` 링크를 우선 사용하고 코드를 얻지 못하면 국기
    파일명으로 폴백하여 행당 최대 한 통화만 판별하므로, 한 행의 복수 통화 귀속은 감지하지 않는다. 값이 같다는 것은
    근거가 아니다). 값 미확보 확인 → `missing/no_value`
    (`selector_miss`·`parse_error`·`empty_value`·`not_matched`·`path_failed`). 계측 유실·관측 상한 초과 →
    `unknown/evidence_incomplete` — 확인된 위반은 지우지 않지만 miss 로 "미확보" 를 단정하지도 않는다.
  - 후보 판정 우선순위: 위반 > 계측 불완전 > 관측(근거 미확인) > miss > 경로 결과.
- **회차 요약은 `valid → unknown → missing → not_attempted`** 로 시도 간 증거를 고르고(같은 상태면 가장 최근 시도),
  선택한 `path`·`attempt_id` 를 남긴다. 다른 시도의 유효 관측 확보 여부가 미확정이면 회차 전체를 `누락`("필요한
  유효 관측을 확보하지 못함")으로 확정할 수 없다 — 예: 공식 경로 미확보 + MIBANK 값 확보(근거 미확인)는 `unknown`.
  Investing 도 같은 순서로 맞췄다(§7.10 `/2`, D9 해소 — 순서 상수는 `investing_report.SUMMARY_ORDER` 하나다).
  ⚠️ 알려진 한계: 관측 훅은 모두 `safely_report`(일반 `Exception` 만 격리)를 거치므로, 훅 안에서 `BaseException`
  이 나면 `telemetry_incomplete` 표식 없이 경로가 끝나고 `_judge` 가 그 통화를 `missing/no_value(path_failed)` 로
  확정한다(예외를 주입하면 재현된다). 현재 운영 경로(스케줄러 스레드 풀의 동기 wrapper)에서는 그런 중단의 발생원을
  확인하지 못했다. Investing `/2` 처럼 `Exception` 으로 끝난 경로만 확정하는 조건은 다음 은행 계약 판올림 때 맞춘다.
- 폴백별 상태: `succeeded`(루틴 정상 반환 — 유효 관측·저장 성공 아님) / `failed` / `policy_skipped`
  (`mibank_untrusted_window`) / `unnecessary`(앞 경로 성공) / `not_reached`. 실행되지 않은 경로는 품질 등급이 아니라
  `not_attempted` 라는 별도 사실이다. 취소 등 `BaseException` 은 기존대로 폴백 없이 전파한다(`except Exception` 을
  넓히지 않는다) — 각 경로 `try` 의 형제 절 `except BaseException` 이 **그 경로에** 종료 원인을 기록하고 그대로
  재전파한다. 회차 종료 시점까지 `attempted` 로 남은 시도(종료 기록 유실)는 회차 예외를 추정해 붙이지 않고
  `unknown/attempt_end_unrecorded` 로 둔다. 취소 뒤 `db.close()` 도 실패하면 경로에는 취소가, 회차에는 close 예외가 남는다.
- **MIBANK 관측(R1b)**: 공유 함수 `utils.crawl_mibank_rates(observer=None)` — 9개 은행이 쓰므로 은행 이름으로
  활성화를 판단하지 않고, 관측자를 넘긴 bs·citi 만 기록한다. 판별은 한 곳(`_mibank_*_with_basis`)에서 한 번 하고
  기존 함수는 값만 돌려준다. 행마다 값이 `current_rates` 에 들어간 직후 `observed`(행 번호 `item_key`·코드·코드 근거
  `explicit_code_param`/`flag_filename`·값 분기 근거), 빈 값은 `empty_value`, 파싱 실패는 그 행에 `parse_error` 를
  결속한 뒤 기존대로 재전파한다. 값 분기 근거: `header_index`(헤더 인덱스 칸 — 비었거나 `-` 면 폴백하지 않는다) /
  `fallback`(`row_cells_insufficient` 또는 `column_index_unresolved`, 처음 매칭된 selector·매칭 수 — 값은 마지막 요소).
  표 구조(`structure`: 열 인덱스, `column_basis` = `label_found`/`label_not_found`/`header_row_absent`, 헤더 텍스트·
  칸 수·span)와 행 사실(행 텍스트·`td` 수·span)은 **구조 사실일 뿐** — 헤더 인덱스는 colspan 을 고려하지 않으므로
  기준환율 열 대응의 증거가 아니고, 코드 근거는 통화 축만 채운다 → MIBANK 관측도 `unknown/v2_evidence_unconfirmed`.
  필수 밖 코드는 값을 읽지 않고(기존 동작) 식별만(`outside_required_codes`: 코드 64개·16자 상한, 근거별 개수).
  require_all 실패 시 앞 관측·덮어쓰기 이력은 남는다.
- **호출 지점 기록(R1b)**: 운영 범위 검사 `ops_range_check`(`record_range_check` — 반환/예외와 **입력** 통화만.
  `validate_rate_ranges` 는 첫 초과에서 멈추므로 예외 시 실제 비교 집합은 모르고, 예외 메시지를 파싱하지 않는다.
  정상 반환도 NaN 통과를 증명하지 않는다 → `non_finite_input_pairs`), 편차 `deviation`(soft/hard 와 통화별 비교 값,
  직전 값·시각이 없으면 `compared=false`/`prior_missing` — 통과가 아니다), 채택 `adoption`(`withheld`/
  `deviation_hard_fail` 또는 `submitted`/`deviation_not_hard_fail`). 편차·범위는 판정 계약(V1~V4) 밖의 운영 사실이라
  수집 판정을 바꾸지 않는다. hard_fail 보류는 요약 `writing` 에 `not_attempted/withheld_before_writer`.
- **상한**: 통화별 관측·miss 각 8건(`MAX_OBSERVATIONS_PER_PAIR`·`MAX_MISSES_PER_PAIR`), 넘으면 버리고 `truncated`·
  시도별 `*_truncated`·`dropped` 개수를 남긴다. 계측 실패는 `telemetry_errors`(메서드 이름, 처음 본 순서·중복 없음)와
  `telemetry_error_counts`(횟수) — 행마다 실패해도 목록이 응답 크기로 자라지 않는다.
- **writer(R1c)**: 호출 지점에서 `writer_calls[]`(호출 id·경로·입력 통화·`termination` returned/raised·반환값·예외
  타입)에 더해, `crud.insert_bank_rates_into_db(observer=None)` 가 **실제 분기에서** 가드 결정(`legacy`/`atomic`/
  `blocked` + `write_mode_uninitialized`·`write_mode_<모드>`), 통화별 staging 결정(`_stage_bank_rate_changes` 루프 안:
  `none_skipped`/`staged`(db.add 직후)/`unchanged`)과 완료 여부, `db.commit()` 직전(`attempted`)·정상 반환 직후
  (`committed` — `Session.commit()` 은 None 을 돌려주므로 예외 없는 반환만 근거)를 기록한다. 다른 은행은 관측자를 넘기지
  않아 기록이 없고, crud 는 `bank_report` 를 import 하지 않는다. 시작 기록이 없는 writer 단계 기록은 다른 호출에 붙이지
  않는다(진행 중 호출 id 로만 결속).
  통화별 쓰기 축(§7.2): `policy_blocked`(정책 차단) / `not_attempted`·`value_none` / `no_change_needed`(변경 불필요 — 이
  통화의 비교 결과라 다른 통화의 commit 과 무관) / `performed`(수행 — staged ∧ commit 정상 반환) / `unknown`
  (`commit_outcome_unknown` commit 중 예외, `commit_not_reached` commit 전 예외, `staging_incomplete`, `guard_unrecorded`,
  `telemetry_error` — writer 단계 기록 유실은 "미도달" 로 단정하지 않는다). ⛔ `failed`(실패)는 R1c 에서 쓰지 않는다 —
  bs·citi 는 한 회차의 경로들이 같은 세션(`autoflush=False`, 경로 실패 뒤 rollback 없음)을 써서 commit 에 이르지 못한
  행도 뒤 경로의 commit 에 섞여 반영될 수 있으므로 호출 단위로 실패를 확정할 수 없다(공통 계약에서 `실패` 를 없앤다는
  뜻은 아니다). 회차 요약은 어느 호출이든 `performed` → 어느 호출이든 `unknown`(앞 호출 pending 행이 뒤 commit 에
  섞였을 수 있어 뒤 호출의 `no_change_needed`·차단이 그것을 부정하지 못한다) → 그 통화를 입력으로 가진 마지막 호출,
  호출별 결과는 `writer_calls[].pair_results` 에 모두 남는다. 시작 기록을 잃은 writer 호출이 있으면 그 호출의 입력을
  모르므로, 확정된 `performed` 가 없는 통화는 모두 `unknown/telemetry_error`(`lost_writer_calls` 개수)다. NaN 은 staging
  비교(`!=`)가 늘 참이라 매 회차 `staged` 가 되고, commit 정상 반환이 확인되면 `performed` 다(기존 동작, 수정 대상 아님).
  `final_db=not_checked`(쓰기 ⊥ 최종 DB 확인).
- 계측은 Investing 과 같은 `safely_report` 경계 안에서만 돈다. 핵심 추출 사실을 라벨 후보보다 먼저 저장해, 라벨
  수집 실패(`label_candidates_error`)가 관측을 지우지 않는다. 보고 초기화 실패 시 보고 없이 기존 동작 그대로.
- 로그량(합성 측정, `bank_round_finished` 한 건): 2.4~6.6 KB(라벨 조각 상한까지 채운 경우 최대). bs 는 IN·BREAK1·
  BREAK2·OUT 모두 매분, citi 는 OUT 제외 매분 → 하루 bs ≈ 5~9.5 MB, citi(평일) ≈ 6~8.5 MB. §7.11 실측 Docker 로그
  증가율(약 5.58 MiB/h)에 **10% 안팎**을 더한다 — Docker 로그 보존 창은 그만큼 **줄고**, 원문을 보존하는 Investing
  관측 아카이브 사용량은 **늘어난다**. 배포 전 대표 이벤트를 실응답으로 다시 재고 압축 형식 여부를 정한다(당시 계획 — 배포 뒤 경과는
  아래 '다음 단계' 항목 끝).
  R1b 합성 측정(공식 실패 + MIBANK 경로 회차 한 건): 필수 3 + 필수 밖 41개 표 5.1 KB. 같은 USD 빈 행을 20·200·1,000개
  넣어도 7.7 KB 로 같다(상한 전 R1b 초안은 1,000개에서 약 359 KB — Codex 측정). MIBANK 는 공식 경로 실패 뒤에만 돈다.
  R1c 합성 측정(실제 writer·SQLite, 회차 한 건): 공식 경로 성공 4.4~4.5 KB, 공식 실패 + MIBANK(43행 표) 6.0 KB.
- 다음 단계(당시 계획): R1a~R1c 를 한 번에 배포한다(배포 전 실응답으로 이벤트 크기를 다시 재고 압축 형식을 정한다). fixture
  캡처는 별도 슬라이스(비식별 규칙·상한이 열린 결정). → R1a~R1c 는 2026-09-21 `f94d04a` 로 배포됐다(커밋 `4fc081b`, DEPLOYMENT
  §8.1b). 그 뒤의 Docker 로그 보존 창 관측과 보관은 [LOG_RETENTION.md](LOG_RETENTION.md) 로 이어졌다.
- 운영 관측(2026-09-22): bs MIBANK 자연 폴백 2회차(01:07:43Z·01:08:43Z = 10:07·10:08 KST, round `f6decbcb…`·`cc021dd2…`,
  공식 경로 `ReadTimeout`)에서 R1b 관측·범위 검사·편차 평가(세 통화 0%)·채택(`submitted`)과 R1c atomic writer 결속·세 통화
  `no_change_needed/equal_to_last_record` 를 확인했다. 수집 판정은 `unknown/v2_evidence_unconfirmed`(수집 축 — R1c 의 "쓰기 결과
  불확실 → unknown" 분기는 이번에 실행되지 않았다)이고 `final_db=not_checked` 다. MIBANK 값이 직전 DB 값과 같았던 이유(집계
  지연)는 가설이다. MIBANK 변경값의 commit(같은 writer 호출에서 `staged → committed → performed`)과 citi MIBANK 운영 경로(공식
  두 경로 실패 뒤 진입)는 미관측으로 남기고 자연 표본을 추가 관측한다.

### 7.13 hana·woori 보고 — 프로세스를 넘는 증거 전달 계약 (2026-09-19 Claude·Codex 설계 합의, **구현 확정은 R1 운영 관측 뒤**)

⛔ 설계만이다(코드 없음). 보고 전용 — 반환값·폴백 순서·예외 전파·writer 입력·재시도·crawler_stats 는 바꾸지 않는다(§7.12 와 같은 경계).

- **사실**: 흐름은 공식 Request → Selenium subprocess(`python -m app.crawlers.runner <bank>_selenium`, `capture_output`, 45초) → MIBANK.
  부모는 **종료 코드만** 받는다. 자식은 자기 `SessionLocal()` 로 writer 를 부르고 commit 은 writer 반환·`driver.quit()` 전에 일어나므로
  timeout kill 뒤에도 저장됐을 수 있다. 부모는 `TimeoutExpired` 를 `RuntimeError` 로 바꿔 전파한다(그대로 분류하면 `abnormal`).
  woori 만 과거 날짜를 순회한다(`selected_date` 는 요청 의도일 뿐 응답 기준일의 증거가 아니다).
- **판정은 부모에서만**: 자식은 같은 `bank_report` 관측자로 원자료만 모으고 `finish()`·`emit()`(안에서 판정이 돈다)을 부르지 않으며
  `bank_round_*` 도 발행하지 않는다. 자식 stderr 본문·자식 로그를 재파싱해 결과로 승격하지 않는다. Selenium 요소는 연결부에서 추출
  텍스트와 제한된 구조 사실만 만든다(BeautifulSoup API 를 가정하지 않는다). 자식 writer ID·관측 sequence 는 자식 토큰 접두,
  세션 태그는 `child:<토큰>`.
- **채널**: 부모가 **Selenium 실행마다** 새 0700 임시 디렉터리와 1회용 토큰을 만들어 argv 로 넘긴다. 자식은 `O_EXCL` 임시 파일 → close
  → `os.replace(frame.json)` 를 한 번만 한다(fsync 없음 — 이 계약은 충돌 후 프레임 복구를 요구하지 않고, fsync 는 45초 예산에 대기만
  더한다). 부모는 자식 종료(또는 timeout kill·wait) 뒤 `O_NOFOLLOW|O_NONBLOCK` 로 한 번 열어 일반 파일인지 확인하고, 같은 fd 에서 EOF
  또는 누적 65,537 B 까지 반복해 읽는다 — **수락 상한은 65,536 B**, 65,537 번째 바이트가 읽히면 `too_large`. 그 뒤 엄격 JSON·토큰·은행·
  경로·schema·타입·참조를 검증한다. `O_NONBLOCK` 은 FIFO 열기 대기를 피할 뿐 **일반 파일 읽기의 시간 상한은 보장하지 않는다**.
  프레임이 수락 상한을 넘으면 자식은 원자료 대신 **전송 잘림** 표지만 있는 최소 프레임을 보내고 부모는 `unknown` 으로 본다 — 이 전송
  잘림은 기존 관측·miss **목록 상한**(`observations_truncated` 등)과 다르다: 목록 상한은 확인된 위반을 보존한 채 나머지만 unknown 으로
  두는 기존 규칙 그대로다. 신뢰 전제: 원자적 게시 순간의 완전성뿐이다 —
  같은 UID 의 악의적 변조는 범위 밖이고 토큰은 인증 수단이 아니다. IBK `result_fd` 분기와 분리하고 동시 지정은 거부한다.
- **격리**: 보고 실패(자식 직렬화·쓰기, 부모 생성·읽기·검증·삭제)는 크롤러 반환·종료 코드·폴백을 바꾸지 않는다(IBK emitter 의 쓰기
  오류 `return 1` 을 복사하지 않는다). 채널 생성 실패 → 오늘과 똑같이 실행(`unknown/child_report_channel_unavailable`),
  삭제 실패 → `cleanup_failed` 기록만 한다.
- **판정 표**: 완결 프레임(존재·크기·검증 통과·전송 잘림 없음)의 수집·쓰기 사실은 exit≠0·timeout 이어도 보존하고 실행 결과는 따로
  적는다(timeout 은 catch 지점에서 결속). 프레임 없음·무효 → 수집 `unknown/child_report_*` + 필수 통화마다 쓰기 요약 후보
  `unknown/child_report_*` — writer 호출은 지어내지 않고, 후보는 `performed → unknown → 마지막 호출` 에 참여한다(다른 호출의 확인된
  `performed` 가 선택되어도 **프레임 유실 사유와 다른 호출의 불확실성을 함께 남기고**, `no_change_needed`·차단만으로 유실을
  지우지 않는다). pending 행이 뒤 commit 에 섞인다는 전제는 같은 세션 안에서만.
- **woori**: 조회마다 `query_id`(요청 날짜·응답 날짜 표기·관측·miss·루프 완료)를 두고 충돌은 조회 안에서만 본다. writer 는 채택한
  조회를 참조하고, writer 미호출이어도 각 조회의 miss·확인된 위반은 남긴다. 과거 날짜 값은 `unknown/date_basis_unconfirmed` 이되
  확인된 위반(`missing/validation_rejected`)이 앞선다.
- **한계**: commit 뒤 `driver.quit()` 멈춤 → 프레임 없음 → `unknown`. **자식의** 보고 I/O(직렬화·쓰기·이름 바꾸기)가 45초 예산 안이라
  timeout·MIBANK 폴백이 새로 생길 수 있다(밀리초 수준은 추정). 부모의 생성·읽기·삭제는 예산 밖의 별도 지연이다 — 자식 쓰기·rename,
  부모 I/O, 프레임 없는 timeout 표본까지 잰 뒤 시간 경계를 정한다. 자손(Chrome) 정리는
  판정하지 않는다(§7.7).
- **R1 관측 뒤 확정**: 실측 크기·로그 증가율·상한·압축, 실제 예외 분포, `bank_report` 공통화 범위.


### 7.14 bs·citi fixture 캡처(C) — 설계 합의 (2026-09-19 Claude·Codex) + C1a 추출 경계

⛔ fixture 는 **계약 수준 근거**(라벨·헤더→행 열 대응·단위 표기가 어디 있는가)다. 회차 판정은 여전히 그 회차 응답에서 근거를 다시 읽는다.
캡처는 보고 계측과 별도이며, `_judge` 가 `valid` 를 내게 하는 연결(라벨 목록·열 대응·단위 근거·공식 경로 V3 정책)은 C1 **다음** 슬라이스다.

- **두 단계**: C1 = 개발 머신 수동 계약 캡처 — 5경로(bs 공식·citi 1차·2차·bs MIBANK·citi MIBANK) 각 **요청 1회**(`allow_redirects=False`,
  3xx 도 1회 소비, 재시도 없음), 운영과 같은 `requests`·`HEADERS`·`response.text`·파서, 메타 `origin=dev_machine`·`capture_id`(운영 회차가 없어
  `round_id` 비해당). 개발 머신 응답의 운영 대표성은 주장하지 않는다. C2 = 운영 자동 캡처는 C1 뒤 필요성 재판단(현재 성공 관측도
  `unknown/v2_evidence_unconfirmed` 라 unknown 비율만으로 의미 변화 감지를 주장할 수 없고, 보존 기한 정리 주체도 그때 정한다).
- **합격 기준 = 왕복 일치**: 원본 추출 결과 == 비식별 HTML 을 직렬화·같은 파서로 재파싱해 추출한 결과(값·분기·**요소 대응**·순서·예외).
  이것은 추출 재현성 기준이고, 비식별 검사와 사람 검토는 **독립 필수 조건**이다. fixture 는 사용자 검토 + 두 에이전트 exact 승인 커밋으로만 들어온다.
- **비식별(값 수준 허용 목록)**: `id`·`class` 는 코드 선택자 등록부에서 도출한 값만(class 는 **토큰 단위 부분 보존**), `href` 는
  `?currency=<원본 값>` 로 재구성(빈 값은 빈 채 보존 → 3자 영문이면 보존 → 그 밖 제거, 중복 파라미터의 개수·순서 보존 — 제거하면 다음 링크가
  선택될 수 있어 왕복 일치에서 거부된다), `src` 는 `flag_<원본 세 글자><원본 구분자>`(파서 `flag_([a-z]{3})(?:_|\.)` 대소문자 무시),
  `colspan`·`rowspan` 이 1~20 밖이면 **fixture 거부**, 그 밖 속성 제거. 보존 경계(선택자 조상 사슬·`nth-child` 형제·MIBANK `thead`·`table`)
  안은 요소 골격을 보존하되 `script`·`style`·`noscript`·`template` **본문은 비우고 주석은 모두 제거**, 경계 밖 `meta`·`link`·`input` 은 지운다.
  비운 결과 추출·근거가 달라지면 거부.
  - **경계 밖의 빈 미지원 요소**(2026-09-21 추가): D1 분류의 U(I∪B 의 여집합, 예 `bsib:mnu` 같은 이름공간 커스텀 태그)이면서
    **속성 0 · 자식 0** 인 것도 지운다. ⛔ 판정은 **정제 전 상태**로 한다 — 정제가 속성·주석·`meta`/`link`/`input` 자식을 없애므로
    나중에 물으면 그것들을 지녔던 요소까지 같이 지워진다(6가지 모양 중 4가지가 정제 후에만 비었다, 실측).
    근거는 "의미가 없다" 가 아니라 **제거 후 왕복 일치가 유지된다** 이다 — 빈 요소도 `nth-child` 에 영향을 준다.
    `ruby`·`rb`·`rt`·`rtc`·`rp` 는 **제외**한다(D1 §2.3 이 빈 ruby 까지 거부하므로, 지우면 거부가 통과로 바뀐다).
    자식을 잃어 비게 된 부모는 연쇄 삭제하지 않고, 저장 후보에 남은 U 는 계속 거부한다.
- **등록부 검사**: 선택자는 변수로도 넘어가므로(`select_one(selector)`) 캡처 도구가 추출 중 **실제로 호출된** `select`/`select_one`/`find_*`
  의 메서드·인자(`recursive=False` 포함)를 기록하고 하나라도 등록부 밖이면 거부한다. 라벨 수집 조회도 포함하며, 라벨 수집이 예외를 잡아도
  위반은 도구가 따로 기록해 거부한다. 등록부가 지원하는 선택자 문법(태그·`#id`·`.class`·자식 `>`·자손 결합자·`:nth-child(n)`·
  `[attr*="..."]`) 밖을 만나면 캡처를 중단한다.
- **식별자 탐지기**: 페이지·응답 유래 문자열(fixture 텍스트·보존 속성·`recorded_extraction`·응답 메타)에 적용하고 걸리면 중단. 코드가 만든
  필드(URL 상수·해시·`capture_id`·enum)는 필드별 형식·출처 검증. 정규식 미검출은 식별자 부재의 증명이 아니다 — 사람 검토 뒤에도 한계로 남는다.
  의심 원문은 로그·오류에 남기지 않는다.
  - **하이픈으로 끊긴 식별자 두 모양**(2026-09-21 추가): 기존 `long_number` 는 **끊기지 않은** 숫자 10자리 이상만 보므로
    주민등록번호형·카드형이 통과했다(실측: `901231-1234567`·`1234-5678-9012-3456` 둘 다 통과). `rrn_shape`(`6-7` 자리)과
    `card_shape`(`4-4-4-4`)을 더해 **정확히 그 두 모양만** 막는다. ⛔ 일반 식별자 탐지가 아니다 — `010-1234-5678`,
    공백으로 끊긴 카드형, 비ASCII 하이픈, **인라인 태그로 갈린 모양**(주사는 텍스트 노드 단위다)은 여전히 통과한다.
    자릿수 임계는 쓸 수 없다: 실재하는 법인 국제표기 번호 `82-2-1588-6200` 이 연결 11자리라 함께 걸린다.
    주민등록번호 7번째 자리는 좁히지 않는다 — 9·0(1900년 이전 출생)이 있고, `[1-8]` 로 좁혀도 금액 범위
    `100000-1000000` 오탐을 **막지 못한다**(실측). 이 규칙들은 `search` 라 모양을 **포함한** 더 긴 문자열도 거부한다.
- **D1 직함 문맥 탐지**(2026-09-21 추가, 5a 부터 **캡처 도구가 부른다**, 5b 에 반입 판정 라이브러리, 5c-1 에 반입 시험 관문, 5c-2 에 MIBANK 두 저장본 반입 — **부산 공식·씨티 두 곳은 5c-3**): 명세는 리포 밖
  `design/c1b2/d1_detection_policy_v1.md`(sha256 `47d090a6…`, 슬라이스 4B 부터 같은 바이트의 리포 사본
  `tools/fixture_capture/d1_spec/d1_detection_policy_v1.txt`). `tools/fixture_capture/d1_observe.py`(§3~§5)가 블록 소유
  run·닫힌 직함 목록(`대표자`·`대표이사`·`은행장`·`ceo` 등 12개)으로 node/run 관측을 만들고, 읽을 수 없는 구조(미지원 요소·
  정규화 경계·비일반 텍스트)는 **0건이 아니라 거부**한다. `d1_findings.py`(§6)가 HTML 텍스트·속성·metadata 세 출처를 합쳐
  정확 중복 병합 → 최장 선택(한 run·한 scalar 안에서만, 겹침은 전이하지 않음) → 정렬 → 전역 순번으로 **승인 기록과 대조할
  닫힌 목록**을 낸다. 발견 기록에 원문은 없다 — 토큰은 정책 상수이고, 위치는 구조 좌표(HTML 은 숫자 경로·속성 번호,
  metadata 는 닫힌 schema 가 정한 키 경로)다.
  ⚠️ **명세 §6.1 개정**: 속성은 이름이 아니라 그 요소 속성 이름들의 code point 정렬 **번호(`attribute_index`)** 로 가리킨다 —
  이름은 페이지 텍스트라 성명이 들어올 수 있다(`data-대표자홍길동` 이 그대로 파싱됨, 실측). 슬라이스 1 에서 태그 이름을 위치로 썼다가
  `<x-person-홍길동>` 으로 낸 누출과 같은 부류다.
  ⛔ 직함 관측기이지 성명 탐지기가 아니다 — `<p>홍길동</p>` 은 0건이다. 캡처·반입(A층) 연결과 "네 관문 모두 정상 완료 + 빈 결과"
  반입 조건은 배선 슬라이스에서 한다. 사람 검토를 대체하지 않는다.

  **슬라이스 4A(라이브러리)**: `d1_replace.py`는 별도 공시 등록부의 citi 1차·2차 구조 선택자로 유일한 단일 텍스트 `li`를 확인하고,
  정확한 라벨·정책 공백·단일 값 경계에서 성명을 `[성명삭제]`로 치환한다. 원문·N 정규화 각각의 추출 기록 잔존 검사는 기록을 고치지 않고
  거부하며, 실행 중에는 새 노드 identity·보존 라벨 범위로 D1 발견을 대응시킨다. `verify_stored`는 실제 UTF-8 바이트를 재파싱해 경로·
  조상 태그·전체 문자열·단일 노드를 재검증하고 metadata를 포함한 D1 목록 전체를 다시 산출한다. 캡처·반입 연결은 슬라이스 5에서 한다.

  **슬라이스 4B(라이브러리)**: 명세 v1 을 바이트 그대로 리포에 넣고 개정 1(`d1_spec/d1_detection_policy_v1_amendment1.txt`)을 더했다 —
  A1 속성 번호(슬라이스 3 정정의 명세화), A2 "환경 불일치" 를 필수 조건 불충족(검사 불가)과 검토 환경과의 차이(검사 불가 아님)로 구분,
  A3 digest 분리. `d1_digest.py` 의 **정책 digest** 는 명세·개정·D1 구현 파일(진입 모듈과 패키지 `__init__` 에서 따라간 import 폐포와
  자신)의 실제 바이트 해시를 결정적 JSON 으로 묶은 값이고, **검토 환경 서술자**(Python·Unicode·bs4·soupsieve 버전, `html.parser`·
  `_markupbase` 파일 해시)는 digest 에 넣지 않는다. ⚠️ 이유: CI 는 CPython 3.13.15(2026-09-21 실측), 검토 환경은 3.13.5 라 runtime 을
  결속하면 승인이 CI 에서 영원히 불일치한다. 그래서 개정 1 은 A층에 **어느 환경에서든 전체를 재산출해 승인 목록과 정확히 대조**하라고
  요구한다(C1d 의 "신원 해시 대신 재생 일치" 와 같은 해법, A층 배선은 슬라이스 5). ⛔ 목록 일치는 D1 승인 식별자의 일치일 뿐, 파싱된
  DOM·직함 주변 문맥이 같다는 증명이 아니다. 명세를 `.txt` 로 둔 이유: 전체 시험 워크플로가 `**.md` 변경을 건너뛰므로 명세만 바뀐
  커밋에도 시험이 돌게 하려고. 도구 폴더에 `*.py` 가 하나 늘어 `extraction_contract` 다이제스트가 바뀐다.

  **슬라이스 5a(캡처 배선)**: `roundtrip` 이 정제한 트리를 직렬화해 한 번 다시 파싱(정규화, 5c-3a-A)한 뒤 그 트리에서 `replace_names` 로 검토된
  자리의 성명을 바꾸고, 저장할 바이트를 `verify_stored` 로 재파싱해 검증한 뒤 재생 동일성을 본다. `capture_route` 는 `validate_metadata` 뒤에 **전체
  metadata 를 넣은 `verify_stored`** 를 한 번 더 돌린다 — 응답 헤더처럼 페이지에서 왔지만 `roundtrip` 이 보지 못하는 값의 직함을 여기서 잡는다. 다섯
  번의 파싱(원본·정규화·저장본 검사·재생·최종 검사)이 **파싱 10초 예산 하나**를 나눠 쓴다. ⚠️ **D1 은 이제 모든 경로를 막는다** — 등록부가 없는
  경로는 바꿀 것이 없을 뿐, 직함 발견이 있으면 캡처가 거부된다. 씨티 두 경로는 검토된 공시 자리(`대표자` 한 칸)가 없거나 둘 이상이면 거부한다. 치환
  증거는 메모리에만 두고 Artifact·metadata 에 넣지 않는다. 캡처 배선 파일은 정책 digest 에 넣지 않는다 — 승인은 최종 저장 바이트와 현재 D1 판정에
  결속하고, 캡처가 치환을 실제로 돌렸는지는 배선 시험과 재캡처로 확인한다(처음부터 `[성명삭제]` 였던 입력과 실제 치환 결과는 같은 바이트라 A층은
  구별하지 못한다). 반입(A층) 대조·승인 기록 schema 는 5b, 실제 재캡처·반입은 5c(사용자 승인).

  **슬라이스 5b(반입 판정 라이브러리)**: 5b-1 에서 `query_key` 를 순수 모듈 `queries.py` 로 떼어 detector 가 등록부·앱 import 경계에 닿지 않게
  했고, 5b-2 에서 C1d 의 A층(무결성·구조·기록·출처)을 `tools/fixture_capture/admission.py` 로 옮긴 뒤 끝에 D1 승인 대조를 붙였다
  (`d1_approval.py`). 판정표: 발견 0건·파일 없음 → 통과 / 0건·파일 있음 → 파일을 전부 검사하고 `items=[]` / 발견 있음·파일 없음 → 거부 /
  발견 있음·파일 있음 → 전부 검사 후 정확 대조. 승인 기록은 `tests/fixture_reviews/bank_capture/<route>/<capture_id>.json`(닫힌 schema v1:
  두 최종 파일의 원시 바이트 SHA-256, 현재 정책 digest, 검토 환경 서술자, 항목마다 발견과 판단 `name_removed`·`not_person_name_context`,
  검토자 핸들, UTC 시각). 발견 목록은 `canonical_json` 바이트로 순서·자료형까지 비교한다(`False` 는 `0` 이 아니다). 검토 환경은 모양만 검사하고
  현재 환경과 비교하지 않지만, 현재 정책 digest 와 검토 환경 서술자는 **매 검사마다** 구할 수 있어야 한다(0건이어도). 검사 불가는 0건이
  아니고, JSON `null` 인 승인 파일은 "없음" 이 아니다. 정책 manifest 는 반입 모듈과 그 import 폐포 전체(12파일)로 넓어졌고, 폐포와
  manifest 가 **정확히 같은지** 시험한다.

  **슬라이스 5c-1(반입 시험 관문)**: C1d 의 B/C 층과 보류(hold)를 `tests/bank_capture_contract.py`·`tests/bank_capture_hold.py` 로 옮기고,
  실제 저장본을 쓰는 모든 진입점이 `tests/bank_capture_gate.admitted_evidence` 한 곳을 거치게 했다: 반입 목록 확인 → A층 → D1 승인 대조 →
  (compatibility 는 보류가 없을 때만) B, (scoped 는) C. 반입 목록 `ADMITTED_ROUTES` 는 tuple literal 하나이고 실제 시험·보류 매핑의 유일한
  원천이다(파일 존재나 캡처 등록부에서 만들지 않는다). 목록 밖 경로는 저장 폴더 아래 무엇도 조회·열기 전에 거부한다. 보류는 B 만 면제하고
  A·D1·C 는 면제하지 않으며, 두 승인 체계(D1 `fixture_reviews/…` 와 보류 `approvals/…`)는 서로를 대신하지 못한다. 보류 의무는 compatibility
  시험이 call 단계에 들어갈 때 hook 이 기록하고(`tests/conftest.py` 가 세션마다 `HoldSession` 등록), 세션 종료 검사가 관문 → C → 보류 검증을
  다시 한다. 이식한 판정 함수 12개는 C1d 본문 그대로다(계약이 C1d 원본 소스를 넣어 AST 로 대조). 합성 시험은 저장 폴더를 읽지 않는다 — 계약이
  자식 프로세스까지 경로를 실제 경로로 풀어(`dir_fd` 포함) 감시한다.

  **슬라이스 5c-2(MIBANK 반입, 2026-09-22 사용자 승인)**: 09-19 06:06 UTC 에 캡처한 `bs_mibank`(`bank_cd=032`)·`citi_mibank`(`bank_cd=027`)
  저장본을 바이트 그대로 `tests/fixtures/bank_capture/<route>/` 에 넣고 반입 목록에 두 경로를 추가했다. D1 발견 0건이라 승인 기록은 없고,
  실제 fixture 시험 넷(두 경로 × 재생 호환·범위 계약 증거)이 관문(A → D1) 뒤 B·C 를 통과한다. 사용자 판단 대상은 제3자 공개 환율 페이지
  사본을 시험 자료로 보관하는 일이었다. 남은 반입은 5c-3(부산 공식·씨티 두 곳 재캡처 — 씨티 둘은 이름 치환 뒤 D1 발견 판단과 승인 기록,
  부산 공식은 빈 미지원 요소 때문에 09-19 저장본이 거부됨)이었다 — 5c-3a-A·5c-3a-B·5c-3b 에서 완료.

  **슬라이스 5c-3a-A(치환 전 정규화, 2026-09-22)**: 09-22 재캡처에서 씨티 두 경로가 `d1_replacement_correspondence` 로 거부됐다. 치환은
  `parent.contents` 번호(텍스트 노드 포함) 경로를 기록하는데, 기록한 트리는 정제 직후 트리였다. 정제가 요소·주석을 지우면 그 양옆 문자열이 인접하게
  되고, 저장 바이트를 다시 파싱하면 html.parser 가 둘을 하나로 합쳐 뒤쪽 형제의 번호가 줄어든다(`<ul>a<script></script>b<li>…` → `<ul>ab<li>…`).
  실제 씨티 두 페이지(구조만 측정, 두 페이지 같음)에서 공시 li 조상 경로의 두 단계 앞에 인접 문자열 쌍이 1개와 5개 있었고, 그만큼 번호를 줄인 경로가
  저장 바이트 기준 경로와 정확히 같았다. 그래서 정제한 트리를 직렬화·재파싱해 **저장 바이트가 다시 파싱될 모양**으로 만든 다음 치환한다(참조
  구현으로 두 페이지 모두 왕복 통과). 정확한 경로 결속(4A)은 그대로이고 D1 정책 digest 는 바뀌지 않는다(`roundtrip.py` 가 바뀌므로 새 캡처 metadata
  의 `extraction_contract` 는 달라진다 — 반입 판정은 그 값의 형식만 본다). 정규화 뒤에도 트리가 달라지는 입력이 있으면 저장본 검사가
  거부한다(fail-closed). 부산 공식의 거부(속성이 있던 `bsib:mnu` 가 정제 뒤 빈 미지원 요소로 남음)는 정책 개정이 필요해 5c-3a-B 로 나눴다. 실제
  재캡처·사용자 검토·반입은 5c-3b.

  **슬라이스 5c-3a-B(정책 개정 2 — 검토된 빈 미지원 요소, 2026-09-22)**: 09-22 재캡처에서 부산 공식이 `d1_unsupported_element` 로 거부됐다. 페이지의
  `bsib:mnu` 두 개가 정제 전 속성 셋(`targetwidget`·`currentwidget`·`menu`)을 갖고 자식은 없었는데, 4B 제거 규칙은 "속성도 자식도 없는" U 만 지우므로
  남았고, 정제가 속성을 벗긴 뒤 §2.1 이 거부했다. 구조만 측정하니 둘 다 보호 경계 밖이고 각각 추출과 무관한 `form` 의 유일한 요소 자식이었다.
  `tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment2.txt` §A4 가 정제의 U 제거 조건을 둘로 적는다: (1) 속성·자식 없음(4B 규칙을 명세로
  옮김), (2) 자식 없음 + 이름과 파싱된 속성 이름 집합이 검토 목록(`bsib:mnu` → 위 셋)과 정확히 같음. 속성 값은 보지 않는다 — 어차피 벗겨지고
  요소와 함께 사라진다. 정제 전 판정·보호 경계·ruby 제외·부모로 번지지 않음은 그대로다. 목록은 `d1_policy.REVIEWED_EMPTY_UNSUPPORTED`(읽기 전용)이고
  계약이 명세 표와 대조한다. 개정 파일이 `policy_amendments` 에 추가되고 `d1_policy.py` 가 바뀌어 **정책 digest 가 바뀐다** — 리포에 D1 승인
  기록이 아직 없어 무효화되는 승인은 없다. 씨티 승인 기록은 이 digest 로 5c-3b 에서 쓴다.

  **슬라이스 5c-3b(부산 공식·씨티 두 곳 반입, 2026-09-22 사용자 검토)**: `55348c0` 에서 세 경로를 한 번씩 재캡처했다(09-22 05:43 UTC, 개발
  머신, 공개 페이지 GET 1회씩). 부산 공식 D1 발견 0건, 씨티 두 곳은 각 1건 — 공시란에서 치환 뒤 남는 `대표자` 라벨(`대표자 [성명삭제]`)이다.
  반입 뒤 도는 B·C 판정을 미리 돌려 셋 다 통과를 확인한 뒤, 사용자가 최종 저장 파일을 직접 보고 (a) 세 페이지 사본 보관, (b) 씨티 발견
  `name_removed`, (c) 검토자 `Jay-Hong` 을 정했다. D1 명세 §0 의 "사람 검토"를 AI 검토로 바꾸지 않았고, 5c-2 의 MIBANK 승인을 넓혀 쓰지도 않았다
  (Codex 진행안 검토 r1). 세 쌍을 바이트 그대로 `tests/fixtures/bank_capture/<route>/` 에, 씨티 승인 기록 둘을
  `tests/fixture_reviews/bank_capture/<route>/<capture_id>.json`(정책 digest `9e8ae4d8…`, 실제 검토 시각)에 넣고 `ADMITTED_ROUTES` 를 다섯 경로로
  넓혔다. 실제 fixture 시험 10개(다섯 경로 × 재생 호환·범위 계약 증거)가 관문(반입 목록 → A → D1 승인 대조) 뒤 B·C 를 통과한다.
  ⚠️ 승인 기록은 두 최종 파일 해시와 현재 정책 digest 에 결속된다 — D1 정책·구현 파일이 바뀌면 이 승인은 무효가 되어 재검토가 필요하다.
- **상한·기록**: 총 경과 30초, 수신 중·압축 해제 뒤 각각 2 MiB, 파싱 10초, fixture HTML 256 KiB·메타 64 KiB. 원본 응답은 메모리에서만(해시만
  기록), **모든 실패 경로에서 원문을 로그·오류에 남기지 않는다**. `recorded_extraction` 은 캡처용 완전 기록(보고 이벤트의 스니펫 절단·관측
  상한을 재사용하지 않음, 중간 파싱 실패·덮어쓰기 포함) — 불완전하면 승인하지 않는다.
- **계약 불일치 보류 `hold.json`**: fixture 식별자·해시·**정확한 현재 계약**에 결속(계약이 다시 바뀌면 재승인), 계약 불일치 판정만 면제
  (비식별·해시·schema 검사는 유지), 대체 시험이 존재하고 **이번 실행에서 통과**했는지 확인, 승인은 실제 verdict 기록의 해시에 연결,
  `review_by`(KST) 필수 — 만료는 hold 검사를 실행하는 **다음 완료된 CI** 에서 검출된다(전체 시험 워크플로는 Markdown 전용 변경을 건너뛴다).
- **도구 위치**: 운영 이미지 밖(`Dockerfile` 이 `scripts/` 를 복사하므로 `tools/`). `app` import 가 로깅을 초기화하는 경계는 C1b 에서 정한다.

**C1a — 순수 추출 경계(코드)**: `utils.extract_selector_rates`(bs 공식·citi 2차 — 두 벌이던 같은 루프), `citi.extract_citi_items`(citi 1차),
`utils.extract_mibank_rates`(공유 MIBANK) 가 받은 `soup` 에서 값만 뽑는다(요청·DB·Redis 없음). 사건마다 호출자가 넘긴 `on_event` 를 **그
자리에서** 부른다 — 운영 루틴 콜백은 기존 경고 로그·보고 관측자 호출을 그대로 남기고(파싱 실패 문자열 로그는 운영 콜백에만 있다), 캡처 도구는
자기 콜백으로 완전 기록만 모은다. MIBANK 인라인 선택자 넷은 상수로 올렸다. 요청·`BeautifulSoup`·writer·`"URL 오류"` 예외 로그·
MIBANK 디버그 로그와 필수 통화 검사(루프 완료 → 디버그 로그 → 누락 예외 순서)는 루틴에 남았다.
동작 보존 근거: 리팩터 전 커밋(`f85eb7d`)에서 만든 특성 기록 125경우(공식 3경로·`crawl_mibank_rates` 직접·**9개 은행 `_crawl_mibank_*`
경계** — 로그 로거·수준·문구·extra·예외 정보, 관측자 호출·인자, 예외, 요청·writer·DB 조회 인자)가 현재 코드와 같다
(`tests/test_crawler_extraction_characterization.py`). ⚠️ 경고 로그를 찍는 위치가 콜백으로 옮겨져 운영 JSON 로그의 `function`·`line`
필드는 달라진다(이 필드로 bs·citi 경고를 거르거나 세는 코드는 없다).
**C1b — 캡처 도구(코드, 운영 이미지 밖)**: `tools/fixture_capture/`(Dockerfile 은 `tools/` 를 복사하지 않는다). 실행은 개발 머신에서
`python3 -m tools.fixture_capture.capture --route <bs_official|citi_primary|citi_secondary|bs_mibank|citi_mibank|all> --output <Git checkout 밖>`
이고, 통과한 경로만 `<route>-<capture_id>/fixture.html`·`metadata.json` 쌍으로 쓴다(재시도·대체 요청 없음, 경로마다 요청 1회).
- 등록부는 코드 상수 선택자를 지원 문법으로 파싱해 허용 id·class 토큰과 허용 조회(메서드·인자·`recursive`)를 도출한다. 조회 기록기가
  추출·라벨 수집 중 bs4 조회를 가로채 등록부 밖이면 위반을 남기고(라벨 수집이 예외를 삼켜도) 최종 거부한다.
- 왕복 검사는 원본 soup 과 "비식별(슬라이스 5a 부터 검토된 성명 치환 포함) HTML 직렬화 → 같은 파서 재파싱" soup 에 같은 추출 함수를 돌려 사건(라벨 포함, 스니펫 절단 없음)·조회
  경로·반환값·예외(종류·인자·크롤러 파일 안 발생 위치)를 비교한다. 예외도 양쪽이 같으면 fixture 가 될 수 있다 — 합격은 유효 환율의 보증이 아니다.
- 요청기: 응답 훅이 3xx 를 본문 소비 전에 거부한다(`requests` 는 `allow_redirects=False` 여도 리다이렉트 준비 중 본문을 읽는다). gzip·deflate 만
  압축 해제 호출마다 출력 상한을 걸어 푼다 — br·복합 인코딩은 출력 상한을 거는 이식 가능한 API 가 없어 **본문을 읽기 전에 거부**한다(공유
  `HEADERS` 는 br 을 광고하므로 서버가 br 로 답하면 그 경로 캡처는 실패한다). 디코딩은 `requests` 의 `.text` 에 맡겨 운영과 같게 하고,
  `original_body_sha256` 은 압축 해제 뒤 바이트의 해시다. 시간 상한은 SIGALRM 이라 **POSIX 메인 스레드에서만** 돈다.
- 로깅 경계: `app` 을 import 하면 로그 디렉터리와 두 파일이 생긴다(끌 수 없음, 응답이 오기 전이다). 도구는 import 직후 루트 핸들러를 떼어
  닫고, 캡처 동안 로깅을 끄고 stdout·stderr 를 버리며, 원문 없는 JSON 요약만 stdout 에 쓴다.
- 탐지기는 페이지·응답 유래 문자열에 식별자 의심 규칙을 적용하고 코드 유래 필드는 필드별 형식·출처로 검증한다(`capture_id` 는 정규형 uuid4 —
  uuid1 은 호스트 MAC 을 담아 거부). 미커밋 추출 소스가 있으면 provenance 가 거짓이 되므로 거부한다.
- 시험: 합성 입력만(네트워크 차단) 188건. Claude 재검증 변이 55종 — 1회차 8종 생존(시험 빈틈 7·명세 시점 1) → 보강 뒤 2회차 전멸.
다음: C1c(개발 머신 캡처 → 사용자 검토 → 두 에이전트 exact 승인 커밋) → C1d(의미 시험).

## 열린 결정 (미해결)

- (D1) collection_success 저장: crawler_stats export vs source_registry TODO 기반 신규 필드 — **단, §6-2 실행결과(observed_assets 포함)가 정해진 뒤 결정**(crawler_stats는 source 단위라 per-asset 목표 단독 미충족).
- (D2) §6-2 실행결과 계약을 크롤러 리팩터로 할지 wrapper 레벨로 우회할지. **성공 기준은 `observed_assets`/`valid_observation_count`(유효 관측 asset)이며 `changed_rows`(new_records_count)는 진단값** — new_records_count는 변경-시-INSERT라 정상 수집도 0건이라 **성공 판정에 사용 불가**(codex High). §6-1(유효성 정의)이 이 기준의 선행.
- (D3) USDT in-process liveness export 대상/형식(is_stale만 vs ticker-dead/task.done 포함), 5거래소 ticker-dead 공통화([§12.9.8①](USDT_WS_DESIGN_PLAN.md)) 선행 여부.
- (D4) 영속 heartbeat 저장소(Redis vs DB) 및 multi-worker 대비 필요성 — shadow 관측 후.
- (D5) Citi(수집O·표시X) 및 미표시 소스의 health를 관측만 할지 무시할지.
- (D6) source-health가 무료 트랙 출시 blocker인지 fast-follow인지 (제품/타임라인 결정).
- (D7) §7.4 — `partial` / `unknown` / `preserved` 의 집계 방식과 `success_rate` 분모, 구 의미 누적 통계와의 구분. 이 결정 전에는 관리자 API 의미 호환을 주장하지 않는다.
- (D8) §7.5 — 지속장애 알림의 구체 임계·재알림 간격, 그리고 IBK 기존 통지와 공통 알림의 **사건 소유권**(같은 사건 중복 발송 방지).
- (D9) **해소(2026-09-19 Claude·Codex 설계 합의)** — Investing 요약도 `valid → unknown → missing` 으로 맞췄다. 순서만
  바꾸면 실패한 재시도의 미관측(`unknown/not_observed`)이 앞 시도의 확정 누락을 약하게 만드는 회귀가 생겨, 실패 시도
  미관측 확정(`missing/attempt_failed`, `Exception` 한정)을 함께 넣었다(§7.10 `/2`). 운영 반영은 별도 배포.
- (D10) **Investing 부분 해소** — 이벤트에 `validity_contract` 를 싣고(`schema_version=3`), 필드 없는 schema 2 이벤트는
  `investing_range_checked/1` 로 명시 매핑한다. 요약 의존 지표는 계약별로만 내고 합산하지 않는다
  ([INVESTING_OBSERVE.md 집계 의미](INVESTING_OBSERVE.md#집계-의미)). 처음 적은 `basis_unknown` 대신 **거부**
  (`invalid_events`, 모든 날 coverage partial)로 정했다 — 입력이 우리 추출기 산출물뿐이라 모르는 조합은 결함이다.
  은행 계약 집계는 은행 집계기를 만들 때 정한다.
