"""Api.iter_file and Api.put_chunk against a stub HTTP server, for runs without
jupyter_server installed."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jp.api import Api
from jp.commands._context import build_api
from jp.config import Config
from jp.errors import ApiError, AuthError, NetworkError, ServerDownError

BODY = bytes(range(256)) * 8


class Stub(BaseHTTPRequestHandler):
    received: list[dict] = []
    seen_headers: list[dict] = []

    def log_message(self, *args):
        return None

    def do_GET(self):
        Stub.seen_headers.append(dict(self.headers))
        if self.path == "/files/ranged.bin":
            offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
            self.send_response(206 if offset else 200)
            self.send_header("Content-Length", str(len(BODY) - offset))
            self.end_headers()
            self.wfile.write(BODY[offset:])
        elif self.path == "/files/full.bin":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/files/stopped.bin":
            self.send_response(302)
            self.send_header("Location", "/hub/login?next=/files/stopped.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/files/forbidden.bin":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_PUT(self):
        length = int(self.headers["Content-Length"])
        Stub.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def api():
    Stub.received, Stub.seen_headers = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Api(f"http://127.0.0.1:{server.server_port}", "tok", _allow_http_localhost=True)
    server.shutdown()
    server.server_close()


def test_whole_file_streams_from_the_start(api):
    resumed, chunks = api.iter_file("ranged.bin")
    assert resumed is False
    assert b"".join(chunks) == BODY


def test_offset_asks_for_the_rest_and_reports_resumed(api):
    resumed, chunks = api.iter_file("ranged.bin", 100)
    assert resumed is True
    assert b"".join(chunks) == BODY[100:]
    assert Stub.seen_headers[-1]["Range"] == "bytes=100-"


def test_server_ignoring_range_is_reported(api):
    resumed, _ = api.iter_file("full.bin", 100)
    assert resumed is False


def test_token_is_sent_in_the_header_never_in_the_url(api):
    b"".join(api.iter_file("ranged.bin")[1])
    assert Stub.seen_headers[-1]["Authorization"] == "token tok"


def test_redirect_means_the_server_stopped_not_a_login_page_download(api):
    with pytest.raises(ServerDownError):
        api.iter_file("stopped.bin")


def test_rejected_token_is_an_auth_error(api):
    with pytest.raises(AuthError):
        api.iter_file("forbidden.bin")


def test_missing_file_is_an_api_error(api):
    with pytest.raises(ApiError) as caught:
        api.iter_file("missing.bin")
    assert caught.value.status == 404


def test_unreachable_server_is_a_network_error():
    api = Api("http://127.0.0.1:9", "tok", _allow_http_localhost=True)
    with pytest.raises(NetworkError):
        api.iter_file("x.bin")


def test_failure_while_reading_becomes_a_network_error(api):
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            raise TimeoutError("read timed out")

    with pytest.raises(NetworkError, match="download interrupted"):
        list(api._read_stream(Broken()))


def test_put_chunk_sends_base64_with_the_server_numbering(api):
    api.put_chunk("dir/file.part", b"\x00\xffbytes", 7)
    assert Stub.received == [
        {
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(b"\x00\xffbytes").decode(),
            "chunk": 7,
        }
    ]


def test_configured_timeout_reaches_the_client(monkeypatch):
    monkeypatch.setenv("JP_TOKEN", "tok")
    cfg = Config(base_url="https://hub.example/user/me", prefix="work", timeout=123.0)
    assert build_api(cfg).timeout == 123.0
