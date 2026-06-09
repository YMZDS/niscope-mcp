"""NI-SCOPE MCP Server — AI-controlled oscilloscope via MCP protocol.

Usage:
    python -m niscope_mcp           # direct backend (NI hardware required)
    python -m niscope_mcp --check   # check system readiness
    python -m niscope_mcp --setup   # guided auto-setup

Or install and run:
    pip install -e .
    niscope-mcp
"""

__version__ = "1.2.0"
