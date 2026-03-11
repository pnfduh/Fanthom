@echo off
chcp 65001 >nul 2>&1
echo Fathom Installer starting...
echo.

:: Check admin
net session >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Not running as Administrator
    echo Please right-click and Run as administrator
    pause
    exit /b 1
)
echo OK: Running as Administrator

:: Check drive
echo Drive: %~dp0
echo.

:: Find Python - skip Windows Store stub
echo Searching for Python...
SET REAL_PYTHON=

FOR %%P IN (
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python310\python.exe"
) DO (
    IF EXIST %%P (
        SET REAL_PYTHON=%%~P
        echo Found: %%~P
        GOTO :found_python
    )
)

:: Check PATH but skip WindowsApps
FOR /F "tokens=*" %%i IN ('where python 2^>nul') DO (
    echo Checking: %%i
    echo %%i | findstr /I "WindowsApps" >nul
    IF %ERRORLEVEL% NEQ 0 (
        SET REAL_PYTHON=%%i
        GOTO :found_python
    ) ELSE (
        echo Skipping Windows Store stub: %%i
    )
)

:: No Python found - download it
echo No real Python found. Downloading Python 3.12...
SET PY_INSTALLER=%TEMP%\python312.exe
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%PY_INSTALLER%' -UseBasicParsing"
IF NOT EXIST "%PY_INSTALLER%" (
    echo FAILED: Could not download Python
    echo Install manually from https://python.org then re-run
    pause
    exit /b 1
)
echo Installing Python 3.12...
"%PY_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
del "%PY_INSTALLER%" >nul 2>&1
SET REAL_PYTHON=C:\Program Files\Python312\python.exe
IF NOT EXIST "%REAL_PYTHON%" SET REAL_PYTHON=C:\Python312\python.exe
echo Python installed.

:: Remove Store stub
IF EXIST "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" (
    echo Removing Windows Store Python stub...
    del /F "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" >nul 2>&1
    del /F "%LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe" >nul 2>&1
)

:found_python
echo.
IF NOT EXIST "%REAL_PYTHON%" (
    echo ERROR: Python not found at %REAL_PYTHON%
    pause
    exit /b 1
)
echo Using Python: %REAL_PYTHON%

:: Get pythonw.exe
FOR %%F IN ("%REAL_PYTHON%") DO SET PY_DIR=%%~dpF
SET REAL_PYTHONW=%PY_DIR%pythonw.exe
IF NOT EXIST "%REAL_PYTHONW%" (
    echo pythonw.exe not found, using python.exe
    SET REAL_PYTHONW=%REAL_PYTHON%
)
echo Using Pythonw: %REAL_PYTHONW%
echo.

:: Install dependencies
echo Installing dependencies...
"%REAL_PYTHON%" -m pip install -r "%~dp0requirements-agent.txt" --upgrade
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
echo Dependencies installed.
echo.

:: Copy files
SET INSTALL_DIR=%APPDATA%\FathomAgent
echo Copying files to %INSTALL_DIR%...
IF NOT EXIST "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
xcopy /E /Y "%~dp0agent\*" "%INSTALL_DIR%\"
copy /Y "%~dp0requirements-agent.txt" "%INSTALL_DIR%\"
echo %REAL_PYTHONW%> "%INSTALL_DIR%\pythonw_path.txt"
echo Files copied.
echo.

:: Remove old installs
echo Cleaning up old installations...
schtasks /End /TN "FathomAgent" >nul 2>&1
schtasks /Delete /TN "FathomAgent" /F >nul 2>&1
sc stop FathomAgent >nul 2>&1
sc delete FathomAgent >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
echo Done.
echo.

:: Create scheduled task
echo Creating scheduled task...
schtasks /Create /TN "FathomAgent" /TR "\"%REAL_PYTHONW%\" \"%INSTALL_DIR%\watchdog.py\"" /SC ONLOGON /RL HIGHEST /F /IT /RU "%USERNAME%"
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Could not create scheduled task
    pause
    exit /b 1
)
echo Task created.
echo.

:: Start now
echo Starting agent...
schtasks /Run /TN "FathomAgent"
timeout /t 4 /nobreak >nul

tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | find /I "pythonw.exe" >nul
IF %ERRORLEVEL% EQU 0 (
    echo SUCCESS: Agent is running
) ELSE (
    echo NOTE: Agent may still be starting up
    echo Check logs at: %APPDATA%\FathomAgent\
)

echo.
echo ================================================
echo  Fathom installed successfully
echo  Logs: %APPDATA%\FathomAgent\watchdog.log
echo ================================================
echo.
pause
