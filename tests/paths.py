from __future__ import annotations

from pathlib import Path


# Tests that read committed files resolve them from here, so moving a test
# between subpackages does not change how deep it has to walk.
REPO_ROOT = Path(__file__).resolve().parents[1]
