"""scripts/preview_tether_tab.py 단위 테스트.

검증:
    - --include-krx → load_and_build에 include_krx=True 전달
    - 기본 (no flag) → include_krx=False
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

    # 1) --include-krx → helper에 include_krx=True 전달
    def test_include_krx_flag_passes_true(self):
        out, mock_helper, fake_db = self._run_main(
            ["--include-krx"],
            payload={"type": "snapshot", "version": 1, "data": {}},
        )
        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        self.assertEqual(kwargs.get("include_krx"), True)
        # db 인자 위치 검증
        self.assertIs(mock_helper.call_args.args[0], fake_db)
        # db.close() 호출 검증 (finally 블록)
        fake_db.close.assert_called_once()

    # 2) 기본 (no flag) → include_krx=False
    def test_default_passes_include_krx_false(self):
        out, mock_helper, _ = self._run_main(
            [],
            payload={"type": "snapshot", "version": 1, "data": {}},
        )
        kwargs = mock_helper.call_args.kwargs
        self.assertEqual(kwargs.get("include_krx"), False)

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
