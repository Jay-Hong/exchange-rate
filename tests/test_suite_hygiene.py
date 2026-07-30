"""테스트 스위트 위생 — main guard 뒤에 정의된 테스트를 막는다.

## 무엇을 막나

`if __name__ == "__main__": unittest.main()` **뒤에** 테스트 클래스를 추가하면, 그 파일을 직접
실행할 때 그 클래스는 수집조차 되지 않는다 — `unittest.main()`은 **호출 시점의** 모듈
네임스페이스에서 테스트를 발견하기 때문이다(아직 정의되지 않은 뒤쪽 클래스는 대상이 아니다).
프로세스를 죽이지 않는 `unittest.main(exit=False)`도 같은 이유로 위험하다.

실측(2026-07-30): `tests/test_revenuecat_provider.py`는 guard만 파일 끝으로 옮기자 직접 실행
수집이 **11 → 20**으로 회복됐다(pytest 20과 일치). guard 뒤 4개 클래스가 사라져 있었다.

## 무엇을 막지 못하나 (정직한 범위)

- **CI에는 영향이 없다.** pytest는 모듈을 import만 하므로 guard 블록이 실행되지 않고 모든
  클래스가 수집된다. 즉 이 검사는 *정확성*이 아니라 **직접 실행의 일관성**을 지킨다.
- **직접 실행은 공식 경로가 아니다.** 리포 루트에서 `python3 tests/foo.py`는 `app`을 못 찾아
  `ModuleNotFoundError`로 죽고(`PYTHONPATH=.` 필요), `conftest.py`가 pytest 전용이라 그 스텁에
  의존하는 파일은 애초에 직접 실행이 불가능하다 — 실측: `tests/test_comparison_api.py`는
  guard 이동 전후 모두 21이고 pytest는 25다. 그 4개 격차는 guard가 아니라 스텁 부재로 한
  클래스가 `setUpClass`에서 죽는 것이다. **"격차 = guard 탓"으로 뭉치면 원인 오진이다.**

그래서 이 검사는 **guard 뒤 정의의 존재만** 본다 — 그것이 유일하게 guard에 귀속되는 성질이다.

## 왜 이렇게 작나 (2026-07-30 축소)

구 버전은 673줄 31테스트였고, 그중 24개가 **리포에 존재하지 않는 guard 표기**(별칭 import,
star import, boolop 반전, shadowed `exit`, `with` 래핑, 역순 피연산자 …)를 다루는 검출기 자신의
엣지 케이스였다. 실측: 리포의 157개 guard는 **전부** `if __name__ == "__main__":` 단일 표기이고,
본문은 `unittest.main()` 또는 `unittest.main(verbosity=2)` 둘뿐이다(그 exotic 표기의 유일한
출처가 구 검출기 자신의 fixture였다). 없는 위험에 대비한 복잡도는 오탐만 만든다 —
실제로 구 버전은 폐기된 트랙에서 오탐·누락을 여러 번 고쳤다.

새 표기가 실제로 등장하면 그때 이 검사를 넓힌다. 지금은 **표준 표기만** 본다.
"""
import ast
import pathlib
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _is_standard_guard(node) -> bool:
    """module-level `if __name__ == "__main__":` 이고 본문이 스위트를 돌리는가.

    리포가 쓰는 단일 표기만 인정한다(위 docstring의 실측 근거). 인식하지 못한 표기는
    **guard가 아닌 것으로** 취급 — 그러면 그 뒤 정의를 위반으로 보고하지 않으므로
    모르는 형태에서 오탐을 내지 않는다(놓칠 수는 있고, 그게 이 검사가 감수하는 쪽이다).
    """
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    cmp_node = node.test
    if len(cmp_node.ops) != 1 or not isinstance(cmp_node.ops[0], ast.Eq):
        return False
    names = {
        getattr(cmp_node.left, "id", None),
        getattr(cmp_node.comparators[0], "id", None),
    }
    consts = {
        getattr(cmp_node.left, "value", None),
        getattr(cmp_node.comparators[0], "value", None),
    }
    if "__name__" not in names or "__main__" not in consts:
        return False
    # 본문이 스위트를 돌려야 한다 — 단순 print 등은 뒤 정의를 가리지 않는다.
    return any(
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == "main"
        for stmt in node.body
    )


def find_definitions_after_guard(source: str) -> list:
    """guard **뒤** 모듈 레벨에 정의된 이름 목록. 없으면 빈 리스트."""
    body = ast.parse(source).body
    guard_at = next((i for i, n in enumerate(body) if _is_standard_guard(n)), None)
    if guard_at is None:
        return []
    offenders = []
    for node in body[guard_at + 1:]:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.If, ast.For, ast.While, ast.With)):
            offenders.append(f"<{type(node).__name__} at line {node.lineno}>")
    return offenders


_HAZARD = '''
import unittest


class First(unittest.TestCase):
    def test_a(self):
        pass


if __name__ == "__main__":
    unittest.main()


class Second(unittest.TestCase):
    def test_b(self):
        pass
'''

_SAFE = '''
import unittest


class First(unittest.TestCase):
    def test_a(self):
        pass


class Second(unittest.TestCase):
    def test_b(self):
        pass


if __name__ == "__main__":
    unittest.main()
'''


class TestNoDefinitionsAfterMainGuard(unittest.TestCase):
    def test_detector_flags_a_definition_after_the_guard(self):
        """⛔ 자기검사 — 이것이 없으면 아래 리포 스캔이 "아무것도 못 찾음"으로 공허하게 통과한다."""
        self.assertEqual(find_definitions_after_guard(_HAZARD), ["Second"])

    def test_detector_accepts_a_guard_at_the_end(self):
        """반대 방향 자기검사 — "전부 위반"으로 보고하는 검출기를 배제한다."""
        self.assertEqual(find_definitions_after_guard(_SAFE), [])

    def test_no_test_module_defines_anything_after_its_guard(self):
        offenders = {}
        scanned = 0
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            scanned += 1
            found = find_definitions_after_guard(path.read_text(encoding="utf-8"))
            if found:
                offenders[path.name] = found
        self.assertGreater(scanned, 100, "스캔 대상이 사라졌다 — glob 또는 경로가 깨졌다")
        self.assertEqual(
            offenders, {},
            "`unittest.main()` 뒤의 정의는 직접 실행 시 조용히 누락된다 — guard를 파일 끝으로 옮길 것",
        )


if __name__ == "__main__":
    unittest.main()
