@echo off
echo ============================================
echo  NI-SCOPE MCP Server — Installation
echo ============================================
echo.

REM Find Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    where python3 >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Python not found. Install Python 3.11+ from https://python.org
        pause
        exit /b 1
    )
    set PYTHON=python3
) else (
    set PYTHON=python
)

echo Using: %PYTHON%
echo.

REM Install in development mode
echo [1/3] Installing niscope-mcp package...
%PYTHON% -m pip install -e .
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)

REM Install hardware support (optional, only if NI drivers present)
echo [2/3] Checking NI hardware support...
%PYTHON% -c "import niscope" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo   NI-SCOPE driver found — hardware support enabled.
) else (
    echo   NI-SCOPE driver not found. Use --backend mock for testing.
    echo   Install NI-SCOPE driver from https://ni.com for hardware support.
)

echo.
echo [3/3] Installation complete!
echo.
echo To add to your MCP client config (Claude Desktop / Cursor / etc):
echo.
echo {
echo   "mcpServers": {
echo     "niscope": {
echo       "command": "%PYTHON%",
echo       "args": ["-m", "niscope_mcp"]
echo     }
echo   }
echo }
echo.
echo For mock/testing mode: add "--backend" and "mock" to args.
echo.
echo Run manually: %PYTHON% -m niscope_mcp --backend mock
echo.
pause
