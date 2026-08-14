# 환율 알림 & 구독 관리 구현 가이드

> **목적**: 환율 알림 서비스와 크로스 플랫폼 구독 관리 구현을 위한 기술 선택지 비교
> **작성일**: 2025-12-03
> **상태**: ✅ **결정됨** (Phase 2: 2025-12-05, Phase 3: 2025-12-23)

---

## ✅ 현재 운영 상태 (2026-01-13)

- **EC2**: t3.small (2 vCPU, 2GB RAM)
- **DB**: RDS PostgreSQL (db.t4g.micro, Free Tier)
- **Redis**: EC2 Docker
- **도메인/SSL**: fxi.kr + Let's Encrypt (HTTPS/WSS 정상)
- **저장 표준**: UTC 저장, API는 KST(+09:00) 출력

## 📋 목차

1. [결정된 아키텍처](#-결정된-아키텍처)
2. [구현 로드맵](#️-구현-로드맵)
3. [Phase 3 상세 계획: 구독 시스템](#-phase-3-상세-계획-구독-시스템-구현)
4. [개인화 알림 동작 흐름](#-개인화-알림-동작-흐름)
5. [환율 알림 서비스 구현 방법](#1-환율-알림-서비스-구현-방법)
6. [크로스 플랫폼 구독 관리 방법](#2-크로스-플랫폼-구독-관리-방법)
7. [비용 비교 시나리오](#3-비용-비교-시나리오)
8. [구현 난이도 및 기간](#4-구현-난이도-및-기간)
9. [다음 단계](#5-다음-단계)

---

## 🎯 결정된 아키텍처

### 최종 선택: AWS RDS PostgreSQL + Firebase Auth + FCM + RevenueCat

```text
┌─────────────────────────────────────────────────────┐
│ AWS Cloud (서울 리전)                                │
│                                                     │
│  ┌──────────────────────────────────────────────┐  │
│  │ VPC (같은 네트워크, 지연 1-3ms)                 │  │
│  │                                              │  │
│  │  ┌─────────────────┐    ┌──────────────────┐ │  │
│  │  │ EC2 t3.small    │ ←→ │ RDS PostgreSQL   │ │  │
│  │  │                 │    │ db.t4g.micro     │ │  │
│  │  │ ├── FastAPI     │    │                  │ │  │
│  │  │ ├── 크롤러 (10개)│    │ ├── 환율 데이터   │ │  │
│  │  │ ├── WebSocket   │    │ ├── user_devices │ │  │
│  │  │ └── Redis       │    │ └── notifications│ │  │
│  │  └─────────────────┘    └──────────────────┘ │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
              ↓↑ (외부 서비스)
┌─────────────────────────────────────────────────────┐
│ Firebase (Google Cloud)                             │
│ ├── Firebase Auth (로그인) - 무료 50k MAU           │
│ └── FCM (푸시 알림) - 무료 무제한                   │
└─────────────────────────────────────────────────────┘
              ↓↑ (구독 관리)
┌─────────────────────────────────────────────────────┐
│ RevenueCat                                          │
│ ├── iOS/Android 구독 통합 관리                      │
│ ├── 영수증 검증 + 크로스플랫폼 동기화               │
│ └── 무료 $2,500 MTR까지                            │
└─────────────────────────────────────────────────────┘
```

### 선택 이유

| 항목 | 선택 | 이유 |
|------|------|------|
| **DB** | AWS RDS PostgreSQL | 프리티어 12개월, 같은 VPC 지연 1-3ms |
| **Auth** | Firebase Auth | FCM과 자연스러운 통합, 무료 |
| **Push** | Firebase FCM | 무료 무제한, iOS/Android 통합 |
| **구독** | RevenueCat | 영수증 검증 자동화, 크로스플랫폼 동기화 |
| **인스턴스** | db.t4g.micro | ARM64, 20% 저렴 (~$15/월) |

### 비용 요약

| Phase | 기간 | EC2 | RDS | Auth/FCM | 합계 |
|-------|------|-----|-----|----------|------|
| **1** | 서비스 시작 ~ 12개월 | $15/월 | $0 (프리티어) | $0 | **$15/월** |
| **2** | 12개월 이후 | $15/월 | ~$15/월 | $0 | **$30/월** |
| **3** | 1,000명+ | $30/월 (t3.medium) | ~$25/월 | $0 | **$55/월** |

---

## 🗺️ 구현 로드맵

### Phase 1: iOS MVP 개발

**목표**: 기본 앱 완성 (알림 기능 제외)

| 작업 | 설명 |
|------|------|
| UI/UX 구현 | 환율 비교 화면, 그래프, 은행 목록 |
| API 연동 | RDS PostgreSQL 기반 REST API 사용 |
| WebSocket | 실시간 환율 업데이트 |

**인프라**: t3.small + RDS PostgreSQL (운영 기준)

---

### Phase 2: Firebase Auth + FCM 통합

**목표**: 사용자 인증 + 푸시 알림 기반 구축

| 작업 | 설명 |
|------|------|
| Firebase 프로젝트 생성 | FXi 전용 새 프로젝트 |
| APNs 키 생성 | `.p8` 키 생성 (Sandbox & Production, Team Scoped) |
| Firebase에 iOS 앱 등록 | Bundle ID 등록, `GoogleService-Info.plist` 다운로드 |
| APNs 키 Firebase 등록 | 개발/프로덕션 APNs 인증 키 업로드 |
| Google 로그인 설정 | 프로젝트 공개용 이름, 지원 이메일 설정 |
| Apple 로그인 설정 | App ID, 서비스 ID, Sign in with Apple 키 생성 |
| Firebase OAuth 설정 | 서비스 ID, 팀 ID, 비공개 키 등록 (Android용) |
| iOS Firebase Auth SDK | 로그인/회원가입 구현 |
| iOS FCM SDK | Device Token 등록 |
| 서버 API 구현 | `/api/register-device`, `/api/notification-settings` |
| Alembic 설정 | DB 마이그레이션 스크립트 준비 |

**인프라**: t3.small + RDS PostgreSQL (운영 기준)

#### 🔑 생성되는 키/파일 목록

| 파일 | 용도 | 비고 |
|------|------|------|
| `AuthKey_XXXXXXXX.p8` (APNs) | 푸시 알림 | Firebase Cloud Messaging에 등록 |
| `AuthKey_YYYYYYYY.p8` (Sign in with Apple) | Apple 로그인 | Firebase OAuth에 등록 (Android용) |
| `GoogleService-Info.plist` | iOS Firebase 설정 | Google 로그인 활성화 후 재다운로드 필요 |

#### 📝 Apple Developer 설정 상세

##### 1. APNs 키 생성 (Keys)

- Key Name: 범용적 이름 권장 (예: `APNs Auth Key`)
- Environment: `Sandbox & Production` (개발+프로덕션 모두 지원)
- Key Restriction: `Team Scoped (All Topics)` (모든 앱에서 재사용 가능)

##### 2. App ID 설정 (Identifiers > App IDs)

- Bundle ID와 동일하게 등록
- Capabilities: `Push Notifications`, `Sign in with Apple` 활성화
- Sign in with Apple: `Enable as a primary App ID` 선택

##### 3. 서비스 ID 생성 (Identifiers > Services IDs) - Android Apple 로그인용

- Identifier: `{Bundle ID}.signin` 형식 권장
- Sign in with Apple 활성화 후 Configure:
  - Primary App ID: 위에서 만든 App ID 선택
  - Domains: `{project-id}.firebaseapp.com`
  - Return URLs: `https://{project-id}.firebaseapp.com/__/auth/handler`

##### 4. Sign in with Apple 키 생성 (Keys) - APNs 키와 별도

- Key Name: `Sign in with Apple Key`
- Sign in with Apple 체크 → Configure → Primary App ID 선택

#### 🔥 Firebase Console 설정 상세

##### 1. iOS 앱 등록

- Bundle ID 입력 → `GoogleService-Info.plist` 다운로드

##### 2. APNs 키 업로드 (프로젝트 설정 > Cloud Messaging)

- 개발/프로덕션 APNs 인증 키에 동일한 `.p8` 파일 업로드
- Key ID, Team ID 입력

##### 3. Google 로그인 활성화 (Authentication > Sign-in method)

- 프로젝트 공개용 이름: 앱 이름 (예: `환율아이`)
- 프로젝트 지원 이메일: 운영자 이메일
- ⚠️ 활성화 후 `GoogleService-Info.plist` **재다운로드 필요**

##### 4. Apple 로그인 활성화 (Authentication > Sign-in method)

- iOS 네이티브: 단순 활성화만 하면 됨
- Android용 OAuth 설정 (크로스 플랫폼 구독 동기화 필요 시):
  - 서비스 ID: `{Bundle ID}.signin`
  - Apple 팀 ID: Apple Developer 계정의 Team ID
  - 키 ID: Sign in with Apple 키의 Key ID
  - 비공개 키: `.p8` 파일 내용 붙여넣기

---

### Phase 3: RevenueCat 통합

**목표**: 구독 결제 시스템 구축

| 작업 | 설명 |
|------|------|
| RevenueCat 계정 생성 | 앱 등록, 상품 설정 |
| iOS SDK 통합 | 구독 구매 플로우 |
| 유료 기능 설계 | 프리미엄 알림 (다중 조건, 빈도 등) |
| Firebase ↔ RevenueCat 연동 | uid 기반 사용자 식별 |

**비용**: $2,500 MTR까지 무료

---

## 💳 Phase 3 상세 계획: 구독 시스템 구현

> **결정일**: 2025-12-23
> **상태**: ✅ Phase 3 구현 완료
> **설정 완료일**: 2025-12-25
> **iOS 구현 완료**: 2025-12-27
> **서버 구현 완료**: 2025-12-27

### 1. 가격 구조 (확정)

| 플랜 | 가격 | 무료 체험 | 전략적 의도 |
|------|------|----------|-------------|
| **월간** | 12,000원 | ❌ 없음 | 확신 있는 사용자, 즉시 결제 |
| **연간** | 99,000원 (월 8,250원) | ✅ 7일 | 체험 후 결정, 31% 할인 |

> **⚠️ 7일 무료 체험 자격 제한 (Apple Introductory Offers)**:
> - 구독 그룹당 **1회만** 제공 (이전 구독자, 해지 후 재가입 시 제외)
> - 가족 공유 멤버 중 이미 체험한 사람이 있으면 제외될 수 있음
> - 자격 여부는 `Package.storeProduct.introductoryDiscount`로 확인

**가격 결정 근거:**

1. **카카오톡 환율 알림 서비스 벤치마크**: 월 10,000원에 100~200명 유료 사용자 운영 중
2. **FXi 기능 우위**: 9개 은행 + 3개 통화, 실시간 비교 UI, 24시간 그래프, 맞춤 알림
3. **경쟁 없음**: 앱스토어에 유사한 실시간 환율 비교 앱 부재 (블루오션)
4. **타깃 사용자**: 환전 투자자 - 가치 느끼면 "싸다"고 인식

**구조의 전략적 의도:**

```
사용자 A (확신 있음, 급함)
→ "체험 필요 없어, 바로 쓸래" → 월간 12,000원

사용자 B (가치 확인 필요)
→ "써봐야 알겠어" → 연간 7일 체험 → 해지 안 함 → 99,000원 (자동 결제)

사용자 C (가격 민감)
→ "월간은 체험도 없고..." → 연간 선택 → 월 8,250원으로 더 저렴

결과: 대부분 연간으로 유도 → 안정적 수익
```

**예상 구독 비율:**

| 시나리오 | 월간 | 연간 |
|----------|------|------|
| 보수적 | 30% | 70% |
| 예상 | 20% | 80% |
| 낙관적 | 10% | 90% |

### 2. 매출 시뮬레이션

**초기 목표**: 100~200명 유료 구독자

| 구독자 수 | 월간 20% | 연간 80% | 월 매출 | 연 매출 |
|-----------|----------|----------|---------|---------|
| 100명 | 20명 × 12,000 | 80명 × 8,250 | 약 90만원 | 약 1,080만원 |
| 150명 | 30명 × 12,000 | 120명 × 8,250 | 약 135만원 | 약 1,620만원 |
| 200명 | 40명 × 12,000 | 160명 × 8,250 | 약 180만원 | 약 2,160만원 |

> **참고**: 연간 구독은 월 환산 금액(8,250원)으로 계산. 실제로는 99,000원 일시불 결제.

### 3. App Store Connect 상품 설정

#### 3.1 구독 그룹 생성

```
App Store Connect → 앱 → 구독 → 구독 그룹 생성

그룹 이름: "FXi Premium"
참조 이름: fxi_premium_group
```

#### 3.2 구독 상품 등록

**월간 구독 (무료 체험 없음):**

| 항목 | 값 |
|------|------|
| 참조 이름 | FXi Premium Monthly |
| 제품 ID | `fxi_premium_monthly` |
| 구독 기간 | 1개월 |
| 가격 | ₩12,000 (App Store Connect에서 해당 가격대 선택) |
| 프로모션 오퍼 | 없음 |
| Introductory Offer | **없음** |

**연간 구독 (7일 무료 체험):**

| 항목 | 값 |
|------|------|
| 참조 이름 | FXi Premium Yearly |
| 제품 ID | `fxi_premium_yearly` |
| 구독 기간 | 1년 |
| 가격 | ₩99,000 (App Store Connect에서 해당 가격대 선택) |
| Introductory Offer | **Free Trial, 7일** |

> **참고**: App Store Connect의 가격 Tier는 지역별/시기별로 변경될 수 있습니다. 정확한 가격은 App Store Connect에서 직접 확인하세요.

#### 3.3 Introductory Offer 설정 (연간만)

```
구독 상품 → Introductory Offers → Add Introductory Offer

Type: Free Trial
Duration: 7 days
```

> **iOS 동작**: 무료 체험 종료 24시간 전까지 해지하지 않으면 자동 결제

### 4. RevenueCat 설정 ✅ 완료

#### 4.0 설정된 값 요약 (2025-12-25)

```
App Store Connect
├── 앱 이름: 환율아이 - 환율 비교 및 환율 알림
├── Bundle ID: com.Jay.FXi
├── SKU: FXi
├── Apple ID: 6756925102
├── Products: fxi_premium_monthly (₩12,000), fxi_premium_yearly (₩99,000 + 7일 체험)
└── S2S Notification: RevenueCat URL 설정 완료

RevenueCat
├── Public API Key: appl_vCSLuclmGEWwPxjNAsiYIkJWrGH
├── App ID: appee70d03c62
├── Entitlement: premium (entlf9414cb78d)
├── Offering: default (ofrngee045641c8)
├── In-App Purchase Key: SubscriptionKey_3BB353KJ2L.p8
└── Products:
    ├── fxi_premium_monthly (prodb09e9930b3)
    └── fxi_premium_yearly (prodff25b87b75)
```

#### 4.1 계정 및 프로젝트 생성 ✅

1. https://app.revenuecat.com/signup 에서 계정 생성
2. 새 프로젝트 생성: "FXi" (또는 "환율아이")
3. iOS 앱 추가:
   - Bundle ID: `com.Jay.FXi`
   - In-App Purchase Key (.p8 파일) 업로드

#### 4.2 Products 등록 ✅

RevenueCat → Products → New Product

| Identifier | Store | Product ID |
|------------|-------|------------|
| `fxi_premium_monthly` | App Store | `fxi_premium_monthly` |
| `fxi_premium_yearly` | App Store | `fxi_premium_yearly` |

#### 4.3 Entitlements 생성 ✅

```
RevenueCat → Entitlements → New Entitlement

Identifier: premium
Display Name: 프리미엄 구독 권한
Associated Products: fxi_premium_monthly, fxi_premium_yearly
```

#### 4.4 Offerings 설정 ✅

```
RevenueCat → Offerings → default

Packages:
├── $rc_monthly → fxi_premium_monthly (월간 구독)
└── $rc_annual → fxi_premium_yearly (연간 구독)
```

### 5. iOS SDK 연동 ✅ 완료

> **iOS 최소 버전**: iOS 17.0 (StoreKit 2 완전 지원)
> **구현 완료**: 2025-12-27 (상세: `~/Downloads/Projects/FXi/ios/CLAUDE.md`)

#### 5.1 SDK 설치 (Swift Package Manager)

```
Xcode → File → Add Package Dependencies
URL: https://github.com/RevenueCat/purchases-ios
Version: 5.0.0 이상
```

#### 5.2 SDK 초기화 (AppDelegate.swift)

```swift
import FirebaseCore
import FirebaseCrashlytics
import RevenueCat

func application(_ application: UIApplication,
                 didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {

    // 1. Firebase 초기화
    FirebaseApp.configure()

    // 2. (선택) Crashlytics 설정
    #if DEBUG
    Crashlytics.crashlytics().setCrashlyticsCollectionEnabled(false)
    #else
    Crashlytics.crashlytics().setCrashlyticsCollectionEnabled(true)
    #endif

    // 3. RevenueCat 초기화
    #if DEBUG
    Purchases.logLevel = .debug
    #endif

    Purchases.configure(
        with: Configuration.Builder(withAPIKey: RevenueCatConfig.publicAPIKey)
            .with(storeKitVersion: .storeKit2)
            .build()
    )

    // 4. AuthService 설정
    AuthService.shared.configure()

    // 5. PushNotificationService 설정
    PushNotificationService.shared.configure()

    return true
}
```

#### 5.3 Firebase UID 연동 (AuthService.swift)

> **중요**: RevenueCat `logIn()` 완료 후 반드시 `SubscriptionManager.shared.onAuthCompleted()` 호출해야 합니다.
> 이 호출이 없으면 기존 구독자도 Paywall을 잠깐 볼 수 있습니다. (Section 7.5 참조)

```swift
import RevenueCat

// setupAuthStateListener() 내부 수정
private func setupAuthStateListener() {
    authStateHandle = Auth.auth().addStateDidChangeListener { [weak self] _, user in
        Task { @MainActor [weak self] in
            guard let self = self else { return }

            if let user = user {
                // ✅ RevenueCat에 Firebase UID로 로그인
                do {
                    let result = try await Purchases.shared.logIn(user.uid)
                    print("RevenueCat 로그인: \(result.customerInfo.originalAppUserId)")

                    // 사용자 속성 설정 (선택)
                    if let email = user.email {
                        Purchases.shared.attribution.setEmail(email)
                    }
                    if let name = user.displayName {
                        Purchases.shared.attribution.setDisplayName(name)
                    }

                    // ✅ 구독 상태 로드 완료 알림 (Paywall 깜빡임 방지)
                    SubscriptionManager.shared.onAuthCompleted()
                } catch {
                    print("RevenueCat 로그인 실패: \(error)")
                    // 실패해도 로딩 상태 해제 (빈 상태로 진행)
                    SubscriptionManager.shared.onAuthCompleted()
                }

                // 기존 로직...
                let provider = self.determineProvider(from: user)
                self.authState = .signedIn(user: UserInfo(...))

            } else {
                // ✅ RevenueCat 로그아웃
                do {
                    let _ = try await Purchases.shared.logOut()
                } catch {
                    print("RevenueCat 로그아웃 실패: \(error)")
                }

                // ✅ 구독 상태 초기화
                SubscriptionManager.shared.onAuthSignedOut()
                self.authState = .signedOut
            }
        }
    }
}
```

#### 5.4 SubscriptionManager 서비스

**파일**: `ios/FXi/Services/SubscriptionManager.swift`

> **주의**: 반드시 `isLoadingInitial` + `onAuthCompleted()` 패턴을 사용해야 합니다.
> 이 패턴 없이 init에서 customerInfo를 로드하면 기존 구독자도 Paywall을 잠깐 볼 수 있습니다.
> **참고**: 실제 구현 파일 참조 - `ios/FXi/Services/SubscriptionManager.swift`

```swift
import Foundation
import RevenueCat
import Observation

/// RevenueCat 구독 상태 관리
@MainActor
@Observable
final class SubscriptionManager {
    // MARK: - Singleton

    static let shared = SubscriptionManager()

    // MARK: - Properties

    private(set) var customerInfo: CustomerInfo?
    private(set) var offerings: Offerings?
    private(set) var isPremium: Bool = false
    private(set) var isLoading: Bool = false

    /// 초기 로딩 상태 (Firebase Auth + RevenueCat logIn 완료 전)
    /// RootView에서 이 값이 true인 동안 로딩 화면 표시 → Paywall 깜빡임 방지
    private(set) var isLoadingInitial: Bool = true

    /// 무료 체험 자격 여부 (이전에 체험한 사용자는 false)
    private(set) var isEligibleForIntro: Bool = false

    /// 오퍼링 로드 에러
    private(set) var offeringsError: Error?

    /// 진행 중인 오퍼링 로드 태스크 (중복 호출 시 공유)
    private var offeringsTask: Task<Void, Never>?

    // MARK: - Init

    private init() {
        // ⚠️ init에서 customerInfo 로드하지 않음
        // AuthService에서 logIn 완료 후 onAuthCompleted() 호출
        subscribeToCustomerInfoStream()
    }

    private func subscribeToCustomerInfoStream() {
        Task {
            for await info in Purchases.shared.customerInfoStream {
                self.customerInfo = info
                self.isPremium = info.entitlements[RevenueCatConfig.premiumEntitlement]?.isActive == true
            }
        }
    }

    // MARK: - Auth Integration

    /// AuthService에서 Firebase Auth + RevenueCat logIn 완료 후 호출
    func onAuthCompleted() {
        Task {
            do {
                // 이 시점에는 이미 Purchases.shared.logIn(uid)가 완료됨
                let info = try await Purchases.shared.customerInfo()
                self.customerInfo = info
                self.isPremium = info.entitlements[RevenueCatConfig.premiumEntitlement]?.isActive == true

                // 프리미엄 + 알림 권한 이미 승인 → 푸시 토큰 재등록 (기존 알림 유지)
                if self.isPremium {
                    await rehydratePushTokenIfNeeded()
                }
            } catch {
                print("구독 상태 로드 실패: \(error)")
            }
            self.isLoadingInitial = false
        }
    }

    /// 기존 알림 유지를 위한 푸시 토큰 재등록
    /// - 앱 재시작 후 또는 프리미엄 전환 시 호출
    /// - 알림 권한이 이미 승인된 상태에서만 실행
    func rehydratePushTokenIfNeeded() async {
        await NotificationPermissionManager.shared.checkStatusAsync()

        if NotificationPermissionManager.shared.hasPermission {
            PushNotificationService.shared.shouldRegisterForPush = true
            await PushNotificationService.shared.registerDeviceOnServer()
        }
    }

    /// 로그아웃 시 호출 (상태 초기화)
    func onAuthSignedOut() {
        offeringsTask?.cancel()
        offeringsTask = nil

        self.customerInfo = nil
        self.offerings = nil
        self.isPremium = false
        self.isLoading = false
        self.isLoadingInitial = true
        self.isEligibleForIntro = false
        self.offeringsError = nil
    }

    // MARK: - Offerings

    func loadOfferings() async {
        // 이미 로딩 중인 태스크가 있으면 완료까지 대기 (중복 네트워크 호출 방지)
        if let existingTask = offeringsTask {
            await existingTask.value
            return
        }

        offeringsTask = Task { [weak self] in
            guard let self else { return }
            defer { offeringsTask = nil }
            guard !Task.isCancelled else { return }

            isLoading = true
            offeringsError = nil
            defer { isLoading = false }

            do {
                let loadedOfferings = try await Purchases.shared.offerings()
                guard !Task.isCancelled else { return }

                offerings = loadedOfferings
                offeringsError = nil

                // 무료 체험 자격 확인
                if let annual = annualPackage {
                    let eligibility = await Purchases.shared.checkTrialOrIntroDiscountEligibility(
                        productIdentifiers: [annual.storeProduct.productIdentifier]
                    )
                    guard !Task.isCancelled else { return }

                    let status = eligibility[annual.storeProduct.productIdentifier]?.status
                    isEligibleForIntro = status == .eligible
                } else {
                    isEligibleForIntro = false
                }
            } catch {
                guard !Task.isCancelled else { return }
                offeringsError = error
                print("Offerings 로드 실패: \(error)")
            }
        }

        await offeringsTask?.value
    }

    // MARK: - Purchase

    /// 구매 결과를 반환 (취소 시 false)
    @discardableResult
    func purchase(_ package: Package) async throws -> Bool {
        isLoading = true
        defer { isLoading = false }

        let result = try await Purchases.shared.purchase(package: package)

        // 사용자 취소
        if result.userCancelled {
            return false
        }

        customerInfo = result.customerInfo
        isPremium = result.customerInfo.entitlements[RevenueCatConfig.premiumEntitlement]?.isActive == true
        return true
    }

    // MARK: - Restore

    func restorePurchases() async throws {
        isLoading = true
        defer { isLoading = false }

        let wasPremium = isPremium
        let info = try await Purchases.shared.restorePurchases()
        customerInfo = info
        isPremium = info.entitlements[RevenueCatConfig.premiumEntitlement]?.isActive == true

        // 이미 프리미엄이었던 경우에만 알림 갱신
        if isPremium && wasPremium {
            NotificationCenter.default.post(name: .alertSettingsNeedsRefresh, object: nil)
        }
    }

    // MARK: - Helpers

    var monthlyPackage: Package? {
        offerings?.current?.monthly
    }

    var annualPackage: Package? {
        offerings?.current?.annual
    }

    /// 연간 구독의 무료 체험 가능 여부 (상품 설정 + 사용자 자격 모두 확인)
    var hasIntroductoryOffer: Bool {
        guard let annual = annualPackage else { return false }
        return annual.storeProduct.introductoryDiscount != nil && isEligibleForIntro
    }

    /// 무료 체험 기간 텍스트 (예: "7일") - 자격 없으면 nil
    var introductoryOfferDuration: String? {
        guard isEligibleForIntro,
              let annual = annualPackage,
              let intro = annual.storeProduct.introductoryDiscount else { return nil }

        let unit = intro.subscriptionPeriod.unit
        let value = intro.subscriptionPeriod.value

        switch unit {
        case .day: return "\(value)일"
        case .week: return "\(value * 7)일"
        case .month: return "\(value)개월"
        case .year: return "\(value)년"
        @unknown default: return nil
        }
    }
}
```

**RevenueCatConfig 상수** (`ios/FXi/Utils/Constants.swift`):

```swift
enum RevenueCatConfig {
    /// Public API Key (앱에 포함해도 안전)
    static let publicAPIKey = "appl_vCSLuclmGEWwPxjNAsiYIkJWrGH"

    /// Premium Entitlement 식별자
    static let premiumEntitlement = "premium"
}
```

### 5.5 서버사이드 Entitlement 검증 (Premium API 보호) ✅ 완료

> **중요**: 클라이언트 Paywall만으로는 불충분. 비구독자가 API 직접 호출 시 우회 가능.
> **구현 완료**: 2025-12-27 (상세: `REVENUECAT_SERVER_SIDE.md`)

#### Premium-Gated 엔드포인트

| 엔드포인트 | 비구독자 | 구독자 | 비고 |
| --------- | ------- | ----- | ---- |
| `POST /api/notification-settings` | ❌ 403 Forbidden | ✅ 허용 | 알림 설정 생성 |
| `PUT /api/notification-settings/{id}` | ❌ 403 Forbidden | ✅ 허용 | 알림 설정 수정 |
| `DELETE /api/notification-settings/{id}` | ❌ 403 Forbidden | ✅ 허용 | 알림 설정 삭제 |
| `GET /api/notification-settings` | ✅ 빈 목록 | ✅ 설정 목록 | 조회만 허용 |
| `GET /api/rates`, `WS /ws` | ✅ 허용 | ✅ 허용 | 아래 참고 |

> **PENDING 상태**: RevenueCat 상태 확인 중이면 503 반환 + `Retry-After: 5` 헤더 포함  
> (신규 사용자/일시적 API 오류 시, 잠시 후 재시도 유도)

> 💡 **계정 삭제**: `DELETE /api/user/me` 호출 시 해당 사용자의 알림 데이터(`notification_settings`, `notification_logs`, `user_devices`)가 함께 삭제됩니다.

> **API vs Locked Preview 정책:**
>
> - **서버 API**: `/api/rates`, `WS /ws`는 비구독자에게도 **열려 있음** (기술적으로 접근 가능)
> - **클라이언트 UX**: Locked Preview 화면에서는 **샘플 데이터만 표시** (API 호출하지 않음)
> - **이유**: 서버 복잡도 최소화 + iOS 심사/테스트 용이 + 일반 사용자의 API 직접 호출 가능성 극히 낮음
> - **결론**: 구독 유도는 클라이언트 UX로 처리, 서버는 알림 설정 API만 게이팅

#### Phase 3 MVP: 요청 시 검증 (Request-Time Verification)

> 📖 **구현 상세**: [REVENUECAT_SERVER_SIDE.md](REVENUECAT_SERVER_SIDE.md) 참조

핵심 요약:
- `verify_premium_status()` + `require_premium()`으로 Premium 게이팅 적용
- 200/201만 캐시, 404 포함 4xx/5xx 미캐시 (Get-or-Create 계약상 신규 고객은 201 — 404 는 계약 밖 상태, 2026-08-14 교정)
- LRU 1000명 + TTL 5분 + Stale 1시간
- 캐시 없음 + API 오류 시 **PENDING** 반환 → 503 + Retry-After(5초)

    # 4. API 오류 시: stale 캐시가 있으면 사용 (유료 사용자 보호)
    if cached_value is not None:
        logger.info(
            "API 오류, stale 캐시 사용",
            extra={"user_id": user_id, "stale_value": cached_value}
        )
        return cached_value

    # 5. 캐시도 없고 API도 실패 → PENDING (재시도 필요)
    return PremiumStatus.PENDING
```

#### API 엔드포인트에 적용

```python
# app/main.py
# (require_premium은 main.py 상단 유틸 함수)

@app.post("/api/notification-settings")
async def create_notification_setting(
    request: Request,
    body: NotificationSettingRequest,
    db: Session = Depends(get_db)
):
    user_id = await verify_firebase_token(request)

    # ✅ Premium 검증 추가 (PENDING이면 503)
    await require_premium(user_id, allow_empty=False)

    # 기존 로직...
    setting = crud.create_notification_setting(
        db=db,
        user_id=user_id,
        bank=body.bank.value,
        currency=body.currency.value,
        condition=body.condition.value,
        threshold=body.threshold,
        is_enabled=body.is_enabled,
    )
    return build_notification_setting_response(setting)
```

#### 확장: Webhook 기반 캐싱 ✅ 구현 완료

> **보안 필수**: Webhook 엔드포인트는 반드시 Authorization 헤더 검증을 해야 합니다.
> 검증 없이는 누구나 가짜 이벤트를 보내 구독 상태를 조작할 수 있습니다.

> 📖 **구현 상세**: [REVENUECAT_SERVER_SIDE.md](REVENUECAT_SERVER_SIDE.md) 참조

**RevenueCat Webhook 설정 (완료):**

1. RevenueCat Dashboard → Integrations → Webhooks → FXi(Active) [Add new configuration]
2. Webhook URL: `https://fxi.kr/webhooks/revenuecat`
3. Authorization: Dashboard에 설정한 값과 `.env`의 `REVENUECAT_WEBHOOK_AUTH_KEY`를 동일하게 맞춤

> **현재 상태**: 요청 시 검증 (5분 캐시) + Webhook 캐시 무효화 모두 구현됨.

### 6. Paywall UI 설계

#### 6.1 Paywall 화면 구조

```
┌─────────────────────────────────────┐
│                                     │
│              👑                     │
│                                     │
│      은행별 현재 환율비교의 모든 것        │
│                                     │
├─────────────────────────────────────┤
│ ✓ 9개 은행 환율 비교                    │
│ ✓ USD, JPY, EUR 환율                 │
│ ✓ 맞춤 푸시 알림                       │
│ ✓ 24시간 추이 그래프                    │
├─────────────────────────────────────┤
│                                     │
│  ┌─────────────────────────────┐   │
│  │ 연간 구독 (추천)               │   │
│  │ ₩99,000/년 (월 ₩8,250)       │   │
│  │ 7일 무료 체험                  │   │
│  │ [31% 할인]                   │   │
│  └─────────────────────────────┘   │
│                                    │
│  ┌─────────────────────────────┐   │
│  │ 월간 구독                     │   │
│  │ ₩12,000/월                   │   │
│  └─────────────────────────────┘   │
│                                     │
│         구매 복원                     │
│                                     │
│  [필수 약관 문구 - 아래 참조]            │
└─────────────────────────────────────┘
```

#### 6.2 핵심 UI 요소

1. **연간 우선 배치**: 무료 체험이 있는 연간을 상단에, 더 크게 표시
2. **할인율 강조**: "31% 할인" 뱃지로 가치 인식
3. **무료 체험 강조**: "7일 무료 체험" 명시 (월간에는 없음)
4. **약관 명시**: App Store 심사 통과를 위한 필수 문구

#### 6.3 필수 약관 문구 (App Review 필수)

Paywall 하단에 반드시 포함해야 하는 문구:

```
• 결제는 구매 확인 시 Apple ID 계정으로 청구됩니다.
• 현재 기간 종료 최소 24시간 전에 자동 갱신을 해지하지 않으면 구독이 자동으로 갱신됩니다.
• 갱신 비용은 현재 기간 종료 전 24시간 이내에 계정으로 청구됩니다.
• 구독은 구매 후 설정 > Apple ID > 구독에서 관리하거나 해지할 수 있습니다.
• 무료 체험 기간 중 구독을 구매하면 남은 무료 체험 기간은 소멸됩니다.

[이용약관] [개인정보처리방침]
```

> **중요**: 이 문구가 없거나 불완전하면 App Review에서 리젝될 수 있습니다.

### 7. 사용자 흐름: 온보딩 + Locked Preview

#### 7.1 전체 앱 플로우

```
[앱 실행] → [스플래시] → [로그인 (Firebase Auth)]
                              ↓
                    [구독 상태 로딩]
                              ↓
                    [상태 확인 완료?]
                              ↓
              ┌───────────────┴───────────────┐
              ↓                               ↓
    [구독 중] → [메인 화면]         [미구독 + 첫 방문?]
                                              ↓
                                    ┌─────────┴─────────┐
                                    ↓                   ↓
                          [Yes] → [온보딩 3장]    [No] → [Locked Preview]
                                        ↓                    ↓
                                  [Paywall]            [Paywall Sheet]
                                        ↓                    ↓
                              [구독 완료] → [메인 화면]
```

#### 7.2 온보딩 화면 (첫 방문 사용자용)

**목적**: 구독 가치를 시각적으로 전달 → Paywall 전환율 향상

**구현 포인트:**
- `@AppStorage("hasCompletedOnboarding")` 플래그로 첫 방문 구분
- 온보딩 완료 시 Paywall 자동 표시
- 스킵 버튼 없음 (3장만 보면 됨)

#### 7.3 Locked Preview 화면 (재방문 미구독 사용자용)

**목적**: 앱 강제 종료 대신, 앱의 가치를 **애니메이션 샘플 데이터**로 시각적으로 전달

> **결정**: 실제 환율 데이터 대신 **애니메이션 샘플 데이터** 사용
>
> - 실제 데이터 노출 시 구독 동기 감소 우려
> - 애니메이션이 있는 샘플 화면이 더 흥미롭고 구독 전환에 효과적

```
┌───────────────────────────────────────────────┐
│  ┌─────────────────────────────────────────┐  │
│  │  ⚠️ 예시 화면입니다                      │  │  ← 상단 배너
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  🔔 KB 1,385.50 이하 알림               │  │  ← 샘플 알림 팝업
│  │  목표 환율에 도달하면 알려드립니다        │  │    (8초마다 표시)
│  └─────────────────────────────────────────┘  │
│                                               │
│  USD-KRW                                      │
│  ┌──────────────────────────────────────┐     │
│  │ 🏛️ 인베스팅  1,385.50 ◀━━━━━━━━━━━━ │     │  ← 바 애니메이션
│  └──────────────────────────────────────┘     │    (3초마다 변동)
│  ┌───────────────────────────────────┐        │
│  │ 🏦 KB       1,386.20  +0.70 ◀━━━━━│        │
│  └───────────────────────────────────┘        │
│  ┌────────────────────────────────┐           │
│  │ 🏦 하나     1,384.80  -0.70 ◀━━│           │
│  └────────────────────────────────┘           │
│  ┌─────────────────────────────────┐          │
│  │ 🏦 신한     1,385.90  +0.40 ◀━━━│          │
│  └─────────────────────────────────┘          │
│        ... (blur overlay)                     │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │  🔒 프리미엄 구독으로                    │  │  ← 잠금 오버레이
│  │     은행별 환율 확인하기                  │  │
│  │                                         │  │
│  │  [ 구독하고 전체 기능 사용하기 ]          │  │  ← CTA 버튼
│  └─────────────────────────────────────────┘  │
│                                               │
└───────────────────────────────────────────────┘
```

**핵심 애니메이션:**

1. **바 그래프 애니메이션** (3초마다)
   - 샘플 환율 값이 소폭 변동 (±0.10 ~ ±0.50)
   - 숫자 롤링 애니메이션 (`AnimatableNumberText` 패턴 재사용)
   - 바 너비 변화 + 펄스 효과
   - 방향 표시 (▲/▼) 잠깐 표시 후 차이값으로 전환

2. **샘플 알림 팝업** (8초마다)
   - 상단에서 슬라이드 다운 → 3초 유지 → 페이드 아웃
   - 랜덤 은행 + 목표 환율 표시
   - "알림 기능이 이렇게 동작합니다" 시각적 데모

3. **하단 Blur + 오버레이**
   - 4개 은행 이후 점진적 blur 처리
   - CTA 버튼으로 Paywall 유도

**장점:**

- iOS 심사 거절 위험 없음 (앱 강제 종료 없음)
- 애니메이션이 사용자 시선 유지 → 체류 시간 증가
- 알림 기능 데모 → "이 알림이 필요하다" 욕구 자극
- 앱 UI에서 실제 API 호출 생략, 샘플만 표시 → 구독 동기 유지
  - (참고: API 자체는 열려있으나 클라이언트에서 호출하지 않음, Section 5.5 참조)

#### 7.3.1 LockedPreviewView.swift 구현

```swift
import SwiftUI

/// 비구독자용 애니메이션 샘플 화면
struct LockedPreviewView: View {
    @Binding var showPaywall: Bool

    // MARK: - Sample Data

    /// 샘플 환율 데이터 (애니메이션용)
    @State private var sampleRates: [SampleRate] = SampleRate.initial

    /// 샘플 알림 표시 여부
    @State private var showSampleNotification = false
    @State private var currentNotification: SampleNotification?

    /// 애니메이션 타이머
    @State private var rateAnimationTimer: Timer?
    @State private var notificationTimer: Timer?

    var body: some View {
        ZStack {
            AppColors.background
                .ignoresSafeArea()

            VStack(spacing: 0) {
                // 상단 배너
                sampleBanner

                // 샘플 알림 팝업 (애니메이션)
                if showSampleNotification, let notification = currentNotification {
                    sampleNotificationBanner(notification)
                        .transition(.move(edge: .top).combined(with: .opacity))
                        .zIndex(1)
                }

                // 메인 콘텐츠 (샘플 환율 바)
                ScrollView {
                    VStack(spacing: 12) {
                        sampleRatesSection

                        // 하단 blur 영역
                        blurredSection
                    }
                    .padding()
                }

                // 잠금 오버레이 + CTA
                premiumOverlay
            }
        }
        .onAppear {
            startAnimations()
        }
        .onDisappear {
            stopAnimations()
        }
    }

    // MARK: - Sample Banner

    private var sampleBanner: some View {
        HStack {
            Image(systemName: "info.circle.fill")
                .foregroundColor(.orange)
            Text("예시 화면입니다")
                .font(.subheadline)
                .fontWeight(.medium)
                .foregroundColor(AppColors.primaryText)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color.orange.opacity(0.15))
    }

    // MARK: - Sample Notification Banner

    private func sampleNotificationBanner(_ notification: SampleNotification) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "bell.fill")
                .font(.title3)
                .foregroundColor(.orange)

            VStack(alignment: .leading, spacing: 2) {
                Text("\(notification.bankName) \(notification.formattedRate) 이하 알림")
                    .font(.subheadline)
                    .fontWeight(.semibold)
                    .foregroundColor(AppColors.primaryText)

                Text("목표 환율에 도달하면 알려드립니다")
                    .font(.caption)
                    .foregroundColor(AppColors.secondaryText)
            }

            Spacer()
        }
        .padding(12)
        .background(AppColors.cardBackground)
        .cornerRadius(12)
        .shadow(color: .black.opacity(0.3), radius: 8, y: 4)
        .padding(.horizontal, 16)
        .padding(.top, 8)
    }

    // MARK: - Sample Rates Section

    private var sampleRatesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("USD-KRW")
                .font(.headline)
                .foregroundColor(AppColors.primaryText)

            ForEach(sampleRates.prefix(4)) { rate in
                SampleRateBarView(rate: rate)
            }
        }
        .padding()
        .background(AppColors.cardBackground)
        .cornerRadius(12)
    }

    // MARK: - Blurred Section

    private var blurredSection: some View {
        VStack(spacing: 8) {
            ForEach(0..<3, id: \.self) { _ in
                RoundedRectangle(cornerRadius: 6)
                    .fill(AppColors.inputBackground)
                    .frame(height: 36)
            }
        }
        .padding()
        .background(AppColors.cardBackground)
        .cornerRadius(12)
        .blur(radius: 8)
        .overlay(
            Text("더 많은 은행 환율...")
                .font(.caption)
                .foregroundColor(AppColors.secondaryText)
        )
    }

    // MARK: - Premium Overlay

    private var premiumOverlay: some View {
        VStack(spacing: 16) {
            HStack {
                Image(systemName: "lock.fill")
                    .font(.title2)
                Text("프리미엄 구독으로\n은행별 환율 확인하기")
                    .font(.headline)
                    .multilineTextAlignment(.center)
            }
            .foregroundColor(AppColors.primaryText)

            Button {
                showPaywall = true
            } label: {
                Text("구독하고 전체 기능 사용하기")
                    .font(.headline)
                    .foregroundColor(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Color.accentColor)
                    .cornerRadius(12)
            }
            .padding(.horizontal, 24)
        }
        .padding(.vertical, 24)
        .background(
            LinearGradient(
                colors: [AppColors.background.opacity(0), AppColors.background],
                startPoint: .top,
                endPoint: .center
            )
        )
    }

    // MARK: - Animation Control

    private func startAnimations() {
        // 바 그래프 애니메이션 (3초마다)
        rateAnimationTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in
            withAnimation(.easeInOut(duration: 1.0)) {
                sampleRates = sampleRates.map { $0.randomized() }
            }
        }

        // 샘플 알림 애니메이션 (8초마다)
        notificationTimer = Timer.scheduledTimer(withTimeInterval: 8.0, repeats: true) { _ in
            currentNotification = SampleNotification.random()
            withAnimation(.spring(response: 0.5, dampingFraction: 0.8)) {
                showSampleNotification = true
            }

            // 3초 후 숨김
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
                withAnimation(.easeOut(duration: 0.3)) {
                    showSampleNotification = false
                }
            }
        }

        // 첫 알림 2초 후 표시
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            notificationTimer?.fire()
        }
    }

    private func stopAnimations() {
        rateAnimationTimer?.invalidate()
        notificationTimer?.invalidate()
    }
}
```

#### 7.3.2 샘플 데이터 모델

```swift
// MARK: - Sample Data Models

struct SampleRate: Identifiable {
    let id = UUID()
    let bank: Bank
    var rate: Double
    var diff: Double

    func randomized() -> SampleRate {
        let change = Double.random(in: -0.50...0.50)
        let newRate = rate + change
        let newDiff = diff + change
        return SampleRate(bank: bank, rate: newRate, diff: newDiff)
    }

    static let initial: [SampleRate] = [
        SampleRate(bank: .investing, rate: 1385.50, diff: 0),
        SampleRate(bank: .kb, rate: 1386.20, diff: 0.70),
        SampleRate(bank: .hana, rate: 1384.80, diff: -0.70),
        SampleRate(bank: .shinhan, rate: 1385.90, diff: 0.40)
    ]
}

struct SampleNotification {
    let bankName: String
    let targetRate: Double

    var formattedRate: String {
        String(format: "%.2f", targetRate)
    }

    static func random() -> SampleNotification {
        let banks = ["KB", "하나", "신한", "우리", "농협"]
        let rates = [1380.0, 1385.0, 1390.0, 1375.0, 1395.0]
        return SampleNotification(
            bankName: banks.randomElement()!,
            targetRate: rates.randomElement()!
        )
    }
}
```

#### 7.3.3 샘플 환율 바 뷰

```swift
// MARK: - Sample Rate Bar View

/// 샘플 환율 바 (애니메이션 지원, RateBarView 패턴 참조)
struct SampleRateBarView: View {
    let rate: SampleRate

    var body: some View {
        HStack(spacing: 0) {
            // 은행 아이콘 + 이름
            HStack(spacing: 8) {
                Circle()
                    .fill(rate.bank.color)
                    .frame(width: 24, height: 24)
                    .overlay(
                        Text(rate.bank.shortName.prefix(1))
                            .font(.caption2)
                            .fontWeight(.bold)
                            .foregroundColor(.white)
                    )

                Text(rate.bank.shortName)
                    .font(.subheadline)
                    .foregroundColor(AppColors.primaryText)
                    .frame(width: 50, alignment: .leading)
            }

            Spacer()

            // 환율 (애니메이션)
            Text(String(format: "%.2f", rate.rate))
                .font(.body)
                .fontWeight(.bold)
                .foregroundColor(.white)
                .monospacedDigit()
                .contentTransition(.numericText())

            // 차이값 (기준 제외)
            if rate.bank != .investing {
                Text(String(format: "%+.2f", rate.diff))
                    .font(.caption)
                    .fontWeight(.medium)
                    .foregroundColor(rate.diff > 0 ? AppColors.positive : AppColors.negative)
                    .monospacedDigit()
                    .frame(width: 50, alignment: .trailing)
                    .contentTransition(.numericText())
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(rate.bank.color.opacity(0.8))
                .scaleEffect(x: barWidthScale, y: 1, anchor: .leading)
        )
        .animation(.easeInOut(duration: 1.0), value: rate.rate)
    }

    /// 바 너비 비율 (환율 기반)
    private var barWidthScale: CGFloat {
        let normalized = (rate.rate - 1380) / 20  // 1380~1400 범위
        return 0.5 + normalized * 0.5  // 50%~100%
    }
}
```

#### 7.4 RootView 수정 (FXiApp.swift)

```swift
struct RootView: View {
    @Environment(AuthService.self) private var authService
    @State private var subscriptionManager = SubscriptionManager.shared
    @State private var showPaywall = false
    @AppStorage("hasCompletedOnboarding") private var hasCompletedOnboarding = false

    var body: some View {
        Group {
            switch authService.authState {
            case .unknown:
                splashView

            case .signedOut:
                LoginView()

            case .signedIn:
                // 구독 상태 로딩 중: 로딩 화면 (기존 구독자 Paywall 깜빡임 방지)
                if subscriptionManager.isLoadingInitial {
                    loadingView
                } else if subscriptionManager.isPremium {
                    // ✅ 구독 중: 메인 화면
                    ContentView()
                } else if !hasCompletedOnboarding {
                    // ❌ 미구독 + 첫 방문: 온보딩
                    OnboardingView(onComplete: {
                        hasCompletedOnboarding = true
                        showPaywall = true
                    })
                } else {
                    // ❌ 미구독 + 재방문: Locked Preview
                    LockedPreviewView(showPaywall: $showPaywall)
                }
            }
        }
        .sheet(isPresented: $showPaywall) {
            PaywallView()
        }
        .animation(.easeInOut(duration: 0.3), value: authService.authState)
        .animation(.easeInOut(duration: 0.3), value: subscriptionManager.isPremium)
    }

    private var loadingView: some View {
        ZStack {
            AppColors.background
                .ignoresSafeArea()
            ProgressView()
                .progressViewStyle(CircularProgressViewStyle(tint: .white))
        }
    }
}
```

#### 7.5 SubscriptionManager 로딩 상태 추가

> **주의**: 반드시 Firebase Auth logIn 완료 후 구독 상태를 확인해야 합니다.
> 익명 customerInfo를 먼저 로드하면 기존 구독자도 Paywall을 잠깐 볼 수 있습니다.

```swift
@MainActor
@Observable
final class SubscriptionManager {
    static let shared = SubscriptionManager()

    private(set) var customerInfo: CustomerInfo?
    private(set) var offerings: Offerings?
    private(set) var isPremium: Bool = false
    private(set) var isLoading: Bool = false

    /// 초기 로딩 상태 (Firebase Auth + RevenueCat logIn 완료 전)
    private(set) var isLoadingInitial: Bool = true

    private init() {
        // ⚠️ init에서 customerInfo 로드하지 않음
        // AuthService에서 logIn 완료 후 onAuthCompleted() 호출
        subscribeToCustomerInfoStream()
    }

    /// AuthService에서 Firebase Auth + RevenueCat logIn 완료 후 호출
    func onAuthCompleted() {
        Task {
            do {
                // 이 시점에는 이미 Purchases.shared.logIn(uid)가 완료됨
                let info = try await Purchases.shared.customerInfo()
                self.customerInfo = info
                self.isPremium = info.entitlements["premium"]?.isActive == true
            } catch {
                print("구독 상태 로드 실패: \(error)")
            }
            self.isLoadingInitial = false
        }
    }

    /// 로그아웃 시 호출 (상태 초기화)
    func onAuthSignedOut() {
        self.customerInfo = nil
        self.isPremium = false
        self.isLoadingInitial = true
    }

    private func subscribeToCustomerInfoStream() {
        Task {
            for await info in Purchases.shared.customerInfoStream {
                self.customerInfo = info
                self.isPremium = info.entitlements["premium"]?.isActive == true
            }
        }
    }

    // ... loadOfferings, purchase, restore 등 기존 메서드 ...
}
```

#### 7.6 AuthService 수정 (SubscriptionManager 연동)

```swift
// AuthService.swift - setupAuthStateListener() 수정
private func setupAuthStateListener() {
    authStateHandle = Auth.auth().addStateDidChangeListener { [weak self] _, user in
        Task { @MainActor [weak self] in
            guard let self = self else { return }

            if let user = user {
                // 1. RevenueCat에 Firebase UID로 로그인
                do {
                    let result = try await Purchases.shared.logIn(user.uid)
                    print("RevenueCat 로그인: \(result.customerInfo.originalAppUserId)")

                    // 2. ✅ SubscriptionManager에 로그인 완료 알림
                    SubscriptionManager.shared.onAuthCompleted()

                } catch {
                    print("RevenueCat 로그인 실패: \(error)")
                    // 실패해도 isLoadingInitial = false로 전환
                    SubscriptionManager.shared.onAuthCompleted()
                }

                // 기존 로직...
                self.authState = .signedIn(user: UserInfo(...))

            } else {
                // RevenueCat 로그아웃
                do {
                    let _ = try await Purchases.shared.logOut()
                } catch {
                    print("RevenueCat 로그아웃 실패: \(error)")
                }

                // ✅ SubscriptionManager 상태 초기화
                SubscriptionManager.shared.onAuthSignedOut()

                self.authState = .signedOut
            }
        }
    }
}
```

**핵심 개선:**
- `isLoadingInitial`: Firebase Auth + RevenueCat logIn 완료 후에만 false로 전환
- `onAuthCompleted()`: AuthService에서 명시적으로 호출 → 타이밍 보장
- 기존 구독자가 Paywall/온보딩을 잠깐 보는 문제 해결

### 8. 무료 체험 전환 최적화

#### 8.1 체험 기간 중 푸시 알림 (선택)

> **⚠️ 마케팅 푸시 주의사항**:
> 아래 메시지는 환율 알림이 아닌 **마케팅/리텐션 목적**입니다.
> 사용자가 "환율 알림"만 동의한 경우 별도 **마케팅 푸시 동의**가 필요합니다.
> 미동의 시 App Review 리젝 또는 사용자 불만 가능성 있음.

```
Day 1: "아침 9시에 환율을 확인해보세요. 은행별 차이가 한눈에 보입니다."
Day 4: "체험 3일 남았습니다. 맞춤 알림을 설정해보셨나요?"
Day 6: "내일 체험이 종료됩니다. 구독하시면 끊김 없이 계속 이용하실 수 있습니다."
```

> **주의**: Day 7에는 푸시 보내지 않음. 자동 결제 유도 (해지 리마인드 방지).
> **권장**: Phase 3 MVP에서는 이 기능 구현하지 않음. 마케팅 옵트인 시스템 구축 후 도입 검토.

#### 8.2 iOS 자동 결제 동작

1. 사용자가 "연간 구독 시작" 탭 → Face ID/Touch ID 인증 → **결제 정보 등록됨**
2. 7일 무료 체험 시작
3. 체험 종료 **24시간 전**까지 해지 안 하면 → **99,000원 자동 결제**
4. Apple이 체험 종료 전 시스템 알림 발송 (설정 > Apple ID > 구독)

### 9. 마케팅 전략

#### 9.1 잠재 사용자 풀

| 채널 | 규모 | 특성 |
|------|------|------|
| 카카오톡 오픈채팅방 A | ~800명 | 환전 투자 관심자 |
| 카카오톡 오픈채팅방 B | ~800명 | 중복 있음 |
| **추정 순 사용자** | ~1,000~1,200명 | 10~15% 유료 전환 기대 |

#### 9.2 바이럴 전략

```
1. 자연스러운 공유: "이 앱 써봤는데 좋더라"
2. 스크린샷 각인 효과: 메인화면 은행별 막대그래프
3. 인베스팅 먹통 시 가치 극대화: "아침 9시에 인베스팅 안 될 때 이거 씀"
```

#### 9.3 앱스토어 ASO

**키워드**: 환율, 실시간 환율, 은행 환율, 환율 비교, 환전, 달러 환율

**스크린샷**: 은행별 비교 UI, 그래프, 알림 설정 화면

### 10. 성공 지표 (KPI)

| 지표 | 목표 | 측정 방법 |
|------|------|----------|
| **무료 체험 시작** | 300~500명 | RevenueCat Dashboard |
| **체험→유료 전환율** | 40~50% | 자동 결제 (해지 안 함) |
| **유료 구독자** | 100~200명 | RevenueCat Dashboard |
| **연간 구독 비율** | 70~80% | 월간 대비 연간 비율 |
| **월간 이탈률** | < 10% | RevenueCat Churn Rate |
| **평균 LTV** | > 50,000원 | RevenueCat Analytics |

### 11. 구현 체크리스트 (Phase 3)

#### App Store Connect

- [ ] 구독 그룹 "FXi Premium" 생성
- [ ] 월간 상품 등록 (`fxi_premium_monthly`, ₩12,000)
- [ ] 연간 상품 등록 (`fxi_premium_yearly`, ₩99,000, 7일 무료 체험)
- [ ] App Store Connect Shared Secret 복사

#### RevenueCat

- [ ] RevenueCat 계정 생성
- [ ] iOS 앱 등록 (Bundle ID, Shared Secret)
- [ ] Products 등록 (월간, 연간)
- [ ] Entitlements 생성 ("premium")
- [ ] Offerings 설정 ("default")
- [ ] Public API Key 복사

#### iOS 앱

- [ ] RevenueCat SDK 설치 (SPM)
- [ ] AppDelegate에서 SDK 초기화
- [ ] AuthService에 RevenueCat 로그인/로그아웃 연동
- [ ] SubscriptionManager 서비스 구현
- [ ] PaywallView UI 구현
- [ ] RootView에 Hard Paywall 적용
- [ ] 구매 복원 기능 구현
- [ ] 약관 링크 추가 (개인정보처리방침, 이용약관)

#### 테스트

- [ ] Sandbox 테스터 계정 생성
- [ ] 월간 구독 테스트 (체험 없이 바로 결제)
- [ ] 연간 구독 테스트 (7일 체험 → 자동 결제)
- [ ] 구매 복원 테스트
- [ ] 구독 해지 테스트

---

---

### Phase 4: RDS PostgreSQL 마이그레이션 (완료)

**완료 시점**: 2026-01-13

> ✅ **완료**: RDS 전환 및 EC2 업그레이드 완료. SQLite 데이터 이관 없음.

| 작업 | 설명 |
|------|------|
| ✅ RDS 인스턴스 생성 | db.t4g.micro (프리티어, ARM64) |
| ✅ VPC 설정 | EC2와 같은 네트워크 |
| ✅ Alembic 마이그레이션 실행 | 빈 스키마 생성 (SQLite 이관 안 함) |
| ✅ EC2 업그레이드 | t3.small |
| ✅ 통합 리허설 | Alembic 업/다운, E2E 테스트 |
| ✅ 내부 테스터 안내 | 앱 재설치 공지 발송 |

**비용**: EC2 $15/월 + RDS $0 (프리티어 12개월)

#### 🔄 전환 전략: 데이터 드롭 방식

**기본 전략 (권장)**: Phase 2-3 개발 중 SQLite 사용, Phase 4에서 RDS로 전환 시 내부 테스트 데이터 드롭 (완료)

```text
왜 데이터를 드롭하는가?
├── 내부 테스터 데이터 (10-20명): 복구 비용보다 재설치가 간단
├── 프리티어 12개월 최대 활용: 실제 서비스 시작 시점부터 시작
├── 깔끔한 시작: 테스트 중 발생한 더미 데이터 제거
└── Firebase Auth uid는 유지됨 (재로그인 불필요)
```

**대안 (엄격)**: Phase 2 시작 전 RDS 전환 → 모든 개발을 PostgreSQL에서 진행
- 장점: 환경 차이 없음
- 단점: 프리티어 조기 소모, 로컬 개발 복잡

#### 📋 데이터 드롭 절차

**1. 서버 측 작업**

```bash
# 1. EC2 업그레이드 (t3.small 완료)
# AWS 콘솔에서 인스턴스 유형 변경

# 2. RDS PostgreSQL 생성 (새 DB, SQLite 이관 안 함, 완료)
# AWS 콘솔에서 db.t4g.micro 생성

# 3. 환경변수 변경
# .env
DATABASE_URL=postgresql://user:pass@rds-endpoint:5432/fxi

# 4. Alembic 마이그레이션 (빈 스키마 생성)
alembic upgrade head

# 5. Docker 재배포
docker compose up -d --build
```

**2. 내부 테스터 공지 (템플릿)**

```text
[FXi 베타 테스트 안내]

외부 베타 전환을 위해 서버가 업그레이드됩니다.

📱 필요한 조치:
1. 기존 FXi 앱 삭제
2. 테스트플라이트에서 최신 버전 재설치
3. 다시 로그인 (Firebase 계정 유지됨)

⚠️ 기존 알림 설정은 초기화됩니다.
새 버전에서 다시 설정해 주세요.

적용 일시: YYYY-MM-DD HH:MM
문의: [연락처]
```

**3. 영향 범위**

| 항목 | 드롭 여부 | 복구 방법 |
|------|----------|----------|
| Firebase Auth uid | ❌ 유지 | Firebase 서버에 저장 |
| FCM device_token | ✅ 드롭 | 앱 재설치 시 자동 재발급 |
| notification_settings | ✅ 드롭 | 사용자가 다시 설정 |
| notification_logs | ✅ 드롭 | 히스토리 초기화 |
| 환율 데이터 | ✅ 드롭 | SQLite 이관 없음 (신규 DB) |

---

### Phase 5: 심사 + iOS 출시

**목표**: 앱스토어 심사 → 출시 (외부 베타 생략)

| 작업 | 설명 |
|------|------|
| 테스트플라이트 외부 베타 | **미진행 (생략)** |
| 버그 수정 및 안정화 | 크래시 프리 세션 > 99% 목표 |
| 앱스토어 심사 제출 | 2026-01-15 오전 제출 완료 |
| 로그 기반 알람 구현 | 알림 실패율 모니터링 (선택) |

**완료 게이트**:
- 앱스토어 심사 승인
- 크래시 프리 세션 > 99%
- 주요 버그 0건

---

### Phase 6: Android 개발

**목표**: 크로스 플랫폼 완성

| 작업 | 설명 |
|------|------|
| Kotlin + Jetpack Compose | UI 구현 |
| Firebase Auth/FCM SDK | iOS와 동일 플로우 |
| RevenueCat SDK | 구독 동기화 자동 처리 |
| Google Play Billing | RevenueCat이 처리 |

**완료 게이트**:
- Play 스토어 심사 승인
- iOS ↔ Android 크로스 플랫폼 동기화 확인

---

### 타임라인 요약

```text
[현재] Phase 4 완료
   └── t3.small + RDS 운영 중 (UTC 저장, KST 출력)

[Phase 2] Firebase Auth + FCM
   └── 서버 API 추가, 알림 기능, Alembic 준비

[Phase 3] RevenueCat
   └── 구독 시스템 완성

[Phase 4] RDS 마이그레이션
   └── 완료 (SQLite 이관 없음)

[Phase 5] 심사 + iOS 출시
   └── 앱스토어 심사 대기 → 출시

[Phase 6] Android
   └── 크로스 플랫폼 완성
```

---

### 📊 Phase 매핑 표

> **참고**: 로드맵과 체크리스트의 Phase 번호가 1:1로 매핑됩니다.

| Phase | 목표 | 주요 작업 | 완료 게이트 | 인프라 |
|-------|------|----------|-------------|--------|
| 1 | iOS MVP ✅ | UI, API 연동, WebSocket | 앱 기본 기능 동작 | 이전: t2.micro + SQLite |
| 2 | Firebase Auth + FCM ✅ | 로그인, 푸시 알림, 알림 API | 푸시 알림 E2E 성공, 구조화 로그 준비 | 이전: t2.micro + SQLite |
| 3 | RevenueCat ✅ | 구독 결제, 유료 기능 | 구매 → 권한 반영 ≤ 1분 | 이전: t2.micro + SQLite |
| 4 | RDS 마이그레이션 ✅ | DB 전환, 인프라 업그레이드 | Alembic 성공, 통합 E2E 통과 | t3.small + RDS |
| 5 | 심사 + iOS 출시 | 심사, QA | 앱스토어 승인, 크래시 프리 > 99% | t3.small + RDS |
| 6 | Android | Kotlin UI, SDK 통합 | Play 스토어 승인 | t3.small + RDS |

---

## 🔔 개인화 알림 동작 흐름

### 요구사항

```text
사용자: "하나은행 USD 환율이 1475원 이상이 되면 알려줘"
→ 환율 1478원 도달 시 푸시 알림 수신
```

### Step 1: 사용자 로그인 (앱 최초 실행)

```text
[사용자가 앱 설치]
    ↓
[Firebase Auth 로그인 (이메일/소셜)]
    ↓
[Firebase가 ID Token 발급] ← JWT 형태, user_id 포함
    ↓
[FCM Device Token 획득: "xyz789"]
    ↓
[서버에 등록: POST /api/register-device]
    Headers: { "Authorization": "Bearer <Firebase ID Token>" }  ← 필수!
    Body: { "device_token": "xyz789", "platform": "ios" }
    ↓
[서버에서 Firebase Admin SDK로 ID Token 검증]
    → 검증 성공 시 토큰에서 user_id 추출: "abc123"
    → 검증 실패 시 401 Unauthorized
    ↓
[RDS PostgreSQL에 저장]
    Table: user_devices
    | user_id | device_token | platform | created_at | updated_at |
    |---------|--------------|----------|------------|-----------|
    | abc123  | xyz789       | ios      | 2025-12-05 | 2025-12-05 |
```

**보안 핵심:**

- 클라이언트가 보낸 user_id를 **절대 신뢰하지 않음**
- 서버가 Firebase ID Token을 검증 후 **직접 user_id 추출**
- device_token은 **전역 유니크** → 다른 계정 로그인 시 **소유권 자동 이전(UPSERT)** 로직
- 이렇게 해야 스푸핑 공격 방지 + 중복 토큰 문제 방지 가능

### Step 2: 알림 설정 저장

```text
[사용자가 앱에서 알림 설정]
    - 은행: 하나은행
    - 통화: USD
    - 조건: 1475원 이상
    - 활성화: ON (또는 OFF로 생성 가능)
    ↓
[서버에 저장: POST /api/notification-settings]
    Headers: { "Authorization": "Bearer <Firebase ID Token>" }  ← 필수!
    Body: {
        "bank": "hana",
        "currency": "usd-krw",
        "condition": "above",
        "threshold": 1475.0,
        "is_enabled": true
    }
    ↓
[서버에서 ID Token 검증 → user_id 추출: "abc123"]
    ↓
[RDS PostgreSQL에 저장]
    Table: notification_settings
    | user_id | bank | currency | condition | threshold | enabled | triggered |
    |---------|------|----------|-----------|-----------|---------|-----------|
    | abc123  | hana | usd-krw  | above     | 1475.0    | true    | false     |
```

**is_enabled 필드:**
- 타입: `bool` (기본값 `true`, 생략 가능)
- `null` 전송 시 422 에러 (Pydantic 유효성 검사 실패)

**중복 처리 정책:**
- 동일 조건 (bank + currency + condition + threshold) 알림 존재 시:
  - 새로 생성하지 않고 기존 설정의 `enabled` 상태만 업데이트
  - `is_enabled=true` → `triggered=false` 초기화 (재알림 가능)
  - `is_enabled=false` → `triggered` 유지 ("발송됨" 상태 보존)

**보안 핵심:** 클라이언트는 user_id를 보내지 않음. 서버가 ID Token에서 직접 추출.

### Step 3: 크롤러 → 조건 체크 → FCM 전송

```text
[크롤러가 하나은행 USD 환율 크롤링]
    Before: 1470원
    After:  1478원 ← 변경 감지!
    ↓
[CRUD에서 조건 체크]
    SELECT * FROM notification_settings
    WHERE bank='hana' AND currency='usd-krw'
    ↓
[조건 충족 사용자 찾기]
    user_id: abc123, threshold: 1475.0
    1478 >= 1475? → YES!
    ↓
[FCM API 호출]
    device_token: "xyz789"
    title: "환율 알림"
    body: "하나은행 USD 환율이 1478원이 되었습니다"
    ↓
[사용자 기기에 푸시 알림 도착]
    🔔 "환율 알림: 하나은행 USD 1478원"
```

### 필요한 DB 테이블 (RDS PostgreSQL)

```sql
-- 1. 사용자 기기 정보
CREATE TABLE user_devices (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,           -- Firebase Auth user_id
    device_token TEXT NOT NULL UNIQUE, -- FCM Device Token (전역 유니크)
    platform TEXT NOT NULL,           -- 'ios' or 'android'
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 2. 알림 설정
CREATE TABLE notification_settings (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    bank TEXT NOT NULL,               -- 'hana', 'kb', etc.
    currency TEXT NOT NULL,           -- 'usd-krw', etc.
    condition TEXT NOT NULL,          -- 'above', 'below'
    threshold REAL NOT NULL,          -- 1475.0
    enabled BOOLEAN DEFAULT TRUE,
    triggered BOOLEAN DEFAULT FALSE,  -- 중복 알림 방지
    last_notified_at TIMESTAMP NULL,
    last_notified_rate REAL NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 3. 알림 히스토리 (중복 방지)
CREATE TABLE notification_logs (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    setting_id INTEGER NULL,          -- NotificationSetting.id (선택)
    bank TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate REAL NOT NULL,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT NULL,
    sent_at TIMESTAMP DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_user_devices_user ON user_devices(user_id);
CREATE INDEX idx_notification_settings_bank_currency
    ON notification_settings(bank, currency);
```

### 핵심 포인트

1. **로그인 필수**: 개인화 알림은 user_id로 설정을 저장해야 함
2. **Device Token**: FCM이 "어느 기기로 보낼지" 식별
3. **우리 DB에 저장**: user_devices, notification_settings는 RDS PostgreSQL에 저장
4. **Firebase Auth는 외부**: 로그인만 담당, user_id만 제공

### Source 기반 알림 (USDT exchange + KRX derivative) — 별 API endpoint

위 흐름은 `bank + currency` 기반 legacy 알림 (`/api/notification-settings`). USDT/KRX는 `source + asset` 도메인 모델 차이로 **별 API endpoint** 사용:

| 도메인 | API endpoint | Body 예시 | FCM payload type |
|---|---|---|---|
| Bank/Investing (legacy) | `/api/notification-settings` | `{bank, currency, condition, threshold}` | `rate_alert` |
| USDT exchange | `/api/source-notification-settings` | `{source:"upbit", asset:"usdt-krw", condition, threshold}` | `source_rate_alert` |
| KRX derivative (F-2 2026-05-26 land) | `/api/source-notification-settings` | `{source:"krx", asset:"usd-krw-futures", condition, threshold}` | `source_rate_alert` |

서버 검증 (`source_registry.validate_alert_source_asset`): `phase1_enabled=True` + `category in {"exchange", "derivative"}` 통과만 허용. reference 소스 (investing/kb/hana)는 차단 (기존 `/api/notification-settings` 사용).

KRX 알림 발송은 별 축인 `KRX_ALERT_EVALUATOR_ENABLED` env (F-3) 활성 필요. F-2 land ~ F-3 활성 사이는 의도된 canary staging gap — API 등록 가능 + 발송 안 됨. 운영 단말 영향 0 (테더 탭 자체가 운영 앱에 없음, 테스트 iOS canary 전용).

상세 client 통합 가이드: [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md) (RateSource / SourceRegistry / FCM type 분기). 운영 절차/canary runbook: [KRX_CANARY.md F-3 섹션](KRX_CANARY.md). 의사결정 기록: [ADR-032](DECISIONS.md#adr-032-krx-가격알림-evaluator--source-neutral-재사용--krx-adapter--validate-helper-분리).

---

## 1. 환율 알림 서비스 구현 방법

### 요구사항
```
사용자가 특정 은행의 환율이 N원 이상 변동하면 푸시 알림 수신
예: "하나은행 USD 환율이 1469원 → 1470원으로 변경되었습니다"
```

---

### Option A: Firebase + FCM

**아키텍처:**
```
크롤러 → PostgreSQL (서비스) / SQLite (개발) → CRUD (변화 감지) → FCM → iOS/Android
```

**구현:**
```python
# CRUD 레이어에서 변화 감지 시
if last_record.rate != current_rate:
    await send_fcm_notification(bank, currency, old_rate, new_rate)
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 푸시 알림 | ✅ FCM 완전 무료 (무제한) |
| 구현 난이도 | ✅ 간단 (Python SDK 제공) |
| iOS/Android | ✅ 통합 지원 (APNs 래핑) |
| 배터리 효율 | ✅ OS 레벨 최적화 |
| 인증 연동 | ✅ Firebase Auth 통합 쉬움 |
| 벤더 락인 | ⚠️ Google 의존 |
| DB | ⚠️ Firestore(NoSQL) 또는 별도 PostgreSQL 필요 |

---

### Option B: Supabase + FCM

**아키텍처 B-1: FastAPI 서버 유지**
```
크롤러 → PostgreSQL(Supabase) → CRUD → FCM → iOS/Android
```

**아키텍처 B-2: Edge Functions (서버리스)**
```
크롤러 → PostgreSQL(Supabase) → DB Webhook → Edge Function → FCM
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 푸시 알림 | ✅ FCM 무료 (Firebase와 동일) |
| DB | ✅ PostgreSQL 기본 제공 (현재 프로젝트 Phase 2 계획과 일치) |
| 오픈소스 | ✅ 자체 호스팅 가능 (벤더 락인 없음) |
| Edge Functions | ✅ 무료 500k 요청/월 (서버리스 알림 가능) |
| 구현 난이도 | ⚠️ Edge Functions는 Deno/TypeScript (Python 아님) |
| 생태계 | ⚠️ Firebase보다 작음 (성장 중) |

---

### Option C: 자체 구현 (FCM만 사용)

**아키텍처:**
```
크롤러 → PostgreSQL → CRUD → 자체 알림 로직 → FCM
```

**특징:**
- Firebase/Supabase 없이 FCM만 직접 사용
- 모든 로직을 FastAPI에서 처리

**장단점:**
| 항목 | 평가 |
|------|------|
| 완전 제어 | ✅ 모든 로직 커스터마이징 가능 |
| 비용 | ✅ FCM 무료 + PostgreSQL만 |
| 구현 난이도 | ⚠️ 중간 (사용자 관리, 토큰 관리 직접 구현) |
| 인증 | ⚠️ 별도 인증 시스템 필요 |

---

### 환율 알림 방법 비교표

| 항목 | Firebase + FCM | Supabase + FCM | 자체 구현 |
|------|---------------|---------------|----------|
| **푸시 알림** | FCM (무료) | FCM (무료) | FCM (무료) |
| **DB** | Firestore 또는 별도 | PostgreSQL 포함 | PostgreSQL (별도) |
| **인증** | Firebase Auth | Supabase Auth | 직접 구현 |
| **서버리스 옵션** | Cloud Functions | Edge Functions | ❌ |
| **오픈소스** | ❌ | ✅ | ✅ |
| **벤더 락인** | Google | Supabase (탈출 가능) | 없음 |
| **구현 시간** | 1주 | 1-2주 | 2-3주 |
| **비용 (500명)** | $0 | $0 | $0 |
| **비용 (1000명)** | $15/월 | $25/월 | $25/월 |

---

## 2. 크로스 플랫폼 구독 관리 방법

### 요구사항
```
iOS에서 구독 → Android에서 로그인 → 자동 동기화 (다시 결제 안 함)
```

### 핵심 개념
```
구독 관리 = 사용자 인증 + 영수증 검증 + 크로스 플랫폼 동기화

1. 사용자 인증: Firebase Auth 또는 Supabase Auth
2. 영수증 검증: RevenueCat 또는 자체 구현
3. 동기화: 서버 기준으로 user_id로 구독 상태 관리
```

---

### Option A: Firebase Auth + RevenueCat ⭐

**아키텍처:**
```
[iOS/Android 앱]
    ↓ Firebase Auth 로그인 → user_id 획득
    ↓ RevenueCat에 user_id 연결
[RevenueCat]
    ↓ 영수증 검증 (iOS/Android)
    ↓ 크로스 플랫폼 자동 동기화 ✅
[서버] (선택적) Webhook으로 구독 상태 저장
```

**구현 복잡도:**
```swift
// iOS
FirebaseAuth.auth().signIn(...) { result in
    let userId = result?.user.uid
    Purchases.shared.logIn(userId) { ... }  // RevenueCat 연결
}

// Android (같은 user_id로 자동 동기화!)
FirebaseAuth.getInstance().signIn(...) { result ->
    val userId = result.user?.uid
    Purchases.sharedInstance.logIn(userId) { ... }
}
```

**장단점:**
| 항목 | 평가 |
|------|------|
| 구현 난이도 | ✅ 매우 쉬움 (3-5줄 코드) |
| 크로스 플랫폼 | ✅ 자동 동기화 |
| 영수증 검증 | ✅ 자동 (iOS + Android) |
| 환불/취소 | ✅ Webhook 자동 처리 |
| 분석 | ✅ MRR, Churn, LTV 대시보드 |
| 비용 | ⚠️ 매출의 1% (월 $2,500 이하 무료) |
| 생태계 | ✅ Notion, Duolingo 등 사용 |

---

### Option B: Supabase Auth + RevenueCat ⭐

**아키텍처:**
```
[iOS/Android 앱]
    ↓ Supabase Auth 로그인 → user_id 획득
    ↓ RevenueCat에 user_id 연결
[RevenueCat]
    ↓ 영수증 검증 + 크로스 플랫폼 동기화 ✅
```

**Firebase와의 차이점:**
- Auth만 Supabase로 변경
- RevenueCat 사용법은 동일

**장단점:**
| 항목 | 평가 |
|------|------|
| 구현 난이도 | ✅ Firebase와 동일 |
| 크로스 플랫폼 | ✅ 자동 동기화 |
| 비용 | ✅ Firebase와 동일 |
| DB | ✅ PostgreSQL 포함 |
| 오픈소스 | ✅ 자체 호스팅 가능 |

---

### Option C: 자체 구현 (영수증 검증)

**아키텍처:**
```
[iOS/Android 앱]
    ↓ 구독 완료 → 영수증 전송
[서버]
    ↓ App Store API 검증 (iOS)
    ↓ Google Play API 검증 (Android)
    ↓ DB 저장 (user_id 기준)
[Cron Job]
    ↓ 매일 구독 갱신 상태 확인
```

**구현 복잡도:**
- iOS 영수증 검증: App Store API (복잡)
- Android 영수증 검증: Google Play API (매우 복잡, OAuth 필요)
- 자동 갱신 확인: Cron Job 필요
- 환불/취소 처리: 수동 또는 Webhook 구현
- 엣지 케이스: Offer Code, 가족 공유, Grace Period 등 (10-20개)

**장단점:**
| 항목 | 평가 |
|------|------|
| 비용 | ✅ RevenueCat 매출 1% 절약 |
| 완전 제어 | ✅ 모든 로직 커스터마이징 |
| 구현 난이도 | ❌ 매우 높음 (3-4주) |
| 유지보수 | ❌ Apple/Google API 변경 대응 필요 |
| 실질 비용 | ⚠️ 개발 + 유지보수 고려 시 RevenueCat보다 비쌈 |

---

### 구독 관리 방법 비교표

| 항목 | Firebase + RevenueCat | Supabase + RevenueCat | 자체 구현 |
|------|----------------------|----------------------|----------|
| **인증** | Firebase Auth | Supabase Auth | 직접 구현 |
| **영수증 검증** | RevenueCat | RevenueCat | 직접 구현 |
| **크로스 플랫폼** | ✅ 자동 | ✅ 자동 | ⚠️ 수동 |
| **환불/취소** | ✅ 자동 | ✅ 자동 | ❌ 수동 |
| **구현 시간** | 1주 | 1주 | 3-4주 |
| **비용 (월 매출 $1k)** | $0 | $0 | $0 |
| **비용 (월 매출 $10k)** | $100 | $100 | $500 (유지보수) |
| **Apple 정책** | ✅ | ✅ | ✅ |
| **추천도** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⚠️ |

---

## 3. 비용 비교 시나리오

> **참고**: 아래는 검토 당시 비교 자료입니다. 최종 선택은 **AWS RDS PostgreSQL + Firebase Auth + FCM**입니다.
> 선택 이유: RDS 프리티어 12개월 무료, 같은 VPC 네트워크 지연 최소화 (1-3ms), 기존 AWS 인프라 활용, FCM과 자연스러운 통합

### 시나리오 1: 초기 (사용자 500명, 월 매출 $1,000)

| 항목 | Firebase + RevenueCat | Supabase + RevenueCat | 자체 구현 |
|------|----------------------|----------------------|----------|
| 인증 | $0 (무료) | $0 (무료) | $0 |
| DB | $0 (Firestore 무료) | $0 (PostgreSQL 500MB) | $0 (SQLite) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $0 (매출 $2.5k 이하) | $0 (매출 $2.5k 이하) | $0 |
| **월 합계** | **$0** | **$0** | **$0** |
| **개발 시간** | 2주 | 2주 | 4주 |

**결론:** 초기에는 비용 동일, **Supabase가 PostgreSQL 제공으로 약간 우세**

---

### 시나리오 2: 성장 (사용자 1,000명, 월 매출 $10,000)

| 항목 | Firebase + RevenueCat | Supabase + RevenueCat | 자체 구현 |
|------|----------------------|----------------------|----------|
| 인증 | $0 | $0 | $0 |
| DB | $15 (Firestore) | $25 (PostgreSQL Pro 8GB) | $25 (RDS t3.micro) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $100 (매출의 1%) | $100 (매출의 1%) | $0 |
| Edge Functions | $10 (Cloud Functions) | $0 (무료 500k) | - |
| **월 합계** | **$125** | **$125** | **$25** |
| **유지보수** | $0 | $0 | $500 (버그 수정, API 대응) |
| **실질 비용** | **$125** | **$125** | **$525** |

**결론:** Firebase와 Supabase 비용 동일, **자체 구현은 유지보수 고려 시 더 비쌈**

---

### 시나리오 3: 대규모 (사용자 5,000명, 월 매출 $100,000)

| 항목 | Firebase + RevenueCat | Supabase + RevenueCat | Supabase 자체 호스팅 |
|------|----------------------|----------------------|-------------------|
| 인증 | $55 (50k MAU 초과) | $25 | $0 (자체) |
| DB | $100 (Firestore) | $100 | $30 (RDS t3.small) |
| 푸시 알림 | $0 (FCM) | $0 (FCM) | $0 (FCM) |
| 구독 관리 | $1,000 (매출의 1%) | $1,000 | $1,000 |
| 서버 | - | - | $35 (EC2 t3.medium) |
| **월 합계** | **$1,155** | **$1,125** | **$1,065** |

**결론:** **Supabase + RevenueCat이 가장 저렴** ($1,125/월)

---

## 4. 구현 난이도 및 기간

### 환율 알림 서비스

| 방법 | 난이도 | 개발 기간 | 유지보수 |
|------|-------|----------|---------|
| Firebase + FCM | ⭐⭐ (쉬움) | 1주 | 낮음 |
| Supabase + FCM (FastAPI) | ⭐⭐ (쉬움) | 1-2주 | 낮음 |
| Supabase + Edge Functions | ⭐⭐⭐ (중간) | 2주 | 낮음 |
| 자체 구현 | ⭐⭐ (쉬움) | 2-3주 | 중간 |

---

### 크로스 플랫폼 구독 관리

| 방법 | 난이도 | 개발 기간 | 유지보수 |
|------|-------|----------|---------|
| Firebase + RevenueCat | ⭐ (매우 쉬움) | 1주 | 매우 낮음 |
| Supabase + RevenueCat | ⭐ (매우 쉬움) | 1주 | 매우 낮음 |
| 자체 구현 | ⭐⭐⭐⭐⭐ (매우 어려움) | 3-4주 | 매우 높음 |

---

## 5. 다음 단계

### ✅ 결정된 항목

#### A. 인증 & DB 플랫폼

- [x] **AWS RDS PostgreSQL + Firebase Auth** 선택
  - DB: AWS RDS PostgreSQL 프리티어 (db.t4g.micro)
  - Auth: Firebase Auth (무료 50k MAU)
  - 이유: 같은 VPC 지연 1-3ms, 프리티어 12개월, FCM 통합 용이

#### B. 구독 관리 방식

- [x] **Firebase Auth + RevenueCat** 선택
  - 이유: Firebase uid와 자연스러운 통합, 크로스 플랫폼 자동 동기화
  - 비용: 월 매출 $2,500 이하 무료, 이후 매출의 1%

#### C. 환율 알림 아키텍처

- [x] **FastAPI 서버 유지** 선택
  - Python 코드베이스 유지
  - CRUD 레이어에서 변화 감지 → FCM 전송

---

### 📋 구현 체크리스트

> **참고**: Phase 번호는 위 [Phase 매핑 표](#-phase-매핑-표)와 1:1 대응됩니다.

#### Phase 1: iOS MVP ✅ 완료

- [x] UI/UX 구현 (환율 비교, 그래프, 은행 목록)
- [x] REST API 연동
- [x] WebSocket 실시간 업데이트

#### Phase 2: Firebase Auth + FCM

**Apple Developer 설정:**

- [x] APNs 키 생성 (.p8, Sandbox & Production, Team Scoped)
- [x] App ID 등록 (Push Notifications, Sign in with Apple 활성화)
- [x] 서비스 ID 생성 (Android Apple 로그인용)
- [x] Sign in with Apple 키 생성 (.p8, APNs 키와 별도)

**Firebase Console 설정:**

- [x] Firebase 프로젝트 생성 (FXi)
- [x] iOS 앱 등록 + GoogleService-Info.plist 다운로드
- [x] APNs 키 업로드 (개발/프로덕션)
- [x] Google 로그인 활성화 + GoogleService-Info.plist 재다운로드
- [x] Apple 로그인 활성화 + OAuth 설정 (Android용)

**iOS 앱 작업:**

- [x] Xcode에 Firebase SDK 추가 (SPM)
- [x] GoogleService-Info.plist 프로젝트에 추가
- [x] Capabilities 설정 (Push Notifications, Sign in with Apple)
- [x] Firebase Auth SDK 통합 (Google + Apple 로그인) - `AuthService.swift`
- [x] FCM SDK 통합 (Device Token 등록) - `PushNotificationService.swift`
- [x] 로그인 UI 구현

**서버 작업:**

- [x] Firebase Admin SDK 설치 (`firebase-admin>=6.5.0`)
- [x] user_devices, notification_settings, notification_logs 테이블 생성
- [x] /api/register-device API 구현 (토큰 소유권 이전 로직 포함)
- [x] /api/notification-settings API 구현 (중복 방지, triggered 재설정)
- [x] CRUD 변화 감지 → FCM 전송 로직 구현
- [x] 알림 품질 게이트 통과 ([상세](#-알림-품질-게이트))

#### Phase 3: RevenueCat

- [x] RevenueCat 도입 결정
- [x] RevenueCat 계정 생성 및 앱 등록
- [x] iOS 앱에 RevenueCat SDK 통합
- [x] 유료 기능 설계 (프리미엄 알림 등)
- [x] Firebase uid ↔ RevenueCat 연동

#### Phase 4: RDS 마이그레이션

- [ ] 로컬 스테이징 리허설 통과 ([체크리스트](#-통합-리허설-체크리스트))
- [x] EC2 t3.small 업그레이드 완료
- [x] RDS PostgreSQL 인스턴스 생성 (db.t4g.micro, 운영 중)
- [x] VPC 설정 (EC2와 같은 네트워크)
- [x] Alembic 마이그레이션 스크립트 준비 및 실행 (운영 스키마 적용)
- [x] DATABASE_URL 환경변수 변경 (RDS 연결)
- [x] 내부 테스터 앱 재설치 공지 발송 (해당 없음: 개발자 단독 테스트)
- [x] 통합 E2E 테스트 통과 (운영 서버 사용자 흐름 테스트 완료)

#### Phase 5: 심사 + iOS 출시

- [ ] 테스트플라이트 외부 베타 배포 (미진행, 생략)
- [ ] 베타 피드백 수집 및 버그 수정 (외부 베타 생략으로 제외)
- [ ] 크래시 프리 세션 > 99% 확인
- [x] 앱스토어 심사 제출 (2026-01-15 오전)
- [ ] 앱스토어 승인
- [ ] (선택) 로그 기반 알람 구현

#### Phase 6: Android 개발

- [ ] Kotlin + Jetpack Compose UI 구현
- [ ] Firebase Auth SDK 통합
- [ ] FCM SDK 통합
- [ ] RevenueCat SDK 통합
- [ ] 크로스 플랫폼 구독 동기화 테스트
- [ ] Play 스토어 심사 제출 및 승인

---

### 💡 비용 전략 (확정)

> **참고**: 아래 "비용 단계"는 구현 Phase와 다릅니다. 서비스 운영 기간 기준입니다.

```text
비용 단계 1 (서비스 시작 ~ 12개월):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: $0 (프리티어)
├── Firebase Auth/FCM: $0
└── 합계: $15/월

비용 단계 2 (12개월 후):
├── EC2 t3.small: $15/월
├── RDS db.t4g.micro: ~$15/월
├── Firebase Auth/FCM: $0
└── 합계: $30/월

비용 단계 3 (1,000명+):
├── EC2 t3.medium: $30/월
├── RDS db.t4g.small: ~$25/월
├── Firebase Auth/FCM: $0
└── 합계: $55/월
```

---

## 🧪 통합 리허설 체크리스트

> **목적**: RDS 전환 전 프로덕션 환경과 동일한 조건에서 검증
> **시점**: Phase 4 (RDS 마이그레이션) 시작 전

### 로컬 스테이징 환경 구성

프로덕션 이미지/환경변수와 동일하게 로컬에서 테스트합니다.

> **환경변수 파일**: `.env.staging`은 프로덕션 `.env`와 동일한 키 세트를 사용하고, 값만 스테이징용으로 변경합니다.

```yaml
# docker-compose.staging.yml
version: '3.8'
services:
  app:
    build: .
    platform: linux/amd64  # M1 Mac에서도 x86 에뮬레이션
    environment:
      - ENV=staging
      - DATABASE_URL=postgresql://postgres:dev@postgres:5432/fxi
      - REDIS_URL=redis://redis:6379
      # 프로덕션 .env와 동일한 키 사용
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_DB=fxi
      - POSTGRES_PASSWORD=dev
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

### 리허설 체크리스트

```bash
# 1. 스테이징 환경 실행
docker-compose -f docker-compose.staging.yml up -d --build

# 2. Alembic 마이그레이션 테스트
alembic upgrade head
alembic downgrade -1  # 롤백 테스트
alembic upgrade head  # 다시 업그레이드
```

| 항목 | 테스트 내용 | 통과 기준 |
|------|------------|----------|
| ✅ Alembic 업그레이드 | `alembic upgrade head` | 에러 없이 완료 |
| ✅ Alembic 롤백 | `alembic downgrade -1` | 에러 없이 롤백 |
| ✅ 크롤러 동시 INSERT | 10개 크롤러 동시 실행 | UNIQUE 충돌 없음 |
| ✅ WebSocket 브로드캐스트 | 환율 데이터 실시간 전송 | 클라이언트 수신 확인 |
| ✅ 알림 E2E | 조건 충족 → FCM 전송 | 푸시 알림 수신 (실 토큰 1회) |
| ✅ Redis 캐시 | 브로드캐스트 캐시 동작 | Circuit Breaker 정상 |

### 환경 차이 주의사항

| 차이점 | 로컬 | 프로덕션 | 대응 |
|--------|------|----------|------|
| CPU 아키텍처 | M1 Mac (ARM) | EC2 (x86) | `--platform linux/amd64` |
| TLS | 없음 | Let's Encrypt | 스테이징에서는 HTTP |
| 네트워크 지연 | localhost | VPC 1-3ms | 실제 영향 미미 |

---

## 🔔 알림 품질 게이트

> **목적**: 알림 서비스의 최소 품질 보증
> **적용 시점**: Phase 2 (Firebase Auth + FCM) 구현 시

### 1. 멱등성 (중복 알림 방지)

**방식**: `triggered` 플래그로 단일 조건 기준 중복 방지

```python
# notification_settings 테이블
class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False)
    bank = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    condition = Column(String, nullable=False)  # 'above', 'below'
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True)

    # 멱등성 필드
    triggered = Column(Boolean, default=False)  # 조건 충족 여부
    last_notified_at = Column(DateTime, nullable=True)
    last_notified_rate = Column(Float, nullable=True)
```

```python
# 알림 로직 (1회성 알림)
async def check_and_notify(bank: str, currency: str, new_rate: float):
    settings = await get_notification_settings(bank, currency)

    for s in settings:
        # enabled=False 또는 triggered=True면 스킵
        if not s.enabled or s.triggered:
            continue

        condition_met = (
            (s.condition == 'above' and new_rate >= s.threshold) or
            (s.condition == 'below' and new_rate <= s.threshold)
        )

        if condition_met:
            # 알림 발송
            await send_fcm_notification(s.user_id, bank, currency, new_rate)
            # 1회성 알림: 자동 비활성화
            s.triggered = True
            s.enabled = False  # ← 핵심: 발송 후 자동 비활성화
            s.last_notified_at = datetime.now()
            s.last_notified_rate = new_rate
            await db.commit()
```

**1회성 알림 동작:**
- 알림 발송 시: `triggered=True`, `enabled=False` (자동 비활성화)
- 재알림 받으려면: 사용자가 토글 ON (`PUT /api/notification-settings/{id}` with `is_enabled: true`)
- 토글 ON 시: `triggered=False`, `enabled=True`로 초기화

> **확장 계획**: 다중 조건/프리미엄 기능 추가 시 `triggered` 플래그를 별도 테이블 (`notification_states`)로 분리하거나, `notification_logs` 기반 멱등성으로 전환 검토

### 2. 재시도 정책

**방식**: 선형 재시도 (1초, 2초) + 토큰 오류 시 즉시 삭제

```python
async def send_fcm_with_retry(
    token: str,
    message: messaging.Message,
    max_retries: int = 2
) -> bool:
    delays = [1, 2]  # 초

    for attempt in range(max_retries + 1):
        try:
            messaging.send(message)
            return True
        except FirebaseError as e:
            error_code = e.code

            # 토큰 오류: 재시도 무의미, 즉시 삭제
            if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT']:
                await delete_device_token(token)
                logger.warning("무효 토큰 삭제", extra={
                    "token": token[:20] + "...",
                    "error_code": error_code
                })
                return False

            # 서버 오류: 재시도
            if attempt < max_retries:
                await asyncio.sleep(delays[attempt])
            else:
                logger.error("FCM 전송 실패", extra={
                    "token": token[:20] + "...",
                    "error_code": error_code,
                    "attempts": attempt + 1
                })
                return False

    return False
```

### 3. 구조화 로그 (관측 가능성)

**필수 필드**: `event`, `success`, `latency_ms`, `error_code`

```python
import time
from app import logger

async def send_notification_with_logging(user_id: str, bank: str, currency: str, rate: float):
    start = time.time()
    success = False
    error_code = None

    try:
        # FCM 전송 로직
        await send_fcm_notification(user_id, bank, currency, rate)
        success = True
    except FirebaseError as e:
        error_code = e.code
    finally:
        latency_ms = (time.time() - start) * 1000

        logger.info("FCM 알림 발송", extra={
            "event": "fcm_send",
            "user_id": user_id,
            "bank": bank,
            "currency": currency,
            "rate": rate,
            "success": success,
            "latency_ms": round(latency_ms, 2),
            "error_code": error_code
        })
```

**로그 기반 분석 (Phase 5 이후)**:

```bash
# 실패율 확인
grep '"event": "fcm_send"' logs/app.log | \
  jq 'select(.success == false)' | wc -l

# 평균 지연 확인
grep '"event": "fcm_send"' logs/app.log | \
  jq '.latency_ms' | awk '{sum+=$1; count++} END {print sum/count}'

# 에러 코드별 카운트
grep '"event": "fcm_send"' logs/app.log | \
  jq 'select(.error_code != null) | .error_code' | sort | uniq -c
```

### 완료 게이트 (Phase 2)

| 항목 | 테스트 | 통과 기준 |
|------|--------|----------|
| ✅ 유효 토큰 알림 | 조건 충족 시 푸시 수신 | 10초 내 수신 |
| ✅ 무효 토큰 처리 | 잘못된 토큰으로 전송 | 자동 삭제 확인 |
| ✅ 중복 알림 방지 | 같은 조건 연속 충족 | 알림 1회만 발송 |
| ✅ 조건 리셋 | 미충족 → 재충족 | 새 알림 발송 |
| ✅ 구조화 로그 | 로그 파일 확인 | 필수 필드 포함 |

---

## 🔧 기기 토큰 정리 전략 (구현 예정)

> **목적**: FCM device_token 관리 및 정리 전략 (구현 시점에 최종 결정)
> **상태**: 📋 검토 완료, 구현 대기 → **알림 품질 게이트에 통합됨**

### 문제 상황

FCM device_token은 다음 상황에서 무효화됨:

1. **앱 삭제/재설치**: 새 토큰 발급, 기존 토큰 무효
2. **기기 변경**: 새 기기에서 새 토큰 발급
3. **토큰 만료**: FCM이 주기적으로 토큰 갱신 (드물게 발생)
4. **OS 업데이트**: 일부 메이저 업데이트 시 토큰 변경

무효 토큰으로 FCM 전송 시 `InvalidRegistration` 또는 `NotRegistered` 에러 발생.

### 전략 옵션

#### Option A: 전송 시점 정리 (Lazy Cleanup) ⭐ 권장

```python
# FCM 전송 후 응답 확인
response = messaging.send(message)
# 또는 send_multicast 사용 시
response = messaging.send_multicast(message)

# 실패한 토큰 처리
for idx, result in enumerate(response.responses):
    if not result.success:
        error_code = result.exception.code
        if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT']:
            # 무효 토큰 삭제
            delete_device_token(tokens[idx])
```

**장점:**

- 실제 문제 발생 시에만 정리 (불필요한 API 호출 없음)
- 구현 간단
- FCM 응답으로 정확한 상태 파악

**단점:**

- 첫 번째 실패 시까지 무효 토큰 유지
- 알림 전송 실패 1회 발생

#### Option B: 주기적 검증 (Scheduled Cleanup)

```python
# 매일 새벽 3시 실행
@scheduler.scheduled_job('cron', hour=3)
async def cleanup_stale_tokens():
    # 30일 이상 미사용 토큰 삭제
    cutoff = datetime.now() - timedelta(days=30)
    await db.execute(
        "DELETE FROM user_devices WHERE last_used < ?",
        (cutoff,)
    )
```

**장점:**

- 미사용 토큰 자동 정리
- DB 크기 관리

**단점:**

- `last_used` 컬럼 추가 필요
- 토큰 갱신 시점 추적 필요

#### Option C: 하이브리드 (권장 조합)

```text
1. FCM 전송 실패 시 즉시 삭제 (Option A)
2. 30일 이상 미사용 토큰 주기적 삭제 (Option B)
3. 앱 시작 시 토큰 갱신 확인 (클라이언트)
```

### 멀티 디바이스 정책

한 사용자가 여러 기기 사용 시 정책:

| 정책 | 설명 | 구현 |
|------|------|------|
| **모든 기기** | 모든 등록된 기기에 알림 전송 | `SELECT device_token FROM user_devices WHERE user_id = ?` |
| **최신 기기만** | 마지막 활성 기기에만 전송 | `ORDER BY last_used DESC LIMIT 1` |
| **기기 수 제한** | 최대 N개 기기 허용 | 초과 시 가장 오래된 토큰 삭제 |

**권장**: 모든 기기에 전송 (사용자 경험 우선)

### 중복 토큰 처리

```sql
-- user_devices 테이블의 UNIQUE 제약
UNIQUE(user_id, device_token)

-- 같은 토큰이 다른 user_id로 등록 시도 시
-- (기기 소유자 변경, 로그아웃 후 다른 계정 로그인)
ON CONFLICT (device_token) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    updated_at = NOW()
```

### 구현 체크리스트

- [ ] FCM 전송 응답 핸들링 (무효 토큰 삭제)
- [ ] `last_used` 컬럼 추가 (선택)
- [ ] 주기적 정리 Job 추가 (선택)
- [ ] 멀티 디바이스 정책 결정
- [ ] 중복 토큰 처리 로직 구현

### 관련 문서

- [ADR-016](DECISIONS.md#adr-016-인증알림-인프라---aws-rds--firebase-auth--fcm): 인증/알림 인프라 결정
- [FCM Error Codes](https://firebase.google.com/docs/cloud-messaging/send-message#admin_sdk_error_reference): 에러 코드 참고

---

## 테스트 체크리스트

서버 사이드 구독 검증 로직 구현 시 검증해야 할 핵심 케이스입니다.

### Webhook 인증 검증

| 케이스 | 입력 | 기대 결과 |
|--------|------|----------|
| 유효한 인증 | Authorization 헤더 값이 `REVENUECAT_WEBHOOK_AUTH_KEY`와 일치 | 200 OK, 이벤트 처리 |
| 잘못된 인증 | Authorization 값 불일치 | 401 Unauthorized |
| 헤더 누락 | Authorization 없음 | 401 Unauthorized |
| Secret 미설정 | 환경변수 누락 | 로그 에러, 검증 실패 |

### Stale 캐시 동작

| 케이스 | 캐시 상태 | API 결과 | 기대 동작 |
|--------|----------|---------|----------|
| 신선한 캐시 | TTL 내 | - | 캐시값 반환 (API 호출 없음) |
| Stale + API 성공 | TTL 만료 | 200 OK | 새 값 반환, 캐시 갱신 |
| Stale + API 오류 | TTL 만료 | 5xx/timeout | **stale 값 반환** (유료 사용자 보호) |
| 캐시 없음 + API 오류 | 없음 | 5xx/timeout | **PENDING 반환** (재시도 필요) |
| Stale TTL 초과 | 1시간 초과 | - | 캐시 삭제됨, 새로 조회 |

### 캐시 무효화

| 케이스 | 트리거 | 기대 동작 |
|--------|-------|----------|
| 구독 구매 | INITIAL_PURCHASE Webhook | `invalidate_user_cache()` 호출 → 다음 `verify_premium_status()`에서 API 재조회 |
| 구독 갱신 | RENEWAL Webhook | 캐시 무효화 → 최신 상태 반영 |
| 구독 만료 | EXPIRATION Webhook | 캐시 무효화 → 즉시 접근 차단 |
| 구독 취소 | CANCELLATION Webhook | 캐시 무효화 → 즉시 접근 차단 |

### LRU 캐시 경계

| 케이스 | 조건 | 기대 동작 |
|--------|------|----------|
| 캐시 가득 참 | 1000명 초과 | 가장 오래된 항목 제거 (LRU) |
| 동시 접근 | 여러 요청 동시 | 단일 프로세스 전제 (Lock 없음) |

---

## iOS 프로젝트 파일 구조

> **참고**: iOS 프로젝트 상세 가이드는 `~/Downloads/Projects/FXi/ios/CLAUDE.md` 참조

```text
ios/FXi/
├── FXiApp.swift                    # 앱 진입점, RootView + 구독 분기
├── AppDelegate.swift               # Firebase/RevenueCat/Push 초기화
├── ContentView.swift               # 메인 탭 콘텐츠 (구독자 진입)
│
├── Utils/
│   ├── Constants.swift             # API, RevenueCatConfig, AlertConfig, Bank, 색상 등
│   ├── Date+Formatter.swift        # 날짜 포매터
│   └── ...
│
├── Models/
│   ├── AlertSetting.swift          # 알림 설정 모델 + API 요청/응답
│   ├── ExchangeRate.swift          # 환율 모델
│   └── ...
│
├── Services/
│   ├── AuthService.swift           # Firebase Auth + RevenueCat 로그인 연동
│   ├── SubscriptionManager.swift   # RevenueCat 구독 상태 관리
│   ├── PushNotificationService.swift # FCM 토큰 관리 + 서버 등록
│   ├── DeviceService.swift         # 디바이스 등록 API 클라이언트
│   ├── NotificationSettingsService.swift # 알림 설정 API 클라이언트
│   ├── NotificationPermissionManager.swift # 푸시 권한 관리
│   └── ...
│
├── ViewModels/
│   ├── AlertSettingsViewModel.swift # 알림 설정 CRUD + 재시도 로직
│   ├── ExchangeRateViewModel.swift  # 환율 데이터 관리
│   └── ...
│
└── Views/
    ├── LoginView.swift             # 로그인 화면
    ├── Subscription/
    │   ├── PaywallView.swift       # 구독 결제 화면
    │   ├── LockedPreviewView.swift # 비구독자 샘플 화면
    │   └── Components/             # 샘플 UI 컴포넌트
    └── Components/
        ├── AlertSection.swift      # 알림 목록 섹션
        ├── AlertAddSheet.swift     # 알림 추가/수정 시트
        └── ...
```

### 핵심 서비스 파일

| 파일 | 역할 | 주요 메서드 |
|------|------|-------------|
| `AuthService.swift` | Firebase Auth + RevenueCat 연동 | `signInWithGoogle()`, `signInWithApple()`, `signOut()` |
| `SubscriptionManager.swift` | 구독 상태 관리 | `onAuthCompleted()`, `purchase()`, `restorePurchases()` |
| `PushNotificationService.swift` | FCM 토큰 관리 | `registerDeviceOnServer()`, `unregisterDeviceFromServer()` |
| `NotificationSettingsService.swift` | 알림 설정 API | `fetchSettings()`, `createSetting()`, `toggleSetting()`, `deleteSetting()` |
| `AlertSettingsViewModel.swift` | 알림 설정 UI 상태 | `loadSettings()`, `createSetting()`, `toggleSetting()`, `deleteSetting()` |

### AlertConfig 상수

```swift
// ios/FXi/Utils/Constants.swift
enum AlertConfig {
    /// 사용자당 최대 알림 개수
    static let maxCount = 30
    /// 남은 개수 표시 임계값 (이 개수 이하로 남으면 표시)
    static let showRemainingThreshold = 3
}
```

---

## 참고 자료

### Firebase 관련
- Firebase Auth: https://firebase.google.com/docs/auth
- FCM: https://firebase.google.com/docs/cloud-messaging
- Pricing: https://firebase.google.com/pricing

### Supabase 관련
- Supabase Auth: https://supabase.com/docs/guides/auth
- PostgreSQL: https://supabase.com/docs/guides/database
- Edge Functions: https://supabase.com/docs/guides/functions
- Pricing: https://supabase.com/pricing

### RevenueCat 관련
- 공식 문서: https://www.revenuecat.com/docs
- Pricing: https://www.revenuecat.com/pricing
- Firebase 통합: https://www.revenuecat.com/docs/firebase-integration
- Supabase 통합: Custom User ID 사용

### 기타
- Apple In-App Purchase: https://developer.apple.com/in-app-purchase/
- Google Play Billing: https://developer.android.com/google/play/billing

---

**마지막 업데이트**: 2026-01-09
**결정 완료**: AWS RDS PostgreSQL + Firebase Auth + FCM + RevenueCat
**최근 변경**:

- 문서 업데이트: Premium 검증 PENDING/503 동작 및 코드 스니펫 실제 구현 반영
- 문서 업데이트: Webhook Authorization 헤더 검증 방식 반영
- 문서 업데이트: DB 스키마(user_devices, notification_settings/logs) 실제 모델 반영
- 문서 업데이트: iOS 파일 구조 + AppDelegate 초기화 예시 정합성 개선
