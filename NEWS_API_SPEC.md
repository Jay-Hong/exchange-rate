# News API Specification — 모바일 앱 연동 가이드

> **버전**: 1.0
> **작성일**: 2026-03-28
> **Base URL**: `https://fxi.kr`
> **인증**: 불필요 (공개 API)

---

## 1. 엔드포인트

```
GET /api/news
```

환율 관련 뉴스를 최신순으로 반환합니다.

### 쿼리 파라미터

| 파라미터 | 타입 | 기본값 | 범위 | 설명 |
|---------|------|--------|------|------|
| `limit` | int | 30 | 1~100 | 최대 반환 건수 |
| `hours` | float | 10.0 | 0.5~24.0 | 시간 윈도우 (현재로부터 N시간 이내) |
| `category` | string | 전체 | comma-separated | 카테고리 필터 (서버 내부 분류 기준, 1차 연동에서는 사용하지 않는 것을 권장) |

### 요청 예시

```
GET /api/news
GET /api/news?limit=20
GET /api/news?hours=4&limit=10
GET /api/news?category=forex
GET /api/news?category=forex,global
```

---

## 2. 응답 형식

### 성공 응답 (200 OK)

```json
{
  "news": [
    {
      "id": "AKR20260328003900016",
      "title": "달러-원, 엔화 약세 심화 속 상승폭 확대…1,511.40원 마감",
      "link": "https://news.einfomax.co.kr/news/articleView.html?idxno=4406450",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-03-28T10:42:14+09:00",
      "body": null
    },
    {
      "id": "AKR20260328003500016",
      "title": "달러-엔 오름폭 확대…2024년 7월 이후 최고 160.368엔",
      "link": null,
      "source": "einfomax",
      "content_type": "flash",
      "published_at": "2026-03-28T10:29:20+09:00",
      "body": null
    },
    {
      "id": "AKR20260328084699991",
      "title": "전황 악화일로, 이젠 1,500이 지지선 / 국민은행",
      "link": "http://rreport.einfomax.co.kr/report/ecigczegcgxcigq.pdf",
      "source": "einfomax",
      "content_type": "report_pdf",
      "published_at": "2026-03-28T08:43:03+09:00",
      "body": null
    }
  ],
  "metadata": {
    "returned_count": 3,
    "window_hours": 10.0,
    "responded_at": "2026-03-28T11:00:00.000000+09:00"
  }
}
```

### 정상 빈 응답 (200 OK, 결과 없음)

기사가 없거나 시간 윈도우 내 매칭 기사가 없을 때. 서버 장애와는 다름 (장애는 섹션 8 참고).

```json
{
  "news": [],
  "metadata": {
    "returned_count": 0,
    "window_hours": 10.0,
    "responded_at": "2026-03-28T07:00:00.000000+09:00"
  }
}
```

---

## 3. 필드 상세

### NewsItem

| 필드 | 타입 | Nullable | 설명 |
|------|------|----------|------|
| `id` | string | No | 기사 고유 ID (예: `AKR20260328003900016`) |
| `title` | string | No | 기사 제목 (HTML 엔티티 포함 가능, 아래 주의사항 참고) |
| `link` | string | **Yes** | 기사 URL. `external_link`/`report_pdf`일 때 non-null 기대, 방어적 null 처리 권장 |
| `source` | string | No | 데이터 제공자 (현재 `"einfomax"` 고정, 앱 로직 분기에 사용하지 말 것) |
| `content_type` | string | No | 렌더링 방식 결정용 (아래 상세) |
| `published_at` | string | No | 발행 시간 (ISO 8601, KST +09:00) |
| `body` | string | **Yes** | 뉴스 본문 (`direct_text`일 때만 값 존재, 현재 미사용) |

### NewsMetadata

| 필드 | 타입 | 설명 |
|------|------|------|
| `returned_count` | int | 이번 응답에 포함된 기사 수 |
| `window_hours` | float | 적용된 시간 윈도우 |
| `responded_at` | string | 서버 응답 시간 (ISO 8601, KST) |

---

## 4. content_type별 렌더링 가이드

### `external_link` — 일반 기사

```json
{
  "title": "달러-원, 엔화 약세 심화 속 상승폭 확대…1,511.40원 마감",
  "link": "https://news.einfomax.co.kr/news/articleView.html?idxno=4406450",
  "content_type": "external_link"
}
```

**앱 동작:**
- 제목 표시
- 탭 → `link` URL을 웹뷰 또는 외부 브라우저로 열기
- link는 정상 데이터에서 non-null 기대, 방어적 null 처리 권장

### `flash` — 속보 (본문 없음)

```json
{
  "title": "달러-엔 오름폭 확대…2024년 7월 이후 최고 160.368엔",
  "link": null,
  "content_type": "flash"
}
```

**앱 동작:**
- 제목 표시 + "속보" 배지 또는 아이콘
- "본문없음" 또는 탭 비활성화
- link, body 모두 null
- 나중에 서버가 `external_link`로 전환할 수 있음 (RSS 도착 시)

### `report_pdf` — 은행 보고서 PDF

```json
{
  "title": "전황 악화일로, 이젠 1,500이 지지선 / 국민은행",
  "link": "http://rreport.einfomax.co.kr/report/ecigczegcgxcigq.pdf",
  "content_type": "report_pdf"
}
```

**앱 동작:**
- 제목 표시 + "보고서" 배지 또는 아이콘
- 탭 → PDF 뷰어로 `link` URL 열기
- link는 정상 데이터에서 non-null (PDF URL), 방어적 null 처리 권장
- 평일 아침 8~9시에 은행별 시장 보고서로 4건 내외 발행

### `direct_text` — 직접 텍스트 (향후 예정)

```json
{
  "title": "달러-원 1,510원 돌파 속보",
  "link": null,
  "content_type": "direct_text",
  "body": "달러-원 환율이 장중 1,510원을 돌파했다..."
}
```

**앱 동작:**
- 제목 + `body` 텍스트를 인라인 표시
- link는 null, body는 non-null
- **현재 미사용** — 향후 관리자 직접 입력 시 사용 예정

---

## 5. 정렬 규칙

- **순수 시간순** (published_at 내림차순)
- 기사 종류(환율, 전쟁 속보, 매크로, 산업)와 무관하게 최신 순서
- 서버가 이미 환율 관련성 필터를 적용한 상태이므로, 앱에서 추가 필터 불필요

---

## 6. title HTML 엔티티 처리

제목에 HTML 엔티티가 포함될 수 있습니다:

| 원본 | 엔티티 |
|------|--------|
| `"` | `&quot;` |
| `<` | `&lt;` |
| `>` | `&gt;` |
| `&` | `&amp;` |

**앱에서 HTML 디코딩 필요:**

```swift
// iOS (Swift)
let decoded = title.replacingOccurrences(of: "&quot;", with: "\"")
    .replacingOccurrences(of: "&amp;", with: "&")
    .replacingOccurrences(of: "&lt;", with: "<")
    .replacingOccurrences(of: "&gt;", with: ">")

// 또는 NSAttributedString으로 HTML 디코딩
```

```kotlin
// Android (Kotlin)
val decoded = Html.fromHtml(title, Html.FROM_HTML_MODE_LEGACY).toString()
```

---

## 7. 새로고침 전략

| 상황 | 권장 주기 |
|------|----------|
| 앱 진입/포그라운드 복귀 | 즉시 1회 |
| 뉴스 탭 활성 상태 | 1~3분마다 (서버 수집 주기 5분) |
| 백그라운드 | 호출 안 함 |
| Pull-to-refresh | 즉시 1회 |

서버는 5분마다 새 뉴스를 수집하므로, 1분 미만 간격으로 호출해도 같은 데이터일 가능성이 높습니다.

---

## 8. 에러 처리

| HTTP 코드 | 의미 | 앱 동작 |
|-----------|------|--------|
| 200 | 정상 (빈 배열 포함) | 데이터 표시 또는 "뉴스 없음" |
| 500 | 서버 오류 | 캐시된 이전 데이터 표시 + 재시도 |
| timeout | 네트워크 오류 | 캐시된 이전 데이터 표시 + 재시도 |

> **파라미터 범위 초과**: 에러가 아닌 자동 보정(clamp). `limit=999` → `100`, `hours=50` → `24.0`으로 처리.

뉴스는 비핵심 기능이므로, 실패 시 빈 상태를 보여주거나 이전 캐시를 유지하면 됩니다. 환율 데이터와 달리 실시간 정확성이 필수가 아닙니다.

---

## 9. 앱 렌더링 의사 코드

```
for item in response.news:
    display(item.title)  // HTML 디코딩 필요
    display(timeAgo(item.published_at))  // "3분 전", "2시간 전"

    switch item.content_type:
        case "external_link":
            if item.link != null:
                onTap → openWebView(item.link)
            else:
                onTap → 비활성화

        case "flash":
            showBadge("속보")
            showLabel("본문없음")
            onTap → 비활성화 또는 무반응

        case "report_pdf":
            showBadge("보고서")
            if item.link != null:
                onTap → openPdfViewer(item.link)
            else:
                onTap → 비활성화

        case "direct_text":
            display(item.body)
            onTap → 없음 (인라인 표시)
```

---

## 10. 운영 참고

### 뉴스 발행 패턴

| 시간대 | 특징 |
|--------|------|
| 08:00~09:00 | 은행 시장 보고서 (`report_pdf`) 4건 내외 |
| 09:00~15:30 | 서울 외환시장 개장, 뉴스 활발 |
| 15:30~18:00 | 시장 마감 기사 |
| 18:00~06:00 | 뉴욕장/런던장, 속보 위주 |
| 주말 | 뉴스 대폭 감소, 글로벌 이슈만 간헐적 |

### 일반적인 기사 수

- 평일 영업시간: 15~25건 / 10시간 윈도우
- 심야/주말: 5~10건

### content_type 전환

- `flash` → `external_link`: RSS가 ~2시간 뒤 도착하면 자동 전환
- 앱에서 새로고침 시 자연스럽게 반영됨
- `report_pdf`는 전환 없이 유지
