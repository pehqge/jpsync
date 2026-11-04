"""Bulk and mixed-size sync through push/pull, plus remote .jpignore handling."""

from __future__ import annotations

import pytest

from jp import sync, transfer
from jp.errors import ApiError, NetworkError
from jp.ignore import IgnoreSet
from jp.index import Index

PREFIX = "users/alice"


@pytest.fixture(autouse=True)
def _small_limits(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_BYTES", 1024)
    monkeypatch.setattr(transfer, "LARGE_FILE_BYTES", 2048)
    monkeypatch.setattr(transfer, "BACKOFF_S", 0)


def ignoring(repo, *patterns):
    (repo / ".jpignore").write_text("\n".join(patterns) + "\n")
    return IgnoreSet.from_root(repo)


def test_push_mixes_chunked_and_whole_file_uploads(repo, cfg, fake_api, index, ignore, writer):
    writer(repo, "small.txt", b"hello")
    big = bytes(range(256)) * 20
    writer(repo, "data/big.bin", big)
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert sorted(outcome.transferred) == ["data/big.bin", "small.txt"]
    assert fake_api.files[f"{PREFIX}/data/big.bin"] == big
    assert fake_api.files[f"{PREFIX}/small.txt"] == b"hello"
    assert index.get("data/big.bin").size == len(big)


def test_second_push_of_a_big_file_transfers_nothing(repo, cfg, fake_api, index, ignore, writer):
    writer(repo, "big.bin", b"z" * 5000)
    sync.push(repo, cfg, fake_api, index, ignore)
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert outcome.transferred == []
    assert outcome.up_to_date == ["big.bin"]


def test_pull_streams_big_files_without_loading_them(repo, cfg, fake_api, index, ignore):
    big = bytes(range(256)) * 20
    fake_api.seed(f"{PREFIX}/big.bin", big)
    fake_api.get_file_bytes = lambda path: pytest.fail("big file went through the JSON API")
    outcome = sync.pull(repo, cfg, fake_api, index, ignore)
    assert outcome.transferred == ["big.bin"]
    assert (repo / "big.bin").read_bytes() == big
    assert index.get("big.bin").size == len(big)


def test_unfinished_upload_is_invisible_to_sync(repo, cfg, fake_api, index, ignore):
    fake_api.seed(f"{PREFIX}/__jp_part__.abc", b"half")
    fake_api.seed(f"{PREFIX}/sub/__jp_part__.abc.old", b"old")
    assert sync.scan_remote(fake_api, cfg) == {}
    assert sync.pull(repo, cfg, fake_api, index, ignore).transferred == []


def test_many_files_in_many_folders_all_arrive(repo, cfg, fake_api, index, ignore, writer):
    expected = {}
    for i in range(300):
        rel = f"d{i % 30}/nested/f{i}.txt"
        writer(repo, rel, f"content {i}".encode())
        expected[f"{PREFIX}/{rel}"] = f"content {i}".encode()
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert len(outcome.transferred) == 300
    assert outcome.failures == []
    assert fake_api.files == expected


def test_index_of_a_big_batch_survives_a_reload(repo, cfg, fake_api, index, ignore, writer):
    for i in range(50):
        writer(repo, f"f{i}.txt", b"x")
    sync.push(repo, cfg, fake_api, index, ignore)
    assert len(Index.load(repo).entries) == 50


def test_index_is_not_rewritten_once_per_file(
    repo, cfg, fake_api, index, ignore, writer, monkeypatch
):
    for i in range(100):
        writer(repo, f"f{i}.txt", b"x")
    saves = []
    real_save = index.save
    monkeypatch.setattr(index, "save", lambda: (saves.append(1), real_save()))
    sync.push(repo, cfg, fake_api, index, ignore)
    assert len(saves) < 10


def test_one_bad_file_does_not_stop_the_others(repo, cfg, fake_api, index, ignore, writer):
    for name in ("a.txt", "bad.txt", "c.txt"):
        writer(repo, name, b"x")
    real_put = fake_api.put_file_bytes

    def refuse(path, data):
        if path.endswith("bad.txt"):
            raise ApiError("server returned HTTP 400: nope", status=400)
        return real_put(path, data)

    fake_api.put_file_bytes = refuse
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert sorted(outcome.transferred) == ["a.txt", "c.txt"]
    assert outcome.failures == [("bad.txt", "server returned HTTP 400: nope")]
    assert index.get("bad.txt") is None


def test_transient_failure_on_a_small_file_is_retried(repo, cfg, fake_api, index, ignore, writer):
    writer(repo, "a.txt", b"x")
    real_put = fake_api.put_file_bytes
    failures = [NetworkError("reset")]

    def flaky(path, data):
        if failures:
            raise failures.pop()
        return real_put(path, data)

    fake_api.put_file_bytes = flaky
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert outcome.transferred == ["a.txt"]


def test_size_mismatch_after_upload_is_a_failure(repo, cfg, fake_api, index, ignore, writer):
    writer(repo, "a.txt", b"12345")
    real_put = fake_api.put_file_bytes
    fake_api.put_file_bytes = lambda path, data: real_put(path, data[:2])
    outcome = sync.push(repo, cfg, fake_api, index, ignore)
    assert "size mismatch" in outcome.failures[0][1]
    assert index.get("a.txt") is None


def test_files_on_both_sides_without_index_are_hashed_not_downloaded(
    repo, cfg, fake_api, index, ignore, writer
):
    for i in range(40):
        writer(repo, f"f{i}.txt", b"same")
        fake_api.seed(f"{PREFIX}/f{i}.txt", b"same")
    states = sync.diff(repo, cfg, fake_api, index, ignore)
    assert all(st.change == sync.Change.UNCHANGED for st in states)
    assert fake_api.content_reads == []
    assert len(fake_api.hash_reads) == 40


def test_pull_skips_a_remote_file_matching_jpignore(repo, cfg, fake_api, index):
    fake_api.seed(f"{PREFIX}/keep.txt", b"k")
    fake_api.seed(f"{PREFIX}/model.gguf", b"m" * 5000)
    outcome = sync.pull(repo, cfg, fake_api, index, ignoring(repo, "*.gguf"))
    assert outcome.transferred == ["keep.txt"]
    assert not (repo / "model.gguf").exists()


def test_pull_does_not_even_list_an_ignored_remote_folder(repo, cfg, fake_api, index):
    fake_api.seed(f"{PREFIX}/models/deep/m.bin", b"m")
    fake_api.seed(f"{PREFIX}/keep.txt", b"k")
    listed = []
    real_list = fake_api.list_dir
    fake_api.list_dir = lambda path: (listed.append(path), real_list(path))[1]
    outcome = sync.pull(repo, cfg, fake_api, index, ignoring(repo, "models/"))
    assert outcome.transferred == ["keep.txt"]
    assert listed == [PREFIX]


def test_negated_ignore_rule_still_pulls_the_file(repo, cfg, fake_api, index):
    fake_api.seed(f"{PREFIX}/a.gguf", b"a")
    fake_api.seed(f"{PREFIX}/b.gguf", b"b")
    outcome = sync.pull(repo, cfg, fake_api, index, ignoring(repo, "*.gguf", "!b.gguf"))
    assert outcome.transferred == ["b.gguf"]


def test_ignored_remote_file_is_never_a_mirror_deletion_candidate(
    repo, cfg, fake_api, index, writer
):
    writer(repo, "keep.txt", b"k")
    fake_api.seed(f"{PREFIX}/model.gguf", b"m")
    outcome = sync.push(repo, cfg, fake_api, index, ignoring(repo, "*.gguf"))
    assert outcome.deletable == []


def test_file_in_index_under_a_newly_ignored_folder_is_left_alone(
    repo, cfg, fake_api, index, ignore, writer
):
    writer(repo, "models/m.bin", b"m")
    sync.push(repo, cfg, fake_api, index, ignore)
    ignored = ignoring(repo, "models/")
    (repo / "models/m.bin").unlink()
    push = sync.push(repo, cfg, fake_api, index, ignored)
    pull = sync.pull(repo, cfg, fake_api, index, ignored)
    assert (push.transferred, push.deletable) == ([], [])
    assert pull.transferred == []
    assert "models/m.bin" not in pull.deletable


def test_jp_diff_does_not_load_a_large_file(
    repo, cfg, fake_api, index, ignore, writer, monkeypatch
):
    from jp.commands import diff as diff_command

    writer(repo, "big.bin", b"a" * 5000)
    sync.push(repo, cfg, fake_api, index, ignore)
    writer(repo, "big.bin", b"b" * 5000)

    class Context:
        pass

    context = Context()
    context.cfg, context.root, context.index, context.ignore = cfg, repo, index, ignore
    monkeypatch.setattr(diff_command, "load_repo", lambda: context)
    monkeypatch.setattr(diff_command._context, "build_api", lambda _: fake_api)
    fake_api.get_file_bytes = lambda path: pytest.fail("large file went through memory")
    args = type("Args", (), {"path": ""})()
    assert diff_command.run(args) == 0
