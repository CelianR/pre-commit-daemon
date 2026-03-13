from __future__ import annotations

import importlib.metadata

CONFIG_FILE = '.pre-commit-config.yaml'
DAEMON_CONFIG_FILE = '.pre-commit-daemon-config.yaml'
MANIFEST_FILE = '.pre-commit-hooks.yaml'

# Bump when modifying `empty_template`
LOCAL_REPO_VERSION = '1'

VERSION = importlib.metadata.version('pre_commit')

DEFAULT = 'default'
