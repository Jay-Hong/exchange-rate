"""scripts/preview_tether_tab.py 단위 테스트.

검증:
    - helper 호출 계약: db 위치 인자만 (ADR-038 D2 — include_krx 제거)
    - 기본 출력은 indent JSON, --compact는 single-line JSON
    - SessionLocal mock으로 실제 DB 미연결 (read-only 검증)
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# scripts/ 경로 추가 (observe_kis_master 테스트와 동일 패턴)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import preview_tether_tab  # noqa: E402


class TestPreviewTetherTab(unittest.TestCase):

    def _run_main(self, argv: list, payload: dict) -> str:
        """main() 호출 + stdout 캡처. SessionLocal과 helper mock."""
        fake_db = MagicMock()
        # SessionLocal()이 fake_db 반환, fake_db.close()는 호출 가능
        SessionLocalMock = MagicMock(return_value=fake_db)

        captured = io.StringIO()
        with patch.object(preview_tether_tab, "SessionLocal", SessionLocalMock), \
             patch.object(preview_tether_tab, "load_and_build_tether_tab_payload",
                          return_value=payload) as mock_helper, \
             patch.object(sys, "argv", ["preview_tether_tab.py"] + argv), \
             patch.object(sys, "stdout", captured):
            rc = preview_tether_tab.main()
        self.assertEqual(rc, 0)
        # mock_helper 호출 인자 검증을 위해 함께 반환
        return captured.getvalue(), mock_helper, fake_db

    # 1) helper 호출 계약 — db 위치 인자만, kwargs 없음 (ADR-038 D2: include_krx 제거).
    #    mock이라 시그니처 불일치를 못 잡는 함정 방지 — 실제 함수 시그니처와 교차 검증.
    def test_helper_called_with_db_only(self):
        import inspect
        from app import usdt_topic_payload as utp
        out, mock_helper, fake_db = self._run_main(
            [],
            payload={"type": "snapshot", "version": 1, "data": {}},
        )
        mock_helper.assert_called_once()
        self.assertIs(mock_helper.call_args.args[0], fake_db)
        self.assertEqual(mock_helper.call_args.kwargs, {})
        # 실제 시그니처에 include_krx 부재 (mock 우회 회귀 차단, codex 019f4117)
        sig = inspect.signature(utp.load_and_build_tether_tab_payload)
        self.assertNotIn("include_krx", sig.parameters)
        # db.close() 호출 검증 (finally 블록)
        fake_db.close.assert_called_once()

    # 3) 기본 출력은 indent JSON (multi-line + 공백)
    def test_default_output_is_indented_json(self):
        out, _, _ = self._run_main(
            [],
            payload={"type": "snapshot", "version": 1, "data": {"k": "v"}},
        )
        # parseable JSON
        parsed = json.loads(out)
        self.assertEqual(parsed["type"], "snapshot")
        # indent=2 → 멀티 라인 + 공백 인덴트
        self.assertIn("\n", out)
        self.assertIn('  "type"', out)  # 2-space indent

    # 4) --compact → single-line JSON (no indent)
    def test_compact_output_is_single_line_json(self):
        out, _, _ = self._run_main(
            ["--compact"],
            payload={"type": "snapshot", "version": 1, "data": {"k": "v"}},
        )
        # parseable
        parsed = json.loads(out)
        self.assertEqual(parsed["type"], "snapshot")
        # single line — \n은 마지막 print 줄바꿈 1개만
        self.assertEqual(out.count("\n"), 1)
        # indent 공백 없음
        self.assertNotIn('\n  "', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
