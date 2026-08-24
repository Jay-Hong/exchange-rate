# 재검증과 부하 — jitter · single-flight · bounded wait

- 책임: jitter · single-flight · bounded wait
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `bf76b6e962444339d359e209c23d99aefbb96dc3`
- iOS 기준 commit: `78a9c4891465e788ae79e8e87161ce2a8f8af09d`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `d739aa04b0a292607b4ef2d2ceace88f6dcf56ccef2cf18960bf3f0b696028ba`
- baseline SHA: `4cc944e333789fb4a2ff08217d2dbd3469f29c9f09826946bb1776d43c27d708`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

> 이 문서가 소유하는 것은 **재검증(재구독)이 만드는 동시 부하** 하나다.
> 언제 조용히 재구독하고 언제 배너를 띄우는지는 클라 상태기계 문서가, ack·build·close 의
> 종결 계약은 handoff 문서가 소유한다. 여기서는 그 재구독이 **동시에 몰릴 때** 클라와 서버가
> 각각 무엇을 하는지만 정한다.
>
> ⚠️ 원문의 `§` 번호는 분할과 함께 사라졌다. 다른 문서의 내용은 **RID 와 링크**로만 가리킨다.

---

## 45초 동시 재구독 폭주 완화

이 완화는 **두 쪽이 함께** 있어야 성립한다. 클라는 재구독을 **흩뜨리고**(jitter · 재시도 상한 ·
클라 재시도 cooldown), 서버는 남은 동시 도착을 **흡수한다**(single-flight/cache · bounded wait ·
I/O 상한 · 서버 실패 cooldown).

⚠️ 클라 절반은 이 문서가 아니라 [R-CLI-24](ios-topic-state-machine.md#r-cli-24) 가 소유한다 —
한쪽만 구현하면 완화가 성립하지 않는다는 것이 아래 [R-LOAD-3](#r-load-3) 의 마지막 항목이다.

<!-- rid: R-LOAD-4 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-4"></a>
### R-LOAD-4 — 폭주 완화는 확정 계약이다 · 즉시거절 bulkhead 금지
<!-- relation: references target=R-CLI-11 -->
- references: [R-CLI-11](ios-topic-state-machine.md#r-cli-11)

[R-CLI-11](ios-topic-state-machine.md#r-cli-11) 의 조용한 재구독은 **확정**이다. 그것이 만드는
동시 폭주의 완화도 같은 등급이어야 한다 — 이 절은 ADR-041 의 `[제안·결정 대기]`
**파생 숫자 숨김 정책** 제안의 채택 여부와 무관한 **확정 계약**이다.

⛔ **즉시거절 형태의 bulkhead 를 쓰지 않는다.** 이 리포는 그걸 **이미 기각**했다 —
`auth_executor` docstring: *"즉시거절 semaphore 는 별도로 기각됐다: 배포 재연결은 평균 유입이
낮아도 **동시 도착** 이라 1초면 빠질 큐를 대량 거절한다."*
⚠️ 그리고 그 executor 는 **큐 상한이 아니다**(`SimpleQueue` 무제한) — 같은 docstring 이
*"자원 상한 완료 라고 쓰지 말 것"* 이라 못 박는다.

근거(baseline): **E3 [결정]** `app/auth_executor.py:14-16` docstring(즉시거절 기각) +
`app/auth_executor.py:29-31`(**`SimpleQueue` 무제한** — "자원 상한 완료" 라고 쓰지 말 것).
⚠️ S1a 로 모듈 docstring 이 늘어 두 근거가 갈렸다 — 한 범위로 합쳐 인용하면 낡는다.
⚠️ 즉 기존 `auth_executor` 를 "이미 상한이 있다"는 근거로 인용해서는 안 된다. 그것은 직렬화 장치이지
자원 상한이 아니며, 이 문서가 요구하는 bounded wait 를 대신하지 못한다.
<!-- /rid: R-LOAD-4 -->

<!-- rid: R-LOAD-3 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-3"></a>
### R-LOAD-3 — 서버는 동시 도착을 흡수한다
<!-- relation: references target=R-CLI-24 -->
- references: [R-CLI-24](ios-topic-state-machine.md#r-cli-24)
<!-- relation: references target=R-HAND-14 -->
- references: [R-HAND-14](topic-snapshot-handoff.md#r-hand-14)
<!-- relation: references target=R-HAND-16 -->
- references: [R-HAND-16](topic-snapshot-handoff.md#r-hand-16)
<!-- relation: references target=R-HAND-2 -->
- references: [R-HAND-2](topic-snapshot-handoff.md#r-hand-2)
<!-- relation: references target=R-HAND-4 -->
- references: [R-HAND-4](topic-snapshot-handoff.md#r-hand-4)

**서버 — 동시 도착을 흡수하는 쪽이다.**

- topic 별 **single-flight / cache**.
- **bounded wait** — 큐에서 기다리게 하되 무한이 아니다.
- **DB/Redis I/O 자체 상한** (서버 요청 전체 absolute deadline —
  [R-HAND-16](topic-snapshot-handoff.md#r-hand-16) — 만으로는 부족하다.
  그 부족의 사유는 [R-HAND-4](topic-snapshot-handoff.md#r-hand-4) 가 소유한다).
- **bounded wait/queue 후 예산이 소진됐을 때만 close 1013**.
  ⚠️ 여기서 쓰는 close 1013 은 새 신호가 아니라
  [R-HAND-14](topic-snapshot-handoff.md#r-hand-14) 의 서버 snapshot 총 예산 초과 종결과
  [R-HAND-2](topic-snapshot-handoff.md#r-hand-2) 의 출시용 최소 계약이 이미 정한 그 종결이다 —
  대기 없이 곧바로 닫는 용도로 쓰면 위의 "즉시거절 금지"를 우회하는 것이 된다.
- **서버 실패 cooldown** — ⚠️ **실패 fan-out 이 폭주를 재동기화한다**: single-flight 하나가 실패해
  대기 소켓을 동시에 닫으면 재연결이 다시 정렬된다. 서버 실패 cooldown 과
  클라 jitter([R-CLI-24](ios-topic-state-machine.md#r-cli-24)) 가 **함께 있어야** 완화가 성립한다.

근거(baseline): **E1 [코드]** `app/database_settings.py:85-86` — PostgreSQL `POOL_SIZE = 3`,
`MAX_OVERFLOW = 2` (**최대 5**), 주석은 *"RDS db.t4g.micro 메모리 절약"*. `app/database.py:36-38`
이 그 값을 `create_engine` 으로 넘긴다(구 좌표 `app/database.py:30` — **값 불변, 소유자만 이동**).
**E1-b [코드]** `app/database_settings.py:113` — online `pool_timeout = 10초`(구 SQLAlchemy 기본
30초). 아래 "그대로 쌓인다" 는 **무한 대기가 아니라 10초 상한**이 됐다 — 쌓인 요청은 그 뒤
`sqlalchemy.exc.TimeoutError` → 503 으로 접힌다. 흡수 장치의 필요성은 그대로다(접히는 것이
서비스되는 것은 아니다). **E2 [코드]** `app/topic_initial_snapshot.py:1064-1076`은 요청 topic을
순차 순회한다. `app/topic_initial_snapshot.py:497-838`의 LOAD-S5/S6/S7 래퍼는 같은
`(topic, generation, supported, enabled)` key를 shared build 하나로 합치고 성공 결과만 최대 1초
cache하며, waiter마다 별도 payload 복사본과 요청 예산을 유지한다. 새 shared flight는
`app/config.py:650-660`의 기본 4-slot FIFO admission을 남은 S3 예산까지만 기다리고, 실제 worker는
`app/topic_initial_snapshot.py:841-895`에서 독립된 shared 예산과 S3 checkpoint·S2 I/O 상한을
그대로 쓴다. transient build 실패는 exact key에 기본 1초 cooldown을 arm하고, 그동안 후속 요청은
flight·admission·builder를 시작하지 않는다(`app/config.py:663-670` ·
`app/topic_initial_snapshot.py:651-709` · `app/topic_initial_snapshot.py:733-817`). fatal·영구 DB
오류와 caller 취소, admission 대기 deadline은 cooldown 대상이 아니다. **E2-inf [추론]** ⇒ 연결당
topic 순회는 여전히 **순차**지만 같은 key의 요청은 shared task 하나에 합류하고 새 shared task의
admission 점유는 기본 4다. 서버 실패 직후 같은 key 재진입은 짧게 비동기화됐지만 대기 **시간**만
bounded이고 queue 개수 hard cap은 없다. 클라이언트 jitter·재시도 상한·exact-scope cooldown은
iOS `45a8a12`에서 [R-CLI-24](ios-topic-state-machine.md#r-cli-24)로 **구현**됐고, 아래 좌표는
**pinned** iOS `99316a9` 트리에서 재도출한 것이다 — 구현 시점 commit 과 pin 은 같지 않다
(`ios/FXi/Services/WebSocketService.swift:1430-1448` ·
`ios/FXi/Services/WebSocketService.swift:1543-1583` ·
`ios/FXi/Services/WebSocketService.swift:1622-1644` ·
`ios/FXi/Services/WebSocketService.swift:2057-2116`). 그리고 pinned 트리에는 이 네 좌표가 덮지
않는 축이 하나 더 있다 — **45초 무수신 재검증 경로 자체의 full jitter** 로, 이 문서가 요구하는
"재구독을 흩뜨린다"의 재검증 쪽은 이것으로 **충족된다**: `topicRevalidationJitterMaxSeconds = 2`
(`ios/FXi/Utils/Constants.swift:268-269`)를 `topicRevalidationDelayNanoseconds`
(`ios/FXi/Services/WebSocketService.swift:951-956`)가 `U(0, 2초)`로 바꾸고,
`revalidateSilencedTopic` 이 그만큼 기다린 **뒤에야** 재검증 subscribe 를 보낸다
(`ios/FXi/Services/WebSocketService.swift:1196-1229`). jitter seam 의 프로덕션 기본값은
`Double.random(in: 0...1)` 이고(`ios/FXi/Services/WebSocketService.swift:99` ·
`ios/FXi/Services/WebSocketService.swift:165`), 이미 예약된 재검증이 있거나 channel 이 없거나
변환이 실패하면 **지연 없이 보내는 fallback 대신** degraded 로 접는다
(`ios/FXi/Services/WebSocketService.swift:1196-1218`).
⚠️ 이 축은 `45a8a12` 에는 **없었다**(그 트리의 `FXi/Utils/Constants.swift` 에
`topicRevalidationJitterMaxSeconds` 부재) — 그러니 "`45a8a12` 에서 구현됐다"를 pinned 트리의 클라
절반 **전체**로 읽으면 안 된다. 서버 기본 1초와 클라이언트 jitter를 사용한
45초 다중 클라이언트 LOAD-S4 리허설은 GO였지만 waiter 개수 hard cap과 영구 activation은 별도다.
따라서 리허설 통과를 메모리 상한이나 운영 활성화 완료로 쓰지 않는다. 취소된 sync worker는 다음 협력
checkpoint까지 잠시 남을 수 있어 admission 4가 실제 executor thread 점유의 순간 상한은 아니다.
<!-- /rid: R-LOAD-3 -->

---

## arming 게이트 — 이 문서가 지는 몫

<!-- rid: R-LOAD-1 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-load-1"></a>
### R-LOAD-1 — 게이트 3: 45초 동시 재구독 폭주 완화
[제안·결정 대기]
<!-- relation: references target=R-CLI-24 -->
- references: [R-CLI-24](ios-topic-state-machine.md#r-cli-24)
<!-- relation: references target=R-LOAD-3 -->
- references: [R-LOAD-3](#r-load-3)
<!-- relation: references target=R-LOAD-4 -->
- references: [R-LOAD-4](#r-load-4)

ADR-041 의 파생 숫자 숨김 정책 제안은 **조건부**로만 수용된다. 그 조건인 **arming 차단 게이트 3개**
중 세 번째가 이 문서의 몫이다 — 조건 자체는 ADR-041 의
[R-DEC-5](../DECISIONS.md#r-dec-5) 가 소유한다.

> 3. **45초 동시 재구독 폭주 완화** — [R-LOAD-4](#r-load-4) · [R-LOAD-3](#r-load-3) ·
>    [R-CLI-24](ios-topic-state-machine.md#r-cli-24) 계약이 구현·검증됐는가.

⚠️ 이 게이트가 여기서 `[제안·결정 대기]` 로 표시되는 것은 **완화 계약이 미결정이라는 뜻이 아니다.**
[R-LOAD-4](#r-load-4) 와 [R-LOAD-3](#r-load-3) 은 그 자체로 **확정**이며, 미결정인 것은
"이 셋이 서면 파생 숫자 숨김 정책을 arming 한다"는 **조건부 수용** 쪽이다.
<!-- /rid: R-LOAD-1 -->

---

## nginx 상한

<!-- rid: R-LOAD-2 -->
<!-- requirement-meta: disposition=active owner=LOAD -->
<a id="r-load-2"></a>
### R-LOAD-2 — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후

**nginx — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후.**
이 항목은 **확정**이다(ADR-041 [R-DEC-4](../DECISIONS.md#r-dec-4)).

✅ **`limit_req` 축은 이행됐다**(C1 `463c880`). `location = /ws` 가 **전용 zone**
`ws_handshake_limit`(10r/s, `nginx/conf.d/default.conf:25`)로 `limit_req burst=20 nodelay` +
`limit_req_status 429` 를 건다(`nginx/conf.d/default.conf:89-90`). `/api/` 의 `api_limit`(3r/s)
을 공유하지 않는다(`nginx/conf.d/default.conf:19` · `nginx/conf.d/default.conf:116-118`).
⚠️ **`limit_conn` 은 여전히 `/ws` 에 없다** — 아래 NAT 사유로 계측 후 결정이 유지된다.

→ handshake 폭주는 `limit_req` 로 막는다. 다만 **IP별 `limit_conn` 은 모바일 캐리어 NAT 위험이 크다**
(한 IP 뒤에 다수 사용자) → **계측 후 결정**. 이 항목은 "신설 vs 위험 수용" 이분법이 아니었다.

근거(baseline): **E4 [코드]** `nginx/conf.d/default.conf` 의 `location = /ws` 는 **86~110행** 블록이고
그 안에 전용 zone 기반 `limit_req` 가 **있고**(89~90행) `limit_conn` 은 **없다**. `location /api/` 는
별개 zone 으로 둘 다 있다(116~118행, C1 `5429d0f` 로 burst 20 + 429). zone 정의는 파일 상단 19·25·26행.

⚠️ 10r/s 는 **사용자 트래픽 실측이 아니다** — v2 구독자 실트래픽이 없다. W=4 identity canary
처리량(~13/s) 아래의 capacity guard 이고, handshake 자체는 인증을 돌리지 않으므로 —
인증은 subscribe 처리에서 `authorize_subscribe` 로 일어난다(`app/topic_dispatcher.py:715`) —
"연결 1개당 subscribe 최소 1회"라는 **대리 지표**로 묶은 값이다. zone 자체는
`nginx/conf.d/default.conf:25` 이고 적용은 `nginx/conf.d/default.conf:89` 다.
초기 phased 의 `lrs=` 관측으로 재평가한다.
<!-- /rid: R-LOAD-2 -->
