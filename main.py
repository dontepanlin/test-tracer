#!/usr/bin/env python3
"""
Convenience entry point: `python main.py <scenario args>`.

Prefer the `scenario` console command (installed into the venv) for daily use.
"""

import sys

from test_tracer.cli import main

if __name__ == "__main__":
    sys.exit(main())
