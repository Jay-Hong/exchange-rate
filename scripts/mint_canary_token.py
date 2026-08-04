"""canary 용 Firebase **ID token** 발급기 — 값이 화면·디스크·argv 어디에도 남지 않는다.

⛔ **stdout 에는 검증된 ID token 한 줄만** 나간다. 진단은 전부 stderr 로 보낸다 —
그래야 `mint | canary_monitor --token-stdin` 로 **직접 이어붙일 수 있고**, 중간에 파일이나
command substitution 을 거치지 않는다(거치면 그 순간 값이 디스크·프로세스 목록에 남는다).

⛔ **Web API key 는 argv 가 아니라 stdin 으로** 받는다(`ps` 노출 회피).
⛔ **UID 는 인자로 받는다** — secret 이 아니고, *어느 계정으로 붙는지*는 명시적이어야 한다.

## 왜 교환 뒤에 **다시 검증**하는가

`signInWithCustomToken` 이 200 을 주는 것과 *우리 서버가 그 토큰을 받아들이는 것*은 다른 문제다.
서버는 `verify_id_token(..., check_revoked=True)` 로 판정하므로, 여기서 **같은 의미 검증**을 미리
해 두면 canary 창을 열기 전에 실패를 잡는다. ⚠️ **실행 경로가 같다는 뜻은 아니다** — 실제 WS 는
`ws-auth` named app + 전용 executor + 별도 `httpTimeout` 을 쓴다 — 열고 나서 전 요청이 `invalid_token` 으로
떨어지면 그 창은 통째로 버려진다(env 적용·재기동 비용까지).
⚠️ UID 와 project 도 함께 확인한다. 엉뚱한 계정으로 부하를 주면 그 창의 결과는 의미가 없다.

## 새 UID 를 쓰지 않는 이유

custom token 최초 로그인은 **Firebase 사용자 레코드를 실제로 생성**한다. 그 삭제 책임은
canary rollback 이 소유하지 않으므로(플래그·env 만 되돌린다), **기존 테스트 계정 UID** 를 쓴다.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Sequence

EXCHANGE_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
SERVICE_ACCOUNT_PATH = "/app/firebase-service-account.json"


def log(message: str) -> None:
    """⛔ 진단은 **stderr** — stdout 은 토큰 전용이다."""
    print(message, file=sys.stderr)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="canary 용 ID token 발급 (stdout=token, stderr=진단)", allow_abbrev=False)
    parser.add_argument("--uid", required=True, help="기존 테스트 계정 UID (secret 아님)")
    parser.add_argument("--service-account", default=SERVICE_ACCOUNT_PATH)
    # ⛔ `--api-key` 는 **의도적으로 없다** — argv 는 `ps` 로 보인다. stdin 으로 받는다.
    return parser


def read_api_key(stream: Any = None) -> str:
    key = (stream if stream is not None else sys.stdin).read().strip()
    if not key:
        raise ValueError("Web API key 가 비어 있다(stdin 으로 넣을 것)")   # ⛔ 값 미노출
    return key


#: ⛔ 오류 진단으로 **우리가 고른 문자열만** 내보낸다. 응답 본문을 그대로 실으면 요청값을
#: 반사하는 endpoint 에서 **custom token 과 API key 가 stderr 로 샌다**(재현됨).
KNOWN_EXCHANGE_ERRORS = (
    "INVALID_CUSTOM_TOKEN", "CREDENTIAL_MISMATCH", "TOKEN_EXPIRED",
    "USER_DISABLED", "INVALID_API_KEY", "API_KEY_SERVICE_BLOCKED",
    "PERMISSION_DENIED", "QUOTA_EXCEEDED",
)


def classify_exchange_error(body: str) -> str:
    """본문을 **반사하지 않고** 알려진 코드만 골라 낸다(없으면 코드 미상)."""
    for known in KNOWN_EXCHANGE_ERRORS:
        if known in body:
            return known                       # ⛔ 우리가 고른 리터럴만 나간다
    return "UNRECOGNIZED_ERROR"


def exchange_custom_token(custom_token: str, api_key: str, *,
                          opener: Optional[Callable[..., Any]] = None) -> str:
    """custom token → ID token. ⛔ 오류 **본문을 그대로 싣지 않는다**."""
    request = urllib.request.Request(
        f"{EXCHANGE_URL}?{urllib.parse.urlencode({'key': api_key})}",
        data=json.dumps({"token": custom_token, "returnSecureToken": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:                      # noqa: PERF203
        # ⛔ 본문을 메시지에 넣지 않는다 — 요청값을 반사하는 응답이면 토큰·key 가 그대로 샌다.
        code = classify_exchange_error(exc.read().decode("utf-8", "replace"))
        raise RuntimeError(f"custom token 교환 실패 (HTTP {exc.code}, {code})") from exc
    id_token = payload.get("idToken")
    if not id_token:
        raise RuntimeError("교환 응답에 idToken 이 없다")
    return id_token


def verify_matches_expectation(decoded: dict, *, expected_uid: str, expected_project: str) -> None:
    """⛔ 엉뚱한 계정·project 로 부하를 주면 그 창의 결과는 의미가 없다."""
    actual_uid = decoded.get("uid") or decoded.get("user_id")
    if actual_uid != expected_uid:
        raise RuntimeError(f"ID token 의 uid 가 기대와 다르다: {actual_uid!r} != {expected_uid!r}")
    audience = decoded.get("aud")
    if audience != expected_project:
        raise RuntimeError(f"ID token 의 project 가 다르다: {audience!r} != {expected_project!r}")


def mint(uid: str, api_key: str, service_account_path: str) -> str:
    """존재 확인 → 발급 → 교환 → **서버와 같은 의미로 재검증**(실행 경로는 다르다).

    실패는 전부 예외로 올린다 — 창을 열기 전에 멈추는 것이 목적이다.
    """
    import firebase_admin
    from firebase_admin import auth, credentials

    with open(service_account_path, encoding="utf-8") as handle:
        project_id = json.load(handle).get("project_id")
    if not project_id:
        raise RuntimeError("service account 에 project_id 가 없다")

    app = firebase_admin.initialize_app(
        credentials.Certificate(service_account_path), name="canary-mint")
    try:
        # ⛔ **발급 전에 존재를 확인한다.** custom token 최초 로그인은 UID 가 없으면 사용자
        #    레코드를 **새로 만든다** — 오타나 삭제된 UID 하나면 "기존 계정만 쓴다"는 이 설계의
        #    목적이 통째로 뒤집히고, 뒤의 uid 일치 검사는 (요청한 UID 그대로라) **통과해 버린다**.
        try:
            auth.get_user(uid, app=app)
        except auth.UserNotFoundError as exc:
            raise RuntimeError(
                f"존재하지 않는 UID 다: {uid} — 기존 테스트 계정 UID 를 확인할 것"
                " (새 계정을 만들지 않기 위해 여기서 멈춘다)") from exc
        log(f"[mint] 기존 사용자 확인 (uid={uid})")

        custom_token = auth.create_custom_token(uid, app=app).decode("utf-8")
        log(f"[mint] custom token 발급 (uid={uid}, project={project_id})")
        id_token = exchange_custom_token(custom_token, api_key)
        log("[mint] ID token 교환 완료 — 서버와 같은 의미 기준으로 재검증")
        # ⚠️ 서버와 **같은 의미 검증**(동일 함수·동일 `check_revoked`)이다 — 다만 실제 WS 는
        #    `ws-auth` named app + 전용 executor + 별도 `httpTimeout` 을 쓰므로 **실행 경로는
        #    다르다**. 여기서 잡는 것은 "이 토큰이 우리 판정 기준을 통과하는가"까지다.
        decoded = auth.verify_id_token(id_token, app=app, check_revoked=True)
        verify_matches_expectation(decoded, expected_uid=uid, expected_project=project_id)
        log(f"[mint] 검증 통과 (uid={decoded.get('uid')}, exp={decoded.get('exp')})")
        return id_token
    finally:
        firebase_admin.delete_app(app)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        api_key = read_api_key()
        token = mint(args.uid, api_key, args.service_account)
    except Exception as exc:                                   # noqa: BLE001
        log(f"[mint] 실패: {type(exc).__name__}: {exc}")
        return 1                                               # ⛔ stdout 은 비운다
    sys.stdout.write(token + "\n")                             # ⛔ 오직 이 한 줄
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
