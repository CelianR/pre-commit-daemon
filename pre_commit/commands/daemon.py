from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import signal
import time
from typing import Any

from pre_commit import git
from pre_commit import output
from pre_commit.clientlib import load_config
from pre_commit.commands.run import _compute_hook_key
from pre_commit.commands.run import _file_hash
from pre_commit.commands.run import Classifier
from pre_commit.repository import all_hooks
from pre_commit.repository import install_hook_envs
from pre_commit.store import Store


def _repo_slug(toplevel: str) -> str:
    """12-char hex digest of repo root path, used in per-repo filenames."""
    return hashlib.sha256(toplevel.encode()).hexdigest()[:12]


def _pid_file(store: Store, toplevel: str = '') -> str:
    if toplevel:
        slug = _repo_slug(toplevel)
        return os.path.join(store.directory, f'daemon-{slug}.pid')
    return os.path.join(store.directory, 'daemon.pid')


def _status_file(store: Store, toplevel: str = '') -> str:
    if toplevel:
        slug = _repo_slug(toplevel)
        return os.path.join(store.directory, f'daemon-{slug}.status.json')
    return os.path.join(store.directory, 'daemon.status.json')


def _write_pid(store: Store, pid: int, toplevel: str = '') -> None:
    with open(_pid_file(store, toplevel), 'w') as f:
        f.write(str(pid))


def _read_pid(store: Store, toplevel: str = '') -> int | None:
    try:
        with open(_pid_file(store, toplevel)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _remove_pid(store: Store, toplevel: str = '') -> None:
    try:
        os.remove(_pid_file(store, toplevel))
    except OSError:
        pass


def _write_status(
        store: Store, data: dict[str, Any], toplevel: str = '',
) -> None:
    try:
        with open(_status_file(store, toplevel), 'w') as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def _read_status(store: Store, toplevel: str = '') -> dict[str, Any] | None:
    try:
        with open(_status_file(store, toplevel)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _remove_status(store: Store, toplevel: str = '') -> None:
    try:
        os.remove(_status_file(store, toplevel))
    except OSError:
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ts() -> str:
    """Current timestamp as ISO-8601 string (seconds precision)."""
    return datetime.datetime.now().isoformat(timespec='seconds')


def _log(msg: str) -> None:
    output.write_line(f'[{_ts()}] {msg}')


def _files_differing_from_head() -> list[str]:
    """Return tracked files whose working-tree content differs from HEAD."""
    from pre_commit.util import cmd_output_b
    try:
        # --diff-filter=d excludes deleted files (nothing to run hooks on)
        _, out, _ = cmd_output_b(
            'git', 'diff', 'HEAD', '--name-only', '-z', '--diff-filter=d',
            check=False,
        )
        return [f for f in out.decode().split('\x00') if f]
    except Exception:
        return []


def _run_hooks_on_changed(
        changed: list[str],
        hooks: list[Any],
        file_hashes: dict[str, str],
        store: Store,
        foreground: bool,
        repo_root: str = '',
) -> None:
    """Run all matching hooks on *changed* files and write results to cache."""
    from pre_commit.all_languages import languages

    classifier = Classifier(changed)
    for hook in hooks:
        if not hook.daemon:
            continue
        hook_filenames = tuple(classifier.filenames_for_hook(hook))
        if not hook_filenames:
            continue

        hook_key = _compute_hook_key(hook)
        run_filenames = hook_filenames if hook.pass_filenames else ()

        if foreground:
            _log(
                f'Running hook {hook.id!r} on '
                f'{len(hook_filenames)} file(s)…',
            )

        try:
            language = languages[hook.language]
            with language.in_env(hook.prefix, hook.language_version):
                retcode, _ = language.run_hook(
                    hook.prefix,
                    hook.entry,
                    hook.args,
                    run_filenames,
                    is_local=hook.src == 'local',
                    require_serial=hook.require_serial,
                    color=False,
                )
            result = 0 if retcode == 0 else 1
            result_str = 'pass' if result == 0 else 'fail'
            if foreground:
                _log(
                    f'Hook {hook.id!r} → {result_str} '
                    f'({len(hook_filenames)} file(s))',
                )
            if hook.pass_filenames:
                for f in hook_filenames:
                    store.set_hook_result(
                        hook_key, f, file_hashes.get(f, ''), result,
                        repo_root=repo_root,
                    )
        except Exception as exc:
            if foreground:
                _log(f'Error running hook {hook.id!r}: {exc}')


def _hook_keys_for_hooks(hooks: list[Any]) -> set[str]:
    return {_compute_hook_key(h) for h in hooks}


def _show_cache_summary(
        config_file: str,
        store: Store,
        repo_root: str = '',
) -> None:
    """Print per-hook cache state for the current staged files."""
    try:
        toplevel = git.get_root()
    except Exception:
        output.write_line('  (not in a git repo)')
        return

    try:
        from pre_commit.util import cmd_output_b
        _, staged_out, _ = cmd_output_b(
            'git', 'diff', '--cached', '--name-only', '--diff-filter=ACM',
            cwd=toplevel, check=False,
        )
        staged_files = [f for f in staged_out.decode().splitlines() if f]
    except Exception:
        staged_files = []

    if not staged_files:
        output.write_line('  No staged files.')
        return

    try:
        config = load_config(config_file)
        hooks = list(all_hooks(config, store))
    except Exception as exc:
        output.write_line(f'  (could not load config: {exc})')
        return

    all_pass = True
    all_checked = True

    for hook in hooks:
        classifier = Classifier(staged_files)
        hook_filenames = tuple(classifier.filenames_for_hook(hook))
        if not hook_filenames:
            continue

        hook_key = _compute_hook_key(hook)
        output.write_line(f'  Hook: {hook.name}')
        for f in hook_filenames:
            fhash = _file_hash(f)
            result = store.get_hook_result(
                hook_key, f, fhash, repo_root=repo_root,
            )
            if result == 0:
                mark = 'pass'
            elif result == 1:
                mark = 'FAIL'
                all_pass = False
            else:
                mark = '?   '
                all_pass = False
                all_checked = False
            output.write_line(f'    [{mark}]  {f}')

    output.write_line('')
    if all_pass:
        output.write_line('  Ready to commit: YES (all hooks passed)')
    elif not all_checked:
        output.write_line(
            '  Ready to commit: UNKNOWN '
            '(some files not yet checked — is the daemon running?)',
        )
    else:
        output.write_line('  Ready to commit: NO (some hooks failed)')


def _daemon_start(
    config_file: str,
    store: Store,
    interval: float,
    foreground: bool = False,
) -> int:
    # Resolve git root first so we can scope the PID file per repo
    try:
        toplevel = git.get_root()
    except Exception as exc:
        output.write_line(f'Not in a git repository: {exc}')
        return 1

    pid = _read_pid(store, toplevel)
    if pid is not None and _is_running(pid):
        output.write_line(f'pre-commit daemon already running (pid {pid})')
        return 1

    config_abs = (
        config_file if os.path.isabs(config_file)
        else os.path.join(os.getcwd(), config_file)
    )

    # Check if daemon is disabled in config
    if os.path.exists(config_abs):
        try:
            cfg = load_config(config_abs)
            if not cfg.get('daemon', True):
                output.write_line(
                    f'Daemon is disabled in {config_file} '
                    '(`daemon: false`). Set `daemon: true` to enable.',
                )
                return 1
        except Exception:
            pass  # config parse error — let the daemon handle it

    if not foreground:
        child_pid = os.fork()
        if child_pid > 0:
            # Parent: wait briefly then report
            time.sleep(0.15)
            output.write_line(
                f'pre-commit daemon started in background (pid {child_pid})',
            )
            return 0
        # Child: become session leader, redirect stdio to /dev/null
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

    os.chdir(toplevel)
    config_file = os.path.relpath(config_abs)

    _write_pid(store, os.getpid(), toplevel)
    started_at = _ts()
    _write_status(
        store, {
            'pid': os.getpid(),
            'started_at': started_at,
            'config_file': config_file,
            'last_activity': started_at,
            'last_hook': None,
            'last_result': None,
            'hooks_count': 0,
        }, toplevel,
    )

    if foreground:
        _log(
            f'pre-commit daemon started (pid {os.getpid()}), '
            f'polling every {interval}s',
        )

    running = True

    def _stop(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        config = load_config(config_file)
        hooks = list(all_hooks(config, store))
        install_hook_envs(hooks, store)

        config_hash = _file_hash(config_file)
        hooks_count = len(hooks)

        # Seed initial file hashes (working-tree content)
        tracked: list[str] = list(git.get_all_files())
        file_hashes: dict[str, str] = {f: _file_hash(f) for f in tracked}

        # On startup: immediately run hooks on files that differ from HEAD
        initial_changed = _files_differing_from_head()
        if initial_changed:
            if foreground:
                _log(
                    f'Startup: {len(initial_changed)} file(s)'
                    f' differ from HEAD, running hooks…',
                )
            _run_hooks_on_changed(
                initial_changed, hooks, file_hashes, store, foreground,
                repo_root=toplevel,
            )

        while running:
            time.sleep(interval)

            # Detect config file changes
            new_config_hash = _file_hash(config_file)
            if new_config_hash != config_hash:
                if foreground:
                    _log(f'Config changed ({config_file}), reloading…')
                old_hook_keys = _hook_keys_for_hooks(hooks)
                try:
                    config = load_config(config_file)
                    hooks = list(all_hooks(config, store))
                    install_hook_envs(hooks, store)
                    new_hook_keys = _hook_keys_for_hooks(hooks)
                    stale_keys = old_hook_keys - new_hook_keys
                    if stale_keys:
                        store.purge_stale_hook_results(
                            stale_keys, repo_root=toplevel,
                        )
                        if foreground:
                            _log(
                                f'Purged cache for {len(stale_keys)} '
                                f'removed/changed hook(s)',
                            )
                    config_hash = new_config_hash
                    hooks_count = len(hooks)
                    if foreground:
                        _log(
                            f'Config reloaded: {hooks_count} hook(s) active',
                        )
                except Exception as exc:
                    if foreground:
                        _log(f'Failed to reload config: {exc}')
                    continue

            try:
                current_files = set(git.get_all_files())
            except Exception as exc:
                if foreground:
                    _log(f'git ls-files failed: {exc}')
                continue

            changed = [
                f for f in current_files
                if _file_hash(f) != file_hashes.get(f)
            ]

            # Refresh stored hashes (new, changed, and deleted files)
            for f in changed:
                file_hashes[f] = _file_hash(f)
            for f in set(file_hashes) - current_files:
                del file_hashes[f]

            if not changed:
                continue

            if foreground:
                _log(
                    f'{len(changed)} changed file(s): '
                    f'{", ".join(changed[:5])}'
                    f'{"…" if len(changed) > 5 else ""}',
                )

            _run_hooks_on_changed(
                changed, hooks, file_hashes, store, foreground,
                repo_root=toplevel,
            )
            _write_status(
                store, {
                    'pid': os.getpid(),
                    'started_at': started_at,
                    'config_file': config_file,
                    'last_activity': _ts(),
                    'last_hook': None,
                    'last_result': None,
                    'hooks_count': hooks_count,
                }, toplevel,
            )
    finally:
        _remove_pid(store, toplevel)
        _remove_status(store, toplevel)
        if foreground:
            _log('pre-commit daemon stopped')

    return 0


def _daemon_stop(store: Store, toplevel: str = '') -> int:
    pid = _read_pid(store, toplevel)
    if pid is None:
        output.write_line('No daemon running')
        return 1
    if not _is_running(pid):
        output.write_line(f'No daemon running (stale pid file for {pid})')
        _remove_pid(store, toplevel)
        _remove_status(store, toplevel)
        return 1
    os.kill(pid, signal.SIGTERM)
    output.write_line(f'Sent SIGTERM to daemon (pid {pid})')
    return 0


def _daemon_status(
        store: Store,
        config_file: str | None = None,
        toplevel: str = '',
) -> int:
    pid = _read_pid(store, toplevel)
    if pid is None:
        output.write_line('Daemon is not running')
        return 1
    if not _is_running(pid):
        output.write_line(f'Daemon is not running (stale pid file for {pid})')
        _remove_pid(store, toplevel)
        _remove_status(store, toplevel)
        return 1

    # Running — show rich info from status file
    status = _read_status(store, toplevel)
    if status:
        started = status.get('started_at', '?')
        config = status.get('config_file', '?')
        nhooks = status.get('hooks_count', '?')
        activity = status.get('last_activity', '?')
        output.write_line(f'Daemon is running (pid {pid})')
        output.write_line(f'  Started at:     {started}')
        output.write_line(f'  Config file:    {config}')
        output.write_line(f'  Hooks:          {nhooks}')
        output.write_line(f'  Last activity:  {activity}')
        if status.get('last_hook'):
            output.write_line(
                f'  Last hook:      {status["last_hook"]} '
                f'→ {status.get("last_result", "?")}',
            )
    else:
        output.write_line(f'Daemon is running (pid {pid})')

    # Cache summary for staged files
    if config_file:
        output.write_line('')
        output.write_line('Cache summary (staged files):')
        _show_cache_summary(config_file, store, repo_root=toplevel)

    return 0


def daemon(config_file: str, store: Store, args: argparse.Namespace) -> int:
    subcommand = args.daemon_subcommand or 'start'
    if subcommand == 'start':
        return _daemon_start(
            config_file,
            store,
            interval=args.interval,
            foreground=args.foreground,
        )
    # For stop/status resolve the repo root here so the right PID file is used
    try:
        toplevel = git.get_root()
    except Exception:
        toplevel = ''
    if subcommand == 'stop':
        return _daemon_stop(store, toplevel)
    elif subcommand == 'status':
        return _daemon_status(
            store, config_file=config_file, toplevel=toplevel,
        )
    else:
        raise NotImplementedError(f'Unknown daemon subcommand: {subcommand}')
