"""Shared pytest configuration.

Kept deliberately thin. The application-level fixtures R-TEST-3 describes
(``AsyncClient`` + ``ASGITransport`` wrapped in ``LifespanManager``) arrive
with the real test suite; the structural migration only needs the smoke
gates, and those construct the application themselves.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
