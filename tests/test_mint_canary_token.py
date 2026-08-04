"""canary ID token 발급기 — **값이 새지 않는가** + 잘못된 토큰으로 창을 열지 않는가.

⛔ 이 도구의 출력은 곧바로 `canary_monitor --token-stdin` 으로 이어진다. 그래서 stdout 에
진단이 한 줄이라도 섞이면 **토큰이 오염**되고, 반대로 토큰이 stderr·파일·argv 로 새면
`ps`·로그·디스크에 남는다.
"""

import json
import sys
from io import BytesIO, StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import mint_canary_token as mint  # noqa: E402

TOKEN = "eyJhbGciOi.ID-TOKEN-VALUE.sig"
API_KEY = "AIzaSyFAKE-WEB-API-KEY-VALUE"


# ── 값 비노출 ───────────────────────────────────────────────────────────────


def test_api_key_is_never_a_cli_argument():
    """⛔ argv 는 `ps` 로 다른 사용자에게 보인다 — key 는 stdin 으로만 받는다."""
    parser = mint.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--uid", "u1", "--api-key", API_KEY])
    assert parser.allow_abbrev is False, "접두어 축약이 켜져 있으면 `--api` 가 다른 옵션에 붙는다"


def test_api_key_comes_from_stdin():
    assert mint.read_api_key(StringIO(API_KEY + "\n")) == API_KEY


def test_empty_api_key_is_rejected_without_echoing():
    with pytest.raises(ValueError) as caught:
        mint.read_api_key(StringIO("  \n"))
    assert API_KEY not in str(caught.value)


def test_only_the_token_goes_to_stdout(monkeypatch, capsys):
    """⛔ stdout 에 진단이 섞이면 **파이프로 받은 토큰이 오염**된다."""
    monkeypatch.setattr(mint, "read_api_key", lambda *a, **k: API_KEY)
    monkeypatch.setattr(mint, "mint", lambda uid, key, path: TOKEN)

    assert mint.main(["--uid", "test-uid"]) == 0
    captured = capsys.readouterr()
    assert captured.out == TOKEN + "\n", f"stdout 이 토큰 한 줄이 아니다: {captured.out!r}"


def test_diagnostics_go_to_stderr_only(capsys):
    mint.log("진단 메시지")
    captured = capsys.readouterr()
    assert captured.out == "", "진단이 stdout 으로 샜다 — 파이프가 오염된다"
    assert "진단 메시지" in captured.err


def test_failure_leaves_stdout_empty_and_exits_nonzero(monkeypatch, capsys):
    """⛔ 실패했는데 stdout 에 뭔가 남으면 monitor 가 **쓰레기를 토큰으로** 받는다."""
    monkeypatch.setattr(mint, "read_api_key", lambda *a, **k: API_KEY)

    def boom(uid, key, path):
        raise RuntimeError("교환 실패")

    monkeypatch.setattr(mint, "mint", boom)
    assert mint.main(["--uid", "test-uid"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", "실패인데 stdout 에 출력이 있다"
    assert "교환 실패" in captured.err


def test_exchange_error_message_never_carries_the_custom_token():
    """⚠️ 오류 본문에 토큰을 실으면 로그·터미널에 그대로 남는다."""
    import urllib.error

    def failing(request, timeout=None):
        # ⚠️ 실제 `HTTPError.read()` 는 **bytes** 를 준다 — 대역이 str 을 주면 코드가 아니라
        #    대역이 틀린 것이다(여기서 실제로 겪었다).
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                     BytesIO(b'{"error":{"message":"INVALID_CUSTOM_TOKEN"}}'))

    with pytest.raises(RuntimeError) as caught:
        mint.exchange_custom_token("SECRET-CUSTOM-TOKEN", API_KEY, opener=failing)
    assert "SECRET-CUSTOM-TOKEN" not in str(caught.value)
    assert API_KEY not in str(caught.value)


# ── 잘못된 토큰으로 창을 열지 않는가 ────────────────────────────────────────


def test_uid_mismatch_is_rejected():
    """⛔ 엉뚱한 계정으로 부하를 주면 **그 창의 결과가 의미를 잃는다**."""
    with pytest.raises(RuntimeError) as caught:
        mint.verify_matches_expectation({"uid": "someone-else", "aud": "fx-i-3b95b"},
                                        expected_uid="test-uid", expected_project="fx-i-3b95b")
    assert "uid" in str(caught.value)


def test_project_mismatch_is_rejected():
    """⚠️ 다른 project 의 토큰은 서버가 거부한다 — 창을 열기 **전에** 잡는다."""
    with pytest.raises(RuntimeError):
        mint.verify_matches_expectation({"uid": "test-uid", "aud": "other-project"},
                                        expected_uid="test-uid", expected_project="fx-i-3b95b")


def test_matching_claims_pass():
    mint.verify_matches_expectation({"uid": "test-uid", "aud": "fx-i-3b95b"},
                                    expected_uid="test-uid", expected_project="fx-i-3b95b")


def test_user_id_claim_is_accepted_as_uid():
    """⚠️ 디코드 결과는 `uid` 대신 `user_id` 만 담기도 한다 — 그걸 불일치로 읽으면
    **정상 토큰을 버린다**."""
    mint.verify_matches_expectation({"user_id": "test-uid", "aud": "fx-i-3b95b"},
                                    expected_uid="test-uid", expected_project="fx-i-3b95b")


def test_exchange_requires_an_id_token_in_the_response():
    def empty(request, timeout=None):
        class _R:
            def read(self):
                return json.dumps({"kind": "identitytoolkit#VerifyCustomTokenResponse"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _R()

    with pytest.raises(RuntimeError) as caught:
        mint.exchange_custom_token("ct", API_KEY, opener=empty)
    assert "idToken" in str(caught.value)


# ── 기존 사용자만 쓴다 (새 계정 생성 회피의 실체) ──────────────────────────


class _FakeUserNotFound(Exception):
    pass


class _FakeAuth:
    """`firebase_admin.auth` 대역 — 호출 **순서**와 횟수를 기록한다."""

    UserNotFoundError = _FakeUserNotFound

    def __init__(self, *, existing=("known-uid",)):
        self.existing = set(existing)
        self.calls: list[str] = []

    def get_user(self, uid, app=None):
        self.calls.append(f"get_user:{uid}")
        if uid not in self.existing:
            raise _FakeUserNotFound(uid)
        return object()

    def create_custom_token(self, uid, app=None):
        self.calls.append(f"create_custom_token:{uid}")
        return b"CUSTOM-TOKEN"

    def verify_id_token(self, token, app=None, check_revoked=False):
        self.calls.append(f"verify:{check_revoked}")
        return {"uid": "known-uid", "aud": "test-project", "exp": 1}


def _patch_firebase(monkeypatch, tmp_path, fake_auth):
    import types

    service_account = tmp_path / "sa.json"
    service_account.write_text(json.dumps({"project_id": "test-project"}))

    fake_admin = types.SimpleNamespace(
        initialize_app=lambda cred, name=None: object(),
        delete_app=lambda app: None,
    )
    fake_credentials = types.SimpleNamespace(Certificate=lambda path: object())
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_admin)
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "firebase_admin.credentials", fake_credentials)
    fake_admin.auth = fake_auth
    fake_admin.credentials = fake_credentials
    return str(service_account)


def test_existing_user_is_checked_before_minting(monkeypatch, tmp_path):
    """⛔ **이 확인이 없으면 설계 목적이 뒤집힌다.** custom token 최초 로그인은 UID 가 없으면
    사용자 레코드를 **새로 만들고**, 뒤의 uid 일치 검사는 (요청한 UID 그대로라) 통과한다 —
    오타 하나로 "기존 계정만 쓴다"가 "새 계정을 만든다"가 된다."""
    fake_auth = _FakeAuth()
    path = _patch_firebase(monkeypatch, tmp_path, fake_auth)
    monkeypatch.setattr(mint, "exchange_custom_token", lambda *a, **k: TOKEN)

    assert mint.mint("known-uid", API_KEY, path) == TOKEN
    assert fake_auth.calls[0] == "get_user:known-uid", \
        f"존재 확인이 발급보다 먼저가 아니다: {fake_auth.calls}"
    assert fake_auth.calls[1] == "create_custom_token:known-uid"


def test_unknown_uid_stops_before_any_exchange(monkeypatch, tmp_path):
    """⛔ 존재하지 않는 UID 면 **교환 0회** — 교환이 곧 계정 생성이다."""
    fake_auth = _FakeAuth()
    path = _patch_firebase(monkeypatch, tmp_path, fake_auth)
    exchanges = []
    monkeypatch.setattr(mint, "exchange_custom_token",
                        lambda *a, **k: exchanges.append(1) or TOKEN)

    with pytest.raises(RuntimeError) as caught:
        mint.mint("typo-uid", API_KEY, path)

    assert "존재하지 않는 UID" in str(caught.value)
    assert exchanges == [], "존재하지 않는 UID 인데 교환을 시도했다 — 계정이 생성된다"
    assert not any(c.startswith("create_custom_token") for c in fake_auth.calls)


# ── 오류 본문 반사 유출 (이전 테스트가 공허했던 자리) ──────────────────────


def test_reflecting_error_body_never_leaks_the_token_or_key(capsys, monkeypatch, tmp_path):
    """⛔ **이전 테스트는 공허했다** — 반사하지 않는 고정 본문만 써서 유출을 볼 수 없었다.
    실제로 요청값을 **되돌려주는** 응답을 만들어 확인한다(그런 endpoint 는 실재한다)."""
    import urllib.error

    custom_token = "CUSTOM-TOKEN-SECRET"

    def reflecting(request, timeout=None):
        body = json.dumps({"error": {"message": "INVALID_CUSTOM_TOKEN",
                                     "echo": {"url": request.full_url,
                                              "payload": request.data.decode()}}}).encode()
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body))

    with pytest.raises(RuntimeError) as caught:
        mint.exchange_custom_token(custom_token, API_KEY, opener=reflecting)

    message = str(caught.value)
    assert custom_token not in message, "custom token 이 예외 메시지로 샜다"
    assert API_KEY not in message, "API key 가 예외 메시지로 샜다"
    assert "INVALID_CUSTOM_TOKEN" in message, "알려진 오류 코드는 남겨야 진단이 된다"

    mint.log(f"[mint] 실패: {message}")
    stderr = capsys.readouterr().err
    assert custom_token not in stderr and API_KEY not in stderr, "stderr 로 샜다"


def test_unknown_error_body_is_not_reflected():
    """⚠️ 모르는 코드면 **아무것도 인용하지 않는다** — 반사가 곧 유출이다."""
    import urllib.error

    def reflecting(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {},
                                     BytesIO(request.data))          # 요청 전문을 그대로

    with pytest.raises(RuntimeError) as caught:
        mint.exchange_custom_token("CT-SECRET", API_KEY, opener=reflecting)
    message = str(caught.value)
    assert "CT-SECRET" not in message and API_KEY not in message
    assert "UNRECOGNIZED_ERROR" in message
