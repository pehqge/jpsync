"""jp -- a git-like CLI that safely syncs local folders with a remote JupyterHub.

Stdlib-only, zero external dependencies. The package version is hard-coded as a
reliable fallback and overridden by installed package metadata when available.
"""

from __future__ import annotations

import contextlib

__all__ = ["__version__"]

__version__ = "1.4.0"  # x-release-please-version

try:  # Prefer the installed distribution's version when present.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _version

    with contextlib.suppress(PackageNotFoundError):
        __version__ = _version("jpsync")
    del _version, PackageNotFoundError
except Exception:  # pragma: no cover - importlib.metadata always present on 3.8+
    pass
