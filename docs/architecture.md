# Architecture

`jp` is a pure standard-library Python package. There are **no third-party
runtime dependencies** — only `urllib`, `ssl`, `json`, `base64`, `hashlib`,
`argparse`, and friends. That keeps installation trivial across macOS, Windows,
and Linux and makes the tool easy to ship as a single `.pyz` or a standalone
binary.

## Module layout

```
src/jp/
├── cli.py          # argparse subcommands + dispatch + top-level error boundary
├── errors.py       # JpError hierarchy with stable exit codes
├── ui.py           # color/quiet/verbose, progress, prompts, secret redaction
├── config.py       # .jp/config + global config; token-path resolution
├── paths.py        # THE path-safety layer (jail + sanitization + atomic write)
├── api.py          # Contents API client over urllib (TLS, retries, redaction)
├── index.py        # .jp/index: the last-synced base state
├── ignore.py       # built-in ignores + .jpignore matching; dotfile policy
├── sync.py         # scan, 3-way diff, push, pull engine
├── transfer.py     # chunked upload, segmented download, retries, worker pool
├── bundle.py       # many small files through one archive
└── commands/       # one module per subcommand
```

## Key design decisions

- **`paths.py` is the single security boundary.** It is the only place allowed
  to translate between local and remote paths, and the only place that composes
  a remote path. Every mutating remote call re-asserts containment immediately
  before it runs. Concentrating this logic in one audited module makes the safety
  guarantees testable.

- **A local index, like git.** `.jp/index` records, per file, the size, hashes,
  and remote modification time at the last successful sync. This is what makes
  three-way conflict detection possible, and it is written only *after* a
  transfer is verified — so an interrupted run leaves a consistent state and the
  next run resumes cleanly.

- **Change detection prefers content hashes.** The Contents API can return a
  file's SHA-256 without sending the body, so `jp` compares hashes and downloads
  only what actually differs.

- **Workspace discovery walks up for `.jp/`**, the same way git looks for
  `.git/`, so commands work from any subdirectory of a workspace.

- **Named credentials keep token values out of synced files (`credentials.py`).**
  `jp login` writes the token *value* to a private `0600` file under a `0700`
  directory and maps a name to it in a `credentials.json` registry — global
  (`~/.config/jp/`) or local (a workspace's `.jp/`, never synced). The config
  records only the credential *name*; `config.load_token` resolves it at call
  time (local registry before global), falling back to the legacy `token_path` /
  `~/.config/jp/token`. This lets one machine hold several servers' tokens while
  keeping secrets off the synced tree.

- **Large files stay out of memory and resume from the other side's real state.**
  The Contents API has no idempotent chunk: chunk 1 replaces the file and every
  other chunk appends. So after any failure `transfer.py` reads the server's size
  and continues from that byte, and only byte 0 may use chunk 1. Because that
  protocol would destroy the destination, chunks go to a `__jp_part__.*` name,
  chosen from the path and content hash so a later run finds it, and the swap into
  place happens after the server's SHA-256 matches. The server's rename refuses an
  existing target, so the previous version steps aside first and is restored if
  the swap fails.

- **Downloads of large files use `/files`, not the Contents API.** It serves raw
  bytes with `Range` support and no base64. Redirects stay unfollowed, because a
  stopped server answers with a login page and a 200. Several connections fetch
  32 MiB segments into a preallocated file, and a log records each finished segment
  so a later run skips it. The final SHA-256 covers the whole file, so a wrong log
  costs a re-download, never a bad file.

- **A big tree moves as one archive.** The limit on a Hub is usually requests per
  second, which no amount of parallelism gets past, so reducing requests is the
  only fix. Twenty or more small files become one archive, built by the side that
  has them: locally on push, by a script run in a server terminal on pull. The
  script on the server is confined by checks of its own, because a terminal is
  outside the path-jail: it refuses absolute paths, `..`, anything but a regular
  file, a folder that resolves outside the prefix, and writing through a link. On
  pull, `jp` extracts only the names it asked for, through the same safe write as
  any other download. Any failure falls back to one request per file.

- **Bulk runs are parallel where the cost is network.** Transfers, per-level remote
  listing and first-sync hashing use a pool of six workers, and only the main
  thread touches the index. The index is saved at most every two seconds, since
  saving per file is quadratic. A crash can lose that much progress, which the next
  run recovers by comparing hashes.

## What is intentionally out of scope (for now)

- Remote kernel execution (`jp run`/`jp exec` over WebSocket) is planned as an
  optional, isolated module so the safe-sync core never depends on it.
- Branching/merging: there is no version model on the server side, so `jp`
  deliberately does not pretend to offer one.
