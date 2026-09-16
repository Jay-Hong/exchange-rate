# 100개 접속 시험 준비 — 운영 공개 경로, 단일 출발지

2026-09-16. 사용자가 운영 서버 대상으로 **준비**를 승인했다. 이 문서는 실제 부하 실행·계정 생성·서버 설정 변경 승인을 대신하지 않는다. 실행자는 Codex이며, 실행 전 최종 계획 hash와 시간대를 확인한다.

## 이번에 준비한 범위

- `scripts/capacity_100.py`: 기본 실행은 오프라인 계획 출력이다. `https://fxi.kr`의 기존 Nginx를 통과한다. 직접 앱 포트 호출·IP 제한 완화·캐시 삭제·운영 재시작은 하지 않는다.
- 10 → 30 → 50 → 100개 **연결**을 단계적으로 시험한다. 단계마다 이전 소켓을 모두 닫는다. 동일 시험 계정 재사용 가능, 실제 사용자 100명/출발지 100개와 동등하다고 쓰지 않는다.
- 5개 topic을 하나의 subscribe에 묶는다: USD/JPY/EUR/USDT/DXY. KRX 권한 경로는 이번 프로필에 없다. 앱의 모든 요청·재시도·권한 전이를 재현하는 iOS 시뮬레이터가 아니다.
- 연결은 120초 창에 분산한다. **전원 ACK와 최초 유효 snapshot을 받은 뒤** 유지 시간을 시작한다. 10/30/50개는 150초, 100개는 10분 유지한다.
- 모드 `mixed`: 연결 유지 중 각 client가 catalog 1회와 선택 탭 1일 그래프 1회를 순차 호출한다. 120초 창에 분산한다. 최대 단계 전체 380회 + 계정 사전 확인 catalog(계정당 1회) + health 2회다.
- 모드 `steady`: WS 접속·전송 유지만 확인한다(계정 사전 확인 REST는 있다).
- 모드 `reconnect`: 유지 후 **시험 소켓만** 닫고 120초 창에 재연결해 1분 유지한다. 서버 재시작이나 100개 동시 handshake 폭주는 아니다. 자동 재시도는 0회다.
- 한 번에 한 모드만 실행한다. 기본 mixed는 최대 1,880초(31분 20초), reconnect는 2,680초(44분 40초)이며 코드가 전체 45분을 넘는 계획을 거부한다. 실행 중 토큰 refresh·lease 갱신은 하지 않으며, 남은 전체 실행 시간보다 짧은 토큰과 유지 시간을 덮지 못하는 lease는 시작/진행을 거부한다.

## 제3자 인증 서비스 보호 (실행 전 보완)

- 현재 premium 검증은 RevenueCat **v1** `GET /subscribers/{id}`를 쓴다. [RevenueCat 직원의 2024-08-15 안내](https://community.revenuecat.com/general-questions-7/what-are-the-current-rate-limits-on-the-rest-api-4946)는 가변 제한과 **초당 약 1회 권고**다. 프로젝트의 현재 계약·할당량이나 안전 보장이 아니며 v2의 RPM을 대신 적용하지 않는다. 이 프로젝트에 적용되는 허용량·다른 트래픽 여유는 **미확인, 실행 전 확인 필요**다.
- 시험 발생기 하나에서 모든 WS subscribe와 인증 REST 시작에 **공유 최소 간격 1초**를 적용한다. 연결 램프만 믿지 않으며, 지연 뒤 밀린 시작을 한꺼번에 따라잡지 않는다. preflight catalog도 포함한다. HTTP 응답을 기다리는 동안 다음 요청은 가능하므로 응답 전체를 직렬화하는 장치는 아니다.
- 이는 **클라이언트 요청 시작률** 제한이다. 네트워크/서버 큐에서 도착이 모이거나 다른 사용자가 API를 쓰는 경우, 외부 서비스 전체 도착률까지 제한하지 않는다. RC 실호출 수도 아니다(REST 캐시, 권한 분기, 실패에 따라 다름).
- mixed는 WS subscribe 190회 + 인증 REST 380회 + 입력 토큰별 preflight catalog, reconnect는 subscribe 380회 + 입력 토큰별 preflight catalog다. REST를 포함한 잠재적 외부 호출을 따로 세며, 같은 계정의 REST 캐시 적중을 안전 근거로 가정하지 않는다.
- mixed의 REST 작업은 120초 창에 예약하되 공유 간격 때문에 실제 전송이 더 길어질 수 있다(100개에서 200회). WS 유지 시간을 넘으면 실패로 중단한다. 이것을 원래의 무제한 동시 복귀 버스트 시험으로 해석하지 않는다.
- 시험 연결의 `subscription_error`/거부 ACK는 첫 발생에 중단한다. wire의 `temporarily_unavailable`만으로 RevenueCat 장애라고 단정하지 않는다. 추가로 기존 admin 집계의 `premium_rc.by_outcome.unavailable_transient`/`unavailable_persistent`가 기준 표본보다 늘면 첫 관측에 중단한다. 이 집계는 **현재 단일 앱 프로세스 전체 WS 시도**이며 REST·다른 RC 소비자를 포함하지 않고, 증가를 시험 탓으로 단정하지 않는다.
- 집계의 process identity 변경, 카운터 감소, 필드 누락도 중단한다. 기존 실패 누적값을 새 실패로 세지 않는다. 관측은 주기적이므로 실제 실패 발생과 중단 사이에 지연이 있다. 최초 baseline 이후 두 번째 정상 표본을 받은 다음 부하를 시작한다.
- **저사용 시간대와 exact plan을 사용자에게 제시하고 운영 실행 승인을 받은 뒤** 실행한다. 공개 권고 확인만으로 프로젝트별 허용량 확인이 끝났다고 기록하지 않는다.

출발지 1개의 공개 REST 예산은 기존 `3r/s, burst 20`을 다른 API·사용자와 공유한다. 위 분산은 시험 도착률을 줄일 뿐, 429가 절대로 없음을 보장하지 않는다. 429가 발생하면 중단하고 해당 경로의 결과로 남긴다. **Nginx를 우회해 성공시킨 결과로 대체하지 않는다.**

## 자동 중단과 결과 해석

임계값은 이번 준비용 보수적 운영 정책이며 용량 실측으로 확정된 SLO가 아니다.

- HTTP non-200/응답 계약 위반, 잘못된 ACK·최초 데이터 미수신, 예상 밖 WS 종료: 첫 발생에 중단한다(종전 2% 기준보다 보수적).
- endpoint/단계별 20개 이상 표본에서 REST p95 > 3초, subscribe ACK p95 > 5초, 연결 시작부터 전 topic 최초 수신 p95 > 10초: 중단한다. 개별 연결·요청 deadline은 15초다.
- **컨테이너 CPU quota 대비** 90% 초과가 30초 지속하거나, memory limit 대비 90% 이상이면 중단한다. CPU 0.9 core / memory 800 MiB가 달라져도 후보를 다시 검토하도록 중단한다.
- health 실패, telemetry 유실/오래된 시각, 감시 프로세스 종료, SIGINT/SIGTERM, 전체 시간 상한: 부하 task를 취소하고 시험 소켓을 닫는다.
- telemetry는 cgroup v2와 로컬 `/health`, 기존 `/admin/api/ws-connection-metrics`를 읽는다. admin 비밀번호는 기존 수집기처럼 **컨테이너 내부 환경에서만 읽어** Basic 인증에 사용하고 밖으로 내보내지 않는다. 앱 모듈 import·env dump·config 변경·프로세스 재시작은 없다. 집계 응답 원문은 저장하지 않고 고정 숫자 필드만 내보낸다.
- 감시는 SSH stdin heartbeat가 끝나거나 6초 이상 없으면 종료한다. 진행 중 2초 대기/각 최대 3초 health·집계 요청 때문에 종료 관측은 더 늦을 수 있으며 독립 45분 상한도 있다.

성공 상태 이름은 `completed_bounded_scenario`다. 이는 지정 동작을 끝냈다는 뜻이다. `sessions_with_updates_on_all_topics`와 개별 `ws_end.frames`를 함께 보아야 한다. 최초 snapshot만 받은 연결을 지속 전송 성공으로 세지 않는다. 시장 데이터가 바뀌지 않은 시간대라 후속 전송이 없으면 해당 축은 미측정이다. 수신 간격은 서버→단말 전달 지연이 아니며 고정 1초/10초 수신을 요구하지 않는다.

기록: `plan.json`, `events.jsonl`, `result.json`. 사건 순번·UTC·monotonic 경과시간, endpoint별 상태/표본 수/p50/p95/max, ACK·최초 수신 시간, 실제 최대 연결 수와 정리 후 열린 연결 수를 남긴다. 토큰·UID·이메일·응답 원문·가격은 기록하지 않는다. 결과 경로가 이미 있으면 덮어쓰지 않는다.

## 로컬 준비 및 실행 방법

필요한 도구 전용 의존성: Python 3.11+, `websockets==16.0`, `httpx==0.28.1`, 시험용 `pytest==9.0.1`. 제품 requirements는 바꾸지 않는다.

현재 준비된 Python:

```sh
CAPACITY_PY=/Users/jay/Downloads/FXi-Release-Prep-20260916-4zwe64r6/capacity-tool-venv/bin/python
cd /Users/jay/Downloads/Projects/FXi/wt-capacity-100
"$CAPACITY_PY" -m pytest -q tests/test_capacity_100.py tests/test_ws_auth_load.py
"$CAPACITY_PY" scripts/capacity_100.py --scenario mixed
```

마지막 명령은 네트워크·토큰 조회 없이 plan과 `plan_sha256`을 출력한다. 코드/collector/대상/모드/단계를 바꾸면 hash가 달라진다. 실행 전에 승인된 hash와 다시 대조한다.

실제 부하는 아래 형태로 실행한다. `<...>`는 검토한 계획 hash와 새 증거 경로로 채운다. 토큰 파일은 소유자가 현재 사용자이고 권한 600인 regular file이어야 하며 내용은 `ID token 문자열 배열`이다. 토큰을 shell history/argv에 직접 넣지 않는다. 기존 승인된 프리미엄 시험 계정만 사용한다.

```sh
"$CAPACITY_PY" scripts/capacity_100.py --scenario mixed --execute \
  --plan-sha256 '<offline plan hash>' \
  --token-file '<private token array file>' \
  --output '<new absolute evidence directory>'
```

기존 안전한 토큰 발급 경로와 연결할 때는 파일 대신 `--tokens-stdin`을 사용해 JSON 배열을 pipe로 전달할 수 있다. 터미널에 직접 붙여 넣는 모드는 거부한다. JWT claim decoding은 만료 사전 확인/계정 수 집계만 하며 서명 검증을 대신하지 않는다. 서버 인증·구독 검증은 그대로 실행된다.

운영 SSH는 기존 `ubuntu@fxi.kr`, `/Users/jay/fxi-server-key-pair.pem`, `exchange-rate-app`을 대상으로 host-key 검증을 유지한다. collector 연결 실패 시 부하는 시작하지 않는다. 원본 token JSON은 결과 폴더나 Git에 넣지 않는다.

## 남겨 둘 미검증 범위

1. **서로 다른 IP들의 동시 복귀**: 별도 출발지 발생기가 있어야 한다. 현재 도구는 한 출발지에서 pacing을 적용하는 시험이다. 같은 IP 보호 한계 시험과 다중 IP 처리량을 혼동하지 않는다.
2. **서로 다른 사용자 100명의 인증·구독 상태**: REST `subscription.py:verify_premium_status`에는 UID별 캐시가 있다. WS `topic_authorization.py:_observe_premium`은 필요할 때 `fetch_revenuecat_result`를 직접 호출한다. 두 경로를 같은 캐시 비용으로 묶지 않는다. 이번 계정 재사용 시험은 사용자별 cold 상태의 동등성을 보장하지 않는다.
3. **연결 수와 인증 횟수**: `main.py:verify_ws_subscribe_token`은 subscribe 메시지 검증 경로다. 새 연결의 최초 subscribe뿐 아니라 같은 연결의 재구독도 비용을 낼 수 있다. `check_revoked=True`의 외부 조회와 전용 executor 대기·실행을 구분한다.
4. **서버 이벤트 루프 지연**: 현재 도구가 직접 측정하지 않는다. 기존 default-executor queue probe나 클라이언트 지연을 그 값으로 부르지 않는다. 관련 계측을 추가하려면 별도 제품 변경 검토가 필요하다.
5. **3967ms → 31ms**: `FREE_TIER_ACCESS_MODEL_PLAN.md` 및 `app/auth_executor.py`의 공용 executor 동거 작업 지연 실험이다. 100명 ACK·재연결 완료 시간의 기준선이 아니다.
6. **앱의 429 복구·KRX 권한·캐시 미스**: 앱 시험/별도 권한 프로필/서버 측 cache hit 증거가 필요하다. 오래된 cache를 유지하는 것은 영향이 없다는 뜻이 아니다. 이번 준비에서 캐시를 강제로 비우거나 시험용 계정 권한을 부여하지 않는다.

## 검토 범위

부하 도구·collector·독립 테스트·이 문서만 추가한다. 제품 코드, nginx, Compose, 인증/과금, 크롤러는 변경하지 않는다. `canary_monitor.py --rehearse`를 호출하거나 기존 모의 부하를 실제 부하로 해석하지 않는다.

Docs impact: 이 새 실행 절차의 정본은 본 문서다. 기존 제품 API·스키마·env·스케줄러 동작 변경은 없으므로 기존 운영 문서 수정은 필요 없다. 의존 API 참고: [websockets client](https://websockets.readthedocs.io/en/16.0/reference/asyncio/client.html), [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/).
