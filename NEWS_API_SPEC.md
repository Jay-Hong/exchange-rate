# News API Specification — 모바일 앱 연동 가이드

> **버전**: 2.0
> **작성일**: 2026-03-28
> **최종 업데이트**: 2026-04-01
> **Base URL**: `https://fxi.kr`
> **인증**: 불필요 (공개 API)

---

## 1. 엔드포인트

```
GET /api/news
```

뉴스를 최신순으로 반환합니다.

### 쿼리 파라미터

| 파라미터 | 타입 | 기본값 | 범위 | 설명 |
|---------|------|--------|------|------|
| `limit` | int | 50 | 1~100 | 최대 반환 건수 (범위 초과 시 자동 보정) |
| `hours` | float | 24.0 | 0.5~24.0 | 시간 윈도우 (범위 초과 시 자동 보정) |

> **파라미터 범위 초과**: 에러가 아닌 자동 보정(clamp). `limit=999` → `100`, `hours=50` → `24.0`으로 처리.

### 요청 예시

```
GET /api/news
GET /api/news?limit=20
GET /api/news?hours=12&limit=10
```

---

## 2. 응답 형식

### 성공 응답 (200 OK)

```json
{
  "news": [
    {
      "id": "AKR20260401003900016",
      "title": "달러-원, 엔화 약세 심화 속 상승폭 확대…1,511.40원 마감",
      "link": "https://news.einfomax.co.kr/news/articleView.html?idxno=4406500",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-04-01T10:42:14+09:00"
    },
    {
      "id": "AKR20260401003500016",
      "title": "달러-엔 오름폭 확대…160.368엔 (본문없음)",
      "link": "https://fx.kbstar.com/quics?page=C110648&cc=b113988:b114074&newsUniqNo=AKR20260401003500016&QSL=F",
      "source": "einfomax",
      "content_type": "external_link",
      "published_at": "2026-04-01T10:29:20+09:00"
    },
    {
      "id": "AKR20260401084699991",
      "title": "전황 악화일로, 이젠 1,500이 지지선 / 국민은행",
      "link": "http://rreport.einfomax.co.kr/report/ecigczegcgxcigq.pdf",
      "source": "einfomax",
      "content_type": "report_pdf",
      "published_at": "2026-04-01T08:43:03+09:00"
    }
  ],
  "metadata": {
    "returned_count": 3,
    "window_hours": 24.0,
    "responded_at": "2026-04-01T11:00:00.000000+09:00"
  }
}
```

### 정상 빈 응답 (200 OK, 결과 없음)

```json
{
  "news": [],
  "metadata": {
    "returned_count": 0,
    "window_hours": 24.0,
    "responded_at": "2026-04-01T07:00:00.000000+09:00"
  }
}
```

---

## 3. 필드 상세

### NewsItem

| 필드 | 타입 | Nullable | 설명 |
|------|------|----------|------|
| `id` | string | No | 기사 고유 ID |
| `title` | string | No | 기사 제목 (HTML 엔티티 포함 가능) |
| `link` | string | **Yes** | 기사 URL. 정상 경로에서 non-null 기대, 방어적 null 처리 권장 |
| `source` | string | No | 데이터 제공자 (현재 `"einfomax"` 고정, 앱 로직 분기에 사용하지 말 것) |
| `content_type` | string | No | 렌더링 방식 결정용 |
| `published_at` | string | No | 발행 시간 (ISO 8601, KST +09:00) |

### NewsMetadata

| 필드 | 타입 | 설명 |
|------|------|------|
| `returned_count` | int | 이번 응답에 포함된 기사 수 |
| `window_hours` | float | 적용된 시간 윈도우 |
| `responded_at` | string | 서버 응답 시간 (ISO 8601) |

---

## 4. content_type별 렌더링 가이드

### `external_link` — 일반 기사

```json
{
  "title": "달러-원, 1,511.40원 마감",
  "link": "https://news.einfomax.co.kr/...",
  "content_type": "external_link"
}
```

**앱 동작:**
- 제목 표시
- 탭 → `link` URL을 웹뷰로 열기
- 제목이 "(본문없음)"으로 끝나는 경우: 속보성 기사, 본문이 비어있을 수 있음

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
- 탭 → **외부 브라우저(Safari)로** `link` URL 열기
- link는 `http://` (HTTPS 아님) → iOS 인앱 WebView에서 ATS 정책으로 차단될 수 있음
- 평일 아침 8~9시에 은행별 시장 보고서 4건 내외 발행

---

## 5. 정렬 규칙

- **순수 시간순** (published_at 내림차순)
- 서버가 초보/상보 중복을 자동으로 접어서 대표 1건만 노출

---

## 6. title HTML 엔티티 처리

| 원본 | 엔티티 |
|------|--------|
| `"` | `&quot;` |
| `&` | `&amp;` |
| `<` | `&lt;` |
| `>` | `&gt;` |

```swift
// iOS (Swift)
let decoded = title.replacingOccurrences(of: "&quot;", with: "\"")
    .replacingOccurrences(of: "&amp;", with: "&")
    .replacingOccurrences(of: "&lt;", with: "<")
    .replacingOccurrences(of: "&gt;", with: ">")
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
| 뉴스 탭 활성 상태 | 1~3분마다 |
| 백그라운드 | 호출 안 함 |
| Pull-to-refresh | 즉시 1회 |

---

## 8. 에러 처리

| HTTP 코드 | 의미 | 앱 동작 |
|-----------|------|--------|
| 200 | 정상 (빈 배열 포함) | 데이터 표시 또는 "뉴스 없음" |
| 500 | 서버 오류 | 캐시된 이전 데이터 표시 + 재시도 |
| timeout | 네트워크 오류 | 캐시된 이전 데이터 표시 + 재시도 |

---

## 9. 앱 렌더링 의사 코드

```
for item in response.news:
    display(htmlDecode(item.title))
    display(timeAgo(item.published_at))

    switch item.content_type:
        case "external_link":
            if item.link != null:
                onTap → openWebView(item.link)
            else:
                onTap → 비활성화

        case "report_pdf":
            showBadge("보고서")
            if item.link != null:
                onTap → openExternalBrowser(item.link)  // HTTP → Safari
            else:
                onTap → 비활성화
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
| 주말 | 뉴스 대폭 감소 |

### 일반적인 기사 수

- 평일: 30~50건 / 24시간 윈도우
- 주말: 10~20건

### "(본문없음)" 기사

- 속보성 기사로, KB에서 `*` 접두사가 붙어 온 것
- 링크는 있지만, 탭하면 본문이 비어있을 수 있음
- RSS가 ~2시간 뒤 도착하면 정상 제목+link로 자동 교체됨
