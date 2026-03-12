"""Tests for the `fix` entry field and per-file result cache in run.py."""
from __future__ import annotations

import shlex
import sys
from unittest import mock

import pre_commit.constants as C
from pre_commit.commands.run import _compute_hook_key
from pre_commit.commands.run import _file_hash
from pre_commit.commands.run import run
from testing.fixtures import write_config
from testing.util import cwd
from testing.util import run_opts


# ---------------------------------------------------------------------------
# Helpers (mirrors run_test.py _do_run)
# ---------------------------------------------------------------------------

def _do_run(cap_out, store, repo, args, config_file=C.CONFIG_FILE):
    with cwd(repo):
        ret = run(config_file, store, args)
    return ret, cap_out.get_bytes()


def _local_hook(*, id, name, entry, language='system', fix='', **extra):
    hook = {
        'id': id,
        'name': name,
        'entry': entry,
        'language': language,
    }
    if fix:
        hook['fix'] = fix
    hook.update(extra)
    return {'repo': 'local', 'hooks': [hook]}


# Conveniently quoted path to the current Python interpreter
_PY = shlex.quote(sys.executable)
_FAIL = f'{_PY} -c "import sys; sys.exit(1)"'
_PASS = f'{_PY} -c "pass"'


# ---------------------------------------------------------------------------
# Unit tests: _file_hash
# ---------------------------------------------------------------------------

def test_file_hash_stable(tmp_path):
    f = tmp_path / 'f.txt'
    f.write_text('hello')
    assert _file_hash(str(f)) == _file_hash(str(f))


def test_file_hash_hex_length(tmp_path):
    f = tmp_path / 'f.txt'
    f.write_text('hello')
    assert len(_file_hash(str(f))) == 64  # sha256 produces 64 hex chars


def test_file_hash_changes_with_content(tmp_path):
    f = tmp_path / 'f.txt'
    f.write_text('hello')
    h1 = _file_hash(str(f))
    f.write_text('world')
    h2 = _file_hash(str(f))
    assert h1 != h2


def test_file_hash_missing_returns_empty():
    assert _file_hash('/no/such/path/ever.txt') == ''


# ---------------------------------------------------------------------------
# Unit tests: _compute_hook_key
# ---------------------------------------------------------------------------

def _make_mock_hook(**kwargs):
    defaults = dict(
        id='hook', entry='cmd', args=(),
        language='system', language_version='default',
    )
    defaults.update(kwargs)
    h = mock.Mock()
    for k, v in defaults.items():
        setattr(h, k, v)
    return h


def test_compute_hook_key_stable():
    h1 = _make_mock_hook()
    h2 = _make_mock_hook()
    assert _compute_hook_key(h1) == _compute_hook_key(h2)


def test_compute_hook_key_hex_length():
    h = _make_mock_hook()
    assert len(_compute_hook_key(h)) == 64


def test_compute_hook_key_differs_by_entry():
    h1 = _make_mock_hook(entry='mypy')
    h2 = _make_mock_hook(entry='flake8')
    assert _compute_hook_key(h1) != _compute_hook_key(h2)


def test_compute_hook_key_differs_by_id():
    h1 = _make_mock_hook(id='hook-a')
    h2 = _make_mock_hook(id='hook-b')
    assert _compute_hook_key(h1) != _compute_hook_key(h2)


def test_compute_hook_key_differs_by_args():
    h1 = _make_mock_hook(args=('--strict',))
    h2 = _make_mock_hook(args=())
    assert _compute_hook_key(h1) != _compute_hook_key(h2)


# ---------------------------------------------------------------------------
# Integration tests: fix entry
# ---------------------------------------------------------------------------

def test_fix_runs_and_succeeds(cap_out, store, in_git_dir):
    """Hook fails → fix command passes → status Fixed, overall retval 0."""
    write_config(
        '.', _local_hook(
            id='failing', name='Failing Hook',
            entry=_FAIL, fix=_PASS,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )

    assert ret == 0
    assert b'Fixed' in printed
    assert b'Failed' not in printed


def test_fix_not_called_when_hook_passes(cap_out, store, in_git_dir):
    """Hook passes → fix command is never invoked."""
    write_config(
        '.', _local_hook(
            id='passing', name='Passing Hook',
            entry=_PASS,
            fix=_FAIL,  # would fail if called
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )

    assert ret == 0
    assert b'Passed' in printed
    assert b'Fixed' not in printed


def test_fix_also_fails_leaves_status_failed(cap_out, store, in_git_dir):
    """Hook fails AND fix command also fails → status Failed, retval 1."""
    write_config(
        '.', _local_hook(
            id='broken', name='Broken Hook',
            entry=_FAIL, fix=_FAIL,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )

    assert ret == 1
    assert b'Failed' in printed
    assert b'Fixed' not in printed


def test_fix_without_fix_field_still_fails(cap_out, store, in_git_dir):
    """No fix field → behaviour identical to pre-existing: Failed, retval 1."""
    write_config(
        '.', _local_hook(
            id='no-fix', name='No Fix Hook',
            entry=_FAIL,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )

    assert ret == 1
    assert b'Failed' in printed


# ---------------------------------------------------------------------------
# Integration tests: result cache
# ---------------------------------------------------------------------------

def test_cache_hit_on_second_run(cap_out, store, in_git_dir):
    """Second run with identical file content shows (cached) Passed."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_PASS,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))

    # First run: executes hook, writes result to cache
    ret1, printed1 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 0
    assert b'(cached)' not in printed1
    assert b'Passed' in printed1

    # Second run: file unchanged → cache hit
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 0
    assert b'(cached)' in printed2


def test_cache_miss_after_file_change(cap_out, store, in_git_dir):
    """Modifying the file busts the cache so the hook reruns."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_PASS,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))

    # First run populates cache
    _do_run(cap_out, store, str(in_git_dir), args)
    cap_out.get_bytes()  # flush

    # Modify the file → different SHA-256 → cache miss
    with open('test.py', 'w') as fh:
        fh.write('x = 2\n')

    ret, printed = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret == 0
    assert b'(cached)' not in printed
    assert b'Passed' in printed


def test_cache_failing_result_short_circuits_as_fail(
        cap_out, store, in_git_dir,
):
    """Cached fail result short-circuits as (cached) Failed on next run."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))

    # First run: hook fails, result 1 written to cache
    ret1, printed1 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 1
    assert b'(cached)' not in printed1

    # Second run: cache has result=1 → short-circuit as (cached) Failed
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 1
    assert b'(cached)' in printed2
    assert b'Failed' in printed2


def test_no_cached_output_on_first_run(cap_out, store, in_git_dir):
    """First run never shows (cached) — the cache is empty to begin with."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_PASS,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )
    assert ret == 0
    assert b'Passed' in printed
    assert b'(cached)' not in printed


# ---------------------------------------------------------------------------
# Integration tests: --no-fix flag
# ---------------------------------------------------------------------------

def test_no_fix_flag_disables_fix_command(cap_out, store, in_git_dir):
    """When --no-fix is set, fix command is not invoked even if hook fails."""
    write_config(
        '.', _local_hook(
            id='failing', name='Failing Hook',
            entry=_FAIL, fix=_PASS,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir),
        run_opts(files=('test.py',), no_fix=True),
    )

    # fix would succeed, but --no-fix means hook stays failed
    assert ret == 1
    assert b'Failed' in printed
    assert b'Fixed' not in printed


def test_no_fix_flag_does_not_affect_passing_hooks(cap_out, store, in_git_dir):
    """--no-fix has no effect when the hook already passes."""
    write_config(
        '.', _local_hook(
            id='passing', name='Passing Hook',
            entry=_PASS, fix=_FAIL,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir),
        run_opts(files=('test.py',), no_fix=True),
    )

    assert ret == 0
    assert b'Passed' in printed


def test_fix_still_works_without_no_fix_flag(cap_out, store, in_git_dir):
    """Control test: fix runs normally when --no-fix is not set."""
    write_config(
        '.', _local_hook(
            id='failing', name='Failing Hook',
            entry=_FAIL, fix=_PASS,
        ),
    )
    open('test.py', 'w').close()

    ret, printed = _do_run(
        cap_out, store, str(in_git_dir),
        run_opts(files=('test.py',)),  # no_fix=False by default
    )

    assert ret == 0
    assert b'Fixed' in printed


# ---------------------------------------------------------------------------
# Verify: working-tree hash (not staged) drives cache
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cached-fail short-circuit and fix-on-commit
# ---------------------------------------------------------------------------

def test_cached_fail_no_fix_short_circuits(cap_out, store, in_git_dir):
    """2nd run: cached fail, no fix → (cached) Failed, no re-run."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))

    # First run: entry runs and fails, result cached
    ret1, printed1 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 1
    assert b'(cached)' not in printed1

    # Second run: cache hit on fail → short-circuit, no entry re-run
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 1
    assert b'(cached)' in printed2
    assert b'Failed' in printed2


def test_cached_fail_with_fix_runs_fix_directly(cap_out, store, in_git_dir):
    """Cached fail + fix passes → fix runs directly, shows Fixed."""
    # Step 1: run with no fix so the fail is cached with no fix involved
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))
    ret1, _ = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 1

    # Step 2: add fix field (hook_key doesn't include fix, so cache hit still)
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL, fix=_PASS,
        ),
    )
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 0
    assert b'Fixed' in printed2
    assert b'Failed' not in printed2


def test_cached_fail_with_fix_also_fails(cap_out, store, in_git_dir):
    """Cached fail + fix also fails → Failed."""
    # Step 1: cache the fail without running fix
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))
    ret1, _ = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 1

    # Step 2: add a fix that also fails
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL, fix=_FAIL,
        ),
    )
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 1
    assert b'Failed' in printed2
    assert b'Fixed' not in printed2


def test_cached_fail_no_fix_flag_skips_fix(cap_out, store, in_git_dir):
    """Cached fail + fix exists + --no-fix → (cached) Failed."""
    # Step 1: cache a fail
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')
    ret1, _ = _do_run(
        cap_out, store, str(in_git_dir), run_opts(files=('test.py',)),
    )
    assert ret1 == 1

    # Step 2: add fix + --no-fix → cached fail, fix skipped
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_FAIL, fix=_PASS,
        ),
    )
    ret2, printed2 = _do_run(
        cap_out, store, str(in_git_dir),
        run_opts(files=('test.py',), no_fix=True),
    )
    assert ret2 == 1
    assert b'(cached)' in printed2
    assert b'Failed' in printed2


def test_mix_cached_fail_and_uncached_entry_passes(
        cap_out, store, in_git_dir,
):
    """One cached-fail (fix runs), one uncached (entry passes)."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_PASS, fix=_PASS,
        ),
    )
    with open('pass.py', 'w') as fh:
        fh.write('x = 1\n')
    with open('fail.py', 'w') as fh:
        fh.write('y = 2\n')

    import os
    from pre_commit.commands.run import _file_hash, _compute_hook_key
    from pre_commit.clientlib import load_config
    from pre_commit.repository import all_hooks
    cfg = load_config('.pre-commit-config.yaml')
    hooks = list(all_hooks(cfg, store))
    repo_root = os.getcwd()
    for hook in hooks:
        hk = _compute_hook_key(hook)
        # Seed fail.py as cached-fail, leave pass.py uncached
        store.set_hook_result(
            hk, 'fail.py', _file_hash('fail.py'), 1, repo_root=repo_root,
        )

    args = run_opts(files=('pass.py', 'fail.py'))
    ret, printed = _do_run(cap_out, store, str(in_git_dir), args)

    # entry runs on pass.py (uncached), fix runs on fail.py (cached-fail)
    # both succeed → Fixed overall
    assert ret == 0
    assert b'Fixed' in printed


# ---------------------------------------------------------------------------
# Verify: working-tree hash (not staged) drives cache
# ---------------------------------------------------------------------------

def test_cache_uses_working_tree_hash_not_staged(cap_out, store, in_git_dir):
    """Cache uses on-disk file content hash, independent of git staging."""
    write_config(
        '.', _local_hook(
            id='checker', name='Checker', entry=_PASS,
        ),
    )
    with open('test.py', 'w') as fh:
        fh.write('x = 1\n')

    args = run_opts(files=('test.py',))

    # First run populates cache with current working-tree hash
    ret1, printed1 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret1 == 0
    assert b'(cached)' not in printed1

    # Second run — file unchanged on disk → cache hit (even though not staged)
    ret2, printed2 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret2 == 0
    assert b'(cached)' in printed2

    # Modify the file on disk (do NOT stage it) → hash changes → cache miss
    with open('test.py', 'w') as fh:
        fh.write('x = 99\n')  # changed but not staged

    ret3, printed3 = _do_run(cap_out, store, str(in_git_dir), args)
    assert ret3 == 0
    assert b'(cached)' not in printed3  # cache invalidated by on-disk change
