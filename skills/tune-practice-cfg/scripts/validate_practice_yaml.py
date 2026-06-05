#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Practice YAML (replaces MCP yaml_validation_validate)."""

from __future__ import annotations

import argparse
import sys

from script_utils import add_common_lib, emit_result, ensure_msmodelslim

add_common_lib(__file__)
from yaml_validation_helpers import validate_practice_yaml  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Practice YAML against PracticeConfig.")
    parser.add_argument("--practice-path", required=True)
    args = parser.parse_args()

    ensure_msmodelslim()
    return emit_result(validate_practice_yaml(args.practice_path))


if __name__ == "__main__":
    sys.exit(main())
