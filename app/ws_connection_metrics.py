"""`/ws` 연결 계측 — nginx ingress 상한을 **관측 위에서** 정하기 위한 최소 계측.

왜 앱에서 재는가: nginx access log 로는 **할 수 없다**(실측).
- (구 결함, `9821974` 에서 해소) `main` 포맷에 `$request_time` · `$limit_req_status` ·
  `$limit_conn_status` 가 **없었다** — 지금은 있다. 그래도 아래 한계는 그대로다.
- ⛔ **WebSocket access log 는 연결이 끝날 때 기록된다** — 이건 필드를 추가해도 해소되지 않는다. `/ws` 101 로그를 세면
  handshake **유입률**이 아니라 **종료된 연결의 기록률**을 보게 되고, 장수명 연결은 아직 로그에
  없어 **현재 동시 연결 수와 IP 별 분포를 알 수 없다**. 그 둘이 `limit_conn` 값을 정하는 근거다.

⛔ **원시 IP 를 밖으로 내보내지 않는다.** 내부 map 은 값을 세는 데만 쓰고, 노출은 **IP 당 연결 수
히스토그램 · 최댓값 · 총 연결 수**뿐이다.
⚠️ 계측은 **process-local** 이다. 지금은 Uvicorn worker 1개라 그것이 전체값이지만, worker 를 늘리면
합산 없이는 전체가 아니다.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any

# handshake 피크는 **누적 합계로 계산할 수 없다** — 고정 크기 시간 버킷이 필요하다.
# ⛔ 무제한 시계열·IP history 를 만들지 않는다(메모리가 유입에 비례해 자란다).
HANDSHAKE_BUCKET_SECONDS = 10
HANDSHAKE_BUCKETS_KEPT = 60          # 10초 × 60 = 최근 10분

UNKNOWN_IP = "unknown"


def client_ip_from_headers(headers: Any) -> str:
    """⛔ **`X-Forwarded-For` 첫 값을 믿지 말 것** — 클라가 보낸 값이 그대로 앞에 붙어(`nginx` 는
    `$proxy_add_x_forwarded_for` 로 **append** 한다) spoof 가 된다.
    `/ws` 블록의 nginx 는 `X-Real-IP` 를 `$remote_addr` 로 **덮어쓰므로** 그것만 쓴다.

    ⚠️ nginx 를 우회한 직접 접근이면 헤더가 없거나 형식이 깨진다 → `"unknown"` 으로 모은다
    (그 자체가 신호다 — 정상 경로라면 0이어야 한다).
    """
    raw = None
    try:
        raw = headers.get("x-real-ip")
    except Exception:
        return UNKNOWN_IP
    if not raw:
        return UNKNOWN_IP
    candidate = raw.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return UNKNOWN_IP
    return candidate


class HandshakeBuckets:
    """고정 크기 시간 버킷. **피크**를 보려면 이게 있어야 한다(누적 합계로는 못 낸다)."""

    def __init__(self, bucket_seconds: int = HANDSHAKE_BUCKET_SECONDS,
                 buckets_kept: int = HANDSHAKE_BUCKETS_KEPT) -> None:
        if bucket_seconds <= 0 or buckets_kept <= 0:
            raise ValueError("bucket_seconds / buckets_kept 는 양수여야 한다")
        self._bucket_seconds = bucket_seconds
        self._buckets_kept = buckets_kept
        self._counts: dict[int, int] = {}
        # ⛔ **eviction 과 무관한** 수명 전체 피크. ring 은 600초 rolling 인데 캡처는 24h 간격이라
        #    이 필드가 없으면 관측 창의 대부분이 evict 된다 — ring 채택의 전제다.
        #    ⚠️ 기존 `max_in_bucket`(보존 버킷 중 최댓값 = 의도된 rolling gauge)과 **별개**다.
        self._peak_since_start = 0

    def record(self, now: float | None = None) -> None:
        index = int((time.monotonic() if now is None else now) // self._bucket_seconds)
        self._counts[index] = self._counts.get(index, 0) + 1
        # ⚠️ **record 직후** 갱신한다 — snapshot 에서 하면 snapshot 이 안 불린 창의 피크를 놓친다.
        if self._counts[index] > self._peak_since_start:
            self._peak_since_start = self._counts[index]
        self._evict(index)

    def _evict(self, newest: int) -> None:
        oldest_kept = newest - self._buckets_kept + 1
        for index in [i for i in self._counts if i < oldest_kept]:
            del self._counts[index]

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        index = int((time.monotonic() if now is None else now) // self._bucket_seconds)
        self._evict(index)
        return {
            "bucket_seconds": self._bucket_seconds,
            "buckets_kept": self._buckets_kept,
            "buckets_present": len(self._counts),
            "max_in_bucket": max(self._counts.values(), default=0),
            "current_bucket": self._counts.get(index, 0),
            # ⛔ additive — 기존 5키의 값·의미는 그대로다(S6-5).
            "peak_in_bucket_since_start": self._peak_since_start,
        }


def connections_per_ip_histogram(counts_by_ip: dict[str, int]) -> dict[str, Any]:
    """⛔ 원시 IP 를 내보내지 않는다 — "연결 수 → 그 연결 수를 가진 IP 개수" 만 준다.

    ⚠️ `limit_conn` 값은 이 **상위 꼬리**를 덮어야 한다. 모바일 carrier NAT 는 여러 사용자를 한 IP
    로 묶으므로, 꼬리를 모르고 값을 정하면 정상 사용자를 자른다.

    ⛔ **`unknown` 을 실제 IP 하나처럼 섞지 말 것.** 한때 그렇게 해서, `X-Real-IP` 가 전부 누락된
    100 연결이 `histogram={"100": 1}` 로 보였다 — carrier NAT 한 IP 의 100 연결과 **구분되지
    않는다**. 그 값으로 `limit_conn` 을 정하면 판단이 **정반대**로 간다(전자는 "IP 데이터가 없다",
    후자는 "상한을 높게 잡아야 한다"). 그래서 `unknown` 은 **분리해서** 따로 센다.
    """
    known = {ip: n for ip, n in counts_by_ip.items() if ip != UNKNOWN_IP}
    histogram: dict[str, int] = {}
    for connections in known.values():
        key = str(connections)
        histogram[key] = histogram.get(key, 0) + 1
    return {
        # ↓ 전부 **known IP 만** 기준이다.
        "distinct_ips": len(known),
        "max_connections_per_ip": max(known.values(), default=0),
        "histogram": histogram,
        "known_connections": sum(known.values()),
        # ⚠️ nginx 를 우회한 직접 접근(또는 헤더 손상) 건수. **0 이 정상**이고, 0이 아니면
        #    위 known 통계의 대표성부터 의심해야 한다.
        "unknown_connections": counts_by_ip.get(UNKNOWN_IP, 0),
    }
