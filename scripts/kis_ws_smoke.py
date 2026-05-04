"""KIS WebSocket smoke test — KRX USD futures (주간/야간 자동 감지).

Read-only one-off script for PR6 research. It verifies:
  1. WebSocket approval key can be loaded or issued.
  2. KIS WebSocket can connect.
  3. Active KRX session 기준 TR 자동 선택 + 구독 success/tick 수신.

Session 자동 감지 (app.sources.kis_futures.get_active_session):
  CF (주간 정규세션 08:30-15:45) → H0CFCNT0/H0CFASP0
  CM (야간세션 17:50-06:00, 시작일 기준) → H0MFCNT0/H0MFASP0
  None (휴장) → 구독 안 함, "market closed" 출력 후 종료

PR6 운영 코드와 같은 decision logic 사용 — 잘못된 TR 구독으로 인한
"subscribe success but no tick" 혼동 방지.

The market may be closed, so receiving live ticks is not required for success.
Secrets and approval keys are never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import websockets
from dotenv import load_dotenv

# scripts/ 에서 app/ 패키지 import 가능하게 project root 추가
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.sources.kis_futures import get_active_session  # noqa: E402

KIS_PROD_HOST = "https://openapi.koreainvestment.com:9443"
KIS_WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"
APPROVAL_CACHE_PATH = Path(".cache/kis_ws_approval.json")
TR_KEY = "A75605"  # 미국달러 F 202605, fo_com_code.mst

# Session 자동 감지로 선택할 TR 매핑.
# 주간 H0CFxxxx (commodity futures), 야간 H0MFxxxx (krx night futures).
SESSION_TR_MAP = {
    "CF": [
        ("H0CFCNT0", "commodity futures conclusion"),
        ("H0CFASP0", "commodity futures quote"),
    ],
    "CM": [
        ("H0MFCNT0", "krx night futures conclusion"),
        ("H0MFASP0", "krx night futures quote"),
    ],
}


def load_env() -> dict[str, str]:
    env_path = Path(".env")
    load_dotenv(env_path)
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET missing in .env")
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            print(f"[env] WARN — .env permission is {oct(mode)}; chmod 600 .env recommended")
    print(f"[env] OK — key/secret loaded (lengths: {len(app_key)}, {len(app_secret)})")
    return {"app_key": app_key, "app_secret": app_secret}


def load_cached_approval() -> str | None:
    if not APPROVAL_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(APPROVAL_CACHE_PATH.read_text())
    except Exception as e:
        print(f"[approval] WARN — cache read failed: {type(e).__name__}: {e}")
        return None
    expires_at = float(data.get("expires_at_epoch") or 0)
    if expires_at <= time.time() + 300:
        print("[approval] INFO — cache expired or near expiry")
        return None
    approval_key = data.get("approval_key")
    if not approval_key:
        return None
    print(f"[approval] OK — cached approval key used (expires_at: {data.get('expires_at_text')})")
    return approval_key


def save_cached_approval(approval_key: str) -> None:
    # KIS sample treats websocket auth as 24h. Keep the same conservative TTL.
    expires_at_epoch = time.time() + 86400
    cache = {
        "approval_key": approval_key,
        "created_at_epoch": time.time(),
        "expires_at_epoch": expires_at_epoch,
        "expires_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at_epoch)),
    }
    APPROVAL_CACHE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    APPROVAL_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    APPROVAL_CACHE_PATH.chmod(0o600)
    print(f"[approval] OK — approval key cached: {APPROVAL_CACHE_PATH} (chmod 600)")


def get_approval_key(creds: dict[str, str]) -> str:
    cached = load_cached_approval()
    if cached:
        return cached

    url = f"{KIS_PROD_HOST}/oauth2/Approval"
    payload = {
        "grant_type": "client_credentials",
        "appkey": creds["app_key"],
        "secretkey": creds["app_secret"],
    }
    print(f"[approval] POST {url}")
    resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=10)
    print(f"[approval] HTTP {resp.status_code}")
    data = resp.json()
    approval_key = data.get("approval_key")
    if not approval_key:
        raise RuntimeError(f"approval_key missing: {json.dumps(data, ensure_ascii=False)}")
    print(f"[approval] OK — approval key issued (length {len(approval_key)})")
    save_cached_approval(approval_key)
    return approval_key


def sub_message(approval_key: str, tr_id: str, tr_key: str) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key,
                }
            },
        },
        ensure_ascii=False,
    )


def summarize_raw(raw: str) -> str:
    if raw.startswith(("0|", "1|")):
        parts = raw.split("|", 3)
        if len(parts) >= 4:
            return f"tick tr_id={parts[1]} count={parts[2]} data_prefix={parts[3][:120]}"
        return f"tick raw_prefix={raw[:160]}"
    try:
        data: dict[str, Any] = json.loads(raw)
    except Exception:
        return f"raw_prefix={raw[:200]}"
    header = data.get("header") or {}
    body = data.get("body") or {}
    return (
        f"system tr_id={header.get('tr_id')} tr_key={header.get('tr_key')} "
        f"rt_cd={body.get('rt_cd')} msg_cd={body.get('msg_cd')} msg1={body.get('msg1')}"
    )


async def run_ws(approval_key: str) -> None:
    # Session 자동 감지 — PR6 운영 코드와 동일 decision logic.
    # KIS 시간은 KST. 로컬/EC2/Docker timezone 의존성 제거 위해 명시.
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    session = get_active_session(now)
    print(f"[ws] active session: {session} (KST {now.strftime('%Y-%m-%d %H:%M:%S')})")
    if session is None:
        print("[ws] market closed — 구독 생략, 종료")
        return
    subscriptions = [(tr_id, TR_KEY, label) for tr_id, label in SESSION_TR_MAP[session]]

    print(f"[ws] connect {KIS_WS_URL}")
    async with websockets.connect(KIS_WS_URL, ping_interval=None, open_timeout=10) as ws:
        print("[ws] OK — connected")
        for tr_id, tr_key, label in subscriptions:
            print(f"[ws] subscribe {label}: tr_id={tr_id}, tr_key={tr_key}")
            await ws.send(sub_message(approval_key, tr_id, tr_key))

        deadline = time.time() + 15
        received = 0
        while time.time() < deadline:
            timeout = max(0.1, deadline - time.time())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            received += 1
            summary = summarize_raw(raw)
            print(f"[ws] recv#{received}: {summary}")
            if "PINGPONG" in raw:
                await ws.pong(raw.encode())
        print(f"[ws] done — received {received} messages in 15s")


def main() -> None:
    print("=" * 70)
    print("KIS WebSocket smoke — USD/KRW futures candidate")
    print("=" * 70)
    creds = load_env()
    approval_key = get_approval_key(creds)
    asyncio.run(run_ws(approval_key))


if __name__ == "__main__":
    main()
