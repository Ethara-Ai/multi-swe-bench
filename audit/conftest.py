"""Make the flat audit modules importable by pytest.

The harness uses a flat layout (modules directly under audit/, imported by bare
name). This puts the audit/ directory on sys.path so `from models import ...`
resolves both under `uv run` and a bare `pytest`.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
