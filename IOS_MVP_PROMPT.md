# FXi iOS MVP 개발 프롬프트

> **목적**: Xcode Claude Code에게 전달할 iOS 앱 초기 구현 가이드
> **Phase**: 1 - iOS MVP (알림 기능 제외)

---

## 🎯 프로젝트 개요

**FXi**는 실시간 은행 간 환율 비교 서비스입니다. 인베스팅닷컴 기준 환율과 9개 은행의 환율을 비교하여 사용자가 최적의 환전 시점을 파악할 수 있도록 합니다.

### 핵심 기능

1. **실시간 환율 비교**: WebSocket으로 10초마다 업데이트
2. **24시간 그래프**: 통화별 환율 추이 시각화
3. **은행별 비교 바 차트**: 기준 환율 대비 차이 표시

---

## 📱 앱 구조

### 탭 구성 (TabView)

```
┌─────────────────────────────────────────────┐
│  [USD/KRW]  [JPY/KRW]  [EUR/KRW]            │
├─────────────────────────────────────────────┤
│                                             │
│  ┌─────────────────────────────────────┐   │
│  │      📈 24시간 환율 그래프           │   │
│  │      (Chart - 각 탭별 통화)          │   │
│  └─────────────────────────────────────┘   │
│                                             │
│  ┌─────────────────────────────────────┐   │
│  │  인베스팅  ████████████  1,407.50   │   │
│  │  국민은행  ██████████    1,409.20   │   │
│  │  하나은행  █████████     1,408.80   │   │
│  │  신한은행  ████████      1,410.10   │   │
│  │  ...                                 │   │
│  └─────────────────────────────────────┘   │
│                                             │
└─────────────────────────────────────────────┘
```

### 화면별 상세

각 탭 (USD/KRW, JPY/KRW, EUR/KRW)은 동일한 레이아웃:

1. **상단**: 24시간 환율 그래프 (Swift Charts)
2. **하단**: 은행별 환율 비교 리스트 (ScrollView)

---

## 🔌 API 연동

### Base URL

```
https://fxi.n-e.kr
```

### 공통 규칙

- **타임존**: 모든 시간은 **KST (UTC+09:00)**, ISO8601 형식
- **환율 소수점**: 2자리 (예: `1407.50`)
- **타임스탬프 예시**: `"2025-12-05T14:30:00+09:00"`

### 통화별 표시 규칙

| 통화 | API 반환 단위 | 표시 형식 | 예시 |
|------|-------------|----------|------|
| **USD/KRW** | 1달러당 | 그대로 표시 | `1,407.50원` |
| **EUR/KRW** | 1유로당 | 그대로 표시 | `1,520.30원` |
| **JPY/KRW** | **100엔당** | 그대로 표시 + 라벨 | `949.50원 (100엔)` |

> **중요**: 백엔드 API는 JPY를 **이미 100엔당 환율**로 반환합니다 (예: `949.50`).
> 클라이언트에서 추가 스케일링(×100) 불필요. 라벨 "(100엔)"만 추가하면 됩니다.

```swift
/// 환율 포맷팅 (JPY는 라벨만 추가, 스케일링 불필요)
func formatRate(rate: Double, currency: String) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .decimal
    formatter.minimumFractionDigits = 2
    formatter.maximumFractionDigits = 2

    let formatted = formatter.string(from: NSNumber(value: rate)) ?? "\(rate)"

    if currency == "jpy-krw" {
        return "\(formatted)원 (100엔)"  // API가 이미 100엔당으로 반환
    }
    return "\(formatted)원"
}
```

### REST API

#### 전체 환율 조회

```http
GET /api/rates
```

**Response:**
```json
{
  "rates": [
    {
      "currency": "usd-krw",
      "bank": "investing",
      "rate": 1407.50,
      "timestamp": "2025-12-05T14:30:00+09:00"
    },
    {
      "currency": "usd-krw",
      "bank": "kb",
      "rate": 1409.20,
      "timestamp": "2025-12-05T14:28:00+09:00"
    }
    // ... 총 30개 (10개 은행 × 3개 통화)
  ],
  "metadata": {
    "updated_at": "2025-12-05T14:30:00+09:00",
    "currencies": ["usd-krw", "jpy-krw", "eur-krw"],
    "banks": ["investing", "kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"],
    "total_count": 30
  }
}
```

#### 그래프 데이터 조회 (24시간)

```http
GET /api/graph/{currency}
```

**Parameters:**
- `currency`: `usd-krw`, `jpy-krw`, `eur-krw`

**Response:**
```json
{
  "pair": "usd-krw",
  "sources": {
    "investing": [[1733380800, 1407.8, 1407.2, 1407.5], ...],
    "kb": [[1733380800, 1409.5, 1409.0, 1409.2], ...],
    "hana": [[1733380800, 1409.0, 1408.5, 1408.8], ...]
  },
  "as_of": "2025-12-05T14:30:00+09:00"
}
```

**그래프 데이터 형식 (REST):**
- 배열: `[timestamp, max, min, close]`
- timestamp: Unix timestamp (초 단위)
- 10분 버킷 단위 집계 (24시간 = 144개 버킷)
- **소스**: investing, kb, hana **3개만** 반환 (환율 비교는 10개 은행 전체)
- **Carry-forward**: 데이터 없는 버킷은 직전 close 값으로 채움 (그래프 연속성 보장)

> ⚠️ **주의**: REST API는 **배열** 형식, WebSocket `graph_buckets`는 **객체** 형식으로 스키마가 다름

#### API 스키마 명세 (Open Questions 해소)

| 항목 | 값 | 비고 |
|------|---|------|
| **JPY 반환 단위** | 100엔당 (예: `949.50`) | 클라이언트 스케일링 불필요 |
| **graph_buckets 소스** | `investing`, `kb`, `hana` (3개) | REST `/api/graph`와 동일 |
| **그래프 데이터 형식** | REST: 배열 `[ts, max, min, close]` | WebSocket: 객체 `{bucket_ts, max, min, close}` |

### WebSocket

#### 연결

```
wss://fxi.n-e.kr/ws
```

#### 메시지 타입

**수신 - 환율 데이터:**
```json
{
  "type": "rates",
  "data": {
    "rates": [...],
    "metadata": {...}
  },
  "graph_buckets": {
    "usd-krw": {
      "investing": {"bucket_ts": 1733380800, "max": 1407.8, "min": 1407.2, "close": 1407.5},
      "kb": {...},
      "hana": {...}
    }
  }
}
```

> **bucket_ts**: Unix epoch 초 단위, KST 10분 경계 (예: 14:30:00 → 1733380800)

**수신 - Pong:**
```json
{
  "type": "pong"
}
```

**송신 - Ping:**
```
ping
```
> **참고**: Plain text 문자열 `ping` (JSON 아님)

#### 연결 관리

- **Ping 주기**: 30초마다 `ping` 전송
- **재연결**: 연결 끊김 시 2초 × 시도횟수 딜레이 후 재연결 (최대 5회)
- **5회 실패 후**: 에러 UI 표시 + 수동 "재연결" 버튼 제공
- **백그라운드 복귀**: 앱 foreground 전환 시 즉시 재연결

---

## 🎨 UI/UX 디자인

### 색상 팔레트 (Dark Theme)

```swift
// 배경
let background = Color(hex: "#121212")
let cardBackground = Color(hex: "#1e1e1e")
let inputBackground = Color(hex: "#2c2c2c")

// 텍스트
let primaryText = Color(hex: "#e0e0e0")
let secondaryText = Color(hex: "#95a5a6")

// 상태
let positive = Color(hex: "#e74c3c")  // 환율 상승 (빨강)
let negative = Color(hex: "#29b44a")  // 환율 하락 (초록)
let neutral = Color(hex: "#bdc3c7")

// 연결 상태
let statusOnline = Color(hex: "#2ecc71")
let statusConnecting = Color(hex: "#f39c12")
let statusError = Color(hex: "#e74c3c")
```

### 은행별 브랜드 색상

```swift
let bankColors: [String: Color] = [
    "investing": Color(hex: "#2c3e50"),
    "kb": Color(hex: "#ffb200"),
    "hana": Color(hex: "#009792"),
    "shinhan": Color(hex: "#0052ff"),
    "woori": Color(hex: "#0089d4"),
    "ibk": Color(hex: "#0049a0"),
    "nh": Color(hex: "#00ac41"),
    "sc": Color(hex: "#0075f2"),
    "bs": Color(hex: "#dd1a25"),
    "citi": Color(hex: "#006eb3")
]
```

### 은행 표시 이름

```swift
let bankNames: [String: String] = [
    "investing": "인베스팅",
    "kb": "국민은행",
    "hana": "하나은행",
    "shinhan": "신한은행",
    "woori": "우리은행",
    "ibk": "기업은행",
    "nh": "농협은행",
    "sc": "SC제일",
    "bs": "부산은행",
    "citi": "씨티은행"
]
```

### 바 차트 디자인

```
┌──────────────────────────────────────────────────────────┐
│ [🏦 아이콘] ████████████████████  1,409.20  +1.70  14:28 │
└──────────────────────────────────────────────────────────┘

- 바 너비: 환율 값에 비례 (min~max 범위 기준)
- 기준 은행: 기본값 "investing" (인베스팅), 파란 테두리 + "기준" 라벨
- 차이값: 기준 대비 +/- 표시 (빨강/초록)
- 타임스탬프: MM-dd HH:mm
```

### 색상 체계 설명

```
빨강 (#e74c3c): 기준보다 비쌈 (사용자 불리) → 경고색
초록 (#29b44a): 기준보다 쌈 (사용자 유리) → 긍정색

※ 한국 금융 관례 (주식: 빨강=상승) + 사용자 관점 UX 반영
```

### UX 상태 정의

앱에서 처리해야 할 3가지 주요 상태:

| 상태 | 조건 | UI 표현 | 메시지 예시 |
|------|------|---------|------------|
| **로딩** | 초기 데이터 로드 중 | ProgressView + 텍스트 | "환율 정보를 불러오는 중..." |
| **에러** | API 호출 실패 또는 5회 재연결 실패 | 에러 아이콘 + 메시지 + 버튼 | "연결할 수 없습니다" / "다시 시도" |
| **오프라인** | 네트워크 연결 없음 | 상단 배너 + 캐시 데이터 | "오프라인 모드 · 마지막 업데이트: 14:30" |

```swift
enum AppState {
    case loading
    case connected(rates: [ExchangeRate])  // rates만 포함, 그래프는 GraphViewModel에서 별도 관리
    case error(message: String)
    case offline(cachedRates: [ExchangeRate]?)
}
```

### 오프라인 캐시 전략

#### 캐시 데이터 구조

```text
┌─────────────────────────────────────────────────────────────────┐
│ 메모리 캐시 (GraphViewModel)                                     │
│   [currency: [source: [GraphBucket]]]                           │
│   예: ["usd-krw": ["investing": [bucket1, bucket2, ...144개]]]  │
├─────────────────────────────────────────────────────────────────┤
│ 디스크 캐시 (CacheService) - 통화별 파일                          │
│   graph_cache_usd-krw.json → [source: [GraphBucket]]            │
│   graph_cache_jpy-krw.json → [source: [GraphBucket]]            │
│   graph_cache_eur-krw.json → [source: [GraphBucket]]            │
└─────────────────────────────────────────────────────────────────┘

변환 책임:
- 저장: GraphViewModel이 currency별로 분리 → CacheService.saveGraphData
- 로드: CacheService.loadGraphData → GraphViewModel이 메모리 캐시에 병합
```

```swift
/// 캐시 저장소: UserDefaults (환율) + FileManager (그래프, 통화별 파일)
class CacheService {
    static let shared = CacheService()

    private let ratesKey = "cached_rates"
    private let fileManager = FileManager.default

    // MARK: - 환율 캐시 (UserDefaults, ~5KB)

    func saveRates(_ rates: [ExchangeRate]) {
        if let data = try? JSONEncoder().encode(rates) {
            UserDefaults.standard.set(data, forKey: ratesKey)
        }
    }

    func loadCachedRates() -> [ExchangeRate]? {
        guard let data = UserDefaults.standard.data(forKey: ratesKey),
              let rates = try? JSONDecoder().decode([ExchangeRate].self, from: data)
        else { return nil }
        return rates
    }

    // MARK: - 그래프 캐시 (FileManager, 통화별 ~50KB)
    // 디스크 형식: [source: [GraphBucket]] (통화별 파일)

    func saveGraphData(_ data: [String: [GraphBucket]], currency: String) {
        let url = graphCacheURL(for: currency)
        if let encoded = try? JSONEncoder().encode(data) {
            try? encoded.write(to: url)
        }
    }

    func loadGraphData(currency: String) -> [String: [GraphBucket]]? {
        let url = graphCacheURL(for: currency)

        // TTL 체크 (24시간)
        if let attrs = try? fileManager.attributesOfItem(atPath: url.path),
           let modDate = attrs[.modificationDate] as? Date,
           Date().timeIntervalSince(modDate) > 86400 {
            return nil  // 캐시 만료
        }

        guard let data = try? Data(contentsOf: url),
              let graph = try? JSONDecoder().decode([String: [GraphBucket]].self, from: data)
        else { return nil }
        return graph
    }

    /// 메모리 캐시 전체를 디스크에 저장 (앱 백그라운드 진입 시 호출)
    func saveAllGraphData(_ memoryCache: [String: [String: [GraphBucket]]]) {
        for (currency, sources) in memoryCache {
            saveGraphData(sources, currency: currency)
        }
    }

    /// 디스크에서 전체 그래프 캐시 로드 (앱 시작 시 호출)
    func loadAllGraphData() -> [String: [String: [GraphBucket]]] {
        var result: [String: [String: [GraphBucket]]] = [:]
        for currency in SupportedCurrency.allCases {
            if let data = loadGraphData(currency: currency.rawValue) {
                result[currency.rawValue] = data
            }
        }
        return result
    }

    private func graphCacheURL(for currency: String) -> URL {
        let docs = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appendingPathComponent("graph_cache_\(currency).json")
    }
}
```

> **오프라인 동작**:
>
> 1. 앱 시작 시 캐시 데이터 먼저 표시
> 2. 네트워크 연결되면 최신 데이터로 교체
> 3. 연결 실패 시 캐시 유지 + 상단 배너 표시

---

## 🏗️ 권장 아키텍처

### MVVM + Combine

```
├── Models/
│   ├── ExchangeRate.swift       # 환율 데이터 모델
│   ├── GraphData.swift          # 그래프 데이터 모델
│   └── WebSocketMessage.swift   # WebSocket 메시지 모델
│
├── Services/
│   ├── APIService.swift         # REST API 호출
│   ├── WebSocketService.swift   # WebSocket 연결 관리
│   └── GraphCacheService.swift  # 그래프 데이터 캐싱
│
├── ViewModels/
│   ├── ExchangeRateViewModel.swift  # 환율 상태 관리
│   └── GraphViewModel.swift         # 그래프 상태 관리
│
├── Views/
│   ├── ContentView.swift        # 메인 TabView
│   ├── CurrencyTabView.swift    # 각 통화 탭 (재사용)
│   ├── RateGraphView.swift      # 24시간 그래프
│   ├── RateBarView.swift        # 환율 바 차트 행
│   └── ConnectionStatusView.swift  # 연결 상태 표시
│
└── Utils/
    ├── Color+Hex.swift          # 헥스 색상 변환
    └── Date+Formatter.swift     # 날짜 포맷
```

### 공통 상수

```swift
// Constants.swift
/// 지원 통화 목록 (통화 추가/삭제 시 여기만 수정)
enum SupportedCurrency: String, CaseIterable {
    case usdKrw = "usd-krw"
    case jpyKrw = "jpy-krw"
    case eurKrw = "eur-krw"

    var displayName: String {
        switch self {
        case .usdKrw: return "USD/KRW"
        case .jpyKrw: return "JPY/KRW"
        case .eurKrw: return "EUR/KRW"
        }
    }
}

/// 그래프 소스 (백엔드와 동일)
enum GraphSource: String, CaseIterable {
    case investing, kb, hana
}
```

> **사용 예시**: `SupportedCurrency.allCases.map { $0.rawValue }` → `["usd-krw", "jpy-krw", "eur-krw"]`

### 핵심 모델

```swift
// ExchangeRate.swift
struct ExchangeRate: Codable, Identifiable {
    let currency: String
    let bank: String
    let rate: Double
    let timestamp: Date

    var id: String { "\(currency)-\(bank)" }
}

struct ExchangeRateResponse: Codable {
    let rates: [ExchangeRate]
    let metadata: Metadata

    struct Metadata: Codable {
        let updatedAt: Date
        let currencies: [String]
        let banks: [String]
        let totalCount: Int

        enum CodingKeys: String, CodingKey {
            case updatedAt = "updated_at"
            case currencies, banks
            case totalCount = "total_count"
        }
    }
}

// GraphData.swift
/// 그래프 버킷 데이터 (10분 단위 집계)
struct GraphBucket: Codable {
    let bucketTs: Int      // Unix timestamp (초)
    let max: Double
    let min: Double
    let close: Double

    enum CodingKeys: String, CodingKey {
        case bucketTs = "bucket_ts"
        case max, min, close
    }

    /// REST API 배열 형식 [timestamp, max, min, close]에서 변환
    /// - Returns: nil if array length != 4 (방어적 처리)
    init?(fromArray arr: [Double]) {
        guard arr.count == 4 else {
            // 배열 길이 불일치 시 nil 반환 (런타임 크래시 방지)
            return nil
        }
        self.bucketTs = Int(arr[0])
        self.max = arr[1]
        self.min = arr[2]
        self.close = arr[3]
    }

    /// WebSocket 객체 형식에서 변환 (Codable 자동 처리)
    init(bucketTs: Int, max: Double, min: Double, close: Double) {
        self.bucketTs = bucketTs
        self.max = max
        self.min = min
        self.close = close
    }
}

/// REST API 그래프 응답 (/api/graph/{currency})
struct GraphResponse: Codable {
    let pair: String
    let sources: [String: [[Double]]]  // 배열 형식
    let asOf: Date

    enum CodingKeys: String, CodingKey {
        case pair, sources
        case asOf = "as_of"
    }

    /// 배열 형식을 GraphBucket으로 변환 (잘못된 배열은 무시)
    func toBuckets() -> [String: [GraphBucket]] {
        sources.mapValues { arrays in
            arrays.compactMap { GraphBucket(fromArray: $0) }  // nil 제거
        }
    }
}

/// WebSocket graph_buckets 타입 정의
/// 구조: [currency: [source: GraphBucket]]
/// 예: ["usd-krw": ["investing": GraphBucket, "kb": GraphBucket, "hana": GraphBucket]]
typealias WebSocketGraphBuckets = [String: [String: GraphBucket]]

/// WebSocket 메시지 타입 (오타 방지용 enum)
enum MessageType: String, Codable {
    case rates
    case pong
}

/// WebSocket 메시지 전체 구조
struct WebSocketMessage: Codable {
    let type: MessageType
    let data: ExchangeRateResponse?
    let graphBuckets: WebSocketGraphBuckets?

    enum CodingKeys: String, CodingKey {
        case type, data
        case graphBuckets = "graph_buckets"
    }

    /// 그래프 캐시 업데이트 (마지막 버킷만 교체/추가)
    ///
    /// 규칙:
    /// - 같은 bucketTs면 교체, 새 버킷이면 추가
    /// - 144개 초과 시 오래된 버킷 제거 (24시간 = 144 버킷)
    /// - bucketTs 기준 정렬 유지 (지연 도착 대비)
    func updateGraphCache(_ cache: inout [String: [String: [GraphBucket]]]) {
        guard let buckets = graphBuckets else { return }
        let maxBuckets = 144  // 24시간 × 6 (10분 버킷)

        for (currency, sources) in buckets {
            for (source, bucket) in sources {
                if cache[currency] == nil {
                    cache[currency] = [:]
                }
                if cache[currency]?[source] == nil {
                    cache[currency]?[source] = []
                }

                // 같은 버킷이면 교체, 새 버킷이면 정렬 위치에 삽입
                if let existingIndex = cache[currency]?[source]?.firstIndex(where: { $0.bucketTs == bucket.bucketTs }) {
                    cache[currency]?[source]?[existingIndex] = bucket
                } else {
                    // 이진 탐색으로 삽입 위치 찾기 (O(log n), 정렬 유지)
                    let insertIndex = cache[currency]?[source]?.firstIndex { $0.bucketTs > bucket.bucketTs }
                        ?? cache[currency]?[source]?.count ?? 0
                    cache[currency]?[source]?.insert(bucket, at: insertIndex)
                }

                // 144개 초과 시 오래된 버킷 제거
                if let count = cache[currency]?[source]?.count, count > maxBuckets {
                    cache[currency]?[source]?.removeFirst(count - maxBuckets)
                }
            }
        }
    }
}
```

---

## ✅ 구현 체크리스트

### Phase 1-1: 기본 구조 설정

- [ ] Xcode 프로젝트 생성 (SwiftUI, iOS 17+)
- [ ] 폴더 구조 생성 (Models, Services, ViewModels, Views, Utils)
- [ ] 색상 팔레트 정의 (Color+Hex.swift)
- [ ] 은행 상수 정의 (bankColors, bankNames)

### Phase 1-2: 데이터 모델 & API

- [ ] ExchangeRate 모델 정의
- [ ] GraphData 모델 정의
- [ ] APIService 구현 (REST API 호출)
- [ ] JSON 디코딩 테스트

### Phase 1-3: WebSocket 연결

- [ ] WebSocketService 구현
- [ ] 연결/재연결 로직
- [ ] Ping/Pong 하트비트
- [ ] 백그라운드/포그라운드 전환 처리

### Phase 1-4: UI 구현

- [ ] ContentView (TabView 3개 탭)
- [ ] CurrencyTabView (통화별 화면)
- [ ] RateBarView (은행별 바 차트)
- [ ] ConnectionStatusView (연결 상태)

### Phase 1-5: 그래프

- [ ] Swift Charts 통합
- [ ] RateGraphView 구현
- [ ] 그래프 데이터 캐싱
- [ ] WebSocket으로 실시간 업데이트

### Phase 1-6: 마무리

- [ ] 로딩 상태 처리
- [ ] 에러 처리 UI
- [ ] 앱 아이콘 설정
- [ ] 다크 모드 최적화

---

## 💡 구현 팁

### 1. WebSocket 재연결

```swift
// 앱이 백그라운드에서 돌아올 때
NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)
    .sink { _ in
        webSocketService.reconnect()
    }
```

### 2. 그래프 데이터 캐싱

```swift
// 캐싱 규칙:
// - 메모리 캐시: 3개 통화 × 3개 소스(investing, kb, hana) × 144개 버킷 (24시간)
// - 갭 감지: 마지막 버킷 후 15분(900초) 이상 경과 시 → REST API 풀 리프레시
// - 실시간 업데이트: WebSocket graph_buckets로 마지막 버킷만 업데이트
// - 실패 시: 캐시 데이터 유지 + 에러 표시 + 재시도 버튼
//
// 소스 선택 이유:
// - investing: 기준 환율, 24시간 운영
// - kb: 국내 최대 은행, 높은 신뢰도
// - hana: 빈번한 환율 고시, 장시간 운영
//
// 차트 모드 전환:
// - 1개 소스 선택: Band Chart (close 라인 + max/min 밴드)
// - 2개 이상 선택: Line Chart (각 소스별 close 비교)
```

### 3. 바 너비 계산

```swift
func calculateBarWidth(rate: Double, minRate: Double, maxRate: Double) -> CGFloat {
    let range = maxRate - minRate
    if range == 0 { return 0.65 }

    let normalized = (rate - minRate) / range
    return 0.3 + normalized * 0.4  // 30%~70% 범위
}
```

### 4. 환율 차이 색상

```swift
func diffColor(diff: Double) -> Color {
    if diff > 0 { return .positive }  // 빨강 (비싸다)
    if diff < 0 { return .negative }  // 초록 (싸다)
    return .neutral
}
```

### 5. JSON 디코딩 설정

```swift
// APIService, WebSocketService 공통
let decoder = JSONDecoder()
decoder.dateDecodingStrategy = .iso8601  // "2025-12-05T14:30:00+09:00" 파싱
```

### 6. 네트워크 상태 감지

```swift
import Network

// NWPathMonitor 사용 (Network.framework)
let monitor = NWPathMonitor()
monitor.pathUpdateHandler = { path in
    if path.status == .satisfied {
        // 온라인 → WebSocket 재연결
    } else {
        // 오프라인 → AppState.offline 전환, 캐시 데이터 표시
    }
}
monitor.start(queue: DispatchQueue.global())
```

### 7. 그래프 갭 감지 (GraphViewModel)

```swift
// WebSocket 메시지 수신 시 갭 감지 로직:
func handleGraphBucket(_ bucket: GraphBucket, for currency: String) {
    // 캐시된 마지막 버킷과 새 버킷의 시간 차이 확인
    if let lastBucket = graphCache[currency]?[GraphSource.investing.rawValue]?.last {
        let gap = bucket.bucketTs - lastBucket.bucketTs
        if gap > 900 {  // 15분(900초) 이상 갭
            // REST API 풀 리프레시 (/api/graph/{currency})
            Task { await fetchFullGraphData(currency: currency) }
            return
        }
    } else {
        // 캐시 비어있음 → REST 풀 리프레시
        Task { await fetchFullGraphData(currency: currency) }
        return
    }

    // 정상 범위 → WebSocket 버킷만 업데이트
    updateGraphCache(bucket, for: currency)
}
```

---

## 🚀 시작하기

1. **Xcode 프로젝트 생성**
   - Product Name: `FXi`
   - Interface: SwiftUI
   - Language: Swift
   - Minimum Deployments: iOS 17.0

2. **기본 구조 설정**
   - 위의 폴더 구조대로 그룹 생성
   - Color+Hex.swift 유틸리티 추가

3. **API 연결 테스트**
   - `https://fxi.n-e.kr/api/rates` 호출 확인
   - JSON 디코딩 테스트

4. **점진적 구현**
   - REST API로 먼저 데이터 표시
   - WebSocket 실시간 업데이트 추가
   - 그래프 기능 마지막에 추가

---

## 📝 참고 자료

- **백엔드 API**: `https://fxi.n-e.kr`
- **웹 프론트엔드**: `/templates/index.html` (UI 참고)
- **은행 아이콘**: `/static/` 폴더 (PNG 파일)

### 백엔드 코드 참조 (선택)

> API 동작이 불명확할 때 백엔드 소스를 직접 참조할 수 있습니다.
> 경로: `../exchange-rate/` (같은 FXi 프로젝트 내)

| 참조 목적 | 파일 | 주요 내용 |
|----------|------|----------|
| **WebSocket 브로드캐스트** | `app/main.py` | `broadcast_rates_once()`, `build_graph_buckets()` |
| **REST API 엔드포인트** | `app/main.py` | `/api/rates`, `/api/graph/{currency}` |
| **그래프 캐시 로직** | `app/admin/graph_cache.py` | 10분 버킷 집계, carry-forward |
| **DB 스키마** | `app/models.py` | `ExchangeRate`, `InvestingRate` 테이블 |
| **크롤러 구현** | `app/crawlers/*.py` | 각 은행별 크롤링 로직 |

```bash
# 백엔드 프로젝트 구조 확인
ls ../exchange-rate/app/
```

> ⚠️ **주의**: 상대 경로는 로컬 개발 환경 기준입니다. CI/CD 환경에서는 무효할 수 있습니다.

---

## 🔮 Phase 2 TODO (향후 구현)

> 아래 항목은 MVP 이후 Phase 2에서 구현 예정입니다.

### 에러 타입 세분화

```swift
// Phase 2: 에러 타입별 다른 안내 메시지
enum AppError: Error {
    case network(underlying: Error)     // "네트워크 연결을 확인해주세요"
    case server(statusCode: Int)        // "서버에 문제가 발생했습니다 (500)"
    case parsing(description: String)   // "데이터 형식이 올바르지 않습니다"
    case timeout                        // "응답 시간이 초과되었습니다"
    case websocketDisconnected          // "실시간 연결이 끊어졌습니다"

    var userMessage: String {
        switch self {
        case .network: return "네트워크 연결을 확인해주세요"
        case .server(let code): return "서버 오류가 발생했습니다 (\(code))"
        case .parsing: return "데이터를 처리할 수 없습니다"
        case .timeout: return "응답 시간이 초과되었습니다"
        case .websocketDisconnected: return "실시간 연결이 끊어졌습니다"
        }
    }
}
```

### 접근성 (Accessibility)

- [ ] VoiceOver 레이블 추가 (환율 값, 은행명, 변동폭)
- [ ] Dynamic Type 지원 (텍스트 크기 조절)
- [ ] 색상 대비 개선 (색맹 사용자 고려)
- [ ] Reduce Motion 설정 존중

### 환율 변동 알림 (Push Notification)

```swift
// Phase 2: 알림 임계값 기준
struct NotificationThreshold {
    let currency: String
    let bank: String
    let condition: ThresholdCondition
    let value: Double  // 예: 1% 변동, 1400원 이하 등

    enum ThresholdCondition {
        case percentChange(Double)     // 예: 1.0 = 1% 변동
        case absoluteBelow(Double)     // 예: 1400.0원 이하
        case absoluteAbove(Double)     // 예: 1450.0원 이상
    }
}
```

---

**작성일**: 2025-12-05
**최종 수정**: 2025-12-06
**Phase**: 1 - iOS MVP
