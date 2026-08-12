"""topic 인가 정책표 + 순수 planner (R-GATE-1 S2c).

⛔ 이 파일의 존재 이유는 **불변식이 공허하지 않음을 증명**하는 것이다. 표를 publisher 상수로
   comprehension 하면 `assert_policy_covers_universe()` 의 등식이 항진명제가 되어 통과만 하고
   아무것도 잡지 못한다 — `test_policy_table_is_an_immutable_literal_mapping` 이 그 형태를 거부한다.
"""
import ast
import pathlib
import unittest
from unittest.mock import patch

from app import topic_policy as tp
from app.config import TopicAuthStage

FX = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")
USDT = "usdt:krw"
KRX = "krx:usd-krw-futures"


def _policy_literal_source_errors(source: str):
    """Return structural violations that can make the policy/universe check vacuous."""
    tree = ast.parse(source)

    def binds_policy(node):
        if isinstance(node, ast.AnnAssign):
            return getattr(node.target, "id", None) == "TOPIC_POLICY"
        if isinstance(node, ast.Assign):
            return any(
                isinstance(target, ast.Name) and target.id == "TOPIC_POLICY"
                for target in node.targets
            )
        if isinstance(node, ast.AugAssign):
            return getattr(node.target, "id", None) == "TOPIC_POLICY"
        return False

    errors = []
    top_level_bindings = [node for node in tree.body if binds_policy(node)]
    if len(top_level_bindings) != 1:
        errors.append(
            "TOPIC_POLICY must have exactly one direct top-level binding"
        )

    # Looking only at tree.body misses rebinding under module-level control flow and
    # local/global rebinding hidden in a function. Forbid every additional store by
    # this canonical name; a second policy table is never a supported local concept.
    stores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "TOPIC_POLICY"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    if len(stores) != 1:
        errors.append("TOPIC_POLICY is rebound or deleted outside its declaration")

    mutating_methods = {
        "__delitem__", "__ior__", "__setitem__", "clear", "pop", "popitem",
        "setdefault", "update",
    }
    mutations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "TOPIC_POLICY"
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            mutations.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "TOPIC_POLICY"
            and node.func.attr in mutating_methods
        ):
            mutations.append(node)
    if mutations:
        errors.append("TOPIC_POLICY is mutated after its literal declaration")

    if len(top_level_bindings) != 1:
        return errors
    declaration = top_level_bindings[0]
    if not isinstance(declaration, ast.AnnAssign):
        errors.append("TOPIC_POLICY declaration is not an annotated assignment")
        return errors
    value = declaration.value
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "MappingProxyType"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Dict)
    ):
        errors.append("TOPIC_POLICY declaration is not an immutable literal mapping")
        return errors
    literal = value.args[0]

    literal_keys = []
    for key in literal.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            errors.append(f"TOPIC_POLICY has a non-literal string key: {ast.dump(key)}")
            continue
        literal_keys.append(key.value)
    if len(literal_keys) != len(set(literal_keys)):
        errors.append("TOPIC_POLICY dict literal has duplicate keys")
    return errors


class TestInvariants(unittest.TestCase):
    def test_all_three_hold_as_shipped(self):
        """양성 대조군 — 이게 통과해야 아래 변이들이 '불변식 때문'이라고 말할 수 있다."""
        tp.assert_policy_covers_universe()
        tp.assert_runtime_supported_subset()
        tp.assert_single_entitlement_topic()

    def test_universe_is_flag_independent(self):
        """⛔ universe 가 KRX 배포 flag 에 흔들리면 운영(flag off)에서 KRX 를 못 잡는다."""
        from app import config

        seen = set()
        for effective in (False, True):
            with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", effective):
                seen.add(tp.implemented_topic_universe())
        self.assertEqual(len(seen), 1, f"flag 에 따라 universe 가 갈렸다: {seen}")
        self.assertIn(KRX, seen.pop())

    def test_missing_krx_row_fails_even_when_krx_flag_is_off(self):
        """⛔ **핵심 변이**(codex 지정). 운영은 KRX flag off 라 `supported` 에 KRX 가 없다 —
        런타임 부분집합 검사만으로는 KRX 행이 빠진 표가 **통과한다**. 등식이 그걸 잡아야 한다.
        """
        from app import config

        without_krx = {k: v for k, v in tp.TOPIC_POLICY.items() if k != KRX}
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", False), \
             patch.object(tp, "TOPIC_POLICY", without_krx):
            # 부분집합 검사는 통과한다 — 이게 등식이 필요한 이유다(이 줄이 곧 반례다).
            tp.assert_runtime_supported_subset()
            with self.assertRaises(RuntimeError) as ctx:
                tp.assert_policy_covers_universe()
        self.assertIn(KRX, str(ctx.exception))

    def test_universe_reads_publishers_live(self):
        """universe 가 publisher 를 **호출 시점에** 읽는지.

        ⚠️ 이것만으로는 등식의 비공허성을 증명하지 못한다 — 표를 module-level comprehension 으로
           써도 그건 **import 시 1회** 평가라 여기서 patch 해도 재계산되지 않는다(실측). 그래서
           `test_policy_table_is_an_immutable_literal_mapping` 이 따로 필요하다.
        """
        from app import fx_topic_publisher

        extra = dict(fx_topic_publisher.FX_TOPICS)
        extra["gbp-krw"] = "fx:gbp-krw"          # publisher 에만 추가
        with patch.object(fx_topic_publisher, "FX_TOPICS", extra):
            with self.assertRaises(RuntimeError) as ctx:
                tp.assert_policy_covers_universe()
        self.assertIn("fx:gbp-krw", str(ctx.exception))

    def test_policy_table_is_an_immutable_literal_mapping(self):
        """⛔ **여기서 등식의 비공허성이 지켜진다.** 표를 publisher 상수로 comprehension 하면
        표와 universe 가 같은 소스에서 나와 `==` 가 항진명제가 된다 — 그러면 통과만 하고
        아무 drift 도 잡지 못한다. 런타임 검사로는 잡을 수 없어(import 시 1회 평가) 구조로 잠근다.
        """
        self.assertEqual(
            _policy_literal_source_errors(pathlib.Path(tp.__file__).read_text()),
            [],
            "TOPIC_POLICY 는 리터럴로 한 번만 선언되고 이후 재대입·변이되지 않아야 한다",
        )

    def test_policy_table_is_runtime_immutable(self):
        """별칭을 통한 변이는 AST 이름 검사로 완전히 막을 수 없으므로 객체 자체도 불변이다."""
        alias = tp.TOPIC_POLICY
        with self.assertRaises(TypeError):
            alias["fx:gbp-krw"] = tp.AuthorizationClass.PREMIUM_ONLY

    def test_literal_guard_rejects_nested_rebinding_and_direct_mutation(self):
        """tree.body-only 검사는 조건부 재대입과 dict 변이를 놓쳐 독립성이 다시 공허해진다."""
        declaration = 'TOPIC_POLICY: dict = MappingProxyType({"a": 1})\n'
        counterexamples = {
            "conditional rebind": declaration + "if True:\n    TOPIC_POLICY = derived()\n",
            "subscript mutation": declaration + 'TOPIC_POLICY["b"] = 2\n',
            "method mutation": declaration + 'TOPIC_POLICY.update({"b": 2})\n',
        }
        for label, source in counterexamples.items():
            with self.subTest(label=label):
                self.assertTrue(
                    _policy_literal_source_errors(source),
                    f"구조 가드가 {label}을 허용했다",
                )

    def test_supported_subset_catches_unmapped_supported_topic(self):
        with patch.object(tp, "TOPIC_POLICY", {KRX: tp.AuthorizationClass.PREMIUM_AND_ENTITLEMENT}):
            with self.assertRaises(RuntimeError) as ctx:
                tp.assert_runtime_supported_subset()
        self.assertIn("usdt:krw", str(ctx.exception))

    def test_invalid_policy_value_fails_the_invariant_and_planner(self):
        """⛔ key coverage 만 맞아도 값이 잘못되면 유효한 정책표가 아니다.

        broad fallback 이 있으면 미지 값을 final stage 의 PREMIUM_ONLY 로 조용히 승인한다.
        """
        malformed = dict(tp.TOPIC_POLICY)
        malformed[USDT] = object()
        with patch.object(tp, "TOPIC_POLICY", malformed):
            with self.assertRaises(RuntimeError):
                tp.assert_policy_covers_universe()
            with self.assertRaises(RuntimeError):
                tp.entitlement_gated_topics()
            with self.assertRaises(ValueError):
                tp.plan_authenticated(
                    [USDT],
                    stage=TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM,
                    uid="u1",
                )

    def test_future_enum_member_is_not_silently_classified(self):
        """⛔ **`else: raise` 가 실제로 무언가를 한다는 증거.**

        새 `AuthorizationClass` member 는 `_class_of` 의 `isinstance` 를 **통과한다** — 진짜
        member 이기 때문이다. broad fallback(`elif stage is ENFORCE: ... else: identity_only`)이면
        그 member 가 조용히 premium-only 나 identity-only 로 분류된다. 명시 분기 + `else: raise`
        만이 막는다. member 를 런타임에 못 늘리므로 enum 자체를 확장해 시뮬레이션한다.
        """
        import enum

        extended = enum.Enum("AuthorizationClass", {
            "PREMIUM_ONLY": "premium_only",
            "PREMIUM_AND_ENTITLEMENT": "premium_and_entitlement",
            "FUTURE_CLASS": "future_class",
        })
        with patch.object(tp, "AuthorizationClass", extended), \
             patch.object(tp, "TOPIC_POLICY", {"fx:usd-krw": extended.FUTURE_CLASS}):
            for stage in TopicAuthStage:
                with self.subTest(stage=stage):
                    with self.assertRaises(ValueError):
                        tp.plan_authenticated(["fx:usd-krw"], stage=stage, uid="u1")

    def test_second_entitlement_topic_is_rejected(self):
        """⛔ REST 가시성 함수가 KRX 를 하드코딩으로 걸러내므로, 두 번째 gated 상품은
        evaluator·가시성 함수를 함께 고치기 전에는 존재하면 안 된다."""
        two = dict(tp.TOPIC_POLICY)
        two[USDT] = tp.AuthorizationClass.PREMIUM_AND_ENTITLEMENT
        with patch.object(tp, "TOPIC_POLICY", two):
            with self.assertRaises(RuntimeError):
                tp.assert_single_entitlement_topic()

    def test_entitlement_set_derives_from_the_table(self):
        self.assertEqual(tp.entitlement_gated_topics(), frozenset({KRX}))

    def test_dxy_row_is_absent_until_its_publisher_lands(self):
        """⚠️ ADR-040 — 소비자(publisher) 없이 정책행을 먼저 넣지 않는다.
        DXY 수직 슬라이스가 implemented/runtime-supported 집합을 확장하면 coverage 검사가 정책행도
        강제한다. 새 publisher 파일의 존재를 자동 탐색하는 검사는 아니다.
        """
        self.assertNotIn("dxy:spot", tp.TOPIC_POLICY)
        self.assertNotIn("dxy:spot", tp.implemented_topic_universe())


class TestStartupWiring(unittest.TestCase):
    """⛔ **불변식이 기전이 되는 지점.** 호출자가 없으면 위 assert 들은 산출물일 뿐이다.

    런타임으로는 잡을 수 없다 — `app.main` 은 테스트 수집 시점에 이미 import 돼 있어 그 시점의
    표로 한 번 판정하고 끝난다. 그래서 **호출의 존재와 순서**를 구조로 잠근다.
    """

    def _main_tree(self):
        import ast
        import pathlib as _pl

        return ast.parse((_pl.Path(tp.__file__).parent / "main.py").read_text())

    def test_main_validates_policy_before_creating_the_manager(self):
        import ast

        tree = self._main_tree()
        validation_calls = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "topic_policy"
                and func.attr == "assert_topic_policy_invariants"
            ):
                validation_calls.append(node)
        self.assertEqual(
            len(validation_calls), 1,
            "main.py 는 assert_topic_policy_invariants() 를 top-level 에서 정확히 한 번 불러야 한다 — "
            "dead function 안의 호출은 기동 검증이 아니다",
        )
        validate_line = validation_calls[0].lineno
        manager_bindings = []
        for node in tree.body:                       # top-level `manager = ConnectionManager(...)`
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "manager" for t in node.targets)):
                manager_bindings.append(node)
        self.assertEqual(
            len(manager_bindings), 1,
            "main.py 의 top-level manager 대입은 정확히 하나여야 한다 — 검증 전 임시 manager를 "
            "만든 뒤 다시 대입하면 순서 검사가 공허해진다",
        )
        manager_binding = manager_bindings[0]
        self.assertIsInstance(manager_binding.value, ast.Call)
        self.assertIsInstance(manager_binding.value.func, ast.Name)
        self.assertEqual(manager_binding.value.func.id, "ConnectionManager")
        self.assertLess(
            validate_line, manager_binding.lineno,
            "정책 검증이 manager 생성보다 **뒤**다 — 잘못된 표로 rollout 객체가 먼저 만들어진다")


class TestPlanAnonymous(unittest.TestCase):
    def _plan(self, stage, topics=(FX[0], USDT)):
        return tp.plan_anonymous(list(topics), stage=stage, fx_topics=frozenset(FX))

    def test_compatibility_passes_everything(self):
        self.assertEqual(self._plan(TopicAuthStage.COMPATIBILITY), [FX[0], USDT])

    def test_reject_anonymous_fx_removes_only_fx(self):
        self.assertEqual(self._plan(TopicAuthStage.REJECT_ANONYMOUS_FX), [USDT])

    def test_enforce_denies_everything(self):
        self.assertEqual(self._plan(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM), [])

    def test_order_and_duplicates_preserved(self):
        got = tp.plan_anonymous([USDT, USDT, FX[0]],
                                stage=TopicAuthStage.REJECT_ANONYMOUS_FX,
                                fx_topics=frozenset(FX))
        self.assertEqual(got, [USDT, USDT])

    def test_fx_scope_rejects_string_iterable_instead_of_failing_open(self):
        """문자열을 collection으로 받으면 문자 집합이 되어 실제 FX가 제거되지 않는다."""
        with self.assertRaises(TypeError):
            tp.plan_anonymous(
                [FX[0]],
                stage=TopicAuthStage.REJECT_ANONYMOUS_FX,
                fx_topics=FX[0],
            )
        with self.assertRaises(ValueError):
            tp.plan_anonymous(
                [FX[0]],
                stage=TopicAuthStage.REJECT_ANONYMOUS_FX,
                fx_topics=frozenset(FX[0]),
            )

    def test_unknown_stage_raises_instead_of_passing_through(self):
        """⛔ 삼키고 원본을 돌려주면 fail-open 이다."""
        with self.assertRaises(ValueError):
            tp.plan_anonymous([USDT], stage="enforce", fx_topics=frozenset(FX))


class TestPlanAuthenticated(unittest.TestCase):
    def _plan(self, stage, topics=(FX[0], USDT, KRX)):
        return tp.plan_authenticated(list(topics), stage=stage, uid="u1")

    def test_compatibility_keeps_non_gated_as_identity_only(self):
        p = self._plan(TopicAuthStage.COMPATIBILITY)
        self.assertEqual(p.identity_only, (FX[0], USDT))
        self.assertEqual(p.premium_only, ())
        self.assertEqual(p.premium_and_entitlement, (KRX,))

    def test_reject_anonymous_fx_does_not_change_the_identified_axis(self):
        """⛔ 두 축은 독립이다 — 익명 FX 는 거부되지만 식별 FX 는 여전히 identity-only.
        한쪽에서 다른 쪽을 파생할 수 없다는 근거."""
        self.assertEqual(
            self._plan(TopicAuthStage.REJECT_ANONYMOUS_FX).identity_only, (FX[0], USDT))

    def test_enforce_moves_non_gated_to_premium_only(self):
        p = self._plan(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)
        self.assertEqual(p.identity_only, ())
        self.assertEqual(p.premium_only, (FX[0], USDT))
        self.assertEqual(p.premium_and_entitlement, (KRX,))

    def test_krx_is_full_gated_in_every_stage(self):
        for stage in TopicAuthStage:
            self.assertEqual(
                tp.plan_authenticated([KRX], stage=stage, uid="u1").premium_and_entitlement,
                (KRX,), f"{stage} 에서 KRX 가 full gated 가 아니다")

    def test_unmapped_topic_raises(self):
        with self.assertRaises(ValueError):
            tp.plan_authenticated(["dxy:spot"],
                                  stage=TopicAuthStage.COMPATIBILITY, uid="u1")

    def test_unknown_stage_raises(self):
        with self.assertRaises(ValueError):
            tp.plan_authenticated([USDT], stage="enforce", uid="u1")

    def test_empty_uid_rejected(self):
        for bad in ("", None):
            with self.assertRaises(ValueError):
                tp.plan_authenticated([USDT], stage=TopicAuthStage.COMPATIBILITY, uid=bad)

    def test_partition_is_disjoint_and_covers_input(self):
        for stage in TopicAuthStage:
            p = self._plan(stage)
            groups = (p.identity_only, p.premium_only, p.premium_and_entitlement)
            union = set().union(*(set(g) for g in groups))
            self.assertEqual(union, {FX[0], USDT, KRX})
            self.assertEqual(sum(len(g) for g in groups), len(union))

    def test_duplicate_topic_occurrences_stay_in_their_partition(self):
        """⛔ dispatcher 는 입력 중복을 허용하고 ack 도 요청 순서를 보존한다.

        중복을 partition overlap 으로 오인하면 기존에 유효하던 식별 요청이 ValueError 로 연결을
        끊는다. 같은 topic 의 반복은 같은 분류 안에서 그대로 유지해야 한다.
        """
        topics = [USDT, USDT, KRX, KRX]
        plan = tp.plan_authenticated(
            topics,
            stage=TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM,
            uid="u1",
        )
        self.assertEqual(plan.premium_only, (USDT, USDT))
        self.assertEqual(plan.premium_and_entitlement, (KRX, KRX))

    def test_duplicate_within_one_group_is_not_cross_group_overlap(self):
        plan = tp.AuthorizationPlan(
            uid="u1",
            identity_only=(USDT, USDT),
            premium_only=(),
            premium_and_entitlement=(),
        )
        self.assertEqual(plan.identity_only, (USDT, USDT))

    def test_plan_carries_uid(self):
        self.assertEqual(self._plan(TopicAuthStage.COMPATIBILITY).uid, "u1")

    def test_overlapping_groups_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            tp.AuthorizationPlan(uid="u1", identity_only=(USDT,),
                                 premium_only=(USDT,), premium_and_entitlement=())

    def test_requires_flags(self):
        p = self._plan(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)
        self.assertTrue(p.requires_premium())
        self.assertTrue(p.requires_entitlement())
        only_free = tp.plan_authenticated([USDT], stage=TopicAuthStage.COMPATIBILITY, uid="u1")
        self.assertFalse(only_free.requires_premium())
        self.assertFalse(only_free.requires_entitlement())


if __name__ == "__main__":
    unittest.main(verbosity=2)
