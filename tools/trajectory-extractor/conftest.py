"""Make the extractor importable when tests run from the msagent repo root."""

import sys
from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)
