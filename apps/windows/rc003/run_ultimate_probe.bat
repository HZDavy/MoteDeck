@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo  RC003 ultimate probe wizard
echo  Quit the official bridge app first (tray - Exit),
echo  otherwise the remote is held by the bridge.
echo ==========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Create .venv in the rc003 folder and install requirements.txt first.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "src\rc003_ultimate_probe.py"
set EXIT_CODE=%errorlevel%

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Program exited with code %EXIT_CODE%
    pause
)

endlocal
exit /b %EXIT_CODE%
