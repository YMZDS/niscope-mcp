"""NI-SCOPE MCP Server — AI-controlled oscilloscope via MCP protocol.

Usage:
    python -m niscope_mcp                  # direct backend (NI hardware)
    python -m niscope_mcp --backend mock   # simulated devices

Or install and run:
    pip install -e .
    niscope-mcp --backend mock
"""

__version__ = "1.1.0"
