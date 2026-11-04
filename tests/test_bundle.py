"""Archive transfers, with the real packing scripts running in a temporary folder."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from jp import bundle, sync, transfer
from jp.errors import ApiError, TransferError

PREFIX = "users/alice"


@pytest.fixture(autouse=True)
def _small_limits(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_BYTES", 1024)
    monkeypatch.setattr(transfer, "LARGE_FILE_BYTES", 4096)
    monkeypatch.setattr(transfer, "SEGMENT_BYTES", 2048)
    monkeypatch.setattr(transfer, "BACKOFF_S", 0)


@pytest.fixture
def server_shell(fake_api, tmp_path, monkeypatch):
    """Stand-in for the terminal: runs the command against a folder that mirrors
    FakeApi, so what the scripts do is what the test sees."""
    disk = tmp_path / "server-disk"
    disk.mkdir()

    def run(api, cfg, command):
        for path, data in api.files.items():
            target = disk / path[len(PREFIX) + 1 :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        done = subprocess.run(
            command.replace("python3", sys.executable, 1),
            shell=True,
            cwd=disk,
            capture_output=True,
            text=True,
        )
        if done.returncode:
            raise TransferError(f"remote step failed: {done.stdout}{done.stderr}")
        for path in disk.rglob("*"):
            if path.is_file():
                key = f"{PREFIX}/{path.relative_to(disk).as_posix()}"
                api.seed(key, path.read_bytes(), mtime="2024-07-01T00:00:00Z")

    monkeypatch.setattr(bundle, "run_remote", run)
    return disk


def forget_scripts(api):
    return [path for path in api.files if "__jp_part__" in path]


def uploads_of_real_files(api):
    return [p for verb, p in api.calls if verb == "PUT" and "__jp_part__" not in p]


def test_many_small_files_travel_as_one_archive(
    repo, cfg, fake_api, index, ignore, writer, server_shell
):
    for i in range(30):
        writer(repo, f"d{i % 3}/f{i}.txt", f"file {i}".encode())
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 30
    assert outcome.failures == []
    assert uploads_of_real_files(fake_api) == []
    assert fake_api.files[f"{PREFIX}/d1/f7.txt"] == b"file 7"
    assert index.get("d1/f7.txt").size == 6


def test_index_after_an_archive_makes_the_next_push_a_no_op(
    repo, cfg, fake_api, index, ignore, writer, server_shell
):
    for i in range(30):
        writer(repo, f"f{i}.txt", b"x")
    sync.push(repo, cfg, fake_api, index, ignore)
    for path in forget_scripts(fake_api):
        fake_api.files.pop(path)
    again = sync.push(repo, cfg, fake_api, index, ignore)
    assert again.transferred == []
    assert len(again.up_to_date) == 30


def test_pull_of_many_files_uses_the_archive(repo, cfg, fake_api, index, ignore, server_shell):
    for i in range(30):
        fake_api.seed(f"{PREFIX}/d{i % 3}/f{i}.txt", f"file {i}".encode())
    outcome = sync.pull(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 30
    assert (repo / "d1" / "f7.txt").read_bytes() == b"file 7"
    assert [p for p in fake_api.content_reads if "__jp_part__" not in p] == []
    assert list((repo / ".jp" / "parts").glob("*")) == []


def test_few_files_keep_the_plain_path(repo, cfg, fake_api, index, ignore, writer, server_shell):
    for i in range(bundle.MIN_FILES - 1):
        writer(repo, f"f{i}.txt", b"x")
    sync.push(repo, cfg, fake_api, index, ignore)
    assert len(uploads_of_real_files(fake_api)) == bundle.MIN_FILES - 1


def test_large_files_stay_out_of_the_archive(
    repo, cfg, fake_api, index, ignore, writer, server_shell
):
    for i in range(25):
        writer(repo, f"f{i}.txt", b"x")
    writer(repo, "big.bin", b"z" * 5000)
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 26
    assert fake_api.files[f"{PREFIX}/big.bin"] == b"z" * 5000


def test_server_without_terminals_falls_back_to_one_by_one(
    repo, cfg, fake_api, index, ignore, writer
):
    for i in range(25):
        writer(repo, f"f{i}.txt", b"x")
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 25
    assert len(uploads_of_real_files(fake_api)) == 25
    assert forget_scripts(fake_api) == []


def test_pull_falls_back_when_the_server_cannot_pack(repo, cfg, fake_api, index, ignore):
    for i in range(25):
        fake_api.seed(f"{PREFIX}/f{i}.txt", b"x")
    outcome = sync.pull(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 25
    assert forget_scripts(fake_api) == []


def test_file_missing_from_the_archive_is_fetched_one_by_one(
    repo, cfg, fake_api, index, ignore, monkeypatch, server_shell
):
    for i in range(25):
        fake_api.seed(f"{PREFIX}/f{i}.txt", f"file {i}".encode())
    real_read = bundle.read_archive

    def drop_one(archive, wanted):
        return ((n, d) for n, d in real_read(archive, wanted) if n != "f3.txt")

    monkeypatch.setattr(bundle, "read_archive", drop_one)
    outcome = sync.pull(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 25
    assert (repo / "f3.txt").read_bytes() == b"file 3"


def test_file_of_the_wrong_size_in_the_archive_is_not_trusted(
    repo, cfg, fake_api, index, ignore, monkeypatch, server_shell
):
    for i in range(25):
        fake_api.seed(f"{PREFIX}/f{i}.txt", f"file {i}".encode())
    real_read = bundle.read_archive

    def corrupt_one(archive, wanted):
        for name, data in real_read(archive, wanted):
            yield name, b"wrong size" if name == "f3.txt" else data

    monkeypatch.setattr(bundle, "read_archive", corrupt_one)
    sync.pull(repo, cfg, fake_api, index, ignore)
    assert (repo / "f3.txt").read_bytes() == b"file 3"


def test_temporary_files_are_removed_when_the_remote_step_fails(
    repo, cfg, fake_api, index, ignore, writer, monkeypatch
):
    for i in range(25):
        writer(repo, f"f{i}.txt", b"x")

    def fail(api, cfg, command):
        raise ApiError("server returned HTTP 500", status=500)

    monkeypatch.setattr(bundle, "run_remote", fail)
    sync.push(repo, cfg, fake_api, index, ignore)
    assert forget_scripts(fake_api) == []


def unpack(folder: Path, archive: Path) -> subprocess.CompletedProcess:
    script = folder / "unpack.py"
    script.write_text(bundle.UNPACK_SCRIPT)
    return subprocess.run(
        [sys.executable, str(script), str(archive), "__jp_part__.t"],
        cwd=folder,
        capture_output=True,
        text=True,
    )


def tar_with(path: Path, *members: tarfile.TarInfo) -> Path:
    with tarfile.open(path, "w") as tar:
        for member in members:
            tar.addfile(member, io.BytesIO(b"x" * member.size))
    return path


def regular(name: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = 1
    return member


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape.txt", "/tmp/jp-escape.txt"])
def test_unpack_refuses_a_path_leaving_the_workspace(tmp_path, name):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    archive = tar_with(tmp_path / "bad.tar", regular(name))
    assert unpack(workspace, archive).returncode != 0
    assert not (tmp_path / "escape.txt").exists()
    assert not Path("/tmp/jp-escape.txt").exists()


def test_unpack_refuses_a_symlink_member(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc"
    assert unpack(workspace, tar_with(tmp_path / "bad.tar", link)).returncode != 0
    assert not (workspace / "link").exists()


def test_unpack_refuses_a_folder_that_links_outside(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (workspace / "data").symlink_to(shared)
    archive = tar_with(tmp_path / "bad.tar", regular("data/x.txt"))
    assert unpack(workspace, archive).returncode != 0
    assert list(shared.iterdir()) == []


def test_unpack_refuses_to_write_through_a_file_link(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (workspace / "x.txt").symlink_to(victim)
    archive = tar_with(tmp_path / "bad.tar", regular("x.txt"))
    assert unpack(workspace, archive).returncode != 0
    assert victim.read_text() == "keep me"


def test_read_archive_returns_only_what_was_requested(tmp_path):
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    archive = tar_with(tmp_path / "a.tar", regular("asked.txt"), regular("extra.txt"), link)
    names = [name for name, _ in bundle.read_archive(archive, {"asked.txt", "link"})]
    assert names == ["asked.txt"]


def test_pack_local_reports_the_hash_of_each_file(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    packed = bundle.pack_local({"sub/a.txt": tmp_path / "a.txt"}, tmp_path / "out.tar")
    assert packed["sub/a.txt"][1] == 5
    with tarfile.open(tmp_path / "out.tar") as tar:
        assert tar.getnames() == ["sub/a.txt"]
