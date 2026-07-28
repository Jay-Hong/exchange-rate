"""테스트 스위트 자체의 위생 — **조용히 누락되는 테스트**를 막는다.

## 왜 필요한가 (실측 사고, 2026-07-28)

`if __name__ == "__main__": unittest.main()` **뒤에** 테스트 클래스를 추가하면, 직접 실행 시
그 클래스는 스위트에 들어가지 못한다. pytest는 모듈을 import만 하므로 전부 수집하지만,
`python tests/foo.py`로 직접 돌리면 조용히 일부만 실행되고 **OK가 뜬다**.

⚠️ **원인은 `sys.exit`가 아니다** (2026-07-28 실측으로 정정). `unittest.main()`은 호출 시점의
모듈 네임스페이스에서 테스트를 **발견**하므로, 아직 정의되지 않은 뒤쪽 클래스는 애초에
수집 대상이 아니다. 그래서 프로세스를 죽이지 않는 `unittest.main(exit=False)`도 **똑같이
위험하다** — 실측: 클래스 2개 중 `Ran 1 test`. 반대로 스위트를 돌리지 않는 guard
(예: `print()`만 하는 블록)는 **안전하다** — 실측: `Ran 2 tests`.
그래서 이 검사기는 "프로세스를 끝내는 guard"가 아니라 **"스위트를 확정하는 guard"**를 찾는다.

실측 격차(수정 전):

    test_strict_authz        직접 32 / pytest 43   ← revocation 축·uid 결속·생성 안전성 누락
    test_revenuecat_provider 직접 11 / pytest 20
    test_comparison_api      직접 21 / pytest 25

앞의 둘은 `cat >>`로 클래스를 덧붙이다 **한 세션에 두 번** 만든 것이고, 셋째는 그 전부터
있었다. 습관에서 반복되는 결함이라 메모리나 리뷰가 아니라 **테스트로** 막는다.

⚠️ 이 검사는 CI 결과를 바꾸지 않는다(CI는 pytest로 전부 수집한다). 막는 것은 **개발자가
파일을 직접 실행했을 때 보는 가짜 green**이다.

⚠️ 별개 사실: 직접 실행은 `tests/conftest.py`를 로드하지 않으므로 firebase stub·DB URL 격리가
없다. 그래서 그 stub에 의존하는 파일은 직접 실행 시 **에러**가 난다 — 그건 이 검사의 대상이
아니고(조용하지 않다), 정상이다.
"""
import ast
import pathlib
import unittest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent

# guard 본문이 이걸 호출하면 **그 지점에서 스위트가 확정된다**(또는 프로세스가 끝난다).
# `unittest.main`은 `exit=` 값과 무관하게 포함한다 — 실측상 `exit=False`도 위험하기 때문이다.
_TERMINAL_CALLS = frozenset({
    ("unittest", "main"),
    ("main",),          # `from unittest import main`
    ("sys", "exit"),
    ("exit",),
    ("os", "_exit"),
})


def _dotted_call_name(node: ast.Call) -> tuple[str, ...] | None:
    """`a.b.c(...)` → `("a", "b", "c")`. 이름으로 환원 불가하면 None."""
    parts: list[str] = []
    func = node.func
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    parts.append(func.id)
    return tuple(reversed(parts))


def _is_terminal_guard(guard: ast.If) -> bool:
    """이 guard가 실행되면 뒤의 정의가 스위트에 못 들어가는가."""
    for node in ast.walk(guard):
        if isinstance(node, ast.Call) and _dotted_call_name(node) in _TERMINAL_CALLS:
            return True
    return False


def _terminal_guards(tree: ast.Module) -> list[ast.If]:
    """top-level `__main__` guard 중 **terminal**인 것만 (등장 순서)."""
    return [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and "__main__" in ast.dump(node.test)
        and _is_terminal_guard(node)
    ]


def _definitions_after_main_guard(source: str) -> list[str]:
    """**첫** terminal guard 뒤에 정의된 top-level 클래스/함수 이름.

    ⚠️ 앵커는 **첫** guard다(마지막이 아니다). 마지막을 앵커로 삼으면 "guard → 클래스 →
    guard" 배치에서 결과가 `[]`가 되어 **결함을 그대로 통과시킨다**(2026-07-28 실측).
    """
    tree = ast.parse(source)
    terminal = _terminal_guards(tree)
    if not terminal:
        return []
    anchor = terminal[0]
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno > anchor.lineno
    ]


def _extra_terminal_guards(source: str) -> int:
    """첫 terminal guard 이후의 추가 terminal guard 개수 (0이어야 한다).

    두 번째 guard는 최선의 경우에도 **죽은 코드**이고, 최악의 경우 앞 guard 뒤에 붙은
    정의를 "끝에 guard가 있으니 괜찮다"고 오독하게 만든다 — 실제로 그 배치가 이 검사기의
    구 버전을 통과시켰다. 배치 자체를 금지한다.
    """
    return max(0, len(_terminal_guards(ast.parse(source))) - 1)


_GUARD = 'if __name__ == "__main__":\n    unittest.main()\n'


class TestNoDefinitionsAfterMainGuard(unittest.TestCase):
    def test_every_test_module_is_fully_reachable_when_run_directly(self):
        offenders = {}
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            after = _definitions_after_main_guard(path.read_text(encoding="utf-8"))
            if after:
                offenders[path.name] = after
        self.assertEqual(
            offenders,
            {},
            "`unittest.main()` 뒤의 정의는 직접 실행 시 조용히 누락된다 — guard를 파일 끝으로 옮길 것",
        )

    def test_no_module_has_a_second_terminal_guard(self):
        offenders = {
            path.name: extra
            for path in sorted(_TESTS_DIR.glob("test_*.py"))
            if (extra := _extra_terminal_guards(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(
            offenders, {}, "terminal guard는 파일당 1개 — 두 번째는 죽은 코드이고 위 검사를 무력화한다"
        )

    def test_detector_actually_detects(self):
        """positive control — 검사기가 항상 빈 목록을 돌려주는 형태면 위 단언이 공허하다."""
        planted = "import unittest\n" + _GUARD + "\nclass TestAppendedAfterGuard(unittest.TestCase):\n    pass\n"
        self.assertEqual(_definitions_after_main_guard(planted), ["TestAppendedAfterGuard"])

    def test_detector_accepts_guard_at_end(self):
        """negative control — 정상 배치를 위반으로 잡으면 안 된다."""
        ok = "import unittest\n\nclass TestFine(unittest.TestCase):\n    pass\n\n" + _GUARD
        self.assertEqual(_definitions_after_main_guard(ok), [])

    def test_second_guard_at_end_does_not_hide_the_first(self):
        """⛔ 회귀 잠금 — 구 버전(마지막 guard 앵커)이 **조용히 통과시키던** 배치.

        실측(2026-07-28): 이 배치를 직접 실행하면 정의된 4개 중 `Ran 2 tests`에 **`OK`**가
        찍히고, 구 검사기는 `[]`(위반 없음)을 반환했다.
        """
        hidden = (
            "import unittest\n"
            + _GUARD
            + "\nclass TestHiddenByTrailingGuard(unittest.TestCase):\n    pass\n\n"
            + _GUARD
        )
        self.assertEqual(_definitions_after_main_guard(hidden), ["TestHiddenByTrailingGuard"])
        self.assertEqual(_extra_terminal_guards(hidden), 1)

    def test_exit_false_is_still_terminal(self):
        """`exit=False`는 프로세스를 안 죽이지만 **수집 시점**이 앞이라 여전히 위험하다(실측 Ran 1/2)."""
        src = (
            "import unittest\n"
            'if __name__ == "__main__":\n    unittest.main(exit=False)\n'
            "\nclass TestAfter(unittest.TestCase):\n    pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_non_suite_running_guard_is_not_terminal(self):
        """스위트를 돌리지 않는 guard는 뒤 정의를 막지 않는다(실측 Ran 2/2) — 잡으면 오탐이다."""
        src = (
            "import unittest\n"
            'if __name__ == "__main__":\n    print("setup only")\n'
            "\nclass TestAfter(unittest.TestCase):\n    pass\n" + _GUARD
        )
        self.assertEqual(_definitions_after_main_guard(src), [])
        self.assertEqual(_extra_terminal_guards(src), 0)

    def test_bare_imported_main_counts(self):
        """`from unittest import main` 형태도 앵커다 — 이름만 다르고 위험은 같다."""
        src = (
            "from unittest import main\n"
            'if __name__ == "__main__":\n    main()\n'
            "\nclass TestAfter(unittest.TestCase):\n    pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])


if __name__ == "__main__":
    unittest.main()
