"""Many small files through one archive.

The way into a JupyterHub answers only so many requests a second, so one request
per file is the slow part of a large tree. An archive is one file: it is packed
on one side, moved with the verified transfer, and unpacked on the other. Packing
on the server runs through a terminal, so a server without terminals simply keeps
the file-by-file path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import select
import shlex
import tarfile
import threading
from collections.abc import Iterable
from pathlib import Path

from . import config as config_mod
from . import paths, pty, transfer
from ._ws import WebSocket, WebSocketError
from .api import Api
from .config import Config
from .errors import JpError, NetworkError, TransferError

MIN_FILES = 20
SILENCE_LIMIT_S = 300
HASH_READ_BYTES = 1 << 20

Unavailable = (JpError, WebSocketError, OSError, tarfile.TarError)

PACK_SCRIPT = """\
import json, sys, tarfile

with open(sys.argv[1]) as listing:
    names = json.load(listing)
with tarfile.open(sys.argv[2], "w", format=tarfile.PAX_FORMAT) as archive:
    for name in names:
        archive.add(name, recursive=False)
"""

# Runs inside the workspace prefix on the server. Everything it writes must stay
# there, so a planted link cannot carry an archive member into a shared folder.
UNPACK_SCRIPT = """\
import os, shutil, sys, tarfile

root = os.path.realpath(".")
with tarfile.open(sys.argv[1]) as archive:
    for member in archive:
        name = member.name
        folder = os.path.dirname(name) or "."
        if not member.isreg() or name.startswith("/") or ".." in name.split("/"):
            sys.exit(f"unsafe archive member: {name}")
        os.makedirs(folder, exist_ok=True)
        real = os.path.realpath(folder)
        if real != root and not real.startswith(root + os.sep):
            sys.exit(f"archive member leaves the workspace: {name}")
        if os.path.islink(name):
            sys.exit(f"refusing to write through a link: {name}")
        temporary = os.path.join(folder, f"{sys.argv[2]}.{os.getpid()}")
        with archive.extractfile(member) as source, open(temporary, "wb") as target:
            shutil.copyfileobj(source, target)
        os.replace(temporary, name)
"""


def run_remote(api: Api, cfg: Config, command: str) -> None:
    """Run ``command`` in the workspace prefix on the server; raise if it fails."""
    prefix = paths.validate_prefix(cfg.prefix)
    marker = secrets.token_hex(8)
    session = api.create_terminal(cwd=prefix)
    enter = "" if session.cwd_applied else f"cd -- {shlex.quote(prefix)} && "
    line = (
        f"printf '\\033_jp{marker}C\\033\\\\'; {enter}{command} </dev/null; __rc=$?; "
        f"printf '\\033_jp{marker}D%d\\033\\\\' \"$__rc\"; exit\n"
    )
    try:
        socket = WebSocket.connect(
            api.terminal_ws_url(session.name),
            headers={"Authorization": f"token {config_mod.load_token(cfg)}"},
            timeout=cfg.timeout,
        )
        try:
            socket.send_text(pty.stdin_message(line.encode()))
            scanner = pty.RunScanner(marker)
            output = _read_until_done(socket, scanner)
        finally:
            socket.close()
    finally:
        api.delete_terminal(session.name)
    if scanner.exit_code != 0:
        raise TransferError(f"remote step failed: {output.strip()[-300:]}")


def _read_until_done(socket: WebSocket, scanner: pty.RunScanner) -> str:
    output = bytearray()
    while not socket.closed and not scanner.done:
        ready, _, _ = select.select([socket.fileno()], [], [], SILENCE_LIMIT_S)
        if not ready:
            raise NetworkError(f"no answer from the server for {SILENCE_LIMIT_S} s")
        for raw in socket.read_messages():
            kind, payload = pty.parse_server_message(raw.decode("utf-8", "replace"))
            if kind == "stdout" and isinstance(payload, str):
                output += scanner.feed(payload.encode("utf-8"))
    return output.decode("utf-8", "replace")


class _ServerFiles:
    """Temporary files of one run, always under the workspace prefix."""

    def __init__(self, api: Api, cfg: Config) -> None:
        self.api = api
        self.cfg = cfg
        self.prefix = paths.validate_prefix(cfg.prefix)
        self.stem = f"{paths.PART_PREFIX}{secrets.token_hex(8)}"
        self._created: list[str] = []

    def name(self, suffix: str) -> str:
        return f"{self.stem}.{suffix}"

    def path(self, suffix: str) -> str:
        remote = f"{self.prefix}/{self.name(suffix)}"
        paths.assert_within_prefix(remote, self.prefix)
        if remote not in self._created:
            self._created.append(remote)
        return remote

    def write(self, suffix: str, data: bytes) -> None:
        self.api.put_file_bytes(self.path(suffix), data)

    def remove_all(self) -> None:
        for remote in self._created:
            try:
                self.api.delete(remote)
            except JpError:
                continue


def fetch_archive(
    api: Api, cfg: Config, names: list[str], archive: Path, part: Path, meter_add
) -> None:
    """Have the server pack ``names`` and download the archive to ``archive``."""
    server = _ServerFiles(api, cfg)
    try:
        server.write("py", PACK_SCRIPT.encode())
        server.write("json", json.dumps(names).encode())
        remote = server.path("tar")
        run_remote(
            api,
            cfg,
            f"python3 {shlex.quote(server.name('py'))} {shlex.quote(server.name('json'))} "
            f"{shlex.quote(server.name('tar'))}",
        )
        entry = api.stat(remote, as_file=True)
        if entry is None or not entry.size:
            raise TransferError("the server did not produce the archive")
        digest = api.hash(remote, timeout=transfer.HASH_TIMEOUT_S)
        transfer.download_large(
            api, remote, archive, part, entry.size, digest, threading.Event(), meter_add
        )
    finally:
        server.remove_all()


def read_archive(archive: Path, wanted: Iterable[str]):
    """Yield (name, bytes) for the regular members that were asked for.

    Only names jp requested come out, so a hostile archive cannot add a path.
    """
    expected = set(wanted)
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isreg() and member.name in expected:
                source = tar.extractfile(member)
                if source is not None:
                    yield member.name, source.read()


def pack_local(files: dict[str, Path], archive: Path) -> dict[str, tuple[str, int]]:
    """Archive local files under their wire names; return {name: (sha256, size)}."""
    digests: dict[str, tuple[str, int]] = {}
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in files.items():
            data = path.read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mtime = int(path.stat().st_mtime)
            member.mode = 0o644
            tar.addfile(member, _BytesReader(data))
            digests[name] = (hashlib.sha256(data).hexdigest(), len(data))
    return digests


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        stop = len(self._data) if size < 0 else self._position + size
        piece = self._data[self._position : stop]
        self._position += len(piece)
        return piece


def send_archive(api: Api, cfg: Config, archive: Path, meter_add) -> None:
    """Upload ``archive`` and unpack it into the workspace prefix on the server."""
    server = _ServerFiles(api, cfg)
    try:
        server.write("py", UNPACK_SCRIPT.encode())
        remote = server.path("tar")
        size = archive.stat().st_size
        digest = _file_sha256(archive)
        prefix = paths.validate_prefix(cfg.prefix)
        if size >= transfer.LARGE_FILE_BYTES:
            transfer.upload_large(
                api, archive, remote, digest, size, prefix, threading.Event(), meter_add
            )
        else:
            api.put_file_bytes(remote, archive.read_bytes())
            meter_add(size)
        if api.hash(remote, timeout=transfer.HASH_TIMEOUT_S) not in (digest, None):
            raise TransferError("checksum mismatch uploading the archive")
        run_remote(
            api,
            cfg,
            f"python3 {shlex.quote(server.name('py'))} {shlex.quote(server.name('tar'))} "
            f"{shlex.quote(server.stem)}",
        )
    finally:
        server.remove_all()


def _file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(HASH_READ_BYTES), b""):
            sha.update(block)
    return sha.hexdigest()
