# REALTIME_V2_CLIENT_GUIDE — topic 구독 클라이언트 계약 (신규 iOS/Android 앱)

> **상태**: Proposed/Draft (2026-06-25, codex 019efdf0+019efe0b 검토 반영). 신규 topic-consuming 앱 출시용 **단일 핸드오프 계약**.
> 초기 OPEN 2건 모두 해소: usdt:krw REST bootstrap(§3, `/api/v2/topics/snapshot`) + USDT/KRX same-bucket ordering(§5, `rate_changed_at` 노출).
> ⛔ **"서버 측 계약 closed" 는 그 2건에 한정된다** (2026-08-01 축소). 서버 §8 WS 인증(1C)의
> **활성화 필수 부분은 완료**이고, `reauth_required` / 만료 lease registry 제거 **2건은 후속 계약**
> 으로 분리한다(2026-08-03 — 근거는 아래 "아직 없는 것" 절). 현재 land 된 것:
> `subscription_ack`/`subscription_error` + 종결 프레임 계약(§8-B-term) / **topic 별 lease**
> (ack 에 `lease_id` + **남은** duration, 상한 15분) / **uid 를 lease 에 바인딩** /
> **모든 발행 경로가 지나는 lease 게이트**(만료 시 전송 0) /
> **KRX per-user 판정**(2026-08-02 `4a45173`).
>
> ⚠️ **lease 는 topic 마다 길이가 다를 수 있다.** 만료는 *부여 시각*이 아니라 **가장 오래된
> authoritative 관측**에서 흐른다 — 무료 topic 은 identity 1축이지만 `krx:*` 는 identity ·
> premium · entitlement 3축을 함께 보므로 **같은 요청 안에서도 KRX 가 먼저 만료될 수 있다**.
> 클라는 topic 별 duration 을 각각 읽어야 한다(가장 긴 것 하나로 타이머를 잡으면 KRX 가 조용히 죽는다).
>
> **KRX per-user 판정의 세 갈래** (§8-C 범위가 다르다):
> - **entitled** → `accepted_topics` 에 lease 와 함께 실린다.
> - **자격 없음** → **per-topic 거부**다. `rejected_topics` 에 `premium_required` 또는
>   `krx_entitlement_required` 로 실리고 **같은 요청의 무료 topic 은 그대로 수락된다**.
>   이미 갖고 있던 KRX 구독이 있으면 **즉시 철회**된다(만료를 기다리지 않는다).
> - **판정 불가**(제공자·DB 장애, 인가 지연) → 그때만 **전체 요청**이 `temporarily_unavailable`
>   로 접히고 **registry 는 하나도 바뀌지 않는다**(무료 topic 조차 새로 등록되지 않는다).
>
> ⛔ **아직 없는 것**: `reauth_required` 프레임 / 만료 시 registry 제거.
> ⚠️ **`reauth_required` 는 서버-only 슬라이스가 아니다**(2026-08-03 확인): 현행 iOS 는 그 type 의
> 모델·파서가 **아예 없어** envelope 분기에서 unknown 으로 버린다. 닫으려면 서버 eviction + wire
> 프레임 + **iOS 소비**(모델 · 파서 · 현재 lease/topic 소유권 확인 · 현재 의도 재대조 · 중복 송신
> 방지)까지 **한 수직 슬라이스**여야 한다. 반면 **만료 registry 제거는 서버 단독으로 먼저** 할 수 있다.
> ⚠️ **이 2건은 활성화 blocker 로 분류하지 않는다**(2026-08-03). 만료는 **모든 발행이 지나는 단일
> lease gate 가 fail-closed** 로 막아 **데이터가 새지 않고 조용해질 뿐**이고, 클라는 만료 **전에**
> 선제 재인증한다(`lease − 180s − U(0,60)`, `duration: 0` = 즉시 재인증, bounded retry + watchdog).
> registry 잔여 항목도 연결 종료 시 사라진다.
> ⛔ **주요 사용자 영향은 하나** — 재인증이 반복 실패하면 그 topic 이 **서버 신호 없이 조용히
> 멈춘다**. 그게 `reauth_required` 가 메울 공백이다.
> ⚠️ **다만 "위험은 그것뿐"이 아니다** — 만료 registry 를 제거하지 않으면 **장기 연결**에서
> 만료 항목이 계속 남아 (i) `active_subscriptions` 에 `duration: 0` 으로 반복 노출되고,
> (ii) `registry.subscriber_count()` 를 부풀린다. (ii) 는 관측 왜곡에 그치지 않는다 —
> **krx · fx · tether 세 publisher 가 모두** `subscriber_count(topic) == 0` 을 **빌드 비용 차단**
> 으로 쓰므로(`app/krx_topic_publisher.py` · `app/fx_topic_publisher.py` · `app/tether_topic_publisher.py`),
> 그 단축이 무력화되어 **아무도 받지 않을 payload 를 매번 만든다**(전송 자체는 per-lease gate 가
> 막는다 — 보안 누수가 아니라 낭비·관측 문제다).
> → 운영 GO 는 "조용한 중단" 뿐 아니라 **이 stale registry 비용까지 수용**하는 결정이다.
> 비-blocker 로 둘지는 그대로 제품 결정으로 남긴다.
> ✅ **클라 축은 land 했다**(2026-08-02~03): **lease 소비**(최단 만료 기준 재인증 타이머,
> `duration: 0` = "지금 재인증" — iOS `1f6040e`/`a826dbf`) · **request timeout**(송신 직후 무장,
> 20초 무응답 → 같은 연결에서 새 `request_id` 재전송, 기존 재시도 상한 공유 — `cbc1c0f`/`f1e72c9`) ·
> **구매 직후 `premium_required` 복구**(서버 stable `krx_visible=true` 확정 → 기록된 topic 만
> 재구독 — `b83b99b`/`0eb91a1`).
> 이 문서를 "서버가 다 됐다"로 읽고 활성화를 앞당기지 말 것 — 활성화 선행 조건은 아래 3조건이다.
> 서버 코드 구현 완료(snapshot-on-subscribe + wire e2e). ⚠️ **prod 현재 OFF** — 구 "prod LIVE"(2026-06-27
> `TOPIC_DISPATCHER_ENABLED`/`FX_TOPIC_ENABLED` ON)는 2026-07-22 route auth 감사에서 무인증 누수 완화로
> `TOPIC_DISPATCHER_ENABLED=false`로 되돌렸다(2026-07-25 재확인: `topics/snapshot` → 404 `topics_disabled`).
> 재활성화 선행 3조건(§1): ①**E3**(REST twin 인증 게이트, 2026-07-25 land) ②**WS 인증(1C) —
> 활성화 필수 부분 land**(서버 `4a45173` + 클라 lease 소비·request timeout·구매 복구,
> 2026-08-02~03; 후속 2건은 위 참조) ③**클라 bootstrap 3종의 인증 transport 이관** — ✅ **land**
> (2026-07-26 iOS `4cb050f`: `TopicSnapshotService` 가 `AuthedRESTTransport` 로 3종을 보내고,
> 전용 테스트가 `Authorization: Bearer` 부착을 잠근다).
> → **기능 선행 3조건 모두 충족.** ⛔ 다만 "GO 하나만 남았다"가 **아니다**: 서버 PLAN §자원 상한의
> **열린 항목 1건**(nginx `/ws` ingress 상한)이 *flag ON 전 결정 필요*로 남아 있고
> (~~인증 전용 executor~~ 는 **✅ 닫혔다** — 구현 land + `W=4` 실측 확정 2026-08-05; 당시 서술은
> "2건"이었다 [superseded]), GO 뒤에도 **활성화 실행 절차**(서버 flag → prod smoke →
> `TOPIC_V2_RELEASE_ON` Release arming → phased rollout)가 있다. ⚠️ arming 은 **문서가 아니라
> Archive 직전의 실제 build setting 으로 확인**한다. GO 는 위 "아직 없는 것" 절의 수용 항목
> (조용한 중단 · stale registry 비용 · **연결 내부 subscribe 남용** — nginx 상한으로는 닫히지 않는
> 별도 위험이라 GO 목록에서 빠뜨리면 안 된다)까지 함께 받아들이는 결정이다.
> KRX는 2026-07-08부터 독립 topic
> `krx:usd-krw-futures`(ADR-038 D2 — 구 `KRX_TOPIC_INCLUDE` env 제거). 잔여 = **client release gate**
> (iOS `RealtimeV2Config` build-config gate `TOPIC_V2_RELEASE_ON`; 절차는 iOS repo `TOPIC_V2_RELEASE_RUNBOOK.md`).
> 이 문서가 topic 계약의 **authoritative source**. [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md)
> "Topic API" 섹션은 본 문서로 supersede(구현에 사용 금지).

## 0. 범위 (실제 구현 기준)

| topic | 의미 | 상태 |
| --- | --- | --- |
| `fx:usd-krw` | USD/KRW 은행(≤8, **Citi 제외** — `FX_TOPIC_BANK_ORDER`) + Investing reference | ✅ 구현 |
| `fx:jpy-krw` | JPY/KRW 은행(≤8, Citi 제외) + reference | ✅ 구현 |
| `fx:eur-krw` | EUR/KRW 은행(≤8, Citi 제외) + reference | ✅ 구현 |
| `usdt:krw` | 테더 탭 (USDT 5거래소 + USD/KRW 은행[kb,hana] + reference) — **KRX 선물 미포함**(ADR-038 D2) | ✅ 구현 |
| `krx:usd-krw-futures` | KRX 미국달러선물 단독 (ADR-038 D2 독립 topic — `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`[G2∧G3] on일 때만 발행/snapshot) | ✅ 구현 (2026-07-08) |

**범위 밖 (topic publisher 미구현 — 구독해도 데이터 안 옴)**:
- **DXY / news / graph**: 독립 topic 없음. DXY는 legacy broadcast `data.indices.dxy`, news/graph는 REST.
- 미지원 topic / 게이트 off 인 `krx:usd-krw-futures` 의 처리는 **요청이 식별됐는지에 따라 갈린다**
  (2026-08-01 개정 — 구 서술 "registry에는 등록되나 조용히 skip"은 미식별 경로에만 참이다):
  - **식별된 요청** → ack 의 `rejected_topics` 에 `unknown_topic` / `topic_unavailable` 로 실리고
    **registry 에 등록되지 않는다**.
  - **미식별 요청**(구 클라) → **ack 이 없어 조용히 무시**된다. ⛔ 등록되는 것은 **지원되는 무료
    topic 뿐**이다 — 미지원 topic 과 `krx:*` 는 **registry 에 들어가지 않는다**(2026-08-02
    `4a45173` 개정). 무토큰으로 gated topic 을 등록하면 lease 가 없고, 발행은 lease 부재를
    "무제한"으로 취급해 **무인증 유료 데이터 우회**가 된다(실측 재현).

## 1. 연결 + 구독 프로토콜

```text
URL:         wss://fxi.kr/ws            (legacy broadcast와 동일 endpoint 공유)
Subscribe:   {"type":"subscribe",   "request_id":"<opaque-id>", "id_token":"<firebase>",
              "topics":["fx:usd-krw","usdt:krw"]}
Unsubscribe: {"type":"unsubscribe", "request_id":"<opaque-id>",
              "topics":["usdt:krw"]}          // id_token 불요 (§8-A: 축소는 fail-open)
Keep-alive:  "ping" (raw text) → 서버 {"type": "pong"}
```

⛔ **필수 필드는 verb 마다 다르다** (2026-08-01 개정). 구 예시는 둘 다 없었는데, 그대로
구현하면 §E1 **미식별 경로**로 들어간다 — 인증도 받지 않고 **ack 도 오지 않아** 요청의
성공·실패를 알 수 없다.

| verb | `request_id` | `id_token` |
| --- | --- | --- |
| `subscribe` | **필수** | **필수** (§8-A) |
| `unsubscribe` | **필수** | **불요** — §8-A: 권한을 *축소*하는 작업이라 fail-open. 보내도 무시되지만, ⚠️ 토큰을 실었다면 `request_id` 는 반드시 있어야 한다(없으면 `invalid_request`) |

서버는 `request_id` **키** 또는 `id_token` **값** 중 하나라도 있으면 "식별된 요청"으로 보고
**반드시 종결시킨다**(§8-B-term). ⚠️ "반드시 답한다"가 아니다 — 종결은 *프레임 하나* **또는**
*연결 종료*이고, 비-JSON/비-dict 처럼 **둘 다 오지 않는** 경로도 있다(아래 0-프레임 절). `request_id` 는 **opaque 문자열**이며 UUID 를 강제하지 않는다 —
서버는 echo 만 한다.

### 서버 → 클라이언트 메시지 종류 (⚠️ /ws는 legacy와 공유)

| `type` | 언제 | 처리 |
| --- | --- | --- |
| `"rates"` | **연결 직후 1건 자동** (legacy 초기 payload) + legacy broadcast cycle | **무시** (topic 클라이언트 대상 아님) |
| `"snapshot"` | subscribe 직후 + 값 변경 시 | **처리** — `payload["topic"]`으로 분기 |
| `"subscription_ack"` | **식별된** subscribe/unsubscribe 의 성공·부분성공 | **처리** — `active_subscriptions` 가 연결의 최종 상태다 (§아래 ack 계약) |
| `"subscription_error"` | 식별된 요청의 전체-요청 실패 | **처리** — `error` 코드로 terminal / 재시도 판정 |
| `"pong"` | `"ping"` 응답 | keep-alive |

> **🔴 필수**: 신규 앱은 **`type=="snapshot"` 이고 `topic` 필드가 있는 메시지만** topic payload로 decode한다.
> 연결 직후 오는 `type=="rates"` legacy payload(main.py가 모든 `/ws` 연결에 자동 전송)를 snapshot으로
> 파싱하면 첫 메시지에서 깨진다. `type`으로 먼저 분기할 것.

### 활성 조건 (서버 flag)

- `TOPIC_DISPATCHER_ENABLED=true` 필요(전 topic). ⚠️ **현 prod는 OFF** — 2026-07-22 route auth 감사에서
  무인증 누수 완화로 되돌렸다(2026-07-25 재확인: `topics/snapshot` → 404 `topics_disabled`).
  구 "현 prod는 ON(2026-06-27 확인)"은 폐기. **재활성화 선행 조건**: ①서버 REST twin 인증 게이트(E3, 2026-07-25 land)
  ②WS 인증(1C, ✅ 2026-08-03) ③**클라 bootstrap 3종의 인증 transport 이관**(✅ 2026-07-26 iOS `4cb050f`)
  — ⚠️ ③ 이 없으면 켜는 순간 클라 cold-start bootstrap 이 401 로 **조용히** 사라진다(`try?` 격리라
  크래시가 없어 더 안 보인다). 그래서 필수였고, 지금은 충족돼 있다.
- FX topic(`fx:*`)은 추가로 `FX_TOPIC_ENABLED=true` 필요. `usdt:krw`는 `TOPIC_DISPATCHER_ENABLED`만. `krx:usd-krw-futures`는 추가로 `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`(=`KRX_FUTURES_ENABLED`∧`KRX_CLIENT_DISTRIBUTION_ENABLED`, ADR-038 G2·G3) 필요.
- live 활성 = 별도 운영 GO (출시 직전).

### ack / error / auth

⚠️ **2026-08-01 개정.** 구 서술("subscribe 성공/실패 ack 없음 … 모두 조용히 무시")은 **폐기**됐다.

- **식별된 요청은 ack/error 를 받는다** (§8-B-term). "식별된" = `request_id` **키**를 실었거나
  `id_token` **값**을 실은 요청. 그런 요청에는 **종결 프레임 하나 또는 연결 종료**가 보장된다.

  | 상황 | 응답 |
  | --- | --- |
  | 성공 | `subscription_ack` — `operation` 으로 subscribe/unsubscribe 구분, **`active_subscriptions` 가 연결의 최종 상태**(클라는 이것으로 수렴한다) |
  | FF-off | 전 topic `topics_disabled` 인 **ack**(§8-C 에서 per-topic 코드다). ⚠️ **인증 이전**에 나가므로 **ack 수신 ≠ 인증 통과** |
  | 미지원 topic / 개별 flag off | ack 의 `rejected_topics` (`unknown_topic` / `topic_unavailable`) |
  | 형식 위반 — 빈 topics · 미지 `type` · `request_id` 누락/비문자열 | `subscription_error` `invalid_request` |
  | 자격 실패 / 판정 불가 | `invalid_token` / `temporarily_unavailable`(+`retry_after_seconds`) |

- ⛔ **"항상 프레임 하나"로 설계하지 말 것.** 프레임 0개인 경로가 있고 **둘로 갈린다**:
  - **연결 종료를 동반** — 분류 불가 인증 예외 · 16KB 초과(전송 계층 close **1009**) ·
    반쯤 닫힌 소켓. → **disconnect 를 모든 in-flight 의 종결 신호로** 처리하면 닫힌다.
  - **연결이 살아 있음** — 비-JSON / 비-dict 입력(서버가 `request_id` 를 읽을 수 없어 답할
    대상이 없다). ⛔ 여기서는 disconnect 가 **영영 오지 않으므로 timeout 이 유일한 종결 수단**이다.
  → 두 축을 **모두** 구현해야 한다. (구 서술은 비-JSON/비-dict 를 "연결 종료" 쪽에 넣었는데
    오분류였다 — 그것만 보고 설계하면 그 경로에서 큐가 멈춘다.)
- **미식별 요청**(구 클라 — `request_id` 도 `id_token` 도 없음)은 **동작 불변**이다: 조용히
  등록되고 **snapshot 수신 자체가 성공 신호**다. 이 합집합이 구 클라 보호막이다.

#### 정확한 wire 형태 (Stage 1 — 이 문서만으로 decoder 를 쓸 수 있어야 한다)

<!-- topic-wire-examples:start -->
```jsonc
// 성공 / 부분 성공
{
  "type": "subscription_ack",
  "request_id": "<opaque-id>",     // 요청의 id 를 그대로 echo — ack 은 **null 이 아니다**
  "operation": "subscribe",        // | "unsubscribe"  (같은 schema 라 이 필드로 구분)
  "accepted_topics":      [{"topic": "fx:usd-krw", "lease_id": "c7f1a2", "lease_duration_seconds": 900}],
  "rejected_topics":      [{"topic": "krx:usd-krw-futures", "error": "topic_unavailable"}],
  "removed_topics":       [],      // **항상 빈 배열** (§C2 eviction 축, 요청 결과 아님)
  "active_subscriptions": [{"topic": "fx:usd-krw", "lease_id": "c7f1a2", "lease_duration_seconds": 900}]
}

// 전체-요청 실패 — ⛔ **두 형태다.** 아래 결합 규칙은 서버 builder 가 생성 시점에 강제한다.
//    (한 프레임에 둘을 섞은 예시를 쓰면 안 된다 — 그 조합은 서버가 만들 수 없다.)

// (i) 재시도 불가 — `retry_after_seconds` **필드 자체가 없다**
{
  "type": "subscription_error",
  "request_id": "<opaque-id>",     // ⚠️ **nullable** — 요청에서 id 를 읽을 수 없었으면 null
  "error": "invalid_request"       // | "invalid_token" | "request_too_large"
}

// (ii) 재시도 가능 — `temporarily_unavailable` **만** 이 필드를 동반한다(항상 동반한다)
{
  "type": "subscription_error",
  "request_id": "<opaque-id>",
  "error": "temporarily_unavailable",
  "retry_after_seconds": 5
}
```
<!-- topic-wire-examples:end -->

⚠️ **`retry_after_seconds` 는 서버가 매 응답마다 고르는 값이다 — 위 예시의 `5` 를 상수로 굳히지
말 것.** 같은 `temporarily_unavailable` 이라도 **재시도로 풀리는 장애**(제공자 일시 오류, DB 커넥션
단절, 인가 deadline)와 **재시도로 안 풀리는 결함**(DB 권한·카탈로그 오류 등)에 서로 다른 간격이
실린다. 후자를 짧은 간격으로 재시도하면 모든 클라가 고장 난 백엔드를 훨씬 자주 두드린다 — 값을
나눠 둔 이유가 그것이다. **받은 값을 그대로 쓰고**, 필드가 없을 때만 클라 기본값을 쓴다.

⚠️ **컨테이너는 객체 배열**이다(문자열 배열이 아니다). **Stage 2 에서 `lease_id`·
`lease_duration_seconds` 가 필드로 추가됐다** — 컨테이너 형태는 그대로이므로 Stage 1 형태로
디코드해 둔 클라는 shape 를 바꾸지 않아도 된다. `identity_generation` 은 아직 없다.

⚠️ **`lease_duration_seconds: 0` 은 "타이머 없음"이 아니라 "이미 만료 — 지금 재인증하라"다**
(2026-08-01 과도기 결정). 서버는 만료된 구독을 **registry 에서 지우지 않고** `active_subscriptions`
에 남긴 채 `0` 을 싣는다. 필드를 아예 빼면 **무토큰(§E1) 구독과 구분되지 않아** 클라가 그 topic 을
*무제한*으로 오해하기 때문이다. 그 상태에서 발행은 **이미 0** 이다(게이트가 막는다).
⛔ 최종 계약은 *만료 시 registry 제거 + `reauth_required` 프레임*이고, 이건 그 전 단계다 —
클라는 `0` 을 받으면 **즉시 재인증**해야 하며, "만료됐으니 무시"로 처리하면 그 topic 이 조용히 죽는다.

⛔ **lease 는 topic 별로 붙는다.** 붙는 곳: 인증된 subscribe 의 `accepted_topics`, **그리고
어느 ack 이든 `active_subscriptions` 에 남아 있는 구독**(`operation="unsubscribe"` 포함 —
클라가 그걸로 재인증 시점을 잡는다). 붙지 않는 곳: unsubscribe 로 **제거된** topic /
flag-off 의 전부-rejected ack / **무토큰(§E1) 구독**.

⚠️ **무토큰 구독은 무료 topic 만 가능하다.** per-user 판정이 필요한 topic(`krx:*`)은 무토큰으로
등록되지 않는다 — lease 없는 구독을 무제한으로 취급하면 무인증 유료 데이터 우회가 되기 때문이다.
⚠️ **lease 는 `active_subscriptions` 에도 실린다** — 그게 연결의 최종 상태이므로 클라는 여기서
각 topic 의 만료를 읽는다. 서버는 **전송 직전에 lease 를 다시 확인**하고, 만료된 구독에는
프레임을 보내지 않는다(§8.1 S5 의 15분 revoke 상한). 클라는 **가장 이른 만료 전에** 재인증해야
한다 — 그 전까지 그 topic 은 조용히 멈춘다.
⚠️ 정렬: `active_subscriptions` 는 **사전순**, `accepted_topics`/`rejected_topics` 는 **요청 순서**.

**오류 코드 — 실리는 위치가 다르다:**

| 범위 | 코드 | 위치 |
| --- | --- | --- |
| 전체-요청 | `invalid_token` · `temporarily_unavailable` · `invalid_request` · `request_too_large` | `subscription_error.error` |
| per-topic | `topics_disabled` · `unknown_topic` · `topic_unavailable` · `premium_required` · `krx_entitlement_required` | ack 의 `rejected_topics[].error` |

- **인증**: 무토큰 subscribe 는 여전히 동작한다(§E1 중간 상태). `id_token` 을 실으면 서버가
  Firebase 로 검증한다(revoked 포함). **강제 전환은 별도 결정**이다.

## 2. Payload schema (version=1)

### 2.1 top-level (모든 topic 공통)

```jsonc
{
  "type": "snapshot",   // v1은 snapshot-only (delta/seq 없음 — §8)
  "version": 1,         // schema version. 비호환 변경 시 bump
  "topic": "fx:usd-krw",// 송신 topic (multi-구독 분기용, publisher 주입)
  "data": { /* topic별 그룹 (§2.3/§2.4) */ }
}
```

### 2.2 entry shape (모든 그룹 공통)

```jsonc
{
  "source": "kb",                            // string — 데이터 공급자
  "asset": "usd-krw",                        // string — 통화쌍/상품
  "rate": 1385.5,                            // float — KRW
  "timestamp": "2026-06-25T15:00:00+09:00"   // ISO8601 KST — ⚠️ source별 의미 상이 (§5)
}
```

- Entry 식별자 = **`(source, asset)` tuple**. 같은 source가 다른 asset 가능.
- 서버는 표시명/아이콘/색상/정렬 **미전송**. 단말이 `(source, asset)`로 자체 registry lookup. 새 source 추가 시 단말 registry 갱신.
- **`rate_changed_at`(optional, ISO8601 KST)**: `usdt_krw` 거래소 + `krx:usd-krw-futures` topic entry의 정밀 변경 시각(Redis-served 시). 이들 `timestamp`는 `seen_at`(5초 bucket)일 수 있어 merge ordering은 이 필드를 우선(§5). bank/investing/FX entry엔 없음(`timestamp`가 이미 정밀). client는 항상 `rate_changed_at ?? timestamp` 사용.

### 2.3 `fx:<asset>` data

```jsonc
"data": {
  "banks": [ {entry}, ... ],     // 은행 ≤8 (FX_TOPIC_BANK_ORDER = Citi 제외, 미등록 source 자동 제외)
  "reference": {entry}           // Optional — source="investing". 없으면 key 누락
}
```

### 2.4 `usdt:krw` data

```jsonc
"data": {
  "usdt_krw": [ {entry + "rate_changed_at"}, ... ],  // USDT 5거래소 — entry에 rate_changed_at 추가(§5)
  "usd_krw_banks": [ {entry}, ... ],  // 은행 (kb, hana)
  "usd_krw_reference": {entry}        // Optional — source="investing", asset="usd-krw"
}
```

> ADR-038 D2 (2026-07-08): 구 `usd_krw_futures` optional group은 **제거** — KRX는 §2.5 독립 topic.

### 2.5 `krx:usd-krw-futures` data (ADR-038 D2, 2026-07-08)

```jsonc
"data": {
  "usd_krw_futures": {entry (+"rate_changed_at" when Redis-served)}  // source="krx", asset="usd-krw-futures"
}
```

- entry shape는 구 usdt:krw group과 **동일**(TopicSourceEntry 하위호환) — group 키 이름도 유지.
- 발행/snapshot 조건: `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`(G2∧G3) on. off면 발행 중단 + REST 404 + WS snapshot skip.
- per-user 노출 — **경로별로 다르다**:
  - **REST** `/api/v2/topics/snapshot`: **서버가 강제**(2026-07-25~, ADR-039 §8.1 E3). 비-entitled에겐 404 unknown_topic.
  - **WS**: **서버 강제 구현됨**(2026-08-02 `4a45173`) — 무토큰은 `krx:*` 를 registry 에 **등록조차
    하지 않고**, 토큰이 실리면 premium ⊥ entitlement 판정 후 자격 없으면 ack 의 `rejected_topics`
    에 `krx_entitlement_required`(또는 `premium_required`)로 실리고 기존 구독은 즉시 철회된다.
    ⛔ **그래도 클라 `krx_visible` gate(GET /api/entitlements, ADR-038 Decision 3)는 유지한다.**
    ⚠️ **"1C 서버 land = 클라 gate 제거 가능" 이 아니다.** 위 강제는 `TOPIC_DISPATCHER_ENABLED`
    가 켜져야 **한 줄이라도 돈다** — 꺼져 있는 동안은 구독 경로 자체가 없다.
    ⛔ **그리고 flag ON 도 제거 조건이 아니다.** 한때 여기 "제거 조건은 flag ON 이후"라고 적었는데
    **틀렸다**: flag ON 이 만드는 것은 *"WS subscribe 사전 필터가 중복이 되는 시점"* 일 뿐이다.
    `krx_visible` 은 WS 뿐 아니라 **그래프 series · 소스 목록 · 알림 선택지**까지 gate 하는데,
    `/api/v2/graph/*` 는 **여전히 무인증**이다(KRX 는 `krx_visible` **파라미터** default false 로만
    빠진다). 그 표면에서는 클라 gate 가 아직 **1차 강제선**이다.
    → **전역 gate 제거 조건 = graph v2 에 서버 per-user 강제가 생긴 뒤**(ADR-039 Stage B 계열).
    ⚠️ 클라의 lease·request timeout 소비는 2026-08-02~03 에 land 했다(그건 제거 조건이 아니라
    활성화 조건이었다).

**snapshot 크기(레이아웃 참고)**: `fx:*` = 은행 ≤8(Citi 제외) + reference 1. `usdt:krw` = 거래소 5 + 은행 2 + reference 1 = ≤8 entry. `krx:*` = 1 entry. 작음.

## 3. Bootstrap (초기 상태 획득)

**PRIMARY (권장) = WS connect + subscribe → 즉시 snapshot**:
1. `wss://fxi.kr/ws` 연결 → (연결 직후 오는 `type="rates"` legacy payload는 무시, §1)
2. subscribe 메시지 전송
3. **서버가 해당 topic 현재 전체 snapshot 1건 즉시 푸시**(snapshot-on-subscribe, 구현됨) → 첫 화면 렌더
4. 이후 값 변경 시 추가 snapshot

→ 정상 경로 **REST bootstrap 불필요**. 빈 시간대(주말/조용한 통화)에도 구독 즉시 현재값 수신.

**REST bootstrap (WS 미연결/실패 시 권장 fallback)** — v2 endpoint:

```text
GET /api/v2/topics/snapshot?topic=<topic>     // topic ∈ {fx:usd-krw, fx:jpy-krw, fx:eur-krw, usdt:krw} (+ krx:usd-krw-futures — G2∧G3 on **이고 그 사용자에게 entitlement가 있을 때만**, 아니면 404 unknown_topic)
Authorization: Bearer <Firebase ID token>     // 필수 (2026-07-25~)
```

- 응답 = **WS snapshot과 동일 schema**(`{type:"snapshot", version:1, topic, data}` + usdt/krx tick entry의 `rate_changed_at`). 같은 builder 공유 → client는 REST/WS 동일 merge 로직(`rate_changed_at ?? timestamp`).
- 권장 흐름: **REST bootstrap(즉시 렌더) → WS subscribe → snapshot/live merge**(REST 응답을 §5 merge로 흡수, WS snapshot이 자연 갱신).
- `Cache-Control: no-store` — 이 endpoint가 **직접 만드는 응답 전부**(200 · 404 3종 · DB 순단 503).
  401/403/PENDING 503은 `HTTPException` 공통 예외 경로라 헤더가 없다 — 세 코드 모두 휴리스틱 캐시
  대상이 아니라(RFC 9110 §15.1 목록에 부재) 준수 캐시는 저장 자체를 못 한다.
- ⚠️ **인증·권한 필수 (2026-07-25 변경 — ADR-039 §8.1 E3)**: 구 "인증 없음"은 **폐기**.
  이 endpoint는 WS topic의 REST twin이라 같은 권한 매트릭스를 따른다 — **Firebase 인증 + premium**,
  KRX는 **+ entitlement**. 클라는 토큰 없이 호출하면 안 된다(1B 인증 transport 경유).
  entitlement 없는 사용자에게 krx topic은 `unknown_topic` 404이고 `supported_topics` 에코에도
  나타나지 않는다 — **미지원 topic과 구분 불가**(KRX 존재 비노출 계약).
- 응답 코드: 200(payload) / **401**(토큰 없음·무효) / **403**(premium 아님) /
  **503** — **세 원인, 전부 재시도 대상**(인증 실패로 처리하지 말 것). 구분은 **본문**으로 한다:

  | 원인 | 본문 | `Retry-After` | 재시도 감각 |
  |---|---|---|---|
  | 구독 판정 PENDING | `{"detail": "Subscription status pending..."}` | **있음**(5) | 초 단위 |
  | 인증 인프라 장애(Firebase 미초기화/네트워크) | `{"detail": "..."}` | 없음 | 클라 backoff |
  | **DB 순단**(entitlement 조회·snapshot 빌드) | `{"error": "temporarily_unavailable"}` | 없음 | 클라 backoff, **분 단위**(RDS failover) |

  ⚠️ 인증 인프라 장애는 **토큰이 없어도** 401보다 먼저 나올 수 있다(초기화 확인이 헤더 검사보다 앞).
  ⚠️ `temporarily_unavailable`은 WS `subscription_error`의 같은 이름과 **같은 의미**(판정 불가)지만,
  REST 쪽은 `Retry-After`를 주지 않는다 — 5초 재시도를 지시하면 DB failover 동안 storm이 된다.
  **헤더가 없다는 사실만으로는 storm이 막히지 않으므로 클라 재시도를 계약으로 고정한다.**

  ⚠️ 전제: 이 endpoint는 **best-effort 가속기**지 데이터 경로의 정본이 아니다(정본은 §3 PRIMARY의
  WS snapshot). 그래서 재시도는 **짧고 유한**해야 한다 — 긴 backoff는 의미가 없다. 수 초 뒤엔 WS
  snapshot이 이미 도착해 있을 가능성이 높고, 그때 도착한 REST 응답은 §5 merge에서 구값으로 버려진다.

  | 항목 | 계약 |
  |---|---|
  | 총 시도 | **3회 이내**(첫 시도 포함). 소진하면 **포기하고 WS snapshot에 맡긴다** — 무기한 재시도 금지 |
  | backoff | 0.5s → 1.5s(±20% jitter). 마지막 값 이후는 재사용 |
  | 조기 종료 | 그 topic의 snapshot을 이미 받았으면(WS/다른 경로) 남은 시도 취소 |
  | 취소(즉시) | **① 계정(UID) 변경 ② 게이트/권한 변경** — in-flight task를 **실제로 cancel**(명시 배선) **+** 적용 직전 UID 재대조(취소 전 완료된 응답용) |
  | latency guard | **이벤트 무관 · 요청 latency 10초 상한** — 예산 초과 응답 폐기(아래). ⚠️ background·연결 generation을 **감지하지 않는다** |
  | 동시성 | **topic별 독립 타이머** — cold-start 4~5건이 같은 시각에 재시도하지 않도록 |
  | 재시도 **대상 아님** | 401 · 403 · 404 3종(상태가 바뀌어야 해소된다) |
  | 토큰 획득 실패 | **정적 분류 금지** — 캡처 UID == live UID일 때만 bounded retry(Firebase mint 일시 실패). UID가 없거나 바뀌었으면 **즉시 terminal + 취소**(로그아웃·계정 전환). `CancellationError`는 감싸지 않고 그대로 전파 |

  ℹ️ 참조 구현: iOS `ExchangeRateViewModel.bootstrapKrxWithRetry`(`krxBootstrapMaxAttempts = 3`,
  `krxBootstrapBackoffsSeconds = [0.5, 1.5]`, WS-wins revision 체크). tether/fx bootstrap은 현재
  **재시도 없음**(1회 시도 후 WS에 위임).

  ✅ **아래 4종은 모두 구현됨**(2026-08-08 재실측 — 구 "미구현" 서술을 대체한다):

  | 항목 | 구현 위치 |
  |---|---|
  | ① UID 변경 시 명시 취소 | `FXiApp` scenePhase/auth 경계 → `ExchangeRateViewModel.handleDirectAccountSwitch()` |
  | ① 적용 직전 UID 재대조 | `ExchangeRateViewModel.canApplyBootstrap(capturedUID:)` |
  | latency budget(10초·`ContinuousClock`) | `canApplyBootstrap(capturedUID:issuedAt:)` |
  | 인증 transport 이관 | `TopicSnapshotService` 가 `AuthedRESTTransport` 보유·사용(`Bearer` 부착은 transport 소유) |
  | 조건부 `.notAuthenticated` 처리 | `ExchangeRateViewModel` KRX bootstrap 오류 분류(정적 분류 불가 → 조건부 재시도) |

  ⚠️ 구 서술("2026-07-26 실측: 넷 다 없다")은 **그 시점 기준으로는 맞았다** — 이후 인증 이관
  슬라이스가 land 하면서 무효가 됐다. 날짜 없는 "미구현" 서술을 릴리스 준비도 판단에 쓰지 말 것.
  (**③ background · ④ 연결 generation은 계약 항목이 아니다** — 아래에서 내렸으므로 "미구현 gap"으로
  읽지 말 것. generation fence를 새로 만들면 오탐만 늘린다.)
  ⚠️ **background·연결 generation을 "이벤트 취소 조건"에서 내린 근거**(2026-07-26 최종, codex).
  구 표기는 이벤트 계약처럼 적어 놓고 기전은 시간만 검사해 **표와 구현이 불일치**했다.

  근거 — **이벤트 crossing 자체는 무해**하다: 실제 해악은 늦게 도착한 응답이
  `tetherReceived`/테더 freshness deadline·`fxReceivedAssets`/FX freshness deadline을 **무조건 갱신**해
  topic을 fresh로 오인시키는 것(→ legacy fallback 최대 45초 억제)인데, 이건 **경과 시간**의 함수다.
  background 직후 3초 만에 도착한 응답은 데이터가 실제로 신선하고 freshness 마킹도 정확하다
  (그 뒤 5분 backgrounded면 deadline이 지나 정상적으로 stale 판정된다).
  reconnect 직전 발행돼 직후 도착한 응답도 마찬가지다. → **일반 latency 상한이 해악을 정확히 덮고,
  이벤트 세대 카운터는 오탐(빠른 응답 폐기)만 늘린다.**

  **latency budget 계약** (test-first 가능한 수준으로 확정, 2026-07-26):

  | 항목 | 값 / 규칙 |
  |---|---|
  | 적용 범위 | **모든 bootstrap 응답**(이벤트 조건 없음) |
  | 예산 | **10초**. 요청 **발행** 시각 → 적용 직전까지의 경과 |
  | 시계 | **`ContinuousClock`**(monotonic, 기기 sleep 중에도 진행). wall-clock `Date`는 NTP·사용자 변경으로 점프 가능해 부적합 |
  | 캡처 단위 | **시도마다 재캡처** — 예산은 요청 1건의 latency지 bootstrap 세션 전체가 아니다(안 그러면 KRX 3회차가 1회차 경과를 물려받아 오폐기) |
  | 초과 시 | **요청 실패와 동일 취급** — 값 merge ❌ / `tetherReceived`·`fxReceivedAssets` ❌ / freshness deadline 갱신 ❌ / `krxSnapshotRevision` bump ❌. 그리고 **재시도하지 않고 종료**(가속기 창이 이미 지났고 WS가 정본) |
  | 테스트 seam | 시계 **3개를 역할별로 분리**해 각각 주입한다 — 합치지 않는다 |

  **시계 3개** (2026-08-08 갱신 — staleness 를 저장 상태로 바꾸며 전용 시계가 생겼다):

  | provider | 종류 | 쓰임 |
  |---|---|---|
  | `nowProvider` | wall-clock `Date` | **disk write throttle 한 곳뿐** (staleness에서 손 뗐다) |
  | `monotonicNowProvider` | `ContinuousClock` | 위 bootstrap latency 예산 |
  | `freshnessNowProvider` | `ContinuousClock` | topic freshness deadline **전용** |

  ⚠️ freshness 가 `ContinuousClock` 인 것은 **의도**다 — 기기 sleep 중에도 진행하므로 foreground
  복귀 시 실제 경과가 반영된다. 그리고 freshness 는 이제 계산값이 아니라 **관측되는 저장 상태**
  (`tetherIsFresh`/`freshFxAssets`)이고, deadline 에 깨어나는 monitor task 가 만료를 집행한다.
  구 `lastTetherTopicAt`/`lastFxTopicAt`/`isTetherTopicFresh()`/`isFxTopicFresh()` 는 삭제됐다 —
  계산값이라 SwiftUI 무효화 트리거가 없어 **화면을 건드리지 않으면 영원히 stale 표시가 안 됐다**
  (2026-08-08 운영 리허설에서 실측).

  **왜 10초인가**: (a) 이 fence가 막으려는 해악의 척도인 `topicStalenessThresholdSeconds = 45`보다
  충분히 작아야 신선도 오마킹이 창의 일부에 그친다 (b) 정상 latency(sub-second)보다 충분히 커서
  좋은 응답을 버리지 않는다 (c) 10초를 넘으면 WS snapshot이 이미 도착했을 가능성이 높아 bootstrap의
  존재 이유(cold-start 가속)가 사라진다.

  ⚠️ **이름이 계약이다 — 이건 "응답 나이"가 아니라 "요청 latency 예산"이다**(codex). 서버가 응답
  직전에 **fresh** snapshot을 만들었어도 요청이 느렸으면 폐기된다. best-effort 경로라 **의도적으로
  보수적**으로 택했다 — 정본은 WS snapshot이고, 애매하면 버리는 쪽이 신선도를 잘못 마킹하는 것보다 낫다.
  ①은 **인가 경계**다(2026-07-26 최종. 앞선 "defense-in-depth로 격하" 판단은 **철회** — 아래 두 근거).

  **근거 1 — 리포가 이 시나리오 클래스를 이미 두 번 방어하기로 결론냈다.**
  `EntitlementsManager.refresh`의 계정 소유권 gate 주석: *"reset을 거치지 않는 **직접 UID 전환**에서도
  B가 A의 krxVisible=true를 물려받지 않게"*(codex blocker 019f641a). `FXiApp`의 `ContentView().id(userId)`도
  *"UID 전환 시 이전 유저 KRX-gated VM 재사용 구조적 차단"*(codex High). `LoginView`가 `.signedOut`에서만
  렌더된다는 사실은 **Firebase auth listener가 반드시 signedOut을 방출한다는 보장이 아니다** —
  리포는 그 보장에 기대지 않기로 이미 정했다. bootstrap만 예외로 둘 이유가 없다.

  **근거 2 — "payload가 사용자 독립이니 값 차이 0"은 KRX에서 성립하지 않는다.**
  값 자체는 같아도 **받을 권리가 다르다**: A(entitled)는 200 + KRX 데이터, B(비-entitled)는 404다.
  A의 200이 B 세션에 적용되면 B가 **권한 없는 데이터를 받는 것**이다. `krxVisible` fail-close는
  `refresh()` **안**에 있어 `.signedIn` → Task hop 만큼 async 창이 남고, 늦게 도착한 A 응답이 그 창과
  경쟁한다. (fx/usdt는 값·권한 모두 동일해 실제로 무해 — KRX가 경계다.)

  → **요청 시작 시 UID를 캡처하고 mutation 직전 live UID와 재대조**한다(auth generation은 부적합 —
  같은 계정 강제 refresh에도 증가하고 로그아웃엔 증가하지 않아 계정 동일성 술어가 아니다).

  ℹ️ 부수 관찰(**독립 도달 경로가 아님** — 위 직접 UID 전환이 이미 전제다): 그 전제 위에서 B의
  `start()`가 초기 fetch 실패 + 캐시 없음으로 조기 return하면 세 launcher에 도달하지 못해 A의 task를
  덮어쓸 기회조차 없어져 **생존 시간이 늘어난다**(KRX 재시도 루프가 추가 연장). 도달성 근거가 아니라
  **노출 창 확대 요인**으로 읽을 것.

  ⚠️ **fence가 덮지 못하는 것**(별 트랙): fence는 *write*를 지키지 `retention`을 지키지 않는다.
  `stop()`은 `tetherStore`/`fxStore`/수신 플래그만 비우고 **`appState`와 로컬 캐시는 남긴다**
  (`cached_topic_rates` 등 캐시 키에 uid 스코프 없음, `SourcePreferenceManager`는 signedOut에서
  reset되지 않음) → 계정 전환 후 첫 프레임에 이전 계정의 화면·소스 구성이 보일 수 있다.
  값 자체는 공개 시장데이터라 심각도는 낮지만 **fence로는 원리적으로 닿지 않는다.**

  **404 세 종류** — 전부 재시도 무의미(상태가 바뀌어야 해소된다):
  `topics_disabled`(`TOPIC_DISPATCHER_ENABLED` off = 출시 전) /
  `unknown_topic`(+`supported_topics`. **미지원 topic과 미인가 KRX가 동일 응답**) /
  `topic_unavailable`(지원 topic이나 현재 미제공, 예 `FX_TOPIC_ENABLED` off).

  **순서 = dormant flag → 인증 → premium → topic 판정**이므로, flag off면 미인증이어도 404이고,
  미인증이면 unknown topic이어도 401이다(미인증자는 topic 목록을 열거할 수 없다).
- legacy `/api/rates/{usdt-krw|usd-krw-futures}`는 여전히 410 Gone(use_topic) — 신규 앱은 위 v2 bootstrap 사용. FX legacy `/api/rates/{asset}`(legacy shape)도 v2 bootstrap으로 대체 권장.

> ✅ (구 OPEN — usdt:krw REST bootstrap 부재)는 본 endpoint로 **해소**(2026-06-25). 전 topic(fx:*+usdt:krw) 통일 bootstrap.

## 4. 수신 규칙 (snapshot 처리)

- 모든 topic 메시지는 `type="snapshot"` = **해당 topic 현재 전체 상태 dump**. delta/증분 없음(§8).
- 수신 시 **`(source, asset)` 기준 merge**(§5) — 같은 키만 갱신, 전체 교체 불필요.
- **Optional 그룹 누락 = 삭제 아님(v1 tombstone 없음)**:
  - *최초 부재*(키가 한 번도 안 온 경우): 미표시.
  - *중도 부재*(이전엔 왔는데 이번 메시지에 없음): **이전 값 유지**(v1엔 삭제 신호 없음).
  - ⚠️ 운영 함의: `krx:usd-krw-futures` 게이트를 끄면(발행 중단) **이미 연결된 앱에서 KRX가 즉시 사라지지 않고 마지막 값이 남는다**(v1 tombstone 없음). 클라 측 즉시 제거는 `krx_visible`(GET /api/entitlements) refresh가 담당 — 서버 push 신호는 v1 미지원.

## 5. ⚠️ snapshot ↔ live race → merge 규칙 (필수 계약)

서버는 구독 snapshot과 직후 live publish의 **순서 역전**이 가능(per-message seq 없음). 클라이언트 merge 규칙:

> **각 `(source, asset)`에 대해 `merge_at = rate_changed_at ?? timestamp`를 비교해, `merge_at`이 더 최신인 entry만 반영. 더 오래된 `merge_at`으로 기존 값을 덮지 않는다(같으면 기존 유지 → 회귀 방지).**

- **FX(은행/investing)**: `timestamp`=실제 값 변경 시각(정밀). `rate_changed_at` 없음 → `merge_at = timestamp`. merge 정확.
- **USDT 5거래소(`usdt_krw` 그룹)**: `timestamp`=`seen_at`(5초 bucket alias)지만 **`rate_changed_at`(정밀 변경 시각)이 entry에 포함됨** → `merge_at = rate_changed_at`로 same-bucket 연속 변경도 정밀 ordering. (서버: Redis path `get_latest_usdt_rate_from_sync_job` + DB fallback `get_latest_source_rates_for_topic` 양쪽 노출.)
- **KRX(`krx:usd-krw-futures` topic)**: Stage E tick writer(`KRX_REDIS_TICK_WRITE_ENABLED`, 운영 활성)가 USDT와 동일한 5-field schema를 써서 `timestamp`=`seen_at`(5초 bucket)일 수 있음 → **Redis-served entry에 `rate_changed_at`(정밀) 포함**(USDT와 대칭). DB fallback 시엔 `timestamp`가 정밀이라 `rate_changed_at` 생략 → `merge_at = rate_changed_at ?? timestamp`로 일관 처리.

> 한계: `rate_changed_at`은 ms 해상도라 5초 bucket 문제는 해소되나, 진정한 total order(동일 ms 동시 변경)는 per-message `seq`가 필요(v1 미지원, 실질 영향 없음).

## 6. reconnect / resync + 클라이언트 정책

- 끊김 → 재연결 → **재구독 필수**(서버는 connection별 registry, 끊기면 구독 소실).
- 재구독 시 §3대로 **현재 snapshot 다시 수신**(resync). 별도 resync 프로토콜 불요. 서버측 snapshot 캐시/증분 resync는 v1 미구현.
- **권고 클라이언트 정책**: 재연결 exponential backoff(예 1s→2s→…→30s cap), keep-alive `"ping"` 주기(예 30s) — 서버는 raw `"ping"`에 `{"type":"pong"}` 응답만.

### close code — `1008`(정책 위반)의 의미와 **올바른 대응**

한 연결에 **다른 UID** 가 나타나면 서버는 프레임이 아니라 **`close(1008)`** 으로 끝낸다(§8-C 어휘에
identity 충돌 코드가 없다 — `invalid_token` 은 "토큰이 유효했다"는 사실과 어긋나고,
`temporarily_unavailable` 은 `retry_after_seconds` 를 강제해 영영 성공 못 할 재시도가 된다).

**그 경우 재연결이 곧 정상 복구 경로다.** UID 결속은 **연결당 하나**라 새 연결에서는 비어 있는
상태로 시작한다. 그래서 계정 전환(A → B) 직후의 전형적인 흐름은 *기존 연결에서 B 가 거부 →
close → 재연결 → B 가 첫 소유자로 결속 → 정상*이다. 여기서 재연결을 막으면 **계정 전환이
복구 불가능해진다**.

⛔ **다만 "1008이면 재연결"을 일반 규칙으로 굳히지 말 것.** `1008`은 일반 정책 위반 코드이고
서버는 `reason`도 싣지 않는다. 현재 서버 구현에서 확인된 직접 발신 사유는 cross-UID 충돌
한 가지이며, `test_cross_uid_closes_the_connection_without_error_logs`가 그 경로에서 실제
`1008`이 나가는 동작을 잠근다.

⚠️ 이것은 **현재 구현 상태에 대한 관측**이지, 두 번째 정책 사유를 자동으로 막는 정적 불변식이
아니다. 다른 정책 종료 사유를 추가할 때는 먼저 private `4xxx` code 또는 안정적인 `reason`처럼
클라이언트가 구분할 수 있는 신호를 정의하고, 이 절과 클라이언트 정책을 함께 갱신해야 한다.

ℹ️ FastAPI는 WebSocket endpoint의 dependency·parameter 검증 실패를 자체적으로 `1008`로
종료할 수 있다. 현재 `/ws`는 검증 대상 파라미터가 없어 해당하지 않지만, endpoint 시그니처나
dependency를 추가할 때는 이 절의 "현재 발신 사유 한 가지" 전제를 다시 확인해야 한다.

ℹ️ **지금 클라가 할 일은 없다.** 현재 iOS 는 close code 를 **읽지 않고** 모든 수신 오류를
**기존 reconnect 경로**로 접는데(2026-08-02 실측: `WebSocketService.swift` 에 `closeCode` 참조
0건), 그 동작이 위 복구 경로와 이미 일치한다. 서버가 `1008` 을 보내는 것은 **로그·프록시에서
정상 종료와 구분**하고 나중에 구분하고 싶어질 때 재료를 남겨 두기 위해서다.

⚠️ **그 reconnect 경로는 실질적으로 bounded 가 아니다** — `maxReconnectAttempts` 상한이 있지만
`reconnectAttempts` 가 **프레임 수신마다 0 으로 리셋**되고(`WebSocketService.swift` 의
`.connected` 전이) 서버는 **연결 직후 legacy payload 를 항상** 보낸다(`app/main.py` 의 `/ws`).
그래서 매 사이클이 `0 → 1` 을 반복해 상한에 영영 닿지 않는다
([FREE_TIER_ACCESS_MODEL_PLAN.md](FREE_TIER_ACCESS_MODEL_PLAN.md) §8 이 같은 것을 **reconnect
storm** 조건으로 이미 기록해 두었다).
cross-UID 에서는 재연결이 곧 성공이라 루프가 자연히 끝나지만, **영구적으로 실패하는 `1008` 사유가
새로 생기면 그것은 ~2초 간격 무한 재연결이 된다** — 두 번째 사유를 정의할 때 반드시 함께 볼 것.

## 7. versioning / forward-compat

- `version=1` 고정. 비호환 변경 시에만 bump(단말 release 동기화 후 lock-in).
- 클라이언트는 **모르는 필드/그룹 무시**(forward-compat). 서버가 그룹/필드 추가해도 안 깨지게.
- `type`이 미래값(`delta` 등)이면 v1 단말은 무시 가능.

## 8. v1 범위 밖 (단말이 기대하면 안 되는 것)

REALTIME_ARCHITECTURE_PLAN.md §5에 설계로 적혀 있으나 **현재 서버 미구현** — 계약 제외:
- **hello handshake**(protocol_version/supported_topics 협상): 없음. 단말은 고정 topic 목록(§0) 사용.
- **delta / seq**: 없음. 항상 full snapshot. dedup/순서는 §5 merge(`rate_changed_at ?? timestamp`)로 단말이 처리.
- **서버측 stale 신호**: payload에 stale 필드 없음(timestamp만). 휴장/주말 stale UI는 단말 정책(권고: 마지막 값 유지).

## 9. 참조

- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) §5 (서비스 계약 SoT)
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md) (Topic API 섹션 — 본 문서로 supersede)
- [DECISIONS.md](DECISIONS.md) ADR-028 (Topic-only Tether/KRX + dual-emit FX)
- 서버 구현: `app/fx_topic_payload.py`, `app/usdt_topic_payload.py`, `app/topic_dispatcher.py`, `app/topic_initial_snapshot.py`(snapshot-on-subscribe)
