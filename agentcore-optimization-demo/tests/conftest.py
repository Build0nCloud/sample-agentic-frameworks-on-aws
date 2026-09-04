"""Pytest configuration: make the src-layout package importable without install.

Allows `pytest` to run before an editable install (`pip install -e .`). Tests use
stubbed/mocked AWS clients only; nothing here touches real AWS.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
