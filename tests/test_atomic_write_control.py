"""AtomicWriteControl model + app/atomic_write_control.py 단위 테스트 (PR D / P1b A1).

in-memory SQLite + synthetic row로 검증. 외부 DB/firebase 의존성 0
(conftest.py가 DATABASE_URL/firebase stub 처리하지만 본 파일은 자체 engine 사용).

검증:
- ORM DDL compile (postgresql + sqlite) + CHECK 3개 포함
- CHECK 런타임 enforcement (sqlite): singleton(id=1) / enum / activation_epoch non-negative
- bootstrap idempotent seed (1 row, 재호출 시 값 미변경)
- compute_effective_mode 매트릭스 (§3/§7 fail-closed)
- evaluate_preflight pass/fail (§6 진단)
- get_control_state_dict projection + KST isoformat
- behavior-change-0 dormancy trip-wire (writer/broadcast 모듈에 control symbol 부재)
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.atomic_write_control import (
    ATOMIC_SCHEMA_FLOOR,
    CONTROL_ROW_FORMAT_VERSION,
    IMAGE_MAX_WRITER_PROTOCOL,
    IMAGE_MIN_WRITER_PROTOCOL,
    WriterMode,
    bootstrap_atomic_write_control,
    compute_effective_mode,
    evaluate_preflight,
    get_control_state_dict,
    read_control_row,
)
from app.models import AtomicWriteControl


def _fresh_session():
    """격리된 in-memory SQLite engine + AtomicWriteControl 테이블."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AtomicWriteControl.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _mode_row(
    requested_mode=WriterMode.LEGACY,
    activation_epoch=0,
    control_row_format_version=CONTROL_ROW_FORMAT_VERSION,
    target_write_schema_version=1,
):
    """compute_effective_mode/evaluate_preflight용 attribute stub."""
    return SimpleNamespace(
        requested_mode=requested_mode,
        activation_epoch=activation_epoch,
        control_row_format_version=control_row_format_version,
        target_write_schema_version=target_write_schema_version,
        required_writer_protocol=1,
    )


class TestDDLCompile(unittest.TestCase):
    def test_compiles_both_dialects_with_three_checks(self):
        for dialect in (postgresql.dialect(), sqlite_dialect.dialect()):
            ddl = str(CreateTable(AtomicWriteControl.__table__).compile(dialect=dialect))
            self.assertIn("ck_atomic_write_control_singleton", ddl)
            self.assertIn("ck_atomic_write_control_requested_mode", ddl)
            self.assertIn("ck_atomic_write_control_activation_epoch", ddl)
            # CHECK 식 자체도 포함
            self.assertIn("id = 1", ddl)
            self.assertIn("activation_epoch >= 0", ddl)

    def test_exactly_three_checks(self):
        # §4 line 43-45: CHECK 정확히 3개 (monotonic은 conditional-update로 defer)
        checks = [c for c in AtomicWriteControl.__table__.constraints
                  if c.__class__.__name__ == "CheckConstraint"]
        self.assertEqual(len(checks), 3)


class TestCheckEnforcement(unittest.TestCase):
    """SQLite는 CHECK를 기본 enforce (PRAGMA 불요)."""

    def test_singleton_check_rejects_second_id(self):
        db = _fresh_session()
        db.add(AtomicWriteControl(
            id=2, control_row_format_version=1, target_write_schema_version=1,
            required_writer_protocol=1, activation_epoch=0, mode_generation=0,
            requested_mode="legacy",
        ))
        with self.assertRaises(IntegrityError):
            db.commit()

    def test_enum_check_rejects_bogus_mode(self):
        db = _fresh_session()
        db.add(AtomicWriteControl(
            id=1, control_row_format_version=1, target_write_schema_version=1,
            required_writer_protocol=1, activation_epoch=0, mode_generation=0,
            requested_mode="bogus",
        ))
        with self.assertRaises(IntegrityError):
            db.commit()

    def test_activation_epoch_check_rejects_negative(self):
        db = _fresh_session()
        db.add(AtomicWriteControl(
            id=1, control_row_format_version=1, target_write_schema_version=1,
            required_writer_protocol=1, activation_epoch=-1, mode_generation=0,
            requested_mode="legacy",
        ))
        with self.assertRaises(IntegrityError):
            db.commit()


class TestBootstrap(unittest.TestCase):
    def test_seeds_singleton_with_defaults(self):
        db = _fresh_session()
        row = bootstrap_atomic_write_control(db)
        self.assertEqual(row.id, 1)
        self.assertEqual(row.requested_mode, WriterMode.LEGACY)
        self.assertEqual(row.activation_epoch, 0)
        self.assertEqual(row.mode_generation, 0)
        self.assertEqual(row.required_writer_protocol, 1)
        self.assertEqual(row.control_row_format_version, CONTROL_ROW_FORMAT_VERSION)
        self.assertIsNone(row.activated_at)  # 활성화 전 None (activation_epoch=0과 정합)
        self.assertEqual(db.query(AtomicWriteControl).count(), 1)

    def test_idempotent_no_second_row(self):
        db = _fresh_session()
        bootstrap_atomic_write_control(db)
        bootstrap_atomic_write_control(db)
        self.assertEqual(db.query(AtomicWriteControl).count(), 1)

    def test_idempotent_does_not_mutate_existing(self):
        db = _fresh_session()
        bootstrap_atomic_write_control(db)
        # 기존 값을 halt로 변경 후 재-bootstrap → 덮어쓰지 않음
        row = read_control_row(db)
        row.requested_mode = WriterMode.HALT
        db.commit()
        again = bootstrap_atomic_write_control(db)
        self.assertEqual(again.requested_mode, WriterMode.HALT)

    def test_read_control_row_none_on_empty(self):
        db = _fresh_session()
        self.assertIsNone(read_control_row(db))


class TestComputeEffectiveMode(unittest.TestCase):
    def test_matrix(self):
        H, L, A = WriterMode.HALT, WriterMode.LEGACY, WriterMode.ATOMIC
        self.assertEqual(compute_effective_mode(None), H)                                  # row 부재
        self.assertEqual(compute_effective_mode(_mode_row(L, 0)), L)                       # legacy, 미활성화
        self.assertEqual(compute_effective_mode(_mode_row(L, 1)), H)                       # legacy + activation 이력 → halt
        self.assertEqual(compute_effective_mode(_mode_row(H)), H)                          # halt
        self.assertEqual(compute_effective_mode(_mode_row(A, 1, target_write_schema_version=ATOMIC_SCHEMA_FLOOR)), A)  # atomic 정상
        self.assertEqual(compute_effective_mode(_mode_row(A, 0, target_write_schema_version=ATOMIC_SCHEMA_FLOOR)), H)  # atomic, 미활성화 → halt
        self.assertEqual(compute_effective_mode(_mode_row(A, 1, target_write_schema_version=1)), H)                    # atomic, schema floor 미달 → halt
        self.assertEqual(compute_effective_mode(_mode_row("garbage")), H)                  # enum corruption → halt
        self.assertEqual(compute_effective_mode(_mode_row(L, 0, control_row_format_version=99)), H)  # format 불일치 → halt

    def test_numeric_corruption_fails_closed(self):
        # DB CHECK/NOT NULL이 1차 방어이나, fail-closed 함수로서 corrupted row도 halt
        H = WriterMode.HALT
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.LEGACY, activation_epoch=-1)), H)   # 음수 epoch → halt (legacy 아님)
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.LEGACY, activation_epoch=None)), H)  # None epoch → halt (예외 아님)
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.ATOMIC, activation_epoch=1, target_write_schema_version=None)), H)  # None schema → halt
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.ATOMIC, activation_epoch=True, target_write_schema_version=2)), H)  # bool epoch corruption → halt
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.LEGACY, target_write_schema_version=-1)), H)  # 음수 schema → halt (legacy여도)
        self.assertEqual(compute_effective_mode(_mode_row(WriterMode.LEGACY, target_write_schema_version=0)), H)   # schema 0(< 1) → halt


class TestPreflight(unittest.TestCase):
    def test_pass_when_required_in_image_range(self):
        result = evaluate_preflight(_mode_row())  # required_writer_protocol=1
        self.assertTrue(result["passed"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["image_min_protocol"], IMAGE_MIN_WRITER_PROTOCOL)
        self.assertEqual(result["image_max_protocol"], IMAGE_MAX_WRITER_PROTOCOL)

    def test_fail_when_required_outside_range(self):
        row = _mode_row()
        row.required_writer_protocol = 2  # image range [1,1] 밖
        result = evaluate_preflight(row)
        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["reason"])

    def test_none_row_fails_closed(self):
        result = evaluate_preflight(None)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["required_writer_protocol"])


class TestStateDict(unittest.TestCase):
    def test_projects_all_fields_with_kst(self):
        db = _fresh_session()
        row = bootstrap_atomic_write_control(db)
        state = get_control_state_dict(row)
        for key in ("id", "control_row_format_version", "target_write_schema_version",
                    "required_writer_protocol", "activation_epoch", "mode_generation",
                    "requested_mode", "activated_at", "updated_at"):
            self.assertIn(key, state)
        # updated_at: naive UTC → KST isoformat 문자열 (+09:00). activated_at: seed에선 None (활성화 전).
        self.assertIsInstance(state["updated_at"], str)
        self.assertIn("+09:00", state["updated_at"])
        self.assertIsNone(state["activated_at"])


class TestDormancyTripwire(unittest.TestCase):
    """behavior-change-0: writer/broadcast/mirror 모듈이 control symbol을 호출하지 않음.

    A2에서 writer wiring이 추가되면 이 테스트가 의도적으로 깨지며 경계를 재확인시킨다.
    허용 위치: app/atomic_write_control.py(정의) + app/main.py(status endpoint 진단).
    """

    SYMBOLS = ("compute_effective_mode", "read_control_row")

    def test_main_py_symbols_only_in_status_endpoint(self):
        # main.py는 status endpoint 함수 내부만 allow (broadcast hot path도 main.py에 있어
        # 파일 전체 allow는 사각지대 — AST로 함수 line range만 정밀 허용).
        main_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
        src = main_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        allowed = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_atomic_write_control_status":
                allowed = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        self.assertTrue(allowed, "main.py에서 get_atomic_write_control_status 함수를 못 찾음")
        for i, line in enumerate(src.splitlines(), start=1):
            if any(s in line for s in self.SYMBOLS):
                self.assertIn(
                    i, allowed,
                    f"main.py:{i} control symbol이 status endpoint 밖에서 사용됨 (A1 dormancy 위반)",
                )

    def test_control_symbols_absent_from_other_app_modules(self):
        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
        # 합법적 consumer만 allow: atomic_write_control.py(정의) / atomic_write_runtime.py(A2 cache
        # layer — writer 아님, control symbol 읽어 snapshot 계산) / main.py(status endpoint, 위 AST
        # 테스트가 정밀 검사). writer(crud/latest_rates_cache/scheduler 등) 누수는 여전히 차단.
        allow = {"atomic_write_control.py", "atomic_write_runtime.py", "main.py"}
        offenders = []
        for py in app_dir.rglob("*.py"):
            if py.name in allow:
                continue
            text = py.read_text(encoding="utf-8")
            # 호출 형태(`symbol(`)만 = 실제 사용. docstring/주석의 단순 언급은 dormancy 위반 아님.
            if any((s + "(") in text for s in self.SYMBOLS):
                offenders.append(str(py.relative_to(app_dir.parent)))
        self.assertEqual(offenders, [], f"control symbol 누출 (A1 dormancy 위반): {offenders}")


class TestCreateAllExclusionBehavior(unittest.TestCase):
    """create_all_app_tables가 control table은 제외하고 나머지는 생성하는 *실제 동작* 검증.

    정적 멤버십(CREATE_ALL_EXCLUDE_TABLES)만이 아니라 behavioral — 이 PR의 핵심 safety
    property("control table은 create_all 진입점에서 미생성")를 helper 로직 변경 회귀까지 잠금.
    """

    def test_helper_excludes_control_table_creates_others(self):
        from sqlalchemy import create_engine, inspect

        from app.database import create_all_app_tables

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        create_all_app_tables(engine)
        tables = set(inspect(engine).get_table_names())
        self.assertNotIn("atomic_write_control", tables)  # 핵심 safety: control table 미생성
        self.assertIn("bank_exchange_rates", tables)       # 다른 ORM table은 정상 생성됨


if __name__ == "__main__":
    unittest.main()
