"""Tests for jp.api.Api transport against the REAL empirical server behaviour.

These mock urllib at the boundary (urlopen / opener) so we never touch the
network, while exercising the exact quirks recorded in
``research/01-jupyter-api.md``:

  * ``hash()`` uses ``content=0&hash=1`` and returns the server sha256 without
    a body download.
  * a 404 GET body is PLAIN TEXT (not JSON) and must NOT crash error parsing.
  * base64 download ``content`` carries a trailing newline that must be stripped
    before decoding (else the bytes are corrupted).
  * the health probe must NOT follow redirects: a 3xx -> /hub/ means the server
    is stopped and yields an actionable ServerDownError.
  * notebooks are fetched/sent as raw bytes (type=file) and detected by ext.
  * PUT reports create (201) vs update (200).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.request

import pytest

from jp.api import Api
from jp.errors import ApiError, AuthError, NetworkError, ServerDownError


class _FakeResp(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse context manager."""

    def __init__(self, body: bytes, status: int = 200, content_type: str = "application/json"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _api() -> Api:
    return Api("https://hub.example/user/alice", "tok-abcdef1234567890")


# --------------------------------------------------------------------------- #
# hash(): cheap server sha256 via content=0&hash=1 (NO body download)
# --------------------------------------------------------------------------- #
def test_hash_uses_content0_hash1_and_returns_sha256(monkeypatch):
    api = _api()
    payload = b"hello jp\nline2\n"
    sha = hashlib.sha256(payload).hexdigest()
    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=None, context=None):
        seen_urls.append(req.full_url)
        body = json.dumps(
            {"content": None, "size": len(payload), "hash": sha, "hash_algorithm": "sha256"}
        ).encode()
        return _FakeResp(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = api.hash("users/alice/hello.txt")
    assert got == sha
    # We asked WITHOUT content (content=0) and WITH hash=1 -> no body download.
    assert any("content=0" in u and "hash=1" in u for u in seen_urls)
    # ...and ALWAYS type=file, so a notebook-pairing ContentsManager (jupytext,
    # which treats .md/.py/.Rmd as notebooks) hashes the RAW bytes rather than a
    # converted-notebook model -- otherwise the server sha256 never matches the
    # local sha256 and the file is classified as a perpetual conflict.
    assert any("type=file" in u for u in seen_urls)


def test_hash_forces_type_file_for_notebook_paired_extensions(monkeypatch):
    """A jupytext server returns a notebook model (different bytes -> different
    sha) for a .md/.py file unless we force type=file. Regression: jp must force
    it so markdown/script files compare byte-faithfully and sync at all."""
    api = _api()
    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=None, context=None):
        seen_urls.append(req.full_url)
        return _FakeResp(json.dumps({"hash": "deadbeef", "hash_algorithm": "sha256"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api.hash("users/alice/notes.md")
    assert all("type=file" in u for u in seen_urls)


def test_stat_as_file_forces_type_file_but_plain_stat_does_not(monkeypatch):
    """``stat(as_file=True)`` (used by push's post-write size check) forces
    type=file so a jupytext server reports the RAW byte size, not the converted
    notebook size -- otherwise every markdown PUT trips a false size mismatch.
    Plain ``stat()`` must NOT force it: ``jp rm`` relies on it to tell a
    directory (type='directory') from a file."""
    api = _api()
    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=None, context=None):
        seen_urls.append(req.full_url)
        return _FakeResp(json.dumps({"type": "file", "size": 12, "path": "users/alice/x"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api.stat("users/alice/dir-or-file")
    assert all("type=file" not in u for u in seen_urls)  # dir detection preserved

    seen_urls.clear()
    api.stat("users/alice/notes.md", as_file=True)
    assert all("type=file" in u for u in seen_urls)


def test_hash_returns_none_when_server_gives_no_hash(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp(json.dumps({"hash": None, "hash_algorithm": None}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert api.hash("users/alice/x.txt") is None


def test_hash_returns_none_on_404(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b"does not exist")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert api.hash("users/alice/missing.txt") is None


# --------------------------------------------------------------------------- #
# 404 GET body is PLAIN TEXT -> defensive parsing must not raise
# --------------------------------------------------------------------------- #
def test_404_plain_text_body_does_not_crash_and_maps_to_apierror(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        # Real server: 404 body is plain text, NOT JSON.
        raise urllib.error.HTTPError(
            req.full_url,
            404,
            "Not Found",
            {"Content-Type": "text/html"},
            io.BytesIO(b"file or directory '/lapix/privado/x' does not exist"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # stat() swallows 404 -> None; the underlying parse must not have thrown.
    assert api.stat("users/alice/x") is None
    # A raw GET that 404s on a non-stat path surfaces ApiError(status=404),
    # and the plain-text body is carried through as detail without crashing.
    with pytest.raises(ApiError) as ei:
        api.get_file_bytes("users/alice/x")
    assert ei.value.status == 404


def test_error_detail_handles_empty_and_garbage_bodies(monkeypatch):
    api = _api()

    def make(body: bytes, code: int = 400, ctype: str = "application/json"):
        def fake_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(
                req.full_url, code, "Bad", {"Content-Type": ctype}, io.BytesIO(body)
            )

        return fake_urlopen

    # Empty body.
    monkeypatch.setattr(urllib.request, "urlopen", make(b""))
    with pytest.raises(ApiError):
        api.mkdir("users/alice/d")
    # Garbage "json-looking" body.
    monkeypatch.setattr(urllib.request, "urlopen", make(b"{not json at all"))
    with pytest.raises(ApiError):
        api.mkdir("users/alice/d2")


# --------------------------------------------------------------------------- #
# base64 download: trailing newline must be stripped before decode
# --------------------------------------------------------------------------- #
def test_get_file_bytes_strips_trailing_newline_from_base64(monkeypatch):
    api = _api()
    raw = bytes(range(256))  # binary, definitely non-UTF8
    b64 = base64.b64encode(raw).decode() + "\n"  # server appends a trailing \n

    def fake_urlopen(req, timeout=None, context=None):
        assert "type=file" in req.full_url  # byte-faithful read
        return _FakeResp(json.dumps({"format": "base64", "content": b64}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = api.get_file_bytes("users/alice/blob.bin")
    assert got == raw  # exact bytes recovered despite the trailing newline


def test_get_file_bytes_text_format(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp(json.dumps({"format": "text", "content": "hi\n"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert api.get_file_bytes("users/alice/a.txt") == b"hi\n"


# --------------------------------------------------------------------------- #
# notebooks: fetched as raw bytes (type=file), uploaded as base64
# --------------------------------------------------------------------------- #
def test_notebook_uploaded_as_base64_type_file(monkeypatch):
    api = _api()
    nb = b'{"cells": [], "metadata": {}, "nbformat": 4}\n'
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp(
            json.dumps({"type": "notebook", "path": "users/alice/n.ipynb"}).encode(),
            status=201,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = api.put_file("users/alice/n.ipynb", nb)
    assert captured["body"]["type"] == "file"  # NOT "notebook" -> byte fidelity
    assert captured["body"]["format"] == "base64"
    assert base64.b64decode(captured["body"]["content"]) == nb
    assert res.created is True  # 201


def test_put_text_for_utf8_and_reports_overwrite_with_200(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode())
        assert body["format"] == "text"  # UTF-8 content -> text format
        assert body["type"] == "file"
        return _FakeResp(b"{}", status=200)  # 200 = overwrote

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = api.put_file("users/alice/readme.txt", b"plain ascii text")
    assert res.created is False  # 200 -> updated, not created


def test_put_binary_uses_base64(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode())
        assert body["format"] == "base64"  # NUL byte -> not safe as text
        return _FakeResp(b"{}", status=201)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api.put_file("users/alice/x.bin", b"\x00\x01\x02")


# --------------------------------------------------------------------------- #
# health probe: must NOT follow redirects; 3xx -> /hub means server stopped
# --------------------------------------------------------------------------- #
class _FakeOpener:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def open(self, req, timeout=None):
        return self._behaviour(req)


def test_status_probe_up(monkeypatch):
    api = _api()

    def behaviour(req):
        return _FakeResp(json.dumps({"started": "now"}).encode(), status=200)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener(behaviour))
    res = api.status_probe()
    assert res.up is True


def test_status_probe_redirect_to_hub_is_server_down(monkeypatch):
    api = _api()

    def behaviour(req):
        # Stopped server: 302 with Location into /hub/ -- raised, NOT followed.
        raise urllib.error.HTTPError(
            req.full_url,
            302,
            "Found",
            {"Location": "/hub/user/alice/api/status"},
            io.BytesIO(b""),
        )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener(behaviour))
    with pytest.raises(ServerDownError) as ei:
        api.status_probe()
    msg = ei.value.message.lower()
    assert "start" in msg and "server" in msg  # actionable hint


def test_status_probe_403_is_auth_error(monkeypatch):
    api = _api()

    def behaviour(req):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"Forbidden"}')
        )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener(behaviour))
    with pytest.raises(AuthError):
        api.status_probe()


def test_status_probe_connection_error_is_network(monkeypatch):
    api = _api()

    def behaviour(req):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener(behaviour))
    with pytest.raises(NetworkError):
        api.status_probe()


# --------------------------------------------------------------------------- #
# delete: non-empty dir 400 gets a clearer, actionable message
# --------------------------------------------------------------------------- #
def test_delete_nonempty_dir_400_is_reraised_with_hint(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"message":"Directory /lapix/privado/sub not empty","reason":null}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ApiError) as ei:
        api.delete("users/alice/sub")
    assert "recursive" in ei.value.message.lower()


# --------------------------------------------------------------------------- #
# terminals: ephemeral PTY sessions (POST/DELETE /api/terminals only)
# --------------------------------------------------------------------------- #
def test_create_terminal_sends_cwd_and_returns_name(monkeypatch):
    api = _api()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp(json.dumps({"name": "1"}).encode(), status=200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sess = api.create_terminal(cwd="projetos/x")
    assert sess.name == "1"
    assert sess.cwd_applied is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/terminals")
    assert captured["body"] == {"cwd": "projetos/x"}


def test_create_terminal_falls_back_when_server_rejects_cwd(monkeypatch):
    api = _api()
    calls: list[dict | None] = []

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode()) if req.data else None
        calls.append(body)
        # First call carries cwd and is rejected (older server -> 500); the
        # retry with no body succeeds.
        if body is not None and "cwd" in body:
            raise urllib.error.HTTPError(
                req.full_url, 500, "Server Error", {}, io.BytesIO(b"unexpected kwarg cwd")
            )
        return _FakeResp(json.dumps({"name": "7"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sess = api.create_terminal(cwd="projetos/x")
    assert sess.name == "7"
    assert sess.cwd_applied is False  # caller must cd manually
    assert calls == [{"cwd": "projetos/x"}, None]  # tried cwd, then retried bare


def test_create_terminal_disabled_propagates(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        # Terminals disabled on the server -> 404, must NOT be swallowed/retried.
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b"No such handler")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ApiError) as ei:
        api.create_terminal(cwd="projetos/x")
    assert ei.value.status == 404


def test_create_terminal_403_is_auth_error(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"Forbidden"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AuthError):
        api.create_terminal(cwd="projetos/x")


def test_delete_terminal_uses_name_and_tolerates_404(monkeypatch):
    api = _api()
    seen: list[tuple[str, str]] = []

    def fake_urlopen(req, timeout=None, context=None):
        seen.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/api/terminals/missing"):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"gone"))
        return _FakeResp(b"", status=204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api.delete_terminal("3")
    api.delete_terminal("missing")  # 404 tolerated, no raise
    assert seen[0] == ("DELETE", "https://hub.example/user/alice/api/terminals/3")


def test_terminal_ws_url_derivation():
    api = _api()  # base_url = https://hub.example/user/alice
    assert api.terminal_ws_url("1") == "wss://hub.example/user/alice/terminals/websocket/1"
    # http -> ws, and a stray trailing /api is stripped. (No token over http,
    # so the constructor's cleartext-credential guard is satisfied with "".)
    plain = Api("http://localhost:8888/api", "")
    assert plain.terminal_ws_url("ab") == "ws://localhost:8888/terminals/websocket/ab"


# --------------------------------------------------------------------------- #
# Cleartext-token guard + the _allow_http_localhost loopback escape hatch.
# --------------------------------------------------------------------------- #
def test_http_token_refused_by_default():
    # A token over plain http:// is refused regardless of host.
    with pytest.raises(AuthError):
        Api("http://example.com:8888", "secret-token")
    with pytest.raises(AuthError):
        Api("http://127.0.0.1:8888", "secret-token")  # not opted in


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_http_token_allowed_on_loopback_when_opted_in(host):
    # Loopback http+token is permitted ONLY with the private opt-in. Loopback
    # never traverses a network, so a cleartext token there cannot be sniffed.
    api = Api(f"http://{host}:8888", "secret-token", _allow_http_localhost=True)
    assert api.base_url == f"http://{host}:8888"


@pytest.mark.parametrize("host", ["example.com", "10.0.0.5", "192.168.1.1", "8.8.8.8"])
def test_http_token_still_refused_for_nonloopback_even_when_opted_in(host):
    # The opt-in must NOT weaken the guard for a real network address: a token
    # over http to a non-loopback host is still refused.
    with pytest.raises(AuthError):
        Api(f"http://{host}:8888", "secret-token", _allow_http_localhost=True)


def test_loopback_opt_in_does_not_affect_https():
    # https is always fine with a token, opt-in or not.
    api = Api("https://hub.example/user/alice", "secret-token", _allow_http_localhost=True)
    assert api.base_url == "https://hub.example/user/alice"


# --------------------------------------------------------------------------- #
# create_checkpoint: cheap server-side undo before a remote overwrite
# --------------------------------------------------------------------------- #
def test_create_checkpoint_posts_and_returns_id(monkeypatch):
    api = _api()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _FakeResp(json.dumps({"id": "checkpoint", "last_modified": "now"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cid = api.create_checkpoint("users/alice/hello.txt")
    assert cid == "checkpoint"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/contents/users/alice/hello.txt/checkpoints")


def test_create_checkpoint_returns_empty_when_unavailable(monkeypatch):
    api = _api()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", {}, io.BytesIO(b"no checkpoints")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert api.create_checkpoint("users/alice/hello.txt") == ""


# --------------------------------------------------------------------------- #
# kernels: ephemeral compute sessions (POST/DELETE/GET /api/kernels)
# --------------------------------------------------------------------------- #
def test_kernel_ws_url_uses_wss_and_api_path():
    # base_url has NO trailing /api
    api = Api("https://hub.example/user/alice", token="tkn-secret-xyz")
    url = api.kernel_ws_url("KID")
    assert url == "wss://hub.example/user/alice/api/kernels/KID/channels"
    assert "token" not in url  # never in the URL


def test_create_kernel_posts_and_returns_id(monkeypatch):
    import jp.api as apimod

    class _Resp:
        status = 201
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"id":"KID","name":"python3"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(apimod.urllib.request, "urlopen", fake_urlopen)

    api = Api("https://hub.example/user/alice", token="tkn-secret-xyz")  # NO trailing /api
    assert api.create_kernel() == "KID"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://hub.example/user/alice/api/kernels"  # exact, single /api


def test_question_mark_in_a_file_name_is_part_of_the_path():
    api = Api("https://hub.example/user/me", "tok")
    assert api._url("api/contents/a?b.bin").endswith("/api/contents/a%3Fb.bin")
    assert api._url("api/contents/a?b.bin?content=0&type=file").endswith(
        "/api/contents/a%3Fb.bin?content=0&type=file"
    )
