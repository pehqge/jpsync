"""``jp diff`` -- show a unified text diff of changed files (read-only).

For text files, prints a unified diff between the remote (or base) and local
content. Binary files are reported as "binary differs". Read-only: never writes.
"""

from __future__ import annotations

import argparse
import difflib

from .. import transfer, ui
from ..errors import EXIT_OK
from ..sync import Change
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("diff", help="show unified diffs of changed files (read-only)")
    p.add_argument("path", nargs="?", default="", help="limit to a single relative path")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    from .. import sync

    states = sync.diff(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore)
    only = args.path.replace("\\", "/").strip("/") if args.path else ""

    shown = 0
    for st in states:
        if only and st.rel != only:
            continue
        if st.change in (Change.UNCHANGED,):
            continue
        if st.change in (Change.REMOTE_NEW,):
            ui.heading(f"remote-only: {st.rel}")
            continue
        if st.change == Change.LOCAL_NEW:
            ui.heading(f"local-only: {st.rel}")
            continue

        if _too_large_to_diff(ctx.root, st):
            ui.heading(f"{st.rel}: large file differs")
            shown += 1
            continue

        local_text = _read_local_text(ctx.root, st.rel)
        remote_text = _read_remote_text(api, st)
        if local_text is None or remote_text is None:
            ui.heading(f"{st.rel}: binary differs")
            shown += 1
            continue

        ui.heading(f"diff {st.rel}")
        diff_lines = difflib.unified_diff(
            remote_text.splitlines(keepends=True),
            local_text.splitlines(keepends=True),
            fromfile=f"remote/{st.rel}",
            tofile=f"local/{st.rel}",
        )
        for line in diff_lines:
            ui.out(line.rstrip("\n"))
        shown += 1

    if shown == 0:
        ui.info("no textual differences")
    return EXIT_OK


def _too_large_to_diff(root, st) -> bool:
    """A text diff holds both versions in memory, so it stops where sync streams."""
    path = root / st.rel
    local_size = path.stat().st_size if path.is_file() else 0
    remote_size = (st.remote_entry.size or 0) if st.remote_entry else 0
    return max(local_size, remote_size) >= transfer.LARGE_FILE_BYTES


def _read_local_text(root, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    return _decode(data)


def _read_remote_text(api, st) -> str | None:
    if st.remote_entry is None:
        return ""
    try:
        data = api.get_file_bytes(st.remote_entry.path)
    except Exception:
        return None
    return _decode(data)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
