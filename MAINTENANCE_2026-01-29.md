# Investing Cloudflare 403 차단 대응

> 📅 **날짜**: 2026-01-29
> 📚 **관련 문서**: [ADR-018](DECISIONS.md#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장), [CRAWLERS.md](CRAWLERS.md), [CHANGELOG.md](CHANGELOG.md)

---

## 장애 타임라인

> ⏱️ 아래 시각은 로그 기준 추정 시각입니다.

### 1차 장애

| 시점 | 이벤트 |
|------|--------|
| 2026-01-27 23:40 KST | Investing 크롤러 403 Forbidden 시작 |
| 2026-01-28 05:23 KST | 자연 복구 (Cloudflare 차단 해제) |
| **총 장애 시간** | **약 5시간 42분** |

### 2차 장애

| 시점 | 이벤트 |
|------|--------|
| 2026-01-28 19:27 KST | Investing 크롤러 403 재발 |
| 2026-01-29 ~03:00 KST | UA 로테이션 + Jitter + Circuit Breaker 배포 (효과 없음) |
| 2026-01-29 ~12:33 KST | curl_cffi + safari17_0 배포 → **복구 완료** |
| **총 장애 시간** | **약 17시간** |

---

## 근본 원인

**Cloudflare TLS fingerprint (JA3/JA4) 탐지**

- Python `requests` 라이브러리는 고유한 TLS 핸드셰이크 패턴을 가짐
- Cloudflare는 HTTP 헤더(User-Agent 등)가 아닌 TLS 핸드셰이크 단계에서 봇을 탐지
- User-Agent를 아무리 변경해도 TLS 지문이 동일하면 차단됨

```
Browser TLS handshake: ClientHello에 특정 cipher suite 순서, 확장 필드 포함
requests TLS handshake: urllib3/OpenSSL 기반 → 브라우저와 다른 지문
curl_cffi TLS handshake: libcurl-impersonate 기반 → 실제 브라우저 지문 모방
```

---

## 단계별 대응 과정

### 1단계: UA 로테이션 + Jitter + Circuit Breaker (실패)

**적용 내용:**
- impersonate에 맞는 UA 풀에서 랜덤 선택
- 0~2초 Jitter (요청 타이밍 분산)
- Circuit Breaker (연속 403 → 점진적 쿨다운)
- 상태 전이 로깅 (로그 폭주 방지)

**결과:** 403 지속 — TLS 지문 탐지에는 무효 (단, Circuit Breaker와 로그 억제는 운영 안정성에 기여)

### 2단계: curl_cffi + chrome131 (실패)

**적용 내용:**
- `curl_cffi` 설치 (`pip install curl_cffi`)
- `impersonate="chrome131"` 설정

**결과:** 여전히 403 — Cloudflare가 Chrome 최신 지문도 차단

### 3단계: curl_cffi + safari17_0 (성공)

**적용 내용:**
- `impersonate="safari17_0"` 변경
- Safari UA 풀과 매칭

**결과:** 200 OK — Cloudflare 우회 성공

---

## 변경된 파일

| 파일 | 변경 내용 |
|------|----------|
| `app/crawlers/investing.py` | curl_cffi 도입, Circuit Breaker, UA 로테이션, Jitter, 로그 억제 |
| `requirements.txt` | `curl_cffi>=0.7.4,<1.0.0` 추가 |

---

## 복구 플레이북

### Cloudflare 403 재발 시 대응 절차

**1. 현상 확인:**
```bash
# 서버 로그에서 InvestingForbidden 확인
ssh -i ~/fxi-server-key-pair.pem ubuntu@3.36.30.32
docker logs exchange-rate-app 2>&1 | grep -i "investing.*403\|InvestingForbidden\|차단"
```

**2. 수동 테스트:**
```bash
# Docker 컨테이너 내부에서 직접 테스트
docker exec -it exchange-rate-app python3 -c "
from curl_cffi import requests
r = requests.get('https://kr.investing.com/currencies/exchange-rates-table',
                  impersonate='safari17_0', timeout=10)
print(f'Status: {r.status_code}')
print(f'Length: {len(r.text)}')
"
```

**3. impersonate 변경 시도:**
```python
# safari17_0이 차단된 경우 다른 옵션 시도
# 실측 확인된 impersonate 값:
# chrome131 (2026-01-29 실측: 403 차단됨)
# safari17_0 (2026-01-29 실측: 200 OK)
# 그 외 값은 curl_cffi 공식 문서에서 지원 여부 확인 후 테스트
CFFI_IMPERSONATE = "safari17_0"
```

**4. 배포:**
```bash
# 서버에서 재배포
cd exchange-rate
git pull
docker compose up -d --build
docker logs -f exchange-rate-app | grep investing
```

**5. 최후 수단 (Proxy):**
- IP 레벨 우회가 필요한 경우 Proxy 서비스 도입 검토
- 비용 발생 ($5~20/월), 지연 증가 (100~300ms)

---

## 교훈

1. **User-Agent 변경은 Cloudflare에 무의미**: TLS 핸드셰이크 단계에서 탐지
2. **impersonate 선택이 중요**: chrome131은 차단되나 safari17_0은 통과 — Cloudflare의 차단 정책은 브라우저별로 다름
3. **Circuit Breaker는 여전히 유용**: 차단 시 불필요한 요청을 줄여 IP 평판 보호
4. **로그 억제 필수**: 10초마다 반복되는 403을 모두 로깅하면 로그 폭주

---

**작성일**: 2026-01-29
**작성자**: Claude Code & OpenAI Codex
