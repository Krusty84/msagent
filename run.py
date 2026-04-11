#!/usr/bin/env python3
"""Simple launcher for the msagent CLI."""

import sys
from pathlib import Path

src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from msagent.cli.bootstrap.app import cli

if __name__ == "__main__":
    cli()
