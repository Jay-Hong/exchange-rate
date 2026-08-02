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

작성 시점(2026-07-17) main.py 890~2620 `verify_firebase_token`/`require_premium` 0건.
"호출 확인"과 "공개=차단 대상"은 별개:

| endpoint | 신규앱 caller | 차단 | 상태 |
|---|---|---|---|
| `/api/rates` | iOS ✅ / Android ✅([FXiApiService.kt:25]) | B | 무인증 |
| `/api/rates/{currency}` · `/api/banks/{pair}` · `/api/investing/{pair}` | ❌ (공개=차단대상) | B | 무인증 |
| `/ws` legacy `type:rates` | iOS ✅ / Android ✅([WebSocketService.kt:177]) | B | 무인증 |
| `/api/graph/{currency}` | iOS ❌(dead-runtime) / Android ✅([FXiApiService.kt:28]) | B | 무인증 |
| `/api/v2/topics/snapshot` | iOS ✅ / **Android ❌** | **A** | ✅ **인증+premium+per-user KRX (E3, 2026-07-25)** |
| `/api/v2/graph/tab` · `/api/v2/graph/catalog` | iOS ✅ / **Android ❌** | **A** | 🔴 **무인증 잔여** — §3.1 아래 경고 |
| topic WS (`/ws` subscribe) | iOS ✅ / **Android ❌** | **A** | 🔴 무인증 잔여 — 1C |
| `/api/news` | iOS·Android | §7 S2 | 무인증 |

> ⚠️ **Android = 완전 legacy**(v2 grep 0건). 운영 v1.2.2 실사용은 §5 측정.
> ⚠️ 라인 번호는 자주 어긋나 **제거**했다(구 `:2615`는 실제 `:2745`였다) — 심볼명으로 찾을 것.
> ⚠️ **무인증 표면은 endpoint 목록이 전부가 아니다**: `/openapi.json` · `/docs` · `/redoc`이
> prod에서 무인증 200이다(2026-07-25 실측 — 49 paths, `/admin/api/*` 라우트명 전부 + 핸들러
> docstring + `EntitlementsResponse.krx_visible` 필드명 포함). §3.2 각주 참조.

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
| Graph catalog/tab의 KRX series | ⚠️ **미구현** — 아래 경고 참조 |

> 🔴 **graph v2 행은 "제외"까지만 참이고 per-user 판정은 아직 없다 (2026-07-26 현재).**
> 구 상태: `_effective_tab_series`가 전역 게이트(G2∧G3)만 봐서 `KRX_CLIENT_DISTRIBUTION_ENABLED=true`
> 한 줄로 **무인증 caller가 KRX 그래프를 받았다**(ADR-038 D3가 수용한 절충이나 §3.1과 모순).
> 현 상태: 무인증 경로는 **어떤 flag로도 krx를 싣지 않는다**(`krx_visible: bool = False` 파라미터
> + serve-time strip, §6.1). 남은 것은 **entitled 사용자에게 다시 보여주는 per-user 게이트** —
> 그게 land해야 이 행이 완전히 참이 된다.

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

> ⚠️ **이 계약의 범위 = 런타임 응답 표면** (2026-07-25 명시). topic 이름 `krx:usd-krw-futures`
> 자체는 (a) `REALTIME_V2_CLIENT_GUIDE.md` §0/§2.5에 공개 문서화돼 있고 (b) prod `/openapi.json`·
> `/docs`가 무인증이라 핸들러 docstring·`EntitlementsResponse.krx_visible` 필드명으로 노출된다
> (2026-07-25 curl 실측). 즉 §3.2가 실제로 보장하는 것은 **"제품 표면(응답 payload·UI·문구·알림)에
> KRX가 나타나지 않는다"**이지 "이름을 절대 알 수 없다"가 아니다.
> 이 한정을 적어두지 않으면 후속 검토가 OpenAPI 노출을 §3.2 위반 blocker로 과대평가한다
> (실제로 한 번 그렇게 보고됐다). **더 엄격히 가려면** prod에서 `docs_url`/`openapi_url`/`redoc_url`을
> 끄거나 `verify_admin` 뒤로 옮기는 별 슬라이스가 필요하다 — 무인증 API 맵이 `/admin/api/*`
> 라우트명까지 담고 있어 KRX와 무관하게도 권장된다(미착수).

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

### 6.1 KRX 관련 flag는 **둘**이고, 무인증 graph의 KRX는 **flag가 아니라 파라미터**다 (2026-07-26 확정)

| flag | 여는 표면 | 선행 조건 | 상태 |
|---|---|---|---|
| `TOPIC_DISPATCHER_ENABLED` | topic WS subscribe + `/api/v2/topics/snapshot` | ① **E3 REST twin 게이트** ✅ land ② **1C WS 인증** ③ **iOS bootstrap 3종 인증 이관** ④ **entitlement 조회 실패 503**(아래) | 🔴 false |
| `KRX_CLIENT_DISTRIBUTION_ENABLED`<br>(= G2, `KRX_FUTURES_ENABLED`와 AND) | KRX topic 발행/snapshot, KRX 알림 게이트 | 없음(topic 쪽은 E3+1C가 담당) | 🔴 false |

**무인증 graph v2(`/api/v2/graph/tab`·`/catalog`)의 krx.\* series는 어떤 flag로도 열리지 않는다.**
`_effective_tab_series` / `tab_1d_specs` / `build_catalog` / `build_tab` 등이 **호출자가 넘기는
`krx_visible: bool = False`**로 결정한다. 이 endpoint들엔 인증이 없어 넘길 사용자가 없으므로
실제 호출자는 항상 default(False)를 쓴다.

- **왜 env flag가 아닌가** (2026-07-26, codex 2R 수렴): 초안은 `KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE`
  라는 default-false 승인 flag였다. 그러나 그 flag의 **유일한 용도가 §3.2 위반 상태를 켜는 것**이라,
  per-user 게이트를 만들지 않은 채 `.env` 한 줄로 열 수 있는 통로가 남는다. 파라미터는 그 통로를
  없애면서도 계약 테스트가 `krx_visible=True`로 **entitled 구성을 그대로 검증**하게 해준다 —
  "계약 보존 vs 우회 통로 제거"가 trade-off가 아니었다.
- **왜 노출 자체를 삭제하지 않았나**: krx-in-graph는 실수가 아니라 **명세된 계약**이다(ADR-038 D3
  Open 2 + D4, GRAPH_API_V2_CONTRACT §3/§4). 파라미터 default를 False로 두면 노출은 닫히고
  계약은 코드·테스트에 남는다.

  ⚠️ **"판정만 넘기면 끝"은 아니다** (2026-07-26 정정, codex): graph 응답은 **사용자 공통 Redis 키**
  (`graph_v2:tab:{tab}:{period}`)를 쓰고 1d precompute도 `krx_visible` 기본값으로 굽는다. 판정만
  endpoint에 흘리면 **캐시 내용이 "누가 먼저 요청했는가"에 좌우된다** — 비인가 요청이 먼저 캐시를
  만들면 entitled 사용자도 KRX를 못 받고(1d precompute는 아예 KRX 없이 구워져 필터를 풀어도 복원 안 됨),
  반대 순서면 비인가 응답은 serve-time strip으로 안전하지만 캐시가 최초 요청에 의존한다.

  **per-user 게이트 slice가 실제로 해야 할 일**:
  ① `/api/v2/graph/tab`·`/catalog`에 **인증 추가**(현재 무인증이라 넘길 사용자 자체가 없다)
  ② **캐시 전략** — §3.1이 이미 처방한 대로 **superset 캐시(krx 포함) + serve-time per-user 필터**
     (개인화 결과를 공용 키에 재캐시 금지). 대안인 visibility별 캐시 키 분리는 키·precompute가 2배.
  ③ 1d **precompute도 superset**으로 굽기(현재 default False)
  ④ WARNING(`graph_v2_krx_stripped`) → debug 격하 (superset에선 제거가 정상 동작)
  ⑤ catalog는 서버 Redis 캐시가 없어 ①만 하면 되지만, 무인증인 한 per-user가 불가능한 건 동일

  **현재 subset 캐시는 §3.1 처방과 의도적으로 다르다**(더 엄격한 쪽): 오늘은 graph KRX를 볼
  legitimate 소비자가 0이라 **캐시에 KRX를 아예 넣지 않는 쪽**이 fail-closed다. superset은
  serve-time 필터를 load-bearing으로 만들므로, 그 전환은 위 slice에서 필터가 실증된 뒤에 한다.
- **guard는 2중이다**: build 경로(series accessor)만 막으면 **캐시 hit이 우회한다**
  (`/api/v2/graph/tab`은 Redis read-through라 hit 시 build를 안 거치고, 과거에 krx가 포함된 채
  구워진 payload가 TTL[최대 30분] 살아 있으며 startup DEL은 예외를 비치명으로 흡수한다 —
  codex Major, 실제 probe로 재현). → 3경로(장기 hit / 1d closed / 1d in_progress) **공통 exit**에
  serve-time `strip_krx_if_not_allowed(payload, krx_visible=False)`: `series` 리스트 +
  `in_progress` seed 두 shape를 copy-on-write로 훑고, 제거가 실제로 일어나면
  WARNING(`graph_v2_krx_stripped`)으로 캐시 잔존을 관측 가능하게 한다.
- **startup fail-fast는 기각**: CLAUDE.md "KRX optional source — baseline은 KRX 없이 항상 정상 동작"
  위반이고, 단일 인스턴스라 KRX **설정** 하나로 전면 장애가 된다. 노출되는 건 선물 가격 시계열
  (개인정보 아님)이라 서비스 중단과 균형이 맞지 않는다. → 해당 표면만 serve-time fail-closed.
- **catalog version**: 기본 응답에서 krx가 영구 제외되므로 `CATALOG_VERSION`을 `2026-07-26`으로
  올렸다(계약 식별자. iOS는 `version`을 decode만 하고 기능적으로 쓰지 않아 무해).
- **pre-flip 필수 항목** (`TOPIC_DISPATCHER_ENABLED=true` 전, 단순 후속 아님):
  ① iOS bootstrap 3종 인증 이관 ② 1C WS 인증
  ③ ~~entitlement 조회 실패의 HTTP 계약~~ → ✅ **land 2026-07-26**: 경계 = `TRANSIENT_DB_ERRORS`
  (`OperationalError`/`InterfaceError`/`TimeoutError`[풀 고갈]/`DisconnectionError` **4종**)
  **∧ `is_transient_db_error`**(영구 SQLSTATE deny-list: 28000·28P01·3D000·42501) →
  503 `{"error": "temporarily_unavailable"}`(WS `subscription_error`와 같은 어휘) + no-store,
  **Retry-After 없음**(PENDING 초 단위 신호와 충돌 + DB failover는 분 단위라 storm. 대신 가이드에
  **유한 재시도**를 계약으로 고정 — 3회 상한 + 조기종료 + 취소 4조건), 판정·빌드 **양쪽** 감쌈
  (한쪽만 감싸면 상태코드가 topic 종류에 따라 갈린다).
  ⛔ `except Exception`은 물론 **`except SQLAlchemyError`도 너무 넓다**(ProgrammingError·
  InvalidRequestError 등 영구 결함 포함). 클래스만으로도 부족해(PEP 249 `OperationalError`는
  인증 실패·DB 부재 같은 영구 케이스 포함) SQLSTATE deny-list를 덧댔다 —
  **allow-list가 아닌 이유**: connect 실패/failover가 SQLSTATE 없이 도착해(psycopg 3.3 실측:
  도달 불가 호스트 → `OperationalError`, `sqlstate is None`) allow-list면 **가장 중요한 케이스가
  500**이 된다. 비용 비대칭도 같은 방향(미지→transient는 재시도 낭비 / transient→permanent는
  회복 가능한 상태 포기).
  ⚠️ **단 그 비대칭은 재시도가 유한할 때만 성립한다** — codex 지적. 초안은 "bounded 재시도"라고
  적었지만 가이드엔 backoff 모양만 있고 **종료 조건이 없어** 계약상 무기한이었다. 이 endpoint는
  best-effort 가속기(정본은 WS snapshot)이므로 **3회 상한 + snapshot 도착 시 조기종료 + lifecycle·
  generation·게이트 변경 시 취소**를 가이드 §3에 명시해 전제를 실제로 만들었다.
  (현 iOS는 이미 만족 — KRX는 `krxBootstrapMaxAttempts=3`, tether/fx는 재시도 자체가 없다.
  위험은 "다음 구현자가 backoff만 보고 무기한 루프를 만드는 것"이었다.)
- 두 flag 모두 2026-07-22 route auth 감사 완화 이후 false다.

---

## 7. 미결 sub-decision

- **S1** 무료 그래프 → RESOLVED = real hourly-clamped(KRX 없이).
- **S2** `/api/news` → 권고 무료 공개 유지.
- **S3** 익명 무료 → RESOLVED = 인증 필수.
- **S4 (제품 결정) legacy 유예 기간**: 기존 6개월([REALTIME:483]) 유지 vs 단축. 권고 = **양 플랫폼 <1% + 최소 30일**(codex, <500명+개편 감안) — 단 **미업데이트 사용자 지원 종료 수용** 결정 필요. **ADR-039에서 명시적 supersede**.
- **S5 (제품 결정) KRX revoke 반영 지연 — RESOLVED = bounded-lease v1, 최대 15분** (2026-07-25, 사용자+codex+Claude).
  즉시 제거(UID registry + Redis 제어 이벤트)는 **별도 후속** — 운영·라이선스 요구가 확인될 때 재검토.
  - **왜 15분**: leak window(권한 상실 후 KRX 수신 지속)를 15분으로 상한. 토큰 수명(~1h)에 정렬하면 지연이 1시간이라 과다.
  - **구현 계약(정본 = §8.1)**: 서버 lease **상한** 15분 — 실제 값은 **가변**이다(§8.1 A1 3-way min).
    클라 재인증은 고정 주기가 아니라 **§8.1 D6 공식**(`max(0, lease_duration_seconds − 180 − U(0,60))`, jitter 하향 전용).
    제거·통지 시점은 topic별 `lease_expires_at`.
    ⚠️ 구 표기("서버 15분 / 클라 ~12분 + jitter / 15분 경계 제거")는 가변 lease 도입으로 **폐기** —
    10분 lease를 받고 12분에 갱신하면 이미 만료다.
  - **핵심**: leak window를 가르는 건 토큰 신선도가 아니라 **서버의 entitlement 재조회 주기**다. 클라가 캐시 ID token을
    보내도(Firebase는 만료 전까지 캐시 반환) 서버가 UID 기준으로 premium/KRX entitlement를 다시 조회하므로 권한 변화는 정확히 반영된다.
  - ⚠️ **구현 규모 실사(코드 확인 2026-07-25)**: "lease timer만 추가"가 아니다. `TopicRegistry`(app/topic_dispatcher.py:46)는
    `Dict[WebSocket, Set[str]]`라 **per-subscription 메타데이터가 없고**, `/ws`(app/main.py:889)는 **현재 무인증**이다.
    → 필요한 것 = (a) subscribe 경로 인증 (b) (ws, topic)별 uid·만료 메타 (c) 만료 sweep (d) `reauth_required` 발신.
    **(a)·(b) 는 land 됐다**(2026-08-02 `4a45173` — `TopicRegistry` 가 topic 별 `TopicLease{lease_id, uid,
    expires_at_mono}` 를 갖고, `/ws` subscribe 는 토큰이 실리면 Firebase 검증을 거친다). **잔여 = (c)·(d)**.
    ⚠️ 위 실사 자체는 폐기 대상이 아니다 — "lease timer만 추가"가 아니었다는 판단이 정확했다.
- **S6 (제품 결정, codex Medium) last-good 최대 stale 정책**: serve의 process-local last-good은 현재 **무기한**(Redis/cron 장기 장애 시 며칠 stale canonical도 반환 — 실시간 우회는 아님, `as_of`가 정직하게 old 표시). (a) 최대 stale N시간 초과 시 503 전환 vs (b) 무기한 유지 + 클라 UI에 stale 명시. **결정 = (a) 24h hard cutoff** (2026-07-21, 코덱스+Claude+사용자). 구 비결정 혼합(process-local 생존 중 무기한 + Redis ~25h TTL → 재시작 여부로 stale-or-503)을 (now-as_of)>=24h serve 거부로 일관화. **서버+iOS 구현 완료**(서버 d9e60ab: `is_snapshot_too_stale` per-candidate age + stale Redis가 fresh local 안 덮음 / iOS 7050bd3: `.unavailable` + isTooStale age>=24h[future clock-skew 허용] + gated current·displayData[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파). codex Blocker/High/Medium 0. source-level staleness는 별도 source-health.

> S4·S5·S6는 서버 MVP(step 2·3)와 **독립**. S5=WS(1C) 전 확정 필요했고 **2026-07-25 확정 완료** /
> S4=**Stage B**(legacy 종료) 전 / S6=App Store 출시(실사용자 client) 전.
> ⚠️ 구 표기는 S4·S5를 "Stage B/WS 전"으로 묶었으나, **S4는 WS 인증(1C) 블로커가 아니다**(legacy 종료 시점 결정).

---

## 8. WS 인증 계약 (출시 임계 경로, testable)

> **역할 분리**: **§8 = canonical wire schema**(메시지 형태의 단일 진실 소스) /
> **§8.1 = 구현 불변식**(시간·lock·상태 전이·롤아웃). 구현은 §8의 schema를 그대로 따른다.

### 8-A 클라 → 서버

상태를 바꾸는 모든 메시지는 **`request_id`를 갖고 ack을 받는다**(조용한 실패 금지).

```json
{"type":"subscribe",   "request_id":"<opaque-id>", "id_token":"<firebase>", "topics":["fx:usd-krw", …]}
{"type":"unsubscribe", "request_id":"<opaque-id>", "topics":["krx:usd-krw-futures", …]}
```
- `unsubscribe`에는 **`id_token`이 필요 없다** — 권한을 **축소**하는 작업이라 fail-open이다(§8.1 D8).

### 8-B 서버 → 클라

**전체-요청 실패** (토큰 자체 무효 / 형식 위반 / 일시 장애):
```json
{"type":"subscription_error","request_id":"<opaque-id>","error":"invalid_token"}
{"type":"subscription_error","request_id":"<opaque-id>","error":"temporarily_unavailable","retry_after_seconds":5}
```

**성공 ack** (subscribe/unsubscribe **공통**):
```json
{
  "type": "subscription_ack",
  "request_id": "<opaque-id>",
  "operation": "subscribe",
  "identity_generation": 3,
  "accepted_topics": [
    {"topic": "fx:usd-krw", "lease_id": "c7f1…", "lease_duration_seconds": 660}
  ],
  "rejected_topics": [{"topic": "krx:usd-krw-futures", "error": "krx_entitlement_required"}],
  "removed_topics": ["usdt:krw"],
  "active_subscriptions": [
    {"topic": "fx:usd-krw", "lease_id": "c7f1…", "lease_duration_seconds": 660}
  ]
}
```
#### 8-B-stage — **단계형 wire (2026-07-30 개정)**

⚠️ 위 ack은 **lease 기계를 전제**한다(`lease_id` / `lease_duration_seconds` / `identity_generation`).
lease가 없는 단계에서 그 필드를 채우면 **없는 사실을 만들어 내는 것**이고, 빼면 코드가 정본과 다른
**제3의 계약**을 만든다. 그래서 단계를 명시한다 — 구현은 자기 단계의 행을 그대로 따른다.

| 필드 | Stage 1 (subscribe + unsubscribe + flag-off — §8-B-term, **구 단계**) | Stage 2 (lease 도입) — **현재** |
| --- | --- | --- |
| `type` / `request_id` / `operation` | 그대로 | 그대로 |
| `accepted_topics` | `[{"topic": …}]` | ✅ **land** — `+ lease_id`, `+ lease_duration_seconds` (⚠️ `operation="unsubscribe"` 는 예외 — U4) |
| `rejected_topics` | `[{"topic": …, "error": …}]` | 그대로 |
| `removed_topics` | `[]` (제거 전이 미구현) | §C2 eviction 결과 |
| `active_subscriptions` | `[{"topic": …}]` (구 shape) | ✅ **land** — `+ lease_id`, `+ lease_duration_seconds` (subscribe·unsubscribe ack 양쪽) |
| `identity_generation` | **보류(미포함)** | 재검토 후 결정 |

⛔ **컨테이너 형태는 Stage 1부터 최종형(객체 배열)이다.** 문자열 배열로 시작하면 lease가 들어올 때
클라가 shape를 바꿔야 한다 — 그 비용을 피하는 것이 이 표의 요점이다.

⚠️ `identity_generation` 보류 근거: 폐기된 트랙에서 "소비자 0 + 새 소켓은 서버 상태가 처음부터라
reconnect를 전역 구분할 수 없다"는 이유로 제거 결정이 있었다. 그 판단 자체는 archive와 함께
보류 상태이고, **필수 필드로 모델링한 뒤 빼면 breaking**이므로 소비자가 생길 때 결정한다.

⚠️ **`subscription_error` 의 `request_id` 는 nullable 이다** (2026-08-01 기록).

정확한 근거는 **dict 로는 파싱됐지만 id 가 없거나 쓸 수 없는 경우** 하나다 — `request_id` 가
없거나, 문자열이 아니거나, 빈 문자열인 요청. 그때 서버는 echo 할 것이 없으므로 `null` 을 싣는다.

⛔ 한때 여기 "`id_token` 형식 위반처럼 파싱되기 전에 실패하면 id 를 모른다"고 적었는데 **틀렸다**
(codex Low): dict 가 파싱됐다면 `id_token` 유효성과 **무관하게** `request_id` 는 이미 읽을 수 있다.

⛔ 한때 "JSON 이 dict 로 파싱되지 않은 경우"도 여기 포함해 적었는데 **그것도 틀렸다**(codex Low):
비-JSON 과 비-dict 입력에 dispatcher 는 **프레임을 아예 보내지 않고 조용히 무시한다**(실측). 그게
의도다 — `/ws` 는 legacy 평문도 받는 경계라, 아무 텍스트에나 오류를 쏘면 구 클라에 스팸이 된다.
즉 nullable 은 **그 경로 때문이 아니다**.

**ack 은 nullable 이 아니다.** 인증 경로는 `request_id` 를 **검증 후 진행**하므로(없으면
`invalid_request` + `request_id: null` 로 종료, registry 불변) ack 에 도달한 요청은 반드시 id 를
갖는다. 클라가 필수 필드로 모델링해도 안전하다.
⛔ 한때 이 검증이 없어 **id 없는 인증 subscribe 가 `request_id: null` 인 ack 을 받았고**, 그것이
클라 디코드를 깨뜨렸다(실측 재현) — 문서의 "항상 echo" 서술과 코드가 모순이었다.

⚠️ **`request_id` 는 opaque 문자열이다 — UUID 를 강제하지 않는다** (2026-08-01 확정, codex Medium).

한때 §8-C 가 `invalid_request` 의 사유로 "UUID 오류"를 적었는데, 그 문구는 §8-A **예시**의
`"<uuid>"` 였던 데서 흘러온 것이지 제약이 아니었다(그래서 예시도 `<opaque-id>` 로 고쳤다). 코드는 non-empty 문자열만 보므로 문서와 코드가
어긋나 있었고, 어느 쪽으로 맞출지 정해야 했다. **opaque 로 확정한다**:

- 서버는 request_id 를 **echo 만 한다** — 색인도, 중복 제거도, 저장도 하지 않는다. 형식은
  서버 쪽에서 아무 의미를 갖지 않는다.
- 유일성은 **클라의 상관(correlation) 관심사**다. 충돌시키는 클라는 자기 상관만 망가뜨린다.
- UUID 를 강제하면 counter·ULID·nanoid 를 쓰는 구현이 **보호 효과 없이** `invalid_request` 로
  거부된다.

⛔ 길이 상한도 **여기서 정하지 않는다**. 크기 제한은 §8-C 에 `request_too_large` 라는 **별 코드**로
이미 있고(전체-메시지 범위), 그건 아직 미구현이다. request_id 전용 상한을 지금 박으면 그 슬라이스보다
먼저 **제3의 규칙**을 만드는 것이 된다.

⚠️ 무토큰 경로(§E1 중간 상태)에는 `request_id` 를 요구하지 않는다 — 그 클라는 id 를 보내지 않고
ack 도 받지 않는다. 요구하면 구 클라가 topic 을 잃는다.

#### 8-B-term — **종결 프레임 계약** (2026-08-01, 두 공백 해소)

위 두 유예(unsubscribe ack 부재 / flag-off 침묵)는 **닫혔다.** 계기는 다음 슬라이스의 클라
명령 큐다: 연결당 1 in-flight 로 두고 ack·error·timeout 까지 다음 명령을 막으면, **응답 없는
경로가 매번 큐를 timeout 까지 정지**시킨다. 그래서 큐보다 **먼저** 서버가 답하게 만든다.

**불변식** — *식별된 요청은 **정확히 하나의 종결 프레임 또는 연결 종료**를 받는다.*

⛔ "항상 프레임 하나"라고 적으면 **거짓**이다. 프레임 0개인 경로가 실재하고, **둘로 갈린다**:

**(a) 0 프레임 + 연결 종료** — disconnect 가 종결 신호가 된다.

| 경로 | 근거 |
| --- | --- |
| 분류 불가 인증 예외 | `app/main.py` 가 재전파 — "분류 불가는 삼키지 않는다" → 상위 `except` → `finally` 가 연결 정리 |
| 16KB 초과 메시지 | uvicorn `--ws-max-size 16384` → transport close **1009**. §8-C 의 `request_too_large` 도 "전송 계층은 close 1009"로 규정한다 |
| 반쯤 닫힌 소켓 | `send_json` 자체가 실패 → 같은 경로로 정리 |

**(b) 0 프레임 + 연결 유지** — ⛔ **timeout 이 유일한 종결 수단**이다.

| 경로 | 근거 |
| --- | --- |
| 비-JSON / 비-dict 입력 | dispatcher 가 `return` 만 한다(연결 **안 닫음**). 서버는 `request_id` 를 읽을 수 없어 답할 대상이 없다 — 클라는 식별된 요청을 보냈다고 믿어도 서버에겐 미식별이다 |

⛔ 한때 이 표가 비-JSON/비-dict 를 (a) 에 넣었다 — **오분류**였다. (a) 만 보고 큐를 설계하면
그 경로에서 disconnect 를 기다리다 영원히 멈춘다.

→ **클라 큐는 disconnect 를 모든 in-flight 의 종결 신호로 처리해야 하고, timeout 은 여전히
load-bearing 이다.** 이 문장이 다음 슬라이스 설계에 그대로 들어간다.

⚠️ **클라 큐 설계 노트 — `request_id` 만으로 연결 세대를 대체할 수 없다** (2026-08-01).

한때 "정수 세대는 필요 없고 이미 가진 `request_id` 로 소유권을 확인하면 된다"고 적었는데
**부족하다**: `request_id` 는 명령마다 유일할 뿐 **어느 연결의 명령인지**를 말하지 않는다.
구 연결에서 토큰을 기다리던 task 가 `cleanup()` **뒤에** 깨어나면, 자기 id 로 새 연결의
in-flight 슬롯을 덮어쓸 수 있다 — id 가 충돌하지 않아도 **슬롯이 뺏긴다**.

→ 최소한 **(channel identity, request_id) 쌍**을 비교하거나, in-flight 상태를 **연결 객체에
귀속**시켜야 한다. 구현할 때 **세 지점을 각각 점검**한다:

1. **토큰 대기 후 송신 직전** — 확실히 필요하다. 토큰 await 이 가장 긴 창이고, 그 사이
   `cleanup()` 이 돌 수 있다.
2. **ack / error 처리** — 수신 루프의 소켓 동일성 가드가 구 소켓 프레임을 이미 떨궈서
   **중복일 수 있다**. 큐를 쓸 때 실제로 필요한지 확인하고, 불필요하면 넣지 말 것.
3. **timeout 처리** — `cleanup()` 이 해당 timeout task 를 취소하는지에 달렸다. 취소한다면
   불필요하다.

⚠️ **"어느 하나라도 빠지면 샌다"고 단정하지 말 것** — 위 2·3은 다른 가드가 이미 덮을 수 있다.
필요 여부는 큐 코드가 생긴 뒤 **변이로** 판정한다.

⛔ **`cleanup()` 의 무효화-먼저 순서는 방어적 선택이지 현재 load-bearing 계약이 아니다.**
한때 여기에 "반대로 하면 close 예외가 먼저 올라와 구 task 가 끼어든다"고 적었는데 **틀렸다**
(codex Medium, 코드로 확인): `closeNormally()` 는 **동기·비throwing**이고 `cleanup()` 에는
**`await` 이 하나도 없어** 원자적이다 — 그 시나리오는 현재 코드에서 **표현 불가능**하다.
순서를 지키는 이유는 나중에 누가 `cleanup()` 에 await 을 넣거나 `closeNormally` 를 async 로
바꾸는 순간 load-bearing 이 되기 때문이고, 그때 조용히 깨지지 않게 지금 적어 둔다.

**테스트는 문장 순서가 아니라 행동으로** 잠근다(codex 제안 — 문장 순서 검사는 구현을
테스트하는 것이다). 양방향:
- 구 연결의 **토큰 task / ack·error / timeout** 이 **새 연결의 슬롯을 바꾸지 못한다**
- 같은 경로가 **현재 연결에서는 성공한다** (⛔ "항상 무시"로 통과하는 구현 배제)

fake channel 로 재개 시점을 **결정적으로** 제어할 수 있으므로 행동 테스트가 가능하다
(서버 쪽 직렬 처리는 타이밍을 결정적으로 못 만들어 AST 검사로 갔지만, 클라는 반대다).

#### ⛔ **순서 개정 (2026-08-01) — 배칭이 먼저 land 했고, reconciler 는 보류다**

아래 3-슬라이스 분해는 `reconciler + 1 in-flight` 를 **1단계**로 뒀는데, 적대적 검토가 그 전제를
깼다. 실제로 land 한 것은 **배칭만**이다(iOS `093333a`).

⛔ **1 in-flight 는 독립 실패를 직렬 실패로 바꾼다.** 현행은 topic 들이 토큰을 **동시에**
기다려 늦게 와도 함께 복구되지만, 직렬화하면 느린 첫 토큰에서 **정확히 하나가 그 연결 동안
소실**되고 정렬 규칙이 그걸 결정론적으로 고정한다(회복 = 재연결/토글뿐). 즉 배칭 없이 넣으면
**현행 대비 순수 회귀**다.

그리고 목표였던 **재연결당 Firebase 검증 N→1** 은 reconciler 없이 얻어졌다 — 재전송의 delta 는
자명하게 "구독 전체"이고 이미 그걸 보내고 있었다(형태만 N개였다). delta 계산 기계가 살 것이 없다.

⚠️ **reconciler 는 기각이 아니라 보류다.** 근거는 **"이번 슬라이스에 불필요"** 까지다 —
⛔ 한때 "lease 전에는 소비자가 없다"고 적었는데 **과했다**(codex Medium): `active_subscriptions`
와 `confirmedTopics` 는 **이미 per-topic 상태**다. lease 는 그 위에 만료·갱신 축을 더할 뿐이다.

⛔ **활성화 blocker 2건 — 배칭은 land 했지만 이 슬라이스는 닫히지 않았다.**

1. ✅ **[닫힘] 재시도 없는 배칭 = 전체 실패 결합** (iOS `163b403`).
   배치는 서버 인증 **1회**를 공유하므로 그 한 번이 실패하면 **전 topic 이 함께 실패**한다 —
   ⛔ 근거는 이 **결정적 동작**이다. 한때 여기 확률 논증("전부 성공↑ / 적어도 하나 성공↓")을
   적었는데 **요청별 실패의 독립성을 가정**한 것이라 근거로 부적절했다(codex).
   계약: terminal ❌ / 전부-rejected **ack** 은 오류가 아니라 이 경로에 안 옴(재시도하면 flag-off
   무한 반복) / 총 3회 상한 / 서버 `retry_after_seconds` 사용 / **원 JSON 재전송 금지** —
   넘기는 것은 *실패한 요청의 범위*뿐이고 "그 시점의 의도와 교차"는 **송신 지점**이 책임진다 /
   **연결 귀속**(대기 중 재연결을 건너면 발화 금지).
   ⚠️ 운영 flag off 라 운영 영향 0이었다 — blocker 이지 회귀가 아니었다.

2. ⏸ **[보류·활성화 전 필수] request timeout** — ⛔ **"소비자 없음"이 아니다.**

   한때 여기에 *"우리 클라는 항상 유효한 식별 요청을 보내므로 서버가 종결 프레임을 주거나
   연결이 죽고, ping/pong 이 ≤40초에 잡는다 → 도달 불가"* 라고 적었는데 **틀렸다**(codex High,
   코드 확인). 그 논증은 **서버가 프레임을 안 보내는** 경우만 봤다. 빠진 것은 **클라가 프레임을
   못 읽는** 경우다: `subscription_ack`/`subscription_error` 디코드가 실패하면 iOS 는 그대로
   `return` 하고 **pending 을 지우지 않는다**. 그동안 ping/pong 은 계속 성공한다 —
   즉 *"응답도 없고 연결도 살아 있는"* 상태가 **표현 가능**하다.

   → **ping/pong 은 연결 장애를, request timeout 은 프로토콜 응답 장애를 검출한다. 대체 관계가
   아니다.** 그리고 버전 스큐(= 디코드가 깨질 바로 그 상황)에서 필요해지므로, 없으면 가장
   필요한 순간에 없다. lease 재인증에서는 응답 유실이 **만료를 넘길** 수 있어 더 중요해진다.

   **보류 사유는 순서다** — 응답 deadline 이 실제 만료 동작과 결합되는 **iOS lease 소비
   슬라이스**에서 함께 구현한다. ⚠️ **활성화 전 필수 항목으로 남긴다.**

   ⛔ **설계 정정 — "timeout 시 연결 폐기 + 별도 recovery budget" 은 폐기한다**(codex 자신의
   이전 권고 철회, 나도 동의). 서버의 두 연산이 **멱등**이기 때문이다(subscribe=합집합 /
   unsubscribe=차집합). 결과를 모르는 timeout 에서도 **같은 연결에서** 현재 의도와 교차한 요청을
   제한적으로 다시 보내면 된다 → 연결 폐기도, 별도 예산도, storm 대책도 **필요 없다**.

   구현 계약(그 슬라이스에서):
   - timer 는 **실제 `sendText` 성공 후** 시작하고 **(channel identity, request_id)** 에 귀속
   - 정상 ack/error 가 오면 취소 / timeout 이면 그 pending 만 제거
   - **기존 bounded retry 체인**을 그대로 써서 **같은 연결에서** 재시도
   - 과거 JSON 이 아니라 **실패 요청 범위 ∩ 현재 의도**로 재구성(= 이미 land 한 규칙)
   - 늦게 온 구 `request_id` 의 ack 은 무시 / `cleanup()` 이 모든 timer 취소
   - **timeout 이 여러 개여도 reconnect 를 발생시키지 않는다**

아래 분해와 그때 발견된 제약(rejected 배제 / 보류 delta / timeout 정책)은 **그 시점에 다시
읽을 것** — 특히 다음 세 사실은 여전히 유효하다:
- flag-off ack 은 전 topic rejected + `active_subscriptions` 빈 배열 → "ack 이면 delta 재계산"
  규칙은 **무한 루프**가 된다(운영 flag 가 off 라 첫 연결부터).
- subscribe 는 **register → ack** 순서라 ack 유실 시 상태가 갈리고, 그 갈림은 **축소 방향에서
  해롭다**(끄려 해도 unsubscribe 를 만들지 않아 서버가 계속 보낸다).
- ⛔ "timeout 이면 연결을 폐기" 는 **재연결 storm** 이 된다: `parseMessage` 가 프레임 수신마다
  `reconnectAttempts = 0` 을 하고 서버는 연결 직후 legacy payload 를 **항상** 보내므로
  backoff 상한이 영영 걸리지 않는다.

#### (보류) 다음 iOS 작업은 세 슬라이스로 분리한다

⛔ `reconciler + 1 in-flight + batching + retry` 를 한 번에 넣지 않는다. 특히
`temporarily_unavailable` 재시도가 처음 캡처한 subscribe 배치를 그대로 보관하면, 대기 중 들어온
unsubscribe 의도를 무시하고 사용자가 끈 topic 을 다시 살릴 수 있다.

1. **연결 귀속 reconciler + 1 in-flight만 구현**한다. retry와 batching은 넣지 않는다.
   ack/error/disconnect/timeout이 **현재 연결의 현재 요청 하나만** 해소한다. ack은 서버가 준
   `active_subscriptions`를 적용한 뒤 현재 `subscribedTopics`(우리 의도)와 `confirmedTopics`의 차이를
   다시 계산한다.
   error/timeout은 미충족 의도를 남기되 **같은 차이를 즉시 다시 보내지 않는다** — 그러면 retry를
   안 넣었다는 말과 달리 `temporarily_unavailable`에서 무한 즉시 재시도가 생긴다. 재개는 새 연결이나
   사용자 의도 변경처럼 명시된 외부 trigger가 맡고, 자동 timer retry는 3단계 전까지 없다.
2. **배칭을 별도 추가**한다. 재연결 시점의 현재 차이만 한 요청으로 보내며, 오래전에 캡처한 topic
   배열을 큐에 보존하지 않는다.
3. **bounded retry를 마지막에 추가**한다. retry timer는 연결 또는 사용자 의도가 바뀌면 취소·대체되고,
   발화 시 원 요청을 재전송하지 않고 **그 시점의 현재 차이**를 다시 계산한다. unsubscribe 같은 최신
   축소 의도는 subscribe retry 뒤에서 기다리지 않는다.

각 슬라이스는 반대 방향도 잠근다: 구 연결/구 요청은 현재 상태를 바꾸지 못하고, 같은 사건이 현재
연결/현재 요청에 속하면 정상적으로 다음 reconciliation을 진행해야 한다.

**결정 (U1~U8)** — 정본이 규정하지 않아 이번에 정하고 기록한다.

| # | 결정 | 근거 |
| --- | --- | --- |
| **U1** | "식별된 요청" = `request_id` **키 존재** ∪ `id_token` **값 존재**. ⚠️ 두 축 모두 **`type` 과 무관**하게 본다 | 둘 다 없으면 §E1 구 클라 → 동작 불변이 그 보호다. ⛔ truthiness 로 보면 `request_id: ""` 가 legacy 로 새어 조용히 처리된다. ⛔ subscribe 를 `id_token` 값만으로 보면 `{"request_id":…,"id_token":null}` 이 미식별로 새어 **blind register + 0 프레임** — 클라는 id 를 발급했으므로 영구 정지다 |
| **U2** | unsubscribe 의 `accepted_topics` = 요청 topic **전부**(idempotent) | 구독한 적 없어도 목표 상태("구독 안 함")가 달성됐고, §8-C 의 닫힌 per-topic 어휘에 "미구독" 코드가 없다 |
| **U3** | `removed_topics` 는 **항상 `[]`** | §C2 eviction 축이지 요청 결과 축이 아니다. builder 의 **인자에서 제외**해 규율이 아니라 타입으로 만든다 |
| **U4** | Stage 2 에서도 `operation="unsubscribe"` 의 `accepted_topics` 는 lease 필드를 **갖지 않는다** | 제거된 topic 에 lease 가 없다 |
| **U5** | flag-off 는 subscribe·unsubscribe 모두 reject-all, **registry 불변** | ⚠️ 이 선택은 **운영에서 관측되지 않는다**: flag 는 import 시점 상수라 한 연결의 수명 동안 불변이고 등록은 flag 검사 **뒤**에서만 일어난다 → flag-off 프로세스의 연결은 애초에 뺄 구독이 없다. 관측 불가하므로 **현행 동작(무변경)을 유지**한다 |
| **U6** | 검사 순서 = 형식 → flag → 인증 → per-topic | flag-off 에서 무인증 Firebase RTT 를 열지 않는다. REST twin 도 같은 순서다(flag 404 가 토큰 검증 앞) |
| **U7** | unsubscribe 는 `supported_snapshot_topics()` 를 **조회하지 않는다** | 그 집합은 flag-aware 다. 축소 연산에 flag-aware 검증을 붙이면 이득 없이 실패 모드만 는다(배포 flag 가 꺼지면 이미 든 구독을 못 빼는 형태) |
| **U8** | flag-off ack 은 **인증 이전**에 나간다 → **ack 수신은 인증 통과를 뜻하지 않는다** | `topics_disabled` 는 §8-C 에서 per-topic 이라 전체-요청 오류로 만들 수 없다. 쓰레기 토큰도 이 ack 을 받으므로 클라는 ack 을 인증 증거로 읽으면 안 된다 |

**선언된 동작 변화 4건** — 구 클라 영향 0. 근거는 **U1 의 합집합 그 자체**다: §E1 클라는
`request_id` 도 `id_token` 도 보내지 않으므로 **미식별**이고, 넷 다 식별된 요청에만 발화한다.
(⛔ 한때 이 줄이 "셋 다 `request_id` 를 보내는 요청"이라고 적었는데, 4번은 `request_id` 가
**없는** token-bearing unsubscribe 라 제목과 목록이 어긋나 있었다.)

1. `request_id` 가 빈 문자열/비문자열인 **unsubscribe 는 이제 거부된다**(구: 조용히 해제).
   §8-A 의 "축소는 fail-open" 은 *토큰* 축이고, echo 할 id 가 없으면 ack 자체를 만들 수 없다 —
   ack 없는 unregister 가 바로 이 슬라이스가 삭제하는 침묵이다. **fail-closed 를 택한다.**
2. `topics: []` 는 `invalid_request` 다. 구 동작은 `all([])` 가 True 라 검사를 통과했고,
   **두 경로에서 서로 다르게** 잘못됐다 — ⛔ 한때 이 줄이 둘을 뭉쳐 적었다(codex Low):
   - **무토큰 legacy 경로**: `registry.register(ws, [])` 가 **무조건** 호출돼 빈 entry 를
     만들었다(`setdefault`) → **유령 entry** 로 admin 카운터만 오염.
   - **인증 경로**: `if accepted_names` 가드 덕에 registry 는 **건드리지 않았고**, 대신
     성공과 구분되지 않는 **빈 ack** 이 나갔다.
3. flag-off·topics 형식 오류·**미지 `type`** 이 **식별된 요청에 한해** 프레임을 받는다.
   ⚠️ 미지 type 을 종결시키는 이유: "unknown type 은 forward-compat 으로 무시"가
   request/response 에서는 **역방향으로 해롭다** — 서버보다 새 클라가 모르는 타입을 보내면
   "미지원"을 배우는 대신 **매단다**. 오타 하나(`subscrbe`)로도 같은 일이 난다.
   미식별 미지 type 은 여전히 침묵이다(기다리는 요청자가 없다).
4. **토큰을 실은 `unsubscribe`** 도 `request_id` 를 요구한다(구: 조용히 해제).
   §8-A 의 unsubscribe 는 토큰이 없으므로, 토큰을 실었다는 것은 **신 프로토콜 클라 신호**다.
   §E1 구 클라는 토큰을 보내지 않으므로 영향 0이다.

⛔ **U1 의 두 축은 `type` 과 무관하다.** 한때 `id_token` 을 `msg_type == "subscribe"` 일 때만
읽었고, 그래서 `{"type":"subscrbe","id_token":"tok"}` 이 미식별로 새어 0 프레임이었다(실측).
U1 이 문서에만 있고 코드에는 **반만** 있던 것이다 — 세 경우(`request_id` 만 / `id_token` 만 /
둘 다 없음)를 각각 독립으로 잠근다. 축을 동시에 넣은 테스트는 한 축만 검증한다.

⚠️ **판정기 부재의 처리**: per-user 판정이 필요한 topic이 지원 집합에 들어 있는데(배포 flag on)
WS 판정기가 없으면, 그건 transient가 아니라 **설정 결함**이다. `topic_unavailable`을 쓰면 안 된다 —
그 코드는 "개별 flag off"를 뜻하고, flag가 켜진 상태에 쓰면 운영자가 flag를 보고 코드와 모순을 겪는다.

⛔ **구현은 이 문단과 다르다 — 열린 결정이다**(2026-08-02 `4a45173` 확인). 여기엔 "ERROR 로그 +
전체-요청 `temporarily_unavailable`"이라고 적혀 있으나, 실제로는 `assert_single_gated_topic()`
(`app/topic_authorization.py`)이 **`RuntimeError` 를 올리고 아무도 잡지 않는다** — 판정기가 일반
`Exception` 을 의도적으로 삼키지 않기 때문이다(같은 파일: *프로그래밍 오류를 가용성 verdict 로
바꾸면 클라는 재시도 가능한 정상 결과로 읽고 서버는 영영 안 낫는다*). 결과는 `app/main.py` 의
`except Exception` → `logger.exception` + finally 정리 = **ERROR 로그 + 연결 종료**다.
§8-B-term 은 연결 종료도 종결로 인정하므로 계약 위반은 아니다.

**어느 쪽을 택할지는 미결이다**: (a) 문서대로 프레임을 보내면 클라가 **영영 성공 못 할 재시도**를
한다(설정 결함은 재시도로 안 낫는다). (b) 현행대로 두면 그 요청뿐 아니라 **연결의 다른 구독까지**
사라진다. 두 번째 gated topic 을 추가하는 슬라이스에서 함께 정한다 — 그 전까지는 트립와이어가
**배포 전에** 터지게 하는 것이 목적이므로 현행으로 둔다.

✅ **timeout — 구현 완료. 축은 두 개이고 계약은 아래와 같다** (2026-07-30 도입 / 2026-08-02 범위 축소):

1. **wire deadline** — **identity + gated 인가의 누적 상한**. **dispatcher 에** 걸어야 한다:
   E2E harness 가 verifier 를 통째로 fake 하므로 verifier 안에 두면 `/ws` 경계에서 영원히
   관측되지 않는다.
   ⚠️ **범위 축소 (2026-08-02)**: 한때 "호출자가 기다리는 시간의 상한"이라고 적어 **전체 응답
   상한**처럼 읽혔는데, 실제로 이 창이 덮는 것은 **인증 두 단계까지**다 — registry 변경과 ack
   송신은 밖이다. 그 보장을 원하면 창을 넓혀야 하고 그건 별도 결정이다.
   ⛔ **단계마다 새로 시작하지 않는다.** 그러면 상한이 단계 수만큼 곱해진다(identity 10s +
   gated 10s = 20s). dispatcher 가 요청 시작에 절대 시각을 잡아 두 단계가 **공유**한다 —
   그 성질은 `timeout_at` 의 `when` 이 두 호출에서 같은지로 잠근다(시간 의존 테스트는
   CI 부하에서 거짓 green 이 된다).
2. **SDK transport 상한** — 1번만으론 부족하다. `asyncio.to_thread` 작업은 **취소되지 않으므로**
   호출자가 포기해도 스레드는 계속 돌며 **공유 executor** 슬롯을 점유한다(직접 재현). 그 pool 은
   `min(32, cpu+4)` 이고 리포의 `to_thread` 호출부가 함께 쓴다 — 인증 대기가 snapshot·DB 작업까지
   밀어낸다.

⛔ **정정**: 한때 "`httpTimeout` 은 프로세스 전역이라 FCM 까지 조인다"고 적었는데 **틀렸다**(codex).
firebase-admin 6.9.0 은 `app.options.get("httpTimeout", …)` 로 **app 별**로 읽고
(`_auth_client.py:42` 와 `messaging.py:470` 이 각각 자기 app 을 본다), `initialize_app(..., name=…)`
로 named app 을 만들 수 있으며 `verify_id_token(id_token, app=…)` 이 그 app 을 받는다.
즉 **인증 전용 named app 으로 transport 상한을 FCM 과 분리**할 수 있다 — "전역이라 못 한다"는
근거는 성립하지 않는다.

#### 자원 상한 — 열린 항목 2건 (flag ON 전 결정 필요)

timeout 두 축을 넣으면서 **자원 상한**을 판정했는데, 그때 두 개념을 뭉쳐 기각했다. 분리한다.

- ⛔ **admission control(semaphore 즉시거절) = 기각 유지.** 측정: 유입 0.2×용량에서 성공 120→**18**,
  거절 0→**102**. 배포 재연결은 평균 유입이 낮아도 **동시 도착**이라 `sem.locked()` 가 즉시 참이
  되고, 1초면 빠질 큐를 대량 거절한다. 이 워크로드의 모양과 정반대다.
- 🔲 **격리(인증 전용 executor) = 미결.** 이건 admission control 이 **아니다** — 거절하지 않고
  자원을 나눌 뿐이라 새 wire 결과가 없다. 측정: 동거 작업 p99 **3967ms → 31ms**.
  ⚠️ 대가: 워커 수 W 가 곧 인증 처리 용량(W/T)이라 잘못 잡으면 스스로 문턱을 낮춘다.
  ⛔ **"5줄 격리"라고 적었던 것은 과소평가다**(codex Medium). 실제로 다뤄야 하는 것:
    · **수명주기** — 모듈 전역 pool 의 생성·종료(앱 shutdown 에서 안 닫으면 스레드가 남는다),
    · **kwargs** — `loop.run_in_executor(ex, func, *args)` 는 **키워드 인자를 받지 않는다**
      (`functools.partial` 필요). 현행 호출은 `check_revoked=`/`app=` 를 키워드로 넘긴다,
    · **큐 적체** — 전용 pool 도 `SimpleQueue` 라 무제한이다. 격리는 *동거인* 을 지킬 뿐
      인증 자신의 적체는 그대로다.
  ⛔ **"구조 트립와이어로만 잠긴다"도 틀렸다**(codex Medium): **기본 executor 를 점유해 놓고**
    인증이 진행되는지 보면 **행동으로** 검증된다 — 격리가 없으면 인증이 함께 막힌다.
- 🔲 **ingress 상한(nginx `/ws`) = 미결.** 실측: `/ws` location 블록에 `limit_req`·`limit_conn` 이
  **없다**(주석도 "Rate Limit 없음"). `/api/` 만 3r/s + conn 10 이 걸려 있다. 즉 인증 이전 단계에서
  토큰 flood 를 막는 것이 없다. **호스트 config 변경이라 별도 승인이 필요하다.**

⚠️ 큐 자체는 caller deadline 이 드레인하지만(취소가 concurrent future 로 전파돼 dequeue 시 skip),
큐 **크기**는 유입률 × deadline 이고 고정 상한이 없다 — 위 두 항목이 그 상한을 정하는 자리다.

⚠️ **dormant 의 범위 정정**: `TOPIC_DISPATCHER_ENABLED=false` 가 막는 것은 **요청 경로**뿐이다.
인증 전용 named app 초기화는 **flag 와 무관하게 startup 에서 실행**되므로, 다음 배포에서
프로세스마다 Firebase app 이 하나 더 생긴다(실패해도 로그 후 `None` 이라 서비스 영향은 없다).
"flag off 라 이 경로는 프로덕션에서 아직 돌지 않는다"는 서술은 **요청 경로에만** 정확하다.

⚠️ 그래도 SDK 재시도가 남는다: `accounts:lookup` 은 per-attempt 120초 기본 + status 재시도 4회
(`_http_client.py:41-52`)라, transport 상한을 낮추지 않으면 한 subscribe 가 분 단위로 스레드를
점유할 수 있다.

- `operation`: `"subscribe"` | `"unsubscribe"` — 같은 schema를 쓰므로 구분자가 필요하다.
- `accepted_topics`/`rejected_topics`/`removed_topics` = **이번 요청의 결과**.
- **`active_subscriptions` = 그 연결의 최종 상태 전체**(topic별 `lease_id` + 남은 duration). 클라는 이걸로 수렴한다.
- **`lease_id`는 재사용 불가한 opaque 값**(§8.1 D2) — 정수 generation을 쓰면 제거 후 재구독 시 초기화돼
  과거 `reauth_required`가 새 lease와 오인 일치할 수 있다.

**lease 만료 통지**:
```json
{"type":"reauth_required","topics":[{"topic":"krx:usd-krw-futures","lease_id":"c7f1…"}],"reason":"lease_expired"}
```
- 클라는 **자기가 들고 있는 `lease_id`와 다르면 무시**한다(늦게 도착한 구 통지 방어).

### 8-C 오류 코드

| 코드 | 범위 | 의미 |
|---|---|---|
| `invalid_token` | 전체 | 토큰 무효·만료·revoked |
| `temporarily_unavailable` | **전체(항상)** | 인증·권한을 **판정할 수 없음**. **`retry_after_seconds` 동반**, registry 불변 |
| `invalid_request` | 전체 | 형식 위반 / `request_id` 누락·비문자열·빈 문자열 / 빈 topics / 중복 정규화 후 빈 목록 |
| `request_too_large` | 전체 | 상한 위반(전송 계층은 **close 1009** — 클라이언트 관측) |
| `topics_disabled` | per-topic | 서버 topic 기능 off |
| `unknown_topic` | per-topic | 미지원 topic |
| `premium_required` | per-topic | 구독 필요 |
| `krx_entitlement_required` | per-topic | KRX entitlement 필요 |
| `topic_unavailable` | per-topic | topic 자체가 서버에서 비활성(개별 flag off) — REST twin이 이미 사용(main.py:2771) |

**flag → 코드 매핑** (없으면 "accepted + lease인데 데이터가 영원히 안 오는" 상태가 생긴다):

| flag | off일 때 |
|---|---|
| `TOPIC_DISPATCHER_ENABLED` | 전 topic `topics_disabled` |
| `FX_TOPIC_ENABLED` 등 topic별 flag | 해당 topic `topic_unavailable` (accepted 금지) |
| `KRX_CLIENT_DISTRIBUTION_EFFECTIVE` | krx topic `topic_unavailable` |

> §3.2(KRX 존재 비노출)와의 관계: 무료 사용자는 D5 **4단계에서 `premium_required`로 먼저 거부**되어
> 5단계(`krx_entitlement_required`)에 도달하지 않는다. premium이면서 entitlement가 없는 조합은 §3.1이
> 명시적으로 허용하는 상태이므로 `krx_entitlement_required` 노출은 §3.2 위반이 아니다.

- 판정은 단일 전순서가 아니라 **처리 단계**를 따른다(§8.1 D5).
- 검증 실패 topic은 registry에 **등록하지 않는다**.

### 8-D 재인증·만료

- **재인증**: 클라는 ack의 **`lease_duration_seconds`에서 유도한 시점**에 `id_token`을 재첨부해 다시 subscribe한다
  (고정 주기 아님 — §8.1 D6 공식).
- **만료**: 서버가 registry에서 해당 topic을 제거하고 `reauth_required`를 보낸다.
  **LEASE 상한 = 15분**(S5). 실제 부여 lease는 authoritative 확인 시점에 묶여 **가변**이다(§8.1 A1).
- 구현 시 **KRX per-user 강제 동시 해소**(ADR-038 Open 2 close).

### §8.1 1C 구현 설계 — **폐기 (2026-07-30)**

> 이 자리에 1C 구현 설계 약 765줄(A 시간·인가 / B 강제 / C 전이 / D wire / E 롤아웃 / F iOS 입력 /
> G 테스트 매트릭스 / H 문서 갱신)이 있었다. **삭제했다.**
>
> **왜**: 그 설계로 만든 구현은 소비자를 한 번도 갖지 못한 채 81커밋 동안 자랐고, 그 과정에서
> 이 절이 전제한 것 여럿이 반증됐다(노출 상한 산술 · 관측 시각의 출처 · 실패를 접는 방향과 위치 ·
> 요청 단위). 폐기 시점에 이 본문에는 **부정확한 것으로 확인된 서술**이 남아 있었고(어긋난
> 파일:라인 포인터, 과소평가된 유한 상한, 이후 제거된 wire 필드), 머리에 경고만 달아 두면
> `rg` 결과나 부분 인용은 **경고를 지나쳐 본문에 도달한다**. 그래서 본문을 남기지 않는다.
>
> **어디에 있나**: 태그 `archive/adr039-dormant-2026-07-30` (설계 문서·코드·테스트 전량).
> **무엇을 배웠나 / 어떻게 다시 시작하나**: `DECISIONS.md`의 **ADR-040** — 착수 전 그것부터 읽을 것.
>
> ⛔ archive에서 이 절을 되살려 조항 목록으로 쓰지 말 것. 폐기 이유가 "코드가 나빠서"가 아니라
> **"소비자 없이 자라 검증의 종료 조건이 없어서"**이므로, 되살리면 그 조건이 그대로 돌아온다.

#### §8.1이 남긴 것 — 리셋 후에도 유효한 롤아웃 제약

구현 설계와 달리 아래는 **현행 코드·운영에 대한 사실**이라 그대로 살아 있다.

- **무토큰 subscribe는 현재 무시되지 않고 실제로 등록된다.** topic dispatch flag가 켜지면
  `id_token`/`request_id` 검사 없이 registry 등록 + snapshot 전송이 일어난다(`app/topic_dispatcher.py`의
  subscribe 분기). 현행 iOS도 무토큰 payload(`{type, topics}`)를 보낸다. 구 문서의 "구형 무인증
  메시지는 silent-ignore" 서술은 **오서술이었다**.
- **enforcement는 capability와 분리해야 한다.** 인증 capability를 배포하되 강제하지 않는 중간 상태
  (무토큰은 기존대로 등록되고 만료·재인증이 돌지 않음)가 있어야 dormant→flip이 all-or-nothing이 아니다.
  ⚠️ **강제 전환 시점에 구 클라(무토큰)는 topic을 잃는다** → legacy 유예(S4)와 **같은 시점**에 묶어야 한다.
- **REST twin 인증 게이트는 land됐다**(2026-07-25). `GET /api/v2/topics/snapshot`이 인증+premium+
  per-user KRX를 강제한다. 이것이 선행 조건이었던 이유: **1C는 topic dispatch flag를 켜야 동작**하는데,
  그 flag가 현재 무인증 KRX REST를 막고 있던 유일한 장치였다.
  ⚠️ **잔여(flag ON 전 필수)**: iOS `APIService`의 bootstrap 3종이 아직 무인증 경로라 flag ON 시
  401을 받는다. 전부 `try?` 격리라 크래시는 없고 cold-start bootstrap만 조용히 사라진다 →
  인증 transport로 이관 필요. 운영은 현재 flag off라 사용자 영향 0.
- **구매 수렴 계약이 enforcement 활성화의 blocker다.** 서버측 무효화·freshness만으로는 구매가
  수렴하지 않는다 — 둘 다 "서버가 다시 물어볼 준비"만 시키고, 실제 복구 트리거는 **클라의 다음
  subscribe**인데 `premium_required`가 **terminal**이라 클라가 재시도하지 않는다. 그래서 시간이
  지나도, webhook이 와도 그 연결은 거부 상태로 남는다. 활성화 전에 "구매 완료 → 제한된
  refresh/backoff → 정상 ack" 클라 경로가 필요하다.
  ⚠️ forced refresh도 즉시 복구가 아니다 — 우리 캐시만 우회할 뿐 RevenueCat 자신의 전파 지연은
  못 넘는다(구매 직후면 다시 "구독 없음"을 받는다).
- **`check_revoked`는 별도 정책이 아니라 인가 상한에 통합**한다 — 계정 삭제·비활성도 같은 상한 안에서 종료.

## 9. 구현 체크리스트

- [ ] 전 라우트 auth 감사 — 누수 0.
- [x] iOS·Android 공통 client-version metadata + nginx 로깅 (step 2 land 2026-07-17: server `55ab1d8` / iOS `1b736f2` / Android `5f93409`. 데이터는 신규 앱 release 후 생성 — nginx deploy/reload + 실 로그 cp/cv/cb 확인 별도).
- [x] hourly endpoint(인증만, **KRX 제외**, self-describing) + 매시간 계약(§4.2) — **step 3 land + 배포 2026-07-17** (app/free_snapshot.py + GET /api/v2/free/snapshot, MVP=usd·1d/1w/3m/1y). workflow 설계+adversarial + **codex MCP 다라운드**: B1~B4/N5/N6/NB → **freshness 불변식(serve canonical-only, DB 재생성 제거)** → validator Pydantic 스키마. **2026-07-18 HH:30 basis 개정(§4.2)**: as_of=마지막 HH:30 + `timestamp <= as_of` cutoff 쿼리 불변식(fetch_rate_entries_until + build_tab_1d_payload now_kst 주입) + cron :30 — 구 :00 floor의 라벨↔데이터 mismatch(사용자 실측) 해소. **S6 결정=(a) 24h hard cutoff(2026-07-21) — 서버+iOS 구현 완료(dev-side)**: 구 비결정 혼합(process-local 무기한 + Redis 25h TTL)을 (now-as_of)>=24h 렌더 거부로 일관화. **서버**(d9e60ab: `is_snapshot_too_stale` per-candidate age, stale Redis가 fresh local 안 덮음). **iOS**(7050bd3: `LoadState.unavailable` + isTooStale age>=24h[future asOf는 clock-skew 허용] + current/displayData gated[sticky 포함] + **fetch-독립 expiryTask**로 SwiftUI 시간-미관찰 in-flight 만료 공백까지 폐쇄 + 뷰 전파. codex Blocker/High/Medium 0). 실사용자 노출은 App Store 배포 후. iOS USD reference slice land(987d161). **FX 3탭 확장(2026-07-21)**: `FREE_SNAPSHOT_TABS=("usd","jpy","eur")` — jpy/eur는 usd와 동일 코드 경로(bank/investing rate reader + source_daily/hourly canonical + 1d intraday) 재사용, `TAB_ASSET`만 차이(별도 reader 0). tether는 free reader가 source_rates(거래소 데이터) 미조회로 rate가 비어 N4(별도 reader) 전까지 제외. 프로덕션 read-only 실증(jpy/eur 4기간 non-empty·asset 정확·KRX-free) + 계약 테스트 +3 + **프로덕션 배포(acea749, 2026-07-21)**. **iOS 활성화 완료(step 4, 57f6648) — freeConfig switch(usd/jpy/eur)로 FX 3탭 실렌더 dev-side 활성(합성 시뮬 게이트 포함)**. **jpy/eur dev 기능 E2E PASS(사용자 dev 빌드 2026-07-21)**: 그래프 4기간/기본 토글 investing+hana/은행목록/알림 통화(jpy-krw·eur-krw)/KRX·DXY 미노출 + VM-hoist(News 왕복·인접 왕복 재요청 0, 3탭×4기간=12 GET 초기 warm만) 전부 확인. 실사용자 노출·트래픽 실측은 출시앱 배포(별도 GO) 이후.
- [x] **테더 N4 (무료 테더 탭 백엔드 + iOS N4-4 + graph 하드닝)** — 무료 테더 rate는 grouped shape(`kind="source_grouped"`: `usdt_krw`[거래소 5] + `usd_krw_banks`[kb·hana] + `usd_krw_reference`[investing singleton], `primary_asset="usdt-krw"`)로 도입(FX는 flat `{asset,entries}` 그대로). **N4-1 land**(58b9d9c): `_assert_krx_free`가 FX shape(bank/currency)뿐 아니라 topic-native shape(source/asset)도 검사 → 테더 KRX(달러선물) 우회 차단. **N4-2a land**(grouped-aware validation 리팩터, codex Blocker/Medium 2라운드 반영): `_iter_rate_items`(shape 무관 순회 단일 진실소스)로 nonempty·within-as_of·precompute 카운트 통일 + rate.asset(grouped=primary_asset) grouped-aware + **3중 hybrid/KRX 방어** — (1) `_FreeRate`/`_FreeRateGrouped` `extra="forbid"`(반대 shape 컨테이너 키를 extra로 얹은 hybrid를 schema서 거부, 양방향) (2) `_assert_krx_free`가 `_iter_all_rate_dicts`로 entries+3그룹을 **kind 무관 동시 스캔**(hybrid가 반대 컨테이너에 KRX를 숨기는 우회 폐쇄, belt-and-suspenders) (3) `_GROUPED_RATE_TABS` **tab↔shape 결합**(grouped-USD/flat-tether 오염 canonical을 fail-closed 거부 → 구 FX client 깨진 200 회귀 차단). graph 절반은 코드 완료(tether series 정의 + `exclude_krx` 필터). **N4-2b land**(cutoff-aware grouped tether reader): 유료 토픽 grouper `build_tether_tab_payload`(정규화·정렬·investing singleton 무결성) 재사용 + cutoff fetcher만 신규 — `crud.get_source_rates_until`(source_rates cutoff 변형, `timestamp<=as_of`, rate_changed_at 미포함[정적 스냅샷]) + `free_snapshot.fetch_tether_grouped_rate_until`(거래소 5=source_rates / kb·hana·investing=usd-krw는 기존 FX cutoff reader 재사용 후 선별) → `build_free_snapshot_payload` tab 분기(tether=grouped / FX=flat). free↔paid shape 일치(iOS 단일 어댑터). dup-source/wrong-asset은 각 source explicit query라 자연 차단(entitlement 우회 아님, KRX는 조회 경로에 아예 없음 + `_assert_krx_free` belt-and-suspenders). codex 2라운드(N4-2a Blocker/Medium + N4-2b) 통과. **N4-3 land + 배포 + 프로덕션 verify**(08db271, 2026-07-22): `FREE_SNAPSHOT_TABS`에 tether 추가(precompute 순회 + endpoint 게이트 단일 진실소스 → 자동 활성) + build 분기 + serve tab↔shape 결합. codex 3라운드 통과. EC2 배포(build+force-recreate) 후 precompute 트리거 + Redis canonical 직접 검증 — **4기간(1d/1w/3m/1y) 전부 validate=True / kind=source_grouped / primary_asset=usdt-krw / as_of=HH:30 / stale=False / rate=거래소5(upbit·bithumb·coinone·korbit·gopax)+kb·hana+investing / KRX series·rate 0 / graph 실데이터 non-empty(1d 10/10 각 144pt · 장기 4/4)**. 서버 활성은 dormant(엔드포인트 auth-gated, 소비 client는 App Store 배포 후). 테스트 free_snapshot 72 passed / 전체 3604. **N4-4 land + dev E2E PASS(2026-07-22)**: iOS grouped adapter(FreeRate flat/grouped, kind-peek)/group-aware allowlist(KRX fail-closed)/코어 탭(source 바)/그래프(거래소 5토글·DXY↔선물 상호배타·구역 프리미엄 정합)/3 알림 preview(거래소 가격·김프·비교, no-persist + malformed-rate 크래시 가드)/라우팅(테더-first, 인증 게이트). **+ graph fail-closed 하드닝(codex 6라운드 Blocker 0)**: per-tab ID allowlist(4탭 카탈로그 1:1) + envelope 결합(assertEnvelope/decodeAndValidate behavioral) + content-level KRX guard(서버 `_assert_krx_free` graph + 클라 `hasKrxContaminatedPoint`, 4마커 대칭) — 서버 257a05d 배포·검증 / iOS 1cfa978·778d294·9c255eb. 실사용자 노출은 App Store 배포(TOPIC_V2 arming, 별도 GO) 후.
- [ ] §3.1 매트릭스 + 캐시 G2∧G3 전역 → serve-time G1∧premium.
- [ ] iOS 4a~4d → Android 이식(REST interceptor 재사용).
- [x] **E3 REST twin 게이트** — `GET /api/v2/topics/snapshot`에 인증+premium+per-user KRX 강제 (2026-07-25 서버 land,
      §8.1 E3). `TOPIC_DISPATCHER_ENABLED=true` 선행 조건. 잔여 = iOS bootstrap 3종 인증 이관(F 슬라이스).
- [ ] WS 계약(§8): subscription_error + ack accepted/rejected + bounded-lease(15분) + reauth_required.
      **← 1C 진행 중 — 서버 축은 land 완료, 잔여는 클라(iOS lease 소비) + request timeout**
      ✅ **무료 topic identity lease land** — /ws subscribe → Firebase 검증 → 15분 lease
      (**identity 축만**) → ack(`lease_id` + **남은** duration) → registry 저장 →
      **모든 발행 경로가 지나는 단일 lease 게이트**(만료 시 전송 0) → disconnect 시 제거, 한 E2E.
      ⚠️ 무토큰(§E1)은 **무료 topic 만** lease 없이 수신한다. gated topic 은 무토큰으로 등록되지
      않는다(등록하면 lease 부재가 "무제한"이라 **무인증 유료 데이터 우회**).
      ⛔ 무토큰 재구독이 **기존 lease 를 지우지 못한다** — 지우면 인증된 구독이 무제한이 되는 우회다.

      ✅ **KRX per-user 판정 land (2026-08-02 `4a45173`).** 토큰을 실은 KRX 요청은
      `app/topic_authorization.py` 의 `authorize_gated_subscription` 이 premium ⊥ entitlement
      2축으로 판정한다 — entitled 는 4축 lease 와 함께 accept, 자격 없으면 **per-topic** 거부
      (`premium_required` / `krx_entitlement_required`) + 기존 KRX 구독 즉시 철회,
      **판정 불가일 때만** 전체 요청이 `temporarily_unavailable` 로 접힌다(registry 불변).
      12축 E2E 를 **변이 확인까지** 마쳤다(아래 체크리스트).

      ⚠️ (2026-08-02 이전 상태 기록 — 지금은 위가 맞다) 이 자리에 "판정기 부재로 전체 요청이
      접힌다"고 적혀 있었다. 그 전에는 반대로 "슬라이스 land, 잔여는 iOS 뿐"으로 적었는데
      **과대 서술**이었다(codex). socket UID 결속을 미룬 판단도 **이 범위 안에서만** 유효하다.

      ⚠️ **과도기 결정 — 만료 lease 는 `duration 0` 으로 남긴다.** 최종 계약은 *만료 시 registry
      제거 + `reauth_required`* 이지만, 그 전까지는 `active_subscriptions` 에 남긴 채 `0` 을 싣는다.
      필드를 빼면 **무토큰(§E1) 구독과 구분되지 않아** 클라가 *무제한*으로 오해하기 때문이다.
      ⛔ 클라 계약: `0` = **"즉시 재인증"**(≠ "타이머 없음"). iOS lease 소비 슬라이스에서
      **양방향 테스트**로 잠글 것 — 0 을 무시하면 그 topic 이 조용히 죽는다.

      잔여 = ~~① KRX per-user 판정~~ **✅ land** ② **iOS lease 소비**(최단 만료 전 재인증)
      ③ **request timeout**(활성화 blocker).

      ⚠️ ①의 완료 조건은 **숫자가 아니라 이름 목록**이다("8축" 같은 요약은 provider/DB 의
      transient·persistent 를 각각 세면 어긋난다 — codex).

      **① land 완료 (2026-08-02, `4a45173`, CI run 30731908679 = `3924 passed / 2 skipped`).**
      12개 완료 조건 전부 `[x]` — 각각 **변이 확인**까지 마친 뒤에만 체크했다.
      `wip/krx-per-user-checkpoint` 를 `--squash` 로 단일 커밋 land. **prod flag 는 계속 off.**

      ⛔ **부분 land 금지**(당시 상태 기록): production diff 에 UID 결속 · cross-UID 종료 ·
      Denied 즉시 철회 · 4축 lease · mixed-request 원자성 · deadline 처리가 이미 들어 있는데
      12개 완료 조건이 전부 미검증이었다. (⚠️ "E2E 커버리지 0" 은 **과대 표현**이었다 — 공유
      deadline 경계 테스트와 기존 lease 발행 E2E 는 이미 통과 중이었다.)
      stale 테스트만 교체해 green 을 만들면 *"새 구현이 맞다"* 가 아니라 *"옛 기대값이 사라졌다"* 만
      증명한다.

      ⛔ **checkpoint 브랜치는 `--squash` 로만 합친다.** WIP 커밋 자체가 red 이므로 일반 merge 하면
      **master 이력에 실패 커밋이 남아 `git bisect` 가 깨진다**. 완료 후
      `git merge --squash wip/…` 로 단일 diff 를 만들고 **전체 검증 뒤 새 커밋**으로 land 한다.

      E2E 체크리스트 — 테스트 **+ 변이 확인**까지 끝난 것만 `[x]`:
      - [x] `entitled_subscription_receives_a_four_axis_lease` — ⚠️ 실행이 순차라 identity 가
        **언제나** 최소다 → 그 배치만으로는 "min of 4" 와 "identity only" 를 구분 못 한다.
        축마다 낡은 관측을 만들어 **duration 이 달라지는지**(800/810/820) 로 지배력을 본다 +
        `now` 는 바닥(관측이 전부 미래여도 900 초과 금지)
      - [x] `premium_denial_skips_the_database_and_revokes_existing_krx` — DB 증가량 0 은
        **grant 이후 delta** 로 본다(최초 grant 가 이미 DB 를 부르므로 총 호출 수로 보면 틀린다)
      - [x] `entitlement_denial_revokes_existing_krx` — 두 축 모두 **publish positive control**
        동반: 철회 *전* KRX publish 가 1 이어야 한다. `leased_subscribers` 를 `set()` 으로
        고장 내면 후단 `0` 은 그대로 통과하고 **positive control 만** 발화함을 확인했다
      - [x] `provider_transient_folds_the_whole_request_and_leaves_registry_untouched`
      - [x] `provider_persistent_uses_the_long_retry_after` — retry_after=30 ∧ registry 불변 ∧
        **정책 승격 ERROR 정확히 1건**(leaf 의 원인 로그와 별개 — 그 승격은 leaf 가 모르는 사실이다)
      - [x] `db_transient_folds_the_whole_request` — 짧은 retry_after ∧ registry 불변(양방향)
      - [x] `db_permanent_folds_the_whole_request` — 긴 retry_after ∧ **ERROR 정확히 1건**.
        두 축은 **서로를 죽이는 변이가 다르다**(오분류 방향이 반대) — 각각 확인했다
      - [x] `gated_authorization_deadline_warns_once_and_changes_nothing` — WARNING 정확히 1건
        (판정기를 안 지나므로 dispatcher 가 유일 소유자) ∧ transient retry_after
      - [x] `same_uid_reauthentication_succeeds` — 토큰 갱신마다 끊으면 lease 갱신 자체가
        불가능해진다. `bind` 를 "언제나 충돌" 로 만든 변이가 죽인다
      - [x] `cross_uid_closes_the_connection_without_error_logs` — ⚠️ **처음 형태는 공허했다**:
        `closed = True` 를 try 와 except 양쪽에서 세팅해 항상 참이라 `identity.bind` 를 통째로
        지워도 통과했다(실측). 배타 3결과(disconnected / second_ack / **hung**)로 교체.
        `close(1008)` 제거 변이는 red 가 아니라 **행**이었어서 daemon thread + join(5s) 로
        시간을 걸었다 — CI 에서 행은 실패보다 나쁘다.
        ⚠️ 이 변이가 `ConnectionIdentity` **무커버리지**도 드러냈다 → `tests/test_topic_wire.py`
        `TestConnectionIdentityBinding` 신설(falsy 거부 후 소유권 공백 유지 / 충돌 후 소유권 불변)
      - [x] `mixed_request_keeps_free_topics_when_krx_is_denied` — 철회 축과 **다른 분기**다
        (저기는 기존 구독 보존, 여기는 **처음 등록**) ∧ 무료 publish 1 / KRX 0
      - [x] `mixed_request_registers_nothing_when_authorization_is_unavailable` — ⚠️ **단독
        테스트는 없다.** `_assert_whole_request_folded` 의 `B_after == 0` 이 판정 불가 4축
        (provider transient/persistent · DB transient/permanent · gated deadline)에서 **매번**
        같은 요청의 무료 topic 을 검사한다. 그 속성 자체를 겨냥한 변이(`GatedUnavailable` 분기에서
        무료만 미리 등록)가 **네 축을 동시에 죽인다** — 근거는 중복이 아니라 4중이다
      ⛔ 철회 축들은 **시계를 전진시키지 않은 채** 검증한다 — 전진시키면 만료 게이트가 대신
      통과시켜 단언이 공허해진다.
      그 전까지 flag off 유지.
      — A1/A2 **산술** land(2026-07-27, `app/clock.py` + `app/topic_lease.py`, 배포 없음).
      ⛔ 구 `strict cache → 3-state verifier → single-flight` 순서는 ADR-040에서 **폐기**됐다.
      현재 **Stage 2 wire**(lease 실린 ack) + 서버 lease 발급 + **publish 직전 lease 게이트** +
      **KRX per-user 판정**(`4a45173`)까지 land. iOS 는 토큰·ack/error 소비 + 재연결 배칭(N→1) +
      `temporarily_unavailable` 제한 재시도까지.
      ⛔ 구 "reconciler + 1 in-flight → 배칭" 순서는 **개정됐다** — 배칭이 먼저 land 했고 reconciler 는
      보류다(위 "순서 개정" 절). **서버 lease 발급 + publish 직전 인가는 land 했다** —
      다음은 **② iOS lease 소비**(최단 만료 전 재인증, `duration: 0` = "지금 재인증") +
      **③ 클라 request timeout**(활성화 blocker). 관측 cache·single-flight는 실제 병목 측정 전에는 넣지 않는다.
- [ ] 웹 디버그 페이지 Stage B.
- [ ] Stage B 측정: store console primary + 서버 보조(iOS UA / Android 토큰+UID).
- [x] **제품 결정 S5(revoke latency) = bounded-lease v1 15분 확정(2026-07-25)** — §7 S5 / §8 만료 항목 참조.
- [ ] **제품 결정 S4(유예 기간·supersede) — Stage B 전 확정**. ⚠️ S4는 *legacy를 언제 끄느냐*라 **Stage B 게이트**이고
      WS 인증(1C) 구현의 블로커는 아니다(구 표기는 S4·S5를 "Stage B/WS 전"으로 함께 묶어 오해 소지).
- [x] **제품 결정 S6(last-good 최대 stale) = 24h hard cutoff 확정(2026-07-21)** — **서버(d9e60ab, 배포)+iOS(7050bd3, dev-side) 구현 완료**((now-as_of)>=24h 렌더 거부; 서버 503 + 클라 `.unavailable`+fetch-독립 expiryTask). 실사용자 노출은 App Store 배포 후. source-level staleness는 별도 source-health. + [ ] validator value-level 완결(entry.rate 타입/point 내부).
