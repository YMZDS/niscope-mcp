#!/usr/bin/env python3
"""Compatibility wrapper — delegates to niscope_mcp package.

Usage (legacy):
    python niscope-mcp/server.py
    python niscope-mcp/server.py --check

Preferred:
    python -m niscope_mcp
"""

import sys
import os

# Ensure the parent dir is on path for the niscope_mcp import
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from niscope_mcp.__main__ import main

if __name__ == "__main__":
    main()
