# 무료/구독 차등 접근 모델 — ADR 초안 (Draft, rev5 · Proposed 후보)

> **Status**: **Proposed ADR-039** (2026-07-17, [DECISIONS.md](DECISIONS.md) 등재) — 설계 수렴 rev5, codex 5-round. Final 승격엔 제품 결정 **S4만** 남음 (S5=bounded-lease v1 15분 확정 2026-07-25 → 1C 착수 가능 / S6=24h hard cutoff 확정 2026-07-21, 서버+iOS 구현 완료 dev-side). **배포 완료(2026-07-21): 효율화 slice(refresh_not_before) + step 3 무료 hourly endpoint를 FX 3탭(usd/jpy/eur)으로 확장 — 서버 프로덕션 live(acea749), iOS dev-side 활성(3f5801e/e740ed2/57f6648). 출시앱 v1.2.2 legacy엔 무료 탭 미포함 → 실사용자 노출·트래픽 실측은 App Store 배포(별도 GO) 이후. **jpy/eur dev 기능 E2E PASS(2026-07-21).**
> **Scope**: 비구독자 hourly 무료 스냅샷 + 최신-데이터 endpoint 접근 강제 + KRX entitlement 게이트 + iOS·Android 양 플랫폼 legacy 종료.
> **닫는 것**: [ADR-038](DECISIONS.md#L5977) 잔여 Open 2(WS per-user 인증). `krx_visible = G3 ∧ G2 ∧ G1 ∧ premium` 전 표면 준수.
> **supersede/연동**: [REALTIME_ARCHITECTURE_PLAN.md:479](REALTIME_ARCHITECTURE_PLAN.md#L479) 레거시 제거 계약(양 플랫폼 <1% + 6개월)을 §4.4/S4에서 명시적 조정. topic 계약 = [REALTIME_V2_CLIENT_GUIDE.md](REALTIME_V2_CLIENT_GUIDE.md). 전략 = iOS reference → Android 이식.
> **rev5 변경**: codex 리뷰 1 High + 2 Medium. **무료 hourly에서 KRX 항상 제외**(High) / WS 전체-요청 실패 frame + bounded-lease 확정(Medium 1) / Stage B 측정 = store console primary + Android 토큰 신호(Medium 2).

---

## 1. Context

제품 방향(2026-07-10): **비구독자 = 매시간 고정 갱신 스냅샷 / 구독자 = 실시간 WS**. 최신 rate/graph endpoint 대부분 무인증 → 매시간 제한이 UI에만 존재 → 서버 강제 필요.

### 1.1 무인증 최신-데이터 endpoint 전수 (2026-07-17)

main.py 890~2620 `verify_firebase_token`/`require_premium` 0건. "호출 확인"과 "공개=차단 대상"은 별개:

| endpoint | main.py | 신규앱 caller | 차단 |
|---|---|---|---|
| `/api/rates` | :992 | iOS ✅ / Android ✅([FXiApiService.kt:25]) | B |
| `/api/rates/{currency}` · `/api/banks/{pair}` · `/api/investing/{pair}` | :1035/:954/:945 | ❌ (공개=차단대상) | B |
| `/ws` legacy `type:rates` | :889 | iOS ✅ / Android ✅([WebSocketService.kt:177]) | B |
| `/api/graph/{currency}` | :2130 | iOS ❌(dead-runtime) / Android ✅([FXiApiService.kt:28]) | B |
| `/api/v2/graph/tab` · `/api/v2/topics/snapshot` · topic WS · `/api/v2/graph/catalog` | :2542/:2615/:889/:2434 | iOS ✅ / **Android ❌(v2 미사용)** | **A** |
| `/api/news` | :2057 | iOS·Android | §7 S2 |

> ⚠️ **Android = 완전 legacy**(v2 grep 0건). 운영 v1.2.2 실사용은 §5 측정.

---

## 2. 확정 결정

- **D1** hourly 스냅샷 = Firebase 인증만(익명·premium 불요). **단 KRX 항상 제외**(§3.1, codex High).
- **D2** strict-신규표면: 최신 realtime 표면(topic WS·v2 snapshot·GraphV2·catalog) = 출시 시 **premium**. hourly는 인증만.
- **D3** 배포본 = iOS·Android 모두 v1.2.2 계열.
- **D4** 신규 앱 legacy anon fallback 금지(양 플랫폼).
- **D5** 무료 그래프 = 실제 hourly-clamped(모의 제외, KRX 없이). 숨김은 비상 fallback만.

---

## 3. 접근 계약

| 주체 | 허용 |
|---|---|
| **무료** (Firebase, 비구독) | hourly 스냅샷만 (rate + real hourly 그래프, **KRX 제외**) |
| **구독** (premium) | topic WS + v2 snapshot + GraphV2 + catalog + hourly. **KRX는 entitlement 추가** |
| **v1.2.2 (iOS·Android)** | legacy REST/WS만. 유예 후 종료 |
| **신규 앱** | legacy anon fallback 금지 |

### 3.1 Stage A 권한 매트릭스 (ADR-038 준수)

| 표면 | 요구 권한 |
|---|---|
| hourly 무료 스냅샷 | **Firebase 인증만 + KRX 항상 제외**(codex High — 무료엔 KRX 불포함, premium+entitled는 최신 경로 사용) |
| 최신 Graph/topic/catalog (비-KRX) | Firebase + premium |
| KRX WS·REST snapshot | Firebase + premium + KRX entitlement |
| Graph catalog/tab의 KRX series | entitlement 없으면 서버 제외 (ADR-038 G2) |

- 판정 = 서버 단일 `krx_visible = G3 ∧ G2 ∧ G1 ∧ premium`. 클라 조합 금지.
- **캐시 = 전역 게이트(G2∧G3) 후 저장 → serve-time per-user(G1∧premium) 필터**. 개인화 결과 공용 키 재캐시 금지.
- **무료 hourly precompute는 KRX 없는 별도 산출물**(무료 endpoint에 KRX가 아예 안 들어가므로 serve-time 필터 불요).

### 3.2 KRX 존재 완전 비노출 계약 (사용자 지시 2026-07-17, authoritative)

**비구독자는 KRX(미국달러선물)의 존재 자체를 알 수 없어야 한다.** KRX는 구독자 중에서도 서버 등록된
일부(entitlement)만 보는 표면이므로, 무료 티어의 어떤 표면에도 다음을 포함하지 않는다:

| 표면 | 계약 |
|---|---|
| 무료 rate payload | `bank=="krx"` / `currency=="usd-krw-futures"` (FX shape) **및** `source=="krx"` / `asset=="usd-krw-futures"` (topic-native shape, 테더 N4) 전부 제외. `_assert_krx_free`는 양 shape 검사 |
| 무료 graph series | `krx.*` id 제외 (서버 `exclude_krx=True` + 클라 어댑터 strip 이중) |
| 무료 UI | 잠금 티저/행/범례/아이콘/명칭 일절 금지 (잠금 카드로도 노출 금지 — codex 티저 제안 기각됨) |
| Paywall 문구·기능 목록 | KRX 미언급 (구독 유도 문구에 "달러선물" 포함 금지) |
| 접근성(VoiceOver) 라벨 | KRX 미언급 |
| 소스/은행 설정 화면 | 무료 경로에서 KRX 항목 미노출 |

- 구현 상태: 서버 3중(legacy allowlist + `exclude_krx` + `_assert_krx_free`) + iOS 3중(어댑터 series strip +
  buildRatesState 명시 제외 + prepared 기반 토글). **테더 N4 land 전 `_assert_krx_free` source/asset shape 확장 필수**
  (topic-native 우회 차단) — KRX가 잠깐이라도 무료 payload/UI에 들어가는 중간 상태 금지.

---

## 4. ADR 4축

### 4.1 인증된 무료 hourly endpoint
- 신규 self-describing endpoint(예: `GET /api/v2/free/snapshot?tab=`), day-1 `verify_firebase_token`, premium 불요, **KRX series 미포함**.

### 4.2 매시간 고정 갱신 계약 — HH:30 basis (2026-07-18 개정)
- **시간 계약(사용자 + codex 2026-07-18)**:
  ```text
  as_of:        매시 HH:30:00 (마지막 :30 경계. 09:00 개장 → 09:30 첫 무료 정보로 30분 만에 장 파악)
  rates/graph:  timestamp <= as_of 인 마지막 데이터만 포함 (쿼리 불변식 — 라벨과 데이터 정합)
  generated_at: 실제 스냅샷 조립 완료 시각
  UI:           "매시 30분 갱신 · HH:30 기준"
  ```
- cutoff은 라벨이 아니라 **쿼리 불변식**: rate = `fetch_rate_entries_until`(`timestamp <= as_of` 각 소스 최신 1건),
  1d graph = `build_tab_1d_payload(now_kst=as_of)`(잘라낼 경계=as_of 고정 — cron 지연/재빌드에도 초과 봉 유입 불가),
  1w/3m/1y = hourly/daily canonical이라 as_of(HH:30) 초과 bucket 자체가 없음. 배경: :00 floor + 최신값 조회 시
  "21:00 기준" 라벨에 21:19 값이 보이는 구조적 mismatch(사용자 실측 2026-07-18).
- cron 매시 :30 발화. keep-last-good(성공+non-empty일 때만 SET) + 원자적 Redis SETEX + long-TTL + 무료용 별도 키(`free:snapshot:{tab}:{period}`).
- **serve 모델(구현 확정, codex 리뷰 반영)**: cron이 **Redis 단독 canonical writer**. serve는 **cron canonical만 반환하고 DB로 재생성하지 않는다** — 이게 무료=1시간 고정의 핵심(serve가 DB 최신값으로 rebuild하면 같은 시간대에도 값이 바뀌어 유료 실시간 차등이 깨짐). serve = Redis canonical read(timeout 2s + serve-time 재검증[KRX/empty fail-closed] + circuit) → 성공값 process-local last-good 보존 → Redis 장애 시 마지막 canonical → **canonical 전무 시 503**(최신값 fabricate 금지). serve는 Redis/DB에 쓰지 않음.

### 4.3 최신 ↔ 무료 분리
- 무료 그래프는 hourly `as_of` 별도 snapshot(최신 GraphV2 재사용 금지).

### 4.4 강제선 / 종료 (2단, 양 플랫폼)
- **soft 전환기** → **Stage A(출시 시)**: §3.1 매트릭스. 선행 = 클라·서버 토큰 전달 + §6 legacy 이탈. behavior-change-0 아님.
- **Stage B(유예 → hard)**: legacy rate/graph 종료 + 웹 디버그 페이지([templates/index.html](templates/index.html):1437/:1862) 처리.

**종료 조건 (양 플랫폼, [REALTIME:479](REALTIME_ARCHITECTURE_PLAN.md#L479) 승계):**
1. **iOS·Android 양쪽 모두** legacy 활성 사용자 < 1%(한쪽만 <1%면 보류).
2. 최소 유예 경과 — **기간은 S4 제품 결정**(기존 6개월 유지 vs 단축).
3. unknown·bot 잔여는 종료 기준 아님.

---

## 5. Legacy 잔여 측정 (Stage B 근거)

**측정 난점(codex Medium 2)**: 구버전은 X-Client-*를 안 보냄. 서버 로그 단독으론 버전 판별 한계.

- **Primary = App Store Connect · Play Console 버전별 활성 기기 비율**(우리 계측 독립, 로그아웃 사용자 포함, 봇 무관). 종료 조건 <1%의 authoritative source.
- **Secondary(서버 보조)**:
  - **iOS legacy**: `FXi/1` UA(약한 신호, REST 토큰 없음 — [APIService] Authorization 전무).
  - **Android legacy**: **Firebase Bearer 토큰 첨부**([NetworkModule.kt:37] auth interceptor → 모든 요청에 `Authorization: Bearer`) → "유효 토큰 + X-Client-* 없음 + legacy endpoint" 조합으로 **실사용자↔봇 구분 + UID 카운트 가능**(봇은 유효 토큰 없음). 서버가 legacy endpoint에서 토큰 presence/UID를 로깅해야 활용.
  - 신규 앱: X-Client-* 헤더.
- **⚠️ client metadata는 출시+채택 후에야 데이터 생성**(§6 step 2는 capability 배포일 뿐 — 관측은 신규 앱 release 이후). 필요 시 **telemetry-only 중간 버전** 선출시 옵션.

---

## 6. 롤아웃 (dormant→flip, 양 플랫폼)

1. 무료 그래프 = real hourly 확정(D5).
2. **iOS·Android 공통 client metadata(`X-Client-Platform/Version/Build`) + nginx log_format/미들웨어**([nginx.conf:23]에 `$http_x_client_*` 추가). **← 최저위험 첫 슬라이스**. ⚠️ 데이터는 신규 앱 release 후 생성.
3. **서버 hourly endpoint(인증 즉시 ON) + 최신 표면 인증 capability 추가하되 최신 표면 enforcement = OFF**(hourly 인증은 dormant 아님).
4. **iOS legacy 이탈**: 4a REST/WS 토큰 전달 → 4b `dxy:spot` topic 신설 → 4c 부팅·offline·stale를 topic snapshot/cache 기준 전환 → 4d /api/rates+legacy WS 제거.
5. **Android 이식**(iOS reference 경화 후, 완전 legacy라 topic client 신규 구현). ℹ️ **REST Authorization interceptor는 이미 보유**([NetworkModule:37]) → REST 인증 거의 free, WS subscribe payload 토큰만 추가.
6. **TestFlight/내부 테스트**(양 플랫폼): 로그인·무료·구독·재연결·토큰 만료·KRX entitlement grant/revoke.
7. **Stage A enforcement ON** + 재검증 후 출시.
8. **양 플랫폼 legacy <1% + 유예(S4) 충족 시** Stage B 제거.

---

## 7. 미결 sub-decision

- **S1** 무료 그래프 → RESOLVED = real hourly-clamped(KRX 없이).
- **S2** `/api/news` → 권고 무료 공개 유지.
- **S3** 익명 무료 → RESOLVED = 인증 필수.
- **S4 (제품 결정) legacy 유예 기간**: 기존 6개월([REALTIME:483]) 유지 vs 단축. 권고 = **양 플랫폼 <1% + 최소 30일**(codex, <500명+개편 감안) — 단 **미업데이트 사용자 지원 종료 수용** 결정 필요. **ADR-039에서 명시적 supersede**.
- **S5 (제품 결정) KRX revoke 반영 지연 — RESOLVED = bounded-lease v1, 최대 15분** (2026-07-25, 사용자+codex+Claude).
  즉시 제거(UID registry + Redis 제어 이벤트)는 **별도 후속** — 운영·라이선스 요구가 확인될 때 재검토.
  - **왜 15분**: leak window(권한 상실 후 KRX 수신 지속)를 15분으로 상한. 토큰 수명(~1h)에 정렬하면 지연이 1시간이라 과다.
  - **구현 계약**: 서버 lease **15분** / 클라 재인증 **~12분 + jitter**(3분 여유로 지각 재인증 흡수, thundering herd 회피) /
    재인증 실패·ack 미수신 시 서버가 15분 경계에서 해당 topic을 registry에서 제거하고 `reauth_required` 전송.
  - **핵심**: leak window를 가르는 건 토큰 신선도가 아니라 **서버의 entitlement 재조회 주기**다. 클라가 캐시 ID token을
    보내도(Firebase는 만료 전까지 캐시 반환) 서버가 UID 기준으로 premium/KRX entitlement를 다시 조회하므로 권한 변화는 정확히 반영된다.
  - ⚠️ **구현 규모 실사(코드 확인 2026-07-25)**: "lease timer만 추가"가 아니다. `TopicRegistry`(app/topic_dispatcher.py:46)는
    `Dict[WebSocket, Set[str]]`라 **per-subscription 메타데이터가 없고**, `/ws`(app/main.py:889)는 **현재 무인증**이다.
    → 필요한 것 = (a) subscribe 경로 인증 (b) (ws, topic)별 uid·만료 메타 (c) 만료 sweep (d) `reauth_required` 발신.
- **S6 (제품 결정, codex Medium) last-good 최대 stale 정책**: serve의 process-local last-good은 현재 **무기한**(Redis/cron 장기 장애 시 며칠 stale canonical도 반환 — 실시간 우회는 아님, `as_of`가 정직하게 old 표시). (a) 최대 stale N시간 초과 시 503 전환 vs (b) 무기한 유지 + 클라 UI에 stale 명시. **결정 = (a) 24h hard cutoff** (2026-07-21, 코덱스+Claude+사용자). 구 비결정 혼합(process-local 생존 중 무기한 + Redis ~25h TTL → 재시작 여부로 stale-or-503)을 (now-as_of)>=24h serve 거부로 일관화. **서버+iOS 구현 완료**(서버 d9e60ab: `is_snapshot_too_stale` per-candidate age + stale Redis가 fresh local 안 덮음 / iOS 7050bd3: `.unavailable` + isTooStale age>=24h[future clock-skew 허용] + gated current·displayData[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파). codex Blocker/High/Medium 0. source-level staleness는 별도 source-health.

> S4·S5·S6는 서버 MVP(step 2·3)와 **독립**. S5=WS(1C) 전 확정 필요했고 **2026-07-25 확정 완료** /
> S4=**Stage B**(legacy 종료) 전 / S6=App Store 출시(실사용자 client) 전.
> ⚠️ 구 표기는 S4·S5를 "Stage B/WS 전"으로 묶었으나, **S4는 WS 인증(1C) 블로커가 아니다**(legacy 종료 시점 결정).

---

## 8. WS 인증 계약 (출시 임계 경로, testable)

**요청**: `{"type":"subscribe","request_id":"<uuid>","id_token":"<firebase>","topics":[...]}`

**전체-요청 실패**(토큰 자체 무효 — codex Medium 1): `{"type":"subscription_error","request_id":"<uuid>","error":"invalid_token"}`

**인증 성공 + per-topic 결과**(등록 전 검증, 통과 topic만 registry 등록):
```json
{"type":"subscription_ack","request_id":"<uuid>",
 "accepted_topics":["fx:usd-krw"],
 "rejected_topics":[{"topic":"krx:usd-krw-futures","error":"krx_entitlement_required"}]}
```
- 오류 코드: `invalid_token`(전체) / `premium_required` · `krx_entitlement_required`(per-topic) / 기존 `unknown_topic` · `topics_disabled`.
- 검증 실패 topic은 registry 미등록.
- **재인증**: 토큰 만료 전 갱신 + 재연결 시 재구독에 `id_token` 재첨부.
- **만료 시(bounded-lease v1, codex Medium 1)**: 만료 → registry에서 해당 인증 topic 제거 + `reauth_required` 전송.
  **lease = 15분 확정(S5 RESOLVED 2026-07-25)** — leak window 상한 15분. 클라 재인증 ~12분 + jitter.
  서버는 재인증 실패·미수신 시 15분 경계에서 제거. 즉시 revoke는 별도 후속.
- 구현 시 **KRX per-user 강제 동시 해소**(ADR-038 Open 2 close).

**§8.1 구현 계약 (test-first 전 확정, 2026-07-25 — codex 3 + Claude 1)**

lease 15분을 실제로 보장하려면 아래 4개가 계약이다. 각각 상태 전이 테스트로 고정한다.

1. **만료 강제는 subscriber 조회 경계에서** — `TopicRegistry.get_subscribers`가 만료 구독을 제외한다.
   주기 sweep은 `reauth_required` 통지·메모리 정리용이며 **강제 수단이 아니다**.
   - 왜: sweep-only면 실제 상한이 `15분 + sweep 주기`가 되어 문서의 "최대 15분"을 배신한다.
   - 왜 publisher가 아니라 registry: publisher가 `publish_topic`(app/topic_dispatcher.py:126)과
     `publish_topic_detailed`(:197) **둘**이고 앞으로 늘 수 있다. publisher마다 필터를 붙이면 drift하므로
     단일 choke point인 조회 경계에서 막는다(`subscriber_count`/`subscribed_connection_count` guard도 동일 경로).
2. **재인증 replacement** — 인증 성공한 subscribe에서 **reject된 topic은 registry에서 제거**한다.
   `TopicRegistry.register`는 additive union(`existing.update(topics)`, :66)이라 "accepted만 추가"하면
   권한을 잃은 이전 등록이 그대로 남아 lease 만료까지 계속 수신된다.
   - 단, **이번 요청에 언급되지 않은** topic은 건드리지 않는다(증분 subscribe 보존).
3. **snapshot도 accepted-only** — `send_initial_snapshots`(:288)는 현재 **요청 topics 전체**로 호출된다.
   1C에서는 **accepted subset**으로만 호출해야 한다. 아니면 "검증 실패 topic은 registry 미등록"을 지켜도
   reject된 topic의 스냅샷이 즉시 1회 새어 나간다.
4. **재인증 타이밍 상한** — 재인증 시작 = **`12분 − U(0,60s)`** ∈ [11:00, 12:00] (**하향 jitter만**).
   - ±jitter는 최악이 13분+가 되어 deadline 여유가 줄어든다. 하향 전용이면 최악이 12:00으로 고정.
   - ack timeout **10초**, 실패 시 **1회 즉시 재시도** → 최악 완료 ≈ 12:20, 15:00까지 여유 ≈ 2분 40초.
   - 클라 타이머는 **monotonic**(wall-clock 점프 시 deadline 계산이 깨진다).
   - 서버 deadline이 단일 진실 소스 — 클라 타이밍은 그보다 앞서기 위한 것일 뿐이다.

**Non-goal**: legacy 그래프 dead-code 삭제(별도 커밋) / 고급 rate-limiting(후속).

---

## 9. 구현 체크리스트

- [ ] 전 라우트 auth 감사 — 누수 0.
- [x] iOS·Android 공통 client-version metadata + nginx 로깅 (step 2 land 2026-07-17: server `55ab1d8` / iOS `1b736f2` / Android `5f93409`. 데이터는 신규 앱 release 후 생성 — nginx deploy/reload + 실 로그 cp/cv/cb 확인 별도).
- [x] hourly endpoint(인증만, **KRX 제외**, self-describing) + 매시간 계약(§4.2) — **step 3 land + 배포 2026-07-17** (app/free_snapshot.py + GET /api/v2/free/snapshot, MVP=usd·1d/1w/3m/1y). workflow 설계+adversarial + **codex MCP 다라운드**: B1~B4/N5/N6/NB → **freshness 불변식(serve canonical-only, DB 재생성 제거)** → validator Pydantic 스키마. **2026-07-18 HH:30 basis 개정(§4.2)**: as_of=마지막 HH:30 + `timestamp <= as_of` cutoff 쿼리 불변식(fetch_rate_entries_until + build_tab_1d_payload now_kst 주입) + cron :30 — 구 :00 floor의 라벨↔데이터 mismatch(사용자 실측) 해소. **S6 결정=(a) 24h hard cutoff(2026-07-21) — 서버+iOS 구현 완료(dev-side)**: 구 비결정 혼합(process-local 무기한 + Redis 25h TTL)을 (now-as_of)>=24h 렌더 거부로 일관화. **서버**(d9e60ab: `is_snapshot_too_stale` per-candidate age, stale Redis가 fresh local 안 덮음). **iOS**(7050bd3: `LoadState.unavailable` + isTooStale age>=24h[future asOf는 clock-skew 허용] + current/displayData gated[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파. codex Blocker/High/Medium 0). 실사용자 노출은 App Store 배포 후. iOS USD reference slice land(987d161). **FX 3탭 확장(2026-07-21)**: `FREE_SNAPSHOT_TABS=("usd","jpy","eur")` — jpy/eur는 usd와 동일 코드 경로(bank/investing rate reader + source_daily/hourly canonical + 1d intraday) 재사용, `TAB_ASSET`만 차이(별도 reader 0). tether는 free reader가 source_rates(거래소 데이터) 미조회로 rate가 비어 N4(별도 reader) 전까지 제외. 프로덕션 read-only 실증(jpy/eur 4기간 non-empty·asset 정확·KRX-free) + 계약 테스트 +3 + **프로덕션 배포(acea749, 2026-07-21)**. **iOS 활성화 완료(step 4, 57f6648) — freeConfig switch(usd/jpy/eur)로 FX 3탭 실렌더 dev-side 활성(합성 시뮬 게이트 포함)**. **jpy/eur dev 기능 E2E PASS(사용자 dev 빌드 2026-07-21)**: 그래프 4기간/기본 토글 investing+hana/은행목록/알림 통화(jpy-krw·eur-krw)/KRX·DXY 미노출 + VM-hoist(News 왕복·인접 왕복 재요청 0, 3탭×4기간=12 GET 초기 warm만) 전부 확인. 실사용자 노출·트래픽 실측은 출시앱 배포(별도 GO) 이후.
- [x] **테더 N4 (무료 테더 탭 백엔드 + iOS N4-4 + graph 하드닝)** — 무료 테더 rate는 grouped shape(`kind="source_grouped"`: `usdt_krw`[거래소 5] + `usd_krw_banks`[kb·hana] + `usd_krw_reference`[investing singleton], `primary_asset="usdt-krw"`)로 도입(FX는 flat `{asset,entries}` 그대로). **N4-1 land**(58b9d9c): `_assert_krx_free`가 FX shape(bank/currency)뿐 아니라 topic-native shape(source/asset)도 검사 → 테더 KRX(달러선물) 우회 차단. **N4-2a land**(grouped-aware validation 리팩터, codex Blocker/Medium 2라운드 반영): `_iter_rate_items`(shape 무관 순회 단일 진실소스)로 nonempty·within-as_of·precompute 카운트 통일 + rate.asset(grouped=primary_asset) grouped-aware + **3중 hybrid/KRX 방어** — (1) `_FreeRate`/`_FreeRateGrouped` `extra="forbid"`(반대 shape 컨테이너 키를 extra로 얹은 hybrid를 schema서 거부, 양방향) (2) `_assert_krx_free`가 `_iter_all_rate_dicts`로 entries+3그룹을 **kind 무관 동시 스캔**(hybrid가 반대 컨테이너에 KRX를 숨기는 우회 폐쇄, belt-and-suspenders) (3) `_GROUPED_RATE_TABS` **tab↔shape 결합**(grouped-USD/flat-tether 오염 canonical을 fail-closed 거부 → 구 FX client 깨진 200 회귀 차단). graph 절반은 코드 완료(tether series 정의 + `exclude_krx` 필터). **N4-2b land**(cutoff-aware grouped tether reader): 유료 토픽 grouper `build_tether_tab_payload`(정규화·정렬·investing singleton 무결성) 재사용 + cutoff fetcher만 신규 — `crud.get_source_rates_until`(source_rates cutoff 변형, `timestamp<=as_of`, rate_changed_at 미포함[정적 스냅샷]) + `free_snapshot.fetch_tether_grouped_rate_until`(거래소 5=source_rates / kb·hana·investing=usd-krw는 기존 FX cutoff reader 재사용 후 선별) → `build_free_snapshot_payload` tab 분기(tether=grouped / FX=flat). free↔paid shape 일치(iOS 단일 어댑터). dup-source/wrong-asset은 각 source explicit query라 자연 차단(entitlement 우회 아님, KRX는 조회 경로에 아예 없음 + `_assert_krx_free` belt-and-suspenders). codex 2라운드(N4-2a Blocker/Medium + N4-2b) 통과. **N4-3 land + 배포 + 프로덕션 verify**(08db271, 2026-07-22): `FREE_SNAPSHOT_TABS`에 tether 추가(precompute 순회 + endpoint 게이트 단일 진실소스 → 자동 활성) + build 분기 + serve tab↔shape 결합. codex 3라운드 통과. EC2 배포(build+force-recreate) 후 precompute 트리거 + Redis canonical 직접 검증 — **4기간(1d/1w/3m/1y) 전부 validate=True / kind=source_grouped / primary_asset=usdt-krw / as_of=HH:30 / stale=False / rate=거래소5(upbit·bithumb·coinone·korbit·gopax)+kb·hana+investing / KRX series·rate 0 / graph 실데이터 non-empty(1d 10/10 각 144pt · 장기 4/4)**. 서버 활성은 dormant(엔드포인트 auth-gated, 소비 client는 App Store 배포 후). 테스트 free_snapshot 72 passed / 전체 3604. **N4-4 land + dev E2E PASS(2026-07-22)**: iOS grouped adapter(FreeRate flat/grouped, kind-peek)/group-aware allowlist(KRX fail-closed)/코어 탭(source 바)/그래프(거래소 5토글·DXY↔선물 상호배타·구역 프리미엄 정합)/3 알림 preview(거래소 가격·김프·비교, no-persist + malformed-rate 크래시 가드)/라우팅(테더-first, 인증 게이트). **+ graph fail-closed 하드닝(codex 6라운드 Blocker 0)**: per-tab ID allowlist(4탭 카탈로그 1:1) + envelope 결합(assertEnvelope/decodeAndValidate behavioral) + content-level KRX guard(서버 `_assert_krx_free` graph + 클라 `hasKrxContaminatedPoint`, 4마커 대칭) — 서버 257a05d 배포·검증 / iOS 1cfa978·778d294·9c255eb. 실사용자 노출은 App Store 배포(TOPIC_V2 arming, 별도 GO) 후.
- [ ] §3.1 매트릭스 + 캐시 G2∧G3 전역 → serve-time G1∧premium.
- [ ] iOS 4a~4d → Android 이식(REST interceptor 재사용).
- [ ] WS 계약(§8): subscription_error + ack accepted/rejected + bounded-lease(15분) + reauth_required. **← 1C, S5 확정으로 착수 가능**
- [ ] 웹 디버그 페이지 Stage B.
- [ ] Stage B 측정: store console primary + 서버 보조(iOS UA / Android 토큰+UID).
- [x] **제품 결정 S5(revoke latency) = bounded-lease v1 15분 확정(2026-07-25)** — §7 S5 / §8 만료 항목 참조.
- [ ] **제품 결정 S4(유예 기간·supersede) — Stage B 전 확정**. ⚠️ S4는 *legacy를 언제 끄느냐*라 **Stage B 게이트**이고
      WS 인증(1C) 구현의 블로커는 아니다(구 표기는 S4·S5를 "Stage B/WS 전"으로 함께 묶어 오해 소지).
- [x] **제품 결정 S6(last-good 최대 stale) = 24h hard cutoff 확정(2026-07-21)** — **서버(d9e60ab, 배포)+iOS(7050bd3, dev-side) 구현 완료**((now-as_of)>=24h 렌더 거부; 서버 503 + 클라 `.unavailable`+fetch-독립 expiryTask). 실사용자 노출은 App Store 배포 후. source-level staleness는 별도 source-health. + [ ] validator value-level 완결(entry.rate 타입/point 내부).
