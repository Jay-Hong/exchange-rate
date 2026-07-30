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

    test_revenuecat_provider 직접 11 / pytest 20   ← guard 뒤 4개 클래스가 수집조차 안 됨

⚠️ **격차가 곧 guard 탓은 아니다** (2026-07-30 실측). 위 파일은 guard만 파일 끝으로 옮기자
11 → 20으로 회복됐다(pytest와 일치) — guard가 원인이다. 반면 `test_comparison_api`는 이동 전후
모두 21이고 pytest는 25다: 그 4개 격차는 **직접 실행이 `conftest.py`를 로드하지 않아** 스텁이
없고 한 클래스가 `setUpClass`에서 죽는 것이다(그 클래스는 애초에 직접 실행이 불가능하다).
그래서 이 검사기는 **격차를 재는 것이 아니라 guard 뒤 정의의 존재만** 본다 — 그것이 유일하게
guard에 귀속되는 성질이다.

앞의 둘은 `cat >>`로 클래스를 덧붙이다 **한 세션에 두 번** 만든 것이고, 셋째는 그 전부터
있었다. 습관에서 반복되는 결함이라 메모리나 리뷰가 아니라 **테스트로** 막는다.

⚠️ 이 검사는 CI 결과를 바꾸지 않는다(CI는 pytest로 전부 수집한다). 막는 것은 **개발자가
파일을 직접 실행했을 때 보는 가짜 green**이다.

⚠️ 별개 사실: 직접 실행은 `tests/conftest.py`를 로드하지 않으므로 firebase stub·DB URL 격리가
없다. 그래서 그 stub에 의존하는 파일은 직접 실행 시 **에러**가 난다 — 그건 이 검사의 대상이
아니고(조용하지 않다), 정상이다.

## 못 잡는 것 (실측했고, 의도적으로 안 고쳤다)

이름 해석은 **정적**이라 실행 시점에야 정해지는 참조는 원리적으로 닿지 않는다. 아래는 전부
합성 재현으로 누락을 확인했지만, 잡으려면 데이터플로 분석이 필요하고 실제 테스트 파일에서
나올 일이 거의 없어 한계로 남긴다:

    run_suite = unittest.main; run_suite()      # import 후 재바인딩
    getattr(unittest, "main")()                 # 속성 동적 조회
    importlib.import_module("unittest").main()  # 동적 import
    sys.modules["unittest"].main()              # 모듈 테이블 경유
    def _run(): unittest.main()  → _run()       # 래퍼 함수 경유
    load_tests / TextTestRunner / 메타클래스 팩토리

또 `unittest.main(defaultTest="X")`는 저자가 **일부만 돌리겠다고 명시한** 것이라 잡지 않는다.
"""
import ast
import pathlib
import unittest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────
# 이름 해석 — 하드코딩하지 않고 **각 파일의 import를 읽어** 실제 바인딩을 만든다.
# `import unittest as ut` / `from unittest import main as run` / `import unittest.mock`
# / `from unittest import *` 가 모두 같은 함수를 가리킨다(전부 실측으로 확인).
# ─────────────────────────────────────────────────────────────────────────
_SUITE_ORIGINS = {
    ("unittest", "main"),
    ("unittest", "TestProgram"),   # `unittest.main is unittest.main.TestProgram`
    ("unittest.main", "main"),
}
_EXIT_ORIGINS = {("sys", "exit"), ("os", "_exit")}
_STAR_EXPORTS = {"unittest": {("main",), ("TestProgram",)}}

# `__name__`이 이 값 중 하나일 때만 참인 조건은 **보호 구간**이다.
# `__mp_main__`은 multiprocessing spawn 자식 프로세스의 이름이다.
_MAIN_LIKE = frozenset({"__main__", "__mp_main__"})


def _binds_top_level_name(tree: ast.Module, name: str) -> bool:
    """모듈이 이 이름을 top-level에서 스스로 묶는가 (builtin 가림 감지용)."""
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
    return False


def _resolve_runner_names(tree: ast.Module) -> tuple[set, set]:
    """(스위트 실행 이름, terminal 이름) — 이 모듈의 import를 반영해 해석한다."""
    suite: set = set()
    terminal: set = set()
    # builtin `exit` — 단, 모듈이 같은 이름을 스스로 묶으면 그건 다른 함수다.
    # (실측: `from helpers import exit`가 정상 파일을 위반으로 만들었다.)
    if not _binds_top_level_name(tree, "exit"):
        terminal.add(("exit",))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b`는 `a`를 묶고, `import a.b as c`는 `c`를 **서브모듈**에 묶는다.
                bound = alias.asname or alias.name.split(".")[0]
                origin = alias.name if alias.asname else alias.name.split(".")[0]
                for mod, func in _SUITE_ORIGINS:
                    if origin == mod:
                        suite.add((bound, func))
                for mod, func in _EXIT_ORIGINS:
                    if origin == mod:
                        terminal.add((bound, func))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    suite |= _STAR_EXPORTS.get(node.module, set())
                    continue
                local = alias.asname or alias.name
                if (node.module, alias.name) in _SUITE_ORIGINS:
                    suite.add((local,))
                if (node.module, alias.name) in _EXIT_ORIGINS:
                    terminal.add((local,))
    return suite, terminal | suite


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


# ─────────────────────────────────────────────────────────────────────────
# guard 판정
# ─────────────────────────────────────────────────────────────────────────
def _mentions_main_check(expr: ast.expr) -> bool:
    nodes = list(ast.walk(expr))
    return any(isinstance(n, ast.Name) and n.id == "__name__" for n in nodes) and any(
        isinstance(n, ast.Constant) and n.value in _MAIN_LIKE for n in nodes
    )


def _is_inverted_main_check(expr: ast.expr) -> bool:
    """`!=` / `not in` / `not (...)` 처럼 **의미가 뒤집힌** 형태인가."""
    if isinstance(expr, ast.BoolOp):
        return any(_is_inverted_main_check(v) for v in expr.values)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _mentions_main_check(expr.operand)
    if isinstance(expr, ast.Compare):
        return _mentions_main_check(expr) and any(
            isinstance(op, (ast.NotEq, ast.NotIn)) for op in expr.ops
        )
    return False


def _is_protective_condition(expr: ast.expr) -> bool:
    """`__name__`이 main-like일 때만 참임이 **구조적으로 확인되는** 조건인가."""
    if isinstance(expr, ast.BoolOp):
        if isinstance(expr.op, ast.And):
            return any(_is_protective_condition(v) for v in expr.values)
        return all(_is_protective_condition(v) for v in expr.values)
    if not isinstance(expr, ast.Compare) or len(expr.ops) != 1:
        return False
    op, left, right = expr.ops[0], expr.left, expr.comparators[0]
    if isinstance(op, ast.Eq):
        return any(
            isinstance(name, ast.Name)
            and name.id == "__name__"
            and isinstance(const, ast.Constant)
            and const.value in _MAIN_LIKE
            for name, const in ((left, right), (right, left))
        )
    if isinstance(op, ast.In):
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            return False
        if not isinstance(right, (ast.Tuple, ast.List, ast.Set)) or not right.elts:
            return False
        return all(
            isinstance(e, ast.Constant) and e.value in _MAIN_LIKE for e in right.elts
        )
    return False


def _is_main_guard(node: ast.stmt) -> bool:
    """이 `if`가 직접 실행에서만 들어가는 **보호 구간**인가.

    판정은 비대칭이다. 놓치면 안 되는 건 **의미가 뒤집힌 guard**이고, 안전한 철자를 잘못
    잡으면 멀쩡한 CI가 깨지기 때문이다:

      1. 뒤집힌 형태(`!=` / `not in` / `not (...)`)는 **무조건 보호 아님**.
         실측: `if __name__ != "__main__": unittest.main(...)`은 직접 실행에서 아무것도 안
         돌리고(출력 없음, exit 0) import 시 스위트를 돌린다.
      2. 구조적으로 보호가 확인되는 형태(`==`, main-like만 담은 `in`, 이들의 `and`/`or`)는 보호.
         `if __name__ in ("__main__", "__mp_main__")`(multiprocessing)과
         `if __name__ == "__main__" and not os.environ.get("SKIP")`가 여기 해당한다.
      3. 그 외인데 `__name__`과 main-like 상수를 함께 언급하면 **보호로 관용**한다.
         인식 못 한 철자를 위반으로 몰면 안전한 파일이 CI를 깨뜨린다(실측 7건).
    """
    if not isinstance(node, ast.If):
        return False
    if _is_inverted_main_check(node.test):
        return False
    return _is_protective_condition(node.test) or _mentions_main_check(node.test)


# ─────────────────────────────────────────────────────────────────────────
# 실행 보장 — "복합문"과 "조건부 실행"은 다르다
# ─────────────────────────────────────────────────────────────────────────
# `with`/`try`의 **본문**과 `finally`는 반드시 실행된다. 이걸 조건부로 묶으면
# `with warnings.catch_warnings(): unittest.main()` 을 안전하다고 오판한다(실측 Ran 1/3).
_CONDITIONAL_STMTS = (ast.If, ast.For, ast.While, ast.AsyncFor, ast.Match)
_ALWAYS_RUN_BODY = (ast.With, ast.AsyncWith, ast.Try)


def _unconditional_statements(stmts):
    """실행이 보장되는 문장만 (조건부 분기·예외 처리기는 제외)."""
    for stmt in stmts:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # 정의는 호출이 아니다
        if isinstance(stmt, ast.Try):
            yield from _unconditional_statements(stmt.body)
            yield from _unconditional_statements(stmt.finalbody)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            yield from _unconditional_statements(stmt.body)
        elif isinstance(stmt, _CONDITIONAL_STMTS):
            continue
        else:
            yield stmt


def _is_terminal_guard(guard: ast.If, terminal: set) -> bool:
    """이 guard가 실행되면 뒤의 정의가 스위트에 못 들어가는가.

    ⚠️ 실행이 **보장되는** 문장만 본다. 도달하지 않는 분기의 호출까지 세면 아래 같은
    정상 코드가 오탐된다(실측: 정의 3 전부 실행되는데 위반 판정):

        if __name__ == "__main__":
            try:
                import json          # 항상 성공 → exit 분기는 실행되지 않는다
            except ImportError:
                sys.exit("needs json")
    """
    for stmt in _unconditional_statements(guard.body):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _dotted_call_name(node) in terminal:
                return True
    return False


def _outermost_definitions_after(stmts, anchor_lineno: int, out: list[str]) -> None:
    """앵커 뒤의 **가장 바깥** 클래스/함수 정의를 모은다 (중첩 포함, 메서드는 제외).

    ⚠️ `tree.body`만 훑으면 안 된다 — 뒤에 덧붙인 클래스를 `if`로 감싸면 `tree.body`의
    원소가 `ClassDef`가 아니라 `If`라서 보이지 않는다(실측 Ran 1/3).
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


def _terminal_guards(tree: ast.Module, terminal: set) -> list[ast.If]:
    """terminal guard 전부 (등장 순서). 중첩된 것도 찾는다.

    ⚠️ `tree.body`만 보면 `_scan_for_unguarded`(재귀)와 **모순**된다 — 같은 노드가 한쪽에선
    guard가 아니고 다른 쪽에선 guard가 되어 어느 검사에도 안 걸리는 사각이 생긴다(실측 5건).
    """
    found: list[ast.If] = []

    def walk(stmts) -> None:
        for node in stmts:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # 함수 안의 guard는 import 시 실행되지 않는다
            if _is_main_guard(node) and _is_terminal_guard(node, terminal):
                found.append(node)
                continue
            for field in ("body", "orelse", "finalbody"):
                branch = getattr(node, field, None)
                if isinstance(branch, list):
                    walk(branch)
            for handler in getattr(node, "handlers", None) or []:
                walk(handler.body)

    walk(tree.body)
    found.sort(key=lambda n: n.lineno)
    return found


def _definitions_after_main_guard(source: str) -> list[str]:
    """**첫** terminal guard 뒤에 남아 있는 것 — 이름을 알 수 있으면 이름, 아니면 종류.

    ⚠️ 앵커는 **첫** guard다(마지막이 아니다). 마지막을 앵커로 삼으면 "guard → 클래스 →
    guard" 배치에서 `[]`가 되어 결함을 그대로 통과시킨다(실측 정의 4 / `Ran 2` + `OK`).

    ⚠️ 판정 대상은 "정의"가 아니라 **문장 전부**다. 정의만 보면
    `TestHost.test_x = lambda self: None`(`ast.Assign`)을 놓친다(실측 Ran 1/3).
    """
    tree = ast.parse(source)
    guards = _terminal_guards(tree, _resolve_runner_names(tree)[1])
    if not guards:
        return []
    anchor = guards[0]
    offenders: list[str] = []
    # guard 자신의 `else` 가지는 import 시에만 실행된다 — 여기 놓인 정의는 직접 실행에서 누락된다.
    _outermost_definitions_after(anchor.orelse, anchor.lineno, offenders)
    for node in tree.body:
        if node.lineno <= anchor.lineno:
            continue
        named: list[str] = []
        _outermost_definitions_after([node], anchor.lineno, named)
        offenders.extend(named or [f"{type(node).__name__} at line {node.lineno}"])
    return offenders


def _unguarded_suite_runners(source: str) -> list[int]:
    """guard **밖**에서 스위트를 돌리는 module-level 호출의 줄 번호.

    guard 없이 `unittest.main(module=__name__, exit=False)`를 파일 중간에 두면 pytest가
    import하는 것만으로 스위트가 돌고, 그 아래 정의는 직접 실행에서 누락된다(실측 Ran 1/3).
    """
    tree = ast.parse(source)
    suite, _ = _resolve_runner_names(tree)
    lines: list[int] = []
    _scan_for_unguarded(tree.body, False, suite, lines)
    return sorted(lines)


def _scan_for_unguarded(stmts, in_guard: bool, suite: set, out: list[int]) -> None:
    """진짜 guard 안이 아닌 곳의 스위트 실행 호출을 모은다.

    ⚠️ 조건부라고 무조건 넘기면 안 된다 — `if __name__ != "__main__":` 안의 호출은 import
    시점에 **실제로 실행된다**(실측). 반대로 guard의 `else` 가지는 보호되지 않는다
    (실측: else는 import 시 `Ran 1 test`, 직접 실행은 0개).
    """
    for node in stmts:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # 메서드 본문은 import 시점에 실행되지 않는다
        if isinstance(node, _CONDITIONAL_STMTS + _ALWAYS_RUN_BODY):
            body_guard = in_guard or _is_main_guard(node)
            for field in ("body", "orelse", "finalbody"):
                branch = getattr(node, field, None)
                if isinstance(branch, list):
                    _scan_for_unguarded(
                        branch, body_guard if field == "body" else in_guard, suite, out
                    )
            for handler in getattr(node, "handlers", None) or []:
                _scan_for_unguarded(handler.body, in_guard, suite, out)
            continue
        if in_guard:
            continue
        for sub_node in ast.walk(node):
            if isinstance(sub_node, ast.Call) and _dotted_call_name(sub_node) in suite:
                out.append(node.lineno)
                break


def _extra_terminal_guards(source: str) -> int:
    """첫 terminal guard 이후의 추가 terminal guard 개수 (0이어야 한다).

    두 번째 guard는 최선의 경우에도 **죽은 코드**이고, 최악의 경우 앞 guard 뒤에 붙은
    정의를 "끝에 guard가 있으니 괜찮다"고 오독하게 만든다 — 실제로 그 배치가 이 검사기의
    구 버전을 통과시켰다.
    """
    tree = ast.parse(source)
    return max(0, len(_terminal_guards(tree, _resolve_runner_names(tree)[1])) - 1)


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

    def test_negated_guard_is_not_a_guard(self):
        """⛔ 회귀 잠금 — `!=` 오타. 직접 실행은 **아무것도 안 돌리고**(출력 없음, exit 0)
        import 시 스위트가 돈다. 구 문자열 매칭은 이걸 정상 guard로 봤다."""
        src = (
            "import unittest\n"
            "class TestOne(unittest.TestCase):\n    def test_a(self):\n        pass\n"
            'if __name__ != "__main__":\n    unittest.main(exit=False)\n'
        )
        self.assertEqual(_unguarded_suite_runners(src), [6])
        self.assertEqual(_terminal_guards(ast.parse(src), _resolve_runner_names(ast.parse(src))[1]), [])

    def test_reversed_operand_order_is_still_a_guard(self):
        """`"__main__" == __name__`도 정상 guard다 — 잡으면 오탐."""
        src = 'import unittest\nif "__main__" == __name__:\n    unittest.main()\n'
        self.assertEqual(_unguarded_suite_runners(src), [])
        self.assertEqual(len(_terminal_guards(ast.parse(src), _resolve_runner_names(ast.parse(src))[1])), 1)

    def test_module_alias_is_resolved(self):
        """⛔ 회귀 잠금 — `import unittest as ut`(실측 Ran 1 / 정의 3)."""
        src = (
            "import unittest as ut\n"
            'if __name__ == "__main__":\n    ut.main()\n'
            "\nclass TestAfter(ut.TestCase):\n    pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_function_alias_is_resolved(self):
        """⛔ 회귀 잠금 — `from unittest import main as run`(실측 Ran 1 / 정의 3)."""
        src = (
            "from unittest import main as run\nimport unittest\n"
            'if __name__ == "__main__":\n    run()\n'
            "\nclass TestAfter(unittest.TestCase):\n    pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_guard_else_branch_is_not_protected(self):
        """guard의 `else`는 **import 시 실행된다** — 보호 구간이 아니다.

        실측: 직접 실행은 아무것도 안 돌리고(`Ran` 없음), import 시 `Ran 1 test`.
        (mutation N6 생존으로 발견된 미검증 속성)
        """
        src = (
            "import unittest\n"
            "class TestOne(unittest.TestCase):\n    def test_a(self):\n        pass\n"
            'if __name__ == "__main__":\n    pass\nelse:\n    unittest.main(exit=False)\n'
        )
        self.assertEqual(_unguarded_suite_runners(src), [8])

    def test_unrelated_main_import_is_not_a_suite_runner(self):
        """`from mymod import main`은 스위트 실행이 아니다 — 잡으면 오탐."""
        src = "from mymod import main\nmain()\n"
        self.assertEqual(_unguarded_suite_runners(src), [])

    def test_with_wrapped_main_is_terminal(self):
        """⛔ 회귀 잠금 — `with`/`try` 본문은 **반드시 실행된다**(실측 Ran 1 / 정의 3).

        복합문이라고 조건부로 묶으면 `warnings.catch_warnings()`로 감싼 흔한 형태를 놓친다.
        """
        for wrapper in (
            "    with warnings.catch_warnings():\n        unittest.main()\n",
            "    try:\n        unittest.main()\n    finally:\n        pass\n",
        ):
            src = ("import warnings\nimport unittest\n"
                   'if __name__ == "__main__":\n' + wrapper
                   + "\nclass TestAfter(unittest.TestCase):\n    pass\n")
            with self.subTest(wrapper=wrapper.split(chr(10))[0].strip()):
                self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_submodule_import_still_binds_the_package(self):
        """⛔ 회귀 잠금 — `import unittest.mock`은 이름 `unittest`를 묶는다(실측 Ran 1 / 정의 3)."""
        src = ("import unittest.mock\n" + _GUARD
               + "\nclass TestAfter(unittest.TestCase):\n    pass\n")
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_submodule_alias_binds_the_submodule_not_the_package(self):
        """`import unittest.mock as m`은 **서브모듈**을 묶는다 — `m.main`은 없다. 잡으면 오탐."""
        src = ("import unittest.mock as m\nimport unittest\n"
               'if __name__ == "__main__":\n    m.main()\n'
               "\nclass TestAfter(unittest.TestCase):\n    pass\n")
        self.assertEqual(_definitions_after_main_guard(src), [])

    def test_star_import_binds_main(self):
        """⛔ 회귀 잠금 — `from unittest import *`(실측 Ran 1 / 정의 3)."""
        src = ("from unittest import *\n"
               'if __name__ == "__main__":\n    main()\n'
               "\nclass TestAfter(TestCase):\n    pass\n")
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

    def test_shadowed_exit_is_not_terminal(self):
        """⛔ 오탐 잠금 — 모듈이 `exit`를 스스로 묶으면 builtin이 아니다(실측 Ran 2 / 정의 2 = 안전)."""
        src = ("import unittest\nfrom helpers import exit\n"
               'if __name__ == "__main__":\n    exit("banner")\n'
               "\nclass TestTwo(unittest.TestCase):\n    pass\n" + _GUARD)
        self.assertEqual(_definitions_after_main_guard(src), [])
        self.assertEqual(_extra_terminal_guards(src), 0)

    def test_compound_and_tuple_guards_are_protective(self):
        """⛔ 오탐 잠금 — 정상 변형을 위반으로 몰면 안전한 파일이 CI를 깨뜨린다."""
        for cond in ('__name__ == "__main__" and not os.environ.get("SKIP")',
                     '__name__ in ("__main__", "__mp_main__")',
                     '"__main__" == __name__'):
            src = f"import os\nimport unittest\nif {cond}:\n    unittest.main()\n"
            with self.subTest(cond=cond):
                self.assertEqual(_unguarded_suite_runners(src), [])

    def test_unrecognized_but_safe_guard_spellings_are_tolerated(self):
        """⛔ 오탐 잠금 — 구조적으로 못 알아본 철자는 **보호로 관용**한다.

        둘 다 실측 안전(직접 `Ran 2 tests` == 정의 2, import 시 실행 0). 관용 경로가 없으면
        멀쩡한 파일이 위반으로 잡혀 CI가 깨진다. (mutation P6 생존으로 발견된 미검증 속성)
        """
        for cond in ('__name__ == "__main__" == __name__', '__name__.endswith("__main__")'):
            src = f"import unittest\nif {cond}:\n    unittest.main()\n"
            with self.subTest(cond=cond):
                self.assertEqual(_unguarded_suite_runners(src), [])

    def test_inverted_guard_survives_boolop(self):
        """`and`로 감싼 부정형도 보호가 아니다."""
        src = ("import unittest\n"
               'if __name__ != "__main__" and True:\n    unittest.main(exit=False)\n')
        self.assertEqual(_unguarded_suite_runners(src), [3])

    def test_definitions_in_guard_else_branch_are_caught(self):
        """⛔ 회귀 잠금 — guard의 `else`에 놓인 클래스는 직접 실행에서 정의되지 않는다
        (실측: 직접 `Ran 1 test` / 정의 2)."""
        src = ("import unittest\n"
               'if __name__ == "__main__":\n    unittest.main()\n'
               "else:\n    class TestOnlyOnImport(unittest.TestCase):\n        pass\n")
        self.assertEqual(_definitions_after_main_guard(src), ["TestOnlyOnImport"])

    def test_nested_guard_is_seen_by_all_predicates(self):
        """`_terminal_guards`와 `_scan_for_unguarded`가 같은 노드를 같게 봐야 한다.

        한쪽만 재귀하면 "guard가 아니면서 동시에 guard인" 사각이 생겨 어느 검사에도 안 걸린다.
        """
        src = ("import unittest\n"
               "try:\n" + "".join("    " + l + "\n" for l in _GUARD.splitlines())
               + "except Exception:\n    pass\n"
               "\nclass TestAfter(unittest.TestCase):\n    pass\n")
        self.assertEqual(_definitions_after_main_guard(src), ["TestAfter"])

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
