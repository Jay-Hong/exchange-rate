# USDT Phase 1 — iOS/Android 클라이언트 구현 가이드

> ⚠️ **Superseded — 데이터 수신 방식 부분 폐기 (2026-05-06 갱신)**
>
> 이 가이드는 USDT Phase 1 백엔드 구현 직후 작성되었다. 본문에서 가정한 **데이터 수신 방식은 legacy 통합 모델(`/api/rates` + `/ws` `rates` 배열에 USDT 포함)** 기반이고, 이는 **iOS dev/test 단계의 임시 모델**이다.
>
> **서비스 출시 계약 (현재 합의)**:
> - USDT 거래소 / KRX 미국달러선물 = **topic-only** (legacy `rates` 배열 미포함)
> - dual-emit 범위 = 환율 탭 데이터에만 (테더/KRX는 topic만 발사)
>
> 새 topic protocol 도입 후 본 가이드는 V2 클라이언트 가이드로 대체될 예정.
>
> **본 가이드의 다음 항목은 여전히 참고 유효**:
> - `RateSource` 모델 (`source` + `asset` + `category` + `displayName`)
> - 어댑터 패턴 (서버 응답 → 내부 모델 변환)
> - SourceRegistry 구조
> - 표시 정책 (sortOrder, displayName, currency formatting)
>
> **본 가이드의 다음 항목은 V2 protocol 도입 후 재작성**:
> - "기존 `/api/rates` 응답은 호환성 때문에 `bank + currency`로 내려오므로 어댑터로 변환"
> - "/ws `rates` 배열에서 USDT 수신" 가정
>
> 단일 진리 source: [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) + [DECISIONS.md ADR-028](DECISIONS.md)
>
> Status: 부분 superseded (도메인 모델 참고 유효, 데이터 수신 방식 V2 protocol로 대체 예정)
> Updated: 2026-04-23 (원본); 2026-05-06 (status 갱신)
> Related: [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md), [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md)

## 이 문서의 목적

USDT Phase 1 백엔드 작업이 완료되었다. 이제 iOS/Android 테더 탭 구현이 남았다.

이 문서는 **iOS와 Android가 같은 개념 모델을 쓰도록** 공통 설계 기준을 제시한다. 두 플랫폼이 각자 다르게 설계하면 이후 비교 알림(Phase 3) 통합 시 반드시 divergence가 발생한다.

## 핵심 원칙

### 1. 기존 `Bank` enum을 확장하지 말 것

iOS `Bank.swift`, Android `Bank.kt` 둘 다 "은행" 도메인 가정으로 작성되어 있다. `displayName = "국민은행"`, `shortName = "국민"`처럼. 여기에 `UPBIT, BITHUMB, COINONE, GOPAX, KORBIT`을 추가하면:

- `displayName` 컨벤션이 깨짐 (예: "업비트 은행"?)
- `iconRes`/`colorHex`는 은행 아이콘 전용 리소스와 섞임
- `Bank.sortedEntries`를 쓰는 로직(예: [Android ExchangeRate.kt:88-89](../android/app/src/main/java/com/jay/fxi/domain/model/ExchangeRate.kt#L88))이 암묵적으로 깨질 수 있음 (unknown bank = -1)

**해법**: 별도 `RateSource` (또는 `Source`) 모델을 도입하고, Bank는 **은행 정보 계층** 그대로 유지한다. 서버는 이미 `source + asset` 구조이므로 클라이언트도 같은 개념으로 간다.

### 2. 서버 모델과 같은 개념: `source + asset`

서버:

- `source_rates` 테이블: `source`, `asset`, `rate`, `timestamp`
- `source_notification_settings.source + .asset`
- `comparison_alerts` (Phase 3): `left_source, left_asset, right_source, right_asset`

클라이언트도 동일 개념 모델을 쓴다. 단, 기존 `/api/rates` 응답은 호환성 때문에 `bank + currency`로 내려오므로 **어댑터로 변환**해서 내부 모델에 채운다.

### 3. 레거시 API는 그대로, 새 API는 source/asset

| API | 필드 | 용도 |
|--- |--- |--- |
| `GET /api/rates` | `bank` + `currency` | 기존 + USDT 자동 포함 |
| `GET /api/rates/{currency}` | `bank` + `currency` | `/api/rates/usdt-krw`도 지원 |
| `POST/GET/PUT/DELETE /api/source-notification-settings` | `source` + `asset` | 테더 탭 알림 전용 |
| `POST/GET/PUT/DELETE /api/notification-settings` (기존) | `bank` + `currency` | 달러/엔화/유로 탭 알림 |

## 공통 개념 모델

### `RateSource` (공통 정의)

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `id` | `String` | canonical key (`f"{source}:{asset}"`) |
| `source` | `String` | `upbit` / `bithumb` / `investing` / `kb` / ... |
| `asset` | `String` | `usdt-krw` / `usd-krw` / `jpy-krw` / ... |
| `displayName` | `String` | 한글 표시명 (`업비트`, `인베스팅`, `국민은행`) |
| `category` | `enum` | `.exchange` / `.reference` / `.derivative` |
| `sortOrder` | `Int` | 기본 표시 순서 (레지스트리 기준) |

**Stale 판정은 Phase 1에서 도입하지 않는다** (아래 "Stale 표시 정책" 섹션 참조). `freshnessSeconds` 필드는 사용하지 않는다.

### `SourceRate` (시세 데이터)

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `source` | `String` | |
| `asset` | `String` | |
| `rate` | `Double` | |
| `timestamp` | `Date` | 마지막 **값 변경** 시각 (insert-if-changed 정책 결과). UI에는 "N초 전 변동" 형태로 정보 표시. stale 판정에 사용하지 않음. |

앱 내부에서 이 모델로 통일한다. 서버 응답(`bank` + `currency`)은 어댑터로 `source` + `asset`으로 변환한다.

### 변환 어댑터 (한 방향: 서버 응답 → 내부 모델)

```
서버 응답 ExchangeRate { currency, bank, rate, timestamp }
  ↓ 어댑터
내부 SourceRate { source=bank, asset=currency, rate, timestamp }
```

필드명이 다를 뿐 의미는 같다. 역방향 변환은 필요 없다 (내부 모델은 항상 `source` + `asset`로 유지, 서버로 보낼 때는 API별 스키마 그대로 사용).

## 소스 레지스트리 (클라이언트 측)

서버의 [`app/source_registry.py`](app/source_registry.py)와 **같은 9개 엔트리**를 앱에 하드코딩한다. 런타임 조회 불필요 — 정적 상수로 유지.

```text
인베스팅    investing   usd-krw         reference   sort=10
국민은행     kb          usd-krw         reference   sort=20
하나은행     hana        usd-krw         reference   sort=30
미국달러F    krx         usd-krw-futures derivative  sort=40  (Phase 2, phase1_enabled=false)
업비트       upbit       usdt-krw        exchange    sort=50
빗썸         bithumb     usdt-krw        exchange    sort=60
코인원       coinone     usdt-krw        exchange    sort=70
코빗         korbit      usdt-krw        exchange    sort=80
고팍스       gopax       usdt-krw        exchange    sort=90
```

### iOS 예시 (Swift)

```swift
enum RateSourceCategory: String {
    case exchange, reference, derivative
}

struct RateSourceDefinition {
    let source: String
    let asset: String
    let displayName: String
    let category: RateSourceCategory
    let sortOrder: Int

    var id: String { "\(source):\(asset)" }
}

enum SourceRegistry {
    static let all: [RateSourceDefinition] = [
        .init(source: "investing", asset: "usd-krw", displayName: "인베스팅",
              category: .reference, sortOrder: 10),
        .init(source: "kb", asset: "usd-krw", displayName: "국민은행",
              category: .reference, sortOrder: 20),
        .init(source: "hana", asset: "usd-krw", displayName: "하나은행",
              category: .reference, sortOrder: 30),
        .init(source: "upbit", asset: "usdt-krw", displayName: "업비트",
              category: .exchange, sortOrder: 50),
        .init(source: "bithumb", asset: "usdt-krw", displayName: "빗썸",
              category: .exchange, sortOrder: 60),
        .init(source: "coinone", asset: "usdt-krw", displayName: "코인원",
              category: .exchange, sortOrder: 70),
        .init(source: "korbit", asset: "usdt-krw", displayName: "코빗",
              category: .exchange, sortOrder: 80),
        .init(source: "gopax", asset: "usdt-krw", displayName: "고팍스",
              category: .exchange, sortOrder: 90),
    ]

    static func find(source: String, asset: String) -> RateSourceDefinition? {
        all.first { $0.source == source && $0.asset == asset }
    }
}
```

### Android 예시 (Kotlin)

```kotlin
enum class RateSourceCategory { EXCHANGE, REFERENCE, DERIVATIVE }

data class RateSourceDefinition(
    val source: String,
    val asset: String,
    val displayName: String,
    val category: RateSourceCategory,
    val sortOrder: Int,
) {
    val id: String get() = "$source:$asset"
}

object SourceRegistry {
    val all: List<RateSourceDefinition> = listOf(
        RateSourceDefinition("investing", "usd-krw", "인베스팅",
            RateSourceCategory.REFERENCE, 10),
        RateSourceDefinition("kb", "usd-krw", "국민은행",
            RateSourceCategory.REFERENCE, 20),
        RateSourceDefinition("hana", "usd-krw", "하나은행",
            RateSourceCategory.REFERENCE, 30),
        RateSourceDefinition("upbit", "usdt-krw", "업비트",
            RateSourceCategory.EXCHANGE, 50),
        RateSourceDefinition("bithumb", "usdt-krw", "빗썸",
            RateSourceCategory.EXCHANGE, 60),
        RateSourceDefinition("coinone", "usdt-krw", "코인원",
            RateSourceCategory.EXCHANGE, 70),
        RateSourceDefinition("korbit", "usdt-krw", "코빗",
            RateSourceCategory.EXCHANGE, 80),
        RateSourceDefinition("gopax", "usdt-krw", "고팍스",
            RateSourceCategory.EXCHANGE, 90),
    )

    fun find(source: String, asset: String): RateSourceDefinition? =
        all.firstOrNull { it.source == source && it.asset == asset }
}
```

**주의**:

- `krx / usd-krw-futures`는 Phase 2이므로 지금은 레지스트리에서 **생략**하거나, 엔트리를 넣되 `phase1_enabled=false` 같은 플래그로 걸러낸다.
- 기존 `Bank` enum은 그대로 둔다. RateSource와 공존.

## 테더 탭 데이터 흐름

```text
서버 /api/rates, /ws
  ↓ 기존 Codable/Serializable (ExchangeRate { currency, bank, rate, timestamp })
앱 레이어
  ↓ adapter: bank → source, currency → asset
SourceRate { source, asset, rate, timestamp }
  ↓ 필터
현재 테더 탭 = asset == "usdt-krw" 인 것 (5 거래소) + reference 3개(investing/kb/hana usd-krw)
  ↓ SourceRegistry lookup
최종 화면용 row: RateSourceDefinition + SourceRate + diff_vs_baseline
```

### 단일 리스트 UI (Decision C 확정) — 기존 환율 탭 UX 완전 재사용

```text
인베스팅  1,452.5  기준 ▲         ← 맨 위 = 자동 기준
국민은행  1,453.2  +0.7
하나은행  1,452.8  +0.3
(미국달러F — Phase 2)
업비트    1,486.0  +33.5
빗썸      1,486.0  +33.5
코인원    1,485.0  +32.5
고팍스    1,485.0  +32.5
코빗      1,487.0  +34.5
```

**동작 원칙 (기존 달러/엔화/유로 탭과 완전히 동일)**:

- **맨 위 소스 = 자동 기준**. 나머지 소스는 기준 대비 차이값 표시.
  - iOS: [`ExchangeRateViewModel.swift:126`](../ios/FXi/ViewModels/ExchangeRateViewModel.swift#L126) — `let reference = filtered.first`
  - Android: [`BankPreference.kt:39`](../android/app/src/main/java/com/jay/fxi/domain/model/BankPreference.kt#L39) — `val referenceRate = orderedRates.firstOrNull()`
- **사용자 커스터마이즈**: 기존 `BankCustomizeSheet` UX처럼 사용자가 소스 순서를 drag-and-drop으로 재배열. 저장된 순서가 다음 세션에도 유지.
- **기본 순서**: `SourceRegistry.sortOrder` (인베스팅 → KB → 하나 → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗)
- **Phase 1에서 도입하지 않는 기능**:
  - 가격 기반 동적 정렬 (오름차순/내림차순). 수동 재배열과는 별개 기능 — 가격 변동에 따라 순서가 실시간으로 바뀌는 정렬을 원한다면 추가 구현 필요. 현재 Phase 1 범위에는 포함하지 않음.
  - 기준 소스 변경 토글. 맨 위가 자동 기준이므로 기능상 불필요 (사용자가 원하는 소스를 맨 위로 재배열하면 됨).
- **막대 비교 UI**: 기존 환율 탭 `RateBarView` 컴포넌트 그대로 재사용

**구현 포인트**:

- 기존 `BankPreferenceManager`를 그대로 쓸 수 없음 (Bank enum 기반, Decision E 충돌)
- 별도 `SourcePreferenceManager` (또는 테더 탭 전용 preference) 신규 도입
- 저장 key: canonical key (`"upbit:usdt-krw"` 등) 리스트로 순서 저장
- 기본값: registry sort_order 순서
- 사용자 재배열 시 해당 탭 전용 순서로 persisted

### 요약/최저/최고는 클라이언트 계산

서버가 안 주는 이유: 기준 소스가 사용자 선택에 따라 바뀌기 때문. 클라이언트에서 `rates.filter { asset == "usdt-krw" }`에 대해 `min`/`max`/`avg` 계산하면 끝.

### Stale 표시 정책 — Phase 1에서는 도입하지 않음

현재 `timestamp`는 **마지막 값 변경 시각**(insert-if-changed 정책 결과)이지 **마지막 수집 성공 시각**이 아니다. 두 개념이 다르기 때문에 단순 `now - timestamp` 비교로는 다음과 같은 오진이 발생한다:

- **은행 주말 정지**: investing/kb/hana는 주말 ~48-72h 정상적으로 값 변경 없음 → stale 오판
- **USDT 저유동성**: 5개 거래소가 동일 가격에서 수 분간 유지 → stale 오판

따라서 Phase 1 클라이언트는 다음을 지킨다:

- `timestamp`를 **정보 표시용**으로만 사용 (예: "마지막 변동: 5초 전", "2시간 전")
- stale 배지, 요약값에서의 stale 제외, 회색 처리 등은 **구현하지 않는다**
- 진짜 수집 장애는 서버 측 관리자 페이지에서 별도 탐지

추후 서버에 `observed_at` 또는 `last_collection_success_at` 추적이 추가되면 그때 stale UI를 재도입한다.

## WebSocket 처리

기존 `/ws` 엔드포인트는 변경 없음. `data.rates` 배열에 이제 `usdt-krw` 엔트리가 자동 포함된다.

- 기존 클라이언트: currency 필터에서 `usdt-krw`를 걸러내므로 자동 무시 (검증 완료)
- 새 테더 탭: `data.rates.filter { currency == "usdt-krw" }`로 추출

**하위 호환성 유의사항**:

- `metadata.banks`와 `metadata.currencies`는 **레거시 호환용 dead field**로 동결되었다.
  - `metadata.banks`: `investing, kb, hana, shinhan, woori, ibk, nh, sc, bs, citi` (10개, 고정)
  - `metadata.currencies`: `usd-krw, jpy-krw, eur-krw` (3개, 고정)
  - USDT 거래소(`upbit, bithumb, coinone, korbit, gopax`)와 `usdt-krw`는 **포함되지 않는다**.
- 이유: 이 필드는 현재 iOS/Android/웹 어디서도 사용되지 않는 dead field라서, USDT source를 섞어 넣으면 시맨틱 오염만 커진다. 미래에 필드 자체를 제거할 예정이므로 레거시 값으로 동결하는 것이 가장 안전하다.
- 새 앱은 이 필드를 **쓰지 말 것** — 대신 `SourceRegistry`와 `rates` 배열을 참조한다. `rates` 배열에는 USDT 엔트리가 정상 포함된다.

## Topic API (Phase Z-2b, REALTIME v2)

테더 탭 전용 신규 채널. legacy `/ws` `rates` 배열과 **별개 채널**로 동시 운영
(legacy는 유지). topic API는 ADR-028 "topic-only Tether/KRX" 계약 구현.

### Legacy endpoint 제거 (Phase Z-2d, 2026-05-12)

**`/api/rates/{currency}` 요청 시 410 Gone 응답** (PR Z-2d Step 4):

| 요청 | 응답 |
| --- | --- |
| `GET /api/rates/usdt-krw` | HTTP **410 Gone** + `detail.use_topic="usdt:krw"` |
| `GET /api/rates/usd-krw-futures` | HTTP **410 Gone** + `detail.use_topic="usdt:krw"` |
| `GET /api/rates/usd-krw` (allowed FX) | HTTP 200 (그대로) |

**Detail shape**:

```json
{
  "detail": {
    "error": "legacy_rate_removed",
    "currency": "usdt-krw",
    "use_topic": "usdt:krw"
  }
}
```

**단말 구현 권고**:

- legacy `/api/rates/{usdt-krw|usd-krw-futures}` 호출 금지 — 모두 410. `use_topic` 안내 따라 WebSocket topic subscribe로 마이그레이션.
- legacy `/api/rates` aggregate와 WebSocket `/ws` legacy `rates` 배열도 USDT/KRX **자동 제외**됨 (Z-2d allowlist). 즉 legacy 응답을 파싱해 `currency == "usdt-krw"` entry를 찾지 못한다 — 정상 동작.
- **단말 USDT freshness 기준은 `usdt:krw` topic payload만** 사용. legacy rates 배열의 timestamp는 USDT에 대한 신호 X.

### WebSocket URL / Subscribe protocol

```text
URL:       wss://fxi.kr/ws (기존 endpoint 재사용)
Subscribe: {"type": "subscribe", "topics": ["usdt:krw"]}
Unsubscribe: {"type": "unsubscribe", "topics": ["usdt:krw"]}
```

- Subscribe 메시지 전송 → 서버 registry 등록 → topic 변경 발생 시 snapshot 수신
- `TOPIC_DISPATCHER_ENABLED=false` 시 subscribe 메시지는 silently ignore
  (기존 client 호환)

### Topic 이름

- `usdt:krw` — 테더 탭 (USDT 5거래소 + USD/KRW 은행 + Investing reference +
  KRX 미국달러선물 optional)

### Payload top-level

```jsonc
{
  "type": "snapshot",
  "version": 1,
  "topic": "usdt:krw",
  "data": { /* 4 groups */ }
}
```

- `version=1`: schema version. 미래 호환 변경 시 bump
- `type="snapshot"`: 현재 전체 상태 dump 형태. delta는 미정 (필요 시 별도 type)
- `topic`: 송신 topic 이름. multi-topic 동시 구독 시 단말이 수신 메시지의
  topic 식별용 (2026-05-11 추가). 단말은 `payload["topic"]`으로 분기 가능 —
  예: `if payload["topic"] == "usdt:krw"`. builder는 여전히 topic-agnostic,
  publisher wrapper가 schema 책임.

### Entry shape (모든 그룹 공통)

```jsonc
{
  "source": "upbit",          // string, 데이터 공급자 식별
  "asset": "usdt-krw",        // string, 통화쌍/상품
  "rate": 1485.0,             // float, KRW
  "timestamp": "2026-05-11T15:00:00+09:00"  // ISO8601 KST, 데이터 관측 시각
}
```

**Entry 식별 + 표시 정책**:

- Entry 식별자는 **`(source, asset)` tuple**. 같은 source가 다른 asset 가능
  (예: 미래 `krx + usd-krw-futures` + `krx + 다른 KRX 상품`).
- 서버 payload는 **표시명/짧은 이름/아이콘/색상/정렬을 보내지 않음** (2026-05-11
  `display_name` 제거).
- 단말은 `(source, asset)` tuple로 자체 registry를 lookup해 표시명, 짧은 이름,
  아이콘, 색상, 정렬을 결정. iOS는 [`Constants.swift`](../ios/FXi/Utils/Constants.swift)
  `Bank enum` + `RateSource`, Android는 [`Bank.kt`](../android/app/src/main/java/com/jay/fxi/domain/model/Bank.kt)
  enum 패턴.
- 새 source 추가 시 단말 registry 업데이트 필요 (`KRX`, USDT 거래소 등).
- 미등록 source는 서버가 payload에 그대로 포함 — 단말이 무시하거나 source 코드
  raw 표시 가능 (정책).

### `data` 그룹

**Required** (key는 항상 존재, list/dict는 비어있을 수 있음):

| key | type | 내용 |
| --- | --- | --- |
| `usdt_krw` | list[entry] | USDT/KRW 5거래소 (upbit, bithumb, coinone, korbit, gopax) |
| `usd_krw_banks` | list[entry] | USD/KRW 은행 (현재 kb, hana) |
| `usd_krw_reference` | entry | USD/KRW Investing reference |

**Optional**:

| key | type | 조건 |
| --- | --- | --- |
| `usd_krw_futures` | entry | `KRX_TOPIC_INCLUDE=true` + KRX 데이터 존재 시만 |

list 그룹은 SourceRegistry sort_order로 서버 측 정렬됨. 단말은 받은 순서 그대로
표시하면 됨 (재정렬 불필요).

### `usd_krw_futures` (KRX 미국달러선물) — Optional 처리

**키 자체 부재 가능성**:

- 서버 운영 `KRX_TOPIC_INCLUDE=false` (Z-2d cleanup 후 legacy 노출은 legacy_policy allowlist로 단일화 — 본 flag는 topic 안 KRX optional group 전용)
- 또는 KRX 데이터 없음 / source/asset mismatch

**구현 가이드**:

- `data["usd_krw_futures"]` 존재 여부로 분기 — 미존재 시 해당 항목 미표시
- 존재 시 일반 entry처럼 `rate` + `timestamp` 표시

**Stale 처리 — 일반 source와 동일 원칙**:

- 은행/Investing이 휴장/주말에 마지막 값을 그대로 표시하는 것과 같은 패턴
- KRX 휴장 (15:45~18:00, 05:00~다음 거래일, 주말): 마지막 거래값 + timestamp 그대로 유지
- 단말은 source별 분기 X — 일반 표시 정책 그대로 (rate + 시간)
- `timestamp`로 사용자에게 "마지막 거래 시점" 정보 제공 가능 (선택)

### FX topic schema (Phase Z-2c 예정)

새 단말이 legacy WebSocket/API를 보지 않고 모든 외환 탭을 topic API로 처리하려면
통화별 FX topic을 사용한다. `usdt:krw`와 동일하게 top-level은
`type/version/topic/data`, entry는 `{source, asset, rate, timestamp}` shape를 사용한다.

**Topic 이름**:

| topic | 탭 | asset |
| --- | --- | --- |
| `fx:usd-krw` | 달러 | `usd-krw` |
| `fx:jpy-krw` | 엔화 | `jpy-krw` |
| `fx:eur-krw` | 유로 | `eur-krw` |

**Payload 예시**:

```jsonc
{
  "type": "snapshot",
  "version": 1,
  "topic": "fx:usd-krw",
  "data": {
    "banks": [
      {
        "source": "kb",
        "asset": "usd-krw",
        "rate": 1370.5,
        "timestamp": "2026-05-11T15:00:00+09:00"
      }
    ],
    "reference": {
      "source": "investing",
      "asset": "usd-krw",
      "rate": 1371.2,
      "timestamp": "2026-05-11T15:00:00+09:00"
    }
  }
}
```

**`data` 그룹**:

**Required** (key는 항상 존재, list는 비어있을 수 있음):

| key | type | 내용 |
| --- | --- | --- |
| `banks` | list[entry] | 해당 asset의 은행 고시 환율 (transient 빈 list 허용) |

**Optional**:

| key | type | 조건 |
| --- | --- | --- |
| `reference` | entry | Investing 데이터 존재 + `(source="investing", asset=<topic asset>)` 정확 일치 시만 포함. 미수집 / source-asset mismatch 시 key 자체 누락 — `usdt:krw`의 `usd_krw_reference`와 동일 정책 (`app/usdt_topic_payload.py` schema 무결성 보호 정책). 단말은 `data["reference"]` 존재 여부로 분기 |

FX topic은 topic 하나가 단일 asset만 표현하므로 `usd_krw_banks` 같은 asset prefix를
data key에 반복하지 않는다. 단말은 `payload["topic"]`으로 탭을 구분하고,
모든 FX 탭에서 `data.banks`(+ 선택적 `data.reference`)를 같은 방식으로 처리한다.

**Entry registry (FX 섹션 재확인)** — entry 식별자는 위 "Entry 식별 + 표시 정책"
섹션과 동일하게 `(source, asset)` tuple. 따라서 단말 registry는 동일 source라도
asset별 별도 entry가 필요하다 (예: `kb + usd-krw`, `kb + jpy-krw`, `kb + eur-krw`
각각 별 entry). 미등록 `(source, asset)` 조합은 그대로 raw 표시 또는 무시 가능.

### 운영 모니터링

`GET /admin/api/topic-status` (HTTP Basic auth):

```jsonc
{
  "enabled": true,                          // TOPIC_DISPATCHER_ENABLED
  "topic": "usdt:krw",
  "krx_topic_include": true,                // KRX_TOPIC_INCLUDE (legacy와 분리)
  "subscribed_connection_count": 2,
  "hook_called": 12345,
  "skipped_disabled": 0,
  "built": 12340,
  "publish_called": 12340,
  "publish_sent_total": 24680,
  "error": 0,
  "last_result": "sent",
  "last_at_kst": "...",
  "last_error": null
}
```

reset (시험 구간 분리용): `POST /admin/api/topic-status/reset`.

**FX topic 별도 endpoint** (PR Z-2c, 3 asset 일괄 조회):

`GET /admin/api/topic-status/fx`:

```jsonc
{
  "fx:usd-krw": {
    "enabled": false,                       // FX_TOPIC_ENABLED AND TOPIC_DISPATCHER_ENABLED
    "fx_topic_enabled": false,              // config.FX_TOPIC_ENABLED
    "topic_dispatcher_enabled": true,
    "topic": "fx:usd-krw",
    "asset": "usd-krw",
    "subscriber_count": 0,
    "hook_called": 0,
    "skipped_disabled": 0,
    "built": 0,
    "publish_called": 0,
    "publish_sent_total": 0,
    "error": 0,
    "last_result": null,
    "last_at_kst": null,
    "last_error": null
  },
  "fx:jpy-krw": { /* same shape */ },
  "fx:eur-krw": { /* same shape */ }
}
```

reset (3 topic 일괄): `POST /admin/api/topic-status/fx/reset` → `{"results": {asset: bool}, "success": bool, "reason": str | null}`.

각 FX topic의 Redis telemetry key는 `topic:fx:<asset>:stats` (tether legacy `topic:tether:stats`와 namespace 분리). 운영자는 asset별 counter 독립 관찰 가능.

### Schema 버전 관리

- `version=1` lock-in 시점은 클라이언트 release 동기화 후.
- 기존 키 의미 변경 금지. 새 그룹 추가는 `version` 유지 가능 (forward-compat).
- 의미 충돌 시 새 key + `version` bump.

## 알림 통합

### FCM payload type 처리

이제 두 종류의 FCM `type`이 온다:

| `type` | 의미 | data 필드 |
|--- |--- |--- |
| `rate_alert` | 기존 bank+currency 알림 | `bank`, `currency`, `rate`, `threshold`, `condition`, `setting_id` |
| `source_rate_alert` | **신규** source+asset 알림 | `source`, `asset`, `rate`, `threshold`, `condition`, `setting_id` |

**필수 처리**:

- FCM 수신 핸들러에서 `type` 분기 처리
- 알려지지 않은 type (향후 `comparison_alert` 같은)은 안전하게 무시
- 로컬 히스토리 DB에 저장해서 사용자 조회 가능하도록 (Phase 1 이후 기능)

### 알림 설정 API 분리

| 탭 | 사용 API | setting 객체 형태 |
|--- |--- |--- |
| 달러/엔화/유로 | `/api/notification-settings` | `{ bank, currency, condition, threshold, is_enabled }` |
| 테더 | `/api/source-notification-settings` | `{ source, asset, condition, threshold, is_enabled }` |

**주의**: 탭 스코프로 알림을 관리한다 (달러 탭에서 만든 알림은 달러 탭에만 표시). 통합 "내 알림" 화면이 필요하면 두 API를 모두 호출해 클라이언트에서 합치거나 서버에 집계 엔드포인트 추가 요청.

### 서버 검증 (category=="exchange")

테더 탭 알림 생성 시 `source`가 거래소(`upbit`, `bithumb`, `coinone`, `korbit`, `gopax`)가 아니면 서버가 `400` 반환:

```
"Source alerts are only supported for exchange sources in Phase 1 (got category=reference).
Use /api/notification-settings for bank/investing alerts."
```

클라이언트는 이 응답을 사용자 친화적 메시지로 매핑할 것.

## Phase 3 비교 알림 대비

지금 모델을 `source + asset` 구조로 잡아두면, Phase 3에서 비교 알림 추가 시 자연스럽게 확장된다:

```swift
struct ComparisonAlert {
    let id: Int
    let left: RateSourceReference    // { source, asset }
    let right: RateSourceReference   // { source, asset }
    let diffType: DiffType           // .signed / .absolute
    let operator_: ComparisonOperator // .gte / .lte
    let threshold: Double
    let isEnabled: Bool
    let triggered: Bool
}

struct RateSourceReference {
    let source: String
    let asset: String
}
```

서버 스키마(`USDT_PHASE1_DESIGN.md` § Comparison Alerts Draft)의 `left_source, left_asset, right_source, right_asset` 4컬럼과 매핑. Bank enum 확장으로 가면 이 확장성을 얻지 못한다.

## 구현 순서 (권고)

1. **공통 데이터 모델 정의** (iOS + Android 동시에 같은 개념으로)
   - `RateSource`, `SourceRate`, `SourceRegistry`, `RateSourceCategory`
2. **어댑터 구현**
   - 기존 `ExchangeRate` → `SourceRate` 변환
3. **테더 탭 UI 스켈레톤** (기존 환율 탭 컴포넌트 재사용)
   - 단일 리스트, 막대 차트, 정렬/기준 토글
4. **FCM 핸들러 `type` 분기**
   - `rate_alert`, `source_rate_alert` 처리
5. **Source 알림 설정 UI**
   - `/api/source-notification-settings` 호출
   - 거래소 5종만 선택 가능 (SourceRegistry 필터)
6. **로컬 알림 히스토리 저장**
   - 두 type 모두 로컬 DB에 저장
7. **테스트**
   - 달러/엔화/유로 탭 영향 없는지 회귀 검증
   - 테더 탭 렌더링 (실시간 수신, 정렬, 기준 변경)
   - 알림 생성→발송→자동 비활성화 end-to-end

## 교차 플랫폼 일관성 체크리스트

iOS/Android 리뷰 시 다음 항목 동일한지 확인:

- [ ] `RateSource` 모델 필드 (id, source, asset, displayName, category, sortOrder)
- [ ] `RateSourceCategory` enum 3종 (exchange, reference, derivative)
- [ ] `SourceRegistry` 9개 엔트리 (인베스팅, 국민은행, 하나은행, 업비트, 빗썸, 코인원, 고팍스, 코빗 + (미국달러F))
- [ ] 정렬 기본값: SourceRegistry.sortOrder
- [ ] stale 판정 **미구현** (Phase 1 범위 외, timestamp는 "N초 전 변동" 정보 표시용만)
- [ ] 어댑터: `ExchangeRate.bank → SourceRate.source`, `.currency → .asset`
- [ ] FCM `type` 분기 (rate_alert / source_rate_alert / unknown 무시)
- [ ] 알림 API 분리 (bank vs source endpoint)

## 호환성 요약

| 대상 | 영향 | 조치 |
|--- |--- |--- |
| 기존 iOS/Android 앱 | `/api/rates` 응답에 usdt-krw 추가됨. 현재 앱은 currency 필터로 배제 → 영향 없음 (검증 완료). | 없음 |
| 기존 Web 관리자 템플릿 | currencies 하드코딩 (`templates/index.html:1590`) → 영향 없음 (검증 완료). | 없음 |
| 새 테더 탭 앱 | 이 문서 기준으로 신규 개발 | iOS/Android 공통 모델 사용 |
| `metadata.banks/currencies` dead field | non-optional Codable/Serializable → 즉시 제거 불가. | USDT 릴리스에서 optional 변경 → 서버 제거 (기술부채) |

## 참고 문서

- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md) — 백엔드 설계 (테이블/API/스케줄러)
- [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md) — 제품 방향 + Decision A~F
- [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md) — 기존 알림 시스템
- [CRAWLERS.md § Group E](CRAWLERS.md) — 거래소 API 엔드포인트
