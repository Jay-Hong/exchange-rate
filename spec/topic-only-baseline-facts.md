# 검증된 사실 baseline (v6 — repo 정본)

> ⚠️ **이 파일이 정본이다.** 이전 사본은 세션 scratchpad 에 있었고 세션 종료와 함께 사라진다 —
> 다른 사람·다음 세션이 참조할 수 없으므로 리포로 옮겼다.

> **참조 revision — 이 사실들은 아래 시점에서만 유효하다**
> - 참조 commit 은 **`spec/topic-only.lock.json` 의 `pinned_commit`** 이 정본이다
>   (여기 값을 복제하지 않는다 — 단축 해시 복제로 계약이 어긋난 실측이 있다)
> - ⚠️ iOS 미커밋 변경 `FXi.xcodeproj/project.pbxproj` (사용자 소유 arming) 은 **범위 제외**

> ⛔ **run 중 수정 금지**(규율이지 강제가 아니다). agent 는 읽은 전체 SHA 를 결과에 보고한다.
> ⚠️ **"동결"이라 부르지 않는다** — `chmod 444` 는 git 이 보존하지 않고, 기대 SHA 도 같은 사람이
> 고칠 수 있는 lock 에 있다. 진짜 기준점은 **승인된 commit/tag** 이며 현재 파일들은 untracked 다.
> ⛔ **`TOPIC_ONLY_DELIVERY_CONTRACT.md` 는 검토 대상 Draft 이지 진실 소스가 아니다** —
>    그걸 근거로 인용하면 순환 검증이다.
> ⚠️ **증거 종류를 섞지 않는다.** 아래 4분류는 신뢰도와 갱신 규칙이 다르다:
>
> | 표기 | 뜻 |
> |---|---|
> | **[코드]** | 소스에서 직접 읽음. file:line 명시 |
> | **[추론]** | 코드 구조에서 도출. 코드가 그렇게 적혀 있지는 않음 |
> | **[결정]** | 과거에 기록된 결정(주석·문서). 사실이 아니라 합의 |
> | **[운영]** | 런타임/배포 상태. 코드로 확인 불가, 시점 의존 |

---

## A. legacy 노출

- **A1 [코드]** `exchange-rate/app/legacy_policy.py:35` — `LEGACY_RATE_SOURCES` = investing + 은행 9곳.
  같은 파일 docstring 에 doctest: `should_include_source_in_legacy_rates("upbit","usdt-krw") → False`.
  ⇒ legacy 에 USDT 거래소·KRX 없음.
- **A2 [코드]** `exchange-rate/app/main.py:1216` — `/api/rates/{currency}` 가 usdt-krw 에 410 + `use_topic`.

## B. publish 의미론

- **B1 [코드·부정]** topic data-plane heartbeat·주기적 재발행 **없음**.
  증거(부정 사실은 행 번호가 없다 — 명령·범위·결과로 단다). ⚠️ 키워드 검색만으로는 약해서
  **주기적 실행 원시자**로 다시 확인했다:
  `rg -n 'create_task|while True|sleep\(|IntervalTrigger|add_job|Timer' app/topic_dispatcher.py app/fx_topic_publisher.py`
  → **0건**.
  ⚠️ **범위 한정**: 이 두 파일 안에 없다는 뜻이다. 외부(scheduler 등)가 이들을 주기 호출하는
  가능성까지 배제하려면 호출자 검사가 추가로 필요하다. transport ping/pong 은 별개로 존재.
- **B2 [코드]** `app/latest_rates_cache.py:480` — USDT coalesce = `same rate + same 5s bucket → skip`.
  ⇒ 가격이 평평해도 새 5초 버킷 tick 이면 SET → publish.
- **B3 [코드]** 같은 파일 — KRX 는 위 coalesce 를 **tick writer 경로에서만** 적용.
  `KRX_REDIS_TICK_WRITE_ENABLED` 코드 기본값 false.
  **B3-op [운영]** 운영은 2026-05-26 활성(`CLAUDE.md` 기록). ⚠️ 코드가 아니라 문서 기록이다.
- **B4 [코드]** `app/topic_wire.py:30` `WHOLE_REQUEST_ERRORS`(4종) /
  `app/topic_wire.py:40` `PER_TOPIC_ERRORS`(5종) — 오류 어휘가 **두 층**.
- **B5 [코드·부정]** ack 반환 dict 에 `server_time` **없음**.
  증거: `rg -c 'server_time' app/topic_wire.py` → **0건**.

## C. 인가 (현재 구현 상태)

- **C1 [코드]** 익명(미식별) subscribe 의 처리는 **`WS_TOPIC_AUTH_STAGE` 에 따라 갈린다**
  (기본값 `compatibility`). 한 파일만 봐서는 증명되지 않아 네 계층을 함께 인용한다:
  stage 정의·엄격 파서·코드 기본값 `app/config.py:645-688` · **정책 정본**
  `app/topic_policy.py:244-282`(`plan_anonymous`) · 그 위임 wrapper
  `app/topic_auth_rollout.py:286-301` · 필터 호출과 등록 `app/topic_dispatcher.py:555-577` ·
  production 주입 `app/main.py:312-320`.
  ⚠️ `0cfe474` 이전에는 stage 별 분기가 rollout wrapper 안에 있었다 — 지금은 정책표
  모듈이 정본이고 wrapper 는 주입받은 FX 집합을 넘겨 위임만 한다(익명·식별 두 축이
  `reject_anonymous_fx` 에서 값이 갈리므로 함수가 둘이다).
  - `compatibility` — 무료 topic 등록 + snapshot, ack 없음(구 동작 보존).
  - `reject_anonymous_fx` — 무료 집합에서 **canonical FX 만** 조용히 제외. USDT 는 유지되고
    FX-only 요청은 등록·응답 모두 0이다. 익명 unsubscribe 는 stage 와 무관하게 기존 경로를
    그대로 탄다(`app/topic_dispatcher.py:827-853`).
  - `enforce_authenticated_premium` — 익명 subscribe topic 을 전부 조용히 제외. unsubscribe 는 유지.
  ⚠️ 세 stage 모두 **익명 subscribe 시도**와 형식 검증을 통과한 **미검증 token-bearing 후보**를
     서로 다른 축으로 계측한다. 두 계측은 `TOPIC_DISPATCHER_ENABLED` 검사보다 앞이라
     (`app/topic_dispatcher.py:509-525`) flag-off 운영 상태에서도 값이 쌓인다. token-bearing 축은
     Firebase 검증 전 관측이라 인증 사용자나 실제 RevenueCat 호출 수가 아니다
     (`app/topic_auth_rollout.py:237-274` · snapshot `app/topic_auth_rollout.py:317-361`). 정책 topic과
     현재 availability 기반 최종-stage RC 후보 topic은 production 기동 시 한 번 계산해 주입한다
     (`app/main.py:302-320`).
- **C2 [코드]** `app/topic_initial_snapshot.py:97` — `per_user_gated_snapshot_topics()`.
  entitlement 전용 snapshot 판정 대상은 **KRX 뿐**이다. 이 집합은 FX/USDT premium 범위를
  나타내지 않는다.
- **C3 [코드]** `exchange-rate/app/main.py:3053` `@app.get("/api/v2/topics/snapshot")` —
  `app/main.py:3088` `verify_firebase_token(request)` → `app/main.py:3091`
  `require_premium(user_id, allow_empty=False)`. ⇒ REST twin 은 premium 을 **코드로 강제**한다.
  ⚠️ 초안은 이걸 [결정]으로 적어 "현재 구현 상태" 절에 뒀는데 **분류가 어긋났다** — 코드 사실이다.
- **C4 [코드]** `app/topic_policy.py:87-93` — 인가 **정책표**(리터럴). 비-KRX = `PREMIUM_ONLY`,
  KRX = `PREMIUM_AND_ENTITLEMENT`. `app/topic_policy.py:285-330` 이 stage 별로 partition 을
  파생하고, `compatibility`·`reject_anonymous_fx` 에서는 비-KRX 가 identity-only 로 남는다.
- **C5 [코드]** `app/topic_authorization.py:372-402` — coordinator. RC 는 요청당 **≤1회**,
  entitlement 는 premium 승인 뒤 KRX 요청이 있을 때만 **≤1회**. `Unavailable` 은 전체-요청,
  `Denied` 는 per-topic 이다.
  **C5-inf [추론]** ⇒ `enforce_authenticated_premium` 에서는 authorizable topic 이 하나라도
  있는 인증 subscribe 마다 RevenueCat 왕복이 1회 생긴다 — WS 경로는 cache-free 다
  (stale fallback 은 REST 전용 `verify_premium_status` 안에만 있다).

## D. 실패 경로

- **D1 [코드]** `app/topic_dispatcher.py:151` `remove_websocket` — docstring:
  *"publish 송신 실패 격리에서 호출된다 … '연결이 죽었다'의 동의어가 아니다"*.
- **D2 [코드]** `app/topic_dispatcher.py:211` `leased_subscribers` — 만료 lease 를
  **전송 직전에만** 필터. registry 제거·클라 통지 없음.
  ⚠️ 이 함수는 자신을 *"모든 발행 경로가 공유하는 단일 게이트"* 라고 적지만, `882d92b`
  이전에는 **initial snapshot 경로가 우회**했다(그 모듈에 `lease` 참조 0건). 지금은 D6 이
  그 경로를 같은 함수에 태운다.
- **D3 [코드]** `app/topic_initial_snapshot.py:341-350` — build 실패를 `logger.warning` 후 **격리**,
  연결 유지. `None`(flag off)도 조용히 skip.
- **D4 [결정]** `app/topic_dispatcher.py:367` §8-B-term —
  *"식별된 요청은 반드시 종결된다 … 종결 프레임 하나 **또는 연결 종료**"*.
- **D5 [코드]** 같은 파일 — `registry.register(...)` 가 ack send 보다 **먼저**. outbound 직렬화 없음.
- **D6 [코드]** `app/topic_initial_snapshot.py:363-370` — initial snapshot 도 **전송 직전**에
  `leased_subscribers(topic)` 멤버십을 다시 본다(`882d92b`). 게이트에 걸리면 **해당 topic skip**
  이고 연결 실패가 아니다.
  **D6-inf [추론]** ⇒ 검사가 build **뒤**여야 하는 이유는 `_build_snapshot_sync` 가 `to_thread`
  로 돌고 **인증 wire deadline 밖**(상한 없음)이라, 위험한 창이 build→send 구간이기 때문이다.
  ⛔ `registry.get_lease(...) is None` 으로 자체 판정하면 **무토큰(§E1) 구독**과 **등록 소멸**이
  구분되지 않아 취소된 구독에 데이터가 나간다 — 그래서 구독자 집합에서 출발하는 함수를 쓴다.

## E. 부하

- **E1 [코드]** `app/database.py:30` — PostgreSQL `pool_size=3`, `max_overflow=2` (**최대 5**).
  주석: *"RDS db.t4g.micro 메모리 절약"*.
- **E2 [코드]** `app/topic_initial_snapshot.py:319-335` —
  `for topic in topics: ... await asyncio.to_thread(subscribe_load.timed_call, …, _build_snapshot_sync, topic)`.
  **E2-inf [추론]** ⇒ 연결당 **순차**이므로 순간 동시 job ≈ 연결 수 N, 총작업량 N×M.
  (코드가 이렇게 적어 두지는 않았다 — 루프 구조에서 도출)
- **E3 [결정]** `app/auth_executor.py:8` docstring — *"즉시거절 semaphore 는 별도로 **기각**됐다:
  배포 재연결은 평균 유입이 낮아도 **동시 도착** 이라 1초면 빠질 큐를 대량 거절한다."*
  같은 docstring: *"이것은 큐 상한이 아니다"*(`SimpleQueue` 무제한), *"자원 상한 완료 라고 쓰지 말 것"*.
- **E4 [코드·부정]** `nginx/conf.d/default.conf` `location /ws` 는 **80~99행** 블록이고
  그 안에 `limit_req`·`limit_conn` **없음**(블록 전체를 훑어 확인). `location /api/` 에는 둘 다 있고
  zone 정의는 파일 상단에 존재.

## F. 클라이언트 현재 동작

- **F1 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:283` `recomputeFreshness` —
  불리언 `tetherIsFresh`/`freshFxAssets` **만** 갱신. 재구독 호출 없음.
- **F2 [코드]** `ios/FXi/Services/WebSocketService.swift` — `resendSubscriptions()` 호출처 **2곳**
  (911, 1044 = 연결 수립·foreground 복귀).
  **F2-inf [추론]** ⇒ **45초 재구독은 현재 동작이 아니다**(도입하려는 설계다).
- **F3 [코드]** `ios/FXi/Services/WebSocketService.swift:402` `takePending` —
  `topicRequestTimeoutTasks.removeValue(...)?.cancel()`.
  **F3-inf [추론]** ⇒ ack 수신 즉시 20초 watchdog 소멸 → 그 뒤 build 에 클라 상한 없음.
- **F4 [코드]** `ios/FXi/Services/WebSocketService.swift:220` `sendTopicCommand` — subscribe 는 **배치**(한 요청에 여러 topic).
- **F5 [코드]** `WebSocketService` catch — `shouldRetryCommandFailure`(denylist) + 최대 3회
  bounded retry. **공백은 재시도 소진 이후**.
- **F6 [코드·부정]** `confirmedTopics` 와 `subscribedTopics` 를 **비교하는 코드 없음**.
  증거: ⚠️ "같은 줄에 없다"는 다중 행 비교·helper 를 배제하지 못해 **약하다**. 그래서
  `subscribedTopics` **전 참조 13곳(47·202·210·387·391·429·464·659·754·809·826·833·874)을 열거해 읽었다**.
  387/391 은 *의도*와의 재대조, 659 는 `premiumGatedTopics` 와의 교집합, 나머지는 선언·삽입·삭제·주석·
  재전송이다 — `confirmedTopics` 와 대조하는 곳은 **없다**.
  ⚠️ 826 주석이 같은 주장을 하지만 그 주석의 **다른 부분(bounded retry 서술)은 stale** 이므로
  주석이 아니라 위 전수 열거를 근거로 삼을 것.
- **F7 [코드]** `applyLeaseSchedule` — 만료 전 재구독 예약(jitter 포함).
  **F7-inf [추론]** 실패 시 F5 재시도, 소진되면 hard-expiry 집행 **없음**.
- **F8 [코드]** `ios/FXi/Services/TopicSnapshotMerger.swift:38` —
  `mergeAt <= existing.mergeAt` 이면 entry 를 버린다.
  **F8-inf [추론]** 서버가 rate 불변 시 `rate_changed_at` 보존(B2 파일) ⇒ 평평하면 store timestamp 가
  마지막 변동 시각에 **동결**.
- **F9 [코드]** `ios/FXi/Views/Components/GraphV2Section.swift:153` `liveFreshnessThreshold` —
  600초(hana 1200초). 주석이 *"timestamp=last-change … calm flat 을 과도 skip"* 이라 자인.
- **F10 [코드]** `ios/FXi/Models/AppState.swift:13` — `.connected(rates: [ExchangeRate])` 등
  **legacy 배열이 enum payload**.
- **F11 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:505` `rates(for:)` →
  `appState.rates` 직결. `ios/FXi/Views/Components/AlertAddSheet.swift` 가 이걸 쓴다(`baseRates` 미경유).
- **F12 [코드]** `ios/FXi/Services/WebSocketService.swift:1073` — DXY 는 legacy envelope 의 `indices` 로 수신(`onIndicesReceived`).
- **F13 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:623` `func usdtDisplayState` —
  최외곽 게이트 = `if RealtimeV2Config.isTetherTopicEnabled`, 그 else 는 `sourceRates()`.
- **F14 [코드]** `ios/FXi/Utils/RealtimeV2Config.swift:32` `isTetherTopicEnabled` —
  Release 기본값 topic **OFF** (`#if DEBUG` / `#elseif TOPIC_V2_RELEASE_ON` / else false).
