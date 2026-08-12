"""문서가 **현재 코드와 반대되는 사실**을 말하지 않는지 (SEM-018 재발 방지).

## 왜 이 파일이 필요한가

C2 재-baseline(`371c474`)은 인용 locator 를 새 C1 으로 정확히 옮기면서 **그 코드를 서술한 문장은
그대로 뒀다**. 결과가 "옛 의미를 새 SHA 에 재봉인" 이다 — 네 문서가 *"WS 의 FX/USDT 에 premium
강제가 없다"* 를 주장하는데 코드에는 그 강제가 있었다. 게이트 4종(preflight·verify·check-docs·
semantic 테스트)이 **전부 통과**했다: 해시 연쇄는 **내부 정합**만 증명하고, 재도출 절차는 내용
앵커 방식이라 "코드는 새 줄에 있는데 문장이 거짓" 을 구조적으로 탐지할 수 없다.

## ⚠️ 이 tripwire 의 한계 (정직하게)

⛔ **의미 검증이 아니다.** 아래는 **이번에 실제로 틀렸던 주장들**의 재등장만 막는다. 새로운
형태의 의미 drift 는 못 잡는다 — 문구를 바꿔 같은 거짓을 쓰면 통과한다(SEM-010 이 같은 부류의
약점을 이미 보여줬다). 그래서 두 번째 검사를 함께 둔다: **코드에서 사라진 심볼**을 문서가 계속
이름으로 부르고 있으면 실패한다. 그쪽은 문구와 무관하게 성립한다.
"""
import pathlib
import re
import unittest

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
]

# 이 슬라이스가 **제거한** 심볼. 문서가 계속 부르면 그 문단은 옛 코드를 서술하고 있다.
REMOVED_SYMBOLS = ["free_accepted"]


def _texts():
    return [(p, p.read_text()) for p in DOCS + LEDGERS if p.exists()]


class TestDocsDoNotContradictTheCode(unittest.TestCase):
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
