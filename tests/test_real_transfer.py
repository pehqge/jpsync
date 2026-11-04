"""Large transfers against a REAL jupyter_server, the code the Hub runs.

FakeApi only knows what we believed about the server. These tests prove the chunk
numbering, the rename dance, the /files streaming and Range support for real. They
skip when jupyter_server is not installed.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

pytest.importorskip("jupyter_server", reason="jupyter_server not installed")

from jp import sync, transfer  # noqa: E402
from jp.api import Api  # noqa: E402
from jp.config import Config  # noqa: E402
from jp.errors import ServerDownError  # noqa: E402
from jp.ignore import IgnoreSet  # noqa: E402
from jp.index import Index  # noqa: E402

MIB = 1024 * 1024
PREFIX = "work"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, tmp_path):
        self.root = tmp_path / "srv"
        (self.root / PREFIX).mkdir(parents=True)
        self.token = "jp-" + os.urandom(8).hex()
        self.url = f"http://127.0.0.1:{free_port()}"
        self.log = open(tmp_path / "server.log", "wb")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "jupyter_server",
                f"--ServerApp.token={self.token}",
                f"--ServerApp.port={self.url.rsplit(':', 1)[1]}",
                "--ServerApp.ip=127.0.0.1",
                f"--ServerApp.root_dir={self.root}",
                "--ServerApp.open_browser=False",
                "--FileContentsManager.delete_to_trash=False",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{self.url}/api/status?token={self.token}", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        else:
            self.stop()
            pytest.fail("jupyter_server did not start")
        self.api = Api(self.url, self.token, _allow_http_localhost=True)
        self.cfg = Config(base_url=self.url, prefix=PREFIX)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=20)
        self.log.close()

    def on_disk(self, rel):
        return self.root / PREFIX / rel

    def leftovers(self):
        return [p.name for p in self.root.rglob(f"{paths_prefix()}*")]


def paths_prefix() -> str:
    from jp.paths import PART_PREFIX

    return PART_PREFIX


@pytest.fixture
def server(tmp_path):
    instance = Server(tmp_path)
    yield instance
    if instance.proc.poll() is None:
        instance.stop()


def workspace(tmp_path, name, cfg):
    root = tmp_path / name
    (root / ".jp").mkdir(parents=True)
    return root, Index.load(root), IgnoreSet.from_root(root)


def noise(nbytes: int) -> bytes:
    return os.urandom(nbytes)


def test_large_file_round_trip_is_byte_identical(server, tmp_path):
    blob = noise(40 * MIB + 123)
    root, index, ignore = workspace(tmp_path, "a", server.cfg)
    (root / "model.bin").write_bytes(blob)

    pushed = sync.push(root, server.cfg, server.api, index, ignore)
    assert pushed.failures == []
    assert server.on_disk("model.bin").read_bytes() == blob
    assert server.leftovers() == []

    other, other_index, other_ignore = workspace(tmp_path, "b", server.cfg)
    pulled = sync.pull(other, server.cfg, server.api, other_index, other_ignore)
    assert pulled.failures == []
    assert (other / "model.bin").read_bytes() == blob
    assert list((other / ".jp" / "parts").iterdir()) == []


def test_overwriting_a_large_file_replaces_it_and_cleans_up(server, tmp_path):
    root, index, ignore = workspace(tmp_path, "a", server.cfg)
    (root / "model.bin").write_bytes(noise(20 * MIB))
    sync.push(root, server.cfg, server.api, index, ignore)
    newer = noise(30 * MIB)
    (root / "model.bin").write_bytes(newer)
    outcome = sync.push(root, server.cfg, server.api, index, ignore)
    assert outcome.failures == []
    assert server.on_disk("model.bin").read_bytes() == newer
    assert server.leftovers() == []


def test_upload_resumes_from_a_part_on_the_real_server(server, tmp_path):
    blob = noise(30 * MIB)
    local = tmp_path / "model.bin"
    local.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    target = f"{PREFIX}/model.bin"
    part = transfer.part_path(target, digest)
    (server.root / part).write_bytes(blob[: 11 * MIB + 7])
    sent = []
    transfer.upload_large(
        server.api, local, target, digest, len(blob), PREFIX, threading.Event(), sent.append
    )
    assert server.on_disk("model.bin").read_bytes() == blob
    assert sent[0] == 11 * MIB + 7


def test_range_download_is_honoured_by_the_real_server(server):
    blob = noise(3 * MIB)
    server.on_disk("data.bin").write_bytes(blob)
    resumed, chunks = server.api.iter_file(f"{PREFIX}/data.bin", 1000)
    assert resumed is True
    assert b"".join(chunks) == blob[1000:]


def test_unicode_and_spaces_survive_chunked_transfer(server, tmp_path):
    blob = noise(17 * MIB)
    root, index, ignore = workspace(tmp_path, "a", server.cfg)
    (root / "pasta com espaço").mkdir()
    (root / "pasta com espaço" / "módulo #1?.bin").write_bytes(blob)
    outcome = sync.push(root, server.cfg, server.api, index, ignore)
    assert outcome.failures == []
    other, other_index, other_ignore = workspace(tmp_path, "b", server.cfg)
    pulled = sync.pull(other, server.cfg, server.api, other_index, other_ignore)
    assert pulled.failures == []
    assert (other / "pasta com espaço" / "módulo #1?.bin").read_bytes() == blob


def test_many_small_files_in_many_folders_round_trip(server, tmp_path):
    root, index, ignore = workspace(tmp_path, "a", server.cfg)
    for i in range(400):
        path = root / f"d{i % 40}" / "sub" / f"f{i}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"file {i}")
    outcome = sync.push(root, server.cfg, server.api, index, ignore)
    assert len(outcome.transferred) == 400
    assert outcome.failures == []
    again = sync.push(root, server.cfg, server.api, Index.load(root), ignore)
    assert again.transferred == []
    assert len(again.up_to_date) == 400


def test_ignored_large_remote_file_is_not_pulled(server, tmp_path):
    server.on_disk("m.gguf").write_bytes(noise(20 * MIB))
    server.on_disk("keep.txt").write_text("k")
    root, index, _ = workspace(tmp_path, "a", server.cfg)
    (root / ".jpignore").write_text("*.gguf\n")
    outcome = sync.pull(root, server.cfg, server.api, index, IgnoreSet.from_root(root))
    assert outcome.transferred == ["keep.txt"]


def test_redirect_to_the_hub_reads_as_a_stopped_server(server, monkeypatch):
    server.on_disk("data.bin").write_bytes(b"x")
    redirecting = Api(f"{server.url}/login?next=", server.token, _allow_http_localhost=True)
    with pytest.raises(ServerDownError):
        redirecting.iter_file("api/status")
