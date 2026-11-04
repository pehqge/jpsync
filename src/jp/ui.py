"""Terminal output helpers and the central secret-redaction filter.

The single most important function here is :func:`redact`. EVERY line of output
and EVERY error message printed by jp must pass through it, so that a token can
never leak to a terminal, a log, a CI transcript, or a screen-share. We register
known secrets in a module-global set the moment we load them, and we also redact
anything that *looks* like a JupyterHub token even if it was never registered.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable

# Secrets we have actually seen this process (e.g. the loaded token). They are
# matched literally and removed. We never store the token anywhere else.
_KNOWN_SECRETS: set[str] = set()

_REDACTED = "***REDACTED***"

# Heuristic for JupyterHub / Jupyter-server tokens: long hex-ish or
# base64url-ish blobs. Used as a belt-and-suspenders pass so that even an
# unregistered secret that sneaks into a string gets masked.
_TOKEN_PATTERNS = [
    # Authorization: token <secret>  /  Authorization: Bearer <secret>
    re.compile(r"((?:token|bearer)\s+)([A-Za-z0-9._\-]{16,})", re.IGNORECASE),
    # ?token=<secret> in URLs
    re.compile(r"((?:[?&])token=)([A-Za-z0-9._\-]{16,})", re.IGNORECASE),
    # Long opaque hex blobs (>=32) that are almost certainly a token.
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]

# Absolute server filesystem paths leak in some API error bodies (e.g. the
# real server's root maps to /lapix, so errors read "Encoding error saving
# /lapix/privado/x.txt"). They are noise to the user and disclose server layout,
# so we collapse a leaked absolute path down to just its basename. These known
# server roots are masked first; a generic deep-absolute-path pass catches the
# rest. We deliberately do NOT touch ordinary words or relative paths.
_SERVER_PATH_ROOTS = (
    "/lapix",
    "/home/jovyan",
)
# A reasonably long absolute POSIX path (>= 2 segments) embedded in a message.
_ABS_PATH_RE = re.compile(r"(?<![\w/])/(?:[\w.\-]+/){1,}[\w.\-]+")


def _shorten_abs_path(match: re.Match[str]) -> str:
    full = match.group(0)
    base = full.rsplit("/", 1)[-1] or full
    return f".../{base}"


def register_secret(secret: str | None) -> None:
    """Register a literal secret string to be scrubbed from all output."""
    if secret:
        _KNOWN_SECRETS.add(secret)


def redact(text: object) -> str:
    """Return ``text`` with every known/likely secret replaced.

    Defensive: accepts any object, coerces to str. Applied at the output
    boundary AND eagerly inside this module's print helpers.
    """
    out = str(text)
    # 1) literal known secrets first (most precise)
    for secret in _KNOWN_SECRETS:
        if secret and secret in out:
            out = out.replace(secret, _REDACTED)
    # 2) structural patterns (token/bearer/?token=) keep the prefix
    out = _TOKEN_PATTERNS[0].sub(lambda m: m.group(1) + _REDACTED, out)
    out = _TOKEN_PATTERNS[1].sub(lambda m: m.group(1) + _REDACTED, out)
    # 3) bare long hex blobs
    out = _TOKEN_PATTERNS[2].sub(_REDACTED, out)
    # 4) absolute server filesystem paths leaked in error bodies (/lapix/...).
    #    Collapse to just the basename so we neither disclose server layout nor
    #    confuse the user with paths that do not exist on their machine.
    if any(root in out for root in _SERVER_PATH_ROOTS):
        out = _ABS_PATH_RE.sub(_shorten_abs_path, out)
    return out


# --- color handling ---------------------------------------------------------


# Process-global color mode set from the workspace config ("auto"|"always"|
# "never"). Defaults to "auto" until a command loads its config. Environment
# (NO_COLOR / JP_NO_COLOR / --no-color) always overrides it.
_color_mode = "auto"


def set_color_mode(mode: str) -> None:
    """Set the color policy: 'auto' (tty only), 'always', or 'never'."""
    global _color_mode
    _color_mode = mode if mode in ("auto", "always", "never") else "auto"


def _color_enabled(stream: object) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("JP_NO_COLOR") is not None:
        return False
    if _color_mode == "never":
        return False
    if _color_mode == "always":
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


class _Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def _wrap(text: str, code: str, stream: object) -> str:
    if _color_enabled(stream):
        return f"{code}{text}{_Style.RESET}"
    return text


# --- print helpers (all redact before emitting) -----------------------------

# Quiet mode is a process-global toggle set by the CLI parent parser.
_quiet = False


def set_quiet(value: bool) -> None:
    global _quiet
    _quiet = bool(value)


def is_quiet() -> bool:
    return _quiet


def out(message: str = "") -> None:
    """Print a normal line to stdout (redacted)."""
    if _quiet:
        return
    print(redact(message))


def info(message: str) -> None:
    if _quiet:
        return
    print(redact(message))


def success(message: str) -> None:
    if _quiet:
        return
    print(_wrap("✓ ", _Style.GREEN, sys.stdout) + redact(message))


def warn(message: str) -> None:
    # Warnings go to stderr and are shown even in quiet mode.
    print(_wrap("warning: ", _Style.YELLOW, sys.stderr) + redact(message), file=sys.stderr)


def error(message: str) -> None:
    print(_wrap("error: ", _Style.RED, sys.stderr) + redact(message), file=sys.stderr)


def detail(message: str) -> None:
    if _quiet:
        return
    print(_wrap(redact(message), _Style.DIM, sys.stdout))


def heading(message: str) -> None:
    if _quiet:
        return
    print(_wrap(redact(message), _Style.BOLD, sys.stdout))


def bullets(lines: Iterable[str], indent: str = "  ") -> None:
    if _quiet:
        return
    for line in lines:
        print(indent + redact(line))
