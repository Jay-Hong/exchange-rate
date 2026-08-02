"""`app/clock.py` — 시간 소스 주입 seam의 **monotonic 축** 완결분.

ADR-039 §8.1 harness 선행 (1). wall 축은 2026-07-26에 `app/subscription.py`로 land했고
(`tests/test_subscription_clock.py`), 이 파일은 A1 lease horizon이 요구하는 `mono` 축과
**정본 모듈 이동**(`app.subscription` → `app.clock`)을 잠근다.

⚠️ **이 파일이 증명하는 것 / 증명하지 않는 것** (귀속을 흐리지 않기 위해 먼저 적는다):

- 증명한다: `mono`가 **기본값 없는 필수 필드**다 / 프로덕션 배선이 `time.monotonic`이다 /
  wall·mono 두 축이 서로 **독립**이다 / `Clock`의 정본 import 경로가 하나다.
- 증명하지 않는다: §8.1 G의 `[server] wall clock 역행에도 strict horizon 불변`.
  harness (1)이 그 행을 닫는 조건을 못 박아 뒀다 — "monotonic 축 도입만으로는 부족하다.
  계산기가 wall을 무시한다는 것만 증명될 뿐이고, `verified_at_monotonic`의 **저장·재사용
  경로**(관측 캐시 + 배선)까지 있어야 실제 불변식이 검증된다."
  → 여기서 축 독립을 단언하더라도 **그 행은 열려 있다**.

`_clock` 헬퍼를 `tests/test_subscription_clock.py`와 공유하지 않는 이유: 이 리포는 test 파일
간 헬퍼 import가 0건이고(공유 자산은 데이터 파일 1건), 158개 중 155개가 `unittest.TestCase`
계열이라 conftest fixture 주입도 성립하지 않는다. 조립부가 둘로 늘어나는 비용은
`app/clock.py` docstring의 "조립부" 목록으로 상쇄한다.
"""
import ast
import math
import pathlib
import time
import unittest
from dataclasses import MISSING, fields
from datetime import datetime, timedelta, timezone

from app.clock import Clock, system_clock

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestMonoIsRequired(unittest.TestCase):
    """§8.1 A2: "`Clock.mono`는 **기본값 없이 필수 주입**한다(wall과 동일).

    기본값을 주면 호출부가 빠뜨려도 실클럭으로 조용히 동작해 테스트에 실시간이 섞인다."
    """

    def _field(self, name):
        return {f.name: f for f in fields(Clock)}[name]

    def test_mono_has_no_default(self):
        """`Clock(mono=time.monotonic)` 같은 기본값 부여 mutation을 잡는 **유일한** 단언.

        `TypeError` 단언(아래)만으로는 부족하다 — 기본값이 생기면 그 예외 자체가 사라져
        "예외가 안 난다"를 관측할 수는 있어도 *왜*인지는 드러나지 않는다. 여기서는
        `default`/`default_factory`가 **둘 다** MISSING임을 직접 본다.
        """
        mono = self._field("mono")
        self.assertIs(mono.default, MISSING, "mono에 기본값이 생겼다 — 실클럭 조용한 fallback")
        self.assertIs(mono.default_factory, MISSING, "mono에 default_factory가 생겼다")

    def test_wall_has_no_default(self):
        """positive control — mono만 검사하는 구현과 구별한다(대칭이 계약)."""
        wall = self._field("wall")
        self.assertIs(wall.default, MISSING)
        self.assertIs(wall.default_factory, MISSING)

    def test_construction_without_mono_raises(self):
        with self.assertRaises(TypeError):
            Clock(wall=lambda: datetime.now(timezone.utc))  # type: ignore[call-arg]

    def test_construction_without_wall_raises(self):
        with self.assertRaises(TypeError):
            Clock(mono=time.monotonic)  # type: ignore[call-arg]


class TestRejectsNonCallable(unittest.TestCase):
    """"**값이 아니라 콜러블을 주입한다**"(harness (1))를 생성 시점에 강제.

    검증이 없으면 `Clock(wall=datetime.now(timezone.utc), ...)` 같은 실수가 **첫 호출
    시점까지** 미뤄진다 — 리포 관례는 Crash Early(`app/atomic_retry.py`의 `__post_init__`
    → `ValueError`).
    """

    def test_non_callable_wall_raises(self):
        with self.assertRaises(ValueError):
            Clock(wall=datetime(2026, 1, 1, tzinfo=timezone.utc), mono=time.monotonic)

    def test_non_callable_mono_raises(self):
        """wall 검사만 남기는 mutation을 잡는다(둘을 따로 본다)."""
        with self.assertRaises(ValueError):
            Clock(wall=lambda: datetime.now(timezone.utc), mono=123.0)

    def test_callable_pair_constructs(self):
        clock = Clock(wall=lambda: datetime.now(timezone.utc), mono=time.monotonic)
        self.assertTrue(callable(clock.wall) and callable(clock.mono))


class TestSystemClockWiring(unittest.TestCase):
    """프로덕션 조립부 — mono가 **monotonic 축**이라는 사실 자체가 계약이다."""

    def test_mono_is_time_monotonic(self):
        """identity 단언. `lambda: time.time()`(wall epoch) 배선 mutation을 잡는다.

        값 비교로는 못 잡는다 — 두 축 모두 "증가하는 float"라 구별되지 않는다.
        """
        self.assertIs(system_clock().mono, time.monotonic)

    def test_mono_is_non_decreasing(self):
        clock = system_clock()
        first = clock.mono()
        second = clock.mono()
        self.assertIsInstance(first, float)
        self.assertGreaterEqual(second, first)

    def test_wall_is_aware_utc(self):
        wall = system_clock().wall()
        self.assertIsNotNone(wall.tzinfo)
        self.assertEqual(wall.utcoffset(), timedelta(0))


class TestAxesAreIndependent(unittest.TestCase):
    """두 축이 **한 소스에서 파생되지 않는다**는 것을 주입 클럭으로 잠근다.

    A1의 상한 증명은 lease deadline과 `authoritative_verified_at`이 **같은 monotonic 축**
    이라는 전제 위에 있다(A2). 축이 섞여 있으면(예: mono가 wall 파생) wall 역행이 horizon을
    늘려 증명이 깨진다.
    """

    def test_wall_can_move_while_mono_frozen(self):
        wall_values = iter([
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 1, tzinfo=timezone.utc),   # 역행 (NTP step / VM restore)
            datetime(2027, 1, 1, tzinfo=timezone.utc),   # 전진
        ])
        clock = Clock(wall=lambda: next(wall_values), mono=lambda: 1000.0)
        observed_years = [clock.wall().year for _ in range(3)]
        self.assertEqual(observed_years, [2026, 2025, 2027], "wall은 역행·전진한다")
        self.assertEqual([clock.mono(), clock.mono(), clock.mono()], [1000.0] * 3)

    def test_mono_can_move_while_wall_frozen(self):
        mono_values = iter([1000.0, 1001.5, 1002.0])
        frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = Clock(wall=lambda: frozen, mono=lambda: next(mono_values))
        self.assertEqual([clock.mono(), clock.mono(), clock.mono()], [1000.0, 1001.5, 1002.0])
        self.assertEqual(clock.wall(), frozen)

    def test_mono_is_not_derived_from_wall(self):
        """wall을 역행시켜도 mono 관측값이 바뀌지 않는다."""
        wall_values = iter([
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 1, tzinfo=timezone.utc),
        ])
        clock = Clock(wall=lambda: next(wall_values), mono=lambda: 555.5)
        before = clock.mono()
        clock.wall()
        clock.wall()
        self.assertEqual(clock.mono(), before)


_BANNED_VIA_SUBSCRIPTION = ("Clock", "system_clock")


def _dotted_name(node):
    """`a.b.c` 형태 Attribute/Name 체인을 문자열로 편다. 그 외에는 None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _scan_subscription_clock_usage(src, filename="<test>"):
    """`app.subscription`을 **경유해** Clock/system_clock에 닿는 모든 형태를 찾는다.

    from-import뿐 아니라 **모듈 별칭 + attribute 접근**까지 본다 — 이 리포 테스트는
    `from app import atomic_watermark as aw` 형태를 7곳 넘게 쓰므로
    `from app import subscription as s; s.Clock`은 현실적인 회귀 형태다.
    """
    tree = ast.parse(src, filename=filename)
    banned = set(_BANNED_VIA_SUBSCRIPTION)
    aliases = set()          # `app.subscription` 모듈 객체에 바인딩된 이름
    violations = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.subscription":
                    aliases.add(alias.asname or "app.subscription")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "app" and node.level == 0:
                for alias in node.names:
                    if alias.name == "subscription":
                        aliases.add(alias.asname or "subscription")
                    elif alias.name in banned:
                        violations.append(f"from app import {alias.name}")
            elif node.module == "app.subscription" and node.level == 0:
                violations += [f"from app.subscription import {a.name}"
                               for a in node.names if a.name in banned]
            elif node.level > 0 and node.module == "subscription":
                # `from .subscription import Clock` — 현재 리포엔 상대 import가 0건이지만
                # 형태 자체는 유효하므로 함께 막는다.
                violations += [f"from .subscription import {a.name}"
                               for a in node.names if a.name in banned]

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            dotted = _dotted_name(node)
            if dotted and dotted.rsplit(".", 1)[0] in aliases:
                violations.append(dotted)

    return violations


class TestCanonicalImportPath(unittest.TestCase):
    """정본은 `app.clock` **하나**다 — `app.subscription` 경유 접근을 AST로 금지.

    `from app.clock import Clock`(사용 목적)은 `app.subscription`에도 module attribute를
    남기므로 `from app.subscription import Clock`이 **여전히 성공한다**. `__all__` 제거는
    `import *`만 막는다 → 경로가 둘로 갈리는 것을 막는 것은 이 trip-wire뿐이다.

    (`app/subscription.py` 자신은 `app.clock`에서 가져와 쓰므로 대상이 아니다 — 그 파일에는
     `Clock`/`system_clock`을 *subscription 경유로* 접근하는 노드가 없다.
     subscription의 기본 클럭을 patch해야 하면 **소비자 네임스페이스**
     `app.subscription.system_clock`을 쓴다 — 리포의 모듈 한정 patch 관례.)
    """

    def test_no_file_reaches_clock_symbols_via_subscription(self):
        offenders = []
        for path in sorted(list((_REPO_ROOT / "app").rglob("*.py"))
                           + list((_REPO_ROOT / "tests").rglob("*.py"))):
            viol = _scan_subscription_clock_usage(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
            if viol:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {viol}")
        self.assertEqual(offenders, [], "Clock 정본은 app.clock — app.subscription 경유 금지")

    def test_scanner_self_arms(self):
        """스캐너가 **실제로 무언가를 잡는지** 심는다(리포의 negative self-arm 관례).

        이게 없으면 스캐너가 항상 `[]`를 돌려주는 회귀가 위 테스트를 green으로 통과시킨다.
        """
        planted = [
            "from app.subscription import Clock\n",
            "from app.subscription import (CACHE_TTL, system_clock)\n",
            "from app.subscription import Clock as C\n",
            "from app import subscription as s\nx = s.Clock\n",
            "from app import subscription\nx = subscription.system_clock()\n",
            "import app.subscription as s\nx = s.system_clock\n",
            "import app.subscription\nx = app.subscription.Clock\n",
            "from app import Clock\n",
            "from .subscription import Clock\n",
        ]
        for src in planted:
            with self.subTest(src=src):
                self.assertTrue(_scan_subscription_clock_usage(src), src)

    def test_scanner_does_not_flag_legitimate_forms(self):
        """positive control — 정본 경로와 무관한 subscription 사용은 통과해야 한다."""
        allowed = [
            "from app.clock import Clock, system_clock\n",
            "from app import subscription\nsubscription._cache.clear()\n",
            "from app.subscription import CACHE_TTL, PremiumStatus\n",
            "import app.clock as c\nx = c.Clock\n",
        ]
        for src in allowed:
            with self.subTest(src=src):
                self.assertEqual(_scan_subscription_clock_usage(src), [], src)


class TestNonFiniteIsNotClockConcern(unittest.TestCase):
    """`Clock`은 값 검증을 하지 않는다는 비-책임을 명시(경계 문서화).

    비유한 시각의 fail-closed 처리는 `app/topic_lease.py`의 소비 지점 책임이다
    (`compute_lease_expiry`는 ValueError, `is_expired`는 만료로 접는다).
    """

    def test_clock_does_not_validate_returned_values(self):
        clock = Clock(wall=lambda: datetime.now(timezone.utc), mono=lambda: math.nan)
        self.assertTrue(math.isnan(clock.mono()))


if __name__ == "__main__":
    unittest.main()
