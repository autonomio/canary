#!/usr/bin/env python3
"""Native coverage research and deterministic source tools for its single scientist."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from canary.coverage_runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
