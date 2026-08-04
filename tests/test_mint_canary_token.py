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
