"""Shared pytest fixtures and a FakeApi that mocks the remote Contents API.

The FakeApi mimics jp.api.Api's public surface over an in-memory dict so tests
never touch the network or a real server. It records every mutating call so we
can assert no-delete / path-jail invariants.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

# Make 'src' importable without installation.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jp.api import PutResult, RemoteEntry, StatusResult  # noqa: E402
from jp.config import Config  # noqa: E402
from jp.ignore import IgnoreSet  # noqa: E402
from jp.index import Index  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redaction_secrets():
    """Isolate the global ``ui._KNOWN_SECRETS`` registry between tests.

    ``ui.register_secret`` (called by ``Api.__init__``) adds to a module-level
    set with no reset, so a secret registered by one test would mask that string
    from every later test's output -- an order-dependent failure. Snapshot and
    restore the set around each test.
    """
    from jp import ui

    saved = set(ui._KNOWN_SECRETS)
    try:
        yield
    finally:
        ui._KNOWN_SECRETS.clear()
        ui._KNOWN_SECRETS.update(saved)


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """Isolate every test from the real ``~/.config/jp`` credential store.

    ``os.path.expanduser("~")`` resolves via ``USERPROFILE`` on Windows and
    ``HOME`` elsewhere, so both are pointed at ``tmp_path``. Env tokens are
    cleared too, so a developer's ``JP_TOKEN`` never bleeds into a test run.

    Without this, a credential written by one test lands in the runner's real
    profile and trips later tests -- the Windows-only "credential already
    exists" / "multiple saved credentials" failures seen in CI.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)


class FakeApi:
    """In-memory stand-in for jp.api.Api.

    Storage maps absolute api paths (e.g. ``users/alice/foo.txt``) to bytes.
    Directories are implicit. Records mutating calls in ``self.calls``.
    """

    def __init__(self, base_url: str = "https://hub.example/api", token: str = "tok"):
        self.base_url = base_url
        self._token = token
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.calls: list[tuple[str, str]] = []  # (method, path)
        # Reads that fetch a body. Lets tests assert we did NOT download when the
        # cheap hash() was enough to decide equality.
        self.content_reads: list[str] = []
        self.hash_reads: list[str] = []
        # Health-probe scripting: set to "up" | "auth" | "down" | "network".
        self.status_mode: str = "up"

    # --- seeding helpers ----------------------------------------------------
    def seed(self, api_path: str, data: bytes, mtime: str = "2024-01-01T00:00:00Z") -> None:
        self.files[api_path] = data
        self.mtimes[api_path] = mtime
        parts = api_path.split("/")
        for i in range(1, len(parts)):
            self.dirs.add("/".join(parts[:i]))

    # --- read ops -----------------------------------------------------------
    def list_dir(self, api_path: str) -> list[RemoteEntry]:
        api_path = api_path.rstrip("/")
        entries: list[RemoteEntry] = []
        seen_dirs: set[str] = set()
        base = api_path + "/"
        for path in sorted(self.files):
            if not path.startswith(base):
                continue
            rest = path[len(base) :]
            if "/" in rest:
                top = rest.split("/", 1)[0]
                dpath = base + top
                if dpath not in seen_dirs:
                    seen_dirs.add(dpath)
                    entries.append(
                        RemoteEntry(
                            name=top, path=dpath, type="directory", size=None, last_modified=""
                        )
                    )
            else:
                entries.append(
                    RemoteEntry(
                        name=rest,
                        path=path,
                        type="file",
                        size=len(self.files[path]),
                        last_modified=self.mtimes.get(path, ""),
                    )
                )
        return entries

    def stat(self, api_path: str, *, as_file: bool = False) -> RemoteEntry | None:
        if api_path in self.files:
            return RemoteEntry(
                name=api_path.rsplit("/", 1)[-1],
                path=api_path,
                type="file",
                size=len(self.files[api_path]),
                last_modified=self.mtimes.get(api_path, ""),
            )
        if api_path in self.dirs:
            return RemoteEntry(
                name=api_path.rsplit("/", 1)[-1],
                path=api_path,
                type="directory",
                size=None,
                last_modified="",
            )
        return None

    def get_file_bytes(self, api_path: str) -> bytes:
        self.content_reads.append(api_path)  # records a real body download
        if api_path not in self.files:
            from jp.errors import ApiError

            raise ApiError(f"not found: {api_path}", status=404)
        return self.files[api_path]

    def iter_file(self, api_path: str, offset: int = 0, end: int | None = None):
        """Raw streaming read. Returns (ranged, chunks) like Api.iter_file."""
        self.content_reads.append(api_path)
        if api_path not in self.files:
            from jp.errors import ApiError

            raise ApiError(f"not found: {api_path}", status=404)
        body = self.files[api_path][offset:end]
        ranged = offset > 0 or end is not None
        return ranged, iter([body[i : i + 1024] for i in range(0, len(body), 1024)])

    def hash(self, api_path: str, *, timeout: float | None = None) -> str | None:
        """Cheap server-side sha256 (content=0&hash=1) -- no body download."""
        self.hash_reads.append(api_path)
        if api_path not in self.files:
            return None
        return hashlib.sha256(self.files[api_path]).hexdigest()

    # --- write ops ----------------------------------------------------------
    def put_file(self, api_path: str, data: bytes) -> PutResult:
        created = api_path not in self.files
        entry = self.put_file_bytes(api_path, data)
        return PutResult(entry=entry, created=created)

    def put_file_bytes(self, api_path: str, data: bytes) -> RemoteEntry:
        self.calls.append(("PUT", api_path))
        # Mimic the server rejecting hidden uploads (allow_hidden=False).
        if any(part.startswith(".") for part in api_path.split("/")):
            from jp.errors import ApiError

            raise ApiError(f"hidden path rejected: {api_path}", status=400)
        self.files[api_path] = data
        self.mtimes[api_path] = "2024-06-01T00:00:00Z"
        parts = api_path.split("/")
        for i in range(1, len(parts)):
            self.dirs.add("/".join(parts[:i]))
        return self.stat(api_path)  # type: ignore[return-value]

    def put_chunk(self, api_path: str, data: bytes, number: int) -> None:
        """Mirror LargeFileManager: chunk 1 replaces the file, the others append."""
        self.calls.append(("CHUNK", f"{api_path}#{number}"))
        self.files[api_path] = data if number == 1 else self.files[api_path] + data
        self.mtimes[api_path] = "2024-06-01T00:00:00Z"

    def mkdir(self, api_path: str) -> None:
        self.calls.append(("MKDIR", api_path))
        if any(part.startswith(".") for part in api_path.split("/")):
            from jp.errors import ApiError

            raise ApiError(f"hidden dir rejected: {api_path}", status=400)
        self.dirs.add(api_path)

    def rename(self, src_path: str, dst_path: str) -> RemoteEntry:
        self.calls.append(("RENAME", f"{src_path}->{dst_path}"))
        data = self.files.pop(src_path, b"")
        mt = self.mtimes.pop(src_path, "")
        self.files[dst_path] = data
        self.mtimes[dst_path] = mt or "2024-06-01T00:00:00Z"
        return self.stat(dst_path)  # type: ignore[return-value]

    def delete(self, api_path: str) -> None:
        self.calls.append(("DELETE", api_path))
        if api_path in self.files:
            self.files.pop(api_path, None)
            self.mtimes.pop(api_path, None)
            return
        if api_path in self.dirs:
            # Mimic the real server: DELETE is NOT recursive (research §5).
            # A non-empty directory is refused with 400 "not empty".
            base = api_path + "/"
            non_empty = any(p.startswith(base) for p in self.files) or any(
                d.startswith(base) for d in self.dirs
            )
            if non_empty:
                from jp.errors import ApiError

                raise ApiError(
                    f"refusing to delete non-empty directory {api_path!r}: "
                    "the server is not recursive. Use 'jp rm --recursive' instead.",
                    status=400,
                )
            self.dirs.discard(api_path)
            return
        # Missing path: the real Api.delete swallows 404 silently.

    def create_terminal(self, cwd: str | None = None):
        """Terminals disabled, unless a test replaces this."""
        from jp.errors import ApiError

        raise ApiError("terminals disabled", status=404)

    def status_probe(self) -> StatusResult:
        """Scripted health probe mirroring Api.status_probe's outcomes."""
        if self.status_mode == "up":
            return StatusResult(up=True, detail="reached /api/status")
        if self.status_mode == "auth":
            from jp.errors import AuthError

            raise AuthError("the server rejected the token (HTTP 403).")
        if self.status_mode == "down":
            from jp.errors import ServerDownError

            raise ServerDownError()
        from jp.errors import NetworkError

        raise NetworkError("could not reach the server")

    # --- test assertions ----------------------------------------------------
    @property
    def deletes(self) -> list[str]:
        return [p for (m, p) in self.calls if m == "DELETE"]


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A working tree with a valid .jp config and an empty index."""
    root = tmp_path / "work"
    root.mkdir()
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    from jp import config as config_mod

    config_mod.save(root, cfg)
    Index(root).save()
    return root


@pytest.fixture
def cfg() -> Config:
    return Config(base_url="https://hub.example/api", prefix="users/alice")


@pytest.fixture
def index(repo) -> Index:
    return Index.load(repo)


@pytest.fixture
def ignore(repo) -> IgnoreSet:
    return IgnoreSet.from_root(repo)


def write_file(root: Path, rel: str, content: bytes) -> Path:
    """Helper to create a file (and parents) under ``root``."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


@pytest.fixture
def writer():
    """Fixture wrapper around :func:`write_file` for tests that prefer fixtures."""
    return write_file


@pytest.fixture
def make_fake_api():
    """Factory fixture returning fresh FakeApi instances."""
    return FakeApi
