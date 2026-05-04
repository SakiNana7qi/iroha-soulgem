@echo off
setlocal

set SERVICE_NAME=SoulGemMonitor
set SERVICE_DISPLAY=SoulGem Server Monitor
set SCRIPT_DIR=%~dp0..
set SCRIPT_PATH=%SCRIPT_DIR%\main.py

for /f "delims=" %%i in ('where python') do (
    set PYTHON_EXE=%%i
    goto :found_python
)
echo [ERROR] python not found in PATH.
exit /b 1
:found_python
echo Using Python: %PYTHON_EXE%

where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] nssm not found in PATH.
    echo Download from https://nssm.cc/download and add to PATH.
    echo Or run: choco install nssm
    exit /b 1
)

echo.
echo Service will run as current user: %USERDOMAIN%\%USERNAME%
set /p SVC_PASSWORD=Enter password for %USERDOMAIN%\%USERNAME%:

echo.
echo Installing service: %SERVICE_NAME%
nssm install %SERVICE_NAME% "%PYTHON_EXE%" "%SCRIPT_PATH%"
nssm set %SERVICE_NAME% DisplayName "%SERVICE_DISPLAY%"
nssm set %SERVICE_NAME% AppDirectory "%SCRIPT_DIR%"
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppStdout "%SCRIPT_DIR%\logs\service.log"
nssm set %SERVICE_NAME% AppStderr "%SCRIPT_DIR%\logs\service.log"
nssm set %SERVICE_NAME% AppRotateFiles 1
nssm set %SERVICE_NAME% AppRotateBytes 10485760
nssm set %SERVICE_NAME% ObjectName "%USERDOMAIN%\%USERNAME%" "%SVC_PASSWORD%"

if not exist "%SCRIPT_DIR%\logs" mkdir "%SCRIPT_DIR%\logs"

echo Starting service...
nssm start %SERVICE_NAME%

echo.
echo Done. Service "%SERVICE_DISPLAY%" installed and started.
echo Running as: %USERDOMAIN%\%USERNAME%
echo Logs: %SCRIPT_DIR%\logs\service.log

pause
