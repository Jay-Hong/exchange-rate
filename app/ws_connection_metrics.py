"""`/ws` 연결 계측 — nginx ingress 상한을 **관측 위에서** 정하기 위한 최소 계측.

왜 앱에서 재는가: nginx access log 로는 **할 수 없다**(실측).
- 실제 쓰이는 포맷은 `nginx/nginx.conf` 의 `main` 인데 `$request_time` · `$limit_req_status` ·
  `$limit_conn_status` 가 없다(`json_combined` 는 정의만 되고 어디서도 쓰이지 않는다).
- ⛔ 더 근본적으로 **WebSocket access log 는 연결이 끝날 때 기록된다.** `/ws` 101 로그를 세면
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

    def record(self, now: float | None = None) -> None:
        index = int((time.monotonic() if now is None else now) // self._bucket_seconds)
        self._counts[index] = self._counts.get(index, 0) + 1
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
        }


def connections_per_ip_histogram(counts_by_ip: dict[str, int]) -> dict[str, Any]:
    """⛔ 원시 IP 를 내보내지 않는다 — "연결 수 → 그 연결 수를 가진 IP 개수" 만 준다.

    ⚠️ `limit_conn` 값은 이 **상위 꼬리**를 덮어야 한다. 모바일 carrier NAT 는 여러 사용자를 한 IP
    로 묶으므로, 꼬리를 모르고 값을 정하면 정상 사용자를 자른다.
    """
    histogram: dict[str, int] = {}
    for connections in counts_by_ip.values():
        key = str(connections)
        histogram[key] = histogram.get(key, 0) + 1
    return {
        "distinct_ips": len(counts_by_ip),
        "max_connections_per_ip": max(counts_by_ip.values(), default=0),
        "histogram": histogram,
    }
