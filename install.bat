@echo off
chcp 65001 >nul
echo ============================================
echo   NI-SCOPE MCP Server v2 — Installation
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

REM Step 1: Install the package
echo [1/3] Installing niscope-mcp package...
%PYTHON% -m pip install -e .
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
echo   OK Package installed
echo.

REM Step 2: Install NI hardware driver
echo [2/3] Installing NI-SCOPE hardware driver (niscope)...
%PYTHON% -m pip install "niscope-mcp[hardware]"
if %ERRORLEVEL% EQU 0 (
    echo   OK niscope hardware driver installed
) else (
    echo   WARNING: niscope hardware driver install failed.
    echo   Run manually: %PYTHON% -m pip install "niscope-mcp[hardware]"
)
echo.

REM Step 3: Print config instructions
echo [3/3] Installation complete!
echo.
echo ============================================
echo   niscope-mcp v2.1 installed
echo ============================================
echo.
echo   IMPORTANT: Add this MCP entry to your AI assistant
echo   config, then RESTART the assistant.
echo.
echo   --- Proma / Claude Desktop / Cursor ---
echo   {
echo     "servers": {
echo       "niscope": {
echo         "type": "stdio",
echo         "command": "%PYTHON%",
echo         "args": ["-u", "-m", "niscope_mcp"],
echo         "enabled": true
echo       }
echo     }
echo   }
echo.
echo   First start: %PYTHON% -m niscope_mcp
echo   (auto-installs niscope if missing)
echo.
pause
