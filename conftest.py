"""Root conftest — ensures the repo root is on sys.path so ``import app.*``
works regardless of where pytest is invoked from, and resets the auth
module's cached key state between tests so one test's monkeypatching can't
leak into another.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest

from app import auth


@pytest.fixture(autouse=True)
def _reset_auth_key_cache():
    """Ensure every test starts with auth's module-level key cache unset."""
    auth._collector_keys = None
    auth._client_keys = None
    yield
    auth._collector_keys = None
    auth._client_keys = None
