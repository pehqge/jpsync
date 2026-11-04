<h1 align="center">jpsync</h1>

<p align="center">
  <em>A git-like CLI to safely sync local folders with a remote JupyterHub — zero dependencies, pure Python.</em>
</p>

<p align="center">
  <a href="https://github.com/pehqge/jpsync/actions/workflows/ci.yml"><img src="https://github.com/pehqge/jpsync/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/jpsync/"><img src="https://img.shields.io/pypi/v/jpsync?color=blue&logo=pypi&logoColor=white" alt="PyPI version"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white" alt="Python 3.9+">
  <a href="https://github.com/pehqge/jpsync/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero dependencies">
  <a href="https://doi.org/10.5281/zenodo.20552614"><img src="https://zenodo.org/badge/DOI/10.5281/zenodo.20552614.svg" alt="DOI"></a>
  <a href="https://github.com/sponsors/pehqge"><img src="https://img.shields.io/badge/sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white" alt="Sponsor"></a>
</p>

<p align="center">
  <img src="assets/hero.png" alt="jp --help: the git-like command surface" width="680">
</p>

---

`jp` keeps a local folder in sync with a directory on a **JupyterHub** server —
the way `git` keeps you in sync with a remote. You edit notebooks and scripts on
your laptop, `jp push` to send them up, run your training on the server's GPUs,
and `jp pull` the results back down.

It talks to the JupyterHub REST API directly, has **zero third-party
dependencies** (pure Python standard library), and runs anywhere Python 3.9+
runs — macOS, Windows, Linux.


<p align="center">
  <img src="assets/demo.gif" alt="jp demo: clone a JupyterHub folder, edit locally, push, then pull a teammate's results" width="760">
</p>

## Why jp?

- **Git-like workflow** — `jp clone`, `jp status`, `jp push`, `jp pull`. Same muscle memory.
- **Or skip the copy** — `jp live <url>` mounts the remote folder as a local folder you edit in place, and `jp terminal` drops you into a shell on the server. No SSH, no FUSE, no server-side install.
- **Safe by default** — on a *shared* research machine, jp never deletes remote files unless you explicitly turn that on, and even then it asks you file-by-file. Conflicts are never silently overwritten.
- **Handles big files and big trees** — files of any size move in chunks with a checksum at the end, resume after a dropped connection, and never load into memory. Thousands of small files travel as one archive, in seconds instead of minutes.
- **Zero dependencies** — one install, no dependency hell; ships as a wheel, a single `.pyz`, or a standalone binary.
- **Cross-platform** — macOS, Windows, Linux; Python 3.9 → 3.13.

---

## Installation

**Recommended — [pipx](https://pipx.pypa.io):**
```bash
pipx install jpsync
```

**Or with [uv](https://docs.astral.sh/uv/):**
```bash
uv tool install jpsync
```

Both install the `jp` command in an isolated environment and put it on your
`PATH`. Then check it works:
```bash
jp --version
```

> If `jp: command not found`, run `pipx ensurepath` (or `uv tool update-shell`)
> and reopen your terminal.

<details>
<summary>Other ways to install</summary>

**No Python required** — standalone binary (macOS / Linux):
```bash
curl -fsSL https://raw.githubusercontent.com/pehqge/jpsync/main/scripts/install.sh | sh
```

**No Python required** — standalone binary (Windows, PowerShell):
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/pehqge/jpsync/main/scripts/install.ps1 | iex"
```

**Single file** — grab `jp.pyz` from the
[latest release](https://github.com/pehqge/jpsync/releases/latest) and run it
with any Python 3.9+:
```bash
python jp.pyz --help
```

**From source** (latest `main`):
```bash
pipx install "git+https://github.com/pehqge/jpsync"
```

</details>

---

## Getting started

### 1. Get your JupyterHub API token

`jp` authenticates with a personal API token from your JupyterHub.

1. Open your JupyterHub in a browser and log in (e.g. `https://jupyter.example.com`).
2. Go to the **Token** page — usually the **Token** link in the top bar, or
   visit `https://<your-hub>/hub/token` directly.
3. Type a note (e.g. `jp`), leave the scopes blank (full access to what *you*
   can already do), and click **Request new API token**.
4. **Copy the token now** — JupyterHub shows it only once.

> Security: the token is like a password for your account. `jp` stores only the
> *path* to a token file, never the token value, and never logs or commits it.

### 2. Log in with `jp login`

Run `jp login` and follow the prompts. It first asks for your Jupyter URL (so
the credential is **linked to that server**), suggests a name from it, opens the
token page in your browser, then you paste the token you generate (your input
stays **hidden**). Credentials are saved **globally** by default (usable from
anywhere); pass `--local` to keep it only in the current workspace:

```console
$ jp login
Paste your Jupyter URL (to link this credential to its server), or leave blank: https://jupyter.example.com/user/me/lab/tree/x
Name this server/credential [me-jupyter.example.com]:
Opening the token page to create an API token: https://jupyter.example.com/hub/token
Open it in your browser now? [Y/n]: y

Paste your API token (input hidden):
✓ saved global credential 'me-jupyter.example.com'
```

The URL is optional (leave it blank to skip the link); pass `--url <URL>` to
supply it non-interactively, or `--no-browser` to not open the token page. The
**site** (the server's `scheme://host`) is stored alongside the name so that,
when you have several credentials, `jp` offers only the ones for the server
you're working with.

**Everything stays on your machine.** `jp` writes the token to a private file
(permissions `600`) under `~/.config/jp/` (or the workspace's `.jp/` for a local
credential) and records only the credential's *name* in config — never the token
value. The token is never printed, logged, committed, or sent anywhere except as
the `Authorization` header to your own hub.

Run `jp login` again any time to add another server — keep as many credentials as
you like and pick one when you clone. See [Credentials](#credentials--jp-login).

### 3. Make sure your server is running

`jp` talks to your *single-user* server, so it must be started: open JupyterHub
and, if needed, click **Start My Server**. (`jp doctor` will tell you if it's
stopped.)

### 4. Clone your folder

Copy the URL of the folder from your browser's address bar — the `lab/tree/...`
URL works directly:

```bash
jp clone https://jupyter.example.com/user/<you>/lab/tree/your-folder
cd your-folder
```

That creates a `your-folder/` folder with a `.jp/` workspace inside (like `.git/`)
and downloads the remote tree. `jp` looks at the URL's server and offers only the
credentials saved for it: with a single match it just uses it; with several it
shows a picker (press `a` to see all servers, `s` to link a site to a credential).
The choice is remembered in the workspace (`jp clone … --credential <name>` to
skip the prompt).

### 5. Work like git

```bash
jp status          # what changed, locally vs the server
jp push            # send local changes up
jp pull            # bring remote changes (e.g. training output) down
```

That's it. From any subdirectory of the workspace, `jp` finds its root
automatically (it walks up looking for `.jp/`, stopping at your home folder).

### Bonus: a shell on the server

Need to actually *run* something on the remote box — install a package, kick off
training, poke around? Run `jp terminal` from your workspace and your terminal
becomes the remote machine's shell, right in the mapped folder. No SSH, no setup:

```bash
jp terminal        # you're now in a remote shell, in this folder
```

It only opens an ephemeral terminal session (it never touches your files), and
the session is cleaned up when you exit. See the
[command reference](docs/commands.md#remote-shell) for the details.

### Bonus: edit the remote folder live (no clone)

Don't want a local copy at all — just open the remote folder and edit it in
place? `jp live <url>` mounts it as a normal folder on your machine over the same
kernel transport. Reads and (by default) writes travel straight to the server.

```bash
jp live https://jupyter.example.com/user/<you>/lab/tree/your-folder
# → confirms writable vs read-only, then mounts it under ./your-folder/
#   edit with any tool; Ctrl-C (or `jp live unmount`) to stop.

jp live <url> --code        # open it in VS Code with a remote shell wired up
jp live <url> --read-only   # browse without any risk of writing
```

It mounts under one auto-managed handle (a folder on macOS/Linux, a drive letter
on Windows) using the OS's built-in WebDAV client — no FUSE, no third-party
deps. The mount is loopback-only and gated behind a per-session secret. Full
guide and the cross-OS notes: [docs/jp-live.md](docs/jp-live.md).

### Bonus: run a local script on the server — without pushing it

Editing a script locally and want to run it remotely without `jp push` on every
change? `jp run <file>` ships the *source* through an ephemeral terminal and runs
it on the remote, in the same mapped folder you're standing in — so it behaves
exactly like running it locally (relative `open()`, sibling `import`, `__file__`
all resolve against that folder). The exit code is propagated.

```bash
jp run train.py --epochs 5      # from the workspace root -> runs in <prefix>
jp run analyze.py               # from a subfolder -> runs in <prefix>/<subfolder>
jp run cleanup.sh               # shebang/extension picks the interpreter
jp run --as python3 script      # force the interpreter (e.g. extensionless file)
jp run --dry-run train.py       # preview target folder + source, run nothing
```

The interpreter is chosen from the file's shebang, else its extension (`.py`,
`.sh`, `.js`, `.rb`, `.R`, …), or `--as`. The script source is written to a temp
file (`__temp__.<name>.<random>.<ext>`) *in that folder*, executed, then removed —
only that one file is ever created or deleted, so concurrent `jp run`s never clash
and your files are never overwritten. For Python, `sys.argv[0]`/`__file__`/
tracebacks show the real name, not the temp file. You see only the program's
output, streamed live — no remote shell prompt, and `input()` works just like
locally. Any **data** files the script opens must already exist on the remote
(`jp push` those first). Works on macOS, Linux and Windows (the interactive raw
proxy uses the same `jp.pty` backend as `jp terminal`).

---

## Command reference

| Command | What it does |
|---|---|
| `jp clone <url> [dir]` | Clone a remote Jupyter folder into a new local directory. Accepts a `lab/tree` URL or `--base-url`/`--prefix`. |
| `jp init <url>` | Turn the current folder into a jp workspace (no download). |
| `jp login` | Save a named API-token credential linked to its server (paste the Jupyter URL, name it, paste the token; defaults to global; use `--local` for workspace-only). |
| `jp credentials` | List, edit, or delete saved credentials. No args opens an interactive manager (delete, set site, rename); `--list`, `--rm NAME`, `--rename OLD NEW`, `--set-site NAME URL` for scripting. |
| `jp status` | Show local vs. remote differences. Read-only. |
| `jp push` | Upload local changes. Additive by default. |
| `jp pull` | Download remote changes. Additive by default. |
| `jp diff [path]` | Show file-level differences. |
| `jp ls [remote-path]` | List a remote directory (no local writes). |
| `jp live <url>` | Mount a remote folder as a local folder over the kernel websocket — edit it in your own tools; changes travel to the server. Writable by default (confirmed); `--read-only` opts out. `--code` opens it in VS Code with a remote shell. `jp live unmount` (from inside) stops it; `jp live --defaults` sets the defaults. ([guide](docs/jp-live.md)) |
| `jp open` | Open this workspace's folder (or the subfolder you're in) in the Jupyter web UI. Confirms first, shows the URL, and can copy instead of opening. Refuses folders jp never syncs (`.jp/`, hidden dot-names, `.jpignore` matches) since they aren't on the remote. Press `r` in the prompt to remember your choice (skip it next time); `jp open --ask` forgets it. Token-free URL; no network. |
| `jp config` | Interactive settings editor (see below). Also `config get/set/list`. |
| `jp ignore [pattern]` | Manage `.jpignore` patterns. |
| `jp rm <path>` | Delete on the remote — gated, dry-run + typed confirmation. The only deleter. |
| `jp kernel` | Set up a VS Code remote kernel to run notebooks in the right directory ([guide](docs/vscode-remote-cwd.md)). |
| `jp terminal [url]` | Open the remote machine's shell in your terminal, in the workspace folder. With a `<url>` it works standalone (no workspace needed). Creates/deletes only an ephemeral terminal session; touches no files. |
| `jp run <file> [args…]` | Run a local script on the remote in the current mapped folder, without pushing it. Interpreter from shebang/extension or `--as`; `--dry-run` to preview. Propagates the exit code. macOS/Linux/Windows. |
| `jp doctor` | Diagnose token, connectivity, server status. |
| `jp update` | Update jp to the latest version. |
| `jp changelog` | Show release notes (newer releases, a specific version, or `--all`). |
| `jp version` | Print the version (`--changelog` also shows release notes; `jp --version`). |

Global flags: `-q/--quiet`, `--no-color`. Every command has `--help`.

### `jp config` — interactive settings

Run `jp config` with no arguments in a terminal for a settings screen:

```
  Mirror mode (allow deletes)              false
> Dotfile policy                           skip
  Colored output                           auto
  Network timeout (s)                      30.0

Up/Down move · Space change · i info · / search · Enter save · Esc cancel
```

- **↑/↓** move · **Space** cycle the value · **i** show help for the selected
  setting · **/** search · **Enter** save · **Esc** cancel.

For scripts, the classic forms still work: `jp config list`,
`jp config get <key>`, `jp config set <key> <value>`.

### Mirror mode (deleting files to match the other side)

By default `jp push`/`jp pull` are **additive** — they never delete. If you want
true mirroring (delete on the remote when you delete locally, and vice-versa),
turn on **mirror mode**:

```bash
jp config set mirror true      # persist it, or use --mirror for one run
jp push --mirror               # one-off
```

With mirror on, after the normal sync jp finds files that exist on one side but
not the other and — **always, before deleting anything** — shows you the list
and lets you choose, with the arrow keys, which to **keep** and which to
**delete**:

```
Mirror mode: 2 file(s) exist on remote but not on the other side.
Choose which to DELETE on remote. Default is KEEP.

> [keep]   old_experiment.py
  [keep]   scratch.ipynb

Up/Down move · Space toggle · a delete-all · n keep-all · Enter confirm · Esc cancel
```

Nothing is deleted unless you mark it. In a non-interactive shell, mirror
deletions are refused unless you pass `--yes`. Conflicts (both sides changed) are
*never* deleted or overwritten.

### Credentials & `jp login`

`jp login` is how you give `jp` your JupyterHub API token. It is fully
interactive and **everything happens locally** — the token never leaves your
machine and is never printed:

- It first asks for your **Jupyter URL** and links the credential to that
  server's **site** (`scheme://host`); the URL is optional (`--url` to supply it,
  blank to skip).
- You give the credential a **name** — prefilled from the URL (e.g.
  `me-jupyter.example.com`), editable.
- It opens the server's token page in your browser (`--no-browser` to skip), then
  prompts you to paste the token with the input **hidden** (no echo).
- The **scope** defaults to **global** (stored in `~/.config/jp/`, usable from
  any directory). Pass `--local` to store the credential only in the current
  workspace's `.jp/`.
- The token value goes into a private `600` file; only its *name* is recorded in
  the workspace config (`credential` key).
- A **local** credential lives in the workspace's `.jp/`, and `jp` drops a
  `.jp/.gitignore` (`*`) so that — even if the workspace is also a git repo — git
  ignores the whole `.jp/` directory and the token can never be committed.

Save as many as you like — run `jp login` once per server:

```bash
jp login                                      # interactive: name, paste token; saves globally
jp login --name myserver --global             # scriptable form
jp login --token-stdin --name lab-gpu --local < token.txt
```

When you `jp clone` / `jp init`, `jp` reads the credentials available **globally
and locally** and matches them against the URL's server: with one match it's used
automatically, with several you pick (or pass `--credential <name>`). In the
picker, each credential shows its site; press `a` to toggle between this-server
and all credentials, and `s` to link a site to one that has none. The choice is
saved in the workspace so later `push`/`pull` just work.

Manage saved credentials with **`jp credentials`** — run it with no arguments for
an interactive manager (move with arrows; `d` delete, `s` set/replace site, `r`
rename, `q` quit), or use flags for scripting:

```bash
jp credentials --list                                   # name, scope, site (no tokens)
jp credentials --set-site myserver https://host/user/me # link/replace its site
jp credentials --rename old new                          # rename
jp credentials --rm myserver --force                    # delete (no prompt)
```

Legacy credentials saved before sites existed keep working — they have no site
and match any server until you link one.

At sync time the token is resolved, in order: `$JP_TOKEN` (a value, for CI) →
`$JP_TOKEN_FILE` (a path) → the workspace's saved credential → legacy
`token_path` / `~/.config/jp/token`. `jp` warns if any token file is readable by
other users.

### Keeping jp up to date

```bash
jp update           # detects pipx / uv / pip and upgrades in place
jp update --check   # just check; don't install
```

For a standalone binary install, `jp update` prints the one-line reinstall
command for your OS.

`jp` also checks for a newer release at most once a day, in the background, and
shows a one-line notice next time you run a command — only in an interactive
terminal, never in scripts, pipes, or CI:

```
jp 1.1.1 → 1.2.0  (update available)
run `jp changelog` to see what's new · `jp update` to upgrade
```

- Turn the notice off: `export JP_NO_UPDATE_NOTIFIER=1` or `jp config set update_notifier false`.
- Opt in to automatic updates: `jp config set auto_update true`. When on, `jp`
  updates itself in the background between commands and tells you on the next run.
  It never updates the running command mid-flight, and never in CI or dev installs.
- See what changed: `jp changelog` (releases newer than yours), `jp changelog 1.2.0`
  (a specific version), `jp changelog --all`, or `jp version --changelog`.

---

## Configuration

Each workspace stores its settings in `.jp/config.json` (JSON, never the token
value). Keys: `base_url`, `prefix`, `credential`, `token_path`, `mirror`,
`dotfiles`, `color`, `timeout`. See [docs/commands.md](docs/commands.md) and
[docs/architecture.md](docs/architecture.md).

---

## Security

`jp` is built for a **shared** machine where a mistake can destroy someone
else's research. The guarantees:

- **`push`/`pull` never delete** unless you opt into mirror mode — and even then
  jp asks you, file by file, defaulting to keep.
- **Conflicts are never silently overwritten.** If both sides changed since the
  last sync, jp aborts that file and tells you.
- **Path-jailing.** Every remote operation is confined to your workspace's
  prefix. The server root and shared spaces (`compartilhado`, `lapix`,
  `shared`, …) are refused outright.
- **Untrusted server on download.** File names from the server are sanitized
  before anything is written locally (anti path-traversal / Zip-Slip), and
  writes are atomic and never follow a symlink.
- **Your token never leaks** — stored by path only, sent in the `Authorization`
  header (never a URL), redacted from all output, never committed.

Found a vulnerability? See [SECURITY.md](SECURITY.md) — please don't open a
public issue.

---

## FAQ

**Is `jp` related to git?** No — it borrows git's *workflow*, not its internals.
There's no remote version history on a JupyterHub.

**Does it need Jupyter installed locally?** No. Just Python 3.9+; it talks to the
Hub over HTTPS.

**Why won't my `.gitignore` (or any dotfile) upload?** Most JupyterHub servers
run with `allow_hidden=False`, which rejects creating hidden files (names
starting with `.`). `jp` detects this and *skips* dotfiles on push, reporting
them instead of failing — your `.git/`, `.gitignore`, `.env` etc. simply stay
local (which is usually what you want). A nice side effect: secrets in dotfiles
never get pushed by accident.

**Will it overwrite my work?** Never silently. A conflict aborts that file;
remote deletes are opt-in (mirror mode) and confirmed file-by-file.

**How big can a file be?** `jp` has no size limit: only the disk on each side and
the time it takes. Files of 16 MiB or more go in 8 MiB chunks, so memory use stays
flat whatever the file size. An upload lands under a temporary `__jp_part__.*` name
and replaces the real file only after the server's SHA-256 matches. A download goes
to `.jp/parts/` and replaces the local file only after its SHA-256 matches. See
[large files and bulk runs](docs/commands.md#large-files-and-bulk-runs).

**Can I edit notebooks locally in VS Code but run them on the remote GPUs?** Yes —
that's a core workflow. Sync with jp, then connect VS Code to your remote kernel.
One catch: a remote kernel starts in the server's home, not your notebook's
folder, so relative paths fail. Run `jp kernel` once to fix it. The full
walkthrough (connecting the kernel + the cwd fix) is in
[docs/vscode-remote-cwd.md](docs/vscode-remote-cwd.md).

## Troubleshooting

- **`jp: command not found`** — run `pipx ensurepath` / `uv tool update-shell`, reopen the terminal.
- **`your JupyterHub server appears to be stopped`** — open the Hub UI and click *Start My Server*.
- **`authentication failed` / HTTP 403** — your token expired; create a new one and `jp login` again (or update the token file).
- **A big transfer was interrupted** — run the same `jp push` or `jp pull` again. It continues from the last byte the other side has.
- **A `__jp_part__.*` file shows up on the server** — an upload that never finished. It is invisible to sync; the same file resumes from it. If you changed the file or gave up, remove it with `jp rm`.
- **`FileNotFoundError` / relative paths fail in VS Code with a remote kernel** — the kernel starts in the server's home, not your notebook's folder. Run `jp kernel` (see the [VS Code remote-kernel guide](docs/vscode-remote-cwd.md)).

Run `jp doctor` for a guided check.

---

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). The project is standard-library only;
please keep it dependency-free.

## Support

`jpsync` is free and open source. If it saves you time on your JupyterHub
workflow, you can support its development — thank you! ☕

<p align="center">
  <a href="https://github.com/sponsors/pehqge">
    <img src="https://img.shields.io/badge/sponsor-30363D?style=for-the-badge&logo=GitHub-Sponsors&logoColor=EA4AAA" alt="Sponsor on GitHub">
  </a>
  &nbsp;&nbsp;
  <a href="https://www.buymeacoffee.com/pehqge">
    <img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy me a coffee">
  </a>
</p>

## License

[Apache 2.0](LICENSE) © Pedro Henrique Gimenez
