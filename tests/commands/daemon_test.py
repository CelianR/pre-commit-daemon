"""Tests for the daemon lifecycle helpers in pre_commit/commands/daemon.py."""
from __future__ import annotations

import os

from pre_commit import git
from pre_commit.commands.daemon import _daemon_start
from pre_commit.commands.daemon import _daemon_status
from pre_commit.commands.daemon import _daemon_stop
from pre_commit.commands.daemon import _files_differing_from_head
from pre_commit.commands.daemon import _is_running
from pre_commit.commands.daemon import _pid_file
from pre_commit.commands.daemon import _read_pid
from pre_commit.commands.daemon import _read_status
from pre_commit.commands.daemon import _remove_pid
from pre_commit.commands.daemon import _repo_slug
from pre_commit.commands.daemon import _show_cache_summary
from pre_commit.commands.daemon import _write_pid
from pre_commit.commands.daemon import _write_status
from testing.fixtures import write_config
from testing.util import cwd
from testing.util import git_commit


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def test_pid_file_is_inside_store_directory(store):
    path = _pid_file(store)
    assert path.startswith(store.directory)
    assert path.endswith('daemon.pid')


def test_write_and_read_pid(store):
    _write_pid(store, 42)
    assert _read_pid(store) == 42


def test_read_pid_when_absent(store):
    assert _read_pid(store) is None


def test_remove_pid(store):
    _write_pid(store, 42)
    _remove_pid(store)
    assert _read_pid(store) is None


def test_remove_pid_is_idempotent(store):
    """Removing a nonexistent PID file should not raise."""
    _remove_pid(store)
    _remove_pid(store)


# ---------------------------------------------------------------------------
# Status file helpers
# ---------------------------------------------------------------------------

def test_write_and_read_status(store):
    data = {'pid': 99, 'started_at': '2026-01-01T00:00:00', 'hooks_count': 3}
    _write_status(store, data)
    result = _read_status(store)
    assert result == data


def test_read_status_when_absent(store):
    assert _read_status(store) is None


# ---------------------------------------------------------------------------
# _is_running
# ---------------------------------------------------------------------------

def test_is_running_current_process():
    assert _is_running(os.getpid()) is True


def test_is_running_nonexistent_pid():
    assert _is_running(536870911) is False


# ---------------------------------------------------------------------------
# daemon status (PID-level)
# ---------------------------------------------------------------------------

def test_daemon_status_not_running(cap_out, store):
    ret = _daemon_status(store)
    assert ret == 1
    assert b'not running' in cap_out.get_bytes()


def test_daemon_status_running(cap_out, store):
    _write_pid(store, os.getpid())
    ret = _daemon_status(store)
    assert ret == 0
    assert b'running' in cap_out.get_bytes()


def test_daemon_status_running_shows_rich_info(cap_out, store):
    """When status file exists, _daemon_status prints richer info."""
    _write_pid(store, os.getpid())
    _write_status(
        store, {
            'pid': os.getpid(),
            'started_at': '2026-01-01T10:00:00',
            'config_file': '.pre-commit-config.yaml',
            'last_activity': '2026-01-01T10:05:00',
            'last_hook': 'trailing-whitespace',
            'last_result': 'pass',
            'hooks_count': 2,
        },
    )
    ret = _daemon_status(store)
    printed = cap_out.get_bytes()
    assert ret == 0
    assert b'trailing-whitespace' in printed
    assert b'2026-01-01T10:00:00' in printed


def test_daemon_status_stale_pid_cleans_up(cap_out, store):
    """A stale PID file (process gone) is removed and 1 is returned."""
    _write_pid(store, 536870911)
    ret = _daemon_status(store)
    assert ret == 1
    assert _read_pid(store) is None


# ---------------------------------------------------------------------------
# daemon stop
# ---------------------------------------------------------------------------

def test_daemon_stop_no_daemon(cap_out, store):
    ret = _daemon_stop(store)
    assert ret == 1
    assert b'No daemon' in cap_out.get_bytes()


def test_daemon_stop_stale_pid_cleans_up(cap_out, store):
    """A stale PID → clean up and return 1, no signal sent."""
    _write_pid(store, 536870911)
    ret = _daemon_stop(store)
    assert ret == 1
    assert _read_pid(store) is None


# ---------------------------------------------------------------------------
# daemon start guard (already-running)
# ---------------------------------------------------------------------------

def test_daemon_start_when_already_running(cap_out, store, in_git_dir):
    """start returns 1 immediately when a daemon is already running."""
    with cwd(str(in_git_dir)):
        toplevel = git.get_root()
        _write_pid(store, os.getpid(), toplevel)
        ret = _daemon_start('does-not-matter.yaml', store, interval=1.0)
    assert ret == 1
    assert b'already running' in cap_out.get_bytes()


# ---------------------------------------------------------------------------
# daemon: false / daemon: true in config
# ---------------------------------------------------------------------------

def test_daemon_start_blocked_when_daemon_false_in_config(
    cap_out, store, in_git_dir,
):
    """_daemon_start returns 1 when config has daemon: false."""
    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [
                    {
                        'id': 'h', 'name': 'h',
                        'entry': 'true', 'language': 'system',
                    },
                ],
            }],
            'daemon': False,
        },
    )
    with cwd(str(in_git_dir)):
        ret = _daemon_start('.pre-commit-config.yaml', store, interval=1.0)
    assert ret == 1
    assert b'disabled' in cap_out.get_bytes()


def test_daemon_start_allowed_when_daemon_true_in_config(
    cap_out, store, in_git_dir,
):
    """_daemon_start is not blocked when config has daemon: true."""
    # We can't fully start the daemon in a test (it would loop forever),
    # but we verify it doesn't fail before even trying.  We simulate
    # "already running" so the function returns early after the config check.
    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [
                    {
                        'id': 'h', 'name': 'h',
                        'entry': 'true', 'language': 'system',
                    },
                ],
            }],
            'daemon': True,
        },
    )
    with cwd(str(in_git_dir)):
        toplevel = git.get_root()
        _write_pid(store, os.getpid(), toplevel)  # already running
        ret = _daemon_start('.pre-commit-config.yaml', store, interval=1.0)
    # Returns 1 because already running — NOT because daemon is disabled
    assert ret == 1
    assert b'already running' in cap_out.get_bytes()
    assert b'disabled' not in cap_out.get_bytes()


def test_daemon_start_allowed_when_daemon_key_absent(
    cap_out, store, in_git_dir,
):
    """Not blocked when config has no daemon key (defaults to true)."""
    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [
                    {
                        'id': 'h', 'name': 'h',
                        'entry': 'true', 'language': 'system',
                    },
                ],
            }],
        },
    )
    with cwd(str(in_git_dir)):
        toplevel = git.get_root()
        _write_pid(store, os.getpid(), toplevel)  # already running
        ret = _daemon_start('.pre-commit-config.yaml', store, interval=1.0)
    assert ret == 1
    assert b'already running' in cap_out.get_bytes()
    assert b'disabled' not in cap_out.get_bytes()


# ---------------------------------------------------------------------------
# cache summary (_show_cache_summary)
# ---------------------------------------------------------------------------

def _make_mock_hook(**kwargs):
    """Build a simple mock hook object for cache-summary tests."""
    from unittest import mock
    defaults = dict(
        id='hook', name='My Hook', entry='cmd', args=(),
        language='system', language_version='default',
        files='', exclude='^$', types=['file'], types_or=[],
        exclude_types=[],
    )
    defaults.update(kwargs)
    h = mock.Mock()
    for k, v in defaults.items():
        setattr(h, k, v)
    return h


# ---------------------------------------------------------------------------
# _files_differing_from_head
# ---------------------------------------------------------------------------

def test_files_differing_from_head_empty_on_clean_tree(in_git_dir):
    """Clean working tree → no files differ from HEAD."""
    with cwd(str(in_git_dir)):
        git_commit()  # ensure there is a HEAD commit
        assert _files_differing_from_head() == []


def test_files_differing_from_head_detects_modification(in_git_dir):
    """A modified (not yet staged) file appears in the result."""
    with cwd(str(in_git_dir)):
        git_commit()
        with open('modified.py', 'w') as fh:
            fh.write('x = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'modified.py')
        git_commit()

        # Now modify the file without staging
        with open('modified.py', 'w') as fh:
            fh.write('x = 99\n')

        changed = _files_differing_from_head()
        assert 'modified.py' in changed


def test_files_differing_from_head_detects_staged(in_git_dir):
    """A staged (index) change also appears in the result."""
    with cwd(str(in_git_dir)):
        git_commit()
        with open('staged.py', 'w') as fh:
            fh.write('a = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'staged.py')
        git_commit()

        # Stage a change
        with open('staged.py', 'w') as fh:
            fh.write('a = 2\n')
        cmd_output_b('git', 'add', 'staged.py')

        changed = _files_differing_from_head()
        assert 'staged.py' in changed


# ---------------------------------------------------------------------------
# cache summary (_show_cache_summary)
# ---------------------------------------------------------------------------

def test_show_cache_summary_no_staged_files(cap_out, store, in_git_dir):
    """With no staged files, summary says 'No staged files'."""
    with cwd(str(in_git_dir)):
        _show_cache_summary('.pre-commit-config.yaml', store)
    assert b'No staged files' in cap_out.get_bytes()


def test_show_cache_summary_all_pass(cap_out, store, in_git_dir):
    """All staged files cached-pass → Ready YES."""
    import shlex
    import sys
    _PASS = f'{shlex.quote(sys.executable)} -c "pass"'

    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [{
                    'id': 'checker', 'name': 'Checker',
                    'entry': _PASS, 'language': 'system',
                }],
            }],
        },
    )
    with cwd(str(in_git_dir)):
        # Create and stage a file
        with open('f.py', 'w') as fh:
            fh.write('x = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'f.py')

        # Seed cache with a pass result
        from pre_commit.commands.run import _file_hash, _compute_hook_key
        from pre_commit.clientlib import load_config
        from pre_commit.repository import all_hooks
        cfg = load_config('.pre-commit-config.yaml')
        hooks = list(all_hooks(cfg, store))
        for hook in hooks:
            hk = _compute_hook_key(hook)
            fhash = _file_hash('f.py')
            store.set_hook_result(hk, 'f.py', fhash, 0)

        _show_cache_summary('.pre-commit-config.yaml', store)

    printed = cap_out.get_bytes()
    assert b'pass' in printed
    assert b'YES' in printed


def test_show_cache_summary_with_fail(cap_out, store, in_git_dir):
    """A cached-fail file → Ready NO."""
    import shlex
    import sys
    _FAIL = f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(1)"'

    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [{
                    'id': 'checker', 'name': 'Checker',
                    'entry': _FAIL, 'language': 'system',
                }],
            }],
        },
    )
    with cwd(str(in_git_dir)):
        with open('f.py', 'w') as fh:
            fh.write('x = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'f.py')

        from pre_commit.commands.run import _file_hash, _compute_hook_key
        from pre_commit.clientlib import load_config
        from pre_commit.repository import all_hooks
        cfg = load_config('.pre-commit-config.yaml')
        hooks = list(all_hooks(cfg, store))
        for hook in hooks:
            hk = _compute_hook_key(hook)
            fhash = _file_hash('f.py')
            store.set_hook_result(hk, 'f.py', fhash, 1)

        _show_cache_summary('.pre-commit-config.yaml', store)

    printed = cap_out.get_bytes()
    assert b'FAIL' in printed
    assert b'NO' in printed


def test_show_cache_summary_with_unchecked(cap_out, store, in_git_dir):
    """A file with no cache entry → Ready UNKNOWN."""
    import shlex
    import sys
    _PASS = f'{shlex.quote(sys.executable)} -c "pass"'

    write_config(
        '.', {
            'repos': [{
                'repo': 'local',
                'hooks': [{
                    'id': 'checker', 'name': 'Checker',
                    'entry': _PASS, 'language': 'system',
                }],
            }],
        },
    )
    with cwd(str(in_git_dir)):
        with open('f.py', 'w') as fh:
            fh.write('x = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'f.py')

        # Do NOT seed any cache entry
        _show_cache_summary('.pre-commit-config.yaml', store)

    printed = cap_out.get_bytes()
    assert b'?' in printed
    assert b'UNKNOWN' in printed


# ---------------------------------------------------------------------------
# Per-hook daemon: false flag
# ---------------------------------------------------------------------------

def test_run_hooks_on_changed_skips_daemon_false_hooks(store):
    """Hooks with daemon: false are skipped entirely by the daemon watcher."""
    from unittest import mock
    from pre_commit.commands.daemon import _run_hooks_on_changed

    def _mock_hook(id_, daemon_enabled):
        h = mock.Mock()
        h.id = id_
        h.daemon = daemon_enabled
        h.pass_filenames = True
        h.files = ''
        h.exclude = '^$'
        h.types = ['file']
        h.types_or = []
        h.exclude_types = []
        return h

    hook_on = _mock_hook('on', daemon_enabled=True)
    hook_off = _mock_hook('off', daemon_enabled=False)

    with mock.patch('pre_commit.commands.daemon.Classifier') as MockClassifier:
        instance = MockClassifier.return_value
        instance.filenames_for_hook.return_value = iter([])

        _run_hooks_on_changed(
            ['f.py'], [hook_on, hook_off], {}, store, foreground=False,
        )

        # filenames_for_hook only called for enabled hook, not disabled
        called_hooks = [
            call.args[0] for call in instance.filenames_for_hook.call_args_list
        ]
        assert hook_on in called_hooks
        assert hook_off not in called_hooks


def test_daemon_false_hook_config_is_valid():
    """daemon: false is accepted as valid hook config (schema check)."""
    from testing.fixtures import write_config
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as d:
        write_config(
            d, {
                'repos': [{
                    'repo': 'local',
                    'hooks': [{
                        'id': 'h',
                        'name': 'h',
                        'entry': 'true',
                        'language': 'system',
                        'daemon': False,
                    }],
                }],
            },
        )
        from pre_commit.clientlib import load_config
        cfg = load_config(os.path.join(d, '.pre-commit-config.yaml'))
        hook_cfg = cfg['repos'][0]['hooks'][0]
        assert hook_cfg['daemon'] is False


# ---------------------------------------------------------------------------
# Per-repo isolation: PID files
# ---------------------------------------------------------------------------

def test_pid_file_differs_by_repo(store):
    """Two different toplevel paths → two different PID file paths."""
    path_a = _pid_file(store, '/repos/project-a')
    path_b = _pid_file(store, '/repos/project-b')
    assert path_a != path_b
    assert path_a.startswith(store.directory)
    assert path_b.startswith(store.directory)


def test_pid_file_no_toplevel_uses_default(store):
    """No toplevel argument → falls back to the global daemon.pid name."""
    path = _pid_file(store)
    assert path == _pid_file(store, '')
    assert path.endswith('daemon.pid')


def test_pid_file_same_repo_same_slug(store):
    """Same toplevel always produces the same PID file path."""
    p1 = _pid_file(store, '/repos/my-project')
    p2 = _pid_file(store, '/repos/my-project')
    assert p1 == p2


def test_repo_slug_is_12_hex_chars():
    slug = _repo_slug('/any/path')
    assert len(slug) == 12
    assert all(c in '0123456789abcdef' for c in slug)


def test_two_repos_pid_files_are_independent(store):
    """Writing a PID for repo A is invisible when queried for repo B."""
    _write_pid(store, 42, '/repos/project-a')
    assert _read_pid(store, '/repos/project-a') == 42
    assert _read_pid(store, '/repos/project-b') is None


def test_daemon_start_in_two_repos_is_independent(
        cap_out, store, tempdir_factory,
):
    """PID files are per-repo: daemon in repo A is invisible to repo B."""
    from testing.fixtures import git_dir as make_git_dir
    repo_a = make_git_dir(tempdir_factory)
    repo_b = make_git_dir(tempdir_factory)

    with cwd(repo_a):
        git_commit()
        toplevel_a = git.get_root()
    with cwd(repo_b):
        git_commit()
        toplevel_b = git.get_root()

    # Simulate daemon running in repo A (this test process itself)
    _write_pid(store, os.getpid(), toplevel_a)

    # Repo B sees no daemon
    assert _read_pid(store, toplevel_b) is None
    assert _daemon_status(store, toplevel=toplevel_b) == 1

    # Repo A sees its daemon running
    assert _read_pid(store, toplevel_a) == os.getpid()
    assert _daemon_status(store, toplevel=toplevel_a) == 0


# ---------------------------------------------------------------------------
# Per-repo isolation: hook result cache
# ---------------------------------------------------------------------------

def test_cache_result_isolated_between_repos(store, tempdir_factory):
    """Hook results written for repo A are invisible to repo B."""
    import shlex
    import sys
    _PASS = f'{shlex.quote(sys.executable)} -c "pass"'

    from testing.fixtures import git_dir as make_git_dir
    repo_a = make_git_dir(tempdir_factory)
    repo_b = make_git_dir(tempdir_factory)

    # Write a config in repo_a
    write_config(
        repo_a, {
            'repos': [{
                'repo': 'local',
                'hooks': [{
                    'id': 'checker', 'name': 'Checker',
                    'entry': _PASS, 'language': 'system',
                }],
            }],
        },
    )

    with cwd(repo_a):
        git_commit()
        toplevel_a = git.get_root()
        with open('f.py', 'w') as fh:
            fh.write('x = 1\n')
        from pre_commit.util import cmd_output_b
        cmd_output_b('git', 'add', 'f.py')

        from pre_commit.commands.run import _file_hash, _compute_hook_key
        from pre_commit.clientlib import load_config
        from pre_commit.repository import all_hooks
        cfg = load_config('.pre-commit-config.yaml')
        hooks = list(all_hooks(cfg, store))
        for hook in hooks:
            hk = _compute_hook_key(hook)
            fhash = _file_hash('f.py')
            # Store result under repo_a's root
            store.set_hook_result(hk, 'f.py', fhash, 0, repo_root=toplevel_a)
            # Repo B should not see this result
            assert store.get_hook_result(
                hk, 'f.py', fhash, repo_root=repo_b,
            ) is None
            # Repo A should see it
            assert store.get_hook_result(
                hk, 'f.py', fhash, repo_root=toplevel_a,
            ) == 0
