from __future__ import annotations

import logging
import os.path
import shlex
import sqlite3
import stat
from unittest import mock

import pytest

import pre_commit.constants as C
from pre_commit import git
from pre_commit.store import _get_default_directory
from pre_commit.store import _LOCAL_RESOURCES
from pre_commit.store import Store
from pre_commit.util import CalledProcessError
from pre_commit.util import cmd_output
from testing.fixtures import git_dir
from testing.util import cwd
from testing.util import git_commit
from testing.util import xfailif_windows


def _select_all_configs(store: Store) -> list[str]:
    with store.connect() as db:
        rows = db.execute('SELECT * FROM configs').fetchall()
        return [path for path, in rows]


def _select_all_repos(store: Store) -> list[tuple[str, str, str]]:
    with store.connect() as db:
        return db.execute('SELECT repo, ref, path FROM repos').fetchall()


def test_our_session_fixture_works():
    """There's a session fixture which makes `Store` invariantly raise to
    prevent writing to the home directory.
    """
    with pytest.raises(AssertionError):
        Store()


def test_get_default_directory_defaults_to_home():
    # Not we use the module level one which is not mocked
    ret = _get_default_directory()
    expected = os.path.realpath(os.path.expanduser('~/.cache/pre-commit'))
    assert ret == expected


def test_adheres_to_xdg_specification():
    with mock.patch.dict(
        os.environ, {'XDG_CACHE_HOME': '/tmp/fakehome'},
    ):
        ret = _get_default_directory()
    expected = os.path.realpath('/tmp/fakehome/pre-commit')
    assert ret == expected


def test_uses_environment_variable_when_present():
    with mock.patch.dict(
        os.environ, {'PRE_COMMIT_HOME': '/tmp/pre_commit_home'},
    ):
        ret = _get_default_directory()
    expected = os.path.realpath('/tmp/pre_commit_home')
    assert ret == expected


def test_store_init(store):
    # Should create the store directory
    assert os.path.exists(store.directory)
    # Should create a README file indicating what the directory is about
    with open(os.path.join(store.directory, 'README')) as readme_file:
        readme_contents = readme_file.read()
        for text_line in (
            'This directory is maintained by the pre-commit project.',
            'Learn more: https://github.com/pre-commit/pre-commit',
        ):
            assert text_line in readme_contents


def test_clone(store, tempdir_factory, caplog):
    path = git_dir(tempdir_factory)
    with cwd(path):
        git_commit()
        rev = git.head_rev(path)
        git_commit()

    ret = store.clone(path, rev)
    # Should have printed some stuff
    assert caplog.record_tuples[0][-1].startswith(
        'Initializing environment for ',
    )

    # Should return a directory inside of the store
    assert os.path.exists(ret)
    assert ret.startswith(store.directory)
    # Directory should start with `repo`
    _, dirname = os.path.split(ret)
    assert dirname.startswith('repo')
    # Should be checked out to the rev we specified
    assert git.head_rev(ret) == rev

    # Assert there's an entry in the sqlite db for this
    assert _select_all_repos(store) == [(path, rev, ret)]


def test_warning_for_deprecated_stages_on_init(store, tempdir_factory, caplog):
    manifest = '''\
-   id: hook1
    name: hook1
    language: system
    entry: echo hook1
    stages: [commit, push]
-   id: hook2
    name: hook2
    language: system
    entry: echo hook2
    stages: [push, merge-commit]
'''

    path = git_dir(tempdir_factory)
    with open(os.path.join(path, C.MANIFEST_FILE), 'w') as f:
        f.write(manifest)
    cmd_output('git', 'add', '.', cwd=path)
    git_commit(cwd=path)
    rev = git.head_rev(path)

    store.clone(path, rev)
    assert caplog.record_tuples[1] == (
        'pre_commit',
        logging.WARNING,
        f'repo `{path}` uses deprecated stage names '
        f'(commit, push, merge-commit) which will be removed in a future '
        f'version.  '
        f'Hint: often `pre-commit autoupdate --repo {shlex.quote(path)}` '
        f'will fix this.  '
        f'if it does not -- consider reporting an issue to that repo.',
    )

    # should not re-warn
    caplog.clear()
    store.clone(path, rev)
    assert caplog.record_tuples == []


def test_no_warning_for_non_deprecated_stages_on_init(
        store, tempdir_factory, caplog,
):
    manifest = '''\
-   id: hook1
    name: hook1
    language: system
    entry: echo hook1
    stages: [pre-commit, pre-push]
-   id: hook2
    name: hook2
    language: system
    entry: echo hook2
    stages: [pre-push, pre-merge-commit]
'''

    path = git_dir(tempdir_factory)
    with open(os.path.join(path, C.MANIFEST_FILE), 'w') as f:
        f.write(manifest)
    cmd_output('git', 'add', '.', cwd=path)
    git_commit(cwd=path)
    rev = git.head_rev(path)

    store.clone(path, rev)
    assert logging.WARNING not in {tup[1] for tup in caplog.record_tuples}


def test_clone_cleans_up_on_checkout_failure(store):
    with pytest.raises(Exception) as excinfo:
        # This raises an exception because you can't clone something that
        # doesn't exist!
        store.clone('/i_dont_exist_lol', 'fake_rev')
    assert '/i_dont_exist_lol' in str(excinfo.value)

    repo_dirs = [
        d for d in os.listdir(store.directory) if d.startswith('repo')
    ]
    assert repo_dirs == []


def test_clone_when_repo_already_exists(store):
    # Create an entry in the sqlite db that makes it look like the repo has
    # been cloned.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            'INSERT INTO repos (repo, ref, path) '
            'VALUES ("fake_repo", "fake_ref", "fake_path")',
        )

    assert store.clone('fake_repo', 'fake_ref') == 'fake_path'


def test_clone_shallow_failure_fallback_to_complete(
    store, tempdir_factory,
    caplog,
):
    path = git_dir(tempdir_factory)
    with cwd(path):
        git_commit()
        rev = git.head_rev(path)
        git_commit()

    # Force shallow clone failure
    def fake_shallow_clone(self, *args, **kwargs):
        raise CalledProcessError(1, (), b'', None)
    store._shallow_clone = fake_shallow_clone

    ret = store.clone(path, rev)

    # Should have printed some stuff
    assert caplog.record_tuples[0][-1].startswith(
        'Initializing environment for ',
    )

    # Should return a directory inside of the store
    assert os.path.exists(ret)
    assert ret.startswith(store.directory)
    # Directory should start with `repo`
    _, dirname = os.path.split(ret)
    assert dirname.startswith('repo')
    # Should be checked out to the rev we specified
    assert git.head_rev(ret) == rev

    # Assert there's an entry in the sqlite db for this
    assert _select_all_repos(store) == [(path, rev, ret)]


def test_clone_tag_not_on_mainline(store, tempdir_factory):
    path = git_dir(tempdir_factory)
    with cwd(path):
        git_commit()
        cmd_output('git', 'checkout', 'master', '-b', 'branch')
        git_commit()
        cmd_output('git', 'tag', 'v1')
        cmd_output('git', 'checkout', 'master')
        cmd_output('git', 'branch', '-D', 'branch')

    # previously crashed on unreachable refs
    store.clone(path, 'v1')


def test_create_when_directory_exists_but_not_db(store):
    # In versions <= 0.3.5, there was no sqlite db causing a need for
    # backward compatibility
    os.remove(store.db_path)
    store = Store(store.directory)
    assert os.path.exists(store.db_path)


# ---------------------------------------------------------------------------
# hook_results cache
# ---------------------------------------------------------------------------

def test_hook_result_miss(store):
    """Cache miss returns None for an entry that was never written."""
    assert store.get_hook_result('k', 'f.py', 'abc') is None


def test_hook_result_pass(store):
    """Stored pass result (0) is returned on exact hit."""
    store.set_hook_result('k', 'f.py', 'abc', 0)
    assert store.get_hook_result('k', 'f.py', 'abc') == 0


def test_hook_result_fail(store):
    """Stored fail result (1) is returned on exact hit."""
    store.set_hook_result('k', 'f.py', 'abc', 1)
    assert store.get_hook_result('k', 'f.py', 'abc') == 1


def test_hook_result_hash_mismatch(store):
    """Cache miss when hash differs — file content changed."""
    store.set_hook_result('k', 'f.py', 'old_hash', 0)
    assert store.get_hook_result('k', 'f.py', 'new_hash') is None


def test_hook_result_overwrite(store):
    """Same (hook_key, file_path) with a new hash replaces the row."""
    store.set_hook_result('k', 'f.py', 'h', 0)
    store.set_hook_result('k', 'f.py', 'h', 1)
    assert store.get_hook_result('k', 'f.py', 'h') == 1


def test_hook_result_multiple_hooks_independent(store):
    """Different hook_keys are stored independently."""
    store.set_hook_result('hook-a', 'f.py', 'h', 0)
    store.set_hook_result('hook-b', 'f.py', 'h', 1)
    assert store.get_hook_result('hook-a', 'f.py', 'h') == 0
    assert store.get_hook_result('hook-b', 'f.py', 'h') == 1


def test_hook_result_multiple_files_independent(store):
    """Different file paths under the same hook_key are independent."""
    store.set_hook_result('k', 'a.py', 'h', 0)
    store.set_hook_result('k', 'b.py', 'h', 1)
    assert store.get_hook_result('k', 'a.py', 'h') == 0
    assert store.get_hook_result('k', 'b.py', 'h') == 1


def test_hook_result_persists_across_store_instances(tempdir_factory):
    """Results survive across separate Store instances (same db file)."""
    path = os.path.join(tempdir_factory.get(), '.pre-commit')
    s1 = Store(path)
    s1.set_hook_result('k', 'f.py', 'h', 0)

    s2 = Store(path)
    assert s2.get_hook_result('k', 'f.py', 'h') == 0


def test_hook_result_readonly_store_is_noop(tempdir_factory):
    """set_hook_result on a readonly store silently does nothing."""
    path = os.path.join(tempdir_factory.get(), '.pre-commit')
    store = Store(path)
    store.readonly = True  # readonly is a plain instance attribute
    store.set_hook_result('k', 'f.py', 'h', 0)
    # Write was skipped → cache miss
    assert store.get_hook_result('k', 'f.py', 'h') is None


# ---------------------------------------------------------------------------
# purge_stale_hook_results
# ---------------------------------------------------------------------------

def test_purge_stale_hook_results_removes_matching_keys(store):
    store.set_hook_result('hook-a', 'f.py', 'h', 0)
    store.set_hook_result('hook-b', 'f.py', 'h', 0)
    store.purge_stale_hook_results({'hook-a'})
    assert store.get_hook_result('hook-a', 'f.py', 'h') is None
    assert store.get_hook_result('hook-b', 'f.py', 'h') == 0


def test_purge_stale_hook_results_empty_set_is_noop(store):
    store.set_hook_result('hook-a', 'f.py', 'h', 0)
    store.purge_stale_hook_results(set())
    assert store.get_hook_result('hook-a', 'f.py', 'h') == 0


def test_purge_stale_hook_results_unknown_keys_ok(store):
    """Purging keys that don't exist should not raise."""
    store.purge_stale_hook_results({'nonexistent-key'})


def test_purge_stale_hook_results_multiple_files(store):
    store.set_hook_result('hook-a', 'a.py', 'h', 0)
    store.set_hook_result('hook-a', 'b.py', 'h', 0)
    store.set_hook_result('hook-b', 'a.py', 'h', 0)
    store.purge_stale_hook_results({'hook-a'})
    assert store.get_hook_result('hook-a', 'a.py', 'h') is None
    assert store.get_hook_result('hook-a', 'b.py', 'h') is None
    assert store.get_hook_result('hook-b', 'a.py', 'h') == 0


# ---------------------------------------------------------------------------
# repo_root isolation
# ---------------------------------------------------------------------------

def test_hook_result_isolated_by_repo_root(store):
    """Same hook_key+file_path+hash in two repos are independent."""
    store.set_hook_result('k', 'src/main.py', 'h', 0, repo_root='/repo/a')
    # repo/b doesn't see repo/a's result
    assert store.get_hook_result(
        'k', 'src/main.py', 'h', repo_root='/repo/b',
    ) is None
    # repo/a still has its result
    assert store.get_hook_result(
        'k', 'src/main.py', 'h', repo_root='/repo/a',
    ) == 0


def test_hook_result_default_repo_root_is_isolated(store):
    """Empty repo_root (default) is its own namespace, distinct from named."""
    store.set_hook_result('k', 'f.py', 'h', 0, repo_root='')
    assert store.get_hook_result(
        'k', 'f.py', 'h', repo_root='/repo/a',
    ) is None
    assert store.get_hook_result('k', 'f.py', 'h', repo_root='') == 0


def test_purge_stale_isolated_by_repo_root(store):
    """purge_stale_hook_results only removes entries for the given repo."""
    store.set_hook_result('hook-a', 'f.py', 'h', 0, repo_root='/repo/a')
    store.set_hook_result('hook-a', 'f.py', 'h', 0, repo_root='/repo/b')
    store.purge_stale_hook_results({'hook-a'}, repo_root='/repo/a')
    assert store.get_hook_result(
        'hook-a', 'f.py', 'h', repo_root='/repo/a',
    ) is None
    assert store.get_hook_result(
        'hook-a', 'f.py', 'h', repo_root='/repo/b',
    ) == 0


def test_create_when_store_already_exists(store):
    # an assertion that this is idempotent and does not crash
    Store(store.directory)


def test_db_repo_name(store):
    assert store.db_repo_name('repo', ()) == 'repo'
    assert store.db_repo_name('repo', ('b', 'a', 'c')) == 'repo:b,a,c'


def test_local_resources_reflects_reality():
    on_disk = {
        res.removeprefix('empty_template_')
        for res in os.listdir('pre_commit/resources')
        if res.startswith('empty_template_')
    }
    assert on_disk == {os.path.basename(x) for x in _LOCAL_RESOURCES}


def test_mark_config_as_used(store, tmpdir):
    with tmpdir.as_cwd():
        f = tmpdir.join('f').ensure()
        store.mark_config_used('f')
        assert _select_all_configs(store) == [f.strpath]


def test_mark_config_as_used_idempotent(store, tmpdir):
    test_mark_config_as_used(store, tmpdir)
    test_mark_config_as_used(store, tmpdir)


def test_mark_config_as_used_does_not_exist(store):
    store.mark_config_used('f')
    assert _select_all_configs(store) == []


def test_mark_config_as_used_roll_forward(store, tmpdir):
    with store.connect() as db:  # simulate pre-1.14.0
        db.executescript('DROP TABLE configs')
    test_mark_config_as_used(store, tmpdir)


@xfailif_windows  # pragma: win32 no cover
def test_mark_config_as_used_readonly(tmpdir):
    cfg = tmpdir.join('f').ensure()
    store_dir = tmpdir.join('store')
    # make a store, then we'll convert its directory to be readonly
    assert not Store(str(store_dir)).readonly  # directory didn't exist
    assert not Store(str(store_dir)).readonly  # directory did exist

    def _chmod_minus_w(p):
        st = os.stat(p)
        os.chmod(p, st.st_mode & ~(stat.S_IWUSR | stat.S_IWOTH | stat.S_IWGRP))

    _chmod_minus_w(store_dir)
    for fname in os.listdir(store_dir):
        assert not os.path.isdir(fname)
        _chmod_minus_w(os.path.join(store_dir, fname))

    store = Store(str(store_dir))
    assert store.readonly
    # should be skipped due to readonly
    store.mark_config_used(str(cfg))
    assert _select_all_configs(store) == []


def test_clone_with_recursive_submodules(store, tmp_path):
    sub = tmp_path.joinpath('sub')
    sub.mkdir()
    sub.joinpath('submodule').write_text('i am a submodule')
    cmd_output('git', '-C', str(sub), 'init', '.')
    cmd_output('git', '-C', str(sub), 'add', '.')
    git.commit(str(sub))

    repo = tmp_path.joinpath('repo')
    repo.mkdir()
    repo.joinpath('repository').write_text('i am a repo')
    cmd_output('git', '-C', str(repo), 'init', '.')
    cmd_output('git', '-C', str(repo), 'add', '.')
    cmd_output('git', '-C', str(repo), 'submodule', 'add', str(sub), 'sub')
    git.commit(str(repo))

    rev = git.head_rev(str(repo))
    ret = store.clone(str(repo), rev)

    assert os.path.exists(ret)
    assert os.path.exists(os.path.join(ret, str(repo), 'repository'))
    assert os.path.exists(os.path.join(ret, str(sub), 'submodule'))
