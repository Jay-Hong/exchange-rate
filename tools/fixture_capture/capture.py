"""Manual C1 capture CLI. No retries, fallback requests, commits or live tests.

Usage (C1c only)::

    python3 -m tools.fixture_capture.capture --route bs_official --output /tmp/fxi-captures

``--route all`` makes one independent attempt for each of the five routes.
Only complete, verified pairs are published in new per-capture directories.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import bs4
import soupsieve

from .detector import scan_fixture, validate_metadata
from .errors import CaptureError, DeadlineExpired
from .fetch import fetch_once
from .limits import METADATA_LIMIT, Deadline, wall_timeout
from .registry import Registry
from .roundtrip import PARSER, roundtrip
from .runtime import quiet_logging

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = tuple(f"app/crawlers/{name}.py" for name in ("bs", "citi", "utils", "bank_report", "constants"))
ROUTES = ("bs_official", "citi_primary", "citi_secondary", "bs_mibank", "citi_mibank")


def parser_versions():
    return {"name": PARSER, "beautifulsoup4": bs4.__version__,
            "soupsieve": soupsieve.__version__, "python": platform.python_version()}


def source_identity(registry):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    # No stale HEAD provenance for locally edited production extraction code.
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *SOURCE_FILES],
                           cwd=REPO_ROOT, capture_output=True)
    if clean.returncode != 0:
        raise CaptureError("dirty_extraction_source", "provenance")
    files = [REPO_ROOT / path for path in SOURCE_FILES]
    files += sorted(Path(__file__).parent.glob("*.py"))
    manifest = {str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in files}
    payload = {"files": manifest, "parser": parser_versions(), "selectors": registry.selectors}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return head, "c1b/1:" + digest


@dataclass(frozen=True)
class Artifact:
    fixture: bytes
    metadata: bytes
    capture_id: str
    route: str


def capture_route(route_name, *, registry=None, get=None, deadline=None):
    """Memory-only pipeline; all errors are code-owned and no traceback is logged."""
    try:
        with quiet_logging():
            registry = registry or Registry()
            if route_name not in registry.routes:
                raise CaptureError("unknown_route")
            route = registry.routes[route_name]
            source_commit, contract = source_identity(registry)
            deadline = deadline or Deadline()
            with wall_timeout(deadline.remaining(), "total_timeout"):
                fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                fetched = fetch_once(route.url, registry.sources.constants.HEADERS,
                                     deadline=deadline, get=get)
                fixture, soup, record = roundtrip(fetched.text, route, registry, deadline)
                scan_fixture(soup)
                capture_id = str(uuid4())
                parser = parser_versions()
                meta = {"origin": "dev_machine", "capture_id": capture_id,
                        "extraction_contract": contract, "source_commit": source_commit,
                        "route": route.name, "source_url_base": route.url, "parser": parser,
                        "fetched_at": fetched_at, "http_status": fetched.http_status,
                        "content_type": fetched.content_type, "charset": fetched.charset,
                        "original_body_sha256": fetched.original_body_sha256,
                        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
                        "recorded_extraction": record}
                validate_metadata(meta, route=route, registry=registry, source_commit=source_commit,
                                  contract=contract, parser=parser,
                                  original_hash=fetched.original_body_sha256, fixture=fixture)
                metadata = (json.dumps(meta, ensure_ascii=False, allow_nan=False,
                                       separators=(",", ":")) + "\n").encode("utf-8")
                if len(metadata) > METADATA_LIMIT:
                    raise CaptureError("metadata_size", "metadata")
                deadline.remaining()
                return Artifact(fixture, metadata, capture_id, route.name)
    except DeadlineExpired as error:
        raise CaptureError(error.rule) from None
    except KeyboardInterrupt:
        raise CaptureError("interrupted") from None
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("capture_failed") from None


def outside_repository(output):
    path = Path(output).expanduser().resolve()
    # Reject the current worktree and *any* other repository/worktree, including
    # a symlink into one. A worktree .git is a file, not necessarily a directory.
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise CaptureError("output_inside_repository", "output")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise CaptureError("output_inside_repository", "output")
    return path


def write_artifact(artifact, output):
    path = outside_repository(output)
    path.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=path))
    try:
        (staging / "fixture.html").write_bytes(artifact.fixture)
        (staging / "metadata.json").write_bytes(artifact.metadata)
        destination = path / f"{artifact.route}-{artifact.capture_id}"
        if destination.exists():
            raise CaptureError("capture_already_exists", "output")
        staging.rename(destination)
        return destination
    except BaseException:
        shutil.rmtree(staging)
        raise


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CaptureError("invalid_arguments", "cli")


def main(argv=None, *, get=None):
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--route", choices=(*ROUTES, "all"), required=True)
    parser.add_argument("--output", required=True, help="Output directory outside every Git checkout")
    try:
        args = parser.parse_args(argv)
        output = outside_repository(args.output)  # Reject before the first request.
        with quiet_logging():
            registry = Registry()
        selected = ROUTES if args.route == "all" else (args.route,)
        failed = False
        for name in selected:
            try:
                artifact = capture_route(name, registry=registry, get=get)
                with quiet_logging():
                    write_artifact(artifact, output)
                print(json.dumps({"route": name, "status": "captured", "capture_id": artifact.capture_id}))
            except CaptureError as error:
                failed = True
                print(json.dumps({"route": name, "status": "rejected", "rule": error.rule,
                                  "location": error.location}))
            except Exception:
                failed = True
                print(json.dumps({"route": name, "status": "rejected", "rule": "output_failed"}))
        return 1 if failed else 0
    except CaptureError as error:
        print(json.dumps({"status": "rejected", "rule": error.rule, "location": error.location}))
        return 1
    except Exception:
        print(json.dumps({"status": "rejected", "rule": "capture_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
