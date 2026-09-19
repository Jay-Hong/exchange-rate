"""Synthetic capture fixtures and an explicit network tripwire."""

import io
import logging
import socket
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests


@contextmanager
def redirect_app_logs(directory):
    """Use real file handlers, but keep import-time app logs in test scratch.

    app.config hardcodes its logs directory. Redirect only that directory's
    mkdir and FileHandler paths; never alter app files or logging behavior.
    This also works before pytest collection and in a fresh subprocess.
    """
    original_dir = Path(__file__).resolve().parents[1] / 'logs'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    mkdir = Path.mkdir
    initialize = logging.FileHandler.__init__

    def redirected_mkdir(path, *args, **kwargs):
        return mkdir(directory if path == original_dir else path, *args, **kwargs)

    def redirected_handler(handler, filename, *args, **kwargs):
        path = Path(filename)
        if path.parent == original_dir:
            filename = directory / path.name
        initialize(handler, filename, *args, **kwargs)

    with patch.object(Path, 'mkdir', redirected_mkdir), patch.object(logging.FileHandler, '__init__', redirected_handler):
        yield


@pytest.fixture
def artifact_output(tmp_path, monkeypatch):
    """Isolate publication tests from the separately tested path policy.

    A restricted verification run may place pytest scratch inside the checkout.
    Only in that case substitute its exact destination. Ordinary runs and all
    other paths use the real policy.
    """
    from tools.fixture_capture import capture  # Lazy: avoid app imports in test bootstrap.

    if capture.REPO_ROOT in tmp_path.parents:
        validate = capture.outside_repository
        monkeypatch.setattr(capture, 'outside_repository',
                            lambda output: tmp_path if Path(output) == tmp_path else validate(output))
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("fixture capture tests must not access a network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Raw:
    def __init__(self, body):
        self.body = io.BytesIO(body)
        self.reads = []
        self.closed = False

    def read(self, amount, decode_content=False):
        assert decode_content is False
        self.reads.append(amount)
        return self.body.read(amount)

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


def response(body=b"", *, status=200, content_type="text/html; charset=utf-8", encoding=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    if encoding:
        result.headers["Content-Encoding"] = encoding
    result.encoding = requests.utils.get_encoding_from_headers(result.headers)
    result.raw = Raw(body)
    return result


def getter(body, **kwargs):
    result = response(body, **kwargs)
    return Mock(return_value=result), result


def official_html(route="bs_official", rates=("1,300.25", "900.5", "1,500"), extra=""):
    rows = "".join(f"<tr><td>{code}</td><td>{rate}</td></tr>"
                   for code, rate in zip(("USD", "JPY", "EUR"), rates))
    table = '<thead><tr><th>통화</th><th>기준환율</th></tr></thead><tbody>' + rows + '</tbody>'
    table = '<table id="resultTable">' + table + '</table>' if route == "bs_official" else (
        '<div id="tab01"><table>' + table + '</table></div>')
    return '<html><head></head><body>' + extra + table + '</body></html>'


def citi_html(extra="", labels=("USD", "CNY", "EUR", "JPY")):
    items = "".join(f'<li><div><div>{code}</div><div><span>{1300 + index}</span></div></div></li>'
                    for index, code in enumerate(labels))
    return '<html><head></head><body><div id="content">' + extra + '<ul>' + items + '</ul></div></body></html>'


def mibank_row(code="USD", rate="1,300.25", links=None, flag=None, rate_class="counter", cells=None):
    if links is None:
        links = f'<a href="https://example.invalid/private?currency={code}">{code}</a>'
    flag = '' if flag is None else f'<img src="{flag}">'
    cells = f'<td><span class="{rate_class}">{rate}</span></td>' if cells is None else cells
    return f'<tr><td>{links}{flag}</td>{cells}</tr>'


def mibank_html(rows=None, header="<th>통화</th><th>기준환율</th>", extra=""):
    if rows is None:
        rows = ''.join(mibank_row(code) for code in ("USD", "JPY", "EUR"))
    head = '' if header is None else f'<thead><tr>{header}</tr></thead>'
    return '<html><head></head><body>' + extra + '<div class="box_contents1"><table>' + head + '<tbody>' + rows + '</tbody></table></div></body></html>'
