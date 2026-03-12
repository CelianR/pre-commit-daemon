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
from pre_commit.color import format_color
from pre_commit.color import GREEN
from pre_commit.color import RED
from pre_commit.color import SUBTLE
from pre_commit.color import YELLOW
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
        if not hook.cache:
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
                retcode, run_out = language.run_hook(
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
            for f in hook_filenames:
                store.set_hook_result(
                    hook_key, f, file_hashes.get(f, ''), result,
                    repo_root=repo_root,
                )
            if result == 1 and not hook.fix:
                store.set_hook_output(
                    hook_key, run_out, repo_root=repo_root,
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
        filter_statuses: set[str] | None = None,
        files_mode: str = 'current',
        use_color: bool = False,
) -> None:
    """Print per-hook cache state for files differing from HEAD or staged files."""
    try:
        toplevel = git.get_root()
    except Exception:
        output.write_line('  (not in a git repo)')
        return

    if files_mode == 'current':
        checked_files = _files_differing_from_head()
        if not checked_files:
            output.write_line('  No modified files.')
            return
    else:
        try:
            from pre_commit.util import cmd_output_b
            _, staged_out, _ = cmd_output_b(
                'git', 'diff', '--cached', '--name-only',
                '--diff-filter=ACM',
                cwd=toplevel, check=False,
            )
            checked_files = [
                f for f in staged_out.decode().splitlines() if f
            ]
        except Exception:
            checked_files = []
        if not checked_files:
            output.write_line('  No staged files.')
            return

    try:
        config = load_config(config_file)
        hooks = [h for h in all_hooks(config, store) if h.cache]
    except Exception as exc:
        output.write_line(f'  (could not load config: {exc})')
        return

    all_pass = True
    all_checked = True

    # Single Classifier instance — _types_for_file is cached on the instance
    # so file type detection is done at most once per file across all hooks.
    classifier = Classifier(checked_files)
    # Lazy hash cache: only read files that are actually matched by some hook,
    # avoiding a full scan of all tracked files up-front.
    file_hashes: dict[str, str] = {}

    for hook in hooks:
        hook_filenames = tuple(classifier.filenames_for_hook(hook))
        if not hook_filenames:
            continue

        # Hash only newly-seen files for this hook
        for f in hook_filenames:
            if f not in file_hashes:
                file_hashes[f] = _file_hash(f)

        hook_key = _compute_hook_key(hook)
        # One DB query per hook instead of one per file
        cached = store.get_hook_results_bulk(
            hook_key, hook_filenames, repo_root=repo_root,
        )
        hook_lines = []
        for f in hook_filenames:
            fhash = file_hashes[f]
            cached_entry = cached.get(f)
            if cached_entry is not None and cached_entry[0] == fhash:
                result: int | None = cached_entry[1]
            else:
                result = None  # hash mismatch or never cached
            if result == 0:
                mark = format_color('pass', GREEN, use_color)
                status_key = 'pass'
            elif result == 1:
                mark = format_color('FAIL', RED, use_color)
                status_key = 'fail'
                all_pass = False
            else:
                mark = format_color('?   ', YELLOW, use_color)
                status_key = 'unknown'
                all_pass = False
                all_checked = False
            if filter_statuses is None or status_key in filter_statuses:
                hook_lines.append(f'    [{mark}]  {f}')

        if hook_lines:
            output.write_line(f'  Hook: {hook.name}')
            for line in hook_lines:
                output.write_line(line)

    output.write_line('')
    if all_pass:
        yes = format_color('YES', GREEN, use_color)
        output.write_line(f'  Ready to commit: {yes} (all hooks passed)')
    elif not all_checked:
        unknown = format_color('UNKNOWN', YELLOW, use_color)
        output.write_line(f'  Ready to commit: {unknown}')
    else:
        no = format_color('NO', RED, use_color)
        output.write_line(f'  Ready to commit: {no} (some hooks failed)')


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

    # Validate config before forking — avoids a silent daemon exit
    if not os.path.exists(config_abs):
        output.write_line(
            f'Config file not found: {config_file}\n'
            'Create a .pre-commit-config.yaml first.',
        )
        return 1
    try:
        cfg = load_config(config_abs)
        if not cfg.get('daemon', True):
            output.write_line(
                f'Daemon is disabled in {config_file} '
                '(`daemon: false`). Set `daemon: true` to enable.',
            )
            return 1
    except Exception as exc:
        output.write_line(f'Invalid config {config_file!r}: {exc}')
        return 1

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
                    # Re-run hooks on files that differ from HEAD so the
                    # cache reflects the new hook definitions immediately.
                    reload_changed = _files_differing_from_head()
                    if reload_changed:
                        if foreground:
                            _log(
                                f'Running new hooks on '
                                f'{len(reload_changed)} file(s) '
                                f'differing from HEAD…',
                            )
                        _run_hooks_on_changed(
                            reload_changed, hooks, file_hashes, store,
                            foreground, repo_root=toplevel,
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
        filter_statuses: set[str] | None = None,
        files_mode: str = 'current',
        use_color: bool = False,
) -> int:
    pid = _read_pid(store, toplevel)
    if pid is None:
        not_running = format_color('Daemon is not running', RED, use_color)
        output.write_line(not_running)
        return 1
    if not _is_running(pid):
        not_running = format_color(
            f'Daemon is not running (stale pid file for {pid})',
            RED, use_color,
        )
        output.write_line(not_running)
        _remove_pid(store, toplevel)
        _remove_status(store, toplevel)
        return 1

    # Running — show rich info from status file
    status = _read_status(store, toplevel)
    running_hdr = format_color(
        f'Daemon is running (pid {pid})', GREEN, use_color,
    )
    if status:
        started = status.get('started_at', '?')
        config = status.get('config_file', '?')
        nhooks = status.get('hooks_count', '?')
        activity = status.get('last_activity', '?')
        output.write_line(running_hdr)
        output.write_line(
            f'  {format_color("Started at:", SUBTLE, use_color)}     {started}',
        )
        output.write_line(
            f'  {format_color("Config file:", SUBTLE, use_color)}    {config}',
        )
        output.write_line(
            f'  {format_color("Hooks:", SUBTLE, use_color)}          {nhooks}',
        )
        output.write_line(
            f'  {format_color("Last activity:", SUBTLE, use_color)}  {activity}',
        )
        if status.get('last_hook'):
            output.write_line(
                f'  {format_color("Last hook:", SUBTLE, use_color)}      '
                f'{status["last_hook"]} '
                f'→ {status.get("last_result", "?")}',
            )
    else:
        output.write_line(running_hdr)

    # Cache summary
    if config_file:
        label = (
            'current files' if files_mode == 'current'
            else 'staged files'
        )
        output.write_line('')
        output.write_line(f'Cache summary ({label}):')
        _show_cache_summary(
            config_file, store, repo_root=toplevel,
            filter_statuses=filter_statuses, files_mode=files_mode,
            use_color=use_color,
        )

    return 0


def _daemon_clear(
        config_file: str,
        store: Store,
        toplevel: str = '',
        hook_id: str | None = None,
        file_path: str | None = None,
) -> int:
    """Clear cached hook results (and stored outputs) for this repo."""
    hook_keys: set[str] | None = None
    if hook_id is not None:
        try:
            config = load_config(config_file)
            hooks = list(all_hooks(config, store))
        except Exception as exc:
            output.write_line(f'Could not load config: {exc}')
            return 1
        matched = [
            h for h in hooks
            if h.id == hook_id or h.alias == hook_id
        ]
        if not matched:
            output.write_line(f'No hook with id {hook_id!r} found')
            return 1
        hook_keys = {_compute_hook_key(h) for h in matched}

    n = store.clear_hook_results(
        repo_root=toplevel,
        hook_keys=hook_keys,
        file_path=file_path,
    )
    store.clear_hook_outputs(repo_root=toplevel, hook_keys=hook_keys)

    parts = []
    if hook_id:
        parts.append(f'hook {hook_id!r}')
    if file_path:
        parts.append(f'file {file_path!r}')
    scope = ' and '.join(parts) if parts else 'all hooks'
    output.write_line(f'Cleared {n} cached result(s) for {scope}')
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
        raw = getattr(args, 'filter_statuses', 'fail,pass')
        filter_statuses = {
            s.strip().lower().replace('?', 'unknown')
            for s in raw.split(',')
            if s.strip()
        }
        return _daemon_status(
            store,
            config_file=config_file,
            toplevel=toplevel,
            filter_statuses=filter_statuses,
            files_mode=getattr(args, 'files_mode', 'current'),
            use_color=getattr(args, 'color', False),
        )
    elif subcommand == 'clear':
        return _daemon_clear(
            config_file,
            store,
            toplevel=toplevel,
            hook_id=getattr(args, 'hook_id', None),
            file_path=getattr(args, 'file_path', None),
        )
    else:
        raise NotImplementedError(f'Unknown daemon subcommand: {subcommand}')
