"""FCM 확정 미등록 토큰 정리 — 소유자 fence 회귀 잠금.

구 `delete_devices_by_token(db, device_token)` 은 토큰만 보고 지웠다. `device_token` 은
전역 unique 이고 `register_device` 가 충돌 시 `user_id` 를 갱신하므로(소유권 이전),
**A 로그아웃 → B 로그인 → A 의 뒤늦은 정리 → B 의 정상 등록 삭제** 가 성립했다.

여기서 잠그는 것은 네 가지다.
  1. SQL 술어가 `user_id == captured_uid` **와** `device_token IN (sent ∩ unregistered)` 를
     동시에 갖는다 — 한쪽만으로는 구 동작과 구분되지 않는다.
  2. A→B 소유권 이전 후 A 의 정리가 B 의 행을 건드리지 않는다 (실 DB 로 검증).
  3. 보낸 적 없는 토큰은 삭제되지 않는다 (fail-closed).
  4. UID 별 누적이 서로 섞이지 않고, 발송 간 cross-term 이 생기지 않는다.

실 SQLite 세션을 쓴다 — MagicMock 은 WHERE 절이 틀려도 행 개수를 돌려주므로
"몇 행이 지워졌나" 를 증명하지 못한다.
"""

import ast
import pathlib
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models


def _user_device_token_delete_functions(source: str, label: str):
    """UserDevice token DELETE를 보수적으로 함수 단위로 찾는다.

    direct query chain만 보면 다음 우회가 전부 false green이 된다:
    ``q = query(...); q.delete()``, model alias, helper-return query,
    ``db.execute(delete(UserDevice).where(...))``. 따라서 함수 안에
    (1) delete 동작, (2) device_token, (3) UserDevice 계열 표식이 함께 있으면
    후보로 올린다. 허용 함수의 실제 UID/token 술어는 SQLite 동작 테스트가
    별도로 잠근다.
    """
    records = []
    tree = ast.parse(source)
    sqlalchemy_delete_names = {"delete"}
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and (item.module or "").startswith("sqlalchemy"):
            for imported in item.names:
                if imported.name == "delete":
                    sqlalchemy_delete_names.add(imported.asname or imported.name)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_source = ast.get_source_segment(source, node) or ""
        lowered = function_source.lower()
        calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
        delete_calls = [
            call
            for call in calls
            if (
                isinstance(call.func, ast.Attribute)
                and "delete" in call.func.attr.lower()
            )
            or (
                isinstance(call.func, ast.Name)
                and (
                    "delete" in call.func.id.lower()
                    or call.func.id in sqlalchemy_delete_names
                )
            )
        ]
        has_delete = bool(delete_calls) or (
            "delete" in lowered and "user_devices" in lowered
        )
        has_token = "device_token" in lowered
        has_user_device = (
            "userdevice" in lowered
            or "user_device" in lowered
            or "user_devices" in lowered
        )
        if not (has_delete and has_token and has_user_device):
            continue
        # 진단값은 delete expression 자체만 본다. 함수의 별개 UID SELECT를 합치면
        # token-only DELETE가 fenced로 보이는 false green이 된다.
        has_uid_reference = any(
            "user_id" in ast.unparse(call) or "owner_uid" in ast.unparse(call)
            for call in delete_calls
        )
        records.append((label, node.name, has_uid_reference))
    return records


def _session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _device(db, *, user_id: str, token: str) -> models.UserDevice:
    d = models.UserDevice(user_id=user_id, device_token=token, platform="ios")
    db.add(d)
    db.commit()
    return d


def _owners(db) -> dict:
    return {d.device_token: d.user_id for d in db.query(models.UserDevice).all()}


class TestPurgeUnregisteredDevices(unittest.TestCase):
    """`purge_unregistered_devices` — 두 겹 fence."""

    def setUp(self):
        self.db = _session()

    def tearDown(self):
        self.db.close()

    def test_deletes_only_own_confirmed_unregistered(self):
        _device(self.db, user_id="A", token="tA")
        _device(self.db, user_id="B", token="tB")
        deleted = crud.purge_unregistered_devices(
            self.db, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=["tA"]
        )
        self.db.commit()
        self.assertEqual(deleted, 1)
        self.assertEqual(_owners(self.db), {"tB": "B"})

    def test_ownership_transfer_a_then_b_is_not_deleted(self):
        """A→B 이전 후 A 의 뒤늦은 정리가 B 의 등록을 지우지 않는다 (사고 시나리오)."""
        # 수동 row 변경이 아니라 실제 production UPSERT를 두 번 태운다.
        # 이래야 사고 전제(전역 unique + 소유권 이전)도 함께 잠긴다.
        crud.register_device(
            self.db, user_id="A", device_token="shared", platform="ios"
        )
        crud.register_device(
            self.db, user_id="B", device_token="shared", platform="ios"
        )

        deleted = crud.purge_unregistered_devices(
            self.db, owner_uid="A", sent_tokens=["shared"], unregistered_tokens=["shared"]
        )
        self.db.commit()
        self.assertEqual(deleted, 0, "A 의 정리가 B 의 등록을 지웠다")
        self.assertEqual(_owners(self.db), {"shared": "B"})

    def test_out_of_sent_token_is_not_deleted(self):
        _device(self.db, user_id="A", token="tA")
        _device(self.db, user_id="A", token="other")
        deleted = crud.purge_unregistered_devices(
            self.db, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=["other"]
        )
        self.db.commit()
        self.assertEqual(deleted, 0)
        self.assertEqual(set(_owners(self.db)), {"tA", "other"})

    def test_empty_sent_tokens_deletes_nothing(self):
        _device(self.db, user_id="A", token="tA")
        deleted = crud.purge_unregistered_devices(
            self.db, owner_uid="A", sent_tokens=[], unregistered_tokens=["tA"]
        )
        self.db.commit()
        self.assertEqual(deleted, 0)
        self.assertEqual(set(_owners(self.db)), {"tA"})

    def test_helper_does_not_commit(self):
        """⛔ helper 는 commit 하지 않는다 — 호출자 트랜잭션 경계를 바꾸면 안 된다."""
        _device(self.db, user_id="A", token="tA")
        crud.purge_unregistered_devices(
            self.db, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=["tA"]
        )
        self.db.rollback()          # commit 했다면 rollback 으로 되돌아오지 않는다
        self.assertEqual(set(_owners(self.db)), {"tA"}, "helper 가 몰래 commit 했다")

    def test_helper_does_not_log_success_before_the_caller_commits(self):
        """commit 전 성공·원문 token 로그를 되살리지 못하게 한다."""
        _device(self.db, user_id="A", token="tA")
        with patch.object(crud, "logger") as logger:
            deleted = crud.purge_unregistered_devices(
                self.db,
                owner_uid="A",
                sent_tokens=["tA"],
                unregistered_tokens=["tA"],
            )
        self.assertEqual(deleted, 1)
        self.assertEqual(logger.method_calls, [], "helper 가 commit 전에 성공 로그를 남겼다")
        self.db.rollback()

    def test_bare_str_and_mapping_bypass_would_delete(self):
        """⚠️ 이 케이스들은 **검증을 우회하면 실제로 행이 지워진다**.

        `sent_tokens="tA"` 같은 다중문자 토큰은 우회해도 `{'t','A'}` 가 되어 교집합이
        우연히 비므로 "0 건" 이 **틀린 이유로** 통과한다(변이 시험에서 실측). 검증이
        실효인지 보려면 우회 시 삭제로 이어지는 입력이어야 한다:
          · 1글자 토큰 — `list("a") == ["a"]` 라 분해해도 그대로 자기 자신
          · Mapping — key 순회가 곧 정상 토큰 목록
        """
        _device(self.db, user_id="A", token="a")
        _device(self.db, user_id="A", token="tA")

        with self.subTest("bare str, 1글자 토큰"):
            self.assertEqual(
                crud.purge_unregistered_devices(
                    self.db, owner_uid="A", sent_tokens="a", unregistered_tokens=["a"]),
                0)
            self.db.commit()
            self.assertIn("a", _owners(self.db), "bare str 우회로 행이 지워졌다")

        with self.subTest("mapping key 가 정상 토큰"):
            self.assertEqual(
                crud.purge_unregistered_devices(
                    self.db, owner_uid="A", sent_tokens={"tA": 1}, unregistered_tokens=["tA"]),
                0)
            self.db.commit()
            self.assertIn("tA", _owners(self.db), "mapping 우회로 행이 지워졌다")

        self.assertEqual(set(_owners(self.db)), {"a", "tA"})

    def test_invalid_inputs_fail_closed(self):
        """잘못된 입력은 **아무것도 지우지 않는다**. 보안 경계이므로 조용히 넘어가지 않는다."""
        _device(self.db, user_id="A", token="tA")

        def partial_generator():
            yield "tA"
            raise RuntimeError("중간 실패")

        cases = [
            ("bare str sent",        dict(owner_uid="A", sent_tokens="tA", unregistered_tokens=["tA"])),
            ("bare str unreg",       dict(owner_uid="A", sent_tokens=["tA"], unregistered_tokens="tA")),
            ("mapping",              dict(owner_uid="A", sent_tokens={"tA": 1}, unregistered_tokens=["tA"])),
            ("generator raises",     dict(owner_uid="A", sent_tokens=partial_generator(), unregistered_tokens=["tA"])),
            ("non-string element",   dict(owner_uid="A", sent_tokens=["tA", 7], unregistered_tokens=["tA"])),
            ("empty element",        dict(owner_uid="A", sent_tokens=["tA", ""], unregistered_tokens=["tA"])),
            ("owner_uid None",       dict(owner_uid=None, sent_tokens=["tA"], unregistered_tokens=["tA"])),
            ("owner_uid empty",      dict(owner_uid="", sent_tokens=["tA"], unregistered_tokens=["tA"])),
            ("owner_uid non-str",    dict(owner_uid=7, sent_tokens=["tA"], unregistered_tokens=["tA"])),
        ]
        for label, kwargs in cases:
            with self.subTest(label):
                self.assertEqual(crud.purge_unregistered_devices(self.db, **kwargs), 0)
                self.db.commit()
                self.assertEqual(set(_owners(self.db)), {"tA"}, f"{label}: 행이 지워졌다")


class TestAccumulateUnregisteredByOwner(unittest.TestCase):
    """UID 별 누적 — multi-user 비혼입 + 발송 간 cross-term 차단."""

    def test_different_uids_do_not_mix(self):
        acc = {}
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=["tA"])
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="B", sent_tokens=["tB"], unregistered_tokens=["tB"])
        self.assertEqual(acc, {"A": {"tA"}, "B": {"tB"}})

    def test_cross_send_terms_do_not_combine(self):
        """발송1 의 out-of-sent 오류가 발송2 의 sent 토큰과 결합하면 안 된다.

        합집합을 먼저 만들고 마지막에 교차하면 `b` 가 자격을 얻는다:
            sent {a} ∪ {b,c} = {a,b,c}   unregistered {b} ∪ {c} = {b,c}   → {b,c}
        발송 단위로 먼저 교차하면 `{}` ∪ `{c}` = `{c}` 다.
        """
        acc = {}
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="A", sent_tokens=["a"], unregistered_tokens=["b"])
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="A", sent_tokens=["b", "c"], unregistered_tokens=["c"])
        self.assertEqual(acc, {"A": {"c"}}, "발송 간 cross-term 이 결합됐다")

    def test_invalid_input_leaves_accumulator_untouched(self):
        """검증 실패 시 부분 bucket 이 남으면 안 된다 (원자성)."""
        def partial_generator():
            yield "x"
            raise RuntimeError("중간 실패")

        for label, kwargs in [
            # 우회 시 실제로 bucket 이 채워지는 입력이어야 검증이 실효인지 보인다
            ("bare str 1글자", dict(owner_uid="A", sent_tokens="a", unregistered_tokens=["a"])),
            ("bare str",    dict(owner_uid="A", sent_tokens="tok", unregistered_tokens=["tok"])),
            ("mapping",     dict(owner_uid="A", sent_tokens={"t": 1}, unregistered_tokens=["t"])),
            ("generator",   dict(owner_uid="A", sent_tokens=partial_generator(), unregistered_tokens=["x"])),
            ("non-string",  dict(owner_uid="A", sent_tokens=["t", 7], unregistered_tokens=["t"])),
        ]:
            with self.subTest(label):
                acc = {}
                crud.accumulate_unregistered_by_owner(acc, **kwargs)
                self.assertEqual(acc, {}, f"{label}: accumulator 가 오염됐다")

    def test_no_unregistered_records_nothing(self):
        acc = {}
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=[])
        self.assertEqual(acc, {})


class TestPurgeUnregisteredByOwner(unittest.TestCase):
    """누적본 일괄 삭제 — UID 별로 각자의 fence 가 걸린다."""

    def setUp(self):
        self.db = _session()

    def tearDown(self):
        self.db.close()

    def test_each_uid_fenced_separately(self):
        _device(self.db, user_id="A", token="tA")
        _device(self.db, user_id="B", token="tB")
        _device(self.db, user_id="B", token="keep")

        acc = {}
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="A", sent_tokens=["tA"], unregistered_tokens=["tA"])
        crud.accumulate_unregistered_by_owner(
            acc, owner_uid="B", sent_tokens=["tB"], unregistered_tokens=["tB"])
        deleted = crud.purge_unregistered_by_owner(self.db, acc)
        self.db.commit()

        self.assertEqual(deleted, 2)
        self.assertEqual(_owners(self.db), {"keep": "B"})

    def test_wrong_owner_bucket_deletes_nothing(self):
        """A 의 bucket 에 B 의 토큰이 들어가도 (평탄화 회귀) 삭제되지 않는다."""
        _device(self.db, user_id="B", token="tB")
        deleted = crud.purge_unregistered_by_owner(self.db, {"A": {"tB"}})
        self.db.commit()
        self.assertEqual(deleted, 0)
        self.assertEqual(_owners(self.db), {"tB": "B"})

    def test_does_not_commit(self):
        _device(self.db, user_id="A", token="tA")
        crud.purge_unregistered_by_owner(self.db, {"A": {"tA"}})
        self.db.rollback()
        self.assertEqual(set(_owners(self.db)), {"tA"}, "몰래 commit 했다")


class TestNoRawTokenOnlyDelete(unittest.TestCase):
    """token-only DELETE 재도입 방지 — 인벤토리 trip-wire."""

    def test_only_named_fenced_functions_delete_by_device_token(self):
        """UserDevice token 기반 DELETE는 두 fenced 함수에만 존재해야 한다.

        ⚠️ 인증된 `(uid, token)` 단건 해제(`delete_device`)와 보관기간 정리
        (`cleanup_old_user_devices`)는 금지 대상이 아니다 — 그들은 토큰만으로
        지우지 않는다. 보관기간 정리는 token 조건 자체가 없어 inventory 대상이 아니다.
        """
        records = []
        for path in sorted(pathlib.Path("app").rglob("*.py")):
            records.extend(
                _user_device_token_delete_functions(
                    path.read_text(encoding="utf-8"), str(path)
                )
            )
        found = {(path, name) for path, name, _ in records}
        self.assertEqual(
            found,
            {
                ("app/crud.py", "delete_device"),
                ("app/crud.py", "purge_unregistered_devices"),
            },
            f"예상 밖 UserDevice token DELETE inventory: {records}",
        )
        self.assertTrue(all(fenced for _, _, fenced in records), records)

    def test_inventory_detects_token_equality_without_uid(self):
        """구 helper의 `device_token == token` 형태도 반드시 잡아야 한다."""
        source = """
def unsafe(db, token):
    return db.query(models.UserDevice).filter(
        models.UserDevice.device_token == token
    ).delete()
"""
        self.assertEqual(
            _user_device_token_delete_functions(source, "synthetic.py"),
            [("synthetic.py", "unsafe", False)],
        )

    def test_inventory_detects_assigned_query_delete(self):
        source = """
def unsafe(db, token):
    q = db.query(models.UserDevice).filter(models.UserDevice.device_token == token)
    return q.delete()
"""
        self.assertEqual(
            _user_device_token_delete_functions(source, "synthetic.py"),
            [("synthetic.py", "unsafe", False)],
        )

    def test_inventory_detects_model_alias_and_helper_query(self):
        alias_source = """
def unsafe(db, token):
    UD = models.UserDevice
    return db.query(UD).filter(UD.device_token == token).delete()
"""
        helper_source = """
def unsafe(db, token):
    q = user_device_query(db).filter_by(device_token=token)
    return q.delete()
"""
        for source in (alias_source, helper_source):
            with self.subTest(source=source):
                records = _user_device_token_delete_functions(source, "synthetic.py")
                self.assertEqual(len(records), 1, records)
                self.assertEqual(records[0][1:], ("unsafe", False))

    def test_inventory_detects_sqlalchemy_core_delete(self):
        source = """
def unsafe(db, token):
    stmt = delete(models.UserDevice).where(models.UserDevice.device_token == token)
    return db.execute(stmt)
"""
        self.assertEqual(
            _user_device_token_delete_functions(source, "synthetic.py"),
            [("synthetic.py", "unsafe", False)],
        )

    def test_inventory_detects_aliased_sqlalchemy_core_delete(self):
        source = """
from sqlalchemy import delete as remove

def unsafe(db, token):
    stmt = remove(models.UserDevice).where(models.UserDevice.device_token == token)
    return db.execute(stmt)
"""
        self.assertEqual(
            _user_device_token_delete_functions(source, "synthetic.py"),
            [("synthetic.py", "unsafe", False)],
        )

    def test_each_allowed_delete_function_has_one_fenced_delete_expression(self):
        """허용 함수 안에 두 번째 token-only DELETE를 숨기는 회귀를 막는다.

        함수 간 동적 query 전달까지 증명하는 정적 분석기는 아니다. 그 한계는 유지하되,
        실제 허용 함수 내부의 추가 DELETE라는 가장 가까운 우회는 정확히 잠근다.
        """
        source = pathlib.Path("app/crud.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name, uid_name in (
            ("delete_device", "user_id"),
            ("purge_unregistered_devices", "owner_uid"),
        ):
            with self.subTest(function=name):
                node = functions[name]
                deletes = [
                    call
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "delete"
                ]
                self.assertEqual(len(deletes), 1, ast.unparse(node))
                expression = ast.unparse(deletes[0])
                self.assertIn("device_token", expression)
                self.assertIn(uid_name, expression)

    def test_unrelated_uid_query_does_not_fence_token_only_delete(self):
        """별개 UID SELECT는 token-only DELETE의 소유자 fence가 아니다."""
        source = """
def unsafe(db, uid, token):
    db.query(models.UserDevice).filter(models.UserDevice.user_id == uid).first()
    return db.query(models.UserDevice).filter(
        models.UserDevice.device_token == token
    ).delete()
"""
        self.assertEqual(
            _user_device_token_delete_functions(source, "synthetic.py"),
            [("synthetic.py", "unsafe", False)],
        )


if __name__ == "__main__":
    unittest.main()
