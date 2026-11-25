"""Vercel entrypoint.

Vercel's FastAPI preset looks for a module-level `app` at one of these root files: app.py,
index.py, server.py, main.py, wsgi.py, asgi.py. `api/index.py` is not on that list — the /api
directory is for individual functions — which is why the first deployment built nothing.

The repository root is added to sys.path so `mmdesk` imports work regardless of how Vercel
places the file in the bundle.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mmdesk.service import app  # noqa: E402  (path setup must precede the import)

__all__ = ["app"]
