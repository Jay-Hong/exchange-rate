# Publisher health / SLO — topic-only 분할 (HEALTH)

- 책임: publisher health · SLO
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `8bbf9879e9d5fac3cb18ab7330ee4853ec550738`
- iOS 기준 commit: `94b20fa981a45c42eb396115a3224100c567a54f`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `d67dcbc660edb7992c1e6e565817a4c9b6b2aa522b8eef8190df4d6ad4735d9b`
- baseline SHA: `6d8a2bb22cd6a7f70dcfda186740c7888cdf9a60ce1e5aef94a577f0c7dc3cda`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

> **이 문서의 몫**: 클라이언트가 **구조적으로 판별할 수 없는** publisher 침묵을 무엇으로 덮는가 —
> 서버 publisher health / SLO 계약과, 그 계약이 arming 차단 게이트로서 갖는 지위.
> 서버 build·ack·close 계약은 [`topic-snapshot-handoff.md`](topic-snapshot-handoff.md),
> 클라 상태기계·재검증은 [`ios-topic-state-machine.md`](ios-topic-state-machine.md),
> 재구독 폭주 완화는 [`revalidation-and-load.md`](revalidation-and-load.md),
> 불변식·결정·arming 게이트는 [`../DECISIONS.md`](../DECISIONS.md) ADR-041 이 소유한다.
>
> ⚠️ 원문의 `§` 번호는 분할과 함께 사라진다 — 다른 문서 내용은 **RID 와 링크로만** 가리킨다.

---

## 요구사항

<!-- rid: R-HLT-1 -->
<!-- requirement-meta: disposition=active owner=HEALTH -->
<a id="r-hlt-1"></a>
### R-HLT-1

**서버 — 정상 initial snapshot _이후_ 의 publisher 사망은 클라가 판별할 수 없다**
(⛔ 파생 숫자 숨김 정책([R-DEC-2](../DECISIONS.md#r-dec-2), `[제안·결정 대기]`)을 수용할 경우
이것은 **arming 선행조건**이며 **후속이 아니다**)
([R-DEC-1](../DECISIONS.md#r-dec-1) — 45초는 데이터의 나이가 아니므로 **정상 침묵과 구분 불가**).
**서버 publisher health/SLO/알람**으로 덮는다.
이건 클라 계약이 아니라 **운영 계약**이다.
판별 불가의 코드 근거: baseline **B1**(publisher 두 모듈 내부 timer 부재, 범위 한정) · **B2**(USDT
coalesce = same rate + same 5s bucket → 침묵은 가격 안정이 아니라 tick 부재). 외부 caller 는 매초
wake-up 하지만 publisher 호출은 payload `is_changed` 분기 안이므로 무조건 재발행도 아니다
(`app/scheduler.py:1402-1412` · `app/main.py:947-970`).

**구멍이 어디서 생기는가.** LOAD-S3 구현으로 initial snapshot의 deadline·transient/fatal build
실패는 1013/1011 close로 바뀌어 더는 조용하지 않다(baseline D3). 다만 builder가 `None`을 반환하는
경로는 연결을 유지한 채 skip하므로([R-HAND-1](topic-snapshot-handoff.md#r-hand-1)), **연결·pong·ack·
lease가 전부 정상인데 snapshot이 한 번도 오지 않는 상태**는 아직 표현된다. 그 뒤 **정상 발행되던
publisher가 죽는 경우**도 침묵으로만 관측된다 — publisher 모듈에는 timer가 없고(baseline B1),
외부 caller도 변경이 있을 때만 publish한다(`app/main.py:947-970`). transport ping/pong은 **연결
생존만** 증명하지 특정 topic publisher의 생존을 증명하지 않는다.

코드 근거: baseline **B1**(data-plane heartbeat·주기적 재발행 부재) · **D3**(실패는 close,
`None`은 연결 유지 상태로 skip).

⚠️ **클라 축으로는 이 축을 메울 수 없다.** [R-CLI-1](ios-topic-state-machine.md#r-cli-1) 이
(a) 연결 상태 · (b) lease 갱신 · (c) 서버 close-on-send-failure 셋으로 침묵을 판단하지만,
그 셋이 덮는 것은 **transport / auth / subscription 이상까지**다. *"FX 침묵을 감지한다"* 로 넓게
읽으면 안 된다.

→ 따라서 이 축의 탐지 책임은 **서버 운영**이 진다. 그 구체 계약이 아래 R-HLT-3 이다.

<!-- relation: references target=R-HLT-3 -->
- references: [R-HLT-3](#r-hlt-3)
<!-- /rid: R-HLT-1 -->

<!-- rid: R-HLT-3 -->
<!-- requirement-meta: disposition=active owner=HEALTH -->
<a id="r-hlt-3"></a>
### R-HLT-3

**Publisher health / SLO 계약**

[R-HAND-1](topic-snapshot-handoff.md#r-hand-1)의 남은 `None` 침묵과 [R-HLT-1](#r-hlt-1)의 publisher
사망은 클라가 판별할 수 없다 — 서버가 이 계약으로 덮는다. 이 계약은 **확정**이며,
[R-DEC-2](../DECISIONS.md#r-dec-2) 의 `[제안·결정 대기]` **파생 숫자 숨김 정책** 제안의 채택
여부와 **무관하다**.
판별 불가의 코드 근거: baseline **D3**(`None` snapshot을 연결 유지 상태로 skip)
· **B1**(publisher 모듈 내부 timer 부재) · `app/main.py:947-970`(외부 caller 도 변경 시에만 publish).

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

⚠️ `publish_topic_detailed` 호출 구문은 dormant 모듈의 `app/atomic_fx_live.py:257` 에 존재한다.
다만 그 모듈은 운영 live 진입점에 연결되지 않은 상태다(`app/atomic_fx_live.py:9-13` ·
`tests/test_atomic_fx_live.py:323-350`). 따라서 정확한
현재 사실은 **호출 구문 0이 아니라 운영 live 배선 0**이다. 또한 lease 게이트 뒤 대상이 비면
`attempted=0` 하나로 반환하므로(`app/topic_dispatcher.py:400-430`), "전원 lease 만료"와 "구독자 0"이
**합쳐진다** — `no-eligible-lease` 와 `no-subscriber` 를 가르려면 그 분리가 선행이다.
<!-- /rid: R-HLT-3 -->

<!-- rid: R-HLT-2 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-hlt-2"></a>
### R-HLT-2

[제안·결정 대기]

**arming 차단 게이트 ① — publisher health / SLO 계약이 구현·검증됐는가.**

파생 숫자(김프/비교 spread)를 _시간_ 이 아니라 **전달 이상이 확정된 경우에만** 숨긴다는
[R-DEC-2](../DECISIONS.md#r-dec-2) 제안을 **수용하기 위한 조건 세 개 중 첫째**다.
⛔ **이 수용은 _조건부_ 다 — 조건이 안 서면 수용하지 않는다.** 클라가 못 보는 실패를 **아무도 안
보면** 그건 수용이 아니라 방치다([R-DEC-5](../DECISIONS.md#r-dec-5)).

판정 기준은 이 문서의 R-HLT-3 계약이다. ⚠️ **문서로 존재하는 것으로는 게이트가 열리지 않는다** —
`flushDisposition` / `deliveryOutcome` / `telemetryHealth` 세 축과 수치화된 탐지·대응 시간이
**구현·검증**까지 되어야 한다.

나머지 두 게이트는 다른 문서가 소유한다 —
② safety-stop 리허설 실측 [R-CUT-14](legacy-cutover.md#r-cut-14),
③ 45초 동시 재구독 폭주 완화 [R-LOAD-1](revalidation-and-load.md#r-load-1).
⛔ **셋 중 하나라도 미루면 수용으로 보지 않는다.**
⚠️ **①~③ 중 하나라도 출시 후속이라면 잔존 위험을 수용해서는 안 된다** — 그 경우 김프 표시 정책을
다시 논의해야 한다([R-DEC-3](../DECISIONS.md#r-dec-3)).

⚠️ **한계를 명시한다.** 클라 status signal(서버 health 결과를 클라에 전달)은 **후속**이다. 따라서
숨김 조건은 클라가 확정 가능한 전달 실패에 한하고, 그 밖의 조용한 사망에서는 **김프가 last-known
조합으로 계속 보일 수 있다** — 이건 **제품이 수용하는 잔존 위험**이며, 없애려면 status signal 이
필요하다([R-DEC-3](../DECISIONS.md#r-dec-3)).

<!-- relation: references target=R-HLT-3 -->
- references: [R-HLT-3](#r-hlt-3)
<!-- /rid: R-HLT-2 -->
