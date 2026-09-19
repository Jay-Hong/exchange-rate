"""One streamed requests.get, bounded wire/decoded bodies, no raw disk writes."""

import hashlib
import zlib
from dataclasses import dataclass

import requests

from .errors import CaptureError, DeadlineExpired
from .limits import BODY_LIMIT, Deadline, wall_timeout

CHUNK_SIZE = 16 * 1024


@dataclass(frozen=True)
class Fetched:
    text: str
    http_status: int
    content_type: str
    charset: str | None
    original_body_sha256: str


class _Inflater:
    """Bound gzip/zlib/raw-deflate expansion inside zlib itself.

    Other content encodings are rejected before body reads. In particular br
    is advertised by the shared HEADERS but Python's optional Brotli APIs have
    no portable output bound. Failing closed avoids an unbounded decompression
    allocation; callers must not silently retry with different headers.
    """
    def __init__(self, encoding):
        self.encoding = encoding
        self.decoder = None
        self.pending = b""
        if encoding == "gzip":
            self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding not in ("", "identity", "deflate"):
            raise CaptureError("unsupported_content_encoding", "response")

    def feed(self, data, remaining):
        if self.encoding in ("", "identity"):
            return data
        if self.encoding == "deflate" and self.decoder is None:
            self.pending += data
            if len(self.pending) < 2:
                return b""
            data, self.pending = self.pending, b""
            # zlib header (CM=8, CINFO<=7, FCHECK); otherwise raw deflate.
            wrapped = (data[0] & 15 == 8 and data[0] >> 4 <= 7
                       and int.from_bytes(data[:2], "big") % 31 == 0)
            self.decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        output = bytearray()
        while data:
            if self.decoder.eof:
                if self.encoding != "gzip":
                    raise CaptureError("trailing_compressed_data", "response")
                self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            part = self.decoder.decompress(data, remaining + 1 - len(output))
            output.extend(part)
            # Stop here: another iteration after the sentinel byte would pass
            # max_length=0 to zlib, which means unlimited output allocation.
            if len(output) > remaining:
                raise CaptureError("decoded_body_limit", "response")
            data = self.decoder.unused_data if self.decoder.eof else self.decoder.unconsumed_tail
        return bytes(output)

    def finish(self):
        if self.encoding not in ("", "identity") and (self.decoder is None or not self.decoder.eof):
            raise CaptureError("incomplete_compressed_body", "response")


def _check_status(response, **kwargs):
    # requests prepares Response.next even with allow_redirects=False, consuming
    # a redirect body on that path. A response hook rejects before that happens.
    if not 200 <= response.status_code < 300:
        response.close()
        raise CaptureError("http_status", "response")
    return response


def fetch_once(url, headers, *, deadline=None, get=None):
    deadline = deadline or Deadline()
    get = get or requests.get
    response = None
    try:
        with wall_timeout(deadline.remaining(), "total_timeout"):
            response = get(url, headers=headers, allow_redirects=False, stream=True,
                           timeout=min(10, deadline.remaining()),
                           hooks={"response": _check_status})
            deadline.remaining()
            _check_status(response)  # Also enforced for injected fake responses.
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            inflater = _Inflater(encoding)
            wire_count = 0
            body = bytearray()
            while True:
                deadline.remaining()
                # Read at most one byte beyond the wire limit, then stop at once.
                chunk = response.raw.read(min(CHUNK_SIZE, BODY_LIMIT - wire_count + 1),
                                          decode_content=False)
                deadline.remaining()
                if not chunk:
                    break
                wire_count += len(chunk)
                if wire_count > BODY_LIMIT:
                    raise CaptureError("wire_body_limit", "response")
                decoded = inflater.feed(chunk, BODY_LIMIT - len(body))
                if len(body) + len(decoded) > BODY_LIMIT:
                    raise CaptureError("decoded_body_limit", "response")
                body.extend(decoded)
            inflater.finish()
            # A bounded in-memory Response delegates decoding to requests itself:
            # header encoding, apparent_encoding and errors='replace' all match.
            decoded_response = requests.Response()
            decoded_response._content = bytes(body)
            decoded_response.encoding = response.encoding
            text = decoded_response.text
            deadline.remaining()
            return Fetched(text, response.status_code,
                           response.headers.get("Content-Type", ""), response.encoding,
                           hashlib.sha256(body).hexdigest())
            # Hash basis: decompressed response.content bytes, before text decoding.
    except DeadlineExpired as error:
        raise CaptureError(error.rule, "response") from None
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("fetch_failed", "response") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                # A failed close cannot make a failed capture acceptable.
                raise CaptureError("response_close_failed", "response") from None
