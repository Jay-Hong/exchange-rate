"""observe_kis_master.py 단위 테스트.

Codex 5가지 필수 케이스 (helper / collect / main flow) + stdout JSONL 무오염 검증.
"""
from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# scripts/ 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app.sources.kis_master import extract_commodity_future_master_observation  # noqa: E402

# observe_kis_master는 app.sources.kis_master에 의존하므로 import 후 사용
from observe_kis_master import collect_observations, main  # noqa: E402


# ---------------------------------------------------------------------------
# extract_commodity_future_master_observation — Codex 필수 케이스
# ---------------------------------------------------------------------------


class TestExtractObservation(unittest.TestCase):
    """fixed-width row → 관찰 dict.

    KIS commodity master row format:
        [0:2]  상품구분/종류
        [2:11] 단축코드 (9자, strip)
        [11:23] 표준코드 (12자, strip)
        [23:55] 한글종목명 (32자, strip)
        [55:]  tail (lstrip 후 [8:9]=mmsc_cls_code, KIS 공식 샘플 기준)
    """

    @staticmethod
    def _make_row(
        short_code: str = "A75605",
        name: str = "미국달러 F 202605",
        mmsc: str = "1",
        base: str = "USD",
    ) -> str:
        """tail format: 8자 filler + 1자 mmsc + base/이름 (lstrip 후 [8:9]=mmsc)."""
        head = "C5"
        std = "KR4A75650007"
        # tail 기본: filler 8자 ("FILLERXX") + mmsc 1자 + base 3자 + 한글명
        # lstrip 후 첫 8자가 base_short, [8]이 mmsc_cls_code (KIS 공식 패턴)
        tail = "FILLERXX" + mmsc + base + "미국달러"
        return f"{head}{short_code:<9}{std:<12}{name:<32}{tail}"

    def test_normal_usd_row_with_mmsc_1(self):
        """tail[8:9] == "1" → mmsc_cls_code="1"."""
        row = self._make_row(mmsc="1")
        obs = extract_commodity_future_master_observation(row)
        self.assertIsNotNone(obs)
        self.assertEqual(obs["short_code"], "A75605")
        self.assertEqual(obs["name"], "미국달러 F 202605")
        self.assertEqual(obs["mmsc_cls_code"], "1")

    def test_tail_short_returns_none_mmsc(self):
        """tail 길이 9 미만 → mmsc_cls_code=None (dict는 반환)."""
        # row[55:] = "ab" (lstrip 후 길이 2 < 9)
        row = ("C5" + f"{'A75605':<9}" + f"{'KR4A75650007':<12}"
               + f"{'미국달러 F 202605':<32}" + "ab")
        obs = extract_commodity_future_master_observation(row)
        self.assertIsNotNone(obs)
        self.assertIsNone(obs["mmsc_cls_code"])

    def test_mmsc_blank_returns_none(self):
        """tail[8:9]가 공백 → mmsc_cls_code=None."""
        row = self._make_row(mmsc=" ")  # 8자 filler + " " + ...
        obs = extract_commodity_future_master_observation(row)
        self.assertIsNotNone(obs)
        self.assertIsNone(obs["mmsc_cls_code"])

    def test_short_row_returns_none(self):
        """row 길이 < 55 → None."""
        row = "short row only"
        self.assertIsNone(extract_commodity_future_master_observation(row))

    def test_empty_short_code_returns_none(self):
        """short_code 빈 칸 → None."""
        row = ("C2" + " " * 9 + "KR4XXXXXXXXX"
               + "미국달러 F 202605".ljust(32) + "FILLERXX1USD")
        self.assertIsNone(extract_commodity_future_master_observation(row))

    def test_mmsc_cls_code_2(self):
        """tail[8:9] == "2" → 차근월."""
        row = self._make_row(short_code="A75606", name="미국달러 F 202606", mmsc="2")
        obs = extract_commodity_future_master_observation(row)
        self.assertEqual(obs["mmsc_cls_code"], "2")


# ---------------------------------------------------------------------------
# collect_observations — Codex 필수 케이스 (USD 필터 / short-code 필터)
# ---------------------------------------------------------------------------


class TestCollectObservations(unittest.TestCase):

    @staticmethod
    def _make_row_str(short_code: str, name: str, mmsc: str) -> str:
        """fixture row — tail = "FILLERXX" + mmsc + "USD..." (lstrip 후 [8]=mmsc)."""
        head = "C5"
        std = "KR4A75650007"  # 12자 hardcoded (테스트 단순화 — 실제 표준코드 매핑 X)  # 임의
        tail = "FILLERXX" + mmsc + "USD미국달러"
        return f"{head}{short_code:<9}{std:<12}{name:<32}{tail}"

    @classmethod
    def _make_raw(cls, rows: list) -> bytes:
        return "\n".join(rows).encode("cp949")

    def test_usd_only_excludes_other_products(self):
        """금 F 202605 같은 비-USD 종목은 collect()에서 제외."""
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
            self._make_row_str("Z00001", "금 F 202605", "1"),
        ]
        raw = self._make_raw(rows)
        observations = collect_observations(raw)
        codes = [o["short_code"] for o in observations]
        self.assertIn("A75605", codes)
        self.assertNotIn("Z00001", codes)

    def test_short_code_filter_multiple(self):
        """--short-code 2개 → 둘 다 포함."""
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
            self._make_row_str("A75606", "미국달러 F 202606", "2"),
            self._make_row_str("A75607", "미국달러 F 202607", "3"),
        ]
        raw = self._make_raw(rows)
        observations = collect_observations(raw, filter_codes={"A75605", "A75606"})
        codes = [o["short_code"] for o in observations]
        self.assertEqual(set(codes), {"A75605", "A75606"})

    def test_contract_month_extracted(self):
        """name "미국달러 F 202605"에서 contract_month=202605."""
        rows = [self._make_row_str("A75605", "미국달러 F 202605", "1")]
        observations = collect_observations(self._make_raw(rows))
        self.assertEqual(observations[0]["contract_month"], "202605")

    def test_mmsc_propagated(self):
        """mmsc_cls_code가 출력 dict에 정확히 들어감."""
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
            self._make_row_str("A75606", "미국달러 F 202606", "2"),
        ]
        observations = collect_observations(self._make_raw(rows))
        by_code = {o["short_code"]: o for o in observations}
        self.assertEqual(by_code["A75605"]["mmsc_cls_code"], "1")
        self.assertEqual(by_code["A75606"]["mmsc_cls_code"], "2")


# ---------------------------------------------------------------------------
# main() end-to-end
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):

    @staticmethod
    def _make_row_str(short_code: str, name: str, mmsc: str) -> str:
        head = "C5"
        std = "KR4A75650007"  # 12자 hardcoded (테스트 단순화 — 실제 표준코드 매핑 X)
        tail = "FILLERXX" + mmsc + "USD미국달러"
        return f"{head}{short_code:<9}{std:<12}{name:<32}{tail}"

    def test_main_normal_jsonl_output(self):
        """fetch 성공 → JSONL 1줄 stdout."""
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
            self._make_row_str("A75606", "미국달러 F 202606", "2"),
        ]
        raw = "\n".join(rows).encode("cp949")
        captured = io.StringIO()
        with patch("observe_kis_master.fetch_commodity_future_master", return_value=raw), \
             patch("sys.stdout", captured), \
             patch("sys.argv", ["observe_kis_master.py"]):
            rc = main()
        self.assertEqual(rc, 0)
        output = captured.getvalue().strip()
        # JSONL 1줄
        self.assertEqual(len(output.splitlines()), 1)
        snapshot = json.loads(output)
        self.assertIn("timestamp", snapshot)
        self.assertIn("contracts", snapshot)
        codes = [c["short_code"] for c in snapshot["contracts"]]
        self.assertEqual(set(codes), {"A75605", "A75606"})

    def test_main_fetch_failure_exit_1(self):
        """fetch 예외 → exit 1 + stderr."""
        captured_err = io.StringIO()
        with patch("observe_kis_master.fetch_commodity_future_master",
                   side_effect=ConnectionError("network down")), \
             patch("sys.stderr", captured_err), \
             patch("sys.argv", ["observe_kis_master.py"]):
            rc = main()
        self.assertEqual(rc, 1)
        self.assertIn("fetch 실패", captured_err.getvalue())

    def test_main_stdout_pure_json_even_with_logger_info(self):
        """fetch 내부 logger INFO 호출되어도 stdout은 JSON 1줄만 (Codex BLOCKING fix).

        실제 운영에서 `python observe_kis_master.py >> /tmp/obs.jsonl` 호출 시
        fetch_commodity_future_master의 logger.info("[kis_master] downloading ...")가
        stdout으로 leak되어 첫 줄에 비-JSON 텍스트가 섞이면 jq/json.tool 파싱 실패.
        main()이 logger level을 WARNING으로 silencing하는지 검증.
        """
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
        ]
        raw = "\n".join(rows).encode("cp949")

        def fake_fetch_with_logger_info(*args, **kwargs):
            # 실제 fetch_commodity_future_master 동작 시뮬레이션 — INFO log 발생
            logging.getLogger("app.sources.kis_master").info(
                "[kis_master] downloading https://example.com/master.zip"
            )
            return raw

        captured = io.StringIO()
        # logger output을 stderr로 강제하지 않고, app.sources.kis_master logger의
        # 기본 propagation으로 실제 운영 환경(stdout) 시뮬레이션
        kis_logger = logging.getLogger("app.sources.kis_master")
        original_level = kis_logger.level
        # 명시 stdout handler 추가해 운영 환경 모방
        stdout_handler = logging.StreamHandler(captured)
        stdout_handler.setLevel(logging.INFO)
        kis_logger.addHandler(stdout_handler)
        kis_logger.setLevel(logging.INFO)  # main()이 WARNING으로 낮춰야 통과

        try:
            with patch(
                "observe_kis_master.fetch_commodity_future_master",
                side_effect=fake_fetch_with_logger_info,
            ), patch("sys.stdout", captured), patch(
                "sys.argv", ["observe_kis_master.py"]
            ):
                rc = main()

            self.assertEqual(rc, 0)
            output = captured.getvalue().strip()
            # 핵심: 1줄만, 첫 줄이 valid JSON
            lines = output.splitlines()
            self.assertEqual(
                len(lines), 1,
                f"stdout에 logger leak — 예상 1줄, 실제 {len(lines)}줄: {output!r}",
            )
            snapshot = json.loads(lines[0])
            self.assertIn("contracts", snapshot)
            self.assertEqual(snapshot["contracts"][0]["short_code"], "A75605")
        finally:
            kis_logger.removeHandler(stdout_handler)
            kis_logger.setLevel(original_level)

    def test_main_short_code_filter(self):
        """--short-code 옵션이 collect로 전달되는지 검증."""
        rows = [
            self._make_row_str("A75605", "미국달러 F 202605", "1"),
            self._make_row_str("A75606", "미국달러 F 202606", "2"),
        ]
        raw = "\n".join(rows).encode("cp949")
        captured = io.StringIO()
        with patch("observe_kis_master.fetch_commodity_future_master", return_value=raw), \
             patch("sys.stdout", captured), \
             patch("sys.argv", ["observe_kis_master.py", "--short-code", "A75606"]):
            rc = main()
        self.assertEqual(rc, 0)
        snapshot = json.loads(captured.getvalue().strip())
        codes = [c["short_code"] for c in snapshot["contracts"]]
        self.assertEqual(codes, ["A75606"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
