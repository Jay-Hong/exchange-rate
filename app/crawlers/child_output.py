"""자식 프로세스가 stdout 에 남긴 것을 부모 로그로 옮기기 위한 최소 도우미.

배경(2026-09-21 실측): 앱 로깅은 `StreamHandler(sys.stdout)` 이고, `runner.py` 는 실패할 때
`logger.exception(...)` 으로 추적 기록을 **stdout** 에 쓴다. 그런데 hana·woori 부모는
`capture_output=True` 로 두 스트림을 다 붙잡아 놓고 **stderr 만** 읽는다. 그래서 컨테이너
로그에 남는 것은 `"No error output"` 한 줄뿐이다 — 원인이 없는 것이 아니라, 있는 곳을
안 읽고 **없다고 적는다**.

⚠️ 기록 자체가 사라지지는 않는다(내가 처음에 그렇게 단정했다가 확인하고 정정했다).
자식도 같은 로깅 설정으로 `/app/logs/error.log` 에 `exc_info` 까지 쓰고 거기엔 6주치가
남아 있었다. 다만 `app.log` 는 10MB×3 회전이라 몇 시간이면 지나가고, 무엇보다 운영자가
보는 Docker 로그 스트림에는 안 온다.

⛔ 여기서 만든 값은 **로그 전용**이다. 판정·반환값·폴백 순서·writer 입력에 쓰지 않는다
(§7.13 의 "자식 로그를 재파싱해 결과로 승격하지 않는다" 경계를 지킨다).

⛔ **이 후처리는 subprocess.run(timeout=45)의 반환/시간초과 뒤에 돈다.** 자식의 45초
창을 갉아먹지는 않지만, 부모의 예외 전파와 다음 폴백을 늦추므로 처리 시간은 입력 길이에
선형이어야 한다. 1차 구현은 이름 양쪽에 `*` 를 둔 정규식을 **모든 위치에서** 시도해
8,000자에 8초가 걸렸다(실측). 완전한 구조와 완전한 로그 객체 줄을 정밀하게 처리한다.
로그 객체 사이의 비정형 구간은 민감한 대입이 있으면 통째로 지운다. 값 경계를 추측하지 않으며,
중첩 처리 깊이에도 고정 상한을 두어 같은 문자열의 반복 파싱을 제한한다.
"""

import ast
import json
import re

#: 남길 꼬리 길이. 추적 기록은 자식 stdout 의 **끝**에 있으므로 머리가 아니라 꼬리를 쓴다 —
#: 앞쪽 1500자는 기동 잡음뿐이다.
TAIL_LIMIT = 1500

MASK = "***MASKED***"

#: 마스킹 자체가 실패하면 원문 대신 이것만 남긴다. 진단 하나 때문에 비밀을 흘리지 않는다.
MASK_FAILED = "***MASKING_FAILED***"

#: 이름에 이 낱말이 들어가면 자료형에 관계없이 **값 전체**를 지운다.
_KEYWORDS = re.compile(
    r"key|secret|password|passwd|token|credential|authorization|cookie",
    re.IGNORECASE,
)

#: 이름 토큰을 한 번씩 소비한다. Unicode IGNORECASE로 잡힌 글자도 \w 안에 들어가므로
#: 옛 ASCII 이름 확장/scanned 불변식에 의존하지 않는다.
_NAMES = re.compile(r"[\w.-]+")
#: 줄바꿈·인용부호는 건너뛰되 대입 기호가 있어야 한다. 값의 시작/끝은 검사하지 않는다.
_ASSIGNMENT = re.compile(r"[\\\"'\s]*[:=]")
#: 파싱 불가 텍스트에서도 이스케이프로 숨긴 이름을 검사한다. 출력은 원문 또는 표지다.
_ESCAPED_CHAR = re.compile(
    r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[0-7]{1,3}|[\\\"'/abfnrtv])"
)
#: JSON 문자열 안의 U+2028 등은 줄 경계가 아니다. splitlines()를 쓰지 않는다.
_LINES = re.compile(r"[^\r\n]+")
_MAX_DEPTH = 32

#: `scheme://user:password@host` 의 비밀번호.
#: ⛔ 패턴을 `[a-zA-Z][a-zA-Z0-9+.-]*://` 로 시작하면 `x` 가 이어진 긴 입력에서 매 위치마다
#:    끝까지 훑고 되돌아와 **제곱 시간**이 된다(실측: 200,000자에 113초). literal `://` 로
#:    앵커하면 엔진이 `:` 를 먼저 찾아 선형으로 지나간다.
#: ⚠️ 사용자명은 `*` 다 — `redis://:password@host` 처럼 **비어 있는** 형태가 실제로 쓰인다(실측).
_URL_CREDENTIAL = re.compile(r"(://[^/\s:@]*:)([^/\s@]+)(@)")


def _as_text(value):
    """str·bytes·None 을 모두 받는다.

    ⛔ bytes 를 받는 경로가 실제로 있다. `subprocess.run(text=True, timeout=...)` 이 시간을
    넘겨 `TimeoutExpired` 를 올릴 때, 그 예외의 `stdout` 은 **디코딩되지 않은 bytes** 이고
    `stderr` 는 `None` 일 수 있다(Python 3.13.5 실측). 정상 종료 경로만 보고 str 을
    가정하면, 가장 드물어서 가장 검증하기 어려운 타임아웃 경로에서만 터진다.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _decode_escape(match):
    escape = match[0][1:]
    if escape[0] in 'uUx':
        return chr(int(escape[1:], 16))
    if escape[0] in '01234567':
        return chr(int(escape, 8))
    return {'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n',
            'r': '\r', 't': '\t', 'v': '\v'}.get(escape, escape)


def _mask_unstructured(text):
    """민감한 대입이 하나라도 있으면 문자열 전체를 지운다. 값의 끝은 찾지 않는다."""
    inspected = text
    for _ in range(_MAX_DEPTH):
        decoded = _ESCAPED_CHAR.sub(_decode_escape, inspected)
        if decoded == inspected:
            break
        inspected = decoded
    else:
        return MASK_FAILED
    for token in _NAMES.finditer(inspected):
        name = token.group()
        if not _KEYWORDS.search(name):
            continue
        assignment = _ASSIGNMENT.match(inspected, token.end())
        if assignment is None:
            continue
        # KeyError: 'USD'는 비밀값 대입이 아니라 누락 키를 알려 주는 예외다. 이 정확한
        # 비인용 예외명만 제외해 진단을 보존한다. 그 뒤 'api_key=...' 등은 계속 검사하며,
        # 객체의 "KeyError" 키나 KeyError=... 대입에는 이 예외를 적용하지 않는다.
        if (name == "KeyError" and assignment.group().lstrip().startswith(":")
                and (token.start() == 0 or inspected[token.start() - 1].isspace())):
            continue
        return MASK
    # URL 자격증명은 기존의 명확한 구문을 유지한다. 이스케이프된 URL은 원문 위치를
    # 역산하지 않고 전체를 가린다. 비밀이 없는 텍스트는 디코딩한 사본이 아닌 원문이다.
    if inspected != text and _URL_CREDENTIAL.search(inspected):
        return MASK
    return _mask_url_credentials(text)


def _mask_url_credentials(text):
    return _URL_CREDENTIAL.sub(lambda m: m.group(1) + MASK + m.group(3), text)


def _mask_value(value, depth):
    """파싱된 값만 순회한다. (새 값, 변경 여부)로 불필요한 재직렬화를 피한다."""
    if depth >= _MAX_DEPTH:
        return MASK_FAILED, True
    if isinstance(value, dict):
        result = {}
        changed = False
        for key, item in value.items():
            sensitive = False
            if isinstance(key, (str, bytes)):
                # 이름 자체도 외부 입력이다. 값의 민감도는 원래 이름으로 판정하되,
                # 키도 문자열 값과 같은 규칙으로 지운다(대입·직렬화된 구조·헤더·URL).
                key_text = _as_text(key)
                sensitive = _KEYWORDS.search(key_text)
            # Python 리터럴의 튜플 키에도 문자열·bytes·중첩 튜플이 들어갈 수 있다.
            masked_key, key_changed = _mask_value(key, depth + 1)
            if sensitive:
                result[masked_key], replaced = MASK, item != MASK
            else:
                result[masked_key], replaced = _mask_value(item, depth + 1)
            changed |= replaced or key_changed
        return result, changed
    if isinstance(value, (list, tuple, set)):
        result = []
        changed = False
        for item in value:
            masked, replaced = _mask_value(item, depth + 1)
            result.append(masked)
            changed |= replaced
        return type(value)(result), changed
    if isinstance(value, (str, bytes)):
        text = _as_text(value)
        masked = _mask_text(text, depth + 1)
        if masked == text:
            return value, False
        return (masked.encode("utf-8") if isinstance(value, bytes) else masked), True
    return value, False


def _mask_structure(text, depth, *, record=False):
    """완전한 구조를 처리한다. 파싱 불가 또는 로그 레코드가 아니면 None이다."""
    stripped = text.lstrip()
    # 스칼라·배열은 여러 줄 비밀값의 일부일 수 있어 로그 경계를 만들지 않는다.
    if record and not stripped.startswith("{"):
        return None
    # JSON을 우선하고 작은따옴표·bytes·tuple 등의 Python repr는 literal_eval로 처리한다.
    # eval은 쓰지 않는다. 줄 중간마다 파서를 재시도하면 제곱 시간이 되므로 각각 한 번만 시도한다.
    if stripped.startswith(("{", "[", "(", '"', "'", "b'", 'b"')):
        collapsed = False

        def object_from_pairs(pairs):
            nonlocal collapsed
            result = dict(pairs)
            collapsed |= len(result) != len(pairs)
            return result

        try:
            value = json.loads(text, object_pairs_hook=object_from_pairs)
            is_json = True
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(stripped)
                is_json = False
            except (SyntaxError, ValueError):
                return None
        # 객체만으로도 부족하다: api_key=\n{"value": "..."}는 비밀값이다.
        # 최상위 혼합 출력의 경계는 exc_info 또는 로깅 공통 필드가 있는 dict로
        # 한정한다. message만 있는 일반 객체도 값일 수 있다. 그 밖의 객체·set은
        # 앞뒤 비정형 줄과 함께 검사한다. 중첩 문자열에는 적용하지 않는다.
        if record and (not isinstance(value, dict)
                       or not ("exc_info" in value
                               or all(key in value for key in ("timestamp", "level", "message")))):
            return None
        masked, changed = _mask_value(value, depth + 1)
        # 중복 키는 파싱 도중 앞의 값이 사라진다. 남은 값에 변경이 없다고 원문을 반환하면
        # 검사하지 못한 앞의 비밀값이 다시 나온다. JSON은 중복이 있을 때 재직렬화하고,
        # literal_eval에는 pairs hook이 없으므로 Python 리터럴은 항상 재직렬화한다.
        if changed or collapsed or not is_json:
            rendered = json.dumps(masked, ensure_ascii=False) if is_json else repr(masked)
            # 레코드 앞뒤의 공백·줄 구분자는 구조의 일부가 아니므로 그대로 남긴다.
            end = len(text.rstrip())
            return text[:len(text) - len(stripped)] + rendered + text[end:]
        return text
    return None


def _mask_record_gap(text):
    """연속된 비정형 구간을 가리되 레코드 사이의 공백·줄 구분자는 보존한다."""
    stripped = text.strip()
    if not stripped:
        return text
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return text[:start] + _mask_unstructured(stripped) + text[end:]


def _mask_text(text, depth, *, records=False):
    if depth >= _MAX_DEPTH:
        return MASK_FAILED
    # 디코딩한 문자열은 여러 줄이어도 하나의 값이다. 파싱 실패 후 줄별로 나누면
    # 다음 줄의 스칼라나 콜론이 비밀 이름에서 분리된다.
    masked = _mask_structure(text, depth)
    if masked is not None:
        return masked
    if not records or ("\n" not in text and "\r" not in text):
        return _mask_unstructured(text)
    # 최상위 stdout에서만 로그 객체 줄이 비정형 구간을 끊는다. 깨진 줄을 각각
    # 검사하지 않고 다음 레코드까지 모아 다음 줄의 값·콜론도 함께 가린다.
    # 구간을 계속 덧붙이거나 재검사하지 않고 겹치지 않는 슬라이스를 한 번씩 처리한다.
    parts = []
    consumed = 0
    for line in _LINES.finditer(text):
        masked = _mask_structure(line.group(), depth, record=True)
        if masked is None:
            continue
        parts.append(_mask_record_gap(text[consumed:line.start()]))
        parts.append(masked)
        consumed = line.end()
    if not parts:
        return _mask_unstructured(text)
    parts.append(_mask_record_gap(text[consumed:]))
    return "".join(parts)


def mask_secrets(text):
    """JSON 로그와 그 안의 문자열을 각각 마스킹해 바깥 진단 필드를 보존한다.

    고정 깊이 안에서 겹치지 않는 줄·값·이름만 처리하므로 O(n)이다. 파싱 불가는 보수적
    fallback으로 처리하고, 그 밖의 예외가 나면 원문 없이 실패 표지만 반환한다.
    """
    try:
        return _mask_text(_as_text(text), 0, records=True)
    except Exception:
        return MASK_FAILED


def stdout_tail(output, limit=TAIL_LIMIT):
    """자식 stdout 의 꼬리를, **먼저 마스킹한 뒤** 잘라서 돌려준다.

    ⛔ 순서가 중요하다. 잘라서 마스킹하면 경계가 `"api_key": "abc` 한가운데를 지나갈 때
    이름이 잘려 나가 값 조각만 남고, 규칙이 그것을 못 알아본다. 먼저 지우면 경계는
    `***MASKED***` 안쪽만 자른다.

    마스킹이 예외를 내면 원문을 흘리는 대신 표지만 돌려준다 — 이 값은 진단용이고,
    진단을 얻자고 비밀을 내보내는 교환은 하지 않는다.
    """
    try:
        masked = mask_secrets(output)
    except Exception:
        return MASK_FAILED
    if masked == MASK_FAILED:
        return MASK_FAILED
    if limit is not None and limit >= 0:
        return masked[-limit:] if limit else ""
    return masked


def child_log_fields(output, *, prefix, limit=TAIL_LIMIT):
    """로그 `extra` 에 실을 필드 묶음.

    길이는 **문자 수**다(바이트가 아니다 — 자식 출력은 디코딩된 문자열이고 한글이 섞인다).
    잘렸는지는 길이로 유추할 수 없다: 마스킹이 길이를 바꾸므로 원문 길이와 꼬리 길이를
    비교해도 답이 안 나온다. 그래서 따로 적는다.

    ⛔ 마스킹은 **한 번만** 한다. 1차 구현은 여기서 한 번, `stdout_tail` 안에서 또 한 번
    돌려 비용을 두 배로 냈다.
    """
    text = _as_text(output)
    try:
        masked = mask_secrets(text)
    except Exception:
        masked = MASK_FAILED
    if masked == MASK_FAILED:
        return {f"{prefix}_tail": MASK_FAILED, f"{prefix}_chars": len(text),
                f"{prefix}_truncated": False}
    tail = masked
    if limit is not None and limit >= 0:
        tail = masked[-limit:] if limit else ""
    return {
        f"{prefix}_tail": tail,
        f"{prefix}_chars": len(text),
        f"{prefix}_truncated": len(masked) > len(tail),
    }
