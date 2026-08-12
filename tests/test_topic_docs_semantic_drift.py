"""문서가 **현재 코드와 반대되는 사실**을 말하지 않는지 (SEM-018 재발 방지).

## 왜 이 파일이 필요한가

C2 재-baseline(`371c474`)은 인용 locator 를 새 C1 으로 정확히 옮기면서 **그 코드를 서술한 문장은
그대로 뒀다**. 결과가 "옛 의미를 새 SHA 에 재봉인" 이다 — 네 문서가 *"WS 의 FX/USDT 에 premium
강제가 없다"* 를 주장하는데 코드에는 그 강제가 있었다. 게이트 4종(preflight·verify·check-docs·
semantic 테스트)이 **전부 통과**했다: 해시 연쇄는 **내부 정합**만 증명하고, 재도출 절차는 내용
앵커 방식이라 "코드는 새 줄에 있는데 문장이 거짓" 을 구조적으로 탐지할 수 없다.

## ⚠️ 이 tripwire 의 한계 (정직하게)

R-INV-2의 stage 표는 실제 planner 결과와 대조하므로 그 범위에서는 문구를 바꿔 우회할 수 없다.
그 밖의 검사는 **이번에 실제로 틀렸던 주장들**과 제거된 심볼의 재등장만 막는다. 즉 전체 문서의
의미 검증은 아니다. 새로운 형태의 자연어 drift는 여전히 상호 의미 검토가 찾아야 한다.
"""
import pathlib
import re
import unittest
from unittest.mock import patch

from app import topic_policy
from app.config import TopicAuthStage
from app.fx_topic_publisher import FX_TOPICS as PUBLISHER_FX_TOPICS
from app.krx_topic_publisher import KRX_TOPIC
from app.tether_topic_publisher import TETHER_TOPIC
from app.topic_policy import plan_anonymous, plan_authenticated

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = [
    REPO / "DECISIONS.md",
    REPO / "spec/topic-snapshot-handoff.md",
    REPO / "spec/ios-topic-state-machine.md",
    REPO / "spec/legacy-cutover.md",
    REPO / "spec/publisher-health-slo.md",
    REPO / "spec/revalidation-and-load.md",
    REPO / "spec/topic-only-baseline-facts.md",
]
LEDGERS = [
    REPO / "spec/topic-only-implementation-ledger.json",
    REPO / "spec/topic-only-code-claim-review.json",
]

# ⛔ 이번에 실제로 거짓이 된 주장들. 문구 그대로의 재등장만 막는다.
# ⚠️ **과거형 서술도 걸린다** — 문구 검사는 시제를 모른다. 역사를 남기려면 다른 표현을 써야
#    한다(실측: R-INV-2 의 "…이전에는 … KRX 하나뿐이었고" 가 이 검사에 걸렸다). 그 비용을
#    감수하는 이유는, 시제를 판별하려 들면 검사 자체가 추측이 되기 때문이다.
STALE_CLAIMS = [
    ("premium 강제가 없다", "최종 stage 가 FX/USDT 에 premium 을 강제한다"),
    ("판정 대상은 KRX 하나뿐", "enforce stage 에서는 FX/USDT 도 premium 관측 대상이다"),
    ("premium 강제와 USDT 단계는 잔존", "두 축 모두 0cfe474 에서 구현됐다(운영 미활성일 뿐)"),
    ("premium 축과 USDT 는 잔존", "두 축 모두 0cfe474 에서 구현됐다"),
    ("어느 stage 도 FX/USDT 의 premium 판정을 추가하지", "최종 stage 가 그 판정을 추가한다"),
    ("어느 stage 도 premium 판정을 추가하지", "최종 stage 가 그 판정을 추가한다"),
    ("FX/USDT premium 강제도 없다", "0cfe474 에 강제 경로가 있다"),
]

# 이 슬라이스가 **제거한** 심볼. 문서가 계속 부르면 그 문단은 옛 코드를 서술하고 있다.
REMOVED_SYMBOLS = ["free_accepted"]

FX_TOPICS = frozenset(PUBLISHER_FX_TOPICS.values())
USDT = TETHER_TOPIC
KRX = KRX_TOPIC


def _texts():
    return [(p, p.read_text()) for p in DOCS + LEDGERS if p.exists()]


def _rid_block(path: pathlib.Path, rid: str) -> str:
    text = path.read_text()
    match = re.search(
        rf'<a id="{re.escape(rid.lower())}"></a>.*?<!-- /rid: {re.escape(rid.upper())} -->',
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{path.relative_to(REPO)} 에 {rid} block 이 없다")
    return match.group(0)


def _normalize_cell(value: str) -> str:
    return value.replace("`", "").replace("**", "").strip()


def _documented_stage_matrix() -> dict[str, tuple[str, str, str, str]]:
    block = _rid_block(REPO / "DECISIONS.md", "R-INV-2")
    table = re.search(
        r"\| stage \| 익명 FX \| 익명 USDT \| 식별 FX/USDT \| 식별 KRX \|\n"
        r"\|[-|]+\|\n(?P<rows>(?:\|.*\|\n?)+)",
        block,
    )
    if table is None:
        raise AssertionError("R-INV-2 의 stage 표를 찾지 못했다")

    result = {}
    for line in table.group("rows").splitlines():
        cells = [_normalize_cell(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            raise AssertionError(f"R-INV-2 stage 표의 열 수가 5가 아니다: {line!r}")
        if cells[0] in result:
            raise AssertionError(
                f"R-INV-2 stage 표가 같은 stage 를 두 번 정의한다: {cells[0]!r}"
            )
        result[cells[0]] = tuple(cells[1:])
    return result


def _authorization_label(plan, topic: str) -> str:
    if topic in plan.identity_only:
        return "identity-only"
    if topic in plan.premium_only:
        return "premium-only"
    if topic in plan.premium_and_entitlement:
        return "premium + entitlement"
    raise AssertionError(f"planner 가 {topic!r} 을 어떤 partition 에도 넣지 않았다: {plan!r}")


def _uniform_label(labels, *, stage: TopicAuthStage, column: str) -> str:
    unique = set(labels)
    if len(unique) != 1:
        raise AssertionError(
            f"R-INV-2 의 집계 열 {column!r} 이 {stage.value!r} 에서 topic 별로 갈린다: "
            f"{sorted(unique)}"
        )
    return unique.pop()


def _code_stage_matrix() -> dict[str, tuple[str, str, str, str]]:
    result = {}
    fx_topics = tuple(sorted(FX_TOPICS))
    for stage in TopicAuthStage:
        anonymous = plan_anonymous(
            [*fx_topics, USDT], stage=stage, fx_topics=FX_TOPICS
        )
        authenticated = plan_authenticated(
            [*fx_topics, USDT, KRX], stage=stage, uid="semantic-doc-check"
        )
        result[stage.value] = (
            _uniform_label(
                ("허용" if topic in anonymous else "거부" for topic in fx_topics),
                stage=stage,
                column="익명 FX",
            ),
            "허용" if USDT in anonymous else "거부",
            _uniform_label(
                (
                    _authorization_label(authenticated, topic)
                    for topic in (*fx_topics, USDT)
                ),
                stage=stage,
                column="식별 FX/USDT",
            ),
            _authorization_label(authenticated, KRX),
        )
    return result


class TestDocsDoNotContradictTheCode(unittest.TestCase):
    def test_r_inv_2_stage_table_matches_the_planners(self):
        """문구가 아니라 stage 전수와 네 정책 셀을 실제 planner 결과에 대조한다."""
        self.assertEqual(_documented_stage_matrix(), _code_stage_matrix())

    def test_aggregate_columns_reject_per_topic_policy_divergence(self):
        """집계 열을 USD 하나로 대표하면 JPY/EUR/USDT만 갈리는 회귀가 통과한다."""
        for topic in (*sorted(FX_TOPICS), USDT):
            divergent = dict(topic_policy.TOPIC_POLICY)
            divergent[topic] = topic_policy.AuthorizationClass.PREMIUM_AND_ENTITLEMENT
            with self.subTest(topic=topic), \
                 patch.object(topic_policy, "TOPIC_POLICY", divergent), \
                 self.assertRaises(AssertionError):
                _code_stage_matrix()

    def test_current_operating_stage_is_not_inferred_from_the_code_default(self):
        """코드 기본값은 production 현재값이나 과거 활성화 이력을 증명하지 않는다."""
        forbidden = {
            "운영 현재값": "코드 기본값을 운영값으로 승격했다",
            "운영은 `compatibility`": "production 직접 측정 없이 현재 stage 를 단정했다",
            "운영 stage 는 `compatibility`": "production 직접 측정 없이 현재 stage 를 단정했다",
            "운영은 아직 켜지 않았다": "활성화 이력을 저장소만으로 단정했다",
            "한 번도 켜진 적이 없다": "활성화 이력을 저장소만으로 단정했다",
        }
        hits = [
            f"{path.relative_to(REPO)}: {phrase!r} — {reason}"
            for path, text in _texts()
            for phrase, reason in forbidden.items()
            if phrase in text
        ]
        self.assertEqual(hits, [], "미측정 운영 상태를 단정한다:\n" + "\n".join(hits))

        current_contracts = {
            "R-INV-2": _rid_block(REPO / "DECISIONS.md", "R-INV-2"),
            "R-HAND-11": _rid_block(
                REPO / "spec/topic-snapshot-handoff.md", "R-HAND-11"
            ),
            "R-GATE-1 ledger": (
                REPO / "spec/topic-only-implementation-ledger.json"
            ).read_text(),
        }
        for label, contract in current_contracts.items():
            with self.subTest(contract=label):
                self.assertIn(
                    "직접 측정",
                    contract,
                    f"{label}이 코드 기본값과 production 실측을 분리하지 않는다",
                )

    def test_baseline_names_every_configured_stage(self):
        baseline = (REPO / "spec/topic-only-baseline-facts.md").read_text()
        section = baseline.split("## C. 인가 (현재 구현 상태)", 1)[1].split("## D.", 1)[0]
        for stage in TopicAuthStage:
            with self.subTest(stage=stage.value):
                self.assertIn(f"`{stage.value}`", section)

    def test_stale_claims_do_not_reappear(self):
        hits = [
            f"{p.relative_to(REPO)}: {phrase!r} — {why}"
            for p, t in _texts()
            for phrase, why in STALE_CLAIMS
            if phrase in t
        ]
        self.assertEqual(hits, [], "문서가 현재 코드와 반대되는 주장을 담고 있다:\n" + "\n".join(hits))

    def test_removed_symbols_are_not_still_named_by_docs(self):
        """⛔ 문구와 무관하게 성립하는 검사 — 코드에 없는 이름을 문서가 계속 부르면 실패한다."""
        app = REPO / "app"
        for symbol in REMOVED_SYMBOLS:
            alive = any(
                re.search(rf"\b{re.escape(symbol)}\b", f.read_text())
                for f in app.rglob("*.py")
            )
            self.assertFalse(
                alive, f"{symbol} 이 코드에 다시 생겼다 — REMOVED_SYMBOLS 에서 빼고 이 검사를 재설계할 것")
            named = [
                str(p.relative_to(REPO))
                for p, t in _texts()
                if re.search(rf"\b{re.escape(symbol)}\b", t)
            ]
            self.assertEqual(
                named, [],
                f"코드에 없는 `{symbol}` 을 문서가 여전히 부른다 — 그 문단은 옛 구현을 서술한다: {named}")

    def test_the_tripwire_itself_is_not_vacuous(self):
        """양성 대조군 — 검사 대상 문서가 실제로 읽히고 비어 있지 않은지."""
        texts = _texts()
        self.assertGreaterEqual(len(texts), 8, "검사 대상 문서를 못 읽었다")
        self.assertTrue(all(len(t) > 500 for _, t in texts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
