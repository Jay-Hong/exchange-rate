"""KIS REST inquire-price observer — 만기일 응답 변화 시점 관찰 (#6).

목적 (KRX_CANARY.md §6 보강):
    5/18 11:30 전후 A75605 (옛 만기) REST inquire-price 응답이 어떻게 바뀌는지
    관찰. 2026-05-07 사전 검증에서 rt_cd=0이어도 futs_prpr 부재 + bstp_* (지수형)
    keys 응답 패턴을 봤음. 만기 정확한 시점 vs master 제거 시점을 분리 측정.

격리 원칙:
    - 운영 helper (fetch_kis_futures_quote / KisAccessTokenManager) import 금지
      이유: 운영 helper는 active session 판정 / cache 발급을 섞어서 raw 변화
      관찰에 부적합. observer는 직접 REST endpoint 호출하고 raw JSON 기록.
    - access_token cache READ-ONLY 우선 (P1 default)
    - --allow-issue-token 시 P2 — observer 자체 발급 + 분리 cache
      (`/tmp/krx_expiry_obs/.observer_token.json`)로 운영 cache 미오염
    - DB/Redis/alert/scheduler 모두 미터치
    - JSONL append만 `/tmp/krx_expiry_obs/rest.jsonl`

KIS endpoint (운영 코드 인용):
    - URL: https://openapi.koreainvestment.com:9443/uapi/domestic-futureoption/v1/quotations/inquire-price
    - TR: FHMIF10000000
    - FID_COND_MRKT_DIV_CODE: CF (정규) / CM (야간)
    - FID_INPUT_ISCD: short_code (e.g. A75605)
    - Headers: Bearer access_token + appkey/appsecret + tr_id

사용법 (단발 호출, cron에서 schedule):
    docker compose exec -T fastapi python scripts/observe_kis_rest.py \\
        --short-codes A75605 A75606 --session CF
    docker compose exec -T fastapi python scripts/observe_kis_rest.py \\
        --short-codes A75605 --session CF --allow-issue-token
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Endpoints + TR (운영 코드 인용: app/crawlers/krx_kis.py:77, 382, 387)
KIS_PROD_HOST = "https://openapi.koreainvestment.com:9443"
KIS_REST_QUOTE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-price"
KIS_REST_QUOTE_TR_ID = "FHMIF10000000"
KIS_REST_TOKEN_PATH = "/oauth2/tokenP"  # access_token 발급 (P2 모드)

# Cache paths
ACCESS_TOKEN_CACHE_PATH = Path(".cache/kis_access_token.json")
OBSERVER_TOKEN_CACHE_PATH = Path("/tmp/krx_expiry_obs/.observer_token.json")

OBS_DIR = Path("/tmp/krx_expiry_obs")
OUTPUT_PATH = OBS_DIR / "rest.jsonl"

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("observe_kis_rest")


# ---------------------------------------------------------------------------
# Token read (P1 read-only / P2 1회 발급)
# ---------------------------------------------------------------------------

def read_token_from_cache(path: Path) -> Optional[str]:
    """Read access_token from cache. None if missing/expired/invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    expires_at = data.get("expires_at_epoch")
    if not isinstance(expires_at, (int, float)):
        return None
    if expires_at <= time.time() + 60:
        return None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    return token


def get_p1_token() -> Optional[str]:
    """P1: 운영 access_token cache read-only."""
    return read_token_from_cache(ACCESS_TOKEN_CACHE_PATH)


def get_p2_token(app_key: str, app_secret: str) -> Optional[str]:
    """P2: observer 자체 cache 우선, 없으면 1회 발급. 분리 cache.

    운영 cache는 절대 안 건드림. observer 자체 발급 토큰은
    /tmp/krx_expiry_obs/.observer_token.json에 저장.
    """
    cached = read_token_from_cache(OBSERVER_TOKEN_CACHE_PATH)
    if cached:
        return cached

    # 1회 발급
    url = f"{KIS_PROD_HOST}{KIS_REST_TOKEN_PATH}"
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    try:
        r = requests.post(url, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.exception("[rest_obs] access_token 발급 실패: %s", type(exc).__name__)
        return None
    token = data.get("access_token")
    expires_in = data.get("expires_in", 86400)
    if not token:
        # Codex Low/Security: raw response dump 금지 — metadata만 로그.
        logger.warning(
            "[rest_obs] access_token missing in response: keys=%s rt_cd=%s msg_cd=%s msg1=%s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            data.get("rt_cd") if isinstance(data, dict) else None,
            data.get("msg_cd") if isinstance(data, dict) else None,
            data.get("msg1") if isinstance(data, dict) else None,
        )
        return None
    OBSERVER_TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OBSERVER_TOKEN_CACHE_PATH.write_text(json.dumps({
        "access_token": token,
        "expires_at_epoch": time.time() + int(expires_in) - 300,
        "issued_at_epoch": time.time(),
        "issued_by": "observe_kis_rest.py P2",
    }, ensure_ascii=False))
    try:
        os.chmod(OBSERVER_TOKEN_CACHE_PATH, 0o600)
    except Exception:
        pass
    logger.info("[rest_obs] P2 token 발급 완료 (expires_in=%ds)", expires_in)
    return token


# ---------------------------------------------------------------------------
# inquire-price single call
# ---------------------------------------------------------------------------

def call_inquire_price(
    *,
    short_code: str,
    session: str,
    token: str,
    app_key: str,
    app_secret: str,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Single REST call → raw response dict. 예외도 dict로 정형화 (관찰 가치)."""
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": KIS_REST_QUOTE_TR_ID,
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": session,
        "FID_INPUT_ISCD": short_code,
    }
    url = f"{KIS_PROD_HOST}{KIS_REST_QUOTE_PATH}"
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        # raise_for_status는 안 함 — 4xx/5xx 응답 자체도 관찰 가치
        try:
            body = r.json()
        except json.JSONDecodeError:
            body = {"_non_json_text": r.text[:1000]}
        return {
            "http_status": r.status_code,
            "body": body,
        }
    except requests.RequestException as exc:
        return {
            "http_status": None,
            "request_exception": type(exc).__name__,
            "request_exception_msg": str(exc)[:300],
        }


def evaluate_normalized_possible(body: Dict[str, Any]) -> Dict[str, Any]:
    """Observer 자체 normalized 판정 (운영 helper 미사용).

    fetch_kis_futures_quote 의도와 동일한 조건만 직접 검사:
    - rt_cd == "0"
    - output dict 존재
    - futs_prpr (또는 prpr) 필드 존재 + float 변환 가능
    """
    rt_cd = body.get("rt_cd")
    output = body.get("output") or body.get("output1")
    has_output_dict = isinstance(output, dict)
    futs_prpr = None
    prpr = None
    if has_output_dict:
        futs_prpr = output.get("futs_prpr")
        prpr = output.get("prpr")
    candidate = futs_prpr if futs_prpr is not None else prpr
    can_parse_float = False
    if candidate is not None:
        try:
            float(candidate)
            can_parse_float = True
        except (TypeError, ValueError):
            pass
    return {
        "rt_cd": rt_cd,
        "msg_cd": body.get("msg_cd"),
        "msg1": body.get("msg1"),
        "output_keys": list(output.keys()) if has_output_dict else None,
        "has_output_dict": has_output_dict,
        "futs_prpr_present": futs_prpr is not None,
        "prpr_present": prpr is not None,
        "price_candidate": candidate,
        "can_parse_float": can_parse_float,
        "normalized_possible": (
            rt_cd == "0" and has_output_dict and can_parse_float
        ),
    }


def write_jsonl(record: Dict[str, Any]) -> None:
    with OUTPUT_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="KIS REST inquire-price observer")
    parser.add_argument(
        "--short-codes", nargs="+", required=True,
        help="e.g. A75605 A75606 (multiple)",
    )
    parser.add_argument(
        "--session", choices=["CF", "CM"], default="CF",
        help="market div code. CF=정규세션 / CM=야간세션",
    )
    parser.add_argument(
        "--allow-issue-token", action="store_true",
        help="P2 모드 활성화. 운영 cache 없으면 observer 자체 1회 발급 + 분리 cache.",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    OBS_DIR.mkdir(parents=True, exist_ok=True)

    app_key = os.environ.get("KIS_APP_KEY", "")
    app_secret = os.environ.get("KIS_APP_SECRET", "")
    if not app_key or not app_secret:
        logger.error("[rest_obs] KIS_APP_KEY/KIS_APP_SECRET env 미설정")
        return 2

    # Token acquisition
    token = get_p1_token()
    mode = "P1"
    if not token:
        if args.allow_issue_token:
            token = get_p2_token(app_key, app_secret)
            mode = "P2"
        else:
            logger.warning(
                "[rest_obs] P1 access_token cache 미존재 — P2 (--allow-issue-token) "
                "활성화 시 1회 발급 가능. 현재 GO 없음 — 종료."
            )
            return 3
    if not token:
        logger.error("[rest_obs] token 획득 실패 (P1/P2 모두). 종료.")
        return 4

    logger.info(
        "[rest_obs] mode=%s short_codes=%s session=%s",
        mode, args.short_codes, args.session,
    )

    issued_at = datetime.now(KST).isoformat()
    for short_code in args.short_codes:
        resp = call_inquire_price(
            short_code=short_code, session=args.session,
            token=token, app_key=app_key, app_secret=app_secret,
            timeout=args.timeout,
        )
        body = resp.get("body") or {}
        record: Dict[str, Any] = {
            "timestamp": issued_at,
            "short_code": short_code,
            "session": args.session,
            "token_mode": mode,
            "http_status": resp.get("http_status"),
            "request_exception": resp.get("request_exception"),
            "request_exception_msg": resp.get("request_exception_msg"),
        }
        if isinstance(body, dict):
            record["normalized_check"] = evaluate_normalized_possible(body)
            record["body_keys"] = list(body.keys())
            # raw body 그대로 기록 (관찰의 핵심)
            record["raw_body"] = body
        write_jsonl(record)
        logger.info(
            "[rest_obs] %s session=%s http=%s rt_cd=%s normalized_possible=%s",
            short_code, args.session, record["http_status"],
            (record.get("normalized_check") or {}).get("rt_cd"),
            (record.get("normalized_check") or {}).get("normalized_possible"),
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
