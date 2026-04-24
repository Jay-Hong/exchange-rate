# USDT Phase 1 — iOS/Android 클라이언트 구현 가이드

> Status: design guide (iOS/Android 구현 전 설계 기준)
> Updated: 2026-04-23
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
고팍스       gopax       usdt-krw        exchange    sort=80
코빗         korbit      usdt-krw        exchange    sort=90
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
        .init(source: "gopax", asset: "usdt-krw", displayName: "고팍스",
              category: .exchange, sortOrder: 80),
        .init(source: "korbit", asset: "usdt-krw", displayName: "코빗",
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
        RateSourceDefinition("gopax", "usdt-krw", "고팍스",
            RateSourceCategory.EXCHANGE, 80),
        RateSourceDefinition("korbit", "usdt-krw", "코빗",
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

### 단일 리스트 UI (Decision C 확정)

```text
인베스팅  1,452.5  기준
국민은행  1,453.2  +0.7
하나은행  1,452.8  +0.3
(미국달러F — Phase 2)
업비트    1,486.0  +33.5  (막대 그래프 가장 짧게)
빗썸      1,486.0  +33.5
코인원    1,485.0  +32.5
고팍스    1,485.0  +32.5
코빗      1,487.0  +34.5
```

- 기본 정렬: SourceRegistry.sortOrder
- 사용자 토글:
  - 가격 오름차순/내림차순
  - 기준 소스 변경 (Investing → KB → 하나)
- 막대 비교 UI는 기존 환율 탭 컴포넌트 재사용

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

- `metadata.banks`에 `upbit, bithumb, coinone, gopax, korbit`가 포함된다. 기존 앱은 이 필드를 읽지 않아 안전하다 (검증 완료). 새 앱도 이 필드를 **쓰지 말 것** — 대신 `SourceRegistry`와 `rates` 배열을 쓴다.
- `metadata.currencies`에 `usdt-krw`가 포함된다. 마찬가지로 사용 안 함.

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

테더 탭 알림 생성 시 `source`가 거래소(`upbit`, `bithumb`, `coinone`, `gopax`, `korbit`)가 아니면 서버가 `400` 반환:

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
