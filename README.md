> **This is a fork of [pre-commit/pre-commit](https://github.com/pre-commit/pre-commit)**
> adding a background daemon and per-hook result caching so hooks run
> incrementally in the background and gate commits with zero extra latency.

## pre-commit-daemon

A framework for managing and maintaining multi-language pre-commit hooks,
extended with a background daemon and hook result cache.

For the upstream documentation see: https://pre-commit.com/

---

## New features

> [!NOTE]
> We recommend to not modify files in hooks except in a `fix` field when you use the daemon.

### `fix` field

Each hook has an optional `fix` field. When set, the daemon runs the `fix`
command in the background (to auto-fix files) while `entry` is used at commit
time (to verify the fix was applied cleanly).

This lets you separate the "repair" step (run by the daemon) from the
"verify" step (run at commit time), keeping commits fast.

### Background daemon

```
pre-commit daemon start      # start in background
pre-commit daemon stop       # stop
pre-commit daemon status     # show running hooks and cache state
pre-commit daemon clear      # clear cached results
```

The daemon watches for file changes, runs matching hooks proactively, and
stores results in a local cache. At commit time the cached result is returned
immediately — no hook needs to run again if the file hasn't changed.

### `cache` flag

Each hook has a new boolean `cache` field (default `true`).

- `cache: true` (default) — the daemon runs this hook proactively in the background and
  the result is served from cache at commit time.
- `cache: false` — the hook is excluded from background pre-computation and
  runs normally at commit time (use this for fast, trivial hooks).

---

## `.pre-commit-daemon-config.yaml`

You can override hook settings for the daemon without touching
`.pre-commit-config.yaml` by creating a `.pre-commit-daemon-config.yaml` in
your repo root.

Supported override fields: `entry`, `fix`, `args`, `cache`, `name`, `alias`,
`files`, `exclude`, `types`, `types_or`, `exclude_types`, `always_run`,
`fail_fast`, `pass_filenames`, `description`, `log_file`, `require_serial`,
`verbose`.

Fields that cannot be overridden (they affect environment installation or hook
staging): `language`, `language_version`, `additional_dependencies`, `stages`,
`minimum_pre_commit_version`.

If the file is absent, behaviour is completely unchanged.

### Example

The daemon config below tells the daemon to run the read-only variants in the
background and use the mutating variants as fixers:

**`.pre-commit-config.yaml`**

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff
        name: ruff (lint and fix)
        language: system
        files: \.py$
        types: [file]
        entry: ruff check --fix

      - id: gofmt
        name: gofmt (format)
        language: system
        files: \.go$
        types: [file]
        entry: gofmt -w
```

**`.pre-commit-daemon-config.yaml`**

```yaml
- repo: local
  hooks:
    - id: ruff
      entry: ruff check          # verify-only in daemon
      fix: ruff check --fix      # auto-fix run by daemon before verifying

    - id: gofmt
      entry: gofmt               # verify-only (exit code) in daemon
      fix: gofmt -w              # auto-fix run by daemon before verifying
```

The daemon will:
1. Run `ruff check --fix` (and `gofmt -w`) on changed files to auto-fix them.
2. Run `ruff check` (and `gofmt`) to verify the fix was applied cleanly.
3. Cache the result so the commit hook completes instantly.
