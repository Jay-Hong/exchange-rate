# NEWS_IMPL_SPEC.md — 환율 뉴스 피드 구현 스펙

> **용도**: 구현 참고용 임시 문서 (안정화 후 삭제)
> **작성일**: 2026-03-27
> **최종 업데이트**: 2026-03-28
> **상태**: 구현 완료, 운영 모니터링 중

---

## 1. 개요

연합인포맥스 뉴스를 **2개 경로**(RSS + KB API)로 수집하여 환율 관련 뉴스 API를 제공한다.

**핵심 결정:**

- 저장소: Redis-only (8시간 윈도우 휘발성 데이터, DB 불필요)
- 수집 경로: KB API (속보, ~2시간 빠름) + RSS (원문 링크 보유, 백필)
- 정렬: 순수 시간순 (published_at 내림차순)
- API 응답: id, title, link, source, content_type, published_at
- category: 내부 Redis에만 저장 (필터링/디버깅용, API 미노출)

---

## 2. 데이터 소스

### 2.1 RSS 소스

| 피드 | 채널명 | category | 필터 수준 |
|------|--------|----------|----------|
| S1N16 | 채권/외환 | `forex` | `noise_only` |
| S1N23 | 국제뉴스 | `global` | `loose` |
| S1N21 | 해외주식 | `stock_global` | `strict` |
| S1N2 | 증권 | `stock_domestic` | `strict` |

- 수집 주기: 5분 (:45초)
- ETag/Last-Modified 조건부 GET (304 지원)
- ~2시간 딜레이 있음

### 2.2 KB API 소스

```
POST https://fx.kbstar.com/quics?page=C110648&QAction=1253470&RType=json
```

- 외환탭(21) + 경제탭(22), 각 2페이지 수집
- 수집 주기: 5분 (:15초)
- rpsnt 파라미터 미전송 → 대표뉴스도 목록에 자동 포함

**분류코드 → 필터/category 매핑:**

| KB 코드 | 의미 | 필터 수준 | category |
|---------|------|----------|----------|
| `IS` | 외환 | `noise_only` (운영 가설) | `forex` |
| `IY` | 서환 | `noise_only` | `forex` |
| `IT` | 채권 | `loose` | `global` |
| `IU` | 증권 | `loose` | `global` |
| `99` | 기타 | `loose` | `global` |

---

## 3. KB ↔ RSS 병합 (Upsert 규칙)

nsid 기반 중복 제거. 6개 분기:

| # | 상태 | title/link/category | match_type | published_at |
|---|------|-------------------|------------|-------------|
| 1 | 새 기사 | 그대로 저장 | 그대로 | 그대로 |
| 2 | KB→RSS | RSS 유효값으로 교체 | fx면 승격, 아님 유지 | min() |
| 3 | RSS→KB | 유지 | 유지 | min() |
| 4 | RSS有+KB재수집 | 유지 (RSS 보호) | 유지 | min() |
| 5 | KB만+KB재수집 | 최신값 갱신 | 갱신 | min() |
| 6 | RSS재수집 | 갱신 | fx 승격만 | min() |

**정책:**

- RSS 유효값 우선 (빈값 overwrite 방지)
- match_type 승격만, 강등 없음
- report_pdf는 RSS 와도 유지 (PDF 직링크가 최선의 UX)
- flash → external_link 승격 (RSS에 link가 있으면)

---

## 4. 필터 시스템

### 4.1 잡음 제외 (`is_noise_title`)

- 접두사: `[인사]`, `[부고]`, `[게시판]`
- 키워드: `신임`, `선임`, `임명`, `전보`, `승진`, `이동` (강한 외환 키워드 없으면)

### 4.2 환율 관련도 (`is_forex_relevant`)

- PRIMARY: `환율`, `외환`, `달러-원`, `달러-엔`, `엔화`, `유로`, `위안`, `환헤지`, `환위험`, `DXY`, `외환시장`
- CONDITIONAL: `금리`+달러, `연준`+달러, `Fed`+달러, `BOJ`+엔, `ECB`+유로, `PBOC`+위안

### 4.3 매크로 이벤트 (`classify_macro`)

- GEOPOLITICAL (범용+시의성): `전쟁`, `휴전`, `종전`, `이란`, `중동`, `호르무즈`, `트럼프`
- SEVERITY (고강도): `폭격`, `공습`, `미사일`, `핵`, `봉쇄`, `침공`, `격추`, `전면전`, `확전`, `보복`, `철수`, `대피`, `차단`, `막혔다`, `되돌아가`, `공격`
- TRANSMISSION (시장전파): `유가`, `원유`, `브렌트유`, `WTI`, `달러`, `환율`, `원화`, `엔화`, `위안`, `시장`, `증시`, `위험회피`, `안전자산`, `하락`, `반등`, `급락`, `급등`, `협상`, `데드라인`

### 4.4 산업 영향 (`is_industry_impact`)

- TRIGGERS: `AI`, `반도체`, `HBM`, `메모리`, `파운드리`, `삼성전자`, `SK하이닉스`, `엔비디아`, `TSMC`
- IMPACT: `수출`, `실적`, `외국인`, `코스피`, `무역`, `공급`, `수주`, `매출`, `달러`, `환율`, `원화`, `급락`, `급등`

### 4.5 적용 규칙

| filter_level | 잡음 | forex | macro | industry |
|--------------|------|-------|-------|----------|
| `noise_only` | O | - | - | - |
| `loose` | O | O (loose) | O | O |
| `strict` | O | O (strict) | - | - |

### 4.6 특수 처리

- `*` 접두사 → `flash` (본문 없음, 제목에서 `*` 제거)
- `[전문]` 접두사 → `report_pdf` (KB 상세에서 PDF URL 추출, 제목에서 `[전문]` 제거)

---

## 5. API 응답

### 5.1 스키마

```python
class NewsItem(BaseModel):
    id: str
    title: str
    link: Optional[str] = None
    source: str
    content_type: str = "external_link"  # external_link | flash | report_pdf | direct_text
    published_at: str
    body: Optional[str] = None
```

### 5.2 content_type별 동작

| content_type | link | body | 앱 동작 |
|-------------|------|------|--------|
| `external_link` | URL | - | 기사 링크 열기 |
| `report_pdf` | PDF URL | - | PDF 바로 열기 |
| `flash` | - | - | 제목만, "본문없음" |
| `direct_text` | - | 본문 | 인라인 표시 (향후) |

### 5.3 정렬

순수 시간순 (published_at 내림차순). match_type은 내부 추적용으로만 유지.

---

## 6. Redis 내부 구조

| 필드 | 공개 API | 내부용 | 설명 |
|------|---------|--------|------|
| title | O | | 기사 제목 |
| link | O | | URL (external_link, report_pdf) |
| source | O | | "einfomax" |
| content_type | O | | external_link/flash/report_pdf |
| published_at | O | | ISO 8601 |
| category | | O | forex, global, ... |
| match_type | | O | fx, macro, macro_severity, macro_industry |
| ingested_via | | O | kb_api, rss, kb_api+rss |

---

## 7. 파일 구조

```
app/news/
├── __init__.py
├── sources.py       # RSS 소스 정의
├── filters.py       # 필터 (잡음/관련도/macro/severity/industry)
├── fetcher.py       # RSS 수집 + 파싱 + upsert + cleanup
├── kb_fetcher.py    # KB API 수집 (flash, [전문] PDF, 2페이지)
└── upsert.py        # 공통 Redis upsert (6개 분기 병합 규칙)
```

---

## 8. 구현 상태

- [x] RSS 4개 소스 수집 + ETag 조건부 GET
- [x] KB API 외환+경제탭, 2페이지 수집
- [x] 필터 (잡음/관련도/macro/severity/industry)
- [x] upsert 병합 (6개 분기, RSS 정규화 보호)
- [x] flash 속보 처리 (`*` 제거, content_type 교정)
- [x] report_pdf 은행 보고서 PDF 직링크
- [x] content_type별 API 직렬화 (link/body 분기)
- [x] 시간순 정렬
- [x] cache.py ZSET/HASH 메서드 (bytes 디코딩)
- [x] schemas.py NewsItem/NewsResponse
- [x] scheduler.py KB :15, RSS :45
- [x] 운영 서버 배포

### 모니터링 체크 (운영 중)

- [ ] IS=noise_only 운영 가설 검증
- [ ] 필터 오탐/누락 확인
- [ ] KB API 안정성 (IP 차단 등)
- [ ] flash→external_link 자동 교정
- [ ] [전문] PDF 추출 정상 동작 (월요일 아침)
- [ ] KB↔RSS 링크 승격 확인
