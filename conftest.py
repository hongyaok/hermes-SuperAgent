"""Pytest bootstrap for running the suite from the project root.

The plugin root keeps ``__init__.py`` (Hermes requires it), so pytest treats
the root as a package and imports that ``__init__`` top-level during
collection. The sibling modules fall back to absolute imports in that case;
this file makes sure the project root is on ``sys.path`` so those resolve.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
