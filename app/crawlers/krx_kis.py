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
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

import requests
import websockets

from app import config
from app.sources.kis_futures import (
    compute_close_boundary_kst,
    compute_close_grace_end_kst,
    get_active_session,
    is_close_snapshot_eligible,
    is_in_close_grace_window,
    is_in_session_end_grace,
    is_in_single_price_window,
    is_krx_business_day,
    parse_h0cfasp0_payload,
    parse_h0cfcnt0_payload,
    parse_h0mfasp0_payload,
    parse_h0mfcnt0_payload,
)
from app.sources.kis_master import ContractInfo, _extract_contract_month

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

# Fix (2026-06-10, silent-session reconnect) — data frame 침묵이 이 임계를 넘으면
# recv loop가 _KrxSilentSessionError를 raise해 기존 reconnect backoff 경로로 탈출.
# STALE_AFTER_SEC(라벨/REST 평가)와 분리된 "액션" 임계. config 가드: > KRX_STALE_SEC.
SILENT_RECONNECT_SEC = config.KRX_SILENT_RECONNECT_SEC


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
        # Stage C guard용 REST 응답 원본 필드 (위 contract_month/expires_on은
        # 요청 contract 기준 — 아래 resp_*는 KIS 응답 원본). active contract
        # gate(월물/만기일 일치 검증) + freshness telemetry(acml_vol)용.
        "resp_hts_kor_isnm": output.get("hts_kor_isnm"),
        "resp_futs_last_tr_date": output.get("futs_last_tr_date"),
        "resp_acml_vol": output.get("acml_vol"),
    }


# ---------------------------------------------------------------------------
# WebSocket client — connect/subscribe + reconnect 상태 머신
# ---------------------------------------------------------------------------

TickHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class _KrxSilentSessionError(Exception):
    """silent WS session (data frame 정지인데 ConnectionClosed 미발생) 감지 시
    reconnect loop로 탈출시키는 내부 신호. _run_session의 `except Exception`
    경로가 잡아 backoff + 재접속 (Gopax `_SilentSessionError` 패턴 이식).

    6/9 사고의 직접 대응: CF 15:04 silent stall — KIS가 data frame을 멈췄지만
    PINGPONG으로 TCP는 유지 → recv timeout은 continue만, stale은 라벨만 →
    예외 없음 → reconnect_attempts=0으로 40분 고착 → 15:45 종가 누락.
    """


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
            # Stage C guard (첫 PR — 판정/telemetry only, DB/Redis/topic 반영 X)
            "rest_guard_pass": 0,
            "rest_guard_rejected_calendar": 0,
            "rest_guard_rejected_contract": 0,
            "rest_guard_rejected_session": 0,
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
                # Stage C guard 판정 (첫 PR: 판정 + telemetry only — DB/Redis/topic 반영 X)
                reject = self._evaluate_rest_guard(result, self._now_kst())
                if reject is None:
                    self.counters["rest_guard_pass"] += 1
                    logger.info(
                        "[kis_ws] REST guard PASS contract=%s price=%s acml_vol=%s last_tr=%s",
                        self._contract.short_code, result.get("price"),
                        result.get("resp_acml_vol"), result.get("resp_futs_last_tr_date"),
                    )
                else:
                    self.counters[f"rest_guard_rejected_{reject}"] += 1
                    logger.info(
                        "[kis_ws] REST guard REJECTED reason=%s contract=%s acml_vol=%s",
                        reject, self._contract.short_code, result.get("resp_acml_vol"),
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

    def _now_kst(self) -> datetime:
        """guard 판정용 KST naive now (테스트 patch seam)."""
        return datetime.now(KST).replace(tzinfo=None)

    def _evaluate_rest_guard(self, result: dict, now: datetime) -> Optional[str]:
        """Stage C guard 판정 (ADR-027 §Stage C guard 설계).

        첫 PR: 판정 + telemetry only — DB/Redis/topic 반영 없음. 결과는 호출자
        (`_invoke_rest_fallback`)가 counter/log로만 기록한다.

        hard reject 3 gate (통과 순서):
          1. calendar — 오늘 KRX 거래일? (`is_krx_business_day` — kr_holidays 기반
             연도 무관 동적 + KRX 연말 폐장. 구 2-date 하드코딩에서 업그레이드 완료).
          2. active contract — 만기 미경과 + REST 응답 월물/만기일이 현재 contract와
             일치 (만기 후 / rollover stale 월물 차단 — 5/18 사고 대응).
          3. session — CF/CM 장중 + 종료 grace 아님.
        freshness(gate 4)는 hard reject 아님 — 호출자가 acml_vol을 telemetry로만 기록.

        Args:
            result: `fetch_kis_futures_quote` 반환 dict (resp_* 응답 필드 포함).
            now: KST naive datetime.

        Returns:
            None — 3 hard gate 통과 (fill 후보, 단 첫 PR은 반영 안 함).
            "calendar" / "contract" / "session" — 해당 gate에서 reject.
        """
        today = now.date()

        # gate 1: calendar (오늘 KRX 거래일?)
        if not is_krx_business_day(today):
            return "calendar"

        # gate 2: active contract (만기 미경과 + 응답 identity 정확 일치, fail-closed)
        #   - 월물: _extract_contract_month로 마지막 token이 정확히 YYYYMM인지 검증.
        #     substring 'in'은 '202605X'(suffix)·'1202605'(prefix) variant를 통과시켜
        #     약함 (Codex finding 1). contract_month를 만든 동일 파서 재사용 = DRY.
        #   - futs_last_tr_date 부재도 reject — 6/7 실측상 항상 존재했으므로 missing은
        #     이상 신호 (default-safe fail-closed, Codex finding 2).
        if self._contract.expiry_date < today:
            return "contract"
        resp_month = _extract_contract_month(result.get("resp_hts_kor_isnm") or "")
        if resp_month != self._contract.contract_month:
            return "contract"
        if result.get("resp_futs_last_tr_date") != self._contract.expiry_date.strftime("%Y%m%d"):
            return "contract"

        # gate 3: session (CF/CM 장중 + 종료 grace 아님)
        #   NOTE(Stage C 실 반영 시): now 기준 session을 재계산만 한다. result["session"]
        #   (fetch 시점 주입)과의 consistency check는 telemetry-only 첫 PR엔 불필요
        #   (반영 없음) — 실 반영 단계에서 추가 (Codex non-blocking memo 2026-06-07).
        session = get_active_session(now, self._contract.expiry_date)
        if session is None:
            return "session"
        if is_in_session_end_grace(
            now,
            session,
            config.KRX_REST_FALLBACK_SESSION_END_GRACE_MIN,
            self._contract.expiry_date,
        ):
            return "session"

        return None  # 3 hard gate 통과 (gate 4 freshness는 telemetry only)

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

        # KRX_CLOSE_SNAPSHOT_PLAN.md (2026-05-15) — session boundary 직후
        # REST close snapshot. access_token_manager 미주입 시 controller None
        # (KrxRestFallbackController와 동일 패턴 — REST 비활성 환경 호환).
        self._close_snapshot_controller: Optional[KrxCloseSnapshotController] = (
            KrxCloseSnapshotController(token_manager=access_token_manager)
            if access_token_manager is not None else None
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

    def _maybe_schedule_close_snapshot(
        self,
        *,
        ended_session: str,
        today_kst: date,
    ) -> None:
        """Session 종료 감지 시점에 close snapshot schedule 요청.

        KRX_CLOSE_SNAPSHOT_PLAN.md (2026-05-15):
            - boundary 시점에 contract / session / boundary_at_kst 캡처
            - controller가 retry sequence + DB/Redis 갱신 책임
            - 휴장일은 is_close_snapshot_eligible(CF: today / CM: today-1)로 skip
            - controller None (access_token_manager 미주입)이면 skip
            - 예외는 격리 — _run_session 흐름에 영향 X
        """
        if self._close_snapshot_controller is None:
            return
        if ended_session not in ("CF", "CM"):
            return
        # 만기일 expiring CF 11:30 종료는 plan 1차 PR scope 제외
        # (KRX_CLOSE_SNAPSHOT_PLAN §4.10). rollover 실패/지연으로 expiring
        # contract가 만기일까지 잔존해도 15:45 boundary로 schedule되지 않도록 가드.
        # CM은 만기일 06:00 시점에 07:00 swap 전이라 expiring 정상 처리 대상 — 건드리지 않음.
        if (
            ended_session == "CF"
            and self._contract.expiry_date == today_kst
        ):
            logger.info(
                "[krx_close_snapshot] skip (만기일 expiring CF — plan scope 제외) "
                "today=%s expiry=%s contract=%s",
                today_kst.isoformat(),
                self._contract.expiry_date.isoformat(),
                self._contract.short_code,
            )
            return
        try:
            if not is_close_snapshot_eligible(ended_session, today_kst):
                logger.info(
                    "[krx_close_snapshot] skip (휴장일) session=%s today=%s",
                    ended_session, today_kst.isoformat(),
                )
                return
            boundary_at_kst = compute_close_boundary_kst(ended_session, today_kst)
            self._close_snapshot_controller.schedule_close_snapshot(
                contract=self._contract,
                session=ended_session,  # type: ignore[arg-type]
                boundary_at_kst=boundary_at_kst,
            )
        except Exception:
            logger.exception(
                "[krx_close_snapshot] schedule 실패 (격리) session=%s",
                ended_session,
            )

    async def _drain_alert_tick_handlers(self, timeout: float = 5.0) -> None:
        """KrxAlertTickHandler instances drain — session boundary + stop() 공통 helper.

        F-1 (2026-05-26) 외부 검토 #1 보강: ``UsdtAlertEvaluator`` 내부
        ``PriceAlertCoalescer``는 매 5초 wall-clock bucket을 *다음 bucket의 tick
        도래* 또는 *evaluator.close()* 호출 시점에만 emit. KRX는 1
        ``KisFuturesClient``가 여러 session (CF↔CM)을 ``_run_session`` 단위로
        전환하는 구조라, USDT 5 source의 "1 source = 1 WS lifecycle 종료 시
        finally drain" 패턴이 자동 적용되지 X. session 갭 (예: CF 15:45 종료 후
        CM 미시작 만기일 시나리오, 일반 CF→CM 2-3시간 갭)이 길어지면 close
        grace crossing이 영영 누락될 위험. session boundary + stop() 양쪽에서
        명시 drain.

        scope: 등록된 ``KrxAlertTickHandler`` instances만 ``close()`` 호출.
        ``KrxDbWriter`` / ``KrxRedisLatestWriter`` 같은 다른 tick handler는
        건드리지 X (각자의 drain timing 정책 존중).

        예외 격리: 한 handler ``close()`` 실패가 다른 handler / session loop /
        shutdown 흐름을 깨뜨리지 못하도록 ``logger.exception`` + propagate X.
        """
        for handler in self._tick_handlers:
            if isinstance(handler, KrxAlertTickHandler):
                try:
                    await handler.close(timeout=timeout)
                except Exception:
                    logger.exception(
                        "[kis_ws] KrxAlertTickHandler drain failed (격리)"
                    )

    async def stop(self) -> None:
        self._stop.set()
        # KRX_FANOUT_REFACTOR_PLAN 5.1.B — controller cleanup 위임 (pending cancel/await/clear).
        await self._fallback_controller.cleanup_tasks()
        # KRX_CLOSE_SNAPSHOT_PLAN.md — close snapshot pending retry drain
        if self._close_snapshot_controller is not None:
            await self._close_snapshot_controller.close()
        # KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 3 — close window writer pending flush drain.
        # KrxCloseWindowWriter는 tick_handlers list에 등록되어 있으므로 직접 조회.
        # 정책: KrxCloseWindowWriter drain은 인라인 유지 (단일 책임, 기존 정책 보존).
        for handler in self._tick_handlers:
            if isinstance(handler, KrxCloseWindowWriter):
                try:
                    await handler.close(timeout=5.0)
                except Exception:
                    logger.exception(
                        "[kis_ws] KrxCloseWindowWriter drain failed (격리)"
                    )
        # F-1 (2026-05-26): KrxAlertTickHandler drain은 helper로 위임 — session
        # boundary와 동일 helper 공유 (coalescer pending flush 보장, 외부 검토 #1).
        await self._drain_alert_tick_handlers(timeout=5.0)

    async def _run_session(self, session: str) -> None:
        """단일 active session 동안 WebSocket 연결 유지 + reconnect.

        KRX_CLOSE_SNAPSHOT_PLAN.md (2026-05-15): session 종료 감지 시점에
        close snapshot controller에 schedule 요청 (boundary 시점에 contract
        캡처 — retry 재resolve 금지). controller가 retry sequence + DB/Redis
        갱신 책임. 예외/실패는 격리 — _run_session 흐름에 영향 X.

        F-1 (2026-05-26) 외부 검토 #1: session boundary 시점에 alert evaluator
        drain 추가. ``UsdtAlertEvaluator.PriceAlertCoalescer``는 마지막 5초
        bucket을 다음 tick 또는 close()까지 보류하므로, KRX session 갭 동안
        close grace crossing 누락 위험. ``_drain_alert_tick_handlers`` helper로
        ``stop()``과 동일 drain 동작 공유.
        """
        attempt = 0
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            # PR6c-2d-1: contract-aware session 판정
            current_session = get_active_session(now, self._contract.expiry_date)
            if current_session != session:
                logger.info("[kis_ws] session changed %s → %s, exit loop", session, current_session)
                # F-1 외부 검토 #1: session boundary 시점에 alert evaluator drain.
                # coalescer 마지막 bucket이 다음 session tick (CM 18:00+ 또는 익일
                # CF 08:00+)까지 보류되면 close grace crossing이 영영 누락될 수 있음.
                # close_snapshot schedule 전에 drain — 순서는 독립이라 의미 영향 X,
                # logger 메시지 직후 명확성 우선.
                await self._drain_alert_tick_handlers(timeout=5.0)
                self._maybe_schedule_close_snapshot(ended_session=session, today_kst=now.date())
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

    def _check_silent_session(
        self, *, now_epoch: float, now_kst: datetime, session: str
    ) -> None:
        """Fix (2026-06-10): silent-session reconnect 판정 — recv loop에서 호출.

        data frame 침묵(_last_tick_at 기준)이 SILENT_RECONNECT_SEC 초과 지속이면
        _KrxSilentSessionError raise → _run_session의 except Exception이 잡아
        backoff + 재접속. PINGPONG/system frame은 _last_tick_at을 갱신하지 않으므로
        (observe_tick은 _dispatch_tick 한정) "TCP 살아있는데 data만 죽은" 세션도 감지.

        close grace window(boundary 직후 60s, KRX_CLOSE_FINALIZER_ENABLED 시) 중에는
        skip — 건강한데 조용한 연결이면 close frame capture 기회 보호, 죽은 연결이면
        어차피 frame 없고 grace 종료 후 boundary exit가 정상 처리. recv loop의
        boundary grace 분기(connect loop 상단)와 동일 조건으로 대칭.
        """
        if not self._last_tick_at:
            return
        silence = now_epoch - self._last_tick_at
        if silence <= SILENT_RECONNECT_SEC:
            return
        if config.KRX_CLOSE_FINALIZER_ENABLED and is_in_close_grace_window(
            now_kst, session
        ):
            return
        raise _KrxSilentSessionError(
            f"data silent {silence:.0f}s > {SILENT_RECONNECT_SEC}s "
            f"(session={session}, last_tick_at={self._epoch_to_kst_iso(self._last_tick_at)})"
        )

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
                    # KRX_CLOSE_SNAPSHOT_PLAN §5.1 Stage 5 (2026-05-17): close grace drain.
                    # boundary 직후 60초 window 안이면 listen 유지 — KrxCloseWindowWriter가
                    # capture할 frame을 cutoff 없이 fanout. get_active_session 의미는 변경 X
                    # (session 종료 감지는 _run_session outer loop에서 정상 발화 + close
                    # snapshot schedule도 그쪽에서). 본 분기는 WS recv loop만 +59초 연장.
                    if (
                        config.KRX_CLOSE_FINALIZER_ENABLED
                        and is_in_close_grace_window(now, session)
                    ):
                        # grace 유지 — recv 계속 진행 (다음 iteration에서 재체크)
                        pass
                    else:
                        logger.info("[kis_ws] session boundary reached, disconnect")
                        return

                # stale 임계 — 마지막 tick 후 60s 초과 시 status 갱신
                if (
                    self._last_tick_at
                    and time.time() - self._last_tick_at > STALE_AFTER_SEC
                ):
                    self._set_status("stale")

                # Fix (2026-06-10, silent-session reconnect) — data 침묵이
                # SILENT_RECONNECT_SEC 초과 지속 시 raise → reconnect 경로 탈출.
                self._check_silent_session(
                    now_epoch=time.time(), now_kst=now, session=session
                )

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

        KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 3 (2026-05-17): close grace window
        (CF 15:45:00~15:45:59 / CM 06:00:00~06:00:59) 진입 시 일반 path skip —
        KrxCloseWindowWriter가 전담 (DB row 1 보장, Plan §5.2 / Finding 1 fix).
        env false 시 skip 분기 미진입 (1차 PR 동작 그대로 — rollback path).
        """
        await asyncio.sleep(self._window_sec)
        tick = self._last_tick
        self._last_tick = None
        if tick is None:
            return

        # §5.2 Stage 3: close grace window 진입 시 일반 path skip.
        # KrxCloseWindowWriter가 close 영역 전담 (window-end 1건 unconditional INSERT).
        if config.KRX_CLOSE_FINALIZER_ENABLED:
            session = tick.get("session", "")
            received_at_iso = tick.get("received_at", "")
            try:
                tick_kst_naive = datetime.fromisoformat(received_at_iso)
                if is_in_close_grace_window(tick_kst_naive, session):
                    # close grace tick은 KrxCloseWindowWriter가 처리 — 일반 path skip
                    # finally 블록에서 다음 tick window 재예약은 그대로 진행
                    if self._last_tick is not None:
                        self._timer = asyncio.create_task(self._flush_after_window())
                    return
            except (ValueError, TypeError):
                # received_at 파싱 실패 시 일반 path 진행 (defensive — 회귀 안전)
                pass

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

        # Phase B.2 PR3 → ADR-038 Decision 2: Redis latest 갱신 시 KRX 독립 topic 발행
        # (구 usdt:krw 재발행 대체 — KRX는 krx:usd-krw-futures 전용).
        if redis_write_success:
            from app import krx_topic_publisher
            try:
                krx_topic_publisher.request_krx_topic_publish(
                    reason="krx_redis_write_success",
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
            # Stage E (KRX_FANOUT_REFACTOR_PLAN §5.2 E): flag true 시 Redis write/
            # trigger는 tick-level handler(KrxRedisLatestWriter.__call__)가 담당.
            # DB writer는 DB history만 담당 — return False로 caller (_flush_after_window)
            # 의 trigger 분기도 자연 차단 (의미적 일관: DB-bound trigger는 ADR-031 1차
            # spec, tick-level trigger는 Stage E 영역).
            if config.KRX_REDIS_TICK_WRITE_ENABLED:
                return False
            # flag=false(Stage E rollback) routine Redis: latest:source:krx는 FX atomic keyspace와
            # disjoint라 mode-independent (구 P1b A2-3 전역 FX write-mode gate 제거 — 전역 FX mode가
            # KRX Redis write를 막아 테더/KRX topic을 freeze시키던 버그 수정). set_latest_krx(598) +
            # tick-level(675)도 mode-independent로 정렬. KRX 자체 cutover track에서 per-source gate 도입.
            # ADR-031: DB insert 성공 시점에 Redis direct write (KrxRedisLatestWriter 위임).
            return KrxRedisLatestWriter.write_after_db_insert(db, source, asset)


class KrxRedisLatestWriter:
    """KRX Redis latest direct write — KrxDbWriter에서 분리된 책임 (PR Next-C).

    ADR-031 spec 유지 — DB insert 성공 후 같은 sync 컨텍스트에서 Redis latest
    갱신. 단 Stage E (KRX_FANOUT_REFACTOR_PLAN §5.2 E) 진입 시 tick-level write
    도 함께 지원 (KRX_REDIS_TICK_WRITE_ENABLED=true 시 `__call__` tick handler 활성).

    behavior-change-0 책임 분리 수행 (KRX_FANOUT_REFACTOR_PLAN 5.1.C):
        - 호출 위치/timing: KrxDbWriter._sync_db_write inserted=True 분기 직후 (DB-bound)
        - latest 재조회 방식: crud.get_latest_source_rate (USDT _mirror_changed_source_to_redis 패턴)
        - Redis helper: latest_rates_cache.set_latest_krx_rate_from_sync_job (KRX 전용, stats 미부착)
        - 예외 격리: writer loop 영향 X

    Stage E 확장 (KRX_FANOUT_REFACTOR_PLAN 5.2.E):
        - `__call__` tick handler — 매 WS tick → tick-level Redis SET (USDT 5b-bis
          schema mirror, 5-field + in-memory state + 5s grain coalescing)
        - close grace window 안 tick은 skip (KrxCloseWindowWriter 단독 처리)
        - KrxLatestWriteOutcome.SET 시점에만 `request_krx_topic_publish` 발사 (ADR-038 독립 topic)
          (trigger 발사 책임 이동 — DB-bound → tick-level)
    """

    async def __call__(self, payload: Dict[str, Any]) -> None:
        """Tick handler — Stage E (KRX_FANOUT_REFACTOR_PLAN §5.2 E).

        매 WS tick → tick-level Redis SET (USDT 5b-bis mirror). flag false 시
        scheduler가 본 handler를 미등록하지만 defensive check 유지.

        Close grace window 안 tick은 skip — KrxCloseWindowWriter가 단독 처리
        (KRX_CLOSE_SNAPSHOT_PLAN §5.7 non-interference 정책).

        KrxLatestWriteOutcome.SET 시점에만 tether topic trigger 발사 (USDT 5d-a
        패턴 mirror — SKIPPED/FAILED는 silent).
        """
        if not config.KRX_REDIS_TICK_WRITE_ENABLED:
            # Defensive — scheduler에서 등록 안 했으나 안전망
            return

        # Close grace window check — KrxDbWriter 패턴 mirror (line 1488-1501)
        if config.KRX_CLOSE_FINALIZER_ENABLED:
            session = payload.get("session", "")
            received_at_iso = payload.get("received_at", "")
            try:
                tick_kst_naive = datetime.fromisoformat(received_at_iso)
                if is_in_close_grace_window(tick_kst_naive, session):
                    # close grace tick은 KrxCloseWindowWriter가 처리 — Stage E skip
                    return
            except (ValueError, TypeError):
                # received_at 파싱 실패 시 defensive — 일반 path 진행 (회귀 안전)
                pass

        # Payload 파싱 — KIS WS frame 정규화 결과
        source = payload.get("source", "krx")
        asset = payload.get("asset", "usd-krw-futures")
        price_str = payload.get("price")
        timestamp = payload.get("received_at", "")
        if not price_str or not timestamp:
            return

        # Timestamp normalize — KRX 운영 payload `received_at`은 KST naive ISO
        # (line ~1398 `datetime.now(KST).replace(tzinfo=None).isoformat()`)인데
        # helper의 `_parse_kst()`는 USDT 5b-bis strict 정책상 naive를 거부.
        # writer 측에서 KST-aware ISO로 변환해 helper 전달. aware payload도
        # 방어적으로 KST로 변환 (forward-compat).
        try:
            dt = datetime.fromisoformat(timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            else:
                dt = dt.astimezone(KST)
            timestamp = dt.isoformat()
        except (ValueError, TypeError):
            return  # defensive — 파싱 실패 시 skip

        try:
            # KrxDbWriter와 같은 정규화 (0.1 KRW tick)
            normalized_rate = float(Decimal(price_str).quantize(Decimal("0.1")))
        except Exception:
            logger.warning(
                "[krx_redis_latest_writer] price 정규화 실패 (격리): %r",
                price_str,
            )
            return

        # to_thread로 sync helper 호출 (event loop non-blocking)
        from app import latest_rates_cache as _latest_rates_cache
        try:
            outcome = await asyncio.to_thread(
                _latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level,
                asset=asset,
                rate=normalized_rate,
                timestamp=timestamp,
            )
        except Exception:
            logger.exception("[krx_redis_latest_writer] tick write 예외 (격리)")
            return

        # SET-only trigger (USDT 5d-a mirror) → ADR-038: KRX 독립 topic 발행
        if outcome is _latest_rates_cache.KrxLatestWriteOutcome.SET:
            from app import krx_topic_publisher
            try:
                krx_topic_publisher.request_krx_topic_publish(
                    reason="krx_redis_write_success",
                )
            except Exception:
                logger.exception(
                    "[krx_redis_latest_writer] tether topic trigger 호출 실패 (격리)"
                )

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
# KrxAlertTickHandler — F-1 (2026-05-26) KRX 가격 알림 adapter
# ---------------------------------------------------------------------------

class KrxAlertTickHandler:
    """KIS 체결 tick → ``AlertObservation`` 변환 + ``KrxAlertEvaluator.schedule``.

    F-1 thin adapter (Codex 정정 반영) — `KrxAlertEvaluator`는 source-neutral
    `UsdtAlertEvaluator` thin subclass라 KRX 전용 로직은 본 handler에 집중.

    책임:
        - KRX payload(``source/asset/price/received_at`` 등) → ``AlertObservation``
          5-field 변환.
        - ``price`` 0.1 KRW tick 정규화 (`KrxDbWriter._sync_db_write` /
          `KrxRedisLatestWriter.__call__` mirror).
        - ``received_at`` (KST naive ISO) → epoch ms 변환 (KST→UTC).
        - 파싱 실패 시 격리 (logger.warning, propagate X) — alert가 KRX
          수집 lifecycle을 깨뜨리지 못하도록.

    정책 anchor (Codex 정정):
        - **No SET-only**: Stage E mirror layer가 SET/SKIPPED 분기를 가지지만
          alert는 그런 게 없음. 매 tick 평가 대상 (coalescer가 5초 wall-clock
          grain으로 동일 source/asset 묶는 건 별 layer 책임).
        - **No close grace skip**: close grace tick(15:45:00~15:45:59 /
          06:00:00~06:00:59)도 알림 평가 — 종가 crossing 보존. mirror layer
          (`KrxRedisLatestWriter.__call__`)는 close grace skip이지만, alert는
          사용자 알림 누락 방지 우선.
        - **Lifecycle drain**: `KisFuturesClient.stop()`이 `close()` 호출 →
          `KrxAlertEvaluator.close()` 위임 → FCM in-flight 보호 (5s timeout).

    Flag default false — `KRX_ALERT_EVALUATOR_ENABLED=false` 시 scheduler가
    handler를 등록하지 않음. defensive guard도 `__call__`에 둠 (env 변경
    timing 안전망).
    """

    def __init__(self, evaluator: Optional["KrxAlertEvaluator"] = None) -> None:
        # 함수 내부 import — 순환 참조 회피. 모듈 import 시점에 alert_evaluator
        # 로드 강제 X (KRX optional source 원칙).
        from app.notifications.alert_evaluator import KrxAlertEvaluator
        self._evaluator: "KrxAlertEvaluator" = (
            evaluator if evaluator is not None else KrxAlertEvaluator()
        )

    @property
    def evaluator(self) -> "KrxAlertEvaluator":
        """테스트/검증용 — 외부에서 evaluator 인스턴스 조회."""
        return self._evaluator

    async def __call__(self, payload: Dict[str, Any]) -> None:
        """Tick handler — KisFuturesClient fanout에서 호출.

        env false 시 scheduler가 본 handler를 등록하지 X (defensive guard 유지).
        Close grace skip 없음 — alert는 모든 tick 평가 (종가 crossing 보존).
        """
        if not config.KRX_ALERT_EVALUATOR_ENABLED:
            return

        source = payload.get("source", "krx")
        asset = payload.get("asset", "usd-krw-futures")
        price_str = payload.get("price")
        received_at_iso = payload.get("received_at", "")
        if not price_str or not received_at_iso:
            return

        # received_at은 KST naive ISO (line ~1398 datetime.now(KST).replace(tzinfo=None)).
        # epoch ms 변환을 위해 KST aware로 보정 후 UTC 기준 epoch ms 산출.
        try:
            dt = datetime.fromisoformat(received_at_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            timestamp_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            # defensive — 파싱 실패 시 skip (logger 부담 회피, KRX optional 원칙)
            return

        # Price 0.1 KRW tick 정규화 (KrxDbWriter / KrxRedisLatestWriter mirror).
        try:
            normalized_rate = float(Decimal(price_str).quantize(Decimal("0.1")))
        except Exception:
            logger.warning(
                "[krx_alert_tick] price 정규화 실패 (격리): %r", price_str,
            )
            return

        # 함수 내부 import — alert_evaluator 모듈 강제 로드 회피 (격리 원칙).
        from app.notifications.alert_evaluator import observation_from_tick
        observation = observation_from_tick(
            {"source": source, "asset": asset, "rate": normalized_rate, "timestamp_ms": timestamp_ms}
        )
        try:
            self._evaluator.schedule(observation)
        except Exception:
            logger.exception(
                "[krx_alert_tick] evaluator.schedule 실패 (격리, WS session 유지)"
            )

    async def close(self, timeout: float = 5.0) -> None:
        """Lifecycle drain — KisFuturesClient.stop()에서 호출.

        `KrxAlertEvaluator.close()` 위임. FCM in-flight 보호.
        """
        try:
            await self._evaluator.close(timeout=timeout)
        except Exception:
            logger.exception("[krx_alert_tick] evaluator.close() 실패 (격리)")


# ---------------------------------------------------------------------------
# KrxCloseFinalizer — KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 3 (2026-05-17)
# ---------------------------------------------------------------------------
# WS-first close finalizer. KrxDbWriter (일반 path)와 직교 책임:
#   - KrxDbWriter: 1초 window debounce + insert-if-changed (일반 시간대)
#                  close grace 진입 시 skip (Option A, Plan §5.2 / F1 fix)
#   - KrxCloseWindowWriter: close grace window 전담 — window-end 1건
#                            unconditional INSERT + Redis SET + flag SET
#                            (DB+Redis 모두 성공 시에만 flag SET, F2 fix)


class KrxCloseFinalizerStats:
    """KRX close finalizer telemetry — Plan §5.5 (5 counters).

    case A/B/C 분포 실측을 위한 운영 metric:
        - case A (우리 cutoff): close_grace_saved > 0 AND close_rest_fallback_used == 0 in day
        - case B (KIS 미송신): close_grace_saved == 0 AND close_rest_fallback_used > 0
        - case C (dedup skip): 2차 PR 정책상 0건 예상

    Process restart 시 in-memory counter 초기화 — 7일 telemetry 측정 동안
    deploy/restart 최소화 권장. Stage 6+ 후속에서 Redis hash 영구 저장 검토.
    """

    def __init__(self) -> None:
        self.close_grace_tick_count: int = 0
        self.close_grace_saved: int = 0
        self.close_rest_fallback_used: int = 0
        self.close_duplicate_count: int = 0
        self.single_price_window_frame_count: int = 0


_krx_close_finalizer_stats = KrxCloseFinalizerStats()


def get_krx_close_finalizer_stats() -> KrxCloseFinalizerStats:
    """KrxCloseFinalizerStats singleton accessor — admin/monitor 조회용."""
    return _krx_close_finalizer_stats


def reset_krx_close_finalizer_stats_for_tests() -> None:
    """Tests 전용 reset — production 경로에서 호출 금지."""
    global _krx_close_finalizer_stats
    _krx_close_finalizer_stats = KrxCloseFinalizerStats()


class KrxCloseWindowWriter:
    """KRX close grace window tick handler — window-end last candidate 1건 unconditional INSERT.

    KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 3 (2026-05-17). KrxDbWriter와 직교 책임:
        - KrxDbWriter: 1초 window debounce + insert-if-changed (일반 path,
                       close grace 시간 skip — Option A / F1 fix)
        - KrxCloseWindowWriter: close grace window 전담 — window-end 1건
                                 unconditional INSERT + Redis SET + flag SET

    Lifecycle (per session):
        1. close grace window 진입 (CF 15:45:00 / CM 06:00:00 첫 frame) →
           `_last_tick` 갱신 + window-end flush task 예약
        2. 추가 frame 들어오면 `_last_tick` 갱신 (count++)
        3. window 종료 시점(15:46:00 / 06:01:00 KST)에 flush task 실행 →
           DB unconditional INSERT + Redis SET → 양쪽 성공 시 flag SET +
           close_grace_saved counter ++ + tether topic trigger
        4. flush 후 `_last_tick` reset

    F2 fix (Finding 2, 2026-05-17 분할 단계 재시작):
        DB INSERT 실패 또는 Redis SET 실패 시 close_captured flag SET 안 함.
        REST fallback (KrxCloseSnapshotController, Stage 4)이 미보정 케이스를
        실행하도록 capacity 유지.

    Timestamp 정책 (Plan §5.2) — `_parse_event_at_kst` helper 참조:
        event_at_kst = KIS payload 체결시각 (market_time HHMMSS, KIS `bsop_hour`) 우선,
                       malformed/empty 시 `received_at` (WS frame receive 시각) fallback.
                       market_time hour/min/sec는 received_at의 date 기준 결합.
        DB.timestamp = event_at_kst.astimezone(UTC).replace(tzinfo=None)
        Redis.timestamp = event_at_kst.isoformat() (KST ISO)

    Single-price window (CF 15:35:00~15:44:59 / CM 05:50:00~05:59:59):
        Close 확정 제외 — `single_price_window_frame_count` counter only.

    실패 격리: DB/Redis write 예외는 logger.warning + propagate X.
        KisFuturesClient fanout return_exceptions=True 추가 안전망.

    Env: `KRX_CLOSE_FINALIZER_ENABLED=true`일 때만 동작 (false 시 `__call__` early return).
    """

    def __init__(self) -> None:
        # session ("CF"/"CM") → 상태. 매일 새 window에서 자연 reset.
        self._last_tick: Dict[str, Dict[str, Any]] = {}
        self._frame_count: Dict[str, int] = {}
        self._flush_task: Dict[str, Optional[asyncio.Task]] = {}

    async def __call__(self, payload: Dict[str, Any]) -> None:
        """Tick handler — KisFuturesClient `_fanout`에서 호출.

        Close grace window 진입 시 last_tick 갱신 + window-end flush task 예약.
        Single-price window 진입 시 telemetry counter만 ++.
        그 외 시간대는 즉시 return (KrxDbWriter가 일반 path로 처리).
        """
        # 함수 내부 import — runtime env 변경 시 즉시 반영 (test patch.object 호환)
        from app import config as _config
        if not _config.KRX_CLOSE_FINALIZER_ENABLED:
            return

        session = payload.get("session", "")
        if session not in ("CF", "CM"):
            return

        received_at_iso = payload.get("received_at", "")
        try:
            received_at_kst_naive = datetime.fromisoformat(received_at_iso)
        except (ValueError, TypeError):
            return  # 잘못된 timestamp — skip

        # Single-price window: telemetry only
        if is_in_single_price_window(received_at_kst_naive, session):
            _krx_close_finalizer_stats.single_price_window_frame_count += 1
            return

        # Close grace window: last candidate 추적
        if not is_in_close_grace_window(received_at_kst_naive, session):
            return

        _krx_close_finalizer_stats.close_grace_tick_count += 1
        self._last_tick[session] = payload
        self._frame_count[session] = self._frame_count.get(session, 0) + 1

        # Flush task 예약 (이미 활성이면 skip — `_last_tick`은 계속 갱신됨)
        existing = self._flush_task.get(session)
        if existing is None or existing.done():
            grace_end = compute_close_grace_end_kst(received_at_kst_naive, session)
            self._flush_task[session] = asyncio.create_task(
                self._flush_at_window_end(session, grace_end)
            )

    async def _flush_at_window_end(
        self, session: str, grace_end_kst: datetime
    ) -> None:
        """Window 종료 시점까지 대기 후 last_tick unconditional INSERT + Redis SET + flag SET."""
        KST = ZoneInfo("Asia/Seoul")
        now_kst = datetime.now(KST)
        sleep_sec = (grace_end_kst - now_kst).total_seconds()
        if sleep_sec > 0:
            await asyncio.sleep(sleep_sec)

        tick = self._last_tick.pop(session, None)
        count = self._frame_count.pop(session, 0)
        if tick is None:
            return

        # 2 frame 이상 → close_duplicate_count ++ (이상 신호 alert)
        if count >= 2:
            _krx_close_finalizer_stats.close_duplicate_count += 1
            logger.warning(
                "[krx_close_window] close grace window 안 %d frames detected "
                "(session=%s) — raw count 누적, last candidate 1건만 저장",
                count, session,
            )

        try:
            await asyncio.to_thread(self._sync_write, tick, session)
        except Exception as e:
            logger.warning(
                "[krx_close_window] flush failed (격리): %s: %s",
                type(e).__name__, e,
            )

    @staticmethod
    def _parse_event_at_kst(tick: Dict[str, Any]) -> datetime:
        """KIS payload market_time(HHMMSS) 우선 + received_at fallback → KST aware datetime.

        Plan §5.2 event_at_kst 정책:
            - market_time(KIS `bsop_hour`) HHMMSS 6자리 형식이면 received_at의 date에
              market_time hour/min/sec 결합 → KST aware datetime
            - market_time empty/malformed → received_at ISO 그대로 (KST naive → aware)
            - received_at parse 실패 → datetime.now(KST) fallback

        Args:
            tick: make_normalized_payload 결과 (received_at ISO, market_time HHMMSS).

        Returns:
            KST aware datetime — DB는 .astimezone(UTC).replace(tzinfo=None), Redis는 .isoformat().
        """
        kst_tz = ZoneInfo("Asia/Seoul")
        received_at_iso = tick.get("received_at", "")
        try:
            received_at_naive = datetime.fromisoformat(received_at_iso)
        except (ValueError, TypeError):
            received_at_naive = datetime.now(kst_tz).replace(tzinfo=None)

        market_time = tick.get("market_time", "")
        if isinstance(market_time, str) and len(market_time) == 6 and market_time.isdigit():
            try:
                hh = int(market_time[0:2])
                mm = int(market_time[2:4])
                ss = int(market_time[4:6])
                if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
                    return datetime(
                        received_at_naive.year,
                        received_at_naive.month,
                        received_at_naive.day,
                        hh, mm, ss, 0,
                        tzinfo=kst_tz,
                    )
            except ValueError:
                pass
        # Fallback: received_at KST naive → aware
        return received_at_naive.replace(tzinfo=kst_tz)

    @staticmethod
    def _sync_write(tick: Dict[str, Any], session: str) -> bool:
        """sync DB INSERT (unconditional) + Redis SET + close_captured flag SET.

        F2 fix (Plan §5.3): flag SET은 DB+Redis 모두 성공 시에만.
        DB 실패 또는 Redis 실패 시 flag SET 안 함 → REST fallback이 보정 가능.

        Returns:
            True: DB INSERT + Redis SET 모두 성공 (close_grace_saved ++ + tether trigger).
            False: 어느 한 쪽 실패 — flag SET 안 함.
        """
        from app import crud
        from app import latest_rates_cache
        from app.database import get_db_context
        from decimal import InvalidOperation as _InvalidOperation

        KST = ZoneInfo("Asia/Seoul")

        try:
            normalized_rate = float(Decimal(tick["price"]).quantize(Decimal("0.1")))
        except (_InvalidOperation, KeyError, ValueError):
            logger.warning(
                "[krx_close_window] price parse failed (격리): %r",
                tick.get("price"),
            )
            return False

        source = tick.get("source", "krx")
        asset = tick.get("asset", "usd-krw-futures")

        # event_at_kst 결정 (Plan §5.2):
        #   - KIS payload 체결시각(market_time, HHMMSS) 우선 사용
        #   - market_time empty/malformed → received_at (WS frame receive 시각) fallback
        # CF close(15:45)와 CM close(06:00) 모두 received_at의 KST date 기준 결합.
        # CM 야간장이 자정 넘는 경우(예: 23:50 frame)와 close grace(06:00) 시간대는
        # 다르므로 본 helper는 close grace 진입 후에만 호출되어 date 일관성 보장.
        event_at_kst = KrxCloseWindowWriter._parse_event_at_kst(tick)

        timestamp_utc_naive = event_at_kst.astimezone(dt_timezone.utc).replace(tzinfo=None)
        timestamp_kst_iso = event_at_kst.isoformat()
        kst_date_iso = event_at_kst.date().isoformat()

        # DB unconditional INSERT — 예외 전파(crud helper) → 여기서 catch
        db_ok = False
        try:
            with get_db_context() as db:
                crud.insert_source_rate_unconditional(
                    db=db,
                    source=source,
                    asset=asset,
                    rate=normalized_rate,
                    timestamp=timestamp_utc_naive,
                )
            db_ok = True
        except Exception as e:
            logger.warning(
                "[krx_close_window] DB INSERT failed (격리, Redis 시도 계속): %s: %s",
                type(e).__name__, e,
            )

        # Redis unconditional SET (best-effort helper, 예외 격리)
        redis_ok = latest_rates_cache.set_latest_krx_rate_from_sync_job(
            asset=asset,
            rate=normalized_rate,
            timestamp=timestamp_kst_iso,
        )

        # F2 fix: DB+Redis 모두 성공 시에만 flag SET + counter ++
        if db_ok and redis_ok:
            latest_rates_cache.set_krx_close_captured_flag(session, kst_date_iso)
            _krx_close_finalizer_stats.close_grace_saved += 1
            logger.info(
                "[krx_close_window] close saved session=%s rate=%.1f ts=%s",
                session, normalized_rate, timestamp_kst_iso,
            )
            # 2026-05-26: structured event persist (best-effort, no-throw)
            try:
                latest_rates_cache.emit_krx_close_event(
                    "ws_close_saved",
                    session=session,
                    date_kst=kst_date_iso,
                    boundary_at_kst=timestamp_kst_iso,
                    rate=normalized_rate,
                )
            except Exception:
                logger.warning(
                    "[krx_close_window] emit_krx_close_event 실패 (격리)",
                    exc_info=True,
                )
            # KRX topic publish — KrxDbWriter pattern과 동일 (Redis write 성공 기반, ADR-038)
            try:
                from app import krx_topic_publisher
                krx_topic_publisher.request_krx_topic_publish(
                    reason="krx_close_finalizer",
                )
            except Exception:
                logger.exception(
                    "[krx_close_window] krx topic publish failed (격리)"
                )
            # KRX daily-append (Unit 4b/4c) — CF 정규장 종가만 source_daily_rates에 append.
            # source_rates+Redis+flag 성공 후 호출. 실패는 전부 격리 (finalizer return True/흐름 영향 0).
            # CM(야간장 06:00)은 daily canonical close 아님 → 미append (그 시점 CF session 미발생).
            # Unit 4c gate: KRX_DAILY_APPEND_ENABLED(default false)일 때만 — 배포 ≠ 동작 변화.
            #   함수-레벨 config 참조 (test patch.object 호환; 운영 env 변경은 프로세스 재시작/재배포 필요).
            # NOTE(2026-06-10, ADR-035 D3): KRX hourly는 cron append
            #   (scripts/hourly_append_krx_source_hourly_rates.py — 월물 제거 + CF/CM 통합)로
            #   재설계되어 in-process hourly hook은 제거됨. daily append만 finalizer tail로 유지.
            if session == "CF":
                from app import config as _config
                from app.source_daily_rates import (
                    KRX_APPEND_HARD,
                    append_krx_cf_daily_row,
                )
                if _config.KRX_DAILY_APPEND_ENABLED:
                    try:
                        contract_code = tick.get("contract_code")
                        with get_db_context() as db_append:
                            daily_action, reason = append_krx_cf_daily_row(
                                db_append,
                                event_at_kst.date(),
                                normalized_rate,
                                contract_code,
                            )
                        log_fn = logger.warning if daily_action == KRX_APPEND_HARD else logger.info
                        log_fn(
                            "[krx_cf_append] %s date=%s contract=%s close=%.1f — %s",
                            daily_action, kst_date_iso, contract_code, normalized_rate, reason,
                        )
                    except Exception:
                        logger.warning(
                            "[krx_cf_append] daily-append 실패 (격리, finalizer 영향 없음)",
                            exc_info=True,
                        )
            return True

        # DB 또는 Redis 실패 — flag 미설정, REST fallback에 보정 capacity 유지
        logger.warning(
            "[krx_close_window] partial failure: db_ok=%s redis_ok=%s session=%s — "
            "flag 미설정, REST fallback이 보정 진행 예정",
            db_ok, redis_ok, session,
        )
        return False

    async def close(self, timeout: float = 5.0) -> None:
        """Shutdown drain — pending flush tasks 대기 후 종료.

        KisFuturesClient `stop` 시 호출. timeout 초과 시 task cancel.
        """
        tasks = [
            t for t in self._flush_task.values()
            if t is not None and not t.done()
        ]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# KrxCloseSnapshotController — CF 15:45 / CM 06:00 single-price close 보정
# ---------------------------------------------------------------------------
# KRX_CLOSE_SNAPSHOT_PLAN.md (2026-05-15 합의):
#   - 단일가 구간 KIS 체결 tick 미발화 + boundary 즉시 disconnect → 종가 누락
#   - REST close snapshot 1~3회 bounded 호출로 보정
#   - Primary correctness target = Redis latest (DB는 best-effort history)
#   - boundary timestamp 명시 생성 (now.replace 금지)
#   - retry 시 contract/session/boundary_at 캡처 (재resolve 금지)
#   - REST authoritative — Redis unconditional overwrite
#   - DB insert-if-changed (가격 다름 시 INSERT, 동일 시 skip)
#   - Sanity check ±2% — abort + next retry
#   - 책임 분리: KrxRestFallbackController(stale-based)와 별개 controller

# CF: 15:46:00 / 15:46:30 / 15:47:00 (3회, 첫 성공 시 skip)
# CM: 06:01:00 / 06:01:30 / 06:02:00
_CLOSE_SNAPSHOT_RETRY_DELAYS_SEC: Dict[str, List[float]] = {
    "CF": [60.0, 90.0, 120.0],  # boundary(15:45:00) + 60s / 90s / 120s
    "CM": [60.0, 90.0, 120.0],  # boundary(06:00:00) + 60s / 90s / 120s
}

# Sanity check threshold (±2%) — 직전 latest 대비 비합리적 차이 abort
_CLOSE_SNAPSHOT_SANITY_PCT: float = 0.02

# REST timeout (close snapshot은 stale fallback보다 길어도 OK — bounded 호출)
_CLOSE_SNAPSHOT_REST_TIMEOUT_SEC: float = 10.0

# Close timeout — shutdown drain
_CLOSE_SNAPSHOT_CLOSE_TIMEOUT_SEC: float = 5.0


class KrxCloseSnapshotController:
    """Close snapshot lifecycle — session boundary → REST 1~3회 → DB/Redis.

    책임:
        - boundary 시점 contract/session/boundary_at_kst 캡처
        - retry task lifecycle (set 관리, shutdown drain)
        - REST 호출 (explicit session, 캡처된 contract 재사용)
        - Sanity check (±2%) + first-success short circuit
        - DB insert-if-changed (boundary UTC naive timestamp)
        - Redis unconditional overwrite (boundary KST ISO timestamp)
        - 실패 격리 (logger.warning, 다음 retry 또는 자연 복구)

    KrxRestFallbackController와 책임 분리:
        - KrxRestFallbackController: stale gating (장중 60s+ silence 보정)
        - KrxCloseSnapshotController: session boundary trigger (세션 종료 확정)
    """

    def __init__(
        self,
        *,
        token_manager: KisAccessTokenManager,
        retry_delays_sec: Optional[Dict[str, List[float]]] = None,
        sanity_pct: float = _CLOSE_SNAPSHOT_SANITY_PCT,
        rest_timeout_sec: float = _CLOSE_SNAPSHOT_REST_TIMEOUT_SEC,
    ) -> None:
        self._token_manager = token_manager
        self._retry_delays_sec = retry_delays_sec or _CLOSE_SNAPSHOT_RETRY_DELAYS_SEC
        self._sanity_pct = sanity_pct
        self._rest_timeout_sec = rest_timeout_sec
        self._tasks: "set[asyncio.Task]" = set()
        # baseline counter (telemetry — Redis는 후속 PR)
        self.counters: Dict[str, int] = {
            "scheduled": 0,
            "attempted": 0,
            "success": 0,
            "sanity_aborted": 0,
            "rest_failed": 0,
            "exhausted": 0,
            # KRX_CLOSE_REST_WRITE_ENABLED=false 분기 — REST 호출/sanity는 진행,
            # DB/Redis write만 차단 후 short-circuit. case A/B/C telemetry 보존.
            "rest_write_blocked": 0,
            # 2026-06-10 #4 gate chain (flag 무관 shadow 평가) — reject 사유별 counter.
            # Stage C `rest_guard_rejected_{reason}` 패턴 mirror.
            "rest_write_gate_rejected_calendar": 0,
            "rest_write_gate_rejected_contract": 0,
            "rest_write_gate_rejected_session_evidence": 0,
        }

    def schedule_close_snapshot(
        self,
        *,
        contract: ContractInfo,
        session: Literal["CF", "CM"],
        boundary_at_kst: datetime,
    ) -> None:
        """boundary 감지 시점에 호출 (KisFuturesClient에서).

        snapshot task 1개 시작. retry 시퀀스는 task 내부에서 진행.
        """
        self.counters["scheduled"] += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "[krx_close_snapshot] no running loop — skip schedule (session=%s)",
                session,
            )
            return
        task = asyncio.create_task(
            self._retry_sequence(
                contract=contract,
                session=session,
                boundary_at_kst=boundary_at_kst,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _retry_sequence(
        self,
        *,
        contract: ContractInfo,
        session: Literal["CF", "CM"],
        boundary_at_kst: datetime,
    ) -> None:
        """boundary + delay 시점에 REST 호출. 첫 성공 시 break.

        Stage 4 (Plan §5.3, 2026-05-17): KRX_CLOSE_FINALIZER_ENABLED=true 시
        정책 전환 — close finalizer 단순화:
          1. 진입부 captured flag GET → WS path가 이미 capture 시 즉시 return
          2. delays = delays[:1] (1회 fallback만)
          3. sleep 후 flag 재확인 → captured during wait 시 return
          4. close_rest_fallback_used += 1 (실제 REST 호출 직전, invariant 보장)
          5. _attempt_once 호출

        env false 시 1차 PR (c0855ff) 동작 그대로 — 3 retry, flag GET 없음
        (legacy rollback path, counter 미증가).

        Invariant (env true): close_rest_fallback_used == _attempt_once.call_count
        """
        from app import latest_rates_cache as _latest_rates_cache

        delays = self._retry_delays_sec.get(session, [])
        kst_date_iso: Optional[str] = None

        # Stage 4: env-gated 정책 전환
        if config.KRX_CLOSE_FINALIZER_ENABLED:
            kst_date_iso = boundary_at_kst.astimezone(KST).date().isoformat()
            # (1) 진입 시 captured flag GET — WS path가 이미 capture 시 skip
            if _latest_rates_cache.get_krx_close_captured_flag(session, kst_date_iso):
                logger.info(
                    "[krx_close_snapshot] WS captured at entry → REST skip "
                    "(session=%s date=%s)", session, kst_date_iso,
                )
                # 2026-05-26: structured event persist (best-effort)
                try:
                    _latest_rates_cache.emit_krx_close_event(
                        "rest_skipped_ws_captured",
                        session=session,
                        date_kst=kst_date_iso,
                        boundary_at_kst=boundary_at_kst.isoformat(),
                        extra={"check": "entry"},
                    )
                except Exception:
                    logger.warning(
                        "[krx_close_snapshot] emit_krx_close_event 실패 (격리)",
                        exc_info=True,
                    )
                return
            # (2) 1회 fallback만
            delays = delays[:1]

        now_kst = datetime.now(KST).replace(tzinfo=None)
        boundary_naive = boundary_at_kst.astimezone(KST).replace(tzinfo=None)
        elapsed = (now_kst - boundary_naive).total_seconds()

        for attempt, target_delay in enumerate(delays, start=1):
            wait_sec = target_delay - elapsed
            if wait_sec > 0:
                try:
                    await asyncio.sleep(wait_sec)
                except asyncio.CancelledError:
                    raise

            # (3) env true: sleep 후 flag 재확인 — captured during wait 시 skip (counter 미증가)
            if config.KRX_CLOSE_FINALIZER_ENABLED and kst_date_iso is not None:
                if _latest_rates_cache.get_krx_close_captured_flag(session, kst_date_iso):
                    logger.info(
                        "[krx_close_snapshot] WS captured during wait → REST skip "
                        "(session=%s attempt=%d)", session, attempt,
                    )
                    # 2026-05-26: structured event persist (best-effort)
                    try:
                        _latest_rates_cache.emit_krx_close_event(
                            "rest_skipped_ws_captured",
                            session=session,
                            date_kst=kst_date_iso,
                            boundary_at_kst=boundary_at_kst.isoformat(),
                            attempt=attempt,
                            extra={"check": "during_wait"},
                        )
                    except Exception:
                        logger.warning(
                            "[krx_close_snapshot] emit_krx_close_event 실패 (격리)",
                            exc_info=True,
                        )
                    return

            self.counters["attempted"] += 1
            # (4) env true: 실제 REST 호출 직전 counter ++ (invariant: counter == call_count)
            if config.KRX_CLOSE_FINALIZER_ENABLED:
                _krx_close_finalizer_stats.close_rest_fallback_used += 1
            # 2026-05-26: structured event persist (best-effort)
            if kst_date_iso is None:
                # env false path도 fallback_attempted emit — date_kst를 인라인 derive
                _kst_date_iso = boundary_at_kst.astimezone(KST).date().isoformat()
            else:
                _kst_date_iso = kst_date_iso
            try:
                _latest_rates_cache.emit_krx_close_event(
                    "rest_fallback_attempted",
                    session=session,
                    date_kst=_kst_date_iso,
                    boundary_at_kst=boundary_at_kst.isoformat(),
                    attempt=attempt,
                )
            except Exception:
                logger.warning(
                    "[krx_close_snapshot] emit_krx_close_event 실패 (격리)",
                    exc_info=True,
                )
            # (5) _attempt_once 호출
            try:
                ok = await self._attempt_once(
                    contract=contract,
                    session=session,
                    boundary_at_kst=boundary_at_kst,
                    attempt=attempt,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[krx_close_snapshot] attempt %d 예외 (격리)", attempt,
                )
                self.counters["rest_failed"] += 1
                ok = False
            if ok:
                self.counters["success"] += 1
                return
            elapsed = (datetime.now(KST).replace(tzinfo=None) - boundary_naive).total_seconds()

        self.counters["exhausted"] += 1
        logger.warning(
            "[krx_close_snapshot] all %d attempts exhausted session=%s contract=%s",
            len(delays), session, contract.short_code,
        )

    def _evaluate_close_write_gates(
        self,
        *,
        session: Literal["CF", "CM"],
        result: Dict[str, Any],
        contract: ContractInfo,
        boundary_at_kst: datetime,
        last: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """2026-06-10 #4 — close REST write gate chain 판정 (pure, DB touch 없음).

        flag(KRX_CLOSE_REST_WRITE_ENABLED)와 무관하게 항상 평가 (shadow) —
        verdict는 호출자(_sync_write)가 counter/event로 기록하고, write 허용은
        flag=true AND verdict None일 때만.

        gate 1 calendar: is_close_snapshot_eligible(session, boundary_date) —
            CF는 boundary 당일, CM은 야간장 시작일(boundary−1)이 KRX 거래일.
            scheduling eligibility와 정합 (2026-06-13: 구 is_krx_business_day(
            boundary_date)는 금요일밤 CM의 토요일 boundary를 매주 휴장 오판 reject).
        gate 2 contract identity: REST 응답 월물/만기 == 캡처 contract + 만기 미경과
            (Stage C guard gate 2 mirror — 5/18 rollover stale 차단, fail-closed).
        gate 3 session evidence: 우리 WS의 마지막 tick(last)이 boundary −
            KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS 이내 — 자기 데이터로 "오늘
            세션이 실제 거래했다" 증명. 캘린더 라이브러리 미등록 휴장(5/25 모드
            본질)까지 차단. last=None / timestamp 파싱 불가도 reject (fail-closed —
            구 sanity-skip 구멍 동시 폐쇄).
        (price sanity ±2%는 기존 _sync_write 분기 유지 — 본 함수 범위 밖.)

        Args:
            result: fetch_kis_futures_quote 반환 dict (resp_* 응답 원본 필드 포함).
            contract: boundary 시점 캡처된 ContractInfo.
            boundary_at_kst: close boundary (KST aware).
            last: crud.get_latest_source_rate 결과 (timestamp는 KST ISO 문자열) 또는 None.

        Returns:
            None — 전 gate 통과 (write 후보).
            "calendar" / "contract" / "session_evidence" — 해당 gate에서 reject.
        """
        boundary_date = boundary_at_kst.astimezone(KST).date()

        # gate 1: calendar — CM은 야간장 시작일(boundary−1) 기준 (is_close_snapshot_eligible
        # 재사용 → scheduling과 정합). CF는 boundary 당일 검사라 동작 불변. 5/25형 휴장
        # 보호는 gate 3(session evidence)가 유지 — 본 gate 완화가 그 백스톱을 약화 X.
        if not is_close_snapshot_eligible(session, boundary_date):
            return "calendar"

        # gate 2: contract identity (Stage C _evaluate_rest_guard gate 2 mirror)
        if contract.expiry_date < boundary_date:
            return "contract"
        resp_month = _extract_contract_month(result.get("resp_hts_kor_isnm") or "")
        if resp_month != contract.contract_month:
            return "contract"
        if result.get("resp_futs_last_tr_date") != contract.expiry_date.strftime("%Y%m%d"):
            return "contract"

        # gate 3: session evidence (last WS tick recency)
        if last is None:
            return "session_evidence"
        try:
            last_ts = datetime.fromisoformat(str(last.get("timestamp")))
        except (TypeError, ValueError):
            return "session_evidence"
        if last_ts.tzinfo is None:
            # to_kst_isoformat은 aware(+09:00)가 정상 — naive면 KST로 간주
            last_ts = last_ts.replace(tzinfo=KST)
        age_sec = (boundary_at_kst - last_ts).total_seconds()
        # 경계: age == 임계는 pass (strict >). 미래 timestamp(late tick)는 음수 age → pass.
        if age_sec > config.KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS * 3600:
            return "session_evidence"

        return None

    async def _attempt_once(
        self,
        *,
        contract: ContractInfo,
        session: Literal["CF", "CM"],
        boundary_at_kst: datetime,
        attempt: int,
    ) -> bool:
        """1회 REST 호출 → sanity → DB/Redis write.

        Returns:
            True: 정상 success (가격 다름 INSERT 또는 동일 skip, 둘 다 Redis 갱신)
            False: REST 실패 / sanity abort / write 예외 → 다음 retry 진행
        """
        from app import crud, latest_rates_cache
        from app.database import get_db_context

        # 2026-05-26: structured event persist용 context (best-effort, no-throw)
        _date_kst = boundary_at_kst.astimezone(KST).date().isoformat()
        _boundary_iso = boundary_at_kst.isoformat()

        def _emit(event_type: str, *, rate: Optional[float] = None,
                  extra: Optional[Dict[str, Any]] = None) -> None:
            try:
                latest_rates_cache.emit_krx_close_event(
                    event_type,
                    session=session,
                    date_kst=_date_kst,
                    boundary_at_kst=_boundary_iso,
                    attempt=attempt,
                    rate=rate,
                    extra=extra,
                )
            except Exception:
                logger.warning(
                    "[krx_close_snapshot] emit_krx_close_event 실패 (격리)",
                    exc_info=True,
                )

        result = await fetch_kis_futures_quote(
            contract=contract,
            token_manager=self._token_manager,
            session=session,
            timeout=self._rest_timeout_sec,
        )
        if result is None:
            self.counters["rest_failed"] += 1
            logger.warning(
                "[krx_close_snapshot] REST returned None attempt=%d session=%s",
                attempt, session,
            )
            _emit("rest_returned_none")
            return False

        price_str = result.get("price")
        if not price_str:
            self.counters["rest_failed"] += 1
            logger.warning("[krx_close_snapshot] REST price 부재 attempt=%d", attempt)
            _emit("rest_price_missing")
            return False

        try:
            rest_rate = float(Decimal(price_str).quantize(Decimal("0.1")))
        except Exception:
            self.counters["rest_failed"] += 1
            logger.warning(
                "[krx_close_snapshot] REST price 정규화 실패 price=%r", price_str,
            )
            _emit("rest_price_parse_failed", extra={"price_raw": str(price_str)[:50]})
            return False

        # Sanity check (±sanity_pct) vs 직전 DB latest
        source = result.get("source", "krx")
        asset = result.get("asset", "usd-krw-futures")

        # 2026-06-10 #4 Finding 2 — "success" INFO를 실제 saved 경로로 한정하기 위한
        # closure flag. gate-rejected/blocked terminal(return True)은 각자 분기에서
        # 이미 로그 — tail의 success 로그 중복 방지 (counter/return 의미는 불변).
        write_saved = False

        def _sync_write() -> bool:
            nonlocal write_saved
            with get_db_context() as db:
                last = crud.get_latest_source_rate(db, source, asset)
                if last is not None:
                    last_rate = float(last["rate"])
                    if last_rate > 0:
                        diff_pct = abs(rest_rate - last_rate) / last_rate
                        if diff_pct > self._sanity_pct:
                            logger.warning(
                                "[krx_close_snapshot] sanity abort: rest=%.1f last=%.1f "
                                "diff=%.2f%% (>±%.0f%%)",
                                rest_rate, last_rate, diff_pct * 100,
                                self._sanity_pct * 100,
                            )
                            _emit("rest_sanity_aborted", rate=rest_rate,
                                  extra={"last_rate": last_rate, "diff_pct": round(diff_pct * 100, 2)})
                            return False
                # 2026-06-10 #4 — gate chain shadow 평가: flag와 무관하게 항상 판정.
                # reject는 deterministic(calendar/contract/recency — 수초 간 retry로
                # 안 바뀜)이라 True short-circuit (blocked와 동일 의미).
                gate_verdict = self._evaluate_close_write_gates(
                    session=session,
                    result=result, contract=contract,
                    boundary_at_kst=boundary_at_kst, last=last,
                )
                if gate_verdict is not None:
                    self.counters[f"rest_write_gate_rejected_{gate_verdict}"] += 1
                    logger.warning(
                        "[krx_close_snapshot] REST write gate REJECTED reason=%s "
                        "session=%s rate=%.1f boundary=%s (flag=%s)",
                        gate_verdict, session, rest_rate,
                        boundary_at_kst.isoformat(),
                        config.KRX_CLOSE_REST_WRITE_ENABLED,
                    )
                    _emit("rest_write_gate_rejected", rate=rest_rate,
                          extra={"reason": gate_verdict})
                    return True  # short-circuit retry loop
                # KRX_CLOSE_REST_WRITE_ENABLED=false: write 차단 (2026-05-25 사고
                # 정책의 kill-switch 잔존). 2026-06-10부터 gate 통과 verdict가
                # extra로 남음 (shadow 증거 — WS-miss 날에만 쌓이는 게 정상).
                if not config.KRX_CLOSE_REST_WRITE_ENABLED:
                    self.counters["rest_write_blocked"] += 1
                    logger.info(
                        "[krx_close_snapshot] REST write blocked by flag "
                        "(KRX_CLOSE_REST_WRITE_ENABLED=false, gates=passed) "
                        "session=%s rate=%.1f boundary=%s",
                        session, rest_rate, boundary_at_kst.isoformat(),
                    )
                    _emit("rest_write_blocked", rate=rest_rate,
                          extra={"gates": "passed"})
                    return True  # short-circuit retry loop
                # boundary timestamp — DB UTC naive / Redis KST ISO
                boundary_utc_naive = boundary_at_kst.astimezone(
                    dt_timezone.utc
                ).replace(tzinfo=None)
                # DB insert-if-changed (가격 동일 시 skip)
                crud.insert_source_rate_if_changed(
                    db=db, source=source, asset=asset,
                    rate=rest_rate,
                    timestamp=boundary_utc_naive,
                )
                # Redis unconditional overwrite (REST authoritative).
                # bool return propagate — 본 PR primary correctness target이
                # Redis latest이므로 SET 실패 시 attempt False → 다음 retry로 보정.
                redis_ok = latest_rates_cache.set_latest_krx_rate_from_sync_job(
                    asset=asset,
                    rate=rest_rate,
                    timestamp=boundary_at_kst.isoformat(),
                )
                if bool(redis_ok):
                    write_saved = True
                    _emit("rest_write_saved", rate=rest_rate)
                else:
                    # 정상 path return False (예외 X) — terminal event 보존
                    _emit("rest_write_failed", rate=rest_rate,
                          extra={"reason": "redis_set_failed"})
                    return False

            # 2026-06-10 #4 (a안) — REST-origin daily append tail (WS 경로
            # KrxCloseWindowWriter._sync_write tail mirror). gate 통과 + write 성공
            # 후에만. CF 한정 (CM 06:00은 daily canonical close 아님). 독립 db
            # context — append rollback이 close write에 영향 없도록 격리.
            # captured flag는 SET 안 함 (flag = "WS captured" 의미 보존 — 소비자는
            # _retry_sequence entry/during-wait GET뿐이고 finalizer 경로는 단일
            # 시도라 기능 효과 0, rest_write_saved 이벤트로 가시성 충분).
            # hourly chain 없음 — KRX hourly는 별도 cron append로 재설계됨
            # (scripts/hourly_append_krx_source_hourly_rates.py, ADR-035 D3 2026-06-10).
            if session == "CF" and config.KRX_DAILY_APPEND_ENABLED:
                try:
                    from app.source_daily_rates import (
                        KRX_APPEND_HARD,
                        append_krx_cf_daily_row,
                    )
                    append_date = boundary_at_kst.astimezone(KST).date()
                    with get_db_context() as db_append:
                        daily_action, daily_reason = append_krx_cf_daily_row(
                            db_append, append_date, rest_rate, contract.short_code,
                            metadata_extra={"origin": "rest_close_write"},
                        )
                    log_fn = (
                        logger.warning if daily_action == KRX_APPEND_HARD
                        else logger.info
                    )
                    log_fn(
                        "[krx_cf_append] %s date=%s contract=%s close=%.1f — %s "
                        "(origin=rest_close_write)",
                        daily_action, append_date.isoformat(),
                        contract.short_code, rest_rate, daily_reason,
                    )
                    _emit("rest_daily_append", rate=rest_rate,
                          extra={"action": daily_action, "reason": daily_reason})
                except Exception:
                    logger.warning(
                        "[krx_cf_append] REST-origin daily-append 실패 "
                        "(격리, close write 영향 없음)",
                        exc_info=True,
                    )
            return True

        try:
            ok = await asyncio.to_thread(_sync_write)
        except Exception:
            logger.exception("[krx_close_snapshot] DB/Redis write 예외 (격리)")
            _emit("rest_write_failed", rate=rest_rate)
            return False

        if ok and write_saved:
            # Finding 2 (2026-06-10 #4): success 로그는 실제 saved 경로만.
            # gate-rejected/blocked terminal(ok=True, saved=False)은 각자 분기에서
            # 이미 REJECTED WARNING / blocked INFO 기록 — "reject 직후 success" 오독 방지.
            logger.info(
                "[krx_close_snapshot] success session=%s contract=%s rate=%.1f "
                "boundary=%s attempt=%d",
                session, contract.short_code, rest_rate,
                boundary_at_kst.isoformat(), attempt,
            )
        elif not ok:
            self.counters["sanity_aborted"] += 1
        return ok

    async def close(
        self,
        timeout: float = _CLOSE_SNAPSHOT_CLOSE_TIMEOUT_SEC,
    ) -> None:
        """shutdown drain — pending retry task 완료까지 대기, timeout 후 cancel."""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[krx_close_snapshot] close timeout (%ds), cancel %d pending tasks",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()


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
