"""Thin JupyterHub / Jupyter-server Contents API client over stdlib urllib.

Security rules (see docs/architecture.md):
  * Authorization header ONLY -- the token is never put in the URL/query string.
  * TLS is always verified (a real, default ``ssl`` context). We never disable
    certificate checks.
  * We refuse to send a token over plain ``http://`` (no cleartext credentials).
  * Errors raised here are mapped to NetworkError/ApiError; the CLI passes their
    messages through ``ui.redact`` so a token (and absolute server paths) can
    never leak via an error.

Empirical behaviour baked in (research/01-jupyter-api.md, JupyterHub 5.2.1):
  * ``GET <file>?content=0&hash=1`` returns the **sha256** of the raw bytes
    WITHOUT downloading the body (cheap even at 20 MiB). :meth:`hash` uses this
    as the primary remote change-detection signal so we only download when the
    remote hash differs from the local sha256.
  * PUT auto-creates parent directories (``mkdir -p``); PATCH does NOT.
  * PUT returns 201 (created) vs 200 (overwrote) -- surfaced as ``created``.
  * ``GET`` 404 bodies are **plain text** (other errors are JSON) -> the error
    parser is defensive and never raises while parsing.
  * ``format=text`` does NOT reject binary; jp decides text-vs-binary itself.
    Downloads always force ``type=file`` (byte-faithful, incl. ``.ipynb``) and
    base64 ``content`` carries a trailing newline that must be stripped.
  * Health probe: ``GET /api/status`` must NOT follow redirects; a 3xx to
    ``/hub/...`` means the server is stopped (-> :class:`ServerDownError`).

This module performs raw transport only; it does NOT enforce the path-jail and
does NOT know the workspace prefix. The path-jail lives ENTIRELY in the callers
(``sync.py`` / ``commands/rm.py``): they MUST call ``paths.remote_path_for`` to
compose a remote path and ``paths.assert_within_prefix`` immediately before
invoking any mutating method here (put/mkdir/rename/delete).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any

from . import ui
from .errors import ApiError, AuthError, NetworkError, ServerDownError

_DEFAULT_TIMEOUT = 30.0

# Warn before a single-request upload this big. Push and pull never get here, they
# use transfer.py; this guards the callers that PUT in one go, such as `jp run`.
_LARGE_FILE_WARN_BYTES = 50 * 1024 * 1024  # ~50 MiB

# Extensions we treat as opaque bytes even though the server reports them as a
# "notebook" model. Forcing type=file keeps the bytes faithful (research §11).
_NOTEBOOK_EXTS = (".ipynb",)

_STREAM_READ_BYTES = 1024 * 1024
_JP_QUERY = re.compile(r"\?(?:content|type|format|hash)=[^?/]*\Z")


@dataclass
class RemoteEntry:
    """One item from a Contents API directory listing."""

    name: str
    path: str
    type: str  # "file" | "directory" | "notebook"
    size: int | None
    last_modified: str
    mimetype: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that NEVER follows 3xx -- it re-raises the response.

    We need this for the health probe: a stopped JupyterHub server answers
    ``/api/status`` with a 302 into ``/hub/...``. urllib follows redirects by
    default, which would hide that signal, so we install a dedicated opener that
    surfaces the 3xx as an :class:`urllib.error.HTTPError` instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # do not build a follow-up request

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


@dataclass
class StatusResult:
    """Outcome of the health probe (see :meth:`Api.status_probe`)."""

    up: bool
    detail: str = ""


@dataclass
class TerminalSession:
    """A freshly created remote terminal (see :meth:`Api.create_terminal`)."""

    name: str
    # True when the server honoured the requested ``cwd`` natively. False means
    # an older server ignored/rejected it and the caller should ``cd`` manually.
    cwd_applied: bool


def _is_notebook_path(api_path: str) -> bool:
    name = api_path.rsplit("/", 1)[-1].lower()
    return name.endswith(_NOTEBOOK_EXTS)


def _looks_utf8_text(data: bytes) -> bool:
    """True if ``data`` is safe to send as a UTF-8 ``format=text`` payload.

    Embedded NULs or non-UTF-8 bytes mean we must use base64 instead -- the
    server happily stores raw bytes in a text field (no 400), corrupting them.
    """
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_loopback_host(host: str | None) -> bool:
    """True only for the loopback interface (``localhost`` or a 127/8 / ::1 IP).

    Used to permit a token over plain ``http://`` for a strictly local server.
    Anything that is not provably loopback (including a missing host) is False,
    so the cleartext-token guard stays in force for every real network address.
    """
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Api:
    """Contents API client. One instance per repo/session."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
        large_file_warn_bytes: int = _LARGE_FILE_WARN_BYTES,
        _allow_http_localhost: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.large_file_warn_bytes = large_file_warn_bytes
        self._warned_large: set[str] = set()
        # Always a verifying context. We never pass an unverified one.
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
            # Require TLS 1.2+ (a default context still permits broken 1.0/1.1).
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_context = ssl_context
        ui.register_secret(token)

        split = urllib.parse.urlsplit(self.base_url)
        scheme = split.scheme
        if scheme not in ("http", "https"):
            raise NetworkError(f"unsupported base_url scheme: {scheme!r}")
        if scheme == "http" and token:
            # Never transmit credentials over cleartext -- UNLESS the host is the
            # loopback interface AND the caller explicitly opted in. Loopback
            # traffic never leaves the machine (no network to sniff), so a token
            # over http://127.0.0.1 is genuinely safe; this exists solely so a
            # local throwaway jupyter_server can be exercised in tests. The guard
            # is private (leading underscore) and refuses any non-loopback host.
            if _allow_http_localhost and _is_loopback_host(split.hostname):
                pass
            else:
                raise AuthError("refusing to send a token over plain http:// -- use https://")

    # --- low-level request --------------------------------------------------
    def _url(self, api_path: str) -> str:
        # Contents API lives under /user/<name>/api/contents OR /api/contents;
        # base_url is expected to already include the api root. The token is
        # NEVER appended here.
        #
        # CRITICAL: split off the query string FIRST. We percent-encode only the
        # path segments (keeping '/' raw) and leave the query (?content=0&hash=1)
        # verbatim -- otherwise '?', '=' and '&' get encoded too and the server
        # treats the whole thing as a literal filename, silently ignoring every
        # query parameter (content/hash/type/format).
        #
        # Only a query jp itself appended counts: a '?' earlier in the string belongs
        # to the file name.
        raw = api_path.lstrip("/")
        matched = _JP_QUERY.search(raw)
        path_part = raw[: matched.start()] if matched else raw
        query = matched.group() if matched else ""
        quoted = urllib.parse.quote(path_part, safe="/")
        return f"{self.base_url}/{quoted}{query}"

    def _request(
        self,
        method: str,
        api_path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        url = self._url(api_path)
        data = None
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self.timeout, context=self._ssl_context
            ) as resp:
                payload = resp.read()
                if not payload:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype or payload[:1] in (b"{", b"["):
                    return json.loads(payload.decode("utf-8"))
                return payload
        except urllib.error.HTTPError as exc:
            self._raise_for_http(exc)
        except urllib.error.URLError as exc:
            # Includes TLS verification failures and DNS/connection errors.
            raise NetworkError(f"network error talking to server: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"network error: {exc}") from exc
        return None

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        """Best-effort, never-raising extraction of a server error message.

        404 bodies on this server are PLAIN TEXT; most other errors are JSON
        ``{"message":...,"reason":...}``. We try JSON first, fall back to text,
        and swallow everything so a malformed body cannot crash error handling.
        Note: messages may leak an absolute ``/lapix/...`` path -- ``ui.redact``
        masks those at the output boundary.
        """
        try:
            raw = exc.read()
        except Exception:
            return ""
        if not raw:
            return ""
        text = raw.decode("utf-8", "replace")
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                return text[:200]
            if isinstance(parsed, dict):
                return str(parsed.get("message") or parsed.get("reason") or "")[:200]
            return text[:200]
        # Plain-text body (e.g. the 404 case).
        return text[:200]

    def _raise_for_http(self, exc: urllib.error.HTTPError) -> None:
        status = exc.code
        # Try to extract a server message, but never echo headers (which carry
        # the Authorization we sent), and never raise while parsing.
        detail = self._error_detail(exc)
        msg = f"server returned HTTP {status}"
        if detail:
            msg += f": {detail}"
        if status in (301, 302, 303, 307, 308):
            raise ServerDownError()
        if status in (401, 403):
            raise AuthError(msg)
        raise ApiError(msg, status=status)

    # --- read operations ----------------------------------------------------
    def list_dir(self, api_path: str) -> list[RemoteEntry]:
        """List a remote directory. Returns [] if it does not exist (404).

        The listing carries only ``name/type/size/last_modified`` per child --
        NO ``hash``/``content`` (research §1) -- so it is purely the cheap first
        pass; per-file equality uses :meth:`hash`.
        """
        try:
            data = self._request("GET", f"api/contents/{api_path}?content=1")
        except ApiError as exc:
            if exc.status == 404:
                return []
            raise
        if not isinstance(data, dict):
            return []
        if data.get("type") != "directory":
            return [self._to_entry(data)]
        out: list[RemoteEntry] = []
        for item in data.get("content") or []:
            if isinstance(item, dict):
                out.append(self._to_entry(item))
        return out

    def stat(self, api_path: str, *, as_file: bool = False) -> RemoteEntry | None:
        """Stat a single remote path without fetching content. None if missing.

        ``as_file=True`` forces ``type=file`` so a notebook-pairing
        ContentsManager (jupytext treats ``.md``/``.py``/``.Rmd``/... as
        notebooks) reports the RAW byte ``size`` rather than the size of the
        converted-notebook model. Callers that must distinguish a directory from
        a file (``jp rm``) leave it False -- forcing type=file would both hide
        the ``directory`` type and 400 on an actual directory.
        """
        query = "content=0&type=file" if as_file else "content=0"
        try:
            data = self._request("GET", f"api/contents/{api_path}?{query}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(data, dict):
            return self._to_entry(data)
        return None

    def hash(self, api_path: str, *, timeout: float | None = None) -> str | None:
        """Return the server-computed **sha256** of a file WITHOUT downloading it.

        Uses ``?content=0&hash=1`` -- the cheapest, most definitive remote
        change-detection signal (research §1: identical to local ``shasum -a
        256``, ~0.1 s even at 20 MiB). Returns None if the path is missing
        (404), is a directory, or the server did not populate a sha256 hash
        (older servers). Callers fall back to a content download in that case.

        We force ``type=file`` (like :meth:`get_file_bytes`) so the hash is taken
        over the RAW bytes. Without it, a notebook-pairing ContentsManager
        (jupytext, which by default treats ``.md``/``.py``/``.R``/``.Rmd``/...
        as notebooks) would hash the converted-notebook model instead, so the
        server sha256 would never equal the local sha256 of the raw file and the
        file would be misclassified as a perpetual conflict -- silently breaking
        push/pull for every jupytext-paired extension (the ``.md`` sync bug).
        """
        try:
            data = self._request(
                "GET", f"api/contents/{api_path}?content=0&hash=1&type=file", timeout=timeout
            )
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        algo = data.get("hash_algorithm")
        digest = data.get("hash")
        if digest and (algo is None or str(algo).lower() == "sha256"):
            return str(digest)
        return None

    def get_file_bytes(self, api_path: str) -> bytes:
        """Download a file's raw bytes, byte-faithfully.

        We force ``type=file`` for EVERY path (including ``.ipynb``) so the
        server treats the API as a plain byte store and does not normalize a
        notebook through its JSON model (research §11). base64 ``content`` from
        the server carries a trailing newline which we strip before decoding.
        """
        data = self._request("GET", f"api/contents/{api_path}?content=1&type=file")
        if not isinstance(data, dict):
            raise ApiError(f"unexpected response fetching {api_path}")
        fmt = data.get("format")
        content = data.get("content")
        if fmt == "base64":
            # The server appends a trailing "\n" to base64 content; strip ALL
            # ASCII whitespace before decoding so we recover the exact bytes.
            b64 = "".join(str(content or "").split())
            return base64.b64decode(b64)
        if fmt == "text":
            return str(content or "").encode("utf-8")
        if fmt == "json" or data.get("type") == "notebook":
            # Should not happen now that we force type=file, but stay defensive:
            # re-serialize a parsed model rather than crash.
            return json.dumps(content, indent=1).encode("utf-8")
        if content is None:
            return b""
        return str(content).encode("utf-8")

    def iter_file(
        self, api_path: str, offset: int = 0, end: int | None = None
    ) -> tuple[bool, Iterator[bytes]]:
        """Stream raw bytes from ``/files``, without base64 and without buffering.

        Asks for bytes ``offset`` up to, not including, ``end``. Returns ``(ranged,
        chunks)``: ``ranged`` is False when the server ignored the ``Range`` and is
        sending the whole file. Redirects stay unfollowed, because a stopped server
        answers with its HTML login page and a 200.
        """
        headers = {"Authorization": f"token {self._token}"}
        if offset or end is not None:
            last = "" if end is None else end - 1
            headers["Range"] = f"bytes={offset}-{last}"
        req = urllib.request.Request(self._url(f"files/{api_path}"), headers=headers)
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=self._ssl_context)
        )
        try:
            resp = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            self._raise_for_http(exc)
            raise
        except urllib.error.URLError as exc:
            raise NetworkError(f"network error talking to server: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"network error: {exc}") from exc
        return resp.status == 206, self._read_stream(resp)

    @staticmethod
    def _read_stream(resp: Any) -> Iterator[bytes]:
        with resp:
            while True:
                try:
                    chunk = resp.read(_STREAM_READ_BYTES)
                except (HTTPException, OSError) as exc:
                    raise NetworkError(f"download interrupted: {exc}") from exc
                if not chunk:
                    return
                yield chunk

    # --- write operations (caller MUST have asserted within prefix) ---------
    def put_file_bytes(self, api_path: str, data: bytes) -> RemoteEntry:
        """Upload raw bytes to ``api_path`` byte-faithfully (type=file).

        Picks ``format=text`` for UTF-8 content (smaller, diffable) and
        ``format=base64`` for binary/non-UTF-8 -- jp decides client-side because
        the server does NOT reject binary sent as text (research §1, §11).
        Parent dirs are auto-created by the server on PUT. Callers that need the
        create-vs-update (201 vs 200) signal should use :meth:`put_file`, which
        this method delegates to.
        """
        return self.put_file(api_path, data).entry

    def put_file(self, api_path: str, data: bytes) -> PutResult:
        """Like :meth:`put_file_bytes` but also reports create-vs-update.

        Returns a :class:`PutResult` whose ``created`` is True on HTTP 201 (new
        file) and False on HTTP 200 (overwrote) -- the empirical create/update
        signal (research §2).
        """
        self._warn_if_large(api_path, len(data))
        if _looks_utf8_text(data) and not _is_notebook_path(api_path):
            body = {
                "type": "file",
                "format": "text",
                "content": data.decode("utf-8"),
            }
        else:
            # Notebooks and any non-UTF-8 bytes go up as base64 for fidelity.
            body = {
                "type": "file",
                "format": "base64",
                "content": base64.b64encode(data).decode("ascii"),
            }
        created, resp = self._request_status("PUT", f"api/contents/{api_path}", body=body)
        if isinstance(resp, dict):
            entry = self._to_entry(resp)
        else:
            entry = RemoteEntry(
                name=api_path.rsplit("/", 1)[-1],
                path=api_path,
                type="file",
                size=len(data),
                last_modified="",
            )
        return PutResult(entry=entry, created=created)

    def put_chunk(self, api_path: str, data: bytes, number: int) -> None:
        """Send one piece of a chunked upload.

        The server's numbering: 1 replaces the file, -1 appends and closes the
        upload, anything else appends. Appending is not idempotent, so a caller
        must learn the real size before resending (see transfer.py).
        """
        body = {
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "chunk": number,
        }
        self._request("PUT", f"api/contents/{api_path}", body=body)

    def _warn_if_large(self, api_path: str, nbytes: int) -> None:
        if nbytes >= self.large_file_warn_bytes and api_path not in self._warned_large:
            self._warned_large.add(api_path)
            mb = nbytes / (1024 * 1024)
            ui.warn(
                f"{api_path}: {mb:.0f} MiB will be sent as one in-memory request "
                "(no chunking; base64 inflates ~33%); this may be slow or time out."
            )

    def _request_status(
        self, method: str, api_path: str, body: dict[str, Any] | None = None
    ) -> tuple[bool, Any]:
        """Like :meth:`_request` but also returns whether the status was 201.

        Used by PUT to distinguish 201 (created) from 200 (overwrote). We read
        the status off the response object; on any non-2xx the normal error path
        in :meth:`_request` would have raised, so we only reach here for 2xx.
        """
        url = self._url(api_path)
        data = None
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout, context=self._ssl_context
            ) as resp:
                created = resp.status == 201
                payload = resp.read()
                if not payload:
                    return created, None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype or payload[:1] in (b"{", b"["):
                    return created, json.loads(payload.decode("utf-8"))
                return created, payload
        except urllib.error.HTTPError as exc:
            self._raise_for_http(exc)
        except urllib.error.URLError as exc:
            raise NetworkError(f"network error talking to server: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"network error: {exc}") from exc
        return False, None

    def mkdir(self, api_path: str) -> None:
        """Create a remote directory (idempotent-ish: 409/exists is tolerated).

        Mostly defensive now -- PUT auto-creates parents (research §2). PATCH
        does NOT, so callers that move into a new dir must mkdir it first.
        """
        try:
            self._request("PUT", f"api/contents/{api_path}", body={"type": "directory"})
        except ApiError as exc:
            if exc.status in (409,):
                return
            raise

    def rename(self, src_path: str, dst_path: str) -> RemoteEntry:
        """Rename/move ``src_path`` to ``dst_path``.

        PATCH does NOT auto-create the destination's parent (500 if missing) and
        does NOT overwrite an existing target (409), so we mkdir the parent
        first (research §3).
        """
        parent = dst_path.rsplit("/", 1)[0] if "/" in dst_path else ""
        if parent:
            self.mkdir(parent)
        resp = self._request("PATCH", f"api/contents/{src_path}", body={"path": dst_path})
        if isinstance(resp, dict):
            return self._to_entry(resp)
        return RemoteEntry(
            name=dst_path.rsplit("/", 1)[-1],
            path=dst_path,
            type="file",
            size=None,
            last_modified="",
        )

    def delete(self, api_path: str) -> None:
        """Delete a remote path: ``jp rm`` (gated), mirror mode after confirmation,
        and jp's own temporary files.

        DELETE is NOT recursive on this server: a non-empty directory returns
        400 "not empty" (research §5). ``jp rm --recursive`` walks bottom-up.
        We re-raise the 400 with a clearer, actionable message.
        """
        try:
            self._request("DELETE", f"api/contents/{api_path}")
        except ApiError as exc:
            if exc.status == 404:
                return
            if exc.status == 400 and "not empty" in exc.message.lower():
                raise ApiError(
                    f"refusing to delete non-empty directory {api_path!r}: "
                    "the server is not recursive. Use 'jp rm --recursive' instead.",
                    status=400,
                ) from exc
            raise

    def create_checkpoint(self, api_path: str) -> str:
        """POST a server-side checkpoint of a file (cheap undo before overwrite).

        Research §3.6: 1 checkpoint per file (id 'checkpoint'); restore reverts.
        NOTE: this is a building block and is NOT yet wired into the live write
        path -- `jp live --writable` does not call it, so writable overwrites have
        no automatic undo. Returns the checkpoint id (or '' if unavailable).
        """
        try:
            data = self._request("POST", f"api/contents/{api_path}/checkpoints")
        except ApiError:
            return ""
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        return ""

    # --- health probe -------------------------------------------------------
    def status_probe(self) -> StatusResult:
        """Probe ``GET /api/status`` WITHOUT following redirects.

        Decision table (research §8):
          * 200            -> server up.
          * 403 (JSON)     -> server up, token invalid/expired (AuthError).
          * 3xx -> /hub/.. -> server stopped/not routed (ServerDownError).
          * connection err -> network/Hub down (NetworkError).
        """
        url = f"{self.base_url}/api/status"
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler())
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"token {self._token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return StatusResult(up=resp.status == 200, detail="reached /api/status")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "") if exc.headers else ""
                if "/hub/" in location or "/hub" in location:
                    raise ServerDownError() from exc
                raise ServerDownError(
                    f"unexpected redirect from /api/status to {location!r}; "
                    "the server may be stopped -- start it from the JupyterHub UI."
                ) from exc
            if exc.code in (401, 403):
                raise AuthError(
                    "the server rejected the token (HTTP 403). "
                    "Refresh your token via the JupyterHub UI and run 'jp login'."
                ) from exc
            self._raise_for_http(exc)
        except urllib.error.URLError as exc:
            raise NetworkError(f"could not reach the server: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"could not reach the server: {exc}") from exc
        return StatusResult(up=False, detail="unknown")

    # --- terminals (ephemeral PTY sessions; NEVER touch the Contents API) ----
    def create_terminal(self, cwd: str | None = None) -> TerminalSession:
        """Create a remote terminal via ``POST /api/terminals``; return its name.

        A terminal is an ephemeral PTY session, not a path, so the path-jail does
        not apply to this call. Whatever runs inside has to confine itself: the
        archive scripts in bundle.py check every path they touch.

        When ``cwd`` is given we request it natively (modern
        ``jupyter_server_terminals`` resolves it relative to the server root,
        which is exactly what the workspace prefix is relative to). An older
        server that does not understand the field answers 400/500; we then retry
        with no body and report ``cwd_applied=False`` so the caller can fall back
        to a manual ``cd``. Terminals being disabled (401/403 -> AuthError, or
        404 -> ApiError) propagates unchanged.
        """
        if cwd:
            try:
                data = self._request("POST", "api/terminals", body={"cwd": cwd})
                return TerminalSession(self._terminal_name(data), cwd_applied=True)
            except ApiError as exc:
                # 400/500 most likely means the server rejected the cwd field;
                # retry without it. Anything else (404 disabled, etc.) propagates.
                if exc.status not in (400, 500):
                    raise
        data = self._request("POST", "api/terminals")
        return TerminalSession(self._terminal_name(data), cwd_applied=False)

    def delete_terminal(self, name: str) -> None:
        """Delete a terminal session by name (idempotent: 404 is tolerated).

        The caller passes ONLY the name it created itself, so this can never
        affect another user's or another session's terminal.
        """
        try:
            self._request("DELETE", f"api/terminals/{name}")
        except ApiError as exc:
            if exc.status == 404:
                return
            raise

    def terminal_ws_url(self, name: str) -> str:
        """Derive the ``wss://.../terminals/websocket/<name>`` URL.

        ``base_url`` is the single-user server root; the token is NEVER put in
        the URL (it travels in the Authorization header on the handshake). A
        trailing ``/api`` is stripped defensively for back-compat with configs
        that stored it.
        """
        server = self.base_url
        if server.endswith("/api"):
            server = server[:-4]
        server = server.rstrip("/")
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            raise NetworkError(f"unsupported base_url scheme for websocket: {server!r}")
        return f"{ws_base}/terminals/websocket/{name}"

    @staticmethod
    def _terminal_name(data: Any) -> str:
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
        raise ApiError("server did not return a terminal name")

    # --- kernels (ephemeral compute; used by `jp live`) ---------------------
    def create_kernel(self, name: str = "python3") -> str:
        """Start a kernel via ``POST /api/kernels``; return its id.

        Like terminals, a kernel is not a path -- the path-jail does not apply
        to its lifecycle. jp tracks the returned id and deletes ONLY that id.
        """
        data = self._request("POST", "api/kernels", body={"name": name})
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        raise ApiError("server did not return a kernel id")

    def kernel_alive(self, kernel_id: str) -> bool:
        """True if ``GET /api/kernels/<id>`` is 200; False on 404."""
        try:
            self._request("GET", f"api/kernels/{kernel_id}")
            return True
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise

    def delete_kernel(self, kernel_id: str) -> None:
        """Delete a kernel by id (idempotent: 404 tolerated). Frees the GPU."""
        try:
            self._request("DELETE", f"api/kernels/{kernel_id}")
        except ApiError as exc:
            if exc.status == 404:
                return
            raise

    def kernel_ws_url(self, kernel_id: str) -> str:
        """Derive ``wss://.../api/kernels/<id>/channels``. Token never in URL.

        ``base_url`` is the single-user server root WITHOUT a trailing ``/api``
        (same convention as ``terminal_ws_url``). We swap the scheme and
        prepend the ``api/`` segment explicitly so the websocket path is
        correct on the real server.
        """
        server = self.base_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            raise NetworkError(f"unsupported base_url scheme for websocket: {server!r}")
        return f"{ws_base.rstrip('/')}/api/kernels/{kernel_id}/channels"

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _to_entry(item: dict[str, Any]) -> RemoteEntry:
        return RemoteEntry(
            name=str(item.get("name", "")),
            path=str(item.get("path", "")),
            type=str(item.get("type", "")),
            size=item.get("size"),
            last_modified=str(item.get("last_modified", "")),
            mimetype=item.get("mimetype"),
        )


@dataclass
class PutResult:
    """Result of a PUT: the resulting entry plus the create-vs-update signal."""

    entry: RemoteEntry
    created: bool  # True on HTTP 201 (new file); False on HTTP 200 (overwrote)
