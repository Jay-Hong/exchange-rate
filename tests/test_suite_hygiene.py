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
_SUITE_RUNNERS = frozenset({
    ("unittest", "main"),
    ("main",),          # `from unittest import main`
})
_TERMINAL_CALLS = _SUITE_RUNNERS | frozenset({
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


_CONDITIONAL_STMTS = (
    ast.If, ast.Try, ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith,
)


def _is_terminal_guard(guard: ast.If) -> bool:
    """이 guard가 실행되면 뒤의 정의가 스위트에 못 들어가는가.

    ⚠️ guard 본문의 **직속 문장만** 본다. 조건부 안에 있는 호출은 실행된다는 보장이 없어
    terminal이 아니다 — `ast.walk`로 통째로 뒤지면 아래 같은 **정상 코드가 오탐**된다
    (실측: 3개 정의 전부 실행되는데 검출기는 위반이라고 했다):

        if __name__ == "__main__":
            try:
                import json          # 항상 성공 → exit 분기는 실행되지 않는다
            except ImportError:
                sys.exit("needs json")
    """
    for stmt in guard.body:
        if isinstance(stmt, _CONDITIONAL_STMTS):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _dotted_call_name(node) in _TERMINAL_CALLS:
                return True
    return False


def _outermost_definitions_after(stmts, anchor_lineno: int, out: list[str]) -> None:
    """앵커 뒤의 **가장 바깥** 클래스/함수 정의를 모은다 (중첩 포함, 메서드는 제외).

    ⚠️ `tree.body`만 훑으면 안 된다 — 뒤에 덧붙인 클래스를 `if`로 감싸면 `tree.body`의
    원소가 `ClassDef`가 아니라 `If`라서 **보이지 않는다**. 실측: 아래는 3개 중 1개만
    실행되는데(`Ran 1 test`) top-level 스캔은 위반 0을 반환했다.

        if __name__ == "__main__":
            unittest.main()

        if sys.version_info >= (3, 10):
            class TestNestedAfterGuard(unittest.TestCase): ...
    """
    for node in stmts:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno > anchor_lineno:
                out.append(node.name)
            continue  # 메서드까지 보고하지 않는다
        for field in ("body", "orelse", "finalbody"):
            branch = getattr(node, field, None)
            if isinstance(branch, list):
                _outermost_definitions_after(branch, anchor_lineno, out)
        for handler in getattr(node, "handlers", None) or []:
            _outermost_definitions_after(handler.body, anchor_lineno, out)


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
    """**첫** terminal guard 뒤에 남아 있는 것 — 이름을 알 수 있으면 이름, 아니면 종류.

    ⚠️ 앵커는 **첫** guard다(마지막이 아니다). 마지막을 앵커로 삼으면 "guard → 클래스 →
    guard" 배치에서 결과가 `[]`가 되어 **결함을 그대로 통과시킨다**(2026-07-28 실측).

    ⚠️ 판정 대상은 "정의"가 아니라 **문장 전부**다. 정의만 보면 아래를 놓친다(실측 Ran 1/3):

        if __name__ == "__main__":
            unittest.main()

        TestHost.test_appended = lambda self: None   # ast.Assign — 정의가 아니다

    terminal guard 뒤에는 아무 문장도 없어야 한다. 이 규칙은 단순하고, 위반은 guard를
    끝으로 옮기는 것으로 항상 해소된다.
    """
    tree = ast.parse(source)
    terminal = _terminal_guards(tree)
    if not terminal:
        return []
    anchor = terminal[0].lineno
    offenders: list[str] = []
    for node in tree.body:
        if node.lineno <= anchor:
            continue
        named: list[str] = []
        _outermost_definitions_after([node], anchor, named)
        offenders.extend(named or [f"{type(node).__name__} at line {node.lineno}"])
    return offenders


def _unguarded_suite_runners(source: str) -> list[int]:
    """guard **밖**에서 스위트를 돌리는 module-level 호출의 줄 번호.

    `unittest.main(module=__name__, exit=False, argv=[...])`을 guard 없이 파일 중간에 두면
    pytest가 import하는 것만으로 스위트가 돌고, 그 아래 정의는 직접 실행에서 누락된다
    (실측 Ran 1 / 정의 3). 테스트 파일에서 이 형태는 언제나 결함이다.

    ⚠️ 클래스/함수 **안으로는 내려가지 않는다**. 메서드 본문의 호출은 import 시점에 실행되지
    않기 때문이다. 이걸 빠뜨리면 스크립트 자체의 `main()`을 호출하는 평범한 테스트가 오탐된다
    — 실측으로 `test_krx_baseline_extract.py`, `test_observe_kis_master.py`,
    `test_usdt_ws_baseline_extract.py` 3개가 걸렸고, 셋 다 `TestMain*` 클래스 안에서
    각 스크립트의 `main(...)`을 부르는 정상 코드였다.
    """
    tree = ast.parse(source)
    lines: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            continue
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, _CONDITIONAL_STMTS):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and _dotted_call_name(sub) in _SUITE_RUNNERS:
                lines.append(node.lineno)
                break
    return lines


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
        self.assertEqual(
            _definitions_after_main_guard(hidden),
            ["TestHiddenByTrailingGuard", "If at line 8"],  # 뒤따르는 guard 자체도 잔여 문장이다
        )
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

    def test_definitions_nested_in_a_conditional_are_still_caught(self):
        """⛔ 회귀 잠금 — top-level 스캔만 하면 놓치던 배치(실측 Ran 1 / 정의 3)."""
        src = (
            "import sys\nimport unittest\n"
            + _GUARD
            + "\nif sys.version_info >= (3, 10):\n"
            "    class TestNestedAfterGuard(unittest.TestCase):\n        pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestNestedAfterGuard"])

    def test_conditional_exit_inside_guard_is_not_terminal(self):
        """⛔ 오탐 잠금 — 실행되지 않는 분기의 `sys.exit`(실측 Ran 3 / 정의 3 = 안전)."""
        src = (
            "import sys\nimport unittest\n"
            'if __name__ == "__main__":\n'
            "    try:\n        import json\n    except ImportError:\n"
            '        sys.exit("needs json")\n'
            "\nclass TestAfter(unittest.TestCase):\n    pass\n" + _GUARD
        )
        self.assertEqual(_definitions_after_main_guard(src), [])
        self.assertEqual(_extra_terminal_guards(src), 0)

    def test_methods_are_not_reported_as_offenders(self):
        """보고 단위는 **가장 바깥** 정의다 — 메서드까지 나열하면 메시지가 소음이 된다."""
        src = (
            "import unittest\n"
            + _GUARD
            + "\nclass TestAfter(unittest.TestCase):\n"
            "    def test_one(self):\n        pass\n"
            "    def test_two(self):\n        pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_statements_appended_after_guard_are_caught(self):
        """⛔ 회귀 잠금 — 정의가 아닌 문장으로 테스트를 덧붙이는 형태(실측 Ran 1 / 정의 3)."""
        src = (
            "import unittest\n"
            "class TestHost(unittest.TestCase):\n    pass\n"
            + _GUARD
            + "\nTestHost.test_appended = lambda self: None\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["Assign at line 7"])

    def test_no_module_runs_the_suite_outside_a_guard(self):
        offenders = {
            path.name: lines
            for path in sorted(_TESTS_DIR.glob("test_*.py"))
            if (lines := _unguarded_suite_runners(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(offenders, {}, "guard 밖 `unittest.main()`은 import만으로 스위트를 돌린다")

    def test_unguarded_runner_detector_is_not_vacuous(self):
        """positive/negative control."""
        bad = "import unittest\nunittest.main(exit=False)\nclass T(unittest.TestCase):\n    pass\n"
        self.assertEqual(_unguarded_suite_runners(bad), [2])
        self.assertEqual(_unguarded_suite_runners("import unittest\n" + _GUARD), [])

    def test_call_inside_a_method_is_not_module_level(self):
        """⛔ 오탐 잠금 — 스크립트 자체의 `main()`을 부르는 평범한 테스트(실측 3개 파일이 걸렸다)."""
        src = (
            "import unittest\n"
            "from mymod import main\n"
            "class TestMain(unittest.TestCase):\n"
            "    def test_runs(self):\n        main(['--flag'])\n" + _GUARD
        )
        self.assertEqual(_unguarded_suite_runners(src), [])

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
