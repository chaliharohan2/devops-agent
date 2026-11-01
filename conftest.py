"""Workspace-wide pytest setup.

Hide the developer's global and system git config from every test so local runs
behave like a fresh CI runner. Without this, a ``user.name`` / ``user.email`` (or
``commit.gpgsign``) in ``~/.gitconfig`` can make tests pass locally that fail in
CI. Set at import time so it applies before any test module or fixture runs git.
"""

from __future__ import annotations

import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
