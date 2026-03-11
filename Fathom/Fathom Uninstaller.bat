@echo off
:: ══════════════════════════════════════════════════════════════════
::  Fathom — Task Scheduler Remover
::  Run as Administrator
:: ══════════════════════════════════════════════════════════════════

title Fathom Task Remover

net session >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo  [!] Run as Administrator required.
    pause & exit /b 1
)

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   Fathom Task Remover                  ║
echo  ╚══════════════════════════════════════════╝
echo.

echo  [*] Stopping agent...
schtasks /End /TN "FathomAgent" >nul 2>&1

echo  [*] Removing scheduled task...
schtasks /Delete /TN "FathomAgent" /F
IF %ERRORLEVEL% NEQ 0 (
    echo  [!] Task not found - may already be removed.
)

echo  [*] Killing any running agent processes...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq telemetry_agent*" >nul 2>&1

echo.
echo  [✓] Fathom removed.
echo      Log files kept at: %APPDATA%\FathomAgent\
echo.
pause
