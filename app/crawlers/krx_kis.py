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
  - Redis write (latest:source:krx:usd-krw-futures) — ADR-031에서 도입,
    DB insert 성공 시 호출 (tick-level은 ADR-031의 후속 phase)
  - Alert evaluator hook — 매 tick 평가
  - scheduler 등록 / crawler_config 토글
  - front-month contract resolver — 마스터 파일 (`fo_com_code.mst`) 기반

현재 fanout 흐름 (PR6c 운영 + ADR-031 Redis 통합):
  WebSocket tick → on_tick() → callback fanout
                                  └─ KrxDbWriter (1초 window의 last)
                                      ├─ insert_source_rate_if_changed (DB)
                                      └─ set_latest_krx_rate_from_sync_job (Redis,
                                         ADR-031 — DB insert 성공 시 best-effort)

미래 fanout 목표 (ADR-031 후속 phase):
  WebSocket tick → on_tick() → callback fanout
                                  ├─ RedisLatestWriter (매 tick)
                                  ├─ AlertEvaluator (매 tick 평가)
                                  └─ DbWindowWriter (1초 window의 last)
  — REALTIME_ARCHITECTURE_PLAN "알림 모든 tick, DB window close" 원래 계획.
  KRX/USDT/은행 모두 동일 패턴 적용은 별 phase.
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
    is_in_session_end_grace,
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
        # PR6c-2d-1: contract-aware session 판정 (next month 운영 시 정상 15:45 종료)
        session = get_active_session(datetime.now(KST), contract.expiry_date)
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


class KrxLivenessMonitor:
    """KRX WebSocket frame liveness state — frame counters / age timestamps / gap buckets.

    KRX_FANOUT_REFACTOR_PLAN section 5.1.A 추출 (behavior-change-0).
    KisFuturesClient에서 frame/age/gap ownership을 분리.

    유지 (KisFuturesClient에 그대로):
        - status (normal/reconnecting/stale) + transition counter
        - `_set_status()` — RestFallbackController 결합 (PR-B 영역)
        - `_reconnect_attempt_count`, `_active_session`, lifecycle metadata

    state 분류 (reset 대상 vs lifetime):
        - reset 대상 (active session 시작 / 휴장 진입): last_*_frame_at, gap_buckets, max_gaps
        - lifetime (reset X): frame counters (total/trade/quote/system/malformed/unknown)
    """

    _TRADE_TR_IDS = ("H0CFCNT0", "H0MFCNT0")
    _QUOTE_TR_IDS = ("H0CFASP0", "H0MFASP0")

    def __init__(self) -> None:
        # frame counters (lifetime)
        self.frame_count_total: int = 0
        self.trade_frame_count: int = 0  # H0CFCNT0 / H0MFCNT0
        self.quote_frame_count: int = 0  # H0CFASP0 / H0MFASP0
        self.system_frame_count: int = 0
        self.malformed_frame_count: int = 0
        self.unknown_tr_id_count: int = 0
        # last frame timestamps (active session — reset 대상)
        self.last_tick_at: Optional[float] = None
        self.last_trade_frame_at: Optional[float] = None
        self.last_quote_frame_at: Optional[float] = None
        # gap buckets (active session — reset 대상)
        self.gap_buckets_total: Dict[str, int] = self._init_gap_buckets()
        self.gap_buckets_trade: Dict[str, int] = self._init_gap_buckets()
        self.gap_buckets_quote: Dict[str, int] = self._init_gap_buckets()
        # max gaps (active session — reset 대상)
        self.max_frame_gap_sec: float = 0.0
        self.max_trade_gap_sec: float = 0.0
        self.max_quote_gap_sec: float = 0.0

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

    def reset_active_session(self) -> None:
        """active session 시작 / 휴장 진입 시 active-session gap metric reset.

        2026-05-07 외부 검토(Codex)에서 발견된 metric 오염 fix:
        세션 break (예: CM 06:00 종료 → CF 08:30 진입, 2.5h) 가
        `max_*_gap_sec`에 9000초대 오염값으로 잡힘 → baseline 무의미.

        reset 대상: last_*_frame_at, max_gaps, gap_buckets (active-session 한정)
        유지 대상: frame counters (lifetime)

        ⚠️ 주의: `last_tick_at = None`은 PR6e carry-over fix(60s grace)를 깨뜨림.
        호출자는 reset 직후 반드시 `last_tick_at = time.time()`으로 grace start
        설정 책임. (KisFuturesClient._connect_and_listen이 호출 후 즉시 재설정.)
        """
        self.last_tick_at = None
        self.last_trade_frame_at = None
        self.last_quote_frame_at = None
        self.max_frame_gap_sec = 0.0
        self.max_trade_gap_sec = 0.0
        self.max_quote_gap_sec = 0.0
        self.gap_buckets_total = self._init_gap_buckets()
        self.gap_buckets_trade = self._init_gap_buckets()
        self.gap_buckets_quote = self._init_gap_buckets()

    def observe_tick(self, tr_id: str, now: float) -> None:
        """tick frame 관측 — frame/age/gap state 갱신 (`_dispatch_tick`에서 위임).

        파서 성공 후 호출. unknown tr_id는 별도 (`unknown_tr_id_count += 1` 직접).
        체결/호가 자동 분리 + gap 측정 + max gap 갱신.

        Args:
            tr_id: KIS TR ID (H0CFCNT0/H0CFASP0/H0MFCNT0/H0MFASP0).
            now: 현재 epoch (time.time() 결과).
        """
        prev_tick_at = self.last_tick_at
        self.last_tick_at = now
        self.frame_count_total += 1

        # 전체 frame gap 측정
        if prev_tick_at is not None:
            gap_total = now - prev_tick_at
            if gap_total > self.max_frame_gap_sec:
                self.max_frame_gap_sec = gap_total
            self.gap_buckets_total[self._bucket_for(gap_total)] += 1

        # 체결/호가 분리 측정
        if tr_id in self._TRADE_TR_IDS:
            self.trade_frame_count += 1
            if self.last_trade_frame_at is not None:
                gap = now - self.last_trade_frame_at
                if gap > self.max_trade_gap_sec:
                    self.max_trade_gap_sec = gap
                self.gap_buckets_trade[self._bucket_for(gap)] += 1
            self.last_trade_frame_at = now
        elif tr_id in self._QUOTE_TR_IDS:
            self.quote_frame_count += 1
            if self.last_quote_frame_at is not None:
                gap = now - self.last_quote_frame_at
                if gap > self.max_quote_gap_sec:
                    self.max_quote_gap_sec = gap
                self.gap_buckets_quote[self._bucket_for(gap)] += 1
            self.last_quote_frame_at = now


class KrxRestFallbackController:
    """KRX REST fallback eligibility evaluator + invoke + asyncio task lifecycle.

    KRX_FANOUT_REFACTOR_PLAN section 5.1.B 추출 (behavior-change-0).
    KisFuturesClient에서 fallback counters / evaluation / REST invoke / task
    lifecycle 책임 분리.

    유지 (KisFuturesClient에 그대로):
        - `_set_status()` — normal→stale 전이 시 `controller.evaluate()` 위임
        - `_active_session` 갱신 (lifecycle)
        - summary_log_loop의 60s cycle 호출 패턴

    Getter 패턴 (Codex 핵심 권고):
        - `active_session_getter`: `_invoke_rest_fallback` *실행 시점*에 read
          (현재 동작 — session이 evaluate 시점에 freeze되지 않음)
        - `last_tick_at_getter`: evaluate 시점 frame_age 계산 — getter 통일
    """

    def __init__(
        self,
        *,
        contract: ContractInfo,
        access_token_manager: Optional[KisAccessTokenManager],
        active_session_getter: Callable[[], Optional[str]],
        last_tick_at_getter: Callable[[], Optional[float]],
    ) -> None:
        self._contract = contract
        self._access_token_manager = access_token_manager
        self._get_active_session = active_session_getter
        self._get_last_tick_at = last_tick_at_getter
        # state — KisFuturesClient에서 이전
        self.counters: Dict[str, int] = {
            "evaluated": 0,
            "eligible": 0,
            "suppressed_disabled": 0,
            "suppressed_below_threshold": 0,
            "suppressed_session_end_grace": 0,
            "suppressed_cooldown": 0,
            "rest_success": 0,
            "rest_error": 0,
        }
        self.last_fallback_at: Optional[float] = None  # cooldown 추적
        self.tasks: "set[asyncio.Task]" = set()  # fire-and-forget task lifecycle

    def evaluate(self, now_epoch: float) -> str:
        """KRX_FANOUT_REFACTOR_PLAN 5.1.B — `_evaluate_rest_fallback` 이전.

        호출 시점 / 검사 순서 / return label 모두 동일 (Codex BLOCKING 1/2 fix
        그대로 보존). client._set_status(normal→stale) + summary_log_loop가 호출.

        검사 순서 (Codex BLOCKING 2 fix):
        1. session_end_grace 차단
        2. frame_age < KRX_REST_FALLBACK_STALE_SEC → below_threshold
        3. cooldown 미경과 → cooldown
        4. NOT enabled (env=false) → disabled
        5. 모두 통과 → eligible (env=true 시 task 생성)
        """
        self.counters["evaluated"] += 1

        # 1. session-end grace
        now_kst = datetime.fromtimestamp(now_epoch, tz=KST).replace(tzinfo=None)
        if is_in_session_end_grace(
            now_kst,
            self._get_active_session(),
            config.KRX_REST_FALLBACK_SESSION_END_GRACE_MIN,
            self._contract.expiry_date,
        ):
            self.counters["suppressed_session_end_grace"] += 1
            return "suppressed_session_end_grace"

        # 2. frame_age 임계 (getter로 통일)
        last_tick_at = self._get_last_tick_at()
        frame_age = now_epoch - last_tick_at if last_tick_at else 0.0
        if frame_age < config.KRX_REST_FALLBACK_STALE_SEC:
            self.counters["suppressed_below_threshold"] += 1
            return "suppressed_below_threshold"

        # 3. cooldown
        if self.last_fallback_at and (
            now_epoch - self.last_fallback_at < config.KRX_REST_COOLDOWN_SEC
        ):
            self.counters["suppressed_cooldown"] += 1
            return "suppressed_cooldown"

        # 4. env
        if not config.KRX_REST_FALLBACK_ENABLED:
            self.counters["suppressed_disabled"] += 1
            return "suppressed_disabled"

        # 5. eligible
        self.counters["eligible"] += 1

        # Stage B: env=true일 때만 task 생성. last_fallback_at은 task 생성 직전 갱신
        # (cooldown 호출 시도 시점 기준 — REST hang 시 중복 task 폭주 방지).
        if config.KRX_REST_FALLBACK_ENABLED:
            self.last_fallback_at = now_epoch
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # async loop 외 호출 (테스트 환경 등) — task 생성 skip
                return "eligible"
            task = loop.create_task(self._invoke_rest_fallback())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return "eligible"

    async def _invoke_rest_fallback(self) -> None:
        """Stage B — eligible 시 REST 호출. 결과는 log/counter만.

        ★ session은 invoke 시점에 read (evaluate 시점 freeze X — Codex 핵심 보존).
        Stage C에서 broadcast/DB 반영 (별도 GO + V2 protocol).
        """
        if self._access_token_manager is None:
            self.counters["rest_error"] += 1
            logger.warning(
                "[kis_ws] REST fallback skip — access_token_manager 미주입 (wiring 누락)"
            )
            return
        try:
            result = await fetch_kis_futures_quote(
                contract=self._contract,
                token_manager=self._access_token_manager,
                session=self._get_active_session(),
            )
            if result is not None:
                self.counters["rest_success"] += 1
                logger.info(
                    "[kis_ws] REST fallback success contract=%s price=%s session=%s",
                    self._contract.short_code, result.get("price"), result.get("session"),
                )
            else:
                self.counters["rest_error"] += 1
                logger.warning(
                    "[kis_ws] REST fallback returned None contract=%s",
                    self._contract.short_code,
                )
        except Exception:
            self.counters["rest_error"] += 1
            logger.exception("[kis_ws] REST fallback 호출 실패")

    async def cleanup_tasks(self) -> None:
        """stop() cleanup — KisFuturesClient.stop()에서 위임.

        fire-and-forget task가 shutdown 시 pending warning 또는 hang 방지.
        기존 동작 보존: pending → cancel → await → clear.
        """
        if self.tasks:
            pending = list(self.tasks)
            for t in pending:
                if not t.done():
                    t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            self.tasks.clear()


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
        access_token_manager: Optional[KisAccessTokenManager] = None,
    ) -> None:
        """KIS futures WebSocket client.

        Args:
            approval_manager: approval_key cache/refresh helper (WebSocket).
            contract: 구독 대상 종목 (PR6c-2a). None이면 PR6a static
                (A75605, 2026-05-18 만기) — 단위 테스트 / 운영 미연결용.
                운영 진입 (PR6c-2b)에서는 select_active_usd_futures_contract
                결과 주입.
            access_token_manager: REST API access_token cache/refresh (PR6d-2b Stage B).
                None이면 REST fallback 비활성 (Stage A 호환). 운영 bootstrap에서 주입.
        """
        self._approval = approval_manager
        self._access_token_manager = access_token_manager
        self._contract = contract or self._default_contract()
        self._tick_handlers: List[TickHandler] = []
        self._status: str = "normal"
        self._stop = asyncio.Event()

        # PR6d-2a — raw frame metric state
        # KRX_FANOUT_REFACTOR_PLAN 5.1.A — frame/age/gap state는 KrxLivenessMonitor로 분리.
        # _last_tick_at은 property(backward compat shim, 아래) — 내부 access는 self._liveness.last_tick_at.
        self._liveness = KrxLivenessMonitor()
        now = time.time()
        self._started_at: float = now
        self._connected_at: Optional[float] = None
        self._active_session: Optional[str] = None
        # status transition counters
        self._status_transition_count: Dict[str, int] = {
            "normal": 0, "reconnecting": 0, "stale": 0,
        }
        # reconnect attempt count (누적)
        self._reconnect_attempt_count: int = 0

        # PR6d-2b — REST fallback decision telemetry + invoke + task lifecycle.
        # KRX_FANOUT_REFACTOR_PLAN 5.1.B — KrxRestFallbackController로 분리 (behavior-change-0).
        # getter 패턴 (Codex 핵심): active_session/last_tick_at은 invoke/evaluate 시점에 read.
        self._fallback_controller = KrxRestFallbackController(
            contract=self._contract,
            access_token_manager=access_token_manager,
            active_session_getter=lambda: self._active_session,  # ★ invoke 시점 read
            last_tick_at_getter=lambda: self._last_tick_at,  # property → liveness
        )

    @staticmethod
    def _epoch_to_kst_iso(epoch: Optional[float]) -> Optional[str]:
        """epoch float → KST ISO timestamp (PR6d-2a follow-up fix).

        admin 페이지 / 운영자 가독성 위해 ISO 형식 노출. None은 그대로 유지.
        """
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=KST).isoformat()

    # ── backward compat shims (KRX_FANOUT_REFACTOR_PLAN 5.1.A) ──────────
    # KrxLivenessMonitor 추출 후 외부(테스트 + 내부 미refactor 영역) 호환 유지.
    # 후속 PR (refactor 진척 시) 직접 self._liveness.X 접근으로 정리 가능.

    @property
    def _last_tick_at(self) -> Optional[float]:
        return self._liveness.last_tick_at

    @_last_tick_at.setter
    def _last_tick_at(self, value: Optional[float]) -> None:
        self._liveness.last_tick_at = value

    def _reset_active_session_gap_metrics(self) -> None:
        """backward compat wrapper — self._liveness.reset_active_session() 위임.

        호출자는 reset 직후 `self._last_tick_at = time.time()`으로 PR6e grace
        start 설정 책임 (KrxLivenessMonitor.reset_active_session docstring 참조).
        """
        self._liveness.reset_active_session()

    def get_metrics(self) -> Dict[str, Any]:
        """현재 metric state snapshot (PR6d-2a + follow-up fix, ADR-027).

        admin endpoint / summary log / 테스트에서 호출. read-only.

        timestamp 노출 정책:
        - 모든 lifecycle / last_*_at은 epoch + KST ISO 둘 다 제공
        - epoch는 backward compat (기존 클라이언트), ISO는 admin 페이지 가독성
        - age 필드는 그대로 유지 (호환성)
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
                "started_at_kst": self._epoch_to_kst_iso(self._started_at),
                "connected_at": self._connected_at,
                "connected_at_kst": self._epoch_to_kst_iso(self._connected_at),
                "uptime_sec": int(now - self._started_at),
            },
            "last_frame_at": self._liveness.last_tick_at,
            "last_frame_at_kst": self._epoch_to_kst_iso(self._liveness.last_tick_at),
            "last_frame_age_sec": (
                int(now - self._liveness.last_tick_at) if self._liveness.last_tick_at else None
            ),
            "last_trade_frame_at": self._liveness.last_trade_frame_at,
            "last_trade_frame_at_kst": self._epoch_to_kst_iso(self._liveness.last_trade_frame_at),
            "last_trade_frame_age_sec": (
                int(now - self._liveness.last_trade_frame_at) if self._liveness.last_trade_frame_at else None
            ),
            "last_quote_frame_at": self._liveness.last_quote_frame_at,
            "last_quote_frame_at_kst": self._epoch_to_kst_iso(self._liveness.last_quote_frame_at),
            "last_quote_frame_age_sec": (
                int(now - self._liveness.last_quote_frame_at) if self._liveness.last_quote_frame_at else None
            ),
            "counters": {
                "frame_total": self._liveness.frame_count_total,
                "trade_frame": self._liveness.trade_frame_count,
                "quote_frame": self._liveness.quote_frame_count,
                "system_frame": self._liveness.system_frame_count,
                "malformed_frame": self._liveness.malformed_frame_count,
                "unknown_tr_id": self._liveness.unknown_tr_id_count,
                "reconnect_attempt": self._reconnect_attempt_count,
                "status_transitions": dict(self._status_transition_count),
                # PR6d-2b — REST fallback decision telemetry (Stage A)
                "fallback": dict(self._fallback_controller.counters),
            },
            "fallback_last_at": self._fallback_controller.last_fallback_at,
            "fallback_last_at_kst": self._epoch_to_kst_iso(self._fallback_controller.last_fallback_at),
            "gap_buckets": {
                "total": dict(self._liveness.gap_buckets_total),
                "trade": dict(self._liveness.gap_buckets_trade),
                "quote": dict(self._liveness.gap_buckets_quote),
            },
            "max_gap_sec": {
                "total": self._liveness.max_frame_gap_sec,
                "trade": self._liveness.max_trade_gap_sec,
                "quote": self._liveness.max_quote_gap_sec,
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

        PR6d-2a follow-up: 60초마다 summary log task를 background에서 함께 실행.
        active session 중에만 INFO 1줄 출력 (휴장 중에는 noise 방지로 skip).
        """
        # PR6d-2a follow-up — summary log background task. start() 동일 lifetime.
        summary_task = asyncio.create_task(self._summary_log_loop())
        try:
            while not self._stop.is_set():
                now = datetime.now(KST).replace(tzinfo=None)
                # PR6c-2d-1: contract-aware — next month 운영 시 만기일 정상 15:45 종료
                session = get_active_session(now, self._contract.expiry_date)
                if session is None:
                    logger.debug("[kis_ws] no active session, sleep 30s")
                    self._set_status("normal")  # 휴장은 stale 아님
                    self._active_session = None  # PR6d-2a — 휴장 metric 반영
                    # PR6d-2a follow-up — 휴장 진입 시 active-session gap
                    # metric reset. break 시간이 다음 session에 섞이지 않게 함.
                    self._reset_active_session_gap_metrics()
                    await asyncio.sleep(30)
                    continue
                await self._run_session(session)
            # PR6d-2a — stop 시 active_session 정리
            self._active_session = None
        finally:
            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass

    async def _summary_log_loop(self) -> None:
        """PR6d-2a follow-up — active session 중 60초마다 metric summary INFO 1줄.

        baseline 자동 수집 (Docker logs 기반 24~48h grep). 휴장/break 중에는
        noise 방지로 skip — 운영자가 logs에서 baseline window 분리하기 쉬움.

        Caveat (2026-05-06 외부 검토):
            active session 진입 직후 첫 summary log의 frames_per_min은 직전
            60초 전체 기준이라 active 상태였던 시간만의 rate가 아닐 수 있다.
            예: 휴장 중 50초 + active 10초이면 active만의 rate는 더 높음.
            baseline 분석 시 첫 summary log는 caveat 또는 무시 권장. 24~48h
            누적 데이터로 보면 무시 가능 수준.
        """
        prev_frame_total = self._liveness.frame_count_total
        prev_at = time.time()
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            # active session 중에만 emit
            if self._active_session is None:
                # 휴장 중 — counter baseline reset (다음 active 진입 시 정확한 분당 계산)
                prev_frame_total = self._liveness.frame_count_total
                prev_at = time.time()
                continue
            now = time.time()
            elapsed = max(now - prev_at, 1e-9)
            frames_in_window = self._liveness.frame_count_total - prev_frame_total
            frames_per_min = int(round(frames_in_window * 60 / elapsed))
            prev_frame_total = self._liveness.frame_count_total
            prev_at = now
            m = self.get_metrics()
            # PR6d-2b — REST fallback evaluation (status==stale일 때만, 60s cycle 1회).
            # Codex 권고: counter 부풀림 차단 위해 evaluation 단위는 summary cycle.
            # KRX_FANOUT_REFACTOR_PLAN 5.1.B — wrapper 경유 (behavior-change-0).
            if self._status == "stale":
                self._evaluate_rest_fallback(now)

            # m을 evaluation 후 다시 가져옴 (counter 갱신 반영)
            m = self.get_metrics()
            fb = m["counters"]["fallback"]
            logger.info(
                "[kis_ws] metrics session=%s status=%s frames_per_min=%d "
                "frame_age=%s trade_age=%s quote_age=%s "
                "max_total_gap=%.1f max_trade_gap=%.1f max_quote_gap=%.1f "
                "stale_transitions=%d reconnect_attempts=%d "
                "fb_eval=%d fb_eligible=%d fb_grace=%d fb_below_th=%d "
                "fb_cooldown=%d fb_disabled=%d",
                m["active_session"], m["status"], frames_per_min,
                m["last_frame_age_sec"], m["last_trade_frame_age_sec"], m["last_quote_frame_age_sec"],
                m["max_gap_sec"]["total"], m["max_gap_sec"]["trade"], m["max_gap_sec"]["quote"],
                m["counters"]["status_transitions"]["stale"],
                m["counters"]["reconnect_attempt"],
                fb["evaluated"], fb["eligible"], fb["suppressed_session_end_grace"],
                fb["suppressed_below_threshold"], fb["suppressed_cooldown"], fb["suppressed_disabled"],
            )

    # ── PR-B backward compat shims (KRX_FANOUT_REFACTOR_PLAN 5.1.B) ─────
    # KrxRestFallbackController 추출 후 외부(테스트 46건 + 내부 미refactor 영역) 호환.

    @property
    def _fallback_counters(self) -> Dict[str, int]:
        return self._fallback_controller.counters

    @property
    def _last_fallback_at(self) -> Optional[float]:
        return self._fallback_controller.last_fallback_at

    @_last_fallback_at.setter
    def _last_fallback_at(self, value: Optional[float]) -> None:
        self._fallback_controller.last_fallback_at = value

    @property
    def _fallback_tasks(self) -> "set[asyncio.Task]":
        return self._fallback_controller.tasks

    def _evaluate_rest_fallback(self, now_epoch: float) -> str:
        """backward compat wrapper — `self._fallback_controller.evaluate()` 위임.

        호출 시점 / 검사 순서 / return label 모두 동일 (KrxRestFallbackController
        내부 로직 보존). test_krx_fallback_eligibility.py 46건 호환.
        """
        return self._fallback_controller.evaluate(now_epoch)

    async def _invoke_rest_fallback(self) -> None:
        """backward compat wrapper — controller의 _invoke_rest_fallback 위임."""
        await self._fallback_controller._invoke_rest_fallback()

    async def stop(self) -> None:
        self._stop.set()
        # KRX_FANOUT_REFACTOR_PLAN 5.1.B — controller cleanup 위임 (pending cancel/await/clear).
        await self._fallback_controller.cleanup_tasks()

    async def _run_session(self, session: str) -> None:
        """단일 active session 동안 WebSocket 연결 유지 + reconnect."""
        attempt = 0
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            # PR6c-2d-1: contract-aware session 판정
            current_session = get_active_session(now, self._contract.expiry_date)
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
            prev_status = self._status
            logger.info("[kis_ws] status %s → %s", self._status, new_status)
            self._status = new_status
            # PR6d-2a — status transition counter (flap / false stale 감지용)
            if new_status in self._status_transition_count:
                self._status_transition_count[new_status] += 1
            # PR6d-2b BLOCKING 1 fix (Codex 검토): normal → stale 전이 시점에
            # fallback evaluation 1회. 짧은 stale (6~15s) 누락 차단.
            # summary loop은 60s cycle이라 transition만으로는 부족 (stale 종료 후 평가).
            if prev_status == "normal" and new_status == "stale":
                # KRX_FANOUT_REFACTOR_PLAN 5.1.B — wrapper 경유 (behavior-change-0,
                # 기존 `client._evaluate_rest_fallback` patch.object 테스트 호환).
                self._evaluate_rest_fallback(time.time())

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

            # PR6d-2a follow-up (2026-05-07) — active-session gap metric reset.
            # 세션 break 오염값(예: max_*_gap_sec=9000s)이 다음 active session
            # baseline에 섞이지 않도록 reset. lifetime counters는 유지.
            # ⚠️ 순서 중요: reset → _last_tick_at = time.time() (PR6e grace
            # 보존). reset만 하면 _last_tick_at=None이라 carry-over fix 깨짐.
            self._reset_active_session_gap_metrics()

            # PR6e — 새 WebSocket 세션 관찰 시작점. 이전 세션 마지막 tick의
            # _last_tick_at carry-over로 인한 즉시 stale 전이 차단 (60초 grace).
            # 60초 안에 첫 tick 안 들어오면 line 384-389 체크가 stale 정상 감지.
            self._last_tick_at = time.time()
            self._set_status("normal")

            while not self._stop.is_set():
                # 세션 boundary 도달 체크 (15:45 / 06:00 등). PR6c-2d-1: contract-aware.
                now = datetime.now(KST).replace(tzinfo=None)
                if get_active_session(now, self._contract.expiry_date) != session:
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
                self._liveness.malformed_frame_count += 1  # PR6d-2a
                return
            tr_id, _count, data = parts[1], parts[2], parts[3]
            await self._dispatch_tick(tr_id, data, session)
        else:
            # system message (subscribe success/PINGPONG/error 등)
            self._liveness.system_frame_count += 1  # PR6d-2a
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
            self._liveness.unknown_tr_id_count += 1
            return

        # PR6d-2a — raw frame metric 갱신 (체결/호가 분리, gap bucket).
        # KRX_FANOUT_REFACTOR_PLAN 5.1.A — KrxLivenessMonitor.observe_tick 위임.
        # last_tick_at 갱신은 tick 종류 무관 (호가/체결 둘 다 연결 정상 신호).
        now = time.time()
        self._liveness.observe_tick(tr_id, now)

        if self._status != "normal":
            self._set_status("normal")

        # PR6a: 체결 tick (H0CFCNT0/H0MFCNT0)만 fanout. 호가 (H0CFASP0/H0MFASP0)는
        # 매도호가1을 대표값으로 잘못 저장할 위험 + 체결보다 19× 빈도라 latest를
        # 매도호가로 덮어쓰기 — 호가 데이터 필요 시 별도 path 도입.
        if tr_id in ("H0CFASP0", "H0MFASP0"):
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

        Phase B.2 PR3: `_sync_db_write` 반환 bool 받아 finally **밖**에서
        tether topic trigger 호출 (PR2 lock 밖 패턴 mirror — race-prevention
        timer 재예약 책임과 분리). trigger 예외는 격리 — writer loop 영향 X.
        """
        await asyncio.sleep(self._window_sec)
        tick = self._last_tick
        self._last_tick = None
        if tick is None:
            return
        redis_write_success = False
        try:
            redis_write_success = await asyncio.to_thread(self._sync_db_write, tick)
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

        # Phase B.2 PR3: tether topic trigger 호출 — finally **밖** (책임 분리).
        # Redis latest 실제 갱신 시점에만 발화 (의미적 정확성).
        if redis_write_success:
            from app import tether_topic_trigger
            from app.tether_topic_trigger import (
                TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS,
            )
            try:
                tether_topic_trigger.request_tether_topic_trigger(
                    source=tick.get("source", "krx"),
                    asset=tick["asset"],
                    reason=TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS,
                )
            except Exception:
                logger.exception(
                    "[krx_db_writer] tether topic trigger 호출 실패 (격리, writer loop 유지)"
                )

    @staticmethod
    def _sync_db_write(tick: Dict[str, Any]) -> bool:
        """sync DB 호출 — to_thread 내부에서 실행.

        SessionLocal은 thread-local이라 새 thread에서 새 session 열고 닫기.
        crud.insert_source_rate_if_changed는 내부에서 db.commit() 호출.

        PR6e — KIS payload string에 "1457.40007441" 같은 8자리 정밀도가
        포함되어 들어오므로, KRX USD 미국달러선물 tick size(0.1 KRW)로
        정규화. Decimal(str).quantize는 IEEE float 잔차 없이 정확한
        십진수 라운딩 (`Decimal(float)` 패턴은 IEEE 잔차 carry-over 위험).

        ADR-031: DB insert 성공 시 Redis direct write 호출 (best-effort).
        Redis write-through 책임은 `KrxRedisLatestWriter`로 위임 (PR Next-C,
        KRX_FANOUT_REFACTOR_PLAN 5.1.C — behavior-change-0 책임 분리). 호출
        위치/timing/예외 격리는 ADR-031 그대로 유지.

        Phase B.2 PR3: bool return 추가. inserted=True AND Redis SET 성공일 때만
        True. `_flush_after_window`가 main event loop 안에서 tether topic trigger
        분기에 사용 (의미: Redis latest가 실제로 갱신된 시점만 trigger 발화).
        """
        # 함수 내부 import — to_thread만 import 비용, 모듈 로드 영향 X.
        from app import crud
        from app.database import get_db_context

        normalized_rate = float(Decimal(tick["price"]).quantize(Decimal("0.1")))
        source = tick["source"]
        asset = tick["asset"]

        with get_db_context() as db:
            inserted = crud.insert_source_rate_if_changed(
                db=db,
                source=source,
                asset=asset,
                rate=normalized_rate,
            )
            if not inserted:
                return False
            # ADR-031: DB insert 성공 시점에 Redis direct write (KrxRedisLatestWriter 위임).
            return KrxRedisLatestWriter.write_after_db_insert(db, source, asset)


class KrxRedisLatestWriter:
    """KRX Redis latest direct write — KrxDbWriter에서 분리된 책임 (PR Next-C).

    ADR-031 spec 유지 — DB insert 성공 후 같은 sync 컨텍스트에서 Redis latest
    갱신. tick-level write가 *아님* (Stage C 영역, 별 phase).

    behavior-change-0 책임 분리만 수행 (KRX_FANOUT_REFACTOR_PLAN 5.1.C):
        - 호출 위치/timing: KrxDbWriter._sync_db_write inserted=True 분기 직후
        - latest 재조회 방식: crud.get_latest_source_rate (USDT _mirror_changed_source_to_redis 패턴)
        - Redis helper: latest_rates_cache.set_latest_krx_rate_from_sync_job (KRX 전용, stats 미부착)
        - 예외 격리: writer loop 영향 X
    """

    @staticmethod
    def write_after_db_insert(db: "Session", source: str, asset: str) -> bool:
        """DB insert 성공 직후 호출 — latest 재조회 + Redis SET.

        Args:
            db: SQLAlchemy session (DB insert commit 완료 상태).
            source: 항상 "krx" (의미 명시 목적).
            asset: 예 "usd-krw-futures".

        Returns:
            True: Redis SET 성공.
            False: latest 조회 None / `set_latest_krx_rate_from_sync_job` False / 예외.

        Phase B.2 PR3: bool return 추가 — `_sync_db_write`가 `inserted=True AND
        redis_write_success=True` 합성 분기에 사용. tether topic trigger는 Redis
        latest가 실제로 갱신된 시점에만 발화 (의미적 정확성).
        """
        # 함수 내부 import — to_thread 새 thread 환경에서 호출되므로 import 비용 격리.
        # latest_rates_cache가 crud를 module-level로 import한다는 사실은 영향 X
        # (양방향 import 아님, 단방향).
        from app import crud, latest_rates_cache

        try:
            latest = crud.get_latest_source_rate(db, source, asset)
            if latest is None:
                return False
            return latest_rates_cache.set_latest_krx_rate_from_sync_job(
                asset=asset,
                rate=latest["rate"],
                timestamp=latest["timestamp"],
            )
        except Exception:
            logger.exception(
                "[krx_redis_latest_writer] write 예외 (격리, DB fallback 의존)",
                extra={"source": source, "asset": asset},
            )
            return False


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
