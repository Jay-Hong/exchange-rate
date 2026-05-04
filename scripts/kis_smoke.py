"""KIS Open API smoke test v1 — PR6 사전 검증.

read-only 단발 스크립트. 운영 코드와 분리. 사용자 .env에 KIS_APP_KEY,
KIS_APP_SECRET 추가 후 1회 실행.

v1 단계:
  1. 환경변수 로드 (.env에서 KIS_APP_KEY, KIS_APP_SECRET)
  2. 인증 토큰 로드 또는 발급 (KIS는 접근 토큰 1일 1회 발급 원칙)
  3. REST inquire_price 1회 호출 (KIS 예제 코스피200 선물 코드로
     endpoint + 인증 + tr_id 동작 검증)

USD/KRW 선물 종목코드 식별 + 시장분류 코드 + off-hours 응답 분석은
v1 결과 보고 v2에서 진행 (마스터 다운로드 추가).

각 단계는 try/except로 격리 — 부분 실패해도 다음 단계 계속.
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

KIS_PROD_HOST = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_PATH = Path(".cache/kis_access_token.json")


def step1_load_env():
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    load_dotenv(env_path)
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        print("[step1] FAIL — KIS_APP_KEY 또는 KIS_APP_SECRET 미설정")
        print("        .env에 두 키 추가 후 chmod 600 .env")
        return None
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            print(f"[step1] WARN — .env 권한이 {oct(mode)} 입니다. chmod 600 .env 권장")
    print(f"[step1] OK — APP_KEY 로드됨 (길이 {len(app_key)})")
    print(f"[step1] OK — APP_SECRET 로드됨 (길이 {len(app_secret)})")
    return {"app_key": app_key, "app_secret": app_secret}


def step2_get_token(creds):
    cached = load_cached_token()
    if cached:
        return cached

    url = f"{KIS_PROD_HOST}/oauth2/tokenP"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": creds["app_key"],
        "appsecret": creds["app_secret"],
    }
    print(f"[step2] POST {url}")
    try:
        r = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"[step2] HTTP {r.status_code}")
        data = r.json()
        if "access_token" in data:
            tok = data["access_token"]
            print(f"[step2] OK — access_token 발급됨 (길이 {len(tok)})")
            print(f"[step2] expires_in: {data.get('expires_in')}초")
            print(f"[step2] token_type: {data.get('token_type')}")
            print(f"[step2] access_token_token_expired: {data.get('access_token_token_expired')}")
            save_cached_token(tok, data)
            return tok
        print("[step2] FAIL — 응답에 access_token 없음")
        print(f"[step2] 응답: {json.dumps(data, ensure_ascii=False, indent=2)}")
        return None
    except Exception as e:
        print(f"[step2] FAIL — {type(e).__name__}: {e}")
        return None


def load_cached_token():
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
    except Exception as e:
        print(f"[step2] WARN — 토큰 캐시 읽기 실패: {type(e).__name__}: {e}")
        return None

    expires_at = float(data.get("expires_at_epoch") or 0)
    # Leave five minutes of margin so a smoke run does not race token expiry.
    if expires_at <= time.time() + 300:
        print("[step2] INFO — 토큰 캐시 만료 또는 만료 임박")
        return None

    token = data.get("access_token")
    if not token:
        return None
    print(f"[step2] OK — 캐시된 access_token 사용 (만료: {data.get('expires_at_text')})")
    return token


def save_cached_token(token, response_data):
    expires_in = int(response_data.get("expires_in") or 86400)
    expires_at_epoch = time.time() + expires_in
    cache = {
        "access_token": token,
        "expires_at_epoch": expires_at_epoch,
        "expires_at_text": response_data.get("access_token_token_expired"),
        "created_at_epoch": time.time(),
    }
    TOKEN_CACHE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    TOKEN_CACHE_PATH.chmod(0o600)
    print(f"[step2] OK — 토큰 캐시 저장: {TOKEN_CACHE_PATH} (chmod 600)")


def step3_inquire_price_endpoint_check(creds, token):
    """알려진 코스피200 선물 코드로 endpoint 동작 검증.

    USD/KRW 선물 종목코드 식별은 v2에서. 여기서는 endpoint + 인증 +
    tr_id 동작 + 계좌 요구 여부 + off-hours 응답 형태만 1차 확인.
    """
    url = f"{KIS_PROD_HOST}/uapi/domestic-futureoption/v1/quotations/inquire-price"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": creds["app_key"],
        "appsecret": creds["app_secret"],
        "tr_id": "FHMIF10000000",
    }
    # KIS 예제 inquire_price.py 기본값 (코스피200 지수선물 근월)
    params = {
        "FID_COND_MRKT_DIV_CODE": "F",
        "FID_INPUT_ISCD": "101W09",
    }
    print(f"[step3] GET {url}")
    print(f"[step3] params: {params}")
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"[step3] HTTP {r.status_code}")
        data = r.json()
        rt_cd = data.get("rt_cd")
        msg_cd = data.get("msg_cd")
        msg1 = data.get("msg1")
        print(f"[step3] rt_cd={rt_cd}, msg_cd={msg_cd}, msg1={msg1}")
        if rt_cd == "0":
            outputs = [
                (name, data.get(name))
                for name in ("output", "output1", "output2", "output3")
                if isinstance(data.get(name), dict)
            ]
            if not outputs:
                print("[step3] OK — output dict가 없음. 전체 top-level keys:")
                print(f"[step3]   {sorted(data.keys())}")
                return
            for name, output in outputs:
                print(f"[step3] OK — {name} 필드 수: {len(output)}")
                print(f"[step3] {name} keys (first 20): {sorted(output.keys())[:20]}")
                for k in (
                    "stck_prpr",
                    "futs_prpr",
                    "prpr",
                    "bstp_kor_isnm",
                    "hts_kor_isnm",
                    "futs_shrn_iscd",
                    "futs_prdy_vrss",
                ):
                    if k in output:
                        print(f"[step3]   {name}.{k}: {output[k]}")
        else:
            print("[step3] 비정상 응답 — 디버깅 필요")
            print(f"[step3] 전체 응답: {json.dumps(data, ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"[step3] FAIL — {type(e).__name__}: {e}")


def main():
    print("=" * 70)
    print("KIS Open API smoke test v1 — 토큰 발급 + endpoint 동작 검증")
    print("=" * 70)

    print("\n--- Step 1: 환경변수 로드 ---")
    creds = step1_load_env()
    if not creds:
        sys.exit(1)

    print("\n--- Step 2: 인증 토큰 발급 ---")
    token = step2_get_token(creds)
    if not token:
        print("\n[main] step2 실패 — 키 유효성 또는 네트워크 확인")
        sys.exit(1)

    print("\n--- Step 3: REST inquire_price endpoint 검증 ---")
    step3_inquire_price_endpoint_check(creds, token)

    print("\n" + "=" * 70)
    print("v1 smoke 완료. 결과 분석 가이드:")
    print("- step2 OK → 키 유효 + 토큰 발급 정상 (24h 만료)")
    print("- step3 rt_cd=0 → endpoint + 인증 정상, off-hours 응답 형태 확인")
    print("- step3 rt_cd≠0 → msg1으로 권한/계좌/HTS_ID 요구 여부 1차 진단")
    print("- 다음 (v2): 종목 마스터 다운로드 → USD/KRW 선물 식별 → 실호출")
    print("=" * 70)


if __name__ == "__main__":
    main()
