"""The safe-sync engine: scan, hash, 3-way diff, push, pull (see docs/architecture.md).

Invariants enforced here (and exercised by the test suite):

  * NO DELETES. ``push`` and ``pull`` never remove a file on either side. The
    only deletions live in ``jp rm`` (gated). A file present on one side and
    absent on the other is simply uploaded/downloaded or left alone.
  * NO OVERWRITE ON CONFLICT. If the index base differs from BOTH the local and
    the remote content (a true 3-way conflict), we ABORT that file -- never
    "last writer wins".
  * DOTFILES SKIP, NEVER ABORT. Hidden files are skipped on push (the server
    rejects them with HTTP 400) and reported in the summary; a dotfile never
    aborts the whole run.
  * PER-FILE RESILIENCE. A benign per-file failure is collected and reported;
    the run continues and returns the partial-failure exit code at the end.
  * INDEX AFTER SUCCESS. The index entry for a file is written only after that
    file's transfer is verified; ``status``/``--dry-run`` never write anything.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, TypeVar

from . import bundle, paths, transfer, ui
from .api import Api, RemoteEntry
from .config import Config
from .errors import ApiError, JpError, TransferError
from .ignore import IgnoreSet
from .index import Entry, Index

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


# Rewriting the index once per file is quadratic in a bulk run. A crash can lose
# this much progress, which the next run recovers by comparing hashes.
INDEX_SAVE_INTERVAL_S = 2.0

Job = TypeVar("Job", "_Upload", "_Download")


class Change(Enum):
    """Per-file classification after a 3-way comparison."""

    UNCHANGED = "unchanged"
    LOCAL_NEW = "local_new"  # exists locally, not in index/remote
    LOCAL_MODIFIED = "local_modified"  # local differs from base, remote == base
    REMOTE_NEW = "remote_new"  # exists remotely, not in index/local
    REMOTE_MODIFIED = "remote_modified"  # remote differs from base, local == base
    CONFLICT = "conflict"  # both sides diverged from base
    SKIPPED_HIDDEN = "skipped_hidden"  # dotfile -> server rejects upload


@dataclass
class FileState:
    rel: str
    change: Change
    local_sha: str | None = None
    remote_sha: str | None = None  # may be None if we did not download remote
    base_sha: str | None = None
    local_exists: bool = False
    remote_exists: bool = False
    remote_entry: RemoteEntry | None = None


@dataclass
class Outcome:
    """Result of a push/pull run."""

    transferred: list[str] = field(default_factory=list)
    skipped_hidden: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    up_to_date: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (rel, reason)
    # Mirror-mode deletion CANDIDATES (never deleted by the engine; the command
    # layer confirms each one interactively before acting). For push: files that
    # exist remotely but not locally. For pull: files that exist locally but not
    # remotely.
    deletable: list[str] = field(default_factory=list)
    # Files actually deleted (filled in by the command layer after confirmation).
    deleted: list[str] = field(default_factory=list)

    @property
    def had_failures(self) -> bool:
        return bool(self.failures)

    @property
    def had_conflicts(self) -> bool:
        return bool(self.conflicts)


# --------------------------------------------------------------------------- #
# Hashing & local scan
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_local(root: Path, ignore: IgnoreSet) -> dict[str, Path]:
    """Walk the working tree and return {rel_posix: absolute_path} for files.

    Skips the ``.jp`` dir, ignored paths, and SYMLINKS (we never follow a
    symlink into or out of the tree). Every discovered rel path is run through
    ``normalize_rel`` so a weird filename cannot smuggle traversal.
    """
    root = Path(root).resolve()
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune ignored / metadata / symlinked directories in place.
        rel_dir = os.path.relpath(dirpath, root)
        keep: list[str] = []
        for d in dirnames:
            abs_d = Path(dirpath) / d
            if abs_d.is_symlink():
                continue  # never descend into a symlinked directory
            rel_d = _rel_posix(rel_dir, d)
            if rel_d == paths.DOT_DIR or ignore.is_ignored(rel_d, is_dir=True):
                continue
            keep.append(d)
        dirnames[:] = keep

        for f in filenames:
            abs_f = Path(dirpath) / f
            if abs_f.is_symlink():
                continue  # never sync symlinks
            rel = _rel_posix(rel_dir, f)
            try:
                norm = paths.normalize_rel(rel)
            except JpError:
                continue
            if ignore.is_ignored(norm, is_dir=False):
                continue
            found[norm] = abs_f
    return found


def _rel_posix(rel_dir: str, name: str) -> str:
    if rel_dir in (".", ""):
        return name
    return f"{rel_dir}/{name}".replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# Remote scan
# --------------------------------------------------------------------------- #
def scan_remote(api: Api, cfg: Config, ignore: IgnoreSet | None = None) -> dict[str, RemoteEntry]:
    """List the remote prefix level by level; return {rel_posix: RemoteEntry}.

    Keys are made RELATIVE to the prefix. Each remote name is validated via
    ``safe_local_dest``-style normalization so a hostile listing entry like
    ``../../etc/x`` cannot become a sync target. Ignored folders are not even
    listed, and jp's own unfinished uploads never show up.
    """
    prefix = paths.validate_prefix(cfg.prefix)
    result: dict[str, RemoteEntry] = {}
    pending = [prefix]
    with ThreadPoolExecutor(max_workers=transfer.WORKERS) as pool:
        while pending:
            listings = list(pool.map(lambda path: _list_remote_dir(api, path), pending))
            pending = []
            for entries in listings:
                pending += _collect_entries(entries, prefix, ignore, result)
    return result


def _list_remote_dir(api: Api, api_path: str) -> list[RemoteEntry]:
    # A single corrupted entry can make the server return 400 "is not a
    # directory" for a GET on the PARENT (observed: a trashed/half-deleted child
    # poisons the parent listing). Degrade with a warning instead of aborting the
    # whole status/pull (see docs/architecture.md).
    try:
        return api.list_dir(api_path)
    except ApiError as exc:
        ui.warn(f"skipping unreadable remote directory {api_path!r}: {exc.message}")
        return []


def _collect_entries(
    entries: list[RemoteEntry],
    prefix: str,
    ignore: IgnoreSet | None,
    acc: dict[str, RemoteEntry],
) -> list[str]:
    """File the entries into ``acc``; return the subfolders still to list."""
    subfolders: list[str] = []
    for entry in entries:
        # Defensive: validate the server-supplied path stays under the prefix.
        try:
            inside = paths.assert_within_prefix(entry.path, prefix)
        except JpError:
            # Skip anything the server claims is outside our prefix.
            continue
        rel = inside[len(prefix) + 1 :] if inside != prefix else ""
        if entry.name.startswith(paths.PART_PREFIX):
            continue
        if entry.type == "directory":
            if ignore is None or not ignore.is_ignored(rel, is_dir=True):
                subfolders.append(entry.path)
        elif rel:
            # Re-validate the relative name is traversal-free.
            try:
                norm = paths.normalize_rel(rel)
            except JpError:
                continue
            # A genuinely hidden wire name cannot be served on pull anyway; skip
            # it defensively. (Protect-encoded aliases are NOT hidden on the wire.)
            if paths.is_hidden(norm):
                continue
            # Decode protect aliases back to the canonical dotted key so they pair
            # with the matching local dotfile in the diff; the entry keeps its
            # encoded wire path for GET/hash. Decoding is policy-independent so
            # switching skip<->protect never strands already-uploaded files.
            key = paths.decode_protected(norm)
            try:
                key = paths.normalize_rel(key)
            except JpError:
                continue
            if ignore is None or not ignore.is_ignored(key):
                acc[key] = entry
    return subfolders


# --------------------------------------------------------------------------- #
# 3-way diff
# --------------------------------------------------------------------------- #
def diff(
    root: Path,
    cfg: Config,
    api: Api,
    index: Index,
    ignore: IgnoreSet,
    *,
    fetch_remote_content: bool = True,
) -> list[FileState]:
    """Compute the per-file 3-way state across local, remote and index.

    Pure analysis: NEVER writes locally or remotely (safe for ``status`` and
    ``--dry-run``). Remote content is hashed only when we have a candidate
    conflict or modification to resolve and ``fetch_remote_content`` is True.
    """
    local = scan_local(root, ignore)
    remote = scan_remote(api, cfg, ignore)
    known = (rel for rel in index.entries if not _under_ignored_rule(rel, ignore))
    rels = sorted(set(local) | set(remote) | set(known))
    hashed = _prefetch_hashes(api, local, remote, index) if fetch_remote_content else {}

    states: list[FileState] = []
    for rel in rels:
        lpath = local.get(rel)
        rentry = remote.get(rel)
        base = index.get(rel)

        local_exists = lpath is not None
        remote_exists = rentry is not None
        local_sha = sha256_file(lpath) if lpath is not None else None
        base_sha = base.sha256 if base else None

        # Compute remote sha lazily: cheap path uses size/mtime against index;
        # we only download to confirm a real change/conflict. A module-level
        # factory binds the loop values explicitly (no closure-over-loop-var bug).
        cache = _RemoteHashCache()
        cache.value = hashed.get(rel)
        remote_hash = _make_remote_hash(api, rentry, cache, fetch_remote_content)

        change = _classify(
            rel,
            local_exists=local_exists,
            remote_exists=remote_exists,
            local_sha=local_sha,
            base_sha=base_sha,
            remote_hash=remote_hash,
            base=base,
            rentry=rentry,
        )

        states.append(
            FileState(
                rel=rel,
                change=change,
                local_sha=local_sha,
                remote_sha=cache.value,
                base_sha=base_sha,
                local_exists=local_exists,
                remote_exists=remote_exists,
                remote_entry=rentry,
            )
        )
    return states


def _under_ignored_rule(rel: str, ignore: IgnoreSet) -> bool:
    """True if ``rel`` or one of its parent folders is ignored."""
    parts = rel.split("/")
    folders = ("/".join(parts[:i]) for i in range(1, len(parts)))
    return ignore.is_ignored(rel) or any(ignore.is_ignored(d, is_dir=True) for d in folders)


def _prefetch_hashes(
    api: Api,
    local: dict[str, Path],
    remote: dict[str, RemoteEntry],
    index: Index,
) -> dict[str, str]:
    """Hash in parallel every file on both sides that has no base to compare with,
    the case of a first sync over an existing tree."""
    pending = [rel for rel in local if rel in remote and index.get(rel) is None]
    with ThreadPoolExecutor(max_workers=transfer.WORKERS) as pool:
        digests = pool.map(lambda rel: _server_hash(api, remote[rel]), pending)
    return {rel: digest for rel, digest in zip(pending, digests) if digest}


def _is_large(rentry: RemoteEntry) -> bool:
    return rentry.size is not None and rentry.size >= transfer.LARGE_FILE_BYTES


def _server_hash(api: Api, rentry: RemoteEntry) -> str | None:
    timeout = transfer.HASH_TIMEOUT_S if _is_large(rentry) else None
    try:
        return api.hash(rentry.path, timeout=timeout)
    except Exception:
        return None


class _RemoteHashCache:
    """Single-slot cache so a given remote file is downloaded at most once."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: str | None = None


def _make_remote_hash(
    api: Api,
    rentry: RemoteEntry | None,
    cache: _RemoteHashCache,
    fetch_remote_content: bool,
) -> Callable[[], str | None]:
    """Return a zero-arg lazy hasher bound to explicit args (no loop capture).

    PRIMARY path (research §1): ask the server for ``?content=0&hash=1`` -- it
    returns the sha256 of the raw bytes WITHOUT downloading the body (cheap even
    at 20 MiB). We only fall back to downloading the content when the server did
    not give us a usable sha256 (e.g. an older server). This eliminates the
    unnecessary full downloads the prototype did just to compare files.
    """

    def remote_hash() -> str | None:
        if cache.value is not None or rentry is None:
            return cache.value
        if not fetch_remote_content:
            return None
        # Cheap server-side sha256 -- no body transfer.
        digest = _server_hash(api, rentry)
        if digest:
            cache.value = digest
            return cache.value
        # Fallback: server gave no hash -> download and hash locally.
        if _is_large(rentry):
            cache.value = transfer.stream_sha256(api, rentry)
        else:
            cache.value = sha256_bytes(api.get_file_bytes(rentry.path))
        return cache.value

    return remote_hash


def _remote_matches_base(base: Entry | None, rentry: RemoteEntry | None) -> bool | None:
    """Cheap, no-download equality guess between remote and base.

    Returns True/False if we can decide from size+mtime, else None ("unsure").
    """
    if base is None or rentry is None:
        return None
    if rentry.size is not None and rentry.size != base.size:
        return False
    if base.remote_mtime and rentry.last_modified and base.remote_mtime == rentry.last_modified:
        return True
    return None


def _classify(
    rel: str,
    *,
    local_exists: bool,
    remote_exists: bool,
    local_sha: str | None,
    base_sha: str | None,
    remote_hash,
    base: Entry | None,
    rentry: RemoteEntry | None,
) -> Change:
    """Map the (local, remote, base) triple to a Change per the the design docstable."""
    local_changed = (local_sha != base_sha) if local_exists else (base_sha is not None)

    # No base yet: decide purely from existence (and only download when BOTH
    # sides exist and we must check for a real conflict). Crucially, a file that
    # exists on only one side never triggers a remote download here, so a flaky
    # GET cannot break classification for plain new files.
    if base is None:
        if local_exists and remote_exists:
            rsha = remote_hash()
            if rsha == local_sha:
                return Change.UNCHANGED
            return Change.CONFLICT
        if local_exists:
            return Change.LOCAL_NEW
        if remote_exists:
            return Change.REMOTE_NEW
        return Change.UNCHANGED  # only in index, gone both sides -> nothing to do

    # Resolve remote-vs-base, downloading only if the cheap check is unsure.
    if not remote_exists:
        remote_changed = base_sha is not None
        rsha = None
    else:
        cheap = _remote_matches_base(base, rentry)
        if cheap is True:
            remote_changed = False
            rsha = base_sha
        elif cheap is False:
            rsha = remote_hash()
            remote_changed = rsha != base_sha
        else:
            rsha = remote_hash()
            remote_changed = rsha != base_sha

    # We have a base.
    if not local_changed and not remote_changed:
        return Change.UNCHANGED
    if local_changed and not remote_changed:
        return Change.LOCAL_MODIFIED if local_exists else Change.UNCHANGED
    if remote_changed and not local_changed:
        return Change.REMOTE_MODIFIED if remote_exists else Change.UNCHANGED
    # Both changed relative to base -> true conflict.
    # If they happen to have converged to the same content, it's not a conflict.
    if local_exists and remote_exists:
        rsha = rsha if rsha is not None else remote_hash()
        if rsha == local_sha:
            return Change.UNCHANGED
    return Change.CONFLICT


# --------------------------------------------------------------------------- #
# PUSH (local -> remote). Never deletes. Skips dotfiles. Aborts conflicts.
# --------------------------------------------------------------------------- #
def push(
    root: Path,
    cfg: Config,
    api: Api,
    index: Index,
    ignore: IgnoreSet,
    *,
    dry_run: bool = False,
) -> Outcome:
    prefix = paths.validate_prefix(cfg.prefix)
    outcome = Outcome()
    states = diff(root, cfg, api, index, ignore)
    uploads: list[_Upload] = []
    # "protect" uploads dotfiles under a reversible server-safe alias; the default
    # "skip" policy reports them and never uploads (the server rejects hidden names).
    protect = cfg.dotfiles == "protect"

    for st in states:
        rel = st.rel
        # Dotfiles under the default "skip" policy: report + skip, NEVER abort the
        # run (fixes the prototype bug). Under "protect" they fall through and are
        # uploaded below under an encoded name.
        if paths.is_hidden(rel) and not protect:
            if st.local_exists and st.change in (
                Change.LOCAL_NEW,
                Change.LOCAL_MODIFIED,
                Change.CONFLICT,
            ):
                outcome.skipped_hidden.append(rel)
            continue

        if st.change == Change.CONFLICT:
            outcome.conflicts.append(rel)
            continue
        if st.change not in (Change.LOCAL_NEW, Change.LOCAL_MODIFIED):
            if st.change == Change.UNCHANGED and st.local_exists:
                outcome.up_to_date.append(rel)
            continue

        # We are going to upload this file.
        if dry_run:
            outcome.transferred.append(rel)
            continue

        # Under "protect", hidden segments are aliased to a server-safe name
        # (non-hidden segments pass through unchanged); the index/outcome keep
        # the real (dotted) rel as the canonical key.
        wire_rel = paths.encode_protected(rel) if protect else rel
        remote_path = paths.remote_path_for(prefix, wire_rel)
        uploads.append(_Upload(rel, root / rel, remote_path, st.local_sha))

    if uploads:
        created_dirs: set[str] = set()
        meter = transfer.Meter("push", _local_bytes(uploads), len(uploads))

        def upload(job: _Upload, cancel: threading.Event) -> Entry:
            entry = _upload_one(api, prefix, job, created_dirs, cancel, meter)
            meter.file_done()
            return entry

        try:
            remaining = _push_small_files_as_archive(
                api, root, cfg, uploads, ignore, created_dirs, index, outcome, meter
            )
            _run_jobs(remaining, upload, index, outcome)
        finally:
            meter.close()

    # Mirror-mode candidates: remote files with no local counterpart. The engine
    # NEVER deletes -- the command layer confirms each one (see commands/push.py).
    outcome.deletable = [
        st.rel
        for st in states
        if st.remote_exists and not st.local_exists and not paths.is_hidden(st.rel)
    ]
    return outcome


@dataclass(frozen=True)
class _Upload:
    rel: str
    path: Path
    remote_path: str
    sha: str | None


def _local_bytes(uploads: list[_Upload]) -> int:
    return sum(_safe_size(job.path) for job in uploads)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _upload_one(
    api: Api,
    prefix: str,
    job: _Upload,
    created_dirs: set[str],
    cancel: threading.Event,
    meter: transfer.Meter,
) -> Entry:
    """Upload and verify one file. Raises instead of touching the index."""
    size = job.path.stat().st_size
    # SAFETY: assert immediately before the mutating calls.
    paths.assert_within_prefix(job.remote_path, prefix)
    transfer.with_retries(
        lambda: _ensure_remote_dirs(api, prefix, job.remote_path, created_dirs), cancel
    )
    rentry: RemoteEntry | None = None
    if size >= transfer.LARGE_FILE_BYTES:
        sha = job.sha or sha256_file(job.path)
        transfer.upload_large(api, job.path, job.remote_path, sha, size, prefix, cancel, meter.add)
    else:
        data = job.path.read_bytes()
        size = len(data)
        sha = sha256_bytes(data)
        # A whole-file PUT is written atomically by the server (threat model T11),
        # so it needs no temporary name and repeating it is harmless.
        rentry = transfer.with_retries(lambda: api.put_file_bytes(job.remote_path, data), cancel)
        meter.add(size)
    # as_file=True: a jupytext server would otherwise report the
    # converted-notebook size for a .md/.py file and trip a false
    # "post-write size mismatch" on every such upload. The PUT response already
    # answers for the common case, which halves the requests of a bulk run.
    if rentry is None or rentry.size != size:
        rentry = transfer.with_retries(lambda: api.stat(job.remote_path, as_file=True), cancel)
    if rentry is not None and rentry.size is not None and rentry.size != size:
        raise TransferError(f"post-write size mismatch (sent {size}, server {rentry.size})")
    return Entry(
        sha256=sha,
        size=size,
        remote_mtime=rentry.last_modified if rentry else "",
        remote_hash=sha,  # server sha256 == agreed bytes' sha256
        local_mtime=_safe_mtime(job.path),
    )


def _push_small_files_as_archive(
    api: Api,
    root: Path,
    cfg: Config,
    uploads: list[_Upload],
    ignore: IgnoreSet,
    created_dirs: set[str],
    index: Index,
    outcome: Outcome,
    meter: transfer.Meter,
) -> list[_Upload]:
    """Send the small files as one archive. Returns what still needs a normal upload,
    which is everything when the server cannot unpack."""
    prefix = paths.validate_prefix(cfg.prefix)
    small = [job for job in uploads if _safe_size(job.path) < transfer.LARGE_FILE_BYTES]
    if len(small) < bundle.MIN_FILES:
        return uploads
    by_wire = {job.remote_path[len(prefix) + 1 :]: job for job in small}
    archive = root / paths.DOT_DIR / "parts" / f"push-{os.getpid()}.tar"
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        packed = bundle.pack_local({name: job.path for name, job in by_wire.items()}, archive)
        _ensure_remote_dirs(api, prefix, f"{prefix}/placeholder", created_dirs)
        bundle.send_archive(api, cfg, archive, meter.add)
        listing = scan_remote(api, cfg, ignore)
    except bundle.Unavailable as exc:
        ui.warn(f"sending files one by one, the archive step failed: {exc}")
        return uploads
    finally:
        archive.unlink(missing_ok=True)
    sent: set[str] = set()
    for name, job in by_wire.items():
        sha, size = packed[name]
        rentry = listing.get(job.rel)
        if rentry is None or rentry.size != size:
            continue
        index.set(
            job.rel,
            Entry(
                sha256=sha,
                size=size,
                remote_mtime=rentry.last_modified,
                remote_hash=sha,
                local_mtime=_safe_mtime(job.path),
            ),
        )
        outcome.transferred.append(job.rel)
        sent.add(job.rel)
        meter.file_done()
    index.save()
    return [job for job in uploads if job.rel not in sent]


def _pull_small_files_as_archive(
    api: Api,
    root: Path,
    cfg: Config,
    downloads: list[_Download],
    index: Index,
    outcome: Outcome,
    meter: transfer.Meter,
) -> list[_Download]:
    """Fetch the small files as one archive. Returns what still needs a normal
    download, which is everything when the server cannot pack."""
    prefix = paths.validate_prefix(cfg.prefix)
    small = [job for job in downloads if not _is_large(job.entry)]
    if len(small) < bundle.MIN_FILES:
        return downloads
    by_wire = {job.entry.path[len(prefix) + 1 :]: job for job in small}
    parts = root / paths.DOT_DIR / "parts"
    archive = parts / f"pull-{os.getpid()}.tar"
    received: set[str] = set()
    try:
        bundle.fetch_archive(
            api, cfg, sorted(by_wire), archive, archive.with_suffix(".part"), lambda _: None
        )
        for name, data in bundle.read_archive(archive, by_wire):
            job = by_wire[name]
            if job.entry.size is not None and len(data) != job.entry.size:
                continue
            dest = paths.safe_local_dest(root, job.rel)
            paths.atomic_write(dest, data, durable=False)
            sha = sha256_bytes(data)
            index.set(
                job.rel,
                Entry(
                    sha256=sha,
                    size=len(data),
                    remote_mtime=job.entry.last_modified,
                    remote_hash=sha,
                    local_mtime=_safe_mtime(dest),
                ),
            )
            outcome.transferred.append(job.rel)
            received.add(job.rel)
            meter.add(len(data))
            meter.file_done()
    except bundle.Unavailable as exc:
        ui.warn(f"downloading files one by one, the archive step failed: {exc}")
    finally:
        archive.unlink(missing_ok=True)
    index.save()
    return [job for job in downloads if job.rel not in received]


def _run_jobs(
    jobs: list[Job],
    work: Callable[[Job, threading.Event], Entry],
    index: Index,
    outcome: Outcome,
) -> None:
    """Run the transfers; the index reflects only verified successes."""
    last_save = time.monotonic()

    def record(job: Job, entry: Entry) -> None:
        nonlocal last_save
        index.set(job.rel, entry)
        outcome.transferred.append(job.rel)
        if time.monotonic() - last_save >= INDEX_SAVE_INTERVAL_S:
            index.save()
            last_save = time.monotonic()

    def fail(job: Job, exc: JpError | OSError) -> None:
        outcome.failures.append((job.rel, exc.message if isinstance(exc, JpError) else str(exc)))

    try:
        transfer.run_parallel(jobs, work, record, fail)
    finally:
        index.save()
    outcome.transferred.sort()
    outcome.failures.sort()


def _ensure_remote_dirs(api: Api, prefix: str, remote_path: str, created: set[str]) -> None:
    """Create the prefix directory tree and the file's intermediate dirs.

    This server does NOT auto-create parent directories on a file PUT (a missing
    parent yields HTTP 500), and a freshly-cloned prefix may not exist remotely
    at all -- so we create every level explicitly and idempotently.

      1. The prefix's own segments are created top-down. ``validate_prefix`` has
         already refused the server root and shared spaces, so these are the
         user's own folders; ``mkdir`` is create-only and idempotent (it never
         deletes), so existing dirs are a no-op.
      2. The file's intermediate directories (strictly under the prefix) are then
         created, each re-validated to stay inside the prefix.
    """
    # 1) Ensure the prefix tree (and its ancestors) exist.
    cur = ""
    for seg in prefix.split("/"):
        cur = seg if cur == "" else f"{cur}/{seg}"
        if cur in created:
            continue
        api.mkdir(cur)
        created.add(cur)
    # 2) Ensure the file's intermediate dirs, confined to the prefix.
    rel = remote_path[len(prefix) + 1 :]
    cur = prefix
    for seg in rel.split("/")[:-1]:
        cur = f"{cur}/{seg}"
        if cur in created:
            continue
        paths.assert_within_prefix(cur, prefix)
        api.mkdir(cur)
        created.add(cur)


# --------------------------------------------------------------------------- #
# PULL (remote -> local). Never deletes. Aborts conflicts.
# --------------------------------------------------------------------------- #
def pull(
    root: Path,
    cfg: Config,
    api: Api,
    index: Index,
    ignore: IgnoreSet,
    *,
    dry_run: bool = False,
) -> Outcome:
    root = Path(root).resolve()
    outcome = Outcome()
    states = diff(root, cfg, api, index, ignore)
    downloads: list[_Download] = []

    for st in states:
        rel = st.rel
        if st.change == Change.CONFLICT:
            outcome.conflicts.append(rel)
            continue
        if st.change not in (Change.REMOTE_NEW, Change.REMOTE_MODIFIED):
            if st.change == Change.UNCHANGED and st.remote_exists and st.local_exists:
                outcome.up_to_date.append(rel)
            continue
        if st.remote_entry is None:
            continue

        if dry_run:
            outcome.transferred.append(rel)
            continue

        downloads.append(_Download(rel, st.remote_entry, st.remote_sha))

    if downloads:
        total = sum(job.entry.size or 0 for job in downloads)
        meter = transfer.Meter("pull", total, len(downloads))

        def download(job: _Download, cancel: threading.Event) -> Entry:
            entry = _download_one(api, root, job, cancel, meter)
            meter.file_done()
            return entry

        try:
            remaining = _pull_small_files_as_archive(
                api, root, cfg, downloads, index, outcome, meter
            )
            _run_jobs(remaining, download, index, outcome)
        finally:
            meter.close()

    # Mirror-mode candidates: local files with no remote counterpart. The engine
    # NEVER deletes -- the command layer confirms each one (see commands/pull.py).
    outcome.deletable = [st.rel for st in states if st.local_exists and not st.remote_exists]
    return outcome


@dataclass(frozen=True)
class _Download:
    rel: str
    entry: RemoteEntry
    digest: str | None


def _download_one(
    api: Api, root: Path, job: _Download, cancel: threading.Event, meter: transfer.Meter
) -> Entry:
    """Download and verify one file. Raises instead of touching the index."""
    # SAFETY: sanitize the server-derived name before writing locally.
    dest = paths.safe_local_dest(root, job.rel)
    if _is_large(job.entry) and job.entry.size is not None:
        size = job.entry.size
        digest = job.digest or _server_hash(api, job.entry)
        part_name = hashlib.sha256(job.rel.encode()).hexdigest()
        part = root / paths.DOT_DIR / "parts" / part_name
        sha = transfer.download_large(
            api, job.entry.path, dest, part, size, digest, cancel, meter.add
        )
    else:
        data = transfer.with_retries(lambda: api.get_file_bytes(job.entry.path), cancel)
        paths.atomic_write(dest, data)
        size = len(data)
        sha = sha256_bytes(data)
        meter.add(size)
    return Entry(
        sha256=sha,
        size=size,
        remote_mtime=job.entry.last_modified,
        remote_hash=sha,  # downloaded bytes == server sha256
        local_mtime=_safe_mtime(dest),
    )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
