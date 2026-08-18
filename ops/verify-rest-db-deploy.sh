#!/usr/bin/env bash
# Post-deploy smoke for the REST auth lane and DB workload profiles.
# Run after the new container is healthy and before automatic rollback is disarmed.

set -u -o pipefail
umask 077

EVIDENCE_DIR="${1:?usage: verify-rest-db-deploy.sh EVIDENCE_DIR}"
REPO_DIR="${FXI_REPO_DIR:-$HOME/exchange-rate}"
OUT="$EVIDENCE_DIR/rest-db.txt"
FAIL=0

install -d -m 0700 "$EVIDENCE_DIR"
if [ -e "$OUT" ]; then
  echo "evidence already exists: $OUT" >&2
  exit 1
fi
: > "$OUT"

note() {
  printf '%s\n' "$*" | tee -a "$OUT"
}

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    note "  PASS $name ($actual)"
  else
    note "  FAIL $name expected=$expected actual=$actual"
    FAIL=1
  fi
}

note "=== A. REST lane runtime readiness (invalid JWT -> 401)"
# This request runs inside the Uvicorn process. A missing named app or executor
# returns 503; 401 means the token reached the Firebase verifier through the lane.
A=$(docker exec exchange-rate-app curl -sS -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6ImRlcGxveS1wcm9iZSJ9.e30.aW52YWxpZA' \
  http://127.0.0.1:8000/api/notification-settings 2>> "$OUT")
check "invalid JWT" "401" "${A:-error}"

note "=== B. online profile timeout readback"
B=$(docker exec exchange-rate-app python -c '
from sqlalchemy import text
from app.database import engine, DB_WORKLOAD_PROFILE
with engine.connect() as c:
    statement = c.execute(text("SHOW statement_timeout")).scalar()
    params = c.connection.dbapi_connection.info.get_parameters()
print(f"{DB_WORKLOAD_PROFILE}|{statement}|{params.get('\''connect_timeout'\'')}|{engine.pool._timeout}")
' 2>> "$OUT")
check "online profile/statement/connect/pool" "online|1min|5|10" "${B:-error}"

note "=== C. maintenance profile timeout readback"
C=$(
  cd "$REPO_DIR" &&
    docker compose run --rm --no-deps --pull never \
      -e DB_WORKLOAD_PROFILE=maintenance -T fastapi python -c '
from sqlalchemy import text
from app.database import engine, DB_WORKLOAD_PROFILE
with engine.connect() as c:
    statement = c.execute(text("SHOW statement_timeout")).scalar()
    params = c.connection.dbapi_connection.info.get_parameters()
print(f"{DB_WORKLOAD_PROFILE}|{statement}|{params.get('\''connect_timeout'\'')}|{engine.pool._timeout}")
' 2>> "$OUT" | tail -n 1
)
check "maintenance profile/statement/connect/pool" "maintenance|15min|10|30" "${C:-error}"

note "=== D. canceled statement reuses the same physical connection"
D=$(docker exec exchange-rate-app python -c '
from sqlalchemy import text
from app.database import engine
with engine.connect() as c:
    before = c.execute(text("SELECT pg_backend_pid()")).scalar()
    c.execute(text("SET statement_timeout='\''800ms'\''"))
    try:
        c.execute(text("SELECT pg_sleep(5)"))
        sqlstate = "none"
    except Exception as exc:
        sqlstate = str(getattr(getattr(exc, "orig", None), "sqlstate", None))
    c.rollback()
    after = c.execute(text("SELECT pg_backend_pid()")).scalar()
    recovered = c.execute(text("SELECT 1")).scalar()
print(f"{sqlstate}|{int(before == after)}|{recovered}")
' 2>> "$OUT")
check "SQLSTATE/same PID/reuse" "57014|1|1" "${D:-error}"

note "=== E. sixth checkout times out and the pool recovers"
E=$(docker exec exchange-rate-app python -c '
import time
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SATimeoutError
from app.database import engine

held = []
extra = None
try:
    held = [engine.connect() for _ in range(5)]
    started = time.monotonic()
    try:
        extra = engine.connect()
        result = "no_timeout"
    except SATimeoutError:
        result = "timeout_%ds" % round(time.monotonic() - started)
    if extra is not None:
        extra.close()
        extra = None
    held.pop().close()
    with engine.connect() as recovered:
        recovered.execute(text("SELECT 1"))
finally:
    if extra is not None:
        extra.close()
    for connection in held:
        connection.close()
print(result + "|recovered")
' 2>> "$OUT")
case "$E" in
  timeout_9s\|recovered|timeout_10s\|recovered|timeout_11s\|recovered)
    note "  PASS pool exhaustion ($E)"
    ;;
  *)
    note "  FAIL pool exhaustion expected=timeout_~10s|recovered actual=${E:-error}"
    FAIL=1
    ;;
esac

note "=== F. Firebase auth unavailable log count (diagnostic only)"
F=$(docker compose -f "$REPO_DIR/docker-compose.yml" logs fastapi --since 15m \
  2>> "$OUT" | grep -c 'Firebase auth unavailable')
note "  Firebase auth unavailable: ${F:-?} (not a pass/fail oracle)"

if [ "$FAIL" -ne 0 ]; then
  note "FINAL=DEPLOY_BLOCKED"
  exit 1
fi
note "FINAL=REST_DB_EVIDENCE_COMPLETE"
