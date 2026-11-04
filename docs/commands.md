# Command reference

All commands accept the global flags `-q/--quiet` and `--no-color`. Run
`jp <command> --help` for the full option list of any command.

## Workspace

### `jp clone <url> [dir]`
Create a new local workspace from a remote Jupyter folder and download its tree.
The URL is the one from your browser's address bar, e.g.
`https://host/user/<name>/lab/tree/<folder>`; `tree`, `doc/tree`, `notebooks`,
named servers and standalone servers are also understood. Alternatively pass
`--base-url <api-base>` and `--prefix <remote-path>` explicitly. `--credential
<name>` picks a saved credential (see `jp login`); if omitted, jp uses the only
one or prompts you to choose. `--token-path` records a token file directly.
`--dry-run` previews without writing.

The clone **refuses** a prefix that resolves to the server root or a shared
directory (e.g. `compartilhado`, `lapix`), so a workspace can never point at
everyone's data.

### `jp init <url>`
Turn the current directory into a jp workspace without downloading anything
(useful for a first push of an existing local folder). Same URL / `--base-url` +
`--prefix` forms as `clone`.

### `jp login`
Save a **named credential** for a server, interactively. It explains how to get a
JupyterHub API token, prompts you to paste it (hidden, via `getpass`), asks for a
name (e.g. `myserver`), and asks whether to save it **global** (everywhere) or
**local** (this workspace only). The token value is written to a private `0600`
file and registered for redaction; it is never echoed and never written into
`.jp/config.json`. Run it repeatedly to store several servers.

Scriptable flags: `--name <name>`, `--global`/`--local` (local requires a
workspace), `--token-stdin` (read the value from stdin), `--token-path <file>`
(register an existing token file), `--force` (overwrite an existing name).

Token resolution at sync time: `$JP_TOKEN` → `$JP_TOKEN_FILE` → the recorded
credential (local registry before global) → legacy `token_path` → legacy
`~/.config/jp/token`.

## Synchronization

### `jp status`
Show, read-only, how the working tree differs from the last synced state and the
remote: added, modified, conflicting, and skipped (hidden) files. Never writes.

### `jp pull [path...]`
Download remote changes into the working tree. Additive by default — **never
deletes** a local file. A true three-way conflict (both sides changed) aborts
that file instead of overwriting your local copy. `--dry-run` previews.

### `jp push [path...]`
Upload local changes to the remote. Additive by default — **never deletes** a
remote file. Hidden files (dotfiles) are skipped and reported (the server
rejects them) without aborting the run. Conflicts abort that file rather than
overwriting a newer remote version. `--dry-run` previews.

### Large files and bulk runs
`push` and `pull` work the same at any scale; there is nothing to configure.

- **Six transfers run at once**, so a tree of thousands of small files is limited
  by the server rather than by one-at-a-time requests. A transient network or
  server error is retried with backoff. One failed file never stops the others,
  and the summary names each failure with its reason.
- **Files of 16 MiB or more never load into memory.** Uploads go in 8 MiB chunks
  to a temporary `__jp_part__.*` file next to the destination; downloads stream
  from the raw `/files` endpoint to `.jp/parts/`.
- **The real file only changes after a checksum match.** An upload is verified
  against the SHA-256 the server computes, then renamed over the destination, so
  a failed run leaves the previous version in place. If the swap itself fails, the
  previous version is put back. Overwriting moves the previous version to the
  server's trash when the server has one, so it can use quota until the trash is
  emptied.
- **An interrupted run resumes.** Run the same command again: `jp` asks the server
  how many bytes it has (or checks `.jp/parts/`) and continues from there. Changing
  the file in the meantime starts a new upload; the old `__jp_part__.*` file stays
  until you remove it with `jp rm`.

Sync never shows `__jp_part__.*` files, so a half-finished upload is never pulled.

### Mirror mode (`--mirror` / config `mirror`)
By default, sync is additive. With mirror mode on, `push`/`pull` additionally
*offer to delete* files that exist on one side but not the other:

- `push --mirror` — remote files with no local counterpart become deletion candidates.
- `pull --mirror` — local files with no remote counterpart become deletion candidates.

Deletion is **never silent**. In a terminal jp shows a keep/delete selector
(arrow keys; every file defaults to keep) and deletes only what you mark. With
`--yes` it deletes all candidates (for scripts). In a non-interactive shell
without `--yes`, it deletes nothing and lists what it would have offered.
Persist the setting with `jp config set mirror true`, or use `--mirror` /
`--no-mirror` for a single run.

### `jp diff [path...]`
Show file-level differences between the local copy and the last synced state
(and the remote with `--remote`).

## Notebooks

### `jp kernel`
Set up a VS Code **remote** Jupyter kernel so notebooks run in the right
directory. When VS Code runs a local `.ipynb` against a remote kernel, the
kernel's working directory is the server's home, not the notebook's folder, so
relative paths (`pd.read_excel("dataset/x.xlsx")`) raise `FileNotFoundError`;
VS Code's `notebookFileRoot` does not apply to remote kernels.

`jp kernel` does **not** touch the remote. It prints a one-time IPython startup
snippet — pre-filled with this workspace's local root and `prefix` — and copies
it to your clipboard. You paste it into one cell, run it once, and restart the
kernel; every notebook in the workspace then starts in the correct directory
automatically. By default the snippet is only copied (kept out of the output);
`--script` prints it, and `--no-clipboard` skips copying.

`--link` instead shows and copies the **server connection URL** (with your
token) to paste into VS Code's kernel picker. This is the one command that
prints your token, so it is opt-in and asks for confirmation first (`-y/--yes`
skips the prompt); treat the URL like a password.

Must be run inside a workspace. See
[vscode-remote-cwd.md](vscode-remote-cwd.md) for the full walkthrough, including
how to connect VS Code to the remote kernel.

## Remote shell

### `jp terminal`
Turn your local terminal into the **remote machine's shell**, in one command and
with no setup. It creates an ephemeral Jupyter terminal on the server (the same
thing the web UI's "New → Terminal" opens), connects to it, and proxies your
terminal to it — so you get a real, interactive remote shell, starting in this
workspace's mapped folder.

It is intentionally narrow and safe on a shared server: the **only** remote calls
it makes are creating and deleting an ephemeral terminal session
(`POST`/`DELETE /api/terminals`). It never reads, writes, moves, or deletes a
file, and the session it creates is always deleted on exit (only that session,
never anyone else's). The token is sent only in the `Authorization` header on the
websocket handshake, never in a URL.

- `--no-cd` starts in the server's default directory instead of the workspace
  folder.
- `-y/--yes` skips the one-time confirmation prompt.
- Press **Ctrl-]** to force-disconnect (telnet convention) if the remote shell
  ever wedges; `exit`/Ctrl-D end it normally.
- The websocket client is hand-rolled on the standard library, so `jp` keeps its
  zero-dependency promise.
- Requires a POSIX terminal (`termios`). On Windows there is no raw PTY, so the
  command falls back to opening the Jupyter web UI (use New → Terminal there);
  that fallback shows a token-bearing URL and asks for confirmation first.
- Needs server-side terminals to be enabled (most Jupyter Server / Lab
  deployments enable them by default); if they are disabled, `jp terminal` says
  so and exits without creating anything.

Must be run inside a workspace.

### `jp run <file> [args…]`
Run a **local** script on the remote machine in the folder you're currently in —
without `jp push`-ing it first. It's the fast loop for "edit locally, run
remotely": ideal when you're iterating on a script in your editor against a
remote kernel/box.

How it works, and why it looks exactly like a local run:
- Your file's *source* is uploaded to a temp file named
  `__temp__.<name>.<random>.<ext>` **in the mapped folder for your current
  directory** (workspace root → `<prefix>`, a subfolder → `<prefix>/<subfolder>`).
  Being in that folder gives the script the same working directory, `sys.path`,
  and relative-`open()` behavior as a local run. It is removed as soon as the
  program exits.
- For a **Python** file, a tiny bootstrap runs the source under the real name, so
  `sys.argv[0]`, `__file__`, and tracebacks show `train.py` — not the temp file —
  exactly like a local run. (Shebang scripts and non-Python interpreters show the
  temp name; a minor cosmetic difference.)
- The program runs inside a one-shot ephemeral terminal (like `jp terminal`)
  whose command brackets the output with two unique marker control-sequences
  (the FinalTerm / iTerm2 shell-integration technique). The client swallows the
  shell prompt and the echoed command (everything before the start marker) and
  stops at the end marker, which carries the exit code; the session then `exit`s,
  so no prompt is shown before or after. You see only the program's output —
  streamed live — never the remote shell.
- The remote PTY echoes typed input, so `input()` behaves exactly like local
  (what you type appears as you type it); Ctrl-C is forwarded to the program.
- The exit code is propagated to `jp run` (so `jp run x.py && …` works).

Safety / correctness:
- **Never overwrites or deletes your data.** The temp name carries 128 bits of
  randomness and is pre-checked for existence on the remote before upload;
  cleanup deletes exactly that one file. No directory is ever created or removed,
  so concurrent `jp run`s in different terminals can't clash. (The `__temp__.`
  name is *not* hidden — the Contents API rejects dotfiles by default — but it is
  auto-ignored by `jp` and removed right after the run.)
- The interpreter is chosen from the file's **shebang**, else its **extension**
  (`.py`→`python3`, `.sh`→`bash`, `.js`→`node`, `.rb`→`ruby`, `.R`→`Rscript`, …),
  else `--as <interp>`.
- Any **data files** the script opens must already exist on the remote — `jp run`
  only ships the script itself; `jp push` the data first.

Flags:
- `--as <interp>` forces the interpreter (e.g. `--as python3` for an
  extensionless file).
- `--dry-run` prints the target folder, interpreter, and source without running
  anything (and without creating a terminal).

Requires a POSIX terminal (`termios`); macOS and Linux today. Windows support
arrives together with the `jp terminal` Windows fix. Must be run inside a
workspace (there is no URL mode).

## Inspection & configuration

### `jp ls [remote-path]`
List a remote directory through the Contents API without touching local disk.

### `jp config`
With no arguments, opens an interactive settings editor (arrow keys to move,
Space to change a value, `i` for help on the selected setting, `/` to search,
Enter to save, Esc to cancel). In a non-interactive shell it prints the current
settings instead. Scriptable forms:

```
jp config list
jp config get <key>
jp config set <key> <value>
```

Editable settings: `mirror`, `dotfiles`, `color`, `timeout`. Connection fields
(`base_url`, `prefix`, `token_path`) are shown for context and settable via
`jp config set`.

### `jp ignore [pattern...]`
Add or list `.jpignore` patterns (gitignore-style). Always-on ignores include
`.jp/`, `.git/`, `__pycache__/`, `.ipynb_checkpoints/`, `.DS_Store`, `*.pyc`.

Patterns apply to both sides: an ignored remote file is not pulled, shown by
`status`, or offered for deletion in mirror mode, and an ignored remote folder is
not even listed. This is the way to keep a folder of models on the server without
`pull` trying to bring them down.

### `jp doctor`
Diagnose your setup: token present and valid, server reachable, server running
(vs. stopped), and clock sanity. Prints actionable hints.

### `jp update`
Update jp to the latest version. Detects whether jp was installed via pipx, uv,
or pip and upgrades in place; for a standalone binary it prints the reinstall
command. `jp update --check` only reports whether a newer release exists.

### `jp version`
Print the jp version. Also available as `jp --version`.

## Deletion (gated)

### `jp rm <path>`
The **only** command that deletes on the remote outside of mirror mode, and it
is intentionally hard to misuse:

- the path is validated against the workspace prefix immediately before the call;
- it prints a dry-run of exactly what will be removed (`--dry-run` stops there);
- it requires a typed confirmation (or `--yes` in scripts; a non-interactive
  shell without `--yes` refuses);
- `--recursive` is required to remove a non-empty directory, and removal then
  proceeds bottom-up (the server does not delete recursively);
- `--local` also removes the local copy of a single file.

> On many JupyterHub deployments a remote delete moves the file to a trash
> folder rather than erasing it, and can leave the parent listing in a bad
> state — one more reason `push`/`pull` never delete by default.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | generic error |
| 2 | usage error (bad arguments) |
| 3 | not a jp workspace / config problem |
| 4 | authentication error |
| 5 | network error / server down |
| 6 | safety refusal (path-jail, bad prefix, conflict abort) |
| 7 | partial failure (some files failed) |
| 130 | interrupted (Ctrl-C) |
