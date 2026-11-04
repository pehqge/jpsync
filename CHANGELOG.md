# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0](https://github.com/pehqge/jpsync/compare/v1.3.0...v1.4.0) (2026-11-04)


### Features

* **sync:** archive big trees and download over six connections ([#37](https://github.com/pehqge/jpsync/issues/37)) ([c533ef5](https://github.com/pehqge/jpsync/commit/c533ef50d636d3845fe3a58569a7beb64c5c6fd4))
* **sync:** transfer files of any size and large trees safely ([#35](https://github.com/pehqge/jpsync/issues/35)) ([c2bdb67](https://github.com/pehqge/jpsync/commit/c2bdb677886ef741154584d5a6149f9695aeb992))

## [1.3.0](https://github.com/pehqge/jpsync/compare/v1.2.0...v1.3.0) (2026-06-05)


### Features

* **live:** jp live — mount your remote Jupyter folder locally, edit + run over the kernel ([#26](https://github.com/pehqge/jpsync/issues/26)) ([5c14b23](https://github.com/pehqge/jpsync/commit/5c14b23810bf646a933c72dc24a757424ed961dc))


### Bug Fixes

* AI release-notes gh bug + config works outside a workspace ([#27](https://github.com/pehqge/jpsync/issues/27)) ([9c9c963](https://github.com/pehqge/jpsync/commit/9c9c9637aeea8d2ed08a105b6d7301960af1d09c))

## [1.2.0](https://github.com/pehqge/jpsync/compare/v1.1.1...v1.2.0) (2026-06-05)


### Features

* **changelog:** jp changelog command, version --changelog, post-update notes ([616689f](https://github.com/pehqge/jpsync/commit/616689fbec0a6733df6d8f39cb1c1795a8d12071))
* **ci:** Gemini-generated release highlights and update/notifier docs ([e2e13a2](https://github.com/pehqge/jpsync/commit/e2e13a236f1005e8f6ea56a59129f8c11f8c406a))
* **config:** manage global notifier/auto-update prefs without a workspace ([4f5b561](https://github.com/pehqge/jpsync/commit/4f5b5615932e8b376ba38080fdb5a9cdc98cc578))
* **login:** link saved credentials to their origin site ([#22](https://github.com/pehqge/jpsync/issues/22)) ([ae7322e](https://github.com/pehqge/jpsync/commit/ae7322e57ce7eaeefdae7d431200d13e6c16f4a3))
* **open:** jp open — open a workspace folder in the Jupyter web UI ([#23](https://github.com/pehqge/jpsync/issues/23)) ([ecace3b](https://github.com/pehqge/jpsync/commit/ecace3b08001badba4ac0bd559ff32c76d7c7618))
* **prefs:** user-global preferences store ([84b993b](https://github.com/pehqge/jpsync/commit/84b993b3f13b034bc07e061e45f3c9e8e18ad794))
* **run:** jp run — run a local script on the remote like a local run ([#24](https://github.com/pehqge/jpsync/issues/24)) ([0c89efb](https://github.com/pehqge/jpsync/commit/0c89efb56c11fddc5311460efa8cc362f0f1c7a5))
* update notifier, opt-in auto-update, jp changelog, AI release notes ([3da0d78](https://github.com/pehqge/jpsync/commit/3da0d7805b352c70bc97c0bfa3c78c6405f5f440))
* **update-notify:** passive update notifier with detached background worker ([3ceb1c4](https://github.com/pehqge/jpsync/commit/3ceb1c4a3bd33c279f592739793f038238df9a4a))


### Bug Fixes

* **ci:** drop --strict so pip-audit's editable skip is not fatal ([995120c](https://github.com/pehqge/jpsync/commit/995120ce4d32c70224565ebda342f13c7cce332c))
* **ci:** send Gemini key via x-goog-api-key header, not URL query string ([9e12877](https://github.com/pehqge/jpsync/commit/9e128770d54e68385ebbcae7832823d9892177d5))
* **ci:** skip the editable project in pip-audit ([5d127e3](https://github.com/pehqge/jpsync/commit/5d127e34f8e5638ff0c6d4083f2c29f780d97986))
* **ci:** upgrade pip before pip-audit so a CVE in the runner's pip doesn't fail the build ([1b4bd00](https://github.com/pehqge/jpsync/commit/1b4bd001597b922fe9e2921a6d6e126f355a99e8))
* **sync:** force type=file on hash/stat so jupytext-paired files sync ([#21](https://github.com/pehqge/jpsync/issues/21)) ([fba9d44](https://github.com/pehqge/jpsync/commit/fba9d44ccc6b72226acb51fd1da20b5efb9272f7))


### Documentation

* add Zenodo DOI badge and citation DOI ([fca80ad](https://github.com/pehqge/jpsync/commit/fca80ad60ec1a1ec215f14f08849d956be94f000))
* fix Zenodo DOI badge (use direct SVG, no redirect) ([1917f3f](https://github.com/pehqge/jpsync/commit/1917f3f96e64d47f56c7b2d9d25d105154bb33a6))
* point DOI badge image at version DOI record ([44cddb9](https://github.com/pehqge/jpsync/commit/44cddb93fb7ead1cf7fb9cf272620dc84f0a0972))
* **update-notifier:** design spec for notifier, auto-update, changelog, AI release notes ([0106011](https://github.com/pehqge/jpsync/commit/010601132513de1926e54d616a216b6172636328))
* **update-notifier:** implementation plan and spec refinements ([5db3656](https://github.com/pehqge/jpsync/commit/5db3656a544150476f8ca4ae8d3c78df3d187259))
* use concept DOI for badge image so it tracks the latest release ([776490a](https://github.com/pehqge/jpsync/commit/776490aa726671d0aaba6114f6af3166b30c7d28))

## [1.1.1](https://github.com/pehqge/jpsync/compare/v1.1.0...v1.1.1) (2026-06-05)


### Documentation

* **terminal:** show jp terminal in the hero image and README ([c517f2e](https://github.com/pehqge/jpsync/commit/c517f2e6a2677b0d14c34c60f836338605e431bc))

## [1.1.0](https://github.com/pehqge/jpsync/compare/v1.0.1...v1.1.0) (2026-05-31)


### Features

* **terminal:** `jp terminal` — remote shell over Jupyter terminals API ([4dd8e3c](https://github.com/pehqge/jpsync/commit/4dd8e3c070135520c5b060e040270238856a5a95))
* **terminal:** add `jp terminal` remote shell over Jupyter terminals API ([ddffcc7](https://github.com/pehqge/jpsync/commit/ddffcc76661f5762c9a5544cd2d729a3c71fd8c4))


### Documentation

* move demo gif up below the clone example ([45b11ee](https://github.com/pehqge/jpsync/commit/45b11ee92967067923b40bba55a6a2673aa0eff0))
* remove example commands from README ([b2b9e2b](https://github.com/pehqge/jpsync/commit/b2b9e2b327f4bdecbd11e8c7c8bb267a87100e07))

## [1.0.1](https://github.com/pehqge/jpsync/compare/v1.0.0...v1.0.1) (2026-05-31)


### Documentation

* add a small sponsor badge to the header row ([b4358fd](https://github.com/pehqge/jpsync/commit/b4358fde5fef1542824984e4db6a18682b69b487))
* add animated demo gif to the workflow section ([0c642c4](https://github.com/pehqge/jpsync/commit/0c642c4a31c62d79728b65a6419a5485782ba26e))
* add Support section + FUNDING.yml (Sponsors + Buy Me a Coffee) ([dc76f11](https://github.com/pehqge/jpsync/commit/dc76f117d2297e3bbbc30d835d36ff69e3d10791))
* add Support section with sponsor links ([7008195](https://github.com/pehqge/jpsync/commit/7008195c093a438639044ab76457529038c03a90))
* add terminal hero + social preview, switch to live PyPI badges ([fc2d788](https://github.com/pehqge/jpsync/commit/fc2d788be6e27efa30fab8b4f1ada421364f78ff))
* clean up badge row (compact python, drop empty downloads) ([f03173b](https://github.com/pehqge/jpsync/commit/f03173b5243e14c0d3fae4f6e1c69aa9e6edf329))
* simplify install section to two recommended methods ([9f7f90c](https://github.com/pehqge/jpsync/commit/9f7f90c9d04cc70f1bb4f78be98b35a80620c918))
* simplify install section to two recommended methods ([035348a](https://github.com/pehqge/jpsync/commit/035348a667d7bfcf749c3c038befdddeaa8db39f))
* swap support badges for for-the-badge style ([32e1835](https://github.com/pehqge/jpsync/commit/32e183512d5939fb64e82611bfc46dd1b197bc88))

## [1.0.0](https://github.com/pehqge/jpsync/compare/v0.3.1...v1.0.0) (2026-05-31)


### Miscellaneous Chores

* rebrand PyPI package and repo to jpsync ([a26db5c](https://github.com/pehqge/jpsync/commit/a26db5c4981f462e5695d7432e003426dac694eb))

## [0.3.1](https://github.com/pehqge/jpsync/compare/v0.3.0...v0.3.1) (2026-05-31)


### Bug Fixes

* **readme:** use shields static/v1 badge so release-please can't eat the color ([7c9377b](https://github.com/pehqge/jpsync/commit/7c9377b860b5f75942a93c662b7bdab5b7afa83f))
* **update:** detect editable installs instead of faking a successful upgrade ([d12722b](https://github.com/pehqge/jpsync/commit/d12722b47d9ba6192198c1062e01502f4d87be22))

## [0.3.0](https://github.com/pehqge/jpsync/compare/v0.2.0...v0.3.0) (2026-05-31)


### Features

* **kernel:** add --link, fix docs, print privacy verdict ([13d42ca](https://github.com/pehqge/jpsync/commit/13d42cab82df2463d444c5387da59609e8a84e12))
* **kernel:** add `jp kernel` to fix VS Code remote-kernel cwd ([7ba887c](https://github.com/pehqge/jpsync/commit/7ba887c782d7cdf341e262fbd398609d8e7d0864))


### Bug Fixes

* **credentials:** skip world-readable warning on Windows ([44fa06a](https://github.com/pehqge/jpsync/commit/44fa06a4af1b9f3983bfaad12e1a9380257f36c9))
* **readme:** use a static release badge auto-bumped by release-please ([1176d07](https://github.com/pehqge/jpsync/commit/1176d07d4885562db9dd2f4fb9ccfab9128a97ee))
* **tests:** isolate Windows home dir and simplify release badge ([9eeb6ac](https://github.com/pehqge/jpsync/commit/9eeb6acbd7931ee30384b6c74dd9c3214805e425))
* **tests:** keep USERPROFILE comment within ruff line-length ([86f3d68](https://github.com/pehqge/jpsync/commit/86f3d68fea3614e930a1d06278ba11017f8d02bb))


### Documentation

* fix jp login flow to match actual behavior ([c378c4e](https://github.com/pehqge/jpsync/commit/c378c4e7538632155a17fd1d70a28901fe7fac40))
* fix two more stale README details ([1fefd67](https://github.com/pehqge/jpsync/commit/1fefd6719a35457f374ffc33d7ec3ad244910781))
* **readme:** add FAQ entry for the VS Code remote-kernel workflow ([d4e0436](https://github.com/pehqge/jpsync/commit/d4e043644df21357d39156964954a164c68d3cfa))

## [0.2.0](https://github.com/pehqge/jpsync/compare/v0.1.0...v0.2.0) (2026-05-31)


### Features

* **config:** activate color setting and add dotfile "protect" policy ([38c9ac7](https://github.com/pehqge/jpsync/commit/38c9ac7ebe963156e0eb92015ec98c147d617a00))
* named credentials with interactive jp login ([ec290db](https://github.com/pehqge/jpsync/commit/ec290db9233c427a7150ecebb3cfad836d4b0e68))


### Bug Fixes

* add top-level release-type to release-please config ([695d456](https://github.com/pehqge/jpsync/commit/695d456d42511a371a33bad1d3e327af61c45b88))
* tag releases as vX.Y.Z without component prefix ([c2e9794](https://github.com/pehqge/jpsync/commit/c2e9794065feb93f13afa24a947f5db26bae820c))
* **tui:** repair interactive settings render and add navigation hints ([0e6b673](https://github.com/pehqge/jpsync/commit/0e6b673ab1bab1ad4e396679b1874b1284b93cf7))


### Documentation

* replace real JupyterHub URL with generic example placeholder ([54612b7](https://github.com/pehqge/jpsync/commit/54612b7e269d8e8f2c1e4757c6427aeaec625314))

## [Unreleased]

### Added

- Named credentials: `jp login` is now interactive — it explains how to get a
  JupyterHub API token, reads it with hidden input, asks for a name, and saves it
  **globally** (`~/.config/jp/`) or **locally** (a workspace's `.jp/`). Token
  values are written to private `600` files and never leave the machine; only the
  credential name is recorded in config. Scriptable via `--name`,
  `--global`/`--local`, `--token-stdin`, `--token-path`, `--force`.
- `jp clone` / `jp init` select a saved credential automatically when only one
  exists, or prompt to choose when several do (`--credential <name>` to skip the
  prompt); the choice is recorded in the workspace config.
- New `credential` config key and a `credentials.json` registry per scope.
- Every `.jp/` workspace now gets a `.jp/.gitignore` (`*`) so a workspace that is
  also a git repo can never commit its local state or a local token file.

### Changed

- Token resolution order is now `$JP_TOKEN` → `$JP_TOKEN_FILE` → the workspace's
  saved credential → legacy `token_path` / `~/.config/jp/token` (the legacy paths
  remain supported).

## [0.1.0] - 2026-05-30

### Added

- Initial release of `jp` (PyPI package `jpsync`, command `jp`).
- Git-like commands: `clone`, `pull`, `push`, `status`, `diff`, `init`,
  `config`, `auth`, `version`.
- Three-way sync model (local / remote / base) with conflict detection.
- Conflict-safe synchronization: conflicting files are never overwritten
  silently (exit code `6`).
- Token-based authentication resolved as `--token-file` > `$JP_TOKEN_FILE` /
  `$JUPYTER_TOKEN` > repo config > global config > `~/.jupyter_token`;
  only the token path is stored in config, and the token file is expected to be
  mode `0600`.
- Path-jail confinement: remote paths are resolved inside the clone root, with
  `..` traversal, absolute paths, and boundary-crossing symlinks rejected.
- `.jpignore` support plus built-in defaults (`.jp/`, dotfiles, `.git/`,
  `__pycache__/`, `*.pyc`); hidden files opt-in via `allow_hidden`.
- Pure standard-library HTTP client (`urllib`) for the Jupyter Contents API,
  with retry/backoff on network errors.
- Distribution via PyPI, a single-file `jp.pyz` zipapp, standalone per-OS
  PyInstaller binaries, and `install.sh` / `install.ps1` installers.

[Unreleased]: https://github.com/pehqge/jpsync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pehqge/jpsync/releases/tag/v0.1.0
