"""KRX 미국달러선물 (KIS Open API) — operational glue (PR6a-1).

`app/sources/kis_futures.py` pure adapter를 운영에 연결하기 위한 골격.

PR6a-1 범위 (이 파일):
  - approval_key cache/refresh/lock (asyncio.Lock으로 동시 발급 방지)
  - active session → TR 자동 선택 (CF 주간 / CM 야간)
  - WebSocket connect/subscribe + reconnect 상태 머신
  - tick → normalized payload (11 필드)
  - callback fanout hook (handler 등록은 PR6b 이후)
  - handler 예외는 logger.warning으로 가시화 (fanout 다른 handler에 영향 X)
  - A75605 static canary + front-month resolver TODO 명시

PR6a-1 미포함 (PR6b/c 이후):
  - REST access token helper (snapshot/fallback 시점 추가)
  - DB write (source_rates) — 1초 window + insert-if-changed 정책
  - Redis write (latest:source:krx:usd-krw-futures) — 매 tick SET
  - Alert evaluator hook — 매 tick 평가
  - scheduler 등록 / crawler_config 토글
  - front-month contract resolver — 마스터 파일 (`fo_com_code.mst`) 기반

운영 시 fanout 흐름 (PR6b/c 시점):
  WebSocket tick → on_tick() → callback fanout
                                  ├─ Redis latest SET (매 tick)
                                  ├─ Alert evaluator (매 tick 평가)
                                  └─ DB writer debouncer (1초 window의 last)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

import requests
import websockets

from app import config
from app.sources.kis_futures import (
    get_active_session,
    parse_h0cfasp0_payload,
    parse_h0cfcnt0_payload,
    parse_h0mfasp0_payload,
    parse_h0mfcnt0_payload,
)
from app.sources.kis_master import ContractInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIS_PROD_HOST = "https://openapi.koreainvestment.com:9443"
KIS_WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"

# approval_key 캐시 위치 — smoke script와 공유 (chmod 600 운영자 책임).
# PR6c 시점에 config로 분리 검토.
APPROVAL_CACHE_PATH = Path(".cache/kis_ws_approval.json")

# approval_key 만료 5분 전 갱신 (race condition 방지 마진).
APPROVAL_REFRESH_MARGIN_SEC = 300

# PR6d-1 — REST access_token 캐시 위치 (smoke script와 공유).
# WebSocket approval_key와 별도 토큰 (REST API 전용, 24h 유효).
ACCESS_TOKEN_CACHE_PATH = Path(".cache/kis_access_token.json")

# access_token 만료 5분 전 갱신 (approval_key와 같은 race 방지 정책).
ACCESS_TOKEN_REFRESH_MARGIN_SEC = 300

# A75605 static canary (2026-05-18 만기). PR6b/c 전 마스터 기반 resolver 필수.
# TODO(PR6b): front-month contract resolver — fo_com_code.mst 기반 자동 선택.
TR_KEY_STATIC = "A75605"
CONTRACT_MONTH_STATIC = "202605"
CONTRACT_EXPIRES_ON_STATIC = date(2026, 5, 18)

KST = ZoneInfo("Asia/Seoul")

# 세션별 TR 매핑 (smoke script와 동일 decision logic).
SESSION_TR_MAP: Dict[str, List[str]] = {
    "CF": ["H0CFCNT0", "H0CFASP0"],
    "CM": ["H0MFCNT0", "H0MFASP0"],
}

# tick parser dispatch.
PARSER_DISPATCH = {
    "H0CFCNT0": parse_h0cfcnt0_payload,
    "H0CFASP0": parse_h0cfasp0_payload,
    "H0MFCNT0": parse_h0mfcnt0_payload,
    "H0MFASP0": parse_h0mfasp0_payload,
}

# WebSocket reconnect backoff — Codex 권고 (1/2/4/8/16/30, 이후 30s).
RECONNECT_BACKOFF_SEQ = (1, 2, 4, 8, 16, 30)
RECONNECT_BACKOFF_TAIL = 30

# stale 전환 임계 — default 60s, env로 조정 가능 (PR6d-1).
STALE_AFTER_SEC = config.KRX_STALE_SEC


# ---------------------------------------------------------------------------
# Normalized payload (PR6a 출력)
# ---------------------------------------------------------------------------

def make_normalized_payload(
    parsed: Dict[str, str],
    *,
    session: str,
    tr_id: str,
    received_at_kst: datetime,
    contract_code: str = TR_KEY_STATIC,
    contract_month: str = CONTRACT_MONTH_STATIC,
    expires_on: date = CONTRACT_EXPIRES_ON_STATIC,
) -> Dict[str, Any]:
    """KIS 체결 (H0CFCNT0/H0MFCNT0) parser 결과를 PR6 표준 normalized payload로 변환.

    PR6a 최소 11 필드 — Codex 합의 양식. price는 str 유지 (DB writer가 변환).

    **체결 tick 전용**: futs_prpr (현재가)을 price로 사용. 호가 tick
    (H0CFASP0/H0MFASP0)은 fanout 대상 아님 — 호가가 체결보다 19× 빈도이고
    매도호가1을 대표값으로 저장하면 "현재가"가 아닌 잘못된 값이 되기 때문.
    호가 데이터 필요해지면 별도 payload_type 또는 별도 path 도입.
    """
    return {
        "source": "krx",
        "asset": "usd-krw-futures",
        "session": session,
        "contract_code": contract_code,
        "contract_month": contract_month,
        "expires_on": expires_on.isoformat(),
        "price": parsed.get("futs_prpr", ""),
        "market_time": parsed.get("bsop_hour", ""),
        "received_at": received_at_kst.isoformat(),
        "tr_id": tr_id,
        "status": "normal",
    }


# ---------------------------------------------------------------------------
# Approval key manager — asyncio.Lock + cache + refresh
# ---------------------------------------------------------------------------

class KisApprovalManager:
    """KIS WebSocket approval_key 발급/캐시 관리.

    동시 발급 방지 (asyncio.Lock) + 만료 5분 전 갱신 + 발급 실패 시 기존
    valid 캐시 사용. KIS 1일 1회 발급 원칙 + 잦은 발급 제한 대응.

    Note: REST access token은 PR6b 이후 (snapshot/fallback 시점)에 추가.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        cache_path: Path = APPROVAL_CACHE_PATH,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._cache_path = cache_path
        self._lock = asyncio.Lock()

    async def get_approval_key(self) -> str:
        """현재 valid한 approval_key 반환. 만료 임박 시 자동 갱신."""
        async with self._lock:
            cached = self._load_cache()
            if cached and self._is_fresh(cached):
                return cached["approval_key"]

            # 발급 필요 — 단, 발급 실패 시 기존 valid 캐시 fallback.
            try:
                # KIS REST 호출은 동기 requests. asyncio 이벤트 루프 블로킹
                # 회피를 위해 to_thread로 실행. PR6b/c에서 httpx async 검토.
                new_key = await asyncio.to_thread(self._issue_new)
                self._save_cache(new_key)
                return new_key["approval_key"]
            except Exception as e:
                logger.warning(
                    "[kis_approval] 발급 실패 — 기존 캐시 fallback 시도: %s: %s",
                    type(e).__name__, e,
                )
                if cached and self._is_still_valid(cached):
                    return cached["approval_key"]
                raise

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        if not self._cache_path.exists():
            return None
        try:
            return json.loads(self._cache_path.read_text())
        except Exception as e:
            logger.warning("[kis_approval] 캐시 읽기 실패: %s: %s", type(e).__name__, e)
            return None

    def _is_fresh(self, cached: Dict[str, Any]) -> bool:
        """만료 마진(5분) 이상 남아 있으면 True."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time() + APPROVAL_REFRESH_MARGIN_SEC

    def _is_still_valid(self, cached: Dict[str, Any]) -> bool:
        """만료 시각 자체는 안 지났으면 True (발급 실패 fallback 용)."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time()

    def _issue_new(self) -> Dict[str, Any]:
        """KIS REST: POST /oauth2/Approval — smoke script와 동일 endpoint."""
        url = f"{KIS_PROD_HOST}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        r = requests.post(url, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        approval_key = data.get("approval_key")
        if not approval_key:
            # 응답 전체 노출 X — 진단에 필요한 필드만 인용 (외부 API 응답 secret 가능성).
            raise RuntimeError(
                f"approval_key missing: rt_cd={data.get('rt_cd')} "
                f"msg_cd={data.get('msg_cd')} msg1={data.get('msg1')}"
            )
        # KIS approval_key는 명시적 expires_in이 응답에 없음 — 24h 가정 (smoke와 동일).
        # 실제 만료는 KIS 정책 의존, 보수적으로 23.5h로 두어 갱신 마진 확보.
        expires_in = 23 * 3600 + 1800
        now = time.time()
        return {
            "approval_key": approval_key,
            "expires_at_epoch": now + expires_in,
            "created_at_epoch": now,
        }

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            self._cache_path.chmod(0o600)
        except OSError as e:
            logger.warning("[kis_approval] chmod 600 실패: %s", e)


# ---------------------------------------------------------------------------
# REST access token (PR6d-1) — KIS REST API용 별도 토큰
# ---------------------------------------------------------------------------
# WebSocket approval_key와 다른 토큰 체계:
#   approval_key: WebSocket 핸드셰이크용 (KisApprovalManager)
#   access_token: REST API Bearer 인증용 (KisAccessTokenManager, 24h 유효)
# 캐시는 smoke script(scripts/kis_smoke.py)와 동일 형식·경로 공유.

class KisAccessTokenManager:
    """KIS REST access_token 발급/캐시 관리 (PR6d-1).

    KisApprovalManager와 같은 패턴 (lock + cache + refresh margin + fallback)
    이지만 endpoint와 cache 위치만 다르다.

    - endpoint: POST /oauth2/tokenP
    - cache: .cache/kis_access_token.json
    - 토큰 응답 필드: access_token / expires_in (보통 86400=24h)
    - smoke script와 호환 (cache 형식 동일, expires_at_text 추가 필드는 무시)

    failure isolation: 발급 실패 시 valid한 cached 토큰이 있으면 fallback.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        cache_path: Path = ACCESS_TOKEN_CACHE_PATH,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._cache_path = cache_path
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """현재 valid한 access_token 반환. 만료 임박 시 자동 갱신."""
        async with self._lock:
            cached = self._load_cache()
            if cached and self._is_fresh(cached):
                return cached["access_token"]

            try:
                # KIS REST 호출은 동기 requests. asyncio 이벤트 루프 블로킹
                # 회피를 위해 to_thread로 실행 (KisApprovalManager와 동일 패턴).
                new_token = await asyncio.to_thread(self._issue_new)
                self._save_cache(new_token)
                return new_token["access_token"]
            except Exception as e:
                logger.warning(
                    "[kis_token] 발급 실패 — 기존 캐시 fallback 시도: %s: %s",
                    type(e).__name__, e,
                )
                if cached and self._is_still_valid(cached):
                    return cached["access_token"]
                raise

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        if not self._cache_path.exists():
            return None
        try:
            return json.loads(self._cache_path.read_text())
        except Exception as e:
            logger.warning("[kis_token] 캐시 읽기 실패: %s: %s", type(e).__name__, e)
            return None

    def _is_fresh(self, cached: Dict[str, Any]) -> bool:
        """만료 마진(5분) 이상 남아 있으면 True."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time() + ACCESS_TOKEN_REFRESH_MARGIN_SEC

    def _is_still_valid(self, cached: Dict[str, Any]) -> bool:
        """만료 시각 자체는 안 지났으면 True (발급 실패 fallback 용)."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time()

    def _issue_new(self) -> Dict[str, Any]:
        """KIS REST: POST /oauth2/tokenP — smoke script와 동일 endpoint."""
        url = f"{KIS_PROD_HOST}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        r = requests.post(url, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(
                f"access_token missing: rt_cd={data.get('rt_cd')} "
                f"msg_cd={data.get('msg_cd')} msg1={data.get('msg1')}"
            )
        # KIS access_token 만료: 응답의 expires_in (초). 보통 86400=24h.
        expires_in = int(data.get("expires_in") or 86400)
        now = time.time()
        return {
            "access_token": token,
            "expires_at_epoch": now + expires_in,
            "created_at_epoch": now,
        }

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            self._cache_path.chmod(0o600)
        except OSError as e:
            logger.warning("[kis_token] chmod 600 실패: %s", e)


# ---------------------------------------------------------------------------
# REST quote snapshot helper (PR6d-1)
# ---------------------------------------------------------------------------
# WebSocket stale 동안 보조 snapshot 경로 (ADR-027 원칙: WebSocket primary /
# REST bounded fallback probe). 호출자는 stale gating + cooldown 정책으로
# REST 호출 빈도를 제한 (PR6d-2 stale orchestration 영역).

KIS_REST_QUOTE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-price"

# KIS 공식 inquire_price.py는 지수선물 샘플(FID_COND_MRKT_DIV_CODE=F)만
# 보여주지만, 2026-05-06 운영 smoke에서 상품선물 market code(CF/CM)를
# 쓰면 A75605 미국달러선물 output1.futs_prpr가 정상 반환됨을 확인했다.
KIS_REST_QUOTE_TR_ID = "FHMIF10000000"
KIS_REST_QUOTE_MARKET_DIV_CODE: Dict[str, str] = {
    "CF": "CF",  # 상품선물 정규세션
    "CM": "CM",  # 상품선물 야간세션
}


async def fetch_kis_futures_quote(
    *,
    contract: ContractInfo,
    token_manager: KisAccessTokenManager,
    session: Optional[Literal["CF", "CM"]] = None,
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """KIS REST inquire-price helper for KRX USD futures.

    PR6d-1: helper만 제공. stale gating / cooldown / 결과 처리는 PR6d-2.
    2026-05-06 운영 smoke 기준, path/TR은 지수선물 샘플과 동일하지만
    FID_COND_MRKT_DIV_CODE를 CF/CM으로 분기해야 A75605 USD futures price가
    output1.futs_prpr로 반환된다.

    Args:
        contract: ContractInfo (short_code 사용 — 예: A75605)
        token_manager: 발급/캐시된 access_token 제공
        session: "CF" 또는 "CM". None이면 현재 KST 기준 active session 자동 판정
        timeout: HTTP 요청 timeout (초). 기본 5초

    Returns:
        성공 시 normalized dict:
            {
                "source": "krx",
                "asset": "usd-krw-futures",
                "contract_code": "A75605",
                "contract_month": "202605",
                "expires_on": "2026-05-18",
                "price": "1457.4",  # str (raw KIS payload, 호출자가 정규화)
                "session": "CM",
                "market_div_code": "CM",
                "received_at": "2026-05-06T10:30:00+09:00",
                "raw_rt_cd": "0",
            }
        rt_cd != "0"이거나 응답 형식 이상 시 None (호출자가 처리).

    실패 모드:
        - access_token 발급 실패 → token_manager.get_access_token() 예외 propagate
        - HTTP 오류 → requests 예외 propagate
        - rt_cd != "0" → None 반환 (logger.warning 기록)
        - output dict 부재 → None 반환 (logger.warning 기록)

    스레드 안전성:
        async wrapper지만 requests는 sync. asyncio.to_thread로 실행하므로
        이벤트 루프 블로킹 없음 (KisApprovalManager._issue_new와 동일 패턴).
    """
    if session is None:
        session = get_active_session(datetime.now(KST))
    if session not in KIS_REST_QUOTE_MARKET_DIV_CODE:
        logger.warning("[kis_rest] active session 부재 — quote snapshot skip")
        return None

    market_div_code = KIS_REST_QUOTE_MARKET_DIV_CODE[session]
    token = await token_manager.get_access_token()

    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": token_manager._app_key,
        "appsecret": token_manager._app_secret,
        "tr_id": KIS_REST_QUOTE_TR_ID,
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div_code,
        "FID_INPUT_ISCD": contract.short_code,
    }
    url = f"{KIS_PROD_HOST}{KIS_REST_QUOTE_PATH}"

    def _sync_call() -> Dict[str, Any]:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    data = await asyncio.to_thread(_sync_call)

    rt_cd = data.get("rt_cd")
    if rt_cd != "0":
        logger.warning(
            "[kis_rest] inquire-price rt_cd=%s msg_cd=%s msg1=%s",
            rt_cd, data.get("msg_cd"), data.get("msg1"),
        )
        return None

    # KIS 응답: output 또는 output1/output2/output3 (TR/계좌별 다름)
    output = (
        data.get("output")
        or data.get("output1")
        or data.get("output2")
    )
    if not isinstance(output, dict):
        logger.warning(
            "[kis_rest] output dict 부재. top-level keys: %s",
            sorted(data.keys()),
        )
        return None

    price = output.get("futs_prpr") or output.get("prpr")
    if not price:
        logger.warning("[kis_rest] futs_prpr/prpr 필드 부재. output keys: %s",
                       sorted(output.keys()))
        return None

    return {
        "source": "krx",
        "asset": "usd-krw-futures",
        "contract_code": contract.short_code,
        "contract_month": contract.contract_month,
        "expires_on": contract.expiry_date.isoformat(),
        "price": str(price),  # raw KIS string. 호출자가 Decimal 정규화
        "session": session,
        "market_div_code": market_div_code,
        "received_at": datetime.now(KST).isoformat(),
        "raw_rt_cd": rt_cd,
    }


# ---------------------------------------------------------------------------
# WebSocket client — connect/subscribe + reconnect 상태 머신
# ---------------------------------------------------------------------------

TickHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class KisFuturesClient:
    """KRX 미국달러선물 WebSocket client.

    상태 머신: normal / reconnecting / stale
    - normal: active session, tick 정상 수신
    - reconnecting: disconnect 발생, backoff 재시도 중
    - stale: 60s 이상 tick 미수신 (운영 가시화)

    fanout: tick handler 0개로 시작 (PR6b에서 add_tick_handler로 등록).
    handler 예외는 logger.warning으로 가시화, fanout 다른 handler 영향 X.

    PR6a 한정: 운영 미연결. 외부에서 start() 호출하면 active session
    탐지 후 WebSocket 시작. None 세션이면 대기 (sleep + 주기 체크).
    """

    def __init__(
        self,
        approval_manager: KisApprovalManager,
        *,
        contract: Optional[ContractInfo] = None,
    ) -> None:
        """KIS futures WebSocket client.

        Args:
            approval_manager: approval_key cache/refresh helper.
            contract: 구독 대상 종목 (PR6c-2a). None이면 PR6a static
                (A75605, 2026-05-18 만기) — 단위 테스트 / 운영 미연결용.
                운영 진입 (PR6c-2b)에서는 select_active_usd_futures_contract
                결과 주입.
        """
        self._approval = approval_manager
        self._contract = contract or self._default_contract()
        self._tick_handlers: List[TickHandler] = []
        self._status: str = "normal"
        self._last_tick_at: Optional[float] = None
        self._stop = asyncio.Event()

        # PR6d-2a — raw frame metric state (silence 측정용, fallback 호출 X)
        # ADR-027 PR6d-2a 계획 참조
        now = time.time()
        self._started_at: float = now
        self._connected_at: Optional[float] = None
        self._active_session: Optional[str] = None
        # frame counters (체결/호가 분리 — user 우려 야간 false stale 검증용)
        self._frame_count_total: int = 0
        self._trade_frame_count: int = 0  # H0CFCNT0 / H0MFCNT0
        self._quote_frame_count: int = 0  # H0CFASP0 / H0MFASP0
        self._system_frame_count: int = 0
        self._malformed_frame_count: int = 0
        self._unknown_tr_id_count: int = 0
        # frame age tracking (분리)
        self._last_trade_frame_at: Optional[float] = None
        self._last_quote_frame_at: Optional[float] = None
        # status transition counters
        self._status_transition_count: Dict[str, int] = {
            "normal": 0, "reconnecting": 0, "stale": 0,
        }
        # reconnect attempt count (누적)
        self._reconnect_attempt_count: int = 0
        # gap buckets (전체/체결/호가 각각). boundary는 ADR-027 후보 그대로
        # 운영 분포 보고 boundary 조정 예정 (PR6d-2a 배포 후 baseline 분석)
        self._gap_buckets_total: Dict[str, int] = self._init_gap_buckets()
        self._gap_buckets_trade: Dict[str, int] = self._init_gap_buckets()
        self._gap_buckets_quote: Dict[str, int] = self._init_gap_buckets()
        # max gap (전체/체결/호가). 단순 max만 추적. p50/p95/p99는 외부 분석.
        self._max_frame_gap_sec: float = 0.0
        self._max_trade_gap_sec: float = 0.0
        self._max_quote_gap_sec: float = 0.0

    @staticmethod
    def _init_gap_buckets() -> Dict[str, int]:
        """gap bucket dict. ADR-027 후보 boundary."""
        return {
            "<=1s": 0, "<=2s": 0, "<=5s": 0, "<=10s": 0,
            "<=30s": 0, "<=60s": 0, ">60s": 0,
        }

    @staticmethod
    def _bucket_for(gap_sec: float) -> str:
        """gap_sec을 bucket 라벨로 매핑."""
        if gap_sec <= 1:
            return "<=1s"
        if gap_sec <= 2:
            return "<=2s"
        if gap_sec <= 5:
            return "<=5s"
        if gap_sec <= 10:
            return "<=10s"
        if gap_sec <= 30:
            return "<=30s"
        if gap_sec <= 60:
            return "<=60s"
        return ">60s"

    def get_metrics(self) -> Dict[str, Any]:
        """현재 metric state snapshot (PR6d-2a, ADR-027).

        admin endpoint / summary log / 테스트에서 호출. read-only.
        """
        now = time.time()
        return {
            "status": self._status,
            "active_session": self._active_session,
            "contract": {
                "code": self._contract.short_code,
                "month": self._contract.contract_month,
                "expires_on": self._contract.expiry_date.isoformat(),
            },
            "lifecycle": {
                "started_at": self._started_at,
                "connected_at": self._connected_at,
                "uptime_sec": int(now - self._started_at),
            },
            "last_frame_age_sec": (
                int(now - self._last_tick_at) if self._last_tick_at else None
            ),
            "last_trade_frame_age_sec": (
                int(now - self._last_trade_frame_at) if self._last_trade_frame_at else None
            ),
            "last_quote_frame_age_sec": (
                int(now - self._last_quote_frame_at) if self._last_quote_frame_at else None
            ),
            "counters": {
                "frame_total": self._frame_count_total,
                "trade_frame": self._trade_frame_count,
                "quote_frame": self._quote_frame_count,
                "system_frame": self._system_frame_count,
                "malformed_frame": self._malformed_frame_count,
                "unknown_tr_id": self._unknown_tr_id_count,
                "reconnect_attempt": self._reconnect_attempt_count,
                "status_transitions": dict(self._status_transition_count),
            },
            "gap_buckets": {
                "total": dict(self._gap_buckets_total),
                "trade": dict(self._gap_buckets_trade),
                "quote": dict(self._gap_buckets_quote),
            },
            "max_gap_sec": {
                "total": self._max_frame_gap_sec,
                "trade": self._max_trade_gap_sec,
                "quote": self._max_quote_gap_sec,
            },
        }

    @staticmethod
    def _default_contract() -> ContractInfo:
        """PR6a static fallback — TR_KEY_STATIC 등 모듈 상수 사용.

        PR6c-2b 운영 진입 시점에는 외부에서 select_active_usd_futures_contract
        결과 주입. 본 default는 단위 테스트 / 운영 미연결 시점 호환용.
        """
        return ContractInfo(
            short_code=TR_KEY_STATIC,
            standard_code="KR4A75650007",  # PR6a static (KIS smoke 검증값)
            name="미국달러 F 202605",
            contract_month=CONTRACT_MONTH_STATIC,
            expiry_date=CONTRACT_EXPIRES_ON_STATIC,
        )

    @property
    def status(self) -> str:
        return self._status

    def add_tick_handler(self, handler: TickHandler) -> None:
        """PR6b에서 Redis/Alert/DB writer 등록 진입점."""
        self._tick_handlers.append(handler)

    async def start(self) -> None:
        """메인 루프 — active session 동안 WebSocket 유지, 변경 시 재연결.

        세션 None (휴장/break) 동안엔 30초마다 재체크.
        """
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            session = get_active_session(now)
            if session is None:
                logger.debug("[kis_ws] no active session, sleep 30s")
                self._set_status("normal")  # 휴장은 stale 아님
                self._active_session = None  # PR6d-2a — 휴장 metric 반영
                await asyncio.sleep(30)
                continue
            await self._run_session(session)
        # PR6d-2a — stop 시 active_session 정리
        self._active_session = None

    async def stop(self) -> None:
        self._stop.set()

    async def _run_session(self, session: str) -> None:
        """단일 active session 동안 WebSocket 연결 유지 + reconnect."""
        attempt = 0
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            current_session = get_active_session(now)
            if current_session != session:
                logger.info("[kis_ws] session changed %s → %s, exit loop", session, current_session)
                return

            try:
                approval_key = await self._approval.get_approval_key()
                self._set_status("reconnecting" if attempt > 0 else "normal")
                await self._connect_and_listen(session, approval_key)
                attempt = 0  # 정상 종료 (세션 변경 등) 시 reset
            except Exception as e:
                attempt += 1
                self._reconnect_attempt_count += 1  # PR6d-2a — reconnect baseline
                self._set_status("reconnecting")
                backoff = self._compute_backoff(attempt)
                logger.warning(
                    "[kis_ws] connection error (attempt %d): %s: %s — backoff %ds",
                    attempt, type(e).__name__, e, backoff,
                )
                # stale 임계 도달 시 status 갱신 (handler에서 사용 가능)
                if self._last_tick_at and (time.time() - self._last_tick_at > STALE_AFTER_SEC):
                    self._set_status("stale")
                await asyncio.sleep(backoff)

    @staticmethod
    def _compute_backoff(attempt: int) -> int:
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    def _set_status(self, new_status: str) -> None:
        if self._status != new_status:
            logger.info("[kis_ws] status %s → %s", self._status, new_status)
            self._status = new_status
            # PR6d-2a — status transition counter (flap / false stale 감지용)
            if new_status in self._status_transition_count:
                self._status_transition_count[new_status] += 1

    async def _connect_and_listen(self, session: str, approval_key: str) -> None:
        """WebSocket 연결 + subscribe + recv loop. 예외 시 caller가 reconnect."""
        async with websockets.connect(
            KIS_WS_URL, ping_interval=None, open_timeout=10
        ) as ws:
            logger.info("[kis_ws] connected, session=%s", session)
            tr_key = self._contract.short_code  # PR6c-2a: 동적 contract
            # PR6d-2a — lifecycle metric (connected_at + active_session)
            self._connected_at = time.time()
            self._active_session = session
            for tr_id in SESSION_TR_MAP[session]:
                await ws.send(self._sub_message(approval_key, tr_id, tr_key))
                logger.info("[kis_ws] subscribed tr_id=%s key=%s", tr_id, tr_key)

            # PR6e — 새 WebSocket 세션 관찰 시작점. 이전 세션 마지막 tick의
            # _last_tick_at carry-over로 인한 즉시 stale 전이 차단 (60초 grace).
            # 60초 안에 첫 tick 안 들어오면 line 384-389 체크가 stale 정상 감지.
            self._last_tick_at = time.time()
            self._set_status("normal")

            while not self._stop.is_set():
                # 세션 boundary 도달 체크 (15:45 / 06:00 등)
                now = datetime.now(KST).replace(tzinfo=None)
                if get_active_session(now) != session:
                    logger.info("[kis_ws] session boundary reached, disconnect")
                    return

                # stale 임계 — 마지막 tick 후 60s 초과 시 status 갱신
                if (
                    self._last_tick_at
                    and time.time() - self._last_tick_at > STALE_AFTER_SEC
                ):
                    self._set_status("stale")

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue  # idle, 다시 boundary/stale 체크

                # PINGPONG heartbeat — 응답 안 하면 KIS가 일정 시간 후 disconnect.
                # smoke script와 동일 패턴 (line 203-204).
                if isinstance(raw, str) and "PINGPONG" in raw:
                    try:
                        await ws.pong(raw.encode())
                    except Exception as e:
                        logger.warning("[kis_ws] pong send failed: %s: %s", type(e).__name__, e)
                    continue

                await self._handle_frame(raw, session)

    @staticmethod
    def _sub_message(approval_key: str, tr_id: str, tr_key: str) -> str:
        body = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        return json.dumps(body)

    async def _handle_frame(self, raw: str, session: str) -> None:
        """KIS WebSocket frame 분류 — system message vs tick payload."""
        if not raw:
            return
        if raw.startswith(("0|", "1|")):
            # tick frame: "0|TR_ID|count|data^data^..."
            parts = raw.split("|", 3)
            if len(parts) < 4:
                logger.warning("[kis_ws] malformed tick frame: %r", raw[:120])
                self._malformed_frame_count += 1  # PR6d-2a
                return
            tr_id, _count, data = parts[1], parts[2], parts[3]
            await self._dispatch_tick(tr_id, data, session)
        else:
            # system message (subscribe success/PINGPONG/error 등)
            self._system_frame_count += 1  # PR6d-2a
            try:
                msg = json.loads(raw)
                header = msg.get("header") or {}
                body = msg.get("body") or {}
                logger.info(
                    "[kis_ws] system tr_id=%s rt_cd=%s msg_cd=%s msg1=%s",
                    header.get("tr_id"), body.get("rt_cd"),
                    body.get("msg_cd"), body.get("msg1"),
                )
            except Exception:
                logger.debug("[kis_ws] non-JSON system frame: %r", raw[:200])

    async def _dispatch_tick(self, tr_id: str, data: str, session: str) -> None:
        parser = PARSER_DISPATCH.get(tr_id)
        if parser is None:
            logger.warning("[kis_ws] unknown tr_id: %s", tr_id)
            self._unknown_tr_id_count += 1
            return

        # PR6d-2a — raw frame metric 갱신 (체결/호가 분리, gap bucket).
        # last_tick_at 갱신은 tick 종류 무관 (호가/체결 둘 다 연결 정상 신호).
        now = time.time()
        prev_tick_at = self._last_tick_at
        self._last_tick_at = now
        self._frame_count_total += 1

        # 전체 frame gap 측정
        if prev_tick_at is not None:
            gap_total = now - prev_tick_at
            if gap_total > self._max_frame_gap_sec:
                self._max_frame_gap_sec = gap_total
            self._gap_buckets_total[self._bucket_for(gap_total)] += 1

        # 체결/호가 분리 측정
        is_trade = tr_id in ("H0CFCNT0", "H0MFCNT0")
        is_quote = tr_id in ("H0CFASP0", "H0MFASP0")
        if is_trade:
            self._trade_frame_count += 1
            if self._last_trade_frame_at is not None:
                gap = now - self._last_trade_frame_at
                if gap > self._max_trade_gap_sec:
                    self._max_trade_gap_sec = gap
                self._gap_buckets_trade[self._bucket_for(gap)] += 1
            self._last_trade_frame_at = now
        elif is_quote:
            self._quote_frame_count += 1
            if self._last_quote_frame_at is not None:
                gap = now - self._last_quote_frame_at
                if gap > self._max_quote_gap_sec:
                    self._max_quote_gap_sec = gap
                self._gap_buckets_quote[self._bucket_for(gap)] += 1
            self._last_quote_frame_at = now

        if self._status != "normal":
            self._set_status("normal")

        # PR6a: 체결 tick (H0CFCNT0/H0MFCNT0)만 fanout. 호가 (H0CFASP0/H0MFASP0)는
        # 매도호가1을 대표값으로 잘못 저장할 위험 + 체결보다 19× 빈도라 latest를
        # 매도호가로 덮어쓰기 — 호가 데이터 필요 시 별도 path 도입.
        if is_quote:
            logger.debug("[kis_ws] quote tick ignored (PR6a fanout): tr_id=%s", tr_id)
            return

        parsed = parser(data)
        if parsed is None:
            logger.warning("[kis_ws] parser returned None tr_id=%s", tr_id)
            return

        # PR6c-2a: client에 주입된 contract metadata를 명시 전달.
        # make_normalized_payload default는 PR6a static이지만, 운영 client는
        # 동적 contract 사용 (만기 후 다음 월물 등).
        payload = make_normalized_payload(
            parsed,
            session=session,
            tr_id=tr_id,
            received_at_kst=datetime.now(KST).replace(tzinfo=None),
            contract_code=self._contract.short_code,
            contract_month=self._contract.contract_month,
            expires_on=self._contract.expiry_date,
        )
        await self._fanout(payload)

    async def _fanout(self, payload: Dict[str, Any]) -> None:
        """모든 handler 동시 실행 — 한 handler 실패가 다른 handler 영향 X."""
        if not self._tick_handlers:
            return
        results = await asyncio.gather(
            *[h(payload) for h in self._tick_handlers],
            return_exceptions=True,
        )
        for handler, result in zip(self._tick_handlers, results):
            if isinstance(result, Exception):
                # Codex 권고 — 예외 삼키기 X, logger.warning으로 가시화.
                logger.warning(
                    "[kis_ws] handler %s exception: %s: %s",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(result).__name__, result,
                )


# ---------------------------------------------------------------------------
# KrxDbWriter — DB write debouncer (PR6b-2b)
# ---------------------------------------------------------------------------

class KrxDbWriter:
    """KRX 체결 tick → source_rates DB write (1초 window debounce + insert-if-changed).

    REALTIME_ARCHITECTURE_PLAN.md DB writer 정책 (line 88-93):
    매 tick INSERT X, 1초 window의 last 값만 → source_rates 폭증 방지.
    USDT Phase 1 insert-if-changed 정책과 일관 (가격 변동 없으면 0건 INSERT).

    asyncio loop 차단 방지:
      sync SQLAlchemy 호출은 asyncio.to_thread로 격리.
      DB session은 to_thread 내부의 새 thread에서 get_db_context()로 짧게.

    failure isolation (KRX optional source 원칙):
      DB write 실패는 logger.warning으로 가시화하되 propagate X —
      tick 수신 loop / 다른 handler 영향 0.

    race 방지 (PR6b-2b Codex 보정):
      DB write (to_thread) 진행 중 새 tick 들어오면 _last_tick 갱신.
      그러나 _timer는 아직 done X 라 __call__이 새 timer 안 만듦.
      write 종료 후 finally 블록에서 _last_tick 재확인하고 새 timer
      예약 — 누락 방지.

    PR6b-2b 한정: handler 클래스만 정의. scheduler 등록은 PR6c.
    """

    def __init__(self, *, window_sec: float = 1.0) -> None:
        self._window_sec = window_sec
        self._last_tick: Optional[Dict[str, Any]] = None
        self._timer: Optional[asyncio.Task] = None

    async def __call__(self, payload: Dict[str, Any]) -> None:
        """Tick handler — KisFuturesClient fanout에서 호출.

        매 tick의 last 값만 유지, 1초 window timer 시작/유지.
        """
        self._last_tick = payload
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        """window 만료 후 last tick을 DB에 insert-if-changed.

        DB write (to_thread) 진행 중 새 tick이 들어오면 finally 블록에서
        새 timer 예약 — race 방지 (PR6b-2b Codex 보정).
        """
        await asyncio.sleep(self._window_sec)
        tick = self._last_tick
        self._last_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as e:
            # KRX optional 원칙 — 격리. propagate X.
            logger.warning(
                "[krx_db_writer] DB write failed (격리): %s: %s",
                type(e).__name__, e,
            )
        finally:
            # DB write 진행 중 들어온 tick 처리 — 새 window 예약.
            # finally 블록은 같은 코루틴 frame 안 — 다른 코루틴 race X
            # (await 없는 sync 영역). __call__이 _timer.done() 체크하기
            # 전에 새 task 할당 완료.
            if self._last_tick is not None:
                self._timer = asyncio.create_task(self._flush_after_window())

    @staticmethod
    def _sync_db_write(tick: Dict[str, Any]) -> None:
        """sync DB 호출 — to_thread 내부에서 실행.

        SessionLocal은 thread-local이라 새 thread에서 새 session 열고 닫기.
        crud.insert_source_rate_if_changed는 내부에서 db.commit() 호출.

        PR6e — KIS payload string에 "1457.40007441" 같은 8자리 정밀도가
        포함되어 들어오므로, KRX USD 미국달러선물 tick size(0.1 KRW)로
        정규화. Decimal(str).quantize는 IEEE float 잔차 없이 정확한
        십진수 라운딩 (`Decimal(float)` 패턴은 IEEE 잔차 carry-over 위험).
        """
        # 함수 내부 import — to_thread만 import 비용, 모듈 로드 영향 X.
        from app import crud
        from app.database import get_db_context

        normalized_rate = float(Decimal(tick["price"]).quantize(Decimal("0.1")))

        with get_db_context() as db:
            crud.insert_source_rate_if_changed(
                db=db,
                source=tick["source"],
                asset=tick["asset"],
                rate=normalized_rate,
            )


# ---------------------------------------------------------------------------
# Factory — env 기반 client 생성 (운영 진입 시 PR6c에서 사용)
# ---------------------------------------------------------------------------

def build_kis_futures_client() -> KisFuturesClient:
    """env에서 KIS_APP_KEY/KIS_APP_SECRET 로드 + client 생성.

    PR6a-1에서는 entry point만 — 실제 호출은 PR6c (scheduler 등록 시점).
    """
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError(
            "KIS_APP_KEY / KIS_APP_SECRET 환경변수 필요 — "
            "PR6c에서 운영 .env 통합"
        )
    approval = KisApprovalManager(app_key=app_key, app_secret=app_secret)
    return KisFuturesClient(approval_manager=approval)
