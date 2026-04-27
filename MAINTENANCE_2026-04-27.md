# MIBANK URL/DOM 변경 대응

> 📅 **날짜**: 2026-04-27
> 📚 **관련 문서**: [CRAWLERS.md](CRAWLERS.md), [CHANGELOG.md](CHANGELOG.md), [ADR-017](DECISIONS.md#adr-017-mibank-환율-파싱---position-기반-vs-currency-code-기반)

---

## 장애 요약

MIBANK 기반 1차 크롤링이 실패하면서 신한은행과 NH농협은행 크롤러가 매 실행마다 Selenium fallback으로 전환됐다.

영향 범위:

| 영향 | 크롤러 |
|------|--------|
| MIBANK 1차 실패 시 매번 Selenium fallback | `shinhan`, `nh`, `sc` |
| 은행 자체 페이지 실패 시 MIBANK 안전망 실패 | `kb`, `hana`, `woori`, `bs`, `citi`, `ibk` |
| 영향 없음 | `investing`, `dxy`, `usdt_sources` |

---

## 근본 원인

MIBANK의 은행별 환율 페이지 URL과 DOM 구조가 변경됐다.

| 항목 | 기존 | 변경 후 |
|------|------|---------|
| URL | `https://www.mibank.me/exchange/bank/index.php?search_code=088` | `https://exchange.mibank.me/bank?bank_cd=088` |
| 구 URL 동작 | 직접 은행별 페이지 응답 | `https://exchange.mibank.me/bank`로 302 redirect, 은행 코드 유실 |
| 테이블 | `div.box_contents1 table tbody` | `table.main_table.content tbody` |
| 통화 코드 | `href`의 `currency=USD` query | 국기 이미지 파일명 `flag_usd_*.png` |
| 기준환율 | 마지막 rate cell | `기준환율(원)` 헤더 컬럼 |

기존 `requests.get()`은 redirect를 따라가지만, redirect target에 `bank_cd`가 없어 기본 은행 또는 전체 페이지로 이동한다. 또한 새 HTML은 기존 selector와 맞지 않아 `mibank 테이블을 찾을 수 없음` 또는 필수 통화 누락 예외가 발생한다.

---

## 변경 내용

| 파일 | 변경 내용 |
|------|----------|
| `app/crawlers/utils.py` | 새 MIBANK DOM 파싱 지원, `flag_<code>` 기반 통화 추출, `기준환율(원)` 헤더 컬럼 기반 환율 추출 |
| `app/crawlers/kb.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/hana.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/shinhan.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/nh.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/sc.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/ibk.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/woori.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/bs.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |
| `app/crawlers/citi.py` | MIBANK URL을 `exchange.mibank.me/bank?bank_cd=`로 변경 |

---

## 검증

### 라이브 MIBANK 파싱 검증

```bash
DATABASE_URL=sqlite:///./data/test.db python - <<'PY'
from app.crawlers.utils import crawl_mibank_rates

banks = {
    "kb": "004",
    "hana": "005",
    "shinhan": "088",
    "nh": "011",
    "sc": "023",
    "ibk": "003",
    "woori": "020",
    "bs": "032",
    "citi": "027",
}

for bank, code in banks.items():
    url = f"https://exchange.mibank.me/bank?bank_cd={code}"
    rates = crawl_mibank_rates(url, bank, required_codes=("USD", "JPY", "EUR"), require_all=True)
    print(bank, rates)
PY
```

기대 결과:

- 9개 은행 모두 `usd-krw`, `jpy-krw`, `eur-krw` 반환
- `validate_rate_ranges()` 기준 범위 내 값

### 문법 검증

```bash
python -m py_compile app/crawlers/utils.py app/crawlers/kb.py app/crawlers/hana.py app/crawlers/shinhan.py app/crawlers/nh.py app/crawlers/sc.py app/crawlers/ibk.py app/crawlers/woori.py app/crawlers/bs.py app/crawlers/citi.py
```

---

## 운영 확인

### 배포 전 현상 확인

```bash
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32
docker logs exchange-rate-app --since 15m 2>&1 | grep -E "selenium_wrapper_(shinhan|nh)|subprocess 크롤링 시작|subprocess 완료"
```

배포 전에는 신한/NH가 매분 Selenium subprocess로 실행되는 로그가 반복된다.

### 배포 후 확인

```bash
docker logs exchange-rate-app --since 10m 2>&1 | grep -E "MIBANK_(SHINHAN|NH|SC|IBK)_URL 크롤링 실패|selenium_wrapper_(shinhan|nh|sc|ibk)"
```

정상 상태:

- `MIBANK_*_URL 크롤링 실패` 반복 없음
- 신한/NH/SC/IBK가 정상 MIBANK 수집 시 Selenium fallback으로 내려가지 않음

---

## 재발 시 체크리스트

1. 구 URL이 redirect되는지 확인:

```bash
curl -sI "https://www.mibank.me/exchange/bank/index.php?search_code=088" | grep -i location
```

2. 신 URL이 은행 코드를 유지하는지 확인:

```bash
curl -L -s -o /tmp/mibank.html -w "http=%{http_code} final=%{url_effective} size=%{size_download}\n" "https://exchange.mibank.me/bank?bank_cd=088"
```

3. DOM selector 확인:

```bash
grep -E "main_table content|flag_usd|기준환율" /tmp/mibank.html | head
```

4. 파서 검증:

```bash
DATABASE_URL=sqlite:///./data/test.db python - <<'PY'
from app.crawlers.utils import crawl_mibank_rates
print(crawl_mibank_rates("https://exchange.mibank.me/bank?bank_cd=088", "shinhan"))
PY
```

---

## 교훈

1. **URL redirect 성공은 파싱 성공이 아니다**: HTTP 200이어도 은행 코드 유실 또는 DOM 변경으로 데이터가 깨질 수 있다.
2. **Primary MIBANK 크롤러는 fallback 부하를 즉시 만든다**: 신한/NH/SC/IBK는 MIBANK 실패가 Selenium 실행 증가로 바로 이어진다.
3. **통화와 컬럼을 모두 의미 기반으로 잡아야 한다**: 통화는 `flag_<code>`, 환율은 `기준환율(원)` 헤더로 매칭해 행/컬럼 순서 변경 리스크를 줄인다.

---

**작성일**: 2026-04-27
**작성자**: OpenAI Codex
