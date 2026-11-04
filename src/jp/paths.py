"""Path safety layer -- the #1 security boundary of jp (see docs/architecture.md).

This module is the *only* place allowed to translate between local filesystem
paths and remote Contents-API paths, and it is the gatekeeper for every
mutating remote call. The threat model is a SHARED machine and a possibly
hostile remote server, so the rules are strict and intentionally paranoid:

  * ``normalize_rel``      -- collapse a user/local path to a safe POSIX
                              relative path; reject anything that escapes.
  * ``validate_prefix``    -- reject empty / root / "shared"-like prefixes.
  * ``remote_path_for``    -- compose prefix + rel into the API path.
  * ``assert_within_prefix`` -- MUST be called IMMEDIATELY before every
                              PUT/PATCH/DELETE. Uses a trailing-slash compare,
                              NEVER a bare ``startswith(prefix)``.
  * ``safe_local_dest``    -- sanitize a name/path coming FROM the server
                              (anti Zip-Slip / CWE-22) before we ever write it.
  * ``atomic_write``       -- write-to-temp-then-rename, refusing to follow
                              symlinks, so a planted symlink can't redirect a
                              write outside the working tree.
  * ``find_root``          -- locate the repo root (dir containing ``.jp``).

Nothing the path layer produces for the remote may start with a dot, and our
temporary remote dir is ``jp-tmp`` (no leading dot) on purpose (see docs/architecture.md).
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .errors import SafetyError

# Name of the per-repo metadata directory living at the local root.
DOT_DIR = ".jp"
# Temporary name of an unfinished chunked upload. Not hidden, because the server
# refuses dotfiles, so every remote scan has to skip it instead.
PART_PREFIX = "__jp_part__."
# Remote temp dir used for atomic remote writes. Deliberately has NO leading dot
# because the server runs with allow_hidden=False (a dotted name -> HTTP 400).
REMOTE_TMP_DIR = "jp-tmp"

# Windows reserved device names (case-insensitive, with or without extension).
_WIN_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Prefixes we refuse to operate on: too broad / shared spaces (see docs/architecture.md).
# These are common names for multi-user shared roots ("compartilhado" is Portuguese
# for "shared", "lapix" is a shared-lab directory); a user (or a tampered
# .jp/config) pointing a workspace there could damage another group's data, so they
# are refused as ANY path segment, not just the first.
_FORBIDDEN_PREFIXES = {
    "",
    ".",
    "/",
    "shared",
    "share",
    "public",
    "common",
    "compartilhado",
    "lapix",
}


# --------------------------------------------------------------------------- #
# Relative-path normalization
# --------------------------------------------------------------------------- #
def normalize_rel(rel: str) -> str:
    """Normalize an arbitrary path into a safe POSIX-relative path string.

    Returns a clean ``a/b/c`` form (forward slashes, no leading slash, no
    trailing slash). Raises :class:`SafetyError` if the path is absolute, a
    Windows drive/UNC path, or escapes its base via ``..``.

    This is applied to user input AND to local paths discovered by walking the
    working tree, so a symlink or a crafted filename cannot smuggle traversal.
    """
    if rel is None:
        raise SafetyError("empty path is not allowed")

    # Normalize Unicode to NFC so a name created on macOS (NFD) and Linux (NFC)
    # maps to ONE logical key (avoids false "new"/"conflict" in the index and
    # stops a hostile listing from showing the same name in two byte forms).
    raw = unicodedata.normalize("NFC", str(rel)).strip()
    if raw == "":
        raise SafetyError("empty path is not allowed")

    # Embedded NUL is never a legitimate path component.
    if "\x00" in raw:
        raise SafetyError("path contains a NUL byte")

    # Normalize separators: treat backslashes as separators too, so a Windows
    # style path supplied on any OS is handled consistently.
    unified = raw.replace("\\", "/")

    # Reject absolute POSIX paths and Windows drive / UNC paths outright.
    if unified.startswith("/"):
        raise SafetyError(f"absolute paths are not allowed: {raw!r}")
    if _looks_like_windows_absolute(raw):
        raise SafetyError(f"absolute / drive paths are not allowed: {raw!r}")

    # Collapse using POSIX semantics. posixpath.normpath turns 'a/./b' -> 'a/b'
    # and resolves internal '..' where possible.
    collapsed = posixpath.normpath(unified)

    if collapsed in (".", ""):
        raise SafetyError("path resolves to the repository root, not a file")

    # After collapsing, any remaining leading '..' means it escapes the base.
    parts = collapsed.split("/")
    if parts and parts[0] == "..":
        raise SafetyError(f"path escapes the repository root: {raw!r}")
    # Belt-and-suspenders: no component may be '..' or absolute.
    for part in parts:
        if part == "..":
            raise SafetyError(f"path escapes the repository root: {raw!r}")
        if part == "":
            # internal empty component (e.g. 'a//b' already collapsed) -- ok,
            # but a leading empty would mean absolute which we already rejected.
            raise SafetyError(f"malformed path: {raw!r}")

    return collapsed


def _looks_like_windows_absolute(raw: str) -> bool:
    """True for ``C:\\x``, ``C:/x``, ``\\\\server\\share`` style paths."""
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        return True
    # UNC path (// after unification could also be a doubled separator, but
    # treating it as suspicious-absolute is the safe choice).
    return raw.startswith("\\\\") or raw.startswith("//")


def is_hidden(rel: str) -> bool:
    """True if any component of the relative path starts with a dot.

    Used to enforce the dotfile-skip policy (see docs/architecture.md): the server rejects
    hidden uploads, so we never PUT them and report them instead.
    """
    norm = rel.replace("\\", "/")
    return any(part.startswith(".") for part in norm.split("/") if part)


# --------------------------------------------------------------------------- #
# Dotfile "protect" encoding
# --------------------------------------------------------------------------- #
# The server runs with allow_hidden=False and rejects any name starting with a
# dot. Under the "protect" dotfile policy, jp stores a hidden segment under a
# reversible, server-safe alias so the file can still be uploaded -- and restores
# the real name on pull. ".foo" <-> "__jpdot__1_foo"; the leading digit records
# how many dots were stripped so "..foo" <-> "__jpdot__2_foo" round-trips too.
#
# Trade-off: a real (non-hidden) file literally named like the alias (e.g.
# "__jpdot__1_foo") would be decoded back to ".foo" on pull. The marker is
# deliberately distinctive to make that collision unlikely; it is documented.
_DOT_MARKER = "__jpdot__"
_DOT_ENC_RE = re.compile(r"^__jpdot__(\d+)_(.*)$", re.DOTALL)


def _encode_segment(seg: str) -> str:
    if not seg.startswith("."):
        return seg
    n = len(seg) - len(seg.lstrip("."))
    return f"{_DOT_MARKER}{n}_{seg[n:]}"


def _decode_segment(seg: str) -> str:
    m = _DOT_ENC_RE.match(seg)
    if not m:
        return seg
    return "." * int(m.group(1)) + m.group(2)


def encode_protected(rel: str) -> str:
    """Map a (possibly hidden) relative path to its server-safe encoded form.

    Non-hidden segments pass through unchanged, so this is safe to apply to any
    path when the protect policy is active.
    """
    return "/".join(_encode_segment(p) for p in rel.split("/") if p != "")


def decode_protected(rel: str) -> str:
    """Inverse of :func:`encode_protected`: restore real (dotted) names."""
    return "/".join(_decode_segment(p) for p in rel.split("/") if p != "")


def is_protected_encoded(rel: str) -> bool:
    """True if any segment is a protect-encoded dotfile alias."""
    return any(_DOT_ENC_RE.match(p) for p in rel.split("/") if p)


# --------------------------------------------------------------------------- #
# Prefix validation & remote path composition
# --------------------------------------------------------------------------- #
def validate_prefix(prefix: str) -> str:
    """Validate and normalize the remote *prefix* (the server-side root).

    Refuses empty, root, ``.``, and shared-space names so jp can never be
    pointed at a whole-server or shared directory (see docs/architecture.md).
    Returns the normalized prefix with NO leading or trailing slash.
    """
    if prefix is None:
        raise SafetyError("remote prefix is required")

    cleaned = unicodedata.normalize("NFC", str(prefix)).strip().replace("\\", "/")
    # Strip leading/trailing slashes; collapse internal duplicates.
    cleaned = cleaned.strip("/")
    if cleaned == "":
        raise SafetyError("refusing an empty remote prefix -- it would map to the whole server")

    norm = posixpath.normpath(cleaned)
    if norm in (".", "", "/"):
        raise SafetyError("refusing the server root as a prefix")
    if norm.startswith("..") or "/.." in norm:
        raise SafetyError(f"prefix escapes the server root: {prefix!r}")

    # Refuse a shared/too-broad name as ANY segment, not just the first, so that
    # 'projetos/compartilhado' or 'me/lapix' are also rejected.
    if norm.lower() in _FORBIDDEN_PREFIXES:
        raise SafetyError(
            f"refusing a shared/too-broad remote prefix: {prefix!r}. "
            "Choose a personal subdirectory instead."
        )
    for part in norm.split("/"):
        if part.lower() in _FORBIDDEN_PREFIXES and part != "":
            raise SafetyError(
                f"refusing a prefix containing the shared/protected name {part!r}: "
                f"{prefix!r}. Choose a personal subdirectory instead."
            )
        # No component may be hidden (server rejects hidden paths anyway).
        if part.startswith("."):
            raise SafetyError(f"remote prefix components may not start with '.': {prefix!r}")

    return norm


def remote_path_for(prefix: str, rel: str) -> str:
    """Compose the absolute remote (Contents API) path for ``rel`` under ``prefix``.

    Both inputs are validated/normalized; the result is ``prefix/rel`` with no
    leading slash (Contents API paths are root-relative without a leading '/').
    """
    norm_prefix = validate_prefix(prefix)
    norm_rel = normalize_rel(rel)
    return posixpath.join(norm_prefix, norm_rel)


def assert_within_prefix(remote_path: str, prefix: str) -> str:
    """Assert ``remote_path`` is strictly inside ``prefix``; else refuse.

    MUST be called IMMEDIATELY before every PUT/PATCH/DELETE. We compare using a
    trailing-slash terminator so that ``prefixevil`` is NOT considered inside
    ``prefix`` -- a bare ``startswith(prefix)`` would be exploitable.
    """
    norm_prefix = validate_prefix(prefix)
    candidate = str(remote_path).strip().replace("\\", "/").lstrip("/")
    candidate = posixpath.normpath(candidate)

    if candidate.startswith("..") or "/.." in candidate:
        raise SafetyError(f"remote path escapes its prefix: {remote_path!r}")

    # The path may equal the prefix only when it IS the prefix dir itself; for
    # file operations we require it to be strictly *under* the prefix.
    base = norm_prefix + "/"
    if not (candidate == norm_prefix or candidate.startswith(base)):
        raise SafetyError(
            f"refusing to operate outside the configured prefix: "
            f"{remote_path!r} is not under {norm_prefix!r}"
        )
    if candidate == norm_prefix:
        raise SafetyError(f"refusing to operate on the prefix root itself: {remote_path!r}")
    return candidate


# --------------------------------------------------------------------------- #
# Local destination sanitization (anti Zip-Slip / CWE-22)
# --------------------------------------------------------------------------- #
def safe_local_dest(root: Path, server_name: str) -> Path:
    """Resolve a server-supplied name to a safe path INSIDE ``root``.

    The server is untrusted: a listing may return ``../../etc/passwd``,
    ``/etc/shadow``, ``C:\\Windows\\x``, ``foo/../../bar``, a name containing a
    NUL, or a Windows reserved device name. We strip it down to a relative path,
    re-run it through :func:`normalize_rel`, and then verify the *resolved*
    absolute path is still contained within the resolved ``root`` (defeating
    symlinked intermediate directories too).
    """
    root_abs = Path(root).resolve()

    name = str(server_name).replace("\\", "/")
    if "\x00" in name:
        raise SafetyError("server returned a path containing a NUL byte")

    # Drop a leading slash / drive so an absolute server path is forced relative.
    name = name.lstrip("/")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise SafetyError(f"server returned an absolute drive path: {server_name!r}")

    # normalize_rel rejects traversal and absoluteness.
    rel = normalize_rel(name)

    # Reject Windows reserved device names in any component (cross-platform
    # safety -- such a file can be dangerous when synced to Windows).
    for part in rel.split("/"):
        stem = part.split(".")[0].lower()
        if stem in _WIN_RESERVED:
            raise SafetyError(f"server returned a reserved device name: {part!r}")

    candidate = (root_abs / rel).resolve()

    # Final containment check on the resolved real path (handles symlinked
    # ancestors). Use os.path.commonpath for a robust prefix test.
    try:
        common = os.path.commonpath([str(root_abs), str(candidate)])
    except ValueError as exc:
        # Different drives on Windows -> definitely outside.
        raise SafetyError(f"server path escapes the working tree: {server_name!r}") from exc
    if common != str(root_abs):
        raise SafetyError(f"server path escapes the working tree: {server_name!r}")

    return candidate


# --------------------------------------------------------------------------- #
# Safe atomic local write (refuses to follow symlinks)
# --------------------------------------------------------------------------- #
def atomic_write(dest: Path, data: bytes, *, durable: bool = True) -> None:
    """Atomically write ``data`` to ``dest`` without following a symlink.

    If ``dest`` (or its parent) is a symlink we refuse, so a pre-planted symlink
    on a shared machine cannot redirect our write to an arbitrary location
    (e.g. ``~/.ssh/authorized_keys``). We write to a temp file in the same
    directory and ``os.replace`` it into place (atomic on the same filesystem).
    """
    dest = Path(dest)
    parent = dest.parent

    # Create parent dirs, but never traverse through an existing symlinked dir.
    _ensure_dir_no_symlink(parent)

    # Refuse to overwrite through a symlink at the destination itself.
    if dest.is_symlink():
        raise SafetyError(f"refusing to write through a symlink: {dest}")

    tmp = parent / f".jp-write-{os.getpid()}-{abs(hash(str(dest))) & 0xFFFFFF}.tmp"
    # If a stale temp symlink exists, refuse rather than follow it.
    if tmp.is_symlink():
        raise SafetyError(f"refusing to write through a symlinked temp file: {tmp}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise
    os.replace(str(tmp), str(dest))


# O_NOFOLLOW exists on POSIX; on Windows it is absent, so fall back to 0 and
# rely on the explicit is_symlink() checks above.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_part(path: Path) -> BinaryIO:
    """Open a private ``.jp`` part file for appending, refusing a planted symlink."""
    _ensure_dir_no_symlink(Path(path).parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "ab")


def prepare_part(path: Path, size: int) -> None:
    """Create a private ``.jp`` part file of ``size`` bytes for segmented writes."""
    _ensure_dir_no_symlink(Path(path).parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as part:
        part.truncate(size)


def open_segment(path: Path) -> BinaryIO:
    """Open a prepared part file so one worker can write its own region."""
    return os.fdopen(os.open(str(path), os.O_RDWR | _O_NOFOLLOW), "r+b")


def atomic_install(part: Path, dest: Path) -> None:
    """Move a finished part onto ``dest`` with the guarantees of :func:`atomic_write`."""
    dest = Path(dest)
    _ensure_dir_no_symlink(dest.parent)
    if dest.is_symlink():
        raise SafetyError(f"refusing to write through a symlink: {dest}")
    os.replace(str(part), str(dest))


def _ensure_dir_no_symlink(directory: Path) -> None:
    """mkdir -p ``directory`` but refuse if any existing ancestor is a symlink."""
    directory = Path(directory)
    # Walk from the topmost missing ancestor down, checking symlinks.
    parts: list[Path] = []
    cur = directory
    while True:
        parts.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for node in reversed(parts):
        if node.exists():
            if node.is_symlink():
                raise SafetyError(f"refusing to traverse a symlinked directory: {node}")
            continue
        # Does not exist yet -- create it (parent already verified not a symlink).
        try:
            os.mkdir(str(node))
        except FileExistsError:
            # Race: someone created it; re-check it isn't a symlink.
            if Path(node).is_symlink():
                raise SafetyError(f"refusing to traverse a symlinked directory: {node}") from None


# --------------------------------------------------------------------------- #
# Repo root discovery
# --------------------------------------------------------------------------- #
def find_root(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` (default: cwd) to find the dir holding ``.jp``.

    The search stops at ``$HOME`` (inclusive) and at the filesystem root, so a
    stray ``.jp`` directory living above the user's home can never silently
    capture an unrelated working directory. Returns the repo root Path, or None
    if not inside a jp repo.
    """
    cur = Path(start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    while True:
        if (cur / DOT_DIR).is_dir():
            return cur
        if home is not None and cur == home:
            return None  # checked $HOME itself; never look above it
        if cur.parent == cur:
            return None  # filesystem root
        cur = cur.parent


def to_pureposix(rel: str) -> PurePosixPath:
    """Helper: normalized rel as a PurePosixPath (for callers that want parts)."""
    return PurePosixPath(normalize_rel(rel))
