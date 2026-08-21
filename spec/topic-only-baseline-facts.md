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
- **A2 [코드]** `exchange-rate/app/main.py:1265` — `/api/rates/{currency}` 가 usdt-krw 에 410 + `use_topic`.

## B. publish 의미론

- **B1 [코드·부정]** topic data-plane heartbeat·주기적 재발행 **없음**.
  증거(부정 사실은 행 번호가 없다 — 명령·범위·결과로 단다). ⚠️ 키워드 검색만으로는 약해서
  **주기적 실행 원시자**로 다시 확인했다:
  `rg -n 'create_task|while True|sleep\(|IntervalTrigger|add_job|Timer' app/topic_dispatcher.py app/fx_topic_publisher.py`
  → **0건**.
  ⚠️ **범위 한정**: 이 두 파일 안에 없다는 뜻이다. 외부(scheduler 등)가 이들을 주기 호출하는
  가능성까지 배제하려면 호출자 검사가 추가로 필요하다. transport ping/pong 은 별개로 존재.
- **B2 [코드]** `app/latest_rates_cache.py:496` — USDT coalesce = `same rate + same 5s bucket → skip`.
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
  stage 정의·엄격 파서·코드 기본값 `app/config.py:741-784` · **정책 정본**
  `app/topic_policy.py:237-275`(`plan_anonymous`) · 그 위임 wrapper
  `app/topic_auth_rollout.py:308-323` · 필터 호출과 등록 `app/topic_dispatcher.py:620-648` ·
  production 주입 `app/main.py:313-321`.
  ⚠️ `0cfe474` 이전에는 stage 별 분기가 rollout wrapper 안에 있었다 — 지금은 정책표
  모듈이 정본이고 wrapper 는 주입받은 FX 집합을 넘겨 위임만 한다(익명·식별 두 축이
  `reject_anonymous_fx` 에서 값이 갈리므로 함수가 둘이다).
  - `compatibility` — 무료 topic 등록 + snapshot, ack 없음(구 동작 보존).
  - `reject_anonymous_fx` — 무료 집합에서 **canonical FX 만** 조용히 제외. USDT 는 유지되고
    FX-only 요청은 등록·응답 모두 0이다. 익명 unsubscribe 는 stage 와 무관하게 기존 경로를
    그대로 탄다(`app/topic_dispatcher.py:928-953`).
  - `enforce_authenticated_premium` — 익명 subscribe topic 을 전부 조용히 제외. unsubscribe 는 유지.
  ⚠️ 세 stage 모두 **익명 subscribe 시도**와 형식 검증을 통과한 **미검증 token-bearing 후보**를
     서로 다른 축으로 계측한다. 두 계측은 `TOPIC_DISPATCHER_ENABLED` 검사보다 앞이라
     (`app/topic_dispatcher.py:570-586`) flag-off 운영 상태에서도 값이 쌓인다. token-bearing 축은
     Firebase 검증 전 관측이라 인증 사용자나 실제 RevenueCat 호출 수가 아니다
     (`app/topic_auth_rollout.py:254-296` · snapshot `app/topic_auth_rollout.py:339-400`). 정책 topic과
     현재 availability 기반 최종-stage RC 후보 topic은 production 기동 시 한 번 계산해 주입한다
     (`app/main.py:303-321`).
- **C2 [코드]** `app/topic_initial_snapshot.py:354` — `per_user_gated_snapshot_topics()`.
  entitlement 전용 snapshot 판정 대상은 **KRX 뿐**이다. 이 집합은 FX/USDT premium 범위를
  나타내지 않는다.
- **C3 [코드]** `exchange-rate/app/main.py:3131` `@app.get("/api/v2/topics/snapshot")` —
  `app/main.py:3173` `verify_firebase_token(request)` → `app/main.py:3176`
  `require_premium(user_id, allow_empty=False)`. ⇒ REST twin 은 premium 을 **코드로 강제**한다.
  ⚠️ 초안은 이걸 [결정]으로 적어 "현재 구현 상태" 절에 뒀는데 **분류가 어긋났다** — 코드 사실이다.
- **C4 [코드]** `app/topic_policy.py:78-85` — 인가 **정책표**(리터럴). 비-KRX = `PREMIUM_ONLY`,
  KRX = `PREMIUM_AND_ENTITLEMENT`. `app/topic_policy.py:278-323` 이 stage 별로 partition 을
  파생하고, `compatibility`·`reject_anonymous_fx` 에서는 비-KRX 가 identity-only 로 남는다.
- **C5 [코드]** `app/topic_authorization.py:372-402` — coordinator. RC 는 요청당 **≤1회**,
  entitlement 는 premium 승인 뒤 KRX 요청이 있을 때만 **≤1회**. `Unavailable` 은 전체-요청,
  `Denied` 는 per-topic 이다.
  **C5-inf [추론]** ⇒ `enforce_authenticated_premium` 에서는 authorizable topic 이 하나라도
  있는 인증 subscribe 마다 RevenueCat 왕복이 1회 생긴다 — WS 경로는 cache-free 다
  (stale fallback 은 REST 전용 `verify_premium_status` 안에만 있다).

## D. 실패 경로

- **D1 [코드]** `app/topic_dispatcher.py:187` `remove_websocket` — docstring:
  *"publish 송신 실패 격리에서 호출된다 … '연결이 죽었다'의 동의어가 아니다"*.
- **D2 [코드]** `app/topic_dispatcher.py:264` `leased_subscribers` — 만료 lease 를
  **전송 직전에만** 필터. registry 제거·클라 통지 없음.
  ⚠️ 이 함수는 자신을 *"모든 발행 경로가 공유하는 단일 게이트"* 라고 적지만, `882d92b`
  이전에는 **initial snapshot 경로가 우회**했다(그 모듈에 `lease` 참조 0건). 지금은 D6 이
  그 경로를 같은 함수에 태운다.
- **D3 [코드]** `app/topic_initial_snapshot.py:1072-1101` — build deadline·transient 실패와 active
  server cooldown은 연결을
  **1013**, fatal 실패는 **1011**로 닫는다. `None`(미지원/flag off/데이터 없음)은 여전히 조용히 skip한다.
- **D4 [결정]** `app/topic_dispatcher.py:426` §8-B-term —
  *"식별된 요청은 반드시 종결된다 … 종결 프레임 하나 **또는 연결 종료**"*.
- **D5 [코드]** 같은 파일 — `registry.register(...)` 가 ack send 보다 **먼저**. outbound 직렬화 없음.
- **D6 [코드]** `app/topic_initial_snapshot.py:1103-1121` — initial snapshot 도 **전송 직전**에
  `leased_subscribers(topic)` 멤버십을 다시 본다(`882d92b`). 게이트에 걸리면 **해당 topic skip**
  이고 연결 실패가 아니다.
  **D6-inf [추론]** ⇒ 검사가 build **뒤**여야 하는 이유는 `_build_snapshot_sync` 가 `to_thread`
  로 도는 동안 lease가 만료될 수 있기 때문이다. LOAD-S3가 build/send 총예산을 두더라도 그 창 자체는
  사라지지 않으므로 전송 직전 재검증이 필요하다.
  ⛔ `registry.get_lease(...) is None` 으로 자체 판정하면 **무토큰(§E1) 구독**과 **등록 소멸**이
  구분되지 않아 취소된 구독에 데이터가 나간다 — 그래서 구독자 집합에서 출발하는 함수를 쓴다.

## E. 부하

- **E1 [코드]** `app/database_settings.py:85-86` — PostgreSQL `POOL_SIZE = 3`, `MAX_OVERFLOW = 2`
  (**최대 5**). 주석: *"RDS db.t4g.micro 메모리 절약"*. `app/database.py:36-38` 이 그 값을
  `create_engine` 으로 넘긴다.
  ⚠️ **좌표만 이동했다**(구 `app/database.py:30`): `DB_WORKLOAD_PROFILE` 슬라이스가 풀·timeout
  도출을 부작용 없는 별 모듈로 옮겼다. **값(3/2/최대 5)은 불변**이고 소유자만 바뀌었다.
- **E1-b [코드]** `app/database_settings.py:113` — online `pool_timeout = 10초`.
  구 상태는 **SQLAlchemy 기본 30초**였다. 즉 최대 5 커넥션이 찬 뒤 대기하던 요청이 이제
  10초에 접힌다(`sqlalchemy.exc.TimeoutError` → 503). 같은 파일 `:118` 의 online
  `statement_timeout = 60초`도 구 상태가 **0(무제한)** 이었다 — 운영 실측 근거는 그 모듈 docstring.
- **E2 [코드]** `app/topic_initial_snapshot.py:1064-1076` —
  `for topic in topics: ... payload = await build_snapshot_observed(topic, budget=request_budget)`. 그 공유 래퍼가
  `app/topic_initial_snapshot.py:497-838` 에서 같은 topic generation의 동시 요청을 shared build 하나로
  합치고 성공 결과만 최대 1초 cache한다. 새 shared flight는
  `app/config.py:650-660`의 기본 4-slot FIFO admission을 남은 S3 예산까지만 기다리며, join/cache hit는
  slot을 쓰지 않는다. transient build 실패는 같은 exact key에서 기본 1초 동안 새 build를 억제한다
  (`app/config.py:663-670` · `app/topic_initial_snapshot.py:651-709` ·
  `app/topic_initial_snapshot.py:733-817`). 실제 worker는 `app/topic_initial_snapshot.py:841-895`에서
  독립된 shared 예산으로 `asyncio.to_thread(..., _run_snapshot_worker, topic, request_budget)`를
  감싼다 — **REST twin도 같은 single-flight·worker 래퍼를 쓴다**
  (`app/main.py:3196-3197`).
  **E2-inf [추론]** ⇒ 연결당 topic 순회는 **순차**지만, 같은 key의 요청은 shared task 하나에
  합류하고 새 shared task의 admission 점유는 기본 4다. 서버 transient 실패의 즉시 재진입은
  cooldown으로 억제되지만 대기 **시간**만 bounded이고 queue 개수 hard cap은 없다. 기본 1초와
  클라이언트 jitter·재시도 상한을 사용한 45초 다중 클라이언트 리허설은 완료됐지만 waiter 개수
  hard cap과 영구 activation은 별도다. 취소된 sync worker도 다음 협력 checkpoint까지 잠시 남아
  admission 4를 실제
  executor thread 점유의 순간 상한으로 읽으면 안 된다.
- **E3 [결정]** `app/auth_executor.py:15` docstring — *"즉시거절 semaphore 는 별도로 **기각**됐다:
  배포 재연결은 평균 유입이 낮아도 **동시 도착** 이라 1초면 빠질 큐를 대량 거절한다."*
  같은 docstring: *"이것은 큐 상한이 아니다"*(`SimpleQueue` 무제한), *"자원 상한 완료 라고 쓰지 말 것"*.
- **E4 [코드]** `nginx/conf.d/default.conf` `location = /ws` 는 **86~110행** 블록이고 그 안에
  **전용 zone `ws_handshake_limit`(10r/s) 기반 `limit_req burst=20 nodelay` + `limit_req_status 429`
  가 있다**(89~90행). ⚠️ **`limit_conn` 은 여전히 없다** — R-DEC-4 (3) 이 캐리어 NAT 위험 때문에
  계측 후로 유보한 것이고, 부재가 곧 미이행은 아니다. `location /api/` 는 별개 zone `api_limit`
  (3r/s, burst 20, 429) + `limit_conn 10` 이다(116~118행). zone 정의는 파일 상단 19·25·26행.
  (구 baseline 은 "둘 다 없음"이었다 — C1 `463c880` 으로 `limit_req` 축만 해소됐다.)

## F. 클라이언트 현재 동작

- **F1 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:315-323` — tether 45초 deadline이
  fresh→stale로 바뀌면 `revalidateSilencedTopic("usdt:krw")`을 호출한다. 화면 값은 지우지 않는다.
- **F2 [코드]** `ios/FXi/Services/WebSocketService.swift:1164-1183`은 foreground/수동 복구에서
  desired topic을 재검증하고, 자동 reconnect 뒤 복구 batch는 별도 `U(0, 2초)` jitter를 거친다.
- **F3 [코드]** `ios/FXi/Services/WebSocketService.swift:528-625` — 단일 deadline arbiter가 송신
  시점부터 control deadline과 delivery deadline을 함께 소유한다. ACK 뒤에는 control task를 취소하고
  같은 arbiter를 delivery phase로 전환하므로 initial snapshot에도 클라이언트 상한이 남는다.
- **F4 [코드]** `ios/FXi/Services/WebSocketService.swift:250` `sendTopicCommand` — subscribe 는
  **배치**(한 요청에 여러 topic).
- **F5 [코드]** `ios/FXi/Services/WebSocketService.swift:850-905` —
  `shouldRetryCommandFailure` denylist + 최초 시도 포함 최대 3회 bounded retry. 서버 최소 cooldown 뒤
  `U(0, base)` additive jitter를 더하고 exact `(verb, sorted topics)` 실패는 cooldown task 하나를 공유한다.
- **F6 [코드]** desired/confirmed/receive-generation/delivery/rejection은
  `TopicSubscriptionSnapshot`이 canonical하게 소유하고, ACK는 sent scope에 한해 confirmed와 rejection을
  수렴시킨다(`ios/FXi/Services/WebSocketService.swift:1057-1088`).
- **F7 [코드]** lease는 topic별 절대 만료를 추적한다. 새 lease id만 만료를 연장하고, hard-expiry는
  desired를 보존한 채 confirmed를 제거한 뒤 lease 세대당 한 번 reconnect한다
  (`ios/FXi/Services/WebSocketService.swift:750-849`).
- **F8 [코드]** `ios/FXi/Services/TopicSnapshotMerger.swift:38` —
  `mergeAt <= existing.mergeAt` 이면 entry 를 버린다.
  **F8-inf [추론]** 서버가 rate 불변 시 `rate_changed_at` 보존(B2 파일) ⇒ 평평하면 store timestamp 가
  마지막 변동 시각에 **동결**.
- **F9 [코드]** `ios/FXi/Views/Components/GraphV2Section.swift:153` `liveFreshnessThreshold` —
  600초(hana 1200초). 주석이 *"timestamp=last-change … calm flat 을 과도 skip"* 이라 자인.
- **F10 [코드]** `ios/FXi/Models/AppState.swift:11-18` — `AppState`는 payload 없는 lifecycle만
  표현한다. topic last-known은 ViewModel store와 topic 전용 disk cache가 소유한다.
- **F11 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:580-625` — 은행 알림과 탭은
  `baseRates(for:)`의 FX topic live/last-known을 함께 쓴다. `appState.rates`는 존재하지 않는다.
- **F12 [코드]** DXY는 `dxy:spot` REST/WS topic으로 수신·merge되고 legacy `indices` 소비는 없다
  (`ios/FXi/Services/TopicSnapshotService.swift:53-58` ·
  `ios/FXi/ViewModels/ExchangeRateViewModel.swift:980-1010`).
- **F13 [코드]** `ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-714` `usdtDisplayState` —
  topic live/last-known만 사용하며 legacy subset fallback은 없다.
- **F14 [코드]** `ios/FXi/Utils/RealtimeV2Config.swift:31` `isTetherTopicEnabled` —
  Release 기본값 topic **OFF**다. topic-only 앱은 OFF artifact로 출시할 수 없으므로 Archive에서
  `TOPIC_V2_RELEASE_ON`을 fail-closed로 확인해야 한다.
