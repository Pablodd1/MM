"""Vercel entrypoint.

The service is a normal ASGI app; Vercel's Python runtime looks for a module-level
`app`. The repository root is added to sys.path because Vercel builds from this file's
directory and the package lives one level up.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mmdesk.service import app  # noqa: E402  (path setup must precede the import)

__all__ = ["app"]
