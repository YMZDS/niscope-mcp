@echo off
chcp 65001 >nul
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

REM Step 1: Install the package
echo [1/3] Installing niscope-mcp package...
%PYTHON% -m pip install -e .
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
echo   ✅ Package installed
echo.

REM Step 2: Install NI hardware driver (niscope)
echo [2/3] Installing NI-SCOPE hardware driver (niscope)...
%PYTHON% -m pip install "niscope-mcp[hardware]"
if %ERRORLEVEL% EQU 0 (
    echo   ✅ niscope hardware driver installed
) else (
    echo   ⚠️  niscope hardware driver install failed.
    echo      Run manually: %PYTHON% -m pip install "niscope-mcp[hardware]"
)
echo.

REM Step 3: Print config instructions
echo [3/3] Installation complete!
echo.
echo ============================================
echo  ✅ niscope-mcp package + hardware driver
echo ============================================
echo.
echo  IMPORTANT: Add the following MCP entry to your
echo  AI assistant config, then RESTART the assistant.
echo.
echo  --- Reasonix Desktop (config.json) ---
echo  "mcp": [
echo    "niscope=%PYTHON% -u -m niscope_mcp"
echo  ]
echo.
echo  --- Claude Desktop / Cursor ---
echo  "mcpServers": {
echo    "niscope": {
echo      "command": "%PYTHON%",
echo      "args": ["-u", "-m", "niscope_mcp"]
echo    }
echo  }
echo.
echo ============================================
echo.
echo  First start: %PYTHON% -m niscope_mcp
echo  (auto-installs niscope if missing)
echo.
pause
