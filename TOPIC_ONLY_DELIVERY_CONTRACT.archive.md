# Topic-only 전달 계약 (ADR-041 상세)

> **상태**: Draft — 구현 착수 전 합의 대상
> **요약 entry**: `DECISIONS.md` ADR-041
> **대상**: 신규 iOS(reference) → Android 이식. 서버는 이행 기간 동안 legacy 병행 유지.
> **작성 근거**: 2026-08-08 운영 리허설 결함 + 그 수정(iOS `8aadc2f`)이 **틀린 동작을 정확히 집행**하게
> 만든 것을 되돌리기 위한 전진 계약. 이 문서의 모든 코드 단정은 2026-08-09 실측이다.

---

## 1. 왜 이 문서가 필요한가

리포가 **스스로 갈려 있다**. 같은 질문("topic 이 조용하면 무엇을 보여줄 것인가")에 두 문서가 반대로 답한다.

| 출처 | 서술 |
|---|---|
| `REALTIME_V2_CLIENT_GUIDE.md` §8 | 휴장/주말 stale UI 는 단말 정책 — **권고: 마지막 값 유지** |
| `DECISIONS.md` ADR-038 D2 | MODE 2 revert(45초 후 legacy 전환)를 **전제로 설계** — KRX 수신이 tether 신선도를 연장하지 않게 만든 이유가 그것 |
| `TOPIC_V2_RELEASE_RUNBOOK.md` | "topic 무수신 45s 초과 시 **자동으로 legacy 표시**"를 합격 기준·롤백 근거로 사용 |

즉 MODE 2 는 iOS 가 혼자 만든 정책이 아니라 **서버 ADR 이 한 번 승인한 적 있는** 정책이다.
따라서 "클라를 서버 계약에 맞춰라"로 정리되지 않는다 — **결정을 새로 기록해야** 코드를 고쳐도
다음 사람이 되돌리지 않는다.

---

## 2. 불변식 — 신규 앱은 legacy 를 읽지 않는다

```
구독자   → topic WS + 인증된 v2 topic snapshot
무료     → 인증된 v2 hourly snapshot
어느 쪽도 legacy REST/WS 를 읽지 않는다 (DXY 포함 — §2.0(b) 로 topic 신설이 선행조건).
서버의 legacy 병행은 오직 구버전 출시 앱을 위한 것이다.
```

> ⛔ **이것은 목표 상태다. 현재 구현은 이걸 만족하지 않는다.** 아래 §2.0 을 먼저 읽을 것.

### 2.0 현재 상태와의 격차 (출시 차단)

**(a) WS 의 FX/USDT 는 무토큰으로도 구독된다 — premium 강제가 없다.**
`topic_dispatcher` 는 `id_token is None` 이면 *"무토큰 = 기존 동작 그대로(등록 + snapshot, ack 없음)"* 로
처리하고, **per-user 판정 대상은 KRX 하나뿐**이다(`per_user_gated_snapshot_topics`).
토큰이 있어도 FX/USDT 는 premium 판정 없이 `free_accepted` 로 들어간다.
반면 REST twin 은 premium 을 실제로 강제한다(ADR-039 §8.1 E3).

⚠️ 이건 버그가 아니라 **의도적으로 유예된 단계**다 — 코드 주석이 *"enforcement 는 capability 와
분리돼야 하고(§E1), 현행 클라가 무토큰이라 무조건 요구하면 구 클라가 topic 을 잃는다"* 고 적는다.
그러나 **신규 앱 출시 계약과는 양립하지 않는다**: `premium_required` 는 현재 사실상 KRX 에서만
나오므로 §4 전이표의 인가 행이 발화하지 않는다.

→ **Release arming 전에 WS FX/USDT 에도 인증 + premium 판정과 premium lease 를 강제해야 한다**
(ADR-039 Stage A). 이게 닫히기 전까지 §2 불변식은 문서상 목표일 뿐이다.

**(b) DXY 는 현재 legacy envelope 로만 온다 → `dxy` topic 신설이 출시 선행조건이다.**
`WebSocketService` 가 legacy `rates` 프레임의 `indices` 를 통해 DXY 를 받는다
(`onIndicesReceived?(response.indices)`).

⛔ **초안은 여기서 "legacy rate 값만 금지"로 불변식을 좁히고 envelope 예외를 두려 했다. 철회한다.**
`FREE_TIER_ACCESS_MODEL_PLAN.md` §6 롤아웃이 **이미** 순서를 정해 뒀다 —
*"4. iOS legacy 이탈: 4a REST/WS 토큰 전달 → **4b `dxy:spot` topic 신설** → 4c 부팅·offline·stale 를
topic snapshot/cache 기준 전환 → 4d /api/rates + legacy WS 제거"*.
4a 는 끝났다(1B/1C). **지금이 4b 다.** 예외를 두는 것은 합의된 로드맵을 되돌리는 것이다.

→ **채택**: 불변식은 **강한 형태 그대로 유지**하고, **DXY topic 신설을 출시 선행조건으로 올린다**.

✅ **범위 확정: 이번 출시는 `dxy:spot` 하나만**(로드맵 4b 그대로).
근거 — premium live bridge 가 **spot 만** `dxyLive` 로 보충하고 `dxy_futures` 의 live state 는 없다.
따라서 futures topic 은 **legacy 이탈에 불필요**하며 phased 로 미룬다.
(테더 1d 그래프의 DXY_futures 계열은 그래프 데이터 경로이지 live tail 이 아니다.)

### 2.2 불변식의 근거는 두 겹이다 — 둘 다 적어 둔다

하나만 남기면 다른 하나가 잊힌다.

1. **인가** — `FREE_TIER_ACCESS_MODEL_PLAN.md` **D4 "신규 앱 legacy _anon_ fallback 금지(양 플랫폼)"**.
   legacy `/api/rates`·WS `rates` 는 **전부 무인증**이므로, 신규 앱이 legacy 로 떨어지면
   비구독자가 실시간을 공짜로 얻는다 = 페이월 우회.
   ⚠️ `DECISIONS.md` ADR-039 요약은 이 문장에서 **`anon` 을 떨어뜨렸다**. 요약이 원문보다 강하다 —
   같은 슬라이스에서 정정한다.
2. **제품** — USDT/KRX 는 legacy 에 **존재한 적이 없다**. `app/legacy_policy.py` 의
   `LEGACY_RATE_SOURCES` 는 investing + 은행 9곳뿐이고 docstring 이 doctest 로 못 박는다:
   `should_include_source_in_legacy_rates("upbit", "usdt-krw") → False`.
   그래서 테더 탭의 legacy 전환은 fallback 이 아니라 **거래소 5 + KRX 를 잃고 은행 USD 3행만 남는
   순수 손실 교환**이다.

### 2.1 삭제 범위 — **두 함수가 아니다** (2026-08-09 전수 inventory)

초안은 `usdtDisplayState` 의 legacy 분기와 `baseRates(for:)` 두 곳만 적었다. **크게 과소평가였다.**

⚠️ **`usdtDisplayState` 의 최외곽 게이트가 `if RealtimeV2Config.isTetherTopicEnabled` 이고 그 else 가
`sourceRates()` 다.** Release 기본값은 topic **OFF** 이므로, arming 없이 출시하면 테더 탭은
간헐이 아니라 **상시** 은행 3행이 된다. legacy 분기 제거는 곧 **arming 이 출시 전제**임을 뜻한다.

**(A) 단순 삭제로 끝나지 않는 것 — 각각 별도 작업이다**

| # | 지점 | 왜 어려운가 |
|---|---|---|
| A1 | **`AppState` enum 자체** — `.connected(rates:)` / `.offline(cachedRates:)` / `.refreshingCached(cachedRates:)` | legacy 배열이 **enum payload** 이고 `AppState.rates` 가 모든 소비의 단일 관문이다. 지우면 앱 **최상위 화면 라우팅**(loading/error/tab)과 배너 3종의 판정 근거가 통째로 사라진다. topic 쪽엔 전역 lifecycle 상태 개념이 없고 `tetherReceived`/`fxReceivedAssets`/`freshFxAssets` 로 흩어져 있다 → **앱 수명주기 상태머신 재설계**다 |
| A2 | **은행 가격알림 시트** — `AlertAddSheet.rate(for:)` → `rateViewModel.rates(for:)` → `appState.rates` **직결** | `baseRates` 를 **안 거친다**. 즉 fx topic 이 켜져 있어도 이 시트는 **topic 값을 못 본다**(오늘도 그렇다 — 탭은 topic, 알림 시트는 legacy). 배선 교체가 아니라 **topic 경로 신설**이다 |
| A3 | **WS `handleLegacyRatesMessage` 3-in-1 envelope** — `rates` + `indices`(DXY) + `graph_buckets` 동거 | `rates` 만 지워도 `graph_buckets` 소비자(`GraphViewModel`)가 남아 디코드를 못 지운다. ⚠️ **단 서버 계약 변경은 불필요** — 그 VM 은 생성·callback 설치만 되고 `start()` 호출과 소비 View 가 **없다**. **클라에서 VM·callback·환경 주입을 제거**하면 끝이고, 서버는 구버전용으로 `graph_buckets` 를 계속 보내면 된다 |
| A4 | `usdtDisplayState` 5분기 사다리 | 각 분기가 서로 다른 실측 버그(행 reflow / blank flash / MODE 2 frozen)의 대응이고 근거가 주석에 박혀 있다. 마지막 두 분기만 떼면 `isOffline`/`isRefreshingCached` 조건이 함께 무의미해진다 |

**(B) 선행 정리 — cutover 표면을 먼저 줄인다 (저위험, 순이익)**

사용처가 **0** 인 데드 코드: `RateGraphView` 전체(사용처=자기 `#Preview`) / `PeriodTabBar` /
`WebSocketService` 의 `latestRates`·`lastUpdated` 사본 / `WebSocketMessage.updateGraphCache` /
`ExchangeRateViewModel.referenceRate(for:)`·`rateRange(for:)`.
→ **cutover 전에 지운다.** 지우고 나면 남는 소비처가 줄어 나머지 작업이 작아진다.
⚠️ **"위험 0"은 과장이다** — `RateGraphView`/`PeriodTabBar` 는 Preview 외 사용처가 없지만,
`GraphViewModel` 은 **런타임에 생성되어 legacy graph callback 과 observer 를 설치**한다.
저위험이되 **동작 변화 0 은 아니다** → Debug/Release 양쪽 빌드 + callback 소유권 확인이 필요하다.

**(C) ⛔ 테스트 커버리지 공백 — 지금 상태로 제거하면 _맹목_ 이다**

정확히는 **envelope 1차 디코드 테스트는 있다**(`TopicMessageTests.testEnvelopeDecodes_legacyRates_topicAbsent`).
없는 것은 **사용자 행위** 커버리지다 — `ExchangeRateResponse`/`IndicesPayload`/`DxyLiveTick` 를
참조하는 테스트가 **0건**이라 DXY 적용·callback·legacy cache 동작은 제거해도 신호가 없다.

⛔ **그렇다고 "제거 대상 경로"에 테스트를 다는 것은 틀렸다** — 곧 버릴 테스트를 만드는 셈이다.
→ **보존해야 할 사용자 행위**를 먼저 테스트한다(§11.1 매트릭스). 그 테스트는 cutover 후에도 산다.

**(D) 타입은 남긴다**

`ExchangeRate` **struct 자체는 무료 티어 스냅샷이 verbatim 디코드**해 재사용한다.
제거 대상은 `ExchangeRateResponse` / `Metadata` / `IndicesPayload` 이지 `ExchangeRate` 가 아니다.

---

## 3. monitor 의 의미 전환

`8aadc2f` 가 만든 것(관측되는 저장 freshness + deadline monitor + 전용 `ContinuousClock`)은 **유지한다**.
바꾸는 것은 **만료 시 취하는 행동**과 **45초가 무엇의 지표인가**이다.

```
before:  45초 = 데이터 만료  → topic 값 폐기 → legacy 표시
after:   45초 = 전달 이상 의심 → 조용히 재검증 → 실패 확정 시에만 사용자에게 알림
```

### 3.1 45초는 데이터의 나이가 아니다

- **USDT**: Redis coalesce 조건이 `same rate AND same 5s bucket`
  (`latest_rates_cache.py`) — 가격이 평평해도 새 5초 버킷에 tick 이 들어오면 SET → publish.
  → `usdt:krw` 침묵 = 가격 안정이 아니라 **tick 부재**(체결 없음 또는 collector 사망).
- **KRX**: 위 coalesce 는 **Stage E tick writer 경로에서만** 같다(`KRX_REDIS_TICK_WRITE_ENABLED`,
  코드 기본값 false / 운영은 2026-05-26 활성). 일반 KRX writer 는 매번 SET 한다.
  그리고 장마감(15:45) 후 무발행이 정상 — 이미 시간 기반 staleness 가 **없다**(ADR-038 D2).
- **FX**: 주말·휴장 무발행이 정상이다. 최대 수십 시간.
- 서버에 **topic data-plane heartbeat·주기적 재발행이 없다**
  (`topic_dispatcher.py` / `fx_topic_publisher.py` 확인).
  ⚠️ transport 레벨 ping/pong 은 **있다**(iOS 30초 ping ↔ 서버 pong) — 그건 연결 생존만 증명하고
  특정 topic publisher 의 생존은 증명하지 않는다.
- ack 에 **`server_time` 이 없다**(`topic_wire.py`) → WS 경로로 기기 시계 오차를 잴 수단이 현재 없다.

### 3.2 시간 deadline 은 tether 하나만 유지한다

FX/KRX 에 임의의 시간 임계를 만들지 않는다. 대신 **연결 상태 + ack + lease** 로 판단한다.

⚠️ **이 선택에는 전제가 붙는다.** FX 침묵 감지가 아래 셋에 의존하게 된다 —
(a) 연결 상태, (b) lease 갱신, (c) §5 의 서버 close-on-send-failure.
**셋 중 하나라도 빠지면 FX 는 그 축의 감지 수단이 0 이 된다.** 계약으로 함께 잠근다.

⚠️ **이 셋이 덮는 것은 transport / auth / subscription 이상까지다.** *"FX 침묵을 감지한다"* 로
넓게 읽으면 안 된다 — §3.3 대로 **정상 snapshot 이후의 publisher 사망은 클라가 판별할 수 없다**.

### 3.3 ⛔ 그런데 셋으로도 **덮이지 않는 구멍**이 있다 (출시 차단)

서버는 **registry 등록과 ack 를 먼저 끝낸 뒤** initial snapshot 을 만든다(`topic_dispatcher`).
그리고 snapshot build 가 실패하거나 `None` 이면 **연결을 유지한 채 조용히 skip** 한다
(`topic_initial_snapshot`). FX publisher 도 build/publish **전** 예외를 격리하고 `False` 만 반환한다
(`fx_topic_publisher`).

→ **연결·pong·ack·lease 가 전부 정상인데 snapshot 이 한 번도 오지 않는 상태**가 실제로 표현된다.
이때 §5.1 의 close 는 발동할 기회조차 없다(send 자체가 없으므로).

**결정 — 출시 범위**:

**1. 서버 — 정적 `unavailable` 만 ack 전에 판정하고, 나머지는 등록→ack→전송 순서를 유지한다.**

⛔ 초안은 *"ack 뒤에 종결 신호를 보내거나 연결을 닫는다"* 라고 썼다. **철회한다 — 종결 계약 위반이다.**
`topic_dispatcher` §8-B-term 이 *"식별된 요청은 반드시 종결된다 … **종결 프레임 하나 또는 연결 종료**"*
라고 정한다. ack 이 이미 그 하나이므로, 그 뒤에 또 종결 신호를 보내면 **종결 프레임이 둘**이 된다.

⛔ **"ack 전에 전부 build 한다"(초안 (a))도 철회한다.** 그러면 두 가지가 깨진다 —
① build↔register 사이에 발생한 publish 를 놓치고(조용한 FX 는 낡은 prebuilt 로 오래 남는다),
② **"지원되지만 아직 데이터가 없음"을 거부로 오분류**한다. 실제로 KRX 는 데이터가 없어도 구독을
유지하도록 설계돼 있고, iOS 모델은 `usd_krw_futures: null` 인 **빈 snapshot 을 이미 표현**한다.
이를 `topic_unavailable` 로 거부하면 첫 데이터가 생겨도 **그 연결에서는 영영 못 받는다.**

→ **채택: build 결과를 분류하고, 분류마다 다른 경로로 보낸다.** 이분법(a/b)이 아니라 분류 문제다.

| build 결과 | 판정 시점 | 응답 |
|---|---|---|
| **unavailable** — flag off 등 의도적 미지원(현 코드의 `None`) | **ack 전**(플래그 조회, build 불필요) | 같은 ack 의 `topic_unavailable` 거부 |
| **ready(payload)** | ack 후 | 정상 snapshot |
| **supportedEmpty(payload)** — 지원되나 아직 데이터 없음 | ack 후 | **schema-valid 빈 snapshot**. 거부 아님 |
| **transientFailure** — DB/Redis 일시 실패 | **항상 ack 후** | **close 1013**. ⛔ `topic_unavailable` 로 위장 금지 |
| **fatalFailure** — 직렬화/프로그래밍 오류 | **항상 ack 후** | **close 1011** |

⚠️ **transient/fatal 에 "ack 전" 분기가 없는 이유**: 최소 계약에서 ack 전에 하는 일은 **정적 flag 조회**뿐이고
그건 일시 실패하지 않는다. 실제 build 는 전부 ack 후이므로 두 실패도 전부 ack 후다.

**출시용 최소 계약 (이 순서를 그대로 구현한다)**

```text
1. 정적 unavailable 판정 (flag 조회, build 불필요) → 같은 ack 의 topic_unavailable 거부
2. register
3. ack 송신
4. ready / supportedEmpty 전송
5. ack 후 transient build 실패 → close 1013 (Try Again Later)
6. ack 후 fatal 실패        → close 1011 (Internal Error)
   ⛔ 5·6 은 종결 프레임을 더 보내지 않는다 — §8-B-term 은 "프레임 하나 **또는 연결 종료**"다.
      close 로 넘기면 기존 재연결 경로가 그대로 복구를 맡는다.
7. 클라는 **요청 송신 시점의 수신 세대**로 ack 전후 live frame 을 모두 인정한다
```

✅ **재구독 중 기존 active topic 의 build 실패도 같은 규칙**(post-ACK → close)으로 닫힌다.
연결이 닫히므로 `register` 의 합집합 의미론과 `rejected_topics`/`active_subscriptions` 모순 문제가
**발생할 여지 자체가 없다**. 초안이 이걸 미결로 남긴 것은 최소 계약을 채택하기 전 서술이 남은 것이다.

⚠️ 함께 확정할 것:
- *"ack 이 데이터보다 먼저"* 는 **initial snapshot 에만** 적용된다. **모든 live frame 보다 먼저**라는
  보장은 현 구조(outbound 직렬화 없음)에서 성립하지 않는다 — 그래서 아래 2가 필요하다
- ⛔ **경계는 넷이고(클라 2 · 서버 2), 제약은 둘이다.** 초안은 *"build 총예산 < iOS 20초"* 라고 썼는데 **틀렸다** —
  iOS 는 ACK 수신 시 `takePending` 이 timeout task 를 **즉시 취소**한다. build 는 ACK **뒤**라
  그 20초는 이미 사라졌고, 클라 쪽에 build 를 묶는 상한이 **없다**.

  | deadline | 대상 | 소유 | 짝 |
  |---|---|---|---|
  | **클라 ACK deadline** (현행 20초) | 요청 → ack | 클라(`topicRequestTimeoutTasks`) | ① |
  | **서버 ACK 예산** (신규) | 요청 수신 → ack 송신 | 서버 | ① |
  | **클라 initial-delivery deadline** (신규) | 요청 송신 시점 수신 세대 → 첫 프레임 | 클라(§3.3-2) | ② |
  | **서버 snapshot 총 예산** (신규) | ack 이후 전체 build → 초과 시 **close 1013** | 서버 | ② |

  ⛔ **제약 ②는 단순 대소 비교가 아니다 — 두 시계의 _원점이 다르다_.**
  클라 initial-delivery 는 **요청 송신** 부터, 서버 snapshot 예산은 **ack 이후** 부터 잰다.

```text
  ① 서버 ACK 예산 < 클라 ACK deadline
  ② (서버 ACK 경과) + (서버 snapshot 예산) + (전송 여유) < 클라 initial-delivery deadline
```

  그리고 **서버는 요청 전체 absolute deadline 도 함께 유지**한다 — 부분 예산만 두면 합이 새어 나간다.

  ⛔ **초안의 근거는 틀렸다.** *"ACK 가 흐름상 먼저"* 라고 썼는데, 같은 문서 §3.3-2 가
  **register 가 ack send 보다 먼저라 live frame 이 ack 보다 먼저 도착할 수 있다**고 적는다.
  ACK deadline 을 먼저 두는 진짜 이유는 프레임 도착 순서가 아니라 **control-plane 완료를 먼저
  판정하기 위해서**다.

  ⚠️ **세 경우의 단일 소유자를 계약으로 정한다** (현행 ACK watchdog 은 ack 수신 즉시 제거되어
  두 타이머의 생애가 겹치는 구간이 실재한다):

  ⛔ **표에 "동시엔 ACK 우선"이라 적는 것만으로는 보장되지 않는다.** 독립 `Task` 둘은 MainActor 에서
  직렬화되더라도 **어느 쪽이 먼저 재개되는지 계약되지 않는다** — 규칙만 있고 집행자가 없다
  (이번 슬라이스가 고치고 있는 원 결함과 **같은 형태**다).

  → **watchdog 둘이 아니라 _공용 arbiter 하나_** 로 구현한다. arbiter 가 상태와 **두 deadline 을
  함께 읽고 한 번만 전이**한다.

  ⛔ **arbiter 는 두 가지를 _분리해서_ 든다** — 섞으면 ACK 유실이 **이미 받은 데이터 증거를 지운다**:

  | | 범위 | 의미 |
  |---|---|---|
  | `controlState` | **배치 요청 단위** | 요청이 서버에 확인됐는가(ack) |
  | `deliveryState[topic]` | **topic 단위** | 그 topic 이 요청 송신 세대 이후 유효 frame 을 받았는가 |

  ⛔ **subscribe 는 배치다** — 한 요청에 여러 topic 이 실린다. 따라서 구조는
  `request.controlState + [Topic: DeliveryState]` 이고, **한 topic 의 frame 이 배치 전체의 수신 성공으로
  처리되면 안 된다**. 재검증(§6)도 **미수신 topic 만** 범위로 삼는다.

  | 조건 | arbiter 전이 |
  |---|---|
  | ACK 미수신 & ACK deadline 경과 | **먼저 요청 소유권을 회수**(`takePending` 패턴) → 늦게 온 ACK 는 무시 → 재구독. ⛔ **이미 병합한 값과 `receiveGeneration` 은 보존**한다 |
  | ACK 수신 & delivery deadline 경과 & 세대 증가 없음 | 재구독(§6) |
  | **둘 다 경과**(예: background 복귀) | **ACK 를 먼저 처리** — control-plane 판정이 앞선다 |

  ⚠️ **반례가 실재한다**: ack 이 유실됐지만 **유효 frame 은 먼저 도착**한 경우(§3.3-2 의 register→ack
  race). 이때 delivery 판정을 무효화하면 **진짜 데이터를 받고도 못 받은 것으로 처리**한다.

  ⚠️ 그리고 **`ACK deadline < delivery deadline` 을 상수 수준에서 강제**한다(assert). 값이 뒤집히면
  위 전이표가 무의미해진다.

- ⚠️ **`asyncio.to_thread` 취소는 내부 작업을 멈추지 않는다** — 스레드는 계속 돈다.
  따라서 서버 절대 deadline 만으로는 부족하고 **DB/Redis I/O 자체에 상한**이 필요하다.

**2. 클라 — deadline 의 기준선은 ack 이 아니라 _요청 송신 시점_ 이다.**

⚠️ `registry.register(...)` 가 **ack send 보다 먼저** 실행되고 outbound 직렬화가 없다 →
그 사이 publish 가 발화하면 **live frame 이 ack 보다 먼저 도착**할 수 있다.
"ack 이후 프레임만 수신 성공"으로 세면 **조용한 FX 에서 정상 데이터를 받고도 재연결**한다.

→ 요청을 보내는 시점에 topic 별 **수신 세대(카운터)를 캡처**하고, 그 이후 도착한 프레임은
ack 전후 무관하게 **전부 인정**한다. 기한 내 0건이면 재구독 → 실패 시 재연결(§6 사다리).

**3. 서버 — 정상 initial snapshot _이후_ 의 publisher 사망은 클라가 판별할 수 없다** (⛔ §13.1(1) 수용 시 **arming 선행조건**, 후속 아님)
(§3.1 — 정상 침묵과 구분 불가). **서버 publisher health/SLO/알람**으로 덮는다.
이건 클라 계약이 아니라 **운영 계약**이다.

⚠️ **만약 나중에 시간 deadline 을 둘 이상 두게 되면**, `startFreshnessMonitorIfNeeded` 는
`guard freshnessMonitor == nil` 이라 **이미 도는 monitor 를 재무장하지 않는다**. 균일 임계에서는
새 deadline 이 항상 기존보다 뒤라 안전하지만, 임계가 갈리면 더 이른 deadline 이 생겨
**monitor 가 자면서 지나친다** = 2026-08-08 결함의 재생산. 그때는 *"새 최근접 deadline 이 현재
수면 목표보다 이르면 재무장"* 이 **필수 동반**이다.


### 3.4 Publisher health / SLO 계약

§3.3 의 침묵은 클라가 판별할 수 없다 — 서버가 이 절로 덮는다. 이 절은 **확정**이며
ADR-041 의 `[제안·결정 대기]` **파생 숫자 숨김 정책** 제안의 채택 여부와 무관하다.

**시장 세션을 반영한 _인과 기반_ publisher health / SLO** + **수치화된 탐지·대응 시간**
⛔ *"N분 무발행"* 알람은 **금지** — FX 주말(수십 시간)·KRX 장마감·Gopax ~180초 sparse 에서
오탐한다. 그건 우리가 클라에서 고치고 있는 바로 그 결함을 서버에서 재생산하는 것이다.
⚠️ 인과의 시작점도 **raw trigger 가 아니다** — 정상 coalesce / shadow / disabled / 구독자 0 은
raw trigger 뒤 publish 가 없는 **정상 결과**다. **eligible flush(would-publish) 이후**를 기점으로
삼되, ⛔ **평면 목록으로 두지 않는다 — 단계가 섞인다**(이미 eligible 이면 policy skip 은 나올 수 없다).
**세 축으로 직교 분해**한다:

| 축 | 값 |
|---|---|
| `flushDisposition` | coalesced / disabled / shadow / no-subscriber / **eligible** |
| `deliveryOutcome` | build-error / no-eligible-lease / send-all-failed / sent |
| `telemetryHealth` | available / **unavailable** ← 별도 직교 축 |

⛔ **위 표는 _pipeline accounting_ 이다. delivery SLO 는 `flushDisposition == eligible` 인
사건에서만 시작한다** — coalesced/disabled/shadow/no-subscriber 는 정상 결과라 SLO 모집단이 아니다.
⚠️ **`telemetryHealth=unavailable` 은 publish 결과가 아니라 _결과를 알 수 없는 관측계 장애_** 다.
정상으로 간주하지 말고 **별도 경보**한다.
⚠️ 현재 `publish_topic_detailed` 는 **live caller 가 없고**, "전원 lease 만료"와 "구독자 0"이
**합쳐진다** — `no-eligible-lease` 와 `no-subscriber` 를 가르려면 그 분리가 선행이다.
---

## 4. 거부 사유별 전이표 (이 문서의 핵심)

**last-known 은 전송 장애에만 맞는 답이다.** 인가 거부에 last-known 을 보여주면 그게 곧 페이월
우회다. 사유를 구분하지 않으면 legacy 를 걷어내도 인가 정책이 다시 섞인다.

아래 사유는 **실제 코드에서 확인한 것만** 적는다(지어낸 코드 없음).

### 4.0 사유는 **두 층**이다 — 이걸 섞으면 안 된다

`app/topic_wire.py` 가 어휘를 **실행 가능한 집합**으로 강제한다(총 9종).

- **전체-요청** `WHOLE_REQUEST_ERRORS` (4) — 요청 전체가 실패. **모든 요청 topic 에 영향.**
  `invalid_token` / `temporarily_unavailable` / `invalid_request` / `request_too_large`
- **per-topic** `PER_TOPIC_ERRORS` (5) — ack 의 `rejected_topics` 에만 실림. **그 topic 만 영향.**
  `topics_disabled` / `unknown_topic` / `premium_required` / `krx_entitlement_required` / `topic_unavailable`

⚠️ 초안은 7종만 적고 `unknown_topic`·`topic_unavailable` 을 빠뜨렸으며 **두 층 구분 자체가 없었다**.

### 4.1 전이표

**프레임 아닌 조건**

| 사유 | 출처 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|---|
| **무수신(45s)** — tether | 클라 타이머 | 변화 없음(조용히 재검증, §6) | last-known 유지 | 보존 | 즉시 재구독 1회 |
| **연결 끊김** | `connectionState` | "연결 끊김" + 재시도 | last-known 유지 | 보존 | 기존 재연결 |
| **transport close 1009** | 16KB 초과 | "연결 끊김" 경로와 동일 | last-known 유지 | 보존 | 기존 재연결 |

**전체-요청 (모든 요청 topic 에 적용)**

| 사유 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|
| `temporarily_unavailable` | 변화 없음 | last-known 유지 | 보존 | **재시도**(클라 allowlist 유일) |
| `invalid_token` — **동일 UID 토큰 refresh 진행 중** | 변화 없음 | last-known 유지 | 보존 | refresh 후 재요청 |
| `invalid_token` — **refresh 실패 / revoked / disabled 확정** | 재인증 요구 | **premium·KRX 값 clear** (인가 격리) | 보존 | ❌ 재인증 전까지 |
| `invalid_request` | 진단 로그만 | 변화 없음 | — | ❌ terminal(프로그래밍 오류) |
| `request_too_large` | 위 1009 행 참조 | — | — | 실제 운영 경로는 **frame 이 아니라 close** |

⚠️ **`invalid_token` 을 한 줄로 두면 §4 자체 원칙을 위반한다** — 서버의 `invalid_token` 에는
만료뿐 아니라 **revoked / disabled user** 가 포함되고 클라는 셋을 구별할 수 없다. 확정 실패에
last-known 을 유지하면 **인증이 끝난 뒤에도 과거 premium/KRX 값을 무기한 표시**하게 된다.

**per-topic (그 topic 만)**

| 사유 | 화면 | 데이터 | 구독 의도 | 재시도 |
|---|---|---|---|---|
| `topics_disabled` | **명시적 비활성 안내** | 전 topic surface purge(§7) | **보존** | 폭풍만 중단, 재연결·foreground·수동에서 복구 |
| `topic_unavailable` | 그 topic 만 unavailable | 그 topic 값 제거 | **보존** | lifecycle 기반 복구 |
| `unknown_topic` | 그 topic 만 숨김 | 그 topic 값 제거 | **보존** | `nextConnection` |
| `premium_required` | 무료 화면 | **인증된 v2 hourly 로 전환** | 복구 집합에 보존 | 자격 변화 시 재요청 |
| `krx_entitlement_required` | KRX **즉시 숨김** | KRX 행 제거 | **`confirmed` 만 제거, `desired` 보존** | `entitlementChange` |

**클라에 이미 있는 것**: `SubscriptionError.isTerminal`(invalid_token/invalid_request/request_too_large),
`isRetryable`(temporarily_unavailable), `premium_required` 복구 집합.
⚠️ **per-topic 거부 중 전용 동작이 있는 건 `premium_required` 하나뿐이고 나머지는 로그만 남긴다** —
`topic_unavailable`·`unknown_topic`·`topics_disabled` 는 **새로 배선해야 한다**.

⚠️ **`topics_disabled` 를 "영구 중단"으로 처리하지 않는다.** 재시도 폭풍만 멈추고 **구독 의도는
보존**해야 서버 재활성화 후 복구된다.

⚠️ **§2.0(a) 가 닫히기 전에는 `premium_required` 행이 FX/USDT 에서 발화하지 않는다** — 현재
per-user 판정 대상은 KRX 뿐이다. 전이표는 Stage A 완료를 전제로 한다.

### 4.2 구독 상태 — **canonical 저장 필드 + 파생 필드**

`subscribedTopics`(의도) / `confirmedTopics`(서버 확인) 두 축으로는 위 표를 표현할 수 없다.

| 축 | 의미 |
|---|---|
| `desired` | 앱이 원하는 topic — **거부 사유가 무엇이든 함부로 비우지 않는다** |
| `confirmed` | 서버 ack 의 `active_subscriptions` — 연결의 최종 상태 |
| `rejectionReason` | 마지막 거부 사유 — 화면 분기의 입력 |
| `retryPolicy` | **무엇이 재시도 자격을 다시 여는가** (아래) |
| **`deliveryState`** | **구독이 아니라 _데이터가 오고 있는가_** (아래) |
| `receiveGeneration` | 요청 송신 시점에 캡처하는 topic 별 수신 카운터(§3.3-2) |

⛔ **`retryEligible` 을 boolean 으로 두면 안 된다** — 표의 사유마다 복구 트리거가 다르다
(`topics_disabled`=재연결·foreground·수동 / `topic_unavailable`=lifecycle / `unknown_topic`=다음 연결 세대).
foreground 는 **같은 연결 세대 안에서** 일어날 수 있으므로 boolean 으로는 모호하다.
→ **단일 enum 도 부족하다** — `topics_disabled` 는 reconnect·foreground·manual 을 **모두** 열어야 하고
`serverDelay` 는 **지연값**을 함께 지녀야 한다.
→ `Set<RetryTrigger>` 또는 associated value 를 가진 enum:
`RetryTrigger ∈ { serverDelay(seconds), authChange, entitlementChange, nextConnection, foreground, manual }`.
빈 집합 = 재시도 없음.

⛔ **`desired`/`confirmed`/`rejectionReason`/`retryPolicy` 만으로는 §6(재검증)과 §13.1(1)(김프)을 표현할 수 없다.** 아래 둘이 **같은 튜플**이 된다 —
`desired=true, confirmed=true, rejectionReason=nil` 인데
① snapshot 을 정상 수신한 상태 ② ack 은 받았지만 snapshot 을 **한 번도 못 받은** 상태.
tether 45초 후의 `suspect → revalidating → degraded` 도 거부가 없으므로 나타나지 않는다.
→ **per-topic `deliveryState`** 를 별도 축으로 둔다:

```text
deliveryState = neverReceived | healthy | suspect | revalidating | degraded
accessState   = allowed | disabled | authorizationDenied | unknownTopic
```

⛔ **두 축을 섞지 말 것.** 초안은 `disabled`/`authorizationDenied` 를 `deliveryState` 에 넣었는데,
그러면 `rejectionReason`·auth resolution 과 **중복**돼 판정이 갈린다. delivery 는 *데이터가 오는가*,
access 는 *받을 자격/가능성이 있는가* 다.

⛔ **그리고 축 개수를 세지 말고 _저장/파생_ 을 나눠라.** `rejectionReason`·`accessState`·`retryPolicy`
를 **셋 다 저장하면 서로 불일치**할 수 있다(같은 사실의 세 사본).

| 구분 | 필드 |
|---|---|
| 범위 | canonical 저장 |
|---|---|
| **배치 요청** | `controlState` · **`WholeRequestFailure?`** (4종) |
| **topic** | `desired` · `confirmed` · `receiveGeneration` · `deliveryState` · **`TopicRejection?`** (5종) |
| **연결/계정** | `authResolution` (topic 별로 복제하지 않는다) |

| 파생(계산) | 입력 |
|---|---|
| `accessState` | (`TopicRejection`, `authResolution`) |
| `rejectionRetry` | (`WholeRequestFailure`, `TopicRejection`, `authResolution`) — **거부에 대한 재시도** |
| `revalidation` | 아래 **6입력** — **무수신에 대한 재검증** |

⛔ **재시도와 재검증은 _다른 정책_ 이다. 하나로 합치면 45초 무수신을 표현할 수 없다.**
45초 무수신에는 `WholeRequestFailure` 도 `TopicRejection` 도 **없다** — 서버가 거부한 게 아니라
아무 말이 없는 것이다. 그래서 거부 3입력만으로는 `suspect → revalidating` 재구독을 도출할 수 없다.

- **`rejectionRetry`** — 서버가 **거부했을 때** 무엇이 자격을 다시 여는가(§4.1 표의 retryPolicy 열).
- **`revalidation`** — 서버가 **아무 말이 없을 때** 언제 다시 물어보는가(§6 의 조용한 재구독).

  ⛔ **`deliveryState` + `receiveGeneration` 둘로는 부족하다** — "45초가 지났나 / 이미 한 번 물었나 /
  연결은 살아 있나"를 판단할 수 없다. 필요한 입력:

  | 입력 | 무엇을 정하나 |
  |---|---|
  | `deliveryState` | 지금 의심 상태인가 |
  | `receiveGeneration` | 요청 이후 프레임이 왔나 |
  | `deadline` vs `now` | 45초가 실제로 지났나 |
  | `desired` · `confirmed` | 애초에 구독 의도가 있고 확인됐나 |
  | `revalidationAttempt`(또는 generation) | **1회 상한**을 넘었나 |
  | connection / lifecycle 상태 | 연결이 살아 있나, foreground 인가 |

초안이 per-topic 단일 `rejection` 하나만 입력으로 둔 것은 §4.0 의 두 층 구분과도, 위 이분과도 어긋났다.

⛔ **사유를 _문자열만_ 저장하면 정보가 사라진다.** `temporarily_unavailable` 은 `retry_after_seconds`
를 함께 싣는데(§4.1), 문자열만 남기면 `serverDelay(seconds)` 를 **재구성할 수 없다**.

⛔ **그렇다고 optional 필드 구조체(`rejection(reason, retryAfter)`)도 안 된다** — `invalid_token` +
`retryAfter` 같은 **불가능한 조합을 표현할 수 있다**. **tagged enum** 으로 타입이 조합을 강제한다:

⛔ **그리고 §4.0 의 _두 층_ 을 enum 에서도 지킨다.** 초안은 한 enum 에 전체-요청 오류
(`temporarilyUnavailable`·`invalidToken`)를 섞고 `invalidRequest`·`requestTooLarge` 는 빠뜨렸다.
서버(`topic_wire.py`)처럼 **분리**한다:

```swift
// 전체-요청 (4종) — 요청 전체에 적용. 배치의 모든 topic 이 영향받는다.
enum WholeRequestFailure {
    case temporarilyUnavailable(retryAfter: TimeInterval)
    case invalidToken
    case invalidRequest
    case requestTooLarge          // 실제 운영 경로는 frame 이 아니라 close 1009 (§4.1)
}

// per-topic (5종) — 그 topic 만.
enum TopicRejection {
    case topicsDisabled
    case topicUnavailable
    case unknownTopic
    case premiumRequired
    case krxEntitlementRequired
}
```

`WholeRequestFailure` 는 **배치 요청 단위**, `TopicRejection` 은 **topic 단위**로 저장한다
(위 arbiter 범위 구분과 같은 축이다).

⚠️ **`authResolution` 은 per-topic canonical 표 _밖_ 이다** — **연결/계정 범위**(토큰 refresh 진행
중인가, 확정 실패인가)다. topic 마다 복제하면 같은 사실의 N 사본이 되어 §4.2 가 막으려던 불일치가
되살아난다.

파생 필드는 저장하지 않는다 — 저장하는 순간 동기화 책임이 생기고, 그게 불일치의 원천이다.

배너(§6)와 파생 숫자 차단(§13.1)은 **이 축 하나를 공유**해 판정이 갈리지 않게 한다.

⚠️ **`invalid_token` 의 두 갈래는 wire reason 이 같다**(§4.1) — `rejectionReason` 만으로 구분되지
않는다. **auth resolution 상태**(refresh 진행 중 / 확정 실패)를 별도로 둬야 표의 두 행이 구현된다.

⛔ **`unknown_topic` 에서 `desired` 를 지우면 안 된다.** 버전 스큐나 순차 배포가 끝나도 **자동 복구할
근거가 사라진다**. 대신 `retryPolicy = {nextConnection}` 으로 이 연결 세대에서 억제하고, 재연결 시 한 번 더
시도한다 — 이러면 재시도 폭풍 없이 자연 복구된다.
같은 원칙이 `topics_disabled`·`topic_unavailable` 에도 적용된다(서버 재활성화 시 복구되어야 한다).

---

## 5. 조용한 실패 두 가지 — 서버가 고친다

클라가 **구조적으로 탐지할 수 없는** 실패다. 클라에 탐지 기계를 짓는 대신 원인 지점에서 고친다.

### 5.1 publish send 실패가 구독을 지운다

`topic_dispatcher.remove_websocket` docstring 이 스스로 적어 뒀다 —
*"main.py 의 disconnect 경로와 **publish 송신 실패 격리**에서 호출된다 … 이 메서드는
'연결이 죽었다'의 동의어가 아니다."*
**앱 레벨에서 명시적으로 닫는 곳은 `close(1008)`(인증) 하나뿐이다** — 16KB 초과는 transport 가
1009 로 닫지만 그건 앱 정책이 아니다.

→ 결과: **연결은 살아 있고 ping/pong 도 정상인데 그 연결의 모든 구독이 사라진다.** 영구 침묵.

**결정**: 서버가 send 실패 시 **소켓을 닫는다**. 탐지 불가능한 조건을 **이미 처리되는 재연결
경로**로 바꾼다. (전송 재시도 1회 후 close 도 허용 — 다만 "조용히 구독만 제거"는 금지.)

### 5.2 lease 만료가 조용하다

`leased_subscribers()` 는 **전송 직전에만** 만료를 거른다 — registry 에서 지우지도, 클라에
알리지도 않는다. iOS 는 `applyLeaseSchedule` 로 만료 전 재구독을 예약하고 실패 시
bounded retry(최대 3회)가 돌지만, **소진되면 그걸로 끝**이다.

⚠️ **조용한 topic 에서는 §5.1 의 close 도 구제하지 못한다** — publish 자체가 없어 send 실패도
없기 때문이다.

**결정: (A) 채택** (§13.1(2) 에서 확정). (B) 는 후속.
- **(A) 클라 hard-expiry** — lease 실제 만료 시각에 갱신이 확인되지 않았으면 **강제 재연결**.
  클라 전용, 결정적 테스트 가능, 기존 재연결 경로 재사용. **출시 범위 권장.**
- **(B) 서버 expiry sweeper** — 주기적으로 만료 lease 제거 + `reauth_required` 통지 또는 close.
  클라 버그까지 방어. **후속 권장.**

---

## 6. 45초 내부 복구와 사용자 경고를 분리한다

45초에 도달했다고 **즉시 배너를 띄우지 않는다**. 정상적인 짧은 침묵을 장애로 노출하게 된다.

```
45s 도달 → (조용히) 재구독 1회
        → 성공/값 수신 → 아무 일도 없었던 것처럼 복귀, 배너 없음
        → 실패 또는 불일치 확정 → degraded 배너 + 수동 재시도 노출
```

현재 MODE 2 에서는 배너가 렌더조차 되지 않는다(`ConnectionStatusView` 가 `connectionState` 만
본다) — **자동도 수동도 없는 상태**다. 게이트를 전달 상태까지 포함하도록 넓힌다.


### 6.1 45초 동시 재구독 폭주 완화

§6 의 조용한 재구독은 **확정**이다. 그것이 만드는 동시 폭주의 완화도 같은 등급이어야 한다 —
이 절은 ADR-041 의 `[제안·결정 대기]` **파생 숫자 숨김 정책** 제안의 채택 여부와
무관한 **확정 계약**이다.

⛔ **즉시거절 형태의 bulkhead 를 쓰지 않는다.** 이 리포는 그걸 **이미 기각**했다 —
`auth_executor` docstring: *"즉시거절 semaphore 는 별도로 기각됐다: 배포 재연결은 평균 유입이
낮아도 **동시 도착** 이라 1초면 빠질 큐를 대량 거절한다."*
⚠️ 그리고 그 executor 는 **큐 상한이 아니다**(`SimpleQueue` 무제한) — 같은 docstring 이
*"자원 상한 완료 라고 쓰지 말 것"* 이라 못 박는다.

**클라 — 재구독을 흩뜨리는 쪽이다.**
- 재연결·재구독에 **jitter** 를 건다.
- **재시도 상한** 을 둔다.
- 실패 후 **클라 재시도 cooldown** 을 둔다.

**서버 — 동시 도착을 흡수하는 쪽이다.**
- topic 별 **single-flight / cache**.
- **bounded wait** — 큐에서 기다리게 하되 무한이 아니다.
- **DB/Redis I/O 자체 상한** (§3.3 의 절대 deadline 만으로는 부족하다).
- **bounded wait/queue 후 예산이 소진됐을 때만 close 1013**.
- **서버 실패 cooldown** — ⚠️ **실패 fan-out 이 폭주를 재동기화한다**: single-flight 하나가 실패해
  대기 소켓을 동시에 닫으면 재연결이 다시 정렬된다. 서버 실패 cooldown 과 클라 jitter 가
  **함께 있어야** 완화가 성립한다.

---

## 7. 롤백 레버를 대체한다

`TOPIC_V2_RELEASE_RUNBOOK.md` 의 2차 롤백은 `TOPIC_DISPATCHER_ENABLED=false` + 재기동이고,
그것이 **화면에 작용하는 유일한 기전이 지금의 45초 legacy revert** 다. §2.1 로 그걸 걷어내면
운영자가 kill 을 눌러도 화면이 바뀌지 않는다.

⚠️ 다만 현 레버가 "작동"한다는 건 화면이 **바뀐다**는 뜻일 뿐, 바뀐 결과는 USDT 탭에 은행 USD
시세라는 **무관한 데이터**다. 롤백의 목적("새 경로가 오도하는 것을 멈춘다")을 지금도 달성하지
못한다. 그러므로 되돌리는 게 아니라 **대체**한다.

**결정**: `topics_disabled` 를 받으면 클라는 **명시적 비활성 상태**로 전환한다 — 마지막 값을
지우고 "실시간 시세를 일시적으로 제공할 수 없습니다"를 표시한다. 구독 의도는 §4.2 대로 보존한다.
이로써 서버 flag off 가 다시 화면에 도달한다.

⚠️ **이건 "rollback" 이 아니라 전 사용자 실시간 기능을 정직하게 멈추는 safety stop 이다.**
개별 FX/KRX 레버가 필요하면 `topic_unavailable` 전이(§4.1)를 함께 써야 한다.

### 7.1 purge 범위 — **메모리만 지우면 부족하다**

topic 값은 메모리뿐 아니라 **`cached_topic_rates` 로 디스크에 영속화되고 앱 시작 시 복원**된다.
그리고 `stop()` 은 store 만 비우고 디스크 캐시를 지우지 않는다. 지우지 않으면 재실행 시
**비활성이어야 할 값이 되살아난다**.

| 범위 | `topics_disabled` | 인가 거부(§4.1 per-topic) |
|---|---|---|
| tether/fx/KRX in-memory store + `received` 상태 | 전부 | 해당 topic 만 |
| `cached_topic_rates` 디스크 + 메모리 복원본 | 전부 | 해당 topic 만 |
| 파생 상태(live-tail / 김프 / 알림 현재가) | 전부 | 해당 topic 파생만 |

---

## 8. 데이터 나이 — 지금은 잴 수 없다

**"N분 전" 라벨을 새로 만들지 않는다.** 클라에 일관된 per-source 관측 나이가 저장되지 않는다.

- 서버는 rate 가 같으면 **`rate_changed_at` 을 보존**한다(`latest_rates_cache.py`) — `seen_at` 만 전진.
- 클라 `TopicSnapshotMerger` 는 `mergeAt = rateChangedAt ?? timestamp` 로 비교해
  `mergeAt <= existing.mergeAt` 이면 **entry 를 통째로 버린다**.
- → 가격이 평평하면 5초마다 오는 새 `seen_at` 이 전부 폐기되고, store 의 timestamp 는
  **마지막 가격 변동 시각에 동결**된다.
- 게다가 DB fallback 서빙에서는 `rate_changed_at` 이 없고 `timestamp` 자체가 변경 시각이라
  **같은 필드의 의미가 뒤집힌다**.

### 8.1 그런데 이미 살아 있는 age gate 가 있다

`GraphV2Section.liveFreshnessThreshold` 가 `SourceRate.timestamp` 기준 600초(hana 1200초)로
live-tail 을 **버린다**. 그 함수 주석이 스스로 실토한다 —
*"timestamp=last-change(merge unchanged skip + 서버 SET-only)라 임계가 너무 짧으면 calm flat 을 과도 skip."*

즉 나이가 아닌 값을 나이로 쓰고, 임계를 길게 잡아 무마했다.

**결정 — 이번 출시에서는 gate 를 유지한다(제거하지 않는다).** 초안은 "같은 슬라이스에서 제거 또는
교체"라고 썼으나 **그건 위험하다**:

- 그냥 제거하면 **주말 FX 종가나 장마감 KRX 값을 현재 시점의 live tail 로 ingest** 하게 된다.
  bridge 는 source timestamp 를 검사한 뒤 rate 만 `ingestLive` 에 넘기므로, 검사를 빼면 오래된
  값이 지금 값으로 그려진다.
- "전달 상태 기준으로 교체"도 안 된다 — 정상 휴장 중에는 연결·lease 가 **healthy** 라 오래된 값을
  현재 tail 로 연장하는 문제가 그대로 남는다.

→ **의미 재설계는 후속**(§8.2 의 관측 나이가 생긴 뒤). 이번엔 gate 를 남기되 **"이건 데이터 나이가
아니라 last-change 경과다"** 를 주석과 이 문서에 명시해 다음 사람이 나이로 오해하지 않게 한다.

### 8.2 후속

일관된 나이가 필요해지면 둘 중 하나 — merger 가 값을 버릴 때도 `seen_at` 은 갱신하도록
고치거나(클라 전용, 단 DB-fallback 분기 필요), 서버가 `observed_at` 을 추가한다.
**출시 필수가 아니다.**

---

## 9. 반증된 서술 정정 목록 (같은 슬라이스에서 처리)

문서-코드 드리프트가 **실제 분석 오류를 만들었다**. 아래를 고치지 않으면 같은 오판이 재발한다.

| 위치 | 현 서술 | 실제 |
|---|---|---|
| `WebSocketService.swift` ~822 | "`performTopicCommand` 의 `catch` 는 `forgetPending` 만 하고 재시도하지 않는다 … 재연결 전까지 조용히 미구독" | catch 가 **bounded retry 한다**(`shouldRetryCommandFailure` denylist + 최대 3회). 그 주석이 열어 둔 항목이 **이미 구현됐는데 주석만 안 고쳐졌다**. 실제 공백은 **재시도 소진 이후** |
| `TOPIC_V2_RELEASE_RUNBOOK.md` MODE 2 | "45s 초과 시 자동으로 legacy 표시"(합격 기준) | §2 로 폐기 |
| 같은 문서 rollback 절 | "legacy 가 graceful degrade 하므로 서비스 중단 아님" | 테더는 거래소 5 + KRX 가 **전부 사라진다**. degrade 가 아니라 핵심 기능 상실 |
| 같은 문서 (그래프) | "FX 그래프 live-tail 은 legacy 읽고 테이블은 topic" | 반대다 — `GraphV2LiveBridge` 가 `fxTopicRates` 를 읽는다 |
| `DECISIONS.md` ADR-039 요약 | "신규 앱 legacy fallback 금지" | 원문은 "legacy **anon** fallback 금지" — 금지 **근거**가 인가라는 사실이 지워졌다 |
| `REALTIME_V2_CLIENT_GUIDE.md` | "화면을 건드리지 않으면 **영원히** stale 표시가 안 됐다" | 리허설 관측은 **90초**다. 기전상 그럴듯해도 관측보다 강한 단정 |
| `DECISIONS.md` ADR-038 D2 | MODE 2 revert 전제 | **제약은 유지**("KRX 수신은 tether 전달 생존의 증거가 아니다"는 여전히 참이고 재검증 오판 방지에 필요). **목적만** 갱신 |

---

## 10. 부수로 고칠 것

- **알림 시트 범위 검증 우회** — `SourceAlertAddSheet` 의
  `guard let threshold, let range = validRange else { return true }` 는 fail-**open** 이다.
  stale 로 현재가가 nil 이면 ±50% 가드가 **조용히 꺼진 채 임의 임계값 저장이 통과**한다.
  §2 로 last-known 이 공급되면 대부분 복구되고, 남는 건 **한 번도 수신 못 한 cold-start** 다.
  그때는 저장을 막되 **사유 표시 + 탈출구**(값 직접 확인 등)를 함께 둔다. 서버는 현재
  `threshold > 0` 만 검증하므로 클라가 유일한 방어선이다.
- **usd 탭 그래프의 KRX 결합** — fx stale 시 guard 가 뒤의 KRX tail 까지 버린다.
  KRX 는 "freshness 비결합"이 명시 정책인데 그래프만 어긋난다.

---

## 11. 검증 (실기기)

리허설과 **같은 조작**으로 판정한다 — 조작이 다르면 결함이 숨는다.

1. **dispatcher 는 ON 인 채 tether publisher/trigger 만 침묵** + **무조작 90초**
   → 거래소 5행 **값 유지** / 재구독 정확히 1회 / 배너는 실패 확정 후에만.
   ⛔ 이 항목을 `TOPIC_DISPATCHER_ENABLED=false` 로 재현하면 **2번과 동시에 만족 불가**다
      (그건 `topics_disabled` → purge 경로다). 두 시나리오는 **다른 조작**이다.
2. **global dispatcher off + 재기동** → `topics_disabled` 수신 → 명시적 비활성 + purge(§7.1)
   + **재시도 0** + 서버 재활성화 후 복구
3. 백그라운드 45초 초과 후 foreground → 즉시 재평가
4. **lease 15분 초과** 연결 유지 → 재인증 성공 시 지속 / 실패 시 §5.2 (A) 발화
5. 평일 장중 FX live merge + 주말 무발행이 **장애로 표시되지 않음**
6. 구버전 앱 legacy 무영향
7. dev 서버 flag **on/off 양쪽**

⚠️ 판정은 rc + `Executed N tests` + `TEST SUCCEEDED` **셋을 함께** 본다. Release 빌드로
`#if DEBUG` seam 누수도 확인한다.

---

### 11.1 테스트 매트릭스 — **보존할 사용자 행위** 기준

⛔ **삭제 예정 경로에 테스트를 달지 않는다.** `ExchangeRateResponse`·`onRatesReceived`·legacy cache
동작을 지금 고정하면 **곧 버릴 테스트**를 만드는 것이다. 고정할 것은 **cutover 후에도 참이어야 하는
사용자 행위**다 — 그 테스트는 replacement 를 검증하는 데 그대로 쓰인다.

| # | 보존할 행위 | 지금 쓸 수 있나 |
|---|---|---|
| B1 | topic cold-start 에서 **불필요한 blank/reflow 가 없다** | ✅ **이미 있다** — `testUsdtDisplay_coldStart_returnsEmpty_noCachedNoPartialLegacy`. 감사만 |
| B2 | 오프라인/재기동에서 **topic last-known 으로 복원**된다 | 🔶 **A1 과 함께** — 현재는 cache roundtrip·저장만 있고 *재기동 후 표시*는 미검증 |
| B3 | **legacy 프레임을 무시해도 앱 lifecycle 이 정상**이다 | 🔶 **A1 과 함께** — 현행 코드에선 **통과 불가**(`onRatesReceived` 가 `appState = .connected` 를 직접 세팅) |
| B4 | 무료 티어가 `ExchangeRate` **타입**으로 계속 디코드된다 | ✅ **이미 있다** — 무료 스냅샷 디코드 테스트가 잠근다. 감사만 |
| B5 | 45초 무수신 → **조용한 재구독** → 실패 확정 시에만 배너 | 🔶 §6 구현과 함께 |
| B6 | **DXY topic 이 live tip 을 공급**한다 | 🔶 `dxy:spot` 구현과 함께 |
| B7 | **은행 알림 현재가가 FX topic 을 쓴다** | 🔶 A2 신설과 함께 (지금은 legacy 만 본다 = 현행 버그) |
| B8 | `topics_disabled` → purge → **재활성화 시 복구** | 🔶 §7 구현과 함께 |
| B9 | lease 만료 → **hard-expiry 재연결** | 🔶 §5.2(A) 구현과 함께 |
| B10 | 김프는 **확정된 전달 실패에서만** 숨는다(정상 sparse 에선 보인다) | 🔶 §13.1(1) 구현과 함께 |

⛔ **정정: 지금 새로 쓸 테스트는 사실상 없다.** B1·B4 는 **이미 잠겨 있어 감사만** 하면 되고,
B2·B3 는 **A1(AppState 대체) 구현과 같은 커밋**에 들어가야 한다 — 특히 B3 는 legacy 가 `AppState` 를
직접 갱신하는 현행 구조에서 **원리적으로 통과할 수 없다**.
→ 즉 **"테스트부터 시작"이라는 착수 경로는 없다.** 계약을 닫는 것이 실제 다음 단계다.

---

## 12. 순서와 게이트

```
서버 ① §2.0(a) WS FX/USDT 인증 + premium 강제 (ADR-039 Stage A)   ← 새 최우선
     ② §2.0(b) **DXY topic 신설**(계획 4b) — 이게 없으면 legacy 이탈이 불가능
     ③ §5.1 send 실패 시 close
     ④ §3.3 **최소 계약** — 정적 unavailable 만 ack 전, 나머지는 등록→ack→전송, 실패는 close(1013/1011)
  → iOS ⑤ §2 legacy 소비 **전면** cutover (startup·WS parser·은행/김프/비교 알림 + DXY)
        ⑥ last-known + §4 **9종 2층** 사유 상태기계 (topic_unavailable·unknown_topic 신규 배선)
        ⑦ §6 조용한 재구독 → 실패 확정 시 배너 / §3.3 수신 세대 기반 snapshot deadline
        ⑧ §5.2 (A) lease hard-expiry
        ⑨ §7 purge(메모리 + `cached_topic_rates` + 파생 상태) / §10 알림 cold-start fail-close
  → §9 문서 정정 + 런북 합격 기준 교체
  → §11 실기기 리허설 (auth 매트릭스 포함: 무토큰 / non-premium / premium / KRX entitlement / revoke)
  → ⛔ Release arming (별도 GO)
  → ⛔ 서버 TOPIC_DISPATCHER_ENABLED=true (별도 GO)
```

**phased 로 미루는 것**: §5.2 (B) 서버 sweeper / §8.1 graph age 의미 재설계 /
usd 탭 KRX tail 결합 해소 / 역사 ADR 정리(단, **운영에 쓰는 런북과 현재 코드 주석은 출시 전**).

**Release arming(`TOPIC_V2_RELEASE_ON`)은 현재 `project.pbxproj` 의 미커밋 변경 한 건**이고
**사용자 소유**다. 자동화가 커밋하지 않는다. 정식 커밋 + Release archive 확인은 **마지막 게이트**이며
명시적 GO 를 받는다. 그렇지 않으면 clean checkout 의 Release 는 legacy 기본값으로 빌드된다.

---

## 13. 결정

### 13.1 확정 / 제안

⛔ **(1) 은 아직 _제안_ 이다 — 사용자 결정 전이다.** (2)(3) 은 확정.

**(1) [제안·결정 대기] 김프 등 파생 숫자는 _시간_ 으로 숨기지 않는다 — _전달 이상이 확정된 경우_ 에만 숨긴다.**

초안과 앞선 설계안은 "두 다리의 나이 ≤120초" 같은 **시간 게이트**를 제안했다. **채택하지 않는다** —
Gopax 정상 tick 간격이 ~180초라 그 게이트는 **평상시에 김프를 상시 숨긴다**(지키려던 기능을 죽인다).
그리고 §8 대로 **관측 나이를 잴 신호 자체가 없다**.

→ **재구독·재연결이 실패해 전달 이상이 확정된 경우에만** 파생 숫자(김프/비교 spread)를 숨긴다.
**원시 last-known 행은 경고와 함께 유지**한다.

**⛔ 이 수용은 _조건부_ 다 — 조건이 안 서면 수용하지 않는다.**
클라가 못 보는 실패를 **아무도 안 보면** 그건 수용이 아니라 방치다. **arming 차단 게이트 3개**:

1. **publisher health / SLO** — §3.4 계약이 구현·검증됐는가.
2. **safety-stop 리허설 실측** — `topics_disabled` → purge → 재활성화 복구를 실기기로 돌려 본다
3. **45초 동시 재구독 폭주 완화** — §6.1 계약이 구현·검증됐는가.

클라 status signal(서버 health 를 클라에 전달)은 후속으로 둔다.
⛔ **셋 중 하나라도 미루면 수용으로 보지 않는다.**
⚠️ **위 1~3 중 하나라도 출시 후속이라면 잔존 위험을 수용해서는 안 된다** — 그 경우 김프 표시 정책을
다시 논의해야 한다.

⚠️ **한계를 명시한다 — "파이프가 죽었나"가 항상 잴 수 있는 건 아니다.** §3.3-3 대로 **정상 initial
snapshot 이후의 publisher 사망은 클라가 판별할 수 없다**. 따라서 숨김 조건은 **클라가 확정 가능한**
전달 실패(재구독·재연결 실패, close, 명시적 거부)에 한한다. 그 밖의 조용한 사망에서는 **김프가
last-known 조합으로 계속 보일 수 있다** — 이건 **제품이 수용하는 잔존 위험**이고, 없애려면 서버
health 결과를 클라에 전달하는 status signal 이 필요하다(후속).

**(2) lease — 출시는 (A) 클라 hard-expiry, (B) 서버 sweeper 는 후속.**

계약에 함께 잠글 것:
- topic 별 **절대 expiry** 를 추적한다
- **새 `lease_id` 를 포함한 유효 ack 만** 만료를 갱신한다(낡은/미지 request 의 ack 은 해제 불가)
- 만료 topic 은 `confirmed` 에서 제거하되 **`desired` 는 보존**(§4.2)
- **연결/lease 세대당 강제 reconnect 1회**, 그 뒤엔 기존 backoff
- **foreground 복귀 시 즉시 expiry 재평가**

**(3) nginx — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후.**

실측: zone 은 이미 정의돼 있고(`limit_req_zone` / `limit_conn_zone`), **`location /api/` 에만**
`limit_req` + `limit_conn` 이 걸려 있다. **`location /ws` 에는 둘 다 없다.**

→ handshake 폭주는 `limit_req` 로 막는다. 다만 **IP별 `limit_conn` 은 모바일 캐리어 NAT 위험이 크다**
(한 IP 뒤에 다수 사용자) → **계측 후 결정**. 이 항목은 "신설 vs 위험 수용" 이분법이 아니었다.

### 13.2 미결

1. ~~`dxy:spot` / `dxy:futures` 분리~~ → **확정: 이번 출시는 `dxy:spot` 만**(로드맵 4b 그대로).
   근거: premium live bridge 가 **spot 만** `dxyLive` 로 보충하고 `dxy_futures` live state 는 없다
   → futures topic 은 legacy 이탈에 **불필요**, phased.
2. ~~Stage A 범위~~ → **미결이 아니다.** `FREE_TIER_ACCESS_MODEL_PLAN` D2 가 이미 확정했다 —
   **비-KRX 최신 topic = Firebase 인증 + premium / KRX = premium + entitlement**.
   `dxy:spot` 까지 포함한 **명시적 fail-closed 정책표**로 구현하면 된다(미지정 topic 은 통과 불가).
   ⚠️ 다만 **보장 범위 한정은 ADR 요약에 명시**한다 — Stage A 는 **신규 앱 계약 준수**이지 서비스
   전체의 페이월 우회 제거가 **아니다**(구버전용 익명 legacy 는 Stage B 까지 남는다).
