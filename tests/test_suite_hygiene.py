"""테스트 스위트 자체의 위생 — **조용히 누락되는 테스트**를 막는다.

## 왜 필요한가 (실측 사고, 2026-07-28)

`if __name__ == "__main__": unittest.main()` **뒤에** 테스트 클래스를 추가하면,
`unittest.main()`이 `sys.exit()`를 호출하므로 그 뒤의 클래스는 **정의조차 되지 않는다**.
pytest는 모듈을 import만 하므로 전부 수집하지만, `python tests/foo.py`로 직접 돌리면
조용히 일부만 실행되고 **OK가 뜬다**.

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


def _definitions_after_main_guard(source: str) -> list[str]:
    """`__main__` guard **뒤**에 정의된 top-level 클래스/함수 이름."""
    tree = ast.parse(source)
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
    ]
    if not guards:
        return []
    last_guard = max(guards, key=lambda n: n.lineno)
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno > last_guard.lineno
    ]


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

    def test_detector_actually_detects(self):
        """positive control — 검사기가 항상 빈 목록을 돌려주는 형태면 위 단언이 공허하다."""
        planted = (
            "import unittest\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
            "\n"
            "class TestAppendedAfterGuard(unittest.TestCase):\n"
            "    pass\n"
        )
        self.assertEqual(_definitions_after_main_guard(planted), ["TestAppendedAfterGuard"])

    def test_detector_accepts_guard_at_end(self):
        """negative control — 정상 배치를 위반으로 잡으면 안 된다."""
        ok = (
            "import unittest\n"
            "\n"
            "class TestFine(unittest.TestCase):\n"
            "    pass\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
        )
        self.assertEqual(_definitions_after_main_guard(ok), [])


if __name__ == "__main__":
    unittest.main()
