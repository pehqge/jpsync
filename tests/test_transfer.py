"""Large-file transfers: every failure path must leave the data correct or untouched."""

from __future__ import annotations

import hashlib
import threading

import pytest

from jp import transfer
from jp.errors import ApiError, AuthError, NetworkError, SafetyError, TransferError

CHUNK = 1024
PREFIX = "users/me/work"
TARGET = f"{PREFIX}/model.bin"
BLOB = bytes(range(256)) * 40  # 10_240 bytes: ten chunks


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    monkeypatch.setattr(transfer, "CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(transfer, "BACKOFF_S", 0)


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "model.bin"
    path.write_bytes(BLOB)
    return path


def upload(api, local, cancel=None, data=BLOB):
    return transfer.upload_large(
        api, local, TARGET, sha(data), len(data), PREFIX, cancel or threading.Event()
    )


def chunk_numbers(api) -> list[int]:
    return [int(path.rsplit("#", 1)[1]) for verb, path in api.calls if verb == "CHUNK"]


def part_files(api) -> list[str]:
    return [path for path in api.files if "__jp_part__" in path]


class FlakyChunks:
    """Fails chosen chunk sends, either before or after the server stored them."""

    def __init__(self, api, *, fail_on, stored):
        self.api = api
        self.fail_on = set(fail_on)
        self.stored = stored
        self.real_put = api.put_chunk
        api.put_chunk = self

    def __call__(self, path, data, number):
        if number in self.fail_on:
            self.fail_on.discard(number)
            if self.stored:
                self.real_put(path, data, number)
            raise NetworkError("connection dropped")
        self.real_put(path, data, number)


def test_upload_arrives_intact_and_the_temp_file_is_gone(fake_api, local):
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert part_files(fake_api) == []


def test_upload_numbers_chunks_as_the_server_expects(fake_api, local):
    upload(fake_api, local)
    assert chunk_numbers(fake_api) == [1, 2, 3, 4, 5, 6, 7, 8, 9, -1]


def test_target_does_not_exist_until_the_upload_is_complete(fake_api, local):
    seen = []
    real_put = fake_api.put_chunk

    def spy(path, data, number):
        seen.append(TARGET in fake_api.files)
        real_put(path, data, number)

    fake_api.put_chunk = spy
    upload(fake_api, local)
    assert not any(seen)


def test_lost_response_does_not_duplicate_a_stored_chunk(fake_api, local):
    FlakyChunks(fake_api, fail_on={3}, stored=True)
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB


def test_lost_request_is_sent_again(fake_api, local):
    FlakyChunks(fake_api, fail_on={3}, stored=False)
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB


def test_last_chunk_stored_but_response_lost_still_finishes(fake_api, local):
    FlakyChunks(fake_api, fail_on={-1}, stored=True)
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB


def test_interrupted_run_resumes_where_the_server_stopped(fake_api, local):
    part = transfer.part_path(TARGET, sha(BLOB))
    fake_api.seed(part, BLOB[: 6 * CHUNK])
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert chunk_numbers(fake_api) == [7, 8, 9, -1]


def test_resuming_inside_the_first_chunk_never_replaces_the_file(fake_api, local):
    fake_api.seed(transfer.part_path(TARGET, sha(BLOB)), BLOB[:100])
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert 1 not in chunk_numbers(fake_api)


def test_complete_part_from_a_crashed_run_needs_no_chunks(fake_api, local):
    fake_api.seed(transfer.part_path(TARGET, sha(BLOB)), BLOB)
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert chunk_numbers(fake_api) == []


def test_part_with_wrong_bytes_is_rebuilt_from_scratch(fake_api, local):
    part = transfer.part_path(TARGET, sha(BLOB))
    fake_api.seed(part, b"x" * (6 * CHUNK))
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert chunk_numbers(fake_api)[-10:] == [1, 2, 3, 4, 5, 6, 7, 8, 9, -1]


def test_file_changing_during_upload_fails_and_leaves_target_alone(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    with pytest.raises(TransferError, match="checksum"):
        upload(fake_api, local, data=b"y" * len(BLOB))
    assert fake_api.files[TARGET] == b"previous version"


def test_failed_upload_keeps_the_previous_version(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    real_put = fake_api.put_chunk

    def refuse(path, data, number):
        if number == 5:
            raise AuthError("server returned HTTP 403")
        real_put(path, data, number)

    fake_api.put_chunk = refuse
    with pytest.raises(AuthError):
        upload(fake_api, local)
    assert fake_api.files[TARGET] == b"previous version"


def test_overwrite_replaces_the_target_and_leaves_no_temp(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert part_files(fake_api) == []


def test_failed_swap_restores_the_previous_version(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    real_rename = fake_api.rename

    def rename(src, dst):
        if dst == TARGET and "__jp_part__" in src and not src.endswith(".old"):
            raise ApiError("server returned HTTP 500", status=500)
        return real_rename(src, dst)

    fake_api.rename = rename
    with pytest.raises(ApiError):
        upload(fake_api, local)
    assert fake_api.files[TARGET] == b"previous version"


def test_failing_to_remove_the_old_copy_does_not_fail_the_upload(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    real_delete = fake_api.delete
    deletes = []

    def flaky_delete(path):
        deletes.append(path)
        if len(deletes) == 2:
            raise ApiError("server returned HTTP 500", status=500)
        real_delete(path)

    fake_api.delete = flaky_delete
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB


def test_stale_backup_from_a_crash_does_not_block_the_swap(fake_api, local):
    fake_api.seed(TARGET, b"previous version")
    fake_api.seed(transfer.part_path(TARGET, sha(BLOB)) + ".old", b"crashed leftover")
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB
    assert part_files(fake_api) == []


def test_rejected_token_is_not_retried(fake_api, local):
    attempts = []

    def refuse(path, data, number):
        attempts.append(number)
        raise AuthError("server returned HTTP 403")

    fake_api.put_chunk = refuse
    with pytest.raises(AuthError):
        upload(fake_api, local)
    assert attempts == [1]


def test_network_that_never_returns_gives_up(fake_api, local):
    def drop(path, data, number):
        raise NetworkError("connection dropped")

    fake_api.put_chunk = drop
    with pytest.raises(NetworkError):
        upload(fake_api, local)


def test_cancel_stops_between_chunks(fake_api, local):
    cancel = threading.Event()
    real_put = fake_api.put_chunk

    def stop_after_three(path, data, number):
        real_put(path, data, number)
        if number == 3:
            cancel.set()

    fake_api.put_chunk = stop_after_three
    with pytest.raises(transfer.Cancelled):
        upload(fake_api, local, cancel)
    assert TARGET not in fake_api.files
    assert len(fake_api.files[transfer.part_path(TARGET, sha(BLOB))]) == 3 * CHUNK


def test_server_without_hash_support_falls_back_to_size(fake_api, local):
    fake_api.hash = lambda path, *, timeout=None: None
    upload(fake_api, local)
    assert fake_api.files[TARGET] == BLOB


class Download:
    def __init__(self, tmp_path):
        self.dest = tmp_path / "out" / "model.bin"
        self.part = tmp_path / ".jp" / "parts" / "x.part"
        self.dest.parent.mkdir()

    def run(self, api, *, digest=None, size=len(BLOB), cancel=None):
        return transfer.download_large(
            api, TARGET, self.dest, self.part, size, digest, cancel or threading.Event()
        )


@pytest.fixture
def download(tmp_path, fake_api):
    fake_api.seed(TARGET, BLOB)
    return Download(tmp_path)


def test_download_arrives_intact_and_the_part_is_gone(fake_api, download):
    assert download.run(fake_api, digest=sha(BLOB)) == sha(BLOB)
    assert download.dest.read_bytes() == BLOB
    assert not download.part.exists()


def test_dropped_connection_resumes_from_where_it_stopped(fake_api, download):
    real = fake_api.iter_file
    offsets = []

    def dropping(path, offset=0):
        offsets.append(offset)
        resumed, chunks = real(path, offset)

        def stream():
            yield next(chunks)
            if len(offsets) == 1:
                raise NetworkError("connection dropped")
            yield from chunks

        return resumed, stream()

    fake_api.iter_file = dropping
    download.run(fake_api, digest=sha(BLOB))
    assert download.dest.read_bytes() == BLOB
    assert offsets == [0, 1024]


def test_short_read_without_an_error_is_noticed(fake_api, download):
    real = fake_api.iter_file
    calls = []

    def short(path, offset=0):
        calls.append(offset)
        resumed, chunks = real(path, offset)
        return resumed, iter([next(chunks)]) if len(calls) == 1 else chunks

    fake_api.iter_file = short
    download.run(fake_api, digest=sha(BLOB))
    assert download.dest.read_bytes() == BLOB
    assert len(calls) == 2


def test_part_left_by_a_previous_run_is_resumed(fake_api, download):
    download.part.parent.mkdir(parents=True)
    download.part.write_bytes(BLOB[:5000])
    assert download.run(fake_api, digest=sha(BLOB)) == sha(BLOB)
    assert download.dest.read_bytes() == BLOB


def test_server_that_ignores_range_restarts_cleanly(fake_api, download):
    download.part.parent.mkdir(parents=True)
    download.part.write_bytes(BLOB[:5000])
    real = fake_api.iter_file
    fake_api.iter_file = lambda path, offset=0: (False, real(path, 0)[1])
    assert download.run(fake_api, digest=sha(BLOB)) == sha(BLOB)
    assert download.dest.read_bytes() == BLOB


def test_stale_part_with_wrong_bytes_is_discarded(fake_api, download):
    download.part.parent.mkdir(parents=True)
    download.part.write_bytes(b"x" * 5000)
    assert download.run(fake_api, digest=sha(BLOB)) == sha(BLOB)
    assert download.dest.read_bytes() == BLOB


def test_checksum_mismatch_never_touches_the_destination(fake_api, download):
    download.dest.write_bytes(b"previous version")
    with pytest.raises(TransferError, match="checksum"):
        download.run(fake_api, digest=sha(b"something else"))
    assert download.dest.read_bytes() == b"previous version"
    assert not download.part.exists()


def test_body_larger_than_expected_is_rejected(fake_api, download):
    fake_api.files[TARGET] = BLOB + b"unexpected"
    with pytest.raises(TransferError, match="size"):
        download.run(fake_api)
    assert not download.dest.exists()


def test_file_shorter_than_listed_gives_up_without_installing(fake_api, download):
    fake_api.files[TARGET] = b"truncated"
    with pytest.raises(NetworkError):
        download.run(fake_api)
    assert not download.dest.exists()


def test_symlinked_destination_is_refused(fake_api, download, tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep me")
    download.dest.symlink_to(victim)
    with pytest.raises(SafetyError):
        download.run(fake_api)
    assert victim.read_bytes() == b"keep me"


def test_cancel_keeps_the_part_for_the_next_run(fake_api, download):
    cancel = threading.Event()
    real = fake_api.iter_file

    def cancelling(path, offset=0):
        resumed, chunks = real(path, offset)

        def stream():
            yield next(chunks)
            cancel.set()
            yield from chunks

        return resumed, stream()

    fake_api.iter_file = cancelling
    with pytest.raises(transfer.Cancelled):
        download.run(fake_api, cancel=cancel)
    assert 0 < download.part.stat().st_size < len(BLOB)
    assert not download.dest.exists()


def test_with_retries_recovers_from_a_server_error(monkeypatch):
    results = iter([ApiError("bad gateway", status=502), NetworkError("reset"), "done"])

    def call():
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    assert transfer.with_retries(call, threading.Event()) == "done"


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_with_retries_does_not_repeat_a_definite_refusal(status):
    calls = []

    def call():
        calls.append(1)
        raise ApiError("refused", status=status)

    with pytest.raises(ApiError):
        transfer.with_retries(call, threading.Event())
    assert len(calls) == 1


def test_run_parallel_reports_each_failure_and_finishes_the_rest():
    done, failed = [], []

    def work(item, cancel):
        if item == 3:
            raise TransferError("boom")
        return item * 2

    transfer.run_parallel(
        range(6),
        work,
        lambda item, value: done.append(value),
        lambda item, exc: failed.append((item, exc.message)),
    )
    assert sorted(done) == [0, 2, 4, 8, 10]
    assert failed == [(3, "boom")]


def test_run_parallel_cancels_workers_when_interrupted():
    outcome = []
    slow_started = threading.Event()

    def work(item, cancel):
        if item == "fast":
            slow_started.wait(5)
        else:
            slow_started.set()
            outcome.append(cancel.wait(5))

    def interrupt(item, value):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        transfer.run_parallel(["fast", "slow"], work, interrupt, lambda *args: None)
    assert outcome == [True]
