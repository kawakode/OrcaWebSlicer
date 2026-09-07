"""The OrcaWebSlicer API service.

The security-critical worker isolation lives in `scripts/web_worker_executor.py`
and `scripts/web_job_directory.py`, which this package imports rather than
reimplements. See `docs/web/adr/0002-web-stack.md`. Those modules are also
driven by the smoke and baseline services, so they stay outside this package and
are reached by putting `scripts/` on the import path here, once.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(REPO_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

API_VERSION = "v1"
PROTOCOL_VERSION = 1
