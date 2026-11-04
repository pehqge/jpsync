"""Bounded-memory transfers for large files, and the worker pool for bulk sync.

A file of any size stays out of memory: uploads go in chunks to a temporary name
and are renamed only after the server's sha256 matches, downloads stream to
``.jp/parts`` and replace the destination only after the local sha256 matches.
A dropped connection resumes from the byte the other side really has, inside the
run through retries and across runs through the leftover part.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import BinaryIO, TypeVar

from . import paths, ui
from .api import Api, RemoteEntry
from .errors import ApiError, JpError, NetworkError, ServerDownError, TransferError

CHUNK_BYTES = 8 * 1024 * 1024
LARGE_FILE_BYTES = 16 * 1024 * 1024
WORKERS = 6
# One connection to a Hub carries far less than the line can, and several add up.
DOWNLOAD_CONNECTIONS = 6
SEGMENT_BYTES = 32 * 1024 * 1024
MAX_RETRIES = 5
BACKOFF_S = 1.0
# The server reads the whole file to hash it, which outlasts the normal timeout.
HASH_TIMEOUT_S = 900.0
PROGRESS_INTERVAL_S = 0.5

Item = TypeVar("Item")
Result = TypeVar("Result")


class Cancelled(JpError):
    """The run was interrupted; whatever was sent stays resumable."""


class _RangeIgnored(Exception):
    """The server answered a segment request with the whole file."""


def _ignore_progress(_: int) -> None:
    return None


def is_transient(exc: NetworkError) -> bool:
    if isinstance(exc, ApiError):
        return exc.status is None or exc.status == 429 or exc.status >= 500
    return not isinstance(exc, ServerDownError)


def _pause(attempt: int, cancel: threading.Event) -> None:
    if cancel.wait(BACKOFF_S * 2 ** (attempt - 1)):
        raise Cancelled("interrupted")


def with_retries(call: Callable[[], Result], cancel: threading.Event) -> Result:
    attempt = 0
    while True:
        try:
            return call()
        except NetworkError as exc:
            attempt += 1
            if attempt > MAX_RETRIES or not is_transient(exc):
                raise
            _pause(attempt, cancel)


def run_parallel(
    items: Iterable[Item],
    work: Callable[[Item, threading.Event], Result],
    on_done: Callable[[Item, Result], None],
    on_error: Callable[[Item, JpError | OSError], None],
) -> None:
    """Run ``work`` on a pool; the callbacks stay on the calling thread."""
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(work, item, cancel): item for item in items}
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except (JpError, OSError) as exc:
                    on_error(item, exc)
                    continue
                on_done(item, result)
        except BaseException:
            cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
            raise


class Meter:
    """One live progress line on stderr. Silent when piped or under ``--quiet``."""

    def __init__(self, verb: str, total_bytes: int, total_files: int) -> None:
        self._verb = verb
        self._total_bytes = total_bytes
        self._total_files = total_files
        self._bytes = 0
        self._files = 0
        self._started = time.monotonic()
        self._last_draw = 0.0
        self._lock = threading.Lock()
        self._live = sys.stderr.isatty() and not ui.is_quiet() and total_files > 0

    def add(self, nbytes: int) -> None:
        with self._lock:
            self._bytes += nbytes
            self._draw()

    def file_done(self) -> None:
        with self._lock:
            self._files += 1
            self._draw()

    def _draw(self) -> None:
        now = time.monotonic()
        if not self._live or now - self._last_draw < PROGRESS_INTERVAL_S:
            return
        self._last_draw = now
        speed = self._bytes / max(now - self._started, 0.001)
        sys.stderr.write(
            f"\r\x1b[K{self._verb}: {self._files}/{self._total_files} files, "
            f"{_human(self._bytes)}/{_human(self._total_bytes)}, {_human(speed)}/s"
        )
        sys.stderr.flush()

    def close(self) -> None:
        if self._live:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()


def _human(nbytes: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if nbytes < 1024:
            return f"{nbytes:.0f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GiB"


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
def part_path(remote_path: str, sha: str) -> str:
    """Deterministic for (path, content), which is what lets a later run resume."""
    parent, _, _ = remote_path.rpartition("/")
    tag = hashlib.sha256(f"{remote_path}\0{sha}".encode()).hexdigest()[:24]
    leaf = f"{paths.PART_PREFIX}{tag}"
    return f"{parent}/{leaf}" if parent else leaf


def upload_large(
    api: Api,
    local: Path,
    remote_path: str,
    sha: str,
    size: int,
    prefix: str,
    cancel: threading.Event,
    progress: Callable[[int], None] = _ignore_progress,
) -> None:
    part = part_path(remote_path, sha)
    paths.assert_within_prefix(part, prefix)
    with local.open("rb") as source:
        for resume in (True, False):
            _send_chunks(api, source, part, size, resume, cancel, progress)
            digest = with_retries(lambda: api.hash(part, timeout=HASH_TIMEOUT_S), cancel)
            if digest is None or digest == sha:
                break
        else:
            raise TransferError(f"checksum mismatch uploading {local.name}: did it change?")
    _swap_into_place(api, part, remote_path)


def _remote_size(api: Api, part: str, size: int, cancel: threading.Event) -> int:
    entry = with_retries(lambda: api.stat(part, as_file=True), cancel)
    held = (entry.size or 0) if entry else 0
    return held if held <= size else 0


def _send_chunks(
    api: Api,
    source: BinaryIO,
    part: str,
    size: int,
    resume: bool,
    cancel: threading.Event,
    progress: Callable[[int], None],
) -> None:
    """Send what the server lacks. The server appends blindly, so after any failure
    the offset comes from its real file size: resending would duplicate bytes."""
    offset = _remote_size(api, part, size, cancel) if resume else 0
    progress(offset)
    failures = 0
    while offset < size:
        if cancel.is_set():
            raise Cancelled("interrupted")
        source.seek(offset)
        data = source.read(CHUNK_BYTES)
        if not data:
            raise TransferError(f"{source.name} shrank during the upload")
        is_last = offset + len(data) >= size
        number = _chunk_number(offset, is_last)
        try:
            api.put_chunk(part, data, number)
        except NetworkError as exc:
            failures += 1
            if failures > MAX_RETRIES or not is_transient(exc):
                raise
            _pause(failures, cancel)
            offset = _remote_size(api, part, size, cancel)
            continue
        failures = 0
        offset += len(data)
        progress(len(data))


def _chunk_number(offset: int, is_last: bool) -> int:
    """Number 1 tells the server to replace the file, so only byte 0 may use it."""
    if offset == 0:
        return 1
    return -1 if is_last else max(2, 1 + offset // CHUNK_BYTES)


def _swap_into_place(api: Api, part: str, remote_path: str) -> None:
    """The server's rename refuses an existing target, so the old version steps
    aside first and comes back if the swap fails."""
    if api.stat(remote_path, as_file=True) is None:
        api.rename(part, remote_path)
        return
    backup = f"{part}.old"
    api.delete(backup)
    api.rename(remote_path, backup)
    try:
        api.rename(part, remote_path)
    except JpError:
        api.rename(backup, remote_path)
        raise
    try:
        api.delete(backup)
    except JpError as exc:
        ui.warn(f"{remote_path} is updated, but its old copy {backup} stayed: {exc.message}")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def download_large(
    api: Api,
    remote_path: str,
    dest: Path,
    part: Path,
    size: int,
    digest: str | None,
    cancel: threading.Event,
    progress: Callable[[int], None] = _ignore_progress,
) -> str:
    """Stream to ``part``, then replace ``dest``. Returns the verified sha256."""
    for _ in range(2):
        try:
            _fetch_segments(api, remote_path, part, size, cancel, progress)
            sha = _file_sha256(part)
        except _RangeIgnored:
            _discard(part)
            sha = _fetch(api, remote_path, part, size, cancel, progress)
        if digest is None or sha == digest:
            _finished_log(part).unlink(missing_ok=True)
            paths.atomic_install(part, dest)
            return sha
        _discard(part)
    raise TransferError(f"checksum mismatch downloading {remote_path}")


def _finished_log(part: Path) -> Path:
    return part.with_name(f"{part.name}.done")


def _discard(part: Path) -> None:
    part.unlink(missing_ok=True)
    _finished_log(part).unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _finished_segments(part: Path, count: int) -> set[int]:
    log = _finished_log(part)
    if not part.exists() or not log.exists():
        return set()
    return {int(line) for line in log.read_text().split() if int(line) < count}


def _fetch_segments(
    api: Api,
    remote_path: str,
    part: Path,
    size: int,
    cancel: threading.Event,
    progress: Callable[[int], None],
) -> None:
    """Download the missing segments on several connections. A segment counts only
    once it is fully on disk, which is what lets a later run skip it."""
    count = math.ceil(size / SEGMENT_BYTES)
    finished = _finished_segments(part, count)
    missing = [index for index in range(count) if index not in finished]
    held = sum(_segment_end(index, size) - index * SEGMENT_BYTES for index in finished)
    _require_disk(part.parent, size - held)
    paths.prepare_part(part, size)
    progress(held)
    failed = threading.Event()

    def fetch(index: int) -> int:
        start = index * SEGMENT_BYTES
        end = _segment_end(index, size)
        _fetch_segment(api, remote_path, part, start, end, size, cancel, failed, progress)
        return index

    with ThreadPoolExecutor(max_workers=DOWNLOAD_CONNECTIONS) as pool:
        futures = [pool.submit(fetch, index) for index in missing]
        with _finished_log(part).open("a") as log:
            try:
                for future in as_completed(futures):
                    log.write(f"{future.result()}\n")
                    log.flush()
            except BaseException:
                failed.set()
                pool.shutdown(wait=False, cancel_futures=True)
                raise


def _segment_end(index: int, size: int) -> int:
    return min((index + 1) * SEGMENT_BYTES, size)


def _require_disk(folder: Path, needed: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < needed:
        raise TransferError(f"not enough disk: {_human(needed)} needed, {_human(free)} free")


def _fetch_segment(
    api: Api,
    remote_path: str,
    part: Path,
    start: int,
    end: int,
    size: int,
    cancel: threading.Event,
    failed: threading.Event,
    progress: Callable[[int], None],
) -> None:
    position = start
    failures = 0
    with paths.open_segment(part) as out:
        while position < end:
            before = position
            try:
                ranged, chunks = api.iter_file(remote_path, position, end)
                if not ranged and end - start < size:
                    raise _RangeIgnored
                out.seek(position)
                for chunk in chunks:
                    if cancel.is_set() or failed.is_set():
                        raise Cancelled("interrupted")
                    out.write(chunk)
                    position += len(chunk)
                    progress(len(chunk))
                if position < end:
                    raise NetworkError(f"connection closed at byte {position} of {size}")
            except NetworkError as exc:
                failures = 1 if position > before else failures + 1
                if failures > MAX_RETRIES or not is_transient(exc):
                    raise
                _pause(failures, cancel)
        out.flush()
        os.fsync(out.fileno())


def _hash_existing(part: Path, sha: hashlib._Hash, size: int) -> int:
    if not part.exists():
        return 0
    held = part.stat().st_size
    if held > size:
        part.unlink()
        return 0
    with part.open("rb") as existing:
        for block in iter(lambda: existing.read(1 << 20), b""):
            sha.update(block)
    return held


def _fetch(
    api: Api,
    remote_path: str,
    part: Path,
    size: int,
    cancel: threading.Event,
    progress: Callable[[int], None],
) -> str:
    sha = hashlib.sha256()
    offset = _hash_existing(part, sha, size)
    progress(offset)
    failures = 0
    with paths.open_part(part) as out:
        while offset < size:
            if cancel.is_set():
                raise Cancelled("interrupted")
            try:
                resumed, chunks = api.iter_file(remote_path, offset)
                if offset and not resumed:
                    out.truncate(0)
                    sha = hashlib.sha256()
                    offset = 0
                for chunk in chunks:
                    out.write(chunk)
                    sha.update(chunk)
                    offset += len(chunk)
                    progress(len(chunk))
                    if cancel.is_set():
                        out.flush()
                        raise Cancelled("interrupted")
                if offset >= size:
                    break
                raise NetworkError(f"connection closed at byte {offset} of {size}")
            except NetworkError as exc:
                out.flush()
                failures += 1
                if failures > MAX_RETRIES or not is_transient(exc):
                    raise
                _pause(failures, cancel)
        out.flush()
        os.fsync(out.fileno())
    if part.stat().st_size != size:
        part.unlink()
        raise TransferError(f"size mismatch downloading {remote_path}: expected {size} bytes")
    return sha.hexdigest()


def stream_sha256(api: Api, entry: RemoteEntry) -> str:
    sha = hashlib.sha256()
    for chunk in api.iter_file(entry.path)[1]:
        sha.update(chunk)
    return sha.hexdigest()
