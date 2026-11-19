"""Ensure topstep-chart-app root is importable regardless of pytest CWD."""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = str(Path(__file__).resolve().parents[2])
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
